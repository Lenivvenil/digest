# Plan: Priority-Weighted RSS Source Optimization

## Overview
Вводим универсальную систему приоритетов источников: поле `priority` (1–5) в `SourceConfig`
определяет долю от `max_total_articles`, которую источник получает при сборке. Высокоприоритетные
источники (инженерные блоги, ACM Queue) получают больше слотов; шумные широкие фиды (PYMNTS, Habr)
автоматически сжимаются. После этого обновляем сам список источников — убираем нерабочие URL,
добавляем 10–15 новых высококачественных источников.

## Validation Commands
- `pytest tests/ -q`
- `ruff check src/ tests/`
- `python -c "from src.config import load_config; c = load_config(); print(len(c.enabled_sources), 'sources enabled')"`

---

### Task 1: Add `priority` field to `SourceConfig`

Добавляем поле `priority: int` (допустимые значения 1–5, дефолт 3) в `SourceConfig`. Это единственное
изменение в слое данных — все остальные задачи опираются на это поле.

- [x] В `src/config.py`: добавить поле `priority: int = 3` в датакласс `SourceConfig`
- [x] В `src/config.py`: в функции `_load_sources` парсить опциональное поле `priority` из YAML;
      валидировать что значение целое и входит в диапазон 1–5, иначе бросать `ValueError` с пояснением
- [x] В `tests/test_config.py`: тест — источник без `priority` получает `priority=3`
- [x] В `tests/test_config.py`: тест — источник с `priority: 5` парсится корректно
- [x] В `tests/test_config.py`: тест — `priority: 0` и `priority: 6` вызывают `ValueError`
- [x] Mark completed

---

### Task 2: Implement priority-weighted slot allocation in `collector.py`

Заменяем наивный FIFO-лимит (`per_source_count >= max_articles_per_source`, строки 235–239) на
пропорциональное распределение слотов по приоритетам. Алгоритм:

```
slot(source) = max(1, round(max_total_articles * source.priority / Σ priority всех источников с результатами))
```

Итого для каждого источника вычисляется индивидуальный лимит ещё до итерации по статьям.
Это позволяет источнику с `priority=5` получать ~3× больше слотов, чем с `priority=1`.

- [x] В `src/collector.py`: добавить чистую функцию `allocate_slots(sources: list[SourceConfig], total_budget: int) -> dict[str, int]`,
      которая возвращает словарь `{source.name: slot_count}` по формуле выше;
      если `total_weight == 0` — возвращает по 1 слоту каждому источнику
- [x] В `src/collector.py`: в функции `collect()` — после `asyncio.gather` отфильтровать источники,
      у которых `results` не `None`, вызвать `allocate_slots()` и использовать возвращённые значения
      вместо глобального `config.digest.max_articles_per_source` при итерации по статьям
- [x] В `src/collector.py`: убрать строку `if per_source_count >= config.digest.max_articles_per_source`
      и заменить её на `if per_source_count >= slots[source.name]`
- [x] В `src/collector.py`: сохранить `config.digest.max_articles_per_source` как fallback-потолок —
      ни один источник не может получить больше этого значения независимо от приоритета
- [x] В `tests/test_collector.py`: тест `test_allocate_slots_proportional` — источники с приоритетами
      [5, 3, 1] при бюджете 18 получают слоты пропорционально (5/9×18, 3/9×18, 1/9×18)
- [x] В `tests/test_collector.py`: тест `test_allocate_slots_minimum_one` — источник с `priority=1`
      при любом бюджете получает как минимум 1 слот
- [x] В `tests/test_collector.py`: тест `test_collect_respects_priority` — источник с `priority=5`
      отдаёт больше статей, чем источник с `priority=1`, при одинаковом фиде
- [x] Mark completed

---

### Task 3: Audit existing sources — fix URLs and assign priorities

Проставляем `priority` всем существующим источникам и исправляем устаревшие/неверные URL. Только
`config.yaml`, код не меняется.

**Приоритеты существующих источников:**

