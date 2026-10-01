"""Durable editorial work and separately reserved primary delivery.

The workflow persists work, then reserves/pushes a delivery attempt, then executes
it once. Work/report commands never read Telegram credentials or deliver messages.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import uuid
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx

from digest._util import atomic_json_write
from digest.config import Config, load_config
from digest.delivery.markdown import _build_frontmatter, _build_top_articles_section
from digest.delivery.telegram import escape_markdownv2
from digest.editorial_state import load_state, ready_results, store_state
from digest.editorial_worker import run_editorial_pass, summarize_state
from digest.feedback import load_feedback, save_feedback
from digest.radar.collector import (
    AllFeedsFailedError,
    Article,
    CollectionCoverage,
    SourceFetchMetrics,
    _load_cache,
    article_hash,
    collect,
    save_dedup_cache,
)
from digest.radar.summarizer import ArticleSummary
from digest.review import BlindReviewReport, build_evidence_bundle
from digest.review_trial import _ALLOWED_MODELS
from digest.source_scorer import (
    DailySnapshot,
    SourceStats,
    load_source_state,
    load_stats,
    save_source_category_map,
    save_stats,
)


def _config(path: Path) -> Config:
    config = load_config(path)
    models = (config.review.primary, config.review.secondary, config.review.tie_breaker)
    if any((m.provider, m.model) not in _ALLOWED_MODELS for m in models if m is not None):
        raise ValueError("Editorial work requires the approved free-provider lineup.")
    config.llm.max_retries = 0
    return config


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _output(name: str, value: str) -> None:
    if "\n" in value or "\r" in value:
        raise ValueError("Invalid workflow output.")
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with Path(target).open("a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


def _source_article(item: Any) -> Article:
    published = datetime.fromisoformat(item.published) if item.published else None
    return Article(item.title, item.url, item.description, item.source, item.category, published)


def _report(config: Config, state_dir: Path, output_dir: Path) -> None:
    state = load_state(state_dir)
    ready = ready_results(state, config)
    ready.sort(key=lambda item: (-item.value_score, item.admitted_at, item.article_id))
    summary = summarize_state(state, config)
    reserved = sum(item.delivery_state == "reserved" for item in state.articles.values())
    events: dict[str, list[str]] = {}
    for item in ready:
        events.setdefault(item.event_key, []).append(item.article_id)
    repeated = {key: identities for key, identities in events.items() if key and len(identities) > 1}
    output_dir.mkdir(parents=True, exist_ok=True)
    atomic_json_write(output_dir / "editorial-report.json", {
        "created_at": _now(), "report_only": True,
        "ready": [asdict(item) for item in ready], "state": asdict(state),
        "coverage": {**asdict(summary), "reserved_delivery": reserved}, "possible_repeated_events": repeated,
    })
    lines = ["# Редакторский отчёт", "", "Этот отчёт сам по себе не отправляет сообщения в Telegram.",
             f"Допущено: {summary.admitted}; текст получен: {summary.acquired}; полностью проанализировано: "
             f"{summary.fully_analysed}; отклонено редакционно: {summary.rejected}; ждут обработки: {summary.pending}.",
             f"Самая старая незавершённая запись: {summary.oldest_pending_at or 'нет'}. "
             f"Подтверждённые доставки: {summary.delivered}; неизвестный исход: {summary.unknown_delivery}; "
             f"удерживаемые резервации доставки: {reserved}.", ""]
    if repeated:
        lines.append("Возможное повторение событий между готовыми карточками отмечено в JSON; "
                     "это требует смысловой проверки, совпадение темы не равно дубликату.")
    for item in ready:
        card = item.to_article_summary()
        lines.extend([f"## {card.title}", card.link, "", card.summary, ""])
    if not ready:
        lines.append("Готовых карточек пока нет. Незавершённая работа не означает отсутствие достойных новостей.")
    lines.extend(["", "Полное состояние обработки и происхождение выводов: editorial-report.json."])
    (output_dir / "editorial-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _verification_sources(path: Path) -> list[Article]:
    """Explicit report-only source cases; never an automatic candidate-count filter."""
    if path.is_symlink() or path.stat().st_size > 1_000_000:
        raise ValueError("Unsafe or oversized verification source file.")
    raw = json.loads(path.read_text(encoding="utf-8"))
    fields = {"title", "url", "source", "category", "published"}
    if not isinstance(raw, list) or not raw:
        raise ValueError("Verification sources must be a nonempty list.")
    result = []
    for item in raw:
        if (not isinstance(item, dict) or set(item) != fields
                or any(not isinstance(item[key], str) or not item[key] for key in fields - {"published"})):
            raise ValueError("Invalid verification source metadata.")
        stamp = datetime.fromisoformat(item["published"]) if item["published"] else None
        if stamp is not None and stamp.tzinfo is None:
            raise ValueError("Verification source date needs a timezone.")
        result.append(Article(item["title"], item["url"], "", item["source"], item["category"], stamp))
    return result


async def work(
    config_path: Path, state_dir: Path, output_dir: Path, *, independent: bool = False,
    deadline_seconds: float = 480, max_calls: int = 12, verification_sources: Path | None = None,
) -> int:
    config = _config(config_path)
    coverage = CollectionCoverage()
    articles: list[Article] = []
    collection_error = ""
    fetch_metrics: dict[str, SourceFetchMetrics] = {}
    if verification_sources is not None:
        if independent:
            raise ValueError("Independent review must resume the existing immutable sources.")
        articles = _verification_sources(verification_sources)
        coverage.raw_fetched = coverage.eligible_unique = len(articles)
        coverage.admitted = len({article_hash(item.title, item.link) for item in articles})
    elif not independent:
        config.sources = config.effective_sources(load_source_state(".cache"))
        try:
            grouped, _uncommitted_cache = await collect(
                config, admit_all=True, coverage=coverage, fetch_metrics=fetch_metrics,
            )
            articles = [article for group in grouped.values() for article in group]
        except AllFeedsFailedError:
            # Previously acquired work remains resumable when current feeds fail.
            collection_error = "all_feeds_failed"
        from digest.main import _record_source_stats

        source_stats = load_stats(".cache")
        _record_source_stats(source_stats, fetch_metrics, {}, set())
        save_stats(source_stats, ".cache", active_sources={source.name for source in config.enabled_sources})
        save_source_category_map(config.enabled_sources, ".cache")
    result = await run_editorial_pass(
        config, state_dir, articles, mode="independent" if independent else "primary",
        deadline_seconds=deadline_seconds, max_calls=max_calls,
    )
    _report(config, state_dir, output_dir)
    atomic_json_write(output_dir / "editorial-pass.json", {
        "created_at": _now(), "collection": asdict(coverage), "processing": asdict(result.summary),
        "telegram_used": False, "dedup_written": False, "collection_error": collection_error,
        "source_scope": "explicit_verification_cases" if verification_sources is not None else "configured_feeds",
    })
    print(json.dumps({"collection": asdict(coverage), "processing": asdict(result.summary)}, ensure_ascii=False))
    return 0


def prepare(config_path: Path, state_dir: Path) -> Path | None:
    """Reserve exact ready cards; workflow must push this state before execute."""
    config = _config(config_path)
    if (not config.telegram.enabled or not os.environ.get("TELEGRAM_BOT_TOKEN")
            or not os.environ.get("TELEGRAM_CHAT_ID")):
        raise ValueError("Primary delivery is not configured; no articles reserved.")
    state = load_state(state_dir)
    cache = _load_cache()
    enabled = {source.name for source in config.effective_sources(load_source_state(".cache"))}
    ready = [item for item in ready_results(state, config)
             if item.article_id not in cache and state.articles[item.article_id].source in enabled]
    # Existing transport budget bounds this send, not analysis or editorial eligibility.
    ready.sort(key=lambda item: (-item.value_score, item.admitted_at, item.article_id))
    ready = ready[:config.telegram.max_messages]
    if not ready:
        _output("delivery_attempt", "")
        return None
    identity = uuid.uuid4().hex
    cards = []
    for result in ready:
        article = state.articles[result.article_id]
        card = result.to_article_summary()
        if article_hash(article.title, article.url) != result.article_id:
            raise ValueError("Ready article identity does not match its original source.")
        cards.append({"article_id": result.article_id, "card": asdict(card),
                      "analysis": asdict(result), "outcome": "reserved",
                      "source_article": {key: getattr(article, key) for key in
                                         ("title", "url", "description", "source", "category", "published")}})
    directory = state_dir / "deliveries"
    if directory.is_symlink():
        raise ValueError("Delivery directory must not be a symlink.")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{identity}.json"
    record = {"schema_version": 1, "attempt_id": identity, "created_at": _now(),
              "github_run_id": os.environ.get("GITHUB_RUN_ID"), "execute_started": None,
              "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
              "destination_sha256": hashlib.sha256(os.environ["TELEGRAM_CHAT_ID"].encode()).hexdigest(),
              "cards": cards, "archive_state": "not_started"}
    atomic_json_write(path, record)
    for result in ready:
        state.articles[result.article_id].delivery_state = "reserved"
        state.articles[result.article_id].delivery_attempt_id = identity
    store_state(state, state_dir)
    _output("delivery_attempt", path.as_posix())
    return path


def _load_attempt(path: Path, state_dir: Path, config_path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.resolve().is_relative_to((state_dir / "deliveries").resolve()):
        raise ValueError("Delivery attempt must be a plain file in its state directory.")
    if path.stat().st_size > 2_000_000:
        raise ValueError("Oversized delivery attempt.")
    raw = json.loads(path.read_text())
    if (not isinstance(raw, dict) or type(raw.get("schema_version")) is not int
            or raw["schema_version"] != 1 or raw.get("execute_started")
            or not re.fullmatch(r"[a-f0-9]{32}", str(raw.get("attempt_id", "")))
            or raw.get("config_sha256") != hashlib.sha256(config_path.read_bytes()).hexdigest()
            or not isinstance(raw.get("cards"), list) or not raw["cards"]):
        raise ValueError("Invalid, changed or already executed delivery attempt.")
    identities = [entry.get("article_id") for entry in raw["cards"] if isinstance(entry, dict)]
    if (len(identities) != len(raw["cards"])
            or any(not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{32}", identity)
                   for identity in identities)
            or len(set(identities)) != len(identities)
            or path.stem != raw["attempt_id"]
            or raw.get("destination_sha256") != hashlib.sha256(
                os.environ.get("TELEGRAM_CHAT_ID", "").encode()).hexdigest()):
        raise ValueError("Invalid delivery identities or changed destination.")
    if raw.get("github_run_id") != os.environ.get("GITHUB_RUN_ID"):
        raise ValueError("Delivery reservation belongs to another workflow run.")
    if os.environ.get("GITHUB_RUN_ATTEMPT", "1") != "1":
        raise ValueError("A workflow rerun cannot resend a reserved delivery.")
    return raw


def _telegram_payload(card: ArticleSummary, identity: str, config: Config) -> dict[str, Any]:
    title = escape_markdownv2(card.title)
    url = card.link.replace("\\", "\\\\").replace(")", "\\)")
    note = ("Реакции учитываются при следующем запуске" if config.adaptive.enabled
            else "Обработка реакций пока отключена")
    text = (f"[{title}]({url})\n\n{escape_markdownv2(card.summary)}\n\n"
            f"{escape_markdownv2(card.source)} · {escape_markdownv2(card.category)}\n"
            f"_{escape_markdownv2(note)}_")
    if len(text) > 4096:
        raise ValueError("Editorial card exceeds Telegram transport limit; no truncation performed.")
    return {"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text,
            "parse_mode": "MarkdownV2", "disable_web_page_preview": True,
            "reply_markup": {"inline_keyboard": [[
                {"text": "👍", "callback_data": f"fb:a:g:{identity[:8]}"},
                {"text": "👎", "callback_data": f"fb:a:b:{identity[:8]}"},
            ]]}}


async def _post_card(
    client: httpx.AsyncClient, payload: dict[str, Any],
) -> tuple[Literal["delivered", "confirmed_failed", "unknown"], int | None]:
    """One POST only: a timeout/malformed acknowledgement is not safe to retry."""
    try:
        response = await client.post(
            f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
            json=payload, timeout=30.0,
        )
        body = response.json()
        if isinstance(body, dict) and body.get("ok") is True and response.is_success:
            message = body.get("result", {})
            message_id = message.get("message_id") if isinstance(message, dict) else None
            if type(message_id) is int:
                return "delivered", message_id
        if isinstance(body, dict) and body.get("ok") is False and response.status_code < 500:
            return "confirmed_failed", None
    except Exception:
        # Never print the exception: the request URL contains the bot credential.
        pass
    return "unknown", None


def _record_inclusion(source: str, config: Config) -> None:
    """Add confirmed queue output without fabricating another feed fetch."""
    stats = load_stats(".cache")
    entry = stats.setdefault(source, SourceStats(name=source))
    entry.articles_included_in_digest += 1
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    if entry.history and entry.history[-1].date == today:
        entry.history[-1].articles_included += 1
    else:
        entry.history.append(DailySnapshot(today, 0, 1, False))
    save_stats(stats, ".cache", active_sources={item.name for item in config.enabled_sources})


async def deliver(config_path: Path, state_dir: Path, attempt_path: Path) -> int:
    config = _config(config_path)
    if (not config.telegram.enabled or not os.environ.get("TELEGRAM_BOT_TOKEN")
            or not os.environ.get("TELEGRAM_CHAT_ID")):
        raise ValueError("Primary delivery is not configured.")
    record = _load_attempt(attempt_path, state_dir, config_path)
    state = load_state(state_dir)
    prepared = []
    check_state = deepcopy(state)
    for entry in record["cards"]:
        check_state.articles[entry["article_id"]].delivery_state = "pending"
    expected = {item.article_id: item for item in ready_results(check_state, config)}
    for entry in record["cards"]:
        article = state.articles[entry["article_id"]]
        if article.delivery_state != "reserved" or article.delivery_attempt_id != record["attempt_id"]:
            raise ValueError("Article delivery reservation changed.")
        if (entry["article_id"] not in expected
                or asdict(expected[entry["article_id"]].to_article_summary()) != entry["card"]
                or json.loads(json.dumps(asdict(expected[entry["article_id"]]))) != entry["analysis"]):
            raise ValueError("Reserved card differs from its completed source analysis.")
        card = ArticleSummary(**entry["card"])
        try:
            payload = _telegram_payload(card, entry["article_id"], config)
        except ValueError:
            entry.update(outcome="confirmed_failed", error="payload_transport_limit", completed_at=_now())
            article.delivery_state = "confirmed_failed"
            continue
        prepared.append((entry, article, card, payload))
    record["execute_started"] = _now()
    atomic_json_write(attempt_path, record)
    store_state(state, state_dir)
    delivered_cards: list[ArticleSummary] = []
    delivered_articles: dict[str, list[Article]] = {}
    cache = _load_cache()
    feedback = load_feedback(".cache")
    async with httpx.AsyncClient() as client:
        for entry, article, card, payload in prepared:
            article.delivery_state = "unknown"
            store_state(state, state_dir)
            outcome, message_id = await _post_card(client, payload)
            article.delivery_state = outcome
            entry.update(outcome=outcome, message_id=message_id, completed_at=_now())
            if outcome == "delivered":
                cache[entry["article_id"]] = _now()
                feedback.article_source_map[entry["article_id"][:8]] = article.source
                delivered_cards.append(card)
                delivered_articles.setdefault(article.category, []).append(_source_article(article))
                save_dedup_cache(cache)
                feedback.last_digest_sources = sorted({item.source for item in delivered_cards})
                feedback.last_digest_time = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
                save_feedback(feedback, ".cache")
                _record_inclusion(article.source, config)
            atomic_json_write(attempt_path, record)
            store_state(state, state_dir)
            await asyncio.sleep(1.1)
    counts = {name: sum(item["outcome"] == name for item in record["cards"])
              for name in ("delivered", "confirmed_failed", "unknown")}
    record["archive_state"] = "pending" if delivered_cards else "not_needed"
    atomic_json_write(attempt_path, record)
    archive_ok = _archive_attempt(config, attempt_path, record)
    print(json.dumps({"telegram_api": counts, "archive_state": record["archive_state"]}, ensure_ascii=False))
    return 0 if counts["delivered"] == len(record["cards"]) and archive_ok else 1


def _write_generated(path: Path, text: str) -> None:
    """Idempotent generated archive; preserve any conflicting existing content."""
    if path.is_symlink():
        raise ValueError("Archive destination must not be a symlink.")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text(encoding="utf-8") != text:
            raise ValueError("Generated archive conflicts with existing content.")
        return
    temporary = path.with_name(path.name + ".pending")
    if temporary.is_symlink():
        raise ValueError("Archive temporary file must not be a symlink.")
    temporary.write_text(text, encoding="utf-8")
    try:
        # Exclusive final creation does not overwrite a concurrently edited archive.
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _archive_attempt(config: Config, attempt_path: Path, record: dict[str, Any]) -> bool:
    delivered = [entry for entry in record["cards"] if entry["outcome"] == "delivered"]
    if not delivered:
        return True
    try:
        if not config.obsidian.enabled:
            raise ValueError("Editorial archive is disabled.")
        cards = [ArticleSummary(**entry["card"]) for entry in delivered]
        articles: dict[str, list[Article]] = {}
        for entry in delivered:
            source = entry["source_article"]
            article = Article(source["title"], source["url"], source["description"], source["source"],
                              source["category"], datetime.fromisoformat(source["published"])
                              if source["published"] else None)
            articles.setdefault(article.category, []).append(article)
        stamp = datetime.fromisoformat(record["created_at"]).strftime("%Y-%m-%d")
        archive = Path(config.obsidian.output_dir) / f"{stamp}-editorial-{record['attempt_id']}.md"
        # Existing Irritator consumes original RSS context with explicit limited coverage.
        # This is never represented as an independent full-body model opinion.
        bundle = build_evidence_bundle(articles, config.review)
        context = BlindReviewReport(1, bundle, [], "incomplete", None, [], "editorial_source_context_only")
        content = (_build_frontmatter(stamp, len({card.source for card in cards}), len(cards)) + "\n"
                   "Полнотекстовый редакторский выпуск. Независимый разбор может быть ещё не завершён.\n"
                   + _build_top_articles_section(cards) + "\n")
        checkpoint = archive.with_suffix(".review.json")
        _write_generated(archive, content)
        _write_generated(checkpoint, json.dumps(asdict(context), ensure_ascii=False, indent=2) + "\n")
        audit = {"attempt_id": record["attempt_id"], "created_at": record["created_at"],
                 "cards": record["cards"], "purpose": "confirmed_editorial_delivery"}
        _write_generated(archive.with_suffix(".editorial.json"),
                         json.dumps(audit, ensure_ascii=False, indent=2) + "\n")
        record["archive_state"] = "complete"
        record["archive_path"] = archive.as_posix()
        record.pop("archive_error", None)
        atomic_json_write(attempt_path, record)
        _output("review_checkpoint", checkpoint.as_posix())
        return True
    except (OSError, ValueError, KeyError, TypeError) as exc:
        record["archive_state"] = "pending"
        record["archive_error"] = type(exc).__name__
        atomic_json_write(attempt_path, record)
        return False


def repair_archives(config_path: Path, state_dir: Path) -> int:
    """Repair only confirmed-delivery archives, without any Telegram request."""
    config = _config(config_path)
    failed = 0
    for path in sorted((state_dir / "deliveries").glob("*.json")):
        if path.is_symlink() or path.stat().st_size > 2_000_000:
            raise ValueError("Unsafe delivery receipt.")
        raw = json.loads(path.read_text())
        if (raw.get("archive_state") == "pending" and raw.get("execute_started")
                and raw.get("attempt_id") == path.stem):
            failed += not _archive_attempt(config, path, raw)
    return 1 if failed else 0



async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("work", "independent", "report", "prepare", "deliver", "repair-archives"))
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--state", type=Path, default=Path(".cache/editorial"))
    parser.add_argument("--output", type=Path, default=Path("editorial-report"))
    parser.add_argument("--attempt", type=Path)
    parser.add_argument("--verification-sources", type=Path)
    parser.add_argument("--deadline-seconds", type=float, default=480)
    parser.add_argument("--max-calls", type=int, default=12)
    args = parser.parse_args(argv)
    if not 0 < args.deadline_seconds <= 900 or not 0 <= args.max_calls <= 30:
        raise ValueError("Invalid worker execution allowance.")
    if args.command in {"work", "independent"}:
        return await work(args.config, args.state, args.output, independent=args.command == "independent",
                          deadline_seconds=args.deadline_seconds, max_calls=args.max_calls,
                          verification_sources=args.verification_sources)
    if args.command == "report":
        _report(_config(args.config), args.state, args.output)
        return 0
    if args.command == "repair-archives":
        return repair_archives(args.config, args.state)
    if args.command == "prepare":
        prepare(args.config, args.state)
        return 0
    if args.attempt is None:
        raise ValueError("Deliver requires an exact reserved --attempt.")
    return await deliver(args.config, args.state, args.attempt)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
