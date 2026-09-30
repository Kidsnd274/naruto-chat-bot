"""Tools the model can call: definitions, argument checks and execution.

A tool is a JSON-schema description (what the model sees) plus an async
handler. Handlers return text for the model; a ToolError becomes a short
error the model can react to (for example by fixing its arguments).
"""

from dataclasses import dataclass, field
import logging
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from naruto.llm import ToolCall

if TYPE_CHECKING:
    from naruto.agent.context import ContextBuilder
    from naruto.db.chats import Chat
    from naruto.db.messages import StoredMessage
    from naruto.services import BotIdentity, Services

logger = logging.getLogger(__name__)


class ToolError(Exception):
    """A problem to report back to the model (not a crash)."""


@dataclass
class RunState:
    """Counters and side effects of one agent run, shared with the tools."""
    run_id: int
    max_model_requests: int
    model_requests: int = 0
    tool_calls: int = 0
    steps: list[dict] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)  # what the run did in the chat
    switch_to_skill: str | None = None

    @property
    def model_requests_left(self) -> int:
        return self.max_model_requests - self.model_requests


@dataclass
class ToolContext:
    services: "Services"
    telegram: Any  # telegram.Bot (or a recording fake in the evaluation)
    chat: "Chat"
    trigger: "StoredMessage"
    bot: "BotIdentity"
    builder: "ContextBuilder"
    state: RunState
    skill: str = "banter"
    window_ids: list[int] = field(default_factory=list)  # the recent messages in the prompt
    # Stores a message the bot sent (Telegram never echoes it back).
    record_sent: Callable[[int, Any], None] | None = None

    def recorded(self, sent) -> None:
        if self.record_sent is not None and sent is not None:
            self.record_sent(sent.chat_id, sent)


Handler = Callable[[ToolContext, dict], Awaitable[str]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Handler

    def schema(self) -> dict:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description,
                             "parameters": self.parameters}}


def params(properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required)}


# ------------------------------------------------------------- validation

def _coerce(value: Any, spec: dict, where: str) -> Any:
    kind = spec.get("type")
    if kind == "string":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = str(value)
        if not isinstance(value, str):
            raise ToolError(f"{where} must be a string.")
        value = value.strip()
        if spec.get("maxLength") and len(value) > spec["maxLength"]:
            value = value[: spec["maxLength"]]
    elif kind == "integer":
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            value = int(value.strip())
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ToolError(f"{where} must be a whole number.")
        if "minimum" in spec and value < spec["minimum"]:
            value = spec["minimum"]
        if "maximum" in spec and value > spec["maximum"]:
            value = spec["maximum"]
    elif kind == "boolean":
        if isinstance(value, str) and value.lower() in ("true", "false"):
            value = value.lower() == "true"
        if not isinstance(value, bool):
            raise ToolError(f"{where} must be true or false.")
    elif kind == "array":
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            raise ToolError(f"{where} must be a list.")
        item_spec = spec.get("items") or {}
        value = [_coerce(item, item_spec, f"{where}[{i}]") for i, item in enumerate(value)]
        if "minItems" in spec and len(value) < spec["minItems"]:
            raise ToolError(f"{where} needs at least {spec['minItems']} items.")
        if "maxItems" in spec and len(value) > spec["maxItems"]:
            raise ToolError(f"{where} allows at most {spec['maxItems']} items.")
    elif kind == "object":
        if not isinstance(value, dict):
            raise ToolError(f"{where} must be an object.")
        value = _check_object(value, spec, where)
    if "enum" in spec and value not in spec["enum"]:
        raise ToolError(f"{where} must be one of: {', '.join(map(str, spec['enum']))}.")
    return value


def _check_object(value: dict, spec: dict, where: str) -> dict:
    properties = spec.get("properties") or {}
    result = {}
    for name in spec.get("required") or ():
        if value.get(name) in (None, ""):
            raise ToolError(f"Missing required argument {name!r}.")
    for name, item in value.items():
        if name not in properties or item is None:
            continue  # unknown or empty optional arguments are ignored
        result[name] = _coerce(item, properties[name], f"{where}.{name}" if where else name)
    return result


def validate_arguments(tool: Tool, arguments: dict) -> dict:
    return _check_object(arguments, tool.parameters, "")


# ---------------------------------------------------------------- registry

class ToolRegistry:
    def __init__(self, tools: list[Tool]):
        self.tools = {tool.name: tool for tool in tools}

    def schemas(self, names: list[str]) -> list[dict]:
        return [self.tools[name].schema() for name in names if name in self.tools]

    async def execute(self, call: ToolCall, context: ToolContext, allowed: list[str],
                      max_chars: int) -> tuple[str, bool]:
        """Run one call. Returns (text for the model, failed)."""
        tool = self.tools.get(call.name)
        if tool is None or call.name not in allowed:
            return (f"Unknown tool {call.name!r}. Available tools: {', '.join(allowed)}.", True)
        if call.error:
            return f"Invalid arguments: {call.error} Call the tool again with a JSON object.", True
        try:
            arguments = validate_arguments(tool, call.arguments)
            result = await tool.handler(context, arguments)
        except ToolError as exc:
            return f"Error: {exc}", True
        except Exception as exc:
            logger.exception("Tool %s failed", call.name)
            return f"Error: the tool failed ({type(exc).__name__}).", True
        return truncate_result(result, max_chars), False


def truncate_result(text: str, max_chars: int) -> str:
    text = text or "(no result)"
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 60].rstrip() + "\n… (shortened: ask for less to see more)"


def timed() -> Callable[[], int]:
    started = time.monotonic()
    return lambda: int((time.monotonic() - started) * 1000)
