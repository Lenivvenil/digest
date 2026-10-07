# Architecture decision records

Decisions use the MADR format and numbered files. Preserve the original decision,
status and rationale; a translation or implementation observation does not change
who approved it.

## Existing decisions

- [0001](0001-adopt-claude-mini-governance.md) — Adopt the claude-mini engineering workflow
- [0002](0002-engine-instance-split.md) — Separate the public engine from runtime instances
- [0003](0003-source-state-split.md) — Separate source configuration from lifecycle state

These historical records retain their original language and decision context. Current
English explanations are in the [architecture guide](../ARCHITECTURE.md) and canonical
[domain overview](../domain/digest/overview.md).

ADR0004 is proposed/implemented in [draft PR #93](https://github.com/Lenivvenil/digest/pull/93),
which is not merged into main. Its existence in a draft is not a release approval.

Choose the next unused sequence number after checking open PRs. If the local claude-mini
skill package is installed, its `next_adr_number.sh` helper can assist; that private tool
installation is not required to read, configure or run Digest.

[0005](0005-optional-presentation-translation.md) records the implemented opt-in
translation of primary and supplementary generated prose under #94. Narrow real
verification and ordinary translated output do not establish universal semantic fidelity.

[0006](0006-batch-message-voting.md) records batch-compatible message voting and the
150-second callback queue limitation. Real message ingestion, persistence, acknowledgement
and computed priority influence are verified; daily retention limitations remain explicit.

[0007](0007-compact-issue-reservation.md) records compact daily presentation and its
coarse pre-publication reservation. Local implementation is under review; no automatic
resend lifecycle or exactly-once guarantee is implied.


[0008](0008-candidate-selection-progress.md) proposes candidate accounting and
continuation within ordinary bounded preparation under #121. It is not a deployment
or editorial-quality approval.

[0009](0009-selected-source-admission.md) proposes candidate-bound source admission
and technical handoff under #122; it does not approve semantic publication or runtime activation.

[0010](0010-group-source-points-with-qualifications.md) proposes an offline grouped
source-point representation for #55. It does not approve a new prompt or publication path.

[0011](0011-source-anchored-investigation-queries.md) revises the unaccepted mandatory
literal-query guard into optional provenance diagnostics for bounded RSS and full-source
investigation under #77. Useful grounded searches need no copied phrase; neutrality and
useful retrieval remain empirical gates. The optional-provenance revision is accepted and deployed; full-source activation remains separate.

[0012](0012-private-ranking-evidence.md) records accepted bounded candidate/admission and
validated ranking-decision evidence in the existing private JSON archive under #77.
The private archive trace is deployed; recorded decisions are not semantic acceptance.

[0013](0013-discovery-exploration-state.md) records the accepted first slice: configurable exploration areas,
fair passes with least-recent-offer preference, and finite pending-feed validation
cooldowns in existing discovery metadata under #132. Approval-to-candidate protection
and ordinary recommendation quality remain open; owner approval on 2026-10-06 covered PR #133 and its engine-pin rollout.


[0014](0014-optional-humane-closing-item.md) proposes a disabled-by-default humane
closing designation in the existing primary selection response, strict preparation
v1/v2 compatibility and optional presentation before immutable freeze. Source
activation, translated capacity and semantic acceptance remain open under #127.
