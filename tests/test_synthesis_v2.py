"""V2 source scope, full-context evidence and legacy no-replay contracts.

Synthetic semantic counterexamples remain editorial failures, not passed quality
checks: structural acceptance is deliberately not advertised as entailment.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
import respx

from digest._serialization import canonical_json_bytes
from digest.adapters.models.execution import ModelExecution
from digest.adapters.storage import synthesis as storage
from digest.application import synthesis
from digest.config import SynthesisConfig, TelegramConfig, TranslationConfig
from digest.delivery import edition
from digest.domain.editorial.reviews import EvidenceItem
from digest.domain.editorial.synthesis import EvidenceBinding, validate_response
from digest.presentation.telegram import synthesis_text
from digest.translation import protected_content_equal
from scripts.review_fixture import fixture_config
from tests.test_prepared_edition import API, NOW, sender_config, success
from tests.test_synthesis import EVIDENCE, reservation

FIXTURES = Path(__file__).parent / "fixtures" / "synthesis"
LOCAL = "Local Guard processes private messages on the device and sends no raw input to a server."
PIPELINE = "Alert Inspector separates deterministic evidence collection from model inference for security alerts."


def binding(index: int, excerpt: str) -> EvidenceBinding:
    return EvidenceBinding(
        EvidenceItem(
            str(index) * 32,
            f"Synthetic system {index}",
            f"https://example.com/{index}",
            f"Synthetic source {index}",
            "Architecture",
            None,
            excerpt,
            False,
        ),
        str(index) * 64,
        f"Synthetic source {index}",
        f"https://example.com/{index}/rss",
    )


@pytest.fixture
def pair() -> tuple[EvidenceBinding, ...]:
    return (binding(1, LOCAL), binding(2, PIPELINE))


@pytest.fixture
def source_specific(pair: tuple[EvidenceBinding, ...]) -> dict[str, Any]:
    observations = (
        (
            "Processes private messages on the device without sending raw input to a server.",
            "Обрабатывает личные сообщения на устройстве без отправки исходных данных на сервер.",
        ),
        (
            "Separates deterministic evidence collection from model inference for security alerts.",
            "Отделяет детерминированный сбор данных от вывода модели при анализе сигналов безопасности.",
        ),
    )
    return {
        "schema_version": 2,
        "publication_language": "ru",
        "status": "comparison",
        "abstention_reason": None,
        "comparison": {
            "supports": [
                {
                    "evidence_id": b.evidence.evidence_id,
                    "occurrence_sha256": b.occurrence_sha256,
                    "canonical_observation": observations[i][0],
                    "presentation_observation": observations[i][1],
                }
                for i, b in enumerate(pair)
            ],
            "premise_ids": [b.evidence.evidence_id for b in pair],
            "canonical": {
                "interpretation": "Data transfer and separation of collection from inference are distinct "
                "design questions; check each independently if your project needs both.",
                "qualification": "The excerpts do not establish that either implementation has both properties.",
            },
            "presentation": {
                "interpretation": "Передача данных и разделение сбора и вывода модели требуют отдельных решений; "
                "проверяйте каждое свойство, если проекту нужны оба.",
                "qualification": "Отрывки не устанавливают, что какая-либо из систем обладает обоими свойствами.",
            },
        },
    }


def parse(value: dict[str, Any], pair: tuple[EvidenceBinding, ...]) -> Any:
    return validate_response(
        json.dumps(value, ensure_ascii=False), "stop", value["publication_language"], pair, protected_content_equal
    )


def test_source_specific_positive_is_useful_russian_and_not_an_english_quote_dump(
    pair: tuple[EvidenceBinding, ...],
    source_specific: dict[str, Any],
) -> None:
    result = parse(source_specific, pair)
    text = synthesis_text(result, pair)
    for support, bound in zip(result.comparison.supports, pair, strict=True):
        assert support.quote == bound.evidence.excerpt
        assert f"{bound.source}: {support.presentation_observation}" in text
        assert bound.evidence.url in text
        assert support.quote not in text and support.canonical_observation not in text
    assert result.comparison.presentation.interpretation in text
    assert result.comparison.presentation.qualification in text
    assert "не проверено" not in text
    assert "shared_mechanism" not in json.dumps(source_specific)


def test_full_material_context_is_attached_by_code_without_model_echo_or_clipping(
    pair: tuple[EvidenceBinding, ...], source_specific: dict[str, Any],
) -> None:
    context = LOCAL + " This guarantee does not apply to uploaded attachments."
    pair = (replace(pair[0], evidence=replace(pair[0].evidence, excerpt=context)), pair[1])
    result = parse(source_specific, pair)
    assert result.comparison.supports[0].quote == context
    assert "quote" not in source_specific["comparison"]["supports"][0]
    source_specific["comparison"]["supports"][0]["quote"] = LOCAL
    with pytest.raises(ValueError):
        parse(source_specific, pair)


@pytest.mark.parametrize(
    "mutation",
    [
        "id",
        "occurrence",
        "quote",
        "swap",
        "premises",
        "shared_certificate",
        "missing_ru",
        "observation_long",
        "blank_observation",
        "numeric_ru_swap",
    ],
)
def test_each_observation_remains_bound_to_its_source_and_language(
    pair: tuple[EvidenceBinding, ...],
    source_specific: dict[str, Any],
    mutation: str,
) -> None:
    comparison = source_specific["comparison"]
    first, second = comparison["supports"]
    if mutation == "id":
        first["evidence_id"] = "9" * 32
    elif mutation == "occurrence":
        first["occurrence_sha256"] = "9" * 64
    elif mutation == "quote":
        first["quote"] = PIPELINE
    elif mutation == "swap":
        comparison["supports"].reverse()
    elif mutation == "premises":
        comparison["premise_ids"].reverse()
    elif mutation == "shared_certificate":
        comparison["relationship_type"] = "shared_mechanism"
    elif mutation == "missing_ru":
        first["presentation_observation"] = None
    elif mutation == "observation_long":
        first["canonical_observation"] = "x" * 181
    elif mutation == "blank_observation":
        first["canonical_observation"] = " "
    else:
        first["canonical_observation"] += " 12"
        first["presentation_observation"] += " 24"
        second["canonical_observation"] += " 24"
        second["presentation_observation"] += " 12"
    with pytest.raises(ValueError):
        parse(source_specific, pair)


def test_renderer_revalidates_source_identity_and_full_evidence(
    pair: tuple[EvidenceBinding, ...],
    source_specific: dict[str, Any],
) -> None:
    result = parse(source_specific, pair)
    with pytest.raises(ValueError):
        synthesis_text(result, tuple(reversed(pair)))
    altered = (replace(pair[0], evidence=replace(pair[0].evidence, excerpt="Different source body.")), pair[1])
    with pytest.raises(ValueError, match="complete excerpt"):
        synthesis_text(result, altered)


def test_no_generic_qualification_is_required_but_material_one_cannot_disappear_in_translation(
    pair: tuple[EvidenceBinding, ...],
    source_specific: dict[str, Any],
) -> None:
    source_specific["comparison"]["presentation"]["qualification"] = None
    with pytest.raises(ValueError, match="paired presentation"):
        parse(source_specific, pair)
    source_specific["comparison"]["canonical"]["qualification"] = None
    assert parse(source_specific, pair).comparison.canonical.qualification is None
    assert "qualification" not in synthesis_text(parse(source_specific, pair), pair)


@pytest.mark.parametrize("attack", ["unrelated_quote", "negation", "actor", "condition", "compound", "ru_widening"])
def test_semantic_attacks_cannot_modify_the_retained_fact_context(
    pair: tuple[EvidenceBinding, ...],
    source_specific: dict[str, Any],
    attack: str,
) -> None:
    bad = {
        "unrelated_quote": PIPELINE,
        "negation": LOCAL.replace("no raw", "all raw"),
        "actor": LOCAL.replace("Local Guard", "Billing Service"),
        "condition": LOCAL + " This remains true after hardware failure.",
        "compound": LOCAL + " It also separates collection from inference.",
        "ru_widening": "Обе системы сохраняют данные на устройстве.",
    }[attack]
    source_specific["comparison"]["supports"][0]["quote"] = bad
    with pytest.raises(ValueError):
        parse(source_specific, pair)


@pytest.mark.parametrize("attack", ["unrelated_quote", "negation", "actor", "condition", "compound", "ru_widening"])
def test_same_semantic_attacks_in_generated_prose_still_require_editorial_review(
    pair: tuple[EvidenceBinding, ...],
    source_specific: dict[str, Any],
    attack: str,
) -> None:
    """Counterexamples, not editorial successes. Keep publication withheld for finite review."""
    comparison = source_specific["comparison"]
    first = comparison["supports"][0]
    if attack == "unrelated_quote":
        first["canonical_observation"] = "Separates collection from inference, just as the other system does."
    elif attack == "negation":
        first["canonical_observation"] = "Sends raw private input to a remote server."
    elif attack == "actor":
        first["canonical_observation"] = "The unrelated billing service processes private messages on the device."
    elif attack == "condition":
        first["canonical_observation"] += " This holds after hardware failure."
    elif attack == "compound":
        first["canonical_observation"] += " It also separates collection from inference."
    else:
        first["presentation_observation"] = (
            "Обе системы обрабатывают личные сообщения на устройстве без передачи данных."
        )
    result = parse(source_specific, pair)
    assert result.comparison.supports[0].quote == LOCAL
    assert synthesis_text(result, pair)


def test_unscoped_factual_union_in_interpretation_remains_an_explicit_editorial_failure(
    pair: tuple[EvidenceBinding, ...],
    source_specific: dict[str, Any],
) -> None:
    source_specific["comparison"]["canonical"]["interpretation"] = (
        "Both systems keep all processing local and separate collection from model inference."
    )
    # This contradicts the v2 prompt/acceptance rubric; syntax is not an entailment verifier.
    assert parse(source_specific, pair).comparison is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["valid", "failed", "reserved"])
async def test_v1_success_failure_and_in_progress_attempts_remain_immutable_no_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    record = json.loads((FIXTURES / "v1_attempts.json").read_text())[status]
    bound = tuple(
        EvidenceBinding(
            EvidenceItem(**item["evidence"]),
            item["occurrence_sha256"],
            item["occurrence"]["source"],
            item["occurrence"]["source_url"],
        )
        for item in EVIDENCE
    )
    current = reservation(bound)
    assert record["prompt_version"].endswith("v1") and current["prompt_version"].endswith("v2")
    assert record["accepted_sha256"] == current["accepted_sha256"]
    path = storage.attempt_path(record["accepted_sha256"], tmp_path)
    path.parent.mkdir()
    before = canonical_json_bytes(record)
    path.write_bytes(before)
    with pytest.raises(ValueError, match="Unsupported synthesis"):
        storage.load_attempt(record["accepted_sha256"], tmp_path)
    config = fixture_config()
    config.review.review_led_only = True
    config.synthesis = SynthesisConfig(True, False)
    config.telegram = TelegramConfig(delivery_mode="compact")
    config.translation = TranslationConfig(True, "ru", "gemini", "gemini-3.8-flash")
    monkeypatch.setenv("PREPARATION_DEADLINE", str(int(time.time()) + 440))
    monkeypatch.setattr(synthesis, "_request", lambda *args: (current, bound))
    complete, dispatch = AsyncMock(), AsyncMock()
    monkeypatch.setattr(synthesis, "complete", complete)
    monkeypatch.setattr(synthesis, "_dispatch", dispatch)
    for _ in range(2):
        result = await synthesis.prepare_synthesis(
            SimpleNamespace(sha256=record["accepted_sha256"]),
            config,
            publication_date=date(2026, 10, 13),
            execution=ModelExecution(),
            allow_new=True,
            cache_dir=tmp_path,
        )
        assert result.result is None and result.reason == "cache_unavailable"
        assert path.read_bytes() == before
    complete.assert_not_awaited()
    dispatch.assert_not_awaited()
    with pytest.raises(FileExistsError):
        storage.reserve_attempt(current, tmp_path)
    assert path.read_bytes() == before


@pytest.mark.asyncio
@respx.mock
async def test_actual_frozen_v1_fixture_sends_exact_old_bytes_without_v2_parser_or_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    for name in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    frozen = (FIXTURES / "v1_prepared_edition.json").read_bytes()
    manifest = json.loads(frozen)
    assert manifest["presentation_metadata"]["synthesis"]["result"]["schema_version"] == 1
    path = tmp_path / edition.READY_FILE
    path.write_bytes(frozen)
    ready_sha = hashlib.sha256(frozen).hexdigest()
    monkeypatch.setattr("digest.presentation.telegram.synthesis_text", Mock(side_effect=AssertionError("No rerender")))
    monkeypatch.setattr(
        "digest.domain.editorial.synthesis.validate_response", Mock(side_effect=AssertionError("No reparse"))
    )
    _, claim = edition.claim_edition(ready_sha, cache_dir=tmp_path, now=NOW)
    route = respx.post(API).mock(return_value=success())
    sent = await edition.send_prepared_edition(ready_sha, claim, config=sender_config(), cache_dir=tmp_path, now=NOW)
    assert sent.complete
    assert [json.loads(call.request.content) for call in route.calls] == manifest["payloads"]
    assert path.read_bytes() == frozen
    assert not (tmp_path / "synthesis").exists()


def test_code_attached_blank_evidence_cannot_support_a_generated_observation(
    pair: tuple[EvidenceBinding, ...], source_specific: dict[str, Any],
) -> None:
    blank = (replace(pair[0], evidence=replace(pair[0].evidence, excerpt=" ")), pair[1])
    with pytest.raises(ValueError, match="complete excerpt"):
        parse(source_specific, blank)
