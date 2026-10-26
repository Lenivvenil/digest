# Native source-discovery prefill

This opt-in adapter replaces **only suggestion generation** in the existing weekly
source-discovery job. It does not schedule or launch a worker, call a model API, fetch
sources during export/binding/reservation, activate a source, or send a message.
The ordinary provider-backed path remains the default. Selecting
`--discovery-native-prefill` makes generation native-only, including when its path or
bindings are empty, missing, stale or invalid: there is **no provider fallback**.

The adapter is not an installed runtime integration. A reviewed private-runtime change
must connect the existing native controller and weekly job to these steps. Keep the
same weekly job, concurrency discipline and **ten-minute execution ceiling**; do not
create another runner or recurring task. The one-slot ordinary native pilot is separate
and is not changed by this feature.

## Responsibilities and trust

| Owner | Responsibility |
| --- | --- |
| Existing native controller | Retain one durable launch intent; launch at most one worker for it; observe the real worker/terminal result; preserve unchanged output and attest its actual execution reference; publish exact data-only artifacts through the reviewed private-runtime path. |
| Native worker | Read the immutable request, treat source names/URLs/history as untrusted data, and return zero to three ordered suggestions. No browsing, feed HTTP, model API, source-state edits, credentials, approval or send. |
| Existing weekly job | Reserve that exact input under its current real run+attempt, persist/read back the reservation, prepare/validate the remaining candidate prefix, persist the original pending/delivery pair, and use the original sender. |
| Human source owner | Continue the existing bound Add/Reject process; a suggestion or valid RSS response is not source activation. |

The engine verifies schemas, exact hashes, context, freshness, identity attestations
and one-attempt state. It cannot independently authenticate a native service worker.
The trusted controller must obtain `execution_ref` from an observed worker receipt,
not from text claimed by the worker, a Git author/committer name, or an arbitrary input
file. The prefill SHA and controller identity supplied to the weekly job must come
from the trusted private-runtime publication path, not from untrusted source content.
Hashes bind bytes; they do not establish author identity or semantic quality.

**Ambiguous launch/result:** preserve the existing launch intent and evidence. A missing
worker ID or result after a possible launch is a hold, not permission to launch another
worker. The request export is not a launch receipt. Repeating export returns the exact
existing request only when its context/slot/owner still match; it never regenerates a
response. Worker launch bookkeeping belongs to the existing native controller.

## Dated, immutable request

Use the real scheduled UTC start, expressed as a whole minute, and the actual controller
identity. These are illustrative commands, not an activation instruction:

```sh
python -m digest.discovery_native export \
  --config config.yaml --slot "$SCHEDULED_UTC_SLOT" --controller "$CONTROLLER_REF"
```

Export prints the retained request path and SHA256. It reads strict pending/delivery
state and previews pruning and area rotation on copies. It does not write either legacy
state file or advance `attempted_areas`. The semantic snapshot binds:

- all configured source fields, including disabled sources;
- enabled categories/source names and all configured URLs;
- exact pending identities, recent decision identity/membership, exploration policy and area state;
- the selected requested area and exclusions used by the existing prompt.

This is not a whole-repository fingerprint: unrelated archive/main commits and ordinary
model configuration do not invalidate discovery input. A source URL/category/enabled
change, pending/history change, relevant expiry or exploration-policy change does.
The timestamp synthesized while previewing an already-expired proposal is not a
semantic binding: actual prepare records its own observed expiry time. Real changes
in pending/history membership still invalidate the snapshot. No merge of a native
snapshot over current operational state is permitted.

The request is valid from its scheduled UTC start until that date's **UTC midnight**.
It can be created within the preceding 24 hours or later on the same slot day, before
the job consumes it. A result can be bound only before expiry. A delayed cron arrival
within that day is allowed if the exact semantic context still matches. The ten-minute
runner limit is not a ten-minute cron-arrival SLA. A run arriving after UTC midnight
holds new generation rather than moving the prefill to another slot/day.

## Native response contract

