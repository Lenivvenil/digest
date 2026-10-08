# Bounded Context: Irritator

_Discovered: 2026-04-28. Migrated and extended from `irritator-bc.md` (written against commit `cc974f5`; issue-#53 fixes incorporated)._

## Current domain model

Irritator tests the operator's reading against external evidence. It receives
attributed narratives, plans bounded searches, validates external candidates and
ranks their relevance to those narratives. A different model opinion, popularity
score or matching quotation alone does not establish a useful counter-signal.

| Concept | Contract that matters |
| --- | --- |
| Narrative and cited target | Keep the attributed claim and supporting source evidence distinct from exploratory hypotheses. Hypotheses guide query planning; ranking excludes them. |
| Search query | A bounded topic/entity search can be valid without copying a source phrase. Optional phrase matches are provenance diagnostics. |
| External signal | A search hit retains its URL and source data. Deterministic filtering and optional liveness checks do not verify its truth or relevance. |
| Ranked result and status | Report validated ranking outcomes, omissions and incomplete coverage honestly. No result, unavailable search and rejected evidence are different outcomes. |
| Saved investigation evidence | The review-led supplementary path uses saved checkpoints and private archive traces. Compact mode keeps actual results in the archive; the legacy summary path can still run synchronously. |

The application orchestrates search/model work; [pure investigation owners](../../../digest/domain/investigation/)
hold signal/query values and URL rules. Concrete adapters handle search and liveness.
The [current ownership map](../../ARCHITECTURE.md#modules-and-responsibilities) and
[remaining coupling](../../ARCHITECTURE.md#deliberate-remaining-coupling) describe
those boundaries, including the eager compatibility package.

Useful counter-evidence remains an empirical acceptance gate under
[#77](https://github.com/Lenivvenil/digest/issues/77). Read the current status below
for deployed versus local work, then the [historical model](#historical-domain-snapshot--april-2026)
for the original analysis. The [Digest requirements](../digest/overview.md#current-owner-requirements-and-acceptance-traces)
define the primary-delivery and operating constraints.

## Current status — 2026-10-07

This page preserves the April domain model and observations below. The current
[Digest decision register](../digest/overview.md#decision-and-evidence-register--2026-10-01)
distinguishes those observations from owner requirements and later implementation.
English documentation and configurable primary/supplementary translation are implemented.
Finite runtime acceptance under #94 is recorded in [ADR0005](../../decisions/0005-optional-presentation-translation.md);
it does not establish universal semantic fidelity. #55 remains open for useful,
faithful daily content. Full-source acquisition is an experimental option, not a
mandatory processing or closure requirement.

Irritator's product purpose remains genuine external counter-evidence. An
independent model opinion or a Skeptic paragraph is not an external counter-signal.
The primary-first delivery recovery isolates optional-stage failures without
removing that purpose; compact mode retains actual supplementary results in the archive.
Statements below that the pipeline is wholly transient
or that nothing is persisted describe the April snapshot, not the later
follow-up checkpoints. Source-contract repairs are deployed, while real counter-evidence
acceptance remains open in [#77](https://github.com/Lenivvenil/digest/issues/77).

### Signal validation ownership under #147

The structural continuation deployed on 2026-10-08 through
[PR #160](https://github.com/Lenivvenil/digest/pull/160) and
[runtime PR #83](https://github.com/Lenivvenil/digest-prod/pull/83). It separates the unchanged six-field `Signal` into
`domain/investigation/signals.py` and ordered URL/blocklist policy into
`domain/investigation/validation.py`. These owners import without the search registry,
HTTP or application code. `irritator.sources.Signal` remains the same class object;
its `published` annotation remains `str`. The historical model below does not add a
nullable field or new raw/validated lifecycle types to that contract.

`application/signal_validation.py` runs pure validation before optional HEAD operations
in `adapters/http/signal_liveness.py`; `irritator/validator.py` is a compatibility export.
Deduplication still lower-cases the entire URL and strips trailing slashes. The first
occurrence consumes that key before scheme/host and substring-blocklist checks, even
when rejected. Malformed parsing errors still propagate. Optional liveness preserves
result order, semaphore 10, timeout 5 seconds, disabled redirects, 404/410 removal,
other-status retention and the existing network/URL-error drops. Disabled liveness
and empty filtered input perform no HTTP work.

This is an ownership extraction, not evidence verification, a source/SSRF policy
change or search/ranking/query redesign. The existing source set and six fields are
unchanged. [ADR0019](../../decisions/0019-remaining-application-scenarios.md#signal-validation-continuation)
records the boundary; #147 retains separate release and editorial acceptance evidence.

The adjacent continuation in the same draft gives the unchanged two-field `SearchQuery` a pure
owner in `domain/investigation/queries.py` and preserves its old class-object exports.
The unchanged RSS/full-source coverage strings now belong to
`domain/investigation/coverage.py`, with evidence-stage aliases retained. These pure
owners load without orchestration; importing the existing source registry or adapters
still initializes the eager `irritator` package. Search-adapter isolation and broader
result/status ownership are not established by this extraction.

### Draft source-context preservation under #77/#122

The disabled full-source integration retains known qualification passages beside the
original narrative citations for query generation and ranking. Only passages from the
exact cited article and source/body snapshot can be added; the claim and its quotations
remain unchanged. Oversized complete context is technical incomplete under the existing
evidence-envelope bound, not silently shortened. This bound is not provider admission.
The full-source path separately reuses the reader's exact Gemini or labelled local Groq
admission on each actual stage request, sharing its existing counter and deadline.
Unknown/oversized admission holds optional analysis; legacy RSS behavior is unchanged.
Account quota and live acceptance remain unverified. See [proposed ADR0009](../../decisions/0009-selected-source-admission.md).

This fixes loss of supplied evidence, not the model's ability to judge applicability.
The saved Apple/Progent explanation still invented a necessity comparison absent from
the supplied sources. A literal quotation, relation label or valid JSON does not prove
a meaningful counter-signal. Manual comparisons and source qualifications must remain
distinct from automatic search/ranking acceptance.

### Grounded queries and optional provenance

[ADR0011](../../decisions/0011-source-anchored-investigation-queries.md) records
useful grounded topic/entity searches without requiring a copied source phrase. Its
earlier mandatory literal-match guard was an unaccepted draft proposal and is superseded,
not an accepted policy being revoked. Exploratory hypotheses remain distinct from
source claims. A derived source-text match remains optional provenance metadata only;
it proves neither neutrality nor useful recall. Valid nonempty query sets without a
match proceed unchanged to bounded search; no replacement query is fabricated.

The accepted revision is deployed for bounded RSS investigation and also applies to
the disabled full-source path. Source-reading activation remains unapproved.
The standalone legacy generator, lexical/schema validation, query/model/request limits,
deadlines, primary delivery and semantic acceptance remain unchanged.
The deployed query-planning input also retains extracted hypotheses in a separate,
unverified block. They can guide topic angles and intent; ranking still excludes them
and evaluates the attributed claim. Request hashes bind the exact new input; historical
attempts remain unchanged. See [PR140](https://github.com/Lenivvenil/digest/pull/140).
Search results that exactly repeat a cited target URL or its verified final URL are
excluded from external candidates, with explicit URLs/counts retained. Different
documents on the same publisher remain eligible; no guessed alias or extra fetch is used.

### Private ranking evidence

[ADR0012](../../decisions/0012-private-ranking-evidence.md) records a bounded private
JSON trace of post-validation external candidates, admission omissions and fully
validated ranking dispositions. Omitted-source previews explicitly record truncation;
unknown or unreturned relevance is never relabeled as rejection. This deployed private archive
trace does not change ranking behavior or establish semantic counter-evidence quality.

## Historical domain snapshot — April 2026

The following interview/code-derived model preserves its original headings, terms
and rationale. Statements about wholly transient work, data shapes and ownership
refer to that snapshot unless explicitly amended. Use the current model and status
above for the saved-evidence path and the actual source contracts.

---

## Purpose

The Irritator BC owns the problem of breaking the operator's filter bubble. Given the day's news summaries, it surfaces external content that meaningfully challenges, contradicts, or complicates the dominant narratives the operator is being fed.

**Explicitly outside scope:** generating the summaries (Radar's concern); formatting and delivering results to the operator (Delivery's concern); storing feedback about whether a counter-signal was useful (Delivery/Feedback's concern); modelling the operator's bubble across runs (no persistent bubble fingerprint today — red hotspot).

---

## Actors

| Actor | Type | Role |
|-------|------|------|
| Radar BC | Upstream service | Produces `list[CategorySummary]` — the day's summarised news, which is also the bubble to challenge |
| LLM Provider | External system | Executes narrative extraction, query generation, and signal ranking |
| External search platforms (HN, Reddit, arXiv, dev.to, Lobsters) | External systems | Provide raw signals in response to adversarial queries |
| Delivery BC | Downstream service | Consumes `(list[Narrative], list[RankedSignal], IrritatorStatus)` for rendering |
| Operator (indirect) | Human | Configured `IrritatorConfig` determines behaviour; feedback loop from operator votes is not yet wired |

---

## Events (Event Storming)

**Orange — what happened:**

| Event | Aggregate owner | Stage |
|-------|----------------|-------|
| NarrativeExtracted | — (transient) | Stage 1 |
| QueryGenerated | — (transient) | Stage 2 |
| SignalFound | — (transient) | Stage 3 |
| SignalFiltered | — (dedup/blocklist) | Stage 4 |
| SignalRanked | — (transient) | Stage 5 |
| IrritatorCompleted | — (process event) | Orchestrator |
| IrritatorEmptied | — (process event, nothing survived) | Orchestrator |
| IrritatorFailed | — (stage raised exception) | Orchestrator |

**Blue — commands:**

| Command | Handler |
|---------|---------|
| ExtractNarratives | `narrative_extractor.extract_narratives()` |
| GenerateQueries | `query_generator.generate_queries()` |
| SearchAllSources | `sources.search_all_sources()` |
| ValidateSignals | `validator.validate_signals_async()` |
| RankSignals | `ranker.rank_signals()` |

**Lilac — policies:**

- When `NarrativeExtracted` → `GenerateQueries` per narrative, in parallel.
- When `QueryGenerated` → `SearchAllSources` (fan-out: each query × all configured sources).
- When `SignalFound` → `ValidateSignals` (dedup by URL + blocklist + optional HEAD liveness check).
- When `ValidatedSignal` → `RankSignals` per narrative (ALL validated signals ranked against EACH narrative — hot spot: provenance dropped).
- When `RankedSignal.score < min_signal_score` → discarded.

**Yellow — aggregates:**

There are no persisted aggregates. The entire pipeline is transient: data exists only in memory for one run.

**Green — read models:**

- `IrritatorStatus` — for the operator: funnel counters (`N narratives, M signals, K valid, J passed ranking`), level = ok/empty/error.

**Red — hotspots:**

1. **Provenance dropped at search**: `search_all_sources()` flattens all query results into one `list[Signal]`. The `Signal → SearchQuery → Narrative` link is lost. Ranking uses all pairs: each narrative is ranked against the entire signal pool.
2. **Narrative lacks dominance/confidence**: `Narrative` carries no evidence of consensus. One paragraph in one article is indistinguishable from a claim repeated by 12 sources.
3. **No bubble fingerprint**: Irritator knows nothing about the operator beyond today's summaries. Every run starts cold.
4. **Source-narrative fit unmodelled**: all sources receive the same queries; there is no SourceProfile.
5. **"empty" without a reason code**: `IrritatorStatus.level == "empty"` covers three different situations: off-topic signals, on-topic signals below the threshold, and no signals at all.

---

## Boundary

**In scope:**
- Extracting dominant narratives from a set of `CategorySummary` objects
- Generating adversarial search queries for each narrative
- Searching external platforms for signals
- Validation (dedup by URL, blocklist, optional liveness check)
- Ranking signals by the strength of their contradiction of a narrative

**Deliberately outside scope:**
- Persisting narratives or signals between runs
- Formatting and delivering results (Delivery's concern)
- Incorporating operator feedback (Delivery/Feedback hot spot #3 in the Digest BC)
- Profiling the operator's information diet

**Term whose meaning changes at the boundary:**
`Signal` — on external platforms, this is a search hit with a popularity score (HN points, Reddit upvotes, etc.). On entering `rank_signals()`, a `Signal` is assessed by `contradiction_score` (1–10), with the opposite semantics. The same data type, different frames of reference.

---

## Aggregate Root

Irritator has no persisted aggregates. Invariants are bounded by a single pipeline run:

- `Narrative` must contain `claim`, `category`, `implicit_assumptions[]`, and `why_worth_challenging`; otherwise, the parser discards the LLM response.
- `Signal` is deduplicated by a lower-cased URL with its trailing slash stripped. First wins.
- Blocklist matching uses a case-insensitive substring of `title + snippet`.
- `RankedSignal` is accepted only if `score ≥ min_signal_score` (default = 5).
- `IrritatorStatus.level = "error"` only if a stage raised an exception; empty results → `"empty"`, not `"error"`.

---

## Policies

| Trigger | Action |
|---------|--------|
| All stages completed; `all_ranked` is nonempty | `IrritatorStatus(level="ok")` |
| Any stage returned an empty list | `IrritatorStatus(level="empty")` |
| Stage raised exception | Log the error; for Stages 1–2, return early; for Stage 5, skip ranking for that narrative |
| `RankedSignal.score < min_signal_score` | Discard the signal |
| URL already seen in the current run | `SignalFiltered` (dedup) |
| Title/snippet contains a blocklist keyword | `SignalFiltered` |

---

## Context Map

| System | Pattern | Notes |
|--------|---------|-------|
| Radar BC | **Conformist** | Irritator consumes `CategorySummary` directly, with no ACL. If Radar changes the contract, Irritator breaks silently. Irritator has no influence over the upstream. |
| LLM Providers | **Conformist** | We use their API; prompt engineering is on our side |
| HN / Reddit / arXiv / dev.to / Lobsters | **Conformist** | Public APIs without a contract; breakage is possible |
| Delivery BC | **Customer-Supplier** | Irritator is the upstream supplier; Delivery is the downstream customer. Contract: `(list[Narrative], list[RankedSignal], IrritatorStatus)` |

---

## Use Cases

<a id="uc-1-полный-irritator-pipeline-happy-path"></a>

### UC-1: Full Irritator pipeline (happy path)

**Actor:** Digest main pipeline (programmatic `run_irritator()` call)
**Preconditions:** `summaries: list[CategorySummary]` is nonempty; the LLM API is available; `IrritatorConfig.sources` contains at least one source.
**Main scenario:**
1. `extract_narratives(summaries, config)` → the LLM returns ≤ `max_narratives` narratives with `claim`, `category`, `implicit_assumptions`, and `why_worth_challenging` fields.
2. `generate_queries(narratives, config)` → generate `queries_per_narrative` adversarial SearchQuery objects for each narrative, in parallel, with explicit negation/failure phrasing.
3. `search_all_sources(all_queries, config, client)` → fan-out: each query × all configured sources; flatten the results into `list[Signal]`.
4. `validate_signals_async(signals, blocklist, client, check_liveness)` → dedup by URL + blocklist filter + optional HEAD liveness check. Remaining output: `list[Signal]`.
5. For each narrative: `rank_signals(narrative, signals, config)` → the LLM assesses each signal against a single criterion ("how strongly does this CONTRADICT the narrative?") with calibration anchors 9–10/7–8/5–6/1–4. Discard signals with `score < min_signal_score`; return the top `top_signals` as `list[RankedSignal]`.
6. `IrritatorStatus(level="ok")` with funnel counters.

**Alternatives:**
- 1a: The LLM returned no narratives → `IrritatorStatus(level="empty", text="0 narratives from N summaries")`.
- 3a: All sources returned empty results → `IrritatorStatus(level="empty")`.
- 4a: All signals were filtered out (dedup/blocklist) → `IrritatorStatus(level="empty")`.
- 5a: All signals are below `min_signal_score` → `IrritatorStatus(level="empty")`.
- 5b: Ranking failed for one narrative → log the error; continue with the other narratives.

**Postconditions:** Return `(list[Narrative], list[RankedSignal], IrritatorStatus)`. Nothing is persisted.

---

<a id="uc-2-все-сигналы-ниже-порога"></a>

### UC-2: All signals below the threshold

**Actor:** Digest pipeline
**Preconditions:** The pipeline completed Stages 1–4 successfully; `valid_signals` is nonempty.
**Main scenario:**
1. `rank_signals()` runs for each narrative.
2. All `RankedSignal.score < min_signal_score`.
3. `all_ranked` remains empty.
4. `IrritatorStatus(level="empty", text="N narratives, M signals, K valid, 0 passed ranking")`.

**Postconditions:** Delivery receives an empty signal list + `IrritatorStatus.level="empty"`. Delivery sends no counter-signals post.

---

<a id="uc-3-stage-падает-с-исключением"></a>

### UC-3: A stage raises an exception

**Actor:** Digest pipeline
**Preconditions:** The LLM API is unavailable or an external source returned an unexpected format.
**Main scenario:**
1. Stage 1 (extract_narratives) raises an exception.
2. The orchestrator catches the exception: `IrritatorStatus(level="error", text="narrative extraction failed: <exc>")`.
3. Early return: `([], [], status)`.

**Alternatives:**
- Stage 3 (search) fails → return the narratives already extracted: `(narratives, [], status)`.
- Stage 5 (rank) fails for one narrative → log the error; continue with the others.

**Postconditions:** Delivery receives `IrritatorStatus.level="error"`; counter-signals are not sent.

---

### UC-4: Blocklist filtering

**Actor:** Digest pipeline (automatically in the validator)
**Preconditions:** `config.filters.blocklist_keywords` contains keywords.
**Main scenario:**
1. `search_all_sources()` produces `list[Signal]`.
2. `validate_signals_async()` checks each signal: `title + snippet` contains a blocklist keyword (case-insensitive substring) → `SignalFiltered`.
3. Additional checks: dedup by URL (lower-cased, trailing slash stripped); optional HEAD request for liveness.

**Postconditions:** Remaining signals contain no blocklist keywords and have unique URLs.

---

<a id="uc-5-devto-полнотекстовый-поиск-post-issue-53"></a>

### UC-5: dev.to full-text search (post-issue-#53)

**Actor:** Irritator (sources/devto.py)
**Preconditions:** `IrritatorConfig.sources` contains `devto`.
**Main scenario:**
1. Pass the full `SearchQuery.query` text to `?q=<query>` (not the first word as a tag).
2. dev.to returns articles matching the full text.

> **Context:** before issue-#53, the adapter used `params={"tag": query.split()[0].lower()}` — a tag-listing call that returned hype content, the opposite of a counter-signal.

**Postconditions:** dev.to signals are relevant to the full query, not its first word.

---

## Ubiquitous Language

| Term | Business definition | Aliases to avoid |
|------|---------------------|------------------|
| **Narrative** | A dominant claim presented as self-evident in today's information environment, extracted from a group of `CategorySummary` objects in one pipeline run. Not persisted. | Topic, Theme, Consensus |
| **Signal** | External content (article, discussion, preprint) from a search platform (HN, Reddit, arXiv, dev.to, Lobsters), retrieved through an adversarial query. At this stage, a candidate, not evidence. `Signal.score` = **popularity** (upvotes, points), a measure of platform consensus, NOT a contradiction score. | Hit, Result, Item |
| **RankedSignal** | A Signal that has passed LLM ranking. `RankedSignal.score` [1–10] = **contradiction score**: how strongly this content contradicts or complicates a specific Narrative. Its semantics are opposite to `Signal.score`. | Verified signal |
| **CounterSignal** | A product concept: what the BC promises the operator, content that breaks the bubble. There is no separate `CounterSignal` type in the code; it is a `RankedSignal` with `score ≥ min_signal_score`. The term appears in prompts and logs, not data types. | Counter-narrative, Alternative |
| **SearchQuery** | An adversarial search query for a specific Narrative, with explicit negation/failure keywords. Deliberate contradiction distinguishes it from an ordinary search. `intent` is a free-form string; no taxonomy of contradiction types is defined (red hotspot). | Query, Search |
| **IrritatorStatus** | An operator-facing trace of one run: funnel counters + level (ok/empty/error). `level="empty"` = the pipeline ran correctly, but nothing survived. It does not distinguish reasons for emptiness (red hotspot). | Status, Report |

---

## Domain Data Model

### Narrative

| Attribute | Type | Invariants |
|-----------|------|------------|
| `claim` | str | nonempty; a statement of the consensus claim |
| `category` | str | matches the category in `CategorySummary` |
| `implicit_assumptions` | `list[str]` | ≥ 1 |
| `why_worth_challenging` | str | nonempty |

Not persisted. Transient: exists only in memory for one run.

**Known model limitations:** carries neither `dominance` (number of sources repeating the claim) nor `confidence` (the LLM's confidence that this is a consensus rather than a paraphrase of one paragraph). One paragraph and 12 articles have the same type.

### SearchQuery

| Attribute | Type | Invariants |
|-----------|------|------------|
| `query` | str | adversarial phrasing (failure/criticism/limitations keywords) |
| `intent` | str | freeform description (no taxonomy — red hotspot) |

### Signal

| Attribute | Type | Notes |
|-----------|------|-------|
| `url` | str | identity for dedup |
| `title` | str | |
| `snippet` | str | |
| `source_name` | str | platform name |
| `published` | `str \| None` | |
| `score` | float | **popularity** (HN points, Reddit upvotes, etc.), NOT a contradiction score; semantically opposite to `RankedSignal.score` |

**Lifecycle states:** `raw` (from the source adapter) → `validated` (dedup + blocklist + liveness) → implicit `ranked` (participates in `rank_signals()`). No separate types for different states: one dataclass passes through all stages.

### RankedSignal

| Attribute | Type | Invariants |
|-----------|------|------------|
| `signal` | `Signal` | |
| `score` | int | [1..10]; **contradiction score** (NOT popularity); `score ≥ min_signal_score` to be retained |
| `reasoning` | str | explanation from the LLM |
| `narrative_claim` | str | denormalized copy of `Narrative.claim` |

**Note:** `narrative_claim` is a string copy, not a reference to an aggregate. Provenance to SearchQuery and the original Narrative is not retained.

### IrritatorStatus

| Attribute | Type | Values |
|-----------|------|--------|
| `text` | str | funnel counters: `"N narratives, M signals, K valid, J passed ranking"` |
| `level` | `"ok" \| "empty" \| "error"` | `ok` = ranked signals exist; `empty` = the pipeline ran, nothing survived; `error` = a stage raised an exception |

**Limitation:** `level="empty"` does not distinguish (a) off-topic signals, (b) on-topic signals below the threshold, or (c) no signals found at all. The operator cannot diagnose the cause from the level alone.

---

## Interface Contracts

| Interface | Direction | Protocol | Operations | Handled failures | Unhandled failures |
|-----------|-----------|----------|-----------|------------------|--------------------|
| Radar BC (CategorySummary input) | inbound | in-process call | `run_irritator(summaries, config, client)` | empty summaries list → early empty return | CategorySummary schema change — no ACL |
| LLM Providers (narrative extraction) | outbound | HTTPS REST | chat completion, role=EXTRACT_NARRATIVES, temp=0.5 | exception → IrritatorStatus level=error | malformed JSON in LLM response — parser fails |
| LLM Providers (query generation) | outbound | HTTPS REST | chat completion per narrative, in parallel | exception → IrritatorStatus level=error | |
| LLM Providers (ranking) | outbound | HTTPS REST | chat completion per narrative | exception per narrative → logged, narrative skipped | |
| HN Algolia API | outbound | HTTPS REST | `search?query=...&tags=story` | exception caught per-source | API schema change |
| Reddit JSON API | outbound | HTTPS REST | `r/{sub}/search.json?q=...` | exception caught per-source | API auth change, subreddit ban |
| arXiv API | outbound | HTTPS REST + XML | `search_query=...` | exception caught per-source | XML schema change |
| dev.to API | outbound | HTTPS REST | `?q=<full_query>` | exception caught per-source | |
| Lobsters API | outbound | HTTPS REST | `search?q=...` | exception caught per-source | |
| Delivery BC (output) | outbound | in-process return | `(list[Narrative], list[RankedSignal], IrritatorStatus)` | — | Delivery applies its own formatting rules |

---

## NFR

Mechanically verifiable constraints only:

| Constraint | Enforcement | Artifact |
|------------|-------------|----------|
| HTTP requests to sources use `httpx.AsyncClient` with semaphore=10 | `asyncio.Semaphore(10)` in `sources/__init__.py` | `digest/irritator/sources/__init__.py` |
| Signal dedup by URL runs before ranking | `validate_signals_async()` is called before `rank_signals()` | `digest/irritator/__init__.py` |
| Blocklist runs before ranking | same order in the orchestrator | `digest/irritator/__init__.py` |
| Calibration anchors are fixed in the ranker prompt | prompt text in `ranker.py` | `digest/irritator/ranker.py` |

---

## Internal Compliance

| Norm | Enforcement type | Artifact | Honor-system gap? |
|------|-----------------|----------|-------------------|
| Type hints on all functions | mypy strict | CI | No |
| No unused imports | ruff F401 | pre-commit + CI | No |
| async/await for all I/O | mypy + review | — | Yes |
| No real HTTP in tests | pytest convention | `tests/test_sources_*.py` | Yes — no network isolation in CI |
| Per-stage exception isolation | code review | orchestrator pattern | Yes |

---

## Red Hotspots

1. **Missing provenance edge**: Signal does not retain the SearchQuery and Narrative that produced it. All-pairs ranking cannot answer: "did we find anything at all for narrative N?"
2. **Narrative dominance/confidence are not modelled**: one paragraph and 12 sources have the same type.
3. **No bubble fingerprint as an explicit BC input**: Irritator knows nothing about the operator's information diet beyond one run.
4. **Source-narrative fit unmodelled**: no SourceProfile; arXiv and HN receive the same queries.
5. **`IrritatorStatus.level="empty"` without a reason code**: the operator cannot diagnose the cause.
6. **No feedback loop**: operator votes do not reach Irritator; the system does not learn from whether counter-signals were clicked.
