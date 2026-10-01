"""Dashboard."""

from collections import Counter

from fastapi import APIRouter, Depends, Request

from naruto.health import check_model
from naruto.services import Services
from naruto.settings.seed import seed_differences
from naruto.web.auth import require_admin

router = APIRouter(dependencies=[Depends(require_admin)])


def _status_context(services: Services) -> dict:
    return {
        "llm": services.llm,
        "endpoint": services.settings["model.endpoint_url"],
        "model_name": services.settings["model.name"],
        "settings_paused": services.settings["model.background_paused"],
    }


@router.get("/")
async def dashboard(request: Request):
    services: Services = request.app.state.services
    chats = services.chats.list_all()
    counts = Counter(chat.status for chat in chats)
    context = {
        **_status_context(services),
        "counts": counts,
        "pending": [chat for chat in chats if chat.status == "pending"],
        "errors": services.logs.recent_errors(10),
        "drift": len(seed_differences(services.settings, services.chats, services.seed)),
        "lab_waiting": ([run.id for run in services.lab.repo.runs(20) if run.status == "active"
                         and services.lab.repo.comparisons(run.id, status="pending")]
                        if services.lab is not None else []),
    }
    return request.app.state.templates.TemplateResponse(request, "dashboard.html", context)


@router.get("/partials/status")
async def status_partial(request: Request):
    services: Services = request.app.state.services
    return request.app.state.templates.TemplateResponse(
        request, "_status.html", _status_context(services))


@router.post("/model/check")
async def model_check(request: Request):
    services: Services = request.app.state.services
    await check_model(services)
    return request.app.state.templates.TemplateResponse(
        request, "_status.html", _status_context(services))
