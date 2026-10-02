"""Acting in the group: the pinned board, pins, plan proposals and polls."""

from html import escape
import logging

from telegram import ReplyParameters
from telegram.error import ChatMigrated, TelegramError

from naruto.agent.tools.base import Tool, ToolContext, ToolError, params
from naruto.agent.tools.lookup import find_members, message_in_chat
from naruto.db.board import (
    MAX_BOARD_CHARS,
    MAX_DETAILS_PER_PLAN,
    MAX_TITLE_CHARS,
    SECTION_KEYS,
    SECTIONS,
    BoardFull,
    BoardItem,
)
from naruto.db.plans import CANCELLED, PROPOSED
from naruto.tg.access import note_pin
from naruto.tg.board import BoardPublisher
from naruto.tg.plans import render_plan, send_plan

logger = logging.getLogger(__name__)

MAX_POLL_OPTIONS = 10
# After posting something the group sees, the model may send nothing more.
NOTHING_TO_ADD = "If there's nothing to add, answer with just [NO REPLY]; otherwise keep it short."
# The tools whose result the group sees, so a run may end after them without an answer.
POSTING_TOOLS = ("update_board", "propose_plan", "create_poll")


async def update_board(ctx: ToolContext, args: dict) -> str:
    section = args["section"]
    items = [dict(item) for item in args.get("items") or []]
    notes = _who_for(ctx, items)
    actor = f"bot (run {ctx.state.run_id})"
    asked = ctx.services.boards.get(ctx.chat.chat_id).items("questions")
    title = {"title": args["title"]} if args.get("title") else {}
    try:
        board = ctx.services.boards.set_section(ctx.chat.chat_id, section, items, actor=actor,
                                                **title)
    except BoardFull as exc:
        raise ToolError(f"Not changed: {exc}") from None
    heading = dict(SECTIONS)[section]
    ctx.state.actions.append(f"updated the board ({section})")
    if section == "plans" and not board.title:
        notes.append("The title is built from the plan names; pass title to give it a better one.")
    published = await BoardPublisher(ctx.services).publish(ctx.telegram, ctx.chat)
    if section == "questions":
        notes.append(await _ping(ctx, asked, board.items("questions")))
    return " ".join(part for part in (
        f"{heading} now has {len(board.items(section))} items.", published, *notes,
        f"The group can see the board, so don't repeat it in full. {NOTHING_TO_ADD}") if part)


def _who_for(ctx: ToolContext, items: list[dict]) -> list[str]:
    """A question "for" someone gets their name and user ID, so the board
    can mention them. Returns notes for the model."""
    notes = []
    for item in items:
        name = str(item.pop("for", "") or "").strip()
        if not name:
            continue
        found = {m.person_id: m for m in find_members(ctx, name)}
        if len(found) == 1:
            member = next(iter(found.values()))
            item["for_name"], item["for_user_id"] = member.display_name, member.user_id
        else:
            item["for_name"] = name
            who = "nobody" if not found else "more than one person"
            notes.append(f"{name!r} matches {who} in this chat, so they aren't mentioned.")
    return notes


async def _ping(ctx: ToolContext, before: list[BoardItem], after: list[BoardItem]) -> str:
    """Mention the people new questions are for in a message of its own:
    editing the board notifies nobody."""
    if not ctx.services.settings.for_chat(ctx.chat.chat_id)["board.ping_questions"]:
        return ""
    asked = {(q.text.lower(), q.for_user_id) for q in before}
    new = [q for q in after if q.for_user_id and (q.text.lower(), q.for_user_id) not in asked]
    if not new:
        return ""
    board = ctx.services.boards.get(ctx.chat.chat_id)
    chat_id = board.message_chat_id or ctx.chat.chat_id
    reply = (ReplyParameters(message_id=board.message_id, allow_sending_without_reply=True)
             if board.message_id else None)
    text = "\n".join(f'❓ <a href="tg://user?id={q.for_user_id}">{escape(q.for_name or "")}</a>: '
                     f"{escape(q.text)}" for q in new)
    names = ", ".join(dict.fromkeys(q.for_name or "" for q in new))
    try:
        sent = await ctx.telegram.send_message(chat_id=chat_id, text=text, parse_mode="HTML",
                                               reply_parameters=reply)
    except TelegramError as exc:
        logger.warning("Couldn't ping about board questions: %s", exc,
                       extra={"chat_id": chat_id})
        return f"Couldn't send {names} a message about it ({exc})."
    ctx.recorded(sent)
    ctx.state.actions.append(f"asked {names} on the board")
    return f"Sent {names} a message mentioning them, so they're notified."


