"""The prompt lab's storage: runs, candidates, scenarios, sets, batches,
attempts, judgments, rubrics, comparisons, preferences, notes, activations,
events and API tokens (migration 12)."""

from dataclasses import dataclass, fields
import json
import sqlite3
from typing import Any

from naruto.db.database import Database


def _load(value):
    return json.loads(value) if value else None


def _dump(value) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, sort_keys=True)


class _Row:
    """Builds a dataclass from a row; JSON columns are listed in _json."""
    _json: tuple[str, ...] = ()

    @classmethod
    def from_row(cls, row: sqlite3.Row):
        data = {f.name: row[f.name] for f in fields(cls)}  # type: ignore[arg-type]
        for name in cls._json:
            data[name] = _load(data[name])
        return cls(**data)


@dataclass
class LabToken(_Row):
    id: int
    name: str
    token_hash: str
    chats: list
    may_activate: int
    created_at: int
    last_used_at: int | None
    revoked_at: int | None
    _json = ("chats",)


@dataclass
class LabRun(_Row):
    id: int
    slug: str
    objective: str
    spec: dict
    model_endpoint: str
    model_name: str
    model_reported: str | None
    baseline: dict
    baseline_history_id: int
    code_fingerprint: str
    schema_version: int
    status: str
    stop_reason: str | None
    recommendation: dict | None
    summary: str | None
    warnings: list
    token_id: int | None
    created_by: str
    created_at: int
    finished_at: int | None
    _json = ("spec", "baseline", "recommendation", "warnings")

    @property
    def budget(self) -> dict:
        return self.spec.get("budget") or {}

    @property
    def interactive(self) -> dict:
        return self.spec.get("interactive") or {}


@dataclass
class LabCandidate(_Row):
    id: int
    run_id: int
    number: int
    name: str
    parent_id: int | None
    changes: dict
    hypothesis: str
    rationale: str
    settings_hash: str
    created_at: int
    _json = ("changes",)

    @property
    def label(self) -> str:
        return f"c{self.number}-{self.name}"


@dataclass
class LabScenario(_Row):
    id: int
    slug: str
    version: int
    body: dict
    origin: str
    focused: int
    chat_id: int | None
    reason: str | None
    created_by: str
    created_at: int
    _json = ("body",)


@dataclass
class LabSet(_Row):
    id: int
    run_id: int
    name: str
    purpose: str
    created_at: int


@dataclass
class LabSetItem(_Row):
    id: int
    set_id: int
    slug: str
    added_at: int
    removed_at: int | None
    reason: str | None


@dataclass
class LabBatch(_Row):
    id: int
    run_id: int
    spec: dict
    status: str
    owner_request: str | None
    created_at: int
    finished_at: int | None
    _json = ("spec",)


@dataclass
class LabAttempt(_Row):
    id: int
    run_id: int
    batch_id: int | None
    scenario_id: int
    candidate_id: int | None
    repeat: int
    continue_from: int | None
    status: str
    outcome: str | None
    reason: str | None
    result: dict | None
    conditions: dict | None
    model_requests: int
    model_ms: int
    wait_ms: int
    duration_ms: int
    queued_at: int
    started_at: int | None
    finished_at: int | None
    first_viewed_at: int | None
    state_path: str | None
    _json = ("result", "conditions")

    @property
    def turns(self) -> list[dict]:
        return (self.result or {}).get("turns") or []


@dataclass
class LabJudgment(_Row):
    id: int
    attempt_id: int
    run_id: int
    turn: int
    kind: str
    criterion: str
    verdict: str
    score: float | None
    evidence: list
    comment: str | None
    judge: str
    rubric_version: int
    created_at: int
    _json = ("evidence",)


@dataclass
class LabRubric(_Row):
    id: int
    run_id: int
    version: int
    criteria: list
    status: str
    confirmation: str | None
    reason: str | None
    created_at: int
    _json = ("criteria",)


@dataclass
class LabComparison(_Row):
    id: int
    run_id: int
    scenario_id: int
    turn: int
    mapping: dict
    presentation: str
    status: str
    revealed_before_answer: int
    created_at: int
    answered_at: int | None
    _json = ("mapping",)


