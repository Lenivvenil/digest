# 0023. Resolve ordinary editorial authority from response-owned attempts

Status: decision record for [#181](https://github.com/Lenivvenil/digest/issues/181).
The implementing PR and issue record verification and rollout separately. This
changes internal application contracts, not the saved report or packet schemas.

## Context

Ordinary review returned a comparison report while mutating two caller-owned
capture lists. The report, candidate dispositions and optional closing designation
all described the same response, but downstream code had to join them by slot.
Candidate accounting, recovery, closing and presentation selected a delivery review
again. A comparison could remain incomplete while valid primary cards were already
usable. Treating that report status as the editorial decision was misleading.

This was accidental complexity in the live protocol. In contrast, revalidating
untrusted persisted records and distinguishing raw selections from publishable
cards are necessary boundaries. Removing those checks would change the product.

## Decision

A model attempt returns its review, same-response dispositions and optional closing
designation together. The ordinary application no longer supplies mutable capture
objects. One resolver selects the authoritative primary/fallback attempt and derives
its editorial outcome. Repository callers receive that result instead of supplying
a selected attempt and outcome independently. The normal frozen record is constructed
only by the private resolver; this is an ownership convention, not an access-control
mechanism or a claim that its nested model review is deeply immutable.

The outcome distinguishes validated raw selections, genuine primary abstention and
incomplete selection. It does not certify that card projection can publish anything.
Main-card capacity, exact source attribution, optional closing eligibility and
renderability remain subsequent decisions. A closer cannot create a filler edition
without main cards. Capacity omission does not become editorial rejection.

The comparison report remains the existing audit projection. Its unattempted
secondary placeholder is not a completed provider attempt. Independent comparison
can remain pending without blocking useful primary or fallback selections.

Fresh ordinary attempts always retain disposition evidence. A historical packet
with no recorded dispositions is explicitly different from a packet whose recorded
attempts omit the chosen slot. Only the former retains the old no-capture behavior.
Historical report ordering and category-preparation semantics are handled at their
compatibility boundary rather than imposed on fresh primary/fallback execution.

## Preserved contracts

- Primary-first execution, fallback conditions, accepted partial selections and the
  existing refusal to accept fallback abstention as a completed no-news decision
- Exact request messages, provider routing, retries, model budgets and time sampling
- Existing report, packet, closing sidecar and preparation fields and hash encodings
- Duplicate-slot and exact bundle/prompt/response/occurrence checks when restoring
  persisted evidence; current source eligibility is checked separately
- Candidate accounting and proof persistence before accepted-work handoff, verified
  preparation readback, archive/freeze ordering and immutable ready-edition recovery
- Optional-closing failures remain optional; primary provenance and delivery holds
  retain their existing failure behavior

The removed capture classes and their reexports were internal execution seams.
Repository callers and tests move to the owned result in the same change; retaining
an alternate mutable live protocol would preserve the ambiguity this decision removes.
Documented CLI commands and retained-state recovery remain supported.

## Verification and limits

Existing behavior checks are retargeted to the owning contract, with representative
application checks retained for provider calls, persistence, recovery and failure
ordering. Finite before/after traces compare request messages, archived report and
disposition data, projected cards, acceptance and clock calls. Tests protecting only
the deleted helper protocol do not become compatibility requirements.

This is a bounded simplification of ordinary editorial authority. It does not remove
all category or experimental source-reading modes, certify generated prose, establish
useful counter-evidence, or guarantee a suitable positive closing story. A compatible
engine revert can restore the previous implementation without rewriting saved state.
