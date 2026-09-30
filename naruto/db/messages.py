"""Stored chat messages (live and imported) and full-text search."""

from dataclasses import dataclass, field, fields, replace
import json
import re
import sqlite3

from naruto.db.database import Database, now_ts

LIVE = "live"
IMPORT = "import"


@dataclass
class NewMessage:
    """A message about to be stored."""
    chat_id: int
    origin_chat_id: int
    source: str
    message_id: int
    sender_name: str
    date: int
    text: str = ""
    sender_id: int | None = None
    sender_username: str | None = None
    from_bot: bool = False
    thread_id: int | None = None
    import_id: int | None = None
    edit_date: int | None = None
    media_kind: str | None = None
    media_file_id: str | None = None
    media_file_unique_id: str | None = None
    media_meta: dict = field(default_factory=dict)
    forwarded_from: str | None = None
    reply_to_message_id: int | None = None
    reply_to_snippet: str | None = None


@dataclass
class StoredMessage:
    id: int
    chat_id: int
    origin_chat_id: int
    source: str
    message_id: int
    import_id: int | None
    thread_id: int | None
    sender_id: int | None
    sender_name: str
    sender_username: str | None
    from_bot: bool
    date: int
    edit_date: int | None
    text: str
    media_kind: str | None
    media_file_id: str | None
    media_file_unique_id: str | None
    media_meta: dict
    forwarded_from: str | None
    reply_to_message_id: int | None
    reply_to_row_id: int | None
    reply_to_snippet: str | None
    created_at: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "StoredMessage":
        data = {name: row[name] for name in cls.__dataclass_fields__}
        data["from_bot"] = bool(data["from_bot"])
        data["media_meta"] = json.loads(data["media_meta"]) if data["media_meta"] else {}
        return cls(**data)

    @property
    def is_live(self) -> bool:
        return self.source == LIVE


_INSERT_COLUMNS = [f.name for f in fields(NewMessage)] + ["reply_to_row_id", "created_at"]


def fts_query(text: str) -> str | None:
    """Turn free text into a safe FTS5 query: every word must match, as a
    prefix. Returns None when there is nothing searchable."""
    words = re.findall(r"\w+", text or "", flags=re.UNICODE)
    if not words:
        return None
    return " ".join('"' + word.replace('"', '""') + '"*' for word in words)


@dataclass
class MessagePage:
    messages: list[StoredMessage]
    total: int
    offset: int
    limit: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.messages) < self.total


