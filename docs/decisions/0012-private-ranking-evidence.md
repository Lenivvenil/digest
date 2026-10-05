# 0012. Preserve bounded private ranking evidence

Status: proposed local implementation under [#77](https://github.com/Lenivvenil/digest/issues/77);
not owner-accepted, published or deployed

## Context and proposed decision

Accepted signals, aggregate omissions and request/response hashes do not identify
omitted candidates or expose rejected relation judgments. Extend the existing private
Irritator JSON archive with optional `ranking_audit` version 1. Keep the outer result
schema at version 1; older archives and constructors have no recorded audit. No new
store, migration, model/search request or public output is introduced.

The trace starts after offline deduplication, blocklist filtering and exact cited-source
exclusion. It is not a record of every raw search hit. In existing candidate order, keep:

- The validated `Signal`, exact for admitted candidates. Its source name and publication
  field already reflect adapter validation; this is not an original HTTP response
- SHA-256 and character length of the complete original Signal serialized by
  `json.dumps(asdict(signal), ensure_ascii=True, sort_keys=True)`, default separators,
  encoded as UTF-8 for the hash. ASCII escaping keeps hashing defined even for escaped
  lone-surrogate JSON strings, without rejecting a source before existing filters.
  `signal_json_chars` counts this canonical JSON, not the model packet or raw text
- Original title/snippet lengths and explicit per-field truncation flags
- Character length of the would-be `_ranking_signal_payload(signal)` JSON with
  `ensure_ascii=False`. For omitted candidates this is not evidence sent to a model.
  This length is null for `candidate_limit`: the existing selector never evaluates
  their quote payload, and diagnostic code must not introduce a new validation failure
- Zero-based indices in the enclosing result's `queries` for successful retrievals of
  that exact complete Signal snapshot. Same-URL but different-text hits do not receive
  the retained snapshot's lineage. The unchanged validator still chooses the first hit
- Admission: `admitted`, `evidence_budget`, or `candidate_limit`. Budget omission means
  the whole record did not fit beside records already admitted, not necessarily that
  it exceeded the ceiling alone. Later smaller records remain eligible; after twelve
  admitted candidates, later records receive `candidate_limit`

Retain the effective minimum score, maximum returned rankings and existing admission
limits. The full original request/response hashes remain in stage diagnostics.

Only after the entire ranking response validates, attach each returned item's score,
relation, validated reasoning, URL-bound quote ID and reconstructed literal quote.
Its disposition is `accepted`, `non_counter`, or `below_min_score`. Admitted candidates
missing from a valid response are `not_returned`; their relevance was not established.
An explained empty response marks all admitted candidates `not_returned`.

Before a valid response, admitted candidates stay `pending`, decisions remain null and
`response_validated` is false. Malformed later entries still invalidate the whole
response; no valid prefix or raw rejected response text is retained. Caught provider
errors, timeout and full-source request-admission holds retain admission evidence and
existing stage error diagnostics. This is not crash-proof in-flight persistence or a
change to external cancellation behavior.

## Explicit proposed retention policy

Admitted evidence remains exact under the unchanged 8,000-JSON-character model packet
bound. For omitted candidates only, reuse the existing `MAX_RESPONSE_CHARS` value
(16,000) as a **new proposed archive allocation**, not a historically accepted policy.
Divide it equally across omitted candidates, retain the title prefix first and use
any remaining allocation for a snippet prefix. Preserve original lengths and hashes;
mark all shortening explicitly. Very long titles can exhaust a candidate's allocation,
leaving no snippet. A missing/truncated excerpt is unknown evidence, never a relevance
judgment. Prefixes can hide late qualifications and cannot establish semantic rejection.
No publisher body, new fetch, or full provider response is added.

The limit covers omitted title/snippet characters, not the entire serialized archive.
Existing retrieval bounds permit at most 3 queries × 3 sources × 10 records = 90
candidates; currently Lobsters abstains, leaving at most 60 from working adapters.
Each decoded source response is limited to 512,000 bytes. Thus current successful
transport bodies total at most 3.072 MB (4.608 MB for nine active source/query calls),
which is not a strict parsed-Signal or serialized-archive bound. URLs remain bounded to
2,048 characters and publication fields to 80; UTF-8 and JSON escaping add bytes.
Returned decision text is bounded by the unchanged 16,000-character response envelope.

Offline measurement of the indented **whole audit**, using the existing writer's
ASCII-escaped JSON (`ensure_ascii=True`) including identifiers, metadata and decisions,
gives 8,075 bytes for twelve short synthetic candidates. A
conservative ninety-candidate fixture with maximum-length escaped URLs/publication
fields, diagnostic text and 15,000 supplementary-plane reasoning characters gives
1,449,731 bytes. These characters occupy twelve ASCII-escaped bytes each in the archive.
These are measured fixture sizes, not a new runtime rejection limit or a promise about
all enclosing archives. Tests exercise escaping and include the decision text in size
checks. Future changes to source/query/response bounds require revisiting this growth.

The companion private `.irritator.md` keeps its existing narrative, accepted signals and
stage diagnostics, omitting only the new bulky audit field and adding a short summary
and reference to `.irritator.json`. Canonical JSON is persisted before optional
translation or Telegram. Audit-only titles, snippets, reasons and quotes are neither
translated nor sent. Existing accepted-result rendering and at-most-one attempt
semantics remain unchanged. URLs can contain private query values: keep runtime JSON
private and use synthetic public tests; public reports should summarize outcomes.

## Verification and scope

Focused offline tests cover exact admission boundaries, greedy skips/caps, query
lineage, successful and absent judgments, atomic malformed-response rejection,
provider errors, timeout, request-admission hold, size bounds and consumer isolation.
Synthetic differential checks against the draft baseline preserve all pre-existing
result fields and exact model requests for context-only, mixed, malformed and
budget-omission cases. No full suite or live acceptance is implied by these checks.

This additive trace applies to both bounded RSS and full-source investigation. It does
not alter prompts, provider selection, thresholds, sorting, the 8k admission ceiling,
request counts, retry behavior, publication policy, or the legacy synchronous ranker.
Recording a relation and literal quote enables review; it does not certify correctness.
