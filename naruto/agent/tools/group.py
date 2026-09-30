"""Acting in the group: the pinned board, pins, plan proposals and polls."""

import logging

from telegram.error import ChatMigrated, TelegramError

from naruto.agent.tools.base import Tool, ToolContext, ToolError, params
from naruto.agent.tools.lookup import message_in_chat
from naruto.db.board import SECTION_KEYS, SECTIONS
from naruto.db.plans import CANCELLED, PROPOSED
from naruto.tg.board import BoardPublisher
from naruto.tg.plans import render_plan, send_plan

logger = logging.getLogger(__name__)

MAX_POLL_OPTIONS = 10


async def update_board(ctx: ToolContext, args: dict) -> str:
    section = args["section"]
    items = args.get("items") or []
    actor = f"bot (run {ctx.state.run_id})"
    board = ctx.services.boards.set_section(ctx.chat.chat_id, section, items, actor=actor)
    heading = dict(SECTIONS)[section]
    ctx.state.actions.append(f"updated the board ({section})")
    published = await BoardPublisher(ctx.services).publish(ctx.telegram, ctx.chat)
    return (f"{heading} now has {len(board.items(section))} items. {published} "
            "The group can see the board, so don't repeat it in full.")


async def pin_message(ctx: ToolContext, args: dict) -> str:
    message = _pinnable(ctx, args["message_id"])
    try:
        await ctx.telegram.pin_chat_message(message.origin_chat_id, message.message_id,
                                            disable_notification=True)
    except TelegramError as exc:
        raise ToolError(f"Telegram refused the pin ({exc}). I need to be a group admin with "
                        "“Pin messages”.") from None
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
            "it, it goes on the board. Don't repeat the plan in your answer; a short line is "
            "enough.")


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
            "in the chat history. Keep your answer short.")


TOOLS = [
    Tool(
        "update_board",
        "Replace one section of the group's pinned board with a new list of items. "
        "Sections: plans (things to do or happening, done=true when confirmed or "
        "finished), decided (decisions), questions (open questions). Only change the "
        "board when someone asks, or to record something the group clearly agreed on. "
        "Pass the full new list: items you leave out are removed.",
        params({
            "section": {"type": "string", "enum": list(SECTION_KEYS)},
            "items": {"type": "array", "maxItems": 25, "items": {
                "type": "object",
                "properties": {"text": {"type": "string", "maxLength": 300},
                               "done": {"type": "boolean"}},
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
