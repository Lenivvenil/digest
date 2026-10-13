"""One optional comparison bound to accepted RSS evidence and retained local state."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import time
from copy import copy
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, cast

from digest._serialization import canonical_json_bytes
from digest.adapters.models.execution import ModelExecution
from digest.adapters.models.synthesis import groq_synthesis_response_format
from digest.adapters.storage import synthesis as storage
from digest.adapters.storage.candidate_objects import read_packet
from digest.config import ProviderConfig
from digest.domain.catalog.articles import article_hash
from digest.domain.catalog.occurrences import occurrence_sha256
from digest.domain.editorial.synthesis import (
    EvidenceBinding,
    SynthesisResult,
    build_messages,
    validate_request_bounds,
    validate_response,
)
from digest.llm import LLMRole, complete, openai_request_body, request_budget_remaining, request_wait_seconds
from digest.preparation import AcceptedPreparation
from digest.translation import protected_content_equal

logger = logging.getLogger(__name__)
_ROUTE = ("groq", "openai/gpt-oss-120b")
_TERMINAL_FIELDS = {
    "status", "started_at", "completed_at", "response_sha256", "response_text", "reported_model",
    "finish_reason", "usage", "result", "result_sha256", "reason", "sha256",
}


class RequestAdmissionError(ValueError):
    """The complete optional request cannot fit its fixed local envelope."""


@dataclass(frozen=True)
class SynthesisOutcome:
    """Optional captured interpretation; never article selection or delivery authority."""

    status: str
    reason: str
    result: SynthesisResult | None = None
    bindings: tuple[EvidenceBinding, ...] = ()
    request_sha256: str | None = None

    def metadata(self) -> dict[str, Any]:
        value: dict[str, Any] = {"status": self.status, "reason": self.reason}
        if self.result is not None:
            value.update(
                result=asdict(self.result), bindings=[asdict(binding) for binding in self.bindings],
                request_sha256=self.request_sha256,
            )
        return value


def synthesis_enabled(config: Any) -> bool:
    return getattr(getattr(config, "synthesis", None), "enabled", False) is True


def optional_deadline() -> float | None:
    """Convert the managed wall deadline once; the 60-second window includes waiting."""
    raw = os.environ.get("PREPARATION_DEADLINE", "")
    if not raw or not raw.isdecimal() or len(raw) > 20:
        return None
    now = time.monotonic()
    remaining = int(raw) - time.time() - 45.0
    if not math.isfinite(remaining) or remaining <= 0:
        return None
    return now + min(60.0, remaining)


def _instant() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_bindings(
    accepted: AcceptedPreparation, cache_dir: str | Path,
) -> tuple[str, str, tuple[EvidenceBinding, ...]]:
    """Read all selected main evidence in accepted order from the immutable packet."""
    snapshot, report = accepted.snapshot, accepted.snapshot.review_report
    if report is None or report.evidence.evidence_kind != "sanitized_rss_excerpt":
        raise ValueError("Synthesis requires retained RSS review evidence.")
    if not 2 <= len(snapshot.top_articles) <= 5:
        raise ValueError("Synthesis requires two to five selected main items.")
    report_sha = hashlib.sha256(canonical_json_bytes(asdict(report))).hexdigest()
    packet = read_packet(report_sha, cache_dir)
    if packet.report != report or packet.evidence != report.evidence:
        raise ValueError("Synthesis report or bundle differs from accepted evidence.")
    bindings = []
    for card in snapshot.top_articles:
        identity = article_hash(card.title, card.link)
        evidence = [item for item in packet.evidence.items if item.evidence_id == identity]
        occurrences = [item for item in packet.articles if article_hash(item.title, item.link) == identity]
        if len(evidence) != 1 or len(occurrences) != 1:
            raise ValueError("Synthesis requires a unique retained main occurrence.")
        occurrence = occurrences[0]
        bindings.append(EvidenceBinding(
            evidence[0], occurrence_sha256(occurrence), occurrence.source, occurrence.source_url,
        ))
    if len({item.evidence.evidence_id for item in bindings}) != len(bindings):
        raise ValueError("Synthesis selected identities are ambiguous.")
    if len({(item.source, item.source_url) for item in bindings}) < 2:
        raise ValueError("Synthesis requires two retained source identities.")
    return report_sha, report.evidence.bundle_id, tuple(bindings)


def _supported(config: Any) -> bool:
    return bool(
        config.review.enabled and config.review.review_led_only
        and config.radar.language == "en" and config.telegram.delivery_mode == "compact"
        and not getattr(getattr(config, "reading_brief", None), "enabled", False)
    )


def _language(config: Any) -> str:
    return cast(str, config.translation.target_language if config.translation.enabled else config.radar.language)


def _skip(record: dict[str, Any], reason: str, cache_dir: str | Path) -> SynthesisOutcome:
    """Record omission exclusively if possible; an existing attempt always wins."""
    try:
        from digest._serialization import restore_dataclass

        bindings = restore_dataclass(record["bindings"], tuple[EvidenceBinding, ...])
        skipped = storage.new_record(
            accepted_sha256=record["accepted_sha256"], publication_date=record["publication_date"],
            report_sha256=record["report_sha256"], bundle_id=record["bundle_id"], bindings=bindings,
            publication_language=record["publication_language"], request_body=record["request_body"],
            status="skipped", started_at=_instant(), reason=reason,
        )
        storage.reserve_attempt(skipped, cache_dir)
    except (OSError, ValueError, TypeError):
        logger.info("Optional synthesis skip could not be retained; existing attempt remains authoritative.")
    return SynthesisOutcome("skipped", reason)


def _held(config: Any, execution: ModelExecution) -> bool:
    return execution.request_state(config.llm).unavailable_until.get(_ROUTE, 0.0) > time.monotonic()


def _request(
    accepted: AcceptedPreparation, publication_date: date, config: Any, cache_dir: str | Path,
) -> tuple[dict[str, Any], tuple[EvidenceBinding, ...]]:
    report_sha, bundle_id, bindings = resolve_bindings(accepted, cache_dir)
    try:
        messages = build_messages(
            accepted_sha256=accepted.sha256, publication_date=publication_date.isoformat(),
            report_sha256=report_sha, bundle_id=bundle_id, publication_language=_language(config), bindings=bindings,
        )
        payload = openai_request_body(
            _ROUTE[1], messages, 0, 4096, groq=True, reasoning_effort="low",
            response_format=groq_synthesis_response_format(),
        )
        validate_request_bounds(messages, payload)
    except ValueError as exc:
        raise RequestAdmissionError("Optional synthesis request cannot be admitted.") from exc
    record = storage.new_record(
        accepted_sha256=accepted.sha256, publication_date=publication_date.isoformat(),
        report_sha256=report_sha, bundle_id=bundle_id, bindings=bindings,
        publication_language=_language(config), request_body=payload, status="reserved", started_at=_instant(),
    )
    return record, bindings


def _reuse(
    saved: dict[str, Any], expected: dict[str, Any], bindings: tuple[EvidenceBinding, ...],
) -> SynthesisOutcome:
    if {k: v for k, v in saved.items() if k not in _TERMINAL_FIELDS} != {
        k: v for k, v in expected.items() if k not in _TERMINAL_FIELDS
    }:
        return SynthesisOutcome("skipped", "cache_mismatch")
    if saved["status"] != "valid":
        return SynthesisOutcome(saved["status"], saved["reason"] or "retained_attempt")
    result = validate_response(
        saved["response_text"], saved["finish_reason"], expected["publication_language"],
        bindings, protected_content_equal,
    )
    if canonical_json_bytes(asdict(result)) != canonical_json_bytes(saved["result"]):
        raise ValueError("Synthesis result differs from its retained response.")
    return SynthesisOutcome("comparison", "reused", result, bindings, saved["request_sha256"])


def _finish(
    reservation: dict[str, Any], terminal: dict[str, Any], cache_dir: str | Path,
) -> bool:
    try:
        storage.finish_attempt(reservation, terminal, cache_dir)
    except (OSError, ValueError, TypeError):
        logger.warning("Optional synthesis terminal persistence failed; baseline retained without regeneration.")
        return False
    return True


def _failed(
    reservation: dict[str, Any], reason: str, cache_dir: str | Path,
) -> SynthesisOutcome:
    try:
        terminal = storage.terminal_record(reservation, status="failed", completed_at=_instant(), reason=reason)
        _finish(reservation, terminal, cache_dir)
    except (OSError, ValueError, TypeError):
        logger.warning("Optional synthesis failure capture unavailable; retained attempt stays terminal.")
    return SynthesisOutcome("failed", reason)


async def _dispatch(
    record: dict[str, Any], config: Any, execution: ModelExecution, deadline: float,
    bindings: tuple[EvidenceBinding, ...], cache_dir: str | Path,
) -> SynthesisOutcome:
    try:
        storage.reserve_attempt(record, cache_dir)
    except (OSError, ValueError, TypeError):
        return SynthesisOutcome("skipped", "reservation_failed")
    if time.monotonic() >= deadline:
        return _failed(record, "deadline_exhausted", cache_dir)
    bounded = copy(config)
    bounded.llm = copy(config.llm)
    bounded.llm.max_retries = 0
    shared = execution.share_initialized(config.llm)
    try:
        async with asyncio.timeout_at(deadline):
            while (wait := request_wait_seconds(bounded, shared)) > 0:
                if _held(bounded, shared) or time.monotonic() + wait >= deadline:
                    return _failed(record, "pacing_unavailable", cache_dir)
                await asyncio.sleep(wait)
            if _held(bounded, shared) or time.monotonic() >= deadline:
                return _failed(record, "deadline_exhausted", cache_dir)
            text, usage = await complete(
                LLMRole.SYNTHESIZE_EVIDENCE, record["messages"], bounded, execution=shared,
                temperature=0, provider_override=ProviderConfig(*_ROUTE), max_output_tokens=4096,
                request_timeout_seconds=deadline - time.monotonic(), reasoning_effort="low",
                response_format=groq_synthesis_response_format(),
            )
    except asyncio.CancelledError:
        _failed(record, "cancelled", cache_dir)
        raise
    except TimeoutError:
        return _failed(record, "timed_out", cache_dir)
    except (RuntimeError, OSError, ValueError, TypeError, AttributeError, KeyError, IndexError):
        # The established wire adapter can reject malformed successful HTTP
        # envelopes with shape errors. They remain failures of this optional
        # attempt, without weakening or changing the shared transport owner.
        return _failed(record, "provider_failed", cache_dir)
    if not isinstance(text, str) or not isinstance(usage, dict):
        return _failed(record, "response_invalid", cache_dir)
    if time.monotonic() >= deadline:
        return _failed(record, "deadline_exhausted", cache_dir)
    return _capture(record, text, usage, bindings, deadline, cache_dir)


def _capture(
    record: dict[str, Any], text: str, usage: dict[str, Any], bindings: tuple[EvidenceBinding, ...],
    deadline: float, cache_dir: str | Path,
) -> SynthesisOutcome:
    result = None
    reason = "response_invalid"
    try:
        result = validate_response(
            text, usage.get("finish_reason"), record["publication_language"], bindings, protected_content_equal,
        )
    except (ValueError, TypeError, KeyError):
        pass
    if time.monotonic() >= deadline:
        return _failed(record, "deadline_exhausted", cache_dir)
    status = "valid" if result is not None and result.status == "comparison" else (
        "abstained" if result is not None else "invalid"
    )
    if result is not None:
        reason = "completed" if status == "valid" else "abstained"
    try:
        terminal = storage.terminal_record(
            record, status=status, completed_at=_instant(), response_text=text,
            reported_model=usage.get("resolved_model"), finish_reason=usage.get("finish_reason"),
            usage=usage, result=result, reason=reason,
        )
    except (ValueError, TypeError, UnicodeError):
        return _failed(record, "response_oversized", cache_dir)
    if not _finish(record, terminal, cache_dir):
        return SynthesisOutcome("failed", "persistence_failed")
    if time.monotonic() >= deadline:
        return SynthesisOutcome("failed", "deadline_exhausted")
    return SynthesisOutcome(
        "comparison" if status == "valid" else status, reason,
        result if status == "valid" else None, bindings, record["request_sha256"],
    )


async def prepare_synthesis(
    accepted: AcceptedPreparation, config: Any, *, publication_date: date, execution: ModelExecution,
    allow_new: bool = False, cache_dir: str | Path = ".cache",
) -> SynthesisOutcome:
    """Called only after full primary/supplement baseline preflight; fail optional work closed."""
    if not synthesis_enabled(config):
        return SynthesisOutcome("disabled", "disabled")
    deadline = optional_deadline()
    language = _language(config)
    empty = storage.new_record(
        accepted_sha256=accepted.sha256, publication_date=publication_date.isoformat(),
        report_sha256=None, bundle_id=None, bindings=(), publication_language=language,
        request_body=None, status="skipped", started_at=_instant(), reason="unsupported_mode",
    )
    if not _supported(config) or language not in {"en", "ru"}:
        return _skip(empty, "unsupported_mode", cache_dir)
    route = config.review.secondary
    if (route.provider, route.model) != _ROUTE:
        return _skip(empty, "route_unavailable", cache_dir)
    if deadline is None:
        return _skip(empty, "deadline_unavailable", cache_dir)
    try:
        record, bindings = _request(accepted, publication_date, config, cache_dir)
    except RequestAdmissionError:
        return _skip(empty, "request_oversized", cache_dir)
    except (OSError, ValueError, TypeError, KeyError):
        return _skip(empty, "binding_unavailable", cache_dir)
    if time.monotonic() >= deadline:
        return _skip(record, "deadline_exhausted", cache_dir)
    try:
        saved = storage.load_attempt(accepted.sha256, cache_dir)
        if saved is not None:
            outcome = _reuse(saved, record, bindings)
            return outcome if time.monotonic() < deadline else SynthesisOutcome("skipped", "deadline_exhausted")
    except (OSError, ValueError, TypeError, KeyError):
        return SynthesisOutcome("skipped", "cache_unavailable")
    if not allow_new:
        return _skip(record, "recovery_no_attempt", cache_dir)
    try:
        if _held(config, execution):
            return _skip(record, "route_unavailable", cache_dir)
        if request_budget_remaining(config, execution) == 0:
            return _skip(record, "budget_exhausted", cache_dir)
        if time.monotonic() + request_wait_seconds(config, execution) >= deadline:
            return _skip(record, "pacing_unavailable", cache_dir)
    except (RuntimeError, OSError, ValueError):
        return _skip(record, "budget_exhausted", cache_dir)
    return await _dispatch(record, config, execution, deadline, bindings, cache_dir)
