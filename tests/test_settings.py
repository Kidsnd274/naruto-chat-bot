"""Settings registry validation, the settings service and the one-time seed."""

import json

import pytest

from naruto.db import open_database
from naruto.db.chats import ChatRepository
from naruto.settings.registry import REGISTRY, Setting, SettingError
from naruto.settings.seed import apply_seed_if_needed, collect_seed, seed_differences
from naruto.settings.service import SettingsService


@pytest.fixture
def db():
    database = open_database(":memory:")
    yield database
    database.close()


@pytest.fixture
def settings(db):
    return SettingsService(db)


# ---------------------------------------------------------------- registry

def test_every_default_is_valid():
    for setting in REGISTRY.values():
        assert setting.validate(setting.default) == setting.default


@pytest.mark.parametrize("key,raw,expected", [
    ("context.recent_window", "25", 25),
    ("model.temperature", "0.7", 0.7),
    ("model.temperature", "", None),  # nullable: server default
    ("model.top_k", "40", 40),
    ("media.enabled", "on", True),
    ("media.enabled", None, False),  # unchecked checkbox
    ("model.chat_template_kwargs", '{"enable_thinking": false}', {"enable_thinking": False}),
    ("general.timezone", "Asia/Singapore", "Asia/Singapore"),
    ("general.timezone", "", ""),
    ("general.log_level", "DEBUG", "DEBUG"),
    ("persona.prompt", "Line 1\r\nLine 2\n", "Line 1\nLine 2"),
])
def test_parse_form_accepts(key, raw, expected):
    assert REGISTRY[key].parse_form(raw) == expected


@pytest.mark.parametrize("key,raw,message", [
    ("context.recent_window", "0", "at least 1"),
    ("context.recent_window", "2.5", "whole number"),
    ("context.recent_window", "", "required"),
    ("model.temperature", "3", "at most 2"),
    ("model.temperature", "hot", "number"),
    ("model.temperature", "nan", "finite"),
    ("model.chat_template_kwargs", "[1, 2]", "JSON object"),
    ("model.chat_template_kwargs", "{bad", "Invalid JSON"),
    ("general.timezone", "Mars/Olympus", "Unknown time zone"),
    ("general.log_level", "LOUD", "one of"),
    ("model.endpoint_url", "   ", "at least 1"),
])
def test_parse_form_rejects(key, raw, message):
    with pytest.raises(SettingError, match=message):
        REGISTRY[key].parse_form(raw)


def test_validate_rejects_bool_for_int():
    with pytest.raises(SettingError):
        REGISTRY["context.recent_window"].validate(True)


def test_form_value_and_display():
    kwargs = REGISTRY["model.chat_template_kwargs"]
    assert json.loads(kwargs.form_value({"a": 1})) == {"a": 1}
    assert kwargs.display(None) == "(server default)"
    assert REGISTRY["media.enabled"].display(True) == "on"
    assert REGISTRY["model.temperature"].form_value(0.7) == "0.7"


# ----------------------------------------------------------------- service

def test_defaults_until_changed(settings):
    assert settings.get("context.recent_window") == 40
    assert settings.is_default("context.recent_window")
    with pytest.raises(KeyError):
        settings.get("nope")


def test_set_persists_across_reload_and_records_history(db, settings):
    settings.set("context.recent_window", 25, actor="owner")
    assert settings["context.recent_window"] == 25
    assert not settings.is_default("context.recent_window")

    reloaded = SettingsService(db)
    assert reloaded["context.recent_window"] == 25

    entry = settings.history("context.recent_window")[0]
    assert (entry.old_value, entry.new_value, entry.changed_by) == (40, 25, "owner")
    assert entry.old_is_default and not entry.new_is_default


def test_setting_the_default_removes_the_override(settings):
    settings.set("context.recent_window", 25, actor="owner")
    settings.set("context.recent_window", 40, actor="owner")
    assert settings.is_default("context.recent_window")


def test_unchanged_value_adds_no_history(settings):
    settings.set("context.recent_window", 25, actor="owner")
    settings.set("context.recent_window", 25, actor="owner")
    assert len(settings.history("context.recent_window")) == 1


def test_invalid_value_is_rejected_and_not_stored(settings):
    with pytest.raises(SettingError):
        settings.set_from_form("context.recent_window", "-5", actor="owner")
    assert settings.is_default("context.recent_window")
    assert settings.history() == []


def test_revert_and_reset(settings):
    settings.set("model.temperature", 0.5, actor="owner")
    settings.set("model.temperature", 0.9, actor="owner")
    assert settings.revert("model.temperature", actor="owner")
    assert settings["model.temperature"] == 0.5
    settings.reset("model.temperature", actor="owner")
    assert settings["model.temperature"] is None
    assert settings.is_default("model.temperature")
    assert settings.revert("model.temperature", actor="owner")
    assert settings["model.temperature"] == 0.5


