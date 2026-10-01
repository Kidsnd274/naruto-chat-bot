"""Import flow: upload -> preview -> choose what to keep and summarize ->
run the stages (plans/IMPORTED_HISTORY_DIGEST_TECH_PLAN.md).

An import has up to two stages, each with its own status:

1. raw: messages in the chosen dates are stored (however old), if they come
   before live recording began. Earlier imports' messages in the same dates
   are replaced once the new ones are in. They are kept until the owner
   deletes them.
2. archive: the chosen dates are summarized into history digests, period by
   period, straight from the uploaded file (naruto.memory.history).

An import never adds memory notes or touches the rolling digest: those come
from live chat only (plans/MEMORY_SIMPLIFICATION_AND_STABILITY_PLAN.md).

A stage that keeps failing, or that the owner pauses, pauses the import: its
uploaded file is kept for history.source_keep_days from the first pause, so
it can resume. A restart resumes running imports from their checkpoints. The
file is deleted once every stage has finished, or when the owner cancels the
rest.

Pausing or cancelling stops the job at once, wherever it waits (the chat's
history lock, a queue slot, a retry delay, a model request), by cancelling
its task; the request in progress is redone on resume. A worker thread (the
raw stage, reading the export) can't be interrupted: it checks the flag
between messages, and the job records the outcome once it has stopped.

The heavy reading runs in worker threads so the bot keeps answering; model
requests run at background priority.
"""

from array import array
import asyncio
from collections import Counter
from dataclasses import dataclass
import logging
from pathlib import Path
import threading
import time
from typing import IO
import uuid
from zoneinfo import ZoneInfo

from naruto.agent.text import estimate_text_tokens
from naruto.db.chats import Chat
from naruto.db.database import now_ts
from naruto.db.history import (
    ALREADY_PUBLISHED,
    CANCELLED as PERIOD_CANCELLED,
    DONE as PERIOD_DONE,
    FAILED as PERIOD_FAILED,
    INCOMPLETE,
    KEPT_EDITED,
    PUBLISHED,
    REUSED,
    RUNNING as PERIOD_RUNNING,
    STAGED,
    WAITING as PERIOD_WAITING,
    HistoryPeriod,
)
from naruto.db.imports import (
    DISCARDED,
    DONE,
    FAILED,
    PARTIAL,
    PAUSED,
    PREVIEW,
    REPLACED,
    RUNNING,
    STAGE_CANCELLED,
    STAGE_DONE,
    STAGE_EXPIRED,
    STAGE_PAUSED,
    STAGE_RUNNING,
    STAGE_SKIPPED,
    STAGE_WAITING,
    UNFINISHED_STAGES,
    ImportRecord,
    ImportRepository,
)
from naruto.db.messages import IMPORT, NewMessage
from naruto.importer.export_parser import ExportError, ExportReader
from naruto.importer.planning import ImportOptions, ImportPlan, make_plan
from naruto.memory.history import (
    FINISHED,
    PAUSED_BY_QUEUE,
    ExportSource,
    HistoryFailed,
    HistoryWriter,
    SourceStopped,
    WriteOptions,
    day_hashes,
    export_body,
    fingerprint,
    message_hash,
)
from naruto.periods import day_start, day_text, describe_span, local_date, plan_periods
from naruto.services import Services

logger = logging.getLogger(__name__)

BATCH_SIZE = 500
MAX_PARTICIPANTS = 1000  # stored in the preview for identity mapping
STALE_PREVIEW_SECONDS = 24 * 3600
SHUTDOWN_GRACE_SECONDS = 10
LINE_OVERHEAD_TOKENS = 12
_CHUNK = 1024 * 1024

# What a stage reports back to the job.
STAGE_OK = "ok"
STAGE_STOP = "stop"  # shutting down, paused or cancelled: see _after_stop()
STAGE_PAUSE = "pause"  # it paused itself after errors


class ImportProblem(ValueError):
    """Something the owner needs to fix; the message is shown on the page."""


class UploadTooLarge(ImportProblem):
    pass


class ImportCancelled(Exception):
    """The raw stage was stopped (shutdown or pause); it is rolled back."""


@dataclass
class IdentityRow:
    """One person in an export, for the preview's identity mapping."""
    user_id: int
    export_name: str
    count: int
    person: object | None  # naruto.db.people.Person when the account is known
    suggested_name: str


@dataclass
class Match:
    chat: Chat | None
    how: str | None  # "id", "name" or None
    suggested_chat_id: int | None  # for creating a new pending chat


