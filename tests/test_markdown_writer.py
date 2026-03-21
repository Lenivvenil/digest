"""Tests for src/markdown_writer.py — Markdown file output."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


from src.config import Config, LLMConfig, ProviderConfig, DeliveryConfig, DigestConfig
from src.markdown_writer import write_digest, _build_frontmatter


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_config(
    *,
    markdown_to_repo: bool = True,
    markdown_dir: str = "digests",
) -> Config:
    return Config(
        llm=LLMConfig(providers=[ProviderConfig(name="anthropic", model="claude-sonnet-4-20250514")]),
        delivery=DeliveryConfig(
            telegram=False,
            markdown_to_repo=markdown_to_repo,
            markdown_dir=markdown_dir,
        ),
        digest=DigestConfig(
            language="ru",
            max_articles_per_source=5,
            max_total_articles=30,
            summary_style="analytical",
        ),
        sources=[],
    )


FIXED_DATE = datetime(2026, 3, 14, 10, 0, 0, tzinfo=timezone.utc)
SUMMARY = "## AI & LLM\n\nSome digest content here.\n\n## Trends\n\nKey trends."


# ---------------------------------------------------------------------------
# _build_frontmatter
# ---------------------------------------------------------------------------


def test_frontmatter_contains_required_fields() -> None:
    fm = _build_frontmatter(
        date_str="2026-03-14",
        sources_count=10,
        articles_count=25,
        llm_provider="anthropic",
    )
    assert "---" in fm
    assert "title: Daily Digest 2026-03-14" in fm
    assert "date: 2026-03-14" in fm
    assert "sources_count: 10" in fm
    assert "articles_count: 25" in fm
    assert "llm_provider: anthropic" in fm
    assert "tags: [digest, daily]" in fm


def test_frontmatter_starts_and_ends_with_dashes() -> None:
    fm = _build_frontmatter(
        date_str="2026-03-14",
        sources_count=0,
        articles_count=0,
        llm_provider="gemini",
    )
    lines = fm.strip().splitlines()
    assert lines[0] == "---"
    assert lines[-1] == "---"


# ---------------------------------------------------------------------------
# write_digest — disabled delivery
# ---------------------------------------------------------------------------


def test_write_digest_disabled_returns_none(tmp_path: Path) -> None:
    config = make_config(markdown_to_repo=False, markdown_dir=str(tmp_path / "digests"))
    result = write_digest(SUMMARY, config, date=FIXED_DATE)
    assert result is None


def test_write_digest_disabled_creates_no_file(tmp_path: Path) -> None:
    digest_dir = tmp_path / "digests"
    config = make_config(markdown_to_repo=False, markdown_dir=str(digest_dir))
    write_digest(SUMMARY, config, date=FIXED_DATE)
    assert not digest_dir.exists()


# ---------------------------------------------------------------------------
# write_digest — file creation
# ---------------------------------------------------------------------------


def test_write_digest_creates_file(tmp_path: Path) -> None:
    config = make_config(markdown_dir=str(tmp_path / "digests"))
    path = write_digest(SUMMARY, config, date=FIXED_DATE)
    assert path is not None
    assert path.exists()


def test_write_digest_correct_filename(tmp_path: Path) -> None:
    config = make_config(markdown_dir=str(tmp_path / "digests"))
    path = write_digest(SUMMARY, config, date=FIXED_DATE)
    assert path is not None
    assert path.name == "2026-03-14.md"


def test_write_digest_creates_directory_if_missing(tmp_path: Path) -> None:
    nested_dir = tmp_path / "a" / "b" / "digests"
    config = make_config(markdown_dir=str(nested_dir))
    path = write_digest(SUMMARY, config, date=FIXED_DATE)
    assert path is not None
    assert nested_dir.exists()


def test_write_digest_returns_path_object(tmp_path: Path) -> None:
    config = make_config(markdown_dir=str(tmp_path / "digests"))
    path = write_digest(SUMMARY, config, date=FIXED_DATE)
    assert isinstance(path, Path)


# ---------------------------------------------------------------------------
# write_digest — file content
# ---------------------------------------------------------------------------


def test_write_digest_file_has_frontmatter(tmp_path: Path) -> None:
    config = make_config(markdown_dir=str(tmp_path / "digests"))
    path = write_digest(SUMMARY, config, date=FIXED_DATE, sources_count=5, articles_count=20)
    assert path is not None
    content = path.read_text(encoding="utf-8")
    assert content.startswith("---\n")
    assert "title: Daily Digest 2026-03-14" in content
    assert "date: 2026-03-14" in content
    assert "sources_count: 5" in content
    assert "articles_count: 20" in content
    assert "llm_provider: anthropic" in content
    assert "tags: [digest, daily]" in content


def test_write_digest_file_contains_summary(tmp_path: Path) -> None:
    config = make_config(markdown_dir=str(tmp_path / "digests"))
    path = write_digest(SUMMARY, config, date=FIXED_DATE)
    assert path is not None
    content = path.read_text(encoding="utf-8")
    assert SUMMARY in content


def test_write_digest_frontmatter_precedes_body(tmp_path: Path) -> None:
    config = make_config(markdown_dir=str(tmp_path / "digests"))
    path = write_digest(SUMMARY, config, date=FIXED_DATE)
    assert path is not None
    content = path.read_text(encoding="utf-8")
    # Second occurrence of --- closes the frontmatter
    second_dashes = content.index("---", 3)
    summary_pos = content.index("## AI")
    assert second_dashes < summary_pos


def test_write_digest_utf8_encoding(tmp_path: Path) -> None:
    config = make_config(markdown_dir=str(tmp_path / "digests"))
    cyrillic_summary = "## Тренды\n\nИскусственный интеллект меняет мир."
    path = write_digest(cyrillic_summary, config, date=FIXED_DATE)
    assert path is not None
    content = path.read_text(encoding="utf-8")
    assert "Тренды" in content
    assert "Искусственный интеллект" in content


# ---------------------------------------------------------------------------
# write_digest — overwrite (idempotent re-runs)
# ---------------------------------------------------------------------------


def test_write_digest_overwrites_existing_file(tmp_path: Path) -> None:
    config = make_config(markdown_dir=str(tmp_path / "digests"))

    # First write
    write_digest("First version of the digest.", config, date=FIXED_DATE)
    # Second write with different content
    path = write_digest("Second version of the digest.", config, date=FIXED_DATE)

    assert path is not None
    content = path.read_text(encoding="utf-8")
    assert "Second version" in content
    assert "First version" not in content


def test_write_digest_overwrites_preserves_correct_metadata(tmp_path: Path) -> None:
    config = make_config(markdown_dir=str(tmp_path / "digests"))

    write_digest(SUMMARY, config, date=FIXED_DATE, sources_count=3, articles_count=10)
    path = write_digest(SUMMARY, config, date=FIXED_DATE, sources_count=8, articles_count=22)

    assert path is not None
    content = path.read_text(encoding="utf-8")
    assert "sources_count: 8" in content
    assert "articles_count: 22" in content
    assert "sources_count: 3" not in content


# ---------------------------------------------------------------------------
# write_digest — default date uses today
# ---------------------------------------------------------------------------


def test_write_digest_default_date_uses_today(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    config = make_config(markdown_dir=str(tmp_path / "digests"))
    path = write_digest(SUMMARY, config)  # no date argument

    assert path is not None
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert path.name == f"{today_str}.md"
