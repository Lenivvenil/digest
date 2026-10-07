"""Explicit, lazy request ownership independent of model configuration.

An execution holds one event-loop slot. Derived executions may share its initialized
state, while retaining independent slots when either later enters a different loop.
Persistent cycle reservations remain owned by ``digest.model_budget``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from digest.config import LLMConfig


@dataclass
class RequestState:
    """Per-loop concurrency, pacing, cooldowns and local request accounting."""

    loop: asyncio.AbstractEventLoop
    semaphore: asyncio.Semaphore
    spacing_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    next_request_at: float = 0.0
    last_request_at: float | None = None
    unavailable_until: dict[tuple[str, str], float] = field(default_factory=dict)
    request_limit: int | None = None
    requests_attempted: int = 0


@dataclass(eq=False)
class ModelExecution:
    """One explicit request-state owner, created without loop or budget effects."""

    _state: RequestState | None = field(default=None, init=False, repr=False)

    def request_state(self, settings: LLMConfig) -> RequestState:
        """Initialize this loop's state, sampling semaphore capacity only once."""
        loop = asyncio.get_running_loop()
        if self._state is None or self._state.loop is not loop:
            self._state = RequestState(loop, asyncio.Semaphore(getattr(settings, "max_concurrent_requests", 4)))
        return self._state

    def share_initialized(self, settings: LLMConfig) -> ModelExecution:
        """Give a derived operation its own slot sharing the caller's current state."""
        derived = ModelExecution()
        derived._state = self.request_state(settings)
        return derived