def dates_path(file_path: str) -> Path:
    """The preview's sorted message times, next to the upload."""
    return Path(file_path).with_suffix(".dates")


class ImportService:
    def __init__(self, services: Services, upload_dir: Path):
        self.services = services
        self.repo = ImportRepository(services.db)
        self.upload_dir = Path(upload_dir)
        self._tasks: dict[int, asyncio.Task] = {}
        self._stopping = threading.Event()
        self._pause: set[int] = set()
        self._cancel: set[int] = set()
        self._in_thread: set[int] = set()  # jobs waiting for a worker thread
        self._started: set[int] = set()  # jobs past their first step
        self._dates: dict[str, list[int]] = {}

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
        except Exception as exc:
            message = str(exc) if isinstance(exc, ExportError) else \
                f"Could not read the file ({type(exc).__name__})."
            if not isinstance(exc, ExportError):
                logger.exception("Reading upload %s failed", record.id)
            self._delete_file(record.id, path=str(path))
            self.repo.update(record.id, status=FAILED, file_path=None, error=message,
                             finished_at=now_ts())
            raise ImportProblem(message) from None
        self._save_preview(record.id, preview)
        logger.info("Uploaded export %s: %s messages from %r", record.id, preview["total"],
                    preview["type"])
        return self.repo.get(record.id)

    def _save_preview(self, import_id: int, preview: dict) -> None:
        self.repo.update(
            import_id,
            export_name=preview["name"], export_type=preview["type"],
            export_id=preview["export_id"], preview=preview, total=preview["total"],
            first_date=preview["first_date"], last_date=preview["last_date"],
        )

    # -------------------------------------------------------------- preview

    def _tz_name(self) -> str:
        return self.services.settings["general.timezone"] or "server"

    def analyse(self, path: Path) -> dict:
        """One pass over the file: counts, dates, participants, and per local
        day the messages, their estimated tokens and a content hash. The
        sorted message times go to a file next to the upload."""
        services = self.services
        tz = services.timezone()
        max_chars = services.settings["context.max_message_chars"]
        service = 0
        dates: list[int] = []
        days: dict[int, list[int]] = {}
        day_of: dict[int, int] = {}  # 15-minute slot -> local day start (offsets are 15-min)
        senders: Counter = Counter()
        names: dict = {}
        with Path(path).open("rb") as handle:
            reader = ExportReader(handle)
            for message in reader.messages():
                if message.is_service:
                    service += 1
                    continue
                dates.append(message.date)
                slot = message.date // 900
                day = day_of.get(slot)
                if day is None:
                    day = day_of[slot] = day_start(local_date(message.date, tz), tz)
                body = export_body(message)
                entry = days.setdefault(day, [0, 0, 0])
                entry[0] += 1
                if body:
                    entry[1] += estimate_text_tokens(body[:max_chars]) + LINE_OVERHEAD_TOKENS
                entry[2] = (entry[2] + message_hash(message.id, message.date, message.sender_id,
                                                    body)) % (1 << 64)
                key = message.sender_id if message.sender_id is not None else message.sender_name
                senders[key] += 1
                names[key] = message.sender_name
        header = reader.header
        if not header.is_group:
            raise ExportError(
                f"This export is a {header.type or 'unknown'} chat. Only group exports can be imported.")
        dates.sort()
        with dates_path(str(path)).open("wb") as out:
            array("q", dates).tofile(out)
        return {
            "name": header.name,
            "type": header.type,
            "export_id": header.id,
            "candidate_ids": header.bot_api_chat_ids(),
            "total": len(dates),
            "service": service,
            "unreadable": reader.skipped,
            "first_date": dates[0] if dates else None,
            "last_date": dates[-1] if dates else None,
            "participants": [
                {"id": key if isinstance(key, int) else None, "name": names[key], "count": count}
                for key, count in senders.most_common(MAX_PARTICIPANTS)
            ],
            "participant_count": len(senders),
            "tz": self._tz_name(),
            "days": [[day, count, tokens, format(digest, "016x")]
                     for day, (count, tokens, digest) in sorted(days.items())],
        }

    async def refresh_preview(self, record: ImportRecord) -> ImportRecord:
        """Read the file again if the time zone changed since the upload
        (days are local) or the preview predates per-day histograms."""
        preview = record.preview or {}
        if record.status != PREVIEW or not record.file_path:
            return record
        if preview.get("tz") == self._tz_name() and "days" in preview \
                and dates_path(record.file_path).exists():
            return record
        preview = await asyncio.to_thread(self.analyse, Path(record.file_path))
        self._dates.pop(record.file_path, None)
        self._save_preview(record.id, preview)
        return self.repo.get(record.id)

    def message_dates(self, record: ImportRecord) -> list[int]:
        path = record.file_path
        if not path:
            return []
        if path not in self._dates:
            values = array("q")
            with dates_path(path).open("rb") as handle:
                values.frombytes(handle.read())
            if len(self._dates) >= 4:
                self._dates.pop(next(iter(self._dates)))
            self._dates[path] = values.tolist()
        return self._dates[path]

    def default_options(self, record: ImportRecord) -> ImportOptions:
        return ImportOptions.defaults(record.preview or {}, self.services)

    def plan(self, record: ImportRecord, chat_id: int | None,
             options: ImportOptions | None = None) -> ImportPlan:
        """What importing ``record`` into ``chat_id`` with ``options`` would
        do (None: a new chat)."""
        options = options or self.default_options(record)
        chat = self.services.chats.get(chat_id) if chat_id is not None else None
        return make_plan(record.preview or {}, self.message_dates(record), options, chat,
                         self.services, chat_id=chat.chat_id if chat else chat_id)

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

    # ------------------------------------------------------------ identities

    def identity_rows(self, record: ImportRecord) -> list[IdentityRow]:
        """The export's senders with their known person, if any. The suggested
        name is the person's chosen name, else the export's name for them
        (the exporting account's contact name)."""
        bot = self.services.status.bot
        participants = [p for p in (record.preview or {}).get("participants", [])
                        if p.get("id") and p["id"] > 0 and not (bot and p["id"] == bot.id)]
        known = self.services.people.for_users([p["id"] for p in participants])
        rows = []
        for p in participants:
            person = known.get(p["id"])
            suggested = person.name if person is not None and person.name else p["name"]
            rows.append(IdentityRow(p["id"], p["name"], p["count"], person, suggested))
        return rows

    def apply_identities(self, record: ImportRecord, identities: dict[int, dict]) -> None:
        """Apply the owner's choices from the preview before importing.

        ``identities`` maps user IDs to ``{"name": str, "merge_into": person
        id or None}``. An empty name leaves the person's name alone. When an
        account is merged into someone who already has a name, that name is
        kept.
        """
        people = self.services.people
        export_names = {p["id"]: p["name"] for p in (record.preview or {}).get("participants", [])
                        if p.get("id")}
        seen_at = record.first_date or now_ts()
        for user_id, choice in identities.items():
            if user_id not in export_names:
                continue
            person_id = people.touch_imported(user_id, export_names[user_id], seen_at)
            target = choice.get("merge_into")
            merged = False
            if target and target != person_id and people.get(target) is not None:
                person_id = people.move_account(user_id, target).id
                merged = True
            name = (choice.get("name") or "").strip()
            person = people.get(person_id)
            if not name or (merged and person.name):
                continue
            if person.name or name != person.display_name:
                # Only pin a name that changes what is shown; a name equal to
                # the Telegram one would stop following later renames.
                people.set_name(person_id, name)

    # ---------------------------------------------------------------- start

    def busy_import(self, chat_id: int, *, except_id: int | None = None) -> ImportRecord | None:
        """Another import of this chat that is running or paused."""
        for other in self.repo.for_chat(chat_id):
            if other.id != except_id and other.status in (RUNNING, PAUSED):
                return other
        return None

    async def start(self, import_id: int, chat_id: int,
                    identities: dict[int, dict] | None = None,
                    options: ImportOptions | None = None) -> ImportPlan:
        record = self.repo.get(import_id)
        if record is None or record.status != PREVIEW or not record.file_path:
            raise ImportProblem("This import can no longer be started.")
        if import_id in self._tasks:
            raise ImportProblem("This import is already running.")
        record = await self.refresh_preview(record)
        resolved = self.services.chats.resolve(chat_id)
        busy = self.busy_import(resolved)
        if busy is not None:
            raise ImportProblem(f"Import #{busy.id} of this group is {busy.status}. Let it finish, "
                                "or cancel the rest of it, before starting another.")
        plan = self.plan(record, resolved, options)
        if plan.errors:
            raise ImportProblem(" ".join(plan.errors))
        if identities:
            self.apply_identities(record, identities)
        chat = self.services.chats.get(resolved)
        if chat is None:
            # Import before the bot joins: the chat starts out pending.
            chat, _ = self.services.chats.upsert_seen(resolved, title=record.export_name)
        frozen = plan.frozen(self.services)

        def status(stage: dict) -> str:
            return STAGE_WAITING if stage["enabled"] else STAGE_SKIPPED

        self.repo.update(import_id, status=RUNNING, chat_id=chat.chat_id, started_at=now_ts(),
                         options=frozen, processed=0, imported=0,
                         raw_status=status(frozen["raw"]),
                         archive_status=status(frozen["archive"]),
                         archive_total=0, archive_done=0)
        self._spawn(import_id)
        return plan

    def _spawn(self, import_id: int) -> None:
        task = asyncio.create_task(self._job(import_id))
        self._tasks[import_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(import_id, None))

    # ------------------------------------------------------------------ job

    def _should_stop(self, import_id: int) -> bool:
        return self._stopping.is_set() or import_id in self._pause or import_id in self._cancel

    def _zone(self, record: ImportRecord):
        name = (record.options or {}).get("tz") or "server"
        return self.services.timezone() if name == "server" else ZoneInfo(name)

    async def _job(self, import_id: int) -> None:
        self._started.add(import_id)
        record = self.repo.get(import_id)
        try:
            async with self.services.history_locks[record.chat_id]:
                await self._run_stages(import_id)
        except asyncio.CancelledError:
            if import_id not in self._pause and import_id not in self._cancel:
                raise  # shutting down: resumed at the next start
            # The owner paused or cancelled it while it waited.
            self._after_stop(import_id, self._current_stage(self.repo.get(import_id)) or "raw")
        except Exception as exc:
            logger.exception("Import %s failed", import_id, extra={"chat_id": record.chat_id})
            stage = self._current_stage(self.repo.get(import_id))
            self._pause_stage(import_id, stage or "raw", f"{type(exc).__name__}: {exc}")
        finally:
            self._started.discard(import_id)
            if not self._stopping.is_set():
                # The owner's pause or cancel was handled (here, or by the
                # job noticing the flag first): the task doesn't end cancelled.
                task = asyncio.current_task()
                while task.cancelling():
                    task.uncancel()

    async def _in_worker(self, import_id: int, function, *args):
        """Run ``function`` in a worker thread; the job isn't cancelled
        meanwhile (the thread checks the stop flag itself)."""
        self._in_thread.add(import_id)
        try:
            return await asyncio.to_thread(function, *args)
        finally:
            self._in_thread.discard(import_id)

    def _interrupt(self, import_id: int) -> None:
        """Cancel the job where it waits. Not before its first step (it
        couldn't catch it; it checks the flag soon enough) and not while a
        worker thread runs (which checks the flag itself)."""
        task = self._tasks.get(import_id)
        if task is not None and import_id in self._started \
                and import_id not in self._in_thread:
            task.cancel()

    @staticmethod
    def _current_stage(record: ImportRecord) -> str | None:
        for name, status in record.stages:
            if status in UNFINISHED_STAGES:
                return name
        return None

    async def _run_stages(self, import_id: int) -> None:
        for stage, run in (("raw", self._raw_stage), ("archive", self._archive_stage)):
            record = self.repo.get(import_id)
            if getattr(record, f"{stage}_status") not in (STAGE_WAITING, STAGE_RUNNING):
                continue
            outcome = await run(import_id)
            if outcome == STAGE_STOP:
                self._after_stop(import_id, stage)
                return
            if outcome == STAGE_PAUSE:
                return
        self._complete(import_id)

    def _after_stop(self, import_id: int, stage: str) -> None:
        if import_id in self._cancel:
            self._abandon(import_id, "Cancelled by you.", STAGE_CANCELLED)
        elif import_id in self._pause:
            self._pause_stage(import_id, stage, "Paused by you.")
        # Otherwise the bot is shutting down: the import resumes at the next start.

    def _pause_stage(self, import_id: int, stage: str, error: str) -> None:
        """Pause the import. Its file is kept until source_expires_at, set at
        the first pause: pausing again doesn't extend it."""
        ts = now_ts()
        keep_days = self.services.settings["history.source_keep_days"]
        error_field = {"raw": "error", "archive": "archive_error"}[stage]
        expires = self.repo.get(import_id).source_expires_at or ts + keep_days * 86400
        self.repo.update(import_id, **{f"{stage}_status": STAGE_PAUSED, error_field: error[:500]},
                         status=PAUSED, paused_at=ts, source_expires_at=expires)
        for period in self.services.history.periods_for_import(import_id):
            if period.status == PERIOD_RUNNING:
                self.services.history.update_period(period.id, status=PERIOD_WAITING)
        self._pause.discard(import_id)
        logger.warning("Import %s paused (%s): %s", import_id, stage, error)

    def _complete(self, import_id: int) -> None:
        record = self.repo.get(import_id)
        statuses = [status for _, status in record.stages]
        if statuses and all(status == STAGE_DONE for status in statuses):
            status = DONE
        elif any(status == STAGE_DONE for status in statuses):
            status = PARTIAL
        else:
            status = FAILED
        self.repo.update(import_id, status=status, finished_at=now_ts(), paused_at=None,
                         source_expires_at=None)
        self._delete_file(import_id)
        self._pause.discard(import_id)
        self._cancel.discard(import_id)
        logger.info("Import %s %s", import_id, status, extra={"chat_id": record.chat_id})

    # ------------------------------------------------------------ raw stage

    async def _raw_stage(self, import_id: int) -> str:
        self.repo.update(import_id, raw_status=STAGE_RUNNING)
        return await self._in_worker(import_id, self.run, import_id)

    def run(self, import_id: int) -> str:
        """Store the chosen messages (runs in a worker thread)."""
        record = self.repo.get(import_id)
        chat_id = record.chat_id
        options = record.options
        raw = options["raw"]
        selected = raw.get("selected") or [raw["start"], raw["end"]]
        boundary = options.get("raw_boundary")
        messages = self.services.messages
        bot = self.services.status.bot
        bot_id = bot.id if bot else None
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
                             skipped_service=counts["service"],
                             skipped_range=counts["range"])

        try:
            with open(record.file_path, "rb") as handle:
                for message in ExportReader(handle).messages():
                    if self._should_stop(import_id):
                        raise ImportCancelled
                    if message.is_service:
                        counts["service"] += 1
                        continue
                    counts["processed"] += 1
                    if not selected[0] <= message.date < selected[1]:
                        counts["range"] += 1
                    elif boundary is not None and message.date >= boundary:
                        counts["overlap"] += 1
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
            # One step: the new messages become visible as the earlier
            # imports' messages of those dates go, and the stage is done. A
            # crash before it leaves the old ones (recover() drops the new).
            removed = 0
            with self.services.db.transaction():
                if raw["start"] is not None and counts["imported"]:
                    removed = messages.delete_imported_range(chat_id, raw["start"], raw["end"],
                                                             keep_import_id=import_id)
                    for old in self.repo.for_chat(chat_id):
                        if old.id != import_id and old.status in (DONE, PARTIAL) \
                                and old.raw_status == STAGE_DONE \
                                and not messages.count_import(old.id):
                            self.repo.update(old.id, status=REPLACED)
                self.repo.update(import_id, raw_status=STAGE_DONE, error=None,
                                 first_date=first_kept, last_date=last_kept)
            if removed:
                logger.info("Import %s replaced %s earlier imported messages", import_id,
                            removed, extra={"chat_id": chat_id})
            logger.info("Import %s stored %s messages (%s outside the chosen dates, %s excluded "
                        "by the live-recording boundary)", import_id, counts["imported"],
                        counts["range"], counts["overlap"], extra={"chat_id": chat_id})
            return STAGE_OK
        except ImportCancelled:
            messages.delete_import(import_id)
            self.repo.update(import_id, raw_status=STAGE_WAITING, processed=0, imported=0)
            logger.info("Import %s stopped; its messages were rolled back", import_id,
                        extra={"chat_id": chat_id})
            return STAGE_STOP
        except Exception as exc:
            messages.delete_import(import_id)
            logger.exception("Import %s failed", import_id, extra={"chat_id": chat_id})
            self._pause_stage(import_id, "raw", f"{type(exc).__name__}: {exc}")
            return STAGE_PAUSE

    # -------------------------------------------------------- archive stage

    async def _archive_stage(self, import_id: int) -> str:
        record = self.repo.get(import_id)
        options = record.options
        archive = options["archive"]
        tz = self._zone(record)
        chat = self.services.chats.get(record.chat_id)
        history = self.services.history
        self.repo.update(import_id, archive_status=STAGE_RUNNING, archive_error=None)
        source = ExportSource(record.file_path, start=archive["start"], end=archive["end"],
                              stopping=lambda: self._should_stop(import_id))
        try:
            await self._in_worker(import_id, source.load)
        except SourceStopped:
            return STAGE_STOP
        if source.unreadable:
            self._add_limitation(import_id, f"{source.unreadable} entries of the export "
                                            "couldn't be read and aren't summarized.")
        periods = history.periods_for_import(import_id)
        if not periods:
            periods = self._create_periods(record, source, tz)
        self._count_periods(import_id)
        writer = HistoryWriter(self.services)
        opts = WriteOptions(chunk_tokens=options["chunk_tokens"], max_chars=options["max_chars"],
                            timezone=tz, tz_name=options["tz"], actor=f"import {import_id}")
        for period in periods:
            period = history.get_period(period.id)
            if period.status not in (PERIOD_WAITING, PERIOD_RUNNING):
                continue
            if self._should_stop(import_id):
                return STAGE_STOP
            lines = source.between(period.period_start, period.period_end)
            try:
                outcome = await writer.process(
                    chat, period, lines, opts=opts,
                    limitations=self._period_limitations(record, source, period, tz),
                    should_stop=lambda: self._should_stop(import_id))
            except HistoryFailed as exc:
                history.update_period(period.id, status=PERIOD_FAILED, error=str(exc))
                span = describe_span(period.period_start, period.period_end, tz)
                self._pause_stage(import_id, "archive", f"{span}: {exc}")
                return STAGE_PAUSE
            if outcome == PAUSED_BY_QUEUE:
                self._pause_stage(import_id, "archive",
                                  "A summary request was cancelled on the queue page.")
                return STAGE_PAUSE
            if outcome != FINISHED:
                return STAGE_STOP
            self._count_periods(import_id)
            self._publish_ready(import_id)
        waiting = self._publish_ready(import_id)
        if waiting:
            self._count_periods(import_id)
            self._pause_stage(import_id, "archive",
                              f"{waiting} summar{'ies' if waiting != 1 else 'y'} waiting to "
                              "replace older ones went missing before they could. Resume to "
                              "make them again; the older ones stay in use until then.")
            return STAGE_PAUSE
        conflicts = [p for p in history.periods_for_import(import_id)
                     if p.status == PERIOD_CANCELLED and p.error]
        if conflicts:
            self._add_limitation(import_id, f"{len(conflicts)} period"
                                            f"{'s were' if len(conflicts) != 1 else ' was'} "
                                            "left out: an existing summary already covers them.")
        self.repo.update(import_id, archive_status=STAGE_DONE)
        return STAGE_OK

    def _create_periods(self, record: ImportRecord, source: ExportSource,
                        tz) -> list[HistoryPeriod]:
        """One work item per period with messages, deciding for each whether
        an existing digest is reused, replaced or (without permission)
        left alone."""
        options = record.options
        archive = options["archive"]
        tz_name = options["tz"]
        history = self.services.history
        periods = []
        for start, end in plan_periods(archive["start"], archive["end"], archive["grouping"], tz):
            lines = source.between(start, end)
            if not lines:
                continue
            content = fingerprint(archive["grouping"], tz_name, start, end,
                                  options["settings_hash"], day_hashes(lines, tz))
            existing = [d for d in history.overlapping(record.chat_id, start, end)
                        if d.import_id != record.id]
            same = [d for d in existing if (d.period_start, d.period_end) == (start, end)
                    and d.fingerprint == content]
            common = dict(chat_id=record.chat_id, source="export", import_id=record.id,
                          grouping=archive["grouping"], timezone=tz_name, period_start=start,
                          period_end=end, message_count=len(lines), fingerprint=content)
            if same and not archive["regenerate"]:
                periods.append(history.add_period(**common, status=REUSED, digest_id=same[0].id))
            elif existing and (not archive["replace"] or (
                    any(d.edited for d in existing) and not archive["replace_edited"])):
                periods.append(history.add_period(
                    **common, status=PERIOD_CANCELLED,
                    error="An existing summary covers this period and replacing it wasn't "
                          "chosen."))
            else:
                periods.append(history.add_period(**common,
                                                  replaces=[d.id for d in existing]))
        return periods

    def _count_periods(self, import_id: int) -> None:
        periods = self.services.history.periods_for_import(import_id)
        counted = [p for p in periods if p.status != PERIOD_CANCELLED or not p.error]
        self.repo.update(import_id, archive_total=len(counted),
                         archive_done=sum(1 for p in counted if p.status in (PERIOD_DONE, REUSED)))

    def _publish_ready(self, import_id: int) -> int:
        """Publish staged digests whose whole overlap is done: every new
        period replacing any of the same old digests has its summary. A
        group with a summary gone missing (deleted before it was published)
        is never published in part: its periods are made again. Returns how
        many groups are still waiting."""
        history = self.services.history
        record = self.repo.get(import_id)
        replace_edited = bool(record.options["archive"].get("replace_edited"))
        periods = [p for p in history.periods_for_import(import_id)
                   if p.replaces and p.status != PERIOD_CANCELLED]
        groups: list[tuple[set[int], list[HistoryPeriod]]] = []
        for period in periods:
            olds = set(period.replaces)
            merged = [g for g in groups if g[0] & olds]
            for group in merged:
                groups.remove(group)
                olds |= group[0]
            members = [period] + [p for g in merged for p in g[1]]
            groups.append((olds, members))
        waiting = 0
        for olds, members in groups:
            outcome, missing = history.publish_group(
                [p.id for p in members], sorted(olds), replace_edited=replace_edited,
                actor=f"import {import_id}")
            if outcome == PUBLISHED:
                logger.info("Import %s replaced %s history digests with %s", import_id,
                            len(olds), len(members))
                continue
            if outcome == ALREADY_PUBLISHED:
                continue
            if outcome == KEPT_EDITED:
                self._add_limitation(import_id, "Some periods were left out: an existing "
                                                "summary of them was edited while this ran.")
                continue
            if outcome == INCOMPLETE:
                for period_id in missing:
                    history.restart_period(period_id, "Its summary was deleted before it "
                                                      "replaced the older ones: made again.")
                    history.update_period(period_id, status=PERIOD_WAITING, digest_id=None)
            waiting += 1
        return waiting

    def _period_limitations(self, record: ImportRecord, source: ExportSource,
                            period: HistoryPeriod, tz) -> list[str]:
        notes = []
        boundary = record.options.get("boundary")
        if source.first_date is not None and source.first_date > period.period_start + 86400:
            notes.append(f"The export starts on {day_text(local_date(source.first_date, tz))}; "
                         "earlier messages of this period aren't in it.")
        if source.last_date is not None and source.last_date < period.period_end - 86400 \
                and period.period_end != boundary:
            notes.append(f"The export ends on {day_text(local_date(source.last_date, tz))}; "
                         "later messages of this period aren't in it.")
        if boundary is not None and period.period_end == boundary:
            notes.append(f"Live recording began on {day_text(local_date(boundary, tz))}; "
                         "later messages are in the live summaries.")
        return notes

    def _add_limitation(self, import_id: int, note: str) -> None:
        record = self.repo.get(import_id)
        notes = list(record.limitations or [])
        if note not in notes:
            notes.append(note)
            self.repo.update(import_id, limitations=notes)

    # ------------------------------------------------- pause / resume / cancel

    def pause(self, import_id: int) -> None:
        record = self.repo.get(import_id)
        if record is None or record.status != RUNNING or import_id not in self._tasks:
            raise ImportProblem("Only a running import can be paused.")
        self._pause.add(import_id)
        self._interrupt(import_id)

    def _source_problem(self, record: ImportRecord) -> str | None:
        """Why the upload can't be used any more, if it can't."""
        path = Path(record.file_path) if record.file_path else None
        if path is None or not path.exists():
            return "The uploaded file is gone. Upload the export again."
        if path.stat().st_size != record.file_size:
            return "The uploaded file changed. Upload the export again."
        return None

    def resume(self, import_id: int) -> None:
        record = self.repo.get(import_id)
        if record is None or record.status != PAUSED:
            raise ImportProblem("Only a paused import can be resumed.")
        if import_id in self._tasks:
            raise ImportProblem("This import is still stopping. Try again in a moment.")
        problem = self._source_problem(record)
        if problem:
            raise ImportProblem(problem)
        fields: dict = {"status": RUNNING, "paused_at": None}
        for stage, status in record.stages:
            if status == STAGE_PAUSED:
                fields[f"{stage}_status"] = STAGE_WAITING
        self.repo.update(import_id, **fields)
        for period in self.services.history.periods_for_import(import_id):
            if period.status == PERIOD_FAILED:
                self.services.history.update_period(period.id, status=PERIOD_WAITING,
                                                    attempts=0, error=None)
        self._spawn(import_id)

    def cancel_unfinished(self, import_id: int) -> str:
        """Stop the import's unfinished stages for good. Finished summaries
        stay; summaries waiting to replace older ones are dropped."""
        record = self.repo.get(import_id)
        if record is None or record.status not in (RUNNING, PAUSED):
            raise ImportProblem("Only a running or paused import can be cancelled.")
        if import_id in self._tasks:
            self._cancel.add(import_id)
            self._interrupt(import_id)
            return "Stopping."
        self._abandon(import_id, "Cancelled by you.", STAGE_CANCELLED)
        return "Cancelled the rest of the import."

    def _abandon(self, import_id: int, reason: str, stage_status: str) -> None:
        record = self.repo.get(import_id)
        history = self.services.history
        fields: dict = {}
        for stage, status in record.stages:
            if status in UNFINISHED_STAGES:
                fields[f"{stage}_status"] = stage_status
        if fields:
            self.repo.update(import_id, **fields)
        for period in history.periods_for_import(import_id):
            digest = history.get(period.digest_id) if period.digest_id else None
            if digest is not None and digest.status == STAGED:
                history.update_period(period.id, status=PERIOD_CANCELLED, digest_id=None,
                                      error="Not used: the rest of its replacement didn't finish.")
        history.delete_staged(import_id)
        history.cancel_unfinished(import_id, reason)
        if record.raw_status in UNFINISHED_STAGES:
            self.services.messages.delete_import(import_id)
        self._add_limitation(import_id, reason)
        self._complete(import_id)

    # ------------------------------------------------------------ lifecycle

    def discard(self, import_id: int) -> None:
        record = self.repo.get(import_id)
        if record is None or record.status != PREVIEW:
            raise ImportProblem("Only an import that hasn't started can be discarded.")
        self._delete_file(import_id)
        self.repo.update(import_id, status=DISCARDED, finished_at=now_ts())

    def _delete_file(self, import_id: int, *, path: str | None = None) -> None:
        if path is None:
            record = self.repo.get(import_id)
            path = record.file_path if record else None
        if path:
            Path(path).unlink(missing_ok=True)
            dates_path(path).unlink(missing_ok=True)
            self._dates.pop(path, None)
            self.repo.update(import_id, file_path=None)

    def recover(self) -> None:
        """At startup: an import that was running when the process stopped
        continues from its checkpoints (resume_interrupted(), once the bot
        is up); its raw stage starts over. Without its file, unfinished
        stages expire."""
        for record in self.repo.with_status(RUNNING):
            if record.raw_status in (STAGE_RUNNING, None):
                self.services.messages.delete_import(record.id)
                if record.raw_status == STAGE_RUNNING:
                    self.repo.update(record.id, raw_status=STAGE_WAITING, processed=0,
                                     imported=0)
            if record.options is None:  # started before stages existed
                self.repo.update(record.id, status=FAILED, finished_at=now_ts(),
                                 error="Interrupted by a restart. Upload the file again.")
                self._delete_file(record.id)
                continue
            for period in self.services.history.periods_for_import(record.id):
                if period.status == PERIOD_RUNNING:
                    self.services.history.update_period(period.id, status=PERIOD_WAITING)
            problem = self._source_problem(record)
            if problem:
                self._abandon(record.id, f"After a restart: {problem}", STAGE_EXPIRED)
            else:
                logger.info("Import %s was interrupted by a restart; it will continue",
                            record.id)
        # A finished import whose file outlived it (stopped between marking it
        # finished and deleting the file).
        for record in self.repo.with_status(DONE, PARTIAL, FAILED, REPLACED, DISCARDED):
            if record.file_path:
                self._delete_file(record.id)

    def resume_interrupted(self) -> int:
        """Start the jobs recover() left running."""
        count = 0
        for record in self.repo.with_status(RUNNING):
            if record.id not in self._tasks:
                self._spawn(record.id)
                count += 1
        return count

    def expire_paused(self, now: float | None = None) -> int:
        """Paused imports whose file has been kept long enough: the file is
        deleted and their unfinished work expires."""
        now = now or time.time()
        expired = 0
        for record in self.repo.with_status(PAUSED):
            if record.source_expires_at is not None and record.source_expires_at <= now:
                self._abandon(record.id, "Paused for too long: the uploaded file was deleted.",
                              STAGE_EXPIRED)
                expired += 1
        return expired

    def cleanup_stale_previews(self, max_age: int = STALE_PREVIEW_SECONDS) -> int:
        """Discard previews nobody started, so uploaded files don't linger."""
        cutoff = now_ts() - max_age
        stale = [r for r in self.repo.with_status(PREVIEW) if r.created_at < cutoff]
        for record in stale:
            self.discard(record.id)
        return len(stale)

    def running(self) -> bool:
        return bool(self._tasks)

    async def shutdown(self) -> None:
        """Stop running imports between steps; they continue at the next
        start. Jobs still waiting for the model after a short grace are
        cancelled (their checkpoints are kept)."""
        self._stopping.set()
        tasks = list(self._tasks.values())
        if not tasks:
            return
        _, waiting = await asyncio.wait(tasks, timeout=SHUTDOWN_GRACE_SECONDS)
        for task in waiting:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
