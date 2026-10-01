"""Configurations under test: which settings a run may change, the
baseline snapshot, candidates' effective settings and their differences.

A run's baseline is a copy of every global setting value when it started.
A candidate is a set of changes on top of its parent (the baseline or
another candidate); its effective configuration is the baseline plus the
changes along that chain. The model (endpoint and name) is a condition of
the run, never a candidate change.
"""

import difflib
from fnmatch import fnmatchcase
import hashlib
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from naruto.settings.registry import REGISTRY, SettingError

# Always tunable: the prompts and how the model samples.
TUNABLE = (
    "persona.prompt",
    "prompt.rules",
    "skills.*.instructions",
    "skills.*.reasoning",
    "model.temperature",
    "model.top_p",
    "model.top_k",
    "model.min_p",
    "model.repeat_penalty",
    "model.chat_template_kwargs",
    "model.reasoning_effort",
    "model.max_output_tokens",
)
# Tunable when a run's scope names them (patterns allowed, e.g. "context.*").
EXTENDED = (
    "context.recent_window",
    "context.window_step",
    "context.input_token_budget",
    "context.max_message_chars",
    "memory.prompt_notes",
    "agent.max_model_requests",
    "agent.max_tool_calls",
    "agent.deadline_seconds",
    "agent.tool_result_chars",
    "agent.search_results",
    "history.lookup_results",
    "media.description_prompt",
    "media.description_max_tokens",
)
MODEL_KEYS = ("model.endpoint_url", "model.name")
# Model settings that other model requests use too: activating a change to
# one also changes digest updates, history summaries, import memory and
# image descriptions, which the lab doesn't evaluate.
SHARED_MODEL_KEYS = ("model.temperature", "model.top_p", "model.top_k", "model.min_p",
                     "model.repeat_penalty", "model.chat_template_kwargs",
                     "model.reasoning_effort", "model.max_output_tokens")
BACKGROUND_TASKS = ("digest updates", "history summaries", "import memory",
                    "image descriptions")
PACKAGE_DIR = Path(__file__).resolve().parents[1]


def _matches(key: str, patterns) -> bool:
    return any(fnmatchcase(key, pattern) for pattern in patterns)


def tunable_keys(scope_keys: list[str] | None = None) -> list[str]:
    """The settings a run may change. No scope: everything in TUNABLE.
    Otherwise exactly what the scope's patterns name (from TUNABLE and
    EXTENDED), e.g. ["skills.banter.*", "context.recent_window"]."""
    if not scope_keys:
        return [key for key in REGISTRY if _matches(key, TUNABLE)]
    return [key for key in REGISTRY
            if _matches(key, scope_keys) and _matches(key, TUNABLE + EXTENDED)]


def check_scope(scope_keys: list[str]) -> None:
    """Every pattern in a run's scope must name tunable settings."""
    for pattern in scope_keys:
        matched = [key for key in REGISTRY if fnmatchcase(key, pattern)]
        if not matched:
            raise SettingError(f"{pattern!r} doesn't name any setting.")
        outside = [key for key in matched if not _matches(key, TUNABLE + EXTENDED)]
        if outside:
            raise SettingError(f"{', '.join(outside)} can't be tuned in the lab. Tunable: "
                               f"{', '.join(TUNABLE + EXTENDED)}.")


def snapshot(settings) -> dict[str, Any]:
    """Every global setting value (secrets are never settings)."""
    return {key: settings.get(key) for key in settings.registry}


def validate_changes(changes: dict[str, Any], allowed: list[str]) -> dict[str, Any]:
    """Validated with the same rules as the Settings page."""
    if not isinstance(changes, dict) or not changes:
        raise SettingError("A candidate needs at least one change, as {\"key\": value}.")
    result = {}
    for key, value in changes.items():
        if key in MODEL_KEYS:
            raise SettingError(f"{key} is part of the run's conditions, not a candidate "
                               "change: start a run on that model instead.")
        setting = REGISTRY.get(key)
        if setting is None:
            raise SettingError(f"Unknown setting {key}.")
        if key not in allowed:
            raise SettingError(f"{key} is outside this run's scope. In scope: "
                               f"{', '.join(allowed)}.")
        try:
            result[key] = setting.validate(value)
        except SettingError as exc:
            raise SettingError(f"{key}: {exc}") from None
    return result


def effective(baseline: dict[str, Any], chain: list[dict[str, Any]], *,
              endpoint: str, model: str) -> dict[str, Any]:
    """The baseline, then each candidate's changes from the root down, then
    the run's model."""
    values = dict(baseline)
    for changes in chain:
        values.update(changes)
    values["model.endpoint_url"] = endpoint
    values["model.name"] = model
    return values


