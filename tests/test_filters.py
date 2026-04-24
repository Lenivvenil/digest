"""Tests for blocklist filtering (src/filters.py)."""

from __future__ import annotations

from digest.filters import is_blocked


def test_blocked_when_keyword_present() -> None:
    assert is_blocked("Trump signs new bill", ["trump", "election"]) is True


def test_not_blocked_when_no_keyword() -> None:
    assert is_blocked("Kubernetes 1.30 released", ["trump", "election"]) is False


def test_case_insensitive() -> None:
    assert is_blocked("ELECTION results are in", ["election"]) is True
    assert is_blocked("election", ["ELECTION"]) is True


def test_empty_blocklist_never_blocks() -> None:
    assert is_blocked("anything goes", []) is False


def test_empty_text_not_blocked() -> None:
    assert is_blocked("", ["trump"]) is False


def test_partial_word_match() -> None:
    assert is_blocked("trumpeter plays jazz", ["trump"]) is True


def test_multiple_keywords_first_match() -> None:
    assert is_blocked("Sports news: Biden scores goal", ["biden", "sports"]) is True


def test_keyword_with_spaces() -> None:
    assert is_blocked("breaking news today", ["breaking news"]) is True


def test_unicode_keyword() -> None:
    assert is_blocked("Новости выборов сегодня", ["выборов"]) is True


def test_single_char_keyword() -> None:
    assert is_blocked("abc", ["a"]) is True
    assert is_blocked("xyz", ["a"]) is False
