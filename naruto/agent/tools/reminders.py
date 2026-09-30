"""Reminder tools: schedule a message in the group, or cancel one."""

from datetime import datetime
import re
import time

from naruto.agent.tools.base import Tool, ToolContext, ToolError, params
from naruto.db.reminders import PENDING

MAX_PENDING_PER_CHAT = 50
MAX_AHEAD_DAYS = 366
_RELATIVE = re.compile(r"^in\s+(\d+)\s*(minute|min|hour|hr|day|week)s?$", re.IGNORECASE)
_UNITS = {"minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": 86400, "week": 7 * 86400}


def parse_when(value: str, tz, now: float | None = None) -> int:
    """'2026-10-02 09:00' (local time) or 'in 2 hours' -> Unix time."""
    text = " ".join((value or "").strip().split())
    now = now or time.time()
    match = _RELATIVE.match(text)
    if match:
        return int(now + int(match.group(1)) * _UNITS[match.group(2).lower()])
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ToolError("Give a time as well, e.g. 2026-10-02 09:00.")
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        raise ToolError("'when' must look like 2026-10-02 09:00 (the group's time zone) or "
                        "'in 2 hours'.") from None
    if when.tzinfo is None:
        when = when.replace(tzinfo=tz)
    return int(when.timestamp())


async def set_reminder(ctx: ToolContext, args: dict) -> str:
    services = ctx.services
    tz = services.timezone()
    due = parse_when(args["when"], tz)
    now = time.time()
    if due <= now + 30:
        raise ToolError("That time is already past. Check today's date under Chat.")
    if due > now + MAX_AHEAD_DAYS * 86400:
        raise ToolError("Reminders can be at most a year ahead.")
    if services.reminders.pending_count(ctx.chat.chat_id) >= MAX_PENDING_PER_CHAT:
        raise ToolError("This group already has too many pending reminders.")
    reminder = services.reminders.create(
        ctx.chat.chat_id, " ".join(args["text"].split())[:500], due,
        created_by=f"bot (run {ctx.state.run_id})", created_by_user_id=ctx.trigger.sender_id,
        run_id=ctx.state.run_id)
    ctx.state.actions.append(f"set reminder {reminder.id}")
    when = datetime.fromtimestamp(due, tz).strftime("%a %d %b %Y, %H:%M")
    return f"Reminder {reminder.id} set for {when}: {reminder.text}"


async def cancel_reminder(ctx: ToolContext, args: dict) -> str:
    reminder = ctx.services.reminders.get(args["reminder_id"])
    if reminder is None or reminder.chat_id != ctx.chat.chat_id:
        raise ToolError(f"There is no reminder {args['reminder_id']} in this group.")
    if reminder.status != PENDING or not ctx.services.reminders.cancel(reminder.id):
        raise ToolError(f"Reminder {reminder.id} is already {reminder.status}.")
    ctx.state.actions.append(f"cancelled reminder {reminder.id}")
    return f"Cancelled reminder {reminder.id}: {reminder.text}"


TOOLS = [
    Tool(
        "set_reminder",
        "Schedule a reminder message in this group at a given time.",
        params({
            "when": {"type": "string",
                     "description": "YYYY-MM-DD HH:MM in the group's time zone, or 'in 30 "
                                    "minutes' / 'in 2 hours' / 'in 3 days'."},
            "text": {"type": "string", "maxLength": 500,
                     "description": "What to remind the group of."},
        }, ("when", "text")),
        set_reminder,
    ),
    Tool(
        "cancel_reminder",
        "Cancel a pending reminder (they are listed in the background).",
        params({"reminder_id": {"type": "integer"}}, ("reminder_id",)),
        cancel_reminder,
    ),
]
