"""Stored application log records for the web admin's Logs page."""

from dataclasses import dataclass
import logging

from naruto.db.database import Database


@dataclass
class LogRecordRow:
    id: int
    created_at: float
    level: int
    logger: str
    chat_id: int | None
    message: str

    @property
    def level_name(self) -> str:
        return logging.getLevelName(self.level)


class LogRepository:
    def __init__(self, db: Database):
        self.db = db

    def insert_many(self, rows: list[tuple[float, int, str, int | None, str]]) -> None:
        if rows:
            with self.db.transaction():
                self.db.executemany(
                    "INSERT INTO logs (created_at, level, logger, chat_id, message) "
                    "VALUES (?, ?, ?, ?, ?)",
                    rows,
                )

    def query(
        self,
        *,
        min_level: int = logging.DEBUG,
        chat_id: int | None = None,
        logger_prefix: str | None = None,
        after_id: int | None = None,
        limit: int = 200,
    ) -> list[LogRecordRow]:
        """Newest first. ``after_id`` returns only records newer than it (for
        the live tail)."""
        where = ["level >= ?"]
        params: list = [min_level]
        if chat_id is not None:
            where.append("chat_id = ?")
            params.append(chat_id)
        if logger_prefix:
            where.append("(logger = ? OR logger LIKE ?)")
            params.extend([logger_prefix, logger_prefix + ".%"])
        if after_id is not None:
            where.append("id > ?")
            params.append(after_id)
        rows = self.db.query(
            f"SELECT * FROM logs WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
            (*params, limit),
        )
        return [LogRecordRow(**dict(row)) for row in rows]

    def recent_errors(self, limit: int = 10) -> list[LogRecordRow]:
        return self.query(min_level=logging.ERROR, limit=limit)

    def chat_ids(self) -> list[int]:
        return [row[0] for row in self.db.query(
            "SELECT DISTINCT chat_id FROM logs WHERE chat_id IS NOT NULL ORDER BY chat_id")]

    def delete_older_than(self, cutoff: float) -> int:
        return self.db.execute("DELETE FROM logs WHERE created_at < ?", (cutoff,)).rowcount
