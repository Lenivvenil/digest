# Configure a runtime

Start with disabled inputs, inspect an intentional live preview, then enable managed publication.

- [Start a runtime](#runtime-configuration): isolation, safe preview and credentials
- [Choose models](#model-routes) and [sources](#sources-and-categories)
- [Set up delivery](#delivery-settings), [language](#language-and-optional-post-translation) or [optional formats](#legacy-format-and-optional-features)
- [Find a CLI flag](#cli-options) or [configure source exploration](#source-discovery)

## Runtime configuration

Keep `config.yaml`, credentials, `.cache/` and generated output in a separate runtime.
Install a reviewed immutable engine commit there, retain the previous pin, and run
from its own working directory. Two instances must not share writable state.
The engine's CI checks source code; it does not install a daily schedule. A managed
runtime owns its schedule, secrets, engine pin, serialized runs and Git persistence.

Start from the [disabled example](../examples/config.example.yaml), replace the model
and feed placeholders, and enable the intended source. Keep Telegram and Markdown
delivery disabled while reviewing an intentional live preview.

**These commands use the network.** `--check` probes feeds; `--dry-run` can call
models and consume quota. For an offline empty-input check, use the
[disabled-example quick start](../README.md#try-it-safely). Then, when ready:

```sh
python -m digest --config config.yaml --check
python -m digest --config config.yaml --dry-run --radar-only
```

`--check` probes enabled feeds and checks credential presence for category
`llm.providers`/`llm.routing` and enabled Telegram delivery, not separately pinned
review routes. Its totals cover feeds only; configuration or feed failures return
a nonzero exit, while warnings do not. It does not authenticate model or Telegram
access, establish live readiness or prove editorial quality.
`--dry-run` can fetch sources and call models, consuming
their quotas, while suppressing normal digest delivery and saved digest output.
Use the README's disabled-example commands for an offline empty-input check.

### Environment variables

Load selected credentials securely through the shell or runner. The engine does
**not** automatically load `.env`; [.env.example](../.env.example) lists reference
names only. Never put real credentials in a committed URL, screenshot, issue or log.

| Variable | Purpose |
| --- | --- |
| `ANTHROPIC_API_KEY` | Anthropic provider |
| `GEMINI_API_KEY` | Gemini provider |
| `GROQ_API_KEY` | Groq provider |
| `MISTRAL_API_KEY` | Mistral provider |
| `DEEPSEEK_API_KEY` | DeepSeek provider |
| `TELEGRAM_BOT_TOKEN` | Configured Telegram bot |
| `TELEGRAM_CHAT_ID` | Intended destination and supported owner checks |
| `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USERNAME` | Optional existing Reddit adapter credentials |

Missing keys disable matching provider routes; they do not guarantee another route
can succeed. Missing required Telegram credentials is a failure even when Markdown
can be saved.

### Model routes

The provider adapters support Anthropic, Gemini, Groq, Mistral and DeepSeek.
Configure only intended services and models. Check current model availability,
account entitlement, pricing, request limits and token limits before a live run;
context capacity is not a free throughput budget. Do not inadvertently add a paid
fallback to a free-only runtime. Exhausted eligible routes can leave failed or
incomplete work.

Category-analysis mode uses role/provider routing, bounded retries and fallback.
Category work runs concurrently within configured limits before cross-category trends.
A category-specific route can nominate a provider/model; unrouted categories use the
configured chain. Missing credentials follow the existing fallback rules. These
model IDs are placeholders, not recommendations:

```yaml
llm:
  providers:
    - name: gemini
      model: YOUR_GEMINI_MODEL_ID
      role: [summarize, rank_signals, fallback]
    - name: groq
      model: YOUR_GROQ_MODEL_ID
      role: [fallback]
  routing:
    - categories: [AI Engineering]
      provider: gemini
      model: YOUR_GEMINI_MODEL_ID
```

#### Ordinary review routes

Review slots are pinned separately from `llm.providers` and `llm.routing`; changing
category routes does not replace the review slots' defaults. Before enabling review,
set both `review.primary` and `review.secondary` explicitly. Each slot must use a
distinct provider/model identity.

The example below uses the same provider/model placeholders as the category example
above. Replace every `YOUR_*_MODEL_ID` with an intended model that your account can
use; these are not runnable model IDs or promises of availability, free quota or
access. Review slots can use different models from category analysis. Merge these
fields into the existing sections of your runtime config rather than adding duplicate
YAML keys. Keep Telegram and Markdown delivery disabled while reviewing a preview:

```yaml
review:
  enabled: true
  review_led_only: true
  primary:
    provider: gemini
    model: YOUR_GEMINI_MODEL_ID
  secondary:
    provider: groq
    model: YOUR_GROQ_MODEL_ID
telegram:
  enabled: false
  delivery_mode: compact
obsidian:
  enabled: false
```

[Ordinary preparation](OPERATIONS.md#ordinary-preparation) uses one primary attempt
and, only if its output is invalid or unavailable, at most one secondary fallback.
[Independent comparison](ADVANCED_OPERATIONS.md#independent-comparison-and-resume)
is optional and has different execution and completion rules. Enabling review and
choosing compact formatting do not replace the managed
[prepare, persist, claim and send sequence](OPERATIONS.md#persist-claim-and-send).

### Sources and categories

Edit the runtime's `sources` list. The public example contains no private source list:

```yaml
sources:
  - name: My Feed
    url: https://example.com/feed.xml  # Replace before enabling
    category: Architecture
    enabled: false
    priority: 3
    recency_hours: 24
    trial: false
    trial_days: 7
```

Priority ranges from 1 to 5 and influences source-slot allocation; it does not prove
editorial value. Categories are operator-defined. Banking/payments, AI engineering,
distributed systems, enterprise architecture and regional affairs are examples from
the project's original use, not mandatory personal-profile presets.

### Delivery settings

Create a Telegram bot through the official BotFather flow and configure its token and
intended chat ID in runtime environment variables or Actions secrets. The engine sends
to `TELEGRAM_CHAT_ID`; it does not select recipients. Set `telegram.enabled: true`
when ready, and `telegram.required: true` if a Markdown archive alone must not count
as successful delivery. Enable `obsidian` to save Markdown to its configured output
directory, then sync that runtime archive to Obsidian. API acceptance does not mean
a person read the edition.

`telegram.delivery_mode: compact` assembles selected articles into one logical edition,
using necessary chunks without cutting selected text or source URLs. Indexed vote
buttons retain article identity; translation and model-attribution notices appear
once per issue. The ordinary managed path requires [prepare, persist, claim and send](OPERATIONS.md#persist-claim-and-send).
Omitting delivery mode preserves legacy per-article cards; switching formatting alone
does not supply the prepared publication protocol.

For voting links and private-owner checks, follow [feedback and source decisions](OPERATIONS.md#feedback-and-source-decisions), including the bot username setting.

## Language and optional post translation

Without a `translation` section, legacy `radar.language` accepts `en` or `ru` and
its omitted-field default remains `ru`. Upgrading an existing Russian runtime does
not implicitly add translation calls. A new explicit translation section defaults
to English canonical generation only when no generation language was specified.
Enabled translation rejects an explicit `radar.language: ru` conflict.

To opt in, choose an already configured, entitled route:

```yaml
radar:
  language: en
translation:
  enabled: true
  target_language: ru
  provider: gemini             # Must exist in llm.providers or an explicit review route
  model: YOUR_GEMINI_MODEL_ID  # Exact match to that configured provider/model
  max_calls: 1                # Per presentation pass, without HTTP retries
  timeout_seconds: 90         # Total budget, including shared pacing and the request
  max_output_tokens: 2048
  max_input_chars: 12000
```

Translation changes generated card/category prose and published Irritator narratives
and reasoning. Original titles, source metadata, URLs, literal quotations and raw
reviews/evidence remain canonical. The target does not change analysis, review input
or search queries. Telegram and Markdown receive the same presentation.

The route is pinned with no automatic provider fallback or retry. Limits constrain
optional presentation, not article selection. Oversized input or exhausted allowance
keeps canonical publication with a visible status. The default total timeout is 90
seconds; explicit values up to 180 are supported. Include provider pacing in that
budget: a 65-second shared interval leaves little request time in a short budget.
A known wait beyond the remaining deadline makes no attempt record or request.
See [supplementary timing and canonical archives](decisions/0005-optional-presentation-translation.md#bounded-supplementary-presentation)
for the separate post-delivery allowance; configuration does not establish free quota.

Persist private `.cache/translations/` for primary reuse and the supplement's
`.translations/` records beside its canonical `.irritator.json` archive. Compatible
completed batches can be reused. An attempted failure or interruption ends translation
for that canonical version; fallback may already have been delivered. Inspect the
record before an explicit retry, and never delete delivery state to retry translation.
New canonical text, target, model or prompt gets a separate key. Dry-run uses temporary
translation storage and may still consume quota.

Structural validation preserves field IDs, numeric literals, URLs and recognized
quotation/code spans, not every aspect of meaning. Machine translation is labelled;
incomplete translation falls back to canonical English. Finite observed translation
and fallback evidence is recorded in [ADR-0005](decisions/0005-optional-presentation-translation.md#what-validation-does-and-does-not-establish).
It does not establish general fidelity or complete #55's editorial acceptance.

## Source discovery

Exploration areas are independent of the active professional source portfolio.

```yaml
discovery:
  exploration_areas:
    - fintech/banking/architecture
    - science
    - society/institutions
    - history/culture
    - environment
    - design
```

These are provisional defaults, not historical preferences or a required proportion.
Use 1–16 distinct nonempty names, up to 80 characters each. Each pass prefers the
least recently offered area, breaking ties in configured order. Empty/failed attempts
advance the pass without counting as offers. Requested areas are not verified
classifications or proof of novelty. Professional refresh stays eligible; cross-field
requests need no contrived professional connection.

Use the [source discovery workflow](OPERATIONS.md#source-discovery) to propose, persist and approve sources; configuration alone never activates one.

## Legacy format and optional features

The category-summary path supports `radar.summary_style` values `analytical`, `brief`
and `detailed`. Set `radar.perspectives: true` to request Optimist, Skeptic and Realist
views of significant topics; brief mode omits them. Views should supply different
reasoning, with short comments for minor items and cross-category trends for related
developments. The [retained illustrative format](history/readme-2026-10-08.md#digest-format-and-perspectives)
is not a factual news item or benchmark. Markdown includes Obsidian front matter.
Ordinary review-led preparation instead selects attributed cards from RSS evidence.
Neither format establishes that the complete source was read.

An optional humane closing item is implemented but disabled by default. It uses an
eligible, explicitly bound source and the existing review packet without another
selection call or reducing the main-card cap. Activating this optional feature requires
approved closing-source bindings and attribution review; its finite editorial/capacity
acceptance remains open. Follow [ADR-0014](decisions/0014-optional-humane-closing-item.md)
for exact settings, main-versus-closing translation/credit behavior and recovery.
Sparse supply is not a promise of a daily positive story.

## CLI options

Use `python -m digest --help` for the complete option list.

| Option | Behavior |
| --- | --- |
| No flags | Run the configured non-prepared scenario (category summaries or review-led cards); compact publication still requires its guard. The runtime owns Git persistence. |
| `--config PATH` | Read YAML configuration; default `config.yaml`. |
| `--dry-run` | Fetch/analyse and print results without normal Telegram/Markdown digest delivery; may call models. |
| `--radar-only` | Return before Irritator and normal digest delivery; may fetch/call models and, without `--dry-run`, poll feedback. |
| `--verbose` | Enable debug logging; inspect logs before sharing. |
| `--check` | Validate configuration, check expected environment variables and probe feeds. |
| `--feedback-precollected` | The managed runtime owns feedback ingestion; do not poll again in this process. |
| `--discover` | Propose/validate sources, persist candidates and send approval cards when configured. |

Prepared-edition flags use the [publication barriers](OPERATIONS.md#persist-claim-and-send).
Managed `--discovery-phase prepare|send` and its pending/delivery hashes also require
[separate proposal persistence](OPERATIONS.md#source-discovery).
Neither operation is made safe by a local hash alone.
