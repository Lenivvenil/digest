# Plan: Issue #37 — Consolidate Source state (eliminate dual-storage split)

## 1. Problem restatement

`digest/source_scorer.py:389–465` содержит функцию `apply_trial_decisions()`, которая мутирует `config.yaml` во время рантайма через regex-манипуляцию строк YAML (`_find_source_block`, `_set_field_in_block`, `_remove_field_in_block`). Это делает `config.yaml` одновременно human-edited декларацией намерения и хранилищем мутируемого рантайм-состояния. Нарушение инварианта имеет три проявления: (1) любой ручной git diff в `digest-prod` может конфликтовать с автоматической записью на следующем cron; (2) движок требует `config_path` writable, что нарушает ADR-0002 контракт; (3) round-trip property тест (acceptance criterion) невозможен, пока state и config — один файл.

## 2. Affected bounded contexts and files

**Bounded Context: Digest** (единственный BC; domain overview `docs/domain/digest/overview.md`)

Затронутые агрегаты по domain overview:
- **Source** (Yellow aggregate) — владеет `trial_state` как value object; сейчас персистирует его через regex в config.yaml; после изменения — через `SourceStateStore` в `.cache/source_state.json`

Файлы:

| Файл | Тип изменения |
|------|--------------|
| `digest/source_scorer.py` | Крупная переработка: новые `SourceStateEntry`, `SourceStateStore`, `load_source_state()`, `save_source_state()`, `apply_trial_decisions_to_cache()`; удаление `apply_trial_decisions()`, `_find_source_block()`, `_set_field_in_block()`, `_remove_field_in_block()` |
| `digest/source_scorer.py:evaluate_trial_sources()` | Смена сигнатуры: принимает `SourceStateStore` вместо читки `source.trial_started` из `SourceConfig` |
| `digest/config.py` | `SourceConfig`: удалить поле `trial_started`; добавить метод `Config.effective_sources(source_state)` |
| `digest/main.py` | Загрузка/сохранение `SourceStateStore`; замена `apply_trial_decisions()` на `apply_trial_decisions_to_cache()`; замена `config.enabled_sources` на `config.effective_sources(source_state)` в adaptive-ветке |
| `tests/test_source_scorer.py` | Удалить тесты YAML-хелперов и `apply_trial_decisions`; добавить тесты SourceStateStore CRUD, round-trip, `apply_trial_decisions_to_cache` |
| `tests/test_source_state_roundtrip.py` | Новый файл — property round-trip тест (acceptance criterion) |
| `tests/test_config.py` | Убрать `trial_started` из fixture-данных источников |
| `tests/test_main.py` | Обновить mocking: вместо патча `apply_trial_decisions` — патч cache-операций |

## 3. Considered approaches

### Approach A — Merge at load time

`load_config()` принимает опциональный `source_state: SourceStateStore | None` и при загрузке применяет overrides к `SourceConfig.enabled` на основе `demoted` флага.

**Trade-offs:**
- ✓ Единое место merge — весь downstream код видит уже "правильные" источники через `config.enabled_sources`
- ✗ `config.py` получает зависимость от cache-типов из `source_scorer.py` → circular import риск (source_scorer уже импортирует `SourceConfig` из config.py)
- ✗ Нарушает принцип разделения ответственностей: config-loader не должен знать о runtime overrides

### Approach B — Метод `Config.effective_sources(source_state)` (ADR-0003)

`config.py` остаётся чистым loader'ом без знания о cache. Добавляется метод `Config.effective_sources(source_state: SourceStateStore) -> list[SourceConfig]`, который фильтрует demoted источники. Runtime-код в `main.py` использует этот метод вместо `config.enabled_sources` в adaptive-ветке.

**Trade-offs:**
- ✓ Нет circular import — `Config` не знает о `SourceStateStore`
- ✓ `config.enabled_sources` остаётся работающим для non-adaptive flow и тестов без state
- ✓ Merge-семантика явна: `demoted` из cache + `enabled` из config → effective list
- ✗ Два метода (`enabled_sources` и `effective_sources`) — вызывающий должен знать, какой использовать; документировать в docstring

## 4. Chosen approach and why

**Approach B**, per ADR-0003 (Option B — Full state split), `docs/decisions/0003-source-state-split.md`.

