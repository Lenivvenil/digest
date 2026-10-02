# Changelog

## [Unreleased] — Reliability rehabilitation

- Opt-in source-cited reading briefs reuse safe full-article acquisition, exact request
  counting, immutable source spans and durable selected-work progress. Literal passage
  checks are not semantic certification; the finite quality/cost gate remains open.

- English current documentation and safe onboarding example; environment credentials
  are explicitly loaded by the operator. Historical records retain their original language.
- Opt-in generated-text translation with an explicitly configured provider/model,
  bounded calls/time/output, canonical fallback and durable content/target/model/prompt-bound
  records. Absent configuration preserves legacy generation; enabled translation requires
  canonical English. Prompt v2 explicitly preserves technical data-flow direction.
  Ordinary post-v2 semantic acceptance remains open.
- Compact Telegram editions preserve selected text, URLs and article identities across
  UTF-16-bounded chunks, with once-per-edition notices and a durable pre-publication
  reservation. Only confirmed chunk coverage earns delivery attribution; unknown outcomes
  hold automatic publication. Optional-stage results remain archived.

- Opt-in blind evidence review: immutable RSS snapshot and identical prompts for
  explicit primary/secondary model slots, no implicit provider fallback, optional
  third family only on validated selection disagreement. Strict source-ID/quote
  validation and shared input/output budgets; Markdown plus JSON provenance.
- Blind selection runs before optional category prose. Primary cards survive
  prose failure; abstention/unavailable diagnostics are still archived.
- Synthetic offline contract demo: `python -m scripts.review_fixture --output DIR`.
  This does not call providers and is not evidence of real-model quality.

- Optional LLM pacing controls bound shared concurrency and request spacing across
  all stages, with bounded retries for 429/5xx/transport failures. Permanent
  authentication, billing and missing-model errors fail over immediately.
  Provider diagnostics expose only status and a sanitized machine error code.
- `telegram.required: true` makes incomplete article-card delivery a failed run,
  even when Markdown exists. It is opt-in (default false). Under this policy,
  Markdown alone does not consume new articles after a Telegram failure.

- Telegram card delivery returns `ArticleDeliveryResult` with confirmed counts,
  delivered hashes and feedback attribution. Callers must no longer interpret
  the return value as an article-source dictionary. Exhausted 429 retries raise.
- Dedup commits only articles covered by successful Markdown output or accepted
  Telegram cards; failed categories and undelivered Telegram-only cards remain
  retryable. `--radar-only` no longer consumes articles.
- Picker failures tolerate malformed JSON and invalid field types. Selected
  links must match input articles; titles/source/category retain canonical
  identities, and duplicate or invented links are discarded.
- Source fetch health and delivered inclusion counts now update adaptive stats,
  including fetch observations from unsuccessful runs. Dry-run does not persist
  observations. Nano status uses actual fetch success/failure counts.
- Same-day Markdown runs keep previous output (`YYYY-MM-DD.md`, then
  `YYYY-MM-DD-2.md`, etc.) instead of replacing the earlier digest.
- DEV.to counter-signal search is deliberately disabled with a warning: the
  documented `/api/articles` API does not support full-text `q` search. Existing
  configs remain loadable, but this adapter returns no signals until a supported
  search integration is implemented.

Compatibility notes: by default, Markdown remains an independent successful delivery channel.
With `telegram.required: false`, saved Markdown can acknowledge an article even if Telegram fails; this
patch does not introduce a per-channel outbox or exactly-once delivery. Runtime
workflows must persist cache files after partial runs for selective retries to
survive a fresh GitHub runner. No production workflow or credentials are changed.

## [1.0.0] — 2026-04-07

Первый стабильный релиз. Проект работает в продакшене с марта 2026 года.

### Features

**Сбор новостей**
- RSS/Atom feed collection через `httpx` + `feedparser`
- Параллельный fetch всех источников
- Дедупликация статей по MD5(title|link) с 7-дневным окном
- Priority-based slot allocation: источники с высоким приоритетом получают больше слотов
- Ограничение по категории (`max_articles_per_source`) и глобальное (`max_total_articles`)
- Фильтрация по свежести (`recency_hours`)

**LLM-суммаризация**
- Поддержка 5 провайдеров: Anthropic Claude, Google Gemini, Groq, Mistral, DeepSeek
- `ProviderChain`: автоматический fallback на следующего провайдера при сбое
- Category routing: разные провайдеры для разных категорий
- Параллельная обработка категорий через `asyncio.gather()`
- Three perspectives format (Optimist 🟢 / Skeptic 🔴 / Realist ⚖️) для топ-новостей
- Кросс-категорийные тренды в конце дайджеста
- Три стиля: `analytical`, `brief`, `detailed`

**Доставка**
- Telegram Bot API: отдельная карточка на каждую статью с кнопками 👍/👎
- Markdown-файлы в `digests/` с YAML front matter для Obsidian
- Статус-footer с метриками запуска (источники, статьи, провайдеры, средний score)
- Уведомление в Telegram при падении workflow

**Адаптивная система**
- Пользовательские оценки 👍/👎 влияют на приоритеты источников
- Автоматические метрики качества: reliability, productivity, description quality, recency
- Обнаружение trending-источников (+1 к приоритету при росте >50% за 7 дней)
- Trial source system: LLM-discovery → Telegram approval → пробный период → promote/disable

**Обнаружение источников**
- `--discover`: LLM генерирует RSS-кандидатов для недопредставленных категорий
- Валидация URL перед предложением
- Telegram approval workflow с кнопками ✅/❌
- Автоматическое добавление в `config.yaml` после одобрения

**CI/CD**
- GitHub Actions: daily digest (02:00 + 13:00 UTC) с test gate (lint, typecheck, tests)
- Weekly discovery (Sundays 06:00 UTC) с test gate
- Concurrency group предотвращает concurrent writes в `.cache/`
- Commit-back pattern: cache и дайджесты автоматически коммитятся в `main`

**Безопасность**
- DNS pinning + SSRF protection через `_dns_pinning.py`
- Input sanitization feed-контента через `_sanitize.py`
- Atomic JSON writes через `_util.py`

### Quality

- 260+ unit/integration тестов (pytest + pytest-asyncio + respx)
- Покрытие: все модули src/
- Lint: ruff (E, F, B rules)
- Type checking: mypy (strict)
- Pre-commit hooks для автоматического форматирования
