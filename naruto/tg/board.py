"""Publishing the board: one pinned message per chat, edited in place.

The board is sent as a Telegram rich message (Bot API 10.1, sent through
do_api_request because python-telegram-bot 22.8 predates it). If Telegram
refuses rich messages, it falls back to an HTML message and remembers that.

Layout: the title (also the pin text in apps that preview rich messages);
each section under a line and a highlighted heading; each plan as one
tight block, its status emoji and name in bold, then its details; the open
questions (mentioning who each is for) as one block; a small "updated
5 minutes ago" that each app shows in the reader's own time. Blocks join
their lines with <br>: list blocks would put a gap between every line.
"""

from dataclasses import dataclass, replace
from datetime import datetime
from html import escape
import logging
import re

from telegram.error import BadRequest, ChatMigrated, Forbidden, TelegramError

from naruto.db.board import SECTION_KEYS, SECTIONS, Board, BoardItem
from naruto.db.chats import Chat
from naruto.services import Services
from naruto.tg.access import note_pin

logger = logging.getLogger(__name__)

RICH = "rich"
HTML = "html"
TELEGRAM_LIMIT = 4096  # characters in one message
EMPTY_TEXT = "Nothing on it yet. Ask me to add plans or open questions."
CONFIRMED = "✅"
NOT_LOCKED = "⏳"
HEADINGS = dict(SECTIONS)
RULE = "─" * 16  # drawn as text: Telegram centers its divider block
INDENT = "\u2003"  # an em space, which Markdown doesn't collapse
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_\[\]<>|~#$=])")


@dataclass(frozen=True)
class Updated:
    """When the board last changed: apps show ``unix`` relative to now;
    ``text`` (local time) is the fallback."""
    unix: int
    text: str


def _md(text: str) -> str:
    return _MARKDOWN_SPECIAL.sub(r"\\\1", text)


def _time(updated: Updated) -> str:
    return (f'updated <tg-time unix="{updated.unix}" format="r">{escape(updated.text)}'
            "</tg-time>")


def _hidden_note(hidden: int) -> str:
    return f"… and {hidden} more (too long to show; see the web admin)"


def _status(plan: BoardItem) -> str:
    return CONFIRMED if plan.done else NOT_LOCKED


def render_markdown(board: Board, updated: Updated, hidden: int = 0) -> str:
    blocks = [f"📌 **{_md(board.display_title)}**"]
    if board.is_empty:
        blocks.append(EMPTY_TEXT)
    if board.items("plans"):
        blocks.append(f"{RULE}<br>==**{HEADINGS['plans']}**==")
        blocks += ["<br>".join([f"{_status(plan)} **{_md(plan.text)}**",
                                *(f"{INDENT}◦ {_md(detail)}" for detail in plan.details)])
                   for plan in board.items("plans")]
    if board.items("questions"):
        blocks.append(f"{RULE}<br>==**{HEADINGS['questions']}**==")
        lines = []
        for question in board.items("questions"):
            who = ""
            if question.for_user_id:
                who = f"[{_md(question.for_name or 'them')}](tg://user?id={question.for_user_id}): "
            elif question.for_name:
                who = f"{_md(question.for_name)}: "
            lines.append(f"• {who}{_md(question.text)}")
        blocks.append("<br>".join(lines))
    if hidden:
        blocks.append(f"_{_hidden_note(hidden)}_")
    blocks.append(f"<footer><sub>{_time(updated)}</sub></footer>")
    return "\n\n".join(blocks)


# Another plain HTML layout the owner liked (test variant "AN", 2 Oct 2026),
# kept for reference in case the fallback should look like it instead. Plain
# messages have normal line spacing, so it needs no separate blocks per plan:
#
#   📌 <b>Fri dinner + poker · Sat BBQ</b>
#
#   <b>🗓 Plans</b>
#   • <b>Fri 2 Oct · Dinner + poker</b> · ⏳ not locked yet
#   {INDENT}◦ Dinner — venue & time TBC
#   {INDENT}◦ Poker at Jeremy's after, $10 buy-in
#   • <b>Sat 10 Oct · BBQ at East Coast</b> · ✅ confirmed
#   {INDENT}◦ 6 pm, pit 42 booked
#
#   <b>❓ Open questions</b>
#   • <a href="tg://user?id=…">Samuel</a>: Driving or drinking?
#   • Dinner venue and time?
#
#   <i>updated <tg-time unix="…" format="r">02 Oct, 16:47</tg-time></i>
def render_html(board: Board, updated: Updated, hidden: int = 0) -> str:
    blocks = [f"📌 <b>{escape(board.display_title)}</b>"]
    if board.is_empty:
        blocks.append(EMPTY_TEXT)
    if board.items("plans"):
        blocks.append(f"{RULE}\n<b>{HEADINGS['plans']}</b>")
        blocks += ["\n".join([f"{_status(plan)} <b>{escape(plan.text)}</b>",
                              *(f"{INDENT}◦ {escape(detail)}" for detail in plan.details)])
                   for plan in board.items("plans")]
    if board.items("questions"):
        blocks.append(f"{RULE}\n<b>{HEADINGS['questions']}</b>")
        lines = []
        for question in board.items("questions"):
            who = ""
            if question.for_user_id:
                who = (f'<a href="tg://user?id={question.for_user_id}">'
                       f"{escape(question.for_name or 'them')}</a>: ")
            elif question.for_name:
                who = f"{escape(question.for_name)}: "
            lines.append(f"• {who}{escape(question.text)}")
        blocks.append("\n".join(lines))
    if hidden:
        blocks.append(f"<i>{_hidden_note(hidden)}</i>")
    blocks.append(f"<i>{_time(updated)}</i>")
    return "\n\n".join(blocks)


