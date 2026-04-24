# Plan: Engine-instance split (Issue #31)

## 1. Problem restatement

The `digest` repository currently acts as three things at once: a Python codebase with tests and CI, a private configuration store with personal RSS sources and LLM weights, and a production runtime that commits generated digests and cache state back to `main` via GitHub Actions. These roles have incompatible hygiene requirements — code wants a clean PR-only history, config wants to be private, and runtime state wants to be committed every two hours. The goal is to split the repo into `digest` (public engine — code only) and `digest-prod` (private instance — config, state, workflows), as decided in ADR-0002.

## 2. Affected bounded contexts and files

No domain docs exist; bounded contexts are inferred from module structure.

**Engine (digest) — changes required:**

| File / path | Change |
|---|---|
| `src/` (entire directory) | Rename to `digest/` |
| All `from src.X` imports (112 occurrences across 40 files) | → `from digest.X` |
| `src/__main__.py` docstring and import | `from src.main` → `from digest.main` |
| `pyproject.toml` | **`[project] name = "daily-digest"` → `"digest"`** (distribution name — must match `pip install "digest @ git+..."`); package discovery `src` → `digest`; **drop `[project.scripts]`** (workflows use `python -m digest`, no script entry point needed); `known-first-party = ["digest"]`; `--cov=digest` |
| `Makefile` | `ruff check src/` → `ruff check digest/`; `mypy src/` → `mypy digest/` |
| `.pre-commit-config.yaml` | `args: [-r, src/` → `args: [-r, digest/` |
| `.github/workflows/ci.yml` | `python -m src` → `python -m digest`; remove config-validate step (config.yaml no longer in engine repo) |
| `.github/workflows/daily.yml` | Remove entirely (moves to digest-prod) |
| `.github/workflows/discover.yml` | Remove entirely (moves to digest-prod) |
| `config.yaml` | Remove from engine repo (moves to digest-prod) |
| `.cache/` | Remove from engine repo (moves to digest-prod) |
| `digests/` | Remove from engine repo (moves to digest-prod) |
| `README.md` | Update architecture section, installation instructions |
| `CLAUDE.md` | Update project structure, entry point |
| `.gitignore` | Add `config.yaml`, `.cache/`, `digests/` (protect against accidental re-add) |
| `pyproject.toml` `[tool.mypy]` | `python_version` stays 3.12 |

**Instance (digest-prod) — new repo:**

| File / path | Action |
|---|---|
| `config.yaml` | Copy from engine (current) |
| `.cache/` | Copy from engine (current) |
| `digests/` | Copy from engine (current) |
| `.github/workflows/daily.yml` | Adapt: replace `pip install -r requirements.txt` with `pip install "digest @ git+https://github.com/Lenivvenil/digest@main"`; remove `test` job |
| `.github/workflows/discover.yml` | Same adaptation |
| `requirements.txt` | `digest @ git+https://github.com/Lenivvenil/digest@main` |
| GitHub Secrets | Re-create all 7 secrets in digest-prod |
| `.gitignore` | Standard Python |

## 3. Considered approaches

### Approach A — Rename-then-split (chosen)

Rename `src/` → `digest/` in the engine repo first, run tests to confirm, then create `digest-prod` and configure it to `pip install` from engine. The config.yaml mutation problem (trial dates, `enabled: false`) is **not refactored** — config.yaml moves wholesale to `digest-prod` where it can still be mutated freely by `apply_trial_decisions()` and `add_trial_source()`. The dual-nature concern from the issue body only matters if config.yaml needs to live in *both* repos; with it living only in `digest-prod`, mutation is isolated and fine.

**Trade-offs:**
- Pro: minimal scope — no data model changes, no new cache files, no changes to how `apply_trial_decisions` works
- Pro: tests continue to pass without mocking config writes (tests use `tmp_path` fixtures already)
- Con: config.yaml in `digest-prod` is still a mix of static preferences and runtime state — acknowledged but not worse than today
- Con: `pip install git+...@main` means engine's `main` is always the prod version — no pinning; a bad commit can break prod on the next cron run

### Approach B — Extract dynamic state before split

Before splitting, refactor `apply_trial_decisions()` and `add_trial_source()` to write trial metadata and `enabled` flags to `.cache/dynamic_sources.json` instead of `config.yaml`. `config.yaml` becomes truly static.

**Trade-offs:**
- Pro: clean separation — `config.yaml` is pure user intent, `.cache/` is pure runtime state
- Pro: makes it possible to later version-control config.yaml separately without worrying about runtime mutations
- Con: scope creep — changes `source_scorer.py`, `discovery.py`, `config.py` and all their tests before the split even starts; doubles implementation risk
- Con: not required by ADR-0002; the ADR decision is about repo topology, not data model

**Verdict:** Approach A now, Approach B as a separate issue later if the dual-nature of config.yaml becomes a real operational problem.

