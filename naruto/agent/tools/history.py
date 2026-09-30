"""Reading the chat beyond the recent window (live and imported messages)."""

from naruto.agent.tools.base import Tool, ToolContext, params
from naruto.agent.tools.lookup import find_person, message_in_chat, parse_day, user_ids_of

MAX_AROUND = 15
MAX_EARLIER = 60


async def search_chat(ctx: ToolContext, args: dict) -> str:
    services = ctx.services
    sender_ids = None
    who = ""
    if args.get("from_person"):
        member = find_person(ctx, args["from_person"])
        sender_ids = user_ids_of(ctx, member)
        who = f" from {member.display_name}"
    since = parse_day(ctx, args["since"], name="since") if args.get("since") else None
    until = parse_day(ctx, args["until"], name="until") + 86400 if args.get("until") else None
    query = args.get("query") or None
    if not query and not sender_ids and since is None:
        return "Give a query, a person or a date to search for."
    limit = services.settings["agent.search_results"]
    found = services.messages.search(ctx.chat.chat_id, query, sender_ids=sender_ids,
                                     since=since, until=until, limit=limit)
    if not found:
        what = f" for {query!r}" if query else ""
        return f"No messages found{what}{who}."
    found.sort(key=lambda m: (m.date, m.id))
    header = (f"{len(found)} messages" + (f" matching {query!r}" if query else "") + who
              + " (oldest first). Use get_messages_around for more context.")
    return f"{header}\n{ctx.builder.describe_messages(found, ctx.bot)}"


async def get_messages_around(ctx: ToolContext, args: dict) -> str:
    target = message_in_chat(ctx, args["message_id"])
    before = min(args.get("before", 5), MAX_AROUND)
    after = min(args.get("after", 5), MAX_AROUND)
    found = ctx.services.messages.around(ctx.chat.chat_id, target.id, before=before, after=after)
    return ctx.builder.describe_messages(found, ctx.bot)


async def get_earlier_messages(ctx: ToolContext, args: dict) -> str:
    if args.get("before_message_id"):
        anchor = message_in_chat(ctx, args["before_message_id"])
    elif ctx.window_ids:
        anchor = message_in_chat(ctx, ctx.window_ids[0])
    else:
        anchor = ctx.trigger
    count = min(args.get("count", 30), MAX_EARLIER)
    found = ctx.services.messages.before(ctx.chat.chat_id, anchor, limit=count)
    if not found:
        return f"There are no messages before message {anchor.id}."
    return (f"The {len(found)} messages before message {anchor.id} (oldest first):\n"
            + ctx.builder.describe_messages(found, ctx.bot))


TOOLS = [
    Tool(
        "search_chat",
        "Search this group's stored messages (including older history) by words, "
        "person and date. Use it when the answer is not in the recent messages.",
        params({
            "query": {"type": "string", "description": "Words to look for (all must match)."},
            "from_person": {"type": "string",
                            "description": "Only messages from this person (name, @username "
                                           "or nickname; 'me' for the person asking)."},
            "since": {"type": "string", "description": "Earliest day, YYYY-MM-DD."},
            "until": {"type": "string", "description": "Latest day, YYYY-MM-DD."},
        }),
        search_chat,
    ),
    Tool(
        "get_messages_around",
        "Read the messages just before and after one message, for context.",
        params({
            "message_id": {"type": "integer", "description": "The [id] of the message."},
            "before": {"type": "integer", "minimum": 0, "maximum": MAX_AROUND},
            "after": {"type": "integer", "minimum": 0, "maximum": MAX_AROUND},
        }, ("message_id",)),
        get_messages_around,
    ),
    Tool(
        "get_earlier_messages",
        "Read further back: the messages before a given message (default: before "
        "the oldest recent message you can see).",
        params({
            "before_message_id": {"type": "integer",
                                  "description": "Read the messages before this [id]."},
            "count": {"type": "integer", "minimum": 1, "maximum": MAX_EARLIER},
        }),
        get_earlier_messages,
    ),
]
