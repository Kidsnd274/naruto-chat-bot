"""History imports (one row per uploaded export)."""

from dataclasses import dataclass
import json
import sqlite3

from naruto.db.database import Database, now_ts

_JSON_FIELDS = ("preview", "options", "limitations")

# Overall status of an import.
PREVIEW = "preview"
RUNNING = "running"
PAUSED = "paused"  # a stage stopped after errors or by the owner; resumable
DONE = "done"
PARTIAL = "partial"  # finished, but a stage failed, was cancelled or expired
FAILED = "failed"
REPLACED = "replaced"  # a later import replaced all its messages
DISCARDED = "discarded"

# Status of each stage (raw_status, archive_status, distill_status).
STAGE_SKIPPED = "skipped"  # not asked for
STAGE_WAITING = "waiting"
STAGE_RUNNING = "running"
STAGE_PAUSED = "paused"
STAGE_DONE = "done"
STAGE_FAILED = "failed"
STAGE_CANCELLED = "cancelled"
STAGE_EXPIRED = "expired"  # the uploaded file was deleted before it finished
UNFINISHED_STAGES = (STAGE_WAITING, STAGE_RUNNING, STAGE_PAUSED)


@dataclass
class ImportRecord:
    id: int
    chat_id: int | None
    status: str
    file_name: str
    file_path: str | None
    file_size: int
    export_name: str
    export_type: str
    export_id: int | None
    preview: dict | None
    total: int
    processed: int
    imported: int
    skipped_overlap: int
    skipped_retention: int
    skipped_service: int
    first_date: int | None
    last_date: int | None
    error: str | None
    created_at: int
    started_at: int | None
    finished_at: int | None
    options: dict | None = None  # what the owner chose, frozen at start (planning.py)
    skipped_range: int = 0
    raw_status: str | None = None
    archive_status: str | None = None
    archive_total: int = 0  # periods with messages
    archive_done: int = 0
    archive_error: str | None = None
    distill_status: str | None = None
    distill_total: int = 0
    distill_done: int = 0
    notes_added: int = 0
    distill_error: str | None = None
    limitations: list | None = None
    paused_at: int | None = None
    source_expires_at: int | None = None  # a paused import's file is deleted then

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ImportRecord":
        data = {name: row[name] for name in cls.__dataclass_fields__}
        for name in _JSON_FIELDS:
            data[name] = json.loads(data[name]) if data[name] else None
        return cls(**data)

    @property
    def stages(self) -> list[tuple[str, str]]:
        """(name, status) of the stages that were asked for."""
        return [(name, status) for name, status in (
            ("raw", self.raw_status), ("archive", self.archive_status),
            ("distill", self.distill_status)) if status and status != STAGE_SKIPPED]

    @property
    def unfinished(self) -> bool:
        return any(status in UNFINISHED_STAGES for _, status in self.stages)

    @property
    def archive_percent(self) -> int:
        if not self.archive_total:
            return 0
        return min(100, int(self.archive_done * 100 / self.archive_total))

    @property
    def progress_percent(self) -> int:
        if not self.total:
            return 0
        return min(100, int(self.processed * 100 / self.total))

    @property
    def distill_percent(self) -> int:
        if not self.distill_total:
            return 0
        return min(100, int(self.distill_done * 100 / self.distill_total))


class ImportRepository:
    def __init__(self, db: Database):
        self.db = db

    def create(self, *, file_name: str, file_path: str, file_size: int) -> ImportRecord:
        cursor = self.db.execute(
            "INSERT INTO imports (status, file_name, file_path, file_size, created_at) "
            "VALUES ('preview', ?, ?, ?, ?)",
            (file_name, file_path, file_size, now_ts()),
        )
        return self.get(cursor.lastrowid)

    def get(self, import_id: int) -> ImportRecord | None:
        row = self.db.query_one("SELECT * FROM imports WHERE id = ?", (import_id,))
        return ImportRecord.from_row(row) if row else None

    def update(self, import_id: int, **fields) -> None:
        for name in _JSON_FIELDS:
            if name in fields and fields[name] is not None:
                fields[name] = json.dumps(fields[name], ensure_ascii=False)
        assignments = ", ".join(f"{name} = ?" for name in fields)
        self.db.execute(f"UPDATE imports SET {assignments} WHERE id = ?",
                        (*fields.values(), import_id))

    def recent(self, limit: int = 30) -> list[ImportRecord]:
        rows = self.db.query("SELECT * FROM imports ORDER BY id DESC LIMIT ?", (limit,))
        return [ImportRecord.from_row(row) for row in rows]

    def for_chat(self, chat_id: int) -> list[ImportRecord]:
        rows = self.db.query("SELECT * FROM imports WHERE chat_id = ? ORDER BY id DESC",
                             (chat_id,))
        return [ImportRecord.from_row(row) for row in rows]

    def with_status(self, *statuses: str) -> list[ImportRecord]:
        placeholders = ", ".join("?" for _ in statuses)
        rows = self.db.query(f"SELECT * FROM imports WHERE status IN ({placeholders}) "
                             "ORDER BY id", statuses)
        return [ImportRecord.from_row(row) for row in rows]

    def with_distill_status(self, status: str) -> list[ImportRecord]:
        rows = self.db.query("SELECT * FROM imports WHERE distill_status = ? ORDER BY id",
                             (status,))
        return [ImportRecord.from_row(row) for row in rows]