Give the worker the exact request bytes and this fixed task: suggest up to three
substantive RSS/Atom sources for `context.proposal_context.requested_exploration_area`;
do not repeat configured/pending or recently rejected/expired URLs. Match the requested
area: it may refresh the professional radar or broaden reading across other disciplines.
When exploring another discipline, **no technology, finance or banking connection is
required**. Prefer substantive reporting, research or thoughtful specialist publications.
The requested area is a search target, not a claim of actual disciplinary novelty.
Return an ordered list or an empty list.
Use no tools or external requests. Output **only** this data schema:

```json
{
  "schema": "native-discovery-response/v1",
  "request_sha256": "<exact request SHA256>",
  "execution_ref": "<actual worker reference supplied by its controller>",
  "proposals": [
    {"url": "https://example.org/feed", "category": "Science", "name": "Example"}
  ]
}
```

The controller passes the same independently observed execution reference during
binding. Preserve the original raw response; do not repair, paraphrase, wrap, or silently
trim it. The response is at most 8,192 UTF-8 bytes. Unknown/duplicate JSON fields,
non-string fields, empty/oversized values and line/control/delimiter injection fail
closed. Maximum lengths are URL 2,048, category 80 and name 160 characters; maximum
three proposals. The full prefill/request/record bound is 256,000 bytes.

```sh
python -m digest.discovery_native bind \
  --request "$REQUEST_PATH" --response "$RAW_RESPONSE_PATH" \
  --execution-ref "$OBSERVED_WORKER_REF" --controller "$CONTROLLER_REF"
```

Binding prints the prefill path and SHA256 and writes it once, exclusively. The prefill
retains exact request and response text, their hashes, actual-worker attestation and
binding time. It is a technical provenance/schema binding, **not an independent
semantic acceptance** and not fabricated provider/model metadata. Another worker or
changed result cannot replace an existing prefill for that slot.

## Existing weekly job: reserve, persist, prepare, persist, send

1. **Reserve** in the same uninterrupted managed run that will prepare and send:

   ```sh
   python -m digest.discovery_native reserve --config config.yaml \
     --prefill "$PREFILL_PATH" --prefill-sha "$PREFILL_SHA" \
     --controller "$CONTROLLER_REF"
   ```

   The real existing `GITHUB_RUN_ID:GITHUB_RUN_ATTEMPT` owns the slot reservation;
   `GITHUB_RUN_ID` remains the validation cycle. Do not forge either. Separate local
   CLI invocations intentionally receive different local owners and cannot impersonate
   one managed attempt; local Python orchestration must reuse its original
   `DiscoverySession`. Export and binding are independently usable data-only commands.
   The reservation
   binds the immutable prefill, controller, slot, owner and cycle and is exclusive.
   Existing reservation, stale/missing input or changed context cannot be replaced.
   For those expected input holds, the command exits successfully with JSON
   `status: "unavailable"` and an empty `reservation_sha256`; continue to native-only
   prepare so eligible old pending offers are not disabled. `status: "reserved"`
   supplies the new local hash for the external persistence/readback step. Unexpected
   filesystem failures or corrupt operational pending/delivery state raise and stop;
   do not treat them as an ordinary missing prefill.

2. **Persist and read back** all new `.cache/native_discovery/` and referenced
   `.cache/native/` bytes through the existing reviewed private-runtime state barrier.
   For a newly reserved slot, verify the exact reservation SHA from the successful remote revision and retain that
   value as `NATIVE_RESERVATION_SHA`. For an unavailable-input outcome, leave that
   value empty and proceed to step 3 in native-only mode. Local fsync, a printed hash, or a successful Git
   command without readback is not remote durability. Never supply a local-only hash
   as proof. A failed/uncertain persistence stage does not authorize consumption.

3. **Prepare** through the existing entrypoint, explicitly selecting native mode:

   ```sh
   python -m digest --config config.yaml --discover --discovery-phase prepare \
     --discovery-native-prefill "$PREFILL_PATH" \
     --discovery-native-sha "$PREFILL_SHA" \
     --discovery-native-owner "$CONTROLLER_REF" \
     --discovery-native-reservation-sha "$NATIVE_RESERVATION_SHA"
   ```

   Missing/unavailable native input remains an explicit `generation_failed` outcome,
   not a successful empty result or an API fallback. Eligible old pending proposals
   can still be validated, reserved and delivered. Where reservation is unavailable,
   preserve the evidence and keep the native mode selected; do not supply a fabricated
   reservation hash or remove the flag to make the job green. Unexpected storage or
   persistence failures require diagnosis, not blanket shell `|| true` recovery.

