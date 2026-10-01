"""The board and plan proposals on the chat page: view, edit, publish, clear."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from telegram.error import TelegramError

from naruto.db.board import SECTIONS, BoardFull, format_lines, parse_lines
from naruto.db.chats import Chat
from naruto.services import Services
from naruto.tg.board import BoardPublisher
from naruto.web.auth import require_admin
from naruto.web.templating import flash

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(require_admin)])
ACTOR = "owner (web admin)"


def chat_board(services: Services, chat: Chat) -> dict:
    """Section on the chat detail page."""
    board = services.boards.get(chat.chat_id)
    return {
        "board": board,
        "board_sections": [(key, heading, format_lines(board.items(key)))
                           for key, heading in SECTIONS],
        "plans": services.plans.for_chat(chat.chat_id, limit=10),
    }


def _chat(services: Services, chat_id: int) -> Chat:
    chat = services.chats.get(chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Unknown chat.")
    return chat


def _back(chat: Chat) -> RedirectResponse:
    return RedirectResponse(f"/chats/{chat.chat_id}#board", status_code=303)


async def _publish(request: Request, services: Services, chat: Chat, *, fresh: bool) -> None:
    if services.telegram is None:
        flash(request, "Saved. The Telegram bot is not running, so the board wasn't sent.", "warn")
        return
    if not chat.enabled:
        flash(request, "Saved. The chat isn't enabled, so the board wasn't sent.", "warn")
        return
    try:
        result = await BoardPublisher(services).publish(services.telegram, chat, fresh=fresh)
    except TelegramError as exc:
        flash(request, f"Couldn't send the board: {exc}", "error")
        return
    kind = "warn" if "couldn't" in result.lower() or "failed" in result.lower() else "ok"
    flash(request, result, kind)


@router.post("/chats/{chat_id}/board")
async def save_board(request: Request, chat_id: int):
    services: Services = request.app.state.services
    chat = _chat(services, chat_id)
    form = await request.form()
    try:
        services.boards.set_sections(
            chat.chat_id, {key: parse_lines(str(form.get(key) or "")) for key, _ in SECTIONS},
            actor=ACTOR)
    except BoardFull as exc:
        flash(request, f"Not saved: {exc}", "error")
        return _back(chat)
    logger.info("Board edited in the web admin", extra={"chat_id": chat.chat_id})
    if form.get("publish"):
        await _publish(request, services, chat, fresh=False)
    else:
        flash(request, "Board saved. It updates in the chat the next time it's published.")
    return _back(chat)


@router.post("/chats/{chat_id}/board/publish")
async def publish_board(request: Request, chat_id: int):
    services: Services = request.app.state.services
    chat = _chat(services, chat_id)
    form = await request.form()
    await _publish(request, services, chat, fresh=bool(form.get("fresh")))
    return _back(chat)


@router.post("/chats/{chat_id}/board/clear")
async def clear_board(request: Request, chat_id: int):
    services: Services = request.app.state.services
    chat = _chat(services, chat_id)
    form = await request.form()
    if form.get("confirm") != "yes":
        return request.app.state.templates.TemplateResponse(request, "confirm.html", {
            "title": "Clear the board?",
            "message": f"This removes every item on {chat.display_title}'s board and unpins the "
                       "board message. It cannot be undone.",
            "action": f"/chats/{chat.chat_id}/board/clear",
            "fields": {"confirm": "yes"},
            "confirm_label": "Clear board",
            "cancel": f"/chats/{chat.chat_id}#board",
        })
    board = services.boards.get(chat.chat_id)
    if board.message_id and board.pinned and services.telegram is not None:
        try:
            await services.telegram.unpin_chat_message(board.message_chat_id,
                                                       message_id=board.message_id)
        except TelegramError as exc:
            logger.info("Couldn't unpin the old board: %s", exc, extra={"chat_id": chat.chat_id})
    services.boards.clear(chat.chat_id)
    logger.info("Board cleared in the web admin", extra={"chat_id": chat.chat_id})
    flash(request, "Board cleared.")
    return _back(chat)
