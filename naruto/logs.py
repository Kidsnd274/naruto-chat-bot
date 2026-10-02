"""Logging setup: console output plus a buffered handler that stores records
in SQLite for the web admin's Logs page.

The chat an update belongs to is tracked in a context variable, so every log
line written while handling that update can be filtered by chat.
"""

import asyncio
from collections import deque
from contextvars import ContextVar
import logging
import re
import threading

from naruto.db.logs import LogRepository

current_chat_id: ContextVar[int | None] = ContextVar("current_chat_id", default=None)

_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
# No \b at the start: in URLs the token follows "bot" directly.
_TOKEN = re.compile(r"(?<!\d)\d{5,}:[A-Za-z0-9_-]{30,}(?![A-Za-z0-9_-])")
# Third-party loggers that are noisy at INFO. httpx also logs every request
# URL, which contains the bot token.
_QUIET_LOGGERS = ("httpx", "httpcore", "openai", "uvicorn.access")


def redact(text: str) -> str:
    return _TOKEN.sub("<bot-token>", text)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class DatabaseLogHandler(logging.Handler):
    """Buffers records in memory; flush() writes them to the database.

    Writing happens on a timer rather than inside emit(), so logging never
    waits on the database and a database error cannot recurse into logging.
    """

    def __init__(self, level: int = logging.INFO, max_buffer: int = 10_000):
        super().__init__(level)
        self._buffer: deque = deque(maxlen=max_buffer)
        self._buffer_lock = threading.Lock()
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            if record.exc_info and not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            if record.exc_text and record.exc_text not in message:
                message = f"{message}\n{record.exc_text}"
            chat_id = getattr(record, "chat_id", None)
            if chat_id is None:
                chat_id = current_chat_id.get()
            row = (record.created, record.levelno, record.name, chat_id, redact(message))
            with self._buffer_lock:
                self._buffer.append(row)
        except Exception:
            self.handleError(record)

    def drain(self) -> list[tuple]:
        with self._buffer_lock:
            rows = list(self._buffer)
            self._buffer.clear()
        return rows

    def flush_to(self, repository: LogRepository) -> None:
        rows = self.drain()
        if rows:
            repository.insert_many(rows)


def setup_logging(level: str = "INFO") -> DatabaseLogHandler:
    """Configure the root logger. Returns the database handler, which main()
    connects to the database once it is open."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    console = logging.StreamHandler()
    console.setFormatter(RedactingFormatter(_FORMAT))
    root.addHandler(console)
    db_handler = DatabaseLogHandler(logging.DEBUG)
    root.addHandler(db_handler)
    set_level(level)
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    return db_handler


def set_level(level: str) -> None:
    logging.getLogger().setLevel(getattr(logging, level.upper(), logging.INFO))


async def flush_periodically(
    handler: DatabaseLogHandler,
    repository: LogRepository,
    interval: float = 1.0,
) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            handler.flush_to(repository)
        except Exception as exc:  # never log from here: it would recurse
            print(f"Could not store log records: {type(exc).__name__}: {exc}")
