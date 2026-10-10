# Preparing, publishing and recovering a digest

Use this guide from the runtime working directory that owns `config.yaml`, `.cache/`
and the output archive. Start with the [safe quick start](../README.md#quick-start)
for installation. The [domain model](domain/digest/overview.md) explains the terms;
[Architecture](ARCHITECTURE.md) explains why the persistence boundaries exist.

For a new runtime, use [configuration and delivery settings](#runtime-configuration).
For an interrupted edition, start with [delivery states and recovery](#delivery-states-and-recovery).
[Language](#language-and-optional-post-translation), [feedback](#feedback-and-source-decisions)
and [discovery](#source-discovery) have separate settings and operating boundaries.

## Choose the scenario

| Task | Entry | Important boundary |
| --- | --- | --- |
| Prepare the ordinary review-led edition | `python -m digest --prepare-edition` with review enabled, review-led-only mode and compact delivery | Does collection/model/presentation work as needed, but does not send. |
| Inspect, claim or send a frozen edition | `python -m digest --edition-phase inspect\|claim\|send` | Sending consumes exact persisted ready/claim hashes and makes no model call. |
| Preview Radar output | `python -m digest --dry-run --radar-only` | Can fetch sources and call models. It is not a no-network check. |
| Run an independent report-only comparison | `python -m digest.review_trial` | Uses independently pinned review slots; it does not publish a primary edition. |
| Resume an admitted supplementary comparison | `python -m digest.review_resume` | Uses a saved checkpoint and a separately persisted attempt marker. |
| Propose sources | `python -m digest --discover` | Saves proposals and requests approval; it does not activate them. |
| Run legacy direct/category delivery | Ordinary non-prepared invocation with its configured mode | Retains different transport, Markdown and guard rules. Do not mix it into the prepared protocol. |

The `--check` command probes feeds. To validate configuration without external calls,
use the disabled example and commands in the quick start. Model routes, credentials,
source activation and schedules belong to the operator's runtime.

### CLI options

Use `python -m digest --help` for the complete option list.

| Option | Behavior |
| --- | --- |
| No flags | Run the configured non-prepared scenario (category summaries or review-led cards); compact publication still requires its guard. The runtime owns Git persistence. |
| `--config PATH` | Read YAML configuration; default `config.yaml`. |
| `--dry-run` | Fetch/analyse and print results without normal Telegram/Markdown digest delivery; may call models. |
| `--radar-only` | Return before Irritator and normal digest delivery; may fetch/call models and, without `--dry-run`, poll feedback. |
| `--verbose` | Enable debug logging; inspect logs before sharing. |
| `--check` | Validate configuration, check expected environment variables and probe feeds. |
| `--feedback-precollected` | The managed runtime owns feedback ingestion; do not poll again in this process. |
| `--discover` | Propose/validate sources, persist candidates and send approval cards when configured. |

Prepared-edition flags use the [publication barriers below](#persist-claim-and-send).
Managed `--discovery-phase prepare|send` and its pending/delivery hashes also require
[separate proposal persistence](ARCHITECTURE.md#trial-source-lifecycle-and-discovery).
Neither operation is made safe by a local hash alone.

## Ordinary preparation

<a id="review-led-delivery-without-legacy-enrichment"></a>

The ordinary mode uses `review.enabled: true`, `review.review_led_only: true` and
`telegram.delivery_mode: compact`. It does not generate legacy category summaries
or wait for an independent comparison before preparing selected cards.

The managed runtime owns feedback collection, durable persistence and acknowledgement
of the exact saved batch. Pass `--feedback-precollected` whenever that separate stage
owns ingestion, including if its optional collection/persistence/acknowledgement fails,
so preparation cannot consume another uncommitted batch. The flag skips polling; it
does not certify prior success. Without it, preparation uses its normal feedback path.
See the [feedback sequence](#feedback-and-source-decisions); local saving alone is not
a remote persistence guarantee.

```sh
python -m digest --config config.yaml --feedback-precollected --prepare-edition
```

Preparation processes feedback/approved source changes before recovery. It then
prefers an existing ready/claimed edition, or a matching accepted preparation,
before fresh collection. A completed candidate report is a different object: it
is considered after collection and current eligibility reconciliation.

A fresh packet uses one primary attempt and, only if primary output is invalid or
unavailable, at most one configured secondary fallback. The resulting report can
remain `incomplete` for independent comparison even when its cards are usable.

| Editorial result | Preparation outcome |
| --- | --- |
| Valid selected cards, including a validated partial result | Save canonical work, then attempt presentation/archive/freeze. Rejected or unresolved items remain visible in review/candidate evidence. |
| Valid primary abstention with complete required disposition evidence | Save an accepted empty preparation; return no ready edition. Repeating the same publication window does not select another packet. Older packets without disposition capture retain their existing compatibility behavior; fresh candidate packets record attempts. |
| Primary invalid/unavailable; secondary supplies valid selected cards | The permitted fallback supplies cards with its actual provenance; independent comparison is still incomplete. |
| Primary invalid/unavailable; secondary only abstains | Remains incomplete. It is not an accepted no-news result. |
| No usable selection, unfinished output or unresolved empty metadata | Retain unfinished evidence; no ready edition. Ordinary candidate validation failure reports `selection_incomplete`. |
| No eligible candidates | No edition is created. This is not evidence that all sources succeeded or every observed item received an editorial judgment. |

`no_ready` is a publication outcome, not a universal no-news verdict. In particular,
the supported category path also retains its existing `no_ready` result for empty
or failed category analysis. Inspect the saved review/checkpoint and logs before
assigning an editorial meaning to that status.

### What preparation preserves

- `.cache/pending_preparation.json` contains accepted canonical work for the intended
  publication day. It is the recovery point after translation, attribution, archive
  or render failure.
- `.cache/prepared_edition.json` contains the final Telegram payloads and their
  evidence/archive bindings. It is the only input the prepared sender may publish.
- The review archive and candidate proof explain which occurrence and response
  support the accepted cards. A source quote cannot validate unrelated generated claims.

The accepted checkpoint is removed after successful readiness freeze. A valid empty
accepted checkpoint remains until its publication day is past. Corrupt or mismatched
active work raises a failure; it is not silently replaced by another selection.

### Intended publication day

Without `--edition-date`, preparation targets today in UTC. Use
`--prepare-edition --edition-date YYYY-MM-DD` to prepare a later day explicitly.
The ready edition remains `pending_window` until that day's 00:00 UTC and expires at
the end of that day. A preparation made just before midnight does not receive another
24 hours merely because it was created late.

A confirmed/applied edition can permit preparation for a later day. An unresolved
claimed edition blocks replacement, including on a later day. There is one active
record set, not a queue of independently sendable editions.

## Persist, claim and send

The engine writes local files. The managed runtime must establish remote durability
before the next irreversible step. Use its existing persistence workflow rather than
inventing a resend path around a failed push.

| Boundary | Required remote evidence | Next engine operation |
| --- | --- | --- |
| Readiness | Successfully persisted `.cache/prepared_edition.json` and its referenced archive/evidence, with the exact ready-file SHA-256 verified from the remote revision | Create the claim. |
| Claim | Successfully persisted `.cache/prepared_edition_claim.json`, with ready and claim hashes verified from the same remote revision | First send in the continuing managed attempt. |
| Outcome | Persisted receipts and resulting feedback, delivered cache and accounting, including partial/failure states | Inspect completion; optional work must not discard this evidence. |

The workflow-provided `READY_SHA` and `CLAIM_SHA` below are SHA-256 hashes of whole
file bytes. They are not the edition ID or the manifest's internal content hash.
An existing local Git object or locally calculated hash alone does not prove a
successful remote push.

```sh
python -m digest --config config.yaml --edition-phase claim --ready-sha "$READY_SHA"
# Persist the claim and verify both hashes from the successful remote revision.
python -m digest --config config.yaml --edition-phase send --ready-sha "$READY_SHA" --claim-sha "$CLAIM_SHA"
# Persist receipts and resulting operational state, including on partial failure.
```

Claiming validates the edition window and referenced evidence before creating an
immutable claim. Sending validates the exact ready/claim bindings, owner, bot and
checkpoint bytes. It creates `sending` receipts before POST, records attempted count
before each chunk, and records each known positive matching-chat confirmation afterward.
The prepared transport does not retry or switch to plaintext.

Complete article coverage and complete edition transport are different. Every chunk
covering an article must be confirmed before its identity is attributed. A final
notice can fail after all article cards were delivered. After transport, the caller
applies known coverage and only then marks receipts `applied`.

## Inspect without publishing

Inspection performs no collection, model request or Telegram POST. The command
publishes `edition_status` and `ready_sha256` through `GITHUB_OUTPUT`; its exit code
alone distinguishes neither all normal states nor all failure causes. Outside a
managed job, capture those outputs in a temporary file:

```sh
digest_inspection_output=$(mktemp)
GITHUB_OUTPUT="$digest_inspection_output" python -m digest --config config.yaml --edition-phase inspect
cat "$digest_inspection_output"
rm -f "$digest_inspection_output"
```

A held inspection returns exit code 1; ordinary `missing`, `ready`, `pending_window`,
`confirmed` and `expired` inspection returns 0. Malformed/binding errors fail rather
than inventing a valid state. In a shell using automatic exit-on-error, retain the
inspection output even when the command reports a hold.

## Delivery states and recovery

<a id="prepared-editions-and-delivery-recovery-120"></a>

The inspector keeps its conservative status contract and adds a bounded warning for
valid prepared holds. It distinguishes unresolved dispatch, incomplete application,
and an applied marker with incomplete transport. The warning uses already-read
records: state, persisted attempted/confirmed/total chunks, fully covered articles
and existing evidence filenames. Absent receipt counts are unknown, not zero.
Unrecorded transport effects and the accounting write prefix cannot be inferred.
Inspect the retained files, bindings and workflow attempt; never treat missing
receipts or marker presence as permission to resend or reapply. Legacy compact
holds and malformed-binding errors retain their separate existing behavior.

| Observed evidence | Meaning | Safe next action |
| --- | --- | --- |
| No ready file, claim or receipts; no active accepted checkpoint | No recoverable prepared edition is present. | Use the normal preparation path within its existing budget. Inspect collection/review outcomes if it again returns no ready edition. |
| Valid matching `pending_preparation.json`; no active ready/claimed edition | Canonical work was accepted but presentation/freeze is unfinished, or it is an accepted empty result. | Run preparation for the same intended day. It resumes accepted work; empty acceptance remains no-ready for that day. Do not rerun selection to replace it. |
| Missing, replaced or mismatched accepted preparation reference | The active saved work does not match the supplied reference, or its requested publication day is invalid. | Inspect and preserve the checkpoint and failure evidence. Do not clear/rewrite it to bypass verification. No new ready/receipt hold is created; category fetch statistics/map may already have been written before entry verification failed. |
| `selection_incomplete`, without accepted preparation | A usable editorial result was not accepted. | Retain candidate/review evidence and diagnose the recorded failure. A later admitted preparation may continue; the result must not be reported as editorial rejection or accepted no-news. |
| Unclaimed `pending_window` edition | Frozen content targets a future UTC day. | Preserve it and wait for the publication window. Do not claim or send early. |
| Unclaimed eligible `ready` edition | Content is frozen but not yet reserved. | Verify the remote ready/evidence revision, then use the normal claim/persist/send sequence. Claim validation still checks referenced bytes. |
| A new claim created and persisted by the current uninterrupted managed attempt; no receipts yet | The workflow is between its claim barrier and first send. | Continue that same attempt with the verified ready and claim hashes. This is not recovery of an old unknown attempt. |
| A claim rediscovered after interruption; absent receipts | The earlier process may have sent before its local outcome was retained remotely. Inspection reports held. | Preserve the records and inspect workflow/Telegram evidence. Do not reclaim, regenerate or treat absent receipts as proof of no delivery. |
| Receipts `sending`, `unknown`, `partial` or `failed` | Some or all transport is unfinished, failed or uncertain. Known confirmations remain evidence; later automatic publication is held. | Preserve attempted/confirmed prefixes and matching message IDs. Inspect before any targeted recovery; never rerun the sender blindly, including for a later day. |
| Receipts `confirmed` with `applied: false` | Telegram transport completed, but operational-state writes or the final applied marker did not finish. | Preserve receipts and compare current feedback, statistics, lifecycle and delivered-cache records with the saved write order. There is no general automatic reconciliation command; blind reapplication can double-count a persisted prefix. |
| Matching prepared receipts `confirmed` and `applied: true` within the publication window | The edition and its application completed. | Treat inspection as a no-op. Do not send again. Preparation for a later intended day can proceed through the normal guard. |
| Expired unclaimed edition, or expired confirmed/applied edition | It is no longer eligible for sending. | Prepare a later/current permitted day normally. Do not reset unresolved claims: they remain held rather than becoming safe through expiry. |
| Corrupt/unsupported JSON, wrong owner, orphan claim/receipts, or mismatched hashes/references | The saved objects cannot establish a coherent publication attempt. | Retain the files and restore/repair only from verified matching evidence after checking possible external sends. Do not delete a marker or substitute unrelated older state to bypass validation. |
| Legacy `.cache/compact_issue.json` is reserved/sending/partial/unknown, or already confirmed for the relevant day | A legacy direct-compact attempt also constrains publication. | Inspect that legacy attempt. Switching to prepared mode or deleting the old marker is not a recovery procedure. |

A confirmed legacy marker may also produce the inspector's `confirmed` result;
verify which record supplied it before expecting prepared receipts or a manifest.

### Why an unapplied receipt needs inspection

Prepared application writes feedback, source statistics, optional lifecycle state,
and the delivered cache in that order, then marks receipts applied. If the third
write fails, the first two may already be present. A subsequent unconditional repeat
is not a transaction rollback and can count the same delivery again. The held state
exists to make that uncertainty visible.

Use the [current effect matrix](ARCHITECTURE.md#applying-confirmed-outcomes) and
retained Git/run evidence to establish what actually persisted. Recovery requires a
reviewed resolution of that specific prefix; this guide supplies no marker-reset or
blind resend command.

## Walk through the boundaries

These are behavioral examples of the existing contract, not new live-run instructions.

1. **Selection succeeds, presentation fails.** Canonical work is saved in
   `pending_preparation.json`. The same-day next preparation uses that snapshot,
   possibly retries missing presentation work within its rules, and does not select again.
2. **Primary genuinely abstains.** Complete required metadata allows an accepted empty
   checkpoint. No ready file appears; repeated same-window preparation does not consume
   another candidate packet. An abstaining fallback after unavailable primary instead
   leaves selection incomplete.
3. **An article spans two chunks; the second is uncertain.** Its complete coverage is
   unconfirmed, so that article receives no delivered attribution. Earlier complete
   articles may have known coverage. The edition remains held.
4. **Telegram confirms all chunks; a state write fails.** The receipt can be confirmed
   but unapplied. A later inspector reports held and the sender does not replay it.
5. **A fully applied edition is inspected again.** The normal path reports completion
   without collection, generation, rendering, transport or repeated accounting.

## Candidate continuation

<a id="ordinary-preparation-candidate-accounting"></a>

The review instruction evaluates substantive supplied information before reader
relevance: a concrete development, finding, explanation or usable resource. A
relevant question or promised discussion alone is insufficient. Concrete future
announcements remain eligible, with attributed claims and plans distinguished from
achieved outcomes. `deferred` is reserved for otherwise useful items exceeding the
detail budget; insufficient substance or relevance is an editorial `not_selected`
judgment about the supplied metadata, not the unread full article.

These are model instructions, not semantic guarantees enforced by quote validation.
The [#55 correction](https://github.com/Lenivvenil/digest/issues/55) changes prompt
identity for future review; it neither reopens prior decisions nor proves improved
selection before a new ordinary output is inspected.

Candidate progress is saved before a review call. Admission works within the configured
count, character, retry-opportunity and storage bounds; it does not promise to drain
an entire feed cohort in one run. Missing/invalid dispositions and capacity-only
omissions remain unfinished. Policy exclusions, duplicates and editorial not-selection
retain distinct evidence.

The protected first-unseen opportunity uses original identity observation age,
then existing priority/source/identity ties, and skips evidence that cannot fit.
Technical retries and the remaining fresh/age source turns follow within the same
packet. At a one-item cap this deliberately favors age over freshness; a larger
old excerpt can leave less room for later items. Planning is only an opportunity,
not a completed review. See the [oldest-unseen amendment](decisions/0008-candidate-selection-progress.md#oldest-unseen-opportunity-amendment-196).

With optional closing enabled, an otherwise absent approved source can receive
one fitting review opportunity. Spare capacity is used first; a full packet may
defer only its final ordinary backfill item while preserving the first unseen
opportunity and reserved technical retries. The deferred item stays pending.
Count and character limits do not increase, and no fit means no substitution.
An already admitted approved occurrence leaves the packet unchanged. This can
postpone a professional item and does not promise a suitable positive story;
see [the bounded admission decision](decisions/0014-optional-humane-closing-item.md#bounded-closing-source-opportunity).

Keep `.cache/candidate_progress.json` with its immutable source/report objects and
indexed decisions. The archive's `.candidates.json` file is a bounded as-of account
of one report, not a continuously rewritten inventory of every historical body.
Missing required objects fail explicitly. Do not clear progress or rewrite old
rejections to claim complete coverage.

## Independent comparison and resume

<a id="contract"></a>
<a id="resume-a-report-only-comparison"></a>

Independent comparison differs from ordinary primary/fallback preparation. Primary
and secondary slots receive the same frozen evidence and messages and keep their
configured provider/model identities. A failed slot stays failed; another review is
not relabelled as its opinion. Valid complete results may trigger one configured third
review according to overlap. Partial results remain incomplete and do not establish
comparison agreement.

```sh
python -m digest.review_trial --config config.yaml --resume previous/review.json --output fresh-output
```

This manual command can call configured providers. It requires a fresh output path
and leaves the original report intact. Reuse requires matching slot/provider/model,
bundle and current prompt identity plus valid retained selections. Accepted canonical
preparation and frozen editions are not reinterpreted under this new-request policy.

The scheduled `digest.review_resume` path has separate prepare/execute phases and a
separately persisted one-attempt marker. The managed runtime persists the marker
before execution; execution records its start in that marker before provider work.
Its bounded results are archived as supplements; they do not create another primary
Telegram edition or alter its deduplication outcome. Follow the current configured
request allowance rather than assuming comparison is free.

## Supplementary investigation

<a id="primary-first-runtime-with-preserved-irritator"></a>

After confirmed, durably persisted primary delivery, supplementary work can use the
saved evidence checkpoint under a separate reservation. Irritator extracts an attributed
target, plans queries, searches external sources, validates results and ranks their
relationship to the target. New compact schema-3 attempts bind canonical cards from
the exact confirmed/applied edition and their original source occurrences. They retain
exact card/source quotations in query and ranking inputs and exclude speculative
hypotheses. Unbound standalone contracts remain available separately.

Empty search results, unavailable/unsupported adapters, rejected evidence and failed
model stages are different outcomes. Optional translation failure retains canonical
fallback according to its existing contract. An accepted compact result can freeze one
fragment for an ordinary edition on origin day D+1..D+3, after main cards and before
the closer. No extra dispatch or model work is created. Unusable material remains
explicitly archive-only; partial searches retain their limitations. Actual UTC D+4
expires unused material; a future ineligible edition does not expire it early.
The legacy standalone transport retains its separately documented behavior.

Fragment editions use ready schema 3 with independent supplement chunk coverage and
an explicit current-review checkpoint. Complete owner-matching coverage must be
consumed before the applied receipt marker. Claimed/unknown publication is never
automatically released or replayed. The runtime must persist the existing consumed
attempt under `digests/` with `.cache/` receipts and provide the private owner ID to
post-prepare. See [ADR0007](decisions/0007-compact-issue-reservation.md) for the complete
rollout and unclaimed-ready release contract.

New post-attempt markers bind the versioned Hacker News/arXiv/DEV search policy,
effective source list and query ceiling. DEV uses the documented unauthenticated
Forem V1 first-page search; it does not fetch full articles. Operators enable that
bounded slot explicitly in `irritator.sources`. Up to nine real source requests
can now occur within the existing three-by-three ceiling and stage deadline.
See [ADR0022](decisions/0022-versioned-bounded-search-policy.md) for the exact
contract, validation and historical-policy boundary.

An older compact attempt marker or a changed source-policy binding is held before
execution. Preserve it and inspect its original engine/configuration; do not
remove markers or archives to rerun an old edition. Completed attempts and saved
results remain protected. Confirm no pending old-policy attempt crosses a rollout.

Independent comparison and genuine external counter-evidence are separate operations.
Neither a matching quotation nor successful transport establishes factual usefulness.
The [Irritator model](domain/irritator/overview.md#current-domain-model) describes the
source/evidence relationship; [transport compatibility](ARCHITECTURE.md#supported-application-scenarios)
describes the different sending protocols.

## Runtime configuration

Keep `config.yaml`, credentials, `.cache/` and generated output in a separate runtime.
Install a reviewed immutable engine commit there, retain the previous pin, and run
from its own working directory. Two instances must not share writable state.
The engine's CI checks source code; it does not install a daily schedule. A managed
runtime owns its schedule, secrets, engine pin, serialized runs and Git persistence.

Start from the [disabled example](../examples/config.example.yaml), replace the model
and feed placeholders, and enable the intended source. Keep Telegram and Markdown
delivery disabled while reviewing an intentional live preview:

```sh
python -m digest --config config.yaml --check
python -m digest --config config.yaml --dry-run --radar-only
```

`--check` probes feed URLs and checks expected environment variables. It does not
establish editorial quality. `--dry-run` can fetch sources and call models, consuming
their quotas, while suppressing normal digest delivery and saved digest output.
Use the README's disabled-example commands for an offline empty-input check.

### Environment variables

Load selected credentials securely through the shell or runner. The engine does
**not** automatically load `.env`; [.env.example](../.env.example) lists reference
names only. Never put real credentials in a committed URL, screenshot, issue or log.

| Variable | Purpose |
| --- | --- |
| `ANTHROPIC_API_KEY` | Anthropic provider |
| `GEMINI_API_KEY` | Gemini provider |
| `GROQ_API_KEY` | Groq provider |
| `MISTRAL_API_KEY` | Mistral provider |
| `DEEPSEEK_API_KEY` | DeepSeek provider |
| `TELEGRAM_BOT_TOKEN` | Configured Telegram bot |
| `TELEGRAM_CHAT_ID` | Intended destination and supported owner checks |
| `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USERNAME` | Optional existing Reddit adapter credentials |

Missing keys disable matching provider routes; they do not guarantee another route
can succeed. Missing required Telegram credentials is a failure even when Markdown
can be saved.

### Model routes

The provider adapters support Anthropic, Gemini, Groq, Mistral and DeepSeek.
Configure only intended services and models. Check current model availability,
account entitlement, pricing, request limits and token limits before a live run;
context capacity is not a free throughput budget. Do not inadvertently add a paid
fallback to a free-only runtime. Exhausted eligible routes can leave failed or
incomplete work.

Category-analysis mode uses role/provider routing, bounded retries and fallback.
Category work runs concurrently within configured limits before cross-category trends.
A category-specific route can nominate a provider/model; unrouted categories use the
configured chain. Missing credentials follow the existing fallback rules. These
model IDs are placeholders, not recommendations:

```yaml
llm:
  providers:
    - name: gemini
      model: YOUR_GEMINI_MODEL_ID
      role: [summarize, rank_signals, fallback]
    - name: groq
      model: YOUR_GROQ_MODEL_ID
      role: [fallback]
  routing:
    - categories: [AI Engineering]
      provider: gemini
      model: YOUR_GEMINI_MODEL_ID
```

Review slots are separately pinned. See [ordinary primary/fallback preparation](#ordinary-preparation)
and [independent comparison](#independent-comparison-and-resume) rather than assuming
category routing supplies the same fallback semantics for every operation.

### Sources and categories

Edit the runtime's `sources` list. The public example contains no private source list:

```yaml
sources:
  - name: My Feed
    url: https://example.com/feed.xml  # Replace before enabling
    category: Architecture
    enabled: false
    priority: 3
    recency_hours: 24
    trial: false
    trial_days: 7
```

Priority ranges from 1 to 5 and influences source-slot allocation; it does not prove
editorial value. Categories are operator-defined. Banking/payments, AI engineering,
distributed systems, enterprise architecture and regional affairs are examples from
the project's original use, not mandatory personal-profile presets.

### Delivery settings

Create a Telegram bot through the official BotFather flow and configure its token and
intended chat ID in runtime environment variables or Actions secrets. The engine sends
to `TELEGRAM_CHAT_ID`; it does not select recipients. Set `telegram.enabled: true`
when ready, and `telegram.required: true` if a Markdown archive alone must not count
as successful delivery. Enable `obsidian` to save Markdown to its configured output
directory, then sync that runtime archive to Obsidian. API acceptance does not mean
a person read the edition.

`telegram.delivery_mode: compact` assembles selected articles into one logical edition,
using necessary chunks without cutting selected text or source URLs. Indexed vote
buttons retain article identity; translation and model-attribution notices appear
once per issue. The ordinary managed path requires [prepare, persist, claim and send](#persist-claim-and-send).
Omitting delivery mode preserves legacy per-article cards; switching formatting alone
does not supply the prepared publication protocol.

## Language and optional post translation

Without a `translation` section, legacy `radar.language` accepts `en` or `ru` and
its omitted-field default remains `ru`. Upgrading an existing Russian runtime does
not implicitly add translation calls. A new explicit translation section defaults
to English canonical generation only when no generation language was specified.
Enabled translation rejects an explicit `radar.language: ru` conflict.

To opt in, choose an already configured, entitled route:

```yaml
radar:
  language: en
translation:
  enabled: true
  target_language: ru
  provider: gemini             # Must exist in llm.providers or an explicit review route
  model: YOUR_GEMINI_MODEL_ID  # Exact match to that configured provider/model
  max_calls: 1                # Per presentation pass, without HTTP retries
  timeout_seconds: 90         # Total budget, including shared pacing and the request
  max_output_tokens: 2048
  max_input_chars: 12000
```

Translation changes generated card/category prose and published Irritator narratives
and reasoning. Original titles, source metadata, URLs, literal quotations and raw
reviews/evidence remain canonical. The target does not change analysis, review input
or search queries. Telegram and Markdown receive the same presentation.

The route is pinned with no automatic provider fallback or retry. Limits constrain
optional presentation, not article selection. Oversized input or exhausted allowance
keeps canonical publication with a visible status. The default total timeout is 90
seconds; explicit values up to 180 are supported. Include provider pacing in that
budget: a 65-second shared interval leaves little request time in a short budget.
A known wait beyond the remaining deadline makes no attempt record or request.
See [supplementary timing and canonical archives](decisions/0005-optional-presentation-translation.md#bounded-supplementary-presentation)
for the separate post-delivery allowance; configuration does not establish free quota.

Persist private `.cache/translations/` for primary reuse and the supplement's
`.translations/` records beside its canonical `.irritator.json` archive. Compatible
completed batches can be reused. An attempted failure or interruption ends translation
for that canonical version; fallback may already have been delivered. Inspect the
record before an explicit retry, and never delete delivery state to retry translation.
New canonical text, target, model or prompt gets a separate key. Dry-run uses temporary
translation storage and may still consume quota.

Structural validation preserves field IDs, numeric literals, URLs and recognized
quotation/code spans, not every aspect of meaning. Machine translation is labelled;
incomplete translation falls back to canonical English. Finite observed translation
and fallback evidence is recorded in [ADR-0005](decisions/0005-optional-presentation-translation.md#what-validation-does-and-does-not-establish).
It does not establish general fidelity or complete #55's editorial acceptance.

## Feedback and source decisions

Feedback collection is independent of `adaptive.enabled` and uses no continuously
running bot service. The supported owner is the configured private chat; the sender
must match it. Group and inline callbacks are rejected. Set `telegram.bot_username`
to the plain username of the same bot as the token for 👍/👎 links. Tap a vote and
Telegram's **Start** button; the next eligible run collects the message. Commands
`/vote g <article-code>` and `/vote b <article-code>` also work without a username.
Only recognized vote data and `/status` or `/bubble` tags are retained, not arbitrary
message bodies.

For a managed runtime, keep these three steps in order:

```sh
python -m digest.feedback_poll collect
# Commit/push .cache/feedback.json; verify its SHA256 from the successful remote revision.
python -m digest.feedback_poll ack --expected-sha256 "$FEEDBACK_SHA"
```

`FEEDBACK_SHA` must identify those exact durably persisted file bytes. Pass
`--feedback-precollected` to the digest whenever the managed runtime owns collection,
including if its optional feedback stage fails, so the digest cannot consume another
uncommitted batch. Direct local use saves votes and polling offset together before
acknowledgement; local saving alone does not establish remote durability.

The collect command emits two INFO observations:

- **Previous retained successful local collection**, before polling, shows the
  retained UTC time and age in seconds. It stays historical if this invocation fails.
- **Persisted successful local collection**, after the output-file write, reports
  the observation from the same saved bytes used for the exported SHA-256.

Missing, malformed, naive or future times mean unknown; a skipped invocation emits
nothing. Empty polls count as local success. Recent age proves neither complete vote
capture nor remote persistence/acknowledgement; old age does not count lost votes.
A post-save hash/output failure may leave newer state without the second log line.
Keep these observations private. Stdout stage/counts and `feedback_sha256` output
remain unchanged, but the new field changes saved bytes/hashes: verify the exact
remotely committed bytes before acknowledgement.

See [ADR0021](decisions/0021-catalog-feedback-boundaries.md#retained-successful-local-collection--148-g9-2026-10-10)
for timestamp semantics and older-writer field loss, and preserve the
[full-token rollback floor](decisions/0024-full-article-vote-identity.md#deployment-and-rollback-floor).

Ordinary vote/decision messages remain available for at most 24 hours. Legacy callback
buttons are best effort, with about 150 seconds of server retention. A sleeping or
failed schedule can lose uncollected feedback; see [ADR-0006](decisions/0006-batch-message-voting.md).
Saved votes survive later feed/model/delivery failures. Pending replies are best effort
and can be superseded by later collection. Corrupt state is retained for diagnosis.
Unknown/stale cursor history uses a non-confirming read without an offset before
re-anchoring, not an old high offset that could discard updates.

Only the latest valid vote per exact token is effective. New article tokens use
the full 32-character identity; historical eight-character tokens remain supported
without guessed migration. Mixed old/full tokens may count separately, and old
buttons mean this is not a guaranteed short transition. Raw votes never enter model input.
Even with automatic adaptation disabled, feedback can adjust priorities within existing
bounds; unrated sources retain configured priority. With adaptation enabled, reliability,
productivity, description length and recency also affect source scoring, and trials may
graduate or be demoted. Disabling adaptation also disables automatic trial decisions.
See the [feedback contract](ARCHITECTURE.md#feedback-loop) and
[retained scoring rules](history/architecture-2026-10-08.md#source-quality-scoring).

New ready editions use schema 2; the reader also accepts immutable schema-1 editions.
Claims and receipts remain schema 1. A known short-prefix collision in an unstarted
schema-1 edition blocks claim/send, while confirmed/applied history remains readable
and is never resent. After any full-token publication, both the sender and feedback
collector must stay on a compatible-reader engine. An old poller can discard full
tokens as malformed while advancing its cursor. Roll back using a compatible-reader
build or backport, not an arbitrary previous pin; see [ADR0024](decisions/0024-full-article-vote-identity.md#deployment-and-rollback-floor).
An allocation change does not guarantee article selection.

Source proposals use Add/Reject links followed by Start, or `/source ok HASH` and
`/source no HASH`. A private owner decision must match the exact saved proposal,
URL hash and age of 0–30 days during collection and application. A batch receipt means
**decisions saved**, not sources added. Approved sources are applied before collection,
even if later work is empty or fails. Configuration/backup failures retain the decision
for retry; unbound historical decisions cannot authorize replacement proposals.
[#48](https://github.com/Lenivvenil/digest/issues/48) tracks operational acceptance.

## Source discovery

Discovery is separate from Irritator. `python -m digest --discover` proposes feeds,
validates URLs, saves candidates and requests approval through Telegram when configured.
Its exploration areas are independent of the active professional source portfolio:

```yaml
discovery:
  exploration_areas:
    - fintech/banking/architecture
    - science
    - society/institutions
    - history/culture
    - environment
    - design
```

These are provisional defaults, not historical preferences or a required proportion.
Use 1–16 distinct nonempty names, up to 80 characters each. Each pass prefers the
least recently offered area, breaking ties in configured order. Empty/failed attempts
advance the pass without counting as offers. Requested areas are not verified
classifications or proof of novelty. Professional refresh stays eligible; cross-field
requests need no contrived professional connection.

One logical generation permits at most two configured routes, three feed checks/offers
and a 2,048-token output limit. Invalid pending proposals consume the current check
then skip one later eligible preparation before retry; managed retries do not consume
that skip. Confirmed messages, uncertain possible sends, reservations and explicit
API rejections remain distinct. Follow the [discovery state contract](ARCHITECTURE.md#trial-source-lifecycle-and-discovery)
and [ADR-0013](decisions/0013-discovery-exploration-state.md) for persistence and cooldowns.

Approval adds a priority-3 trial source; configuration and lifecycle state are separate
under [ADR-0003](decisions/0003-source-state-split.md). Discovery does not change engine
source files or install a weekly schedule. The proposal-generation slice of
[#132](https://github.com/Lenivvenil/digest/issues/132) leaves active-feed selection and
daily candidate scheduling unchanged; `adaptive.trial_slots` alone does not protect
important professional work after admission. Delivered recommendations still need
editorial evaluation.

## Legacy format and optional features

The category-summary path supports `radar.summary_style` values `analytical`, `brief`
and `detailed`. Set `radar.perspectives: true` to request Optimist, Skeptic and Realist
views of significant topics; brief mode omits them. Views should supply different
reasoning, with short comments for minor items and cross-category trends for related
developments. The [retained illustrative format](history/readme-2026-10-08.md#digest-format-and-perspectives)
is not a factual news item or benchmark. Markdown includes Obsidian front matter.
Ordinary review-led preparation instead selects attributed cards from RSS evidence.
Neither format establishes that the complete source was read.

An optional humane closing item is implemented but disabled by default. It uses an
eligible, explicitly bound source and the existing review packet without another
selection call or reducing the main-card cap. Activating this optional feature requires
approved closing-source bindings and attribution review; its finite editorial/capacity
acceptance remains open. Follow [ADR-0014](decisions/0014-optional-humane-closing-item.md)
for exact settings, main-versus-closing translation/credit behavior and recovery.
Sparse supply is not a promise of a daily positive story.

### Experimental source reading

`reading_brief` is off by default and requires English canonical text, review-led
selection and an explicit model route. It runs only through `--prepare-edition`;
unsupported preview/direct-publish modes stop before source/model work. Completed
source pages are a technical evidence handoff, not an accepted or published edition.
The former standalone reading-angle renderer and delivered-marking Python helpers
have been retired; they were not a supported CLI path. Existing source snapshots and
historical delivered records remain readable. See the
[internal API compatibility note](../CHANGELOG.md#unreleased--reliability-rehabilitation).
Reconciliation builds one validated immutable input from the current source and
completed page state. Offline planning consumes that input; the operation owns saved
request, admission and completion proof, and parsing validates sparse response content.
The unused `digest.reading_points` grouped-point prototype is retired with immutable
historical links in [ADR0010](decisions/0010-group-source-points-with-qualifications.md#retire-the-unused-grouped-point-prototype--2026-10-09).
Direct Python callers must follow the [ADR0009 migration](decisions/0009-selected-source-admission.md#one-immutable-reconciliation-authority--2026-10-09).
Unknown generation outcomes remain held across invocations and route changes.
The [accounting guide](reading-brief-accounting.md) describes verified profiles,
optional offline tokenizer preparation and the bounded configured fallback. Unknown
profiles remain technical pending; advertised context does not establish free quota.
Proposed [ADR0009](decisions/0009-selected-source-admission.md) retains the open
factual-quality, reconciliation and throughput gates. The separate
[closed, unmerged PR #93](https://github.com/Lenivvenil/digest/pull/93) is not on main.

## Upgrades and retained state

Use the package version, immutable commit and [changelog](../CHANGELOG.md) together.
Keep the previous engine pin and compatible runtime configuration for rollback.
The runtime owns its [cache and archived evidence](ARCHITECTURE.md#cache-architecture);
no database or always-on service is required.

Before rollback, verify that the chosen engine and configuration can interpret retained
state. Preserve receipts, deduplication/feedback records, translation records and issue
reservations. Hold publication if compatibility or an earlier delivery is uncertain.
Do not reset state or resend an edition merely because the engine was rolled back.
Optional supplementary failures must remain visible without erasing primary receipts.

## Where to look in code

| Question | Entry point or contract |
| --- | --- |
| Which scenario runs? | `main.main`, `application/execution.py` |
| What is recovered before collection? | `application/preparation.py`, `edition_runtime.recover_preparation` |
| Which primary/fallback result is usable? | `application/review.py:run_primary_review`, `domain/editorial/attempts.py:resolve_review`; saved records enter through `restore_review` |
| What does accepted storage validate? | `preparation.persist_accepted_preparation` saves and reads back canonical work; `load_accepted_preparation` verifies the existing codec. |
| Who completes category preparation? | `application.preparation._accept_category_preparation` owns historical save/empty decisions; the coordinator orders effects; see the [category contract](ARCHITECTURE.md#preparing-an-edition). |
| What does presentation preserve or hold? | `edition_runtime.present_preparation` verifies the supplied path/body hash/full snapshot against the current `.cache` checkpoint before any presentation effect. |
| What can claim/send/inspect do? | `application/prepared_delivery.py` |
| Which bytes and references are checked? | `adapters/storage/edition.py`, `domain/delivery/edition.py` |
| What does confirmed coverage change? | `application/prepared_delivery.py:send_prepared_edition`, from exact persisted receipts through ordered accounting and verified applied marking |

## Decisions and prior operational records

[ADR0007](decisions/0007-compact-issue-reservation.md) owns the prepared publication
boundary; [ADR0008](decisions/0008-candidate-selection-progress.md) owns candidate
continuation. The [historical review/operations record](history/review-operations-2026-10-08.md)
preserves earlier experiment settings, diagnostics, measurements and release evidence.
Optional full-source reading remains outside the ordinary path; its separate
[accounting guide](reading-brief-accounting.md) records that experimental contract.

<details>
<summary>Links to prior sections</summary>

<a id="blind-evidence-review-opt-in-experiment"></a>

- [Blind evidence review (opt-in experiment)](history/review-operations-2026-10-08.md#blind-evidence-review-opt-in-experiment)

<a id="configured-interests-and-reason-fidelity"></a>

- [Configured interests and reason fidelity](history/review-operations-2026-10-08.md#configured-interests-and-reason-fidelity)

<a id="output-and-integration"></a>

- [Output and integration](history/review-operations-2026-10-08.md#output-and-integration)

<a id="example-configuration"></a>

- [Example configuration](history/review-operations-2026-10-08.md#example-configuration)

<a id="offline-verification"></a>

- [Offline verification](history/review-operations-2026-10-08.md#offline-verification)

<a id="explicit-limitations"></a>

- [Explicit limitations](history/review-operations-2026-10-08.md#explicit-limitations)

<a id="rejected-response-diagnostics"></a>

- [Rejected-response diagnostics](history/review-operations-2026-10-08.md#rejected-response-diagnostics)

<a id="partial-selection-recovery-and-narrow-typography-repair"></a>

- [Partial selection recovery and narrow typography repair](history/review-operations-2026-10-08.md#partial-selection-recovery-and-narrow-typography-repair)

<a id="literal-quote-failure-diagnostics"></a>

- [Literal-quote failure diagnostics](history/review-operations-2026-10-08.md#literal-quote-failure-diagnostics)

<a id="source-and-ranking-outcomes"></a>

- [Source and ranking outcomes](history/review-operations-2026-10-08.md#source-and-ranking-outcomes)

<a id="verified-protocol-references-2026-10-02"></a>

- [Verified protocol references (2026-10-02)](history/review-operations-2026-10-08.md#verified-protocol-references-2026-10-02)

<a id="exact-quote-selection-in-bounded-counter-evidence-ranking"></a>

- [Exact quote selection in bounded counter-evidence ranking](history/review-operations-2026-10-08.md#exact-quote-selection-in-bounded-counter-evidence-ranking)

<a id="grounded-irritator-targets-and-complete-abstract-evidence"></a>

- [Grounded Irritator targets and complete abstract evidence](history/review-operations-2026-10-08.md#grounded-irritator-targets-and-complete-abstract-evidence)

<a id="private-ranking-audit"></a>

- [Private ranking audit](history/review-operations-2026-10-08.md#private-ranking-audit)

<a id="deployed-groq-gpt-oss-metadata-review-output-controls"></a>

- [Deployed Groq GPT-OSS metadata-review output controls](history/review-operations-2026-10-08.md#deployed-groq-gpt-oss-metadata-review-output-controls)

<a id="deployed-editorial-context-and-quantitative-qualifier-correction"></a>

- [Deployed editorial context and quantitative-qualifier correction](history/review-operations-2026-10-08.md#deployed-editorial-context-and-quantitative-qualifier-correction)

</details>
