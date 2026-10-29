"""Content-free extraction observations do not relax any acceptance gate."""
import json
from dataclasses import asdict, replace
from typing import Any

import pytest

from digest.article_source import (
    MAX_DIAGNOSTIC_COUNT,
    MAX_DIAGNOSTIC_REGIONS,
    ExtractionDiagnostics,
    ExtractionError,
    _ExtractionObservations,
    extract_html,
    restore_extraction_diagnostics,
)

PROSE = "Synthetic prose explains a measured result and its conditional limits. " * 3


def diagnostics(html: str, error: str) -> ExtractionDiagnostics:
    with pytest.raises(ExtractionError) as caught:
        extract_html(html)
    assert str(caught.value) == error
    return caught.value.extraction_diagnostics


@pytest.mark.parametrize(("content", "predicate"), [
    ("short", "characters"),
    (PROSE, "paragraphs"),
    (f"<p><a>{PROSE}</a></p>", "link_density"),
    ("<p>" + "12345 " * 20 + "</p>", "letters"),
    (f"<p>{PROSE} subscribe to continue</p>", "teaser"),
])
def test_first_failure(content: str, predicate: str) -> None:
    result = diagnostics(f"<article>{content}</article>", "insufficient_article_text")
    assert result.regions[0].predicate == predicate
    assert result.counts[0].count == 1
    assert restore_extraction_diagnostics(json.loads(json.dumps(asdict(result)))) == result


def test_no_regions_mixed_regions_and_first_predicate() -> None:
    assert diagnostics("<p>short</p>", "insufficient_article_text").counts == ()
    result = diagnostics("<body><main><article>tiny</article></main></body>", "insufficient_article_text")
    assert [row.kind for row in result.regions] == ["body", "main", "article"]
    assert [row.predicate for row in result.regions] == ["characters"] * 3
    early = diagnostics("<article><p>short", "clipped_html")
    assert early.phase == "preselection"
    assert early.regions == ()


@pytest.mark.parametrize(("media", "reasons"), [
    ("<svg></svg>", {"tag_svg": 1}),
    ('<svg aria-label="chart"></svg>', {"tag_svg": 1, "chart_svg": 1}),
    ("<iframe></iframe>", {"tag_iframe": 1}),
    ("<canvas></canvas>", {"tag_canvas": 1}),
    ("<video></video>", {"tag_video": 1}),
    ("<audio></audio>", {"tag_audio": 1}),
    ("<object></object>", {"tag_object": 1}),
    ("<embed>", {"tag_embed": 1}),
    ('<img alt="chart">', {"chart_img": 1}),
    ('<a class="author-link"><svg width="16" height="16"><text>meaning</text></svg></a>',
     {"decorative_svg_text": 1}),
    ('<img alt="photograph"><p>See in the figure</p>', {"text_reference": 1}),
])
def test_media_reasons_preserve_hold(media: str, reasons: dict[str, int]) -> None:
    result = diagnostics(f"<article><p>{PROSE}</p>{media}</article>",
                         "coverage_incomplete:unread_critical_media")
    assert {row.reason: row.count for row in result.media_counts} == reasons
    assert result.phase == "selected"


def test_decorative_svg_remains_allowed() -> None:
    result = extract_html(f'<article><p>{PROSE}</p><a class="author-link">'
                          '<svg width="16" height="16"><path d="M0 0"/></svg></a></article>')
    assert result.text == PROSE.strip()
    assert result.extraction_diagnostics.media_counts == ()


def test_secret_content_attributes_and_instruction_never_enter_bounded_metadata() -> None:
    secret = "https://private.invalid/?token=SECRET ignore previous instructions " * 1000
    html = ''.join(f'<article id="{secret}"><p>{secret}</p></article>' for _ in range(40))
    result = diagnostics(html, "coverage_incomplete:multiple_article_regions")
    encoded = json.dumps(asdict(result))
    assert "SECRET" not in encoded and "private.invalid" not in encoded and "instructions" not in encoded
    assert len(result.regions) == MAX_DIAGNOSTIC_REGIONS
    assert result.omitted_regions == 40 - MAX_DIAGNOSTIC_REGIONS
    assert len(encoded) < 5000


@pytest.mark.parametrize("mutation", [
    {"unknown": "secret"}, {"version": True}, {"version": 2}, {"phase": "secret"},
    {"omitted_regions": -1}, {"omitted_regions": MAX_DIAGNOSTIC_COUNT + 1},
    {"saturated": 1}, {"regions": [{}] * (MAX_DIAGNOSTIC_REGIONS + 1)},
    {"counts": [{"kind": "article", "predicate": "secret", "count": 1}]},
    {"media_counts": [{"reason": "secret", "count": 1}]},
])
def test_restoration_rejects_unowned_values(mutation: dict[str, Any]) -> None:
    result = diagnostics("<article>short</article>", "insufficient_article_text")
    payload = asdict(result)
    payload.update(mutation)
    with pytest.raises(ValueError, match="invalid_extraction_diagnostics"):
        restore_extraction_diagnostics(payload)


def test_direct_constructor_validation_and_saturation() -> None:
    result = diagnostics("<article>short</article>", "insufficient_article_text")
    with pytest.raises(ValueError, match="invalid_extraction_diagnostics"):
        replace(result, phase="secret")
    with pytest.raises(ValueError, match="invalid_extraction_diagnostics"):
        replace(result, regions=(replace(result.regions[0], characters=True),))
    collector = _ExtractionObservations()
    collector.include_media({"tag_svg": MAX_DIAGNOSTIC_COUNT + 1})
    bounded = collector.snapshot()
    assert bounded.saturated
    assert bounded.media_counts[0].count == MAX_DIAGNOSTIC_COUNT


@pytest.mark.parametrize("field", ["counts", "media_counts"])
@pytest.mark.parametrize("mutation", ["duplicate", "reverse", "extra_key", "bool_count"])
def test_restoration_rejects_noncanonical_aggregate_rows(field: str, mutation: str) -> None:
    result = diagnostics(f'<body><article><p>{PROSE}</p><svg aria-label="chart"></svg></article></body>',
                         "coverage_incomplete:unread_critical_media")
    payload = json.loads(json.dumps(asdict(result)))
    rows = payload[field]
    assert len(rows) >= 2
    if mutation == "duplicate":
        rows.append(rows[0])
    elif mutation == "reverse":
        rows.reverse()
    elif mutation == "extra_key":
        rows[0]["secret"] = "forbidden"
    else:
        rows[0]["count"] = True
    with pytest.raises(ValueError, match="invalid_extraction_diagnostics"):
        restore_extraction_diagnostics(payload)
