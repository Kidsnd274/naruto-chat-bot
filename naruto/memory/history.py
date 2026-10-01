"""History digests: dated summaries of past periods
(plans/IMPORTED_HISTORY_DIGEST_TECH_PLAN.md, simplified by
plans/MEMORY_SIMPLIFICATION_AND_STABILITY_PLAN.md). The original messages
are kept until the owner deletes them; a summary is a convenience over them.

- plan_periods(): calendar months, weeks from Monday, or one range, in the
  configured time zone.
- Day hashes and fingerprints: what a period's messages were, so the import
  preview can tell which periods would come out the same as an existing
  digest (and reuse it) without reading the file again.
- ExportSource / StoredSource: a period's messages, oldest first, from an
  uploaded export or from stored live messages.
- HistoryWriter: summarizes one period in parts, carrying the summary so
  far, with a checkpoint after each part so a restart or failure resumes
  where it stopped. Requests run at background priority.
- LiveArchiver: once a month is over, summarizes its live messages.
"""

import asyncio
import bisect
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, tzinfo
import hashlib
import json
import logging
import time
from typing import Callable
from zoneinfo import ZoneInfo

from naruto.agent.text import estimate_text_tokens, strip_internal_json
from naruto.db.chats import ENABLED, Chat
from naruto.db.history import (
    ACTIVE,
    DONE,
    FAILED,
    LIVE,
    MONTH,
    RUNNING,
    STAGED,
    WAITING,
    HistoryPeriod,
)
from naruto.db.messages import StoredMessage
from naruto.importer.export_parser import ExportReader
from naruto.llm import LLMError, RequestNotRun
from naruto.markers import message_body
from naruto.memory.keeper import fill
from naruto.model_queue import REFUSED_CANCELLED, RequestInfo
from naruto.periods import (
    day_start,
    day_text,
    describe_span,
    local_date,
    period_label,
    plan_periods,
)
from naruto.services import BotIdentity, Services

logger = logging.getLogger(__name__)

LINE_OVERHEAD_TOKENS = 12  # name and time on each message line
MIN_MESSAGE_TOKENS = 500  # what a request must leave for messages
ATTEMPTS_PER_PART = 3
RETRY_DELAYS_SECONDS = (30, 120)  # between the attempts of one part
TRACE_CHARS = 20_000

# What HistoryWriter.process() returns.
FINISHED = "finished"
STOPPED = "stopped"  # shutting down or paused by the owner: resume later
PAUSED_BY_QUEUE = "cancelled"  # its request was cancelled on the queue page


class HistoryFailed(Exception):
    """A part of a period kept failing; the message says why."""


class SourceStopped(Exception):
    """Reading an export was interrupted (shutting down)."""


# --------------------------------------------------------------- hashing

def message_hash(message_id: int, date_ts: int, sender_id: int | None, body: str) -> int:
    digest = hashlib.sha1(f"{message_id}|{date_ts}|{sender_id}|{body}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def combine(hashes) -> str:
    """Order-independent: an export lists messages by ID, which isn't
    always date order."""
    return format(sum(hashes) % (1 << 64), "016x")


def settings_hash(settings) -> str:
    """The settings that change what a summary says."""
    raw = f"{settings['history.instructions']}|{settings['history.digest_max_chars']}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def fingerprint(grouping: str, tz_name: str, start: int, end: int, settings_key: str,
                day_hashes: list[tuple[int, str]]) -> str:
    payload = json.dumps([grouping, tz_name, start, end, settings_key, sorted(day_hashes)])
    return hashlib.sha1(payload.encode()).hexdigest()


# ----------------------------------------------------------------- sources

@dataclass
class ArchiveLine:
    date: int
    seq: int  # position in the source, for a stable order at equal times
    message_id: int
    sender_id: int | None
    sender_name: str
    body: str  # may be empty (then it isn't shown to the model)
    from_bot: bool = False

    @property
    def hash(self) -> int:
        return message_hash(self.message_id, self.date, self.sender_id, self.body)


def day_hashes(lines: list[ArchiveLine], tz: tzinfo) -> list[tuple[int, str]]:
    days: dict[int, list[int]] = defaultdict(list)
    for line in lines:
        days[day_start(local_date(line.date, tz), tz)].append(line.hash)
    return [(day, combine(hashes)) for day, hashes in days.items()]


