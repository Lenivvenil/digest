# 0003. Split Source state: static config vs runtime cache

* Status: proposed
* Date: 2026-04-26
* Deciders: venil
* Tags: architecture, data-model, config, source-management

## Context and Problem Statement

Issue #37 формулирует проблему как «dual-storage Source state: `user_priority` в `config.yaml` против `system_priority` в `.cache/dynamic_sources.json`». **Это описание не соответствует коду.** В репозитории нет файла `dynamic_sources.json`, и нет поля `system_priority` на `SourceConfig` (`digest/config.py:80–89`). Реальное нарушение инварианта находится в `digest/source_scorer.py:389–465`: функция `apply_trial_decisions()` мутирует `config.yaml` во время рантайма через regex-манипуляцию строк YAML — выставляет `trial: false` для graduated-источников, `enabled: false` для demoted, и `trial_started: <date>` для новых trial. Domain overview (`docs/domain/digest/overview.md` Hot Spot #1) тоже ссылается на несуществующий `dynamic_sources.json` — оба артефакта описывают симптом неточно. Этот ADR опирается на состояние кода, а не на формулировку issue или overview.

Симптом: `config.yaml` одновременно используется как human-edited декларация намерения **и** как хранилище мутируемого рантайм-состояния, записываемого GitHub Actions. Это нарушает явно ожидаемый инвариант «config.yaml read-only при работе движка» (унаследованный от ADR-0002, где config.yaml физически живёт в приватном `digest-prod` и редактируется человеком). Текущая реализация — regex-мутация YAML с `.bak`/`.tmp` файлами — также хрупка: при изменении форматирования YAML вручную (например, добавление комментариев) парсинг блока источника может промахнуться.

Решение требуется **сейчас**, до следующего pipeline-запуска с активным trial-источником, иначе любой merge config-правок в `digest-prod` рискует конфликтом с автоматической записью.

## Decision Drivers

* **Инвариант config.yaml** — после ADR-0002 `config.yaml` физически разделён в `digest-prod`. Если движок продолжает писать в него regex-ом, инвариант «human-edited declaration» нарушается структурно, и любой ручной diff в `digest-prod` может конфликтовать с автоматической записью на следующем cron'е.
* **Принцип 4 (Knowledge в инструментах, не в памяти)** (`docs/principles.md`) — всё значимое в git, а не в неявных побочных эффектах. Сейчас факт «движок пишет в config» нигде не задокументирован, кроме самого кода.
* **Хрупкость regex-парсинга YAML** — `_find_source_block` и `_set_field_in_block` в `source_scorer.py` опираются на отступы и структуру строк. Любое нестандартное форматирование (комментарии в середине блока, anchor/alias YAML, многострочные значения) ломает их незаметно. Cache-файл с явной JSON-схемой такого риска не имеет.
* **Domain language: Trial как value object на Source** (`docs/domain/digest/overview.md`, Yellow): trial_state — часть Source aggregate. Mutable trial state логично хранить рядом с другим mutable state (stats, feedback), а не в декларативном config.
* **Engine portability per ADR-0002** — engine не должен предполагать, что `config_path` writable. Cache-каталог уже writable по контракту. Любая writable-зависимость движка должна быть явной и единственной — cache.
* **Тестируемость** — round-trip property test из acceptance criteria #37 невозможен, пока state мутируется regex-ом по физическому файлу: тест требует двух файлов (config + cache), а сейчас файл один и read+write через regex.

## Considered Options

