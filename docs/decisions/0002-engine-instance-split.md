# 0002. Split engine and instance repositories

* Status: proposed
* Date: 2026-04-24
* Deciders: venil
* Tags: architecture, deployment, repository, open-source

## Context and Problem Statement

Проект `digest` достиг v2.0.0 и работает как живой продукт. Репозиторий одновременно содержит исходный код движка, приватную конфигурацию с RSS-источниками и Telegram chat ID, а также рантайм-артефакты (`.cache/`, `digests/`), которые коммитятся обратно GitHub Actions после каждого запуска. ADR-0001 явно зафиксировал этот split как ADR-0002. Pipeline починен (PR #33), governance установлен — это первое чистое окно для принятия топологического решения без риска сломать production.

## Decision Drivers

* Публичность engine без раскрытия приватных RSS-источников и Telegram chat ID — сейчас невозможна, конфиг физически в том же репо
* Приватный config не должен жить в одном репо с публичным кодом — сейчас они вместе структурно, а не по ошибке
* Git log разработки загрязнён автогенерёнными `digest:`/`discover:` push'ами — диагностика регрессий требует визуального отсеивания data от code
* Падение 19 апреля было невидимо до явного расследования — при split'е data-репо либо пустеет (видно сразу), либо падает изолированно
* Rule 4 в claude-mini governance конфликтует с data-push'ами в main — carve-out через regex хрупкий, отдельный репо решает структурно

## Considered Options

* **A. Orphan-ветка `content`** — `digests/` и `.cache/` коммитятся в отдельную ветку того же репо, `main` содержит только код и конфиг
* **B. Два репо: engine + instance** — `digest` становится публичным кандидатом (код, тесты, CI), `digest-prod` — приватный репо с `config.yaml`, `.cache/`, `digests/`, workflows и секретами
* **C. Git submodule** — `digest-prod` содержит engine как submodule, prod-workflow клонирует его рекурсивно
* **D. Pip package из git tag без нового репо** — engine публикуется как installable package, prod-workflow остаётся в существующем приватном host-репо
* **E. Монорепо с branch protection** — data-коммиты изолируются в отдельную `state`-ветку через workflow, `main` под PR-only

### Option A — Pros and Cons

Хорошее:
* Один репо, один набор секретов — минимальная операционная сложность
* Чистый `main` log без data-коммитов
* Откат одного digest-коммита не трогает код (reset ветки `content`, не `main`)

Плохое:
* Нельзя сделать репо публичным частично — либо весь репо открыт (включая `main` с source-list), либо закрыт
* Config.yaml с приватными источниками остаётся в `main` — driver #1 и #2 не закрыты
* Obsidian git-sync усложняется: нужен отдельный branch checkout на клиенте
* Orphan-ветка нестандартный паттерн — CI сложнее настраивать

### Option B — Pros and Cons

Хорошее:
* Полная изоляция публичного кода от приватного конфига — оба driver #1 и #2 закрыты структурно
* Rule 4 разрешается без carve-out: engine не получает data-коммитов вообще
* Open-source путь реален: engine с example-config публикуется, instance остаётся закрытым
* Падение атрибутируется engine (bug в коде) или instance (secrets, cron, network) — два разных контекста

Плохое:
* Два репо = два набора secrets, два места для troubleshooting и мониторинга
* Нужен release-контракт между engine и instance: смена публичного API = breaking change; без semver-дисциплины instance ломается на следующем cron
* Bootstrap-cost: переименование пакета `src/` → `digest/`, перенос `config.yaml` / `.cache/` / `digests/` / workflows, создание нового репо

### Option C — Pros and Cons

Хорошее:
* Prod-репо получает engine as-is без packaging
* Pinned SHA — воспроизводимость детерминирована

Плохое:
* Submodule workflow неинтуитивен — `git submodule update --init` после каждого pull, CI требует отдельного step
* Не даёт приватности: submodule-ссылка в git config раскрывает URL engine-репо всем с доступом к instance
* Bump версии engine = ручной коммит в instance обновляющий SHA — неявный release-контракт без semver

### Option D — Pros and Cons

Хорошее:
* Не создаётся новый репо — операционная сложность не растёт
* `pip install git+https://...@v2.1.0` в существующем host-репо — стандартный механизм

Плохое:
* Config.yaml и source-list остаются в том же репо что и код — приватность не решена
* Требует существующего приватного host-репо с настроенным токеном; если нет — фактически становится Option B с дополнительным шагом
* Engine repo содержит `digests/` и `.cache/` в git-истории; `pip install` тянет всё дерево — пакет раздут

### Option E — Pros and Cons

Хорошее:
* Один репо, никакого дополнительного ops
* `main` под PR-only при настроенной branch protection

Плохое:
* Config.yaml с приватными источниками остаётся в `main` — driver #1 и #2 не закрыты
* Branch protection + GHA write требует finely tuned `contents: write` permission — хрупкая настройка
* Rule 4 всё равно требует carve-out для bot-author — структурно не решает
* Путь к публичности закрыт: история `main` содержит config на предыдущих коммитах

## Decision Outcome

Chosen option: **Option B — Два репо: engine + instance**

Выбор обусловлен тем, что только B закрывает одновременно driver #1 (публичность без раскрытия) и driver #2 (физическое разделение приватного конфига от публичного кода). Остальные варианты:
* **A, C, E** — не решают приватность, drivers #1 и #2 остаются открытыми
* **D** — решает частично, но требует существующего host-репо и оставляет data-артефакты в истории engine

Rule 4 governance (driver #5) разрешается структурно без carve-out: engine-репо не получает `digest:`/`discover:` коммитов, instance живёт по собственным правилам.

Acknowledged costs: два набора secrets, release-контракт с semver-дисциплиной, bootstrap-cost миграции.

### Positive Consequences

* Engine можно открыть публично с example-config — реальный open-source путь
* История `digest` (engine) с момента split содержит только feat/fix/chore/adr — `git log` читается без фильтрации
* Регрессии в коде изолированы от инцидентов в проде: два issue-трекера, два контекста, без путаницы атрибуции
* Dogfooding: другой пользователь engine как pip-зависимости валидирует публичный API; сейчас API случайно завязан на один instance
* Dependabot на engine становится осмысленным публичным PR-потоком, отделённым от data-коммитов

### Reversibility

Решение **частично обратимо**, но с ненулевым floor-cost. Откат к монорепо потребует: слияния git-истории двух репо (либо потерей истории instance, либо через `git replace`), повторного переноса `config.yaml` / `.cache/` / `digests/` в `digest`, пересборки workflows, ротации или объединения двух наборов secrets, и — если engine был публичен — принятия решения о его закрытии (что не стирает уже проиндексированные fork'и и клоны). Оценочная трудоёмкость отката через 6–12 месяцев: 1–2 рабочих дня. Если split существует менее 3 месяцев и instance-репо не успел накопить значимую историю — откат ближе к 2–4 часам.

### Negative Consequences

* Два репо = два места для мониторинга, secrets rotation, troubleshooting
* Release-контракт обязателен: смена публичного API в engine без bump в instance = silent breakage на следующем cron; требует semver-дисциплины
* Bootstrap-cost: переименование пакета `src/` → `digest/`, создание `digest-prod`, перенос `config.yaml` / `.cache/` / `digests/` / workflows
* Отладка межрепозиторная: баг воспроизводимый только в prod требует двух checkout'ов + `pip install -e engine` — вместо `cd ~/code/digest && pytest`
* Security surface удваивается при публикации engine: внешние люди могут анализировать SSRF в feed-парсере, dependency confusion, supply chain — публичность = явное принятие этой поверхности
* Issue-триаж размазан: при инциденте в проде первый вопрос "engine или instance?" требует атрибуции до начала расследования
* Open-source tax при реальной публикации: внешние PR и issues создают давление на docs и backward compat
* Dependabot workflow удлиняется: merge в engine → tag release → bump pin в instance = +15 минут на каждый update вместо одной операции
* Claude Code контекст размазан: при работе в engine не виден instance config и наоборот; MCP/Serena индексы нужно настраивать на оба репо

## Confirmation

Четыре конкретных проверки:

1. **Еженедельно (при `/project-health`):** в engine main нет коммитов вида `digest: YYYY-MM-DD` или `discover: YYYY-MM-DD` за прошедшие 7 дней. Порог: **любой такой коммит = утечка данных в engine, расследовать немедленно**.

2. **При каждом dependabot bump'е:** от merge в engine до соответствующего bump-PR в instance не более 72 часов. Замер через `gh pr list`. Порог: **более 2 подряд нарушений = release-контракт не работает, revisit**.

3. **Ежемесячно:** `grep -r "TELEGRAM\|API_KEY\|feeds:" engine-repo/` возвращает только example/placeholder значения. Порог: **любой реальный secret или приватный feed URL = критическая утечка**.

4. **После каждого падения prod'а:** время атрибуции ("engine или instance?") ≤5 минут по GitHub Actions логу. Если регулярно превышается — issue-триаж размазан больше ожидаемого.

## Re-visit Trigger

1. **За 6 месяцев после split engine не получил ни одного внешнего contributor'а, issue или dependabot PR сверх того что был бы в монорепо.** Driver "публичность" оказался теоретическим — overhead двух репо не окупается. Rollback или переход на D.

2. **Release-контракт нарушается трижды подряд за квартал** — instance ломается на cron после engine bump без предупреждения. Semver-дисциплина не выдерживает одного разработчика; нужен CI-гейт (compat-тест engine прогоняет instance) или откат к монорепо.

3. **Anthropic или сообщество выпускает официальный toolkit для персональных agent-pipelines**, предполагающий иную топологию репозиториев. Текущая split-архитектура становится идиосинкразией.

4. **Миграция с Mac mini на другую платформу** — baseline инфраструктуры меняется, все архитектурные решения требуют переоценки.

**Не является триггером (noise vs signal):**
* Отдельные failed Actions runs на prod — operational noise
* Периодическое неудобство "надо клонировать два репо" — acknowledged cost
* Один dependabot PR мержится дольше 72 часов — noise; trigger срабатывает на pattern

## Links

* Связанный issue: #31 (Arch: отделить engine-репо от продакшен-инстанса)
* Предыдущий: ADR-0001 — Adopt claude-mini governance (явно предусматривал этот ADR)
* Следующий: `/plan 31` — план реализации split
