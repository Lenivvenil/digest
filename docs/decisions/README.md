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

[ADR0004](0004-durable-editorial-evidence.md) records the selected-source enrichment
implementation in [draft PR #93](https://github.com/Lenivvenil/digest/pull/93). It is not
a production release approval; real-output and operating-capacity gates remain open.

Choose the next unused sequence number after checking open PRs. If the local claude-mini
skill package is installed, its `next_adr_number.sh` helper can assist; that private tool
installation is not required to read, configure or run Digest.
