"""History page per chat: the dated summaries of past periods (history
digests), with filters, edits and deletion, the live months waiting to be
summarized, and when live recording began."""

from datetime import date
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from naruto.db.chats import Chat
from naruto.db.history import ACTIVE, REPLACED, STAGED
from naruto.periods import day_start, describe_span, period_label
from naruto.services import Services
from naruto.web.auth import require_admin
from naruto.web.templating import flash

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(require_admin)])
ACTOR = "owner (web admin)"
PAGE_SIZE = 50
STATUS_FILTERS = {"": ACTIVE, "replaced": REPLACED, "staged": STAGED, "all": None}


def _services(request: Request) -> Services:
    return request.app.state.services


def _chat(services: Services, chat_id: int) -> Chat:
    chat = services.chats.get(chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Unknown chat.")
    return chat


def _to_history(chat: Chat) -> RedirectResponse:
    return RedirectResponse(f"/chats/{chat.chat_id}/history", status_code=303)


def _day(services: Services, raw: str | None) -> int | None:
    try:
        return day_start(date.fromisoformat(raw), services.timezone()) if raw else None
    except ValueError:
        return None


def chat_history(services: Services, chat: Chat) -> dict:
    """Section on the chat detail page."""
    start, end, count = services.history.coverage(chat.chat_id)
    return {"history_count": count,
            "history_span": describe_span(start, end, services.timezone()) if count else None}


@router.get("/chats/{chat_id}/history")
async def history_page(request: Request, chat_id: int):
    services = _services(request)
    chat = _chat(services, chat_id)
    params = request.query_params
    tz = services.timezone()
    status_key = params.get("status") if params.get("status") in STATUS_FILTERS else ""
    since = _day(services, params.get("since"))
    until = _day(services, params.get("until"))
    query = (params.get("q") or "").strip()
    try:
        offset = max(int(params.get("offset") or 0), 0)
        import_id = int(params["import"]) if params.get("import") else None
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid filter.") from None
    digests, total = services.history.for_chat(
        chat.chat_id, status=STATUS_FILTERS[status_key], since=since,
        until=until + 86400 if until is not None else None, query=query or None,
        limit=None if import_id else PAGE_SIZE, offset=0 if import_id else offset)
    if import_id is not None:
        digests = [d for d in digests if d.import_id == import_id]
        total = len(digests)
    live = [p for p in services.history.live_periods(chat.chat_id)
            if p.status not in ("done", "reused")]
    return request.app.state.templates.TemplateResponse(request, "history.html", {
        "chat": chat,
        "digests": digests,
        "labels": {d.id: period_label(d.period_start, d.period_end, d.grouping, tz)
                   for d in digests},
        "spans": {d.id: describe_span(d.period_start, d.period_end, tz) for d in digests},
        "edits": {d.id: services.history.edits(d.id) for d in digests if d.edited},
        "total": total,
        "offset": offset,
        "limit": PAGE_SIZE,
        "filters": {"q": query, "since": params.get("since") or "",
                    "until": params.get("until") or "", "status": status_key,
                    "import": import_id},
        "coverage": services.history.coverage(chat.chat_id),
        "live_periods": live,
        "live_archive": services.settings.for_chat(chat.chat_id)["history.live_archive"],
        "recording_since": chat.recording_since,
        "tz": tz,
    })


@router.post("/chats/{chat_id}/history/{digest_id}")
async def edit_digest(request: Request, chat_id: int, digest_id: int):
    services = _services(request)
    chat = _chat(services, chat_id)
    digest = services.history.get(digest_id)
    if digest is None or digest.chat_id != chat.chat_id:
        raise HTTPException(status_code=404, detail="Unknown summary.")
    form = await request.form()
    text = str(form.get("text") or "").replace("\r\n", "\n").strip()
    if not text:
        flash(request, "A summary can't be empty; delete it instead.", "error")
    elif text != digest.text:
        services.history.edit(digest.id, text, actor=ACTOR)
        flash(request, "Summary saved. Its dates and source stay as they were.")
    return _to_history(chat)


@router.post("/chats/{chat_id}/history/{digest_id}/delete")
async def delete_digest(request: Request, chat_id: int, digest_id: int):
    services = _services(request)
    chat = _chat(services, chat_id)
    digest = services.history.get(digest_id)
    if digest is None or digest.chat_id != chat.chat_id:
        raise HTTPException(status_code=404, detail="Unknown summary.")
    form = await request.form()
    span = describe_span(digest.period_start, digest.period_end, services.timezone())
    if form.get("confirm") != "yes":
        return request.app.state.templates.TemplateResponse(request, "confirm.html", {
            "title": f"Delete the summary of {span}?",
            "message": "The bot can no longer look it up. If its messages are gone too, only "
                       "a new upload of the export can bring it back.",
            "action": f"/chats/{chat.chat_id}/history/{digest.id}/delete",
            "fields": {"confirm": "yes"}, "confirm_label": "Delete summary",
            "cancel": f"/chats/{chat.chat_id}/history"})
    services.history.delete(digest.id)
    logger.info("Deleted the history summary of %s from the web admin", span,
                extra={"chat_id": chat.chat_id})
    flash(request, f"Deleted the summary of {span}.")
    return _to_history(chat)


@router.post("/chats/{chat_id}/history-clear")
async def clear_history(request: Request, chat_id: int):
    services = _services(request)
    chat = _chat(services, chat_id)
    form = await request.form()
    _, _, count = services.history.coverage(chat.chat_id)
    if form.get("confirm") != "yes":
        return request.app.state.templates.TemplateResponse(request, "confirm.html", {
            "title": f"Delete all {count} history summaries?",
            "message": f"This permanently deletes every summary of {chat.display_title}'s past, "
                       "edited ones included. Messages and memory notes are not affected. It "
                       "cannot be undone.",
            "action": f"/chats/{chat.chat_id}/history-clear",
            "fields": {"confirm": "yes"}, "confirm_label": "Delete all summaries",
            "cancel": f"/chats/{chat.chat_id}"})
    deleted = services.history.delete_for_chat(chat.chat_id)
    logger.info("Deleted %s history summaries from the web admin", deleted,
                extra={"chat_id": chat.chat_id})
    flash(request, f"Deleted {deleted} history summaries.")
    return RedirectResponse(f"/chats/{chat.chat_id}", status_code=303)


@router.post("/chats/{chat_id}/recording-since")
async def set_recording_since(request: Request, chat_id: int):
    services = _services(request)
    chat = _chat(services, chat_id)
    form = await request.form()
    raw = str(form.get("recording_since") or "").strip()
    if raw:
        ts = _day(services, raw)
        if ts is None:
            flash(request, "Choose a date.", "error")
            return _to_history(chat)
    else:
        ts = None
    services.chats.set_recording_since(chat.chat_id, ts)
    logger.info("Recording start set to %s in the web admin", raw or "unknown",
                extra={"chat_id": chat.chat_id})
    flash(request, "Saved. Imports and their summaries stop at this date; live summaries "
                   "start from it.")
    return _to_history(chat)
