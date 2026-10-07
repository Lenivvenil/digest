"""Execution ownership preserves intentional aliases without storing state in settings."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, fields, replace
from unittest.mock import patch

from digest.adapters.models.execution import ModelExecution, RequestState
from digest.config import LLMConfig, ProviderConfig
from scripts.review_fixture import fixture_config


def test_model_execution_construction_is_lazy_and_settings_are_serializable() -> None:
    with (patch("asyncio.get_running_loop", side_effect=AssertionError("No loop lookup")),
          patch("digest.model_budget.execution_from_env", side_effect=AssertionError("No budget lookup"))):
        execution = ModelExecution()
    assert execution._state is None
    settings = LLMConfig([ProviderConfig("groq", "fixture")])
    assert "_runtime" not in {item.name for item in fields(settings)}
    assert "_runtime" not in asdict(settings)
    assert not hasattr(settings, "_runtime")


async def test_config_alias_fresh_holder_and_initialized_sharing_preserve_capacity() -> None:
    config = fixture_config()
    config.llm.max_concurrent_requests = 4
    execution = ModelExecution()
    config_only = replace(config, sources=[])
    state = execution.request_state(config_only.llm)
    state.request_limit, state.requests_attempted = 7, 2
    state.next_request_at, state.last_request_at = 80.0, 15.0
    state.unavailable_until[("groq", "fixture")] = float("inf")
    assert config_only.llm is config.llm
    assert execution.request_state(config.llm) is state

    bounded = replace(config, llm=replace(config.llm, max_concurrent_requests=1, max_retries=0))
    fresh = ModelExecution()
    fresh_state = fresh.request_state(bounded.llm)
    assert fresh_state is not state and fresh_state.semaphore._value == 1
    assert fresh_state.request_limit is None and fresh_state.requests_attempted == 0
    assert fresh_state.next_request_at == 0 and not fresh_state.unavailable_until

    shared = execution.share_initialized(config.llm)
    assert shared is not execution and shared.request_state(bounded.llm) is state
    assert state.semaphore._value == 4
    assert state.request_limit == 7 and state.requests_attempted == 2
    assert state.next_request_at == 80.0 and state.last_request_at == 15.0
    assert state.unavailable_until[("groq", "fixture")] == float("inf")
    config.llm.max_concurrent_requests = 2
    assert execution.request_state(config.llm).semaphore._value == 4


def test_shared_holders_replace_their_own_slots_independently_on_new_loops() -> None:
    settings = LLMConfig([ProviderConfig("groq", "fixture")], max_concurrent_requests=4)
    execution = ModelExecution()

    async def initialize() -> tuple[ModelExecution, RequestState]:
        shared = execution.share_initialized(settings)
        state = execution.request_state(settings)
        state.requests_attempted = 3
        return shared, state

    shared, old = asyncio.run(initialize())
    settings.max_concurrent_requests = 2

    async def rebind() -> None:
        parent = execution.request_state(settings)
        assert parent is not old and parent.requests_attempted == 0
        assert parent.semaphore._value == 2
        assert shared._state is old
        child = shared.request_state(settings)
        assert child is not old and child is not parent
        assert child.requests_attempted == 0 and child.semaphore._value == 2
        assert execution.request_state(settings) is parent

    asyncio.run(rebind())
