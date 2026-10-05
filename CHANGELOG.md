# Changelog

## [Unreleased] — Reliability rehabilitation

- Offline reconciliation response binding validates a sparse evidence input against
  exact request/admission/completion metadata and original citation IDs. It preserves
  nominated conditions without claiming semantic completeness or adding dispatch.

- External evidence responses enforce the existing 512,000-byte decoded-body limit
  during streaming; the fixed-endpoint arXiv adapter applies the same bound in both
  investigation paths. The fixed arXiv endpoint explicitly rejects redirects, including
  when a custom caller enables them; owned pipeline callers already rejected them.
  Oversized bodies fail explicitly without parsing a prefix.
  Accepted bytes and charset decoding are retained. This bounds accumulated response
  data, not decompressor allocations or XML-parser memory. Existing MD5 article/source
  IDs remain byte-identical and are explicitly marked as non-security hashing.

- Draft investigation query floor (#77) requires one source-anchored topic query
  within the existing slots. Ungrounded nonempty sets remain technical incomplete
  before search. This affects bounded RSS and full-source investigation on rollout,
  regardless of the reading flag; literal anchoring is not semantic acceptance.
  Exact cited target/final URLs are excluded from external candidates with explicit
  diagnostics; different documents from the same publisher remain eligible.
- Draft source admission (#122) reuses complete-source acquisition and exact/estimated
  request accounting from #107, bound to current saved candidate selections. Durable
  generation-intent holds and actual per-page fallback provenance remain separate from
  #55 semantic acceptance; technical completion does not publish draft prose.
- Candidate accounting (#121): ordinary review-led preparation captures pre-slot
  identities and resumes later bounded packets through persisted selection evidence.
  Existing accepted editions still take precedence. Same-response typed per-ID
  dispositions distinguish metadata reasons/duplicates from capacity-deferred work;
  old missing reasons remain unresolved. Exact source objects and packet-local archives
  keep resolved history outside current-work admission, with reversible exclusions
  and unsent selections recoverable. Real-output and sustained throughput acceptance
  remain open.

- Prepared-edition boundary (#120): accepted canonical analysis is resumable before
  presentation; exact Telegram payloads are frozen and remotely persisted before a
  separate sender claims them. Message IDs and complete article coverage are retained;
  ambiguous sends or incomplete state persistence hold replay. Existing daily schedule
  and job ceilings remain unchanged; natural-release acceptance is still required.

- English current documentation and safe onboarding; historical records retain their
  original language, and no software-license grant is supplied.
- Configurable generated-text translation with a pinned route, bounded requests,
  canonical fallback and durable content/target/model/prompt-bound cache. Absent
  configuration preserves legacy generation. Finite runtime acceptance includes a
  faithful prompt-v2 archived narrative and visible English fallback after a rate limit.
- Compact daily editions (#105) preserve selected text, links and article identities
  across bounded Telegram chunks. Durable reservation precedes publication; only
  confirmed coverage earns delivery attribution, and unknown outcomes hold replay.
  Optional results remain archived. One production edition confirmed five articles
  in one chunk with persisted archive and state.
- Batch-compatible source approvals (#106) bind ordinary-message decisions to a
  current proposal and apply them before collection. Backup/config/state failures
  retain decisions; replay does not duplicate an already configured source.

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
