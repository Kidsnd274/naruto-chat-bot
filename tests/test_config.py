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
    assert cfg.max_model_tokens is None


def test_max_model_tokens_loads_from_json(fresh_config, tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"max_model_tokens": 32768}))
    monkeypatch.setenv("CONFIG_PATH", str(cfg_path))

    fresh_config.config.setup()

    assert fresh_config.config.max_model_tokens == 32768


def test_max_model_tokens_env_overrides_json(fresh_config, tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"max_model_tokens": 32768}))
    monkeypatch.setenv("CONFIG_PATH", str(cfg_path))
    monkeypatch.setenv("MAX_MODEL_TOKENS", "8192")

    fresh_config.config.setup()

    assert fresh_config.config.max_model_tokens == 8192


def test_invalid_max_model_tokens_disables_limit(fresh_config, tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"max_model_tokens": 0}))
    monkeypatch.setenv("CONFIG_PATH", str(cfg_path))

    fresh_config.config.setup()

    assert fresh_config.config.max_model_tokens is None


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


# ---------- system_prompt ----------

def test_system_prompt_defaults_to_empty_when_file_missing(initialized_config):
    assert initialized_config.config.system_prompt == ""


def test_system_prompt_loads_from_file(fresh_config, tmp_path, monkeypatch):
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("You are Naruto. Dattebayo!\n\n", encoding="utf-8")
    monkeypatch.setenv("SYSTEM_PROMPT_PATH", str(prompt_path))

    fresh_config.config.setup()

    # Trailing whitespace stripped.
    assert fresh_config.config.system_prompt == "You are Naruto. Dattebayo!"


def test_system_prompt_empty_file_treated_as_unset(fresh_config, tmp_path, monkeypatch):
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("   \n  \n", encoding="utf-8")
    monkeypatch.setenv("SYSTEM_PROMPT_PATH", str(prompt_path))

    fresh_config.config.setup()

    assert fresh_config.config.system_prompt == ""


# ---------- model_params ----------

def test_model_params_defaults_to_all_none(initialized_config):
    mp = initialized_config.config.model_params
    assert mp.temperature is None
    assert mp.top_p is None
    assert mp.top_k is None
    assert mp.min_p is None
    assert mp.repeat_penalty is None
    assert mp.chat_template_kwargs is None


def test_model_params_loads_partial(fresh_config, tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "model_params": {"temperature": 0.7}
    }))
    monkeypatch.setenv("CONFIG_PATH", str(cfg_path))

    fresh_config.config.setup()

    mp = fresh_config.config.model_params
    assert mp.temperature == 0.7
    assert mp.top_p is None
    assert mp.top_k is None
    assert mp.min_p is None
    assert mp.repeat_penalty is None
    assert mp.chat_template_kwargs is None


def test_model_params_loads_all_fields_including_nested(fresh_config, tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "model_params": {
            "temperature": 0.7,
            "top_p": 0.9,
            "top_k": 40,
            "min_p": 0.05,
            "repeat_penalty": 1.1,
            "chat_template_kwargs": {"enable_thinking": False},
        }
    }))
    monkeypatch.setenv("CONFIG_PATH", str(cfg_path))

    fresh_config.config.setup()

    mp = fresh_config.config.model_params
    assert mp.temperature == 0.7
    assert mp.top_p == 0.9
    assert mp.top_k == 40
    assert mp.min_p == 0.05
    assert mp.repeat_penalty == 1.1
    assert mp.chat_template_kwargs == {"enable_thinking": False}
