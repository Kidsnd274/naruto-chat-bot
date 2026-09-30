"""Shared pytest fixtures.

`pythonpath = app` in pytest.ini makes `import config`, `import util`, etc. work
without packaging the app.
"""
import importlib
import sys

import pytest


@pytest.fixture
def fresh_config(monkeypatch, tmp_path):
    """Force-reload the config module with a clean singleton, and a default
    set of env vars that turn most features off. Tests opt back in by setting
    env vars *before* calling `config.setup()`.
    """
    # Strip env vars that might leak from the developer's shell
    for var in [
        "WHITELIST_ENABLED",
        "CHAT_HISTORY_ENABLED",
        "CHAT_HISTORY_TYPE",
        "DEBUG_CHAT_HISTORY",
        "REDIS_HOST",
        "REDIS_PORT",
        "REDIS_DB",
        "CONFIG_PATH",
        "SYSTEM_PROMPT_PATH",
        "MAX_MODEL_TOKENS",
        "MEDIA_ENABLED",
        "MAX_MEDIA_BYTES",
        "ESTIMATED_IMAGE_TOKENS",
    ]:
        monkeypatch.delenv(var, raising=False)

    # Point CONFIG_PATH and SYSTEM_PROMPT_PATH at non-existent files so
    # setup() uses defaults rather than picking up files from CWD.
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "no-such-config.json"))
    monkeypatch.setenv("SYSTEM_PROMPT_PATH", str(tmp_path / "no-such-system-prompt.md"))

    # Drop cached modules so the singleton resets
    for mod in ("config", "chat_history"):
        sys.modules.pop(mod, None)

    config_module = importlib.import_module("config")
    return config_module


@pytest.fixture
def initialized_config(fresh_config):
    """Like fresh_config but already `setup()`-ed with defaults."""
    fresh_config.config.setup()
    return fresh_config


# ---------------------------------------------------------------- naruto/

@pytest.fixture
def db():
    from naruto.db import open_database

    database = open_database(":memory:")
    yield database
    database.close()


@pytest.fixture
def bootstrap():
    from naruto.bootstrap import Bootstrap

    return Bootstrap(
        telegram_bot_token="123456:TEST-TOKEN",
        openai_api_key="test-key",
        admin_password="correct horse",
        owner_user_id=1000,
        database_path=":memory:",
        web_host="127.0.0.1",
        web_port=8765,
    )


@pytest.fixture
def services(db, bootstrap):
    """Services on an in-memory database, with times shown in UTC and the
    bot already logged in."""
    from naruto.services import BotIdentity, Services

    services = Services.create(bootstrap, db)
    services.settings.set("general.timezone", "UTC", actor="test")
    services.status.bot = BotIdentity(id=42, username="naruto_bot", name="Naruto")
    return services
