"""Logs page: stored application logs with level, chat and logger filters,
and an optional live tail."""

import logging

from fastapi import APIRouter, Depends, Request

from naruto.services import Services
from naruto.web.auth import require_admin

router = APIRouter(dependencies=[Depends(require_admin)])
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def _filters(request: Request) -> dict:
    params = request.query_params
    level = params.get("level") if params.get("level") in LEVELS else "INFO"
    try:
        chat_id = int(params["chat"]) if params.get("chat") else None
    except ValueError:
        chat_id = None
    return {
        "level": level,
        "chat": chat_id,
        "logger": (params.get("logger") or "").strip(),
        "live": params.get("live") == "1",
    }


def _rows(services: Services, filters: dict):
    return services.logs.query(
        min_level=getattr(logging, filters["level"]),
        chat_id=filters["chat"],
        logger_prefix=filters["logger"] or None,
        limit=300,
    )


@router.get("/logs")
async def logs_page(request: Request):
    services: Services = request.app.state.services
    filters = _filters(request)
    chat_titles = {chat.chat_id: chat.display_title for chat in services.chats.list_all()}
    return request.app.state.templates.TemplateResponse(request, "logs.html", {
        "filters": filters,
        "levels": LEVELS,
        "rows": _rows(services, filters),
        "chat_ids": services.logs.chat_ids(),
        "chat_titles": chat_titles,
        "query": request.url.query,
    })


@router.get("/logs/rows")
async def log_rows(request: Request):
    services: Services = request.app.state.services
    filters = _filters(request)
    return request.app.state.templates.TemplateResponse(
        request, "_log_rows.html", {"rows": _rows(services, filters)})