def settings_hash(values: dict[str, Any]) -> str:
    data = json.dumps(values, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(data.encode()).hexdigest()[:16]


def differences(old: dict[str, Any], new: dict[str, Any],
                keys: list[str] | None = None) -> dict[str, dict]:
    """key -> {"from": ..., "to": ...} for values that differ."""
    keys = keys if keys is not None else sorted(set(old) | set(new))
    return {key: {"from": old.get(key), "to": new.get(key)}
            for key in keys if old.get(key) != new.get(key)}


def text_diff(old: Any, new: Any, name: str) -> str:
    """A unified diff for a prompt, or one line for other values."""
    if isinstance(old, str) and isinstance(new, str) and ("\n" in old or "\n" in new
                                                          or len(old) > 80):
        lines = difflib.unified_diff(old.splitlines(keepends=True),
                                     new.splitlines(keepends=True),
                                     fromfile=f"{name} (before)", tofile=f"{name} (after)")
        return "".join(line if line.endswith("\n") else line + "\n" for line in lines)
    return f"{name}: {json.dumps(old, ensure_ascii=False)} → {json.dumps(new, ensure_ascii=False)}\n"


def redact_endpoint(url: str) -> str:
    """Drop any user:password from an endpoint URL."""
    parts = urlsplit(url or "")
    if parts.username or parts.password:
        host = parts.hostname or ""
        if parts.port:
            host += f":{parts.port}"
        return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
    return url


_FINGERPRINT: str | None = None


def code_fingerprint() -> str:
    """A hash of the bot's code and default prompts, so a run notices when
    the bot was restarted on different code."""
    global _FINGERPRINT
    if _FINGERPRINT is None:
        digest = hashlib.sha256()
        for path in sorted(PACKAGE_DIR.rglob("*")):
            if path.suffix in (".py", ".md") and "__pycache__" not in path.parts:
                digest.update(str(path.relative_to(PACKAGE_DIR)).encode())
                digest.update(path.read_bytes())
        _FINGERPRINT = digest.hexdigest()[:12]
    return _FINGERPRINT


def slugify(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (slug[:limit].rstrip("-") or "run")


# ----------------------------------------------------- configuration files

def file_name(key: str) -> str | None:
    """The file a prompt setting is exported to, named like its default in
    naruto/prompts/ (persona.md, rules.md, banter.md...). Other tunable
    values go into settings.json."""
    if key == "persona.prompt":
        return "persona.md"
    if key == "prompt.rules":
        return "rules.md"
    match = re.fullmatch(r"skills\.([a-z_]+)\.instructions", key)
    if match:
        return f"{match.group(1)}.md"
    if key == "media.description_prompt":
        return "describe_image.md"
    return None


def key_for_file(name: str) -> str | None:
    for key in REGISTRY:
        if file_name(key) == name:
            return key
    return None


SETTINGS_FILE = "settings.json"


def to_files(values: dict[str, Any], keys: list[str]) -> dict[str, str]:
    """A configuration as files: one per prompt, the rest in settings.json."""
    files = {}
    other = {}
    for key in keys:
        name = file_name(key)
        if name is not None:
            text = values.get(key) or ""
            files[name] = text if text.endswith("\n") else text + "\n"
        else:
            other[key] = values.get(key)
    files[SETTINGS_FILE] = json.dumps(other, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    return files


def from_files(files: dict[str, str], parent: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    """The changes a folder of configuration files makes to ``parent``:
    only files (and settings.json values) that differ."""
    changes = {}
    for name, text in files.items():
        if name == SETTINGS_FILE:
            try:
                values = json.loads(text)
            except json.JSONDecodeError as exc:
                raise SettingError(f"settings.json isn't valid JSON: {exc.msg}.") from None
            if not isinstance(values, dict):
                raise SettingError("settings.json must be a JSON object.")
            for key, value in values.items():
                if key not in keys:
                    raise SettingError(f"settings.json: {key} is not tunable in this run.")
                if value != parent.get(key):
                    changes[key] = value
            continue
        key = key_for_file(name)
        if key is None:
            raise SettingError(f"{name} isn't a configuration file (expected "
                               f"{', '.join(sorted(n for k in keys if (n := file_name(k))))}, "
                               "settings.json).")
        if key not in keys:
            raise SettingError(f"{name} ({key}) is not tunable in this run.")
        text = text.strip("\n")
        if text != (parent.get(key) or "").strip("\n"):
            changes[key] = text
    return changes
