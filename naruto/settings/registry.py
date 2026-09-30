"""Every setting stored in the database: type, range, default and help text.

This one list drives validation, defaults and the web admin's Settings page.
Values are stored as JSON; a setting with no stored row uses its default.
"""

from dataclasses import dataclass, field
import json
import math
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from naruto.prompts import load_prompt


class SettingError(ValueError):
    """A value was rejected; the message is shown to the owner."""


@dataclass(frozen=True)
class Section:
    id: str
    title: str
    description: str = ""


@dataclass(frozen=True)
class Setting:
    key: str
    section: str
    label: str
    description: str
    type: str  # int | float | bool | str | text | json | choice | timezone
    default: Any
    nullable: bool = False  # None means "not sent / server default"
    min: float | None = None
    max: float | None = None
    choices: tuple[str, ...] = field(default_factory=tuple)
    restart_required: bool = False

    # ------------------------------------------------------------ validate

    def validate(self, value: Any) -> Any:
        """Check a Python value (from JSON, a seed or code). Returns the
        normalized value or raises SettingError."""
        if value is None:
            if self.nullable:
                return None
            raise SettingError("A value is required.")
        match self.type:
            case "int":
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise SettingError("Must be a whole number.")
                if isinstance(value, float) and not value.is_integer():
                    raise SettingError("Must be a whole number.")
                value = int(value)
                self._check_range(value)
            case "float":
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise SettingError("Must be a number.")
                value = float(value)
                if not math.isfinite(value):
                    raise SettingError("Must be a finite number.")
                self._check_range(value)
            case "bool":
                if not isinstance(value, bool):
                    raise SettingError("Must be true or false.")
            case "str" | "text":
                if not isinstance(value, str):
                    raise SettingError("Must be text.")
                value = value.strip() if self.type == "str" else value.strip("\n")
                if self.min is not None and len(value) < self.min:
                    raise SettingError(f"Must be at least {int(self.min)} characters.")
                if self.max is not None and len(value) > self.max:
                    raise SettingError(f"Must be at most {int(self.max)} characters.")
            case "choice":
                if value not in self.choices:
                    raise SettingError(f"Must be one of: {', '.join(self.choices)}.")
            case "json":
                if not isinstance(value, dict):
                    raise SettingError("Must be a JSON object, e.g. {\"key\": \"value\"}.")
            case "timezone":
                if not isinstance(value, str):
                    raise SettingError("Must be a time zone name.")
                value = value.strip()
                if value:
                    try:
                        ZoneInfo(value)
                    except (ZoneInfoNotFoundError, ValueError):
                        raise SettingError(
                            "Unknown time zone. Use a name like Asia/Singapore."
                        ) from None
            case _:
                raise SettingError(f"Unsupported setting type {self.type!r}.")
        return value

    def _check_range(self, value: float) -> None:
        if self.min is not None and value < self.min:
            raise SettingError(f"Must be at least {_fmt(self.min)}.")
        if self.max is not None and value > self.max:
            raise SettingError(f"Must be at most {_fmt(self.max)}.")

    # --------------------------------------------------------------- forms

    def parse_form(self, raw: str | None) -> Any:
        """Parse a web form value. For bool settings the form sends the
        checkbox value, or nothing when unchecked."""
        if self.type == "bool":
            return self.validate(raw in ("on", "true", "1", "yes"))
        text = (raw or "").strip() if self.type != "text" else (raw or "")
        if self.type not in ("str", "text", "timezone") and text == "":
            return self.validate(None)
        match self.type:
            case "int":
                try:
                    number = int(text)
                except ValueError:
                    raise SettingError("Must be a whole number.") from None
                return self.validate(number)
            case "float":
                try:
                    number = float(text)
                except ValueError:
                    raise SettingError("Must be a number.") from None
                return self.validate(number)
            case "json":
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise SettingError(f"Invalid JSON: {exc.msg}.") from None
                return self.validate(parsed)
            case _:
                if self.nullable and self.type in ("str", "text") and not text.strip():
                    return self.validate(None)
                return self.validate(text.replace("\r\n", "\n"))

    def form_value(self, value: Any) -> str:
        """Render a value for an input field."""
        if value is None:
            return ""
        if self.type == "json":
            return json.dumps(value, indent=2, ensure_ascii=False)
        if self.type == "float":
            return _fmt(value)
        return str(value)

    def display(self, value: Any) -> str:
        """Short one-line rendering for history tables and notices."""
        if value is None:
            return "(server default)" if self.nullable else "(empty)"
        if self.type == "bool":
            return "on" if value else "off"
        if self.type == "json":
            return json.dumps(value, ensure_ascii=False)
        if self.type == "float":
            return _fmt(value)
        text = str(value)
        if self.type == "text" and len(text) > 80:
            return text[:77].replace("\n", " ") + "…"
        return text or "(empty)"


def _fmt(number: float) -> str:
    return f"{number:g}"


SECTIONS: tuple[Section, ...] = (
    Section("general", "General"),
    Section("model", "Model", "The OpenAI-compatible inference server (Gufo)."),
    Section("persona", "Persona and skills",
            "Prompt texts. The persona comes first in every request, then the "
            "operating rules, then the skill's instructions."),
    Section("context", "Context", "What goes into each request."),
    Section("import", "Import", "Telegram Desktop history import."),
    Section("media", "Media", "Photos, stickers and other visual media."),
    Section("retention", "Retention",
            "How long raw data is kept. 0 keeps it forever."),
    Section("behaviour", "Behaviour"),
)

