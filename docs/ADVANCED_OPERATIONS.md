# Continue or investigate saved work

Inspect unfinished candidate work or choose an optional investigation; each task has its own evidence, allowance and hold rules.

- [Inspect candidate continuation](#candidate-continuation) after incomplete ordinary selection
- Optional: [Compare independent reviews or resume a report](#independent-comparison-and-resume)
- Optional: [Investigate delivered claims](#supplementary-investigation) after durable primary publication
- Optional: [Experiment with source reading](#experimental-source-reading), off by default

For ordinary preparation, publishing and interruption recovery, use [Operations](OPERATIONS.md).
These tasks never authorize a resend or a reset of retained delivery state.

## Candidate continuation

<a id="ordinary-preparation-candidate-accounting"></a>

Keep `.cache/candidate_progress.json` with its immutable source/report objects and
indexed decisions. The archive's `.candidates.json` file is a bounded as-of account
of one report, not a continuously rewritten inventory of every historical body.
Missing required objects fail explicitly. Do not clear progress or rewrite old
rejections to claim complete coverage.

The review instruction evaluates substantive supplied information before reader
relevance: a concrete development, finding, explanation or usable resource. A
relevant question or promised discussion alone is insufficient. Concrete future
announcements remain eligible, with attributed claims and plans distinguished from
achieved outcomes. `deferred` is reserved for otherwise useful items exceeding the
detail budget; insufficient substance or relevance is an editorial `not_selected`
judgment about the supplied metadata, not the unread full article.

These are model instructions, not semantic guarantees enforced by quote validation.
The [#55 correction](https://github.com/Lenivvenil/digest/issues/55) changes prompt
identity for future review; it neither reopens prior decisions nor proves improved
selection before a new ordinary output is inspected.

Candidate progress is saved before a review call. Admission works within the configured
count, character, retry-opportunity and storage bounds; it does not promise to drain
an entire feed cohort in one run. Missing/invalid dispositions and capacity-only
omissions remain unfinished. Policy exclusions, duplicates and editorial not-selection
retain distinct evidence.

The protected first-unseen opportunity uses original identity observation age,
then existing priority/source/identity ties, and skips evidence that cannot fit.
Technical retries and the remaining fresh/age source turns follow within the same
packet. At a one-item cap this deliberately favors age over freshness; a larger
old excerpt can leave less room for later items. Planning is only an opportunity,
not a completed review. See the [oldest-unseen amendment](decisions/0008-candidate-selection-progress.md#oldest-unseen-opportunity-amendment-196).

With optional closing enabled, an otherwise absent approved source can receive
one fitting review opportunity. Spare capacity is used first; a full packet may
defer only its final ordinary backfill item while preserving the first unseen
opportunity and reserved technical retries. The deferred item stays pending.
Count and character limits do not increase, and no fit means no substitution.
An already admitted approved occurrence leaves the packet unchanged. This can
postpone a professional item and does not promise a suitable positive story;
see [the bounded admission decision](decisions/0014-optional-humane-closing-item.md#bounded-closing-source-opportunity).

## Independent comparison and resume

<a id="contract"></a>
<a id="resume-a-report-only-comparison"></a>

Independent comparison differs from ordinary primary/fallback preparation. Primary
and secondary slots receive the same frozen evidence and messages and keep their
configured provider/model identities. A failed slot stays failed; another review is
not relabelled as its opinion. Valid complete results may trigger one configured third
review according to overlap. Partial results remain incomplete and do not establish
comparison agreement.

**This manual command can call configured providers.** Use a fresh output path; the
original report remains intact.

```sh
python -m digest.review_trial --config config.yaml --resume previous/review.json --output fresh-output
```

Reuse requires matching slot/provider/model,
bundle and current prompt identity plus valid retained selections. Accepted canonical
preparation and frozen editions are not reinterpreted under this new-request policy.

The scheduled `digest.review_resume` path has separate prepare/execute phases and a
separately persisted one-attempt marker. The managed runtime persists the marker
before execution; execution records its start in that marker before provider work.
Its bounded results are archived as supplements; they do not create another primary
Telegram edition or alter its deduplication outcome. Follow the current configured
request allowance rather than assuming comparison is free.

## Supplementary investigation

<a id="primary-first-runtime-with-preserved-irritator"></a>

After confirmed, durably persisted primary delivery, supplementary work can use the
saved evidence checkpoint under a separate reservation. Irritator extracts an attributed
target, plans queries, searches external sources, validates results and ranks their
relationship to the target. New compact schema-3 attempts bind canonical cards from
the exact confirmed/applied edition and their original source occurrences. They retain
exact card/source quotations in query and ranking inputs and exclude speculative
hypotheses. Unbound standalone contracts remain available separately.

Empty search results, unavailable/unsupported adapters, rejected evidence and failed
model stages are different outcomes. Optional translation failure retains canonical
fallback according to its existing contract. An accepted compact result can freeze one
fragment for an ordinary edition on origin day D+1..D+3, after main cards and before
the closer. No extra dispatch or model work is created. Unusable material remains
explicitly archive-only; partial searches retain their limitations. Actual UTC D+4
expires unused material; a future ineligible edition does not expire it early.
The legacy standalone transport retains its separately documented behavior.

Fragment editions use ready schema 3 with independent supplement chunk coverage and
an explicit current-review checkpoint. Complete owner-matching coverage must be
consumed before the applied receipt marker. Claimed/unknown publication is never
automatically released or replayed. The runtime must persist the existing consumed
attempt under `digests/` with `.cache/` receipts and provide the private owner ID to
post-prepare. See [ADR0007](decisions/0007-compact-issue-reservation.md) for the complete
rollout and unclaimed-ready release contract.

New post-attempt markers bind the versioned Hacker News/arXiv/DEV search policy,
effective source list and query ceiling. DEV uses the documented unauthenticated
Forem V1 first-page search; it does not fetch full articles. Operators enable that
bounded slot explicitly in `irritator.sources`. Up to nine real source requests
can now occur within the existing three-by-three ceiling and stage deadline.
See [ADR0022](decisions/0022-versioned-bounded-search-policy.md) for the exact
contract, validation and historical-policy boundary.

An older compact attempt marker or a changed source-policy binding is held before
execution. Preserve it and inspect its original engine/configuration; do not
remove markers or archives to rerun an old edition. Completed attempts and saved
results remain protected. Confirm no pending old-policy attempt crosses a rollout.

Independent comparison and genuine external counter-evidence are separate operations.
Neither a matching quotation nor successful transport establishes factual usefulness.
The [Irritator model](domain/irritator/overview.md#current-domain-model) describes the
source/evidence relationship; [transport compatibility](STATE_AND_EFFECTS.md#transport-protocols)
describes the different sending protocols.

## Experimental source reading

`reading_brief` is off by default and requires English canonical text, review-led
selection and an explicit model route. It runs only through `--prepare-edition`;
unsupported preview/direct-publish modes stop before source/model work. Completed
source pages are a technical evidence handoff, not an accepted or published edition.
The former standalone reading-angle renderer and delivered-marking Python helpers
have been retired; they were not a supported CLI path. Existing source snapshots and
historical delivered records remain readable. See the
[internal API compatibility note](../CHANGELOG.md#unreleased--reliability-rehabilitation).
For the immutable reconciliation input, saved operation proof and parser limits,
see [Reconciliation ownership](#reconciliation-ownership).
The unused `digest.reading_points` grouped-point prototype is retired with immutable
historical links in [ADR0010](decisions/0010-group-source-points-with-qualifications.md#retire-the-unused-grouped-point-prototype--2026-10-09).
Direct Python callers must follow the [ADR0009 migration](decisions/0009-selected-source-admission.md#one-immutable-reconciliation-authority--2026-10-09).
Unknown generation outcomes remain held across invocations and route changes.
The [accounting guide](reading-brief-accounting.md) describes verified profiles,
optional offline tokenizer preparation and the bounded configured fallback. Unknown
profiles remain technical pending; advertised context does not establish free quota.
Proposed [ADR0009](decisions/0009-selected-source-admission.md) retains the open
factual-quality, reconciliation and throughput gates. The separate
[closed, unmerged PR #93](https://github.com/Lenivvenil/digest/pull/93) is not on main.

### Reconciliation ownership

Experimental source preparation reloads the current handoff, source, page state and
eligibility through [`reconciliation_checkpoint`](../digest/reconciliation_checkpoint.py).
[`build_reconciliation_input`](../digest/reading_reconciliation.py) then validates and
freezes one evidence input for offline planning and execution. The
[`reconciliation_operation`](../digest/reconciliation_operation.py) owns its exact
request, admission, attempt history and completion envelope; its decoder verifies
persisted records against the fresh input before cache reuse, uncertainty holds or
fallback. Fresh generation saves accepted metadata before validating terminal output.
The content parser only checks response schema, citation membership and retained
qualifications. Complete source access and sparse evidence remain distinct; none of
these checks establishes semantic completeness or publication acceptance. Reading
remains optional and off by default. [ADR0009](decisions/0009-selected-source-admission.md#one-immutable-reconciliation-authority--2026-10-09)
records the direct-call migration and retained trust boundaries.
