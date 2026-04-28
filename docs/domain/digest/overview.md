# Bounded Context: Digest

_Discovered: 2026-04-26. Updated: 2026-04-28. Post-interview sections added from code._

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
