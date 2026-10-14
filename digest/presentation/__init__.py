"""Publication-only rendering policies, separate from canonical editorial evidence."""

from typing import Any


def publication_language(config: Any) -> str:
    """Static reader copy follows the target even when generated prose falls back."""
    translation = getattr(config, "translation", None)
    if getattr(translation, "enabled", False):
        return str(getattr(translation, "target_language", "en"))
    return str(getattr(getattr(config, "radar", None), "language", "en"))
