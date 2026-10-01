"""Selected-source publication drafts with bounded, durable factual checks and one repair."""
from __future__ import annotations

import asyncio
import hashlib
import math
import time
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from digest.config import Config, ProviderConfig, ReviewModelConfig
from digest.editorial_state import (
    Attempt,
    EditorialState,
    PublicationWork,
    content_hash,
    load_state,
    publication_binding,
    publication_draft_id,
    read_body,
    source_spans,
    store_state,
    utc_now,
)
from digest.editorial_worker import _acquire
from digest.enrichment_tokens import InputCount, TokenProfileUnavailable, count_input
from digest.llm import LLMProviderError, LLMRole, ProviderResponseDiagnostics, _reasoning_options, complete
from digest.publication_contract import (
    PROMPT_VERSION,
    FactualAudit,
    PublicationClaim,
    PublicationDraft,
    audit_complete,
    messages,
    parse_claims,
    parse_verdicts,
)
from digest.publication_pacing import next_request_time, timestamp

TOKEN_LIMIT = 8000
TOKEN_RESERVE = 512


@dataclass
class PublicationPassResult:
    state: EditorialState
    calls: int
    stop_reason: str


def _after(seconds: float) -> str:
    return datetime.fromtimestamp(time.time() + seconds, UTC).isoformat()


def _count(prompt: list[dict[str, str]], route: ReviewModelConfig, config: Config, cap: int) -> InputCount:
    assert config.enrichment is not None
    counted = count_input(prompt, route.provider, route.model, Path(config.enrichment.tokenizer_cache))
    if counted.tokens + cap + TOKEN_RESERVE > TOKEN_LIMIT:
        raise TokenProfileUnavailable("complete_source_request_envelope_pending")
    return counted


def _plan(claims: tuple[PublicationClaim, ...], table: list[dict[str, str]],
          config: Config) -> tuple[tuple[str, ...], ...]:
    settings = config.enrichment
    assert settings is not None

    def fits(items: tuple[PublicationClaim, ...]) -> bool:
        try:
            _count(messages(table, items), settings.verifier, config, settings.verifier_output_tokens)
            return True
        except TokenProfileUnavailable as exc:
            if str(exc) != "complete_source_request_envelope_pending":
                raise
            return False

    if fits(claims):
        return (tuple(claim.claim_id for claim in claims),)
    for split in range(1, len(claims)):
        if fits(claims[:split]) and fits(claims[split:]):
            return (tuple(claim.claim_id for claim in claims[:split]),
                    tuple(claim.claim_id for claim in claims[split:]))
    raise TokenProfileUnavailable("complete_source_check_plan_pending")


def current_work(article_id: str, state: EditorialState, config: Config) -> PublicationWork | None:
    article = state.articles[article_id]
    settings = config.enrichment
    if settings is None or article.body_sha256 is None:
        return None
    binding = publication_binding(article.body_sha256, PROMPT_VERSION, config.radar.language,
                                  settings.writer.provider, settings.writer.model,
                                  settings.verifier.provider, settings.verifier.model)
    return article.publications.get(binding)


def _ensure_work(article_id: str, state: EditorialState, config: Config) -> PublicationWork:
    article = state.articles[article_id]
    settings = config.enrichment
    assert settings is not None and article.body_sha256 is not None
    work = current_work(article_id, state, config)
    if work is None:
        binding = publication_binding(article.body_sha256, PROMPT_VERSION, config.radar.language,
                                      settings.writer.provider, settings.writer.model,
                                      settings.verifier.provider, settings.verifier.model)
        work = PublicationWork(binding, article.body_sha256, PROMPT_VERSION, config.radar.language,
                               settings.writer.provider, settings.writer.model,
                               settings.verifier.provider, settings.verifier.model)
        article.publications[binding] = work
    return work


def _next_stage(work: PublicationWork) -> str | None:
    if work.outcome != "pending":
        return None
    if not work.drafts:
        return "draft"
    draft = work.drafts[-1]
    if not draft.claims:
        work.outcome = "abstained"
        return None
    if len(work.audits) < len(work.drafts):
        return "plan"
    audit = work.audits[-1]
    if not audit_complete(draft, audit):
        return "check"
    if any(item.verdict == "unresolved" for item in audit.verdicts):
        work.last_error = "factual_check_unresolved"
        return None
    if any(item.verdict != "supported" for item in audit.verdicts):
        if work.repair_round:
            if len(work.drafts) == 1:
                if _repair_transport_retry(work):
                    return "repair"
                work.last_error = "reserved_repair_incomplete"
            else:
                work.outcome = "rejected"
            return None
        return "repair"
    work.outcome = "model_checked"
    return None


