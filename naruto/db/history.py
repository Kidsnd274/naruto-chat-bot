"""History digests: dated summaries of past periods, and the work items
(periods) that build them.

A digest is ``active`` (used by the bot and shown in the web admin),
``staged`` (built to replace older digests, published once every summary of
that overlap is done) or ``replaced``. Digests keep their own provenance
(dates, message count, import), because the raw messages and the upload
they came from are deleted later.
"""

from dataclasses import dataclass
import json
import sqlite3

from naruto.db.database import Database, now_ts
from naruto.db.messages import fts_query
from naruto.periods import MONTH, RANGE, WEEK

ACTIVE = "active"
STAGED = "staged"
REPLACED = "replaced"

EXPORT = "export"
LIVE = "live"

GROUPINGS = (MONTH, WEEK, RANGE)

# Period statuses.
WAITING = "waiting"
RUNNING = "running"
DONE = "done"
REUSED = "reused"  # an identical digest already existed
FAILED = "failed"
CANCELLED = "cancelled"
FINISHED_PERIODS = (DONE, REUSED)


@dataclass
class HistoryDigest:
    id: int
    chat_id: int
    status: str
    source: str
    grouping: str
    timezone: str
    period_start: int
    period_end: int
    first_message_at: int | None
    last_message_at: int | None
    message_count: int
    import_id: int | None
    period_id: int | None
    fingerprint: str
    text: str
    limitations: list[str]
    edited: bool
    created_at: int
    updated_at: int
    updated_by: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "HistoryDigest":
        data = {name: row[name] for name in cls.__dataclass_fields__}
        data["limitations"] = json.loads(data["limitations"]) if data["limitations"] else []
        data["edited"] = bool(data["edited"])
        return cls(**data)

    def overlaps(self, start: int, end: int) -> bool:
        return self.period_start < end and start < self.period_end


@dataclass
class DigestEdit:
    id: int
    digest_id: int
    text: str
    changed_at: int
    changed_by: str


@dataclass
class HistoryPeriod:
    id: int
    chat_id: int
    source: str
    import_id: int | None
    grouping: str
    timezone: str
    period_start: int
    period_end: int
    status: str
    message_count: int
    consumed: int
    chunks_done: int
    partial: str | None
    fingerprint: str | None
    replaces: list[int]
    digest_id: int | None
    attempts: int
    error: str | None
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "HistoryPeriod":
        data = {name: row[name] for name in cls.__dataclass_fields__}
        data["replaces"] = json.loads(data["replaces"]) if data["replaces"] else []
        return cls(**data)


