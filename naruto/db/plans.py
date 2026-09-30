"""Plans the bot proposed with Confirm / Change buttons."""

from dataclasses import dataclass
import json
import sqlite3

from naruto.db.database import Database, now_ts

PROPOSED = "proposed"
CONFIRMED = "confirmed"
CANCELLED = "cancelled"


@dataclass
class Plan:
    id: int
    chat_id: int
    title: str
    items: list[str]
    status: str
    message_id: int | None
    message_chat_id: int | None
    run_id: int | None
    proposed_for_user_id: int | None
    created_at: int
    decided_at: int | None
    decided_by_user_id: int | None
    decided_by_name: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Plan":
        data = {name: row[name] for name in cls.__dataclass_fields__}
        data["items"] = json.loads(data["items"] or "[]")
        return cls(**data)

    def one_line(self) -> str:
        """For the board: the title plus the details."""
        if not self.items:
            return self.title
        return f"{self.title}: {'; '.join(self.items)}"


class PlanRepository:
    def __init__(self, db: Database):
        self.db = db

    def create(self, chat_id: int, title: str, items: list[str], *, run_id: int | None,
               proposed_for_user_id: int | None) -> Plan:
        plan_id = self.db.execute(
            "INSERT INTO plans (chat_id, title, items, run_id, proposed_for_user_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, title, json.dumps(items, ensure_ascii=False), run_id,
             proposed_for_user_id, now_ts())).lastrowid
        return self.get(plan_id)

    def get(self, plan_id: int) -> Plan | None:
        row = self.db.query_one("SELECT * FROM plans WHERE id = ?", (plan_id,))
        return Plan.from_row(row) if row else None

    def set_message(self, plan_id: int, message_id: int, message_chat_id: int) -> None:
        self.db.execute("UPDATE plans SET message_id = ?, message_chat_id = ? WHERE id = ?",
                        (message_id, message_chat_id, plan_id))

    def decide(self, plan_id: int, status: str, *, user_id: int | None,
               name: str | None) -> bool:
        """Move a proposed plan to ``status``. False if it was already decided."""
        return self.db.execute(
            "UPDATE plans SET status = ?, decided_at = ?, decided_by_user_id = ?, "
            "decided_by_name = ? WHERE id = ? AND status = 'proposed'",
            (status, now_ts(), user_id, name, plan_id)).rowcount > 0

    def for_chat(self, chat_id: int, *, status: str | None = None, limit: int = 20) -> list[Plan]:
        sql = "SELECT * FROM plans WHERE chat_id = ?"
        params: list = [chat_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [Plan.from_row(row) for row in self.db.query(sql, params)]

    def delete_for_chat(self, chat_id: int) -> int:
        return self.db.execute("DELETE FROM plans WHERE chat_id = ?", (chat_id,)).rowcount