@dataclass
class LabChoice(_Row):
    id: int
    comparison_id: int
    choice: str
    comment: str | None
    channel: str
    supersedes: int | None
    created_at: int


@dataclass
class LabPreferences(_Row):
    id: int
    run_id: int
    version: int
    body: dict
    edited_by: str
    created_at: int
    _json = ("body",)


@dataclass
class LabNote(_Row):
    id: int
    run_id: int
    kind: str
    text: str
    evidence: list
    created_by: str
    created_at: int
    _json = ("evidence",)


@dataclass
class LabActivation(_Row):
    id: int
    run_id: int
    candidate_id: int
    model_endpoint: str
    model_name: str
    mode: str
    previous: dict
    applied: dict
    drift: list | None
    evidence: dict | None
    authorized_by: str
    actor: str
    created_at: int
    reverted_at: int | None
    reverted_by: str | None
    _json = ("previous", "applied", "drift", "evidence")


@dataclass
class LabEvent(_Row):
    id: int
    run_id: int
    kind: str
    detail: dict
    created_at: int
    _json = ("detail",)


_JSON_COLUMNS = {
    "lab_tokens": LabToken._json, "lab_runs": LabRun._json,
    "lab_candidates": LabCandidate._json, "lab_scenarios": LabScenario._json,
    "lab_batches": LabBatch._json, "lab_attempts": LabAttempt._json,
    "lab_judgments": LabJudgment._json, "lab_rubrics": LabRubric._json,
    "lab_comparisons": LabComparison._json, "lab_preferences": LabPreferences._json,
    "lab_notes": LabNote._json, "lab_activations": LabActivation._json,
    "lab_events": LabEvent._json,
}


