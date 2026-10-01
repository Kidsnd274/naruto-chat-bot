"""Import page: upload a Telegram Desktop export, choose which messages to
import and which dates to summarize into history digests, and follow the
stages (with pause, resume and cancel)."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from starlette.datastructures import UploadFile

from naruto.db.chats import Chat
from naruto.db.history import GROUPINGS
from naruto.db.imports import PAUSED, PREVIEW, RUNNING
from naruto.importer.planning import ImportOptions
from naruto.importer.service import ImportProblem, ImportService
from naruto.services import Services
from naruto.web.auth import require_admin
from naruto.web.templating import flash

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(require_admin)])
MULTIPART_OVERHEAD = 64 * 1024
GROUPING_LABELS = {"month": "Monthly", "week": "Weekly", "range": "One summary for the dates"}


def _services(request: Request) -> Services:
    return request.app.state.services


def _importer(request: Request) -> ImportService:
    importer = _services(request).imports
    if importer is None:
        raise HTTPException(status_code=503, detail="Imports are not available.")
    return importer


def _redirect(request: Request, url: str) -> Response:
    if request.headers.get("hx-request"):
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


def chat_imports(services: Services, chat: Chat) -> dict:
    """Section on the chat detail page."""
    if services.imports is None:
        return {}
    return {"imports": services.imports.repo.for_chat(chat.chat_id)}


def _upload_page(request: Request, *, error: str | None = None, status_code: int = 200):
    services = _services(request)
    importer = _importer(request)
    return request.app.state.templates.TemplateResponse(request, "import.html", {
        "records": importer.repo.recent(),
        "max_mb": services.settings["import.max_upload_mb"],
        "retention_days": services.settings["retention.imported_messages_days"],
        "chat_titles": {c.chat_id: c.display_title for c in services.chats.list_all()},
        "error": error,
    }, status_code=status_code)


@router.get("/import")
async def import_page(request: Request):
    return _upload_page(request)


@router.post("/import")
async def upload(request: Request):
    importer = _importer(request)
    max_bytes = _services(request).settings["import.max_upload_mb"] * 1024 * 1024
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > max_bytes + MULTIPART_OVERHEAD:
        return _upload_page(request, status_code=413, error=(
            f"The file is larger than the {max_bytes // (1024 * 1024)} MB limit (Settings → Import)."))
    form = await request.form(max_files=1, max_fields=5)
    file = form.get("file")
    if not isinstance(file, UploadFile) or not file.filename:
        return _upload_page(request, status_code=400, error="Choose a result.json file.")
    try:
        record = await importer.create_from_upload(file.file, file.filename)
    except ImportProblem as exc:
        return _upload_page(request, status_code=400, error=str(exc))
    finally:
        await file.close()
    return _redirect(request, f"/import/{record.id}")


def _target_choice(importer: ImportService, record, raw: str | None):
    """(selected chat id or None for a new chat, the match)."""
    match = importer.match(record)
    if raw:
        if raw == "new":
            return None, match
        try:
            return int(raw), match
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid target.") from None
    return (match.chat.chat_id if match.chat else None), match


def _plan_context(services: Services, importer: ImportService, record, form) -> dict:
    target, match = _target_choice(importer, record, form.get("target"))
    options = ImportOptions.from_form(form, importer.default_options(record))
    plan_chat = target if target is not None else match.suggested_chat_id
    plan = importer.plan(record, plan_chat, options)
    busy = importer.busy_import(services.chats.resolve(plan_chat)) \
        if plan_chat is not None else None
    return {"record": record, "target": target, "match": match, "options": options,
            "plan": plan, "busy": busy, "groupings": GROUPINGS,
            "grouping_labels": GROUPING_LABELS,
            "timezone": services.settings["general.timezone"] or "the server's time zone"}


@router.get("/import/{import_id}")
async def import_detail(request: Request, import_id: int):
    services = _services(request)
    importer = _importer(request)
    record = importer.repo.get(import_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Unknown import.")
    context = {"record": record, "grouping_labels": GROUPING_LABELS,
               "chat": services.chats.get(record.chat_id) if record.chat_id else None,
               "periods": services.history.periods_for_import(import_id),
               "tz": services.timezone()}
    if record.status == PREVIEW:
        record = await importer.refresh_preview(record)
        context.update(_plan_context(services, importer, record, request.query_params))
        context.update({
            "chats": services.chats.list_all(),
            "identities": importer.identity_rows(record),
            "people": sorted((s.person for s in services.people.all()),
                             key=lambda p: p.display_name.lower()),
        })
    return request.app.state.templates.TemplateResponse(request, "import_detail.html", context)


@router.get("/import/{import_id}/estimate")
async def import_estimate(request: Request, import_id: int):
    services = _services(request)
    importer = _importer(request)
    record = importer.repo.get(import_id)
    if record is None or record.status != PREVIEW:
        raise HTTPException(status_code=404, detail="Unknown import.")
    record = await importer.refresh_preview(record)
    return request.app.state.templates.TemplateResponse(
        request, "_import_estimate.html",
        _plan_context(services, importer, record, request.query_params))


@router.get("/import/{import_id}/progress")
async def import_progress(request: Request, import_id: int):
    services = _services(request)
    importer = _importer(request)
    record = importer.repo.get(import_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Unknown import.")
    if record.status != RUNNING:
        return Response(status_code=204, headers={"HX-Refresh": "true"})
    return request.app.state.templates.TemplateResponse(request, "_import_stages.html", {
        "record": record, "periods": services.history.periods_for_import(import_id),
        "tz": services.timezone(), "chat": None,
    })


@router.post("/import/{import_id}/start")
async def start_import(request: Request, import_id: int):
    importer = _importer(request)
    record = importer.repo.get(import_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Unknown import.")
    form = await request.form()
    raw = form.get("target") or ""
    if raw == "new":
        match = importer.match(record)
        chat_id = match.suggested_chat_id
        if chat_id is None:
            raise HTTPException(status_code=400, detail="The export has no chat ID; choose a group.")
    else:
        try:
            chat_id = int(raw)
        except ValueError:
            raise HTTPException(status_code=400, detail="Choose a group.") from None
    options = ImportOptions.from_form(form, importer.default_options(record))
    try:
        await importer.start(import_id, chat_id, identities=_identities(form), options=options)
    except ImportProblem as exc:
        flash(request, f"Not started: {exc}", "error")
    return RedirectResponse(f"/import/{import_id}", status_code=303)


def _identities(form) -> dict[int, dict]:
    """``name-<user id>`` and ``merge-<user id>`` fields from the preview."""
    identities: dict[int, dict] = {}
    for key, value in form.multi_items():
        field, _, raw_id = key.partition("-")
        if field not in ("name", "merge") or not raw_id.lstrip("-").isdigit():
            continue
        choice = identities.setdefault(int(raw_id), {"name": "", "merge_into": None})
        if field == "name":
            choice["name"] = str(value).strip()
        elif str(value).isdigit():
            choice["merge_into"] = int(value)
    return identities


@router.post("/import/{import_id}/pause")
async def pause_import(request: Request, import_id: int):
    try:
        _importer(request).pause(import_id)
        flash(request, "Pausing after the current step. The file is kept so it can resume.")
    except ImportProblem as exc:
        flash(request, str(exc), "error")
    return RedirectResponse(f"/import/{import_id}", status_code=303)


@router.post("/import/{import_id}/resume")
async def resume_import(request: Request, import_id: int):
    try:
        _importer(request).resume(import_id)
        flash(request, "Resumed where it stopped.")
    except ImportProblem as exc:
        flash(request, str(exc), "error")
    return RedirectResponse(f"/import/{import_id}", status_code=303)


@router.post("/import/{import_id}/cancel")
async def cancel_import(request: Request, import_id: int):
    importer = _importer(request)
    record = importer.repo.get(import_id)
    if record is None or record.status not in (RUNNING, PAUSED):
        raise HTTPException(status_code=409, detail="Only a running or paused import can be "
                                                    "cancelled.")
    form = await request.form()
    if form.get("confirm") != "yes":
        return request.app.state.templates.TemplateResponse(request, "confirm.html", {
            "title": f"Cancel the rest of import #{import_id}?",
            "message": "Unfinished stages stop for good and the uploaded file is deleted. "
                       "Messages already imported and finished summaries stay; summaries "
                       "waiting to replace older ones are dropped (the older ones stay).",
            "action": f"/import/{import_id}/cancel",
            "fields": {"confirm": "yes"},
            "confirm_label": "Cancel the rest",
            "cancel": f"/import/{import_id}",
        })
    try:
        flash(request, importer.cancel_unfinished(import_id))
    except ImportProblem as exc:
        flash(request, str(exc), "error")
    return RedirectResponse(f"/import/{import_id}", status_code=303)


@router.post("/import/{import_id}/discard")
async def discard_import(request: Request, import_id: int):
    importer = _importer(request)
    try:
        importer.discard(import_id)
    except ImportProblem as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return RedirectResponse("/import", status_code=303)
