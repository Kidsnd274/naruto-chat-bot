"""Chats list, chat detail (roster, message browser) and data deletion."""

from datetime import date, datetime, time as dtime
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from naruto.db.chats import DISABLED, ENABLED, Chat
from naruto.db.messages import IMPORT, LIVE
from naruto.services import Services
from naruto.web.auth import require_admin
from naruto.web.templating import flash

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(require_admin)])

PAGE_SIZE = 50
ACTOR = "owner (web admin)"


def _services(request: Request) -> Services:
    return request.app.state.services


def _chat_or_404(services: Services, chat_id: int) -> Chat:
    chat = services.chats.get(chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Unknown chat.")
    return chat


def _day_start(services: Services, value: str | None) -> int | None:
    """YYYY-MM-DD in the configured time zone -> Unix time at midnight."""
    if not value:
        return None
    try:
        day = date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid date {value!r}.") from None
    return int(datetime.combine(day, dtime.min, services.timezone()).timestamp())


def _back(chat_id: int | None = None) -> RedirectResponse:
    return RedirectResponse(f"/chats/{chat_id}" if chat_id else "/chats", status_code=303)


async def _referer_or(request: Request, chat_id: int) -> RedirectResponse:
    form = await request.form()
    target = form.get("back")
    if target == "detail":
        return _back(chat_id)
    return _back()


# -------------------------------------------------------------------- list

@router.get("/chats")
async def chats_page(request: Request):
    services = _services(request)
    return request.app.state.templates.TemplateResponse(
        request, "chats.html", {"summaries": services.chats.summaries(),
                                "note_counts": services.notes.counts_by_chat()})


def _rights_summary(chat: Chat) -> str:
    if chat.rights_checked_at is None:
        return "Admin rights unknown (the bot couldn't check)."
    pin = "✓" if chat.can_pin else "✗"
    delete = "✓" if chat.can_delete else "✗"
    return f"Admin rights: pin {pin}, delete {delete}."


@router.post("/chats/{chat_id}/enable")
async def enable_chat(request: Request, chat_id: int):
    services = _services(request)
    chat = _chat_or_404(services, chat_id)
    if chat.enabled:
        flash(request, f"{chat.display_title} is already enabled.")
        return await _referer_or(request, chat_id)
    if services.access is not None:
        chat = await services.access.enable(chat_id, actor=ACTOR)
    else:
        chat = services.chats.set_status(chat_id, ENABLED)
    message = f"Enabled {chat.display_title}."
    if chat.rights_checked_at is not None:
        message += " " + _rights_summary(chat)
    flash(request, message, "warn" if chat.missing_rights() and chat.rights_checked_at else "ok")
    return await _referer_or(request, chat_id)


@router.post("/chats/{chat_id}/disable")
async def disable_chat(request: Request, chat_id: int):
    services = _services(request)
    chat = _chat_or_404(services, chat_id)
    if chat.status == DISABLED:
        flash(request, f"{chat.display_title} is already disabled.")
        return await _referer_or(request, chat_id)
    if services.access is not None:
        await services.access.disable(chat_id, actor=ACTOR)
    else:
        services.chats.set_status(chat_id, DISABLED)
    flash(request, f"Disabled {chat.display_title}: no replies and nothing recorded.")
    return await _referer_or(request, chat_id)


@router.post("/chats/{chat_id}/rights")
async def check_rights(request: Request, chat_id: int):
    services = _services(request)
    chat = _chat_or_404(services, chat_id)
    if services.access is None:
        flash(request, "The Telegram bot is not running, so rights can't be checked.", "error")
        return await _referer_or(request, chat_id)
    before = chat.rights_checked_at
    chat = await services.access.check_rights(chat_id)
    if chat.rights_checked_at == before and before is None:
        flash(request, "Couldn't check admin rights: is the bot still in this group? "
                       "See Logs for the error.", "error")
    else:
        flash(request, _rights_summary(chat), "warn" if chat.missing_rights() else "ok")
    return await _referer_or(request, chat_id)


@router.get("/chats/{chat_id}/leave")
async def leave_confirm(request: Request, chat_id: int):
    services = _services(request)
    chat = _chat_or_404(services, chat_id)
    return request.app.state.templates.TemplateResponse(request, "confirm.html", {
        "title": f"Leave {chat.display_title}?",
        "message": "The bot leaves the group and the chat is marked disabled. Stored "
                   "messages are kept; delete them separately if you want.",
        "action": f"/chats/{chat.chat_id}/leave",
        "fields": {},
        "confirm_label": "Leave group",
        "cancel": f"/chats/{chat.chat_id}",
    })


@router.post("/chats/{chat_id}/leave")
async def leave_chat(request: Request, chat_id: int):
    services = _services(request)
    chat = _chat_or_404(services, chat_id)
    if services.access is None:
        raise HTTPException(status_code=503, detail="The Telegram bot is not running.")
    await services.access.leave(chat_id, actor=ACTOR)
    flash(request, f"Left {chat.display_title}. It is now disabled.")
    return _back(chat_id)


# ------------------------------------------------------------------ detail

def _browse(services: Services, chat: Chat, params) -> dict:
    try:
        offset = max(int(params.get("offset") or 0), 0)
        sender = int(params["sender"]) if params.get("sender") else None
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid filter.") from None
    source = params.get("source") if params.get("source") in (LIVE, IMPORT) else None
    since = _day_start(services, params.get("since"))
    until = _day_start(services, params.get("until"))
    if until is not None:
        until += 86400  # inclusive end day
    query = (params.get("q") or "").strip()
    page = services.messages.browse(chat.chat_id, query=query or None, sender_id=sender,
                                    source=source, since=since, until=until,
                                    offset=offset, limit=PAGE_SIZE)
    filters = {"q": query, "sender": params.get("sender") or "",
               "source": source or "", "since": params.get("since") or "",
               "until": params.get("until") or ""}
    names = services.people.display_names({m.sender_id for m in page.messages})
    return {"page": page, "filters": filters, "names": names}


@router.get("/chats/{chat_id}")
async def chat_detail(request: Request, chat_id: int):
    services = _services(request)
    chat = _chat_or_404(services, chat_id)
    if chat.chat_id != chat_id:
        return RedirectResponse(f"/chats/{chat.chat_id}", status_code=303)
    bot = services.status.bot
    context = {
        "chat": chat,
        "aliases": services.chats.aliases_for(chat.chat_id),
        "members": [m for m in services.members.list(chat.chat_id)
                    if bot is None or m.user_id != bot.id],
        "senders": _senders(services, chat),
        "live_count": services.messages.count(chat.chat_id, LIVE),
        "import_count": services.messages.count(chat.chat_id, IMPORT),
        "setting_overrides": len(services.settings.chat_overrides(chat.chat_id)),
        **_browse(services, chat, request.query_params),
        **(await _extra_detail(request, chat)),
    }
    return request.app.state.templates.TemplateResponse(request, "chat_detail.html", context)


def _senders(services: Services, chat: Chat) -> list[tuple[int, str, int]]:
    """(sender_id, person's name, messages) for the sender filter."""
    senders = services.messages.senders(chat.chat_id)
    names = services.people.display_names({sender_id for sender_id, _, _ in senders})
    return [(sender_id, names.get(sender_id, name), count) for sender_id, name, count in senders]


async def _extra_detail(request: Request, chat: Chat) -> dict:
    """Hook for sections added by later features (imports)."""
    providers = getattr(request.app.state, "chat_detail_extras", [])
    context: dict = {}
    for provider in providers:
        context.update(provider(request.app.state.services, chat))
    return context


@router.get("/chats/{chat_id}/messages")
async def messages_partial(request: Request, chat_id: int):
    services = _services(request)
    chat = _chat_or_404(services, chat_id)
    context = {"chat": chat, **_browse(services, chat, request.query_params)}
    return request.app.state.templates.TemplateResponse(request, "_messages.html", context)


@router.post("/chats/{chat_id}/members/{user_id}/aliases")
async def add_alias(request: Request, chat_id: int, user_id: int):
    services = _services(request)
    chat = _chat_or_404(services, chat_id)
    alias = ((await request.form()).get("alias") or "").strip()
    if alias:
        services.members.add_alias(chat.chat_id, user_id, alias)
    return _back(chat.chat_id)


@router.post("/chats/{chat_id}/members/{user_id}/aliases/delete")
async def remove_alias(request: Request, chat_id: int, user_id: int):
    services = _services(request)
    chat = _chat_or_404(services, chat_id)
    alias = (await request.form()).get("alias") or ""
    services.members.remove_alias(chat.chat_id, user_id, alias)
    return _back(chat.chat_id)


# ------------------------------------------------------------------ delete

DELETE_SCOPES = ("all", "older", "age")
DELETE_SOURCES = {"": None, LIVE: LIVE, IMPORT: IMPORT}
MAX_AGE_DAYS = 36500
DAY = 86400


def _age_days(raw) -> int | None:
    """A whole number of days, 1 or more (not "-1", "1.5", "nan" or "1e3")."""
    raw = str(raw or "").strip()
    if not raw.isascii() or not raw.isdigit() or len(raw) > 6:
        return None
    days = int(raw)
    return days if 1 <= days <= MAX_AGE_DAYS else None


def _busy_reason(services: Services, chat: Chat) -> str | None:
    """Why messages can't be deleted right now: something is reading them
    and could store or summarize them again."""
    busy = services.imports.busy_import(chat.chat_id) if services.imports else None
    if busy is not None:
        return (f"Import #{busy.id} of this group is {busy.status}. Let it finish, or cancel "
                "the rest of it, before deleting messages.")
    if services.history_locks[chat.chat_id].locked():
        return ("A history summary of this group is being written. Try again in a few "
                "minutes.")
    return None


@router.post("/chats/{chat_id}/delete-messages")
async def delete_messages(request: Request, chat_id: int):
    """Two steps: the first POST shows how many messages would go; the
    second (confirm=yes) deletes them. Everything, before a calendar date,
    or older than N days: N × 24 hours before the review, and the
    confirmation deletes up to that same moment."""
    services = _services(request)
    chat = _chat_or_404(services, chat_id)
    form = await request.form()
    scope = form.get("scope")
    if scope not in DELETE_SCOPES:
        raise HTTPException(status_code=400, detail="Choose which messages to delete.")
    if (form.get("source") or "") not in DELETE_SOURCES:
        raise HTTPException(status_code=400, detail="Unknown message source.")
    source = DELETE_SOURCES[form.get("source") or ""]
    confirmed = form.get("confirm") == "yes"
    before_raw = form.get("before") or ""
    days = None
    before = None
    when = ""
    if scope == "older":
        before = _day_start(services, before_raw)
        if before is None:
            raise HTTPException(status_code=400, detail="Choose a date.")
        when = f" from before {before_raw}"
    elif scope == "age":
        days = _age_days(form.get("days"))
        if days is None:
            raise HTTPException(status_code=400,
                                detail="Enter a whole number of days, 1 or more.")
        now = int(services.time())
        if confirmed:
            raw = str(form.get("cutoff") or "")
            before = int(raw) if raw.isascii() and raw.isdigit() and len(raw) <= 12 else None
            if before is None or before > now:
                raise HTTPException(status_code=400, detail="Review the deletion again.")
        else:
            before = now - days * DAY
        stamp = datetime.fromtimestamp(before, services.timezone()).strftime(
            "%d %b %Y, %H:%M %Z")
        when = f" older than {days} day{'s' if days != 1 else ''} (sent before {stamp})"

    busy = _busy_reason(services, chat)
    if busy:
        flash(request, busy, "error")
        return _back(chat.chat_id)
    what = {None: "messages", LIVE: "live messages", IMPORT: "imported messages"}[source]
    if not confirmed:
        count = services.messages.count_for_delete(chat.chat_id, before=before, source=source)
        return request.app.state.templates.TemplateResponse(request, "confirm.html", {
            "title": f"Delete {count} {what}{when}?",
            "message": f"This permanently deletes the bot's stored copies of {count} {what}"
                       f"{when} from {chat.display_title}; the messages in Telegram aren't "
                       "touched. It cannot be undone. Summaries and memory notes may still "
                       "contain information from these messages. Delete them separately if "
                       "needed.",
            "action": f"/chats/{chat.chat_id}/delete-messages",
            "fields": {"scope": scope, "before": before_raw, "days": days or "",
                       "cutoff": before if scope == "age" else "", "source": source or "",
                       "confirm": "yes"},
            "confirm_label": "Delete",
            "cancel": f"/chats/{chat.chat_id}",
        })

    async with services.history_locks[chat.chat_id]:
        deleted = services.messages.delete_for_chat(chat.chat_id, before=before, source=source)
        if deleted:
            services.history.note_deleted_messages(chat.chat_id, before=before, source=source)
    flash(request, f"Deleted {deleted} {what}.")
    logger.info("Deleted %s messages (%s, before=%s, source=%s) from the web admin",
                deleted, scope, before if before is not None else "-", source or "all",
                extra={"chat_id": chat.chat_id})
    return _back(chat.chat_id)
