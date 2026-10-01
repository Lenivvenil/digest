"""Optional offline profile boundaries, without downloaded assets or inference."""
from pathlib import Path

import pytest

from digest.enrichment_tokens import TokenProfileUnavailable, checked_bytes, count_input


def test_unknown_route_never_uses_a_known_model_profile(tmp_path: Path) -> None:
    with pytest.raises(TokenProfileUnavailable, match="unsupported_tokenizer_route"):
        count_input([], "another-provider", "openai/gpt-oss-120b", tmp_path)


@pytest.mark.parametrize("kind", ["missing", "mismatch", "symlink"])
def test_asset_failures_are_explicit_technical_conditions(tmp_path: Path, kind: str) -> None:
    asset = tmp_path / "asset"
    if kind == "mismatch":
        asset.write_text("wrong bytes")
    elif kind == "symlink":
        target = tmp_path / "other"
        target.write_text("outside trusted asset path")
        asset.symlink_to(target)
    with pytest.raises(TokenProfileUnavailable):
        checked_bytes(asset, "0" * 64)


def test_counter_rejects_unsupported_message_shape_before_loading_assets(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exactly two"):
        count_input([{"role": "user", "content": "not the declared template"}],
                    "groq", "openai/gpt-oss-120b", tmp_path)
