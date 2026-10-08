"""Compatible exports for signal validation and optional HTTP liveness checks."""

from digest.adapters.http.signal_liveness import check_signal_liveness as _head_check  # noqa: F401
from digest.application.signal_validation import validate_signals_async as validate_signals_async
from digest.domain.investigation.validation import validate_signals as validate_signals

__all__ = ["validate_signals", "validate_signals_async"]
