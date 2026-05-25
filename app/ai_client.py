import asyncio
import logging
import os
import openai

from dotenv import load_dotenv

# def _normalize_base_url(url: str) -> str:
#     url = (url or "").strip().rstrip("/")
#     if url and not url.endswith("/v1"):
#         url = url + "/v1"
#     return url

logger = logging.getLogger("ai_client")

base_url = os.getenv("OPENAI_BASE_URL")
api_key = os.getenv("OPENAI_API_KEY")
model = os.getenv("OPENAI_MODEL", "qwen3-30b-a3b-2507-instruct-unsloth-settings")

client = None

async def chat(messages) -> str:
    """
    messages format:
      [{"role":"system|user|assistant", "content":"..."}]
    """
    resp = await client.chat.completions.create(
        model=model,
        messages=messages
    )

    return resp

async def _test_connection():
    async for _ in client.models.list():
        return

def test():
    """Test connectivity to the AI backend. Call this after setup()."""
    asyncio.run(_test_connection())

def setup(new_base_url, new_api_key, new_model):
    global base_url, api_key, model, client

    base_url = new_base_url
    api_key = new_api_key
    model = new_model

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