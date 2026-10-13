"""Finite offline contrasts for a reservation inside the established packet budget."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from digest._serialization import canonical_json_bytes
from digest.adapters.storage.candidate_objects import packet_key, read_packet
from digest.adapters.storage.candidate_progress import load_candidate_progress
from digest.application.candidate_review import begin_packet, merge_candidates, plan_packet, reconcile_packet
from digest.application.review_request import build_evidence_bundle
from digest.config import ClosingConfig, ClosingSourceBinding, Config, SourceConfig
from digest.domain.catalog.articles import article_hash
from digest.domain.editorial.attempts import restore_review
from digest.domain.editorial.candidate_policy import packet_articles
from digest.domain.editorial.candidates import Candidate, CandidateArticle, CandidatePacket, CandidateProgress
from digest.domain.editorial.dispositions import capture_review_dispositions
from digest.domain.editorial.evidence import MAX_EVIDENCE_JSON_CHARS, prepare_evidence_item
from tests.test_candidate_review import NOW, population, report_for

Binding = tuple[str, str, str]


def _add(
    progress: CandidateProgress, source: SourceConfig, title: str, minute: int, *,
    published: datetime | None = None, padding: int = 0,
) -> Candidate:
    saved = CandidateArticle(
        title, f"https://items.example/{title.replace(' ', '-')}" + "x" * padding,
        "Synthetic banking mechanism 😀 evidence", source.name, source.category,
        published.isoformat() if published else None, source.url,
    )
    identity = article_hash(saved.title, saved.link)
    candidate = Candidate(identity, saved, (NOW + timedelta(minutes=minute)).isoformat(), source.priority)
    progress.candidates[identity] = candidate
    return candidate


def _population(
    ordinary: int = 6, trials: int = 3, limit: int = 4,
) -> tuple[Config, CandidateProgress, frozenset[Binding]]:
    config, _ = population(0)
    # Roles here are fixture metadata. Production never classifies professional relevance.
    config.sources = [
        SourceConfig(f"Professional {i}", f"https://professional{i}.example/feed", "banking", True,
                     recency_hours=168) for i in range(ordinary)
    ]
    trial = SourceConfig("Technical trial", "https://trial.example/feed", "banking", True, trial=True,
                         recency_hours=168)
    config.sources.append(trial)
    config.review.max_evidence_articles = limit
    config.review.max_technical_retry_articles = 0
    config.adaptive.trial_slots = 2
    progress = CandidateProgress()
    for i, source in enumerate(config.sources[:-1]):
        _add(progress, source, f"Professional {i}", i)
    for i in range(trials):
        _add(progress, trial, f"Trial {i}", 30 + i, published=NOW + timedelta(minutes=30 + i))
    return config, progress, frozenset({(trial.name, trial.url, trial.category)})


def _packet(progress: CandidateProgress, config: Config, bindings: frozenset[Binding] = frozenset(),
            minute: int = 60) -> CandidatePacket:
    packet = plan_packet(progress, config, NOW + timedelta(minutes=minute), trial_sources=bindings)
    assert packet is not None
    return packet


def _ids(packet: CandidatePacket) -> list[str]:
    return [article_hash(item.title, item.link) for item in packet.articles]


def _titles(packet: CandidatePacket) -> list[str]:
    return [item.title for item in packet.articles]


def _resolve(progress: CandidateProgress, packet: CandidatePacket, config: Config, path: Path,
             *, deferred: bool = False) -> None:
    report = report_for(packet, config, "abstained")
    raw = json.dumps({"dispositions": [
        {"evidence_id": item.evidence_id, "status": "deferred" if deferred else "not_selected",
         "reason": "Response capacity" if deferred else "No relevant fixture development"}
        for item in packet.evidence.items
    ]})
    report.reviews[0].response_sha256 = hashlib.sha256(raw.encode()).hexdigest()
    attempt = capture_review_dispositions(packet.evidence, report.reviews[0], raw)
    reconcile_packet(progress, packet, restore_review(report, (attempt,)), config, path)


@pytest.mark.parametrize("case", ["no_bindings", "ineligible", "resolved", "zero", "satisfied"])
def test_inactive_or_satisfied_reservation_preserves_exact_packet_and_status(case: str) -> None:
    config, progress, bindings = _population()
    if case == "no_bindings":
        bindings = frozenset()
    elif case == "ineligible":
        for candidate in progress.candidates.values():
            if candidate.article.source == "Technical trial":
                candidate.eligible = False
    elif case == "resolved":
        for candidate in progress.candidates.values():
            if candidate.article.source == "Technical trial":
                candidate.status = "selected"
    elif case == "zero":
        config.adaptive.trial_slots = 0
    else:
        config.review.max_evidence_articles = 8
    baseline_state = deepcopy(progress)
    baseline = _packet(baseline_state, config)
    packet = _packet(progress, config, bindings)
    assert asdict(packet) == asdict(baseline)
    assert asdict(progress) == asdict(baseline_state)


def test_no_trial_golden_packet_and_state_keep_baseline_with_current_prompt() -> None:
    config, progress, _ = _population()
    packet = _packet(progress, config)
    assert _titles(packet) == ["Professional 0", "Professional 1", "Professional 2", "Professional 3"]
    # Evidence and state retain the cd14cbbf10e9aa7bd8301a0a0816acad0f315fb0 frozen-input goldens.
    assert packet.evidence.bundle_id == "6ee89dcdbbcf133314a1c38dc9918cb37493b55ab3de347db4e58086e652cc72"
    # Independently reconstructed after the exact 287-byte shared reason-qualifier insertion.
    assert packet.prompt_hash == "503f7d953aad2cd52d1ea6c9e92626366e0f6cf6e35aed6f062a83a35c975500"
    assert packet.prompt_hash != "f8dcdeca50f269f1f432ba918ed7df6371c46e777e8cb1b0532e8d38cd605f9c"
    state_hash = hashlib.sha256(canonical_json_bytes(asdict(progress))).hexdigest()
    assert state_hash == "2cb805954ca4e64d6b168af9099dd9480fdf6032afb69143f7bbee7a4a43f27c"


@pytest.mark.parametrize("limit,slots,ordinary,trials,expected", [
    (1, 2, 6, 3, ["Professional 0"]),
    (2, 2, 6, 3, ["Professional 0", "Trial 0"]),
    (4, 2, 6, 3, ["Professional 0", "Professional 1", "Trial 0", "Trial 1"]),
    (20, 4, 24, 5, [*[f"Professional {i}" for i in range(16)], *[f"Trial {i}" for i in range(4)]]),
    (4, 10**12, 6, 5, ["Professional 0", "Trial 0", "Trial 1", "Trial 2"]),
    (4, 10**12, 6, 1, ["Professional 0", "Professional 1", "Professional 2", "Trial 0"]),
    (4, 2, 1, 1, ["Professional 0", "Trial 0"]),
])
def test_exact_incremental_displacement_and_fixed_limits(
    limit: int, slots: int, ordinary: int, trials: int, expected: list[str],
) -> None:
    config, progress, bindings = _population(ordinary, trials, limit)
    config.adaptive.trial_slots = slots
    baseline = _packet(progress, config)
    before = asdict(progress)
    packet = _packet(progress, config, bindings)
    assert _titles(packet) == expected
    assert packet.articles[0] == baseline.articles[0]
    assert len(set(_ids(baseline)) - set(_ids(packet))) <= min(slots, limit)
    assert len(packet.articles) <= limit and asdict(progress) == before
    assert build_evidence_bundle(packet_articles(packet), config.review) == packet.evidence
    assert sum(len(json.dumps(asdict(item), ensure_ascii=False)) for item in packet.evidence.items) <= 16000
    assert packet.max_selections == baseline.max_selections == config.review.max_selections
    if _ids(packet) != _ids(baseline):
        assert packet.prompt_hash != baseline.prompt_hash and packet.evidence.bundle_id != baseline.evidence.bundle_id


def test_zero_is_not_a_trial_ceiling_and_no_professional_classifier_exists() -> None:
    config, progress, bindings = _population(0, 5, 4)
    config.adaptive.trial_slots = 0
    packet = _packet(progress, config, bindings)
    assert len(packet.articles) == 4
    assert all(item.source == "Technical trial" for item in packet.articles)
    config, progress, bindings = _population()
    control = progress.candidates[_ids(_packet(progress, config))[0]]
    control.article = replace(control.article, category="culture")  # A nonprofessional nontrial remains protected.
    packet = _packet(progress, config, bindings)
    assert packet.articles[0].category == "culture"
    assert packet.articles[-1].category == "banking"  # A professional trial can receive the reservation.


@pytest.mark.parametrize("overlap", ["oldest_closer", "retry", "backfill"])
def test_all_admitted_trial_roles_count_once_without_extra_promotion(tmp_path: Path, overlap: str) -> None:
    config, progress, bindings = _population()
    trial = next(item for item in progress.candidates.values() if item.article.title == "Trial 0")
    if overlap == "oldest_closer":
        trial.first_observed_at = (NOW - timedelta(minutes=1)).isoformat()
        config.closing = ClosingConfig(True, (ClosingSourceBinding(*next(iter(bindings))),))
    elif overlap == "retry":
        first = _packet(CandidateProgress(candidates={trial.identity: trial}), config)
        begin_packet(progress, first, tmp_path)
        config.review.max_technical_retry_articles = 1
    else:
        config.review.max_evidence_articles = 7
    config.adaptive.trial_slots = 1
    baseline = _packet(progress, config)
    packet = _packet(progress, config, bindings)
    assert any(item.source == "Technical trial" for item in baseline.articles)
    assert asdict(packet) == asdict(baseline)


def test_oldest_retry_closer_and_admitted_trials_all_remain_protected(tmp_path: Path) -> None:
    config, progress, bindings = _population(7, 3, 5)
    ordinary = list(progress.candidates.values())[:7]
    retry = _packet(CandidateProgress(candidates={ordinary[1].identity: ordinary[1]}), config)
    begin_packet(progress, retry, tmp_path)
    config.review.max_technical_retry_articles = 1
    closer = config.sources[6]
    config.closing = ClosingConfig(True, (ClosingSourceBinding(closer.name, closer.url, closer.category),))
    config.adaptive.trial_slots = 99
    baseline = _packet(progress, config)
    packet = _packet(progress, config, bindings)
    assert _titles(baseline) == [
        "Professional 0", "Professional 1", "Professional 2", "Professional 3", "Professional 6",
    ]
    assert _titles(packet) == ["Professional 0", "Professional 1", "Professional 6", "Trial 0", "Trial 1"]
    assert packet.articles[:2] == baseline.articles[:2]


def test_age_only_source_rounds_use_priority_then_name_without_publication_bias() -> None:
    config, progress, bindings = _population(8, 0, 5)
    sources = [SourceConfig(name, f"https://{name}.example/feed", "banking", True, priority=priority, trial=True)
               for name, priority in [("Beta", 3), ("Alpha", 3), ("High", 5)]]
    for source in config.sources[:-1]:
        _add(progress, source, f"Fresh {source.name}", 20, published=NOW + timedelta(minutes=20))
    config.sources.extend(sources)
    bindings |= frozenset((s.name, s.url, s.category) for s in sources)
    for source in sources:
        _add(progress, source, f"{source.name} old", 10)  # Older than the competing fresh professional heads.
        _add(progress, source, f"{source.name} fresh", 31, published=NOW + timedelta(minutes=31))
        _add(progress, source, f"{source.name} future", 32, published=NOW + timedelta(days=10))
    config.adaptive.trial_slots = 4
    assert all(item.source.startswith("Professional") for item in _packet(progress, config).articles)
    assert _titles(_packet(progress, config, bindings)) == [
        "Professional 0", "High old", "Alpha old", "Beta old", "High fresh",
    ]


@pytest.mark.parametrize("guard", ["url", "category", "alternate", "rebound"])
def test_trial_binding_belongs_to_the_chosen_exact_current_occurrence(guard: str) -> None:
    config, progress, bindings = _population()
    candidate = next(item for item in progress.candidates.values() if item.article.title == "Trial 0")
    original = candidate.article
    # Keep only this trial so an unrelated exact trial cannot obscure the guard.
    progress.candidates = {key: item for key, item in progress.candidates.items()
                           if item.article.source != "Technical trial" or item is candidate}
    if guard == "url":
        candidate.article = replace(original, source_url="https://old.example/feed")
    elif guard == "category":
        candidate.article = replace(original, category="culture")
    else:
        candidate.article = replace(original, source=config.sources[0].name, source_url=config.sources[0].url)
        candidate.occurrences = (original,)
        if guard == "rebound":
            config.sources[0].enabled = False
            merge_candidates(progress, {}, config, {}, now=NOW + timedelta(hours=1))
            assert candidate.article == original
    baseline = _packet(progress, config)
    packet = _packet(progress, config, bindings)
    assert (candidate.article in packet.articles) is (guard == "rebound")
    if guard != "rebound":
        assert asdict(packet) == asdict(baseline)


@pytest.mark.parametrize("fit_case", ["replacement", "last_too_small", "no_donor", "invalid_then_fit"])
def test_whole_unicode_fit_uses_at_most_the_final_unprotected_donor(fit_case: str) -> None:
    config, progress, bindings = _population(3, 0, 5)
    ordinary = list(progress.candidates.values())
    trial = config.sources[-1]
    if fit_case == "replacement":
        # Baseline fits three, but the fourth needs the final donor's characters.
        padding = [6000, 1000, 6500]
        offer_size = 2000
    else:
        padding = [1000, 11000, 1000]
        offer_size = 4000
    for candidate, amount in zip(ordinary, padding, strict=True):
        del progress.candidates[candidate.identity]
        _add(progress, next(s for s in config.sources if s.name == candidate.article.source),
             candidate.article.title,
             int((datetime.fromisoformat(candidate.first_observed_at) - NOW).total_seconds() / 60),
             padding=amount)
    if fit_case == "invalid_then_fit":
        _add(progress, trial, "Oversized", 28, padding=20000)
        invalid = _add(progress, trial, "Invalid", 29)
        invalid.article = replace(invalid.article, link="invalid://evidence")
        invalid.identity = article_hash(invalid.article.title, invalid.article.link)
        progress.candidates = {item.identity: item for item in progress.candidates.values()}
        offer_size = 2000
    offer = _add(progress, trial, "Offer", 30, padding=offer_size)
    config.adaptive.trial_slots = 1
    extra = None
    if fit_case == "replacement":
        extra = _add(progress, trial, "Spare after replacement", 31, padding=3000)
        config.adaptive.trial_slots = 2
    if fit_case == "no_donor":
        config.review.max_technical_retry_articles = 3
        for candidate in progress.candidates.values():
            candidate.status = "technical_pending"
    baseline = _packet(progress, config)
    before = deepcopy(progress)
    packet = _packet(progress, config, bindings)
    expected = fit_case in {"replacement", "invalid_then_fit"}
    assert (offer.article in packet.articles) is expected
    if expected:
        assert _ids(packet) == [*_ids(baseline)[:-1], offer.identity, *([extra.identity] if extra else [])]
    else:
        assert asdict(packet) == asdict(baseline)
    assert asdict(progress) == asdict(before)
    if fit_case == "last_too_small":
        sizes = [prepare_evidence_item(saved.article(), max_excerpt_chars=config.review.max_excerpt_chars)[1]
                 for saved in baseline.articles]
        offer_chars = prepare_evidence_item(
            offer.article.article(), max_excerpt_chars=config.review.max_excerpt_chars,
        )[1]
        assert sum(sizes) - sizes[-1] + offer_chars > MAX_EVIDENCE_JSON_CHARS
        assert sum(sizes) - sizes[1] + offer_chars <= MAX_EVIDENCE_JSON_CHARS
    for identity in set(_ids(baseline)) - set(_ids(packet)):
        assert asdict(progress.candidates[identity]) == asdict(before.candidates[identity])
    for candidate in progress.candidates.values():
        if candidate.article.title in {"Oversized", "Invalid"}:
            assert candidate.status == "technical_pending"
    assert sum(prepare_evidence_item(saved.article(), max_excerpt_chars=config.review.max_excerpt_chars)[1]
               for saved in packet.articles) <= MAX_EVIDENCE_JSON_CHARS
    assert build_evidence_bundle(packet_articles(packet), config.review) == packet.evidence


def test_finite_windows_advance_aging_trials_and_preserve_displaced_exact_proof(tmp_path: Path) -> None:
    config, progress, bindings = _population(12, 3, 4)
    config.adaptive.trial_slots = 1
    # Save real, exact deferred proof for the ordinary and trial technical work.
    config.review.max_evidence_articles = 15
    first = _packet(progress, config)
    begin_packet(progress, first, tmp_path)
    _resolve(progress, first, config, tmp_path, deferred=True)
    frozen_first = asdict(first)
    config.review.max_evidence_articles = 4
    config.review.max_technical_retry_articles = 2
    # New ordinary work keeps the protected oldest from being a trial.
    seen_trials: list[str] = []
    original_observations = {k: v.first_observed_at for k, v in progress.candidates.items()}
    for window in range(3):
        progress = load_candidate_progress(tmp_path)
        _add(progress, config.sources[0], f"Old ordinary {window}", 10 + window)
        baseline = _packet(progress, config, minute=70 + window)
        before = deepcopy(progress)
        packet = _packet(progress, config, bindings, minute=70 + window)
        trial_ids = [k for k in _ids(packet) if progress.candidates[k].article.source == "Technical trial"]
        assert len(trial_ids) == 1
        seen_trials.extend(trial_ids)
        displaced = set(_ids(baseline)) - set(_ids(packet))
        begin_packet(progress, packet, tmp_path)
        _resolve(progress, packet, config, tmp_path)
        restored = load_candidate_progress(tmp_path)
        for identity in displaced:
            assert asdict(restored.candidates[identity]) == asdict(before.candidates[identity])
        assert asdict(read_packet(packet_key(first), tmp_path)) == frozen_first
        for identity, candidate in restored.candidates.items():
            if identity in original_observations:
                assert candidate.first_observed_at == original_observations[identity]
    assert len(set(seen_trials)) == 3


@pytest.mark.parametrize("filler", ["fresh_trials", "trial_retries"])
def test_satisfied_target_can_leave_an_aging_trial_unserved(tmp_path: Path, filler: str) -> None:
    config, progress, bindings = _population(3, 0, 3)
    config.adaptive.trial_slots = 1
    source = config.sources[-1]
    aging = _add(progress, source, "Aging trial", 10)
    fresh = _add(progress, source, "Fresh trial", 11, published=NOW + timedelta(minutes=11))
    if filler == "trial_retries":
        retry = _packet(CandidateProgress(candidates={fresh.identity: fresh}), config)
        begin_packet(progress, retry, tmp_path)
        config.review.max_technical_retry_articles = 1
    else:
        # This source's fresh head is ahead of the ordinary second-round backlog.
        for item in progress.candidates.values():
            if item.article.source.startswith("Professional") and item.article.title != "Professional 0":
                item.first_observed_at = (NOW + timedelta(minutes=20)).isoformat()
    baseline = _packet(progress, config)
    packet = _packet(progress, config, bindings)
    assert fresh.article in baseline.articles and aging.article not in baseline.articles
    assert asdict(packet) == asdict(baseline)
    assert aging.status == "not_presented"


def test_trial_retry_reservation_preserves_exact_attempt_age_and_same_request_reuse(tmp_path: Path) -> None:
    from digest.domain.editorial.reviews import ReviewReuseIdentity, reusable_model_review

    config, progress, bindings = _population(4, 3, 3)
    trials = [item for item in progress.candidates.values() if item.article.source == "Technical trial"]
    for candidate, minute in zip(trials, [53, 51, 52], strict=True):
        first = _packet(CandidateProgress(candidates={candidate.identity: candidate}), config, minute=minute)
        begin_packet(progress, first, tmp_path)
    for candidate in progress.candidates.values():
        candidate.status = "technical_pending"
    config.review.max_technical_retry_articles = 1
    config.adaptive.trial_slots = 2
    baseline = _packet(progress, config)
    packet = _packet(progress, config, bindings)
    assert _titles(packet) == ["Professional 0", "Trial 1", "Trial 2"]
    saved = report_for(baseline, config).reviews[0]
    exact = ReviewReuseIdentity(saved.slot, saved.provider, saved.model, baseline.evidence.bundle_id,
                               baseline.prompt_hash)
    assert reusable_model_review(saved, baseline.evidence, exact) is not None
    changed = replace(exact, bundle_id=packet.evidence.bundle_id, prompt_hash=packet.prompt_hash)
    assert reusable_model_review(saved, packet.evidence, changed) is None


def test_unrepresentable_extra_trial_is_technical_without_displacing_an_item() -> None:
    config, progress, bindings = _population(6, 0)
    invalid = _add(progress, config.sources[-1], "Oversized extra", 30, padding=20000)
    baseline = _packet(progress, config)
    assert invalid.status == "not_presented"  # Baseline reached its count limit before examining this identity.
    packet = _packet(progress, config, bindings)
    assert asdict(packet) == asdict(baseline)
    assert invalid.status == "technical_pending" and invalid.disposition is None
    assert invalid.first_observed_at == (NOW + timedelta(minutes=30)).isoformat()
