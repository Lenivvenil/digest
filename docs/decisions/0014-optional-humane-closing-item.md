# 0014. Keep an optional humane closing item inside accepted preparation

Status: proposed; implemented, disabled by default, for
[#127](https://github.com/Lenivvenil/digest/issues/127). No source activation,
production rollout, daily-availability guarantee or editorial acceptance is implied.
The issue remains open.

This reconciliation uses merged PR #139 (`e0740534`), including the reviewed
PR #138 editorial context, quantitative-qualifier and translation-v3 controls.
It does not activate closing or approve its source and attribution choices.

## Context

A short final story can offer kindness, relief, community connection, restored
access or everyday wonder beyond the usual professional agenda. It must remain
supported by evidence and preserve caveats. Feed membership and a literal matching
quote do not establish editorial quality. Main news must remain deliverable when
optional metadata, presentation or source eligibility fails.

## Decision

The compact review-led `--prepare-edition` path can opt in with an explicit list
of eligible feed bindings. The feature is incompatible with `reading_brief` in
this slice. It does not activate, fetch or approve any new feed:

```yaml
closing:
  enabled: false
  approved_sources: []
```

After separate source approval and attribution review, an operator can supply
exact `name`, `url` (feed URL) and `category` entries and enable the feature. Only
unambiguously matching, currently enabled sources qualify. Missing, disabled or
changed bindings grant no closing eligibility and do not block ordinary delivery.
No new licensing or approval framework is introduced.

Enabled configuration must explicitly allow at least `review.max_selections + 1`
in both `max_detailed_selections` and `max_evidence_articles`. The default remains
five detailed entries: enabling a five-main-card issue therefore requires a
separately reviewed setting of at least six detailed entries. Nothing silently
raises the 4096-token output allowance or lowers the main card cap. This checks
configured capacity, not actual evidence-byte fit, provider completion or a
guaranteed number of useful cards.

The Groq GPT-OSS strict schema adds its closed `closing` property only while
closing is enabled; the valid no-story value has `evidence_id: null`. Disabled
mode retains the current three-field response schema and request behavior. Local
ID, source eligibility, quote, detail-budget and unfinished-response checks remain.

Add a versioned `closing` designation to the existing primary/fallback review
response: `{"schema_version": 1, "evidence_id": "selected ID"}` or an ID of `null`.
Only a normally validated selected ID already admitted to that bounded evidence
packet can qualify. The existing reason and exact quote provide the content; no
second ranker, request, queue, reserved evidence slot or full-article prerequisite
is introduced. The prompt asks for concrete supported human-good events, avoids
promotion and speculative benefits, retains material caveats, and allows
abstention. A failed whole response remains a normal primary-review failure.
Malformed, conflicting, missing or unsupported optional metadata preserves valid
main selections. The designation always comes from the delivery-used slot;
a failed primary cannot supply a fallback's closing story.

Persist completed candidate work first. Then retain a separate bounded terminal
closing sidecar keyed by the exact canonical report hash, including selected card,
provider/slot, prompt/response/bundle hashes, exact evidence and source-occurrence
binding, or an explicit unavailable/incomplete decision. Missing or corrupt
sidecars mean incomplete omission on recovery, never another selection request.
Sidecar-only failures cannot erase accepted main work. CandidatePacket,
ModelReview and BlindReviewReport keep their original wire shapes and hashes.

Before accepting a new preparation, recheck its closing source against the
current exact allowlist. A selected closing identity is excluded before applying
the unchanged main-card cap. If exclusion would empty main, retain the original
main cards and omit the separate closing role. An empty selection does not create
a filler edition. Append at most one closing card as the final article, before the
existing notice/feedback footer, with no duplicate identity or title decoration.

Preparation v1 remains exact: verify its original envelope hash and decode its
seven original fields. Disabled writes retain those bytes and field shape. V2
requires a typed terminal closing decision and validates its report, evidence,
source occurrence, card and duplicate bindings. Generic strict decoding is not
relaxed. ArticleSummary retains its five fields. Accepted preparation and ready
editions do not consult current eligibility or rerun selection.

## Presentation and freeze

Build the normal main translation batches first. Only when the closing explanation
fits an existing batch's input allowance may it join that same request, without
displacing a main field or adding a request. Keep the existing call ceiling,
shared request state and absolute deadline. Validate main translation fields and
the optional closing field separately: malformed closing prose omits closing while
retaining valid translated main prose. Malformed main output keeps its existing
canonical-language fallback; a grounded canonical closing card can follow that
same fallback.

Combined requests have their own exact field/request/selection cache provenance;
they must not masquerade as legacy main-only requests. Disabled-feature request
and cache bytes remain unchanged. A matching cached combined response replays
validated main and closing outcomes without another call, including optional
omission. No rescue translation call or increased allowance is introduced. If optional
reservation metadata cannot fit, omit closing before dispatch and retain the
original main-only request. A combined-cache write failure after valid main
translation retains that main prose in memory, omits closing and leaves the
reserved attempt to prevent an automatic repeated request; it does not invent a
main-only cache record for a combined response. Required main-only persistence
failures keep their existing fallback behavior. As with existing main presentation,
changing request bindings before freeze is distinct
from replaying the same request; ready editions always retain their frozen bytes.

The default `translation.max_calls: 1` can therefore present main and closing from
one successful response when both fit. Oversized optional input is omitted before
dispatch and cannot displace main fields. Shared generation still has an output
limit: a truncated or invalid whole response follows ordinary main fallback. This
is field-validation isolation, not independently fault-tolerant model computation.
Real ordinary capacity and faithful translation remain activation gates.

Preflight required main rendering first; its errors still fail preparation.
Preflight the assembled optional card next and omit it if unsafe to render. Pass
the identical final card list to Markdown and immutable edition creation. Store
canonical closing provenance and actual presentation/omission in existing manifest
metadata; archive references bind the corresponding files by hash. Required
archive failures and corruption remain failures. The sender, article hash,
voting attribution, chunk coverage and delivery receipts are unchanged; no
post-freeze addition or separate Telegram push is introduced.

## Current preparation and translation boundaries

Keep technical empty selection as `selection_incomplete`, with its existing
nonzero CLI result after persistence. A missing or invalid optional designation
cannot turn valid main work into that failure, and null cannot turn all-deferred
main work into a complete editorial abstention. The deployed sender recovery and
reporting workflow are unchanged.

The reconciled combined translator uses presentation-translation-v3, including
its quantitative-qualifier instructions. Main-only cache bindings retain their
existing v3 shape; combined requests remain explicitly bound to required fields,
closing selection, route and output allowance. Cache schema2 is distinct from
prompt version3. No old record is rewritten or relabelled, and no second closing
translation call is introduced.

## Literal attribution support in this draft

Presentation supports two exact feed URLs: NHS England RSS and the Environment
Agency GOV.UK Atom feed. It does not infer permission from an article hostname or
normalize another URL into an approved binding. The selected canonical card and
frozen occurrence identity/hash must agree. Unsupported or inconsistent attribution
omits only the optional presentation with `attribution_unavailable`.

After translation, append the reviewed literal as one paragraph to the closing
presentation summary. NHS uses `NHS England RSS feeds` and an OGL v3 link. Ordinary
Environment Agency news uses the OGL default statement, `Contains public sector
information licensed under the Open Government Licence v3.0.`, and its link, with
the existing Environment Agency identity and article URL retained. Legal credit is
not generated or translated by a model. Canonical reasons, evidence, source names,
article hashes, feedback bindings and translation cache records are unchanged.

Preflight the actual assembled rendering. Include the optional article only when
its entire rendered range, including credit, occupies one chunk. Otherwise record
`attribution_split` and retain the main list unchanged. This avoids exposing a
story whose required credit is stranded in a failed later chunk. It does not add
a sender path, a whole-issue chunk limit or padding; placement can therefore omit
an otherwise short closer. The identical attributed card enters the existing
Markdown archive and immutable edition; the sender still uses its frozen bytes.

The same presentation function also credits ordinary main cards from these feeds:
feed activation is not a closing-only admission rule. Resolve their exact source
occurrences from the existing immutable candidate packet keyed by the accepted
report hash, before any model or translation call. Never infer a feed from a source
name or article hostname. Add credit after translation without changing canonical
summaries, identities, feedback bindings or translation cache records.

If an attributed main item would span chunks, fail preparation before archive,
freeze or send and retain its accepted canonical result. Do not silently drop a
useful main card to satisfy optional placement. If optional insertion would break
credit coverage, omit the optional card and retain the preflighted main list.

Accepted legacy reports can lack candidate packet objects. That recovery remains
supported when no supported attribution feed is enabled and the snapshot did not
use the closing contract. In that legacy-only branch, absent or unreadable optional
packet proof retains the original accepted presentation without inferred credit;
a warning records failed optional inspection. Existing archive validation still
fails on corrupt bound evidence when archival output requires it. Valid retained
packets can restore attribution even after source configuration changes.
Under the new feed configuration or a closing-enabled
snapshot, absent packet proof instead holds preparation with an explicit restore
path; it never guesses source identity, resets editorial status or reruns selection.
Enable feeds only after in-flight unfrozen legacy preparation is reconciled. Ready
and confirmed editions bypass preparation and retain their immutable recovery
behavior across configuration changes. These are pre-freeze integrity rules, not a
new queue or a requirement for full-article processing.

These literal bindings are presentation support, not source activation or a legal
classifier. OGL excludes personal data and unauthorized third-party rights; NHS
also has photo/logo and case-study exceptions. Required attribution does not
establish humane value, factual fidelity or permission for excluded material.
Source terms: [NHS](https://www.england.nhs.uk/terms-and-conditions-2/),
[GOV.UK feeds](https://www.gov.uk/help/terms-conditions),
[OGL v3](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/).

## Unapplied source and capacity recommendation

A minimal activation proposal is these two text-only feeds with explicit seven-day
recency, allocation priority 1, and a community/everyday-life category. Add their
exact bindings to `closing.approved_sources`, retain the main five-card cap, and
explicitly set `review.max_detailed_selections: 6`. Keep the existing 20-item packet,
4096 review tokens, shared calls, deadlines and translation allowance. This is an
unapplied recommendation requiring source/config approval; no existing professional
source is replaced or silently reweighted. Priority is not a semantic role label.

The October 7 public feed sample contained five Environment Agency entries within
seven days (two within 24 hours), with one clear completed-action candidate; NHS
contained two entries within seven days and none within 24 hours. These feeds
provide occasional material, not a daily guarantee. The seven-day window is an
explicit proposed freshness tradeoff, not a claim old news was published today.
Normal identity deduplication prevents repeated publication of the same item.

The Lymington Atom title and summary support installed tidal flaps and refurbished
flood-gate seals, with intended reliability/maintenance benefits. They do not
support whole-system restoration. Separate article research found an unfinished
regulating-valve repair outside the supplied summary. Do not introduce that as a
model-known fact or widen the selected claim; no mandatory article-fetch gate is
added. NHS waiting-time reductions are a reported result; vaccination availability
is access, not proof that the advertised population already received doses.

Story availability, packet admission, selected relevance, faithful presentation
and successful delivery are separate outcomes. The inherited #139 source turns
and retry reservation do not reserve a closing slot. The current packet may
contain no suitable story even when the wider feed has one. No new ranker or
admission redesign is part of this draft.

## Activation and acceptance still required

Source-specific terms, permitted access, item exceptions and required attribution
must be reviewed before activation. Existing presentation provides title, article
link and source attribution. Sources needing additional author, licence,
translation/adaptation or other credit cannot be enabled for closing until that
presentation is supported and reviewed. An allowlist entry is not a legal or
editorial-quality assertion. The baseline RSS occurrence retains its supplied
publication timestamp; it does not infer original versus translation/update dates,
authors or licence metadata that the collector does not supply.

Offline tests cover optional salvage, exact fallback capture, source eligibility,
unchanged main capacity, duplicate/sole-selection behavior, v1/v2 compatibility,
interrupted capture recovery, isolated translation limits, render omission,
archive/freeze parity and partial multi-chunk receipt attribution. They do not
prove faithful humane selection or translation. Before activation, review a finite
real editorial sample and measure ordinary request, time and presentation capacity.
No suitable story in one bounded packet is not evidence that the whole inventory
contains none; the mechanism cannot guarantee a daily closing story.
