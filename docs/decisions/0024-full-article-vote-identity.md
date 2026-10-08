# 0024. Preserve full article identity in future votes

Status: reviewed decision for [#199](https://github.com/Lenivvenil/digest/issues/199),
recorded before implementation. Verification and deployment are separate issue/PR
evidence. This amends the article-vote wire boundary retained by
[ADR0021](0021-catalog-feedback-boundaries.md); source-proposal identifiers do not change.

## Problem

Article identity is a full 32-character hash, but vote links and attribution used
its first eight characters. A finite synthetic reproduction found two distinct
article identities with the same prefix. After both confirmed deliveries, a vote
from the first article's original link credited the second source. Two such
articles from one source also collapsed into one latest vote. Ownership checks
still rejected another user. This is an attribution defect, not an authentication
bypass or an observed production incident.

## Future publication contract

New card and compact publication uses the existing full article identity in its
vote links, manual commands and confirmed attribution. A shared pure token rule
maps a full identity under the explicit `legacy8` or `full32` protocol; rendering
and coverage projection use that same rule. Article vote parsers accept exactly
eight or 32 lowercase hexadecimal characters for callbacks, `/start` and `/vote`.
Source proposals retain their separate eight-character contract.

The full callback and deep-link payloads are 39 ASCII characters. They fit
Telegram's [64-byte callback limit](https://core.telegram.org/bots/api#inlinekeyboardbutton)
and [64-character deep-link limit](https://core.telegram.org/bots/features#deep-linking).
Longer manual identifiers may change chunk boundaries; existing chunk and delivery
bounds still apply. No model request, new provider or source activation is added.

`article_source_map` means the exact delivered vote token mapped to its source;
`delivered_hashes` remains full identity. Source-inclusion accounting first looks
up the full identity in the delivery outcome, falling back to its short token only
when the full key is absent. It never joins against the mixed historical feedback
store or manufactures a short alias for a new full-token delivery.

## Frozen edition compatibility

New ready editions use schema 2, which declares `full32`. Ready readers explicitly
accept schema 1 (`legacy8`) and schema 2. Claim and receipt schemas remain 1; their
fields, immutable bytes and ready/claim hash bindings do not change. Existing
edition, article, claim, receipt and feedback dataclass shapes remain unchanged.

Projection follows the actual ready version, including partially confirmed chunk
coverage. A previously frozen schema-1 edition keeps its original short buttons
and attribution. The sender transmits its stored payloads without re-rendering.
Existing manifest shape, owner, window and hash checks remain the trust boundary;
this amendment does not add a second block parser or promise detection of arbitrary
payload changes accompanied by recomputed hashes. Builder-to-frozen-send checks
prove ordered token/article association using the shared token rule.

A schema-1 edition containing distinct full identities with the same short prefix
cannot be newly dispatched. One pure dispatch check runs before claim creation and
before first receipt creation/POST. General history loading remains unchanged,
and confirmed/applied no-resend recovery returns before this check. Historical
confirmed records remain readable; unknown, partial and unapplied states retain
their existing holds. No receipt or payload is rewritten to repair history.

## Feedback history and limits

Existing feedback fields already store string keys and rating identities. New
publications add full-token entries; completing an existing schema-1 publication
may still add its actual short-token entry. Retain raw historical votes and source
bindings, exact-token latest-vote aggregation, and the existing 1,000-entry map
retention. No binding table, dual aliases, tombstones or guessed migration is added.

An old short vote and a full-token vote cannot safely be inferred to identify the
same article. They remain separate stored-token namespaces and may both influence
a source's score. Dedup retention is seven days, scoring spans 14 days, and an old
button can produce a new vote, so this is not a guaranteed brief migration window.
Existing legacy-prefix ambiguity is not repaired or permanently detected after
retention. Raw history is neither merged nor excluded on a guessed identity.

Owner checks, callback/message replay namespaces, cursor behavior and
collect-before-persist-before-ack ordering remain unchanged.

## Deployment and rollback floor

The old ready reader rejects schema 2, providing a conservative sender rollback
boundary. The old feedback codec can retain full-token strings, but its poller
rejects full-token commands as malformed and can advance the cursor past them.
Codec compatibility alone therefore does not make an old binary safe to run.

Before any full-token publication, every active sender and feedback collector must
use a compatible reader. The known runtime workflows install the engine from one
immutable requirements pin; verify all entrypoints and absence of in-flight jobs
at deployment. After publication, rollback requires a compatible-reader build or
backport for both sending and polling, not an arbitrary previous engine pin.
This is an operator compatibility constraint, not a new hidden enforcement service.

## Verification

Retain the existing owner, replay, strict persistence, partial/unknown delivery and
receipt safeguards. Use the fixed synthetic identities from the defect proof to
check different-source attribution and same-source latest-vote scoring through the
three existing vote routes. Prove full-token source inclusion and partial coverage,
old/new feedback roundtrip without aliases, ready-v1 compatibility, ready-v2
roundtrip, old-reader rejection and the dispatch-only legacy hold. Preserve the
confirmed/applied historical no-resend path. Reuse existing cases where they already
own the contract; do not add a combinatorial protocol matrix.