ADR зафиксировал: только Option B (и A') полностью закрывают инвариант config.yaml read-only. Между ними B выбран из-за наличия `schema_version` (защита от schema drift) и паттерна FeedbackStore (консистентность с существующим cache-механизмом).

**Conflict resolution** (из ADR-0003 Bootstrap section): config wins для declarations; cache wins для outcomes. При конфликте (`enabled: true` в config + `demoted: true` в cache) — cache outcome применяется.

**Конкретные изменения:**

```
SourceConfig:
  - trial_started  ← удалить

SourceStateEntry (новый dataclass):
  + trial_started: str | None = None
  + graduated: bool = False
  + demoted: bool = False

SourceStateStore (новый dataclass):
  + schema_version: int = 1
  + sources: dict[str, SourceStateEntry] = field(default_factory=dict)
  + методы: is_demoted(name), is_graduated(name), get_trial_started(name), set_trial_started(name, date), mark_graduated(name), mark_demoted(name)

Config (обновить):
  + effective_sources(source_state: SourceStateStore) -> list[SourceConfig]
    — возвращает источники, где enabled=True И NOT source_state.is_demoted(name)

source_scorer.py изменения:
  apply_trial_decisions()         → удалить (вместе с YAML-хелперами)
  _find_source_block()            → удалить
  _set_field_in_block()           → удалить
  _remove_field_in_block()        → удалить
  evaluate_trial_sources()        → принимает source_state: SourceStateStore
                                     источник считается "в испытании" только если:
                                     source.trial==True AND NOT is_graduated(name) AND NOT is_demoted(name)
  apply_trial_decisions_to_cache() → новая функция, мутирует SourceStateStore
  load_source_state(cache_dir)    → новая функция
  save_source_state(store, cache_dir) → новая функция, atomic_json_write

main.py изменения:
  + source_state = load_source_state(cache_dir)
  evaluate_trial_sources(...) ← добавить source_state аргумент
  apply_trial_decisions(...) → apply_trial_decisions_to_cache(source_state, promote, demote, needs_start, today)
  + save_source_state(source_state, cache_dir)
  config.enabled_sources → config.effective_sources(source_state) в adaptive-ветке evaluate
```

## 5. Test strategy

### Unit tests (обновление `tests/test_source_scorer.py`)

Удалить:
- `test_find_source_block_*` (3 теста) — функция удаляется
- `test_set_field_in_block_*` (2 теста) — функция удаляется
- `test_remove_field_in_block*` (2 теста) — функция удаляется
- все `test_apply_trial_decisions_*` — функция удаляется

Добавить:
- `test_load_source_state_missing_file` — возвращает пустой SourceStateStore
- `test_load_source_state_corrupted_json` — graceful degradation
- `test_load_source_state_unknown_schema_version` — warning + start fresh
- `test_source_state_store_roundtrip` — save → load → equality
- `test_apply_trial_decisions_to_cache_promote` — graduated=True, trial_started=None
- `test_apply_trial_decisions_to_cache_demote` — demoted=True
- `test_apply_trial_decisions_to_cache_needs_start` — инициализирует trial_started в store
- `test_evaluate_trial_sources_reads_from_state` — trial_started из SourceStateStore, не SourceConfig
- `test_evaluate_trial_sources_skips_graduated` — config trial=True + cache graduated=True → source не в needs_start, не в promote, не в demote (без этого graduated source зацикливается обратно в trial)

### Round-trip property test (новый файл `tests/test_source_state_roundtrip.py`)

Acceptance criterion #37: загрузить config + пустой cache → выставить `trial_started` в SourceStateStore → сохранить cache → перезагрузить → значение совпадает. Тест работает на `tmp_path`, без реального `config.yaml`.

### Обновление `tests/test_config.py`

- Убрать `trial_started` из всех fixture-dict источников
- Добавить `test_config_effective_sources_filters_demoted`

### Обновление `tests/test_main.py`

- Заменить `patch("digest.source_scorer.apply_trial_decisions")` → `patch("digest.source_scorer.apply_trial_decisions_to_cache")`
- Добавить патчи для `load_source_state` / `save_source_state`

### Coverage target

≥ 70% (текущий floor в `pyproject.toml`). Удаляемые тесты заменяются по объёму.

## 6. Risks and unknowns

1. **`enabled_sources` vs `effective_sources` call sites** — в non-adaptive flow `enabled_sources` корректен. Риск: в adaptive flow пропустить место и demoted source попадёт в пайплайн. **Mitigation:** grep `enabled_sources` перед commit.

2. **`evaluate_trial_sources` signature change** — читает `source.trial_started` из `SourceConfig` сейчас. Все callers — только `main.py:719`. mypy strict поймает несовместимость.

3. **`_process_pending_approvals` в `main.py:715`** — ✅ Проверено. Вызывает `add_source_to_config()` из `discovery.py:135` — добавляет новый одобренный пользователем источник. Это прокси для ручного действия, не trial state mutation. **Вне scope #37.** После этого PR config.yaml всё ещё пишется discovery-флоу. Отразить как known limitation в PR description.

4. **`trial_started` migration в `digest-prod`** — вне scope. Существующие источники с `trial_started` в `config.yaml` при первом запуске увидят `None` в cache — trial clock сбросится. Предупреждение в CHANGELOG.

5. **mypy strict** — все новые dataclass-методы требуют полных annotations; `field(default_factory=dict)` для dict-поля.

6. **Четвёртый cache-файл** — graceful degradation обязателен. Копировать паттерн дословно из `feedback.py:42–79`.

---

*Closes #37*
*Implements docs/decisions/0003-source-state-split.md*
