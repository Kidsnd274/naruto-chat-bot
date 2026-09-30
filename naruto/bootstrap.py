"""Secrets and bootstrap values read from the environment (.env).

Everything else lives in the database and is edited in the web admin
(see ``naruto.settings``). Legacy variables such as ``OPENAI_BASE_URL`` are
only read once, to seed the database on first start.
"""

from dataclasses import dataclass
import logging
import os

logger = logging.getLogger(__name__)

DEFAULT_DATABASE_PATH = "data/naruto.db"
DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 8765


@dataclass(frozen=True)
class Bootstrap:
    telegram_bot_token: str
    openai_api_key: str
    admin_password: str
    owner_user_id: int | None
    database_path: str
    web_host: str
    web_port: int

    @property
    def web_enabled(self) -> bool:
        return bool(self.admin_password)


def _optional_int(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from None


def load_bootstrap() -> Bootstrap:
    """Read bootstrap values from the environment. Call after load_dotenv()."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "Missing TELEGRAM_BOT_TOKEN. Put it in .env or export it in your shell."
        )

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        # Local OpenAI-compatible servers usually ignore the key, but the SDK
        # refuses an empty one.
        logger.warning("OPENAI_API_KEY is not set; sending a placeholder key.")
        api_key = "not-set"

    admin_password = os.getenv("ADMIN_PASSWORD", "")
    if not admin_password:
        logger.warning("ADMIN_PASSWORD is not set; the web admin is disabled.")

    owner_user_id = _optional_int("OWNER_USER_ID")
    if owner_user_id is None:
        logger.warning(
            "OWNER_USER_ID is not set; approvals work only from the web admin "
            "and the bot ignores all private messages."
        )

    web_port = _optional_int("WEB_PORT") or DEFAULT_WEB_PORT

    return Bootstrap(
        telegram_bot_token=token,
        openai_api_key=api_key,
        admin_password=admin_password,
        owner_user_id=owner_user_id,
        database_path=os.getenv("DATABASE_PATH", "").strip() or DEFAULT_DATABASE_PATH,
        web_host=os.getenv("WEB_HOST", "").strip() or DEFAULT_WEB_HOST,
        web_port=web_port,
    )
