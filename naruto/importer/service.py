"""Import flow (plan §6): upload -> preview -> choose the group -> import.

- Only messages from before the first live-recorded message are imported,
  so live and imported history never overlap.
- Only messages inside the imported-messages retention window are kept.
- Re-importing a chat replaces its previous import once the new one has
  succeeded, so repeating an import is safe.
- The uploaded file is deleted when the import finishes or is discarded.

The heavy work runs in a worker thread so the bot keeps answering.
"""

import asyncio
import bisect
from collections import Counter
from dataclasses import dataclass
import logging
from pathlib import Path
import time
from typing import IO
import uuid

from naruto.db.chats import Chat
from naruto.db.database import now_ts
from naruto.db.imports import (
    DISCARDED,
    DONE,
    FAILED,
    PREVIEW,
    REPLACED,
    RUNNING,
    ImportRecord,
    ImportRepository,
)
from naruto.db.messages import IMPORT, NewMessage
from naruto.importer.export_parser import ExportError, ExportReader
from naruto.services import Services

logger = logging.getLogger(__name__)

BATCH_SIZE = 500
TOP_PARTICIPANTS = 30
STALE_PREVIEW_SECONDS = 24 * 3600
_CHUNK = 1024 * 1024


class ImportProblem(ValueError):
    """Something the owner needs to fix; the message is shown on the page."""


class UploadTooLarge(ImportProblem):
    pass


@dataclass
class Match:
    chat: Chat | None
    how: str | None  # "id", "name" or None
    suggested_chat_id: int | None  # for creating a new pending chat


def _retention_cutoff(days: int, now: float | None = None) -> int | None:
    if days <= 0:
        return None
    return int((now or time.time()) - days * 86400)


