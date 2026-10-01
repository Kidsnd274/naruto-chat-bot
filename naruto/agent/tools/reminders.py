"""Reminder tools: schedule a message in the group, or cancel one."""

from datetime import datetime, time as dtime, timedelta
import re
import time

from naruto.agent.tools.base import Tool, ToolContext, ToolError, params
from naruto.db.reminders import PENDING

MAX_PENDING_PER_CHAT = 50
MAX_AHEAD_DAYS = 366
# "in 2 hours", "in a minute", "90 mins", "1h 30m", "in 1 hour and 15 minutes"
_AMOUNT = r"(\d+|an?)\s*(minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|wks?|w)"
_RELATIVE = re.compile(rf"^(?:in\s+)?{_AMOUNT}(?:\s*(?:,|and)?\s*{_AMOUNT})?(?:\s+from\s+now)?$",
                       re.IGNORECASE)
_UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}
# "17:30", "5:30pm", "5pm", optionally after "today" / "tomorrow"
_CLOCK = re.compile(r"^(?:(today|tomorrow|tmr)\s+(?:at\s+)?)?(?:at\s+)?(\d{1,2})(?::(\d{2}))?"
                    r"\s*(am|pm)?$", re.IGNORECASE)


def _relative(match: re.Match) -> int:
    seconds = 0
    for amount, unit in (match.group(1, 2), match.group(3, 4)):
        if amount:
            count = 1 if amount.lower() in ("a", "an") else int(amount)
            seconds += count * _UNITS[unit[0].lower()]
    return seconds


def _clock(match: re.Match, tz, now: float) -> int | None:
    day, hour_text, minute_text, half = match.groups()
    if minute_text is None and half is None:
        return None  # a bare "5" is not a time
    hour, minute = int(hour_text), int(minute_text or 0)
    if half:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if half.lower() == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    today = datetime.fromtimestamp(now, tz).date()
    date = today + timedelta(days=1) if day and day.lower() != "today" else today
    when = datetime.combine(date, dtime(hour, minute), tz)
    if not day and when.timestamp() <= now:
        when += timedelta(days=1)  # "17:30" after 17:30 means tomorrow
    return int(when.timestamp())


def parse_when(value: str, tz, now: float | None = None) -> int:
    """'2026-10-02 09:00' (local time), 'in 2 hours', '17:30' or
    'tomorrow 9am' -> Unix time."""
    text = " ".join((value or "").strip().split())
    now = now or time.time()
    match = _RELATIVE.match(text)
    if match:
        return int(now + _relative(match))
    match = _CLOCK.match(text)
    if match:
        when = _clock(match, tz, now)
        if when is not None:
            return when
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
    now = services.time()
    due = parse_when(args["when"], tz, now)
    if due <= now + 30:
        raise ToolError("That time is already past. Check the time now in the current "
                        "request.")
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
                                    "minutes' / 'in 2 hours' / 'in 3 days', or 'tomorrow "
                                    "09:00'."},
            "text": {"type": "string", "maxLength": 500,
                     "description": "What to remind the group of."},
        }, ("when", "text")),
        set_reminder,
    ),
    Tool(
        "cancel_reminder",
        "Cancel a pending reminder (they are listed under Board, plans and reminders).",
        params({"reminder_id": {"type": "integer"}}, ("reminder_id",)),
        cancel_reminder,
    ),
]
