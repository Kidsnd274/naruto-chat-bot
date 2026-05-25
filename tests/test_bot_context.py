"""Tests for build_context_message (the per-call system context block)."""
import importlib
import sys
from datetime import datetime, timezone

import pytest


@pytest.fixture
def bot_module(initialized_config):
    # build_context_message is a pure function; we just need bot's imports
    # to not blow up. config must be set up first.
    pytest.importorskip("telegram")
    sys.modules.pop("bot", None)
    return importlib.import_module("bot")


def _now():
    return datetime(2026, 5, 25, 14, 30, tzinfo=timezone.utc)


def test_context_includes_chat_name_and_type(bot_module):
    info = {"chat_name": "General Hangout", "users": []}
    msg = bot_module.build_context_message(
        chat_info=info,
        chat_type="group",
        bot_username="naruto_bot",
        current_speaker={"display_name": "Alice", "username": "alice123"},
        now=_now(),
    )
    assert msg["role"] == "system"
    assert "General Hangout (group)" in msg["content"]


def test_context_includes_time(bot_module):
    msg = bot_module.build_context_message(
        chat_info={"chat_name": "X", "users": []},
        chat_type="group",
        bot_username="naruto_bot",
        current_speaker={"display_name": "Alice", "username": "alice123"},
        now=_now(),
    )
    assert "2026-05-25" in msg["content"]
    assert "14:30" in msg["content"]


def test_context_includes_bot_handle(bot_module):
    msg = bot_module.build_context_message(
        chat_info={"chat_name": "X", "users": []},
        chat_type="private",
        bot_username="naruto_bot",
        current_speaker={"display_name": "Alice", "username": "alice123"},
        now=_now(),
    )
    assert "@naruto_bot" in msg["content"]


def test_context_includes_current_speaker(bot_module):
    msg = bot_module.build_context_message(
        chat_info={"chat_name": "X", "users": []},
        chat_type="group",
        bot_username="naruto_bot",
        current_speaker={"display_name": "Alice Smith", "username": "alice123"},
        now=_now(),
    )
    assert "Currently replying to: Alice Smith (@alice123)" in msg["content"]


def test_context_handles_speaker_without_username(bot_module):
    msg = bot_module.build_context_message(
        chat_info={"chat_name": "X", "users": []},
        chat_type="group",
        bot_username="naruto_bot",
        current_speaker={"display_name": "Charlie", "username": None},
        now=_now(),
    )
    assert "Charlie (no @username)" in msg["content"]


def test_context_lists_members_with_aliases(bot_module):
    info = {
        "chat_name": "Hangout",
        "users": [
            {"user_id": 1, "display_name": "Alice", "username": "alice123", "aliases": ["Sasuke"]},
            {"user_id": 2, "display_name": "Bob", "username": "bob", "aliases": []},
            {"user_id": 3, "display_name": "Charlie", "username": None, "aliases": ["Boss"]},
        ],
    }
    msg = bot_module.build_context_message(
        chat_info=info,
        chat_type="group",
        bot_username="naruto_bot",
        current_speaker={"display_name": "Alice", "username": "alice123"},
        now=_now(),
    )
    content = msg["content"]
    assert "## Members" in content
    assert "Alice (@alice123) [aliases: Sasuke]" in content
    assert "Bob (@bob)" in content
    assert "[aliases:" not in content.split("Bob (@bob)")[1].split("\n")[0]
    assert "Charlie (no @username) [aliases: Boss]" in content


def test_context_omits_members_section_when_empty(bot_module):
    msg = bot_module.build_context_message(
        chat_info={"chat_name": "Hangout", "users": []},
        chat_type="group",
        bot_username="naruto_bot",
        current_speaker={"display_name": "Alice", "username": "alice123"},
        now=_now(),
    )
    assert "## Members" not in msg["content"]


def test_context_falls_back_when_no_chat_name(bot_module):
    msg = bot_module.build_context_message(
        chat_info={"chat_name": "", "users": []},
        chat_type="private",
        bot_username="naruto_bot",
        current_speaker={"display_name": "Alice", "username": "alice123"},
        now=_now(),
    )
    assert "Chat type: private" in msg["content"]
