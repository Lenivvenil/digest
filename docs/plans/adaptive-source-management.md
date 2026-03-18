# Plan: Adaptive Source Management with Feedback Loop and Trend Detection

## Overview
Реализация адаптивной системы управления источниками: автоматический пересмотр приоритетов на основе качества контента, сбор обратной связи от пользователя через Telegram-кнопки, обнаружение трендов и механизм пробных (trial) источников. Система комбинирует объективные метрики (частота публикаций, уникальность, доступность фида) с пользовательскими предпочтениями для динамической корректировки приоритетов, не полагаясь исключительно ни на одну из сторон.

## Validation Commands
- `python -m pytest tests/ -v`
- `ruff check src/ tests/`
- `mypy src/`

### Task 1: Data models and config extensions for adaptive sources
Фундамент: расширяем конфиг и создаём типы данных для всей подсистемы — статистика источников, обратная связь, пробные источники. Все последующие задачи будут опираться на эти структуры.
- [x] In `src/config.py`: add optional fields to `SourceConfig` dataclass — `trial: bool = False`, `trial_started: str | None = None`, `trial_days: int = 7`; update `_parse_source()` to load these fields from YAML
- [x] In `src/config.py`: add `AdaptiveConfig` dataclass with fields `enabled: bool`, `feedback_weight: float = 0.3`, `score_weight: float = 0.5`, `base_weight: float = 0.2`, `trial_slots: int = 2`, `min_priority: int = 1`, `max_priority: int = 5`; add optional `adaptive` section to `Config` dataclass and `load_config()`
- [x] Create `src/source_scorer.py` with dataclasses: `SourceStats` (fields: `name: str`, `total_fetches: int`, `successful_fetches: int`, `total_articles_found: int`, `articles_included_in_digest: int`, `avg_description_length: float`, `last_seen: str | None`, `history: list[DailySnapshot]`) and `DailySnapshot` (fields: `date: str`, `articles_found: int`, `articles_included: int`, `fetch_ok: bool`)
- [x] Create `src/feedback.py` with dataclasses: `ArticleFeedback` (fields: `article_hash: str`, `source_name: str`, `rating: int`, `timestamp: str`) and `FeedbackStore` (fields: `ratings: list[ArticleFeedback]`, `last_update_id: int`)
- [x] Add/update tests: `tests/test_config.py` — test parsing of `trial` fields and `adaptive` config section; `tests/test_source_scorer.py` — test dataclass creation and defaults; `tests/test_feedback.py` — test dataclass creation
- [x] Mark completed

### Task 2: Source quality scoring engine with persistence
Реализуем подсчёт объективных метрик качества каждого источника: надёжность фида, продуктивность (сколько статей попадает в дайджест), качество описаний. Статистика сохраняется в `.cache/source_stats.json` между запусками.
- [ ] In `src/source_scorer.py`: implement `load_stats(cache_dir: str) -> dict[str, SourceStats]` — load from `.cache/source_stats.json`, return empty dict if file missing
- [ ] In `src/source_scorer.py`: implement `save_stats(stats: dict[str, SourceStats], cache_dir: str) -> None` — serialize to JSON with ISO timestamps
- [ ] In `src/source_scorer.py`: implement `update_stats(stats: dict[str, SourceStats], source_name: str, fetch_ok: bool, articles_found: int, articles_included: int, avg_desc_len: float) -> None` — append `DailySnapshot`, update rolling counters, cap history at 30 days
- [ ] In `src/source_scorer.py`: implement `calculate_score(stats: SourceStats) -> float` returning 0.0–1.0 composite score based on: reliability (`successful_fetches / total_fetches`), productivity (`articles_included / articles_found`), description quality (`avg_description_length > 100`), recency (`last_seen` within 3 days)
- [ ] In `src/collector.py`: after fetching each feed, call `update_stats()` with fetch results (success/fail, article count, avg description length); after `allocate_slots()`, update `articles_included` count per source
- [ ] Add/update tests in `tests/test_source_scorer.py`: test `load_stats`/`save_stats` round-trip, test `update_stats` appends snapshots and caps at 30, test `calculate_score` returns expected values for edge cases (new source with no history → 0.5, perfect source → ~1.0, dead source → ~0.0)
- [ ] Mark completed

### Task 3: Telegram feedback collection
Добавляем кнопки оценки (👍/👎) к сообщениям дайджеста в Telegram и механизм сбора ответов через `getUpdates` при следующем запуске. Обратная связь сохраняется в `.cache/feedback.json`.
- [ ] In `src/telegram.py`: modify `send_digest()` to attach an `InlineKeyboardMarkup` with two buttons (👍 callback_data=`fb:good:{msg_chunk_index}`, 👎 callback_data=`fb:bad:{msg_chunk_index}`) to the last message chunk of the digest
- [ ] In `src/feedback.py`: implement `load_feedback(cache_dir: str) -> FeedbackStore` — load from `.cache/feedback.json`, return empty store if missing
- [ ] In `src/feedback.py`: implement `save_feedback(store: FeedbackStore, cache_dir: str) -> None` — serialize to JSON
- [ ] In `src/feedback.py`: implement `async collect_feedback(bot_token: str, store: FeedbackStore) -> FeedbackStore` — call Telegram `getUpdates` API with `offset=last_update_id+1`, filter `callback_query` results, parse `fb:good`/`fb:bad` from `callback_data`, answer each callback query, update store with new ratings, update `last_update_id`
- [ ] In `src/feedback.py`: implement `get_source_feedback_score(store: FeedbackStore, source_name: str, days: int = 14) -> float | None` — aggregate ratings for source over last N days, return 0.0–1.0 or None if no data
- [ ] Add/update tests in `tests/test_feedback.py`: test `load_feedback`/`save_feedback` round-trip; test `collect_feedback` with mocked Telegram API responses (good rating, bad rating, non-feedback callback, empty updates); test `get_source_feedback_score` aggregation and None for unknown source
- [ ] Mark completed

