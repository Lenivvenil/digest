# 0022. Bind bounded investigation to an explicit search policy

Status: accepted for the bounded repair tracked in
[#179](https://github.com/Lenivvenil/digest/issues/179), under
[#77](https://github.com/Lenivvenil/digest/issues/77). Deployment and useful
counter-evidence remain separate acceptance facts.

## Context

The bounded stage could issue real queries to Hacker News and arXiv, while its
Lobsters slot always reported unavailable without making an HTTP request. Removing
that diagnostic alone would not recover any evidence. The existing DEV adapter
also abstained because its earlier API contract did not establish query search.

The current official Forem V1 contract documents `GET /api/articles/search` with
operation-level `security: []`. A generic public `q=python`, `page=1`, `per_page=1`
probe on 2026-10-08 returned HTTP 200 and the documented JSON-array field types
with the V1 Accept header and redirects disabled. This is one transport/shape
observation, not proof of ten-result availability, topic coverage or relevance.

## Retrieval decision

Repair the existing DEV adapter using `https://dev.to/api/articles/search` and
`Accept: application/vnd.forem.api-v1+json`. Send the unchanged validated nonblank
query, first page and at most ten results. Do not add pagination, a second endpoint,
article-body fetches, an API key, a date/top filter or automatic retries.

Retain the returned article URL, title, description and publication timestamp.
An empty description is valid missing evidence; do not manufacture a snippet from
the title or optional body. The canonical URL may identify another publisher and
is not the retrieved evidence URL. Engagement counts are not relevance; the local
signal score is zero. Validate the envelope, used field types, timestamp and safe
absolute HTTP(S) URL. Treat valid `[]` as empty, and malformed/HTML/error/rate-limit
responses as failed attempts. Disable redirects and retain the bounded response
stream and HTTP timeout.

### Response ownership — 2026-10-08

[#190](https://github.com/Lenivvenil/digest/issues/190) makes each search adapter
own its response status, complete bounded buffering, closure and envelope validation.
The bounded stage no longer attaches a hostname-wide hook to the caller's HTTP
client. Unrelated requests and existing caller hooks remain outside search policy.

Hacker News now uses the same 512,000-decoded-byte ceiling and explicit no-redirect
rule already applied to it by the bounded stage. This deliberately tightens direct
and legacy HN calls: oversized bodies are rejected, redirects are refused even on a
redirect-enabled client, and error statuses are rejected before reading their bodies.
Successful extraction, valid-empty results, endpoint, query parameters and timeout
stay unchanged. arXiv and DEV retain their existing adapter guards and pacing.

The bounded retrieval behavior and request content are unchanged, so this ownership
change does not alter the policy identity below or reinterpret saved reservations.
It introduces no new source, retry, pagination, model call or result-quality claim.

### Configured sources and limits

The reviewed source policy is Hacker News, arXiv and DEV, in that fixed order,
filtered by the configured source names. It permits at most three queries and
three sources, with ten records per attempt. Repeated source names do not create
extra attempts. The legacy Lobsters registry entry remains available with its
honest unavailable outcome. An old bounded configuration naming only Hacker News,
arXiv and Lobsters therefore selects only the first two under the new policy;
operators must explicitly replace the configured slot to enable DEV.

Actual search HTTP traffic can rise from six to nine within the existing ceiling.
Nonempty retrieval can activate ranking that an empty run skipped, but does not
create a new model allowance. Source pacing and HTTP work consume the same
180-second cumulative stage deadline. No outer workflow deadline, translation
allowance, ranking packet, score threshold or query instruction changes here.
The optional persistent model-budget ledger is not enabled by this decision.

Upstream source currently shows per-IP limits of three GETs per second and thirty
per minute. The adapter permits one active request and spaces starts by at least
two seconds within its event loop. This is conservative pacing, not a promise about deployed quotas
or other traffic sharing an IP. A 429 is a recorded failure, not evidence or an
invitation to exceed the current attempt/deadline bounds.

## Reservation identity and historical compatibility

New exclusive post-attempt markers use schema 2 and carry `search_policy` with
`id: bounded-hn-arxiv-devto-v1`, `sources` as the ordered effective JSON list and
`max_queries` as the effective integer ceiling. One pure
builder supplies preparation, verification and actual bounded dispatch. The policy
ID identifies these reviewed retrieval contracts; unrelated engine releases do not
change it, while changes to these contracts require a new identity.

Before execution is marked started or any source/model work begins, verify the
policy against the execution configuration as well as the existing checkpoint
path, complete-file hash and evidence bundle. Unknown, malformed or changed policy
bindings hold the attempt without modifying its marker. Existing result/marker
blockers remain in place; preparation never overwrites or upgrades old work.

Schema-1 prepared-but-unstarted attempts cannot execute under the new policy.
Retain them for explicit inspection with their original engine/configuration;
do not delete, rename or regenerate a marker to obtain another automatic attempt.
Started/completed markers, old archives and primary receipts remain untouched.
Before rollout, inspect any in-flight reservation and avoid changing its policy.

Keep the result, Signal, SourceAttempt and ranking-audit schemas unchanged. The
same-stem companion marker supplies execution-policy identity; historical results
retain their recorded coverage, source names, queries and hashes. A copied archive
without its marker has unknown policy identity, not the current default. There is
no new archive loader or replay mechanism.

Updated coverage text changes new extraction request hashes through the existing
exact request binding. Old hashes and results are never recomputed. Hypotheses
remain query-planning inputs rather than ranking facts; source exclusion, quote
identity, ranking admission and primary-delivery isolation remain unchanged.

## Evidence and limits

Offline transport fixtures and existing stage/marker safeguards verify bounded
requests, validation, pacing, cancellation, empty versus failed retrieval, exact
source lineage and no-repeat execution. Real relevant counter-evidence still
requires independent inspection. Neither more hits nor a working endpoint closes
#77's product acceptance.

Primary references: [Forem V1 OpenAPI](https://github.com/forem/forem-docs/blob/main/api_v1.json#L622),
[ArticleIndex](https://github.com/forem/forem-docs/blob/main/api_v1.json#L5424),
[version-header guidance](https://developers.forem.com/api),
[query implementation](https://github.com/forem/forem/blob/main/app/queries/articles/api_search_query.rb),
and [upstream rate controls](https://github.com/forem/forem/blob/main/config/initializers/rack_attack.rb).
