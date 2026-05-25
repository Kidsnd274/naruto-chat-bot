"""Tests for InMemoryChatMetadata and RedisChatMetadata."""
import importlib
import sys
import types

import pytest


@pytest.fixture
def chat_metadata_memory(fresh_config, monkeypatch):
    """Metadata module loaded with chat history disabled (storage_type defaults to MEMORY)."""
    fresh_config.config.setup()
    sys.modules.pop("chat_metadata", None)
    return importlib.import_module("chat_metadata")


@pytest.fixture
def chat_metadata_redis(fresh_config, monkeypatch):
    fakeredis = pytest.importorskip("fakeredis")
    monkeypatch.setenv("CHAT_HISTORY_ENABLED", "true")
    monkeypatch.setenv("CHAT_HISTORY_TYPE", "redis")
    fresh_config.config.setup()

    sys.modules.pop("chat_metadata", None)
    chat_metadata_module = importlib.import_module("chat_metadata")

    server = fakeredis.FakeServer()

    def _fake_redis(*args, **kwargs):
        kwargs.pop("host", None)
        kwargs.pop("port", None)
        kwargs.pop("db", None)
        return fakeredis.FakeRedis(server=server, **kwargs)

    # NOTE: assigning to `redis.Redis` directly breaks fakeredis — FakeRedis
    # extends redis.Redis and its super().__init__ chain drops decode_responses
    # once the class is shadowed. Instead, replace the *local* `redis` reference
    # inside chat_metadata with a stub module that exposes just what we need.
    import redis as real_redis
    fake_redis_module = types.SimpleNamespace(
        Redis=_fake_redis,
        ConnectionError=real_redis.ConnectionError,
    )
    monkeypatch.setattr(chat_metadata_module, "redis", fake_redis_module)
    return chat_metadata_module


# ---------- IChatMetadata is abstract ----------

def test_ichatmetadata_cannot_be_instantiated(chat_metadata_memory):
    with pytest.raises(TypeError):
        chat_metadata_memory.IChatMetadata()


# ---------- InMemoryChatMetadata ----------

def test_in_memory_update_user_creates_entry(chat_metadata_memory):
    cm = chat_metadata_memory.InMemoryChatMetadata()
    cm.update_user(1, 100, "Alice", "alice123")
    info = cm.get_chat_info(1)
    assert info["users"] == [
        {"user_id": 100, "display_name": "Alice", "username": "alice123", "aliases": []}
    ]


def test_in_memory_update_user_preserves_aliases(chat_metadata_memory):
    cm = chat_metadata_memory.InMemoryChatMetadata()
    cm.update_user(1, 100, "Alice", "alice123")
    cm.add_alias(1, 100, "Sasuke")
    cm.update_user(1, 100, "Alice Smith", "alice_new")
    info = cm.get_chat_info(1)
    assert info["users"][0]["display_name"] == "Alice Smith"
    assert info["users"][0]["username"] == "alice_new"
    assert info["users"][0]["aliases"] == ["Sasuke"]


def test_in_memory_add_alias_returns_false_for_unknown_user(chat_metadata_memory):
    cm = chat_metadata_memory.InMemoryChatMetadata()
    assert cm.add_alias(1, 999, "Ghost") is False


def test_in_memory_add_alias_dedupes(chat_metadata_memory):
    cm = chat_metadata_memory.InMemoryChatMetadata()
    cm.update_user(1, 100, "Alice", "alice123")
    assert cm.add_alias(1, 100, "Sasuke") is True
    assert cm.add_alias(1, 100, "Sasuke") is True
    info = cm.get_chat_info(1)
    assert info["users"][0]["aliases"] == ["Sasuke"]


def test_in_memory_remove_alias(chat_metadata_memory):
    cm = chat_metadata_memory.InMemoryChatMetadata()
    cm.update_user(1, 100, "Alice", "alice123")
    cm.add_alias(1, 100, "Sasuke")
    cm.add_alias(1, 100, "Boss")
    assert cm.remove_alias(1, 100, "Sasuke") is True
    info = cm.get_chat_info(1)
    assert info["users"][0]["aliases"] == ["Boss"]


def test_in_memory_remove_alias_missing(chat_metadata_memory):
    cm = chat_metadata_memory.InMemoryChatMetadata()
    cm.update_user(1, 100, "Alice", "alice123")
    assert cm.remove_alias(1, 100, "Nope") is False
    assert cm.remove_alias(1, 999, "Nope") is False


def test_in_memory_clear_aliases_keeps_roster(chat_metadata_memory):
    cm = chat_metadata_memory.InMemoryChatMetadata()
    cm.update_user(1, 100, "Alice", "alice123")
    cm.update_user(1, 200, "Bob", "bob")
    cm.add_alias(1, 100, "Sasuke")
    cm.add_alias(1, 200, "Builder")
    cm.clear_aliases(1)
    info = cm.get_chat_info(1)
    assert len(info["users"]) == 2
    assert all(u["aliases"] == [] for u in info["users"])


