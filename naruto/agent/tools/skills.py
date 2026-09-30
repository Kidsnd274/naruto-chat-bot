"""use_skill: hand a free-form request over to a focused skill."""

from datetime import datetime, timedelta
import re

from naruto.agent.skills import ROUTABLE, SKILLS
from naruto.agent.tools.base import Tool, ToolContext, ToolError, params
from naruto.agent.tools.lookup import parse_day

_DURATION = re.compile(r"^(\d+)\s*(h|hours?|d|days?)$", re.IGNORECASE)


def parse_since(ctx: ToolContext, value: str) -> int:
    """'today', 'yesterday', YYYY-MM-DD, 'YYYY-MM-DD HH:MM', '6 hours' or '2 days'."""
    text = (value or "").strip()
    match = _DURATION.match(text)
    if match:
        now = datetime.now(ctx.services.timezone())
        unit = timedelta(days=1) if match.group(2)[0].lower() == "d" else timedelta(hours=1)
        return int((now - int(match.group(1)) * unit).timestamp())
    if len(text) > 10:
        try:
            when = datetime.fromisoformat(text)
        except ValueError:
            raise ToolError("since must be 'today', 'yesterday', a date like 2026-09-30, "
                            "'6 hours' or '2 days'.") from None
        if when.tzinfo is None:
            when = when.replace(tzinfo=ctx.services.timezone())
        return int(when.timestamp())
    return parse_day(ctx, text, name="since")


async def use_skill(ctx: ToolContext, args: dict) -> str:
    if ctx.state.model_requests_left < 1:
        raise ToolError("No requests left to switch; answer directly.")
    ctx.state.switch_to_skill = args["skill"]
    if args.get("since"):
        ctx.state.switch_since = parse_since(ctx, args["since"])
    return f"Switching to {args['skill']}."


TOOLS = [
    Tool(
        "use_skill",
        "Hand the current request over to a focused mode with its own instructions and tools, "
        "when the request is one of these: "
        + "; ".join(f"{name}: {SKILLS[name].description}" for name in ROUTABLE)
        + ". Call it on its own, before anything else.",
        params({
            "skill": {"type": "string", "enum": list(ROUTABLE)},
            "since": {"type": "string",
                      "description": "For summarize: read every message since then ('today', "
                                     "'yesterday', a date, '6 hours' or '2 days')."},
        }, ("skill",)),
        use_skill,
    ),
]