def _repair_transport_retry(work: PublicationWork) -> bool:
    attempts = [attempt for attempt in work.attempts if attempt.stage == "repair"]
    return (len(work.drafts) == 1 and len(attempts) == 1 and attempts[0].status == "failed"
            and attempts[0].response_sha256 is None and attempts[0].provider_diagnostics is not None
            and attempts[0].provider_diagnostics.status_code in {429, 503})


def _failure_cooldown(exc: LLMProviderError) -> float:
    diagnostic = exc.diagnostics
    fallback = 86400.0 if diagnostic and diagnostic.quota_axis in {"requests_per_day", "tokens_per_day"} else 3600.0
    if diagnostic and diagnostic.retry_after is not None:
        return max(1.0, timestamp(diagnostic.retry_after.server_retry_at) - time.time())
    return fallback


async def _request(work: PublicationWork, stage: str, table: list[dict[str, str]], state: EditorialState,
                   state_dir: Path, config: Config, deadline: float, attempted: set[str]) -> tuple[bool, float | None]:
    settings = config.enrichment
    assert settings is not None
    writing = stage in {"draft", "repair"}
    route = settings.writer if writing else settings.verifier
    cap = settings.writer_output_tokens if writing else settings.verifier_output_tokens
    checked: tuple[PublicationClaim, ...] = ()
    if stage == "check":
        audit = work.audits[-1]
        batch = set(audit.batches[audit.completed_batches])
        checked = tuple(claim for claim in work.drafts[-1].claims if claim.claim_id in batch)
        prompt = messages(table, checked)
    elif stage == "repair":
        prompt = messages(table, language=work.language, repair=work.drafts[0],
                          feedback=[item for item in work.audits[0].verdicts if item.verdict != "supported"])
    else:
        prompt = messages(table, language=work.language)
    count = _count(prompt, route, config, cap)
    prompt_hash = content_hash(prompt)
    key = content_hash([work.binding, stage, len(work.drafts), len(work.audits), prompt_hash])
    if key in attempted:
        return False, None
    previous = next((item for item in reversed(work.attempts) if item.task_key == key), None)
    retry = (_repair_transport_retry(work) if stage == "repair" else
             previous is not None and previous.status == "failed" and previous.provider_diagnostics
             and previous.provider_diagnostics.status_code in {429, 500, 502, 503, 504})
    if previous is not None and not retry:
        work.last_error = "previous_attempt_held_without_replay"
        return False, None
    allowed, work.wait_reason = next_request_time(state, config, route, count.tokens + cap + TOKEN_RESERVE)
    if not math.isfinite(allowed):
        work.last_error = "observed_quota_request_pending"
        return False, None
    if allowed > time.time():
        work.blocked_until = datetime.fromtimestamp(allowed, UTC).isoformat()
        return False, allowed
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False, None
    if stage == "repair":
        if work.repair_round and not _repair_transport_retry(work):
            raise ValueError("A publication permits at most one correction")
        if not work.repair_round:
            work.repair_round = 1
            work.repair_binding = content_hash([work.binding, work.drafts[0].draft_id, asdict(work.audits[0]), 1])
    attempt = Attempt(content_hash([key, len(work.attempts)]), stage, key, prompt_hash, utc_now(), input_count=count)
    work.attempts.append(attempt)
    attempted.add(key)
    work.blocked_until = None
    # This reservation precedes provider contact; interruption never silently repeats it.
    store_state(state, state_dir)
    single = replace(config, llm=replace(config.llm, max_retries=0, min_request_interval_seconds=0))
    text = ""
    try:
        text, usage = await asyncio.wait_for(complete(
            LLMRole.REVIEW_EVIDENCE, prompt, single, temperature=0,
            provider_override=ProviderConfig(route.provider, route.model), max_output_tokens=cap,
            reasoning_effort="medium" if not writing else None, include_reasoning=False if not writing else None,
        ), timeout=remaining)
        attempt.usage = {key: value for key, value in usage.items()
                         if key in {"prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens"}
                         and type(value) is int and value >= 0}
        actual = attempt.usage.get("prompt_tokens")
        if actual is not None:
            attempt.usage["input_delta"] = actual - count.tokens
        observation = usage.get("provider_diagnostics")
        if isinstance(observation, ProviderResponseDiagnostics):
            attempt.provider_diagnostics = observation
        attempt.response_sha256 = hashlib.sha256(text.encode()).hexdigest()
        if usage.get("finish_reason") not in {"stop", "STOP"}:
            raise ValueError("incomplete_completion")
        if actual is not None and actual > count.tokens + TOKEN_RESERVE:
            raise ValueError("input_count_reserve_exceeded")
        ids = {item["source_id"] for item in table}
        if writing:
            claims = parse_claims(text, ids)
            attempt.parsed_result_sha256 = content_hash([asdict(claim) for claim in claims])
            draft = PublicationDraft("", claims, prompt_hash, attempt.response_sha256, utc_now(), attempt.usage)
            work.drafts.append(replace(draft, draft_id=publication_draft_id(draft)))
        else:
            verdicts = parse_verdicts(text, checked, ids)
            attempt.parsed_result_sha256 = content_hash([asdict(item) for item in verdicts])
            work.audits[-1].verdicts.extend(verdicts)
            work.audits[-1].completed_batches += 1
        attempt.status = "success"
        work.last_error = ""
    except LLMProviderError as exc:
        attempt.status, attempt.error = "failed", "provider_failed"
        attempt.provider_diagnostics = exc.diagnostics
        attempt.retry_at = work.blocked_until = _after(_failure_cooldown(exc))
        state.provider_unavailable_until[route.provider] = work.blocked_until
        work.last_error = "provider_failed; saved cooldown, no retry in this pass"
    except TimeoutError:
        attempt.status, attempt.error = "unknown", "deadline_during_inference"
        work.outcome, work.last_error = "unknown", "uncertain_inference_held_without_replay"
    except (ValueError, TypeError, KeyError) as exc:
        attempt.status, attempt.error = "failed", type(exc).__name__
        attempt.rejected_output = text[:16000] if text else None
        work.last_error = "invalid_or_incomplete_response_held"
    store_state(state, state_dir)
    return True, None


