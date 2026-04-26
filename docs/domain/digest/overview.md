# Bounded Context: Digest

_Discovered: 2026-04-26. Domain discovery session with domain-researcher._

---

## Actors

| Actor | Type | Role |
|-------|------|------|
| User | Human | Configures sources, reads digest, votes on articles |
| RSS Feed Providers | External system | Publishes Atom/RSS feeds consumed by Radar |
| GitHub Actions | Scheduler | Triggers daily pipeline and weekly discovery |
| Telegram Bot API | External system | Delivers digest to user, receives vote callbacks |
| Obsidian Vault | External system | Consumes generated .md files per agreed format |

---

## Events (Event Storming)

**Orange — что произошло (с указанием агрегата-владельца):**

| Event | Aggregate owner | Module |
|-------|----------------|--------|
| FeedFetched | — (process event, no aggregate) | Radar |
| ArticleIngested | Article | Radar.collector |
| ArticleFiltered | Article | Radar.collector |
| SummaryGenerated | — (process event, no aggregate) | Radar.summarizer |
| NarrativeExtracted | — (transient, not persisted) | Irritator |
| CounterSignalFound | — (transient, not persisted) | Irritator |
| DigestDelivered | — (process event, no aggregate) | Delivery |
| FeedbackReceived | Source¹ | Delivery.telegram |
| SourcePriorityUpdated | Source | Radar.collector (next run) |
| TrialStarted | Source | Radar.collector |
| TrialGraduated | Source | Radar.collector |
| TrialDropped | Source | Radar.collector |

**Blue — команды (с целевым агрегатом):**

| Command | Target aggregate | Module |
|---------|----------------|--------|
| FetchFeeds | Article (creates) | Radar.collector |
| SummarizeCategory | — | Radar.summarizer |
| ExtractNarratives | — | Irritator |
| GenerateQueries | — | Irritator |
| SearchCounterSignals | — | Irritator.sources |
| DeliverDigest | — | Delivery |
| UpdateSourcePriority | Source | Radar.collector |
| EvaluateTrialSource | Source | Radar.collector |

**Lilac — политики (с модулем-исполнителем):**
- Когда FeedbackReceived → UpdateSourcePriority (с 14-дневным затуханием). Исполнитель: Radar.collector при следующем pipeline-запуске. ¹ Ownership размазан: Delivery записывает, Radar применяет — hot spot #3.
- Когда Trial активен 7+ дней → EvaluateTrialSource (graduated/dropped). Исполнитель: Radar.collector.
- Когда Article старше 48 часов → ArticleFiltered. Исполнитель: Radar.collector.

**Yellow — агрегаты:**
- **Source** — владеет: user_priority, system_priority, trial_state (value object на Source, не отдельный aggregate), enabled. Эмитирует: TrialStarted, TrialGraduated, TrialDropped, SourcePriorityUpdated. Команды: EvaluateTrialSource, UpdateSourcePriority.
- **Article** — владеет: hash (identity), relevance_score, age, source_ref, delivered_flag. Эмитирует: ArticleIngested, ArticleFiltered. Команды: FetchFeeds.

> **Trial как value object на Source:** Trial не является отдельным aggregate — у него нет независимого lifecycle. Его состояния (pending → active → graduated/dropped) принадлежат Source. Это решение принято явно; если появится потребность отслеживать Trial независимо от Source, пересмотреть.

**Green — read models:**
- SourceLeaderboard (для UI настройки приоритетов — будущее)
- DigestHistory (для Obsidian — формат .md подчинён ожиданиям Obsidian, см. Context Map)

**Red — горячие точки:**
1. **Source dual-storage**: `config.yaml` (декларация, human-edited) мутируется рантаймом — `apply_trial_decisions()` в `source_scorer.py:389–465` пишет `trial_started`, `trial: false`, `enabled: false` regex'ом прямо в YAML. Нет единого владельца — invariant не защищён. Фрейминг уточнён в ADR-0003 (поля `dynamic_sources.json`/`system_priority` в коде не существуют). → Issue #37.
2. **Article без явного lifecycle**: нет состояний raw → scored → delivered → archived. Дедупликация через hash в кеше — ad-hoc решение.
3. **Feedback decay owner**: логика затухания размазана между Delivery (запись feedback) и Radar (применение при следующем запуске). Политика задекларирована, но исполнитель распределён.

