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

load_dotenv()

logger = logging.getLogger("ai_client")

_base_url = os.getenv("OPENAI_BASE_URL")
_api_key = os.getenv("OPENAI_API_KEY")
_model = os.getenv("OPENAI_MODEL", "qwen3-30b-a3b-2507-instruct-unsloth-settings")

if not _api_key:
    raise RuntimeError("Missing OPENAI_API_KEY in environment/.env")
if not _base_url:
    raise RuntimeError("Missing OPENAI_BASE_URL in environment/.env")

_client = openai.AsyncOpenAI(api_key=_api_key, base_url=_base_url)

async def _print_models():
    print(await _client.models.list())

async def chat(messages) -> str:
    """
    messages format:
      [{"role":"system|user|assistant", "content":"..."}]
    """
    resp = await _client.chat.completions.create(
        model=_model,
        messages=messages
    )

    return resp

if __name__ == "__main__":
    asyncio.run(_print_models())