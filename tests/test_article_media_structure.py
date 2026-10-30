"""Synthetic structural regressions, not replays of retained production HTML."""
from __future__ import annotations

import pytest

from digest.article_source import ExtractionError, extract_html

PROSE = "Synthetic article describes a conditional measured result and its limitations. " * 4


def article(extra: str) -> str:
    return f"<html><body><article><p>{PROSE}</p>{extra}</article></body></html>"


def icon(contents: str) -> str:
    return f'<a class="author-link"><svg width="16" height="16">{contents}</svg></a>'


@pytest.mark.parametrize("contents", [
    "<svg><path/></svg>", "<iframe></iframe>", '<img alt="chart">',
    "<p>Inert template text is not article evidence.</p>",
])
def test_ordinary_inert_template_does_not_supply_text_or_media(contents: str) -> None:
    result = extract_html(article(f"<template>{contents}</template>"))
    assert result.text == PROSE.strip()
    assert result.extraction_diagnostics is not None
    assert result.extraction_diagnostics.media_counts == ()


@pytest.mark.parametrize("contents", [
    '<text style="display:none">label</text>',
    '<text style="visibility:hidden">label</text>',
    '<text><tspan style="display:none">label</tspan></text>',
    '<g style="display:none"><text>label</text></g>',
    '<g style="display:none"><text><tspan>label</tspan></text></g>',
])
def test_explicitly_suppressed_decorative_svg_text_does_not_flip_guard(contents: str) -> None:
    result = extract_html(article(icon(contents)))
    assert result.text == PROSE.strip()
    assert result.extraction_diagnostics is not None
    assert result.extraction_diagnostics.media_counts == ()


@pytest.mark.parametrize("contents", [
    "<title>Important graph</title>", "<desc>Important graph</desc>", "<text>Important graph</text>",
    "<text><tspan>Important graph</tspan></text>",
    "<g hidden><text>hidden</text></g><text>visible</text>",
    '<g style="visibility:hidden"><text style="visibility:visible">visible</text></g>',
])
def test_visible_svg_text_still_holds(contents: str) -> None:
    with pytest.raises(ExtractionError, match="coverage_incomplete:unread_critical_media"):
        extract_html(article(icon(contents)))


@pytest.mark.parametrize("attributes", ['shadowrootmode="open"', 'shadowrootmode="closed"',
                                         'shadowrootmode="unknown"', 'shadowrootmode=""'])
@pytest.mark.parametrize("contents", ["<svg><path/></svg>", "<p>Shadow content is not ordinary article text.</p>"])
def test_shadowroot_templates_are_not_treated_as_ordinary_inert_templates(attributes: str, contents: str) -> None:
    with pytest.raises(ExtractionError, match="unread_template_content"):
        extract_html(article(f"<template {attributes}>{contents}</template>"))


def test_client_continuation_inside_template_remains_held() -> None:
    with pytest.raises(ExtractionError, match="coverage_incomplete:client_continuation"):
        extract_html(article('<template><div data-load-more="next">unresolved</div></template>'))


def test_template_only_article_is_not_promoted_to_evidence() -> None:
    with pytest.raises(ExtractionError, match="insufficient_article_text"):
        extract_html(f"<article><template><p>{PROSE}</p></template></article>")


@pytest.mark.parametrize("contents", ["<text>label</text>", "<path/>"])
def test_foreign_namespace_template_is_not_assumed_inert(contents: str) -> None:
    with pytest.raises(ExtractionError, match="unread_template_content"):
        extract_html(article(icon(f"<template>{contents}</template>")))


def test_shadow_template_inside_svg_is_not_hidden_by_svg_extraction_skip() -> None:
    with pytest.raises(ExtractionError, match="unread_template_content"):
        extract_html(article(icon('<template shadowrootmode="open"><path/></template>')))


def test_template_does_not_bypass_paywall_guard() -> None:
    with pytest.raises(ExtractionError, match="paywall_or_teaser"):
        extract_html(article('<template><div class="paywall">unresolved</div></template>'))


@pytest.mark.parametrize("target", ["target-marker", ""])
def test_content_patch_template_remains_held(target: str) -> None:
    with pytest.raises(ExtractionError, match="unread_template_content"):
        extract_html(article(f'<marker id="target-marker"></marker><template for="{target}">'
                             '<p>Potentially rendered content.</p></template>'))


def test_foreign_object_template_is_not_assumed_to_have_html_namespace() -> None:
    with pytest.raises(ExtractionError, match="unread_template_content"):
        extract_html(article(icon('<foreignObject><template><p>unknown</p></template></foreignObject>')))


@pytest.mark.parametrize("style", [
    "display:none;display:block", "visibility:hidden;visibility:visible",
    "display:none;display:var(--display)", "visibility:hidden;all:initial",
    "display:none!important;display:block!important", "visibility:var(--visibility)",
])
def test_ambiguous_descendant_css_does_not_suppress_svg_text(style: str) -> None:
    with pytest.raises(ExtractionError, match="unread_critical_media"):
        extract_html(article(icon(f'<text style="{style}">meaningful text</text>')))


