import ai_client
import bot

from dotenv import load_dotenv
import os
import logging

if __name__ == '__main__':
    load_dotenv()  # reads .env in the current working directory (if present)

    # AI Client Variables
    _base_url = os.getenv("OPENAI_BASE_URL")
    _api_key = os.getenv("OPENAI_API_KEY")
    _model = os.getenv("OPENAI_MODEL", "qwen3-30b-a3b-2507-instruct-unsloth-settings")

    if not _api_key:
        raise RuntimeError("Missing OPENAI_API_KEY in environment/.env")
    if not _base_url:
        raise RuntimeError("Missing OPENAI_BASE_URL in environment/.env")

    # Telegram Bot Variables
    _token = os.getenv("TELEGRAM_BOT_TOKEN")

    if not _token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN. Put it in .env or export it in your shell.")

    logger = logging.getLogger("Starter")
    logger.info("Creating AI Client")
    ai_client.setup(_base_url, _api_key, _model)

    logger.info("Creating Telegram Bot Connection")
    bot.setup(_token)