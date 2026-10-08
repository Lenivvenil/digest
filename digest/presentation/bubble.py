"""Render the existing source and feedback snapshot at an observed time."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from digest.domain.catalog.source_rules import _diversity_score
from digest.domain.catalog.sources import SourceStateStore, SourceStats
from digest.domain.feedback.values import FeedbackStore


def compute_bubble_report(
    feedback_store: FeedbackStore,
    source_stats: dict[str, SourceStats],
    source_state: SourceStateStore,
    category_map: dict[str, str] | None = None,
    *,
    now: datetime,
) -> str:
    """Build a single-screen filter bubble snapshot from cached data. No I/O."""
    lines: list[str] = ["=== Filter Bubble Report ==="]
    lines.append(f"Generated: {now.strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("")

    if feedback_store.last_digest_time:
        try:
            last_dt = datetime.strptime(feedback_store.last_digest_time, "%Y-%m-%d %H:%M UTC").replace(
                tzinfo=timezone.utc
            )
            age_h = int((now - last_dt).total_seconds() // 3600)
            lines.append(f"Last digest: {feedback_store.last_digest_time} ({age_h}h ago)")
        except ValueError:
            lines.append(f"Last digest: {feedback_store.last_digest_time}")
    else:
        lines.append("Last digest: never")

    score, label = _diversity_score(source_stats)
    lines.append(f"Diversity: {label} ({score:.0f}/100)")
    lines.append("")

    recent_sources: dict[str, int] = {}
    for name, s in source_stats.items():
        count = sum(snap.articles_included for snap in s.history[-7:])
        if count > 0:
            recent_sources[name] = count

    if recent_sources:
        total_recent = sum(recent_sources.values())
        if category_map:
            category_counts: dict[str, int] = {}
            for src, count in recent_sources.items():
                cat = category_map.get(src, "Other")
                category_counts[cat] = category_counts.get(cat, 0) + count
            lines.append("Your bubble (7d):")
            for cat, count in sorted(category_counts.items(), key=lambda x: x[1], reverse=True):
                pct = round(count / total_recent * 100)
                lines.append(f"  {cat}: {count} art ({pct}%)")
        else:
            lines.append("Top sources (7d):")
            for name, count in sorted(recent_sources.items(), key=lambda x: x[1], reverse=True)[:5]:
                lines.append(f"  {name}: {count} art")
        lines.append("")

    cutoff = now - timedelta(days=14)
    recent_ratings = []
    for r in feedback_store.ratings:
        try:
            ts = datetime.fromisoformat(r.timestamp)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                recent_ratings.append(r)
        except ValueError:
            pass
    pos = sum(1 for r in recent_ratings if r.rating > 0)
    neg = sum(1 for r in recent_ratings if r.rating < 0)
    lines.append(f"Feedback (14d): {pos + neg} votes — +{pos} / -{neg}")

    graduated = sum(1 for e in source_state.sources.values() if e.graduated)
    demoted = sum(1 for e in source_state.sources.values() if e.demoted)
    trial = sum(1 for e in source_state.sources.values() if e.trial_started and not e.graduated and not e.demoted)
    if graduated or demoted or trial:
        lines.append(f"Sources: {graduated} graduated | {trial} trial | {demoted} demoted")

    return "\n".join(lines)
