"""Enrich a saved shortlist into internal reports; --execute permits fetch/model work, never delivery."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import yaml

from digest._util import atomic_json_write
from digest.config import Config, load_config
from digest.editorial_state import EditorialState, admit_articles, load_state, store_state
from digest.publication_contract import render_card
from digest.publication_worker import current_work, run_publication_pass
from digest.radar.collector import Article, article_hash
from digest.review import EvidenceSelection, _validated_cached_selections
from digest.review_checkpoint import load_review_checkpoint

logger = logging.getLogger(__name__)
DEFAULT_STATE = Path(".cache/editorial-enrichment")


@dataclass(frozen=True)
class SelectionManifest:
    schema_version: int
    checkpoint_sha256: str
    bundle_id: str
    selection_slot: str
    selection_status: str
    in_bundle_selected: tuple[str, ...]
    in_bundle_unselected: tuple[str, ...]
    omitted_unreviewed: int
    article_ids_by_evidence_id: dict[str, str]


def enrichment_config(path: Path) -> Config:
    """Default this isolated entry to English without changing the runtime config."""
    config = load_config(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    radar = raw.get("radar") or {}
    if "language" not in radar:
        config.radar.language = "en"
    config.llm.max_retries = 0
    return config


def load_selection(path: Path, config: Config) -> tuple[SelectionManifest, list[Article]]:
    """Revalidate saved evidence and literal quotes; never turn cached opinions into final drafts."""
    bundle, reviews = load_review_checkpoint(path, config)
    if any(review.bundle_id != bundle.bundle_id or review.status not in {
        "ok", "partial", "abstained", "invalid", "unavailable",
    } for review in reviews):
        raise ValueError("Checkpoint review does not match its evidence or status contract.")
    primary = next((review for review in reviews if review.slot == "primary"), None)
    if primary is None:
        raise ValueError("Enrichment requires a saved primary selection outcome.")
    chosen = primary
    if primary.status in {"invalid", "unavailable"}:
        chosen = next((review for review in reviews if review.slot == "secondary"
                       and review.status in {"ok", "partial"}), primary)
    selections: list[EvidenceSelection] = []
    if chosen.status in {"ok", "partial", "abstained"}:
        selections, _ = _validated_cached_selections(chosen, bundle, config.review.max_selections)
    selected = tuple(selection.evidence_id for selection in selections)
    evidence = {item.evidence_id: item for item in bundle.items}
    articles = []
    for identity in selected:
        item = evidence[identity]
        published = datetime.fromisoformat(item.published) if item.published else None
        if published is not None and published.tzinfo is None:
            raise ValueError("Selected evidence publication timestamp must include its timezone.")
        articles.append(Article(item.title, item.url, item.excerpt, item.source, item.category, published))
    article_ids = tuple(article_hash(article.title, article.link) for article in articles)
    if len(set(article_ids)) != len(article_ids):
        raise ValueError("Selected evidence maps to duplicate article identities.")
    manifest = SelectionManifest(
        1, hashlib.sha256(path.read_bytes()).hexdigest(), bundle.bundle_id, chosen.slot, chosen.status,
        selected, tuple(item.evidence_id for item in bundle.items if item.evidence_id not in selected),
        bundle.omitted_articles, dict(zip(selected, article_ids, strict=True)),
    )
    return manifest, articles


def _write_bytes(path: Path, data: bytes) -> None:
    if path.is_symlink():
        raise ValueError("Enrichment output must not be a symlink.")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".enrichment-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def prepare_selected_state(config: Config, state_dir: Path, checkpoint: Path | None = None) -> list[SelectionManifest]:
    """Persist immutable selection provenance before admitting only the selected work."""
    state = load_state(state_dir)
    directory = state_dir / "selections"
    if directory.is_symlink():
        raise ValueError("Enrichment selections directory must not be a symlink.")
    manifests: dict[str, SelectionManifest] = {}
    articles: dict[str, Article] = {}
    pending: tuple[SelectionManifest, bytes] | None = None
    for path in sorted(directory.glob("*.review.json")):
        if path.is_symlink():
            raise ValueError("Saved enrichment checkpoint must not be a symlink.")
        manifest, selected = load_selection(path, config)
        if path.name != f"{manifest.checkpoint_sha256}.review.json":
            raise ValueError("Saved enrichment checkpoint hash changed.")
        manifest_path = directory / f"{manifest.checkpoint_sha256}.manifest.json"
        expected = json.loads(json.dumps(asdict(manifest)))
        if manifest_path.exists() and (manifest_path.is_symlink() or json.loads(manifest_path.read_text()) != expected):
            raise ValueError("Saved enrichment selection manifest changed.")
        manifests[manifest.checkpoint_sha256] = manifest
        for article in selected:
            articles.setdefault(article_hash(article.title, article.link), article)
    if checkpoint is not None:
        manifest, selected = load_selection(checkpoint, config)
        data = checkpoint.read_bytes()
        if hashlib.sha256(data).hexdigest() != manifest.checkpoint_sha256:
            raise ValueError("Source checkpoint changed while preparing enrichment.")
        pending = manifest, data
        manifests[manifest.checkpoint_sha256] = manifest
        for article in selected:
            articles.setdefault(article_hash(article.title, article.link), article)
    if not manifests:
        raise ValueError("Supply --checkpoint once before resuming selected enrichment work.")
    if set(state.articles) - set(articles):
        raise ValueError("State contains unrelated articles; use a separate enrichment state directory.")
    for identity, work in state.articles.items():
        original = articles[identity]
        if work.to_article() != original:
            raise ValueError("Saved article metadata differs from immutable selected evidence.")
        if work.delivery_state != "pending" or work.delivery_attempt_id is not None:
            raise ValueError("Enrichment requires isolated draft state without delivery activity.")
    directory.mkdir(parents=True, exist_ok=True)
    if pending is not None:
        manifest, data = pending
        target = directory / f"{manifest.checkpoint_sha256}.review.json"
        if not target.exists():
            _write_bytes(target, data)
    for manifest in manifests.values():
        target = directory / f"{manifest.checkpoint_sha256}.manifest.json"
        if not target.exists():
            atomic_json_write(target, asdict(manifest))
    admit_articles(state, list(articles.values()))
    store_state(state, state_dir)
    return list(manifests.values())


def write_report(config: Config, state: EditorialState, manifests: list[SelectionManifest], output: Path) -> None:
    """English report shell; draft text retains its configured generation language."""
    if output.is_symlink():
        raise ValueError("Enrichment report directory must not be a symlink.")
    output.mkdir(parents=True, exist_ok=True)
    drafts = []
    checks = []
    for article in state.articles.values():
        work = current_work(article.article_id, state, config)
        checks.append({"article_id": article.article_id, "acquisition_error": article.acquisition_error,
                       "coverage_notes": article.coverage_notes, "factual_check": asdict(work) if work else None})
        if work is not None and work.outcome == "model_checked":
            text = render_card(work.drafts[-1], work.audits[-1], source=article.source,
                               title=article.title, url=article.url, feed_published_at=article.published,
                               source_published_at=article.source_published, fetched_at=article.fetched_at)
            drafts.append({"article_id": article.article_id, "body_sha256": article.body_sha256,
                           "binding": work.binding, "text": text})
    outcomes = [current_work(identity, state, config) for identity in state.order]
    summary = {"admitted": len(state.articles),
               "acquired": sum(article.body_sha256 is not None for article in state.articles.values()),
               "model_checked": len(drafts),
               "rejected": sum(work is not None and work.outcome == "rejected" for work in outcomes),
               "pending": sum(work is None or work.outcome in {"pending", "unknown"} for work in outcomes)}
    coverage_note = ("Unselected bundle items and omitted articles are not semantic rejections. "
                     "Omitted articles were not reviewed; counts are per checkpoint, not unique across runs.")
    report = {
        "schema_version": 1, "status": "internal_drafts_not_fact_verified", "language": config.radar.language,
        "selections": [asdict(manifest) for manifest in manifests], "summary": summary,
        "drafts": drafts, "publication_checks": checks,
        "check_kind": "draft_aware_factual_check_not_blind_independent_opinion",
        "legacy_generation_count": sum(len(article.generations) for article in state.articles.values()),
        "coverage_note": coverage_note,
    }
    lines = ["# Editorial enrichment", "", "Internal drafts. Not fact-verified. No delivery approval.", "",
             f"Selected work: {len(state.articles)}; acquired: {summary['acquired']}; "
             f"drafts: {len(drafts)}; pending: {summary['pending']}."]
    for manifest in manifests:
        lines.extend(["", f"Checkpoint: {manifest.checkpoint_sha256}; evidence bundle: {manifest.bundle_id}",
                      f"Selection: {manifest.selection_slot}/{manifest.selection_status}; "
                      f"in_bundle_selected: {len(manifest.in_bundle_selected)}; "
                      f"in_bundle_unselected: {len(manifest.in_bundle_unselected)}; "
                      f"omitted_unreviewed: {manifest.omitted_unreviewed}."])
    lines.extend(["", coverage_note])
    lines.append("Factual checking sees the draft; it is not a blind independent opinion.")
    for draft in drafts:
        lines.extend(["", str(draft["text"])])
    _write_bytes(output / "enrichment-report.json", (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode())
    _write_bytes(output / "enrichment-report.md", ("\n".join(lines) + "\n").encode())


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--output", type=Path, default=Path("editorial-enrichment-report"))
    parser.add_argument("--execute", action="store_true",
                        help="Explicitly permit selected article acquisition/model work")
    parser.add_argument("--deadline-seconds", type=float, default=180)
    parser.add_argument("--max-calls", type=int, default=4)
    args = parser.parse_args(argv)
    if (not math.isfinite(args.deadline_seconds) or not 0 < args.deadline_seconds <= 900
            or not 0 <= args.max_calls <= 30):
        raise ValueError("Invalid enrichment execution allowance.")
    config = enrichment_config(args.config)
    manifests = prepare_selected_state(config, args.state, args.checkpoint)
    if args.execute:
        state = load_state(args.state)
        result = await run_publication_pass(config, args.state,
                                          deadline_seconds=args.deadline_seconds, max_calls=args.max_calls)
        state = result.state
    else:
        state = load_state(args.state)
    write_report(config, state, manifests, args.output)
    logger.info("Internal enrichment draft report saved to %s", args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
