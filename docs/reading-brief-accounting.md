# Reading brief admission accounting

Gemini 3.8 Flash retains its exact `countTokens` preflight, including the complete
system prompt and source messages. Counting consumes the shared request budget.
Groq `openai/gpt-oss-120b` uses the optional offline estimate below. Other profiles,
including Qwen, remain pending until their current tokenizer/framing is verified.
The factual `source-passages-v4` prompt is unchanged.

## Optional offline tokenizer preparation

Install `digest[reading-brief]` to obtain the pinned `tiktoken==0.14.0` dependency.
The default install and Gemini route do not require it. Prepare the official rank
asset explicitly before running; production never downloads tokenizer files:

```sh
mkdir -p .cache/tokenizers/token-cache
curl --fail --location https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken \
  --output .cache/tokenizers/token-cache/fb374d419588a4632f3f557e76b4b70aebbca790
printf '%s  %s\n' \
  446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d \
  .cache/tokenizers/token-cache/fb374d419588a4632f3f557e76b4b70aebbca790 | sha256sum --check
```

For a different prepared directory, set `DIGEST_TOKENIZER_ASSETS` to that directory
(the parent of `token-cache`). Missing dependency, wrong dependency version, missing
asset, symlinked asset paths or hash mismatch leave work technical pending. The code
constructs the encoding from verified local bytes; it never calls tiktoken's
network-capable encoding registry. Assets are not bundled in the package.

## Estimate and bounds

[GPT-OSS's tokenizer](https://github.com/openai/gpt-oss/blob/main/gpt_oss/tokenizer.py)
uses o200k Harmony. The ordinary content ranks and regex come from
[OpenAI tiktoken](https://github.com/openai/tiktoken/blob/main/tiktoken_ext/openai_public.py).
Each actual system/user role and complete message content is encoded as ordinary
text. Literal strings resembling special tokens cannot become model framing.
Minimal Harmony start/message/end framing and assistant prefix are counted explicitly.

Admission is `ceil(local count × 1.20) + 256 + reserved output`, bounded by a local
8,000-token request allowance and the 131,072-token model context. The default
reserved output is 2,048 tokens. The allowance conservatively follows the published
[Groq free-tier 8,000 TPM baseline](https://console.groq.com/docs/rate-limits);
it is not a measurement of this account's entitlement or remaining quota. Groq's
hidden framing can differ, so this is a labelled estimate, never an exact count or
a guarantee of provider acceptance. Generation parameters are bound in the full
wire-request identity; only actual role/message text is input-tokenized.

One preserved real Groq request gave a local count of 2,758 versus reported input
usage of 2,821, a difference of 63. The preserved complete Citi source with the v4
prompt counted 2,245 locally: with margin and 2,048 output reserve, admission is
4,998, fitting one request. This is offline calibration on saved evidence, not a
live provider test or general factual-quality/throughput acceptance. When Groq
returns `prompt_tokens`, the runtime logs local count, admission estimate, actual
usage, signed actual-minus-local/estimate differences and output reserve. Original
provider usage is retained separately; negative differences are valid diagnostics.

Admission records are separate from legacy `exact_counts` and bind estimator
version and asset SHA to the provider/model/wire-request identity. Existing v4
Gemini caches remain readable. Each page retains its actual route, prompt,
response and admission binding, so changing the configured primary can resume the
same immutable source without discarding already completed pages.

Accounting overflow may split the complete contiguous source manifest. An estimate
split is conservative admission/budget splitting, not measured context overflow.
No source prefix is dropped; all pages must complete before a technical handoff,
and selected qualifications remain inspectable. This is not #55 semantic publication
acceptance. Known 429/503/unavailable route failures may
try one supported configured fallback; ambiguous timeout and invalid output stay
held across later invocations and route changes. An unresolved counting request is
not repeated, but does not imply that generation occurred. Fallback shares the deadline and request counter and disables stage retries. Each
actual route rebinds pacing before counting/generation: Groq uses the greater of the
configured interval and 65 seconds; Gemini retains the configured interval. The common
pacer preserves prior request timing across a switch. These are local policies, not
verified provider/account quotas. Already completed
checksum-bound evidence can be validated without the optional tokenizer assets. A provider rejection does not trigger truncation or claim success.

See [third-party notice](../THIRD_PARTY_NOTICES.md) for reused tokenizer code.

## Candidate and preparation boundary

The #122 integration admits exact saved candidate selections under current source,
occurrence, recency, blocklist and cache policy. It freezes the source/page/selection
binding into a technical handoff; it never rerenders or publishes inside sender-only
operation. Existing accepted preparation takes precedence before source work.

The in-process request reservation cap is at most ten, with no reset on fallback.
A per-GitHub-run stage claim/journal shares the same ten across separate model
processes. Lost usage holds the lease; it is not ten new requests per job. Atomic
compatible engine/workflow rollout and real capacity remain acceptance gates.
The prior twelve-request draft allowance is withdrawn.
See proposed [ADR0009](decisions/0009-selected-source-admission.md).
