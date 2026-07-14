import asyncio
import logging
import os
import openai

from dotenv import load_dotenv

from config import ModelParams

logger = logging.getLogger("ai_client")

base_url = os.getenv("OPENAI_BASE_URL")
api_key = os.getenv("OPENAI_API_KEY")
model = os.getenv("OPENAI_MODEL", "qwen3-30b-a3b-2507-instruct-unsloth-settings")

client = None
model_params: ModelParams = ModelParams()
_chat_lock = asyncio.Lock()

# Sampling params the OpenAI Python SDK validates and accepts as kwargs.
_STANDARD_PARAMS = ("temperature", "top_p")
# Sampling params the SDK doesn't know about — must go through extra_body
# to reach llama.cpp / vLLM / Lemonade etc.
_EXTRA_BODY_PARAMS = ("top_k", "min_p", "repeat_penalty", "chat_template_kwargs")


async def chat(messages) -> str:
    """
    messages format:
      [{"role":"system|user|assistant", "content":"..." or [content parts]}]
    """
    kwargs = {"model": model, "messages": messages}
    extra_body = {}

    for name in _STANDARD_PARAMS:
        val = getattr(model_params, name)
        if val is not None:
            kwargs[name] = val

    for name in _EXTRA_BODY_PARAMS:
        val = getattr(model_params, name)
        if val is not None:
            extra_body[name] = val

    if extra_body:
        kwargs["extra_body"] = extra_body

    # A local inference server generally has one useful execution slot. Keep
    # all callers process-wide single-flight so concurrent Telegram triggers
    # queue here instead of competing for GPU/CPU resources.
    async with _chat_lock:
        resp = await client.chat.completions.create(**kwargs)
    return resp

async def _test_connection():
    async for _ in client.models.list():
        return

def test():
    """Test connectivity to the AI backend. Call this after setup()."""
    asyncio.run(_test_connection())

def setup(new_base_url, new_api_key, new_model, new_model_params: ModelParams | None = None):
    global base_url, api_key, model, client, model_params

    base_url = new_base_url
    api_key = new_api_key
    model = new_model
    model_params = new_model_params or ModelParams()

    client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url)
    test()

if __name__ == "__main__":
    load_dotenv()

    _base_url = os.getenv("OPENAI_BASE_URL")
    _api_key = os.getenv("OPENAI_API_KEY")
    _model = os.getenv("OPENAI_MODEL", "qwen3-30b-a3b-2507-instruct-unsloth-settings")

    if not _api_key:
        raise RuntimeError("Missing OPENAI_API_KEY in environment/.env")
    if not _base_url:
        raise RuntimeError("Missing OPENAI_BASE_URL in environment/.env")

    setup(_base_url, _api_key, _model)