### Task 4: Dynamic priority engine combining signals
Реализуем движок пересчёта эффективных приоритетов, объединяя три сигнала: базовый приоритет из конфига (стабильность), объективный скоринг (качество контента) и обратную связь пользователя (предпочтения). Тренды выявляются через анализ роста продуктивности источника.
- [ ] In `src/source_scorer.py`: implement `detect_trending_sources(stats: dict[str, SourceStats], window: int = 7) -> list[str]` — return source names where `articles_found` shows >50% increase in last `window` days vs previous window (indicates emerging trend coverage)
- [ ] In `src/source_scorer.py`: implement `calculate_effective_priorities(sources: list[SourceConfig], stats: dict[str, SourceStats], feedback_scores: dict[str, float], adaptive_config: AdaptiveConfig) -> dict[str, int]` — for each source: compute weighted combination of `base_priority / 5 * base_weight + score * score_weight + feedback * feedback_weight`, scale to 1–5 range, apply +1 trend bonus for trending sources (capped at 5), return `{source_name: effective_priority}`
- [ ] In `src/collector.py`: modify `collect()` to accept optional `effective_priorities: dict[str, int] | None`; when provided, override `source.priority` with effective values before calling `allocate_slots()`
- [ ] Add/update tests in `tests/test_source_scorer.py`: test `detect_trending_sources` with flat history (no trends), rising history (detected), test `calculate_effective_priorities` with various signal combinations (high score + bad feedback → moderate priority, low score + good feedback → moderate, trending source gets +1 bonus)
- [ ] Add/update tests in `tests/test_collector.py`: test that `collect()` uses effective priorities when provided, falls back to static priorities when not provided
- [ ] Mark completed

### Task 5: Trial source lifecycle management
Механизм пробных источников: добавление новых источников с пометкой `trial: true`, ограниченный бюджет слотов, автоматическое продвижение в постоянные или удаление после пробного периода на основе скоринга.
- [ ] In `src/source_scorer.py`: implement `evaluate_trial_sources(sources: list[SourceConfig], stats: dict[str, SourceStats], today: str) -> tuple[list[str], list[str]]` — return `(promote_names, demote_names)`: promote trials with score > 0.6 after `trial_days` elapsed, demote trials with score < 0.3 after `trial_days` elapsed, leave others in trial
- [ ] In `src/source_scorer.py`: implement `apply_trial_decisions(config_path: str, promote: list[str], demote: list[str]) -> None` — read `config.yaml`, set `trial: false` for promoted sources, set `enabled: false` for demoted sources, write back to file
- [ ] In `src/collector.py`: modify `allocate_slots()` to reserve separate budget for trial sources (use `adaptive.trial_slots` from config, default 2), so trial sources don't compete with established sources for slots
- [ ] Add/update tests in `tests/test_source_scorer.py`: test `evaluate_trial_sources` — new trial not yet expired (no action), expired with high score (promote), expired with low score (demote); test `apply_trial_decisions` with a temp config.yaml file
- [ ] Add/update tests in `tests/test_collector.py`: test that trial sources get separate slot budget and don't reduce slots for regular sources
- [ ] Mark completed

### Task 6: Pipeline integration and orchestration
Связываем все компоненты в единый пайплайн: загрузка статистики и фидбека, пересчёт приоритетов, сбор статей, обновление статистики, управление trial-источниками. Обновляем документацию и конфиг.
- [ ] In `src/main.py`: in `run()` function, before `collect()`: load source stats via `load_stats()`, load feedback via `load_feedback()`, if adaptive enabled — call `collect_feedback()` to poll new Telegram feedback, call `calculate_effective_priorities()`, pass result to `collect()`
- [ ] In `src/main.py`: in `run()` function, after successful delivery: call `save_stats()` (alongside `save_dedup_cache()`), call `save_feedback()`, if adaptive enabled — call `evaluate_trial_sources()` and `apply_trial_decisions()`
- [ ] In `config.yaml`: add `adaptive` section with default values (`enabled: false`, weights, trial_slots); add comments explaining each field
- [ ] In `src/main.py`: add `--discover` CLI flag that triggers LLM-based source suggestion: build a prompt listing current categories and source names, ask LLM to suggest 2-3 RSS feed URLs for underrepresented topics, validate suggested URLs by attempting to fetch them, print valid suggestions to stdout (user manually adds to config.yaml)
- [ ] Update `RunStats` dataclass in `src/main.py` to include `sources_promoted: int`, `sources_demoted: int`, `feedback_collected: int`; log these in the summary
- [ ] Add/update tests in `tests/test_main.py`: test that `run()` loads/saves stats and feedback when adaptive enabled; test that `run()` skips adaptive logic when disabled; test `--discover` flag argument parsing
- [ ] Mark completed
