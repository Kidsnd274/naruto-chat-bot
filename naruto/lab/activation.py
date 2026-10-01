"""Putting a candidate into the live bot, and taking it out again.

Activation is the only way the lab changes the live settings. It is
refused when:

- an agent asks without both permissions: its token may activate, and the
  run's activation policy allows agents to (the owner can always activate
  on the Lab page);
- a setting the candidate changes was changed by someone else since the run
  started (a conflict: nothing is overwritten silently);
- another setting the evaluation depended on changed since (drift), unless
  that is acknowledged with a reason, which is recorded.

``changes`` applies the candidate's differences from the baseline;
``full`` applies its whole configuration, e.g. to go back to a model and the
configuration tuned for it. A run on another model switches the model too.
Everything applied is one transaction, recorded with what it replaced, so
it can be reverted while nobody has changed those settings again.
"""

from naruto.lab import config
from naruto.lab.errors import LabError

MODES = ("changes", "full")
# Settings that shape a reply in the sandbox, besides the tunable ones.
REPLY_KEYS = ("general.timezone", "memory.prompt_notes", "history.lookup_results",
              "media.enabled", "media.max_size_mb", "media.estimated_image_tokens",
              "board.format", "board.pin")
NOT_REPLY = ("model.parallel_requests", "model.background_requests",
             "model.foreground_reserved", "model.background_paused",
             "model.request_timeout_seconds")


def relevant_keys(baseline: dict) -> list[str]:
    """Settings the run's evaluation depended on."""
    keys = []
    for key in baseline:
        if key in NOT_REPLY:
            continue
        if (key.startswith(("persona.", "prompt.", "skills.", "model.", "context.", "agent."))
                or key in REPLY_KEYS or config.is_extended(key)):
            keys.append(key)
    return keys


def plan(lab, run, candidate, *, mode: str = "changes") -> dict:
    """What activating would change, and what stands in the way."""
    if mode not in MODES:
        raise LabError(f"mode is one of: {', '.join(MODES)}.")
    settings = lab.services.settings
    live = config.snapshot(settings)
    values = lab.effective_settings(run, candidate)
    keys = lab.tunable(run)
    if mode == "changes":
        apply = {key: values[key] for key in keys if values.get(key) != run.baseline.get(key)}
    else:
        apply = {key: values[key] for key in keys}
    for key in config.MODEL_KEYS:  # a run on another model switches the model too
        if run.baseline.get(key) != values[key] or mode == "full":
            apply[key] = values[key]
    apply = {key: value for key, value in apply.items() if live.get(key) != value}
    conflicts = []
    if mode == "changes":
        for key, value in apply.items():
            if live.get(key) != run.baseline.get(key):
                last = settings.last_changed(key)
                conflicts.append({"key": key, "baseline": run.baseline.get(key),
                                  "live": live.get(key), "candidate": value,
                                  "changed_by": last.changed_by if last else None,
                                  "changed_at": last.changed_at if last else None})
    drift = []
    for key in relevant_keys(run.baseline):
        if key in apply or key in config.MODEL_KEYS:
            continue
        if live.get(key) != run.baseline.get(key):
            last = settings.last_changed(key)
            drift.append({"key": key, "baseline": run.baseline.get(key), "live": live.get(key),
                          "changed_by": last.changed_by if last else None})
    return {
        "candidate": candidate.label if candidate else "baseline", "mode": mode,
        "apply": apply, "previous": {key: live.get(key) for key in apply},
        "diffs": {key: config.text_diff(live.get(key), value, config.file_name(key) or key)
                  for key, value in apply.items()},
        "conflicts": conflicts, "drift": drift,
        "per_chat_overrides": lab.overriding_chats(list(apply)),
        "background_effects": [key for key in apply if key in config.SHARED_MODEL_KEYS],
        "model_switch": any(key in apply for key in config.MODEL_KEYS),
    }


def _evidence(lab, run, candidate) -> dict:
    label = candidate.label if candidate else "baseline"
    comparison = lab.compare(run.id)
    attempts = [a for a in lab.repo.attempts(run_id=run.id)
                if a.candidate_id == (candidate.id if candidate else None) and a.status == "done"]
    return {"label": label, "attempts": len(attempts),
            "summary": {k: v for k, v in comparison["summary"].get(label, {}).items()
                        if k in ("checked", "passed", "pass_rate", "outcomes")},
            "baseline": {k: v for k, v in comparison["summary"].get("baseline", {}).items()
                         if k in ("checked", "passed", "pass_rate", "outcomes")},
            "regressions": [r for r in comparison["regressions"] if r["config"] == label],
            "validation": lab.report(run.id)["validation"]["note"],
            "recommendation": run.recommendation}


