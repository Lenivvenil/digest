# 0026. Own public acquisition in one bounded HTTP operation

Status: implementation under review for [#218](https://github.com/Lenivvenil/digest/issues/218).
The concrete plan was independently approved and recorded before implementation.
Merge, exact-main CI and runtime rollout remain separate issue evidence.

## Context

Collector and configuration probes followed redirects after checking only the initial
URL. Their concurrent requests temporarily replaced process-global DNS resolution.
Discovery and optional article fetching checked each hop but shared that unsafe
primitive; an article-only lock could not protect unrelated users. Optional liveness
checked only basic URL syntax. Feed buffering and decompression did not uniformly
bound the acquisition cost. These are source-level risks, not observed exploitation.

## Decision

One concrete `adapters.http.public_fetch.fetch_public` operation owns public URL
acquisition, not parsing, editorial decisions or caller retry policy. It returns
closed metadata and one bounded decoded body. It supports GET and metadata-only HEAD,
with no generic transport framework, new dependency, persistent state or infrastructure.

Validate original URL spelling before normalization: reject userinfo (even empty),
whitespace/control/DEL characters, backslashes, non-HTTP schemes, invalid ports and
percent-bearing/scoped hostnames. Resolve canonical HTTPX ASCII/IDNA hostnames in a
worker, check every returned address before connecting, and reject empty/malformed or
mixed safe/unsafe answers. Require global, non-multicast, non-reserved addresses and
explicitly reject IPv6 site-local space: Python 3.12 reports `fec0::1` as global despite
it being site-local. Reserved translation space, including `64:ff9b::/96`, is excluded;
IPv4-mapped addresses remain subject to the same public-address classifications.

Connect to a validated IP URL, with the logical origin's Host header and `sni_hostname`
request extension. Default TLS verification remains enabled, using the original ASCII
hostname for certificate verification. HTTPX supplies bracketed IPv6 and nondefault-port
Host syntax. Fresh `trust_env=False`, `follow_redirects=False` clients per address/hop
prevent ambient proxies or cross-origin pooled connections from bypassing the operation.
No resolver is replaced and no article-wide lock remains. Fresh DNS/TLS setup has a
performance cost; correctness and bounded useful concurrency take precedence over pooling.

Only connection-establishment failures before headers can advance to another validated
address. HTTPX applies its three-second connect timeout separately to TCP and TLS
establishment, both bounded by the shared total budget. These phases can therefore use
roughly six seconds together. Fallback occurs only while time remains; slow DNS may
leave no opportunity for another address.
Read, protocol and HTTP-status errors do not trigger address retries. Every redirected
logical URL is checked again, including same-origin redirects. Relative Location values
are allowed, while spelling hazards are rejected before our URL join. HTTPX can reject
an invalid Location while constructing its unsent next-request metadata; it never sends
that request automatically. Article's narrower standard-port/2048-character policy runs
at every hop; fragment removal and logical final/source identity remain intact.

## Bodies, deadlines and cancellation

Requesting identity encoding is not proof of compliance. Read raw HTTPX chunks, capped
at 2 MiB before accumulation, then decode gzip/x-gzip or zlib/raw deflate using bounded
stdlib decompression (`max_length=limit+1`), without an unbounded flush. Raw and decoded
caps apply separately. Reject unsupported/chained encodings, truncated streams,
concatenated compressed members and trailing encoded bytes. Validate Content-Length
against the raw cap and actual raw length. A bounded raw chunk can be read ahead before
an overflowing accumulator is rejected; the guarantee is bounded buffering, not that
exactly the limit's bytes and no more reach the transport. No partial evidence is returned.
Response headers describe wire encoding; the returned body is already decoded and is
never reconstructed as an encoded HTTPX response.

One monotonic deadline bounds DNS awaits, all connection attempts, redirects and body
reads: 15 seconds for collector/probes/discovery, 20 seconds for article acquisition.
Discovery retains the existing outer 15-second fetch-plus-parser-thread budget, and
article retains the outer 20-second fetch-plus-extraction budget. Bounded synchronous
decoding/extraction is checked immediately on return; it cannot be preempted mid-call.
An expired await cannot stop an OS resolver or parser worker already running. Response
and client contexts close on every exit and cancellation propagates to the caller.

HEAD does not follow redirects, consume body bytes or validate Content-Length against
a nonexistent body. GET error statuses also return metadata without reading bodies,
so existing status/retry decisions remain available. Returned URLs and status-error
metadata identify the logical origin, not the selected transport IP.

## Compatibility and deliberate restrictions

- Collector retains twenty-way concurrency, its two physical attempts for configured
  transient statuses/timeouts and existing bounded Retry-After/backoff. Its whole-source
  time can therefore include two 15-second attempts plus backoff. Its parser, source
  identity, entry accounting and article order remain unchanged.
- CLI probes retain BLOCKED unsafe destinations, FAIL network/deadline outcomes and
  WARN malformed feeds. No argument, report contract or exit-status redesign.
- Discovery retains strict RSS/Atom parsing, no retries and existing technical error /
  timeout categories. Article retains content-type/partial-response/completeness checks.
- Liveness retains ten-way concurrency, no redirects and no body, dropping 404/410 or
  failed acquisition and keeping other statuses (including 401/403/405/429/5xx). The
  public validator's legacy client parameter remains compatible but cannot bypass the
  owned fetch policy.
- Collection/probes now deliberately reject more than three redirects, more than 2 MiB
  raw or decoded data, unsupported/chained/multi-member compression and URL credentials.
  These are technical acquisition failures, never editorial rejection or truncation.
  Cookies, ambient proxies and cross-origin pooled client state are not inherited.

Domain/persistence/CLI contracts, accepted/ready/receipt bytes and source/feedback
identity are unchanged. Search adapters with fixed configured API endpoints retain
separate HTTP policies; this operation is not a universal outbound networking policy.

## Verification and rollback

Offline tests exercise the actual boundary and substitute only DNS/transport I/O for
safety proof. Cases cover mixed/private/site-local/reserved destinations, every redirect,
rebinding, overlap/cancellation, connection-only fallback, compressed/raw overflow,
clipped/truncated data, total deadlines, HEAD semantics and logical provenance. The
real HTTPX/httpcore connection path is tested with a synthetic network backend: selected
IP, original ASCII SNI, Host/path and default `SSLContext.check_hostname=True` /
`CERT_REQUIRED`. This verifies our integration, not TLS-library implementation or live
publisher availability. Existing parser/retry tests may mock this operation explicitly.

The reviewed extension is documented by [HTTPX](https://www.python-httpx.org/advanced/extensions/#sni_hostname)
and [HTTPCore](https://www.encode.io/httpcore/extensions/#sni_hostname). Tests and source
inspection use HTTPX 0.28.1 / httpcore 1.0.9; future dependency changes require rechecking
that IP connection targets and original-host verification remain separate. Release
proof records the complete tested graph, lint/type checks, aggregate regression results
and at least 92.93% statement coverage, with no new exclusions or test-count packing.

Rollback is a reviewed compatible code revert/engine pin. No runtime data reset,
source-state rewrite, resend or model call is needed. Restoring the old implementation
would restore its documented security/resource gaps and must be an explicit decision.