def test_revert_to_default(settings):
    settings.set("model.temperature", 0.5, actor="owner")
    assert settings.revert("model.temperature", actor="owner")
    assert settings.is_default("model.temperature")
    assert settings.revert("general.timezone", actor="owner") is False


def test_listeners_see_new_values_and_failures_are_contained(settings):
    seen = []
    settings.on_change(lambda key, value: seen.append((key, value)))
    settings.on_change(lambda key, value: 1 / 0)
    settings.set("general.log_level", "DEBUG", actor="owner")
    assert seen == [("general.log_level", "DEBUG")]


def test_invalid_stored_value_falls_back_to_default(db):
    db.execute(
        "INSERT INTO settings (key, value, updated_at, updated_by) VALUES (?, ?, 0, 'x')",
        ("context.recent_window", json.dumps(-3)),
    )
    db.execute(
        "INSERT INTO settings (key, value, updated_at, updated_by) VALUES (?, ?, 0, 'x')",
        ("removed.setting", json.dumps(1)),
    )
    service = SettingsService(db)
    assert service["context.recent_window"] == 40


def test_custom_registry_is_supported(db):
    registry = {"x.y": Setting("x.y", "general", "X", "", "int", 1, min=0)}
    service = SettingsService(db, registry)
    service.set("x.y", 5, actor="t")
    assert service["x.y"] == 5


# -------------------------------------------------------------------- seed

@pytest.fixture
def old_config(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "whitelisted_groups": [-4001, -1002002],
        "whitelisted_ids": [111],
        "max_chat_history": 200,
        "max_model_tokens": 32768,
        "max_media_bytes": 10 * 1024 * 1024,
        "estimated_image_tokens": 1024,
        "model_params": {
            "temperature": 0.7,
            "top_k": 40,
            "chat_template_kwargs": {"enable_thinking": False},
        },
    }))
    prompt = tmp_path / "system_prompt.md"
    prompt.write_text("You are Naruto.\n")
    return {"CONFIG_PATH": str(config), "SYSTEM_PROMPT_PATH": str(prompt)}


def test_collect_seed_maps_old_configuration(old_config):
    env = {**old_config, "OPENAI_BASE_URL": "http://gufo:8080/v1",
           "OPENAI_MODEL": "qwen", "MEDIA_ENABLED": "false", "MAX_MODEL_TOKENS": "16000"}
    seed = collect_seed(env)
    assert seed.settings == {
        "model.temperature": 0.7,
        "model.top_k": 40,
        "model.chat_template_kwargs": {"enable_thinking": False},
        "model.endpoint_url": "http://gufo:8080/v1",
        "model.name": "qwen",
        "context.input_token_budget": 16000,  # env beats config.json
        "media.enabled": False,
        "media.max_size_mb": 10,
        "media.estimated_image_tokens": 1024,
        "persona.prompt": "You are Naruto.",
    }
    assert seed.sources["context.input_token_budget"] == ".env MAX_MODEL_TOKENS"
    assert seed.enabled_chats == [-4001, -1002002]
    assert len(seed.notes) == 2


def test_collect_seed_tolerates_missing_and_invalid_files(tmp_path):
    bad = tmp_path / "config.json"
    bad.write_text("{not json")
    seed = collect_seed({"CONFIG_PATH": str(bad),
                         "SYSTEM_PROMPT_PATH": str(tmp_path / "missing.md")})
    assert seed.settings == {} and seed.enabled_chats == []


def test_seed_applies_once(db, settings, old_config):
    chats = ChatRepository(db)
    seed = collect_seed(old_config)

    assert apply_seed_if_needed(db, settings, chats, seed)
    assert settings["model.temperature"] == 0.7
    assert settings["persona.prompt"] == "You are Naruto."
    assert chats.get(-4001).enabled and chats.get(-1002002).type == "supergroup"
    assert settings.history("model.temperature")[0].changed_by == "seed"

    settings.set("model.temperature", 0.2, actor="owner")
    assert apply_seed_if_needed(db, settings, chats, seed) is False
    assert settings["model.temperature"] == 0.2


def test_seed_skips_invalid_values(db, settings, tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"model_params": {"temperature": 9}}))
    seed = collect_seed({"CONFIG_PATH": str(config), "SYSTEM_PROMPT_PATH": "/none"})
    apply_seed_if_needed(db, settings, ChatRepository(db), seed)
    assert settings["model.temperature"] is None


def test_seed_differences_report_drift(db, settings, old_config):
    chats = ChatRepository(db)
    seed = collect_seed(old_config)
    apply_seed_if_needed(db, settings, chats, seed)
    assert seed_differences(settings, chats, seed) == []

    settings.set("model.temperature", 0.2, actor="owner")
    chats.set_status(-4001, "disabled")
    differences = {d.key: d for d in seed_differences(settings, chats, seed)}
    assert differences["model.temperature"].file_value == "0.7"
    assert differences["model.temperature"].db_value == "0.2"
    assert differences["chat:-4001"].db_value == "disabled"