async def pin_message(ctx: ToolContext, args: dict) -> str:
    message = _pinnable(ctx, args["message_id"])
    try:
        await ctx.telegram.pin_chat_message(message.origin_chat_id, message.message_id,
                                            disable_notification=True)
    except TelegramError as exc:
        note_pin(ctx.services, ctx.chat.chat_id, exc)
        raise ToolError(f"Telegram refused the pin ({exc}). I need to be a group admin with "
                        "“Pin messages”.") from None
    note_pin(ctx.services, ctx.chat.chat_id)
    ctx.state.actions.append(f"pinned message {message.id}")
    return f"Pinned message {message.id}."


async def unpin_message(ctx: ToolContext, args: dict) -> str:
    message = _pinnable(ctx, args["message_id"])
    try:
        await ctx.telegram.unpin_chat_message(message.origin_chat_id,
                                              message_id=message.message_id)
    except TelegramError as exc:
        raise ToolError(f"Telegram refused to unpin it ({exc}).") from None
    ctx.state.actions.append(f"unpinned message {message.id}")
    return f"Unpinned message {message.id}."


def _pinnable(ctx: ToolContext, row_id: int):
    message = message_in_chat(ctx, row_id)
    if not message.is_live:
        raise ToolError("That message comes from an imported history; only messages sent "
                        "while I was in the group can be pinned.")
    if message.origin_chat_id != ctx.chat.chat_id:
        raise ToolError("That message is from before the group was upgraded, so it can't be "
                        "pinned any more.")
    return message


async def propose_plan(ctx: ToolContext, args: dict) -> str:
    plans = ctx.services.plans
    items = [item for item in args.get("items") or [] if item]
    plan = plans.create(ctx.chat.chat_id, args["title"], items, run_id=ctx.state.run_id,
                        proposed_for_user_id=ctx.trigger.sender_id)
    try:
        sent = await send_plan(ctx.telegram, ctx.services, ctx.chat, plan)
    except TelegramError as exc:
        plans.decide(plan.id, CANCELLED, user_id=None, name="not sent")
        raise ToolError(f"Couldn't post the plan ({exc}).") from None
    ctx.recorded(sent)
    ctx.state.actions.append(f"proposed plan {plan.id}")
    replaced = await _replace_older(ctx, plan)
    note = f" It replaces plan {', '.join(map(str, replaced))}." if replaced else ""
    return (f"Posted plan {plan.id} with Confirm / Change buttons.{note} Once someone confirms "
            f"it, it goes on the board. Don't repeat the plan in your answer. {NOTHING_TO_ADD}")


async def _replace_older(ctx: ToolContext, plan) -> list[int]:
    """An open proposal with the same title is superseded by the new one."""
    replaced = []
    for old in ctx.services.plans.for_chat(ctx.chat.chat_id, status=PROPOSED):
        if old.id == plan.id or old.title.strip().lower() != plan.title.strip().lower():
            continue
        if not ctx.services.plans.decide(old.id, CANCELLED, user_id=None,
                                         name=f"replaced by plan {plan.id}"):
            continue
        replaced.append(old.id)
        if old.message_id and old.message_chat_id:
            try:
                await ctx.telegram.edit_message_text(
                    chat_id=old.message_chat_id, message_id=old.message_id,
                    text=render_plan(old, "↪️ Replaced by a newer plan."), parse_mode="HTML")
            except TelegramError as exc:
                logger.debug("Couldn't mark plan %s as replaced: %s", old.id, exc)
    return replaced


