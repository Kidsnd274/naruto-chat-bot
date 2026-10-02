"""LLMClient: request parameters, reasoning, model resolution, errors and
single-flight (ported from the old test_ai_client.py)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import openai
import pytest

from naruto.llm import LLMClient, LLMError, split_reasoning

MSG = [{"role": "user", "content": "hi"}]


def response(content="hello", reasoning=None, model="served-model"):
    message = SimpleNamespace(content=content, reasoning_content=reasoning)
    usage = MagicMock()
    usage.model_dump.return_value = {"prompt_tokens": 10, "completion_tokens": 2}
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")],
                           usage=usage, model=model)


@pytest.fixture
def llm(services):
    services.settings.set("model.name", "test-model", actor="t")
    client = LLMClient(services.settings, "key")
    fake = MagicMock()
    fake.chat.completions.create = AsyncMock(return_value=response())
    client._get_client = lambda: fake
    client.fake = fake
    return client


def sent_kwargs(llm) -> dict:
    llm.fake.chat.completions.create.assert_called_once()
    return llm.fake.chat.completions.create.call_args.kwargs


async def test_defaults_send_only_model_messages_and_output_cap(llm):
    await llm.chat(MSG)
    assert sent_kwargs(llm) == {"model": "test-model", "messages": MSG, "max_tokens": 2048}


async def test_standard_params_are_kwargs(llm, services):
    services.settings.set("model.temperature", 0.7, actor="t")
    services.settings.set("model.top_p", 0.9, actor="t")
    await llm.chat(MSG)
    kwargs = sent_kwargs(llm)
    assert (kwargs["temperature"], kwargs["top_p"]) == (0.7, 0.9)
    assert "extra_body" not in kwargs


async def test_non_standard_params_go_to_extra_body(llm, services):
    for key, value in {"model.top_k": 40, "model.min_p": 0.05, "model.repeat_penalty": 1.1,
                       "model.chat_template_kwargs": {"enable_thinking": False}}.items():
        services.settings.set(key, value, actor="t")
    await llm.chat(MSG)
    kwargs = sent_kwargs(llm)
    assert kwargs["extra_body"] == {"top_k": 40, "min_p": 0.05, "repeat_penalty": 1.1,
                                    "chat_template_kwargs": {"enable_thinking": False}}
    for name in ("top_k", "min_p", "repeat_penalty", "chat_template_kwargs"):
        assert name not in kwargs


async def test_temperature_zero_is_sent(llm, services):
    services.settings.set("model.temperature", 0.0, actor="t")
    await llm.chat(MSG)
    assert sent_kwargs(llm)["temperature"] == 0.0


async def test_reasoning_switch_merges_into_template_kwargs(llm, services):
    services.settings.set("model.chat_template_kwargs", {"foo": 1, "enable_thinking": True},
                          actor="t")
    await llm.chat(MSG, reasoning=False)
    assert sent_kwargs(llm)["extra_body"]["chat_template_kwargs"] == {
        "foo": 1, "enable_thinking": False}


async def test_output_cap_can_be_disabled_or_overridden(llm, services):
    services.settings.set("model.max_output_tokens", None, actor="t")
    await llm.chat(MSG)
    assert "max_tokens" not in sent_kwargs(llm)
    llm.fake.chat.completions.create.reset_mock()
    await llm.chat(MSG, max_tokens=99)
    assert sent_kwargs(llm)["max_tokens"] == 99


async def test_result_splits_reasoning_and_reports_usage(llm):
    llm.fake.chat.completions.create.return_value = response("<think>hmm</think>\nAnswer")
    result = await llm.chat(MSG)
    assert (result.text, result.reasoning) == ("Answer", "hmm")
    assert result.usage == {"prompt_tokens": 10, "completion_tokens": 2}
    assert result.model == "served-model" and result.finish_reason == "stop"

    llm.fake.chat.completions.create.return_value = response("Answer", reasoning="server-side")
    assert (await llm.chat(MSG)).reasoning == "server-side"


@pytest.mark.parametrize("content,text,reasoning", [
    ("plain", "plain", None),
    ("<think>a</think>b", "b", "a"),
    ("<THINK>\n a \n</THINK>\n\n b ", "b", "a"),
    ("<think>cut off mid thought", "", "cut off mid thought"),
    ("", "", None),
])
def test_split_reasoning(content, text, reasoning):
    assert split_reasoning(content) == (text, reasoning)


async def test_api_errors_become_llm_errors(llm):
    request = httpx.Request("POST", "http://x/v1/chat/completions")
    llm.fake.chat.completions.create.side_effect = openai.APIConnectionError(request=request)
    with pytest.raises(LLMError):
        await llm.chat(MSG)
    llm.fake.chat.completions.create.side_effect = None
    llm.fake.chat.completions.create.return_value = SimpleNamespace(choices=[])
    with pytest.raises(LLMError, match="no choices"):
        await llm.chat(MSG)


async def test_calls_are_process_wide_single_flight(llm):
    active = maximum = 0

    async def slow(**kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        return response()

    llm.fake.chat.completions.create.side_effect = slow
    results = await asyncio.gather(llm.chat(MSG), llm.chat(MSG), llm.chat(MSG))
    assert maximum == 1 and len(results) == 3
    assert llm.in_flight == 0 and llm.waiting == 0


async def test_cancelled_waiter_does_not_leak_counters(llm):
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked(**kwargs):
        started.set()
        await release.wait()
        return response()

    llm.fake.chat.completions.create.side_effect = blocked
    first = asyncio.create_task(llm.chat(MSG))
    await started.wait()
    second = asyncio.create_task(llm.chat(MSG))
    await asyncio.sleep(0)
    assert llm.waiting == 1
    second.cancel()
    await asyncio.gather(second, return_exceptions=True)
    release.set()
    await first
    assert llm.waiting == 0 and llm.in_flight == 0


async def test_empty_model_name_uses_first_listed_model(services):
    client = LLMClient(services.settings, "key")
    fake = MagicMock()
    fake.chat.completions.create = AsyncMock(return_value=response())
    client._get_client = lambda: fake
    client.list_models = AsyncMock(return_value=["qwen3.8-27b", "other"])
    await client.chat(MSG)
    await client.chat(MSG)
    assert fake.chat.completions.create.call_args.kwargs["model"] == "qwen3.8-27b"
    client.list_models.assert_awaited_once()  # cached per endpoint

    client.list_models = AsyncMock(return_value=[])
    services.settings.set("model.endpoint_url", "http://elsewhere/v1", actor="t")
    with pytest.raises(LLMError, match="lists no models"):
        await client.chat(MSG)


def test_client_is_rebuilt_when_the_endpoint_changes(services):
    client = LLMClient(services.settings, "key")
    first = client._get_client()
    assert client._get_client() is first
    services.settings.set("model.endpoint_url", "http://gufo:8080/v1", actor="t")
    second = client._get_client()
    assert second is not first
    assert str(second.base_url).startswith("http://gufo:8080/v1")