class MessageRepository:
    def __init__(self, db: Database):
        self.db = db

    # ---------------------------------------------------------------- writes

    def _row_values(self, message: NewMessage, reply_to_row_id: int | None, ts: int) -> tuple:
        values = []
        for name in _INSERT_COLUMNS:
            if name == "reply_to_row_id":
                values.append(reply_to_row_id)
            elif name == "created_at":
                values.append(ts)
            elif name == "media_meta":
                values.append(json.dumps(message.media_meta, ensure_ascii=False)
                              if message.media_meta else None)
            elif name == "from_bot":
                values.append(int(message.from_bot))
            else:
                values.append(getattr(message, name))
        return tuple(values)

    def insert_live(self, message: NewMessage) -> StoredMessage:
        """Store a live message. A duplicate (same Telegram message) returns
        the existing row unchanged."""
        if message.source != LIVE:
            raise ValueError("insert_live() stores live messages only")
        reply_row = None
        if message.reply_to_message_id is not None:
            reply_row = self.db.scalar(
                "SELECT id FROM messages WHERE source = 'live' AND origin_chat_id = ? "
                "AND message_id = ?",
                (message.origin_chat_id, message.reply_to_message_id),
            )
            if reply_row is not None:
                # The target is stored, so a copy of its text is not needed
                # (and would outlive the target if it is deleted).
                message = replace(message, reply_to_snippet=None)
        placeholders = ", ".join("?" for _ in _INSERT_COLUMNS)
        with self.db.transaction():
            self.db.execute(
                f"INSERT OR IGNORE INTO messages ({', '.join(_INSERT_COLUMNS)}) "
                f"VALUES ({placeholders})",
                self._row_values(message, reply_row, now_ts()),
            )
            stored = self.get_live(message.origin_chat_id, message.message_id)
        return stored

    def insert_imported(self, messages: list[NewMessage]) -> None:
        """Bulk-insert one batch of imported messages. Reply links are
        resolved afterwards with resolve_import_replies()."""
        if not messages:
            return
        ts = now_ts()
        placeholders = ", ".join("?" for _ in _INSERT_COLUMNS)
        with self.db.transaction():
            self.db.executemany(
                f"INSERT OR IGNORE INTO messages ({', '.join(_INSERT_COLUMNS)}) "
                f"VALUES ({placeholders})",
                [self._row_values(m, None, ts) for m in messages],
            )

    def resolve_import_replies(self, import_id: int) -> None:
        self.db.execute(
            "UPDATE messages SET reply_to_row_id = ("
            "  SELECT target.id FROM messages AS target "
            "  WHERE target.source = 'import' AND target.import_id = messages.import_id "
            "  AND target.message_id = messages.reply_to_message_id) "
            "WHERE source = 'import' AND import_id = ? AND reply_to_message_id IS NOT NULL",
            (import_id,),
        )

    def delete_import(self, import_id: int) -> int:
        return self.db.execute(
            "DELETE FROM messages WHERE source = 'import' AND import_id = ?", (import_id,)
        ).rowcount

    def apply_edit(
        self,
        origin_chat_id: int,
        message_id: int,
        *,
        text: str,
        edit_date: int | None,
    ) -> bool:
        cursor = self.db.execute(
            "UPDATE messages SET text = ?, edit_date = ? "
            "WHERE source = 'live' AND origin_chat_id = ? AND message_id = ?",
            (text, edit_date or now_ts(), origin_chat_id, message_id),
        )
        return cursor.rowcount > 0

    def delete_for_chat(
        self,
        chat_id: int,
        *,
        before: int | None = None,
        source: str | None = None,
    ) -> int:
        sql = "DELETE FROM messages WHERE chat_id = ?"
        params: list = [chat_id]
        if before is not None:
            sql += " AND date < ?"
            params.append(before)
        if source is not None:
            sql += " AND source = ?"
            params.append(source)
        with self.db.transaction():
            cursor = self.db.execute(sql, params)
            # Reply links pointing at deleted rows would dangle.
            self.db.execute(
                "UPDATE messages SET reply_to_row_id = NULL WHERE chat_id = ? "
                "AND reply_to_row_id IS NOT NULL "
                "AND reply_to_row_id NOT IN (SELECT id FROM messages)",
                (chat_id,),
            )
        return cursor.rowcount

    def count_for_delete(self, chat_id: int, *, before: int | None = None,
                         source: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM messages WHERE chat_id = ?"
        params: list = [chat_id]
        if before is not None:
            sql += " AND date < ?"
            params.append(before)
        if source is not None:
            sql += " AND source = ?"
            params.append(source)
        return int(self.db.scalar(sql, params) or 0)

    # ----------------------------------------------------------------- reads

    def get(self, row_id: int) -> StoredMessage | None:
        row = self.db.query_one("SELECT * FROM messages WHERE id = ?", (row_id,))
        return StoredMessage.from_row(row) if row else None

    def get_many(self, row_ids: list[int]) -> dict[int, StoredMessage]:
        if not row_ids:
            return {}
        placeholders = ", ".join("?" for _ in row_ids)
        rows = self.db.query(f"SELECT * FROM messages WHERE id IN ({placeholders})", row_ids)
        return {row["id"]: StoredMessage.from_row(row) for row in rows}

    def get_live(self, origin_chat_id: int, message_id: int) -> StoredMessage | None:
        row = self.db.query_one(
            "SELECT * FROM messages WHERE source = 'live' AND origin_chat_id = ? "
            "AND message_id = ?",
            (origin_chat_id, message_id),
        )
        return StoredMessage.from_row(row) if row else None

    def count(self, chat_id: int, source: str | None = None) -> int:
        if source is None:
            return int(self.db.scalar(
                "SELECT COUNT(*) FROM messages WHERE chat_id = ?", (chat_id,)) or 0)
        return int(self.db.scalar(
            "SELECT COUNT(*) FROM messages WHERE chat_id = ? AND source = ?",
            (chat_id, source)) or 0)

    def first_live_date(self, chat_id: int) -> int | None:
        return self.db.scalar(
            "SELECT MIN(date) FROM messages WHERE chat_id = ? AND source = 'live'",
            (chat_id,),
        )

    def recent_window(
        self,
        chat_id: int,
        before: StoredMessage,
        *,
        window: int,
        step: int,
    ) -> list[StoredMessage]:
        """Messages before ``before``, oldest first.

        Returns between ``window`` and ``window + step - 1`` messages (fewer if
        the chat is short). The start only moves in whole steps, so the prompt
        prefix stays identical for up to ``step`` new messages and the
        inference server can reuse its cache.
        """
        window = max(window, 1)
        step = max(step, 1)
        earlier = "chat_id = ? AND (date < ? OR (date = ? AND id < ?))"
        params = (chat_id, before.date, before.date, before.id)
        total = int(self.db.scalar(f"SELECT COUNT(*) FROM messages WHERE {earlier}", params) or 0)
        if total <= window:
            start = 0
        else:
            start = ((total - window) // step) * step
        rows = self.db.query(
            f"SELECT * FROM messages WHERE {earlier} ORDER BY date, id LIMIT ? OFFSET ?",
            (*params, total - start, start),
        )
        return [StoredMessage.from_row(row) for row in rows]

    def latest(self, chat_id: int, limit: int) -> list[StoredMessage]:
        rows = self.db.query(
            "SELECT * FROM (SELECT * FROM messages WHERE chat_id = ? "
            "ORDER BY date DESC, id DESC LIMIT ?) ORDER BY date, id",
            (chat_id, limit),
        )
        return [StoredMessage.from_row(row) for row in rows]

    def browse(
        self,
        chat_id: int,
        *,
        query: str | None = None,
        sender_id: int | None = None,
        source: str | None = None,
        since: int | None = None,
        until: int | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> MessagePage:
        """Newest-first page for the web admin's message browser."""
        where = ["m.chat_id = ?"]
        params: list = [chat_id]
        join = ""
        match = fts_query(query) if query else None
        if query and match is None:
            return MessagePage([], 0, offset, limit)
        if match:
            join = "JOIN messages_fts ON messages_fts.rowid = m.id"
            where.append("messages_fts MATCH ?")
            params.append(match)
        if sender_id is not None:
            where.append("m.sender_id = ?")
            params.append(sender_id)
        if source:
            where.append("m.source = ?")
            params.append(source)
        if since is not None:
            where.append("m.date >= ?")
            params.append(since)
        if until is not None:
            where.append("m.date < ?")
            params.append(until)
        clause = " AND ".join(where)
        total = int(self.db.scalar(
            f"SELECT COUNT(*) FROM messages m {join} WHERE {clause}", params) or 0)
        rows = self.db.query(
            f"SELECT m.* FROM messages m {join} WHERE {clause} "
            "ORDER BY m.date DESC, m.id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        return MessagePage([StoredMessage.from_row(r) for r in rows], total, offset, limit)

    def search(
        self,
        chat_id: int,
        query: str,
        *,
        sender_id: int | None = None,
        since: int | None = None,
        limit: int = 20,
    ) -> list[StoredMessage]:
        """Best-matching messages first (FTS5 rank)."""
        match = fts_query(query)
        if match is None:
            return []
        sql = (
            "SELECT m.* FROM messages m JOIN messages_fts ON messages_fts.rowid = m.id "
            "WHERE m.chat_id = ? AND messages_fts MATCH ?"
        )
        params: list = [chat_id, match]
        if sender_id is not None:
            sql += " AND m.sender_id = ?"
            params.append(sender_id)
        if since is not None:
            sql += " AND m.date >= ?"
            params.append(since)
        sql += " ORDER BY messages_fts.rank LIMIT ?"
        params.append(limit)
        return [StoredMessage.from_row(row) for row in self.db.query(sql, params)]

    def senders(self, chat_id: int) -> list[tuple[int, str, int]]:
        """(sender_id, latest name, message count) for the browser's filter."""
        # With MAX(), SQLite takes bare columns from the row holding the
        # maximum, so sender_name is the most recent name.
        rows = self.db.query(
            "SELECT sender_id, sender_name, MAX(date), COUNT(*) AS n FROM messages "
            "WHERE chat_id = ? AND sender_id IS NOT NULL "
            "GROUP BY sender_id ORDER BY n DESC",
            (chat_id,),
        )
        return [(row["sender_id"], row["sender_name"], row["n"]) for row in rows]
