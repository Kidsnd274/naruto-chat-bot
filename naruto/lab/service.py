"""The prompt lab's operations, shared by the agent API and the web admin:
runs, candidates, scenarios, sets, attempts and their budget.

Nothing here changes the live bot's settings; activation (stage 6) is the
only way a candidate reaches them.
"""

import hashlib
import hmac
import logging
from pathlib import Path
import secrets
from typing import Any

from naruto.agent.skills import SKILLS
from naruto.db.lab import (
    LabAttempt,
    LabBatch,
    LabCandidate,
    LabRepository,
    LabRun,
    LabScenario,
    LabSet,
    LabToken,
)
from naruto.lab import config
from naruto.lab.executor import LabExecutor
from naruto.lab.scenario import ScenarioError, parse_scenario
from naruto.settings.registry import SettingError

logger = logging.getLogger(__name__)

POLICIES = ("recommend", "agent_may_activate")
STOP_REASONS = ("objective_met", "budget_exhausted", "no_improvement", "blocked", "cancelled")
PURPOSES = ("tuning", "validation", "regression")
MAX_BATCH = 500
MAX_REPEAT = 10
TOKEN_PREFIX = "nlab_"


def token_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class LabError(Exception):
    """A request the lab refuses; the message is for whoever asked.
    ``code``: invalid | not_found | conflict | forbidden."""

    def __init__(self, message: str, code: str = "invalid", **details):
        super().__init__(message)
        self.code = code
        self.details = details


