# Bounded Context: Irritator

_Discovered: 2026-04-28. Migrated and extended from `irritator-bc.md` (written against commit `cc974f5`; issue-#53 fixes incorporated)._

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

**Orange — что произошло:**

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

**Blue — команды:**

| Command | Handler |
|---------|---------|
| ExtractNarratives | `narrative_extractor.extract_narratives()` |
| GenerateQueries | `query_generator.generate_queries()` |
| SearchAllSources | `sources.search_all_sources()` |
| ValidateSignals | `validator.validate_signals_async()` |
| RankSignals | `ranker.rank_signals()` |

**Lilac — политики:**

- Когда `NarrativeExtracted` → `GenerateQueries` per narrative, параллельно.
- Когда `QueryGenerated` → `SearchAllSources` (fan-out: каждый запрос × все configured sources).
- Когда `SignalFound` → `ValidateSignals` (dedup by URL + blocklist + optional HEAD liveness check).
- Когда `ValidatedSignal` → `RankSignals` per narrative (ALL validated signals ranked against EACH narrative — hot spot: provenance dropped).
- Когда `RankedSignal.score < min_signal_score` → отброшен.

**Yellow — агрегаты:**

Нет персистируемых агрегатов. Весь pipeline transient — данные живут только в памяти одного запуска.

**Green — read models:**

- `IrritatorStatus` — оператору: funnel counters (`N narratives, M signals, K valid, J passed ranking`), level = ok/empty/error.

**Red — горячие точки:**

1. **Provenance dropped at search**: `search_all_sources()` флатенит результаты всех запросов в один `list[Signal]`. Связь `Signal → SearchQuery → Narrative` теряется. Ranking работает по принципу all-pairs: каждый нарратив ранжируется против всего пула сигналов.
2. **Narrative без dominance/confidence**: `Narrative` не несёт доказательств консенсуса. Один абзац одной статьи неотличим от утверждения, повторяемого 12 источниками.
3. **Нет bubble fingerprint**: Irritator не знает об операторе ничего, кроме сегодняшних сводок. Каждый запуск стартует холодно.
4. **Source-narrative fit unmodelled**: все sources получают одинаковые запросы; нет SourceProfile.
5. **"empty" без кода причины**: `IrritatorStatus.level == "empty"` покрывает три разных ситуации: off-topic signals, on-topic но ниже порога, нет сигналов вообще.

---

## Boundary

**В scope:**
- Извлечение доминирующих нарративов из `CategorySummary`-набора
- Генерация adversarial search queries для каждого нарратива
- Поиск сигналов по внешним платформам
- Валидация (dedup by URL, blocklist, опциональный liveness check)
- Ранжирование сигналов по силе противоречия нарративу

**Намеренно вне scope:**
- Персистенция нарративов или сигналов между запусками
- Форматирование и доставка результатов (Delivery's concern)
- Учёт feedback от оператора (Delivery/Feedback hot spot #3 из Digest BC)
- Профилирование информационной диеты оператора

**Термин, меняющий смысл на границе:**
`Signal` — на стороне внешних платформ это search hit с popularity score (HN points, Reddit upvotes и т.д.). После пересечения в `rank_signals()` `Signal` оценивается по `contradiction_score` (1–10) — противоположная семантика. Тот же тип данных, разные системы координат.

---

## Aggregate Root

Irritator не имеет персистируемых агрегатов. Границы инвариантов — на уровне одного pipeline-запуска:

- `Narrative` должен содержать `claim`, `category`, `implicit_assumptions[]`, `why_worth_challenging` — иначе LLM ответ отброшен парсером.
- `Signal` дедуплицируется по lower-cased, trailing-slash-stripped URL. First wins.
- Blocklist match — case-insensitive substring по `title + snippet`.
- `RankedSignal` принимается только если `score ≥ min_signal_score` (default = 5).
- `IrritatorStatus.level = "error"` только если stage raised exception; пустые результаты → `"empty"`, не `"error"`.

---

## Policies

| Trigger | Action |
|---------|--------|
| Все stages завершились, `all_ranked` непустой | `IrritatorStatus(level="ok")` |
| Любой stage вернул пустой список | `IrritatorStatus(level="empty")` |
| Stage raised exception | Ошибка логируется; если это Stage 1–2 — early return; если Stage 5 — ranking для этого narrative пропускается |
| `RankedSignal.score < min_signal_score` | Сигнал отброшен |
| URL уже видели в текущем запуске | `SignalFiltered` (dedup) |
| Title/snippet содержит blocklist keyword | `SignalFiltered` |

---

## Context Map

| System | Pattern | Notes |
|--------|---------|-------|
| Radar BC | **Conformist** | Irritator потребляет `CategorySummary` напрямую, без ACL. Radar меняет контракт — Irritator молча ломается. Irritator не имеет влияния на upstream. |
| LLM Providers | **Conformist** | Используем их API; prompt engineering на нашей стороне |
| HN / Reddit / arXiv / dev.to / Lobsters | **Conformist** | Публичные API без контракта; breakage возможен |
| Delivery BC | **Customer-Supplier** | Irritator upstream supplier; Delivery downstream customer. Контракт: `(list[Narrative], list[RankedSignal], IrritatorStatus)` |

---

## Use Cases

### UC-1: Полный Irritator pipeline (happy path)

**Actor:** Digest main pipeline (программный вызов `run_irritator()`)
**Preconditions:** `summaries: list[CategorySummary]` непустой; LLM API доступен; хотя бы один source в `IrritatorConfig.sources`.
**Main scenario:**
1. `extract_narratives(summaries, config)` → LLM возвращает ≤ `max_narratives` нарративов с полями `claim`, `category`, `implicit_assumptions`, `why_worth_challenging`.
2. `generate_queries(narratives, config)` → для каждого нарратива параллельно генерируется `queries_per_narrative` adversarial SearchQuery с explicit negation/failure phrasing.
3. `search_all_sources(all_queries, config, client)` → fan-out: каждый query × все configured sources; результаты флатенятся в `list[Signal]`.
4. `validate_signals_async(signals, blocklist, client, check_liveness)` → dedup by URL + blocklist filter + optional HEAD liveness. Остаток: `list[Signal]`.
5. Для каждого нарратива: `rank_signals(narrative, signals, config)` → LLM оценивает каждый сигнал по single criterion ("насколько сильно это ПРОТИВОРЕЧИТ нарративу?") с calibration anchors 9-10/7-8/5-6/1-4. Сигналы с `score < min_signal_score` отброшены; топ `top_signals` возвращаются как `list[RankedSignal]`.
6. `IrritatorStatus(level="ok")` с funnel counters.

**Alternatives:**
- 1a: LLM не вернул ни одного нарратива → `IrritatorStatus(level="empty", text="0 narratives from N summaries")`.
- 3a: Все sources вернули пустые результаты → `IrritatorStatus(level="empty")`.
- 4a: Все сигналы отфильтрованы (dedup/blocklist) → `IrritatorStatus(level="empty")`.
- 5a: Все сигналы ниже `min_signal_score` → `IrritatorStatus(level="empty")`.
- 5b: Ranking упал для одного нарратива → ошибка логируется, остальные нарративы продолжают.

**Postconditions:** Возвращается `(list[Narrative], list[RankedSignal], IrritatorStatus)`. Ничего не персистируется.

---

### UC-2: Все сигналы ниже порога

**Actor:** Digest pipeline
**Preconditions:** Pipeline прошёл stages 1–4 успешно; `valid_signals` непустой.
**Main scenario:**
1. `rank_signals()` отрабатывает для каждого нарратива.
2. Все `RankedSignal.score < min_signal_score`.
3. `all_ranked` остаётся пустым.
4. `IrritatorStatus(level="empty", text="N narratives, M signals, K valid, 0 passed ranking")`.

**Postconditions:** Delivery получает пустой список сигналов + `IrritatorStatus.level="empty"`. Delivery не отправляет counter-signals post.

---

### UC-3: Stage падает с исключением

**Actor:** Digest pipeline
**Preconditions:** LLM API недоступен или внешний source вернул неожиданный формат.
**Main scenario:**
1. Stage 1 (extract_narratives) поднимает исключение.
2. Исключение поймано в orchestrator: `IrritatorStatus(level="error", text="narrative extraction failed: <exc>")`.
3. Early return: `([], [], status)`.

**Alternatives:**
- Stage 3 (search) падает → возвращаются уже извлечённые нарративы: `(narratives, [], status)`.
- Stage 5 (rank) падает для одного нарратива → логируется, остальные продолжают.

**Postconditions:** Delivery получает `IrritatorStatus.level="error"`; counter-signals не отправляются.

---

### UC-4: Blocklist filtering

**Actor:** Digest pipeline (автоматически в validator)
**Preconditions:** `config.filters.blocklist_keywords` содержит ключевые слова.
**Main scenario:**
1. После `search_all_sources()` получен `list[Signal]`.
2. `validate_signals_async()` проверяет каждый сигнал: `title + snippet` содержит blocklist keyword (case-insensitive substring) → `SignalFiltered`.
3. Дополнительно: dedup по URL (lower-cased, trailing slash stripped); optional HEAD request для liveness check.

**Postconditions:** Оставшиеся сигналы не содержат blocklist keywords и уникальны по URL.

---

### UC-5: dev.to полнотекстовый поиск (post-issue-#53)

**Actor:** Irritator (sources/devto.py)
**Preconditions:** `devto` в `IrritatorConfig.sources`.
**Main scenario:**
1. `SearchQuery.query` передаётся как полный текст в `?q=<query>` (не первое слово как тег).
2. dev.to возвращает статьи по полнотекстовому совпадению.

> **Контекст:** до issue-#53 адаптер использовал `params={"tag": query.split()[0].lower()}` — tag-listing call, возвращавший хайп-контент, противоположный counter-signal.

**Postconditions:** Сигналы из dev.to релевантны полному запросу, а не первому слову.

---

## Ubiquitous Language

| Term | Business definition | Aliases to avoid |
|------|---------------------|------------------|
| **Narrative** | Доминирующее утверждение, которое сегодняшнее информационное пространство подаёт как само собой разумеющееся — извлечённое из группы `CategorySummary` одного pipeline-запуска. Не персистируется. | Topic, Theme, Consensus |
| **Signal** | Внешний контент (статья, обсуждение, препринт) из одной из поисковых платформ (HN, Reddit, arXiv, dev.to, Lobsters), полученный по adversarial-запросу. На этом этапе — кандидат, не доказательство. `Signal.score` = **popularity** (upvotes, points) — мера консенсуса платформы, НЕ contradiction score. | Hit, Result, Item |
| **RankedSignal** | Signal, прошедший LLM-ранжирование. `RankedSignal.score` [1–10] = **contradiction score** — насколько сильно этот контент противоречит или осложняет конкретный Narrative. Семантика противоположна `Signal.score`. | Verified signal |
| **CounterSignal** | Продуктовый концепт: то, что BC обещает оператору — контент, который разрушает пузырь. В коде не существует отдельного типа `CounterSignal`; им является `RankedSignal` с `score ≥ min_signal_score`. Термин используется в промптах и логах, но не в типах данных. | Counter-narrative, Alternative |
| **SearchQuery** | Adversarial поисковый запрос для конкретного Narrative с явными negation/failure keywords. Отличается от обычного поиска намеренным противоречием. `intent` — свободная строка, таксономия contradiction-видов не определена (red hotspot). | Query, Search |
| **IrritatorStatus** | Операторский трейс одного запуска: funnel counters + level (ok/empty/error). `level="empty"` = pipeline отработал корректно, но ничего не выжило. Не различает причины пустоты (red hotspot). | Status, Report |

---

## Domain Data Model

### Narrative

| Attribute | Type | Invariants |
|-----------|------|------------|
| `claim` | str | непустой; формулировка консенсусного утверждения |
| `category` | str | соответствует категории из `CategorySummary` |
| `implicit_assumptions` | `list[str]` | ≥ 1 |
| `why_worth_challenging` | str | непустой |

Не персистируется. Transient: существует только в памяти одного запуска.

**Известные ограничения модели:** не несёт `dominance` (кол-во источников, повторивших claim) и `confidence` (уверенность LLM, что это консенсус, а не парафраз одного абзаца). Один абзац и 12 статей типизируются одинаково.

### SearchQuery

| Attribute | Type | Invariants |
|-----------|------|------------|
| `query` | str | adversarial phrasing (failure/criticism/limitations keywords) |
| `intent` | str | freeform description (нет taxonomy — red hotspot) |

### Signal

| Attribute | Type | Notes |
|-----------|------|-------|
| `url` | str | identity для dedup |
| `title` | str | |
| `snippet` | str | |
| `source_name` | str | имя платформы |
| `published` | `str \| None` | |
| `score` | float | **popularity** (HN points, Reddit upvotes, etc.) — НЕ contradiction score; семантически противоположен `RankedSignal.score` |

**Lifecycle states:** `raw` (из source adapter) → `validated` (dedup + blocklist + liveness) → implicit `ranked` (участвует в `rank_signals()`). Нет типов для разных состояний — один dataclass проходит все стадии.

### RankedSignal

| Attribute | Type | Invariants |
|-----------|------|------------|
| `signal` | `Signal` | |
| `score` | int | [1..10]; **contradiction score** (НЕ popularity); `score ≥ min_signal_score` для сохранения |
| `reasoning` | str | объяснение от LLM |
| `narrative_claim` | str | денормализованная копия `Narrative.claim` |

**Примечание:** `narrative_claim` — строковая копия, не ссылка на агрегат. Провенанс к SearchQuery и исходному Narrative не сохраняется.

### IrritatorStatus

| Attribute | Type | Values |
|-----------|------|--------|
| `text` | str | funnel counters: `"N narratives, M signals, K valid, J passed ranking"` |
| `level` | `"ok" \| "empty" \| "error"` | `ok` = ranked signals существуют; `empty` = pipeline отработал, ничего не выжило; `error` = stage raised |

**Ограничение:** `level="empty"` не различает: (a) сигналы off-topic, (b) on-topic но ниже порога, (c) сигналов вообще не найдено. Оператор не может диагностировать причину по одному level.

---

## Interface Contracts

| Interface | Direction | Protocol | Operations | Handled failures | Unhandled failures |
|-----------|-----------|----------|-----------|------------------|--------------------|
| Radar BC (CategorySummary input) | inbound | in-process call | `run_irritator(summaries, config, client)` | пустой список summaries → early return empty | изменение схемы CategorySummary — нет ACL |
| LLM Providers (narrative extraction) | outbound | HTTPS REST | chat completion, role=EXTRACT_NARRATIVES, temp=0.5 | exception → IrritatorStatus level=error | плохой JSON в ответе LLM — парсер падает |
| LLM Providers (query generation) | outbound | HTTPS REST | chat completion per narrative, параллельно | exception → IrritatorStatus level=error | |
| LLM Providers (ranking) | outbound | HTTPS REST | chat completion per narrative | exception per narrative → logged, narrative skipped | |
| HN Algolia API | outbound | HTTPS REST | `search?query=...&tags=story` | exception caught per-source | API schema change |
| Reddit JSON API | outbound | HTTPS REST | `r/{sub}/search.json?q=...` | exception caught per-source | API auth change, subreddit ban |
| arXiv API | outbound | HTTPS REST + XML | `search_query=...` | exception caught per-source | XML schema change |
| dev.to API | outbound | HTTPS REST | `?q=<full_query>` | exception caught per-source | |
| Lobsters API | outbound | HTTPS REST | `search?q=...` | exception caught per-source | |
| Delivery BC (output) | outbound | in-process return | `(list[Narrative], list[RankedSignal], IrritatorStatus)` | — | Delivery форматирует по своим правилам |

---

## NFR

Только механически-проверяемые ограничения:

| Constraint | Enforcement | Artifact |
|------------|-------------|----------|
| HTTP-запросы к sources идут через `httpx.AsyncClient` с semaphore=10 | `asyncio.Semaphore(10)` в `sources/__init__.py` | `digest/irritator/sources/__init__.py` |
| Signal dedup по URL выполняется до ranking | `validate_signals_async()` вызывается перед `rank_signals()` | `digest/irritator/__init__.py` |
| Blocklist применяется до ranking | тот же порядок в orchestrator | `digest/irritator/__init__.py` |
| Calibration anchors в ranker prompt зафиксированы | текст промпта в `ranker.py` | `digest/irritator/ranker.py` |

---

## Internal Compliance

| Norm | Enforcement type | Artifact | Honor-system gap? |
|------|-----------------|----------|-------------------|
| Type hints на всех функциях | mypy strict | CI | No |
| Нет неиспользуемых импортов | ruff F401 | pre-commit + CI | No |
| async/await для всех I/O | mypy + review | — | Yes |
| Нет реального HTTP в тестах | pytest convention | `tests/test_sources_*.py` | Yes — нет network isolation в CI |
| Per-stage exception isolation | code review | orchestrator паттерн | Yes |

---

## Red Hotspots

1. **Provenance edge отсутствует**: Signal не помнит, каким SearchQuery и Narrative он был порождён. All-pairs ranking не может ответить: "для нарратива N мы вообще нашли что-нибудь?"
2. **Narrative dominance/confidence не моделируются**: один абзац и 12 источников — один тип.
3. **Нет bubble fingerprint как explicit BC input**: Irritator не знает об информационной диете оператора вне одного запуска.
4. **Source-narrative fit unmodelled**: нет SourceProfile; arXiv и HN получают одинаковые запросы.
5. **`IrritatorStatus.level="empty"` без reason code**: оператор не может диагностировать причину.
6. **Нет feedback loop**: operator votes не доходят до Irritator; система не обучается на том, кликали ли на counter-signals.
