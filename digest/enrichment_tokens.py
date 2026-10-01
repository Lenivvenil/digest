"""Optional route-specific local token counts; never download data or call providers."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from importlib import import_module
from pathlib import Path
from typing import Any

PROFILE_VERSION = "enrichment-tokenizers-v1"
MAX_ASSET_BYTES = 20_000_000
QWEN_MODEL = "qwen/qwen3.8-27b"
GPT_MODEL = "openai/gpt-oss-120b"
QWEN_REVISION = "72a217afab8029b39e4af1c7273a829995a3dbaf"
QWEN_HASH = "0997f410c57a1f4e53b09e4be8f4a172d90edd9564368fb0847030937229b9f3"
TEMPLATE_HASH = "ec0d3f708a4c29dac13d94883213b5abad004378bbc2dbae5c2715b6166a6f0a"
GPT_HASH = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"


@dataclass(frozen=True)
class InputCount:
    tokens: int
    method: str
    tokenizer_sha256: str
    template_sha256: str | None = None
    revision: str | None = None


class TokenProfileUnavailable(ValueError):
    """Technical pending reason, never an editorial rejection."""


def checked_bytes(path: Path, expected_hash: str) -> bytes:
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise TokenProfileUnavailable("tokenizer_asset_symlink")
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_ASSET_BYTES + 1)
    except OSError as exc:
        raise TokenProfileUnavailable("tokenizer_assets_missing: prepare the documented local cache") from exc
    if len(raw) > MAX_ASSET_BYTES or hashlib.sha256(raw).hexdigest() != expected_hash:
        raise TokenProfileUnavailable("tokenizer_asset_integrity_mismatch")
    return raw


@lru_cache(maxsize=1)
def qwen_assets(cache_dir: Path) -> tuple[Any, Any]:
    ImmutableSandboxedEnvironment = import_module("jinja2.sandbox").ImmutableSandboxedEnvironment
    Tokenizer = import_module("tokenizers").Tokenizer

    tokenizer = Tokenizer.from_str(checked_bytes(cache_dir / "qwen-tokenizer.json", QWEN_HASH).decode())
    config = json.loads(checked_bytes(cache_dir / "qwen-tokenizer-config.json", TEMPLATE_HASH))
    template = ImmutableSandboxedEnvironment(trim_blocks=True, lstrip_blocks=True).from_string(config["chat_template"])
    return tokenizer, template


@lru_cache(maxsize=1)
def gpt_encoding(cache_dir: Path) -> Any:
    tiktoken = import_module("tiktoken")

    # Official o200k_base ranks and regex, with explicit Harmony framing counted below.
    # Reading and constructing directly avoids tiktoken's network-capable registry/cache fallback.
    raw = checked_bytes(cache_dir / "token-cache" / "fb374d419588a4632f3f557e76b4b70aebbca790", GPT_HASH)
    ranks = {base64.b64decode(token): int(rank) for token, rank in (line.split() for line in raw.splitlines())}
    pattern = "|".join([
        r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
        r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
        r"\p{N}{1,3}", r" ?[^\s\p{L}\p{N}]+[\r\n/]*", r"\s*[\r\n]+", r"\s+(?!\S)", r"\s+",
    ])
    return tiktoken.Encoding(name="local-o200k-harmony-content", pat_str=pattern,
                             mergeable_ranks=ranks, special_tokens={})


def count_input(messages: list[dict[str, str]], provider: str, model: str, cache_dir: Path) -> InputCount:
    if provider != "groq" or model not in {QWEN_MODEL, GPT_MODEL}:
        raise TokenProfileUnavailable("unsupported_tokenizer_route")
    try:
        return _count_input(messages, model, cache_dir)
    except ImportError as exc:
        raise TokenProfileUnavailable("optional_dependencies_missing: install digest[enrichment]") from exc


def _count_input(messages: list[dict[str, str]], model: str, cache_dir: Path) -> InputCount:
    if (len(messages) != 2 or [message.get("role") for message in messages] != ["system", "user"]
            or any(set(message) != {"role", "content"} or not isinstance(message["content"], str)
                   for message in messages)):
        raise ValueError("Local counting supports exactly two plain system/user messages.")
    if model == QWEN_MODEL:
        tokenizer, template = qwen_assets(cache_dir)
        rendered = template.render(messages=messages, add_generation_prompt=True, tools=None, enable_thinking=False)
        return InputCount(len(tokenizer.encode(rendered, add_special_tokens=False).ids),
                          "qwen_pinned_template_non_thinking_local_count", QWEN_HASH, TEMPLATE_HASH, QWEN_REVISION)
    if model == GPT_MODEL:
        encoding = gpt_encoding(cache_dir)
        # Each input: <|start|>role<|message|>content<|end|>; prefix: <|start|>assistant<|message|>.
        # Content is ordinary text, so literal special-token strings cannot masquerade as framing.
        tokens = sum(3 + len(encoding.encode_ordinary(message["role"]))
                     + len(encoding.encode_ordinary(message["content"])) for message in messages)
        tokens += 2 + len(encoding.encode_ordinary("assistant"))
        return InputCount(tokens, "gpt_o200k_content_plus_minimal_harmony_estimate", GPT_HASH)
    raise ValueError("No approved local counter for this model.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True, help="Prepared tokenizer asset directory; no downloads")
    args = parser.parse_args()
    probe = [{"role": "system", "content": "Count local text."}, {"role": "user", "content": "Offline probe."}]
    try:
        for model in (QWEN_MODEL, GPT_MODEL):
            count = count_input(probe, "groq", model, args.cache)
            print(f"Verified {PROFILE_VERSION}: groq/{model}; {count.method}")
    except TokenProfileUnavailable as exc:
        parser.exit(1, f"Tokenizer preparation incomplete: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