### Approach C — Pin engine to a git tag (variant of A)

Same as A but `requirements.txt` in `digest-prod` pins to a released tag (`digest @ git+...@v2.0.0`) instead of `@main`.

**Trade-offs:**
- Pro: prod stability — a bad commit in engine doesn't break prod until explicitly bumped
- Con: requires a release discipline (tag before prod can get new code); solo developer overhead
- Con: ADR-0002 Re-visit Trigger #2 already covers the case where release-contract fails; for now `@main` is simpler

**Verdict:** Start with `@main`, upgrade to tag-pinning when/if Re-visit Trigger #2 fires.

## 4. Chosen approach and why

**Approach A** (rename-then-split, `@main` pin).

ADR-0002 chose the two-repo split to solve repo topology, not data model. Approach A implements exactly that decision without adding scope. The 112 import rewrites are mechanical and fully covered by `ruff check` + `mypy` + existing test suite. The config.yaml mutation concern is a latent issue regardless of approach — it's better addressed as a focused follow-up than as a prerequisite that blocks the split.

Relevant ADRs: ADR-0002 (engine-instance split), ADR-0001 (governance — commit-msg hook will run on the rename PR).

## 5. Test strategy

**Unit (existing, unchanged):** All 83 tests in `tests/` cover the production code. After renaming `src/` → `digest/`, every `from src.X import Y` becomes `from digest.X import Y`. If any import is missed, pytest will fail with `ModuleNotFoundError` — full coverage of the rename.

**Assertions that matter:**
- `pytest tests/ -v` passes 83/83 after rename
- `ruff check digest/ tests/` clean (catches missed `src` references in imports)
- `mypy digest/` clean
- `python -m digest --help` exits 0 (verifies entry point wiring)
- `python -c "from digest.config import load_config"` exits 0

**Integration (manual, async):** After `digest-prod` is configured:
- Trigger `daily.yml` via `workflow_dispatch` in `digest-prod`
- Observe successful `digest: YYYY-MM-DD` commit in `digest-prod/main`

**No new tests needed** — the rename is structural, not behavioral.

## 6. Risks and unknowns

- **pip install from GitHub in CI:** `pip install "digest @ git+https://github.com/Lenivvenil/digest@main"` requires the engine repo to be public OR a deploy key/PAT to be configured in `digest-prod`. If engine stays private during transition, the `git+https` install will fail with 401. **Mitigation:** make engine repo public before configuring `digest-prod` workflows, or add a PAT secret.

- **`pyproject.toml` package discovery:** currently there is no explicit `[tool.setuptools.packages.find]` — pip/setuptools auto-discovers packages. After renaming `src/` → `digest/`, auto-discovery should find `digest/` as a top-level package. But if `pyproject.toml` has any implicit `src` references in build config, the install will silently produce an empty package. **Mitigation:** test `pip install -e .` locally before creating `digest-prod`.

- **`.cache/` and `digests/` migration:** current `.cache/` contains live state (seen articles, feedback, source stats, pending sources). Copying to `digest-prod` preserves continuity — no articles will be re-sent. However, if the copy is stale (committed state is from last cron run, not current), the first run in `digest-prod` will miss any feedback collected in the gap. Acceptable.

- **GitHub Secrets migration:** 7 secrets must be re-created manually in `digest-prod`. There is no automated way to copy secrets between repos. Risk of typo or missed secret = first prod run silently fails. **Mitigation:** run `workflow_dispatch` and check logs immediately after setup.

- **Commit-msg governance hook (ADR-0001):** the hook runs on `git commit` in the engine repo. The rename commit will be a large mechanical change — governance hook checks commit type prefix (`feat/fix/chore/adr/...`). Use `chore:` prefix. No conflict.

- **`digests/` git history:** after removing `digests/` from engine repo, the history of digest files remains in `git log`. This is expected and acceptable — the history is not deleted, just not tracked going forward.

- **`python-version` drift:** `.mise.toml` pins Python 3.13 locally; CI uses 3.12. The rename is compatible with both. No change needed in this plan; tracking it as a known latent issue.

## Execution order

1. **PR 1 (engine):** rename `src/` → `digest/`, update all imports and infra files (`pyproject.toml`, `Makefile`, `.pre-commit-config.yaml`, `ci.yml`), strip `daily.yml`/`discover.yml`/`config.yaml`/`.cache/`/`digests/`, update README + CLAUDE.md. Tests pass. Merge.
2. **Make `digest` repo public** — required before step 3 so `pip install "digest @ git+https://github.com/Lenivvenil/digest@main"` works without a PAT. This is the simplest option; engine has no secrets or private data after step 1.
3. **Create `Lenivvenil/digest-prod`** (private), copy files, configure secrets, adapt workflows, trigger `workflow_dispatch` to verify pipeline.
