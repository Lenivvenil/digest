"""Terminal and managed-runtime output for application results."""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

from digest.application.results import DigestPreview, Preview, RadarPreview, RunStats

if TYPE_CHECKING:
    from digest.application.discovery import DiscoveryResult, PreparedDiscovery

def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def print_stats(stats: RunStats) -> None:
    print("\n--- Digest Run Summary ---")
    print(f"Feeds attempted:    {stats.feeds_fetched}")
    print(f"New articles:       {stats.new_articles}")
    print(f"Digest length:      {stats.digest_length} chars")
    print(f"Blind review:       {stats.review_status}")
    if stats.telegram_partial:
        print("Telegram sent:      partial (some chunks failed)")
    else:
        print(f"Telegram sent:      {'yes' if stats.telegram_sent else 'no'}")
    if stats.markdown_saved:
        print(f"Markdown saved:     {stats.markdown_path}")
    else:
        print("Markdown saved:     no")
    if stats.feedback_collected:
        print(f"Feedback collected: {stats.feedback_collected}")
    if stats.sources_promoted:
        print(f"Sources promoted:   {stats.sources_promoted}")
    if stats.sources_demoted:
        print(f"Sources demoted:    {stats.sources_demoted}")
    print("--------------------------\n")


def emit_preview(preview: Preview) -> None:
    """Render the application's single preview handoff without owning its effects."""
    print(preview.combined)
    if isinstance(preview, RadarPreview):
        if preview.show_cards:
            for card in preview.cards:
                print(f"\n{card.title}\n{card.link}\n{card.summary}")
        return
    assert isinstance(preview, DigestPreview)
    if preview.cards:
        print("\n=== TOP ARTICLES ===\n")
        for article in preview.cards:
            print(f"[{article.category}] {article.title}")
            print(f"  {article.summary}\n")
    for ranked in preview.ranked:
        print(f"[{ranked.score}/10] {ranked.signal.title} — {ranked.signal.url}")
    print(f"\n💢 Irritator: {preview.irritator_status.text}")
    from digest.presentation.review import render_review

    print(render_review(preview.review_report) if preview.review_report is not None else "")


def publish_discovery_prepared(prepared: PreparedDiscovery) -> None:
    """Publish persisted hashes before the caller can dispatch any proposal."""
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as stream:
            stream.write(f"discovery_pending_sha256={prepared.pending_sha}\n"
                         f"discovery_delivery_sha256={prepared.delivery_sha}\n")


def print_discovery(result: DiscoveryResult) -> None:
    payload: dict[str, object] = {"stage": result.stage}
    if result.status:
        payload["status"] = result.status
    payload["counts"] = result.counts
    print(json.dumps(payload))


def publish_issue_reservation(path: Path, digest: str) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as handle:
            handle.write(f"issue_reservation={path.relative_to(Path.cwd()).as_posix()}\n")
            handle.write(f"issue_reservation_sha256={digest}\n")


def publish_review_checkpoint(stats: RunStats) -> None:
    """Expose a generated local archive only after successful delivery and saves."""
    output = os.environ.get("GITHUB_OUTPUT")
    if not output or not stats.markdown_saved or not stats.review_checkpoint:
        return
    try:
        checkpoint = Path(stats.review_checkpoint).resolve(strict=True)
        expected = Path(stats.markdown_path).resolve(strict=True).with_suffix(".review.json")
        relative = checkpoint.relative_to(Path.cwd().resolve()).as_posix()
        if (checkpoint != expected or not checkpoint.is_file()
                or not re.fullmatch(r"[A-Za-z0-9_./-]+", relative)
                or not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:-\d+)?\.review\.json", checkpoint.name)):
            raise ValueError("Unsafe or non-generated review checkpoint path.")
        with Path(output).open("a", encoding="utf-8") as handle:
            handle.write(f"review_checkpoint={relative}\n")
    except (OSError, ValueError) as exc:
        logging.getLogger(__name__).warning("Review checkpoint output unavailable: %s", exc)