@pytest.mark.parametrize("style", ["visibility:visible", "all:initial", "visibility:var(--visibility)",
                                  "visibility:hidden;visibility:visible"])
def test_child_css_cannot_silently_inherit_a_visibility_hold(style: str) -> None:
    with pytest.raises(ExtractionError, match="unread_critical_media"):
        extract_html(article(icon(f'<g style="visibility:hidden"><text style="{style}">'
                                  'meaningful text</text></g>')))


@pytest.mark.parametrize("attributes", ['shadowrootmode="open"', 'shadowrootmode="closed"',
                                         'shadowrootmode="unknown"', 'for="marker"', 'for=""'])
def test_article_opened_inside_active_template_cannot_escape_hold(attributes: str) -> None:
    with pytest.raises(ExtractionError, match="unread_template_content"):
        extract_html(f'<html><body><template {attributes}><article><p>{PROSE}</p>'
                     '</article></template></body></html>')


def test_article_opened_inside_ordinary_template_is_not_selected() -> None:
    with pytest.raises(ExtractionError, match="insufficient_article_text"):
        extract_html(f'<html><body><template><article><p>{PROSE}</p></article></template></body></html>')


@pytest.mark.parametrize("attributes", [
    'hidden', 'aria-hidden="true"', 'class="share"', 'class="navigation"',
    'hidden style="display:block"', 'hidden style="display:var(--display)"',
])
def test_nonvisual_hints_and_contradicted_hidden_do_not_suppress_svg_evidence(attributes: str) -> None:
    with pytest.raises(ExtractionError, match="unread_critical_media"):
        extract_html(article(icon(f'<text {attributes}>Important graph result</text>')))


@pytest.mark.parametrize("contents", [
    '<g hidden><text>Important graph result</text></g>',
    '<text><tspan hidden>Important graph result</tspan></text>',
])
def test_bare_svg_hidden_is_not_a_visual_suppression_rule(contents: str) -> None:
    with pytest.raises(ExtractionError, match="unread_critical_media"):
        extract_html(article(icon(contents)))


@pytest.mark.parametrize("value", ["visible", "var(--visibility)", "inherit", ""])
def test_visibility_attribute_cannot_silently_inherit_suppression(value: str) -> None:
    with pytest.raises(ExtractionError, match="unread_critical_media"):
        extract_html(article(icon(f'<g style="visibility:hidden"><text visibility="{value}">'
                                  'Important evidence</text></g>')))


@pytest.mark.parametrize("extra", [
    '<template><p>Unclosed inert content</p>',
    '<template/><p>Content after nonvoid self-closing syntax</p>',
    '<template><template></template>',
])
def test_unresolved_template_scope_remains_held(extra: str) -> None:
    with pytest.raises(ExtractionError, match="unread_template_content"):
        extract_html(article(extra))


def test_trailing_unclosed_template_is_not_assumed_complete() -> None:
    with pytest.raises(ExtractionError, match="unread_template_content"):
        extract_html(article("") + '<template><p>Unknown tail</p>')


@pytest.mark.parametrize("attributes", ['shadowrootmode="open"', 'for="marker"'])
@pytest.mark.parametrize("style", ["display:none", "visibility:hidden"])
def test_active_template_style_does_not_prove_instantiated_content_hidden(attributes: str, style: str) -> None:
    with pytest.raises(ExtractionError, match="unread_template_content"):
        extract_html(article(f'<template {attributes} style="{style}"><p>Unresolved content</p></template>'))


@pytest.mark.parametrize("namespace", ["http://www.w3.org/2000/svg", "urn:unknown", ""])
@pytest.mark.parametrize("ancestor", [False, True])
def test_non_html_default_namespace_templates_remain_unresolved(namespace: str, ancestor: bool) -> None:
    content = (f'<div xmlns="{namespace}"><template><svg></svg></template></div>' if ancestor
               else f'<template xmlns="{namespace}"><svg></svg></template>')
    with pytest.raises(ExtractionError, match="unread_template_content"):
        extract_html(article(content))


@pytest.mark.parametrize("content", [
    '<template><svg></svg></template>',
    '<template xmlns="http://www.w3.org/1999/xhtml"><svg></svg></template>',
    '<div xmlns="http://www.w3.org/1999/xhtml"><template><svg></svg></template></div>',
])
def test_ordinary_and_explicit_xhtml_templates_remain_inert(content: str) -> None:
    assert extract_html(article(content)).text == PROSE.strip()


@pytest.mark.parametrize("style", ["display:n one", "visibility:h idden", "d isplay:none", "visib ility:hidden",
                                  "display:\vnone", "visibility:\vhidden"])
def test_invalid_css_identifier_whitespace_does_not_invent_suppression(style: str) -> None:
    with pytest.raises(ExtractionError, match="unread_critical_media"):
        extract_html(article(icon(f'<text style="{style}">Meaningful visible evidence</text>')))


@pytest.mark.parametrize("style", [" DISPLAY : none ; ", "\nvisibility : HIDDEN\t; ", "display:\nnone"])
def test_supported_css_whitespace_around_tokens_preserves_suppression(style: str) -> None:
    assert extract_html(article(icon(f'<text style="{style}">Invisible text</text>'))).text == PROSE.strip()