* **Option A (literal, as framed in issue #37)** — Создать `.cache/source_state.json` с **только** полем `trial_started` per source. Поля `trial: bool` и `enabled: bool` остаются в `config.yaml` как human declarations. `apply_trial_decisions()` пишет `trial_started` в cache вместо config; `trial=true → trial=false` (graduate) и `enabled=true → enabled=false` (demote) **продолжают писаться в config.yaml**, либо не пишутся вовсе.
* **Option A' (completed)** — Как A, но **все три** мутации (`trial_started`, graduate-outcome, demote-outcome) идут в cache. `config.yaml` становится строго read-only во время рантайма. Поля `trial: bool` и `enabled: bool` в config — declarations, поверх которых cache накладывает overrides (`graduated`, `demoted`).
* **Option B (full state split)** — Создать `.cache/source_state.json` с полным mutable state per source: `{trial_started, graduated, demoted, effective_enabled_override, schema_version}`. `config.yaml` truly read-only. Loader в `config.py` строит `SourceConfig` из config + накладывает overrides из cache (отдельная фаза merge). `apply_trial_decisions()` переименовывается в `apply_trial_decisions_to_cache()` и пишет cache-файл атомарно через `atomic_json_write` (как `feedback.py`).
* **Option C (config write only for trial_started)** — Удалить `apply_trial_decisions()` write-path для graduate/demote: graduated/demoted переносятся в cache как outcomes. `trial_started` остаётся в `config.yaml`, но write-path для него удаляется — человек обязан вручную проставить дату при объявлении нового trial-источника. `config.yaml` write никогда не происходит из движка.

## Decision Outcome

Chosen option: **Option B — Full state split**.

Только B и A' закрывают driver «инвариант config.yaml read-only» полностью. A (literal) оставляет два из трёх write-путей в config, не решая проблему. C решает write-путь, но смещает UX: человек обязан помнить дату при объявлении trial — новая silent failure mode (забыл дату → trial никогда не оценивается).

Между B и A' B выбран по двум критическим drivers:
1. **Тестируемость** — явная schema с `schema_version` позволяет migration tests и round-trip property test из acceptance criterion #37; A' не имеет version field и не защищена от schema drift.
2. **Domain language** (`docs/principles.md`, Принцип 4) — Trial как value object сериализуется целиком в одной структуре (`SourceStateStore`), а не двумя полями в config + одним в cache. Split state требует объяснения при дебаге каждого incident'а.

B повторяет паттерн `FeedbackStore` (`digest/feedback.py`) — тот же стиль `dataclass + atomic_json_write + graceful degradation`. Консистентность с уже работающим cache-механизмом снижает cognitive load внедрения.

Acknowledged costs: migration of existing `trial_started` values, merge-семантика config + cache (см. Bootstrap and Migration), четвёртый cache-файл.

### Reversibility

Решение **полностью обратимо** до тех пор, пока `source_state.json` не накопил >3 месяцев истории graduated/demoted outcomes в `digest-prod`. Откат до этого порога: один скрипт, переносящий `trial_started` из cache обратно в config.yaml, + удаление `source_state.json` из `.cache/` + возврат `apply_trial_decisions()` к regex-мутации config. Оценка: 1–2 часа. После 6 месяцев с накопленными outcomes откат дороже: нужно вручную перенести graduated/demoted decisions обратно в config-поля или принять потерю истории. Оценка: 4–8 часов, зависит от числа источников.

**Не является обратимым** без потери данных: если `source_state.json` содержит outcomes, которых нет в config (например, источник был demoted и удалён из config — только cache знает почему). Это граница: migration forward дешевле rollback.

Ссылки на принципы (`docs/principles.md`):
* Принцип 4 (Knowledge в инструментах) — cache-схема должна быть документирована, а не выводима только из кода.
* Раздел «что значит архитектурно-значимо» → пункт «меняется модель данных» — этот ADR именно про это.

### Positive Consequences (для Option B как ведущего варианта)

* Инвариант «config.yaml read-only во время рантайма» становится структурным, не decorative — engine физически не открывает config на запись.
* Round-trip property test становится возможным: load config + load cache → mutate state → save cache → reload → equality (acceptance criterion #37).
* Хрупкий regex-парсинг YAML (`_find_source_block`, `_set_field_in_block`, `_remove_field_in_block` — ~80 строк) удаляется; заменяется на JSON-сериализацию через `dataclass`.
* Migration path для новых полей runtime-state становится дешёвым (добавить поле в dataclass + обновить graceful-degradation в loader), не требует изменений в config-парсере.
* Engine perfectly portable: единственный writable-путь — cache_dir, единственный read-path config.yaml — `load_config()`. Соответствует ADR-0002 контракту.

### Negative Consequences (для Option B)

* **Migration cost**: существующие `trial_started` значения в `digest-prod/config.yaml` нужно перенести в cache. Либо one-shot import на первом запуске (риск двойного источника правды до cleanup), либо ручной cleanup (риск забыть). Без явного миграционного шага состояние временно дублируется.
* **Cognitive load**: «эффективное состояние Source = config + cache merge» — два места для дебага вместо одного. При расследовании «почему source X disabled» нужно проверить и config, и cache. Сейчас всё в одном файле (хрупком, но одном).
* **Schema evolution discipline**: cache-файл коммитится в `digest-prod` через GitHub Actions (как `feedback.json`, `source_stats.json`), значит breaking changes схемы требуют migration script или explicit `schema_version` поля. Сейчас config-changes требуют только YAML edit.
* **`enabled: false` override в cache vs config**: создаёт двойственность «человек хочет включить, но cache всё ещё override'ит как demoted». Нужно явное правило: human edit config → invalidates cache override (как? через timestamp comparison? manual flag?).
* **Несовместимость с существующим cache-форматом**: новый `source_state.json` — четвёртый cache-файл (после `feedback.json`, `source_stats.json`, и `articles_seen.json` для дедупликации). Каждый — отдельный dataclass с graceful-degradation; total surface area cache-IO растёт.
* **Acceptance criterion #37 «`config.yaml` schema documented»**: требует написать и поддерживать раздел в README или CLAUDE.md с явным списком «static fields / forbidden runtime fields». Этот документ может разойтись с кодом, если не привязать его проверкой.
* **Bootstrap edge cases**: первый запуск без `source_state.json` (после миграции в `digest-prod`) — loader должен корректно инициализировать пустой store. Если cache повреждён — graceful start с warning (как `feedback.py` уже делает). Это код, который нужно тестировать.

### Bootstrap and Migration (mandatory for any chosen option)

Эти вопросы должны быть закрыты в `/plan` до начала работы (контракт, не реализация):

1. **Первый запуск без cache-файла**: loader возвращает empty `SourceStateStore`; config.yaml — единственный источник для `trial_started`. На первом mutating-вызове новой write-функции cache-файл создаётся.
2. **Migration существующих `trial_started` из `digest-prod/config.yaml`**: предлагается one-shot migration helper (форма TBD в `/plan` — CLI флаг, отдельный entry-point или script), который читает config, переносит `trial_started` значения в cache, и оставляет в config только декларацию `trial: true`. После одного успешного запуска оператор вручную удаляет `trial_started` из `digest-prod/config.yaml`. **Не автоматизировать удаление** — это write в config, ровно тот invariant break, который мы устраняем.
3. **Schema versioning**: `source_state.json` начинается с `{"schema_version": 1, "sources": {...}}`. Loader проверяет `schema_version`, при unknown version — warning + start fresh (как `feedback.py` обрабатывает corrupted JSON).
4. **Conflict resolution config vs cache**: при conflict (`enabled: false` в config + `demoted: true` в cache) — config wins для declarations, cache wins для outcomes. Конкретный merge-алгоритм фиксируется в `/plan`.

## Pros and Cons of the Options

### Option A (literal)

* Good, потому что минимальный diff: только один новый field мигрирует в cache.
* Good, потому что не требует написания merge-логики в loader — config остаётся плоским источником для declarations + outcomes.
* **Bad, потому что не решает основной driver**: два из трёх write-путей в config (graduate → `trial: false`, demote → `enabled: false`) остаются. Issue #37 не закрывается — invariant нарушен в 2/3 случаев.
* Bad, потому что при чтении кода непонятно, почему один runtime-write мигрирует, а два других — нет; асимметрия требует обоснования в комментариях.
* Bad, потому что хрупкий regex-парсер `_find_source_block` нельзя удалить — он всё ещё нужен для двух оставшихся write-путей.

### Option A' (completed)

* Good, потому что закрывает все три write-пути в config — invariant восстановлен полностью.
* Good, потому что схема cache минимальна: `{trial_started, graduated, demoted}` per source, без дополнительных полей.
* Good, потому что migration проще, чем у B: переносится одно поле (`trial_started`), graduated/demoted рождаются в cache на следующем запуске.
* Bad, потому что smear logic между config (`trial: bool`, `enabled: bool` декларации) и cache (`graduated`, `demoted` outcomes) — два места для дебага «почему source выключен».
* Bad, потому что conflict semantics нетривиальны: что значит `trial: false` в config + `graduated: false` в cache? (Никогда не был trial, или был и graduated в прошлом, но cache потерян?) Требует явного правила и теста.
* Bad, потому что без `schema_version` поля migration на следующее изменение схемы потребует ad-hoc детекции.

### Option B (full state split)

См. Positive/Negative Consequences выше — это leading option.

* Good, потому что повторяет паттерн `FeedbackStore` — code-shape известен, тесты тривиальны.
* Good, потому что Trial как value object сериализуется целиком в одной структуре, что соответствует domain language.
* Good, потому что добавление новых mutable полей (например, `last_evaluated_at`, `evaluation_count`) не требует изменений config-парсера.
* Bad, потому что surface area cache растёт: четвёртый JSON-файл со своей graceful-degradation логикой.
* Bad, потому что merge-семантика config + cache требует явного дизайна (positive/negative выше).
* Bad, потому что migration требует one-shot helper или manual cleanup существующих `trial_started` в `digest-prod/config.yaml`.

### Option C (config write only for trial_started, with manual stamp)

* Good, потому что удаляет write-path в config полностью — самый строгий read-only invariant.
* Good, потому что cache-схема минимальна: только `graduated` / `demoted` outcomes; `trial_started` остаётся декларацией в config.
* Bad, потому что **смещает UX**: человек обязан вручную проставить `trial_started` при объявлении нового источника. Текущая семантика «added trial source без даты → пайплайн проставит» исчезает. Это новая failure mode: забыл дату → trial никогда не оценивается, источник висит как permanent trial. Без напоминания (issue, lint-rule) ошибка тихая.
* Bad, потому что `trial_started` концептуально mutable (set once, но всё равно mutation от unset → set), и хранение его в declarative config нарушает domain language: trial_state — value object, а value object с partial state в двух местах — code smell.
* Bad, потому что migration кажется простой («оставить как есть»), но на деле переносит обязанность проставлять дату на оператора без compensating control.
* Bad, потому что хотя regex-парсер удаляется (write-path исчезает), declarative `trial_started` в config поощряет повторение паттерна «human stamps date → engine reads it» для будущих value-object полей. Domain-language regression закрепляется как локальный convention.

## Confirmation

1. **Round-trip property test** (acceptance criterion #37): `tests/test_source_state_roundtrip.py` загружает config + cache, мутирует runtime-поле (например, `trial_started`), сохраняет cache, перезагружает, сравнивает. Проходит на CI после merge.
2. **Engine config write surface = 0**: `grep -rn "config_path.*open.*['\"]w['\"]" digest/` возвращает 0 результатов после мержа. Проверка автоматизируема через ruff custom rule или pre-commit grep.
3. **Schema document existence**: `README.md` или `digest/config.py` docstring содержит явный список «runtime-only fields, forbidden in config.yaml». Reviewer проверяет на PR.
4. **Bootstrap correctness**: integration-тест на пустой `cache_dir` (без `source_state.json`) — pipeline проходит без ошибок, на втором запуске cache-файл создан.
5. **Migration helper test**: one-shot migration helper (форма TBD в `/plan`) на fixture с `trial_started` в config — выводит план миграции в dry-run, пишет cache в нормальном режиме, **не трогает config** ни в одном случае.

## Re-visit Trigger

1. **Schema breakage**: на первом изменении схемы (новое поле в `source_state.json`) graceful-degradation в loader не сработала — pipeline упал на cron. Если повторяется → нужен formal migration framework, не graceful-degradation.
2. **Cache corruption frequency**: за квартал > 2 случаев corrupted `source_state.json` в `digest-prod` (например, прерванная запись из-за GHA timeout). Текущий `atomic_json_write` должен это покрывать — если не покрывает, паттерн не работает.
3. **Source aggregate growth**: число источников в `config.yaml` превысило 50, ИЛИ в `source_state.json` добавляется второй list-typed field (например, `evaluation_history`). При достижении любого из этих порогов JSON-файл становится неудобным: O(N) поиск, diff нечитаем, atomic-write дорог. Тогда rethink: либо отдельный модуль `digest/source/`, либо вынос Source state в SQLite.
4. **Multi-instance setup**: если когда-нибудь второй instance движка запускается параллельно (race на cache), JSON-файл с atomic_json_write становится недостаточным — нужен lock или отдельный backend (SQLite WAL, Redis). На текущей topology (один cron, одна машина) это не trigger.
5. **Принципиальное изменение ADR-0002**: если engine/instance split откатывается (config.yaml снова в engine repo) — этот ADR теряет один из drivers и требует пересмотра.

## Links

* Связанный issue: #37 (Engine: разделить static config и runtime state источников)
* Предыдущий: ADR-0002 — Split engine and instance repositories (определил инвариант, который этот ADR делает структурным)
* Domain context: `docs/domain/digest/overview.md` Hot Spot #1 (формулировка требует обновления — описывает несуществующий `dynamic_sources.json`; рекомендуется отдельный issue для domain-overview cleanup)
* Принципы: `docs/principles.md` — Принцип 4 (Knowledge в инструментах), раздел «что значит архитектурно-значимо» (пункт «меняется модель данных»)
* Code references:
  * `digest/source_scorer.py:389–465` — `apply_trial_decisions()` (write-path в config.yaml)
  * `digest/source_scorer.py:324–386` — regex helpers `_find_source_block`, `_set_field_in_block`, `_remove_field_in_block` (удаляются в B и A')
  * `digest/config.py:80–89` — `SourceConfig` dataclass (mutable поля, подлежащие split'у)
  * `digest/feedback.py` — reference pattern для cache-store (FeedbackStore + atomic_json_write + graceful degradation)
