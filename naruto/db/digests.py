"""The digest: a rolling summary of what's going on in a chat right now."""

from dataclasses import dataclass
import sqlite3

from naruto.db.database import Database
from naruto.db.messages import StoredMessage

ANY = object()  # save(): no revision check (owner edits)


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
    revision: int = 0  # bumped by every change to the text or cursor

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
             last: StoredMessage | None = None,
             expected_revision: int | None | object = ANY) -> Digest | None:
        """Store new digest text. ``last`` moves the cursor to that message;
        without it (an owner edit) the cursor stays.

        A background update passes ``expected_revision``: the revision it
        read (None if there was no digest). If the digest changed or was
        deleted since, nothing is saved and None is returned."""
        ts = self.db.now()
        current = self.get(chat_id)
        last_row_id = last.id if last else (current.last_row_id if current else None)
        last_date = last.date if last else (current.last_message_date if current else None)
        values = (chat_id, text.strip(), last_row_id, last_date, ts, actor)
        if expected_revision is ANY:
            self.db.execute(
                "INSERT INTO digests (chat_id, text, last_row_id, last_message_date, "
                "updated_at, updated_by, error, failed_at, revision) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, 1) "
                "ON CONFLICT(chat_id) DO UPDATE SET text = excluded.text, "
                "last_row_id = excluded.last_row_id, "
                "last_message_date = excluded.last_message_date, "
                "updated_at = excluded.updated_at, updated_by = excluded.updated_by, "
                "error = NULL, failed_at = NULL, revision = digests.revision + 1", values)
        elif expected_revision is None:
            # Only if there is still no digest (the owner may have written one).
            saved = self.db.execute(
                "INSERT INTO digests (chat_id, text, last_row_id, last_message_date, "
                "updated_at, updated_by, error, failed_at, revision) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, 1) "
                "ON CONFLICT(chat_id) DO NOTHING", values).rowcount
            if not saved:
                return None
        else:
            saved = self.db.execute(
                "UPDATE digests SET text = ?, last_row_id = ?, last_message_date = ?, "
                "updated_at = ?, updated_by = ?, error = NULL, failed_at = NULL, "
                "revision = revision + 1 WHERE chat_id = ? AND revision = ?",
                (*values[1:], chat_id, expected_revision)).rowcount
            if not saved:
                return None
        return self.get(chat_id)

    def set_error(self, chat_id: int, error: str) -> None:
        ts = self.db.now()
        self.db.execute(
            "INSERT INTO digests (chat_id, text, updated_at, updated_by, error, failed_at) "
            "VALUES (?, '', ?, 'bot', ?, ?) ON CONFLICT(chat_id) DO UPDATE SET "
            "error = excluded.error, failed_at = excluded.failed_at",
            (chat_id, ts, error[:500], ts))

    def clear(self, chat_id: int) -> bool:
        return self.db.execute("DELETE FROM digests WHERE chat_id = ?", (chat_id,)).rowcount > 0

    def unread(self, chat_id: int, digest: Digest | None, *,
               limit: int) -> list[StoredMessage]:
        """Live messages the digest hasn't read yet, oldest first. Imported
        messages never go into the digest (or the notes it proposes): an
        export's history goes into history summaries, and stays searchable."""
        sql, params = self._unread_where(chat_id, digest)
        rows = self.db.query(f"SELECT * FROM messages WHERE {sql} ORDER BY date, id LIMIT ?",
                             (*params, limit))
        return [StoredMessage.from_row(row) for row in rows]

    def unread_count(self, chat_id: int, digest: Digest | None) -> tuple[int, int | None]:
        """(live messages not read yet, date of the newest one)."""
        sql, params = self._unread_where(chat_id, digest)
        row = self.db.query_one(f"SELECT COUNT(*), MAX(date) FROM messages WHERE {sql}", params)
        return int(row[0] or 0), row[1]

    @staticmethod
    def _unread_where(chat_id: int, digest: Digest | None) -> tuple[str, tuple]:
        """The cursor is stored by value (date, row ID), so deleting the row
        it points at doesn't move it."""
        if digest is None or digest.last_message_date is None:
            return "chat_id = ? AND source = 'live'", (chat_id,)
        return ("chat_id = ? AND source = 'live' AND (date > ? OR (date = ? AND id > ?))",
                (chat_id, digest.last_message_date, digest.last_message_date,
                 digest.last_row_id or 0))