def export_body(message) -> str:
    body = message_body(message.text, message.media_kind, message.media_meta)
    return " ".join((body or "").split())


class ExportSource:
    """An export's messages in [start, end), read once into memory and
    sorted by date (and file position for equal times). ``load()`` reads the
    whole file: call it in a worker thread."""

    def __init__(self, path: str, *, start: int, end: int,
                 stopping: Callable[[], bool] | None = None):
        self.path = path
        self.start = start
        self.end = end
        self.stopping = stopping
        self.lines: list[ArchiveLine] = []
        self._dates: list[int] = []
        self.unreadable = 0
        self.first_date: int | None = None  # of the whole export
        self.last_date: int | None = None

    def load(self) -> "ExportSource":
        lines = []
        with open(self.path, "rb") as handle:
            reader = ExportReader(handle)
            for seq, message in enumerate(reader.messages()):
                if self.stopping is not None and seq % 1000 == 0 and self.stopping():
                    raise SourceStopped
                if message.is_service:
                    continue
                self.first_date = message.date if self.first_date is None \
                    else min(self.first_date, message.date)
                self.last_date = message.date if self.last_date is None \
                    else max(self.last_date, message.date)
                if not self.start <= message.date < self.end:
                    continue
                lines.append(ArchiveLine(message.date, seq, message.id, message.sender_id,
                                         message.sender_name, export_body(message)))
            self.unreadable = reader.skipped
        lines.sort(key=lambda line: (line.date, line.seq))
        self.lines = lines
        self._dates = [line.date for line in lines]
        return self

    def between(self, start: int, end: int) -> list[ArchiveLine]:
        return self.lines[bisect.bisect_left(self._dates, start):
                          bisect.bisect_left(self._dates, end)]


class StoredSource:
    """Live messages stored for a chat."""

    def __init__(self, services: Services, chat_id: int):
        self.services = services
        self.chat_id = chat_id

    def between(self, start: int, end: int) -> list[ArchiveLine]:
        rows = self.services.db.query(
            "SELECT * FROM messages WHERE chat_id = ? AND source = 'live' AND date >= ? "
            "AND date < ? ORDER BY date, id", (self.chat_id, start, end))
        lines = []
        for row in rows:
            message = StoredMessage.from_row(row)
            body = " ".join((message_body(message.text, message.media_kind,
                                          message.media_meta) or "").split())
            lines.append(ArchiveLine(message.date, message.id, message.id, message.sender_id,
                                     message.sender_name, body, from_bot=message.from_bot))
        return lines


def chunk_lines(lines: list[ArchiveLine], chunk_tokens: int,
                max_chars: int) -> list[list[ArchiveLine]]:
    """Greedy parts of at most ``chunk_tokens``. Packing from any message
    gives the same parts as before, so a resumed period continues with the
    same split. The cost ignores names, which can change between runs."""
    chunks: list[list[ArchiveLine]] = []
    current: list[ArchiveLine] = []
    used = 0
    for line in lines:
        cost = estimate_text_tokens(line.body[:max_chars]) + LINE_OVERHEAD_TOKENS
        if current and used + cost > chunk_tokens:
            chunks.append(current)
            current, used = [], 0
        current.append(line)
        used += cost
    if current:
        chunks.append(current)
    return chunks


# ------------------------------------------------------------------ writer

def generation_hash(settings, *, chunk_tokens: int, max_chars: int, tz_name: str) -> str:
    """Everything that shapes a summary as it is written. A partial summary
    made with other values isn't continued: the period starts over."""
    raw = json.dumps([settings["history.instructions"], settings["history.digest_max_chars"],
                      settings["history.max_output_tokens"], settings["history.reasoning"],
                      chunk_tokens, max_chars, tz_name])
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _hash_lines(hasher, lines: list[ArchiveLine]) -> None:
    """In order: when, who and what (not row IDs, which change on re-import)."""
    for line in lines:
        hasher.update(f"{line.date}|{line.sender_id}|{line.body}\n".encode())


def source_hash(lines: list[ArchiveLine]) -> str:
    hasher = hashlib.sha1()
    _hash_lines(hasher, lines)
    return hasher.hexdigest()


