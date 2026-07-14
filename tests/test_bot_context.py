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


def test_context_includes_reply_behavior_instruction(bot_module):
    msg = bot_module.build_context_message(
        chat_info={"chat_name": "X", "users": []},
        chat_type="group",
        bot_username="naruto_bot",
        current_speaker={"display_name": "Alice", "username": "alice123"},
        now=_now(),
    )
    assert "## Reply behavior" in msg["content"]
    assert "[REPLY]" in msg["content"]


# ---------- persona ----------

def test_context_includes_persona_when_provided(bot_module):
    persona = "You are Naruto Uzumaki. Dattebayo!"
    msg = bot_module.build_context_message(
        chat_info={"chat_name": "X", "users": []},
        chat_type="group",
        bot_username="naruto_bot",
        current_speaker={"display_name": "Alice", "username": "alice123"},
        now=_now(),
        persona=persona,
    )
    content = msg["content"]
    # Persona at the top, followed by a blank line, then the context block.
    assert content.startswith(persona + "\n\n## Chat Context")


def test_context_omits_persona_when_empty(bot_module):
    msg_with_empty = bot_module.build_context_message(
        chat_info={"chat_name": "X", "users": []},
        chat_type="group",
        bot_username="naruto_bot",
        current_speaker={"display_name": "Alice", "username": "alice123"},
        now=_now(),
        persona="",
    )
    msg_without = bot_module.build_context_message(
        chat_info={"chat_name": "X", "users": []},
        chat_type="group",
        bot_username="naruto_bot",
        current_speaker={"display_name": "Alice", "username": "alice123"},
        now=_now(),
    )
    # Empty persona produces byte-identical output to no persona at all,
    # and starts directly with the context header (no leading blank line).
    assert msg_with_empty["content"] == msg_without["content"]
    assert msg_with_empty["content"].startswith("## Chat Context")


# ---------- LLM message assembly ----------

def test_llm_messages_drop_orphaned_leading_assistant(bot_module):
    context = {"role": "system", "content": "context"}
    history = [
        {"role": "assistant", "content": "orphaned answer"},
        {"role": "user", "content": "current question"},
    ]

    result = bot_module.build_llm_messages(context, history, max_model_tokens=None)

    assert result.messages == [
        context,
        {"role": "user", "content": "current question"},
    ]
    assert result.dropped_history_messages == 1


def test_llm_messages_trim_oldest_complete_turn_to_token_budget(bot_module):
    context = {"role": "system", "content": "context"}
    current = {"role": "user", "content": "current question"}
    history = [
        {"role": "user", "content": "old question " * 20},
        {"role": "assistant", "content": "old answer " * 20},
        current,
    ]
    minimum = bot_module.estimate_message_tokens([context, current])

    result = bot_module.build_llm_messages(
        context,
        history,
        max_model_tokens=minimum + 1,
    )

    assert result.limit_reached is True
    assert result.messages == [context, current]
    assert result.estimated_tokens_after <= minimum + 1
    assert result.dropped_history_messages == 2


def test_llm_messages_preserve_current_consecutive_user_message(bot_module):
    context = {"role": "system", "content": "context"}
    current = {"role": "user", "content": "current question"}
    history = [
        {"role": "user", "content": "older group chatter " * 30},
        current,
    ]
    minimum = bot_module.estimate_message_tokens([context, current])

    result = bot_module.build_llm_messages(
        context,
        history,
        max_model_tokens=minimum,
    )

    assert result.limit_reached is True
    assert result.messages == [context, current]
    assert result.dropped_history_messages == 1


def test_llm_messages_report_unavoidable_overflow(bot_module):
    context = {"role": "system", "content": "long context " * 20}
    current = {"role": "user", "content": "long current message " * 20}

    result = bot_module.build_llm_messages(
        context,
        [current],
        max_model_tokens=1,
    )

    assert result.limit_reached is True
    assert result.messages == [context, current]
    assert result.estimated_tokens_after > 1
    assert result.dropped_history_messages == 0


# ---------- parse_reply_marker ----------

