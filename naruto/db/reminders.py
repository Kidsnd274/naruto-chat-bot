"""Reminders: messages the bot posts in a chat at a set time."""

from dataclasses import dataclass
import sqlite3

from naruto.db.database import Database, now_ts

PENDING = "pending"
SENT = "sent"
CANCELLED = "cancelled"
FAILED = "failed"


@dataclass
class Reminder:
    id: int
    chat_id: int
    text: str
    due_at: int
    status: str
    created_by_user_id: int | None
    created_by: str
    run_id: int | None
    created_at: int
    sent_at: int | None
    sent_message_id: int | None
    error: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Reminder":
        return cls(**{name: row[name] for name in cls.__dataclass_fields__})


class ReminderRepository:
    def __init__(self, db: Database):
        self.db = db

    def create(self, chat_id: int, text: str, due_at: int, *, created_by: str,
               created_by_user_id: int | None = None, run_id: int | None = None) -> Reminder:
        reminder_id = self.db.execute(
            "INSERT INTO reminders (chat_id, text, due_at, created_by, created_by_user_id, "
            "run_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, text, due_at, created_by, created_by_user_id, run_id, now_ts())).lastrowid
        return self.get(reminder_id)

    def get(self, reminder_id: int) -> Reminder | None:
        row = self.db.query_one("SELECT * FROM reminders WHERE id = ?", (reminder_id,))
        return Reminder.from_row(row) if row else None

    def for_chat(self, chat_id: int, *, status: str | None = None,
                 limit: int = 50) -> list[Reminder]:
        sql = "SELECT * FROM reminders WHERE chat_id = ?"
        params: list = [chat_id]
        if status:
            sql += " AND status = ? ORDER BY due_at"
            params.append(status)
        else:
            sql += " ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, due_at DESC"
        sql += " LIMIT ?"
        params.append(limit)
        return [Reminder.from_row(row) for row in self.db.query(sql, params)]

    def pending_count(self, chat_id: int) -> int:
        return int(self.db.scalar(
            "SELECT COUNT(*) FROM reminders WHERE chat_id = ? AND status = 'pending'",
            (chat_id,)) or 0)

    def due(self, now: int | None = None, limit: int = 20) -> list[Reminder]:
        rows = self.db.query(
            "SELECT * FROM reminders WHERE status = 'pending' AND due_at <= ? "
            "ORDER BY due_at LIMIT ?", (now or now_ts(), limit))
        return [Reminder.from_row(row) for row in rows]

    def cancel(self, reminder_id: int) -> bool:
        return self.db.execute(
            "UPDATE reminders SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
            (reminder_id,)).rowcount > 0

    def mark_sent(self, reminder_id: int, message_id: int | None) -> None:
        self.db.execute(
            "UPDATE reminders SET status = 'sent', sent_at = ?, sent_message_id = ?, error = NULL "
            "WHERE id = ?", (now_ts(), message_id, reminder_id))

    def mark_failed(self, reminder_id: int, error: str) -> None:
        self.db.execute("UPDATE reminders SET status = 'failed', error = ? WHERE id = ?",
                        (error[:300], reminder_id))

    def delete_finished_before(self, cutoff: int) -> int:
        return self.db.execute(
            "DELETE FROM reminders WHERE status != 'pending' AND due_at < ?", (cutoff,)).rowcount

    def delete_for_chat(self, chat_id: int) -> int:
        return self.db.execute("DELETE FROM reminders WHERE chat_id = ?", (chat_id,)).rowcount