4. **Persist/read back the original pending + delivery pair**, including new native
   sidecars, using the existing runtime failure-preservation rules. Then invoke the
   unchanged `--discovery-phase send` with those two exact hashes. Native arguments
   are not sender arguments. The original target, same-run owner, reservation and
   unknown-before-POST checks still apply.

Preparation validates old eligible pending proposals first. Each attempted old URL,
including failures, consumes one of the **three total feed checks**. Only the first
`3 - validations` native candidates enter the original parser/deduplication/feed
validator. The complete raw list is retained, and `started.json` records its admitted
count and deferred suffix. No fourth candidate, retry or paid fallback is introduced.
This ranked-prefix allocation is native-only; the provider path's overlong-response
rejection is unchanged. Deferred candidates are not pending offers or automatically
carried into another slot.

The existing safe validator retains public-address/redirect checks, 2 MiB body bounds,
three redirects, a 15-second aggregate feed-validation timeout and no retries. A
successful technical check remains no claim of editorial usefulness or diversity.
The runner alone creates `PendingSource` identities/timestamps and persists each
validated prefix; the native worker never does so.

## Recovery and rollback

| Observation | Meaning and action |
| --- | --- |
| Missing/stale/invalid prefill or no verified reservation SHA | New generation unavailable; retain exact old pending identities and normal eligibility/receipt holds. No API fallback. |
| Reservation persisted but runner interrupted before consumption | The later run/attempt cannot reuse it. Preserve it; do not reset/rename the reservation or reconstruct the old run identity. |
| `started.json` exists, even without a new pending source | Consumption may have begun. Do not regenerate/reconsume; inspect preserved output and validated prefix. |
| Pending prefix persisted, later validation/write cancelled | Keep the exact prefix. Later eligible old-pending validation uses existing cooldown/budget rules; it is not native regeneration. Missing attribution stays missing. |
| Confirmed/rejected/unknown delivery receipt | Original delivery policies still apply. Unknown/reserved attempts are not safe to resend; a hash match alone is not permission. |
| Slot expired or semantic context changed | Hold this prefill; do not relabel it to another date/context. A later scheduled slot needs its own legitimate intent, not replay. |

Existing reserved/unknown receipts remain held under legacy preparation policy;
ordinary prepare can replace the current batch metadata. Native mode adds no new
global recovery gate and does not reclaim another owner's reserved batch. The existing
send-only path requires no prefill and remains available for an exact current persisted
pair owned by the same uninterrupted attempt; run it before starting a new prepare.

Retain `request.json`, `prefill.json`, `reserved.json`, `started.json` and referenced
content-addressed bytes as private evidence. Do not publish real source registry,
history, destinations or model output in public patches. Preserve these files on
failure where the runtime can still do so; engine writes alone cannot survive runner
loss. Shared-runtime writers must remain serialized.

Rollback means stop selecting new native work while preserving pending proposals,
reservations, receipts and evidence. Under an **ONLYFREE** policy, do not simply remove
native flags and run the default provider route: that would restore its existing API
cost dependency. Keep discovery generation held until a separately reviewed eligible
route is available. Existing source approvals and already prepared safe delivery are
not revoked by rolling back this adapter.

## What verification establishes

Offline tests establish schema/hash/context/date/ownership/replay boundaries, real
prepare and retained-pair behavior, feed parsing with stubbed acquisition, private-URL
rejection and mocked send/unknown holds. They do **not** establish native semantic
usefulness, actual independent worker execution, unattended integration, live feeds,
confirmed real delivery, GitHub billing/capacity, or universal free operation. This
adapter removes the discovery generation path's model-API dependency when explicitly
selected; other enabled product routes and facilities retain their own acceptance
and cost evidence requirements.

See [source discovery operations](OPERATIONS.md#source-discovery),
[state/effect ordering](STATE_AND_EFFECTS.md#other-retained-state), and the
[ADR amendment](decisions/0013-discovery-exploration-state.md#native-prefill-generation-adapter).
