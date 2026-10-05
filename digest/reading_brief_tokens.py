"""Offline GPT-OSS admission estimate; no registry lookup or automatic downloads.

Ordinary content uses the official o200k_base ranks/regex. Minimal Harmony
framing is explicit; provider-side framing remains an estimate, not exact usage.
https://github.com/openai/gpt-oss/blob/main/gpt_oss/tokenizer.py
https://github.com/openai/tiktoken/blob/main/tiktoken_ext/openai_public.py
"""
from __future__ import annotations

import base64
import hashlib
import os
from functools import lru_cache
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

GPT_HASH = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
MAX_ASSET_BYTES = 20_000_000
ESTIMATOR_VERSION = "gpt-oss-o200k-harmony-margin-v1"


class TokenProfileUnavailable(ValueError):
    """Keep the article pending when optional local accounting is unavailable."""


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


def count_gpt_input(messages: list[dict[str, str]]) -> int:
    """Count ordinary content plus minimal framing; caller must reserve headroom."""
    if (len(messages) != 2 or [message.get("role") for message in messages] != ["system", "user"]
            or any(set(message) != {"role", "content"} or not isinstance(message["content"], str)
                   for message in messages)):
        raise TokenProfileUnavailable("unsupported_tokenizer_messages")
    try:
        if version("tiktoken") != "0.14.0":
            raise TokenProfileUnavailable("tokenizer_version_mismatch: install digest[reading-brief]")
        encoding = gpt_encoding(Path(os.environ.get("DIGEST_TOKENIZER_ASSETS", ".cache/tokenizers")))
    except (ImportError, PackageNotFoundError) as exc:
        raise TokenProfileUnavailable("optional_dependencies_missing: install digest[reading-brief]") from exc
    # Inputs: <|start|>role<|message|>content<|end|>. Assistant prefix is incomplete.
    # Untrusted literal special-token strings are ordinary content, never framing.
    tokens = sum(3 + len(encoding.encode_ordinary(message["role"]))
                 + len(encoding.encode_ordinary(message["content"])) for message in messages)
    return tokens + 2 + len(encoding.encode_ordinary("assistant"))
