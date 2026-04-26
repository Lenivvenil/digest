"""Round-trip property test for SourceStateStore (acceptance criterion for issue #37)."""

from __future__ import annotations

from pathlib import Path

from digest.source_scorer import SourceStateStore, load_source_state, save_source_state


def test_roundtrip_trial_started(tmp_path: Path) -> None:
    """load + mutate trial_started + save + reload → same value."""
    store = load_source_state(str(tmp_path))
    store.set_trial_started("MyFeed", "2026-04-26")
    save_source_state(store, str(tmp_path))

    reloaded = load_source_state(str(tmp_path))
    assert reloaded.get_trial_started("MyFeed") == "2026-04-26"


def test_roundtrip_graduated(tmp_path: Path) -> None:
    """mark_graduated survives save/load cycle."""
    store = load_source_state(str(tmp_path))
    store.set_trial_started("GradFeed", "2026-01-01")
    store.mark_graduated("GradFeed")
    save_source_state(store, str(tmp_path))

    reloaded = load_source_state(str(tmp_path))
    assert reloaded.is_graduated("GradFeed")
    assert reloaded.get_trial_started("GradFeed") is None


def test_roundtrip_demoted(tmp_path: Path) -> None:
    """mark_demoted survives save/load cycle."""
    store = load_source_state(str(tmp_path))
    store.mark_demoted("BadFeed")
    save_source_state(store, str(tmp_path))

    reloaded = load_source_state(str(tmp_path))
    assert reloaded.is_demoted("BadFeed")


def test_roundtrip_multiple_sources(tmp_path: Path) -> None:
    """Multiple sources with different states all survive round-trip."""
    store = SourceStateStore()
    store.set_trial_started("A", "2026-03-01")
    store.mark_graduated("B")
    store.mark_demoted("C")
    save_source_state(store, str(tmp_path))

    reloaded = load_source_state(str(tmp_path))
    assert reloaded.get_trial_started("A") == "2026-03-01"
    assert reloaded.is_graduated("B")
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
