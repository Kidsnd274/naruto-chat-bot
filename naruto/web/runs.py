"""Agent runs page: one row per bot response, with the exact prompt sent."""

from fastapi import APIRouter, Depends, HTTPException, Request

from naruto.services import Services
from naruto.web.auth import require_admin

router = APIRouter(dependencies=[Depends(require_admin)])
PAGE_SIZE = 50
STATUSES = ("ok", "empty", "error", "running")


@router.get("/runs")
async def runs_page(request: Request):
    services: Services = request.app.state.services
    params = request.query_params
    try:
        chat_id = int(params["chat"]) if params.get("chat") else None
        offset = max(int(params.get("offset") or 0), 0)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid filter.") from None
    status = params.get("status") if params.get("status") in STATUSES else None
    runs, total = services.runs.recent(chat_id=chat_id, status=status, offset=offset,
                                       limit=PAGE_SIZE)
    chats = services.chats.list_all()
    return request.app.state.templates.TemplateResponse(request, "runs.html", {
        "runs": runs,
        "total": total,
        "offset": offset,
        "limit": PAGE_SIZE,
        "filters": {"chat": chat_id, "status": status or ""},
        "statuses": STATUSES,
        "chats": chats,
        "chat_titles": {c.chat_id: c.display_title for c in chats},
    })


@router.get("/runs/{run_id}")
async def run_detail(request: Request, run_id: int):
    services: Services = request.app.state.services
    run = services.runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Unknown run.")
    chat = services.chats.get(run.chat_id)
    trigger = services.messages.get(run.trigger_row_id) if run.trigger_row_id else None
    trigger_name = None
    if trigger is not None:
        trigger_name = services.people.display_names([trigger.sender_id]).get(
            trigger.sender_id, trigger.sender_name)
    return request.app.state.templates.TemplateResponse(request, "run_detail.html", {
        "run": run, "chat": chat, "trigger": trigger, "trigger_name": trigger_name,
    })
