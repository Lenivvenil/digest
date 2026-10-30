"""Synthetic discarded-teaser ownership tests; not a replay of production HTML."""
from __future__ import annotations

import pytest

from digest.article_source import ExtractionError, extract_html

PROSE = "The synthetic source records a bounded experiment and preserves every limitation. " * 5
MAIN = "".join("<p>" + PROSE + "</p>" for _ in range(10))


def teaser(media: str) -> str:
    return ('<article><p><a href="/unrelated">' + "Linked teaser " * 5 + "</a>"
            + "Unrelated preview " * 9 + "</p>" + media + "</article>")


def page(main_media: str = "", card_media: str = "", *, after: str = "", main: str = MAIN) -> str:
    return "<html><body><main>" + main + main_media + teaser(card_media) * 3 + "</main>" + after + "</body></html>"


@pytest.mark.parametrize("media", [
    "<svg><path/></svg>", "<canvas></canvas>", "<video></video>", "<audio></audio>",
    "<iframe></iframe>", "<object></object>", "<embed>", '<img alt="chart">',
    '<a role="button"><svg width="16" height="16"><text>Preview only</text></svg></a>',
    '<img alt="Preview photograph">',
])
def test_discarded_teaser_media_does_not_contaminate_selected_main(media: str) -> None:
    baseline = extract_html(page())
    selected = extract_html(page(card_media=media))
    assert baseline.extraction_status == selected.extraction_status == "main"
    assert selected.text == baseline.text and "Linked teaser" not in selected.text
    assert selected.coverage_notes == baseline.coverage_notes
    assert selected.extraction_diagnostics is not None
    assert selected.extraction_diagnostics.media_counts == ()


@pytest.mark.parametrize("media,reason", [
    ("<svg><path/></svg>", "tag_svg"), ("<iframe></iframe>", "tag_iframe"),
    ('<img alt="chart">', "chart_img"),
    ('<a role="button"><svg width="16" height="16"><text>Visible result</text></svg></a>',
     "decorative_svg_text"),
])
def test_selected_main_media_still_holds_and_has_only_selected_counts(media: str, reason: str) -> None:
    with pytest.raises(ExtractionError, match="unread_critical_media") as held:
        extract_html(page(main_media=media, card_media=media))
    assert {row.reason: row.count for row in held.value.extraction_diagnostics.media_counts} == {reason: 1}


def test_appended_footnote_media_remains_a_coverage_hold() -> None:
    with pytest.raises(ExtractionError, match="unread_critical_media") as held:
        extract_html(page(card_media="<svg></svg>",
                          after='<aside class="footnotes"><p>Required qualification.</p><iframe></iframe></aside>'))
    assert {row.reason: row.count for row in held.value.extraction_diagnostics.media_counts} == {"tag_iframe": 1}


def test_selected_main_figure_reference_remains_conservative_for_removed_images() -> None:
    with pytest.raises(ExtractionError, match="unread_critical_media") as held:
        extract_html(page(card_media='<img alt="photograph">', main=MAIN + "<p>See in the figure.</p>"))
    assert {row.reason: row.count for row in held.value.extraction_diagnostics.media_counts} == {"text_reference": 1}


def test_global_and_selected_nonmedia_guards_are_not_relaxed() -> None:
    for extra in ('<link rel="next" href="/next">', '<div data-load-more="next"></div>'):
        with pytest.raises(ExtractionError, match="pagination|client_continuation"):
            extract_html(page(main_media=extra, card_media="<svg></svg>"))


def test_short_main_keeps_original_article_selection_hold() -> None:
    with pytest.raises(ExtractionError, match="multiple_article_regions"):
        extract_html(page(card_media="<svg></svg>", main="<p>" + PROSE + "</p>"))