def test_in_memory_find_user_id_by_username(chat_metadata_memory):
    cm = chat_metadata_memory.InMemoryChatMetadata()
    cm.update_user(1, 100, "Alice", "alice123")
    cm.update_user(1, 200, "Bob", "bob")
    assert cm.find_user_id_by_username(1, "@alice123") == 100
    assert cm.find_user_id_by_username(1, "alice123") == 100
    assert cm.find_user_id_by_username(1, "@ALICE123") == 100
    assert cm.find_user_id_by_username(1, "@nope") is None


def test_in_memory_find_user_id_skips_users_without_username(chat_metadata_memory):
    cm = chat_metadata_memory.InMemoryChatMetadata()
    cm.update_user(1, 100, "Alice", None)
    assert cm.find_user_id_by_username(1, "@anything") is None


def test_in_memory_set_chat_name(chat_metadata_memory):
    cm = chat_metadata_memory.InMemoryChatMetadata()
    cm.set_chat_name(1, "General Hangout")
    assert cm.get_chat_info(1)["chat_name"] == "General Hangout"


def test_in_memory_set_chat_name_ignores_empty(chat_metadata_memory):
    cm = chat_metadata_memory.InMemoryChatMetadata()
    cm.set_chat_name(1, "Real")
    cm.set_chat_name(1, "")
    assert cm.get_chat_info(1)["chat_name"] == "Real"


def test_in_memory_per_chat_isolation(chat_metadata_memory):
    cm = chat_metadata_memory.InMemoryChatMetadata()
    cm.update_user(1, 100, "Alice", "alice123")
    cm.update_user(2, 100, "Alice", "alice123")
    cm.add_alias(1, 100, "Sasuke")
    assert cm.get_chat_info(1)["users"][0]["aliases"] == ["Sasuke"]
    assert cm.get_chat_info(2)["users"][0]["aliases"] == []


def test_in_memory_empty_chat_info(chat_metadata_memory):
    cm = chat_metadata_memory.InMemoryChatMetadata()
    info = cm.get_chat_info(999)
    assert info == {"chat_name": "", "users": []}


def test_in_memory_factory_default(chat_metadata_memory):
    inst = chat_metadata_memory.create_chat_metadata()
    assert isinstance(inst, chat_metadata_memory.InMemoryChatMetadata)


# ---------- RedisChatMetadata (via fakeredis) ----------

def test_redis_update_user_and_get(chat_metadata_redis):
    cm = chat_metadata_redis.RedisChatMetadata()
    cm.update_user(1, 100, "Alice", "alice123")
    info = cm.get_chat_info(1)
    assert info["users"] == [
        {"user_id": 100, "display_name": "Alice", "username": "alice123", "aliases": []}
    ]


def test_redis_update_user_preserves_aliases(chat_metadata_redis):
    cm = chat_metadata_redis.RedisChatMetadata()
    cm.update_user(1, 100, "Alice", "alice123")
    cm.add_alias(1, 100, "Sasuke")
    cm.update_user(1, 100, "Alice Smith", "alice_new")
    info = cm.get_chat_info(1)
    assert info["users"][0]["display_name"] == "Alice Smith"
    assert info["users"][0]["aliases"] == ["Sasuke"]


def test_redis_alias_lifecycle(chat_metadata_redis):
    cm = chat_metadata_redis.RedisChatMetadata()
    cm.update_user(1, 100, "Alice", "alice123")
    assert cm.add_alias(1, 100, "Sasuke") is True
    assert cm.add_alias(1, 999, "Ghost") is False
    assert cm.remove_alias(1, 100, "Sasuke") is True
    assert cm.remove_alias(1, 100, "Sasuke") is False


def test_redis_clear_aliases_keeps_roster(chat_metadata_redis):
    cm = chat_metadata_redis.RedisChatMetadata()
    cm.update_user(1, 100, "Alice", "alice123")
    cm.update_user(1, 200, "Bob", "bob")
    cm.add_alias(1, 100, "Sasuke")
    cm.clear_aliases(1)
    info = cm.get_chat_info(1)
    assert len(info["users"]) == 2
    assert all(u["aliases"] == [] for u in info["users"])


def test_redis_find_user_id_by_username(chat_metadata_redis):
    cm = chat_metadata_redis.RedisChatMetadata()
    cm.update_user(1, 100, "Alice", "alice123")
    assert cm.find_user_id_by_username(1, "@alice123") == 100
    assert cm.find_user_id_by_username(1, "@nope") is None


def test_redis_set_chat_name(chat_metadata_redis):
    cm = chat_metadata_redis.RedisChatMetadata()
    cm.set_chat_name(1, "General Hangout")
    assert cm.get_chat_info(1)["chat_name"] == "General Hangout"


def test_redis_per_chat_isolation(chat_metadata_redis):
    cm = chat_metadata_redis.RedisChatMetadata()
    cm.update_user(1, 100, "Alice", "alice123")
    cm.update_user(2, 100, "Alice", "alice123")
    cm.add_alias(1, 100, "Sasuke")
    aliases_1 = cm.get_chat_info(1)["users"][0]["aliases"]
    aliases_2 = cm.get_chat_info(2)["users"][0]["aliases"]
    assert aliases_1 == ["Sasuke"]
    assert aliases_2 == []


def test_redis_factory_returns_redis(chat_metadata_redis, monkeypatch):
    inst = chat_metadata_redis.create_chat_metadata()
    assert isinstance(inst, chat_metadata_redis.RedisChatMetadata)
