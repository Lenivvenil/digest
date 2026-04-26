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
| TrialDemoted | Source | Radar.collector |
| SourceDiscovered | PendingSource | Discovery |
| SourceApproved | PendingSource | Discovery |
| SourceRejected | PendingSource | Discovery |

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
| DiscoverSources | PendingSource (creates) | Discovery |
| ApproveSource | PendingSource → Source | Discovery |
| RejectSource | PendingSource | Discovery |

**Lilac — политики (с модулем-исполнителем):**
- Когда FeedbackReceived → UpdateSourcePriority (с 14-дневным затуханием). Исполнитель: Radar.collector при следующем pipeline-запуске. ¹ Ownership размазан: Delivery записывает, Radar применяет — hot spot #3.
- Когда Trial активен 7+ дней → EvaluateTrialSource (graduated/demoted). Исполнитель: Radar.collector.
- Когда Article старше 48 часов → ArticleFiltered. Исполнитель: Radar.collector.
- Когда SourceApproved → AddSourceToConfig (добавляет trial-блок в config.yaml) → TrialStarted. Исполнитель: Discovery. ² Config.yaml мутируется runtime-ом — hot spot #1.

**Yellow — агрегаты:**
- **Source** — владеет: user_priority, enabled. Runtime-состояние (trial_started, graduated, demoted) хранится в `SourceStateStore` (`.cache/source_state.json`) — не на агрегате. ADR-0003. Эмитирует: TrialStarted, TrialGraduated, TrialDemoted, SourcePriorityUpdated. Команды: EvaluateTrialSource, UpdateSourcePriority.
- **Article** — владеет: hash (identity), relevance_score, age, source_ref, delivered_flag. Эмитирует: ArticleIngested, ArticleFiltered. Команды: FetchFeeds.
- **PendingSource** — владеет: name, url, category, discovered_at, source_hash. Lifecycle: discovered → approved/rejected. Утверждение через Telegram-кнопки. Одобрение мутирует `config.yaml` (добавляет trial-блок) и запускает TrialStarted. Команды: ApproveSource, RejectSource.

> **Trial state вне Source (ADR-0003):** trial_started, graduated, demoted хранятся в SourceStateStore, а не в Source aggregate. Это позволяет config.yaml оставаться декларативным. Если появится потребность transact trial state вместе с другими полями Source — пересмотреть.

**Green — read models:**
- SourceLeaderboard (для UI настройки приоритетов — будущее)
- DigestHistory (для Obsidian — формат .md подчинён ожиданиям Obsidian, см. Context Map)

**Red — горячие точки:**
1. **Source dual-storage**: `config.yaml` мутируется двумя независимыми runtime-путями: (a) `apply_trial_decisions()` в `source_scorer.py` пишет trial/enabled-флаги, (b) `add_source_to_config()` в `discovery.py` добавляет новые trial-блоки. ADR-0003 перенёс часть состояния в `.cache/source_state.json`, но оба write-пути в config.yaml остались. Нет единого владельца. → Issue #37.
2. **Article без явного lifecycle**: нет состояний raw → scored → delivered → archived. Дедупликация через hash в кеше — ad-hoc решение. → Issue #39.
3. **Feedback decay owner**: логика затухания размазана между Delivery (запись feedback) и Radar (применение при следующем запуске). Политика задекларирована, но исполнитель распределён. → Issue #40.
4. **Radar→Delivery contract drift**: CategorySummary расширяется набором ArticleSummary для рендера индивидуальных постов в Telegram. Delivery начинает зависеть от структурированного списка статей с per-article саммари в дополнение к агрегированному тексту категории. Без явного внутрифазового контракта изменения в Radar могут молча ломать Delivery. → Issue #29.

---

## Boundary

**В scope:**
- Сбор статей из RSS (Radar)
- Фильтрация и оценка релевантности по контексту пользователя
- Дедупликация Article по hash (identity state — внутри BC)
- Генерация нарративов и поиск контрсигналов (Irritator)
- Доставка через Telegram и запись в Obsidian (Delivery)
- Адаптивная система приоритетов источников (trial + feedback)
- Обнаружение и одобрение новых источников (Discovery)

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
| **Source** | Информационный канал (RSS URL), за которым система наблюдает; имеет user_priority (задан вручную). Runtime-приоритет вычисляется из Feedback при каждом запуске — не хранится на Source. ADR-0003. | Feed, Channel |
| **Article** | Единица контента, прошедшая границу BC: имеет relevance_score, возраст и принадлежность к Source. До ingestion — это XML-запись, не Article. | Item, Post, Entry |
| **Trial** | Испытательный период нового источника (pending → active → graduated/demoted). Runtime-состояние хранится в SourceStateStore, не в Source aggregate (ADR-0003). | Probation, Test |
| **Narrative** | Доминирующая тема, выявленная из группы Articles одного pipeline-запуска. Не персистируется — существует только в памяти во время запуска. | Topic, Theme, Category |
| **Feedback** | Явный сигнал пользователя (👍/👎) об Article в Telegram; влияет на эффективный приоритет Source с экспоненциальным затуханием за 14 дней. Feedback без decay — устаревший сигнал. | Vote, Rating, Reaction |
| **Digest** | Результат одного pipeline-запуска: набор Narratives с Articles, доставленный через один или несколько каналов (Telegram + Obsidian) в конкретный момент времени. Не является архивом — это event. | Report, Summary, Run |
| **CounterSignal** | Статья или дискуссия из внешней платформы (HN, Reddit, arXiv, …), найденная Irritator-ом как альтернативная точка зрения на Narrative. Не является частью основного Source-набора. | Alternative, Counterpoint |
| **PendingSource** | Кандидат в Sources, обнаруженный Discovery-фазой и ожидающий одобрения оператором через Telegram. Существует только до момента Approve/Reject — не становится Source напрямую, а инициирует TrialStarted. | Candidate, Suggestion |
| **CategorySummary** | Сгруппированный результат фазы Radar для одной таксономической категории источников (например, «AI & LLM», «Security» — классификация Sources, а не тематика Narratives): объединяет аналитический текст по категории и набор ArticleSummary для индивидуальной подачи. Является контрактом передачи данных от Radar к Delivery внутри одного pipeline-запуска. | Section, Bucket |
| **ArticleSummary** | Сгенерированное LLM саммари одной конкретной статьи в 2-3 предложения, объясняющее почему статья важна для Technology Architect, а не просто пересказывающее заголовок. Часть CategorySummary; используется Delivery для рендера индивидуальных постов (отдельных Telegram-сообщений с кнопками голосования). | Article comment, Blurb |
| **Perspectives** | Три аналитические точки зрения на наиболее значимые события в категории — Оптимист (🟢), Скептик (🔴), Реалист (⚖️). Должны представлять принципиально разные цепочки рассуждений, а не разный тон. Часть аналитического текста CategorySummary, не отдельная сущность. Применяются только в развёрнутых стилях дайджеста, не в кратком. | Viewpoints, Stances, Voices |

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

**Issue #39** — Article lifecycle (act when replay/trend analysis is needed). **Issue #40** — Feedback decay owner consolidation (act when decay formula changes). Both are correctly deferred with explicit trigger conditions.