| Источник | priority | Действие | Причина |
|---|---|---|---|
| Stripe Engineering | 5 | keep | Высочайшая глубина, платежи + архитектура |
| ACM Queue | 5 | keep | Фундаментальные архитектурные кейсы |
| Martin Fowler | 5 | keep | Архитектура, паттерны, высокий S/N |
| Netflix TechBlog | 5 | keep | Детальные инженерные кейсы |
| Uber Engineering | 5 | keep | Распределённые системы, данные |
| Airbnb Tech Blog | 4 | keep | Качественные инженерные кейсы |
| Cloudflare Blog | 4 | keep | Сети, безопасность, инфраструктура |
| InfoQ (Architecture) | 4 | keep | Уже topic-specific feed |
| Cockroach Labs Blog | 4 | keep | Глубокие CRDB/distributed DB кейсы |
| ScyllaDB Blog | 4 | keep | Производительность БД |
| Grab Tech Blog | 4 | keep | Схожий контекст (SE Asia, платежи) |
| Shopify Engineering | 4 | keep | Ruby/platform/commerce инженерия |
| LinkedIn Engineering | 4 | keep | Большая scale, Kafka, Iceberg |
| DoorDash Engineering | 3 | keep | Операционные кейсы |
| Spotify Engineering | 3 | keep | Data/ML/platform |
| Slack Engineering | 3 | keep | Messaging infra, иногда глубоко |
| AWS Architecture Blog | 3 | keep | Полезно, но часто marketing |
| Kubernetes Blog | 3 | keep | Официальный, medium depth |
| CNCF | 3 | keep | Широко, но релевантно |
| Istio Blog | 3 | keep | Service mesh, нишевый |
| ByteByteGo | 3 | keep | System design, education-style |
| ThoughtWorks Insights | 3 | keep | Архитектурные тренды |
| Hugging Face Blog | 3 | keep | Практический ML/AI |
| The Batch (Andrew Ng) | 3 | keep | AI-новости с контекстом |
| Databricks Blog | 3 | keep | Data engineering |
| MIT Technology Review AI | 2 | keep | Широко, но качественно |
| OpenAI News | 2 | keep | Часто product announcements |
| NVIDIA Developer Blog | 2 | keep | Часто marketing |
| Square Corner | 2 | replace URL | squareup.com → developer.squareup.com |
| GitHub Engineering | 2 | replace URL | githubengineering.com устарел → github.blog/engineering.atom |
| Discord Blog | 2 | replace URL | общий blog → engineering-specific feed |
| Finextra | 2 | keep | Новости, мало инженерного |
| PYMNTS | 2 | keep | Высокочастотный, широкий |
| Spot.uz | 2 | keep | Региональный контекст |
| Kun.uz EN | 2 | keep | Региональный контекст |
| Habr — Interesting | 2 | keep | Широкий, требует фильтрации |
| Finovate | 1 | disable | Продуктовые анонсы, нет инженерии |
| Ars Technica AI | 1 | disable | technology-lab — весь tech, не только AI |
| Hacker News Best | 1 | keep | Очень широкий; низкий приоритет сам дросселирует |

- [x] В `config.yaml`: добавить `priority: N` ко всем существующим источникам согласно таблице
- [x] В `config.yaml`: исправить URL для Square Corner, GitHub Engineering, Discord Blog
- [x] В `config.yaml`: выставить `enabled: false` для Finovate и Ars Technica AI
- [x] Убедиться что конфиг валидируется: `python -c "from src.config import load_config; load_config()"`
- [x] Mark completed

---

### Task 4: Add new high-value sources with priorities

Добавляем 13 новых источников, распределённых по категориям. Каждый идёт с `priority` в соответствии
с качеством и специализацией.

**Новые источники:**

| Источник | URL | Категория | priority |
|---|---|---|---|
| High Scalability | `http://feeds.feedburner.com/HighScalability` | Architecture & Distributed Systems | 5 |
| Brendan Gregg's Blog | `https://www.brendangregg.com/blog/rss.xml` | Architecture & Distributed Systems | 5 |
| Increment Magazine | `https://increment.com/feeds/all.rss` | Architecture & Distributed Systems | 5 |
| SRE Weekly | `https://sreweekly.com/feed/` | Architecture & Distributed Systems | 4 |
| Meta Engineering | `https://engineering.fb.com/feed/` | Company Engineering | 4 |
| Monzo Tech Blog | `https://monzo.com/blog/technology/feed` | Banking & Fintech | 5 |
| Adyen Tech Blog | `https://www.adyen.com/knowledge-hub/rss.xml` | Payments & Fintech | 4 |
| Plaid Blog | `https://plaid.com/blog/rss.xml` | Payments & Fintech | 4 |
| Confluent Blog | `https://www.confluent.io/blog/feed/` | Data & Databases | 4 |
| TimescaleDB Blog | `https://www.timescale.com/blog/rss/` | Data & Databases | 4 |
| Prometheus Blog | `https://prometheus.io/blog/feed.xml` | Platform Engineering | 4 |
| OpenTelemetry Blog | `https://opentelemetry.io/blog/feed.xml` | Platform Engineering | 4 |
| Google Research Blog | `https://research.google/blog/rss/` | AI Engineering | 4 |

- [x] В `config.yaml`: добавить все 13 источников из таблицы с указанными `priority` значениями
- [x] Mark completed

---

### Task 5: Integration, validation and docs

Финальная проверка: тесты, конфиг, документация.

- [ ] Запустить `pytest tests/ -v` — все тесты зелёные
- [ ] Запустить `ruff check src/ tests/` — без ошибок
- [ ] Запустить smoke-test конфига и убедиться в разумном числе источников (ожидаем 45–55 enabled)
- [ ] Проверить что в `config.yaml` есть блок документации к полю `priority` (по аналогии с `enabled`)
- [ ] В `README.md`: обновить количество источников, если упомянуто явно
- [ ] Mark completed
