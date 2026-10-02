"""Settings for one chat: overrides of the settings marked per_chat in the
registry (a different persona for one group, fewer digest updates in a busy
one...). Anything not overridden follows the global Settings page."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from naruto.db.chats import Chat
from naruto.services import Services
from naruto.settings.registry import SECTIONS, SettingError
from naruto.web.auth import require_admin
from naruto.web.templating import flash

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(require_admin)])
ACTOR = "owner (web admin)"


def _services(request: Request) -> Services:
    return request.app.state.services


def _chat(services: Services, chat_id: int) -> Chat:
    chat = services.chats.get(chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Unknown chat.")
    return chat


def _per_chat_key(services: Services, key: str) -> None:
    setting = services.settings.registry.get(key)
    if setting is None or not setting.per_chat:
        raise HTTPException(status_code=404, detail="That setting can't be set per chat.")


def _back(chat: Chat, key: str = "") -> RedirectResponse:
    anchor = f"#{key}" if key else ""
    return RedirectResponse(f"/chats/{chat.chat_id}/settings{anchor}", status_code=303)


@router.get("/chats/{chat_id}/settings")
async def chat_settings_page(request: Request, chat_id: int):
    services = _services(request)
    chat = _chat(services, chat_id)
    overrides = services.settings.chat_overrides(chat.chat_id)
    sections = []
    for section in SECTIONS:
        cards = []
        for setting in services.settings.registry.values():
            if setting.section != section.id or not setting.per_chat:
                continue
            override = overrides.get(setting.key)
            global_value = services.settings.get(setting.key)
            cards.append({
                "setting": setting,
                "override": override,
                "global_display": setting.display(global_value),
                "value": override.value if override else global_value,
                "form_value": setting.form_value(override.value if override else global_value),
            })
        if cards:
            sections.append({"section": section, "cards": cards})
    return request.app.state.templates.TemplateResponse(request, "chat_settings.html", {
        "chat": chat, "sections": sections, "override_count": len(overrides)})


@router.post("/chats/{chat_id}/settings/{key}")
async def save_chat_setting(request: Request, chat_id: int, key: str):
    services = _services(request)
    chat = _chat(services, chat_id)
    _per_chat_key(services, key)
    form = await request.form()
    label = services.settings.definition(key).label
    try:
        services.settings.set_for_chat_from_form(chat.chat_id, key, form.get("value"),
                                                 actor=ACTOR)
    except SettingError as exc:
        flash(request, f"{label}: {exc}", "error")
        return _back(chat, key)
    flash(request, f"{label} is now set for {chat.display_title} only.")
    return _back(chat, key)


@router.post("/chats/{chat_id}/settings/{key}/reset")
async def reset_chat_setting(request: Request, chat_id: int, key: str):
    services = _services(request)
    chat = _chat(services, chat_id)
    _per_chat_key(services, key)
    if services.settings.reset_for_chat(chat.chat_id, key, actor=ACTOR):
        flash(request, f"{services.settings.definition(key).label} follows the global "
                       "setting again.")
    return _back(chat, key)
