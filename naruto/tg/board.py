"""Publishing the board: one pinned message per chat, edited in place.

The board is sent as a Telegram rich message (Bot API 10.1, sent through
do_api_request because python-telegram-bot 22.8 predates it). If Telegram
refuses rich messages, it falls back to an HTML message and remembers that.
"""

from datetime import datetime
from html import escape
import logging
import re

from telegram.error import BadRequest, ChatMigrated, Forbidden, TelegramError

from naruto.db.board import SECTIONS, Board
from naruto.db.chats import Chat
from naruto.services import Services

logger = logging.getLogger(__name__)

RICH = "rich"
HTML = "html"
EMPTY_TEXT = "Nothing on it yet. Ask me to add plans, decisions or open questions."
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_\[\]<>|~#])")


def _mark(section: str, done: bool) -> str:
    if section == "plans":
        return "☑" if done else "☐"
    return "•"


def render_markdown(board: Board, updated: str) -> str:
    lines = [f"📌 **Board** · updated {updated}"]
    if board.is_empty:
        return f"{lines[0]}\n\n{EMPTY_TEXT}"
    for key, heading in SECTIONS:
        items = board.items(key)
        if not items:
            continue
        lines += ["", f"### {heading}"]
        lines += [f"- {_mark(key, item.done)} {_MARKDOWN_SPECIAL.sub(r'\\\1', item.text)}"
                  for item in items]
    return "\n".join(lines)


def render_html(board: Board, updated: str) -> str:
    lines = [f"📌 <b>Board</b> · updated {escape(updated)}"]
    if board.is_empty:
        return f"{lines[0]}\n\n{EMPTY_TEXT}"
    for key, heading in SECTIONS:
        items = board.items(key)
        if not items:
            continue
        lines += ["", f"<b>{heading}</b>"]
        lines += [f"{_mark(key, item.done)} {escape(item.text)}" for item in items]
    return "\n".join(lines)


def _not_modified(exc: TelegramError) -> bool:
    return "not modified" in str(exc).lower()


def _message_id(result) -> int:
    if isinstance(result, dict):
        return int(result["message_id"])
    return int(result.message_id)


class BoardPublisher:
    def __init__(self, services: Services):
        self.services = services

    def _updated(self, board: Board) -> str:
        tz = self.services.timezone()
        when = datetime.fromtimestamp(board.updated_at or 0, tz) if board.updated_at \
            else datetime.now(tz)
        return when.strftime("%d %b, %H:%M")

    async def publish(self, telegram, chat: Chat, *, fresh: bool = False) -> str:
        """Show the current board in the chat. Edits the existing board
        message when possible; otherwise sends a new one and pins it.
        Returns a short description of what happened."""
        boards = self.services.boards
        board = boards.get(chat.chat_id)
        wanted = self.services.settings["board.format"]
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
        if self.services.settings["board.pin"]:
            try:
                await telegram.pin_chat_message(chat_id, message_id, disable_notification=True)
                pinned = True
                note = "Sent and pinned the board."
            except TelegramError as exc:
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
                    "rich_message": {"markdown": render_markdown(board, updated)}})
                return _message_id(result), RICH
            except (ChatMigrated, Forbidden):
                raise
            except TelegramError as exc:
                logger.warning("Rich message refused (%s); sending the board as HTML.", exc,
                               extra={"chat_id": chat_id})
        sent = await telegram.send_message(chat_id=chat_id, text=render_html(board, updated),
                                           parse_mode="HTML", disable_notification=True)
        return sent.message_id, HTML

    async def _edit(self, telegram, chat_id: int, message_id: int, board: Board,
                    format: str) -> None:
        updated = self._updated(board)
        if format == RICH:
            await telegram.do_api_request("editMessageText", api_kwargs={
                "chat_id": chat_id, "message_id": message_id,
                "rich_message": {"markdown": render_markdown(board, updated)}})
        else:
            await telegram.edit_message_text(chat_id=chat_id, message_id=message_id,
                                             text=render_html(board, updated), parse_mode="HTML")
