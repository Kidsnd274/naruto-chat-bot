"""SQLite connection wrapper.

One shared connection, serialized by a re-entrant lock. The bot, the web
admin and background jobs all run on one asyncio loop and every statement is
short, so a single connection is simpler than a pool. Long jobs (imports) run
in a worker thread and take the lock per batch.
"""

from contextlib import contextmanager
import logging
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Iterator, Sequence

from naruto.db.migrations import MIGRATIONS

logger = logging.getLogger(__name__)


def now_ts() -> int:
    return int(time.time())


class Database:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._depth = 0
        self._conn = sqlite3.connect(
            path,
            check_same_thread=False,
            isolation_level=None,  # explicit transactions only
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")

    # ------------------------------------------------------------------ core

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block atomically. Nested calls join the outer transaction."""
        with self._lock:
            if self._depth:
                self._depth += 1
                try:
                    yield self._conn
                finally:
                    self._depth -= 1
                return
            self._conn.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield self._conn
            except BaseException:
                self._depth = 0
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._depth = 0
                self._conn.execute("COMMIT")

    def execute(self, sql: str, params: Sequence[Any] | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def executemany(self, sql: str, rows) -> None:
        with self._lock:
            self._conn.executemany(sql, rows)

    def query(self, sql: str, params: Sequence[Any] | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] | dict = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Sequence[Any] | dict = ()) -> Any:
        row = self.query_one(sql, params)
        return row[0] if row is not None else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------ migrations

    @property
    def schema_version(self) -> int:
        return int(self.scalar("PRAGMA user_version") or 0)

    def migrate(self) -> None:
        current = self.schema_version
        for version, script in enumerate(MIGRATIONS, start=1):
            if version <= current:
                continue
            logger.info("Applying database migration %s", version)
            with self._lock:
                # executescript() commits any open transaction first, so wrap
                # the script and the version bump in one explicit transaction.
                try:
                    self._conn.executescript(
                        f"BEGIN IMMEDIATE;\n{script}\n"
                        f"PRAGMA user_version = {version};\nCOMMIT;"
                    )
                except BaseException:
                    if self._conn.in_transaction:
                        self._conn.execute("ROLLBACK")
                    raise
        if current < len(MIGRATIONS):
            logger.info("Database schema is at version %s", len(MIGRATIONS))

    # ------------------------------------------------------------------ meta

    def get_meta(self, key: str) -> str | None:
        return self.scalar("SELECT value FROM meta WHERE key = ?", (key,))

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def open_database(path: str) -> Database:
    db = Database(path)
    db.migrate()
    return db
