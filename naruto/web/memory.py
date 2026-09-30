"""Group memory in the web admin: the Memory page (notes), the digest and
reminders on the chat page, and deleting them."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from naruto.db.chats import Chat
from naruto.db.memory import CATEGORIES, OWNER, NoteLocked
from naruto.db.reminders import PENDING
from naruto.services import Services
from naruto.web.auth import require_admin
from naruto.web.templating import flash

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(require_admin)])
ACTOR = "owner (web admin)"


def chat_memory(services: Services, chat: Chat) -> dict:
    """Sections on the chat detail page."""
    return {
        "digest": services.digests.get(chat.chat_id),
        "unread": services.digests.unread_count(chat.chat_id,
                                                services.digests.get(chat.chat_id))[0],
        "note_count": services.notes.count(chat.chat_id),
        "reminders": services.reminders.for_chat(chat.chat_id, limit=20),
    }


def _services(request: Request) -> Services:
    return request.app.state.services


def _chat(services: Services, chat_id: int) -> Chat:
    chat = services.chats.get(chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Unknown chat.")
    return chat


def _people(services: Services, chat: Chat) -> list[tuple[int, str]]:
    """(person_id, name) for everyone in the chat, for filters and forms."""
    bot = services.status.bot
    people = {m.person_id: m.display_name for m in services.members.list(chat.chat_id)
              if bot is None or m.user_id != bot.id}
    return sorted(people.items(), key=lambda item: item[1].lower())


def _person(form, services: Services, chat: Chat) -> int | None:
    raw = str(form.get("person") or "")
    if not raw:
        return None
    if not raw.isdigit() or int(raw) not in dict(_people(services, chat)):
        raise HTTPException(status_code=400, detail="Unknown person.")
    return int(raw)


def _to_memory(chat: Chat) -> RedirectResponse:
    return RedirectResponse(f"/chats/{chat.chat_id}/memory", status_code=303)


def _to_chat(chat: Chat, anchor: str = "") -> RedirectResponse:
    return RedirectResponse(f"/chats/{chat.chat_id}{anchor}", status_code=303)


def _confirm(request: Request, *, title: str, message: str, action: str, cancel: str,
             label: str):
    return request.app.state.templates.TemplateResponse(request, "confirm.html", {
        "title": title, "message": message, "action": action, "fields": {"confirm": "yes"},
        "confirm_label": label, "cancel": cancel})


# ---------------------------------------------------------------- notes

@router.get("/chats/{chat_id}/memory")
async def memory_page(request: Request, chat_id: int):
    services = _services(request)
    chat = _chat(services, chat_id)
    params = request.query_params
    person = params.get("person") or ""
    category = params.get("category") or ""
    query = (params.get("q") or "").strip()
    notes = services.notes.for_chat(
        chat.chat_id, person_id=int(person) if person.isdigit() else None,
        category=category if category in dict(CATEGORIES) else None, query=query or None)
    people = _people(services, chat)
    names = services.people.names_by_person(n.person_id for n in notes)
    return request.app.state.templates.TemplateResponse(request, "memory.html", {
        "chat": chat,
        "notes": notes,
        "names": names,
        "history": {note.id: services.notes.history(note.id) for note in notes},
        "people": people,
        "categories": CATEGORIES,
        "filters": {"person": person, "category": category, "q": query},
        "total": services.notes.count(chat.chat_id),
        "limit": services.settings["memory.max_notes_per_chat"],
    })


@router.post("/chats/{chat_id}/memory")
async def add_note(request: Request, chat_id: int):
    services = _services(request)
    chat = _chat(services, chat_id)
    form = await request.form()
    try:
        note = services.notes.add(chat.chat_id, str(form.get("content") or ""),
                                  category=str(form.get("category") or ""),
                                  person_id=_person(form, services, chat), created_by=OWNER,
                                  actor=ACTOR)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return _to_memory(chat)
    if form.get("locked"):
        services.notes.set_locked(note.id, True, actor=ACTOR)
    flash(request, f"Added note {note.id}.")
    return _to_memory(chat)


def _note(services: Services, chat: Chat, note_id: int):
    note = services.notes.get(note_id)
    if note is None or note.chat_id != chat.chat_id:
        raise HTTPException(status_code=404, detail="Unknown note.")
    return note


@router.post("/chats/{chat_id}/memory/{note_id}")
async def edit_note(request: Request, chat_id: int, note_id: int):
    services = _services(request)
    chat = _chat(services, chat_id)
    note = _note(services, chat, note_id)
    form = await request.form()
    try:
        services.notes.update(note.id, content=str(form.get("content") or ""),
                              category=str(form.get("category") or note.category),
                              person_id=_person(form, services, chat), actor=ACTOR, force=True)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return _to_memory(chat)
    flash(request, f"Saved note {note.id}.")
    return _to_memory(chat)


@router.post("/chats/{chat_id}/memory/{note_id}/lock")
async def lock_note(request: Request, chat_id: int, note_id: int):
    services = _services(request)
    chat = _chat(services, chat_id)
    note = _note(services, chat, note_id)
    note = services.notes.set_locked(note.id, not note.locked, actor=ACTOR)
    flash(request, f"Note {note.id} is {'locked: the bot and members can’t change it' if note.locked else 'unlocked'}.")
    return _to_memory(chat)


@router.post("/chats/{chat_id}/memory/{note_id}/delete")
async def delete_note(request: Request, chat_id: int, note_id: int):
    services = _services(request)
    chat = _chat(services, chat_id)
    note = _note(services, chat, note_id)
    form = await request.form()
    if form.get("confirm") != "yes":
        return _confirm(request, title=f"Delete note {note.id}?",
                        message=f"“{note.content}” is removed from {chat.display_title}'s "
                                "memory. Its history keeps a copy.",
                        action=f"/chats/{chat.chat_id}/memory/{note.id}/delete",
                        cancel=f"/chats/{chat.chat_id}/memory", label="Delete note")
    try:
        services.notes.delete(note.id, actor=ACTOR, force=True)
    except NoteLocked:  # pragma: no cover - force always succeeds
        pass
    flash(request, f"Deleted note {note.id}.")
    return _to_memory(chat)


@router.post("/chats/{chat_id}/memory-clear")
async def clear_notes(request: Request, chat_id: int):
    services = _services(request)
    chat = _chat(services, chat_id)
    form = await request.form()
    count = services.notes.count(chat.chat_id)
    if form.get("confirm") != "yes":
        return _confirm(request, title=f"Delete all {count} memory notes?",
                        message=f"This permanently deletes every note in {chat.display_title}'s "
                                "memory, locked ones included, and their history. It cannot be "
                                "undone.",
                        action=f"/chats/{chat.chat_id}/memory-clear",
                        cancel=f"/chats/{chat.chat_id}", label="Delete all notes")
    deleted = services.notes.delete_for_chat(chat.chat_id)
    logger.info("Deleted %s memory notes from the web admin", deleted,
                extra={"chat_id": chat.chat_id})
    flash(request, f"Deleted {deleted} memory notes.")
    return _to_chat(chat)


# --------------------------------------------------------------- digest

@router.post("/chats/{chat_id}/digest")
async def save_digest(request: Request, chat_id: int):
    services = _services(request)
    chat = _chat(services, chat_id)
    form = await request.form()
    text = str(form.get("text") or "").replace("\r\n", "\n").strip()
    services.digests.save(chat.chat_id, text, actor=ACTOR)
    flash(request, "Digest saved.")
    return _to_chat(chat, "#digest")


@router.post("/chats/{chat_id}/digest/update")
async def update_digest(request: Request, chat_id: int):
    services = _services(request)
    chat = _chat(services, chat_id)
    if services.keeper is None:
        flash(request, "Memory upkeep isn't running.", "error")
    elif not chat.enabled:
        flash(request, "The chat isn't enabled, so its digest isn't kept up to date.", "warn")
    else:
        services.keeper.request_update(chat.chat_id)
        flash(request, "Queued: the digest updates within a minute, after any replies to people.")
    return _to_chat(chat, "#digest")


@router.post("/chats/{chat_id}/digest/clear")
async def clear_digest(request: Request, chat_id: int):
    services = _services(request)
    chat = _chat(services, chat_id)
    form = await request.form()
    if form.get("confirm") != "yes":
        return _confirm(request, title="Clear the digest?",
                        message="The digest is deleted and rebuilt from the stored messages the "
                                "next time it updates.",
                        action=f"/chats/{chat.chat_id}/digest/clear",
                        cancel=f"/chats/{chat.chat_id}#digest", label="Clear digest")
    services.digests.clear(chat.chat_id)
    flash(request, "Digest cleared.")
    return _to_chat(chat, "#digest")


# ------------------------------------------------------------ reminders

@router.post("/chats/{chat_id}/reminders/{reminder_id}/cancel")
async def cancel_reminder(request: Request, chat_id: int, reminder_id: int):
    services = _services(request)
    chat = _chat(services, chat_id)
    reminder = services.reminders.get(reminder_id)
    if reminder is None or reminder.chat_id != chat.chat_id:
        raise HTTPException(status_code=404, detail="Unknown reminder.")
    if reminder.status == PENDING and services.reminders.cancel(reminder.id):
        flash(request, f"Cancelled reminder {reminder.id}.")
    else:
        flash(request, f"Reminder {reminder.id} is already {reminder.status}.", "warn")
    return _to_chat(chat, "#reminders")