def fit_message(board: Board, render, updated: Updated, limit: int = TELEGRAM_LIMIT) -> str:
    """The rendered board, cut to fit one Telegram message. Saving already
    keeps the board small enough (MAX_BOARD_CHARS); this covers boards saved
    before that check and heavy escaping. The last items are left out."""
    text = render(board, updated)
    sections = {key: list(board.items(key)) for key in SECTION_KEYS}
    hidden = 0
    while len(text) > limit and any(sections.values()):
        last = next(key for key in reversed(SECTION_KEYS) if sections[key])
        sections[last].pop()
        hidden += 1
        text = render(replace(board, sections=sections), updated, hidden)
    return text


def _not_modified(exc: TelegramError) -> bool:
    return "not modified" in str(exc).lower()


def _message_id(result) -> int:
    if isinstance(result, dict):
        return int(result["message_id"])
    return int(result.message_id)


class BoardPublisher:
    def __init__(self, services: Services):
        self.services = services

    def _updated(self, board: Board) -> Updated:
        unix = int(board.updated_at or self.services.time())
        when = datetime.fromtimestamp(unix, self.services.timezone())
        return Updated(unix, when.strftime("%d %b, %H:%M"))

    async def publish(self, telegram, chat: Chat, *, fresh: bool = False) -> str:
        """Show the current board in the chat. Edits the existing board
        message when possible; otherwise sends a new one and pins it.
        Returns a short description of what happened."""
        boards = self.services.boards
        board = boards.get(chat.chat_id)
        settings = self.services.settings.for_chat(chat.chat_id)
        wanted = settings["board.format"]
        same_chat = board.message_chat_id == chat.chat_id
        if board.message_id and same_chat and not fresh and board.format in (wanted, HTML):
            try:
                await self._edit(telegram, chat.chat_id, board.message_id, board, board.format)
            except BadRequest as exc:
                if _not_modified(exc):
                    return "The board message already shows this."
                logger.info("Couldn't edit the board message (%s); sending a new one.", exc,
                            extra={"chat_id": chat.chat_id})
            except TelegramError as exc:
                boards.set_publish_error(chat.chat_id, str(exc)[:300])
                return f"Saved, but updating the board message failed: {exc}"
            else:
                boards.set_message(chat.chat_id, message_id=board.message_id,
                                   message_chat_id=chat.chat_id, format=board.format,
                                   pinned=board.pinned)
                return "Updated the pinned board."
        return await self._send_new(telegram, chat, board, wanted,
                                    old=board if same_chat else None)

    async def _send_new(self, telegram, chat: Chat, board: Board, wanted: str,
                        old: Board | None) -> str:
        boards = self.services.boards
        chat_id = chat.chat_id
        try:
            try:
                message_id, used = await self._send(telegram, chat_id, board, wanted)
            except ChatMigrated as exc:
                self.services.chats.migrate(chat_id, exc.new_chat_id)
                chat_id = exc.new_chat_id
                message_id, used = await self._send(telegram, chat_id, board, wanted)
        except TelegramError as exc:
            boards.set_publish_error(chat.chat_id, str(exc)[:300])
            logger.warning("Couldn't send the board: %s", exc, extra={"chat_id": chat_id})
            return f"Saved, but sending the board failed: {exc}"
        note = "Sent the board."
        pinned = False
        if self.services.settings.for_chat(chat.chat_id)["board.pin"]:
            try:
                await telegram.pin_chat_message(chat_id, message_id, disable_notification=True)
                pinned = True
                note = "Sent and pinned the board."
                note_pin(self.services, chat_id)
            except TelegramError as exc:
                note_pin(self.services, chat_id, exc)
                note = (f"Sent the board, but couldn't pin it ({exc}). I need to be a group "
                        "admin with “Pin messages”.")
            if pinned and old is not None and old.message_id and old.pinned:
                try:
                    await telegram.unpin_chat_message(chat_id, message_id=old.message_id)
                except TelegramError as exc:
                    logger.debug("Couldn't unpin the old board: %s", exc)
        boards.set_message(chat_id, message_id=message_id, message_chat_id=chat_id,
                           format=used, pinned=pinned)
        return note

    async def _send(self, telegram, chat_id: int, board: Board, wanted: str) -> tuple[int, str]:
        updated = self._updated(board)
        if wanted == RICH:
            try:
                result = await telegram.do_api_request("sendRichMessage", api_kwargs={
                    "chat_id": chat_id, "disable_notification": True,
                    "rich_message": {"markdown": fit_message(board, render_markdown, updated)}})
                return _message_id(result), RICH
            except (ChatMigrated, Forbidden):
                raise
            except TelegramError as exc:
                logger.warning("Rich message refused (%s); sending the board as HTML.", exc,
                               extra={"chat_id": chat_id})
        sent = await telegram.send_message(chat_id=chat_id,
                                           text=fit_message(board, render_html, updated),
                                           parse_mode="HTML", disable_notification=True)
        return sent.message_id, HTML

    async def _edit(self, telegram, chat_id: int, message_id: int, board: Board,
                    format: str) -> None:
        updated = self._updated(board)
        if format == RICH:
            await telegram.do_api_request("editMessageText", api_kwargs={
                "chat_id": chat_id, "message_id": message_id,
                "rich_message": {"markdown": fit_message(board, render_markdown, updated)}})
        else:
            await telegram.edit_message_text(chat_id=chat_id, message_id=message_id,
                                             text=fit_message(board, render_html, updated),
                                             parse_mode="HTML")
