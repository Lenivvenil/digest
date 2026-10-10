# Documentation

Choose the task in front of you. Each route leads to the guide that owns the detail.

<a id="operate-a-runtime"></a>

## Try or run

- **[Try the safe example](../README.md#try-it-safely)** — install and run an offline,
  empty-input check
- **[Set up a live runtime](BLIND_REVIEW.md#runtime-configuration)** — keep secrets,
  source configuration, schedules and saved state separate from the engine
- **[Choose models and sources](BLIND_REVIEW.md#model-routes)** — configure entitled
  routes, then [source categories](BLIND_REVIEW.md#sources-and-categories) and
  [language](BLIND_REVIEW.md#language-and-optional-post-translation)
- **[Find a command](BLIND_REVIEW.md#cli-options)** — previews can call external services;
  compact publishing needs the managed sequence below

## Operate or recover

- **[Prepare, persist, claim and send](BLIND_REVIEW.md#persist-claim-and-send)** — follow
  the publication barriers in order
- **[Inspect an interrupted edition](BLIND_REVIEW.md#delivery-states-and-recovery)** —
  distinguish ready work, uncertain sends and unapplied confirmations
- **[Upgrade with retained state](BLIND_REVIEW.md#upgrades-and-retained-state)** —
  check compatibility before changing or rolling back the engine pin
- **[Manage feedback and sources](BLIND_REVIEW.md#feedback-and-source-decisions)** —
  handle votes, approvals and [source discovery](BLIND_REVIEW.md#source-discovery)

<a id="contribute-a-change"></a>
<a id="understand-the-architecture"></a>

## Understand or contribute

- **[Follow one edition](domain/digest/overview.md#one-story-through-the-system)** —
  candidate, accepted work, ready edition and confirmed delivery
- **[Explore the architecture](ARCHITECTURE.md#prepared-edition-data-flow)** — current
  lifecycle and ownership; [target and migration](ARCHITECTURE.md#target-architecture-and-migration)
  remain distinct from implemented guarantees
- **[Understand counter-evidence](domain/irritator/overview.md#current-domain-model)** —
  independent external evidence and its relation to the original claim
- **[Make a contribution](../.github/CONTRIBUTING.md)** — workflow and checks, supported by
  [project principles](principles.md#definition-of-done) and the [decision index](decisions/README.md)

Daily editorial usefulness remains under validation in
[#55](https://github.com/Lenivvenil/digest/issues/55). Use that acceptance record rather
than historical release entries to assess current quality.

<a id="documentation-map-and-status"></a>
<a id="historical-material"></a>
<a id="current-work-boundary--2026-10-07"></a>

<details>
<summary><strong>Historical material and older documentation links</strong></summary>

Older status links land here. The following preserve their dates, original language
and decision context; they are not current setup instructions:

- [Prior README](history/readme-2026-10-08.md),
  [Digest domain record](history/digest-domain-2026-10-08.md),
  [architecture/migration record](history/architecture-2026-10-08.md) and
  [review/operations record](history/review-operations-2026-10-08.md)
- [Original ADRs and later decisions](decisions/README.md) and
  [completed implementation plans](plans/completed/)
- [Superseded Irritator diagnostic](domain/irritator-bc.md),
  [terse summaries and bubble plan](plans/completed/terse-summaries-and-bubble.md) and
  [Reddit QA report](history/reddit-qa.md)
- [April 24 historical output](history/output/2026-04-24.md) and
  [changelog](../CHANGELOG.md), retained as history rather than current quality evidence

Original versions of translated documents remain in Git history. ADR bodies and
historical issue/PR discussions retain their original context.

</details>
