# 0018. Share review reuse and separate general source attribution

Status: implementation record for the scoped
[#146](https://github.com/Lenivvenil/digest/issues/146) structural migration;
merge, runtime rollout and editorial acceptance are separate. The preceding #145
change merged in [PR #151](https://github.com/Lenivvenil/digest/pull/151) and was
deployed through runtime [PR #74](https://github.com/Lenivvenil/digest-prod/pull/74).
This #146 scope merged through [PR #152](https://github.com/Lenivvenil/digest/pull/152)
at `7d5f9cca618eb2257be01d54ffacd577498827a2`; runtime
[PR #75](https://github.com/Lenivvenil/digest-prod/pull/75) deployed it at
`e9daea980b831b69e45ee7c9af6a72ec653f9881` on 2026-10-07.

Refs [the staged architecture](../ARCHITECTURE.md#stage-4-review-reuse-and-source-attribution),
[ADR0005](0005-optional-presentation-translation.md),
[ADR0008](0008-candidate-selection-progress.md),
[ADR0014](0014-optional-humane-closing-item.md) and
[ADR0017](0017-confirmed-delivery-application.md).

## Context

Execution and resume planning duplicated exact-request review eligibility. Candidate
and closing occurrences repeated the same retained fields, while ordinary source
credits and immutable main-packet lookup lived in the optional closing module.
Moving ownership must preserve accepted recovery, source binding and Python/wire
compatibility without introducing a new editorial or rights policy.

## Decision

- `domain/editorial/reviews.py` owns pure `ReviewReuseIdentity` and
  `reusable_model_review`. Both callers use the same configured slot/provider/model,
  bundle and prompt identity and eligible `ok`/`partial`/`abstained` statuses. The
  identity must also match the supplied bundle. Mismatches return no result before
  selection validation; malformed matching selections raise. A valid copy retains
  recorded provenance and marks reuse without mutating the saved review.
- The same module owns strict `validate_request_evidence_bundle`, with explicit
  request limits. Request/checkpoint boundaries validate types, IDs, URL shape,
  budgets and bundle hash. This is distinct from canonical stored integrity, live
  response parsing and cached-selection validation. The reuse helper does not
  independently validate the whole evidence envelope or certify source fidelity.
  Accepted-preparation and frozen-edition recovery retain their existing contracts.
- `domain/catalog/occurrences.py` owns the frozen seven-field `SourceOccurrence`
  and its content hash. Thin, named frozen `CandidateArticle` and `ClosingOccurrence`
  subclasses preserve old imports, constructor/field order, type-specific equality,
  repr, conversion and strict JSON restoration. They do not collapse into aliases.
- `presentation/source_attribution.py` owns reviewed literal notices keyed by exact
  feed URL and the pure presentation-copy transform. `application/source_attribution.py`
  resolves main occurrences from the accepted report's immutable candidate packet.
  Keeping this resolver separate from `application.presentation` avoids coupling
  attribution to that module's review/translation dependency chain. `closing.py`
  retains optional decision/provenance handling, sidecars and compatibility wrappers.

Reviewed notices are not inferred licensing facts, source activation or clearance
for item-specific rights. Neither source names nor article hosts establish a feed
binding. No author, original-publication date or rights metadata is invented.

## Preserved behavior and compatibility

Required main attribution validates packet proof before model/presentation calls;
missing or inconsistent proof holds accepted work. Legacy recovery without a
supported enabled feed or closing-contract snapshot permits absent proof and warns
on failed optional inspection. Optional closing attribution failure omits closing.
Translation → literal credit → rendering/split checks → archive/freeze remains the
order. Required credited main splits hold preparation; unsafe optional insertion
omits closing. The archive and frozen payload use identical final cards, while
canonical evidence and translation request/cache inputs stay unchanged.

Hash encodings are intentionally distinct: sorted prompt JSON retains ASCII escapes
and default spaces; sorted evidence JSON retains non-ASCII text and default spaces;
occurrence/retained-object hashes use compact sorted UTF-8 JSON with non-finite
numbers rejected. Article identity, object envelopes, report/sidecar hashes and
preparation v1/v2 bytes remain compatible. No schema migration, new requests,
fallback/deadline changes, sender changes or state reset is introduced.

`source_admission.py` and `article_source.py` are unchanged. Full-text processing
remains optional; this extraction imposes no full-text prerequisite or new source
activation gate. Regression coverage targets identity mismatch versus malformed
matching review, retained partial/abstained provenance, pre-extraction wire/Python
contracts, pre-call main holds, legacy recovery and optional credit/split omission.
Check results belong to the implementing change, separately from semantic acceptance.

## Remaining ownership and rollback

Review orchestration, prompts and provider execution retain their existing owners.
Closing sidecar persistence, candidate scheduling, provider/configuration separation
and broader presentation/transport ownership remain staged work. Named subclasses
and compatibility exports remain explicit debt for #147/#148. Rollback uses a
compatible code revert or engine pin while retaining all evidence and runtime state.