class ImportService:
    def __init__(self, services: Services, upload_dir: Path):
        self.services = services
        self.repo = ImportRepository(services.db)
        self.upload_dir = Path(upload_dir)
        self._tasks: dict[int, asyncio.Task] = {}

    # --------------------------------------------------------------- upload

    def save_upload(self, source: IO[bytes], file_name: str, max_bytes: int) -> tuple[Path, int]:
        """Copy an uploaded file into the upload directory, enforcing the
        size limit while copying."""
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        path = self.upload_dir / f"{uuid.uuid4().hex}.json"
        size = 0
        try:
            with path.open("wb") as out:
                while chunk := source.read(_CHUNK):
                    size += len(chunk)
                    if size > max_bytes:
                        raise UploadTooLarge(
                            f"The file is larger than the {max_bytes // (1024 * 1024)} MB limit "
                            "(Settings → Import).")
                    out.write(chunk)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path, size

    async def create_from_upload(self, source: IO[bytes], file_name: str) -> ImportRecord:
        max_bytes = self.services.settings["import.max_upload_mb"] * 1024 * 1024
        path, size = await asyncio.to_thread(self.save_upload, source, file_name, max_bytes)
        record = self.repo.create(file_name=file_name[:200] or "result.json",
                                  file_path=str(path), file_size=size)
        try:
            preview = await asyncio.to_thread(self.analyse, path)
        except ExportError as exc:
            path.unlink(missing_ok=True)
            self.repo.update(record.id, status=FAILED, file_path=None, error=str(exc),
                             finished_at=now_ts())
            raise ImportProblem(str(exc)) from None
        self.repo.update(
            record.id,
            export_name=preview["name"], export_type=preview["type"],
            export_id=preview["export_id"], preview=preview, total=preview["total"],
            first_date=preview["first_date"], last_date=preview["last_date"],
        )
        logger.info("Uploaded export %s: %s messages from %r", record.id, preview["total"],
                    preview["type"])
        return self.repo.get(record.id)

    # -------------------------------------------------------------- preview

    def analyse(self, path: Path) -> dict:
        """One pass over the file: counts, date range, participants and how
        many messages each known chat would take."""
        messages = 0
        service = 0
        first = last = None
        dates: list[int] = []
        senders: Counter = Counter()
        names: dict = {}
        with path.open("rb") as handle:
            reader = ExportReader(handle)
            for message in reader.messages():
                if message.is_service:
                    service += 1
                    continue
                messages += 1
                dates.append(message.date)
                first = message.date if first is None else min(first, message.date)
                last = message.date if last is None else max(last, message.date)
                key = message.sender_id if message.sender_id is not None else message.sender_name
                senders[key] += 1
                names[key] = message.sender_name
        header = reader.header
        if not header.is_group:
            raise ExportError(
                f"This export is a {header.type or 'unknown'} chat. Only group exports can be imported.")
        dates.sort()
        # For every chat with live messages: how many export messages come
        # before its first live message (the ones an import would keep).
        before_live = {}
        for row in self.services.db.query(
                "SELECT chat_id, MIN(date) AS first_live FROM messages "
                "WHERE source = 'live' GROUP BY chat_id"):
            before_live[str(row["chat_id"])] = _count_before(dates, row["first_live"])
        return {
            "name": header.name,
            "type": header.type,
            "export_id": header.id,
            "candidate_ids": header.bot_api_chat_ids(),
            "total": messages,
            "service": service,
            "unreadable": reader.skipped,
            "first_date": first,
            "last_date": last,
            "participants": [
                {"id": key if isinstance(key, int) else None, "name": names[key], "count": count}
                for key, count in senders.most_common(TOP_PARTICIPANTS)
            ],
            "participant_count": len(senders),
            "dates": _date_histogram(dates),
            "before_live": before_live,
        }

    def estimate(self, record: ImportRecord, chat_id: int | None) -> dict:
        """What an import into ``chat_id`` would keep, from the preview."""
        preview = record.preview or {}
        total = preview.get("total", 0)
        kept_before_live = preview.get("before_live", {}).get(str(chat_id), total) \
            if chat_id is not None else total
        cutoff = _retention_cutoff(self.services.settings["retention.imported_messages_days"])
        in_retention = total if cutoff is None else _count_from_histogram(preview.get("dates", []), cutoff)
        return {
            "total": total,
            "overlap": total - kept_before_live,
            "in_retention": in_retention,
            "retention_days": self.services.settings["retention.imported_messages_days"],
        }

    def match(self, record: ImportRecord) -> Match:
        """Pre-select the target group: by chat ID (including old IDs from a
        group upgrade), then by name."""
        preview = record.preview or {}
        candidates = preview.get("candidate_ids", [])
        chats = self.services.chats
        for chat_id in candidates:
            chat = chats.get(chat_id)
            if chat is not None:
                return Match(chat, "id", None)
        by_name = chats.find_by_title(preview.get("name") or "") if preview.get("name") else []
        if len(by_name) == 1:
            return Match(by_name[0], "name", None)
        return Match(None, None, candidates[0] if candidates else None)

    # --------------------------------------------------------------- import

    async def start(self, import_id: int, chat_id: int) -> None:
        record = self.repo.get(import_id)
        if record is None or record.status != PREVIEW or not record.file_path:
            raise ImportProblem("This import can no longer be started.")
        if import_id in self._tasks:
            return
        chat = self.services.chats.get(chat_id)
        if chat is None:
            # Import before the bot joins: the chat starts out pending.
            chat, _ = self.services.chats.upsert_seen(chat_id, title=record.export_name)
        self.repo.update(import_id, status=RUNNING, chat_id=chat.chat_id, started_at=now_ts(),
                         processed=0, imported=0)
        task = asyncio.create_task(asyncio.to_thread(self.run, import_id, chat.chat_id))
        self._tasks[import_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(import_id, None))

    def run(self, import_id: int, chat_id: int) -> None:
        """The import job (runs in a worker thread)."""
        record = self.repo.get(import_id)
        messages = self.services.messages
        bot = self.services.status.bot
        bot_id = bot.id if bot else None
        first_live = messages.first_live_date(chat_id)
        cutoff = _retention_cutoff(self.services.settings["retention.imported_messages_days"])
        counts = Counter()
        seen_ids: set[int] = set()
        next_duplicate_id = -1
        roster: dict[int, tuple[str, int]] = {}
        batch: list[NewMessage] = []
        first_kept = last_kept = None

        def flush():
            if batch:
                messages.insert_imported(batch)
                counts["imported"] += len(batch)
                batch.clear()
            self.repo.update(import_id, processed=counts["processed"],
                             imported=counts["imported"],
                             skipped_overlap=counts["overlap"],
                             skipped_retention=counts["retention"],
                             skipped_service=counts["service"])

        try:
            with open(record.file_path, "rb") as handle:
                for message in ExportReader(handle).messages():
                    if message.is_service:
                        counts["service"] += 1
                        continue
                    counts["processed"] += 1
                    if first_live is not None and message.date >= first_live:
                        counts["overlap"] += 1
                    elif cutoff is not None and message.date < cutoff:
                        counts["retention"] += 1
                    else:
                        message_id = message.id
                        if message_id in seen_ids:
                            # Merged histories can repeat IDs; keep both.
                            message_id, next_duplicate_id = next_duplicate_id, next_duplicate_id - 1
                        seen_ids.add(message_id)
                        from_bot = bot_id is not None and message.sender_id == bot_id
                        batch.append(NewMessage(
                            chat_id=chat_id, origin_chat_id=chat_id, source=IMPORT,
                            message_id=message_id, import_id=import_id,
                            sender_id=message.sender_id, sender_name=message.sender_name,
                            from_bot=from_bot, date=message.date, edit_date=message.edit_date,
                            text=message.text, media_kind=message.media_kind,
                            media_meta=message.media_meta, forwarded_from=message.forwarded_from,
                            reply_to_message_id=message.reply_to_id,
                        ))
                        first_kept = message.date if first_kept is None else min(first_kept, message.date)
                        last_kept = message.date if last_kept is None else max(last_kept, message.date)
                        if message.sender_id and message.sender_id > 0 and not from_bot:
                            previous = roster.get(message.sender_id)
                            first_seen = min(previous[1], message.date) if previous else message.date
                            roster[message.sender_id] = (message.sender_name, first_seen)
                    if len(batch) >= BATCH_SIZE or counts["processed"] % BATCH_SIZE == 0:
                        flush()
            flush()
            messages.resolve_import_replies(import_id)
            for user_id, (name, first_seen) in roster.items():
                self.services.members.upsert_imported(chat_id, user_id, name, first_seen)
            for old in self.repo.for_chat(chat_id):
                if old.id != import_id and old.status == DONE:
                    removed = messages.delete_import(old.id)
                    self.repo.update(old.id, status=REPLACED)
                    logger.info("Import %s replaced import %s (%s messages removed)",
                                import_id, old.id, removed, extra={"chat_id": chat_id})
            self.repo.update(import_id, status=DONE, finished_at=now_ts(),
                             first_date=first_kept, last_date=last_kept)
            logger.info("Import %s done: %s imported, %s overlapped live history, %s outside "
                        "retention", import_id, counts["imported"], counts["overlap"],
                        counts["retention"], extra={"chat_id": chat_id})
        except Exception as exc:
            messages.delete_import(import_id)
            self.repo.update(import_id, status=FAILED, finished_at=now_ts(),
                             error=f"{type(exc).__name__}: {exc}"[:500])
            logger.exception("Import %s failed", import_id, extra={"chat_id": chat_id})
        finally:
            self._delete_file(import_id)

    # ------------------------------------------------------------ lifecycle

    def discard(self, import_id: int) -> None:
        record = self.repo.get(import_id)
        if record is None or record.status != PREVIEW:
            raise ImportProblem("Only an import that hasn't started can be discarded.")
        self._delete_file(import_id)
        self.repo.update(import_id, status=DISCARDED, finished_at=now_ts())

    def _delete_file(self, import_id: int) -> None:
        record = self.repo.get(import_id)
        if record and record.file_path:
            Path(record.file_path).unlink(missing_ok=True)
            self.repo.update(import_id, file_path=None)

    def recover(self) -> None:
        """At startup: an import that was running when the process stopped
        is rolled back and marked failed."""
        for record in self.repo.with_status(RUNNING):
            self.services.messages.delete_import(record.id)
            self.repo.update(record.id, status=FAILED, finished_at=now_ts(),
                             error="Interrupted by a restart. Upload the file again.")
            self._delete_file(record.id)
            logger.warning("Import %s was interrupted by a restart and rolled back", record.id)

    def cleanup_stale_previews(self, max_age: int = STALE_PREVIEW_SECONDS) -> int:
        """Discard previews nobody started, so uploaded files don't linger."""
        cutoff = now_ts() - max_age
        stale = [r for r in self.repo.with_status(PREVIEW) if r.created_at < cutoff]
        for record in stale:
            self.discard(record.id)
        return len(stale)

    def running(self) -> bool:
        return bool(self._tasks)


def _count_before(sorted_dates: list[int], limit: int) -> int:
    return bisect.bisect_left(sorted_dates, limit)


def _date_histogram(sorted_dates: list[int]) -> list[list[int]]:
    """Message counts per UTC day, [[day_start, count], ...], so retention
    estimates don't need the whole file again."""
    histogram: list[list[int]] = []
    for date in sorted_dates:
        day = date - date % 86400
        if histogram and histogram[-1][0] == day:
            histogram[-1][1] += 1
        else:
            histogram.append([day, 1])
    return histogram


def _count_from_histogram(histogram: list[list[int]], cutoff: int) -> int:
    """Messages on or after the cutoff day (day resolution)."""
    cutoff_day = cutoff - cutoff % 86400
    return sum(count for day, count in histogram if day >= cutoff_day)
