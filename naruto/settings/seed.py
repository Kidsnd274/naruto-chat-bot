"""One-time seed of the database from the old configuration.

On first start the database takes its settings from ``config.json``, the
legacy ``.env`` variables and ``system_prompt.md``, and the whitelisted groups
become enabled chats. Afterwards these files are ignored; the web admin shows
a notice when they no longer match the database.
"""

from dataclasses import dataclass, field
import json
import logging
import math
import os
from pathlib import Path
from typing import Any, Mapping

from naruto.db.chats import ChatRepository
from naruto.db.database import Database, now_ts
from naruto.settings.registry import SettingError
from naruto.settings.service import SettingsService

logger = logging.getLogger(__name__)

SEEDED_META_KEY = "seeded_at"
SEED_ACTOR = "seed"

_MODEL_PARAMS = ("temperature", "top_p", "top_k", "min_p", "repeat_penalty",
                 "chat_template_kwargs")
_MIB = 1024 * 1024


@dataclass
class SeedData:
    settings: dict[str, Any] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)  # key -> where it came from
    enabled_chats: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class SeedDifference:
    key: str
    label: str
    source: str
    file_value: str
    db_value: str


def _read_json(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read %s (%s); ignoring it.", path, type(exc).__name__)
        return {}
    return data if isinstance(data, dict) else {}


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def collect_seed(
    env: Mapping[str, str] | None = None,
    *,
    config_path: str | None = None,
    system_prompt_path: str | None = None,
) -> SeedData:
    """Gather values from the old configuration sources. Environment variables
    override config.json, as they did before."""
    env = os.environ if env is None else env
    config_path = config_path or env.get("CONFIG_PATH") or "config.json"
    system_prompt_path = system_prompt_path or env.get("SYSTEM_PROMPT_PATH") or "system_prompt.md"
    raw = _read_json(Path(config_path))
    seed = SeedData()

    def put(key: str, value: Any, source: str) -> None:
        if value is not None:
            seed.settings[key] = value
            seed.sources[key] = source

    for name in _MODEL_PARAMS:
        put(f"model.{name}", (raw.get("model_params") or {}).get(name), "config.json")

    put("model.endpoint_url", env.get("OPENAI_BASE_URL") or None, ".env OPENAI_BASE_URL")
    put("model.name", env.get("OPENAI_MODEL") or None, ".env OPENAI_MODEL")

    budget, source = _int_or_none(env.get("MAX_MODEL_TOKENS")), ".env MAX_MODEL_TOKENS"
    if budget is None:
        budget, source = _int_or_none(raw.get("max_model_tokens")), "config.json"
    put("context.input_token_budget", budget, source)

    media_enabled = env.get("MEDIA_ENABLED")
    if media_enabled:
        put("media.enabled", media_enabled.strip().lower() == "true", ".env MEDIA_ENABLED")

    max_bytes, source = _int_or_none(env.get("MAX_MEDIA_BYTES")), ".env MAX_MEDIA_BYTES"
    if max_bytes is None:
        max_bytes, source = _int_or_none(raw.get("max_media_bytes")), "config.json"
    if max_bytes is not None and max_bytes > 0:
        put("media.max_size_mb", max(1, min(20, math.ceil(max_bytes / _MIB))), source)

    image_tokens, source = _int_or_none(env.get("ESTIMATED_IMAGE_TOKENS")), ".env ESTIMATED_IMAGE_TOKENS"
    if image_tokens is None:
        image_tokens, source = _int_or_none(raw.get("estimated_image_tokens")), "config.json"
    put("media.estimated_image_tokens", image_tokens, source)

    try:
        persona = Path(system_prompt_path).read_text(encoding="utf-8").strip()
    except (FileNotFoundError, IsADirectoryError):
        persona = ""
    except OSError as exc:
        logger.warning("Could not read %s (%s).", system_prompt_path, type(exc).__name__)
        persona = ""
    if persona:
        put("persona.prompt", persona, Path(system_prompt_path).name)

    for chat_id in raw.get("whitelisted_groups") or []:
        parsed = _int_or_none(chat_id)
        if parsed is not None and parsed < 0:
            seed.enabled_chats.append(parsed)
    if raw.get("whitelisted_ids"):
        seed.notes.append(
            "config.json whitelisted_ids is no longer used: the bot is group-only "
            "and only the owner (OWNER_USER_ID) can message it privately."
        )
    if raw.get("max_chat_history") is not None:
        seed.notes.append("config.json max_chat_history is no longer used: history is kept in SQLite.")
    return seed


def apply_seed_if_needed(
    db: Database,
    settings: SettingsService,
    chats: ChatRepository,
    seed: SeedData,
) -> bool:
    """Seed an empty database once. Returns True if it seeded now."""
    if db.get_meta(SEEDED_META_KEY):
        return False
    for key, value in seed.settings.items():
        try:
            settings.set(key, value, actor=SEED_ACTOR)
        except SettingError as exc:
            logger.warning("Ignoring seed value for %s from %s: %s",
                           key, seed.sources.get(key), exc)
    for chat_id in seed.enabled_chats:
        chats.seed_enabled(chat_id)
    for note in seed.notes:
        logger.info(note)
    db.set_meta(SEEDED_META_KEY, str(now_ts()))
    logger.info(
        "Seeded the database: %s settings, %s enabled chats.",
        len(seed.settings), len(seed.enabled_chats),
    )
    return True


def seed_differences(
    settings: SettingsService,
    chats: ChatRepository,
    seed: SeedData,
) -> list[SeedDifference]:
    """Where the old configuration files disagree with the database."""
    differences = []
    for key, raw_value in seed.settings.items():
        setting = settings.registry.get(key)
        if setting is None:
            continue
        try:
            file_value = setting.validate(raw_value)
        except SettingError:
            continue
        db_value = settings.get(key)
        if file_value != db_value:
            differences.append(SeedDifference(
                key=key,
                label=setting.label,
                source=seed.sources.get(key, ""),
                file_value=setting.display(file_value),
                db_value=setting.display(db_value),
            ))
    for chat_id in seed.enabled_chats:
        chat = chats.get(chat_id)
        if chat is None or not chat.enabled:
            differences.append(SeedDifference(
                key=f"chat:{chat_id}",
                label=f"Group {chat_id}",
                source="config.json whitelisted_groups",
                file_value="enabled",
                db_value=chat.status if chat else "unknown",
            ))
    return differences
