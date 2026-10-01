# Blind evidence review (opt-in experiment)

This runbook describes the RSS-review code available on main. Full-source enrichment
in [draft PR #93](https://github.com/Lenivvenil/digest/pull/93) is not installed by these
instructions. Editorial quality remains open in [#55](https://github.com/Lenivvenil/digest/issues/55).
[#55](https://github.com/Lenivvenil/digest/issues/55) is the active editorial workstream.
[#94](https://github.com/Lenivvenil/digest/issues/94) remains queued at its translation
and product-acceptance gates; optional translation is still an unmerged draft.

`review.enabled` adds provider-neutral independent selection to the existing RSS,
Markdown and Telegram pipeline. It defaults to false for existing configurations.

## Contract

1. Freeze a version-1 `EvidenceBundle` before any review call. It contains stable
   article IDs, sanitized RSS title/excerpt, canonical URL, source, category and
   available publication time. The SHA-256 bundle ID changes with the evidence.
2. Apply the same deterministic round-robin category sampling once, with bounded
   article count, excerpt length and a 16,000-character evidence-item JSON budget.
   The omission count is recorded. This is excerpt evidence, not full articles.
3. Give primary and secondary slots exactly the same messages, language,
   temperature, selection limit and maximum output tokens. No category summary,
   prior opinion, prior selection or other model identity enters the prompt.
4. Pin each slot to its specified provider/model. There is no role/provider
   fallback for review slots. A failed model remains unavailable rather than
   silently being replaced by the other reviewer. Provider-reported model
   versions are retained when available; otherwise the report says unknown.
5. Validate every selection: known unique evidence ID, bounded reason text,
   controlled confidence label, and a nonempty exact quote from supplied title
   or excerpt. Once the complete response envelope passes validation, invalid
   entries are rejected individually. Accepted entries may be delivered with
   `partial` status; malformed envelopes, excessive counts or invalid limitations
   still reject the whole response. An empty original selection needs an explicit
   limitation, distinguishing abstention from malformed output.
6. Compare selected IDs using Jaccard overlap. If both results are valid and
   overlap is below the configured threshold, optionally call a third model on
   the same complete evidence bundle and same prompt, still without prior answers.
   At most one third-model task is requested. HTTP retries remain independently
   bounded by the shared LLM retry policy.

## Output and integration

Primary selection becomes canonical Telegram cards, labeled as model opinion.
Secondary/third opinions do not generate extra Telegram card floods. All reviews,
quotes, confidence, provider/model identities, token usage, prompt hashes,
completeness and escalation decisions appear in Markdown and a sibling
`YYYY-MM-DD.review.json` (or numbered retry filename).

Blind selection runs before category prose to protect its quota budget. If prose
fails but primary cards succeed, those cards can still be delivered. Even if all
slots abstain or fail, the diagnostic report is preserved when Markdown is enabled.
No category summary or prior output is recycled into another model's evidence.

## Example configuration

```yaml
review:
  enabled: true
  review_led_only: false
  primary: {provider: gemini, model: gemini-3.8-flash}
  secondary: {provider: groq, model: openai/gpt-oss-120b}
  tie_breaker: {provider: groq, model: qwen/qwen3.8-27b}
  max_evidence_articles: 20
  max_excerpt_chars: 500
  max_selections: 5
  max_output_tokens: 4096
  disagreement_threshold: 0.5
```

Qwen is a preview-model option, not an automatic replacement for a failed peer.
These names express the reviewed experiment configuration, not a guarantee of
account access or a free quota. Deployment must confirm free-tier account status;
this implementation does not enable billing or add paid-provider fallback.

## Offline verification

```sh
python -m scripts.review_fixture --output /tmp/digest-review-fixture
```

This command uses synthetic fixture responses and never calls providers. It emits
Markdown/JSON demonstrating different selections and bounded third-model
escalation. It proves the contract wiring only, not the quality of those models.

## Explicit limitations

Selection overlap is not factual agreement or a majority-vote truth detector.
Different opinions about the same selected item do not automatically trigger the
third model. Exact excerpt quotes validate provenance, not claim truth. There is
no live fact-checking or full-article retrieval in this module.

Identical maximum output tokens do not equal identical reasoning effort across
model families. Character caps are not exact tokenizer/RPM/TPM accounting. Quotas
can still make a review incomplete; that state is surfaced, not counted as an
opinion. Existing crash/retry delivery limitations still apply.

## Rejected-response diagnostics

Invalid successful model completions retain a specific validator reason, response
SHA-256 and at most 32,000 characters of untrusted rejected text in the JSON
sidecar, not rendered as Markdown. Control characters and common credential-like
patterns are redacted without inspecting environment secrets. Raw HTTP error
bodies and headers are never retained. This diagnoses future contract failures
without silently retrying a model or guessing what its first response contained.

### Resume a report-only comparison

`python -m digest.review_trial --config config.yaml --resume previous/review.json --output fresh-output`

Resume keeps the exact saved RSS evidence, verifies its content hash and budgets,
and does not collect sources or touch production dedup state. Only successful
`ok`/`partial`/`abstained` slots whose provider, model, evidence hash and current prompt hash
match are reused. Their selection contract is revalidated before any request.
Missing or failed slots are attempted once; changed prompts/models invalidate
reuse. Output must be fresh and cannot replace the original checkpoint. Reports
label reused versus newly attempted slots and preserve original generation times;
legacy version-one reports with no timestamp explicitly show `not recorded`.
Hashes detect accidental mismatch, not malicious editing: checkpoints are trusted
local artifacts, not authenticated provider receipts. Model aliases may change
behind a provider's API; the recorded resolved model is retained for inspection.

When the primary is unavailable or invalid, a successful secondary selection can
lead the digest. Cards identify the actual provider/model and explicitly mark an
incomplete independent comparison. A primary's valid abstention is respected;
it is not silently replaced. Reusing a result never creates another opinion.

Partial cached reviews retain their rejected-item diagnostics and remain incomplete.
Accepted entries are strictly revalidated, with no typography repair at resume
time. A partial slot is reused rather than charged again; partial comparisons do
not produce overlap/disagreement claims or trigger a third model.

The trial CLI is a manual, report-only recovery boundary. The separate
`digest.review_resume` production command provides prepare/execute phases for the
existing runtime schedule. It keeps production evidence budgets, limits completion
to two calls with no retries, and requires an immutable checkpoint attempt marker.
The runtime must commit and push that marker before execute; if persistence fails,
no inference is allowed. Newest incomplete reports under 24 hours old are eligible
once; old reports without timestamps are skipped. Resumed Markdown/JSON are
archival supplements only: no Telegram message or dedup mutation occurs.
A later scheduled run may use a different resolved version behind a provider model
alias; original and new timestamps/model versions are visible in the report.

### Review-led delivery without legacy enrichment

Set `review.review_led_only: true` together with `review.enabled: true` to
deliver the evidence-bound selected article cards and archive the blind-review
report immediately after review. This opt-in skips category summaries,
cross-category trends, and Irritator counter-signal analysis. The selected cards
remain model opinions grounded only in the supplied RSS excerpts. Normal
Telegram delivery checks and dedup rules still apply. The skipped analysis is
explicitly logged; the default `false` preserves the existing full pipeline.

This flag changes orchestration only, not the evidence bundle, review prompt,
prompt hash, or cached-review validity. It makes one primary request and at most
one secondary fallback, with no retries, before delivering and archiving. It does
not guarantee provider availability or a completed comparison.

## Primary-first runtime with preserved Irritator

The explicit `review.review_led_only: true` mode now delivers the primary
selection first. It makes one primary request, or one secondary fallback only
when the primary is unavailable/invalid. Partial validated selections are
deliverable without fallback; a valid abstention is respected. The
independent opinion is marked pending, not counted as complete. Original evidence
and prompts remain the same. The primary command emits `review_checkpoint` to
GitHub Actions only after confirmed required delivery and state saves succeed.

The runtime commits that primary receipt/cache and immutable review archive
before starting a separate follow-up job. A timeout in that job cannot cancel or
roll back the already committed primary result. The follow-up job has two bounded
parts:

1. Irritator: original RSS evidence → one evidence-cited narrative → up to three
   adversarial queries → real Hacker News/arXiv/Lobsters searches → validation and
   evidence-cited ranking. It does not manufacture category summaries or consume
   another model's selections as source facts. Up to three single-provider model
   attempts, no retries/fallback, a 180-second stage deadline, bounded excerpts
   and ranking candidates. This deliberately samples one narrative; it is not the
   old exhaustive per-category analysis. Errors/partial sources remain explicit,
   never silently relabeled as absence of counter-evidence.
2. Independent review: reuse validated successful slots and attempt only missing
   slots on the same original bundle, at most two model calls. This is an archive
   supplement; no extra Telegram comparison message.

The same configured Telegram receives one bounded Irritator supplement with
coverage/completeness labeling and external URLs when available. An attempt
marker is committed before any optional requests. Results are written before
sending; the marker records the Telegram outcome. An uncertain send is not
retried automatically. This is at-most-one workflow attempt, not a promise of
exactly-once network delivery. Failed/aborted marked stages require explicit
inspection; normal workflow reruns cannot resend them.

Quota spacing is conservative: optional Groq calls are spaced at least 65 seconds
and the runtime leaves a 65-second gap before each follow-up phase. Real free-tier
limits remain account-specific; a quota failure produces an incomplete archive,
not a paid fallback. Successful primary delivery does not imply successful
optional analysis; inspect the separate follow-up job reports.

## Partial selection recovery and narrow typography repair

The live-response validator keeps independently valid items when their siblings
fail. The JSON sidecar records `rejected_items` with the zero-based response index,
a known evidence ID only when available, and a safe validator reason. A response
with both accepted and rejected entries is `partial`; one with no accepted entries
remains `invalid`. The original bounded, credential-redacted response and its
original SHA-256 are retained for diagnosis. No citation is synthesized.

Quotes remain literal excerpts from the supplied title or RSS text. The only
allowed live-response repair aligns ASCII `-`, U+2010 HYPHEN and U+2011
NON-BREAKING HYPHEN, each a single character. The validator retrieves the actual
source substring at the same indices and stores that exact text, recording
`typography_normalized: true` on the accepted selection. The 200-character limit
is checked before any repair. Semantic minus U+2212, dashes, ellipses, case,
whitespace, paraphrases and Unicode compatibility transformations are not
normalized. Checkpoint reuse requires the saved quote to match source text exactly.

The captured public-RSS regression fixture in
`tests/fixtures/partial_review.json` yields four accepted entries (three narrow
hyphen repairs and one originally exact quote) and rejects one over-budget,
paraphrased quote. These tests make no model or network calls.

## Durable complete-source editorial path (ADR0004)

The new path is separate from the schema-1 RSS selection experiment above.
[ADR0004](decisions/0004-durable-editorial-evidence.md) records its decision,
source-coverage invariants, private evidence storage and release gates.

**Experimental, not enabled for production:** `python -m digest.editorial_pipeline work`
admits every collected article that passes the existing freshness/blocklist/dedup
filters, before the legacy slot budget. Those filters do not establish semantic
relevance. This all-admit mode has not demonstrated sustainable throughput. The
execution deadline and request allowance bound a pass; unfinished material remains
pending. `--output` is a diagnostic output directory, not a production digest
archive. Use a runner-temporary directory so reports do not become runtime clutter.
Complete extracted bodies and versioned progress belong in `.cache/editorial/`.
The report exposes admission/acquisition/completion, oldest pending work, provider
attempts and possible repeated-event diagnostics. Provider attempts retain
only allowlisted numeric quota observations and normalized server retry/reset
boundaries, with their provenance. These are separate from the worker's policy
cooldown; an unknown quota dimension stays unknown. Raw error prose, request headers
and credentials are not retained by this diagnostic contract. A completed analysis
is not proof of editorial usefulness; real-output review remains required.

The complete-source path uses deterministic numbered source spans, resolved by the
engine to unchanged body offsets. Models reference those IDs rather than transcribe
quotations. A fitting whole body goes directly to editorial synthesis; larger bodies
retain complete chunk coverage and reduce findings only when needed. Raw source
nodes are explicitly unclassified, and valid references alone do not prove semantic
entailment. Qualifiers, audience boundaries and quantifiers remain quality checks.
A changed prompt creates a new analysis generation; prior acquisition is reusable,
but earlier model opinions are not silently relabelled under the new contract.
Explicit provider output exhaustion remains failed/incomplete work, even if its
partial response happens to be parseable JSON. A truncated chunk is subdivided with
a generation-local manifest; successful sibling work is retained. Exhaustion of an
unsplittable segment is an explicit technical block. Optional ranking explanations
and topic labels cannot invalidate substantive card content. Additional inference
may be absent; actual source qualifications cannot be omitted to fill a template.

If the experimental delivery path is later approved for rollout, its required ordering is:

1. Run `work`; persist its state, including normal bounded partial progress.
2. Run `repair-archives` to repair receipts whose confirmed delivery outlived an
   archive failure. This command never sends Telegram messages.
3. Run `prepare`; persist and push the exact delivery reservation.
4. Only after that push succeeds, run `deliver --attempt <reserved-path>` once.
   Preserve receipts/state even when some cards fail. Unknown outcomes remain held
   and are never automatically resent. Existing Telegram transport limits defer
   ready cards; they do not limit source analysis or classify deferred cards as weak.
5. Run the existing separately reserved post-delivery Irritator using the emitted
   source-context checkpoint. Its current bounded RSS/search coverage is explicit;
   this is not a claim of independent full-article verification.
6. `independent` advances the other provider's own full-body analysis and archives
   it. It never consumes the first provider's opinions or repeats primary delivery.

Serialize these commands with the existing runtime concurrency group. A hard runner
loss before state reaches git can repeat provider work; an already reserved Telegram
send is held rather than guessed safe to repeat. Do not convert a model timeout,
inaccessible source or oversized transport payload into editorial rejection.

For a report-only verification, run `work` in an isolated temporary working directory
with absolute config/state/output paths and without Telegram credentials. The pass
may write isolated source-health observations but cannot consume production dedup or
send cards. Keep the full state/body artifact for independent source-based review.

The CLI currently enforces the explicitly approved free-route model lineup shared
with the trial guard. That is a release safety guard, not a domain claim that the
product can only ever use those models. A later approved lineup must update and test
that operational guard; no paid provider is an automatic fallback.


## Current #55 slice: enrich a saved selection without delivery

The isolated `digest.editorial_enrichment` entry reuses a saved RSS review checkpoint
and durable source acquisition. It does not recollect feeds or select every
article. Its output is an **internal, unverified draft**, not a published digest.

Prepare or inspect the selected work without network or model calls:

```sh
python -m digest.editorial_enrichment --config config.yaml \
  --checkpoint digests/example.review.json \
  --state .cache/editorial-enrichment --output /tmp/editorial-enrichment-report
```

A later explicitly requested acquisition/analysis pass uses the same state and adds
`--execute`; `--deadline-seconds` and `--max-calls` limit that pass. A subsequent pass
can omit `--checkpoint` to resume the saved selection. Completed source acquisition
and compatible intermediate work are reused; unfinished long articles remain pending.
No command in this entry sends Telegram messages, reserves delivery, runs Irritator,
or writes the production dedup/source-health state. Keep its state directory separate
from the experimental all-admit worker and preserve it when progress must survive a
runner restart. Using a new temporary directory each run does not provide durable
resume across runners.

The report records each checkpoint's selected IDs, unselected IDs within the RSS
packet, and the count omitted before model selection. Omitted items remain unreviewed;
absence from the shortlist is not a full-article editorial rejection. The bounded RSS
selector's coverage limitation remains open under #55. State may contain only work
traceable to those saved selections.

English is the entry's default when `radar.language` is absent. An explicit setting
such as `radar.language: ru` is honored and bound to the analysis generation. This is
a narrow #94 integration, not the completed configurable translation product flow.
Facts retain source references and material qualifications; optional interpretation,
reading advice and extra limitations need not be invented to fill fields. Valid source
IDs establish provenance, not factual truth. Independent real-output review remains
the publication gate; a successful offline replay or a manually edited example does
not pass it.

### Explicit publication routes and local request budgets

Execution requires the optional [offline tokenizer profiles](ENRICHMENT_TOKENIZERS.md)
and an explicit `enrichment` section. It never inherits writer/checker roles from the
RSS review slots. This example declares routes; it does not enable delivery or prove
free entitlement for an account:

```yaml
enrichment:
  writer:
    provider: groq
    model: qwen/qwen3.8-27b
  verifier:
    provider: groq
    model: openai/gpt-oss-120b
  tokenizer_cache: .cache/enrichment-tokenizers
  pacing: fixed
  requests_per_minute: 30
  writer_output_tokens: 2200
  verifier_output_tokens: 4096
```

The entry checks the complete source against the local route profile, completion
reservation and a 512-token safety allowance inside the supported 8K request budget.
It uses at most two verifier batches, each with the complete source; only the claim
list is partitioned. Unsupported routes, missing/mismatched assets and complete-source
requests that cannot fit remain technical pending. Existing long-source evidence and
intermediates survive, but this publication contract does **not** yet provide a verified
hierarchical publication path for those articles. It does not silently promote older
compressed summaries or discard the source as uninteresting.

A writer returns exact ordered publication statements. The factual checker sees that
draft and checks every statement. This is **not a blind independent opinion** and does
not update the RSS review's `independent_complete`. If a complete check rejects a claim,
one correction may use the original source, draft and only its nonpassing feedback.
The whole revised draft is checked again without previous verdicts. There is no second
correction. Unresolved or incomplete checks are held; a second rejection is explicit.

Each request is reserved durably before contact. Exact source, route, language, prompt,
draft and parsed-result hashes bind cached work; uncertain requests are not replayed
automatically. Confirmed transient provider failures can resume after a saved cooldown,
but never retry within the same pass. The single correction permits at most two
transport attempts: only an explicit HTTP 429 or 503 before any corrected draft allows
one retry of the identical prompt after its saved cooldown. Unknown/timeout, invalid
output, a second transport failure or an already completed correction never starts
another repair request. Keep state and immutable body files together between invocations.

`model_checked` means the configured model accepted the exact internal draft. It does
not establish independent truth, complete editorial quality or permission to deliver.
The renderer includes all checked statements in order, source attribution and a link;
it cannot fix missing qualifications by silently editing prose. Old experimental
generations stay inspectable and do not inherit this status.
Source-page publication metadata, feed publication metadata and acquisition time are
rendered separately when available. Missing dates remain unknown; a later feed update
or acquisition timestamp does not make an old article new.

The next real-output gate must exercise this actual engine entry with one normal
source-attributed candidate, without diagnostic controls or manually selected claims.
Independent review must compare its exact final text against the full source. A
successful control fixture or parser replay cannot establish that result.

### Optional provider-aware pacing

`pacing: fixed` preserves a conservative 65-second enrichment spacing. Explicit
`provider_aware` may reduce that fallback only with fresh, validated target-route and
latest shared-provider observations. The operator's `llm.min_request_interval_seconds`
always remains a floor, as does the configured RPM interval. A configured 65-second
floor therefore stays 65 seconds. Missing/stale observations use the conservative
fallback; a cold writer-to-verifier transition normally has no verifier observation.
Daily reset/cooldown evidence remains authoritative beyond short telemetry freshness.

Reports retain safe numeric usage, local input count, signed actual-minus-estimated
input delta and normalized quota/reset provenance. Different models do not imply
independent organization budgets. Full-cycle capacity must include source acquisition,
selection, cold-start waits, factual checks and the possible correction. Isolated API
latency or a warm-cache calculation does not demonstrate sustained throughput. No
production timeout, frequency or quota allowance changes with this setting.
