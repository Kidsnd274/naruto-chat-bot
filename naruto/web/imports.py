"""Import page: upload a Telegram Desktop export, preview it, choose the
group and import with a progress bar."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from starlette.datastructures import UploadFile

from naruto.db.chats import Chat
from naruto.db.imports import PREVIEW, RUNNING
from naruto.importer.service import ImportProblem, ImportService
from naruto.services import Services
from naruto.web.auth import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(require_admin)])
MULTIPART_OVERHEAD = 64 * 1024


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


def _target_choice(services: Services, importer: ImportService, record, raw: str | None):
    """(selected chat id or None for a new chat, new chat id suggestion)."""
    match = importer.match(record)
    if raw:
        if raw == "new":
            return None, match
        try:
            return int(raw), match
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid target.") from None
    return (match.chat.chat_id if match.chat else None), match


@router.get("/import/{import_id}")
async def import_detail(request: Request, import_id: int):
    services = _services(request)
    importer = _importer(request)
    record = importer.repo.get(import_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Unknown import.")
    context = {"record": record, "chat": services.chats.get(record.chat_id) if record.chat_id else None}
    if record.status == PREVIEW:
        target, match = _target_choice(services, importer, record, request.query_params.get("target"))
        context.update({
            "match": match,
            "target": target,
            "chats": services.chats.list_all(),
            "estimate": importer.estimate(record, target),
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
    target, match = _target_choice(services, importer, record, request.query_params.get("target"))
    return request.app.state.templates.TemplateResponse(request, "_import_estimate.html", {
        "record": record, "estimate": importer.estimate(record, target),
        "target": target, "match": match,
    })


@router.get("/import/{import_id}/progress")
async def import_progress(request: Request, import_id: int):
    importer = _importer(request)
    record = importer.repo.get(import_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Unknown import.")
    if record.status != RUNNING:
        return Response(status_code=204, headers={"HX-Refresh": "true"})
    return request.app.state.templates.TemplateResponse(
        request, "_import_progress.html", {"record": record})


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
    try:
        await importer.start(import_id, chat_id, identities=_identities(form))
    except ImportProblem as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
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


@router.post("/import/{import_id}/discard")
async def discard_import(request: Request, import_id: int):
    importer = _importer(request)
    try:
        importer.discard(import_id)
    except ImportProblem as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return RedirectResponse("/import", status_code=303)
