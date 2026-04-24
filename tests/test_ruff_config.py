"""Test that ruff check passes on digest/ and tests/ with the project ruff.toml config."""
import subprocess


def test_ruff_check_passes() -> None:
    result = subprocess.run(
        ["ruff", "check", "digest/", "tests/"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"ruff check failed with exit code {result.returncode}.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