class HistoryRepository:
    def __init__(self, db: Database):
        self.db = db

    # -------------------------------------------------------------- digests

    def get(self, digest_id: int) -> HistoryDigest | None:
        row = self.db.query_one("SELECT * FROM history_digests WHERE id = ?", (digest_id,))
        return HistoryDigest.from_row(row) if row else None

    def for_chat(self, chat_id: int, *, status: str | None = ACTIVE, since: int | None = None,
                 until: int | None = None, query: str | None = None,
                 limit: int | None = None, offset: int = 0) -> tuple[list[HistoryDigest], int]:
        """Digests oldest first, overlapping [since, until), matching the
        words of ``query``. Returns (page, total)."""
        joins, where, params = "", ["d.chat_id = ?"], [chat_id]
        if query:
            match = fts_query(query)
            if match is None:
                return [], 0
            joins = "JOIN history_digests_fts ON history_digests_fts.rowid = d.id"
            where.append("history_digests_fts MATCH ?")
            params.append(match)
        if status:
            where.append("d.status = ?")
            params.append(status)
        if since is not None:
            where.append("d.period_end > ?")
            params.append(since)
        if until is not None:
            where.append("d.period_start < ?")
            params.append(until)
        clause = " AND ".join(where)
        total = int(self.db.scalar(
            f"SELECT COUNT(*) FROM history_digests d {joins} WHERE {clause}", params) or 0)
        sql = f"SELECT d.* FROM history_digests d {joins} WHERE {clause} " \
              "ORDER BY d.period_start, d.id"
        page_params = list(params)
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            page_params += [limit, offset]
        rows = self.db.query(sql, page_params)
        return [HistoryDigest.from_row(row) for row in rows], total

    def search(self, chat_id: int, *, query: str | None = None, since: int | None = None,
               until: int | None = None, limit: int = 3,
               offset: int = 0) -> tuple[list[HistoryDigest], int]:
        """Active digests for the bot's lookup: with words, the best matches
        first (any word may match); without, oldest first."""
        if not query:
            return self.for_chat(chat_id, since=since, until=until, limit=limit, offset=offset)
        words = [word for word in (fts_query(query) or "").split(" ") if word]
        if not words:
            return [], 0
        where = ["d.chat_id = ?", "d.status = 'active'", "history_digests_fts MATCH ?"]
        params: list = [chat_id, " OR ".join(words)]
        if since is not None:
            where.append("d.period_end > ?")
            params.append(since)
        if until is not None:
            where.append("d.period_start < ?")
            params.append(until)
        clause = " AND ".join(where)
        join = "JOIN history_digests_fts ON history_digests_fts.rowid = d.id"
        total = int(self.db.scalar(
            f"SELECT COUNT(*) FROM history_digests d {join} WHERE {clause}", params) or 0)
        rows = self.db.query(
            f"SELECT d.* FROM history_digests d {join} WHERE {clause} "
            "ORDER BY history_digests_fts.rank, d.period_start LIMIT ? OFFSET ?",
            (*params, limit, offset))
        return [HistoryDigest.from_row(row) for row in rows], total

    def overlapping(self, chat_id: int, start: int, end: int, *,
                    statuses: tuple[str, ...] = (ACTIVE,)) -> list[HistoryDigest]:
        placeholders = ", ".join("?" for _ in statuses)
        rows = self.db.query(
            f"SELECT * FROM history_digests WHERE chat_id = ? AND status IN ({placeholders}) "
            "AND period_start < ? AND period_end > ? ORDER BY period_start, id",
            (chat_id, *statuses, end, start))
        return [HistoryDigest.from_row(row) for row in rows]

    def coverage(self, chat_id: int) -> tuple[int | None, int | None, int]:
        """(earliest start, latest end, count) of the active digests."""
        row = self.db.query_one(
            "SELECT MIN(period_start), MAX(period_end), COUNT(*) FROM history_digests "
            "WHERE chat_id = ? AND status = 'active'", (chat_id,))
        return row[0], row[1], int(row[2] or 0)

    def counts_by_chat(self) -> dict[int, int]:
        return {row[0]: row[1] for row in self.db.query(
            "SELECT chat_id, COUNT(*) FROM history_digests WHERE status = 'active' "
            "GROUP BY chat_id")}

    def add(self, *, chat_id: int, status: str, source: str, grouping: str, timezone: str,
            period_start: int, period_end: int, first_message_at: int | None,
            last_message_at: int | None, message_count: int, import_id: int | None,
            period_id: int | None, fingerprint: str, text: str, limitations: list[str],
            actor: str) -> HistoryDigest:
        ts = now_ts()
        digest_id = self.db.execute(
            "INSERT INTO history_digests (chat_id, status, source, grouping, timezone, "
            "period_start, period_end, first_message_at, last_message_at, message_count, "
            "import_id, period_id, fingerprint, text, limitations, created_at, updated_at, "
            "updated_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_id, status, source, grouping, timezone, period_start, period_end,
             first_message_at, last_message_at, message_count, import_id, period_id,
             fingerprint, text.strip(), json.dumps(limitations, ensure_ascii=False) if
             limitations else None, ts, ts, actor)).lastrowid
        return self.get(digest_id)

    def edit(self, digest_id: int, text: str, *, actor: str) -> HistoryDigest | None:
        """An owner edit: the old text is kept in history_digest_edits; the
        coverage stays as it was."""
        current = self.get(digest_id)
        if current is None:
            return None
        text = text.strip()
        if text == current.text:
            return current
        ts = now_ts()
        with self.db.transaction():
            self.db.execute(
                "INSERT INTO history_digest_edits (digest_id, chat_id, text, changed_at, "
                "changed_by) VALUES (?, ?, ?, ?, ?)",
                (digest_id, current.chat_id, current.text, ts, actor))
            self.db.execute(
                "UPDATE history_digests SET text = ?, edited = 1, updated_at = ?, "
                "updated_by = ? WHERE id = ?", (text, ts, actor, digest_id))
        return self.get(digest_id)

    def edits(self, digest_id: int) -> list[DigestEdit]:
        rows = self.db.query(
            "SELECT * FROM history_digest_edits WHERE digest_id = ? ORDER BY id DESC",
            (digest_id,))
        return [DigestEdit(row["id"], row["digest_id"], row["text"], row["changed_at"],
                           row["changed_by"]) for row in rows]

    def delete(self, digest_id: int) -> bool:
        with self.db.transaction():
            self.db.execute("DELETE FROM history_digest_edits WHERE digest_id = ?",
                            (digest_id,))
            return self.db.execute("DELETE FROM history_digests WHERE id = ?",
                                   (digest_id,)).rowcount > 0

    def delete_for_chat(self, chat_id: int) -> int:
        """Every digest of the chat, and the periods that built them."""
        with self.db.transaction():
            self.db.execute("DELETE FROM history_digest_edits WHERE chat_id = ?", (chat_id,))
            self.db.execute("DELETE FROM history_periods WHERE chat_id = ? AND status IN "
                            "('done', 'reused', 'failed', 'cancelled')", (chat_id,))
            return self.db.execute("DELETE FROM history_digests WHERE chat_id = ?",
                                   (chat_id,)).rowcount

    def delete_staged(self, import_id: int) -> int:
        return self.db.execute(
            "DELETE FROM history_digests WHERE import_id = ? AND status = 'staged'",
            (import_id,)).rowcount

    def publish(self, staged_ids: list[int], replaced_ids: list[int], *, actor: str) -> None:
        """Swap staged digests in for the ones they replace, in one step."""
        ts = now_ts()
        with self.db.transaction():
            for digest_id in replaced_ids:
                self.db.execute(
                    "UPDATE history_digests SET status = 'replaced', updated_at = ?, "
                    "updated_by = ? WHERE id = ? AND status = 'active'", (ts, actor, digest_id))
            for digest_id in staged_ids:
                self.db.execute(
                    "UPDATE history_digests SET status = 'active', updated_at = ? "
                    "WHERE id = ? AND status = 'staged'", (ts, digest_id))

    # -------------------------------------------------------------- periods

    def add_period(self, *, chat_id: int, source: str, import_id: int | None, grouping: str,
                   timezone: str, period_start: int, period_end: int, message_count: int,
                   fingerprint: str | None, status: str = WAITING,
                   replaces: list[int] | None = None, digest_id: int | None = None,
                   error: str | None = None) -> HistoryPeriod:
        ts = now_ts()
        period_id = self.db.execute(
            "INSERT INTO history_periods (chat_id, source, import_id, grouping, timezone, "
            "period_start, period_end, status, message_count, fingerprint, replaces, "
            "digest_id, error, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_id, source, import_id, grouping, timezone, period_start, period_end, status,
             message_count, fingerprint, json.dumps(replaces) if replaces else None, digest_id,
             error, ts, ts)).lastrowid
        return self.get_period(period_id)

    def get_period(self, period_id: int) -> HistoryPeriod | None:
        row = self.db.query_one("SELECT * FROM history_periods WHERE id = ?", (period_id,))
        return HistoryPeriod.from_row(row) if row else None

    def periods_for_import(self, import_id: int) -> list[HistoryPeriod]:
        rows = self.db.query("SELECT * FROM history_periods WHERE import_id = ? "
                             "ORDER BY period_start, id", (import_id,))
        return [HistoryPeriod.from_row(row) for row in rows]

    def live_periods(self, chat_id: int) -> list[HistoryPeriod]:
        rows = self.db.query("SELECT * FROM history_periods WHERE chat_id = ? AND source = 'live' "
                             "ORDER BY period_start, id", (chat_id,))
        return [HistoryPeriod.from_row(row) for row in rows]

    def update_period(self, period_id: int, **fields) -> None:
        if "replaces" in fields and fields["replaces"] is not None:
            fields["replaces"] = json.dumps(fields["replaces"])
        fields["updated_at"] = now_ts()
        assignments = ", ".join(f"{name} = ?" for name in fields)
        self.db.execute(f"UPDATE history_periods SET {assignments} WHERE id = ?",
                        (*fields.values(), period_id))

    def checkpoint(self, period_id: int, *, partial: str, consumed: int,
                   chunks_done: int) -> None:
        """One more part of the period was read: keep the summary so far."""
        self.update_period(period_id, partial=partial, consumed=consumed,
                           chunks_done=chunks_done, attempts=0, error=None)

    def cancel_unfinished(self, import_id: int, reason: str) -> int:
        return self.db.execute(
            "UPDATE history_periods SET status = 'cancelled', error = ?, updated_at = ? "
            "WHERE import_id = ? AND status IN ('waiting', 'running', 'failed')",
            (reason, now_ts(), import_id)).rowcount
