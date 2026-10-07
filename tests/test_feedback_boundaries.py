"""Keep catalog/feedback policy independent of its storage and Telegram effects."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from importlib.util import resolve_name
from pathlib import Path

import pytest

from digest import discovery, feedback
from digest.adapters.storage import feedback as feedback_storage
from digest.adapters.storage import pending_sources
from digest.application import feedback as feedback_application
from digest.domain.catalog import proposals
from digest.domain.feedback import rules, values

ROOT = Path(__file__).resolve().parents[1] / "digest"


def _imports(path: Path) -> list[tuple[int, str]]:
    package = ".".join(("digest", *path.relative_to(ROOT).with_suffix("").parts[:-1]))
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = resolve_name("." * node.level + module, package)
            found.append((node.lineno, module))
            found.extend((node.lineno, f"{module}.{alias.name}") for alias in node.names)
    return found


@pytest.mark.parametrize("relative", [
    "domain/catalog/proposals.py", "domain/feedback/values.py", "domain/feedback/rules.py",
])
def test_feedback_policy_imports_no_effect_owner(relative: str) -> None:
    forbidden = (
        "digest.adapters", "digest.application", "digest.cli", "digest.main", "digest.discovery",
        "digest.feedback", "digest.llm", "httpx", "os", "pathlib", "socket", "subprocess", "time",
    )
    violations = [f"{line}: {target}" for line, target in _imports(ROOT / relative)
                  if any(target == root or target.startswith(root + ".") for root in forbidden)]
    assert not violations, "Policy imports an effect owner:\n" + "\n".join(violations)
    tree = ast.parse((ROOT / relative).read_text())
    effects = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and (
        isinstance(node.func, ast.Name) and node.func.id == "open"
        or isinstance(node.func, ast.Attribute) and node.func.attr in {
            "now", "today", "read_text", "read_bytes", "write_text", "write_bytes", "open",
        }
    )]
    assert not effects, "Policy must receive time and state rather than read effects"


def test_telegram_protocol_does_not_read_or_write_persistent_state() -> None:
    path = ROOT / "adapters/telegram/feedback.py"
    forbidden = (
        "digest.adapters.storage", "digest.application", "digest.discovery", "digest.feedback", "pathlib",
    )
    violations = [f"{line}: {target}" for line, target in _imports(path)
                  if any(target == root or target.startswith(root + ".") for root in forbidden)]
    assert not violations, "Telegram protocol imports persistent state owners:\n" + "\n".join(violations)
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value not in {"feedback.json", "pending_sources.json"}
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                assert node.func.id != "open"
            elif isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {"read_text", "read_bytes", "write_text", "write_bytes", "open"}


def test_compatibility_exports_preserve_owner_identity() -> None:
    assert discovery.PendingSource is proposals.PendingSource
    assert discovery.source_hash is proposals.source_hash
    assert discovery.proposal_binding is proposals.proposal_binding
    assert discovery.load_pending is pending_sources.load_pending
    assert discovery.save_pending is pending_sources.save_pending
    assert feedback.ArticleFeedback is values.ArticleFeedback
    assert feedback.FeedbackStore is values.FeedbackStore
    assert feedback.PendingReply is values.PendingReply
    assert feedback.load_feedback is feedback_storage.load_feedback
    assert feedback.save_feedback is feedback_storage.save_feedback
    assert feedback.collect_feedback is feedback_application.collect_feedback
    assert feedback.acknowledge_feedback is feedback_application.acknowledge_feedback
    assert feedback.apply_delivery_attribution is rules.apply_delivery_attribution


def test_proposal_and_vote_rules_use_the_supplied_decision_time() -> None:
    now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
    proposal = proposals.PendingSource("Feed", "https://example.org/feed", "Tech", now.isoformat())
    store = values.FeedbackStore(article_source_map={"abcd1234": "Feed"})
    assert rules.record_source_decision(store, [proposal], proposal.source_hash, "ok", now=now) == "source_decisions"
    assert store.source_decision_bindings == {proposal.source_hash: proposals.proposal_binding(proposal)}
    assert rules.record_article_vote(store, "abcd1234", "g", recorded_at=now) == "recorded_votes"
    assert store.ratings[0].timestamp == now.isoformat()
    expired = now + timedelta(days=30, microseconds=1)
    assert proposals.resolve_pending_proposal([proposal], proposal.source_hash, now=expired) is None
    before = dict(store.source_decisions)
    assert rules.record_source_decision(store, [proposal], proposal.source_hash, "no", now=expired) == "unknown_source"
    assert store.source_decisions == before


def test_latest_vote_rule_preserves_equal_timestamp_and_whole_day_cutoff() -> None:
    now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
    stamp = (now - timedelta(days=14, hours=23)).isoformat()
    store = values.FeedbackStore(ratings=[
        values.ArticleFeedback("tie", "Feed", 1, stamp),
        values.ArticleFeedback("tie", "Feed", -1, stamp),
        values.ArticleFeedback("old", "Feed", 1, (now - timedelta(days=15)).isoformat()),
    ])
    assert rules.get_source_feedback_score(store, "Feed", now=now) == 0.0
    assert rules.get_source_feedback_score(store, "Feed", now=now + timedelta(hours=1)) is None
