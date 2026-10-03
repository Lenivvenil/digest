# Bounded Context: Digest

_Discovered: 2026-04-26. Historical model updated: 2026-04-28. Decision reconciliation: 2026-10-01, issue #55._

## Decision and evidence register — 2026-10-01

This is the existing canonical Digest domain page. The 2026-10-01 register below
preserves the earlier decision history; current status was reconciled on 2026-10-02.
The owner now prioritizes a simple daily digest within GitHub Actions and free-provider
limits. The operating allocation is a reviewed proposal; the daily schedule is
deployed. Compact rendering is an explicit engine opt-in with a required durable
publication boundary; runtime activation and observed-output acceptance remain separate. [#91](https://github.com/Lenivvenil/digest/issues/91) remains the
single work-order/acceptance tracker. [#55](https://github.com/Lenivvenil/digest/issues/55)
is under active scope reassessment for the simple daily edition; its factual release
gate remains unmet. The latest owner direction permits no parked/deferred tasks:
complete each requirement or record an explicit, justified disposition. English/configurable
translation and source-contract repairs are deployed; ordinary-run acceptance remains
open in #94/#77. The real message-vote path is verified through persistence and computed
priority influence; ordinary collector acceptance remains open in #48.

### Product intent comes before the latest implementation

The [README](../../../README.md) remains the product entry point. Its dated setup
examples are not a reason to discard its product concept:

- **Radar and useful reading:** a personal, category-aware analytical news digest
  for a technology architect, delivered to Telegram and retained in Obsidian.
  [Analytical/detailed formats](../../../README.md#формат-дайджеста) include genuinely
  different Optimist, Skeptic and Realist reasoning for significant topics; the
  brief style deliberately omits those perspectives. Important cross-cutting
  trends are part of the analytical intent, not interchangeable vendor headlines.
- **A learning information diet:** [article votes](../../../README.md#обратная-связь-и-адаптивная-система)
  change source allocation, and [source discovery](../../../README.md#автоматическое-обнаружение-источников)
  uses explicit operator approval and trials. Visible buttons without a working
  influence path violate this concept; they do not redefine it.
- **Bubble breaking:** the later [Irritator domain](../irritator/overview.md#purpose)
  extends this concept with external material that challenges, contradicts or
  complicates dominant narratives. This purpose is documented there and in #53;
  it is not claimed to appear in the older README's architecture diagram.
- **Operational setting:** GitHub Actions, a minimal deterministic Python layer,
  multiple LLM providers, Telegram and a Markdown archive. A recent recovery
  shortcut must not silently turn this product into a generic RSS card sender.

The October primary-first recovery changes execution order and failure isolation.
It does **not** establish that category analysis, trends, meaningful perspectives,
feedback adaptation or counter-signals have ceased to matter. Where the review-led
path omits an existing capability, record the gap and obtain a deliberate product
resolution; do not rewrite the README to legitimize the omission.

### How to read authority and status

- **Owner requirement**: an explicit operator direction. The current October
  directions below supersede conflicting *implementation proposals*, not history.
- **Historical decision/plan**: a recorded rationale, with its original scope and
  status. A merged implementation is not evidence that its output met the product goal.
- **Implementation observation**: behavior verified in the linked commit. It is
  neither a new requirement nor permission to preserve a defect.
- **Open/rejected proposal**: must not become an invariant, acceptance criterion,
  or deployed behavior without the missing decision.

This distinction implements [Principles §2–4](../../principles.md), especially
operator ownership of the domain and knowledge recoverable from the repository.

### Current owner requirements and acceptance traces

The October owner directions are recorded here as requirements, not as an
endorsement of any particular queue, chunking method, model, or card quota.
Their issue-level review surface is #55 and its linked work order #91.

| ID | Owner requirement | Source and rationale | Acceptance / current state |
|---|---|---|---|
| D-01 | Remain a zero-incremental-spend experiment; do not depend exclusively on one model vendor | October owner direction; original [project plan](../../plans/completed/digest.md#plan-daily-news-digest) already requires free hosting/no VPS/no payment | Provider availability, rate quota, context size and output allowance are separate operational constraints. No paid fallback is authorized by this requirement. Independent blind review is an opt-in comparison, not a mandatory daily-release gate or proof of factual correctness; its observed status must stay explicit (#91). |
| D-02 | A Russian digest must add a specific non-obvious insight, with evidence, limitations and a reason to read the original | [#55](https://github.com/Lenivvenil/digest/issues/55); historical [PR73](https://github.com/Lenivvenil/digest/pull/73) already targeted information gain rather than headline repetition | The [three-source sample](https://github.com/Lenivvenil/digest/issues/55#issuecomment-5922127576) received only provisional positive feedback, [recorded here](https://github.com/Lenivvenil/digest/issues/55#issuecomment-5922207102). It is not a final gold standard, a fixed template, or a three-card quota. |
| D-03 | Full source material must support the analysis; provider limits must not silently turn into editorial rejection of longer articles | October owner rejection of the proposed max-three/shared-input-budget policy, tracked under #55 | No article may be labelled uninteresting merely because it is long or a quota is exhausted. Pending/inaccessible/technically incomplete evidence must be distinguished from a completed editorial rejection. The mechanism is not yet decided or implemented. |
| D-04 | Preserve the Irritator's genuine external counter-signal function while primary delivery remains independent of optional-stage failure | Owner-approved primary-first recovery; [PR89](https://github.com/Lenivvenil/digest/pull/89); historical [#53](https://github.com/Lenivvenil/digest/issues/53) defines the unmet core value | Search, provenance, ranking and honest incomplete status must survive editorial changes. A successful supplementary API send does not prove a useful contradiction was found. Source repairs remain #77. |
| D-05 | Telegram is the required primary destination; archived evidence and delivery state must remain truthful and replay-safe | Owner-approved recovery, [PR88](https://github.com/Lenivvenil/digest/pull/88), [PR89](https://github.com/Lenivvenil/digest/pull/89), [PR90](https://github.com/Lenivvenil/digest/pull/90) | Only confirmed card delivery consumes its dedup identity. Footer-only output, an archived report or a green workflow is not proof of a useful delivered digest. |
| D-06 | Changes must preserve accumulated domain analysis and follow one visible issue at a time | Current owner direction; [Principles §4 and Definition of Done](../../principles.md); [#91](https://github.com/Lenivvenil/digest/issues/91) | Link requirement/source → decision/rationale → issue acceptance → change → verification before resuming implementation. Existing documents and issues are updated, not replaced by parallel sources of truth. |

### Operating envelope and daily-edition decision — 2026-10-02

**Owner requirement:** first deliver a simple daily digest and fit the existing
GitHub Actions limits. This narrows the immediate release scope, without deleting
Radar, the learning loop or Irritator from the product concept. Translation remains
an explicit presentation setting. No additional infrastructure, paid provider,
credential or background receiver is introduced.

**Reviewed operating allocation:** one daily cycle at `17 2 * * *` (02:17 UTC),
retaining the existing primary/optional job ceilings of 8/12 minutes. The runtime
schedule reduction was applied separately on 2026-10-02; compact publication requires
the managed reservation and explicit presentation-mode configuration. This is
an intended start time, not a delivery-time guarantee. GitHub may delay or drop
scheduled work. Keep the separately configured weekly discovery budget visible;
do not hide it inside the daily estimate. No additional feedback-only cron is proposed.

| Monthly project allocation (31-day conservative month) | Standard Linux minutes |
|---|---:|
| One daily primary + optional cycle: 31 × (8 + 12) | 620 |
| Up to five existing weekly discoveries: 5 × 10 | 50 |
| Maintenance, private CI, manually admitted verification/retries | 100 |
| Contingency reserve | 230 |
| Total proposed project envelope | **1,000** |

The deployed schedule at the 2026-10-02 audit snapshot had two daily cycles,
allocating up to 1,240 minutes before
weekly discovery or maintenance. The 1,000-minute proposal is an operating allocation,
not an enforced account billing limit. Timeouts, per-job rounding and cancellation
cleanup need reserve; estimates must not be presented as an exact invoice.

The October 1–2 audit covered all 94 digest-family workflow runs and all job attempts.
Per-job timestamps imply approximately 63 private Linux minutes, conservatively 64
with timestamp-resolution headroom: 17 normal production, 7 requested rerun,
37 diagnostic/trial/smoke and 2 private fork CI minutes. Public engine CI accounted
for 61 further standard-runner minutes, which do not consume the private allowance.
Maintenance accounting includes **all** discretionary work: 37 diagnostic/trial/smoke
+ 7 requested rerun + 2 private fork CI = **46 of the 100 allocated minutes already
used** (47 conservatively after assigning one minute of timestamp uncertainty),
leaving 54 estimated or 53 conservative minutes. This is the remaining part of the
original monthly 100-minute allocation, not a new allocation. A possible finite validation package of 12 + 8 correction + 20
integrated minutes would consume at most 40, leaving 13–14; this is an admission
ceiling proposal, not authorization to execute or retry it.

This is measured project use, not account remaining quota. Actual account plan,
other private usage, Packages usage and monetary stop settings are **unknown**.

The [official billing contract](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
shares private usage across the owner's account; public standard runners are free.
The published Free-plan 2,000 minutes are a planning reference, not proof of this
account's entitlement or remaining balance. Each job is
[rounded up independently](https://docs.github.com/en/billing/reference/actions-runner-pricing).
No paid overage or automatic increase is authorized. Before additional private tests,
record their maximum job/request cost and preserve the ordinary-product reserve.
Prefer local checks and public engine CI for implementation verification.

A **daily edition** is one coherent list of selected articles with useful short text,
source links and stable per-article vote attribution. Telegram transport chunks may
be necessary; they must preserve complete selected content and URL/identity bindings.
No artificial first-N article rule or length-based editorial rejection follows from
this format decision. Put translation/verification notices once per edition rather
than repeating them on every article. The proposed first compact release retains
optional Irritator/comparison processing and honest results in the archive, without
additional supplementary Telegram pushes. This presentation change requires code;
it is not available merely by changing the cron or an existing configuration flag.

**Risk/acceptance boundaries:** daily polling has no safety margin against Telegram's
at-most-24-hour ordinary-message retention. Delayed or missed runs can lose votes;
[ADR0006](../../decisions/0006-batch-message-voting.md) records this limitation. Unknown
Telegram sends must not be retried blindly or reported as confirmed. A chunk-spanning
article is delivered only after all its covering chunks are accepted. A green workflow,
an archive or a computed priority change does not prove full article fidelity or a
changed real selection. #55 requires an explicit current-scope decision and verification;
it is neither deferred nor declared complete by this operating record.

**Observed scheduler boundary — 2026-10-03:** all 60 September scheduled daily
runs were found; median creation delay was 4h49m against same-day 02:00/13:00 slots
paired chronologically. REST records omit the triggering cron expression, so this
is a nominal-slot comparison. The daily workflow was unchanged from April 24 to
September 30: delays predate recovery changes and vary rather than fitting a timezone offset.
On October 3, the 02:17 run was created at 08:07:38 UTC; its job started two seconds
later. The measured delay precedes execution; GitHub's internal cause remains unknown.
GitHub's [August incident report](https://github.blog/news-insights/company-news/github-availability-report-august-2026/)
describes event/database saturation, but does not establish this continuing cause.
The [schedule contract](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
allows delayed/dropped runs; minute 17 is no guarantee. The late run persisted
feedback before acknowledgement and blocked a duplicate confirmed edition. These
safeguards worked; they do not eliminate the 24-hour feedback retention risk.

Current model control flow admits a conservative ceiling of ten physical requests
per product cycle before cache reuse, timeout and failure short-circuiting: up to two
primary/fallback reviews, one primary translation, three Irritator phases, one optional
translation and three independent-review slots. Runtime retries are zero. Review
outputs are capped at 4,096 tokens; Irritator/translation outputs at 2,048. The current
20 × 500-character RSS evidence allocation plus metadata is not full-article coverage.
Primary pacing is configured separately from optional stages' 65-second pacing.
These are execution constraints, not an editorial rule to prefer shorter articles.

Provider quotas are a separate ledger: account entitlement, RPM/TPM, daily tokens,
context and output limits are not interchangeable with Actions minutes. Current
routes/retry ceilings remain unchanged; quota exhaustion produces explicit incomplete
status, never a paid fallback. Git archives and translation records still need a
prospective growth/retention policy. Preserve existing history while agreeing it.

### Historical decisions that must not be rediscovered

| Record | What it established | Scope, implementation and remaining evidence gap |
|---|---|---|
| [Original completed plan](../../plans/completed/digest.md), Tasks 1–3 | Free GitHub Actions, minimal Python dependencies, multiple provider adapters; RSS descriptions capped at 500 characters | This is the original implementation baseline. It did not establish full-article analysis or a permanent editorial rule to prefer short material. Its old model names and quota numbers are historical, not current provider documentation. |
| [March 21 truncation fix](https://github.com/Lenivvenil/digest/commit/764b771d026e9a7e4407f148644e89e25d2fe39d) | Replaced silent use of Gemini MAX_TOKENS output with an explicit truncation failure | Token constraints and incomplete-output handling were known before this rehabilitation. Raising a context/output setting is not evidence that throughput quotas permit the request. |
| [#63](https://github.com/Lenivvenil/digest/issues/63) (April 28) | Parallel Lobsters fan-out produced 429s and some 400s; spacing/backoff was a known investigation path | An observed rate failure, not approval to weaken the editorial goal or a verified adapter fix. #77 retains the unresolved contract work. |
| [#84](https://github.com/Lenivvenil/digest/issues/84), [PR85](https://github.com/Lenivvenil/digest/pull/85) (June 9) | DeepSeek 402, Gemini 429/503 and partial Groq failures caused a cascade; raw-feed fallback produced 61 messages | The raw-card fallback was removed. Free-provider failure was a known system constraint, not a newly discovered reason to send weaker content. |
| [PR73](https://github.com/Lenivvenil/digest/pull/73), [plan §3–6](../../../plan.md#3-considered-approaches) (April 28) | Chose prompt tightening plus a mechanical two-sentence cap. Deferred cross-category second-pass context because of latency/token cost, explicitly to revisit if tightening was insufficient. Pre-clustering was also deferred | Implemented in [1cf3e545](https://github.com/Lenivvenil/digest/commit/1cf3e545a4a35d06c5ced437d69827633f1cf878). The plan explicitly leaves information gain to empirical evaluation on a real digest; prompt-string and sentence-cap tests do not prove it. Repeating that patch is not a new solution to #55. |
| [March 26 card change](https://github.com/Lenivvenil/digest/commit/0d70a474a517e8908ab224348d1105f6b295b652), [April 16 usability change](https://github.com/Lenivvenil/digest/commit/9b747ae4338a0d6bde16da5c5659e1d0daf4e2b7) | One article per Telegram post with its own meaningful summary and vote attribution; effective source priorities wired into collection | The aim was readable, actionable intake and a closed feedback loop, not merely successful message transport. Perspectives were deliberately disabled in that runtime change; the README capability is not a mandate to restore them on every card. |
| [April 13 recovery](https://github.com/Lenivvenil/digest/commit/896a9d869ed4182153918eb8195ae182a003a78a) | Restored v1 features lost in the v2 rewrite: twice-daily schedule, discovery, adaptive feedback/scoring, provider/routing support and observability | Losing accumulated capabilities during a structural rewrite is a documented prior failure mode. The next design must map retained capabilities before changing orchestration. |
| [#39](https://github.com/Lenivvenil/digest/issues/39) (April 26) | Article lifecycle/replay was missing; action was deferred until replay, filtered-item traceability or cross-run analysis was needed | A parked architectural debt, not an already accepted durable article queue. No such accepted queue design was found in the reviewed public ADRs, plans or issue bodies. |
| [#66](https://github.com/Lenivvenil/digest/issues/66), [PR67](https://github.com/Lenivvenil/digest/pull/67) | Accepted delayed callback collection with an explicit “next run” note and no new infrastructure | This did not authorize loss of feedback. Current ingestion/selection regression is #48. Immediate acknowledgement and recorded-vote influence are different properties. |
| [#51](https://github.com/Lenivvenil/digest/issues/51), [#52](https://github.com/Lenivvenil/digest/issues/52) | Topic/narrative adaptation and adjacent-topic exploration are intended extensions | Open proposals; #51 explicitly requires an ADR. They are not current implemented personalization and must not be silently folded into #55. |
| [ADR0002](../../decisions/0002-engine-instance-split.md), [PR35](https://github.com/Lenivvenil/digest/pull/35); [ADR0003](../../decisions/0003-source-state-split.md), [PR41](https://github.com/Lenivvenil/digest/pull/41) | Engine/runtime separation and static configuration versus mutable source state | Implementations landed, but both ADR headers still say **proposed**. This register does not retroactively declare them accepted. Formal-status reconciliation remains explicit. |

### Dated constraint evidence — do not substitute an assumed quota

Audit date: **2026-10-01**. Scope: public engine README, domain pages, three ADRs,
completed plans, the retained April #55 plan, issue bodies and relevant commits.
This is an evidence inventory, not a current pricing/limits specification.

| Constraint | Dated evidence and what is actually established | Verification status |
|---|---|---|
| GitHub Actions free execution | The [original plan](../../plans/completed/digest.md) and [architecture limits](https://github.com/Lenivvenil/digest/blob/1cd1a97cb83574fdeb39d7be3c4680c30f35e4e3/docs/ARCHITECTURE.md#ограничения-и-известные-особенности) use **2,000 minutes/month** and estimate **two ~3-minute runs/day** | Historical planning assumption. Current account entitlement, actual runtime and remaining allowance are not established by that text. The original zero-cost intent is established. |
| Gemini output truncation | [2026-03-21 commit](https://github.com/Lenivvenil/digest/commit/1e3fc4486bcb0a7ea4447623ab381e4bb2b7f51f) changed client `maxOutputTokens` **8,192 → 65,536**; the [later same-day fix](https://github.com/Lenivvenil/digest/commit/764b771d026e9a7e4407f148644e89e25d2fe39d) stopped silently accepting partial output | These are configured output ceilings and an observed truncation class, **not** verified requests/minute, tokens/minute, daily allowance, or available context. |
| Provider availability/credit failure | [#84](https://github.com/Lenivvenil/digest/issues/84), **2026-06-09**: DeepSeek **402**, Gemini **429/503**, partial Groq failure; **61** raw cards escaped through the fallback | The failure cascade is evidenced. Its HTTP statuses do not reveal an exact current provider/account/model quota. |
| Search-service throughput | [#63](https://github.com/Lenivvenil/digest/issues/63), **2026-04-28**: up to **15 parallel** Lobsters queries, mostly **429**, some **400** | A known external-service constraint. Exact supported endpoint and safe request cadence remained investigation items; neither an inferred cause nor a configured semaphore proves the fix. |
| Current Groq public free-plan limits | [Official rate-limit table](https://console.groq.com/docs/rate-limits), checked **2026-10-01**, lists `openai/gpt-oss-120b`: **30 RPM, 1,000 RPD, 8,000 TPM, 200,000 TPD** | Current provider documentation, not a recovered historical project decision or verified account allowance. Limits apply at organization level and exceptions may exist. This does not justify an editorial article-count or length cap. |
| Current Groq model capacity | [Official models table](https://console.groq.com/docs/models), checked **2026-10-01**, lists `openai/gpt-oss-120b` context **131,072 tokens** and maximum completion **65,536 tokens** | Model capacity, not free-tier throughput or a billing entitlement. The adjacent throughput column is explicitly for the Developer plan and must not be substituted for the free-plan table above. |
| Recent editorial packet limits | **Three articles / 4.5K estimated input tokens** belonged to the unshipped October implementation proposal | **Rejected by the owner.** These are not provider guarantees, recovered historical decisions, or product acceptance criteria. |

The historical project artifacts reviewed did not contain that dated Groq quota
record or a ready-made durable article-analysis queue. That is a bounded audit finding, not a claim that no
such discussion ever occurred. Future records should name provider, model, account
tier, limit dimension, observation date and primary source; never infer TPM from a
large context window or infer editorial irrelevance from rate exhaustion.

### Current implementation versus intended product

Observation baseline: engine commit
[`a1beb435`](https://github.com/Lenivvenil/digest/tree/a1beb435c47f988096469e4b7337c77e0733819c).

- PR88–PR90 introduced evidence/review contracts and persisted follow-up state,
  but no corresponding new accepted ADR is present in the reviewed public decision
  directory. This is a governance/traceability gap under the existing ADR triggers,
  not permission to label those contracts architecturally accepted retroactively.
- The review-led path supplies bounded RSS evidence, not full articles. Literal
  citation validation and per-item salvage establish provenance only. See
  [review.py](../../../digest/review.py) and [review protocol](../../BLIND_REVIEW.md).
- Primary delivery precedes separately persisted, bounded Irritator and missing-slot
  review work. These checkpoints are implemented; they are not an all-article
  acquisition/analysis queue.
- The optional legacy category path still contains three-perspective prose. That
  format, an external Irritator counter-signal, and an independent second model
  opinion are three different capabilities; none is proof of either of the others.
- #48 separates vote ingestion/durability from automatic adaptation and delivery.
  A managed runtime must push the exact feedback batch before acknowledgement; the
  normal digest then skips collection. Vote-only bounded source priorities influence
  candidate allocation without exposing raw votes to models. Operational
  acceptance still requires real authorized callbacks; synthetic tests are not that proof.
- The collector uses MD5 of `title|link` for article identity and source-configured
  recency windows. Historical statements below describing SHA-256 of description,
  a universal 48-hour window, or priority range 1–10 are not current code invariants.

### Rejected proposals and decisions still open

**Rejected during #55:** a permanent maximum of three analyzed articles; fitting
only short articles into one shared 4.5K estimated-token packet as an editorial
selection policy; calling truncated windows a full read. These were implementation
proposals, not requirements. The isolated experimental patch was not deployed.

**Not yet decided:** how complete-source analysis, provider-specific quotas,
continuation, evidence retention and cross-article comparison should work together.
A durable queue/chunking proposal is not an accepted architecture merely because it
was suggested. Any new persistent state or cross-context contract must satisfy the
existing [ADR triggers](../../principles.md#что-значит-архитектурно-значимо-триггер-для-adr).

The older “full-text storage outside scope” boundary below concerned the original
RSS/hash implementation. Reading original material and retaining replayable evidence
are separate decisions. The current quality requirement must be reconciled with that
boundary explicitly; do not silently delete the boundary or infer a retention policy.

### Verification gate for the next #55 change

1. Record the selected design and rationale against D-01–D-06, including how it
   preserves long-material eligibility and distinguishes unfinished work from rejection.
2. Map each acceptance criterion to deterministic tests **and**, where needed, a
   real-output observation. Reuse existing delivery, provenance and resume regressions.
3. Evaluate a report-only real-source output for information gain, Russian clarity,
   factual support, non-repetition and limitations. Include candidate coverage and
   unresolved work. A fully mocked model response is not this evidence.
4. Record owner/editorial review and remaining gaps before automatic-card rollout.
   Keep useful delivery, semantic quality and independent-review success separate.

## Historical domain snapshot — April 2026

The following material records the earlier interview/code-derived model. It remains
for traceability, not as an override of the reconciliation above. Its lifecycle,
provider behavior and delivery order must not be treated as current verified facts.


---

## Purpose

The Digest BC coordinates the full lifecycle of one operator's daily information intake: collecting articles from configured RSS sources, summarising them per category via LLM, and delivering the result to the operator via Telegram and Obsidian. Counter-signal discovery is delegated to the **Irritator BC** (a separate bounded context); the Digest BC invokes it as an upstream–downstream dependency and delivers its output alongside the main digest.

**Explicitly outside scope:** full-text storage of raw articles (RSS providers own the content; BC stores only the hash for deduplication); user authentication/authorisation (single-owner system); push notifications policy (Telegram's concern); full-text search across digest history (Obsidian's concern); multi-user or multi-tenant scenarios.

---

## Actors

| Actor | Type | Role |
|-------|------|------|
| User (Operator) | Human | Configures sources via `config.yaml`; reads digest; votes 👍/👎 on articles; approves/rejects discovered sources; requests `/bubble` and `/status` reports |
| RSS Feed Providers | External system | Publishes Atom/RSS feeds consumed by Radar |
| GitHub Actions | Scheduler | Triggers daily pipeline (`python -m digest`) and weekly source discovery (`--discover`) |
| Telegram Bot API | External system | Delivers digest article cards and counter-signal posts; receives vote/approval callbacks; responds to bot commands |
| Obsidian Vault | External system | Consumes generated `.md` files per agreed front-matter format |

---

## Events (Event Storming)

**Orange — what happened (with the owning aggregate):**

| Event | Aggregate owner | Module |
|-------|----------------|--------|
| FeedFetched | — (process event) | Radar.collector |
| ArticleIngested | Article | Radar.collector |
| ArticleFiltered | Article | Radar.collector |
| SummaryGenerated | — (process event) | Radar.summarizer |
| NarrativeExtracted | — (transient, not persisted) | Irritator |
| SignalFound | — (transient, not persisted) | Irritator |
| DigestDelivered | — (process event) | Delivery |
| FeedbackReceived | Source¹ | Delivery.telegram |
| SourcePriorityUpdated | Source | Radar.collector (next run) |
| TrialStarted | Source | Radar.collector |
| TrialGraduated | Source | Radar.collector |
| TrialDemoted | Source | Radar.collector |
| SourceDiscovered | PendingSource | Discovery |
| SourceApproved | PendingSource | Discovery |
| SourceRejected | PendingSource | Discovery |
| BubbleReportRequested | — (query, no state change) | Delivery.telegram |
| BubbleReportGenerated | — (query result) | Delivery.telegram |

**Blue — commands:**

| Command | Target aggregate | Module |
|---------|----------------|--------|
| FetchFeeds | Article (creates) | Radar.collector |
| SummarizeCategory | — | Radar.summarizer |
| ExtractNarratives | — | Irritator |
| GenerateQueries | — | Irritator |
| SearchAllSources | — | Irritator.sources |
| DeliverDigest | — | Delivery |
| UpdateSourcePriority | Source | Radar.collector |
| EvaluateTrialSource | Source | Radar.collector |
| DiscoverSources | PendingSource (creates) | Discovery |
| ApproveSource | PendingSource → Source | Discovery |
| RejectSource | PendingSource | Discovery |
| RequestBubbleReport | — (query) | Delivery.telegram |

**Lilac — policies:**

- When `FeedbackReceived` → `UpdateSourcePriority` (14-day decay). Executor: Radar.collector on the next pipeline run. ¹ Ownership is split: Delivery records it, Radar applies it — hot spot #3.
- When a Trial has been active for ≥ `trial_days` days → `EvaluateTrialSource` (graduated/demoted). Executor: Radar.collector.
- When an Article is older than 48 hours → `ArticleFiltered`. Executor: Radar.collector.
- When `SourceApproved` → `AddSourceToConfig` (adds a trial block to `config.yaml`) → `TrialStarted`. Executor: Discovery.
- When delivery fails → roll back `FeedbackStore.last_update_id` and `ratings` to their pre-run state. Executor: main.py.

**Yellow — aggregates:**

- **Source** — owns `user_priority` and `enabled`. Runtime state (`trial_started`, `graduated`, `demoted`) is stored in `SourceStateStore` (`.cache/source_state.json`), not on the aggregate (ADR-0003).
- **Article** — owns `hash` (identity), `relevance_score`, `age`, `source_ref`, and `delivered_flag`.
- **PendingSource** — owns `name`, `url`, `category`, `discovered_at`, and `source_hash`. Lifecycle: `discovered → approved/rejected`.

> **Trial state outside Source (ADR-0003):** `trial_started`, `graduated`, and `demoted` are stored in `SourceStateStore`, allowing `config.yaml` to remain declarative.

**Green — read models:**

- `BubbleReport` — a filter-bubble snapshot: diversity score (Shannon entropy over seven days of article inclusions), top categories by volume, 14-day feedback counters, and trial/graduated/demoted sources. Generated on demand by the `/bubble` command.
- `DigestHistory` — `.md` files in Obsidian (the format conforms to Obsidian's expectations).
- `NanoStatus` — a two-line footer for each digest: source/article counts, LLM provider, promoted/demoted counters, and average source score.

**Red — hotspots:**

1. **Source dual-storage**: `config.yaml` is mutated through two paths: `apply_trial_decisions()` and `add_source_to_config()`. There is no single owner. → Issue #37.
2. **Article lacks an explicit lifecycle**: no `raw → scored → delivered → archived` states. Hash-based deduplication is ad hoc. → Issue #39.
3. **Feedback decay owner**: decay logic is split between Delivery (recording) and Radar (application). → Issue #40.
4. **Radar→Delivery contract drift**: `CategorySummary` is extended with a set of `ArticleSummary` objects without an explicit intra-phase contract. → Issue #29.

---

## Boundary

**In scope:**
- Collecting articles from RSS (Radar)
- Filtering and scoring relevance against the user's context
- Article deduplication by hash
- Narrative generation and counter-signal search (Irritator)
- Telegram delivery and writing to Obsidian (Delivery)
- Adaptive source priorities (trial + feedback)
- Discovering and approving new sources (Discovery)
- Filter-bubble analytics (BubbleReport)

**Deliberately outside scope:**
- Full-text storage of raw articles
- User notification policy (Telegram's concern)
- Full-text search across digest history (Obsidian's concern)
- Authentication and authorization

**Term whose meaning changes at the boundary:**
`Article` — on the RSS side, this is an XML entry without context. After ingestion in Radar.collector, an Article acquires `relevance_score`, `age`, and `source_ref`.

---

## Aggregate Root

**Source**
- Invariants: `priority` ∈ [1..10]; `url` is a valid HTTP/HTTPS URL; `name` is unique among enabled sources; a demoted source does not participate in the pipeline (filtered in `main.py` through `source_state.is_demoted()`).
- Trial runtime state belongs in `SourceStateStore`, not on the aggregate.

**Article**
- Invariants: `hash` = SHA-256 of the first 500 characters of description + title; the same (hash, source) pair is never ingested twice within `CACHE_MAX_AGE_DAYS=7`; `pub_date` older than 48 hours → Article is not ingested.

**PendingSource**
- Invariants: `source_hash` is unique in the pending list; `url` has passed SSRF validation (`_dns_pinning.validate_url`) before entering pending.

---

## Policies

| Trigger | Action | Executor |
|---------|--------|----------|
| `FeedbackReceived` | `UpdateSourcePriority` with 14-day decay | Radar.collector, next run |
| Trial active for ≥ `trial_days` AND `calculate_score > 0.6` | `TrialGraduated` | Radar.collector |
| Trial active for ≥ `trial_days` AND `calculate_score < 0.3` | `TrialDemoted` | Radar.collector |
| Article `pub_date > 48h` | `ArticleFiltered` | Radar.collector |
| `SourceApproved` | Add a trial block to `config.yaml`; emit `TrialStarted` | Discovery |
| `delivery_ok == False` | Roll back `FeedbackStore` to its pre-run state | main.py |

---

## Context Map

| Upstream BC / System | Pattern | Notes |
|----------------------|---------|-------|
| RSS Feed Providers | **Conformist** | We consume Atom/RSS through feedparser and adapt to their format |
| Telegram Bot API | **Conformist** | We use their Published Language (MarkdownV2, inline keyboards, callback queries) |
| HN / Reddit / arXiv / dev.to / Lobsters | **Conformist** | We read public APIs without a contract; API changes can cause breakage |
| Obsidian Vault | **Customer-Supplier** | Obsidian is the downstream customer; the `.md` format conforms to its expectations; Digest is the supplier |

---

## Use Cases

<a id="uc-1-ежедневный-запуск-пайплайна"></a>

### UC-1: Daily pipeline run

**Actor:** GitHub Actions (Scheduler)
**Preconditions:** `config.yaml` exists and is valid; an LLM API key is available; `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set.
**Main scenario:**
1. `collect_feedback()` polls Telegram getUpdates; new votes and approval decisions are recorded in `FeedbackStore`.
2. `calculate_effective_priorities()` recalculates source priorities.
3. `collect()` fetches RSS feeds in parallel; new Articles are deduplicated by hash.
4. `summarize_all()` summarizes each category through an LLM; for ≥1 top topics, it generates three perspectives (Optimist/Skeptic/Realist) if `summary_style != brief`.
5. `run_irritator()` extracts narratives → generates adversarial queries → searches for counter-signals → validates → ranks.
6. `write_digest()` saves `.md` to Obsidian.
7. `send_article_cards()` publishes individual Telegram posts with 👍/👎 buttons.
8. `send_counter_signals()` publishes counter-signals with an IrritatorStatus footer.
9. `evaluate_trial_sources()` makes graduated/demoted decisions.
10. All stores are persisted atomically.

**Alternatives:**
- 3a: All feeds are unavailable → `AllFeedsFailedError` → the pipeline exits with code 1.
- 3b: No new articles → the pipeline exits successfully without delivery.
- 5a: The Irritator pipeline is empty → `IrritatorStatus.level == "empty"` → no counter-signals are published.
- 7a: Telegram delivery failed → retain already saved votes/offset; only undelivered-card attribution is rolled back. Both delivery failures → exit code 1.

**Postconditions:** `feedback.json`, `source_state.json`, `source_stats.json`, and `source_category_map.json` are updated; an `.md` file is created; Telegram messages are sent; the dedup cache is updated.

---

<a id="uc-2-пользователь-голосует-за-статью"></a>

### UC-2: User votes on an article

**Actor:** User (through a Telegram inline button)
**Preconditions:** An article card has been delivered with `fb:a:g:{hash}` / `fb:a:b:{hash}` buttons; `article_source_map` contains `hash → source_name`.
**Main scenario:**
1. The user presses 👍 or 👎.
2. On the next run, `collect_feedback()` receives the callback query.
3. Parse `callback_data = "fb:a:{g|b}:{article_hash}"` → `rating = +1 / -1`.
4. Add `ArticleFeedback(article_hash, source_name, rating, timestamp)` to `FeedbackStore.ratings`.
5. Strictly persist ratings, the handled update offset and minimal pending reply receipts together.
6. In managed Actions, successfully commit/push that exact state before acknowledgement;
   direct CLI use provides local persistence. Verify the expected file hash before responding.
7. Answer the callback best-effort. Expired acknowledgements do not undo the saved vote.

**Alternatives:**
- 3a: `article_source_map` has no entry for `hash` → do not add unattributed source feedback; report that the article cannot be matched.
- 4a: The feedback entry is > 30 days old → prune it at the next `save_feedback()`.

**Postconditions:** `FeedbackStore` contains the new `ArticleFeedback`; on the next run, it affects that source's `effective_priorities`.

---

<a id="uc-3-пользователь-запрашивает-filter-bubble-отчёт"></a>

### UC-3: User requests a filter-bubble report

**Actor:** User (Telegram command `/bubble`)
**Preconditions:** At least one pipeline run has completed; `.cache/source_stats.json`, `.cache/source_state.json`, and `.cache/source_category_map.json` exist.
**Main scenario:**
1. `collect_feedback()` receives a message with `text="/bubble"`.
2. Load `stats`, `state`, and `category_map` from `.cache/`.
3. `compute_bubble_report()` calculates the last digest date, `_diversity_score()` (Shannon entropy), top categories by seven-day inclusions, 14-day feedback, and trial/graduated/demoted counters.
4. Send the report to the same chat.

**Alternatives:**
- 2a: `.cache/` files are missing → `compute_bubble_report()` returns a report with zeros.

**Postconditions:** The user has received a read-only snapshot of their information bubble; system state is unchanged.

---

<a id="uc-4-оператор-одобряетотклоняет-новый-источник"></a>

### UC-4: Operator approves/rejects a new source

**Actor:** User (private owner Telegram message)
**Preconditions:** The discovery phase (`--discover`) has found a new source, saved PendingSource in `.cache/pending_sources.json`, and sent approval links or command instructions.
**Main scenario:**
1. The user presses Add or Reject and Telegram's Start button, or sends `/source ok HASH` / `/source no HASH`.
2. On the next run, `collect_feedback()` parses the ordinary `/start source_{ok|no}_{hash}` or `/source {ok|no} {hash}` message, verifies private owner identity, and rejects replay.
3. Require exactly one current proposal with a matching URL hash and age 0–30 days. Persist `FeedbackStore.source_decisions`, its exact proposal SHA-256 binding, replay receipt and cursor before acknowledging the batch as decisions saved.
4. Before feed collection, `_process_pending_approvals()` repeats the identity, uniqueness and age checks, requiring the same proposal binding.
5. If approved: `add_source_to_config()` idempotently adds a trial block to `config.yaml`; the pipeline reloads config for collection, independently of digest success.
6. If rejected: remove PendingSource from pending.

**Alternatives:**
- 4a: PendingSource is missing, ambiguous, changed, stale or future-dated, or the saved decision has no binding → do not apply it. Legacy unbound decisions cannot authorize a later proposal.
- 5a: Config or backup writing fails → preserve the decision and proposal for retry. State persistence failures do not clear the in-memory decision.
- Legacy `src:{ok|no}:{hash}` callbacks pass the same proposal checks but remain best effort. Ordinary messages have at-most-24-hour Telegram retention; an uncollected decision can still expire upstream.

**Postconditions:** `config.yaml` is updated (approved) or PendingSource is removed (rejected).

---

### UC-5: Trial source evaluation (graduated/demoted)

**Actor:** GitHub Actions (Scheduler, automatically at the end of the pipeline)
**Preconditions:** `config.yaml` contains a source with `trial: true`; `SourceStateStore` contains `trial_started`; ≥ `trial_days` days have elapsed.
**Main scenario:**
1. `evaluate_trial_sources()` reads `SourceStateStore` and `SourceStats`.
2. For each trial source, calculate `calculate_score()` (reliability 30% + productivity 30% + desc_quality 20% + recency 20%).
3. `score > 0.6` → `TrialGraduated`: `SourceStateStore.mark_graduated(name)`.
4. `score < 0.3` → `TrialDemoted`: `SourceStateStore.mark_demoted(name)`; exclude the source from future runs.
5. Save `SourceStateStore`.

**Alternatives:**
- 2a: `score` ∈ [0.3..0.6] → the source stays in trial for another cycle.
- 3a: The source has already graduated/been demoted → skip it.

**Postconditions:** `source_state.json` is updated; the demoted source does not participate in the next pipeline run.

---

## Domain Data Model

### Article

| Attribute | Type | Invariants |
|-----------|------|------------|
| `title` | str | nonempty |
| `link` | str | valid URL |
| `description` | str | max 500 characters after sanitization |
| `source` | str | exists in `config.sources` |
| `category` | str | exists in `config.sources` |
| `pub_date` | `datetime \| None` | if known, no older than 48 hours (otherwise filtered) |

States: `raw` (from feedparser) → `ingested` (passed dedup + age + blocklist) → `summarized` (included in `CategorySummary`). Archival is through the dedup cache's seven-day TTL.

### CategorySummary

| Attribute | Type | Invariants |
|-----------|------|------------|
| `category` | str | |
| `summary_text` | str | passed `_clean_summary()` |
| `article_count` | int | ≥ 1 |
| `article_summaries` | `list[ArticleSummary]` | empty list if per-article summaries were not requested |

### ArticleSummary

| Attribute | Type | Invariants |
|-----------|------|------------|
| `title` | str | |
| `link` | str | |
| `source` | str | |
| `category` | str | |
| `summary` | str | ≤ 2 sentences; reveals a nontrivial/non-obvious aspect (business rule recorded in the UL) |

### Source (aggregate)

Stored declaratively in `config.yaml`. Runtime state is in `SourceStateStore`.

| Attribute | Type | Invariants |
|-----------|------|------------|
| `name` | str | unique among enabled sources |
| `url` | str | HTTP/HTTPS; passed SSRF validation |
| `category` | str | |
| `priority` | int | [1..10] |
| `enabled` | bool | |
| `trial` | bool | |
| `trial_days` | int | > 0 if `trial=true` |

Runtime state (`SourceStateEntry`): `trial_started: str | None`, `graduated: bool`, `demoted: bool`.

### FeedbackStore

Persisted in `.cache/feedback.json`. Prunes ratings > 30 days old; `article_source_map` is capped at 1000 entries.

| Field | Type |
|-------|------|
| `ratings` | `list[ArticleFeedback]` |
| `last_update_id` | int (Telegram update ID cursor) |
| `last_digest_sources` | `list[str]` |
| `last_digest_time` | str (ISO) |
| `source_decisions` | `dict[source_hash, "approved"\|"rejected"]` |
| `article_source_map` | `dict[article_hash_8, source_name]` |

### SourceStats

Persisted in `.cache/source_stats.json`. History is capped at 30 days.

| Field | Type |
|-------|------|
| `name` | str |
| `total_fetches`, `successful_fetches` | int |
| `total_articles_found`, `articles_included_in_digest` | int |
| `avg_description_length` | float (EMA, alpha=0.3) |
| `last_seen` | `str \| None` |
| `history` | `list[DailySnapshot]` (max 30) |

---

### BubbleReport (read model)

Generated on demand by the `/bubble` command. Not persisted: computed from `.cache/` files each time.

| Field | Type | Source |
|-------|------|--------|
| `generated_at` | str (UTC) | `datetime.now()` |
| `last_digest_time` | str | `FeedbackStore.last_digest_time` |
| `diversity_score` | float [0..100] | Shannon entropy over seven days of article inclusions by source |
| `diversity_label` | `"Diverse"\|"Moderate"\|"Concentrated"` | score ≥ 70 → Diverse; ≥ 40 → Moderate; otherwise Concentrated |
| `category_breakdown` | `dict[category, count]` (7d) | `source_category_map` + `SourceStats.history` |
| `feedback_summary` | `(total, positive, negative)` (14d) | `FeedbackStore.ratings` |
| `trial_summary` | `(graduated, trial, demoted)` | `SourceStateStore` |

---

## Interface Contracts

| Interface | Direction | Protocol | Operations | Handled failures | Unhandled failures |
|-----------|-----------|----------|-----------|------------------|--------------------|
| RSS Feed Providers | inbound | HTTP/HTTPS + feedparser | GET feed URL | timeout → source marked error; `feedparser.bozo` + empty entries → warn | SSL errors, XML schema changes |
| Telegram Bot API (delivery) | outbound | HTTPS REST | `sendMessage` (MarkdownV2), `sendMessage` (inline keyboard) | HTTP 4xx → logged warning, non-critical | Bot token revoked |
| Telegram Bot API (feedback) | inbound | HTTPS polling | `getUpdates` + `answerCallbackQuery` + `deleteWebhook` | webhook active → auto-delete before polling; 0 results → info log | Rate limiting, update_id skew |
| Irritator sources (HN/Reddit/arXiv/devto/Lobsters) | outbound | HTTPS REST | search queries (per-adapter) | per-adapter exceptions caught; empty results → continue | API schema changes |
| Obsidian Vault | outbound | filesystem | atomic write `.md` to configured path | write error → `markdown_saved=False` | Obsidian plugin format changes |
| LLM providers (Groq/Gemini/DeepSeek/Anthropic/Mistral) | outbound | HTTPS REST | chat completion | provider exception → logged, category skipped | API key invalid, quota exceeded |

---

## NFR

Mechanically verifiable constraints only:

| Constraint | Enforcement | Artifact |
|------------|-------------|----------|
| HTTP calls go through SSRF guard | `_dns_pinning.validate_url()` rejects private/local ranges | `digest/_dns_pinning.py` |
| HTML/script injection stripped from feed content | `_sanitize.sanitize_article()` | `digest/_sanitize.py` |
| JSON writes are atomic (no partial writes) | `atomic_json_write()` uses tmp + rename | `digest/_util.py` |
| Cache entries cap at 5000 / 7 days | `CACHE_MAX_ENTRIES=5000`, `CACHE_MAX_AGE_DAYS=7` | `radar/collector.py` |
| Feedback ratings pruned at 30 days | `save_feedback()` prune loop | `feedback.py` |
| `article_source_map` capped at 1000 entries | FIFO prune in `save_feedback()` | `feedback.py` |
| ArticleSummary truncated at 2 sentences | `_cap_sentences(text, 2)` | `radar/summarizer.py` |

---

## Internal Compliance

| Norm | Enforcement type | Artifact | Honor-system gap? |
|------|-----------------|----------|-------------------|
| Type hints on all functions | mypy strict | `Makefile typecheck`, CI | No — CI blocks merge |
| No unused imports | ruff F401 | `.pre-commit-config.yaml`, CI | No — pre-commit + CI |
| async/await for all I/O | mypy + code review | — | Yes — no static check for sync I/O calls |
| Dataclasses for data structures | code review | — | Yes |
| No real HTTP in tests | pytest fixture convention | `tests/` mock patterns | Yes — no network isolation in CI |
| Config YAML only | convention | CLAUDE.md | Yes |
| English for log/error messages | code review | CLAUDE.md | Yes |

---

## Red Hotspots

1. **Source dual-storage**: `config.yaml` is mutated through two independent runtime paths. → Issue #37.
2. **Article lacks an explicit lifecycle**: there are no typed states between ingestion and archival. → Issue #39.
3. **Feedback decay owner**: decay logic is distributed between Delivery and Radar. → Issue #40.
4. **Radar→Delivery contract drift**: no explicit intra-phase contract between Radar and Delivery. → Issue #29.
