"""Tests for InMemoryChatHistory, RedisChatHistory, and merge_consecutive_roles."""
import importlib
import json
import sys

import pytest


@pytest.fixture
def chat_history_module(initialized_config):
    """Reload chat_history after config has been initialized."""
    sys.modules.pop("chat_history", None)
    return importlib.import_module("chat_history")


@pytest.fixture
def chat_history_with_memory(fresh_config, monkeypatch):
    """Chat history enabled, in-memory backend, with a known max size."""
    monkeypatch.setenv("CHAT_HISTORY_ENABLED", "true")
    monkeypatch.setenv("CHAT_HISTORY_TYPE", "memory")
    fresh_config.config.setup()
    # Override clamp for tighter testing
    fresh_config.config.chat_history.max_history = 5
    sys.modules.pop("chat_history", None)
    return importlib.import_module("chat_history")


# ---------- merge_consecutive_roles ----------

def test_merge_consecutive_roles_empty(chat_history_module):
    assert chat_history_module.merge_consecutive_roles([]) == []


def test_merge_consecutive_roles_alternating_untouched(chat_history_module):
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "yo"},
        {"role": "user", "content": "how"},
    ]
    assert chat_history_module.merge_consecutive_roles(msgs) == msgs


def test_merge_consecutive_roles_joins_consecutive_users(chat_history_module):
    msgs = [
        {"role": "user", "content": "Alice: hi"},
        {"role": "user", "content": "Bob: hello"},
        {"role": "assistant", "content": "Hey both"},
    ]
    merged = chat_history_module.merge_consecutive_roles(msgs)
    assert merged == [
        {"role": "user", "content": "Alice: hi\nBob: hello"},
        {"role": "assistant", "content": "Hey both"},
    ]


def test_merge_consecutive_roles_joins_consecutive_assistants(chat_history_module):
    msgs = [
        {"role": "assistant", "content": "one"},
        {"role": "assistant", "content": "two"},
    ]
    merged = chat_history_module.merge_consecutive_roles(msgs)
    assert merged == [{"role": "assistant", "content": "one\ntwo"}]