async def create_poll(ctx: ToolContext, args: dict) -> str:
    options = []
    for option in args["options"]:
        option = " ".join(option.split())[:100]
        if option and option.lower() not in (o.lower() for o in options):
            options.append(option)
    if len(options) < 2:
        raise ToolError("A poll needs at least two different options.")
    kwargs = dict(question=args["question"][:300], options=options[:MAX_POLL_OPTIONS],
                  is_anonymous=bool(args.get("anonymous", False)),
                  allows_multiple_answers=bool(args.get("multiple_answers", False)))
    try:
        try:
            sent = await ctx.telegram.send_poll(chat_id=ctx.chat.chat_id, **kwargs)
        except ChatMigrated as exc:
            ctx.services.chats.migrate(ctx.chat.chat_id, exc.new_chat_id)
            sent = await ctx.telegram.send_poll(chat_id=exc.new_chat_id, **kwargs)
    except TelegramError as exc:
        raise ToolError(f"Couldn't create the poll ({exc}).") from None
    ctx.recorded(sent)
    ctx.state.actions.append("created a poll")
    return (f"The poll is up: {kwargs['question']} ({', '.join(options)}). Votes will show "
            f"in the chat history. {NOTHING_TO_ADD}")


TOOLS = [
    Tool(
        "update_board",
        "Replace one section of the group's pinned board with a new list of items. "
        "Sections: plans (each plan with its details: when, where, who does or brings what, "
        "what was decided about it; done=true once confirmed) and questions (open "
        "questions; \"for\" names the one person a question is for, who then gets a "
        "message mentioning them). Only change the board when someone asks, or to record "
        "something the group clearly agreed on. Pass the full new list: items you leave out "
        "are removed. Give a title whenever the plans change. The whole board must fit in "
        f"one message (about {MAX_BOARD_CHARS} characters), so keep items short.",
        params({
            "section": {"type": "string", "enum": list(SECTION_KEYS)},
            "title": {"type": "string", "maxLength": MAX_TITLE_CHARS,
                      "description": "What the board's plans are about in a few words, e.g. "
                                     "'Fri dinner + poker · Sat BBQ'. Shown first and in the "
                                     "pin bar."},
            "items": {"type": "array", "maxItems": 25, "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "maxLength": 300,
                             "description": "A plan's name with its day, e.g. 'Sat 10 Oct · "
                                            "BBQ at East Coast', or a question."},
                    "done": {"type": "boolean", "description": "Plans: true once confirmed."},
                    "details": {"type": "array", "maxItems": MAX_DETAILS_PER_PLAN,
                                "items": {"type": "string", "maxLength": 200},
                                "description": "Plans: the details, one per item."},
                    "for": {"type": "string",
                            "description": "Questions: who it's for, by name."}},
                "required": ["text"]}},
        }, ("section", "items")),
        update_board,
    ),
    Tool(
        "pin_message",
        "Pin a message in the group (silently). Only when someone asks for a pin.",
        params({"message_id": {"type": "integer", "description": "The [id] of the message."}},
               ("message_id",)),
        pin_message,
    ),
    Tool(
        "unpin_message",
        "Unpin a pinned message.",
        params({"message_id": {"type": "integer", "description": "The [id] of the message."}},
               ("message_id",)),
        unpin_message,
    ),
    Tool(
        "propose_plan",
        "Post a clear plan with Confirm / Change buttons, when the group has settled "
        "on something (what, when, where, who does what). Confirmed plans go on the board.",
        params({
            "title": {"type": "string", "maxLength": 120,
                      "description": "Short name, e.g. 'BBQ on Saturday'."},
            "items": {"type": "array", "minItems": 1, "maxItems": 12,
                      "items": {"type": "string", "maxLength": 200},
                      "description": "The details, one per item: date and time, place, "
                                     "who brings or books what."},
        }, ("title", "items")),
        propose_plan,
    ),
    Tool(
        "create_poll",
        "Start a native Telegram poll, e.g. to pick a date or choose between options.",
        params({
            "question": {"type": "string", "maxLength": 300},
            "options": {"type": "array", "minItems": 2, "maxItems": MAX_POLL_OPTIONS,
                        "items": {"type": "string", "maxLength": 100}},
            "multiple_answers": {"type": "boolean",
                                 "description": "Allow picking several options."},
            "anonymous": {"type": "boolean",
                          "description": "Hide who voted (default: votes are visible)."},
        }, ("question", "options")),
        create_poll,
    ),
]
