"""Group memory tools: remember, forget and search the notes."""

from naruto.agent.claims import asks_to_remember
from naruto.agent.text import strip_bot_mention
from naruto.agent.tools.base import Tool, ToolContext, ToolError, params
from naruto.agent.tools.lookup import find_person
from naruto.db.memory import BOT, CATEGORY_KEYS, MEMBER, NoteLocked
from naruto.memory.notes import note_lines


def _note_id(value) -> int:
    text = str(value).strip().lower().lstrip("[n").rstrip("]")
    if not text.isdigit():
        raise ToolError("note_id is the number of a note, e.g. 12 for [n12].")
    return int(text)


def _own_note(ctx: ToolContext, value):
    note = ctx.services.notes.get(_note_id(value))
    if note is None or note.chat_id != ctx.chat.chat_id:
        raise ToolError(f"There is no note {value} in this group's memory.")
    return note


async def remember(ctx: ToolContext, args: dict) -> str:
    notes = ctx.services.notes
    person_id = find_person(ctx, args["about"]).person_id if args.get("about") else None
    sources = [ctx.trigger.id] if ctx.trigger.id else []
    # Asked to ("remember that…", /remember), or the bot's own idea when
    # someone mentioned something worth keeping.
    asked = ctx.skill == "remember" or asks_to_remember(
        strip_bot_mention(ctx.trigger.text or "", ctx.bot.username))
    how = "asked by" if asked else "on its own, talking with"
    actor = f"bot (run {ctx.state.run_id}), {how} user {ctx.trigger.sender_id}"
    try:
        if args.get("replaces_note_id"):
            note = _own_note(ctx, args["replaces_note_id"])
            changes: dict = {"content": args["content"], "category": args.get("category"),
                             "source_row_ids": sources}
            if args.get("about"):
                changes["person_id"] = person_id
            note = notes.update(note.id, actor=actor, **changes)
            ctx.state.actions.append(f"updated note {note.id}")
            return f"Updated note [n{note.id}]: {note_lines(ctx.services, [note])[0]}"
        if notes.count(ctx.chat.chat_id) >= ctx.services.settings["memory.max_notes_per_chat"]:
            raise ToolError("The group's memory is full. Ask the owner to clear old notes, or "
                            "forget one first.")
        note = notes.add(ctx.chat.chat_id, args["content"], category=args.get("category"),
                         person_id=person_id, source_row_ids=sources,
                         created_by=MEMBER if asked else BOT,
                         created_by_user_id=ctx.trigger.sender_id if asked else None,
                         actor=actor)
    except NoteLocked as exc:
        raise ToolError(f"{exc} It can't be changed.") from None
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    ctx.state.actions.append(f"saved note {note.id}")
    return f"Saved as [n{note.id}]: {note_lines(ctx.services, [note])[0]}"


async def forget(ctx: ToolContext, args: dict) -> str:
    note = _own_note(ctx, args["note_id"])
    try:
        ctx.services.notes.delete(note.id, actor=f"bot (run {ctx.state.run_id}), asked by "
                                                 f"user {ctx.trigger.sender_id}")
    except NoteLocked:
        raise ToolError(f"Note [n{note.id}] is locked by the owner, so I can't forget it.") \
            from None
    ctx.state.actions.append(f"deleted note {note.id}")
    return f"Forgot [n{note.id}]: {note.content}"


async def search_memory(ctx: ToolContext, args: dict) -> str:
    person_id = None
    who = ""
    if args.get("about"):
        member = find_person(ctx, args["about"])
        person_id, who = member.person_id, f" about {member.display_name}"
    found = ctx.services.notes.for_chat(ctx.chat.chat_id, person_id=person_id,
                                        category=args.get("category"),
                                        query=args.get("query"), limit=50)
    if not found:
        return f"No notes{who} match."
    return (f"{len(found)} notes{who}:\n"
            + "\n".join(f"- {line}" for line in note_lines(ctx.services, found)))


CATEGORY = {"type": "string", "enum": list(CATEGORY_KEYS)}

TOOLS = [
    Tool(
        "remember",
        "Save a durable fact in the group's memory (kept for months, after the messages "
        "are gone). One short fact in the third person, e.g. 'Sam is vegetarian'. Use it "
        "when someone asks you to remember something, and on your own when someone "
        "mentions a lasting fact worth keeping (a preference, a birthday, a group decision "
        "or tradition). If a note already says something similar, update it with "
        "replaces_note_id. Don't save health, money or relationship details unless they "
        "explicitly asked.",
        params({
            "content": {"type": "string", "maxLength": 400},
            "category": CATEGORY,
            "about": {"type": "string", "description": "Who it is about (name, @username or "
                                                       "'me'), if anyone."},
            "replaces_note_id": {"type": "integer",
                                 "description": "Update this note instead of adding one."},
        }, ("content",)),
        remember,
    ),
    Tool(
        "forget",
        "Delete a note from the group's memory when someone asks you to forget it.",
        params({"note_id": {"type": "integer", "description": "12 for [n12]."}}, ("note_id",)),
        forget,
    ),
    Tool(
        "search_memory",
        "Find notes in the group's memory: about a person, in a category, or containing "
        "words. Use it for 'what do you remember about…'.",
        params({
            "query": {"type": "string"},
            "about": {"type": "string", "description": "Name, @username or 'me'."},
            "category": CATEGORY,
        }),
        search_memory,
    ),
]
