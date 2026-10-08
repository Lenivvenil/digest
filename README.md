# Daily News Digest

A personal news-reading engine for a technology architect: collect RSS/Atom sources,
select developments worth reading, and explain why the original source may matter.
Read a compact edition in Telegram and keep a Markdown archive for Obsidian.

This is the **engine repository**. A separate runtime owns its source portfolio,
configuration, credentials, schedule and saved state. It can run on GitHub Actions
without a VPS or continuously running service. Installing the engine does not create
a schedule or enable delivery.

## Why Digest

A useful reading list should help the reader make a better judgment:

- **Radar** collects the supplied evidence and selects relevant developments
- **Irritator** looks for independent evidence that challenges or complicates a
  narrative; another model opinion does not replace an external source
- **Feedback and discovery** help evolve the source portfolio, with new sources
  requiring operator approval

Useful, faithful daily output remains an acceptance goal under
[#55](https://github.com/Lenivvenil/digest/issues/55). Successful model calls, delivery
receipts and passing tests each establish narrower facts.

## Quick start

### 1. Install the engine

Python **3.12 or newer** is required. From a checkout:

```sh
git clone https://github.com/Lenivvenil/digest.git
cd digest
python -m venv .venv
. .venv/bin/activate
python -m pip install .
python -m digest --help
```

For a separate runtime, install a reviewed immutable engine commit and retain the
previous pin and configuration for rollback. Keep runtime data and credentials out
of this public repository; see [runtime setup](docs/BLIND_REVIEW.md#runtime-configuration).

### 2. Validate an example without credentials or external requests

The [example configuration](examples/config.example.yaml) has English output,
no enabled feeds and delivery disabled:

```sh
python - <<'PY'
from digest.config import load_config
c = load_config('examples/config.example.yaml')
print('Configuration valid:', c.radar.language)
PY
python -m digest --config examples/config.example.yaml --dry-run --radar-only
```

This is an offline empty-input smoke check, **not a demonstrated news digest**.
The example's feed URL and model ID are placeholders.

### 3. Configure a live runtime

Copy the example into an isolated runtime working directory. Follow the
[operator setup and configuration guide](docs/BLIND_REVIEW.md#runtime-configuration)
to choose entitled model routes, enable real sources and preview their output before
enabling delivery. The engine does **not** automatically load `.env`.

With live configuration, `--check` probes feeds and `--dry-run` can fetch sources and
call models. Neither flag generally means offline. The example selects compact
delivery, which requires the managed
[prepare, persist, claim and send sequence](docs/BLIND_REVIEW.md#persist-claim-and-send).
Enabling Telegram alone does not make a plain `python -m digest` publishable.

## How it works

The ordinary review-led path selects cards from bounded RSS evidence, saves accepted
work, then freezes an edition for publication. The runtime persists readiness and a
claim before sending, and preserves confirmed receipts and resulting state afterward.
Optional Irritator work follows confirmed, persisted primary delivery; in compact mode
its result stays in the archive without another Telegram push.

Follow the [illustrated edition lifecycle](docs/ARCHITECTURE.md#prepared-edition-data-flow)
and [domain story](docs/domain/digest/overview.md#one-story-through-the-system) for the
distinctions between a candidate, accepted work, a ready edition and confirmed delivery.
For an interrupted run, start with
[delivery states and recovery](docs/BLIND_REVIEW.md#delivery-states-and-recovery).

Legacy category summaries, optional three-perspective output and direct delivery have
[different operating contracts](docs/BLIND_REVIEW.md#legacy-format-and-optional-features).
Full-source reading is experimental and off by default. It is not required by the
ordinary RSS-review path, and its technical output does not establish editorial
acceptance. See [experimental scope](docs/BLIND_REVIEW.md#experimental-source-reading).

## Development

After installing the engine in your virtual environment:

```sh
python -m pip install -r requirements-dev.txt
make lint
make typecheck
make test
# Or all three:
make check
```

Read the [working agreement](AGENTS.md#working-agreement),
[project principles](docs/principles.md#definition-of-done) and
[ADR index](docs/decisions/README.md) before changing a contract. Optional local
pre-commit hooks are configured in `.pre-commit-config.yaml`.

The configured pytest coverage gate is **70%**; the principles' default is **80%**.
The project-specific policy disposition remains pending under
[#148](https://github.com/Lenivvenil/digest/issues/148). Neither threshold is changed
or waived here. Unit tests mock external calls; editorial usefulness and operational
delivery need their own evidence.

## Find the right guide

- [Documentation map](docs/README.md): operator, contributor and architecture routes
- [Operator guide](docs/BLIND_REVIEW.md): configuration, CLI, publication and recovery
- [Architecture](docs/ARCHITECTURE.md): lifecycle, persisted records and code ownership
- [Digest domain model](docs/domain/digest/overview.md) and
  [Irritator model](docs/domain/irritator/overview.md): purpose, identities and invariants
- [Prior README](docs/history/readme-2026-10-08.md): retained examples, decisions and
  dated rollout evidence

## Releases and licensing

Use the package version in [pyproject.toml](pyproject.toml), an immutable commit and
the [changelog](CHANGELOG.md) together when upgrading. A historical release entry is
not current quality evidence. Preserve runtime state and check compatibility before
rollback; follow the [upgrade and recovery guidance](docs/BLIND_REVIEW.md#upgrades-and-retained-state).

**No software license grant is currently supplied.** There is no LICENSE file or
declared package license. A future grant requires an explicit owner decision.

## Earlier README section links

<a id="architecture"></a>

- [Architecture and ordinary lifecycle](docs/ARCHITECTURE.md#prepared-edition-data-flow)

<a id="3-configure-a-live-report-only-run"></a>
<a id="4-enable-delivery-in-your-runtime"></a>
<a id="5-add-a-runtime-workflow"></a>

- [Live runtime setup and delivery](docs/BLIND_REVIEW.md#runtime-configuration)

<a id="cli-reference"></a>

- [CLI reference](docs/BLIND_REVIEW.md#cli-options)

<a id="project-status"></a>

- [Dated project status](docs/history/readme-2026-10-08.md#project-status)

<a id="llm-providers-and-fallback"></a>
<a id="category-routing"></a>
<a id="choosing-models"></a>

- [Model routes and category routing](docs/BLIND_REVIEW.md#model-routes)

<a id="experimental-source-reading"></a>

- [Experimental source reading](docs/BLIND_REVIEW.md#experimental-source-reading)

<a id="sources-and-categories"></a>

- [Sources and categories](docs/BLIND_REVIEW.md#sources-and-categories)

<a id="обратная-связь-и-адаптивная-система"></a>
<a id="adaptive-source-management-and-feedback"></a>

- [Feedback and source decisions](docs/BLIND_REVIEW.md#feedback-and-source-decisions)

<a id="формат-дайджеста"></a>
<a id="digest-format-and-perspectives"></a>

- [Legacy format and optional features](docs/BLIND_REVIEW.md#legacy-format-and-optional-features)

<a id="автоматическое-обнаружение-источников"></a>
<a id="external-counter-signals-and-source-discovery"></a>

- [External investigation](docs/BLIND_REVIEW.md#supplementary-investigation) and
  [source discovery](docs/BLIND_REVIEW.md#source-discovery)

<a id="cache-and-persistence"></a>

- [Persisted state](docs/ARCHITECTURE.md#cache-architecture)

<a id="language-and-optional-post-translation"></a>

- [Language and optional post translation](docs/BLIND_REVIEW.md#language-and-optional-post-translation)

<a id="compact-daily-presentation"></a>

- [Compact presentation](docs/BLIND_REVIEW.md#delivery-settings) and
  [persist, claim and send](docs/BLIND_REVIEW.md#persist-claim-and-send)

<a id="candidate-coverage-during-preparation"></a>

- [Candidate continuation](docs/BLIND_REVIEW.md#candidate-continuation)

<a id="environment-variables"></a>

- [Environment variables](docs/BLIND_REVIEW.md#environment-variables)
