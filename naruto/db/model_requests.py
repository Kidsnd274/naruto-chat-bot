"""The model queue's log: one row per model request, for the queue page.

Times are fractional seconds. Prompts are not stored (the agent run or the
import shows what was sent). Rows follow the agent-run retention.
"""

from dataclasses import dataclass
import sqlite3
import time
from typing import TYPE_CHECKING

from naruto.db.database import Database

if TYPE_CHECKING:
    from naruto.model_queue import RequestInfo

OPEN_STATES = ("queued", "running", "retrying")


@dataclass
class ModelRequest:
    id: int
    chat_id: int | None
    task: str
    priority: str
    state: str
    run_id: int | None
    import_id: int | None
    period_id: int | None
    chunk: int | None
    lab_attempt_id: int | None
    attempts: int
    queued_at: float
    started_at: float | None
    finished_at: float | None
    error: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ModelRequest":
        return cls(**{name: row[name] for name in cls.__dataclass_fields__})

    @property
    def wait_seconds(self) -> float | None:
        if self.started_at is None:
            return None
        return self.started_at - self.queued_at

    @property
    def run_seconds(self) -> float | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return self.finished_at - self.started_at


class ModelRequestRepository:
    def __init__(self, db: Database):
        self.db = db

    def queued(self, info: "RequestInfo", priority: str, at: float) -> int:
        return self.db.execute(
            "INSERT INTO model_requests (chat_id, task, priority, state, run_id, import_id, "
            "period_id, chunk, lab_attempt_id, queued_at) "
            "VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)",
            (info.chat_id, info.task, priority, info.run_id, info.import_id, info.period_id,
             info.chunk, info.lab_attempt_id, at)).lastrowid

    def requeued(self, request_id: int, attempts: int, at: float) -> None:
        self.db.execute(
            "UPDATE model_requests SET state = 'queued', attempts = ?, queued_at = ?, "
            "started_at = NULL WHERE id = ?", (attempts, at, request_id))

    def started(self, request_id: int, at: float) -> None:
        self.db.execute("UPDATE model_requests SET state = 'running', started_at = ? "
                        "WHERE id = ?", (at, request_id))

    def retrying(self, request_id: int, error: str | None) -> None:
        self.db.execute("UPDATE model_requests SET state = 'retrying', error = ? WHERE id = ?",
                        ((error or "")[:500] or None, request_id))

    def finished(self, request_id: int, state: str, at: float, error: str | None) -> None:
        self.db.execute(
            "UPDATE model_requests SET state = ?, finished_at = ?, error = ? WHERE id = ?",
            (state, at, (error or "")[:500] or None, request_id))

    def recent(self, *, chat_id: int | None = None, task: str | None = None,
               priority: str | None = None, state: str | None = None,
               limit: int = 100) -> list[ModelRequest]:
        where, params = [], []
        for column, value in (("chat_id", chat_id), ("task", task), ("priority", priority),
                              ("state", state)):
            if value is not None and value != "":
                where.append(f"{column} = ?")
                params.append(value)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        rows = self.db.query(f"SELECT * FROM model_requests {clause} ORDER BY id DESC LIMIT ?",
                             (*params, limit))
        return [ModelRequest.from_row(row) for row in rows]

    def tasks(self) -> list[str]:
        return [row[0] for row in self.db.query(
            "SELECT DISTINCT task FROM model_requests ORDER BY task")]

    def interrupt_open(self) -> int:
        """At startup: requests that were waiting or running when the
        process stopped. Replies are not replayed; background jobs queue
        new requests from their checkpoints."""
        placeholders = ", ".join("?" for _ in OPEN_STATES)
        return self.db.execute(
            f"UPDATE model_requests SET state = 'interrupted', finished_at = ?, "
            f"error = 'Interrupted by a restart.' WHERE state IN ({placeholders})",
            (time.time(), *OPEN_STATES)).rowcount

    def delete_older_than(self, cutoff: float) -> int:
        """Finished requests only: an open one keeps its record however old."""
        placeholders = ", ".join("?" for _ in OPEN_STATES)
        return self.db.execute(
            f"DELETE FROM model_requests WHERE queued_at < ? AND state NOT IN ({placeholders})",
            (cutoff, *OPEN_STATES)).rowcount
