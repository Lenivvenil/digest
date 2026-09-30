# Blind evidence review (opt-in experiment)

`review.enabled` adds provider-neutral independent selection to the existing RSS,
Markdown and Telegram pipeline. It defaults to false for existing configurations.

## Contract

1. Freeze a version-1 `EvidenceBundle` before any review call. It contains stable
   article IDs, sanitized RSS title/excerpt, canonical URL, source, category and
   available publication time. The SHA-256 bundle ID changes with the evidence.
2. Apply the same deterministic round-robin category sampling once, with bounded
   article count, excerpt length and a 16,000-character evidence-item JSON budget.
   The omission count is recorded. This is excerpt evidence, not full articles.
3. Give primary and secondary slots exactly the same messages, language,
   temperature, selection limit and maximum output tokens. No category summary,
   prior opinion, prior selection or other model identity enters the prompt.
4. Pin each slot to its specified provider/model. There is no role/provider
   fallback for review slots. A failed model remains unavailable rather than
   silently being replaced by the other reviewer. Provider-reported model
   versions are retained when available; otherwise the report says unknown.
5. Validate every selection: known unique evidence ID, bounded reason text,
   controlled confidence label, and a nonempty exact quote from supplied title
   or excerpt. Invalid entries invalidate the entire response. An empty selection
   needs an explicit limitation, distinguishing abstention from malformed output.
6. Compare selected IDs using Jaccard overlap. If both results are valid and
   overlap is below the configured threshold, optionally call a third model on
   the same complete evidence bundle and same prompt, still without prior answers.
   At most one third-model task is requested. HTTP retries remain independently
   bounded by the shared LLM retry policy.

## Output and integration

Primary selection becomes canonical Telegram cards, labeled as model opinion.
Secondary/third opinions do not generate extra Telegram card floods. All reviews,
quotes, confidence, provider/model identities, token usage, prompt hashes,
completeness and escalation decisions appear in Markdown and a sibling
`YYYY-MM-DD.review.json` (or numbered retry filename).

Blind selection runs before category prose to protect its quota budget. If prose
fails but primary cards succeed, those cards can still be delivered. Even if all
slots abstain or fail, the diagnostic report is preserved when Markdown is enabled.
No category summary or prior output is recycled into another model's evidence.

## Example configuration

```yaml
review:
  enabled: true
  primary: {provider: gemini, model: gemini-3.8-flash}
  secondary: {provider: groq, model: openai/gpt-oss-120b}
  tie_breaker: {provider: groq, model: qwen/qwen3.8-27b}
  max_evidence_articles: 20
  max_excerpt_chars: 500
  max_selections: 5
  max_output_tokens: 4096
  disagreement_threshold: 0.5
```

Qwen is a preview-model option, not an automatic replacement for a failed peer.
These names express the reviewed experiment configuration, not a guarantee of
account access or a free quota. Deployment must confirm free-tier account status;
this implementation does not enable billing or add paid-provider fallback.

## Offline verification

```sh
python -m scripts.review_fixture --output /tmp/digest-review-fixture
```

This command uses synthetic fixture responses and never calls providers. It emits
Markdown/JSON demonstrating different selections and bounded third-model
escalation. It proves the contract wiring only, not the quality of those models.

## Explicit limitations

Selection overlap is not factual agreement or a majority-vote truth detector.
Different opinions about the same selected item do not automatically trigger the
third model. Exact excerpt quotes validate provenance, not claim truth. There is
no live fact-checking or full-article retrieval in this module.

Identical maximum output tokens do not equal identical reasoning effort across
model families. Character caps are not exact tokenizer/RPM/TPM accounting. Quotas
can still make a review incomplete; that state is surfaced, not counted as an
opinion. Existing crash/retry delivery limitations still apply.

## Rejected-response diagnostics

Invalid successful model completions retain a specific validator reason, response
SHA-256 and at most 32,000 characters of untrusted rejected text in the JSON
sidecar, not rendered as Markdown. Control characters and common credential-like
patterns are redacted without inspecting environment secrets. Raw HTTP error
bodies and headers are never retained. This diagnoses future contract failures
without silently retrying a model or guessing what its first response contained.
