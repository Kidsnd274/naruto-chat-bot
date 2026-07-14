"""Tests for ai_client.chat() parameter dispatch.

Verify that ModelParams fields land in the right place on the request:
standard fields as kwargs, non-standard ones inside `extra_body`, and
unset (None) fields omitted entirely.
"""
import asyncio
import importlib
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def ai_module(monkeypatch, initialized_config):
    """Reload ai_client with a fake openai client + clean model_params slate."""
    pytest.importorskip("openai")
    sys.modules.pop("ai_client", None)
    ai = importlib.import_module("ai_client")

    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(return_value=MagicMock(name="response"))

    monkeypatch.setattr(ai, "client", fake_client)
    monkeypatch.setattr(ai, "model", "test-model")

    return ai, fake_client


def _call_kwargs(fake_client) -> dict:
    fake_client.chat.completions.create.assert_called_once()
    return fake_client.chat.completions.create.call_args.kwargs


_MSG = [{"role": "user", "content": "hi"}]


async def test_chat_omits_all_params_when_none_set(ai_module):
    ai, fake_client = ai_module
    from config import ModelParams
    ai.model_params = ModelParams()

    await ai.chat(_MSG)

    kwargs = _call_kwargs(fake_client)
    assert kwargs == {"model": "test-model", "messages": _MSG}
    assert "extra_body" not in kwargs


async def test_chat_passes_standard_params_as_kwargs(ai_module):
    ai, fake_client = ai_module
    from config import ModelParams
    ai.model_params = ModelParams(temperature=0.7, top_p=0.9)

    await ai.chat(_MSG)

    kwargs = _call_kwargs(fake_client)
    assert kwargs["temperature"] == 0.7
    assert kwargs["top_p"] == 0.9
    assert "extra_body" not in kwargs


async def test_chat_routes_non_standard_params_to_extra_body(ai_module):
    ai, fake_client = ai_module
    from config import ModelParams
    ai.model_params = ModelParams(
        top_k=40,
        min_p=0.05,
        repeat_penalty=1.1,
        chat_template_kwargs={"enable_thinking": False},
    )

    await ai.chat(_MSG)

    kwargs = _call_kwargs(fake_client)
    assert kwargs["extra_body"] == {
        "top_k": 40,
        "min_p": 0.05,
        "repeat_penalty": 1.1,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    # None of these should leak out as top-level kwargs.
    for name in ("top_k", "min_p", "repeat_penalty", "chat_template_kwargs"):
        assert name not in kwargs


async def test_chat_handles_mixed_standard_and_extra_body(ai_module):
    ai, fake_client = ai_module
    from config import ModelParams
    ai.model_params = ModelParams(temperature=0.7, top_k=40)

    await ai.chat(_MSG)

    kwargs = _call_kwargs(fake_client)
    assert kwargs["temperature"] == 0.7
    assert kwargs["extra_body"] == {"top_k": 40}
    assert "top_k" not in kwargs


async def test_chat_omits_extra_body_key_when_only_standard_set(ai_module):
    """extra_body should be absent, not an empty dict, when no non-standard params are set."""
    ai, fake_client = ai_module
    from config import ModelParams
    ai.model_params = ModelParams(temperature=0.7)

    await ai.chat(_MSG)

    kwargs = _call_kwargs(fake_client)
    assert "extra_body" not in kwargs


async def test_chat_temperature_zero_is_passed_through(ai_module):
    """temperature=0.0 must reach the API — it's falsy but not None."""
    ai, fake_client = ai_module
    from config import ModelParams
    ai.model_params = ModelParams(temperature=0.0)

    await ai.chat(_MSG)

    kwargs = _call_kwargs(fake_client)
    assert kwargs["temperature"] == 0.0


async def test_chat_calls_are_process_wide_single_flight(ai_module):
    ai, fake_client = ai_module
    active = 0
    maximum_active = 0

    async def slow_create(**kwargs):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return MagicMock(name="response")

    fake_client.chat.completions.create.side_effect = slow_create

    await asyncio.gather(ai.chat(_MSG), ai.chat(_MSG), ai.chat(_MSG))

    assert maximum_active == 1
    assert fake_client.chat.completions.create.call_count == 3
