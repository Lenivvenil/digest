"""Round-trip property test for SourceStateStore (acceptance criterion for issue #37)."""

from __future__ import annotations

from pathlib import Path

from digest.source_scorer import SourceStateStore, load_source_state, save_source_state


def test_roundtrip_multiple_sources(tmp_path: Path) -> None:
    """Multiple sources with different states all survive round-trip."""
    store = load_source_state(str(tmp_path))
    store.set_trial_started("A", "2026-03-01")
    store.set_trial_started("B", "2026-01-01")
    store.mark_graduated("B")
    store.mark_demoted("C")
    save_source_state(store, str(tmp_path))

    reloaded = load_source_state(str(tmp_path))
    assert reloaded.get_trial_started("A") == "2026-03-01"
    assert reloaded.is_graduated("B")
    assert reloaded.get_trial_started("B") is None
    assert reloaded.is_demoted("C")
    assert not reloaded.is_demoted("A")
    assert not reloaded.is_graduated("A")


def test_roundtrip_empty_store(tmp_path: Path) -> None:
    """Empty store round-trips correctly."""
    store = SourceStateStore()
    save_source_state(store, str(tmp_path))

    reloaded = load_source_state(str(tmp_path))
    assert reloaded.sources == {}
    assert not reloaded.is_demoted("anything")
    assert not reloaded.is_graduated("anything")
    assert reloaded.get_trial_started("anything") is None