SETTINGS: tuple[Setting, ...] = (
    # ------------------------------------------------------------- general
    Setting(
        "general.timezone", "general", "Time zone",
        "Used for times shown to the model and in the web admin. Empty uses "
        "the server's time zone (UTC in Docker unless TZ is set).",
        "timezone", "",
    ),
    Setting(
        "general.log_level", "general", "Log level",
        "Minimum level for application logs (console and Logs page).",
        "choice", "INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    ),
    # --------------------------------------------------------------- model
    Setting(
        "model.endpoint_url", "model", "Endpoint URL",
        "Base URL of the OpenAI-compatible API, including /v1.",
        "str", "http://localhost:8080/v1", min=1,
    ),
    Setting(
        "model.name", "model", "Model name",
        "Model ID to request. Empty uses the first model the server lists.",
        "str", "",
    ),
    Setting("model.temperature", "model", "temperature", "Sampling temperature.",
            "float", None, nullable=True, min=0, max=2),
    Setting("model.top_p", "model", "top_p", "Nucleus sampling.",
            "float", None, nullable=True, min=0, max=1),
    Setting("model.top_k", "model", "top_k", "Top-k sampling (sent in extra_body).",
            "int", None, nullable=True, min=0, max=1000),
    Setting("model.min_p", "model", "min_p", "Min-p sampling (sent in extra_body).",
            "float", None, nullable=True, min=0, max=1),
    Setting("model.repeat_penalty", "model", "repeat_penalty",
            "Repetition penalty (sent in extra_body).",
            "float", None, nullable=True, min=0.5, max=2),
    Setting(
        "model.chat_template_kwargs", "model", "chat_template_kwargs",
        "Extra chat-template arguments as a JSON object (sent in extra_body). "
        "Each skill's reasoning switch sets enable_thinking on top of this.",
        "json", None, nullable=True,
    ),
    Setting("model.max_output_tokens", "model", "Max output tokens",
            "Upper limit on generated tokens per request, including reasoning.",
            "int", 2048, nullable=True, min=16, max=65536),
    Setting("model.request_timeout_seconds", "model", "Request timeout (seconds)",
            "Give up on a model request after this long.",
            "int", 180, min=5, max=1800),
    # ------------------------------------------------------------- persona
    Setting("persona.prompt", "persona", "Persona prompt",
            "Who the bot is and how it talks. Used for every skill.",
            "text", load_prompt("persona"), max=20000),
    Setting("prompt.rules", "persona", "Operating rules",
            "How the chat is laid out in the request and the rules for "
            "answering. Shared by every skill.",
            "text", load_prompt("rules"), max=20000),
    Setting("skills.banter.instructions", "persona", "Banter: instructions",
            "Task and output format for normal chat replies.",
            "text", load_prompt("banter"), max=20000),
    Setting("skills.banter.reasoning", "persona", "Banter: reasoning",
            "Let the model think before answering chat replies (slower).",
            "bool", False),
    # ------------------------------------------------------------- context
    Setting("context.recent_window", "context", "Recent-window size",
            "Minimum number of earlier messages shown before the current request.",
            "int", 40, min=1, max=500),
    Setting("context.window_step", "context", "Recent-window step",
            "The window's start moves forward in steps of this many messages, "
            "so the prompt prefix stays the same and the server can reuse its "
            "cache. The window holds up to size + step - 1 messages.",
            "int", 20, min=1, max=500),
    Setting("context.input_token_budget", "context", "Input token budget",
            "Estimated input tokens per request. The oldest recent messages are "
            "dropped to fit.",
            "int", 12000, min=1000, max=1_000_000),
    Setting("context.max_message_chars", "context", "Max characters per message",
            "Longer messages are shortened in the recent window.",
            "int", 1500, min=50, max=100_000),
    # -------------------------------------------------------------- import
    Setting("import.max_upload_mb", "import", "Max upload size (MB)",
            "Largest result.json the Import page accepts.",
            "int", 200, min=1, max=4096),
    # --------------------------------------------------------------- media
    Setting("media.enabled", "media", "Media enabled",
            "Look at images when someone asks about one (the current message "
            "or the message it replies to).",
            "bool", True),
    Setting("media.max_size_mb", "media", "Max media size (MB)",
            "Media larger than this is not downloaded.",
            "int", 20, min=1, max=20),
    Setting("media.estimated_image_tokens", "media", "Estimated tokens per image",
            "Used for the input token budget.",
            "int", 2048, min=1, max=100_000),
    # ----------------------------------------------------------- retention
    Setting("retention.live_messages_days", "retention", "Live messages (days)",
            "How long live messages are kept.",
            "int", 30, min=0, max=36500),
    Setting("retention.imported_messages_days", "retention", "Imported messages (days)",
            "An import keeps only messages newer than this.",
            "int", 30, min=0, max=36500),
    Setting("retention.agent_runs_days", "retention", "Agent runs (days)",
            "Agent traces contain full prompts, so they are cleaned up daily.",
            "int", 30, min=0, max=36500),
    Setting("retention.logs_days", "retention", "Logs (days)",
            "Stored application logs are cleaned up daily.",
            "int", 30, min=0, max=36500),
    # ----------------------------------------------------------- behaviour
    Setting("behaviour.pending_leave_hours", "behaviour", "Leave pending groups after (hours)",
            "Leave a group that nobody approved after this many hours. 0 never leaves.",
            "int", 0, min=0, max=24 * 365),
)

REGISTRY: dict[str, Setting] = {setting.key: setting for setting in SETTINGS}

assert len(REGISTRY) == len(SETTINGS), "duplicate setting keys"
assert {s.section for s in SETTINGS} <= {s.id for s in SECTIONS}, "unknown section"
for _setting in SETTINGS:  # defaults must pass their own validation
    _setting.validate(_setting.default)
