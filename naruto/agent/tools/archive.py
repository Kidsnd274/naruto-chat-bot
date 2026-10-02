"""Looking up history digests: dated summaries of the group's past, kept
after the original messages were deleted."""

from datetime import date, datetime, time as dtime
import re

from naruto.agent.tools.base import Tool, ToolContext, ToolError, params
from naruto.periods import describe_span, period_label

SUMMARY_NOTE = ("These are summaries written when the history was archived, not the original "
                "messages: say so when you use them, and don't quote them as someone's words. "
                "search_chat finds exact messages only while they are still stored.")


def _boundary(ctx: ToolContext, value: str, *, name: str, end: bool) -> int:
    """YYYY, YYYY-MM or YYYY-MM-DD as a local time: the start of that year,
    month or day, or (``end``) the start of the next one."""
    text = (value or "").strip()
    match = re.fullmatch(r"(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?", text)
    if not match:
        raise ToolError(f"{name} must look like 2021, 2021-07 or 2021-07-14.")
    year, month, day = int(match.group(1)), match.group(2), match.group(3)
    try:
        if day:
            start = date(year, int(month), int(day))
            following = date.fromordinal(start.toordinal() + 1)
        elif month:
            start = date(year, int(month), 1)
            following = date(year + (start.month == 12), start.month % 12 + 1, 1)
        else:
            start, following = date(year, 1, 1), date(year + 1, 1, 1)
    except ValueError:
        raise ToolError(f"{name} is not a real date: {text}.") from None
    tz = ctx.services.timezone()
    return int(datetime.combine(following if end else start, dtime.min, tz).timestamp())


async def search_history_summaries(ctx: ToolContext, args: dict) -> str:
    services = ctx.services
    chat_id = ctx.chat.chat_id
    query = (args.get("query") or "").strip() or None
    since = _boundary(ctx, args["since"], name="since", end=False) if args.get("since") else None
    until = _boundary(ctx, args["until"], name="until", end=True) if args.get("until") else None
    if since is not None and until is not None and until <= since:
        raise ToolError("until is before since.")
    per_page = services.settings["history.lookup_results"]
    page = max(1, args.get("page") or 1)
    digests, total = services.history.search(chat_id, query=query, since=since, until=until,
                                             limit=per_page, offset=(page - 1) * per_page)
    tz = services.timezone()
    start, end, count = services.history.coverage(chat_id)
    if not count:
        return "This group has no history summaries yet."
    covered = f"Summaries cover {describe_span(start, end, tz)} ({count} periods)."
    if not digests:
        what = f" about {query!r}" if query else ""
        if page > 1 and total:
            return f"There are only {total} matching summaries{what}. {covered}"
        return (f"No history summaries{what} in those dates. {covered} Try other words, or "
                "only dates.")
    blocks = []
    for digest in digests:
        label = period_label(digest.period_start, digest.period_end, digest.grouping, tz)
        span = describe_span(digest.period_start, digest.period_end, tz)
        header = f"### {label}" + ("" if label == span else f" ({span})")
        details = [f"{digest.message_count} messages"]
        if digest.edited:
            details.append("corrected by the owner")
        lines = [header, f"({'; '.join(details)})"]
        lines += [f"Note: {note}" for note in digest.limitations]
        lines.append(digest.text)
        blocks.append("\n".join(lines))
    pages = -(-total // per_page)
    footer = f"Page {page} of {pages}." + (
        f" Ask for page {page + 1} for more, or narrow the words or dates." if page < pages else "")
    return "\n\n".join([SUMMARY_NOTE, *blocks, footer])


TOOLS = [
    Tool(
        "search_history_summaries",
        "Look up dated summaries of this group's past (a month or week each), kept after the "
        "original messages were deleted. Use it for questions about earlier times (\"what were "
        "we planning in summer 2021?\", \"when did we last go camping?\"), or when search_chat "
        "finds nothing that old. Results are summaries, not the original messages.",
        params({
            "query": {"type": "string",
                      "description": "Words to look for (any may match; best matches first). "
                                     "Leave out to list the dates' summaries in order."},
            "since": {"type": "string", "description": "Earliest year, month or day: 2021, "
                                                       "2021-07 or 2021-07-14."},
            "until": {"type": "string", "description": "Latest year, month or day (included)."},
            "page": {"type": "integer", "minimum": 1, "maximum": 50},
        }),
        search_history_summaries,
    ),
]
