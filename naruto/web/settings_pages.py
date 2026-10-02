"""Settings page: every setting in the registry, grouped by section."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from naruto.services import Services
from naruto.settings.registry import SECTIONS, SettingError
from naruto.settings.seed import seed_differences
from naruto.web.auth import require_admin

router = APIRouter(dependencies=[Depends(require_admin)])
ACTOR = "owner (web admin)"


def _services(request: Request) -> Services:
    return request.app.state.services


def _card(services: Services, key: str, *, error: str | None = None,
          raw: str | None = None, saved: bool = False) -> dict:
    setting = services.settings.definition(key)
    value = services.settings.get(key)
    return {
        "setting": setting,
        "value": value,
        "form_value": raw if raw is not None else setting.form_value(value),
        "is_default": services.settings.is_default(key),
        "last": services.settings.last_changed(key),
        "error": error,
        "saved": saved,
    }


@router.get("/settings")
async def settings_page(request: Request):
    services = _services(request)
    sections = []
    for section in SECTIONS:
        keys = [s.key for s in services.settings.registry.values() if s.section == section.id]
        if keys:
            sections.append({"section": section, "cards": [_card(services, k) for k in keys]})
    context = {
        "sections": sections,
        "differences": seed_differences(services.settings, services.chats, services.seed),
        "recent": services.settings.history(limit=15),
        "registry": services.settings.registry,
    }
    return request.app.state.templates.TemplateResponse(request, "settings.html", context)


def _render_card(request: Request, card: dict, status_code: int = 200):
    if request.headers.get("hx-request"):
        return request.app.state.templates.TemplateResponse(
            request, "_setting.html", {"card": card}, status_code=status_code)
    if card["error"]:
        raise HTTPException(status_code=400, detail=card["error"])
    return RedirectResponse(f"/settings#{card['setting'].key}", status_code=303)


def _known(services: Services, key: str) -> None:
    if key not in services.settings.registry:
        raise HTTPException(status_code=404, detail="Unknown setting.")


@router.post("/settings/{key}")
async def save_setting(request: Request, key: str):
    services = _services(request)
    _known(services, key)
    form = await request.form()
    raw = form.get("value")
    try:
        services.settings.set_from_form(key, raw, actor=ACTOR)
    except SettingError as exc:
        # Keep what the owner typed so they can fix it.
        return _render_card(request, _card(services, key, error=str(exc),
                                           raw=raw if isinstance(raw, str) else None), 400)
    return _render_card(request, _card(services, key, saved=True))


@router.post("/settings/{key}/reset")
async def reset_setting(request: Request, key: str):
    services = _services(request)
    _known(services, key)
    services.settings.reset(key, actor=ACTOR)
    return _render_card(request, _card(services, key, saved=True))


@router.post("/settings/{key}/revert")
async def revert_setting(request: Request, key: str):
    services = _services(request)
    _known(services, key)
    services.settings.revert(key, actor=ACTOR)
    return _render_card(request, _card(services, key, saved=True))


@router.get("/settings/{key}/history")
async def setting_history(request: Request, key: str):
    services = _services(request)
    _known(services, key)
    return request.app.state.templates.TemplateResponse(request, "_setting_history.html", {
        "setting": services.settings.definition(key),
        "entries": services.settings.history(key, limit=50),
    })
