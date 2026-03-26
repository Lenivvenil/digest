# Plan: Fix recurring lint failures in CI

## Overview
CI регулярно падает из-за одних и тех же lint-ошибок (F401 unused imports, F841 unused vars, F821 undefined names). Корневая причина — отсутствие локального lint-гейта и конфигурации ruff. Цель: сделать невозможным push кода с lint-ошибками и дать AI-агентам явную инструкцию проверять lint перед коммитом.

## Validation Commands
- `ruff check src/ tests/`
- `mypy src/`
- `pytest tests/ -v`

### Task 1: Add ruff configuration
Создать явную конфигурацию ruff вместо неявных дефолтов. Зафиксировать правила, target-version, per-file-ignores для тестов.
- [ ] Create `ruff.toml` in project root with `target-version = "py312"`, `select = ["E", "F", "B"]`, `line-length = 120`
- [ ] Add `[per-file-ignores]` section: for `tests/**` ignore `F811` (pytest fixtures often shadow names)
- [ ] Verify `ruff check src/ tests/` passes with new config (no new violations introduced)
- [ ] Add a test: create `tests/test_ruff_config.py` that runs `subprocess.run(["ruff", "check", "src/", "tests/"])` and asserts returncode == 0
- [ ] Mark completed

### Task 2: Add pre-commit hooks
Установить pre-commit framework с ruff hook, чтобы lint-ошибки блокировались до push.
- [ ] Create `.pre-commit-config.yaml` with `astral-sh/ruff-pre-commit` hook (rev matching `ruff==0.15.6` from requirements-dev.txt)
- [ ] Configure two hooks: `ruff` (with `--fix` for safe autofixes) and `ruff-format` (check only)
- [ ] Add `pre-commit` to `requirements-dev.txt`
- [ ] Verify: intentionally add unused import to a test file, run `pre-commit run --all-files`, confirm it catches/fixes the error, then revert
- [ ] Mark completed

### Task 3: Add Makefile with lint/test targets
Добавить Makefile как единую точку входа для локальных проверок — удобнее, чем запоминать команды.
- [ ] Create `Makefile` in project root with targets: `lint` (`ruff check src/ tests/`), `lint-fix` (`ruff check --fix src/ tests/`), `typecheck` (`mypy src/`), `test` (`pytest tests/ -v`), `check` (runs lint + typecheck + test sequentially)
- [ ] Add `.PHONY` declarations for all targets
- [ ] Verify `make check` passes end-to-end
- [ ] Mark completed

### Task 4: Update CLAUDE.md with lint-before-commit rule
Дать AI-агентам (Claude, Codex) явную инструкцию всегда запускать ruff перед коммитом. Это уменьшит количество fix-коммитов.
- [ ] Add section "## Pre-commit Checklist" to `CLAUDE.md` with rule: "Always run `ruff check src/ tests/` before committing. Fix all errors before creating a commit."
- [ ] Add to "## Code Style" section: "Never import symbols that are not used in the file. Never assign to variables that are not read."
- [ ] Add to "## Testing" section: "After writing or modifying test files, run `ruff check tests/` to catch unused imports and undefined names before committing."
- [ ] Verify by reading the updated `CLAUDE.md` and confirming instructions are clear and actionable
- [ ] Mark completed

### Task 5: Harden CI lint step
Улучшить CI lint step: показывать diff автофиксов, чтобы разработчик сразу видел решение.
- [ ] In `.github/workflows/digest.yml`, change lint step from `ruff check src/ tests/` to `ruff check src/ tests/ --output-format=github` for inline annotations on PR diffs
- [ ] Add a second lint step `ruff check src/ tests/ --fix --diff` that shows what autofix would do (informational, non-blocking)
- [ ] Verify CI still fails on lint errors (the first step must remain blocking)
- [ ] Run `pytest tests/ -v` to confirm no test regressions
- [ ] Mark completed
