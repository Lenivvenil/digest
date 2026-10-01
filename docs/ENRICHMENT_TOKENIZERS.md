# Offline tokenizer profiles for selected-source enrichment

This optional dependency set is used only by the isolated enrichment draft. Ordinary
`pip install .` retains the existing dependencies and does not load tokenizer assets.
No model weights are needed. Analysis never downloads assets or uses a network-capable
tokenizer registry fallback.

```sh
pip install '.[enrichment]'
mkdir -p .cache/enrichment-tokenizers/token-cache
```

Prepare the following public files explicitly, outside analysis, then keep the directory
between runs. In CI, cache the directory under the profile key
`enrichment-tokenizers-v1-0997f410-ec0d3f70-446a9538`; restore it before analysis. This
documentation does not change any production workflow or enable inference.

| Local file | Official source / pinned revision | SHA-256 |
|---|---|---|
| `qwen-tokenizer.json` | [Qwen tokenizer](https://huggingface.co/Qwen/Qwen3.8-27B/resolve/72a217afab8029b39e4af1c7273a829995a3dbaf/tokenizer.json) | `0997f410c57a1f4e53b09e4be8f4a172d90edd9564368fb0847030937229b9f3` |
| `qwen-tokenizer-config.json` | [Qwen template/config](https://huggingface.co/Qwen/Qwen3.8-27B/resolve/72a217afab8029b39e4af1c7273a829995a3dbaf/tokenizer_config.json) | `ec0d3f708a4c29dac13d94883213b5abad004378bbc2dbae5c2715b6166a6f0a` |
| `token-cache/fb374d419588a4632f3f557e76b4b70aebbca790` | [OpenAI o200k_base ranks](https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken) | `446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d` |

Save each linked file under its listed name. The verifier refuses missing, oversized,
symlinked or mismatched assets. Verify the prepared cache without contacting a provider:

```sh
python -m digest.enrichment_tokens --cache .cache/enrichment-tokenizers
```

Missing dependencies or assets, an unknown route, or an integrity mismatch is an
explicit technical-pending condition. It never means an article is irrelevant.
Do not commit the asset directory into the engine or runtime repository.

## Scope and accuracy

The Qwen profile is specific to `groq/qwen/qwen3.8-27b` and the listed repository
revision, with its non-thinking chat template. The GPT profile is specific to
`groq/openai/gpt-oss-120b`, using verified local o200k ranks plus explicit minimal
Harmony framing. Neither profile is a universal counter for other providers, models
or server-side templates. A request still reserves completion tokens and safety
overhead. Record actual provider-reported input against the local estimate; a template
or accounting mismatch must remain visible rather than increasing the budget silently.

Context size, TPM/RPM and daily allowance are different constraints. Profiles do not
prove an account's pricing entitlement, remaining quota or source fidelity.

## Provenance and licenses

Checked 2026-10-01. The pinned [Qwen repository metadata](https://huggingface.co/Qwen/Qwen3.8-27B/raw/72a217afab8029b39e4af1c7273a829995a3dbaf/README.md)
declares Apache-2.0. Preserve the upstream license/notice when redistributing its assets.
The OpenAI ranks are used by the official [tiktoken encoding implementation](https://github.com/openai/tiktoken/blob/main/tiktoken_ext/openai_public.py);
[tiktoken is MIT-licensed](https://github.com/openai/tiktoken/blob/main/LICENSE).
The optional packages are [tokenizers (Apache-2.0)](https://github.com/huggingface/tokenizers/blob/main/LICENSE),
[tiktoken (MIT)](https://github.com/openai/tiktoken/blob/main/LICENSE) and
[Jinja2 (BSD-3-Clause)](https://github.com/pallets/jinja/blob/main/LICENSE.txt).
Asset origin and these third-party notices do not assign a license to Digest itself.
The files are downloaded from their publishers during explicit preparation, not
redistributed as model weights or embedded in this repository.
