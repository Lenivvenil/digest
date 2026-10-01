# Bounded Context: Digest

_Discovered: 2026-04-26. Historical model updated: 2026-04-28. Decision reconciliation: 2026-10-01, issue #55._

## Decision and evidence register — 2026-10-01

This is the existing canonical Digest domain page, not a new architecture proposal.
As of 2026-10-01, the work order is [#91](https://github.com/Lenivvenil/digest/issues/91); editorial
quality [#55](https://github.com/Lenivvenil/digest/issues/55) is the sole active item.
Feedback #48 and source contracts #77 remain queued. No code change is approved
by the existence of this register.

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
| D-01 | Remain a zero-incremental-spend experiment; do not depend exclusively on one model vendor | October owner direction; original [project plan](../../plans/completed/digest.md#plan-daily-news-digest) already requires free hosting/no VPS/no payment | Provider availability, rate quota, context size and output allowance are separate operational constraints. No paid fallback is authorized by this requirement. Current blind review is implemented; two valid independent opinions on one real bundle remain unverified (#91). |
| D-02 | A Russian digest must add a specific non-obvious insight, with evidence, limitations and a reason to read the original | [#55](https://github.com/Lenivvenil/digest/issues/55); historical [PR73](https://github.com/Lenivvenil/digest/pull/73) already targeted information gain rather than headline repetition | The [three-source sample](https://github.com/Lenivvenil/digest/issues/55#issuecomment-5922127576) received only provisional positive feedback, [recorded here](https://github.com/Lenivvenil/digest/issues/55#issuecomment-5922207102). It is not a final gold standard, a fixed template, or a three-card quota. |
| D-03 | Full source material must support the analysis; provider limits must not silently turn into editorial rejection of longer articles | October owner rejection of the proposed max-three/shared-input-budget policy, tracked under #55 | No article may be labelled uninteresting merely because it is long or a quota is exhausted. Pending/inaccessible/technically incomplete evidence must be distinguished from a completed editorial rejection. The mechanism is not yet decided or implemented. |
| D-04 | Preserve the Irritator's genuine external counter-signal function while primary delivery remains independent of optional-stage failure | Owner-approved primary-first recovery; [PR89](https://github.com/Lenivvenil/digest/pull/89); historical [#53](https://github.com/Lenivvenil/digest/issues/53) defines the unmet core value | Search, provenance, ranking and honest incomplete status must survive editorial changes. A successful supplementary API send does not prove a useful contradiction was found. Source repairs remain #77. |
| D-05 | Telegram is the required primary destination; archived evidence and delivery state must remain truthful and replay-safe | Owner-approved recovery, [PR88](https://github.com/Lenivvenil/digest/pull/88), [PR89](https://github.com/Lenivvenil/digest/pull/89), [PR90](https://github.com/Lenivvenil/digest/pull/90) | Only confirmed card delivery consumes its dedup identity. Footer-only output, an archived report or a green workflow is not proof of a useful delivered digest. |
| D-06 | Changes must preserve accumulated domain analysis and follow one visible issue at a time | Current owner direction; [Principles §4 and Definition of Done](../../principles.md); [#91](https://github.com/Lenivvenil/digest/issues/91) | Link requirement/source → decision/rationale → issue acceptance → change → verification before resuming implementation. Existing documents and issues are updated, not replaced by parallel sources of truth. |

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
| GitHub Actions free execution | The [original plan](../../plans/completed/digest.md) and [architecture limits](../../ARCHITECTURE.md#ограничения-и-известные-особенности) use **2,000 minutes/month** and estimate **two ~3-minute runs/day** | Historical planning assumption. Current account entitlement, actual runtime and remaining allowance are not established by that text. The original zero-cost intent is established. |
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
- Feedback ingestion is gated by adaptive enablement, and review-led selection does
  not consume feedback. This is a documented regression (#48), not a revised domain goal.
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

### Subsequent decision: durable complete-source processing

[ADR0004](../../decisions/0004-durable-editorial-evidence.md) records the next #55
choice after this reconciliation. Root technical and independent editorial review
accepted implementation under the operator's delegated execution on 2026-10-01.
It does not make the prior sample final acceptance or close the real-output gate.
Complete-source persistence is now an explicit boundary change; incomplete work is
pending rather than rejected by article length or provider quota. See the ADR for
coverage lineage, acquisition uncertainty, storage, delivery and verification rules.

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

**Orange — что произошло (с указанием агрегата-владельца):**

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

**Blue — команды:**

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

**Lilac — политики:**

- Когда `FeedbackReceived` → `UpdateSourcePriority` (14-дневное затухание). Исполнитель: Radar.collector при следующем pipeline-запуске. ¹ Ownership размазан: Delivery записывает, Radar применяет — hot spot #3.
- Когда Trial активен ≥ `trial_days` дней → `EvaluateTrialSource` (graduated/demoted). Исполнитель: Radar.collector.
- Когда Article старше 48 часов → `ArticleFiltered`. Исполнитель: Radar.collector.
- Когда `SourceApproved` → `AddSourceToConfig` (добавляет trial-блок в `config.yaml`) → `TrialStarted`. Исполнитель: Discovery.
- Когда delivery провалилась → откатить `FeedbackStore.last_update_id` и `ratings` до pre-run состояния. Исполнитель: main.py.

**Yellow — агрегаты:**

- **Source** — владеет: `user_priority`, `enabled`. Runtime-состояние (`trial_started`, `graduated`, `demoted`) хранится в `SourceStateStore` (`.cache/source_state.json`) — не на агрегате (ADR-0003).
- **Article** — владеет: `hash` (identity), `relevance_score`, `age`, `source_ref`, `delivered_flag`.
- **PendingSource** — владеет: `name`, `url`, `category`, `discovered_at`, `source_hash`. Lifecycle: `discovered → approved/rejected`.

> **Trial state вне Source (ADR-0003):** `trial_started`, `graduated`, `demoted` хранятся в `SourceStateStore`, позволяя `config.yaml` оставаться декларативным.

**Green — read models:**

- `BubbleReport` — снимок filter bubble: diversity score (Shannon entropy по 7-дневным включениям статей), топ-категории по объёму, счётчики feedback за 14 дней, trial/graduated/demoted источники. Генерируется on-demand по команде `/bubble`.
- `DigestHistory` — `.md`-файлы в Obsidian (формат подчинён ожиданиям Obsidian).
- `NanoStatus` — двухстрочный footer каждого дайджеста: кол-во источников/статей, LLM-провайдер, promoted/demoted счётчики, средний score источников.

**Red — горячие точки:**

1. **Source dual-storage**: `config.yaml` мутируется двумя путями: `apply_trial_decisions()` и `add_source_to_config()`. Нет единого владельца. → Issue #37.
2. **Article без явного lifecycle**: нет состояний `raw → scored → delivered → archived`. Дедупликация через hash — ad-hoc. → Issue #39.
3. **Feedback decay owner**: логика затухания размазана между Delivery (запись) и Radar (применение). → Issue #40.
4. **Radar→Delivery contract drift**: `CategorySummary` расширяется `ArticleSummary`-набором без явного внутрифазового контракта. → Issue #29.

---

## Boundary

**В scope:**
- Сбор статей из RSS (Radar)
- Фильтрация и оценка релевантности по контексту пользователя
- Дедупликация Article по hash
- Генерация нарративов и поиск контрсигналов (Irritator)
- Доставка через Telegram и запись в Obsidian (Delivery)
- Адаптивная система приоритетов источников (trial + feedback)
- Обнаружение и одобрение новых источников (Discovery)
- Filter bubble аналитика (BubbleReport)

**Намеренно вне scope:**
- Полнотекстовое хранилище сырых статей
- Политика уведомлений пользователя (Telegram's concern)
- Полнотекстовый поиск по истории дайджестов (Obsidian's concern)
- Аутентификация и авторизация

**Термин, меняющий смысл на границе:**
`Article` — на стороне RSS это XML-запись без контекста. После ingestion в Radar.collector Article приобретает `relevance_score`, `age`, `source_ref`.

---

## Aggregate Root

**Source**
- Инварианты: `priority` ∈ [1..10]; `url` является валидным HTTP/HTTPS URL; `name` уникален среди enabled sources; demoted source не участвует в pipeline (фильтруется в `main.py` через `source_state.is_demoted()`).
- Runtime-состояние trial в `SourceStateStore`, не на агрегате.

**Article**
- Инварианты: `hash` = SHA-256 первых 500 символов description + title; одна и та же пара (hash, source) никогда не ingested дважды в рамках `CACHE_MAX_AGE_DAYS=7`; `pub_date` старше 48 часов → Article не ingested.

**PendingSource**
- Инварианты: `source_hash` уникален в pending-списке; `url` прошёл SSRF-валидацию (`_dns_pinning.validate_url`) до попадания в pending.

---

## Policies

| Trigger | Action | Executor |
|---------|--------|----------|
| `FeedbackReceived` | `UpdateSourcePriority` с 14-дневным затуханием | Radar.collector, следующий запуск |
| Trial активен ≥ `trial_days` И `calculate_score > 0.6` | `TrialGraduated` | Radar.collector |
| Trial активен ≥ `trial_days` И `calculate_score < 0.3` | `TrialDemoted` | Radar.collector |
| Article `pub_date > 48h` | `ArticleFiltered` | Radar.collector |
| `SourceApproved` | Добавить trial-блок в `config.yaml`, эмитировать `TrialStarted` | Discovery |
| `delivery_ok == False` | Откатить `FeedbackStore` до pre-run состояния | main.py |

---

## Context Map

| Upstream BC / System | Pattern | Notes |
|----------------------|---------|-------|
| RSS Feed Providers | **Conformist** | Потребляем Atom/RSS через feedparser; подстраиваемся под их формат |
| Telegram Bot API | **Conformist** | Используем их Published Language (MarkdownV2, inline keyboards, callback queries) |
| HN / Reddit / arXiv / dev.to / Lobsters | **Conformist** | Читаем публичные API без контракта; breakage возможен при изменении API |
| Obsidian Vault | **Customer-Supplier** | Obsidian — downstream customer; формат `.md` подчинён его ожиданиям; Digest — supplier |

---

## Use Cases

### UC-1: Ежедневный запуск пайплайна

**Actor:** GitHub Actions (Scheduler)
**Preconditions:** `config.yaml` существует и валиден; LLM API key доступен; `TELEGRAM_BOT_TOKEN` и `TELEGRAM_CHAT_ID` установлены.
**Main scenario:**
1. `collect_feedback()` опрашивает Telegram getUpdates; новые votes и approval-решения записываются в `FeedbackStore`.
2. `calculate_effective_priorities()` пересчитывает приоритеты источников.
3. `collect()` фетчит RSS-фиды параллельно; новые Articles дедуплицируются по hash.
4. `summarize_all()` суммаризирует каждую категорию через LLM; для ≥1 топ-тем генерирует три перспективы (Optimist/Skeptic/Realist) если `summary_style != brief`.
5. `run_irritator()` извлекает нарративы → генерирует adversarial-запросы → ищет контрсигналы → валидирует → ранжирует.
6. `write_digest()` сохраняет `.md` в Obsidian.
7. `send_article_cards()` публикует individual Telegram posts с кнопками 👍/👎.
8. `send_counter_signals()` публикует контрсигналы с IrritatorStatus footer.
9. `evaluate_trial_sources()` принимает решения о graduated/demoted.
10. Все сторы персистируются атомарно.

**Alternatives:**
- 3a: Все фиды недоступны → `AllFeedsFailedError` → пайплайн завершается с кодом 1.
- 3b: Нет новых статей → пайплайн завершается успешно, без доставки.
- 5a: Irritator pipeline пуст → `IrritatorStatus.level == "empty"` → контрсигналы не публикуются.
- 7a: Telegram delivery провалилась → `FeedbackStore` откатывается; оба delivery failures → exit code 1.

**Postconditions:** `feedback.json`, `source_state.json`, `source_stats.json`, `source_category_map.json` обновлены; `.md`-файл создан; Telegram messages отправлены; dedup cache обновлён.

---

### UC-2: Пользователь голосует за статью

**Actor:** User (через Telegram inline button)
**Preconditions:** Article card уже доставлена с кнопками `fb:a:g:{hash}` / `fb:a:b:{hash}`; `article_source_map` содержит `hash → source_name`.
**Main scenario:**
1. Пользователь нажимает 👍 или 👎.
2. На следующем запуске `collect_feedback()` получает callback query.
3. Парсинг `callback_data = "fb:a:{g|b}:{article_hash}"` → `rating = +1 / -1`.
4. `ArticleFeedback(article_hash, source_name, rating, timestamp)` добавляется в `FeedbackStore.ratings`.
5. Telegram callback query отвечается (loading indicator сбрасывается).

**Alternatives:**
- 3a: `article_source_map` miss для `hash` → `ArticleFeedback` записывается с `source_name=""`, логируется warning.
- 4a: Feedback запись от > 30 дней назад → будет pruned при следующем `save_feedback()`.

**Postconditions:** `FeedbackStore` содержит новый `ArticleFeedback`; при следующем запуске он влияет на `effective_priorities` этого источника.

---

### UC-3: Пользователь запрашивает filter bubble отчёт

**Actor:** User (Telegram command `/bubble`)
**Preconditions:** Хотя бы один запуск пайплайна был завершён; `.cache/source_stats.json`, `.cache/source_state.json`, `.cache/source_category_map.json` существуют.
**Main scenario:**
1. `collect_feedback()` получает message `text="/bubble"`.
2. Загружаются `stats`, `state`, `category_map` из `.cache/`.
3. `compute_bubble_report()` вычисляет: дата последнего дайджеста, `_diversity_score()` (Shannon entropy), топ-категории по 7-дневным включениям, feedback за 14 дней, trial/graduated/demoted счётчики.
4. Отчёт отправляется в тот же chat.

**Alternatives:**
- 2a: `.cache/`-файлы отсутствуют → `compute_bubble_report()` возвращает отчёт с нулями.

**Postconditions:** Пользователь получил read-only снимок своего информационного пузыря; состояние системы не изменилось.

---

### UC-4: Оператор одобряет/отклоняет новый источник

**Actor:** User (Telegram inline button)
**Preconditions:** Discovery phase (`--discover`) нашла новый источник и отправила approval message с кнопками `src:ok:{hash}` / `src:no:{hash}`; PendingSource сохранён в `.cache/pending_sources.json`.
**Main scenario:**
1. Пользователь нажимает "✅ Добавить" или "❌ Отклонить".
2. На следующем запуске `collect_feedback()` парсит `callback_data = "src:{ok|no}:{source_hash}"`.
3. Решение записывается в `FeedbackStore.source_decisions`.
4. `_process_pending_approvals()` загружает pending список, находит соответствие по `source_hash`.
5. Если approved: `add_source_to_config()` добавляет trial-блок в `config.yaml`; `TrialStarted` эмитируется на следующем запуске.
6. Если rejected: PendingSource удаляется из pending.

**Alternatives:**
- 4a: PendingSource уже удалён (например, отклонён ранее) → решение игнорируется.
- 5a: `add_source_to_config()` падает → ошибка логируется, PendingSource остаётся в pending.

**Postconditions:** `config.yaml` обновлён (approved) или PendingSource удалён (rejected).

---

### UC-5: Trial source evaluation (graduated/demoted)

**Actor:** GitHub Actions (Scheduler, автоматически в конце pipeline)
**Preconditions:** В `config.yaml` есть источник с `trial: true`; `SourceStateStore` содержит `trial_started`; прошло ≥ `trial_days` дней.
**Main scenario:**
1. `evaluate_trial_sources()` читает `SourceStateStore` и `SourceStats`.
2. Для каждого trial-источника вычисляет `calculate_score()` (reliability 30% + productivity 30% + desc_quality 20% + recency 20%).
3. `score > 0.6` → `TrialGraduated`: `SourceStateStore.mark_graduated(name)`.
4. `score < 0.3` → `TrialDemoted`: `SourceStateStore.mark_demoted(name)`; источник исключается из следующих запусков.
5. `SourceStateStore` сохраняется.

**Alternatives:**
- 2a: `score` ∈ [0.3..0.6] → источник остаётся в trial ещё один цикл.
- 3a: Источник уже graduated/demoted → пропускается.

**Postconditions:** `source_state.json` обновлён; demoted источник не участвует в следующем pipeline-запуске.

---

## Domain Data Model

### Article

| Attribute | Type | Invariants |
|-----------|------|------------|
| `title` | str | непустой |
| `link` | str | валидный URL |
| `description` | str | max 500 символов после sanitize |
| `source` | str | существует в `config.sources` |
| `category` | str | существует в `config.sources` |
| `pub_date` | `datetime \| None` | если известна, не старше 48 часов (иначе filtered) |

Состояния: `raw` (из feedparser) → `ingested` (прошёл dedup + age + blocklist) → `summarized` (включён в `CategorySummary`). Архивирование — через dedup cache TTL 7 дней.

### CategorySummary

| Attribute | Type | Invariants |
|-----------|------|------------|
| `category` | str | |
| `summary_text` | str | прошёл `_clean_summary()` |
| `article_count` | int | ≥ 1 |
| `article_summaries` | `list[ArticleSummary]` | пустой список если per-article summaries не запрошены |

### ArticleSummary

| Attribute | Type | Invariants |
|-----------|------|------------|
| `title` | str | |
| `link` | str | |
| `source` | str | |
| `category` | str | |
| `summary` | str | ≤ 2 предложения; раскрывает нетривиальный/неочевидный аспект (бизнес-правило, зафиксировано в UL) |

### Source (aggregate)

Хранится декларативно в `config.yaml`. Runtime-состояние в `SourceStateStore`.

| Attribute | Type | Invariants |
|-----------|------|------------|
| `name` | str | уникален среди enabled sources |
| `url` | str | HTTP/HTTPS, прошёл SSRF-валидацию |
| `category` | str | |
| `priority` | int | [1..10] |
| `enabled` | bool | |
| `trial` | bool | |
| `trial_days` | int | > 0 если `trial=true` |

Runtime state (`SourceStateEntry`): `trial_started: str | None`, `graduated: bool`, `demoted: bool`.

### FeedbackStore

Персистируется в `.cache/feedback.json`. Prunes ratings > 30 дней; `article_source_map` capped at 1000 entries.

| Field | Type |
|-------|------|
| `ratings` | `list[ArticleFeedback]` |
| `last_update_id` | int (Telegram update ID cursor) |
| `last_digest_sources` | `list[str]` |
| `last_digest_time` | str (ISO) |
| `source_decisions` | `dict[source_hash, "approved"\|"rejected"]` |
| `article_source_map` | `dict[article_hash_8, source_name]` |

### SourceStats

Персистируется в `.cache/source_stats.json`. History capped at 30 дней.

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

Генерируется on-demand по команде `/bubble`. Не персистируется — вычисляется из `.cache/`-файлов каждый раз.

| Field | Type | Source |
|-------|------|--------|
| `generated_at` | str (UTC) | `datetime.now()` |
| `last_digest_time` | str | `FeedbackStore.last_digest_time` |
| `diversity_score` | float [0..100] | Shannon entropy по 7-дневным включениям статей по источникам |
| `diversity_label` | `"Diverse"\|"Moderate"\|"Concentrated"` | score ≥ 70 → Diverse; ≥ 40 → Moderate; иначе Concentrated |
| `category_breakdown` | `dict[category, count]` (7d) | `source_category_map` + `SourceStats.history` |
| `feedback_summary` | `(total, positive, negative)` (14d) | `FeedbackStore.ratings` |
| `trial_summary` | `(graduated, trial, demoted)` | `SourceStateStore` |

---

## Interface Contracts

| Interface | Direction | Protocol | Operations | Handled failures | Unhandled failures |
|-----------|-----------|----------|-----------|------------------|--------------------|
| RSS Feed Providers | inbound | HTTP/HTTPS + feedparser | GET feed URL | timeout → source marked error; `feedparser.bozo` + empty entries → warn | SSL errors, schema changes в XML |
| Telegram Bot API (delivery) | outbound | HTTPS REST | `sendMessage` (MarkdownV2), `sendMessage` (inline keyboard) | HTTP 4xx → logged warning, non-critical | Bot token revoked |
| Telegram Bot API (feedback) | inbound | HTTPS polling | `getUpdates` + `answerCallbackQuery` + `deleteWebhook` | webhook active → auto-delete before polling; 0 results → info log | Rate limiting, update_id skew |
| Irritator sources (HN/Reddit/arXiv/devto/Lobsters) | outbound | HTTPS REST | search queries (per-adapter) | per-adapter exceptions caught; empty results → continue | API schema changes |
| Obsidian Vault | outbound | filesystem | atomic write `.md` to configured path | write error → `markdown_saved=False` | Obsidian plugin format changes |
| LLM providers (Groq/Gemini/DeepSeek/Anthropic/Mistral) | outbound | HTTPS REST | chat completion | provider exception → logged, category skipped | API key invalid, quota exceeded |

---

## NFR

Только механически-проверяемые ограничения:

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

1. **Source dual-storage**: `config.yaml` мутируется двумя независимыми runtime-путями. → Issue #37.
2. **Article без явного lifecycle**: нет типизированных состояний между ingestion и archival. → Issue #39.
3. **Feedback decay owner**: логика затухания распределена между Delivery и Radar. → Issue #40.
4. **Radar→Delivery contract drift**: нет явного внутрифазового контракта между Radar и Delivery. → Issue #29.
