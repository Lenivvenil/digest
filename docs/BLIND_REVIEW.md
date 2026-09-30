# Blind evidence review (opt-in experiment)

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
   or excerpt. Invalid entries invalidate the entire response. An empty selection
   needs an explicit limitation, distinguishing abstention from malformed output.
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
`ok`/`abstained` slots whose provider, model, evidence hash and current prompt hash
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
prompt hash, or cached-review validity. It reduces model work to the two blind
slots plus an optional disagreement slot, before configured retries. It does not
guarantee provider availability or a completed comparison.

## Primary-first runtime with preserved Irritator

The explicit `review.review_led_only: true` mode now delivers the primary
selection first. It makes one primary request, or one secondary fallback only
when the primary is unavailable/invalid. A valid abstention is respected. The
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
