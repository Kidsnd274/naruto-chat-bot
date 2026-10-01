"""Model queue page: what the model server is working on and what waits,
the capacity limits, pausing background work and cancelling waiting
requests."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from naruto.model_queue import BACKGROUND, FOREGROUND, PRIORITIES
from naruto.services import Services
from naruto.settings.registry import SettingError
from naruto.web.auth import require_admin
from naruto.web.templating import flash

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(require_admin)])
ACTOR = "owner (web admin)"
HISTORY_SIZE = 100
STATES = ("done", "failed", "cancelled", "expired", "interrupted", "queued", "running",
          "retrying")
LIMIT_KEYS = ("model.parallel_requests", "model.background_requests",
              "model.foreground_reserved")


def _services(request: Request) -> Services:
    return request.app.state.services


def live_context(services: Services) -> dict:
    """The part of the page that refreshes every few seconds."""
    snapshot = services.llm.queue.snapshot()
    return {
        "snapshot": snapshot,
        "foreground": FOREGROUND,
        "background": BACKGROUND,
        "chat_titles": {c.chat_id: c.display_title for c in services.chats.list_all()},
    }


@router.get("/queue")
async def queue_page(request: Request):
    services = _services(request)
    params = request.query_params
    try:
        chat_id = int(params["chat"]) if params.get("chat") else None
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid filter.") from None
    filters = {
        "chat": chat_id,
        "task": params.get("task") or "",
        "priority": params.get("priority") if params.get("priority") in PRIORITIES else "",
        "state": params.get("state") if params.get("state") in STATES else "",
    }
    history = services.requests.recent(chat_id=chat_id, task=filters["task"] or None,
                                       priority=filters["priority"] or None,
                                       state=filters["state"] or None, limit=HISTORY_SIZE)
    return request.app.state.templates.TemplateResponse(request, "queue.html", {
        **live_context(services),
        "history": history,
        "filters": filters,
        "tasks": services.requests.tasks(),
        "states": STATES,
        "priorities": PRIORITIES,
        "chats": services.chats.list_all(),
        "settings": {key: services.settings[key] for key in LIMIT_KEYS},
    })


@router.get("/partials/queue")
async def queue_partial(request: Request):
    return request.app.state.templates.TemplateResponse(
        request, "_queue_live.html", live_context(_services(request)))


@router.post("/queue/limits")
async def save_limits(request: Request):
    services = _services(request)
    form = await request.form()
    errors = []
    for key in LIMIT_KEYS:
        if key not in form:
            continue
        try:
            services.settings.set_from_form(key, str(form.get(key)), actor=ACTOR)
        except SettingError as exc:
            errors.append(f"{services.settings.definition(key).label}: {exc}")
    if errors:
        flash(request, "Not saved: " + " ".join(errors), "error")
    else:
        flash(request, "Limits saved. They apply to waiting requests straight away.")
    return RedirectResponse("/queue", status_code=303)


@router.post("/queue/pause")
async def pause(request: Request):
    _services(request).settings.set("model.background_paused", True, actor=ACTOR)
    flash(request, "Background work paused. Running requests finish; replies are not affected.")
    return RedirectResponse("/queue", status_code=303)


@router.post("/queue/resume")
async def resume(request: Request):
    _services(request).settings.set("model.background_paused", False, actor=ACTOR)
    flash(request, "Background work resumed.")
    return RedirectResponse("/queue", status_code=303)


@router.post("/queue/{request_id}/cancel")
async def cancel(request: Request, request_id: int):
    if _services(request).llm.queue.cancel(request_id):
        flash(request, f"Request {request_id} cancelled.")
    else:
        flash(request, f"Request {request_id} isn't waiting any more (running requests finish "
                       "on their own).", "warn")
    if request.headers.get("hx-request"):
        return request.app.state.templates.TemplateResponse(
            request, "_queue_live.html", live_context(_services(request)))
    return RedirectResponse("/queue", status_code=303)
