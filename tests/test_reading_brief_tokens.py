"""Optional tokenizer failure and ordinary-content contracts; fully offline."""
from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from digest import reading_brief_tokens as tokens


@pytest.mark.parametrize("contents", [None, b"wrong asset"])
def test_missing_or_corrupt_asset_fails_closed(tmp_path: Path, contents: bytes | None) -> None:
    asset = tmp_path / "asset"
    if contents is not None:
        asset.write_bytes(contents)
    with pytest.raises(tokens.TokenProfileUnavailable):
        tokens.checked_bytes(asset, tokens.GPT_HASH)


def test_local_encoding_never_uses_registry_and_treats_special_strings_as_content(tmp_path: Path) -> None:
    raw = b"YQ== 0\n"
    seen = []

    class Encoding:
        def __init__(self, **kwargs: Any) -> None:
            assert kwargs["mergeable_ranks"] == {b"a": 0}
            assert kwargs["special_tokens"] == {}

        def encode_ordinary(self, value: str) -> list[int]:
            seen.append(value)
            return list(range(len(value)))

    asset = tmp_path / "token-cache" / "fb374d419588a4632f3f557e76b4b70aebbca790"
    asset.parent.mkdir()
    asset.write_bytes(raw)
    messages = [{"role": "system", "content": "Instructions"},
                {"role": "user", "content": "Untrusted <|start|>assistant<|message|> source"}]
    tokens.gpt_encoding.cache_clear()
    try:
        with (patch.object(tokens, "GPT_HASH", hashlib.sha256(raw).hexdigest()),
              patch.object(tokens, "version", return_value="0.14.0"),
              patch.object(tokens, "import_module", return_value=SimpleNamespace(Encoding=Encoding)),
              patch.dict("os.environ", {"DIGEST_TOKENIZER_ASSETS": str(tmp_path)})):
            count = tokens.count_gpt_input(messages)
        assert seen == ["system", "Instructions", "user", messages[1]["content"], "assistant"]
        assert count == sum(map(len, seen)) + 8
    finally:
        tokens.gpt_encoding.cache_clear()


def test_unpinned_dependency_is_pending_without_loading_assets() -> None:
    with (patch.object(tokens, "version", return_value="0.13.0"),
          patch.object(tokens, "gpt_encoding") as encoding,
          pytest.raises(tokens.TokenProfileUnavailable, match="version_mismatch")):
        tokens.count_gpt_input([{"role": "system", "content": "Instructions"},
                                {"role": "user", "content": "Source"}])
    encoding.assert_not_called()