def _int(value, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
        raise LabError(f"{name} must be a whole number.")
    if not low <= value <= high:
        raise LabError(f"{name} must be between {low} and {high}.")
    return int(value)


def _text_list(value, name: str, limit: int = 50) -> list[str]:
    value = value or []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise LabError(f"{name} must be a list of text.")
    if len(value) > limit:
        raise LabError(f"{name} takes at most {limit} items.")
    return [v.strip() for v in value if v.strip()]


class LabService:
    def __init__(self, services, state_dir: Path, *, llm_factory=None):
        self.services = services
        self.repo = LabRepository(services.db)
        self.state_dir = Path(state_dir)
        self.executor = LabExecutor(self, llm_factory=llm_factory)

    # ============================================================== tokens

    def create_token(self, name: str, *, chats: list[int] = (),
                     may_activate: bool = False) -> tuple[LabToken, str]:
        """A new API token. The secret is returned once and never stored."""
        name = (name or "").strip()[:60] or "agent"
        secret = TOKEN_PREFIX + secrets.token_urlsafe(32)
        token = self.repo.add_token(name, token_hash(secret), chats=list(chats),
                                    may_activate=may_activate)
        logger.info("Lab API token %s (%s) created", token.id, name)
        return token, secret

    def authenticate(self, secret: str | None) -> LabToken | None:
        if not secret or not secret.startswith(TOKEN_PREFIX):
            return None
        token = self.repo.token_by_hash(token_hash(secret))
        if token is None or token.revoked_at is not None:
            return None
        if not hmac.compare_digest(token.token_hash, token_hash(secret)):
            return None
        self.repo.update_token(token.id, last_used_at=self.services.db.now())
        return token

    def set_token_access(self, token_id: int, *, chats: list[int], may_activate: bool) -> None:
        self.repo.update_token(token_id, chats=list(chats), may_activate=int(may_activate))

    def revoke_token(self, token_id: int) -> None:
        self.repo.update_token(token_id, revoked_at=self.services.db.now())

    # ================================================================ runs

    def start_run(self, raw: dict, *, created_by: str, token: LabToken | None = None) -> LabRun:
        settings = self.services.settings
        if not isinstance(raw, dict):
            raise LabError("A run is a JSON object.")
        unknown = set(raw) - {"objective", "protected", "scope", "budget", "interactive",
                              "activation", "evaluator", "data", "model"}
        if unknown:
            raise LabError(f"Unknown run fields: {', '.join(sorted(unknown))}.")
        objective = (raw.get("objective") or "").strip()
        if not objective:
            raise LabError("A run needs an objective: what should improve.")
        if len(objective) > 4000:
            raise LabError("The objective is too long (4,000 characters at most).")
        scope_raw = raw.get("scope") or {}
        scope_keys = _text_list(scope_raw.get("keys"), "scope.keys", 100)
        try:
            config.check_scope(scope_keys)
        except SettingError as exc:
            raise LabError(f"scope.keys: {exc}") from None
        skills = _text_list(scope_raw.get("skills"), "scope.skills")
        unknown_skills = [s for s in skills if s not in SKILLS]
        if unknown_skills:
            raise LabError(f"Unknown skills in scope: {', '.join(unknown_skills)} "
                           f"(skills: {', '.join(SKILLS)}).")

        defaults = settings["lab.default_budget"] or {}
        budget_raw = {**defaults, **(raw.get("budget") or {})}
        budget = {
            "attempts": _int(budget_raw.get("attempts", 60), "budget.attempts", 1,
                             settings["lab.max_attempts_per_run"]),
            "model_requests": _int(budget_raw.get("model_requests", 300),
                                   "budget.model_requests", 1, 100_000),
        }
        hours = budget_raw.get("hours", 4)
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 0 < hours <= 168:
            raise LabError("budget.hours must be a number of hours, at most 168.")
        budget["hours"] = hours

        interactive_raw = raw.get("interactive") or {}
        interactive = {
            "enabled": bool(interactive_raw.get("enabled", False)),
            "comparisons_at_a_time": _int(interactive_raw.get("comparisons_at_a_time", 1),
                                          "interactive.comparisons_at_a_time", 1, 5),
        }
        policy = (raw.get("activation") or {}).get("policy", "recommend")
        if policy not in POLICIES:
            raise LabError(f"activation.policy must be one of: {', '.join(POLICIES)}.")
        if policy == "agent_may_activate" and token is not None and not token.may_activate:
            raise LabError("This token may not activate candidates, so the run can't allow it. "
                           "The owner can give the token that permission on the Lab page.",
                           "forbidden")
        evaluator_raw = raw.get("evaluator") or {}
        evaluator = {"name": str(evaluator_raw.get("name") or created_by)[:100],
                     "external": bool(evaluator_raw.get("external", True))}
        chats = (raw.get("data") or {}).get("chats") or []
        if not isinstance(chats, list) or not all(isinstance(c, int) for c in chats):
            raise LabError("data.chats must be a list of chat IDs.")
        if token is not None:
            refused = sorted(set(chats) - set(token.chats))
            if refused:
                raise LabError(f"This token may not read chats {refused}. The owner can allow "
                               "chats for it on the Lab page.", "forbidden")
        endpoint, model = self._resolve_model(raw.get("model") or {})

        baseline = config.snapshot(settings)
        history_id = int(self.services.db.scalar(
            "SELECT COALESCE(MAX(id), 0) FROM settings_history") or 0)
        spec = {"protected": _text_list(raw.get("protected"), "protected"),
                "scope": {"keys": scope_keys, "skills": skills}, "budget": budget,
                "interactive": interactive, "activation": {"policy": policy},
                "evaluator": evaluator, "data": {"chats": chats},
                "deadline_at": int(self.services.db.now() + hours * 3600)}
        run = self.repo.add_run(
            slug=config.slugify(objective), objective=objective, spec=spec,
            model_endpoint=endpoint, model_name=model, baseline=baseline,
            baseline_history_id=history_id, code_fingerprint=config.code_fingerprint(),
            schema_version=self.services.db.schema_version, token_id=token.id if token else None,
            created_by=created_by)
        overriding = self.overriding_chats(self.tunable(run))
        if overriding:
            self.repo.add_event(run.id, "per_chat_overrides", chats=overriding)
        logger.info("Lab run %s started by %s", run.id, created_by)
        return run

    def _resolve_model(self, raw: dict) -> tuple[str, str]:
        settings = self.services.settings
        configured = settings["model.endpoint_url"]
        servers = settings["lab.model_servers"] or {}
        if not isinstance(raw, dict):
            raise LabError("model must be an object: {\"server\": name} or {\"endpoint\": url}.")
        endpoint = configured
        if raw.get("server"):
            if raw["server"] not in servers:
                raise LabError(f"Unknown model server {raw['server']!r}. The owner lists them in "
                               "Settings → Lab → Other model servers.", "forbidden")
            endpoint = servers[raw["server"]]
        elif raw.get("endpoint"):
            endpoint = str(raw["endpoint"]).strip()
            if endpoint != configured and endpoint not in servers.values():
                raise LabError("The lab only tests the configured model server or one listed in "
                               "Settings → Lab → Other model servers.", "forbidden")
        name = raw.get("name")
        if name is None:
            name = settings["model.name"] if endpoint == configured else ""
        return endpoint, str(name).strip()

    def get_run(self, run_id: int) -> LabRun:
        run = self.repo.run(run_id)
        if run is None:
            raise LabError(f"There is no run {run_id}.", "not_found")
        return run

    def active_run(self, run_id: int) -> LabRun:
        run = self.get_run(run_id)
        if run.status != "active":
            raise LabError(f"Run {run_id} is finished ({run.stop_reason}).", "conflict")
        return run

    def tunable(self, run: LabRun) -> list[str]:
        return config.tunable_keys(run.spec.get("scope", {}).get("keys"))

    def overriding_chats(self, keys: list[str]) -> dict[str, list[int]]:
        """Chats whose own settings hide a global change to these keys."""
        result: dict[str, list[int]] = {}
        for row in self.services.db.query("SELECT chat_id, key FROM chat_settings"):
            if row["key"] in keys:
                result.setdefault(row["key"], []).append(row["chat_id"])
        return result

    def budget_problem(self, run: LabRun) -> str | None:
        """Why the run can't start another attempt, if it can't."""
        if run.status != "active":
            return "the run is finished"
        budget = run.budget
        used = self.repo.usage(run.id)
        if used["attempts"] >= budget.get("attempts", 0):
            return f"all {budget['attempts']} attempts used"
        if used["model_requests"] >= budget.get("model_requests", 0):
            return f"all {budget['model_requests']} model requests used"
        if self.services.db.now() >= run.spec.get("deadline_at", 0):
            return f"the {budget.get('hours')} hours are up"
        return None

    def run_state(self, run: LabRun) -> dict:
        used = self.repo.usage(run.id)
        budget = run.budget
        pending = self.repo.comparisons(run.id, status="pending")
        busy = [b.id for b in self.repo.batches(run.id) if b.status in ("queued", "running")]
        if run.status == "finished":
            state = "finished"
        elif pending:
            state = "waiting_for_owner"
        elif busy:
            state = "running"
        else:
            state = "idle"
        return {
            "state": state,
            "budget": budget,
            "used": used,
            "remaining": {"attempts": max(budget.get("attempts", 0) - used["attempts"], 0),
                          "model_requests": max(budget.get("model_requests", 0)
                                                - used["model_requests"], 0),
                          "seconds": max(run.spec.get("deadline_at", 0)
                                         - self.services.db.now(), 0)},
            "budget_problem": self.budget_problem(run) if run.status == "active" else None,
            "pending_comparisons": [c.id for c in pending],
            "running_batches": busy,
            "warnings": run.warnings,
        }

    def stop_run(self, run_id: int, *, reason: str = "cancelled", note: str | None = None,
                 actor: str) -> LabRun:
        run = self.active_run(run_id)
        if reason not in STOP_REASONS:
            raise LabError(f"The stop reason must be one of: {', '.join(STOP_REASONS)}.")
        for batch in self.repo.batches(run.id):
            if batch.status in ("queued", "running", "interrupted"):
                self.executor.cancel_batch(batch.id)
        for comparison in self.repo.comparisons(run.id, status="pending"):
            self.repo.update_comparison(comparison.id, status="withdrawn")
        self.repo.update_run(run.id, status="finished", stop_reason=reason,
                             finished_at=self.services.db.now(),
                             summary=note if note else run.summary)
        self.repo.add_event(run.id, "stopped", reason=reason, by=actor)
        return self.get_run(run.id)

    def finish_run(self, run_id: int, *, reason: str, recommendation: dict | None,
                   summary: str | None, actor: str) -> LabRun:
        """End a run with the agent's recommendation (stage 4's report shows
        it)."""
        run = self.active_run(run_id)
        if recommendation is not None:
            action = recommendation.get("action")
            if action not in ("activate", "continue", "keep_baseline"):
                raise LabError("recommendation.action must be activate, continue or "
                               "keep_baseline.")
            if action == "activate":
                self.get_candidate(run, recommendation.get("candidate"))
        self.repo.update_run(run.id, recommendation=recommendation, summary=summary)
        return self.stop_run(run.id, reason=reason, actor=actor)

    def note_conditions(self, run_id: int, attempt_id: int, conditions: dict) -> None:
        """Warn when an attempt ran under different conditions than the run
        (another model, or the bot restarted on other code)."""
        run = self.repo.run(run_id)
        warnings = list(run.warnings)
        reported = conditions.get("models_reported") or []
        if reported and run.model_reported is None:
            self.repo.update_run(run_id, model_reported=reported[0])
            run.model_reported = reported[0]
        others = [m for m in reported if m != run.model_reported]
        if others:
            text = (f"The model server reported {', '.join(others)} instead of "
                    f"{run.model_reported} (from attempt {attempt_id}): results from different "
                    "models aren't a clean comparison.")
            if text.split(" (from")[0] not in " ".join(warnings):
                warnings.append(text)
        if conditions.get("code_fingerprint") != run.code_fingerprint:
            text = (f"The bot's code changed during the run (attempt {attempt_id} and later): "
                    "compare attempts from before and after with care.")
            if not any(w.startswith("The bot's code changed") for w in warnings):
                warnings.append(text)
        if warnings != run.warnings:
            self.repo.update_run(run_id, warnings=warnings)
            self.repo.add_event(run_id, "drift", attempt=attempt_id, warnings=warnings[-1:])

    # ========================================================== candidates

    def get_candidate(self, run: LabRun, candidate_id) -> LabCandidate | None:
        """None for the baseline ("baseline", 0 or null)."""
        if candidate_id in (None, 0, "baseline", "c0"):
            return None
        if isinstance(candidate_id, str) and candidate_id.startswith("c"):
            number = candidate_id[1:].split("-", 1)[0]
            matches = [c for c in self.repo.candidates(run.id) if str(c.number) == number]
            if matches:
                return matches[0]
        try:
            candidate = self.repo.candidate(int(candidate_id))
        except (TypeError, ValueError):
            candidate = None
        if candidate is None or candidate.run_id != run.id:
            raise LabError(f"Run {run.id} has no candidate {candidate_id}.", "not_found")
        return candidate

    def chain(self, candidate: LabCandidate | None) -> list[LabCandidate]:
        chain = []
        while candidate is not None:
            chain.append(candidate)
            candidate = self.repo.candidate(candidate.parent_id) if candidate.parent_id else None
        return list(reversed(chain))

    def effective_settings(self, run: LabRun, candidate: LabCandidate | None) -> dict[str, Any]:
        return config.effective(run.baseline, [c.changes for c in self.chain(candidate)],
                                endpoint=run.model_endpoint, model=run.model_name)

    def add_candidate(self, run_id: int, *, name: str, changes: dict, parent=None,
                      hypothesis: str = "", rationale: str = "") -> LabCandidate:
        run = self.active_run(run_id)
        parent_candidate = self.get_candidate(run, parent)
        allowed = self.tunable(run)
        try:
            changes = config.validate_changes(changes, allowed)
        except SettingError as exc:
            raise LabError(str(exc)) from None
        parent_values = self.effective_settings(run, parent_candidate)
        changes = {k: v for k, v in changes.items() if parent_values.get(k) != v}
        if not changes:
            raise LabError("These changes are what its parent already has.")
        name = config.slugify(name or "candidate", 30)
        values = {**parent_values, **changes}
        candidate = self.repo.add_candidate(
            run.id, name=name, parent_id=parent_candidate.id if parent_candidate else None,
            changes=changes, hypothesis=(hypothesis or "").strip()[:2000],
            rationale=(rationale or "").strip()[:4000],
            settings_hash=config.settings_hash(values))
        logger.info("Lab run %s: candidate %s", run.id, candidate.label)
        return candidate

    def add_candidate_from_files(self, run_id: int, *, name: str, files: dict[str, str],
                                 parent=None, hypothesis: str = "",
                                 rationale: str = "") -> LabCandidate:
        """A candidate from a folder of configuration files (lab export's
        layout): only what differs from the parent counts as a change."""
        run = self.active_run(run_id)
        if not isinstance(files, dict) or not all(isinstance(v, str) for v in files.values()):
            raise LabError("files must map file names to their text.")
        files = {name: text for name, text in files.items() if name != "CHANGES.md"}
        parent_values = self.effective_settings(run, self.get_candidate(run, parent))
        try:
            changes = config.from_files(files, parent_values, self.tunable(run))
        except SettingError as exc:
            raise LabError(str(exc)) from None
        if not changes:
            raise LabError("The files are the same as the parent's configuration.")
        return self.add_candidate(run_id, name=name, changes=changes, parent=parent,
                                  hypothesis=hypothesis, rationale=rationale)

    def candidate_view(self, run: LabRun, candidate: LabCandidate | None) -> dict:
        keys = self.tunable(run)
        values = self.effective_settings(run, candidate)
        parent = (self.repo.candidate(candidate.parent_id)
                  if candidate is not None and candidate.parent_id else None)
        parent_values = self.effective_settings(run, parent)
        view = {
            "id": candidate.id if candidate else None,
            "label": candidate.label if candidate else "baseline",
            "parent": (parent.label if parent else "baseline") if candidate else None,
            "hypothesis": candidate.hypothesis if candidate else "",
            "rationale": candidate.rationale if candidate else "",
            "changes": candidate.changes if candidate else {},
            "vs_parent": config.differences(parent_values, values, keys) if candidate else {},
            "vs_baseline": config.differences(self.effective_settings(run, None), values, keys),
            "settings": {key: values.get(key) for key in keys},
            "settings_hash": config.settings_hash(values),
            "background_effects": [],
        }
        shared = [k for k in view["vs_baseline"] if k in config.SHARED_MODEL_KEYS]
        if shared:
            view["background_effects"] = [
                f"{', '.join(shared)} also change {', '.join(config.BACKGROUND_TASKS)}, which "
                "the lab doesn't evaluate."]
        return view

    # =========================================================== scenarios

    def add_scenario(self, body: dict, *, created_by: str, reason: str | None = None,
                     run_id: int | None = None, chat_id: int | None = None
                     ) -> tuple[LabScenario, bool]:
        """Store a scenario (validated). The same body again is a no-op; a
        different body for an existing id is a new version and needs a
        reason. Returns (scenario, created)."""
        try:
            scenario = parse_scenario(body, None)
        except ScenarioError as exc:
            raise LabError(str(exc)) from None
        existing = self.repo.latest_scenario(scenario.id)
        if existing is not None and existing.body == body:
            return existing, False
        if existing is not None and not (reason or "").strip():
            raise LabError(f"Scenario {scenario.id} exists (version {existing.version}). To "
                           "change it, give a reason: it becomes a new version, and earlier "
                           "results stay tied to the old one.", "conflict",
                           version=existing.version)
        if scenario.origin == "history" and chat_id is None:
            raise LabError("Scenarios from real chats are made from agent runs "
                           "(scenarios/from-agent-run).")
        record = self.repo.add_scenario(scenario.id, body, origin=scenario.origin,
                                        focused=scenario.focused, chat_id=chat_id,
                                        reason=(reason or "").strip() or None,
                                        created_by=created_by)
        if existing is not None and run_id is not None:
            self.repo.add_event(run_id, "scenario_changed", slug=scenario.id,
                                from_version=existing.version, to_version=record.version,
                                reason=reason)
        return record, True

    def get_scenario(self, ref) -> LabScenario:
        """By id (a number) or slug (the latest version)."""
        record = None
        if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
            record = self.repo.scenario(int(ref))
        elif isinstance(ref, str):
            record = self.repo.latest_scenario(ref)
        if record is None:
            raise LabError(f"There is no scenario {ref}.", "not_found")
        return record

    # ================================================================ sets

    def add_set(self, run_id: int, name: str, purpose: str, slugs: list[str]) -> LabSet:
        run = self.active_run(run_id)
        if purpose not in PURPOSES:
            raise LabError(f"A set's purpose is one of: {', '.join(PURPOSES)}.")
        name = config.slugify(name, 40)
        if self.repo.set_by_name(run.id, name):
            raise LabError(f"Run {run.id} already has a set {name}.", "conflict")
        for slug in slugs:
            self.get_scenario(slug)
        lab_set = self.repo.add_set(run.id, name, purpose)
        for slug in dict.fromkeys(slugs):
            self.repo.add_set_item(lab_set.id, slug)
        return lab_set

    def get_set(self, run: LabRun, ref) -> LabSet:
        lab_set = None
        if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
            lab_set = self.repo.set(int(ref))
        elif isinstance(ref, str):
            lab_set = self.repo.set_by_name(run.id, ref)
        if lab_set is None or lab_set.run_id != run.id:
            raise LabError(f"Run {run.id} has no set {ref}.", "not_found")
        return lab_set

    def change_set(self, run_id: int, ref, *, add: list[str] = (),
                   remove: list[dict] = ()) -> LabSet:
        run = self.active_run(run_id)
        lab_set = self.get_set(run, ref)
        current = {item.slug for item in self.repo.set_items(lab_set.id)}
        for slug in add:
            self.get_scenario(slug)
            if slug not in current:
                self.repo.add_set_item(lab_set.id, slug)
                self.repo.add_event(run.id, "set_added", set=lab_set.name, slug=slug)
        for item in remove:
            slug, reason = item.get("slug"), (item.get("reason") or "").strip()
            if not reason:
                raise LabError("Removing a scenario from a set needs a reason: reports show it.")
            if self.repo.remove_set_item(lab_set.id, slug, reason):
                self.repo.add_event(run.id, "set_removed", set=lab_set.name, slug=slug,
                                    reason=reason, purpose=lab_set.purpose)
        return lab_set

    def set_view(self, lab_set: LabSet) -> dict:
        return {"id": lab_set.id, "name": lab_set.name, "purpose": lab_set.purpose,
                "scenarios": [item.slug for item in self.repo.set_items(lab_set.id)],
                "removed": [{"slug": i.slug, "reason": i.reason}
                            for i in self.repo.set_items(lab_set.id, include_removed=True)
                            if i.removed_at]}

    # ============================================================ attempts

    def submit(self, run_id: int, *, scenarios: list = (), set_ref=None, candidates: list = (None,),
               repeat: int = 1, continue_from: int | None = None,
               owner_request: str | None = None, actor: str) -> LabBatch:
        run = self.active_run(run_id)
        pending = self.repo.comparisons(run.id, status="pending")
        owner_request = (owner_request or "").strip() or None
        if pending and not owner_request:
            self.repo.add_event(run.id, "refused_while_waiting", by=actor,
                                comparisons=[c.id for c in pending])
            raise LabError(
                f"Waiting for the owner's choice on comparison {pending[0].id}. Don't run more "
                "experiments until they answer; if the owner asked for something meanwhile, "
                "pass owner_request with what they asked.", "conflict",
                pending=[c.id for c in pending])
        problem = self.budget_problem(run)
        if problem:
            raise LabError(f"The run's budget is used up: {problem}.", "conflict",
                           budget=run.budget)
        records = [self.get_scenario(ref) for ref in scenarios]
        if set_ref is not None:
            lab_set = self.get_set(run, set_ref)
            records += [self.get_scenario(item.slug)
                        for item in self.repo.set_items(lab_set.id)]
        if not records:
            raise LabError("Name at least one scenario (or a set).")
        configs = [self.get_candidate(run, ref) for ref in (candidates or [None])]
        repeat = _int(repeat, "repeat", 1, MAX_REPEAT)
        previous = None
        if continue_from is not None:
            previous = self.repo.attempt(int(continue_from))
            if previous is None or previous.run_id != run.id:
                raise LabError(f"Run {run.id} has no attempt {continue_from}.", "not_found")
            if previous.status != "done" or not previous.state_path:
                raise LabError(f"Attempt {continue_from} has no saved state to continue from "
                               f"(it is {previous.status}).", "conflict")
        for record in records:
            continues = bool(record.body.get("continues"))
            if continues and previous is None:
                raise LabError(f"Scenario {record.slug} continues a conversation: pass "
                               "continue_from (the attempt it follows).")
        count = len(records) * len(configs) * repeat
        remaining = run.budget["attempts"] - self.repo.usage(run.id)["attempts"]
        if count > MAX_BATCH:
            raise LabError(f"That's {count} attempts; one batch takes at most {MAX_BATCH}.")
        if count > remaining:
            raise LabError(f"That's {count} attempts, but the run has {remaining} left.",
                           "conflict", remaining=remaining)
        spec = {"scenarios": [r.id for r in records],
                "candidates": [c.id if c else None for c in configs], "repeat": repeat,
                "continue_from": continue_from, "by": actor}
        batch = self.repo.add_batch(run.id, spec, owner_request)
        if owner_request:
            self.repo.add_event(run.id, "owner_request", batch=batch.id, text=owner_request,
                                by=actor)
        for index in range(1, repeat + 1):
            for record in records:
                for candidate in configs:
                    self.repo.add_attempt(run.id, batch_id=batch.id, scenario_id=record.id,
                                          candidate_id=candidate.id if candidate else None,
                                          repeat=index, continue_from=continue_from)
        self.executor.start(batch.id)
        return batch

    def get_attempt(self, attempt_id: int, run_id: int | None = None) -> LabAttempt:
        attempt = self.repo.attempt(attempt_id)
        if attempt is None or (run_id is not None and attempt.run_id != run_id):
            raise LabError(f"There is no attempt {attempt_id}.", "not_found")
        return attempt

    def get_batch(self, batch_id: int) -> LabBatch:
        batch = self.repo.batch(batch_id)
        if batch is None:
            raise LabError(f"There is no batch {batch_id}.", "not_found")
        return batch

    def batch_view(self, batch: LabBatch) -> dict:
        attempts = self.repo.attempts(batch_id=batch.id)
        counts: dict[str, int] = {}
        for attempt in attempts:
            key = attempt.outcome or attempt.status
            counts[key] = counts.get(key, 0) + 1
        return {"id": batch.id, "run": batch.run_id, "status": batch.status,
                "running": self.executor.is_running(batch.id), "spec": batch.spec,
                "owner_request": batch.owner_request, "counts": counts,
                "attempts": [self.attempt_summary(a) for a in attempts]}

    def label(self, run: LabRun, candidate_id: int | None) -> str:
        if candidate_id is None:
            return "baseline"
        candidate = self.repo.candidate(candidate_id)
        return candidate.label if candidate else f"candidate {candidate_id}"

    def attempt_summary(self, attempt: LabAttempt) -> dict:
        record = self.repo.scenario(attempt.scenario_id)
        run = self.repo.run(attempt.run_id)
        answers = [t.get("answer", "") for t in attempt.turns]
        return {"id": attempt.id, "scenario": record.slug if record else None,
                "scenario_version": record.version if record else None,
                "candidate": self.label(run, attempt.candidate_id), "repeat": attempt.repeat,
                "continues": attempt.continue_from, "status": attempt.status,
                "outcome": attempt.outcome, "reason": attempt.reason,
                "model_requests": attempt.model_requests, "model_ms": attempt.model_ms,
                "wait_ms": attempt.wait_ms, "answers": answers}

    def attempt_detail(self, attempt: LabAttempt, *, prompts: bool = False,
                       viewer: str | None = None) -> dict:
        """Everything about an attempt. Prompts and full traces only when
        asked (they are large). Reading it counts as having seen it (for
        validation-set independence)."""
        if viewer is not None and attempt.status == "done":
            self.repo.mark_viewed(attempt.id)
        view = self.attempt_summary(attempt)
        record = self.repo.scenario(attempt.scenario_id)
        view.update({"batch": attempt.batch_id, "conditions": attempt.conditions,
                     "scenario_body": record.body if record else None,
                     "duration_ms": attempt.duration_ms, "turns": []})
        for turn in attempt.turns:
            entry = {key: turn.get(key) for key in (
                "index", "kind", "trigger", "skill", "final_skill", "note", "since", "answer",
                "threaded", "fallback", "sent", "delivery", "actions", "telegram",
                "state_changes", "checks", "outcome", "reason", "error", "duration_ms",
                "model_requests", "model_ms", "usage", "models")}
            run_trace = turn.get("run") or {}
            steps = run_trace.get("steps") or []
            entry["tool_calls"] = [
                {"name": s.get("name"), "arguments": s.get("arguments"),
                 "result": s.get("result"), "error": s.get("error")}
                for s in steps if s.get("type") == "tool"]
            entry["reasoning"] = run_trace.get("reasoning")
            entry["reasoning_note"] = ("The model's reasoning text as the server returned it; "
                                       "absent when the server doesn't return any.")
            entry["prompt_tokens_estimate"] = run_trace.get("prompt_tokens")
            if prompts:
                entry["prompt"] = run_trace.get("prompt")
                entry["steps"] = steps
            view["turns"].append(entry)
        if attempt.result:
            view["final_state"] = attempt.result.get("final_state")
            view["usage"] = attempt.result.get("usage")
        view["judgments"] = [
            {"id": j.id, "turn": j.turn, "kind": j.kind, "criterion": j.criterion,
             "verdict": j.verdict, "score": j.score, "evidence": j.evidence,
             "comment": j.comment, "judge": j.judge, "rubric_version": j.rubric_version}
            for j in self.repo.judgments(attempt_id=attempt.id)]
        return view

    # =========================================================== upkeep

    def recover(self) -> int:
        return self.executor.recover()

    async def shutdown(self) -> None:
        await self.executor.shutdown()

    def cleanup(self, days: int) -> int:
        """Delete finished runs older than ``days`` (activations stay)."""
        if days <= 0:
            return 0
        cutoff = int(self.services.db.now() - days * 86400)
        runs = self.repo.finished_runs_before(cutoff)
        for run in runs:
            for attempt in self.repo.attempts(run_id=run.id):
                if attempt.state_path:
                    Path(attempt.state_path).unlink(missing_ok=True)
            self.repo.delete_run(run.id)
        return len(runs)
