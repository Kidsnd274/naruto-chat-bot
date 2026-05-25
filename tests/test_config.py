"""Tests for AppConfig loading from env + JSON."""
import json


def test_defaults_with_no_config_file(initialized_config):
    cfg = initialized_config.config
    assert cfg.whitelist.enabled is True
    assert cfg.whitelist.groups == []
    assert cfg.whitelist.ids == []
    assert cfg.chat_history.enabled is False
    assert cfg.chat_history.max_history == 1
    assert cfg.chat_history.storage_type.value == "memory"


def test_whitelist_loads_from_json(fresh_config, tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "whitelisted_groups": [-100123, -100456],
        "whitelisted_ids": [42, 43],
    }))
    monkeypatch.setenv("CONFIG_PATH", str(cfg_path))
    monkeypatch.setenv("WHITELIST_ENABLED", "true")

    fresh_config.config.setup()

    assert fresh_config.config.whitelist.groups == [-100123, -100456]
    assert fresh_config.config.whitelist.ids == [42, 43]


def test_whitelist_disabled_ignores_json(fresh_config, tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "whitelisted_groups": [1],
        "whitelisted_ids": [2],
    }))
    monkeypatch.setenv("CONFIG_PATH", str(cfg_path))
    monkeypatch.setenv("WHITELIST_ENABLED", "false")

    fresh_config.config.setup()

    assert fresh_config.config.whitelist.enabled is False
    assert fresh_config.config.whitelist.groups == []
    assert fresh_config.config.whitelist.ids == []


def test_chat_history_enabled_clamps_max_history(fresh_config, tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"max_chat_history": 1}))
    monkeypatch.setenv("CONFIG_PATH", str(cfg_path))
    monkeypatch.setenv("CHAT_HISTORY_ENABLED", "true")

    fresh_config.config.setup()

    assert fresh_config.config.chat_history.enabled is True
    assert fresh_config.config.chat_history.max_history == 3  # clamped from 1


def test_chat_history_disabled_forces_memory_and_size_one(fresh_config, tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_HISTORY_ENABLED", "false")
    monkeypatch.setenv("CHAT_HISTORY_TYPE", "redis")  # should be ignored

    fresh_config.config.setup()

    ch = fresh_config.config.chat_history
    assert ch.enabled is False
    assert ch.max_history == 1
    assert ch.storage_type.value == "memory"


def test_chat_history_redis_type(fresh_config, monkeypatch):
    monkeypatch.setenv("CHAT_HISTORY_ENABLED", "true")
    monkeypatch.setenv("CHAT_HISTORY_TYPE", "redis")

    fresh_config.config.setup()

    assert fresh_config.config.chat_history.storage_type.value == "redis"


def test_chat_history_unknown_type_falls_back_to_memory(fresh_config, monkeypatch):
    monkeypatch.setenv("CHAT_HISTORY_ENABLED", "true")
    monkeypatch.setenv("CHAT_HISTORY_TYPE", "postgres")  # nonsense

    fresh_config.config.setup()

    assert fresh_config.config.chat_history.storage_type.value == "memory"


def test_redis_port_and_db_are_ints(fresh_config, monkeypatch):
    monkeypatch.setenv("REDIS_HOST", "example.com")
    monkeypatch.setenv("REDIS_PORT", "6380")
    monkeypatch.setenv("REDIS_DB", "2")

    fresh_config.config.setup()

    rc = fresh_config.config.redis_config
    assert rc.host == "example.com"
    assert rc.port == 6380
    assert isinstance(rc.port, int)
    assert rc.db == 2
    assert isinstance(rc.db, int)


def test_setup_is_idempotent(fresh_config):
    fresh_config.config.setup()
    snapshot_id = id(fresh_config.config.whitelist)
    fresh_config.config.setup()  # second call should no-op
    assert id(fresh_config.config.whitelist) == snapshot_id


def test_singleton_returns_same_instance(fresh_config):
    a = fresh_config.AppConfig()
    b = fresh_config.AppConfig()
    assert a is b
