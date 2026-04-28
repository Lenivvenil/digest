# Bounded Context: Irritator — ARCHIVED DIAGNOSTIC

> **Superseded.** This document is a historical diagnostic report written in April 2026.
> The canonical BC overview is at **[docs/domain/irritator/overview.md](irritator/overview.md)**.
> Keep this file for its failure-mode analysis (§§ marked [UPDATED — issue #53]); do not update it.

---

**Purpose:** Given the day's news summaries, surface external content that meaningfully challenges, contradicts, or complicates the dominant narratives the operator is being fed — i.e. break the bubble.

This document was written against `feat/validator-ranker-6` (commit `cc974f5`) by the domain-researcher agent. §§ marked **[UPDATED — issue #53]** reflect changes shipped in the issue-53 fix; remaining sections retain their pre-fix diagnostic value.

---

## 1. Ubiquitous Language

### Core terms (current code)

| Term | Defined where | Meaning in the code | Business meaning intended |
|---|---|---|---|
| **CategorySummary** | `radar/summarizer.py` | LLM-written prose summarizing one news category for one run | The *input bubble*: what the operator just read |
| **Narrative** | `irritator/narrative_extractor.py` | `{claim, category, implicit_assumptions, why_worth_challenging}` extracted by LLM from summaries | A *consensus claim* the day's coverage is implicitly making |
| **SearchQuery** | `irritator/query_generator.py` | `{query, intent}` — adversarial query fanned out to all configured sources **[UPDATED — issue #53]** | A targeted probe that *should* find evidence against the narrative |
| **Signal** | `irritator/sources/__init__.py` | `{url, title, snippet, source_name, published, score}` — a raw hit from any source adapter | A *candidate* piece of external content — not yet evaluated for counter-evidence |
| **RankedSignal** | `irritator/ranker.py` | `{signal, score (1–10), reasoning, narrative_claim}` from LLM | A signal the LLM judges as substantively contradictory to the narrative |
| **Counter-signal** | Only in prompts, comments, log strings | (no dataclass; conceptual) | The product the BC promises: content that breaks consensus |
| **Bubble-breaking** | Implied (CLAUDE.md, prompts) | (no dataclass) | The *value act* — operator reads something they otherwise wouldn't have |
| **IrritatorStatus** | `irritator/__init__.py` | `{text, level: ok|empty|error}` for the run | Operator-facing trace: how far down the funnel did we get |

### Vocabulary drift / concept collisions

These are the load-bearing problems, not stylistic nits.

1. **`Signal` is overloaded.** It is the type returned by source adapters (raw search hit) AND the conceptual vehicle for "counter-signal." Nothing in the type signals "this contradicts narrative X in way Y." Counter-signal is not a domain object — it is an aspirational label attached after ranking. The same dataclass passes through "raw," "validated," and "ranked-against-narrative" stages without a type change.

2. **`score` is two completely different things on `Signal` vs `RankedSignal`.**
   - `Signal.score` (float) = source-native popularity: HN points, Reddit upvotes, dev.to reactions, Lobsters score, arXiv hardcoded `0.0`. This is a measure of *consensus*. Popular = the crowd already agrees.
   - `RankedSignal.score` (int 1–10) = LLM judgment of "substance, contradiction, source credibility, surprise." This is a measure of *anti-consensus fit to a specific narrative*.
   - Same field name, opposite semantics. Anywhere the two are co-handled (e.g. ranker prompt construction, debugging, future ordering logic) they will be confused.

3. **"Counter-signal" is rhetoric, not domain.** The word appears in system prompts (`"Ты — аналитик контр-сигналов"`), in `send_counter_signals()`, in comments. It does not appear as a class, field, or invariant. The pipeline produces `RankedSignal` and the renderer/operator are left to take "score ≥ 7" on faith as proof of counter-ness.

4. **"Narrative" conflates two concepts.** It is currently a single LLM extraction with no notion of:
   - *dominance* (how many articles, how many sources voiced this claim — vs. one outlier?)
   - *confidence* (LLM's certainty that this is actually consensus, not its own paraphrase of one paragraph).
   The same dataclass represents "the entire industry assumes X" and "this one TechCrunch piece said X" indistinguishably.

5. **"Intent" is freeform.** `SearchQuery.intent` is a string with no taxonomy. The query generator prompt does not enumerate the kinds of contradiction one might be hunting (failure evidence, dissenting research, post-mortem, security incident, alternative paradigm, regulator pushback). The LLM falls back to paraphrasing the narrative.

---

## 2. Bounded Context Canvas — Irritator

### Inbound

| From | Payload | Pattern |
|---|---|---|
| **Radar BC** | `list[CategorySummary]` (LLM-written prose, per-category, per-run) | Customer/Supplier — Radar is upstream, Irritator is downstream consumer. No anti-corruption layer; Irritator consumes Radar's output language directly (`category`, `summary_text`, `article_count`). |
| **Config** (`IrritatorConfig`) | `max_narratives=5`, `queries_per_narrative=3`, `top_signals=3`, `min_signal_score=5` **[UPDATED — issue #53]**, `sources=[hn, reddit, arxiv, devto, lobsters]`, `check_liveness`, `reddit_subreddits` | Conformist relationship to operator config. |

The BC has **no awareness of the operator's history** — no feedback loop from prior digests' votes, no "bubble fingerprint" describing what kind of consensus this operator is already drowning in. Each run starts cold from today's summaries.

### Outbound

| To | Payload | Pattern |
|---|---|---|
| **Delivery BC** | `(list[Narrative], list[RankedSignal], IrritatorStatus)` via `run_irritator(...)` | Customer/Supplier. Delivery formats per its own rules. |
| **Operator (telemetry)** | `IrritatorStatus.text` — funnel counters as a Telegram footer | OHS-ish: Delivery shows the operator how far the pipeline got. |

### Internal model

```
CategorySummary[]           (input from Radar)
  ↓ extract_narratives()    [LLM, role=EXTRACT_NARRATIVES, temp=0.5]
Narrative[]                 ≤ max_narratives, no dominance/confidence score
  ↓ generate_queries()      [LLM, per-narrative, parallel]
{narrative_claim → SearchQuery[]}   queries_per_narrative each, adversarial phrasing [UPDATED — issue #53]
  ↓ search_all_sources()    [fan-out: each query × all configured sources, semaphore=10] [UPDATED — issue #53]
Signal[]                    raw hits, popularity-scored
  ↓ validate_signals_async()
Signal[]                    deduped by URL, blocklist-filtered, optional HEAD liveness
  ↓ rank_signals()          [LLM, per narrative, ALL valid signals ranked against EACH narrative]
RankedSignal[]              filtered by score ≥ min_signal_score (=5) [UPDATED — issue #53], top_signals=3 per narrative
```

### Business rules / invariants (enforced)

- A `Narrative` must carry `claim`, `category`, `implicit_assumptions[]`, `why_worth_challenging`.
- ~~A `SearchQuery` must declare exactly one `target_source`. Queries to unconfigured sources are silently dropped.~~ **[REMOVED — issue #53]** Each query now fans out to all configured sources.
- Signal dedup is by lower-cased, trailing-slash-stripped URL. First wins.
- Blocklist match is case-insensitive substring across `title + snippet`.
- A `RankedSignal` is kept only if `score ≥ min_signal_score` (default 5). **[UPDATED — issue #53]**
- `IrritatorStatus.level` is `error` only if a stage *raised* — if every stage runs cleanly and produces empty results, level is `empty`. This means "we tried hard and found nothing" is not distinguishable downstream from "we tried hard and the world had no contradictions today."

### Business rules that should exist but don't

- No invariant linking a `Signal` back to the `SearchQuery` (and therefore the `Narrative`) that produced it. Provenance is dropped at `search_all_sources()`, where results are flattened across queries.
- No invariant that a narrative must be ranked against signals *fetched for it* — see "where it breaks" below.
- No invariant on the *kind* of contradiction (intent taxonomy is freeform).
- No invariant that the operator's bubble is even modelled.

### Where the BC breaks — failure mode analysis

This section was written as a diagnostic against the pre-fix codebase. Issue #53 (commit `8d36567`) addressed 4 of the 8 failure modes; 4 remain open.

#### Fixed by issue #53 (historical record)

**1. Query generator prompt did not ask for contradictions.** `query_generator.py` said *"Craft search queries to find counter-signals"* — but to an LLM "search query for narrative X" is statistically very close to "keywords describing X." The prompt did not enumerate contradiction-shaped patterns ("failure of", "post-mortem", "security incident in", "vs", "criticism of", "limitations of", "did not work"). Generated queries mirrored the narrative's vocabulary, so search engines returned the *same* content that produced the narrative. *Fixed: prompt rewritten to require adversarial phrasing with explicit negation/failure keywords.*

**3. The ranker prompt rewarded "substance + credibility" alongside "contradiction."** `ranker.py` scored signals on *"substance, contradiction to the narrative, source credibility, and surprise factor"* — four criteria AND-ish in LLM judgment. A spicy contrarian Reddit post scored low on credibility; an authoritative paper that mildly qualified the narrative scored low on contradiction. The conjunction collapsed to mid-scores (5–6), falling under the threshold. *Fixed: replaced with a single criterion ("how strongly does this CONTRADICT or COMPLICATE the narrative?") plus explicit 9-10/7-8/5-6/1-4 calibration anchors.*

**4. `min_signal_score=7` sat just above the LLM hedge zone.** The prompt had no anchor examples for what a 7 looks like vs a 5 vs a 9. LLMs default to 5–7 for ambiguous cases. The threshold filtered out exactly the band the LLM produced most. *Fixed: default lowered to 5 (consistent with production `config.yaml` override); calibration anchors added to the ranker prompt (see point 3).*

**5. dev.to adapter was structurally a consensus engine.** `devto.py` used `params={"tag": query.split()[0].lower()}` — a *tag-listing* call, not a text search. It returned popular tutorials for the first word of the query. For any narrative whose first keyword matched a hot tag (e.g. "ai", "rust", "kubernetes") it returned hype articles — the opposite of counter-signal. *Fixed: switched to `?q=full_query` for full-text search.*

#### Still open

The following 4 failure modes were not addressed in issue #53. They are also documented as structural design gaps in §3 "Missing concepts" below.

**2. The narratives are extracted from a curated pro-tech feed.** Radar's source list is a tech-architect bubble (per `CLAUDE.md`). Narratives extracted from this corpus are themselves consensus-flavored. The narrative-extraction prompt (`narrative_extractor.py:30-69`) asks for "dominant narratives that are rarely questioned" — but never instructs the LLM that the corpus itself is selection-biased. (Hypothesis 4, code-supported.)

**6. Source-narrative fit is unmodelled.** Even with fan-out (all queries now reach all sources), the query generator has no profile of what each source is *good for*. arXiv is great for "alternative academic findings" and useless for "industry post-mortems"; Reddit varies wildly by subreddit. Source selection is effectively uniform-prior guessing. See §3 "SourceProfile" for the missing concept. (Hypothesis 3, broader form.)

**7. All-pairs ranking blurs per-narrative results.** `irritator/__init__.py`: `for narrative in narratives: rank_signals(narrative, signals, config)`. Each narrative is ranked against the *full* validated signal pool — including signals fetched for *other narratives'* queries. Because provenance was dropped at `search_all_sources()`, there is no way to detect "we fetched zero signals targeting narrative N." A narrative whose own queries returned nothing can still get "ranked," typically with all-low scores from off-topic signals — indistinguishable from "we tried but the world had no contradictions." See §3 "Provenance edge" for the missing concept.

**8. No feedback loop.** Even if a counter-signal gets through, the BC has no concept of "did this break the bubble for the operator?" The article-vote feedback collected in `feedback.py` is not wired back into Irritator. The system cannot learn that, e.g., its arXiv signals never get clicked. See §3 "Bubble-break verification."

---

## 3. Missing concepts

What concepts would a well-functioning counter-signal system need that the current model doesn't have? Beyond the obvious "intent taxonomy / source profile":

### Structural gaps in the model

- **Provenance edge: `Signal` → `SearchQuery` → `Narrative`.** Today a signal is a free-floating bag of `{url, title, snippet, source, score}`. For per-narrative ranking and per-narrative diagnostics ("which narratives drew zero signals?"), every signal needs to remember which query produced it and therefore which narrative it was fetched for. This is the single most impactful missing edge.

- **Narrative dominance / confidence.** A `Narrative` should carry evidence of its own consensus: how many articles, how many sources, how many categories voiced the claim. Today an LLM hallucination from one paragraph is typed identically to a claim repeated across 12 articles. Without this, the operator gets challenged on things that aren't actually their bubble.

- **Bubble fingerprint as explicit BC input.** The operator's information diet (which sources they read, which articles they upvote, which categories dominate over weeks) is not an input to Irritator. Bubble-breaking against an *unknown bubble* is dart-throwing. This would also be the natural place to plug feedback-loop signal.

- **CounterSignalKind / IntentTaxonomy.** Enumerated kinds of contradiction the system is capable of seeking: `failure-evidence`, `post-mortem`, `dissenting-research`, `security-incident`, `regulatory-pushback`, `alternative-paradigm`, `reproducibility-failure`, `scaling-limit`. With a taxonomy, both the query generator and the ranker can be evaluated per-kind ("we never find post-mortems" is actionable; "min_signal_score is too high" isn't).

- **SourceProfile.** Each source needs a domain-language description of what kinds of counter-signals it produces well (e.g. arXiv → dissenting-research; HN comments → reproducibility-failure & scaling-limit; r/programming → post-mortem & alternative-paradigm; Lobsters → critical commentary; dev.to → mostly hype, low counter-signal yield). Without this the query generator cannot make a non-uniform source choice.

- **NarrativeChallenge or RankedSignal-as-challenge.** A first-class concept that explicitly models *the relationship*: "this URL challenges narrative N along assumption A in way K (kind), to degree D, with reasoning R." Today this is implicit in `RankedSignal` and `narrative_claim` is denormalized as a string copy. A proper aggregate would let invariants like "every accepted narrative must have ≥1 challenge OR be marked 'no contradictions found in today's sources'" be expressed.

- **EmptyOutcome with reason code.** Today `IrritatorStatus.level == "empty"` is a single bucket. Operator gets `"5 narratives, 87 signals, 87 valid, 0 passed ranking"` — informative, but does not distinguish: "all signals were off-topic to their narratives" vs. "signals were on-topic but ranked below threshold" vs. "the threshold itself filtered the LLM's hedge band." Without this the operator can't tell which lever to pull.

- **Calibration anchors.** ~~Not a domain object exactly, but a domain *contract*: the ranker prompt must commit to anchor examples ("a 9 looks like X, a 7 looks like Y, a 5 looks like Z"). Otherwise the 1–10 scale is uncalibrated and `min_signal_score=7` is a guess.~~ **[UPDATED — issue #53]** Calibration anchors (9-10/7-8/5-6/1-4) were added to the ranker prompt alongside the single-criterion rewrite; `min_signal_score` default lowered to 5. This item is no longer missing.

- **Bubble-break verification.** The value act is "operator reads something they otherwise wouldn't have." Nothing in the BC observes whether a delivered counter-signal was clicked, saved, voted on, or ignored. Without closing this loop, the BC is blind to its own success/failure rate.

### Concept-collision fixes the model needs

- Rename `Signal.score` to `Signal.popularity` (or split into `source_popularity`) so it is structurally distinct from `RankedSignal.score`, which should arguably be `RankedSignal.contradiction_score`.
- Stop calling things "counter-signal" in prose while shipping `Signal` and `RankedSignal` in types. Either promote `CounterSignal` to a domain object (with explicit narrative link, kind, and reasoning) or stop using the word.
- Distinguish `RawSignal` from `ValidatedSignal` from `NarrativeChallenge` in the type system, even if the data overlaps. Today the same `Signal` instance flows through three different lifecycle states with no type-level marker.

---

## 4. Open questions (red hotspots)

- Is the operator's actual bubble well-described by their RSS feed list, or is the *received* digest itself the bubble we should challenge?
- Should counter-signals be drawn from outside the tech sphere entirely (humanities, regulation, ethnographic studies)? The current source list is itself inside the tech consensus.
- Is per-day, per-run irritation the right cadence? A genuine counter-narrative may take a week of evidence to surface; today's pipeline expects today's contradictions to today's narratives.
- Should the BC own the narrative-extraction step at all? It currently does, but narratives are arguably a Radar-side concept (a re-derivation of what was reported); the Irritator's true responsibility starts at "given a narrative, hunt contradictions."
- What is the contract with Delivery when zero counter-signals survive? Today `send_counter_signals` quietly sends nothing useful; should the operator instead get an explicit "the bubble held today" message?
