"""Offline language selection, generation isolation, and honest optional card fields."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from digest import editorial_state as state_api
from digest import editorial_worker as worker
from digest.config import Config
from digest.editorial_fetch import FetchedArticle
from digest.editorial_state import EditorialState, Generation
from digest.llm import LLMRole
from scripts.review_fixture import fixture_config
from tests.factories import make_article

FACT = "The release doubles the configurable connection timeout."
QUALIFICATIONS = (
    "Only clients using protocol version 2 support this setting.",
    "The setting does not change the maximum connection lifetime.",
)
PROSE = {
    "en": {
        "fact": FACT,
        "qualification": QUALIFICATIONS,
        "limitation": "The setting requires protocol version 2 and leaves maximum connection lifetime unchanged.",
        "empty": "This segment contains only padding with no substantive source claims.",
    },
    "ru": {
        "fact": "В выпуске удвоено настраиваемое время ожидания подключения.",
        "qualification": (
            "Настройку поддерживают только клиенты с версией протокола 2.",
            "Настройка не меняет максимальную продолжительность соединения.",
        ),
        "limitation": "Настройка требует версии протокола 2 и не меняет максимальную продолжительность соединения.",
        "empty": "Сегмент содержит только заполнение без содержательных утверждений источника.",
    },
}


def _body(chunks: int, qualified: bool = False) -> str:
    tail = " ".join(QUALIFICATIONS) if qualified else ""
    return FACT + " " + "x" * (state_api.CHUNK_WEIGHT * chunks - len(FACT) - len(tail) - 2) + " " + tail


def _answer(payload: Any, language: str) -> dict[str, Any]:
    prose = PROSE[language]
    if isinstance(payload, list):
        return {"claims": [{"kind": claim["kind"], "text": claim["text"], "supports": [claim["claim_id"]]}
                           for node in payload for claim in node["claims"]], "empty_reason": ""}
    if "source_spans" in payload:
        findings = []
        statements = [("fact", FACT, prose["fact"])] + [
            ("qualification", source, text)
            for source, text in zip(QUALIFICATIONS, prose["qualification"], strict=True)
        ]
        for kind, source, text in statements:
            refs = [span["source_id"] for span in payload["source_spans"] if source in span["text"]]
            if refs:
                findings.append({"kind": kind, "text": text, "source_ids": refs})
        if "chunk_id" in payload:
            return {"claims": findings, "empty_reason": "" if findings else prose["empty"]}
        facts = list(dict.fromkeys(ref for claim in findings if claim["kind"] == "fact"
                                   for ref in claim["source_ids"]))
        qualifications = list(dict.fromkeys(ref for claim in findings if claim["kind"] == "qualification"
                                            for ref in claim["source_ids"]))
    else:
        facts = [claim["claim_id"] for claim in payload["findings"] if claim["kind"] == "fact"]
        qualifications = [claim["claim_id"] for claim in payload["findings"] if claim["kind"] == "qualification"]
    return {
        "decision": "ready", "reason": "", "value_score": 7,
        "fact": {"text": prose["fact"], "claim_ids": facts},
        "inference": None,
        "limitation": {"text": prose["limitation"], "claim_ids": qualifications} if qualifications else None,
        "why_read": None,
        "value_rationale": "A concrete protocol setting changed.", "event_key": "connection timeout setting",
    }


@dataclass
class LanguageProvider:
    config: Config = field(default_factory=fixture_config)
    body: str = field(default_factory=lambda: _body(1))
    calls: list[dict[str, Any]] = field(default_factory=list)
    fetches: list[str] = field(default_factory=list)
    damage: str = ""


@pytest.fixture
def language_provider(monkeypatch: pytest.MonkeyPatch) -> LanguageProvider:
    adapter = LanguageProvider()
    monkeypatch.setattr(worker, "GROQ_SPACING_SECONDS", 0)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Language contracts must never make a live HTTP request")

    async def fetch(url: str) -> FetchedArticle:
        adapter.fetches.append(url)
        return FetchedArticle(adapter.body, url, "2026-10-01T03:00:00+00:00",
                              "2026-09-30T12:00:00+00:00", "article", ("Complete synthetic source.",))

    async def complete(role: LLMRole, messages: list[dict[str, str]], config: Config,
                       **kwargs: Any) -> tuple[str, dict[str, int]]:
        payload = json.loads(messages[1]["content"])
        stage = "reduce" if isinstance(payload, list) else "chunk" if "chunk_id" in payload else "final"
        adapter.calls.append({"stage": stage, "messages": deepcopy(messages)})
        answer = _answer(payload, config.radar.language)
        if stage == "final":
            if adapter.damage == "null_limitation":
                answer["limitation"] = None
            elif adapter.damage == "partial_limitation":
                answer["limitation"]["claim_ids"].pop()
            elif adapter.damage == "null_fact":
                answer["fact"] = None
        return json.dumps(answer, ensure_ascii=False), {"prompt_tokens": 20, "completion_tokens": 30}

    monkeypatch.setattr("httpx.Client", forbidden)
    monkeypatch.setattr("httpx.AsyncClient", forbidden)
    monkeypatch.setattr(worker, "fetch_article", fetch)
    monkeypatch.setattr(worker, "complete", complete)
    return adapter


def _generation(state: EditorialState, config: Config) -> Generation:
    work = state.articles[state.order[0]]
    selected = config.review.primary
    generation = state_api.current_generation(work, selected.provider, selected.model, language=config.radar.language)
    assert generation is not None
    return generation


@pytest.mark.parametrize("language", ["en", "ru"])
@pytest.mark.parametrize("chunks", [1, 2])
async def test_configured_language_survives_generation_storage_and_optional_field_rendering(
    tmp_path: Path, language_provider: LanguageProvider, language: str, chunks: int,
) -> None:
    adapter = language_provider
    adapter.config.radar.language = language
    adapter.body = _body(chunks)
    result = await worker.run_editorial_pass(adapter.config, tmp_path, [make_article()], max_calls=3)
    generation = _generation(result.state, adapter.config)
    assert generation.final is not None and generation.final.decision == "ready"
    assert generation.final.inference is generation.final.limitation is generation.final.why_read is None
    assert generation.prompt_version == state_api.prompt_version_for(language)
    assert state_api.generation_language(generation) == language
    assert [call["stage"] for call in adapter.calls] == (["final"] if chunks == 1 else ["chunk", "chunk", "final"])
    assert all(("English" if language == "en" else "Russian") in call["messages"][0]["content"]
               for call in adapter.calls)
    ready = state_api.ready_results(result.state, adapter.config)
    assert len(ready) == 1 and ready[0].language == language
    rendered = ready[0].to_article_summary().summary
    assert PROSE[language]["fact"] in rendered
    assert ("Source fact:" if language == "en" else "Факт из источника:") in rendered
    assert all(label not in rendered for label in (
        "Interpretation:", "Qualification:", "Why read:", "Вывод модели:", "Ограничение:", "Зачем читать:", "None",
    ))
    assert state_api.load_state(tmp_path) == result.state
    resumed = await worker.run_editorial_pass(adapter.config, tmp_path, [], max_calls=1)
    assert resumed.summary.ready == 1 and resumed.summary.calls_this_pass == 0


async def test_changing_output_language_never_reuses_another_language_generation(
    tmp_path: Path, language_provider: LanguageProvider,
) -> None:
    adapter = language_provider
    adapter.config.radar.language = "en"
    first = await worker.run_editorial_pass(adapter.config, tmp_path, [make_article()], max_calls=1)
    english = deepcopy(_generation(first.state, adapter.config))
    assert english.final is not None
    adapter.config.radar.language = "ru"
    assert state_api.ready_results(first.state, adapter.config) == []
    second = await worker.run_editorial_pass(adapter.config, tmp_path, [], max_calls=1)
    russian = _generation(second.state, adapter.config)
    assert russian.final is not None and russian.generation_id != english.generation_id
    work = second.state.articles[second.state.order[0]]
    assert work.generations[english.generation_id] == english
    assert len(work.generations) == len(adapter.calls) == 2 and len(adapter.fetches) == 1
    assert state_api.ready_results(second.state, adapter.config)[0].language == "ru"
    adapter.config.radar.language = "en"
    resumed = await worker.run_editorial_pass(adapter.config, tmp_path, [], max_calls=1)
    assert resumed.summary.calls_this_pass == 0
    assert _generation(resumed.state, adapter.config) == english
    assert state_api.ready_results(resumed.state, adapter.config)[0].language == "en"


@pytest.mark.parametrize("language", ["en", "ru"])
async def test_qualification_findings_keep_language_and_lineage_through_reduction(
    tmp_path: Path, language_provider: LanguageProvider, language: str,
) -> None:
    adapter = language_provider
    adapter.config.radar.language = language
    adapter.body = _body(2, qualified=True)
    result = await worker.run_editorial_pass(adapter.config, tmp_path, [make_article()], max_calls=3)
    generation = _generation(result.state, adapter.config)
    assert generation.final is not None and generation.final.limitation is not None
    assert generation.final.inference is generation.final.why_read is None
    rendered = state_api.ready_results(result.state, adapter.config)[0].to_article_summary().summary
    assert ("Qualification:" if language == "en" else "Ограничение:") in rendered
    assert PROSE[language]["limitation"] in rendered
    children = tuple(node for node in generation.nodes.values() if node.stage == "chunk")
    messages = worker._reduce_messages(children, language=language)
    assert ("English" if language == "en" else "Russian") in messages[0]["content"]
    task = worker.Task("reduce", "language-reduction", messages, children=children, language=language)
    response = _answer(json.loads(messages[1]["content"]), language)
    reduced = worker.parse_node(task, json.dumps(response, ensure_ascii=False), adapter.body, {})
    supplied = [claim for child in children for claim in child.claims]
    assert [claim.text for claim in reduced.claims] == [claim.text for claim in supplied]
    assert {ref for claim in reduced.claims for ref in claim.supports} == {claim.claim_id for claim in supplied}
    assert sum(claim.kind == "qualification" for claim in reduced.claims) == len(QUALIFICATIONS)


@pytest.mark.parametrize("language", ["en", "ru"])
@pytest.mark.parametrize("damage", ["null_limitation", "partial_limitation", "null_fact"])
async def test_optional_sections_cannot_erase_source_qualifications_or_the_fact(
    tmp_path: Path, language_provider: LanguageProvider, language: str, damage: str,
) -> None:
    adapter = language_provider
    adapter.config.radar.language = language
    adapter.body = _body(2, qualified=True)
    adapter.damage = damage
    result = await worker.run_editorial_pass(adapter.config, tmp_path, [make_article()], max_calls=3)
    generation = _generation(result.state, adapter.config)
    assert any(claim.kind == "qualification" for node in generation.nodes.values() for claim in node.claims)
    assert generation.final is None and generation.attempts[-1].status == "failed"
    assert result.summary.pending == 1 and result.summary.ready == result.summary.rejected == 0
    assert state_api.ready_results(result.state, adapter.config) == []


def test_literal_source_wording_is_not_rejected_by_a_filler_keyword() -> None:
    examples = {"en": "The vendor says this feature may be useful only for small local pilots.",
                "ru": "Поставщик пишет, что функция может быть полезной только для небольших локальных испытаний."}
    for language, text in examples.items():
        assert worker._editorial_text(text, 500, language) == text
