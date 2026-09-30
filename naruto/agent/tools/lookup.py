"""Resolving what the model refers to: people by name, dates, message IDs."""

from datetime import date, datetime, time as dtime, timedelta

from naruto.agent.tools.base import ToolContext, ToolError
from naruto.db.members import Member
from naruto.db.messages import StoredMessage

SELF_WORDS = {"me", "myself", "i", "sender"}


def _names(member: Member) -> list[str]:
    values = [member.display_name, member.telegram_name or "", member.export_name or "",
              member.username or "", *member.aliases]
    return [value.lower() for value in values if value]


def find_members(ctx: ToolContext, text: str) -> list[Member]:
    """Members of this chat matching a name, @username or alias. Exact
    matches win over partial ones (a first name matches "Alice Tan")."""
    needle = (text or "").strip().lstrip("@").lower()
    if not needle:
        return []
    members = [m for m in ctx.services.members.list(ctx.chat.chat_id)
               if m.user_id != ctx.bot.id]
    if needle in SELF_WORDS and ctx.trigger.sender_id is not None:
        return [m for m in members if m.user_id == ctx.trigger.sender_id]
    exact = [m for m in members if needle in _names(m)]
    if exact:
        return exact
    return [m for m in members if any(needle in name for name in _names(m))]


def find_person(ctx: ToolContext, text: str) -> Member:
    """Exactly one person, or a ToolError the model can act on."""
    found = find_members(ctx, text)
    people = {m.person_id: m for m in found}
    if not people:
        raise ToolError(f"Nobody in this chat is called {text!r}.")
    if len(people) > 1:
        names = ", ".join(sorted(m.display_name for m in people.values()))
        raise ToolError(f"{text!r} could be several people: {names}. Be more specific.")
    return next(iter(people.values()))


def user_ids_of(ctx: ToolContext, member: Member) -> list[int]:
    person = ctx.services.people.get(member.person_id)
    return [a.user_id for a in person.accounts] if person else [member.user_id]


def parse_day(ctx: ToolContext, value: str, *, name: str = "date") -> int:
    """YYYY-MM-DD (or "today" / "yesterday") -> local midnight as Unix time."""
    tz = ctx.services.timezone()
    text = (value or "").strip().lower()
    today = datetime.now(tz).date()
    if text == "today":
        day = today
    elif text == "yesterday":
        day = today - timedelta(days=1)
    else:
        try:
            day = date.fromisoformat(text[:10])
        except ValueError:
            raise ToolError(f"{name} must look like 2026-09-30 (or 'today').") from None
    return int(datetime.combine(day, dtime.min, tz).timestamp())


def message_in_chat(ctx: ToolContext, row_id: int) -> StoredMessage:
    message = ctx.services.messages.get(row_id)
    if message is None or message.chat_id != ctx.chat.chat_id:
        raise ToolError(f"There is no message {row_id} in this chat.")
    return message
