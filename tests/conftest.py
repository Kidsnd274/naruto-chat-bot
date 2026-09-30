"""Shared pytest fixtures. `pythonpath = .` in pytest.ini makes the naruto
package importable; test helpers live in tests/fakes.py."""

import pytest

from naruto.bootstrap import Bootstrap
from naruto.db import open_database
from naruto.services import BotIdentity, Services


@pytest.fixture
def db():
    database = open_database(":memory:")
    yield database
    database.close()


@pytest.fixture
def bootstrap():
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
    services = Services.create(bootstrap, db)
    services.settings.set("general.timezone", "UTC", actor="test")
    services.status.bot = BotIdentity(id=42, username="naruto_bot", name="Naruto")
    return services
