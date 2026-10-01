"""The prompt lab's JSON API for external agents, under /api/lab/v1.

Authenticated with a bearer token the owner creates on the Lab page (no
session, so no CSRF token: browsers never send it on their own). Every
response is JSON; errors are {"error": code, "message": ..., "details": ...}.
docs/LAB.md describes each endpoint, and `python3 -m naruto.lab` wraps them.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from naruto.db.lab import LabRun, LabToken
from naruto.lab import config
from naruto.lab.capabilities import capabilities
from naruto.lab.export import export_files, folder_name
from naruto.lab.service import LabError, LabService

PREFIX = "/api/lab/v1"
MAX_WAIT_SECONDS = 300
STATUS = {"invalid": 400, "unauthorized": 401, "forbidden": 403, "not_found": 404,
          "conflict": 409}


def lab_error(request: Request, exc: LabError) -> JSONResponse:
    return JSONResponse({"error": exc.code, "message": str(exc), "details": exc.details},
                        status_code=STATUS.get(exc.code, 400))


def _lab(request: Request) -> LabService:
    lab = request.app.state.services.lab
    if lab is None:
        raise LabError("The lab isn't running.", "conflict")
    return lab


async def lab_token(request: Request) -> LabToken:
    header = request.headers.get("authorization") or ""
    scheme, _, secret = header.partition(" ")
    token = _lab(request).authenticate(secret.strip() if scheme.lower() == "bearer" else None)
    if token is None:
        raise LabError("A valid lab API token is required (Authorization: Bearer nlab_...). "
                       "The owner creates tokens on the web admin's Lab page.", "unauthorized")
    request.state.lab_token = token
    return token


router = APIRouter(prefix=PREFIX, dependencies=[Depends(lab_token)])


def _actor(token: LabToken) -> str:
    return f"agent:{token.name}"


async def _body(request: Request) -> dict:
    try:
        data = await request.json()
    except ValueError:
        raise LabError("The request body must be JSON.") from None
    if not isinstance(data, dict):
        raise LabError("The request body must be a JSON object.")
    return data


def _check_chat(token: LabToken, chat_id: int | None) -> None:
    """Real chat content only for chats the owner allowed this token."""
    if chat_id is not None and chat_id not in token.chats:
        raise LabError(f"This comes from chat {chat_id}, which this token may not read.",
                       "forbidden")


def _run_view(lab: LabService, run: LabRun) -> dict:
    spec = {k: v for k, v in run.spec.items() if k != "deadline_at"}
    return {
        "id": run.id, "slug": run.slug, "objective": run.objective, "spec": spec,
        "model": {"endpoint": config.redact_endpoint(run.model_endpoint),
                  "name": run.model_name or "(first listed)", "reported": run.model_reported},
        "status": run.status, "stop_reason": run.stop_reason,
        "recommendation": run.recommendation, "summary": run.summary,
        "created_by": run.created_by, "created_at": run.created_at,
        "finished_at": run.finished_at, "code_fingerprint": run.code_fingerprint,
        "state": lab.run_state(run),
        "tunable": lab.tunable(run),
        "candidates": [{"id": c.id, "label": c.label,
                        "parent": lab.label(run, c.parent_id) if c.parent_id else "baseline",
                        "changes": sorted(c.changes), "hypothesis": c.hypothesis}
                       for c in lab.repo.candidates(run.id)],
        "sets": [lab.set_view(s) for s in lab.repo.sets(run.id)],
        "batches": [{"id": b.id, "status": b.status, "spec": b.spec}
                    for b in lab.repo.batches(run.id)],
        "export_folder": folder_name(run),
    }


# ------------------------------------------------------------- discovery

@router.get("/capabilities")
async def get_capabilities(request: Request):
    return capabilities(_lab(request))


@router.get("/config/active")
async def active_config(request: Request):
    lab = _lab(request)
    settings = request.app.state.services.settings
    keys = [row["key"] for row in capabilities(lab)["settings"]]
    values = {key: settings.get(key) for key in keys}
    return {"settings": values, "settings_hash": config.settings_hash(values),
            "model": {"endpoint": config.redact_endpoint(settings["model.endpoint_url"]),
                      "name": settings["model.name"]},
            "per_chat_overrides": lab.overriding_chats(keys)}


# ------------------------------------------------------------------ runs

@router.post("/runs")
async def create_run(request: Request):
    lab, token = _lab(request), request.state.lab_token
    run = lab.start_run(await _body(request), created_by=_actor(token), token=token)
    return JSONResponse(_run_view(lab, run), status_code=201)


@router.get("/runs")
async def list_runs(request: Request):
    lab = _lab(request)
    return {"runs": [{"id": r.id, "slug": r.slug, "objective": r.objective[:200],
                      "status": r.status, "stop_reason": r.stop_reason,
                      "state": lab.run_state(r)["state"], "created_at": r.created_at}
                     for r in lab.repo.runs()]}


@router.get("/runs/{run_id}")
async def get_run(request: Request, run_id: int):
    lab = _lab(request)
    return _run_view(lab, lab.get_run(run_id))


@router.post("/runs/{run_id}/stop")
async def stop_run(request: Request, run_id: int):
    lab, data = _lab(request), await _body(request)
    run = lab.stop_run(run_id, reason=data.get("reason", "cancelled"), note=data.get("note"),
                       actor=_actor(request.state.lab_token))
    return _run_view(lab, run)


@router.post("/runs/{run_id}/finish")
async def finish_run(request: Request, run_id: int):
    lab, data = _lab(request), await _body(request)
    run = lab.finish_run(run_id, reason=data.get("reason", "objective_met"),
                         recommendation=data.get("recommendation"),
                         summary=data.get("summary"), actor=_actor(request.state.lab_token))
    return _run_view(lab, run)


@router.get("/runs/{run_id}/export")
async def export_run(request: Request, run_id: int):
    lab = _lab(request)
    run = lab.get_run(run_id)
    for attempt in lab.repo.attempts(run_id=run.id):
        record = lab.repo.scenario(attempt.scenario_id)
        _check_chat(request.state.lab_token, record.chat_id if record else None)
    return {"folder": folder_name(run), "files": export_files(lab, run)}


# ------------------------------------------------------------ candidates

@router.post("/runs/{run_id}/candidates")
async def create_candidate(request: Request, run_id: int):
    lab, data = _lab(request), await _body(request)
    common = dict(name=data.get("name", ""), parent=data.get("parent"),
                  hypothesis=data.get("hypothesis", ""), rationale=data.get("rationale", ""))
    if "files" in data:
        candidate = lab.add_candidate_from_files(run_id, files=data["files"], **common)
    else:
        candidate = lab.add_candidate(run_id, changes=data.get("changes"), **common)
    run = lab.get_run(run_id)
    return JSONResponse(lab.candidate_view(run, candidate), status_code=201)


@router.get("/runs/{run_id}/candidates/{ref}")
async def get_candidate(request: Request, run_id: int, ref: str):
    lab = _lab(request)
    run = lab.get_run(run_id)
    return lab.candidate_view(run, lab.get_candidate(run, ref))


# ------------------------------------------------------------- scenarios

@router.post("/scenarios")
async def create_scenario(request: Request):
    lab, data = _lab(request), await _body(request)
    body = data.get("scenario", data)
    record, created = lab.add_scenario(body, created_by=_actor(request.state.lab_token),
                                       reason=data.get("reason"), run_id=data.get("run"))
    return JSONResponse({"id": record.id, "slug": record.slug, "version": record.version,
                         "created": created}, status_code=201 if created else 200)


@router.get("/scenarios")
async def list_scenarios(request: Request, query: str | None = None):
    lab, token = _lab(request), request.state.lab_token
    return {"scenarios": [
        {"id": s.id, "slug": s.slug, "version": s.version, "origin": s.origin,
         "focused": bool(s.focused), "category": s.body.get("category", "other"),
         "description": s.body.get("description", "")}
        for s in lab.repo.scenarios(query=query) if s.chat_id is None or s.chat_id in token.chats]}


@router.get("/scenarios/{ref}")
async def get_scenario(request: Request, ref: str):
    lab = _lab(request)
    record = lab.get_scenario(ref)
    _check_chat(request.state.lab_token, record.chat_id)
    return {"id": record.id, "slug": record.slug, "version": record.version,
            "origin": record.origin, "focused": bool(record.focused), "reason": record.reason,
            "body": record.body,
            "versions": [{"id": v.id, "version": v.version, "reason": v.reason,
                          "created_at": v.created_at}
                         for v in lab.repo.scenario_versions(record.slug)]}


# ------------------------------------------------------------------ sets

@router.post("/runs/{run_id}/sets")
async def create_set(request: Request, run_id: int):
    lab, data = _lab(request), await _body(request)
    lab_set = lab.add_set(run_id, data.get("name", ""), data.get("purpose", ""),
                          list(data.get("scenarios") or []))
    return JSONResponse(lab.set_view(lab_set), status_code=201)


@router.post("/runs/{run_id}/sets/{ref}")
async def change_set(request: Request, run_id: int, ref: str):
    lab, data = _lab(request), await _body(request)
    lab_set = lab.change_set(run_id, ref, add=list(data.get("add") or []),
                             remove=list(data.get("remove") or []))
    return lab.set_view(lab_set)


# -------------------------------------------------------------- attempts

@router.post("/runs/{run_id}/attempts")
async def create_attempts(request: Request, run_id: int):
    lab, data = _lab(request), await _body(request)
    scenarios = list(data.get("scenarios") or [])
    if data.get("scenario") is not None:
        scenarios.append(data["scenario"])
    candidates = data.get("candidates")
    if candidates is None:
        candidates = [data.get("candidate")]
    batch = lab.submit(run_id, scenarios=scenarios, set_ref=data.get("set"),
                       candidates=candidates, repeat=data.get("repeat", 1),
                       continue_from=data.get("continue_from"),
                       owner_request=data.get("owner_request"),
                       actor=_actor(request.state.lab_token))
    wait = data.get("wait") or 0
    if isinstance(wait, (int, float)) and wait > 0:
        await lab.executor.wait(batch.id, min(float(wait), MAX_WAIT_SECONDS))
    return JSONResponse(lab.batch_view(lab.get_batch(batch.id)), status_code=202)


@router.get("/batches/{batch_id}")
async def get_batch(request: Request, batch_id: int, wait: float = 0):
    lab = _lab(request)
    if wait > 0:
        await lab.executor.wait(batch_id, min(wait, MAX_WAIT_SECONDS))
    return lab.batch_view(lab.get_batch(batch_id))


@router.post("/batches/{batch_id}/cancel")
async def cancel_batch(request: Request, batch_id: int):
    lab = _lab(request)
    lab.get_batch(batch_id)
    stopped = lab.executor.cancel_batch(batch_id)
    return {"stopped": stopped, **lab.batch_view(lab.get_batch(batch_id))}


@router.post("/batches/{batch_id}/resume")
async def resume_batch(request: Request, batch_id: int):
    lab = _lab(request)
    batch = lab.get_batch(batch_id)
    lab.active_run(batch.run_id)
    resumed = lab.executor.resume(batch_id)
    return {"resumed": resumed, **lab.batch_view(lab.get_batch(batch_id))}


@router.get("/attempts/{attempt_id}")
async def get_attempt(request: Request, attempt_id: int, prompts: bool = False):
    lab, token = _lab(request), request.state.lab_token
    attempt = lab.get_attempt(attempt_id)
    record = lab.repo.scenario(attempt.scenario_id)
    _check_chat(token, record.chat_id if record else None)
    return lab.attempt_detail(attempt, prompts=prompts, viewer=_actor(token))
