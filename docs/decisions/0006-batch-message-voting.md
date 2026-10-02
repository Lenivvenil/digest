# ADR-0006: Message-based voting for a batch-only runtime

Status: accepted and deployed; real message ingestion, durable persistence and computed priority influence verified. Ordinary collector acceptance remains open in #48.
Date: 2026-10-02.

## Context and evidence

The owner requires a free GitHub Actions runtime and has no infrastructure for a
continuously available receiver. Durable storage cannot rescue events that expire
before the receiver polls. The previous assumption that callbacks remained queued
for a day was incorrect; acknowledgement expiry is a separate issue.

The official Telegram server at immutable commit
[e3e9dd8](https://github.com/tdlib/telegram-bot-api/blob/e3e9dd8e5b3d7ab8537cd5a10dc31d5ffa8f82d1/telegram-bot-api/Client.cpp#L18577-L18580)
enqueues callback queries with a 150-second lifetime. The
[queue implementation](https://github.com/tdlib/telegram-bot-api/blob/e3e9dd8e5b3d7ab8537cd5a10dc31d5ffa8f82d1/telegram-bot-api/Client.cpp#L18412)
adds that lifetime to the current time. Ordinary messages instead use
[message time plus 86400 seconds](https://github.com/tdlib/telegram-bot-api/blob/e3e9dd8e5b3d7ab8537cd5a10dc31d5ffa8f82d1/telegram-bot-api/Client.cpp#L18474-L18478).
The public API's “no longer than 24 hours” wording is an upper bound, not a guarantee
for every update type. The finding is recorded in [#48](https://github.com/Lenivvenil/digest/issues/48#issuecomment-5954662300).

## Decision

Keep the existing batch runtime and transactional collect→persist→ack boundary.
Use ordinary owner messages for article votes: `/start vote_g_<8hex>` or
`/start vote_b_<8hex>`, with `/vote g <8hex>` and `/vote b <8hex>` fallback commands.
Optional `telegram.bot_username` enables URL buttons; it must identify the same bot
as the existing token. No per-card API lookup or new secret is introduced. With no
username, show command instructions without generating unreliable callback buttons.

The official [client link contract](https://core.telegram.org/api/links#bot-links)
requires users to tap Start after opening the vote link, even if the bot was already
started. Tell users about both taps and next-run processing. Do not promise instant
acknowledgement or describe this as one-click voting.

Require the configured private owner and a known article hash. Keep latest effective
vote semantics, bounded receipt retention and raw votes out of model prompts.
Persist before replying or advancing the confirmed offset. Send one bounded vote receipt
per batch, reporting saved and unknown-article counts without identities or vote values.
Deduplicate ordinary vote
messages separately from callback IDs. Retain legacy callback parsing as best effort.

## Alternatives and consequences

A continuous webhook/long-poll receiver would preserve immediate callback UX but
requires additional runtime/access decisions; it is not provisioned here. Native
reaction updates require chat-administrator status and explicit subscription, so
are not assumed to support this private-chat contract. Replying with an emoji would
need trusted persisted message-ID attribution; do not infer identity from quoted text.

Message retention remains bounded to at most 24 hours. Missed or delayed scheduled
runs can still lose uncollected votes. The UI step changes; stored vote history,
source weighting, approved source lifecycle and article identity do not.

## Verification

Offline checks cover URL payloads, command parsing, ownership, known identity,
message replay, persistence failure and latest-vote scoring. Real acceptance requires
one owner message vote to survive delayed collection, durable commit and reload,
and affect the defined source-priority/candidate path. No test fixture or successful
API send alone closes that gate.


## Daily scheduling constraint clarified — 2026-10-02

The owner's later daily-edition/budget instruction does not extend Telegram retention.
A single daily poll has zero timing margin against the at-most-24-hour ordinary-message
limit. GitHub explicitly permits [delayed and dropped scheduled runs](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).
Moving the cycle to 02:17 UTC avoids the top-of-hour burst but does not provide
a delivery SLA. Therefore message voting remains best effort between daily runs; the
successful real vote check is not proof that all future votes will survive.

No extra feedback-only cron or continuous receiver is approved in this operating
proposal. Any later reliability measure must have an explicit bounded cost and
preserve the existing persist-before-ack contract. This clarification preserves the
original two-tap decision and its source evidence; it does not restore callback voting
or claim that the 150-second lifetime was only an acknowledgement/UI timeout.
