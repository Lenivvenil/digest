# Blind evidence review (opt-in experiment)

This runbook describes the RSS-review code available on main. Full-source enrichment
in [closed, unmerged PR #93](https://github.com/Lenivvenil/digest/pull/93) is not installed by these
instructions. Editorial quality remains open in [#55](https://github.com/Lenivvenil/digest/issues/55).
Optional primary and supplementary presentation translation is documented in
[README](../README.md#language-and-optional-post-translation) and
[ADR-0005](decisions/0005-optional-presentation-translation.md). Its presentation acceptance
is separate from #55's full-source factual-quality requirement. Current work order is
tracked in [#91](https://github.com/Lenivvenil/digest/issues/91).

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

## Configured interests and reason fidelity

The shared selection prompt retains the technology-architect audience and considers
practical, operational and business relevance across operator-defined categories.
Its `configured_category_interests` contains only distinct category labels already
present in the exact RSS packet, with an unambiguous match to an enabled configured
source. Matching follows the existing source sanitization and category truncation;
collisions, including disabled look-alikes, convey no configured intent. Missing
context never removes evidence or establishes irrelevance. There is no new reader
profile, category quota or mandatory category coverage.

No feed URLs, unrelated/disabled source list, personal profile, feedback or allocation
priorities are added to the prompt. Priorities retain their allocation role and do
not become editorial scores. The context is bounded by the existing packet count
and 200-character category field. It adds input text without reducing the existing
16,000-character evidence allowance or increasing requests, output limits or routes.
Input headroom therefore decreases: the revised system text adds 970 UTF-8 bytes
(166 content tokens with the existing local `o200k_base` tokenizer). With arbitrary
JSON-escaped labels, the added context has a conservative ceiling of 24,115 UTF-8
bytes at 20 evidence items, or 64,235 bytes across the supported 100-item setting
and existing evidence-character bound. These are safety bounds, not typical usage;
normal short category labels are much smaller. For byte-based `o200k_base`, standalone
context token counts are at most those byte ceilings. These estimates exclude
provider framing and do not establish Gemini or other provider token accounting,
free-tier entitlement or guaranteed prompt fit. No counting call is added.

Reasons must distinguish a supplied observation from conditional relevance inference,
avoid attributing unstated mechanisms/results, and describe insufficient excerpt
evidence without judging the unseen full article. The same restraint applies to
non-selection and duplicate reasons. Existing strict quote/provenance checks remain;
they cannot mechanically prove that every generated claim follows from its quotation.
Capacity-only omissions still require `deferred`, never editorial rejection.

All selection, planned-packet, reconciliation and resume paths hash the same complete
messages. The changed prompt cannot silently reuse an older model-review result as a
new-contract review. Historical reports, completed candidate judgments and accepted
preparations stay readable and keep their original hashes; this change does not
reopen prior editorial rejections. Technical-deferred work retains existing eligibility
and scheduling rules and is not considered re-reviewed merely because a new packet
can be planned. No persisted-state or YAML migration is introduced.

Offline fixtures verify this wiring, privacy boundary and provenance only. Semantic
acceptance for #121/#55 remains open: inspect supported facts, relevance inferences,
inadequate evidence and dispositions in a subsequent ordinary authorized run or an
explicitly admitted bounded replay, preserving the original failed evidence and the
unchanged request/provider/Actions budgets. A new paid/private evaluation is not
required by this change. Do not claim the prompt or synthetic responses prove better
selection or factuality.

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


### Literal-quote failure diagnostics

The bounded Irritator narrative stage keeps literal quotes in the source language;
only generated claims, assumptions, explanation and limitations use the configured
output language. A literal mismatch still stops that stage before external search.
The matcher is not relaxed into paraphrase or semantic equivalence.

For a known evidence ID and a quote within the existing 200-character allowance,
the private Irritator archive can retain the rejected quote, evidence ID and immutable
bundle ID alongside existing prompt/response hashes. This permits comparison against
the original checkpoint to distinguish formatting from unsupported text. It does not
retain a whole provider response, and the rejected text is not logged or delivered
as a counter-signal. Unknown IDs and oversized fields do not enter this diagnostic.
Keep these runtime artifacts private; public issue updates should summarize outcomes.
A mismatch alone does not establish hallucination or a translation cause. Source
adapter availability and useful external evidence remain separate #77 acceptance gates.


### Source and ranking outcomes

Bounded query generation and ranking receive only the narrative claim, category,
evidence IDs and validated literal quotes alongside cited RSS evidence. Generated
assumptions and reasons to challenge remain in the original archived narrative but
are excluded from these requests. Legacy query generation uses claim and category
only; legacy ranking remains claim-only. This input boundary does not verify claim
truth or semantic counter-evidence quality.

The local [ADR0011 revision](decisions/0011-source-anchored-investigation-queries.md)
asks for useful grounded queries in both bounded RSS and full-source investigation,
without requiring a copied source phrase. It supersedes the earlier unaccepted
mandatory-anchor proposal. Exploratory hypotheses stay in `intent`, distinct from
source claims. A whole-query literal match, when available, remains exact derived
provenance metadata. Its absence does not block an otherwise valid query set or mark
it incomplete. Existing lexical/schema checks, source validation, exact cited/final-URL
self-source exclusions, query/model/request limits and deadlines remain unchanged.
This local revision does not establish semantic quality or useful live retrieval.

Both rankers classify returned sources as `contradicts`, `complicates`, `supports`,
`context` or `insufficient` against the supplied claim. Every entry must pass field,
identity, score, relation and reasoning validation before filtering; bounded ranking
also verifies its URL-bound quote ID. Only `contradicts` and `complicates` at the
existing minimum score reach public results, even when another relation has a high
score. Bounded results record fixed omission counts for the three non-counter
relations in the existing limitations list. An all-non-counter response is `empty`,
not a ranking failure; an unknown relation fails the response closed. The relation
filter leaves existing public result fields, quote identity, request counts and
ranking caps unchanged; the proposed private audit extension below is separate.
This filter enforces the declared classification; it cannot prove that the model
assigned the semantically correct relation.

Legacy synchronous Irritator processing retains its list-based source API but records
successful, failed and unavailable source attempts separately. A valid empty response
is a successful search; missing Reddit credentials or intentionally unsupported DEV.to
or Lobsters search is unavailable, not evidence that no counter-signals exist. The bounded and
legacy paths share response-envelope validation. No new credentials are provisioned.

Partial source or ranking failures produce `incomplete` while retaining valid results.
If no attempt in a failed stage succeeds, the outcome is `error`; valid searches and
rankings yielding no usable evidence remain `empty`. Telegram and Markdown preserve
the same outcome text. Diagnostics contain counts and exception classes, not raw
provider error bodies. This classification does not prove live endpoint availability
or semantic counter-evidence quality.

arXiv queries are URL-encoded once by the HTTP client, without changing query grammar.
Hacker News discussion links require a supplied usable story ID when no external URL
exists; unidentifiable items are not manufactured into provenance links. An entirely
unidentifiable response is a contract failure. Live query sensitivity, documented
current external availability remain #77 acceptance work.


### Verified protocol references (2026-10-02)

The [arXiv API manual](https://info.arxiv.org/help/api/user-manual.html) defines
`search_query`, Atom responses and error entries. Its [API terms](https://info.arxiv.org/help/api/tou.html)
require a single connection and at least three seconds between requests. The adapter
serializes requests and spaces starts in the owned event loop; the runtime's existing
job concurrency coordinates its scheduled processes. Operators must also account for
other clients/machines they control; this local gate does not coordinate unrelated
processes. Waiting remains inside existing stage deadlines, without added retries.

Both query-generation paths use the same lexical contract: 1–8 words, including
words inside double-quoted exact phrases, within the existing 200-character bound.
Explanations belong in `intent`. Invalid prose/operator syntax fails explicitly;
queries are never silently shortened. This is a syntax contract, not a relevance
classifier. arXiv receives an `all:` prefix for each term or phrase, joined with
`AND`, as described in its [query grammar](https://info.arxiv.org/help/api/user-manual.html#51-details-of-query-construction).
HN receives the lexical text with exact-phrase syntax enabled; Boolean/field
operators and exclusions are not accepted in generated input. Request counts,
source deadlines and ranking requirements are unchanged. This correction does
not establish the cause of earlier read timeouts or prove counter-evidence recall.

The [HN Algolia documentation](https://hn.algolia.com/api) defines full-text `query`,
`tags=story`, `hitsPerPage`, and story `objectID`; its published limit is 10,000 requests
per IP per hour. These references establish request semantics, not current reachability
from a particular runner or independent quota entitlement on shared infrastructure.

Lobsters remains a recognized configuration value but performs no HTTP request and
reports `unavailable`. The maintained [search controller](https://github.com/lobsters/lobsters/blob/dd8d8b792e37ffc577643c450af7b99dc7ae9d3b/app/controllers/search_controller.rb),
[HTML view](https://github.com/lobsters/lobsters/tree/dd8d8b792e37ffc577643c450af7b99dc7ae9d3b/app/views/search)
and [request specifications](https://github.com/lobsters/lobsters/blob/dd8d8b792e37ffc577643c450af7b99dc7ae9d3b/spec/requests/search_spec.rb)
do not establish a supported JSON search contract. This is a verified contract gap,
not a claim about every historical endpoint response. No user configuration is
removed and no undocumented replacement is attempted. Both execution paths preserve
valid results from other sources with an incomplete coverage status; unavailable
search is never counted as a successful empty search.

Generated Irritator prose is constrained by the existing complete-response and provider
output budgets, rather than separate cosmetic character caps on narrative, reasoning
and limitations. Nonempty string types remain required. Query length, literal quote
length and matching, source identity, score and relation validation remain strict.
Text validation failures use fixed field/reason codes without recording rejected prose.
This improves diagnosis and avoids a brittle failure class; it does not identify the
cause of earlier responses whose bodies were not retained. Presentation translation
also explicitly preserves technical data-flow direction; prompt-version cache binding
keeps prior translations intact and distinct from new attempts. Neither change proves
semantic fidelity without reviewing actual output.


### Exact quote selection in bounded counter-evidence ranking

The bounded rank request represents each supplied signal title/snippet once as ordered
exact segments of at most 200 characters. Their IDs bind the signal URL, field, offsets
and original text. The model selects a quote ID; code reconstructs the unchanged literal
quote, including source typos. Unknown or cross-source IDs fail closed. The existing
8,000-character ranking-packet budget includes these segments; omitted candidates remain
visible in diagnostics. Archived ranked results retain their existing literal quote shape.
This prevents transcription errors; it does not establish that a claimed counter-relation
is semantically justified. Scores, relation criteria and request counts are unchanged.


### Grounded Irritator targets and complete abstract evidence

The extraction call selects one concrete source-attributed assertion or announced
decision from its supplied evidence. Reported framing and inferred assumptions
remain visible archive context, not the assertion challenged by search/ranking.
Duplicate reports of one event do not establish independent consensus. The legacy
summary path must abstain if its summary does not support an attributed target;
its summaries are not full primary sources.

A contradiction concerns what that assertion actually states. A complication may
instead identify a sourced implementation cost, condition or tradeoff relevant to
the announced decision. Its explanation distinguishes the external finding from
the editorial relevance link and preserves favourable results and limitations.
It must not invent a simplicity, necessity, primary-solution or sufficiency premise.
An empty result is legitimate, but a source cohort is not predetermined negative:
a faithfully stated material tradeoff can be useful without refuting an announcement.
Previous controlled explanations that introduced unsupported premises remain failures.

Available arXiv abstracts are retained whole and as exact source text. They are not
full papers. Whole candidates are admitted within the existing ranking packet
budget; omitted candidates remain visible in diagnostics rather than being turned
into misleading prefixes. Exact quote IDs preserve source characters and do not
certify semantic relevance. Source text remains untrusted data, with existing URL,
network-response, request-count and deadline boundaries. No additional model pass
is introduced, and offline checks do not establish live editorial quality.


## Prepared editions and delivery recovery (#120)

The managed compact path persists accepted canonical cards/review evidence before
presentation, then freezes a versioned ready edition. Optional review resumes only
from the checkpoint of a confirmed, durably persisted delivery. Sender eligibility
uses the frozen manifest and does not re-run current review validation or generation.

Preparation defaults to today's UTC publication window. An explicit
`--prepare-edition --edition-date YYYY-MM-DD` can prepare a later day; the sender
reports `pending_window` until that day begins. The canonical preparation checkpoint
retains the same intended date across midnight. Future readiness never authorizes
early delivery. See the [README commands](../README.md#compact-daily-presentation)
and [ADR0007](decisions/0007-compact-issue-reservation.md) for the remote barriers.

If preparation fails, inspect its accepted checkpoint and any existing ready edition.
If delivery is held, retain the claim, exact ready payload and per-chunk receipt file;
compare accepted Telegram message IDs before authorizing recovery. Unapplied coverage
can mean transport succeeded but feedback/dedup persistence did not. Never reset the
claim or interpret optional-stage failure as evidence that primary sending failed.
On a rejected Git push, retained private diagnostics preserve the same three files;
no automatic rebase or resend is allowed. This recovery contract does not certify
full-source factual quality or independent counter-evidence.


## Ordinary preparation candidate accounting

`--prepare-edition` with review-led mode captures the collector inventory before
source allocation and saves candidate progress before its existing primary call.
One fresh preparation window retains the existing primary/fallback request ceiling. Later fresh
preparations choose unseen eligible work before technical retries; presentation of
already accepted work takes precedence and adds no selection request.

The mutable progress file is `.cache/candidate_progress.json`. Frozen report-bound
accounting is archived as `<edition>.md.candidates.json` and included in ready-edition
archive hashes. Keep the existing runtime `.cache`/archive persistence step: local
writes are not proof of remote durability. A planned record alone is not proof a
request reached a provider. The same response now includes typed per-ID dispositions. A missing or invalid
entry supplies no editorial rejection reason; capacity-only omission is deferred.
Reasons are judgments over the supplied RSS occurrence, not full-source conclusions. Feed failures, parser limits, source changes and age exclusions are separate.

Do not clear progress to claim complete coverage. Capacity overflow fails without
truncation; retention and sustainable throughput require the #121/#55 acceptance
review. Sender claims, receipts, feedback attribution and delivered caches remain
under ADR0007. Candidate accounting grants no permission to replay a held edition.


A valid primary abstention retains #120's accepted empty snapshot for its publication
day. New candidate responses with only deferred/missing/invalid dispositions do not
qualify as accepted empty decisions; existing legacy snapshots remain compatible. Repeating preparation in that window returns no ready edition; it does not
advance another packet. Unseen work can advance in a later fresh preparation window.
This inherited limit is part of the remaining throughput acceptance, not a claim
that all observed candidates received an editorial decision.


Disposition capture is bound to the exact delivery-used provider/model, evidence
bundle, prompt and raw-response hash. It is separate from legacy ModelReview and
accepted PreparationSnapshot fields. Old reports remain readable with unavailable
per-item reasons; a changed prompt cannot claim same-prompt reuse. Selected reasons
are not duplicated. Duplicate references must retain a validated selected identity
from the same request; shared topic alone is insufficient and contrary accounts must
remain eligible. Structural checks do not certify semantic correctness.

The candidate working set uses direct verified source and packet references. Each
report preserves only its own packet and current collection accounting; resolved
historical bodies are not expanded for unrelated packet admission. Per-identity
indexes retain exact decisions, while reversible policy exclusions remain enumerable
for reapproval/unblocking. Current alternate occurrences are bounded by source binding;
older revisions remain immutable evidence. Unknown or undelivered selected work stays
recoverable. Missing/corrupt required objects fail explicitly; unrelated historical
objects are not read. Unsupported undeployed prototype codecs fail explicitly, while
deployed accepted preparation and ready-edition formats remain compatible. Do not
delete evidence or delivery markers to bypass a recovery error.

### Proposed private ranking audit

The local [ADR0012 proposal](decisions/0012-private-ranking-evidence.md) adds a versioned
`ranking_audit` to the companion private `.irritator.json`. It preserves exact admitted
source records, explicitly truncated diagnostic previews of omitted candidates,
original hashes/lengths, query lineage, admission causes and fully validated model
dispositions. `not_returned` means absent from a valid bounded model response;
`pending` means no wholly valid response was obtained. Neither means irrelevant.

Only JSON retains this trace; Markdown gets a concise summary/reference, and Telegram
and translation keep their existing selected-prose inputs. The omitted-text allocation
reuses 16,000 characters as a new proposed archive policy, not a ranking threshold.
See the proposal for serialization bounds and the limits of truncated evidence. Old
archives lack this evidence and cannot be retrospectively audited from hashes alone.
This local proposal has not been accepted or deployed.
