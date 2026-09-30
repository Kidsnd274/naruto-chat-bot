"""The digest: a rolling summary of what's going on in a chat right now."""

from dataclasses import dataclass
import sqlite3

from naruto.db.database import Database, now_ts
from naruto.db.messages import StoredMessage


@dataclass
class Digest:
    chat_id: int
    text: str
    last_row_id: int | None
    last_message_date: int | None
    updated_at: int
    updated_by: str
    error: str | None
    failed_at: int | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Digest":
        return cls(**{name: row[name] for name in cls.__dataclass_fields__})


class DigestRepository:
    def __init__(self, db: Database):
        self.db = db

    def get(self, chat_id: int) -> Digest | None:
        row = self.db.query_one("SELECT * FROM digests WHERE chat_id = ?", (chat_id,))
        return Digest.from_row(row) if row else None

    def save(self, chat_id: int, text: str, *, actor: str,
             last: StoredMessage | None = None) -> Digest:
        """Store new digest text. ``last`` moves the cursor to that message;
        without it (an owner edit) the cursor stays."""
        ts = now_ts()
        current = self.get(chat_id)
        last_row_id = last.id if last else (current.last_row_id if current else None)
        last_date = last.date if last else (current.last_message_date if current else None)
        self.db.execute(
            "INSERT INTO digests (chat_id, text, last_row_id, last_message_date, updated_at, "
            "updated_by, error, failed_at) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL) "
            "ON CONFLICT(chat_id) DO UPDATE SET text = excluded.text, "
            "last_row_id = excluded.last_row_id, last_message_date = excluded.last_message_date, "
            "updated_at = excluded.updated_at, updated_by = excluded.updated_by, "
            "error = NULL, failed_at = NULL",
            (chat_id, text.strip(), last_row_id, last_date, ts, actor))
        return self.get(chat_id)

    def set_error(self, chat_id: int, error: str) -> None:
        ts = now_ts()
        self.db.execute(
            "INSERT INTO digests (chat_id, text, updated_at, updated_by, error, failed_at) "
            "VALUES (?, '', ?, 'bot', ?, ?) ON CONFLICT(chat_id) DO UPDATE SET "
            "error = excluded.error, failed_at = excluded.failed_at",
            (chat_id, ts, error[:500], ts))

    def clear(self, chat_id: int) -> bool:
        return self.db.execute("DELETE FROM digests WHERE chat_id = ?", (chat_id,)).rowcount > 0

    def unread(self, chat_id: int, digest: Digest | None, *, limit: int) -> list[StoredMessage]:
        """Messages the digest hasn't read yet, oldest first."""
        if digest is None or digest.last_message_date is None:
            rows = self.db.query(
                "SELECT * FROM messages WHERE chat_id = ? ORDER BY date, id LIMIT ?",
                (chat_id, limit))
        else:
            rows = self.db.query(
                "SELECT * FROM messages WHERE chat_id = ? AND (date > ? OR (date = ? AND id > ?)) "
                "ORDER BY date, id LIMIT ?",
                (chat_id, digest.last_message_date, digest.last_message_date,
                 digest.last_row_id or 0, limit))
        return [StoredMessage.from_row(row) for row in rows]

    def unread_count(self, chat_id: int, digest: Digest | None) -> tuple[int, int | None]:
        """(messages not read yet, date of the newest one)."""
        if digest is None or digest.last_message_date is None:
            row = self.db.query_one(
                "SELECT COUNT(*), MAX(date) FROM messages WHERE chat_id = ?", (chat_id,))
        else:
            row = self.db.query_one(
                "SELECT COUNT(*), MAX(date) FROM messages WHERE chat_id = ? "
                "AND (date > ? OR (date = ? AND id > ?))",
                (chat_id, digest.last_message_date, digest.last_message_date,
                 digest.last_row_id or 0))
        return int(row[0] or 0), row[1]