class LabRepository:
    def __init__(self, db: Database):
        self.db = db

    # -------------------------------------------------------------- helpers

    def _insert(self, table: str, **values) -> int:
        for name in _JSON_COLUMNS.get(table, ()):
            if name in values:
                values[name] = _dump(values[name])
        columns = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        return self.db.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})",
                               tuple(values.values())).lastrowid

    def _update(self, table: str, row_id: int, **values) -> None:
        if not values:
            return
        for name in _JSON_COLUMNS.get(table, ()):
            if name in values:
                values[name] = _dump(values[name])
        assignments = ", ".join(f"{name} = ?" for name in values)
        self.db.execute(f"UPDATE {table} SET {assignments} WHERE id = ?",
                        (*values.values(), row_id))

    def _one(self, cls, table: str, row_id: int):
        row = self.db.query_one(f"SELECT * FROM {table} WHERE id = ?", (row_id,))
        return cls.from_row(row) if row else None

    def _many(self, cls, sql: str, params: tuple = ()) -> list:
        return [cls.from_row(row) for row in self.db.query(sql, params)]

    # --------------------------------------------------------------- tokens

    def add_token(self, name: str, token_hash: str, *, chats: list[int],
                  may_activate: bool) -> LabToken:
        token_id = self._insert("lab_tokens", name=name, token_hash=token_hash, chats=chats,
                                may_activate=int(may_activate), created_at=self.db.now())
        return self.token(token_id)

    def token(self, token_id: int) -> LabToken | None:
        return self._one(LabToken, "lab_tokens", token_id)

    def token_by_hash(self, token_hash: str) -> LabToken | None:
        row = self.db.query_one("SELECT * FROM lab_tokens WHERE token_hash = ?", (token_hash,))
        return LabToken.from_row(row) if row else None

    def tokens(self) -> list[LabToken]:
        return self._many(LabToken, "SELECT * FROM lab_tokens ORDER BY id DESC")

    def update_token(self, token_id: int, **values) -> None:
        self._update("lab_tokens", token_id, **values)

    # ----------------------------------------------------------------- runs

    def add_run(self, **values) -> LabRun:
        values.setdefault("created_at", self.db.now())
        return self.run(self._insert("lab_runs", status="active", **values))

    def run(self, run_id: int) -> LabRun | None:
        return self._one(LabRun, "lab_runs", run_id)

    def runs(self, limit: int = 100) -> list[LabRun]:
        return self._many(LabRun, "SELECT * FROM lab_runs ORDER BY id DESC LIMIT ?", (limit,))

    def update_run(self, run_id: int, **values) -> None:
        self._update("lab_runs", run_id, **values)

    def finished_runs_before(self, cutoff: int) -> list[LabRun]:
        return self._many(LabRun, "SELECT * FROM lab_runs WHERE status = 'finished' AND "
                                  "finished_at < ?", (cutoff,))

    def delete_run(self, run_id: int) -> None:
        """Everything about a run except its activations (kept as the
        record of what changed the live settings)."""
        with self.db.transaction():
            for table in ("lab_judgments", "lab_rubrics", "lab_preferences", "lab_notes",
                          "lab_events", "lab_attempts", "lab_batches", "lab_candidates"):
                if table == "lab_candidates":
                    self.db.execute(
                        "DELETE FROM lab_candidates WHERE run_id = ? AND id NOT IN "
                        "(SELECT candidate_id FROM lab_activations)", (run_id,))
                    continue
                self.db.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))
            self.db.execute("DELETE FROM lab_choices WHERE comparison_id IN "
                            "(SELECT id FROM lab_comparisons WHERE run_id = ?)", (run_id,))
            self.db.execute("DELETE FROM lab_comparisons WHERE run_id = ?", (run_id,))
            self.db.execute("DELETE FROM lab_set_items WHERE set_id IN "
                            "(SELECT id FROM lab_sets WHERE run_id = ?)", (run_id,))
            self.db.execute("DELETE FROM lab_sets WHERE run_id = ?", (run_id,))
            activated = self.db.scalar("SELECT COUNT(*) FROM lab_activations WHERE run_id = ?",
                                       (run_id,))
            if not activated:
                self.db.execute("DELETE FROM lab_runs WHERE id = ?", (run_id,))

    # ----------------------------------------------------------- candidates

    def add_candidate(self, run_id: int, **values) -> LabCandidate:
        number = int(self.db.scalar("SELECT COALESCE(MAX(number), 0) FROM lab_candidates "
                                    "WHERE run_id = ?", (run_id,)) or 0) + 1
        candidate_id = self._insert("lab_candidates", run_id=run_id, number=number,
                                    created_at=self.db.now(), **values)
        return self.candidate(candidate_id)

    def candidate(self, candidate_id: int) -> LabCandidate | None:
        return self._one(LabCandidate, "lab_candidates", candidate_id)

    def candidates(self, run_id: int) -> list[LabCandidate]:
        return self._many(LabCandidate, "SELECT * FROM lab_candidates WHERE run_id = ? "
                                        "ORDER BY number", (run_id,))

    # ------------------------------------------------------------ scenarios

    def add_scenario(self, slug: str, body: dict, *, origin: str, focused: bool,
                     chat_id: int | None, reason: str | None, created_by: str) -> LabScenario:
        version = int(self.db.scalar("SELECT COALESCE(MAX(version), 0) FROM lab_scenarios "
                                     "WHERE slug = ?", (slug,)) or 0) + 1
        scenario_id = self._insert("lab_scenarios", slug=slug, version=version, body=body,
                                   origin=origin, focused=int(focused), chat_id=chat_id,
                                   reason=reason, created_by=created_by,
                                   created_at=self.db.now())
        return self.scenario(scenario_id)

    def scenario(self, scenario_id: int) -> LabScenario | None:
        return self._one(LabScenario, "lab_scenarios", scenario_id)

    def latest_scenario(self, slug: str) -> LabScenario | None:
        row = self.db.query_one("SELECT * FROM lab_scenarios WHERE slug = ? "
                                "ORDER BY version DESC LIMIT 1", (slug,))
        return LabScenario.from_row(row) if row else None

    def scenario_versions(self, slug: str) -> list[LabScenario]:
        return self._many(LabScenario, "SELECT * FROM lab_scenarios WHERE slug = ? "
                                       "ORDER BY version", (slug,))

    def scenarios(self, *, query: str | None = None, limit: int = 200) -> list[LabScenario]:
        """The latest version of each scenario."""
        sql = ("SELECT * FROM lab_scenarios s WHERE version = (SELECT MAX(version) FROM "
               "lab_scenarios WHERE slug = s.slug)")
        params: list[Any] = []
        if query:
            sql += " AND (slug LIKE ? OR body LIKE ?)"
            params += [f"%{query}%", f"%{query}%"]
        sql += " ORDER BY slug LIMIT ?"
        return self._many(LabScenario, sql, (*params, limit))

    def from_chats(self) -> list[LabScenario]:
        """The latest version of every scenario made from a real chat."""
        return [s for s in self.scenarios(limit=10_000) if s.chat_id is not None]

    def delete_scenario(self, slug: str) -> list[str]:
        """Every version of a scenario, with the attempts that ran it and
        their judgments. Returns the attempts' saved state files."""
        with self.db.transaction():
            ids = [row[0] for row in self.db.query(
                "SELECT id FROM lab_scenarios WHERE slug = ?", (slug,))]
            if not ids:
                return []
            marks = ", ".join("?" for _ in ids)
            states = [row[0] for row in self.db.query(
                f"SELECT state_path FROM lab_attempts WHERE scenario_id IN ({marks}) "
                "AND state_path IS NOT NULL", ids)]
            self.db.execute(f"DELETE FROM lab_judgments WHERE attempt_id IN (SELECT id FROM "
                            f"lab_attempts WHERE scenario_id IN ({marks}))", ids)
            self.db.execute(f"DELETE FROM lab_attempts WHERE scenario_id IN ({marks})", ids)
            self.db.execute(f"DELETE FROM lab_comparisons WHERE scenario_id IN ({marks})", ids)
            self.db.execute("DELETE FROM lab_set_items WHERE slug = ?", (slug,))
            self.db.execute(f"DELETE FROM lab_scenarios WHERE id IN ({marks})", ids)
        return states

    # ----------------------------------------------------------------- sets

    def add_set(self, run_id: int, name: str, purpose: str) -> LabSet:
        set_id = self._insert("lab_sets", run_id=run_id, name=name, purpose=purpose,
                              created_at=self.db.now())
        return self._one(LabSet, "lab_sets", set_id)

    def set(self, set_id: int) -> LabSet | None:
        return self._one(LabSet, "lab_sets", set_id)

    def set_by_name(self, run_id: int, name: str) -> LabSet | None:
        row = self.db.query_one("SELECT * FROM lab_sets WHERE run_id = ? AND name = ?",
                                (run_id, name))
        return LabSet.from_row(row) if row else None

    def sets(self, run_id: int) -> list[LabSet]:
        return self._many(LabSet, "SELECT * FROM lab_sets WHERE run_id = ? ORDER BY id",
                          (run_id,))

    def add_set_item(self, set_id: int, slug: str, reason: str | None = None) -> None:
        self._insert("lab_set_items", set_id=set_id, slug=slug, added_at=self.db.now(),
                     reason=reason)

    def remove_set_item(self, set_id: int, slug: str, reason: str) -> bool:
        return self.db.execute(
            "UPDATE lab_set_items SET removed_at = ?, reason = ? WHERE set_id = ? AND slug = ? "
            "AND removed_at IS NULL", (self.db.now(), reason, set_id, slug)).rowcount > 0

    def set_items(self, set_id: int, *, include_removed: bool = False) -> list[LabSetItem]:
        sql = "SELECT * FROM lab_set_items WHERE set_id = ?"
        if not include_removed:
            sql += " AND removed_at IS NULL"
        return self._many(LabSetItem, sql + " ORDER BY id", (set_id,))

    # ------------------------------------------------------------- batches

    def add_batch(self, run_id: int, spec: dict, owner_request: str | None = None) -> LabBatch:
        batch_id = self._insert("lab_batches", run_id=run_id, spec=spec, status="queued",
                                owner_request=owner_request, created_at=self.db.now())
        return self.batch(batch_id)

    def batch(self, batch_id: int) -> LabBatch | None:
        return self._one(LabBatch, "lab_batches", batch_id)

    def batches(self, run_id: int) -> list[LabBatch]:
        return self._many(LabBatch, "SELECT * FROM lab_batches WHERE run_id = ? ORDER BY id",
                          (run_id,))

    def update_batch(self, batch_id: int, **values) -> None:
        self._update("lab_batches", batch_id, **values)

    # ------------------------------------------------------------- attempts

    def add_attempt(self, run_id: int, *, batch_id: int | None, scenario_id: int,
                    candidate_id: int | None, repeat: int = 1,
                    continue_from: int | None = None) -> LabAttempt:
        attempt_id = self._insert("lab_attempts", run_id=run_id, batch_id=batch_id,
                                  scenario_id=scenario_id, candidate_id=candidate_id,
                                  repeat=repeat, continue_from=continue_from, status="queued",
                                  queued_at=self.db.now())
        return self.attempt(attempt_id)

    def attempt(self, attempt_id: int) -> LabAttempt | None:
        return self._one(LabAttempt, "lab_attempts", attempt_id)

    def attempts(self, *, run_id: int | None = None, batch_id: int | None = None,
                 status: str | None = None) -> list[LabAttempt]:
        where, params = [], []
        for column, value in (("run_id", run_id), ("batch_id", batch_id), ("status", status)):
            if value is not None:
                where.append(f"{column} = ?")
                params.append(value)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        return self._many(LabAttempt, f"SELECT * FROM lab_attempts {clause} ORDER BY id",
                          tuple(params))

    def update_attempt(self, attempt_id: int, **values) -> None:
        self._update("lab_attempts", attempt_id, **values)

    def mark_viewed(self, attempt_id: int) -> None:
        self.db.execute("UPDATE lab_attempts SET first_viewed_at = ? WHERE id = ? AND "
                        "first_viewed_at IS NULL", (self.db.now(), attempt_id))

    def usage(self, run_id: int) -> dict:
        """What a run has used of its budget: attempts started and model
        requests made."""
        row = self.db.query_one(
            "SELECT COUNT(*) AS attempts, COALESCE(SUM(model_requests), 0) AS requests, "
            "COALESCE(SUM(model_ms), 0) AS model_ms FROM lab_attempts WHERE run_id = ? AND "
            "started_at IS NOT NULL", (run_id,))
        return {"attempts": row["attempts"], "model_requests": row["requests"],
                "model_ms": row["model_ms"]}

    def interrupt_open(self) -> int:
        """At startup: attempts and batches that were waiting or running."""
        with self.db.transaction():
            count = self.db.execute(
                "UPDATE lab_attempts SET status = 'interrupted', finished_at = ? "
                "WHERE status IN ('queued', 'running')", (self.db.now(),)).rowcount
            self.db.execute("UPDATE lab_batches SET status = 'interrupted' "
                            "WHERE status IN ('queued', 'running')")
        return count

    # ------------------------------------------------------------ judgments

    def add_judgment(self, **values) -> LabJudgment:
        judgment_id = self._insert("lab_judgments", created_at=self.db.now(), **values)
        return self._one(LabJudgment, "lab_judgments", judgment_id)

    def judgments(self, *, attempt_id: int | None = None,
                  run_id: int | None = None) -> list[LabJudgment]:
        if attempt_id is not None:
            return self._many(LabJudgment, "SELECT * FROM lab_judgments WHERE attempt_id = ? "
                                           "ORDER BY id", (attempt_id,))
        return self._many(LabJudgment, "SELECT * FROM lab_judgments WHERE run_id = ? "
                                       "ORDER BY id", (run_id,))

    # -------------------------------------------------------------- rubrics

    def add_rubric(self, run_id: int, criteria: list, *, status: str,
                   confirmation: str | None, reason: str | None) -> LabRubric:
        version = int(self.db.scalar("SELECT COALESCE(MAX(version), 0) FROM lab_rubrics "
                                     "WHERE run_id = ?", (run_id,)) or 0) + 1
        rubric_id = self._insert("lab_rubrics", run_id=run_id, version=version,
                                 criteria=criteria, status=status, confirmation=confirmation,
                                 reason=reason, created_at=self.db.now())
        return self._one(LabRubric, "lab_rubrics", rubric_id)

    def rubric(self, run_id: int) -> LabRubric | None:
        row = self.db.query_one("SELECT * FROM lab_rubrics WHERE run_id = ? "
                                "ORDER BY version DESC LIMIT 1", (run_id,))
        return LabRubric.from_row(row) if row else None

    def rubrics(self, run_id: int) -> list[LabRubric]:
        return self._many(LabRubric, "SELECT * FROM lab_rubrics WHERE run_id = ? "
                                     "ORDER BY version", (run_id,))

    # ---------------------------------------------------------- comparisons

    def add_comparison(self, **values) -> LabComparison:
        comparison_id = self._insert("lab_comparisons", status="pending",
                                     created_at=self.db.now(), **values)
        return self.comparison(comparison_id)

    def comparison(self, comparison_id: int) -> LabComparison | None:
        return self._one(LabComparison, "lab_comparisons", comparison_id)

    def comparisons(self, run_id: int, *, status: str | None = None) -> list[LabComparison]:
        sql = "SELECT * FROM lab_comparisons WHERE run_id = ?"
        params: tuple = (run_id,)
        if status:
            sql += " AND status = ?"
            params += (status,)
        return self._many(LabComparison, sql + " ORDER BY id", params)

    def update_comparison(self, comparison_id: int, **values) -> None:
        self._update("lab_comparisons", comparison_id, **values)

    def add_choice(self, comparison_id: int, choice: str, *, comment: str | None,
                   channel: str, supersedes: int | None) -> LabChoice:
        choice_id = self._insert("lab_choices", comparison_id=comparison_id, choice=choice,
                                 comment=comment, channel=channel, supersedes=supersedes,
                                 created_at=self.db.now())
        return self._one(LabChoice, "lab_choices", choice_id)

    def choices(self, comparison_id: int) -> list[LabChoice]:
        return self._many(LabChoice, "SELECT * FROM lab_choices WHERE comparison_id = ? "
                                     "ORDER BY id", (comparison_id,))

    # ---------------------------------------------------------- preferences

    def add_preferences(self, run_id: int, body: dict, *, edited_by: str) -> LabPreferences:
        version = int(self.db.scalar("SELECT COALESCE(MAX(version), 0) FROM lab_preferences "
                                     "WHERE run_id = ?", (run_id,)) or 0) + 1
        pref_id = self._insert("lab_preferences", run_id=run_id, version=version, body=body,
                               edited_by=edited_by, created_at=self.db.now())
        return self._one(LabPreferences, "lab_preferences", pref_id)

    def preferences(self, run_id: int) -> LabPreferences | None:
        row = self.db.query_one("SELECT * FROM lab_preferences WHERE run_id = ? "
                                "ORDER BY version DESC LIMIT 1", (run_id,))
        return LabPreferences.from_row(row) if row else None

    # ---------------------------------------------------------------- notes

    def add_note(self, run_id: int, kind: str, text: str, *, evidence: list,
                 created_by: str) -> LabNote:
        note_id = self._insert("lab_notes", run_id=run_id, kind=kind, text=text,
                               evidence=evidence, created_by=created_by,
                               created_at=self.db.now())
        return self._one(LabNote, "lab_notes", note_id)

    def notes(self, run_id: int) -> list[LabNote]:
        return self._many(LabNote, "SELECT * FROM lab_notes WHERE run_id = ? ORDER BY id",
                          (run_id,))

    # ---------------------------------------------------------- activations

    def add_activation(self, **values) -> LabActivation:
        activation_id = self._insert("lab_activations", created_at=self.db.now(), **values)
        return self.activation(activation_id)

    def activation(self, activation_id: int) -> LabActivation | None:
        return self._one(LabActivation, "lab_activations", activation_id)

    def activations(self, *, run_id: int | None = None) -> list[LabActivation]:
        if run_id is not None:
            return self._many(LabActivation, "SELECT * FROM lab_activations WHERE run_id = ? "
                                             "ORDER BY id DESC", (run_id,))
        return self._many(LabActivation, "SELECT * FROM lab_activations ORDER BY id DESC")

    def update_activation(self, activation_id: int, **values) -> None:
        self._update("lab_activations", activation_id, **values)

    # --------------------------------------------------------------- events

    def add_event(self, run_id: int, kind: str, **detail) -> None:
        self._insert("lab_events", run_id=run_id, kind=kind, detail=detail,
                     created_at=self.db.now())

    def events(self, run_id: int, kind: str | None = None) -> list[LabEvent]:
        sql = "SELECT * FROM lab_events WHERE run_id = ?"
        params: tuple = (run_id,)
        if kind:
            sql += " AND kind = ?"
            params += (kind,)
        return self._many(LabEvent, sql + " ORDER BY id", params)
