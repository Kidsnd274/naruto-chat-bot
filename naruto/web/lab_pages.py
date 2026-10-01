"""Lab pages in the web admin: runs and the API tokens external agents use."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from naruto.lab.service import LabService
from naruto.services import Services
from naruto.web.auth import require_admin
from naruto.web.templating import flash

router = APIRouter(dependencies=[Depends(require_admin)])


def _lab(request: Request) -> LabService:
    lab = request.app.state.services.lab
    if lab is None:
        raise HTTPException(status_code=503, detail="The lab isn't running.")
    return lab


def _chat_ids(form) -> list[int]:
    ids = []
    for value in form.getlist("chats"):
        try:
            ids.append(int(value))
        except ValueError:
            continue
    return ids


def _page(request: Request, *, new_secret: str | None = None, new_token=None):
    services: Services = request.app.state.services
    lab = _lab(request)
    runs = lab.repo.runs()
    return request.app.state.templates.TemplateResponse(request, "lab.html", {
        "runs": [(run, lab.run_state(run)) for run in runs],
        "tokens": lab.repo.tokens(),
        "chats": services.chats.list_all(),
        "chat_titles": {c.chat_id: c.display_title for c in services.chats.list_all()},
        "new_secret": new_secret,
        "new_token": new_token,
    })


@router.get("/lab")
async def lab_page(request: Request):
    return _page(request)


@router.post("/lab/tokens")
async def create_token(request: Request):
    form = await request.form()
    token, secret = _lab(request).create_token(
        str(form.get("name") or ""), chats=_chat_ids(form),
        may_activate=form.get("may_activate") == "on")
    # The secret is shown on this response only; it isn't stored anywhere.
    return _page(request, new_secret=secret, new_token=token)


@router.post("/lab/tokens/{token_id}")
async def update_token(request: Request, token_id: int):
    lab = _lab(request)
    if lab.repo.token(token_id) is None:
        raise HTTPException(status_code=404, detail="Unknown token.")
    form = await request.form()
    lab.set_token_access(token_id, chats=_chat_ids(form),
                         may_activate=form.get("may_activate") == "on")
    flash(request, "Token access saved.")
    return RedirectResponse("/lab#tokens", status_code=303)


@router.post("/lab/tokens/{token_id}/revoke")
async def revoke_token(request: Request, token_id: int):
    lab = _lab(request)
    if lab.repo.token(token_id) is None:
        raise HTTPException(status_code=404, detail="Unknown token.")
    lab.revoke_token(token_id)
    flash(request, "Token revoked: agents using it are refused from now on.")
    return RedirectResponse("/lab#tokens", status_code=303)
