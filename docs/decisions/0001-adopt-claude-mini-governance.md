# 0001. Adopt claude-mini enterprise workflow

* Status: accepted
* Date: 2026-04-24
* Deciders: venil
* Tags: governance, bootstrap, workflow

## Context and Problem Statement

Проект `digest` существует с 2025 года как персональный генератор новостных дайджестов, достиг v2.0.0 (переписывание на radar + irritator + delivery pipeline, закончено 2026-04-09). С 19 апреля 2026 pipeline остановился — последний успешный `discover:` коммит датирован 2026-04-19, с тех пор ни `digest:` пушей, ни ручных вмешательств. Причина падения на момент принятия этого ADR не диагностирована.

Параллельно на Mac mini 2018 завершён bootstrap проекта `claude-mini` (см. claude-mini ADR-0001..0011) — enterprise workflow с governance hooks, two-voice review, ADR-gated планированием. Этот workflow уже обкатан на smoke-test и на самом `claude-mini`.

Дальнейшая работа над `digest` требует выбора: продолжать ad-hoc-разработкой прямо в `main` (как было до сих пор), или встать на тот же рельс, что и `claude-mini`.

## Decision Drivers

* История digest на main смешивает feat/fix-коммиты разработки и автогенерённые `digest:`/`discover:` пуши от GitHub Actions — диагностика регрессий (как падение 19 апреля) затруднена, git log неотделим от data log.
* v2.0.0 — живой продукт, но сейчас не работает; текущий момент — единственная окно для архитектурной переработки без риска сломать production.
* Инвестиция в `claude-mini` должна окупаться на нескольких проектах, иначе пайплайн — overhead ради одного репо.
* Отсутствие ADR в digest = невозможность формально зафиксировать будущие решения (в частности, предстоящий split на code и runtime репозитории).

## Considered Options

* **Option A: Полный reset main, чистая история, bootstrap с нуля.** Максимальная чистота, но уничтожает историю разработки продукта, теги, и сломает любые внешние ссылки на SHA. Рассмотрено и отвергнуто в ходе сессии.
* **Option B: Установка governance поверх текущего main через `chore: bootstrap` carve-out, сохраняя историю.** Вся новая работа — через feature-branch + PR, старая история остаётся как факт. ADR-0002 впоследствии займётся архитектурной перестройкой (code/runtime split).
* **Option C: Не ставить governance; продолжить ad-hoc разработку.** Сохраняет скорость итерации, но не лечит проблему смешения и не даёт места для формального решения по split.

## Decision Outcome

Chosen option: **Option B** — установка governance поверх текущего main.

Причина выбора: (1) digest достаточно сложен, чтобы оправдать пайплайн — v2.0.0 уже ловит регрессии, которые сложно диагностировать без структурированного процесса; (2) reset main (A) стирает улики, не лечит архитектуру — проблема смешения dev/runtime не в истории, а в топологии репозитория, что требует отдельного ADR, а не ретуши прошлого; (3) без governance (C) у предстоящего split нет места для формального обсуждения, `/plan` и `/advisor` не могут быть призваны.

### Positive Consequences

* Предстоящий split на `digest` (code) и `digest-runtime` (data) получит формальное рассмотрение через ADR-0002.
* Падение 19 апреля зафиксируется как issue и войдёт input'ом в ADR-0002, а не будет тушиться hotfix'ом.
* Все feat/fix-коммиты далее идут через PR — git log разработки отделяется от будущих data-коммитов.
* User-scope claude-mini (`~/.claude/`) используется вторым проектом — валидирует multi-project пригодность пайплайна.

### Negative Consequences

* История digest/discover-коммитов в main остаётся смешанной с разработкой — читаемость ретроспективно не улучшается, только going forward.
* Rule 4 (запрет feat-commit в main) потенциально конфликтует с автогенерённым `digest:`/`discover:` push от Actions. На момент этого ADR workflow не работает, так что конфликт не материализуется; если split (ADR-0002) переносит эти пуши в отдельный репо — конфликт отпадает. Если ADR-0002 не случится или решит иначе — потребуется carve-out в `pre-commit-governance.sh` или bot-author-based allowlist.
* `claude-mini` как зависимость: изменения в его workflow могут потребовать ресинхронизации digest. Это цена reusable-пайплайна.

## Baseline inventory (digest, на 2026-04-24)

**Состояние продукта:**

* Версия: 2.0.0 (teg `v2.0.0`)
* Последний успешный run: `discover: 2026-04-19` (commit `16b63ef`)
* Состояние: pipeline остановлен, причина не диагностирована
* Архитектура: radar → irritator → delivery, 6-фазная, runs on GitHub Actions free tier
* Основные файлы-артефакты в репо: `src/`, `tests/`, `.github/workflows/{digest,discover}.yml`, `config.yaml`, `digests/`, `.cache/`

**Стек:**

* Python 3.12+ (dev на 3.13 через mise)
* httpx (async), feedparser, pyyaml
* pytest + pytest-asyncio, ruff, mypy

**Governance (устанавливается этим ADR):**

* `.mise.toml` — Python 3.13 + uv
* `.git/hooks/commit-msg` — staged из `~/.claude/git-hooks/commit-msg` (claude-mini ADR-0011)
* `docs/decisions/` — каталог под ADR
* User-scope агенты, команды, skills, hooks — из `~/.claude/` (установлено через `claude-mini/bootstrap/universal-setup.sh --install --force`)

**Открытые ветки (post-bootstrap):**

* `main` — основная, содержит v2.0.0 код + data-коммиты digest/discover
* `chore/bootstrap-governance` — эта работа, подлежит merge в main как первый PR
* `dependabot/pip/{pytest-9.0.3, respx-0.23.1, ruff-0.15.10}` — висят, обрабатываются после bootstrap
* Тег `archive/v2-rewrite` — снимок удалённой ветки v2

## Out of scope для этого ADR

* Диагностика падения 19 апреля — отдельный issue, input для ADR-0002.
* Архитектурный split на code/runtime — ADR-0002.
* Carve-out правил для автогенерённых коммитов — решается в ADR-0002 в зависимости от исхода split.
* Судьба dependabot PR — обрабатываются по обычному flow после merge bootstrap.

## Confirmation

* `~/code/claude-mini/bootstrap/universal-setup.sh --check` из `digest` возвращает no drift.
* `~/bin/mini-health` в `digest` отчитывается о presence of commit-msg hook.
* Попытка `git commit -m "feat: ..."` напрямую в main после merge этого PR блокируется rule 4.
* Первый ADR-gated workflow — issue + `/plan` + `/advisor` + ADR-0002 о split — проходит end-to-end.

## Re-visit Trigger

* `claude-mini` существенно меняет workflow (например, отказ от two-voice review) — требуется оценить, остаётся ли governance в digest уместным.
* `digest` прекращает быть активным продуктом — тогда governance становится overhead без возврата.
* Мажорное обновление Claude Code ломает совместимость с установленными hooks/agents.

## Links

* Parent: `claude-mini` ADR-0001..0011 (в репозитории `Lenivvenil/claude-mini`)
* Следующий: ADR-0002 — split code и runtime (to be written)
* Источник решения: сессия 2026-04-24, bootstrap digest под claude-mini workflow
