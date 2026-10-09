"""Round-trip property test for SourceStateStore (acceptance criterion for issue #37)."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import get_type_hints
from unittest.mock import patch

import pytest

from digest import config, source_scorer
from digest._serialization import restore_dataclass
from digest.adapters.storage import delivery_state
from digest.adapters.storage import sources as storage
from digest.domain.catalog import sources
from digest.source_scorer import SourceStateStore, load_source_state, save_source_state


def test_roundtrip_multiple_sources(tmp_path: Path) -> None:
    """Multiple sources with different states all survive round-trip."""
    store = load_source_state(str(tmp_path))
    assert isinstance(store, SourceStateStore)
    assert store.sources == {}
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


def test_source_compatibility_identity_and_dataclass_restoration() -> None:
    assert config.SourceConfig is sources.SourceConfig
    assert config.AdaptiveConfig is sources.AdaptiveConfig
    assert get_type_hints(config.Config)["sources"] == list[sources.SourceConfig]
    assert get_type_hints(config.Config.effective_sources)["source_state"] is sources.SourceStateStore
    values = [
        sources.SourceConfig("Feed", "https://example.org/rss", "Tech", True),
        sources.AdaptiveConfig(False),
        sources.DailySnapshot("2026-10-07", 2, 1, True),
        sources.SourceStats("Feed", history=[sources.DailySnapshot("2026-10-07", 2, 1, True)]),
        sources.SourceStateEntry("2026-10-01"),
        sources.SourceStateStore(sources={"Feed": sources.SourceStateEntry("2026-10-01")}),
    ]
    for value in values:
        owner = type(value)
        alias = getattr(
            config if owner in {sources.SourceConfig, sources.AdaptiveConfig} else source_scorer, owner.__name__
        )
        assert alias is owner
        restored = restore_dataclass(asdict(value), alias)
        assert type(restored) is owner
        assert restored == value
    for name in (
        "load_stats",
        "save_stats",
        "load_source_state",
        "save_source_state",
        "load_source_category_map",
        "save_source_category_map",
    ):
        assert getattr(source_scorer, name) is getattr(storage, name)


def test_source_writers_preserve_exact_bytes_and_non_ascii_order(tmp_path: Path) -> None:
    state = sources.SourceStateStore(
        sources={
            "Zürich": sources.SourceStateEntry("2026-10-01", demoted=True),
            "Alpha": sources.SourceStateEntry(graduated=True),
        }
    )
    stats = {
        "Zürich": sources.SourceStats(
            "Zürich", 2, 1, 3, 1, 98.5, "2026-10-07", [sources.DailySnapshot("2026-10-07", 3, 1, True)]
        ),
        "Alpha": sources.SourceStats("Alpha"),
    }
    storage.save_source_state(state, str(tmp_path))
    storage.save_stats(stats, str(tmp_path))
    state_bytes = (tmp_path / storage.SOURCE_STATE_FILE).read_bytes()
    stats_bytes = (tmp_path / storage.STATS_FILE).read_bytes()
    assert state_bytes == json.dumps(asdict(state), indent=2).encode("utf-8")
    assert stats_bytes == json.dumps({name: asdict(value) for name, value in stats.items()}, indent=2).encode("utf-8")
    delivery_state.save_delivery_source_state(state, str(tmp_path))
    delivery_state.save_delivery_source_stats(stats, str(tmp_path))
    assert (tmp_path / storage.SOURCE_STATE_FILE).read_bytes() == state_bytes
    assert (tmp_path / storage.STATS_FILE).read_bytes() == stats_bytes
    declared = [sources.SourceConfig("Zürich", "https://example.org/rss", "Société", True)]
    storage.save_source_category_map(declared, str(tmp_path))
    assert (tmp_path / storage.CATEGORY_MAP_FILE).read_bytes() == json.dumps({"Zürich": "Société"}, indent=2).encode()


def test_legacy_stats_setup_precedes_pruning_but_failed_write_keeps_pruning(tmp_path: Path) -> None:
    stats = {name: sources.SourceStats(name) for name in ("Active", "Stale")}
    with patch.object(Path, "mkdir", side_effect=OSError("setup unavailable")):
        with pytest.raises(OSError, match="setup unavailable"):
            storage.save_stats(stats, str(tmp_path / "new"), active_sources={"Active"})
    assert list(stats) == ["Active", "Stale"]
    with patch.object(storage, "atomic_json_write", side_effect=OSError("replacement unavailable")):
        storage.save_stats(stats, str(tmp_path / "new"), active_sources={"Active"})
    assert (tmp_path / "new").is_dir()
    assert list(stats) == ["Active"]
    assert not (tmp_path / "new" / storage.STATS_FILE).exists()
    with patch.object(storage, "encode_source_stats", side_effect=TypeError("bad record")):
        with pytest.raises(TypeError, match="bad record"):
            storage.save_stats(stats, str(tmp_path))


@pytest.mark.parametrize("kind", ["state", "categories"])
def test_legacy_source_writers_only_catch_atomic_write_failures(tmp_path: Path, kind: str) -> None:
    values = {
        "state": (sources.SourceStateStore(), storage.save_source_state),
        "categories": ([], storage.save_source_category_map),
    }
    value, writer = values[kind]
    with patch.object(Path, "mkdir", side_effect=OSError("mkdir failed")):
        with pytest.raises(OSError, match="mkdir failed"):
            writer(value, str(tmp_path))
    with patch.object(storage, "atomic_json_write", side_effect=OSError("write failed")):
        writer(value, str(tmp_path))


@pytest.mark.parametrize("kind", ["stats", "state"])
def test_prepared_source_writers_do_not_create_directories_prune_or_swallow(tmp_path: Path, kind: str) -> None:
    stats = {name: sources.SourceStats(name) for name in ("Active", "Stale")}
    state = sources.SourceStateStore(sources={"Stale": sources.SourceStateEntry(demoted=True)})
    value, writer = (
        (stats, delivery_state.save_delivery_source_stats)
        if kind == "stats"
        else (state, delivery_state.save_delivery_source_state)
    )
    with pytest.raises(FileNotFoundError):
        writer(value, str(tmp_path / "missing"))
    assert not (tmp_path / "missing").exists()
    with patch.object(delivery_state, "atomic_json_write", side_effect=OSError("replacement failed")):
        with pytest.raises(OSError, match="replacement failed"):
            writer(value, str(tmp_path))
    assert list(stats) == ["Active", "Stale"]
    assert state.sources["Stale"].demoted