async def run_publication_pass(config: Config, state_dir: Path, *, deadline_seconds: float = 180,
                               max_calls: int = 4) -> PublicationPassResult:
    if (config.enrichment is None or not math.isfinite(deadline_seconds) or deadline_seconds <= 0
            or type(max_calls) is not int or max_calls < 0):
        raise ValueError("Configure explicit enrichment writer/verifier routes and a finite execution allowance")
    state = load_state(state_dir)
    for article in state.articles.values():
        for work in article.publications.values():
            for attempt in work.attempts:
                if attempt.status == "started":
                    attempt.status, attempt.error = "unknown", "interrupted_inference"
                    work.outcome, work.last_error = "unknown", "uncertain_inference_held_without_replay"
    store_state(state, state_dir)
    deadline, calls = time.monotonic() + deadline_seconds, 0
    attempted: set[str] = set()
    reason = "no_runnable_work"
    while state.order and calls < max_calls and time.monotonic() < deadline:
        progress = False
        waiting = []
        cursor = state.cursor % len(state.order)
        for identity in state.order[cursor:] + state.order[:cursor]:
            article = state.articles[identity]
            if article.body_sha256 is None:
                retry = timestamp(article.acquisition_retry_at)
                if retry > time.time():
                    waiting.append(retry)
                    continue
                await _acquire(article, state, state_dir, deadline - time.monotonic())
                progress = True
            else:
                work = _ensure_work(identity, state, config)
                blocked = timestamp(work.blocked_until)
                if blocked > time.time():
                    waiting.append(blocked)
                    continue
                body = read_body(state_dir, article.body_sha256)
                table = [{"source_id": f"S{i}", "text": span.quote}
                         for i, span in enumerate(source_spans(article.chunks, body))]
                stage = _next_stage(work)
                if stage is None:
                    continue
                try:
                    # Validate verifier assets, route parameters and the irreducible full-source
                    # envelope before spending a writer request. Never fetch a tokenizer here.
                    verifier = config.enrichment.verifier
                    try:
                        _reasoning_options(verifier.provider, verifier.model, "medium", False)
                    except ValueError as exc:
                        raise TokenProfileUnavailable("unsupported_verifier_reasoning_profile") from exc
                    _count(messages(table, ()), verifier, config, config.enrichment.verifier_output_tokens)
                    if stage == "plan":
                        draft = work.drafts[-1]
                        work.audits.append(FactualAudit(draft.draft_id, _plan(draft.claims, table, config)))
                        progress = True
                    elif stage is not None:
                        called, eligible = await _request(work, stage, table, state, state_dir,
                                                          config, deadline, attempted)
                        calls += int(called)
                        progress = called
                        if eligible is not None:
                            waiting.append(eligible)
                except TokenProfileUnavailable as exc:
                    work.last_error = str(exc)
            if progress:
                state.cursor = (state.order.index(identity) + 1) % len(state.order)
                break
        store_state(state, state_dir)
        if progress:
            continue
        delay = min(waiting) - time.time() if waiting else 0
        if 0 < delay < deadline - time.monotonic():
            await asyncio.sleep(delay)
            continue
        reason = "cooldown" if waiting else "no_runnable_work"
        break
    if calls >= max_calls:
        reason = "request_allowance"
    elif time.monotonic() >= deadline:
        reason = "deadline"
    # Derive completed/failed outcomes even when the last allowed request finished the audit.
    for article in state.articles.values():
        final_work = current_work(article.article_id, state, config)
        if final_work is not None:
            _next_stage(final_work)
    store_state(state, state_dir)
    return PublicationPassResult(state, calls, reason)
