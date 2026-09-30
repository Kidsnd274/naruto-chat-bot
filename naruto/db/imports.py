"""History imports (one row per uploaded export)."""

from dataclasses import dataclass
import json
import sqlite3

from naruto.db.database import Database, now_ts

PREVIEW = "preview"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
REPLACED = "replaced"
DISCARDED = "discarded"


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
    distill_status: str | None = None  # running | done | failed | skipped
    distill_total: int = 0
    distill_done: int = 0
    notes_added: int = 0
    distill_error: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ImportRecord":
        data = {name: row[name] for name in cls.__dataclass_fields__}
        data["preview"] = json.loads(data["preview"]) if data["preview"] else None
        return cls(**data)

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
        if "preview" in fields and fields["preview"] is not None:
            fields["preview"] = json.dumps(fields["preview"], ensure_ascii=False)
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