def test_structured_user_merge_preserves_image_position(chat_history_module):
    first = {
        "role": "user",
        "content": [
            {"type": "text", "text": "message 10"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAA"}},
        ],
    }
    second = {"role": "user", "content": "message 11"}

    merged = chat_history_module.merge_consecutive_roles([first, second])

    assert [part["type"] for part in merged[0]["content"]] == [
        "text", "image_url", "text", "text",
    ]
    assert merged[0]["content"][1]["image_url"]["url"].endswith("AAA")
    assert merged[0]["content"][-1]["text"] == "message 11"


def test_render_history_message_labels_image_owner(chat_history_module):
    rendered = chat_history_module.render_history_message({
        "role": "user",
        "content": "Alice: Look at this",
        "sender_name": "Alice",
        "telegram_message_id": 123,
        "reply_to_message_id": None,
        "attachments": [{
            "kind": "photo",
            "mime_type": "image/jpeg",
            "base64": "SENTINEL",
            "width": 1280,
            "height": 720,
        }],
    })

    assert rendered["content"][0] == {
        "type": "text",
        "text": "[Telegram message 123 from Alice]\nAlice: Look at this\n[Image 1 follows]",
    }
    assert rendered["content"][1]["image_url"]["url"] == (
        "data:image/jpeg;base64,SENTINEL"
    )


# ---------- InMemoryChatHistory ----------

def test_in_memory_add_and_get(chat_history_with_memory):
    ch = chat_history_with_memory.InMemoryChatHistory()
    ch.add_user_message(1, "Alice", "hello")
    ch.add_assistant_message(1, "hi back")
    assert ch.get_chat_history(1) == [
        {"role": "user", "content": "Alice: hello"},
        {"role": "assistant", "content": "hi back"},
    ]


def test_in_memory_isolated_per_chat(chat_history_with_memory):
    ch = chat_history_with_memory.InMemoryChatHistory()
    ch.add_user_message(1, "A", "in chat 1")
    ch.add_user_message(2, "B", "in chat 2")
    assert ch.get_curr_len(1) == 1
    assert ch.get_curr_len(2) == 1
    assert "chat 1" in ch.get_chat_history(1)[0]["content"]
    assert "chat 2" in ch.get_chat_history(2)[0]["content"]


def test_in_memory_clear(chat_history_with_memory):
    ch = chat_history_with_memory.InMemoryChatHistory()
    ch.add_user_message(1, "A", "hi")
    ch.add_assistant_message(1, "yo")
    ch.clear(1)
    assert ch.get_curr_len(1) == 0
    assert ch.get_chat_history(1) == []


def test_in_memory_respects_max_history(chat_history_with_memory):
    # max_history fixture is 5
    ch = chat_history_with_memory.InMemoryChatHistory()
    for i in range(10):
        ch.add_user_message(1, "U", f"msg{i}")
    assert ch.get_curr_len(1) == 5
    # The oldest five should have been dropped; first remaining is msg5
    first = ch.get_chat_history(1)[0]["content"]
    assert "msg5" in first or first.startswith("U: msg5")


def test_in_memory_merges_consecutive_user_messages_on_read(chat_history_with_memory):
    ch = chat_history_with_memory.InMemoryChatHistory()
    ch.add_user_message(1, "Alice", "first")
    ch.add_user_message(1, "Bob", "second")
    history = ch.get_chat_history(1)
    assert len(history) == 1
    assert history[0]["role"] == "user"
    assert "Alice: first" in history[0]["content"]
    assert "Bob: second" in history[0]["content"]


def test_in_memory_raw_history_stays_unmerged_and_chronological(chat_history_with_memory):
    ch = chat_history_with_memory.InMemoryChatHistory()
    ch.add_user_message(1, "Alice", "first")
    ch.add_user_message(1, "Bob", "second")

    assert ch.get_raw_chat_history(1) == [
        {"role": "user", "content": "Alice: first"},
        {"role": "user", "content": "Bob: second"},
    ]


def test_in_memory_get_is_idempotent(chat_history_with_memory):
    """Reading history must not mutate stored state. Regression test: the
    merge step used to write back into the deque's own dicts, so each read
    duplicated the consecutive run of messages."""
    ch = chat_history_with_memory.InMemoryChatHistory()
    ch.add_user_message(1, "Alice", "hey")
    ch.add_user_message(1, "Bob", "yo")
    ch.add_user_message(1, "Alice", "what did Bob say?")

    first = ch.get_chat_history(1)
    second = ch.get_chat_history(1)
    third = ch.get_chat_history(1)

    assert first == second == third
    # The merged content must contain each message exactly once.
    content = third[0]["content"]
    assert content.count("Bob: yo") == 1
    assert content.count("what did Bob say?") == 1


def test_in_memory_factory_returns_in_memory(chat_history_with_memory):
    inst = chat_history_with_memory.create_chat_history()
    assert isinstance(inst, chat_history_with_memory.InMemoryChatHistory)


def test_in_memory_attachment_is_evicted_with_record(chat_history_with_memory):
    ch = chat_history_with_memory.InMemoryChatHistory()
    ch.add_user_message(
        1,
        "Alice",
        "photo",
        telegram_message_id=1,
        attachments=[{"kind": "photo", "mime_type": "image/jpeg", "base64": "EVICT_ME"}],
    )
    for i in range(2, 7):
        ch.add_user_message(1, "Alice", str(i), telegram_message_id=i, attachments=[])

    assert "EVICT_ME" not in json.dumps(ch.get_raw_chat_history(1))


# ---------- RedisChatHistory (via fakeredis) ----------

@pytest.fixture
def redis_chat_history(fresh_config, monkeypatch):
    fakeredis = pytest.importorskip("fakeredis")
    monkeypatch.setenv("CHAT_HISTORY_ENABLED", "true")
    monkeypatch.setenv("CHAT_HISTORY_TYPE", "redis")
    fresh_config.config.setup()
    fresh_config.config.chat_history.max_history = 5

    sys.modules.pop("chat_history", None)
    chat_history_module = importlib.import_module("chat_history")

    # Each test gets its own in-memory fake server so state can't leak
    server = fakeredis.FakeServer()

    def _fake_redis(*args, **kwargs):
        kwargs.pop("host", None)
        kwargs.pop("port", None)
        kwargs.pop("db", None)
        return fakeredis.FakeRedis(server=server, **kwargs)

    monkeypatch.setattr(chat_history_module.redis, "Redis", _fake_redis)
    return chat_history_module


def test_redis_add_and_get(redis_chat_history):
    ch = redis_chat_history.RedisChatHistory()
    ch.add_user_message(1, "Alice", "hello")
    ch.add_assistant_message(1, "hi back")
    history = ch.get_chat_history(1)
    assert history == [
        {"role": "user", "content": "Alice: hello"},
        {"role": "assistant", "content": "hi back"},
    ]


def test_redis_raw_history_stays_unmerged_and_chronological(redis_chat_history):
    ch = redis_chat_history.RedisChatHistory()
    ch.add_user_message(1, "Alice", "first")
    ch.add_user_message(1, "Bob", "second")

    assert ch.get_raw_chat_history(1) == [
        {"role": "user", "content": "Alice: first"},
        {"role": "user", "content": "Bob: second"},
    ]


def test_redis_clear(redis_chat_history):
    ch = redis_chat_history.RedisChatHistory()
    ch.add_user_message(1, "Alice", "hi")
    ch.clear(1)
    assert ch.get_curr_len(1) == 0


def test_redis_respects_max_history(redis_chat_history):
    ch = redis_chat_history.RedisChatHistory()
    for i in range(10):
        ch.add_user_message(1, "U", f"msg{i}")
    assert ch.get_curr_len(1) == 5


def test_redis_isolated_per_chat(redis_chat_history):
    ch = redis_chat_history.RedisChatHistory()
    ch.add_user_message(1, "A", "in chat 1")
    ch.add_user_message(2, "B", "in chat 2")
    assert ch.get_curr_len(1) == 1
    assert ch.get_curr_len(2) == 1


def test_redis_ltrim_and_clear_remove_embedded_media(redis_chat_history):
    ch = redis_chat_history.RedisChatHistory()
    ch.add_user_message(
        1,
        "Alice",
        "photo",
        telegram_message_id=1,
        attachments=[{"kind": "photo", "mime_type": "image/jpeg", "base64": "EVICT_ME"}],
    )
    for i in range(2, 7):
        ch.add_user_message(1, "Alice", str(i), telegram_message_id=i, attachments=[])

    assert "EVICT_ME" not in json.dumps(ch.get_raw_chat_history(1))
    assert ch.r.keys("*") == [b"chat:history:1"] or ch.r.keys("*") == ["chat:history:1"]

    ch.clear(1)
    assert ch.r.keys("*") == []


# ---------- IChatHistory is abstract ----------

def test_ichathistory_cannot_be_instantiated(chat_history_module):
    with pytest.raises(TypeError):
        chat_history_module.IChatHistory()