---

## Boundary

**В scope:**
- Сбор статей из RSS (Radar)
- Фильтрация и оценка релевантности по контексту пользователя
- Дедупликация Article по hash (identity state — внутри BC)
- Генерация нарративов и поиск контрсигналов (Irritator)
- Доставка через Telegram и запись в Obsidian (Delivery)
- Адаптивная система приоритетов источников (trial + feedback)

**Намеренно вне scope:**
- Полнотекстовое хранилище сырых статей (RSS-провайдеры ответственны за контент; BC хранит только hash для дедупликации)
- Политика уведомлений пользователя (Telegram's concern)
- Полнотекстовый поиск по истории дайджестов (Obsidian's concern)
- Аутентификация и авторизация (нет пользователей, кроме владельца)

**Термин, меняющий смысл на границе:**
`Article` — на стороне RSS это XML-запись без контекста. После пересечения границы BC Article приобретает `relevance_score`, `age`, `source_ref` и становится объектом с lifecycle. Пересечение = момент ingestion в Radar.collector.

---

## Ubiquitous Language

| Term | Business definition | Aliases to avoid |
|------|---------------------|------------------|
| **Source** | Информационный канал (RSS URL), за которым система наблюдает; имеет user_priority (задан вручную) и system_priority (вычислен из feedback). Содержит trial_state как value object. | Feed, Channel |
| **Article** | Единица контента, прошедшая границу BC: имеет relevance_score, возраст и принадлежность к Source. До ingestion — это XML-запись, не Article. | Item, Post, Entry |
| **Trial** | Value object на Source, описывающий испытательный период нового источника (pending → active → graduated/dropped). Не существует независимо от Source. | Probation, Test |
| **Narrative** | Доминирующая тема, выявленная из группы Articles одного pipeline-запуска. Не персистируется — существует только в памяти во время запуска. | Topic, Theme, Category |
| **Feedback** | Явный сигнал пользователя (👍/👎) об Article в Telegram; влияет на system_priority Source с экспоненциальным затуханием за 14 дней. Feedback без decay — устаревший сигнал. | Vote, Rating, Reaction |
| **Digest** | Результат одного pipeline-запуска: набор Narratives с Articles, доставленный через один или несколько каналов (Telegram + Obsidian) в конкретный момент времени. Не является архивом — это event. | Report, Summary, Run |
| **CounterSignal** | Статья или дискуссия из внешней платформы (HN, Reddit, arXiv, …), найденная Irritator-ом как альтернативная точка зрения на Narrative. Не является частью основного Source-набора. | Alternative, Counterpoint |

---

## Context Map

| Upstream BC / System | Pattern | Notes |
|----------------------|---------|-------|
| RSS Feed Providers | **Conformist** | Потребляем их формат (Atom/RSS) через feedparser. Мы подстраиваемся — они не знают о нас. |
| Telegram Bot API | **Conformist** | Используем их Published Language (MarkdownV2, inline keyboards, callback queries). Мы на их условиях. |
| HN / Reddit / arXiv / dev.to / Lobsters | **Conformist** | Читаем их публичные API без контракта. Breakage возможен при изменении их API. |
| Obsidian Vault | **Customer-Supplier** | Obsidian — downstream customer: формат .md (заголовки, ссылки, теги) подчинён его ожиданиям. Digest — supplier. DigestHistory — Published Language этой связи. |

---

## Areas of Unclear Ownership (documented, not ticketed)

Эти смысловые расхождения зафиксированы как технический долг — не как active issues:

- **Feedback decay owner**: логика затухания (14 дней) объявлена в политике, но исполнена в двух модулях: Delivery записывает feedback, Radar читает и применяет при следующем запуске. Единого aggregate нет.
- **Article lifecycle**: нет явных состояний между ingestion и archival. Если появится потребность в replay или ручном редактировании — нужна модель.
- **Narrative transience**: не персистируется между запусками. Если понадобится трендовый анализ — придётся добавлять persistence.

---

## Recommended Next Action

**Issue #37** — консолидировать Source state (устранить dual-storage split). Acceptance criteria в теле issue.
