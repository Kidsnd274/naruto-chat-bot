"""Lab pages in the web admin: runs and the API tokens external agents use."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from naruto.lab import activation, preferences
from naruto.lab.errors import LabError
from naruto.lab.report import build_report, render_markdown
from naruto.lab.service import LabService
from naruto.services import Services
from naruto.web.auth import require_admin
from naruto.web.templating import flash

router = APIRouter(dependencies=[Depends(require_admin)])
OWNER = "owner (web admin)"


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
        "activations": [activation.view(lab, a) for a in lab.repo.activations()],
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


# ------------------------------------------------------------------- runs

def _run_or_404(lab: LabService, run_id: int):
    run = lab.repo.run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Unknown lab run.")
    return run


@router.get("/lab/runs/{run_id}")
async def run_page(request: Request, run_id: int):
    lab = _lab(request)
    run = _run_or_404(lab, run_id)
    report = build_report(lab, run)
    comparisons = [preferences.view(lab, c) for c in lab.repo.comparisons(run.id)]
    return request.app.state.templates.TemplateResponse(request, "lab_run.html", {
        "run": run, "state": lab.run_state(run), "report": report,
        "report_md": render_markdown(report),
        "candidates": [lab.candidate_view(run, c) for c in lab.repo.candidates(run.id)],
        "baseline": lab.candidate_view(run, None),
        "pending": [c for c in comparisons if c["status"] == "pending"],
        "answered": [c for c in comparisons if c["status"] != "pending"],
        "choices": preferences.CHOICES,
        "preferences": lab.repo.preferences(run.id),
        "activations": [activation.view(lab, a) for a in lab.repo.activations(run_id=run.id)],
        "attempts": [lab.attempt_summary(a) for a in lab.repo.attempts(run_id=run.id)],
    })


@router.get("/lab/attempts/{attempt_id}")
async def attempt_page(request: Request, attempt_id: int):
    lab = _lab(request)
    attempt = lab.repo.attempt(attempt_id)
    if attempt is None:
        raise HTTPException(status_code=404, detail="Unknown attempt.")
    run = lab.repo.run(attempt.run_id)
    hidden = attempt.id in {int(a) for c in lab.repo.comparisons(run.id, status="pending")
                            for a in c.mapping.values()}
    return request.app.state.templates.TemplateResponse(request, "lab_attempt.html", {
        "run": run, "attempt": attempt, "summary": lab.attempt_summary(attempt),
        "hidden": hidden and request.query_params.get("show") != "1",
        "scenario": lab.repo.scenario(attempt.scenario_id),
    })


@router.post("/lab/comparisons/{comparison_id}/answer")
async def answer_comparison(request: Request, comparison_id: int):
    lab = _lab(request)
    form = await request.form()
    comparison = preferences.get(lab, comparison_id)
    action = preferences.answer if comparison.status == "pending" else preferences.correct
    try:
        action(lab, comparison_id, str(form.get("choice") or ""),
               comment=str(form.get("comment") or ""), channel=OWNER)
    except LabError as exc:
        flash(request, str(exc), "error")
    else:
        flash(request, "Choice saved. The agent sees it the next time it checks the run.")
    return RedirectResponse(f"/lab/runs/{comparison.run_id}#comparisons", status_code=303)


@router.post("/lab/runs/{run_id}/preferences")
async def correct_preferences(request: Request, run_id: int):
    lab = _lab(request)
    _run_or_404(lab, run_id)
    form = await request.form()
    text = str(form.get("correction") or "").strip()
    if text:
        preferences.add_owner_correction(lab, run_id, text, edited_by=OWNER)
        flash(request, "Your correction is in the preference summary.")
    return RedirectResponse(f"/lab/runs/{run_id}#preferences", status_code=303)


@router.post("/lab/runs/{run_id}/stop")
async def stop_run(request: Request, run_id: int):
    lab = _lab(request)
    try:
        lab.stop_run(run_id, reason="cancelled", note=None, actor=OWNER)
    except LabError as exc:
        flash(request, str(exc), "error")
    else:
        flash(request, "Run stopped. Its results so far stay here.")
    return RedirectResponse(f"/lab/runs/{run_id}", status_code=303)


# ------------------------------------------------------------ activation

@router.get("/lab/runs/{run_id}/activate/{ref}")
async def activation_page(request: Request, run_id: int, ref: str):
    lab = _lab(request)
    run = _run_or_404(lab, run_id)
    mode = request.query_params.get("mode", "changes")
    try:
        candidate = lab.get_candidate(run, ref)
        steps = activation.plan(lab, run, candidate, mode=mode)
    except LabError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(f"/lab/runs/{run_id}", status_code=303)
    return request.app.state.templates.TemplateResponse(request, "lab_activate.html", {
        "run": run, "ref": ref, "plan": steps, "mode": mode,
    })


@router.post("/lab/runs/{run_id}/activate/{ref}")
async def activate_candidate(request: Request, run_id: int, ref: str):
    lab = _lab(request)
    form = await request.form()
    try:
        result = activation.activate(
            lab, run_id, ref, mode=str(form.get("mode") or "changes"),
            authorized_by=OWNER, acknowledge_drift=str(form.get("acknowledge_drift") or ""),
            actor=OWNER)
    except LabError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(f"/lab/runs/{run_id}/activate/{ref}", status_code=303)
    flash(request, f"Activated: {', '.join(sorted(result.applied))} changed. You can revert it "
                   "below.")
    return RedirectResponse(f"/lab/runs/{run_id}#activations", status_code=303)


@router.post("/lab/activations/{activation_id}/revert")
async def revert_activation(request: Request, activation_id: int):
    lab = _lab(request)
    try:
        result = activation.revert(lab, activation_id, actor=OWNER)
    except LabError as exc:
        flash(request, str(exc), "error")
        record = lab.repo.activation(activation_id)
        return RedirectResponse(f"/lab/runs/{record.run_id}" if record else "/lab",
                                status_code=303)
    flash(request, "Reverted: the settings are back to what they were before.")
    return RedirectResponse(f"/lab/runs/{result.run_id}#activations", status_code=303)
