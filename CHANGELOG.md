# Changelog

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