def activate(lab, run_id: int, candidate_ref, *, mode: str = "changes", authorized_by: str,
             acknowledge_drift: str | None = None, token=None, actor: str):
    """``token`` is None for the owner on the Lab page."""
    run = lab.get_run(run_id)
    candidate = lab.get_candidate(run, candidate_ref)
    if candidate is None and mode == "changes":
        raise LabError("The baseline has no changes to activate; to put the baseline's "
                       "configuration back, use mode full.")
    if token is not None:
        if not token.may_activate:
            raise LabError("This token may not activate candidates. The owner can activate on "
                           "the Lab page, or give the token that permission.", "forbidden")
        if run.spec.get("activation", {}).get("policy") != "agent_may_activate":
            raise LabError(f"Run {run.id}'s activation policy is "
                           f"{run.spec.get('activation', {}).get('policy')}: it ends with a "
                           "recommendation, and the owner activates.", "forbidden")
        done = [a for a in lab.repo.attempts(run_id=run.id) if a.status == "done"
                and a.candidate_id == (candidate.id if candidate else None)]
        if not done:
            raise LabError("This candidate hasn't been evaluated in this run.", "conflict")
    authorized_by = (authorized_by or "").strip()
    if not authorized_by:
        raise LabError("Say who authorized this and how (e.g. \"the owner, in the terminal: "
                       "'ship c2'\").")
    steps = plan(lab, run, candidate, mode=mode)
    if not steps["apply"]:
        raise LabError("Nothing to change: the live settings already match.", "conflict")
    if steps["conflicts"]:
        raise LabError("Settings this candidate changes were changed by someone else since the "
                       "run started. Nothing was applied; ask the owner how to merge.",
                       "conflict", conflicts=steps["conflicts"])
    if steps["drift"] and not (acknowledge_drift or "").strip():
        raise LabError("Other settings the evaluation depended on changed since the run "
                       "started, so the results may not hold. To activate anyway, pass "
                       "acknowledge_drift with a reason.", "conflict", drift=steps["drift"])
    label = candidate.label if candidate else "baseline"
    who = f"lab run {run.id} {label} (authorized by {authorized_by})"
    lab.services.settings.set_many(steps["apply"], actor=who)
    activation = lab.repo.add_activation(
        run_id=run.id, candidate_id=candidate.id if candidate else 0,
        model_endpoint=run.model_endpoint, model_name=run.model_name, mode=mode,
        previous=steps["previous"], applied=steps["apply"],
        drift=[{**d, "acknowledged": acknowledge_drift} for d in steps["drift"]] or None,
        evidence=_evidence(lab, run, candidate), authorized_by=authorized_by, actor=actor)
    lab.repo.add_event(run.id, "activated", activation=activation.id, candidate=label,
                       keys=sorted(steps["apply"]), by=actor)
    return activation


def revert(lab, activation_id: int, *, token=None, actor: str):
    activation = lab.repo.activation(activation_id)
    if activation is None:
        raise LabError(f"There is no activation {activation_id}.", "not_found")
    if activation.reverted_at is not None:
        raise LabError("This activation was already reverted.", "conflict")
    if token is not None and not token.may_activate:
        raise LabError("This token may not change the live settings.", "forbidden")
    live = config.snapshot(lab.services.settings)
    changed = [{"key": key, "activated": value, "live": live.get(key)}
               for key, value in activation.applied.items() if live.get(key) != value]
    if changed:
        raise LabError("Some of these settings changed again after the activation; nothing "
                       "was reverted. Revert them one by one on the Settings page.",
                       "conflict", changed=changed)
    lab.services.settings.set_many(activation.previous,
                                   actor=f"lab: reverting activation {activation.id}")
    lab.repo.update_activation(activation.id, reverted_at=lab.services.db.now(),
                               reverted_by=actor)
    lab.repo.add_event(activation.run_id, "reverted", activation=activation.id, by=actor)
    return lab.repo.activation(activation.id)


def _redacted(values: dict | None) -> dict | None:
    if not values or "model.endpoint_url" not in values:
        return values
    return {**values, "model.endpoint_url": config.redact_endpoint(values["model.endpoint_url"])}


def public_plan(steps: dict) -> dict:
    """A plan as shown to agents and the owner: no endpoint passwords."""
    shown = dict(steps, apply=_redacted(steps["apply"]), previous=_redacted(steps["previous"]))
    if "model.endpoint_url" in steps["diffs"]:
        shown["diffs"] = {**steps["diffs"], "model.endpoint_url": config.text_diff(
            config.redact_endpoint(steps["previous"]["model.endpoint_url"] or ""),
            config.redact_endpoint(steps["apply"]["model.endpoint_url"] or ""),
            "model.endpoint_url")}
    return shown


def view(lab, activation) -> dict:
    run = lab.repo.run(activation.run_id)
    candidate = lab.repo.candidate(activation.candidate_id) if activation.candidate_id else None
    return {"id": activation.id, "run": activation.run_id,
            "candidate": candidate.label if candidate else "baseline",
            "ref": f"c{candidate.number}" if candidate else "baseline",
            "model": {"endpoint": config.redact_endpoint(activation.model_endpoint),
                      "name": activation.model_name},
            "mode": activation.mode, "applied": _redacted(activation.applied),
            "previous": _redacted(activation.previous), "drift": activation.drift,
            "evidence": activation.evidence, "authorized_by": activation.authorized_by,
            "by": activation.actor, "at": activation.created_at,
            "reverted_at": activation.reverted_at, "reverted_by": activation.reverted_by,
            "objective": run.objective if run else None}