@pytest.mark.parametrize("text,expected_should_reply,expected_clean", [
    ("[REPLY] hello", True, "hello"),
    ("[reply] hello", True, "hello"),
    ("[Reply]hello", True, "hello"),
    ("  [REPLY]   hello world  ", True, "hello world  "),
    ("**[REPLY]** sure thing", True, "sure thing"),
    ("hello [REPLY] world", False, "hello [REPLY] world"),
    ("hello world", False, "hello world"),
    ("", False, ""),
])
def test_parse_reply_marker(bot_module, text, expected_should_reply, expected_clean):
    should_reply, cleaned = bot_module.parse_reply_marker(text)
    assert should_reply is expected_should_reply
    assert cleaned == expected_clean


# ---------- build_reply_prefix ----------

class _FakeUser:
    def __init__(self, full_name=None, username=None, id=None):
        self.full_name = full_name
        self.username = username
        self.id = id


class _FakeMsg:
    def __init__(self, message_id=999, text=None, caption=None, from_user=None):
        self.message_id = message_id
        self.text = text
        self.caption = caption
        self.from_user = from_user


BOT_ID = 42


def test_reply_prefix_none_when_not_a_reply(bot_module):
    assert bot_module.build_reply_prefix(None, BOT_ID, None) == ""


def test_reply_prefix_includes_full_snippet_for_other_user(bot_module):
    msg = _FakeMsg(
        message_id=100,
        text="what's your favourite ramen?",
        from_user=_FakeUser(full_name="Bob", id=7),
    )
    assert bot_module.build_reply_prefix(msg, BOT_ID, last_seen_message_id=50) == (
        '[replying to Bob: "what\'s your favourite ramen?"] '
    )


def test_reply_prefix_uses_you_when_replying_to_bot(bot_module):
    msg = _FakeMsg(
        message_id=100,
        text="I'm gonna be Hokage!",
        from_user=_FakeUser(full_name="Naruto Bot", id=BOT_ID),
    )
    assert bot_module.build_reply_prefix(msg, BOT_ID, last_seen_message_id=50) == (
        '[replying to you: "I\'m gonna be Hokage!"] '
    )


def test_reply_prefix_falls_back_to_username_when_no_full_name(bot_module):
    msg = _FakeMsg(
        message_id=100,
        text="hi",
        from_user=_FakeUser(username="alice123", id=7),
    )
    assert bot_module.build_reply_prefix(msg, BOT_ID, None) == '[replying to alice123: "hi"] '


def test_reply_prefix_uses_someone_when_no_user_info(bot_module):
    msg = _FakeMsg(message_id=100, text="hi", from_user=None)
    assert bot_module.build_reply_prefix(msg, BOT_ID, None) == '[replying to someone: "hi"] '


def test_reply_prefix_uses_caption_when_no_text(bot_module):
    msg = _FakeMsg(
        message_id=100,
        caption="check this photo",
        from_user=_FakeUser(full_name="Bob", id=7),
    )
    assert bot_module.build_reply_prefix(msg, BOT_ID, None) == (
        '[replying to Bob: "check this photo"] '
    )


def test_reply_prefix_omits_snippet_when_no_text_or_caption(bot_module):
    msg = _FakeMsg(message_id=100, from_user=_FakeUser(full_name="Bob", id=7))
    assert bot_module.build_reply_prefix(msg, BOT_ID, None) == "[replying to Bob] "


def test_reply_prefix_skips_when_referent_is_last_seen(bot_module):
    msg = _FakeMsg(
        message_id=100,
        text="hi",
        from_user=_FakeUser(full_name="Bob", id=7),
    )
    assert bot_module.build_reply_prefix(msg, BOT_ID, last_seen_message_id=100) == ""


def test_reply_prefix_included_when_referent_is_older(bot_module):
    msg = _FakeMsg(
        message_id=100,
        text="hi",
        from_user=_FakeUser(full_name="Bob", id=7),
    )
    assert bot_module.build_reply_prefix(msg, BOT_ID, last_seen_message_id=200) == (
        '[replying to Bob: "hi"] '
    )


def test_reply_prefix_included_when_no_last_seen(bot_module):
    msg = _FakeMsg(
        message_id=100,
        text="hi",
        from_user=_FakeUser(full_name="Bob", id=7),
    )
    assert bot_module.build_reply_prefix(msg, BOT_ID, last_seen_message_id=None) == (
        '[replying to Bob: "hi"] '
    )
