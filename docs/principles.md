# Principles

This document is a contract. Every decision is checked against it; every violation must be deliberate and recorded in an ADR.

Implementation scope, checked 2026-10-07: the tracked [CI workflow](../.github/workflows/ci.yml)
installs dependencies and runs Ruff, mypy and pytest. The claude-mini commands, roles
and hooks below describe installation-dependent local tooling from
[ADR0001](decisions/0001-adopt-claude-mini-governance.md), not additional tracked CI jobs.
The review discipline below remains the working agreement; local tooling and manual
review evidence must not be inferred from a green CI result.

<a id="четыре-директивы"></a>

## Four directives

<a id="1-красные-флаги-вместо-трейдоффов"></a>

### 1. Red flags instead of trade-offs

Where it is tempting to say “option A vs. option B — choose whichever you prefer,” give a direct answer about which option is correct and which is a workaround. If there is no correct answer, say honestly: “I don't know; empirical evidence is needed.”

Check: if a document says “both are good” or “it depends on preferences” without specifying the conditions that determine the choice, the wording itself is a red flag, not useful neutrality.

<a id="2-claude--душный-напарник-не-эксперт"></a>

### 2. Claude is a demanding partner, not an expert

Claude's role is to ask uncomfortable questions, require complete artifacts, and refuse to accept “good enough for now.” The human operator is the sole author and accountable owner of the domain and architecture.

In practice, this is reflected in agent roles: all critics are read-only; none writes code. `adr-reviewer` does not suggest alternatives the author has not considered. `domain-reviewer` does not add terms. `backlog-groomer` does not mutate issues.

<a id="3-автоматизировать-только-низкорискованное"></a>

### 3. Automate only low-risk work

Classify every pipeline step by risk.

**Without approval:** linting, type checking, formatting, read-only analysis, drafts, running tests, reading documentation, and searching code.

**With approval:** architectural decisions, library selection, interpreting ambiguous requirements, security-sensitive changes, production configuration, migrations, and merging into main.

In the historical claude-mini setup, `auto-mode` covers the first category,
`deny-rules` restrict sensitive operations, and an installed `governance-hook` checks
commit issue references and architectural ADR links. Their presence and result depend
on local installation; the repository CI does not run these checks.

<a id="4-knowledge-в-инструментах-не-в-памяти"></a>

### 4. Knowledge belongs in tools, not memory

Everything significant belongs in git (`docs/decisions/`, `docs/domain/`, the `CLAUDE.md` index) and GitHub (Issues, Projects v2, PRs). Claude's memory is a cache, not the source of truth.

**Test:** any colleague (or you, three months later) can enter the repository and recover the context within an hour **without chat history or conversations**. If a verbal introduction is required, the documentation has regressed.

## Definition of Done

A change is **Done** when all conditions are true:

Owner clarification, 2026-10-04: Claude review is optional.

- [ ] An ADR has been opened and merged if the change is architecturally significant
- [ ] Domain documentation is updated if a BC boundary or term has changed
- [ ] Unit tests are written; integration tests cover cross-BC paths; coverage ≥ the project floor (80% by default)
- Optional: `/review` (Claude) may provide an additional review; its absence does not block completion
- [ ] `/codex-review` (Codex) has approved OR a `type:deferred-review` issue records the rationale for graceful degradation
- [ ] When both reviews are performed, disagreements between Claude and Codex are resolved in the PR thread (consensus or a recorded disagreement)
- [ ] Human self-review is complete
- [ ] Security scans are clean: `uv pip audit` / `cargo audit` / `npm audit --audit-level=high` / `govulncheck`, depending on the language
- [ ] Documentation is updated: README (for public changes), the relevant runbook, and CHANGELOG. The historical release-please workflow is not installed in tracked CI.
- [ ] CI is green for all required jobs
- [ ] Conventional Commits are used; where the local governance-hook is installed, its check has passed
- [ ] The PR body references the implementing issue and applicable ADR. Use `Closes #NNN` only when all issue acceptance is met; partial changes reference the issue without auto-closing unfinished work.

Coverage disposition, observed 2026-10-07: [pytest configuration](../pyproject.toml)
currently enforces **70%**, while this checklist specifies **80% by default**. The
repository does not yet record an approved project-specific disposition for that
difference. Resolve it explicitly under [#148](https://github.com/Lenivvenil/digest/issues/148);
a passing configured gate does not itself waive the default or complete this checklist.
Neither threshold is changed by this documentation reconciliation.

Use this checklist during PR review. The [PR template](../.github/pull_request_template.md)
asks for the applicable change, evidence and follow-through without copying every
checkbox. Neither the template nor CI verifies the whole checklist; record the
review and verification evidence actually obtained.

<a id="5-scope-инструмента-ограничен-явной-установкой"></a>

### 5. Tool scope is bounded by explicit installation

Claude-mini affects nothing outside the repositories, files, and settings where its installer physically placed artifacts. No global git configuration, path patterns, allowlists, or other mechanisms that apply rules by matching a path or name. The boundary is actual installation, not a configuration pattern.

In practice: the commit-msg hook is copied directly to `.git/hooks/commit-msg` by `universal-setup.sh --hook-this-repo`; `core.hooksPath` is untouched. New pet projects require an explicit `--install` (which stages the hook in `~/.claude/git-hooks/`) followed by `--hook-this-repo` (which puts it in `.git/hooks/`). System-integrated mechanisms (global launchd, shell-rc conditions, cross-project indexing) are outside the tool's scope and require revisiting this principle before implementation.

<a id="что-значит-архитектурно-значимо-триггер-для-adr"></a>

## What “architecturally significant” means (ADR trigger)

A decision requires an ADR if **at least one** of the following is true:

- A dependency that becomes cross-cutting (logger, ORM, HTTP client) is added or removed
- A bounded-context boundary or the signature of a cross-context contract changes
- A store, queue, or other infrastructure component is selected
- A public API is established or removed
- A constraint that will be difficult to remove in six months is adopted (for example, a single language, database, or cloud platform)
- The security model or data model changes

If none applies, this is a story, not a decision. `/plan` is sufficient.

<a id="что-значит-нетривиальная-задача-триггер-для-advisor--2"></a>

## What a “nontrivial task” means (advisor × 2 trigger)

A task requires two advisor calls if **at least one** of the following is true:

- It affects more than one module/package
- It has non-obvious edge cases
- It competes with similar existing code (duplication is not obvious)
- It involves asynchrony, concurrency, or rare-path error handling

Trivial work (no advisor needed): formatting, renaming, lint fixes, one-line bug fixes with a test, and updating a line in documentation.
