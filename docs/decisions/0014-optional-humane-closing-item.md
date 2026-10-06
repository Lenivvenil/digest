# 0014. Keep an optional humane closing item inside accepted preparation

Status: proposed; implemented, disabled by default, for
[#127](https://github.com/Lenivvenil/digest/issues/127). No source activation,
production rollout, daily-availability guarantee or editorial acceptance is implied.
The issue remains open.

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
