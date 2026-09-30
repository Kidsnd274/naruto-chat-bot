"""Agent run traces: one row per bot response."""

from dataclasses import dataclass
import json
import sqlite3
import time

from naruto.db.database import Database

_JSON_FIELDS = ("prompt", "reply_message_ids", "usage")


@dataclass
class AgentRun:
    id: int
    chat_id: int
    trigger_row_id: int | None
    trigger_message_id: int | None
    user_id: int | None
    skill: str
    status: str
    model: str | None
    prompt: list | None
    prompt_tokens: int | None
    window_size: int | None
    dropped: int | None
    image_count: int | None
    reasoning: str | None
    response: str | None
    reply_message_ids: list | None
    usage: dict | None
    latency_ms: int | None
    finish_reason: str | None
    error: str | None
    started_at: float
    finished_at: float | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "AgentRun":
        data = {name: row[name] for name in cls.__dataclass_fields__}
        for name in _JSON_FIELDS:
            data[name] = json.loads(data[name]) if data[name] else None
        return cls(**data)

    @property
    def duration_ms(self) -> int | None:
        if self.finished_at is None:
            return None
        return int((self.finished_at - self.started_at) * 1000)

    @property
    def prompt_chars(self) -> int:
        if not self.prompt:
            return 0
        total = 0
        for message in self.prompt:
            content = message.get("content")
            if isinstance(content, str):
                total += len(content)
            elif isinstance(content, list):
                total += sum(len(part.get("text", "")) for part in content)
        return total


class AgentRunRepository:
    def __init__(self, db: Database):
        self.db = db

    def start(self, *, chat_id: int, skill: str, trigger_row_id: int | None = None,
              trigger_message_id: int | None = None, user_id: int | None = None) -> int:
        cursor = self.db.execute(
            "INSERT INTO agent_runs (chat_id, trigger_row_id, trigger_message_id, user_id, "
            "skill, status, started_at) VALUES (?, ?, ?, ?, ?, 'running', ?)",
            (chat_id, trigger_row_id, trigger_message_id, user_id, skill, time.time()),
        )
        return cursor.lastrowid

    def update(self, run_id: int, **fields) -> None:
        for name in _JSON_FIELDS:
            if name in fields and fields[name] is not None:
                fields[name] = json.dumps(fields[name], ensure_ascii=False)
        if fields.get("status") in ("ok", "empty", "error"):
            fields.setdefault("finished_at", time.time())
        assignments = ", ".join(f"{name} = ?" for name in fields)
        self.db.execute(f"UPDATE agent_runs SET {assignments} WHERE id = ?",
                        (*fields.values(), run_id))

    def get(self, run_id: int) -> AgentRun | None:
        row = self.db.query_one("SELECT * FROM agent_runs WHERE id = ?", (run_id,))
        return AgentRun.from_row(row) if row else None

    def recent(self, *, chat_id: int | None = None, status: str | None = None,
               offset: int = 0, limit: int = 50) -> tuple[list[AgentRun], int]:
        where, params = [], []
        if chat_id is not None:
            where.append("chat_id = ?")
            params.append(chat_id)
        if status:
            where.append("status = ?")
            params.append(status)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        total = int(self.db.scalar(f"SELECT COUNT(*) FROM agent_runs {clause}", params) or 0)
        rows = self.db.query(
            f"SELECT * FROM agent_runs {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset))
        return [AgentRun.from_row(row) for row in rows], total

    def delete_older_than(self, cutoff: float) -> int:
        return self.db.execute("DELETE FROM agent_runs WHERE started_at < ?", (cutoff,)).rowcount