@dataclass
class WriteOptions:
    chunk_tokens: int  # the whole request, instructions and summary so far included
    max_chars: int  # per message line
    timezone: tzinfo
    tz_name: str
    actor: str


class HistoryWriter:
    def __init__(self, services: Services):
        self.services = services

    def _bot(self) -> BotIdentity:
        return self.services.status.bot or BotIdentity(id=0, username="", name="Naruto")

    def build_prompt(self, chat: Chat, period: HistoryPeriod, chunk: list[ArchiveLine], *,
                     partial: str | None, part: int, parts: int, opts: WriteOptions) -> list[dict]:
        services = self.services
        settings = services.settings.for_chat(chat.chat_id)
        tz = opts.timezone
        bot = self._bot()
        label = period_label(period.period_start, period.period_end, period.grouping, tz)
        span = describe_span(period.period_start, period.period_end, tz)
        system = fill(settings["history.instructions"], bot_name=bot.name,
                      period=label if label == span else f"{label}, {span}",
                      max_chars=settings["history.digest_max_chars"])
        people = [m.display_name for m in services.members.list(chat.chat_id)
                  if m.user_id != bot.id][:80]
        names = services.people.display_names({line.sender_id for line in chunk
                                                if line.sender_id is not None})
        lines = []
        for line in chunk:
            when = datetime.fromtimestamp(line.date, tz).strftime("%a %d %b %Y, %H:%M")
            name = f"{bot.name} (the assistant)" if line.from_bot else \
                names.get(line.sender_id, line.sender_name)
            body = line.body if len(line.body) <= opts.max_chars \
                else line.body[:opts.max_chars] + "…"
            lines.append(f"{name} ({when}): {body}")
        parts_text = [
            "## Chat",
            f"Group: {chat.display_title}",
            f"Period: {label}" + ("" if label == span else f" ({span})"),
            "",
            "## People",
            *([f"- {name}" for name in people] or ["(none known)"]),
            "",
        ]
        if partial:
            done = "part 1" if part == 2 else f"parts 1–{part - 1}"
            parts_text += [f"## Summary so far ({done} of {parts})", partial, ""]
        parts_text += [f"## Messages (part {part} of {parts}, oldest first)", *lines]
        return [{"role": "system", "content": system},
                {"role": "user", "content": "\n".join(parts_text)}]

    def message_budget(self, chat: Chat, period: HistoryPeriod, opts: WriteOptions) -> int:
        """Tokens left for messages in one request: the instructions, the
        chat's details and the longest summary so far come out of
        ``chunk_tokens``."""
        limit = self.services.settings.for_chat(chat.chat_id)["history.digest_max_chars"]
        fixed = self.build_prompt(chat, period, [], partial="x" * (limit * 3 // 2), part=2,
                                  parts=2, opts=opts)
        overhead = sum(estimate_text_tokens(m["content"]) for m in fixed)
        budget = opts.chunk_tokens - overhead
        if budget < MIN_MESSAGE_TOKENS:
            raise HistoryFailed(
                f"The summary instructions and the summary so far take about {overhead} of the "
                f"{opts.chunk_tokens} tokens per request, leaving no room for messages. Raise "
                "History → Tokens per request, or shorten the instructions.")
        return budget

    async def process(self, chat: Chat, period: HistoryPeriod, lines: list[ArchiveLine], *,
                      opts: WriteOptions, limitations: list[str],
                      should_stop: Callable[[], bool]) -> str:
        """Summarize the rest of ``period`` from ``lines`` (all its messages,
        oldest first). Returns FINISHED, STOPPED or PAUSED_BY_QUEUE; raises
        HistoryFailed when a part keeps failing.

        A partial summary is continued only if it was made with the same
        settings from the same messages; otherwise the period starts over."""
        services = self.services
        history = services.history
        settings = services.settings.for_chat(chat.chat_id)
        readable = [line for line in lines if line.body]
        settings_key = generation_hash(settings, chunk_tokens=opts.chunk_tokens,
                                       max_chars=opts.max_chars, tz_name=opts.tz_name)
        hasher = hashlib.sha1()
        if period.consumed or period.partial:
            _hash_lines(hasher, readable[:period.consumed])
            reason = None
            if period.settings_hash != settings_key:
                reason = "Started over: the summary settings changed since it was paused."
            elif period.consumed > len(readable) or period.source_hash != hasher.hexdigest():
                reason = "Started over: messages already read were edited or deleted."
            if reason:
                logger.info("History period %s: %s", period.id, reason,
                            extra={"chat_id": chat.chat_id})
                history.restart_period(period.id, reason)
                period = history.get_period(period.id)
                hasher = hashlib.sha1()
        history.update_period(period.id, status=RUNNING)
        if not readable:
            history.update_period(period.id, status=DONE, error="No readable messages.")
            return FINISHED
        shortened = sum(1 for line in readable if len(line.body) > opts.max_chars)
        budget = self.message_budget(chat, period, opts)
        chunks = chunk_lines(readable[period.consumed:], budget, opts.max_chars)
        parts = period.chunks_done + len(chunks)
        partial, consumed, done = period.partial, period.consumed, period.chunks_done
        failures = period.attempts  # of the next part, kept across restarts
        for chunk in chunks:
            if should_stop():
                history.update_period(period.id, status=WAITING)
                return STOPPED
            part = done + 1
            prompt = self.build_prompt(chat, period, chunk, partial=partial, part=part,
                                       parts=parts, opts=opts)
            try:
                partial = await self._summarize(chat, period, prompt, part, failures)
            except RequestNotRun as exc:
                if exc.reason != REFUSED_CANCELLED:
                    raise HistoryFailed(str(exc)) from None
                history.update_period(period.id, status=WAITING,
                                      error="Its request was cancelled on the queue page.")
                return PAUSED_BY_QUEUE
            consumed += len(chunk)
            done = part
            failures = 0
            _hash_lines(hasher, chunk)
            history.checkpoint(period.id, partial=partial, consumed=consumed, chunks_done=done,
                               settings_hash=settings_key, source_hash=hasher.hexdigest())
        if should_stop():  # turned off during the last request: don't publish it
            history.update_period(period.id, status=WAITING)
            return STOPPED
        notes = list(limitations)
        if shortened:
            notes.append(f"{shortened} long message{'s were' if shortened != 1 else ' was'} "
                         "shortened before summarizing.")
        history.complete_period(
            history.get_period(period.id), status=STAGED if period.replaces else ACTIVE,
            first_message_at=lines[0].date, last_message_at=lines[-1].date,
            message_count=len(lines), text=partial or "", limitations=notes, actor=opts.actor)
        return FINISHED

    async def _summarize(self, chat: Chat, period: HistoryPeriod, prompt: list[dict],
                         part: int, failures: int) -> str:
        """One part, tried until it has failed ATTEMPTS_PER_PART times in all
        (``failures`` happened before, perhaps before a restart)."""
        services = self.services
        settings = services.settings.for_chat(chat.chat_id)
        limit = settings["history.digest_max_chars"]
        last_error = period.error or "unknown error"
        for attempt in range(failures + 1, ATTEMPTS_PER_PART + 1):
            if attempt > 1:
                await asyncio.sleep(RETRY_DELAYS_SECONDS[min(attempt - 2,
                                                             len(RETRY_DELAYS_SECONDS) - 1)])
            run_id = services.runs.start(chat_id=chat.chat_id, skill="history")
            services.runs.update(run_id, prompt=prompt, prompt_tokens=sum(
                estimate_text_tokens(m["content"]) for m in prompt))
            info = RequestInfo(task="history" if period.source != LIVE else "live_archive",
                               chat_id=chat.chat_id, run_id=run_id, import_id=period.import_id,
                               period_id=period.id, chunk=part)
            try:
                result = await services.llm.chat(
                    prompt, reasoning=settings["history.reasoning"],
                    max_tokens=settings["history.max_output_tokens"], background=True,
                    info=info)
            except asyncio.CancelledError:
                services.runs.update(run_id, status="error", error="Stopped.")
                raise
            except RequestNotRun as exc:
                services.runs.update(run_id, status="error", error=str(exc))
                raise
            except LLMError as exc:
                last_error = str(exc)
                services.runs.update(run_id, status="error", error=last_error)
                services.history.update_period(period.id, attempts=attempt, error=last_error)
                logger.warning("History summary part %s of period %s failed: %s", part,
                               period.id, exc, extra={"chat_id": chat.chat_id})
                continue
            outcome = dict(model=result.model, reasoning=result.reasoning,
                           response=(result.text or "")[:TRACE_CHARS], usage=result.usage,
                           latency_ms=result.latency_ms, finish_reason=result.finish_reason,
                           model_requests=1)
            text, _ = strip_internal_json(result.text or "")
            text = text.strip()
            if len(text) > limit * 3 // 2:
                text = text[: limit * 3 // 2].rsplit("\n", 1)[0].rstrip()
            if result.finish_reason == "length":
                last_error = ("The answer was cut short by the output limit (History → Max "
                              "output tokens).")
            elif not text:
                last_error = "The answer had no summary."
            else:
                services.runs.update(run_id, status="ok", **outcome)
                return text
            services.runs.update(run_id, status="error", error=last_error, **outcome)
            services.history.update_period(period.id, attempts=attempt, error=last_error)
        raise HistoryFailed(f"Part {part} kept failing: {last_error}")


def request_overhead(settings) -> int:
    """About how many tokens of a summary request aren't messages: the
    instructions and the longest summary so far (for estimates; the writer
    measures the real prompt)."""
    longest = settings["history.digest_max_chars"] * 3 // 2
    return estimate_text_tokens(settings["history.instructions"]) + \
        estimate_text_tokens("x" * longest) + 200


def retry_live_month(services: Services, period_id: int) -> bool:
    """The owner's Retry on a failed live month: a fresh allowance of
    attempts. The archiver's next round picks it up, and checks its messages
    before continuing a partial summary. Retrying twice is harmless."""
    period = services.history.get_period(period_id)
    if period is None or period.source != LIVE or period.status != FAILED:
        return False
    services.history.update_period(period_id, status=WAITING, attempts=0, error=None)
    return True


def zone(tz_name: str, services: Services) -> tzinfo:
    return services.timezone() if tz_name == "server" else ZoneInfo(tz_name)


# ----------------------------------------------------------- live archive

class LiveArchiver:
    """Once a month is over, summarize its live messages into a history
    digest (Settings → History → Summarize live chat monthly).

    A month whose part keeps failing (ATTEMPTS_PER_PART in all, counted
    across restarts) is marked failed and left until the owner retries it on
    the History page; later months go ahead. A month's dates and time zone
    are fixed once its work starts."""

    CHECK_INTERVAL_SECONDS = 600
    RETRY_AFTER_SECONDS = 15 * 60  # after a failure, before the chat's next month

    def __init__(self, services: Services):
        self.services = services
        self.writer = HistoryWriter(services)
        self._failed_at: dict[int, float] = {}
        self._stopping = False

    def stop(self) -> None:
        self._stopping = True

    def due_month(self, chat: Chat, now: float | None = None) -> tuple[int, int] | None:
        """Unfinished work first (with the dates it started with), else the
        oldest finished month with live messages that no live summary
        covers yet."""
        services = self.services
        periods = services.history.live_periods(chat.chat_id)
        for period in periods:
            if period.status in (WAITING, RUNNING):
                return period.period_start, period.period_end
        tz = services.timezone()
        now = now or time.time()
        this_month = day_start(local_date(int(now), tz).replace(day=1), tz)
        start_from = chat.recording_since
        oldest = services.db.scalar(
            "SELECT MIN(date) FROM messages WHERE chat_id = ? AND source = 'live' AND date < ?",
            (chat.chat_id, this_month))
        if oldest is None:
            return None
        if start_from is not None:
            oldest = max(oldest, start_from)
        for start, end in plan_periods(day_start(local_date(oldest, tz).replace(day=1), tz),
                                       this_month, MONTH, tz):
            if start_from is not None and end <= start_from:
                continue
            period_start = max(start, start_from) if start_from is not None else start
            if any(p.period_start < end and period_start < p.period_end for p in periods):
                continue  # done, failed until retried, or dated differently before
            has_messages = services.db.scalar(
                "SELECT 1 FROM messages WHERE chat_id = ? AND source = 'live' AND date >= ? "
                "AND date < ? LIMIT 1", (chat.chat_id, period_start, end))
            if has_messages:
                return period_start, end
        return None

    def retry(self, period_id: int) -> bool:
        period = self.services.history.get_period(period_id)
        if not retry_live_month(self.services, period_id):
            return False
        self._failed_at.pop(period.chat_id, None)
        return True

    def _enabled(self, chat_id: int) -> bool:
        chat = self.services.chats.get(chat_id)
        return (chat is not None and chat.enabled
                and self.services.settings.for_chat(chat_id)["history.live_archive"])

    async def run_due(self, now: float | None = None) -> int:
        """Archive at most one month per chat. Returns months finished."""
        services = self.services
        finished = 0
        now = now or time.time()
        for chat in services.chats.list_by_status(ENABLED):
            if self._stopping:
                break
            if not self._enabled(chat.chat_id):
                continue
            failed = self._failed_at.get(chat.chat_id)
            if failed and now - failed < self.RETRY_AFTER_SECONDS:
                continue
            lock = services.history_locks[chat.chat_id]
            if lock.locked():
                continue  # an import or a deletion is working on this chat: next round
            due = self.due_month(chat, now)
            if due is None:
                continue
            async with lock:
                if await self.archive(chat, *due, now=now):
                    finished += 1
        return finished

    async def archive(self, chat: Chat, start: int, end: int, *, now: float) -> bool:
        services = self.services
        history = services.history
        source = StoredSource(services, chat.chat_id)
        lines = source.between(start, end)
        period = next((p for p in history.live_periods(chat.chat_id)
                       if (p.period_start, p.period_end) == (start, end)), None)
        settings = services.settings.for_chat(chat.chat_id)
        if period is None:
            tz_name = settings["general.timezone"] or "server"
            period = history.add_period(
                chat_id=chat.chat_id, source=LIVE, import_id=None, grouping=MONTH,
                timezone=tz_name, period_start=start, period_end=end,
                message_count=len(lines),
                fingerprint=fingerprint(MONTH, tz_name, start, end, settings_hash(settings),
                                        day_hashes(lines, zone(tz_name, services))))
        tz = zone(period.timezone, services)
        limitations = live_limitations(chat, start, end, lines, tz,
                                       recorded=period.message_count)
        opts = WriteOptions(chunk_tokens=settings["history.chunk_tokens"],
                            max_chars=settings["context.max_message_chars"], timezone=tz,
                            tz_name=period.timezone, actor="live archive")
        try:
            outcome = await self.writer.process(
                chat, period, lines, opts=opts, limitations=limitations,
                should_stop=lambda: self._stopping or not self._enabled(chat.chat_id))
        except HistoryFailed as exc:
            self._failed_at[chat.chat_id] = now
            history.update_period(period.id, status=FAILED, error=str(exc))
            logger.warning("Live history summary of %s failed: %s",
                           describe_span(start, end, tz), exc, extra={"chat_id": chat.chat_id})
            return False
        if outcome != FINISHED:
            if outcome == PAUSED_BY_QUEUE:
                self._failed_at[chat.chat_id] = now
            return False
        self._failed_at.pop(chat.chat_id, None)
        logger.info("Summarized the live history of %s", describe_span(start, end, tz),
                    extra={"chat_id": chat.chat_id})
        return True

    async def run_forever(self) -> None:
        await asyncio.sleep(self.CHECK_INTERVAL_SECONDS / 4)
        while not self._stopping:
            try:
                await self.run_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Live history archiving failed")
            await asyncio.sleep(self.CHECK_INTERVAL_SECONDS)


def live_limitations(chat: Chat, start: int, end: int, lines: list[ArchiveLine],
                     tz: tzinfo, *, recorded: int = 0) -> list[str]:
    """``recorded``: how many messages the month had when its work began."""
    notes = []
    month_start = day_start(local_date(start, tz).replace(day=1), tz)
    if start > month_start:
        notes.append(f"Live recording began on {day_text(local_date(start, tz))}; "
                     "earlier messages of this month aren't included.")
    if len(lines) < recorded:
        gone = recorded - len(lines)
        notes.append(f"{gone} of this month's messages were deleted before it was summarized.")
    return notes
