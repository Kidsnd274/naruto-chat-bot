"""One queue for every model request.

Replies to people ("foreground") start before waiting background work
(digest upkeep, history summaries). Within a priority,
first come, first served. The limits are settings, read at every dispatch:

- model.parallel_requests: requests running at once, in total;
- model.background_requests: background requests running at once;
- model.foreground_reserved: slots background work never takes, so a new
  reply can start straight away (at most total - 1, so background work still
  runs on a one-slot server);
- model.background_paused: no new background request starts.

A running request is never interrupted: shrinking a limit only stops new
starts until the running count fits. Every request is logged in the
model_requests table for the web admin's queue page (prompts are not).
"""

import asyncio
from dataclasses import dataclass, field
import heapq
import itertools
import logging
import time
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from naruto.db.model_requests import ModelRequestRepository
    from naruto.settings.service import SettingsService

logger = logging.getLogger(__name__)

FOREGROUND = "foreground"
BACKGROUND = "background"
PRIORITIES = (FOREGROUND, BACKGROUND)
# Waiting requests per priority. Background producers submit one request at
# a time per chat or job, so their backlog stays small anyway.
MAX_WAITING = {FOREGROUND: 50, BACKGROUND: 20}

# Request states (model_requests.state).
QUEUED = "queued"
RUNNING = "running"
RETRYING = "retrying"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
EXPIRED = "expired"

# Why a request never reached the model (QueueRefused.reason).
REFUSED_CANCELLED = "cancelled"
REFUSED_EXPIRED = "expired"
REFUSED_BUSY = "busy"


class QueueRefused(Exception):
    """The request was not sent to the model: cancelled from the queue
    page, no longer wanted, or the queue is full."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


@dataclass
class RequestInfo:
    """What a request is for, shown on the queue page."""
    task: str  # reply | image | digest | history | live_archive | lab
    chat_id: int | None = None
    run_id: int | None = None
    import_id: int | None = None
    period_id: int | None = None
    chunk: int | None = None  # 1-based part of a history period
    lab_attempt_id: int | None = None  # a prompt-lab attempt (task lab)
    # Checked just before the request starts; False expires it (for example
    # the chat was disabled while the reply waited).
    still_wanted: Callable[[], bool] | None = field(default=None, repr=False)


@dataclass
class Ticket:
    id: int
    seq: int  # place in the queue; a retry keeps it
    priority: str
    info: RequestInfo
    queued_at: float
    future: asyncio.Future
    attempt: int = 1
    state: str = QUEUED
    started_at: float | None = None


@dataclass
class Limits:
    total: int
    background: int  # as configured
    reserved: int  # as configured
    effective_reserved: int
    effective_background: int  # what background work may use right now
    paused: bool


@dataclass
class Snapshot:
    limits: Limits
    running: list[Ticket]
    waiting: dict[str, list[Ticket]]  # per priority, in order

    def running_count(self, priority: str | None = None) -> int:
        return sum(1 for t in self.running if priority is None or t.priority == priority)

    def waiting_count(self, priority: str | None = None) -> int:
        return sum(len(tickets) for p, tickets in self.waiting.items()
                   if priority is None or p == priority)

    @property
    def oldest_wait(self) -> float | None:
        times = [t.queued_at for tickets in self.waiting.values() for t in tickets]
        return time.time() - min(times) if times else None

    @property
    def over_limit(self) -> int:
        """Requests still running beyond a limit that was just lowered."""
        return max(0, len(self.running) - self.limits.total,
                   self.running_count(BACKGROUND) - self.limits.effective_background)


def compute_limits(settings: "SettingsService") -> Limits:
    total = max(1, int(settings["model.parallel_requests"]))
    background = max(0, int(settings["model.background_requests"]))
    reserved = max(0, int(settings["model.foreground_reserved"]))
    paused = bool(settings["model.background_paused"])
    effective_reserved = min(reserved, total - 1)
    effective = 0 if paused else min(background, total - effective_reserved)
    return Limits(total=total, background=background, reserved=reserved,
                  effective_reserved=effective_reserved, effective_background=effective,
                  paused=paused)


class ModelQueue:
    def __init__(self, settings: "SettingsService",
                 log: "ModelRequestRepository | None" = None):
        self.settings = settings
        self.log = log
        self._seq = itertools.count(1)
        self._ids = itertools.count(1)  # when there is no log
        self._heaps: dict[str, list[tuple[int, int]]] = {p: [] for p in PRIORITIES}
        self._queued: dict[int, Ticket] = {}
        self._running: dict[int, Ticket] = {}
        settings.on_change(self._setting_changed)

    # --------------------------------------------------------------- state

    def limits(self) -> Limits:
        return compute_limits(self.settings)

    def snapshot(self) -> Snapshot:
        waiting = {p: sorted((t for t in self._queued.values() if t.priority == p),
                             key=lambda t: t.seq) for p in PRIORITIES}
        running = sorted(self._running.values(), key=lambda t: t.started_at or 0)
        return Snapshot(self.limits(), running, waiting)

    @property
    def in_flight(self) -> int:
        return len(self._running)

    @property
    def waiting(self) -> int:
        return len(self._queued)

    # ------------------------------------------------------------- acquire

    async def acquire(self, info: RequestInfo, priority: str, *,
                      retry_of: Ticket | None = None) -> Ticket:
        """Wait for a slot. Raises QueueRefused if the request is cancelled,
        expires or the queue is full. If the caller is cancelled while it
        waits (a run's deadline), the request leaves the queue."""
        if priority not in PRIORITIES:
            raise ValueError(f"unknown priority {priority!r}")
        waiting = sum(1 for t in self._queued.values() if t.priority == priority)
        if waiting >= MAX_WAITING[priority]:
            if retry_of is not None:
                self._close(retry_of, FAILED, "The queue was full when retrying.")
            raise QueueRefused(REFUSED_BUSY, "Too many requests are waiting for the model.")
        now = time.time()
        future = asyncio.get_running_loop().create_future()
        if retry_of is None:
            ticket = Ticket(id=0, seq=next(self._seq), priority=priority, info=info,
                            queued_at=now, future=future)
            ticket.id = self.log.queued(info, priority, now) if self.log else next(self._ids)
        else:
            # Keeps its place: a retry goes ahead of requests that came later.
            ticket = retry_of
            ticket.future, ticket.queued_at, ticket.started_at = future, now, None
            ticket.attempt += 1
            ticket.state = QUEUED
            if self.log:
                self.log.requeued(ticket.id, ticket.attempt, now)
        self._queued[ticket.id] = ticket
        heapq.heappush(self._heaps[priority], (ticket.seq, ticket.id))
        self._dispatch()
        try:
            await future
        except asyncio.CancelledError:
            if ticket.state == RUNNING:
                # Started just as the caller gave up: hand the slot back.
                self.release(ticket, EXPIRED, "The caller stopped waiting.")
            elif self._queued.pop(ticket.id, None) is not None:
                self._close(ticket, EXPIRED, "The caller stopped waiting (deadline).")
            raise
        return ticket

    def release(self, ticket: Ticket, state: str = DONE, error: str | None = None) -> None:
        """The request finished (``state`` done, failed, expired or
        cancelled), or ``state`` RETRYING: it frees its slot and will queue
        again with acquire(retry_of=ticket)."""
        if self._running.pop(ticket.id, None) is None:
            return
        if state == RETRYING:
            ticket.state = RETRYING
            if self.log:
                self.log.retrying(ticket.id, error)
        else:
            self._close(ticket, state, error)
        self._dispatch()

    def abandon(self, ticket: Ticket, error: str) -> None:
        """A request waiting to retry that won't be retried after all."""
        if ticket.state == RETRYING:
            self._close(ticket, EXPIRED, error)

    def cancel(self, request_id: int) -> bool:
        """Cancel a waiting request (the queue page). Its caller gets
        QueueRefused. Running requests can't be cancelled."""
        ticket = self._queued.pop(request_id, None)
        if ticket is None:
            return False
        self._close(ticket, CANCELLED, "Cancelled from the queue page.")
        if not ticket.future.done():
            ticket.future.set_exception(
                QueueRefused(REFUSED_CANCELLED, "Cancelled from the queue page."))
        logger.info("Model request %s (%s) cancelled from the queue page", request_id,
                    ticket.info.task, extra={"chat_id": ticket.info.chat_id})
        self._dispatch()
        return True

    # ------------------------------------------------------------ dispatch

    def _setting_changed(self, key: str, _value) -> None:
        if key.startswith("model."):
            self._dispatch()

    def _pop(self, priority: str) -> Ticket | None:
        heap = self._heaps[priority]
        while heap:
            _, ticket_id = heapq.heappop(heap)
            ticket = self._queued.pop(ticket_id, None)
            if ticket is not None:
                return ticket
        return None

    def _has_waiting(self, priority: str) -> bool:
        return any(t.priority == priority for t in self._queued.values())

    def _dispatch(self) -> None:
        """Start as many waiting requests as the limits allow: replies
        first, then background work within its own limit."""
        limits = self.limits()
        while len(self._running) < limits.total:
            ticket = self._pop(FOREGROUND)
            if ticket is None:
                background = sum(1 for t in self._running.values() if t.priority == BACKGROUND)
                if background >= limits.effective_background:
                    return
                ticket = self._pop(BACKGROUND)
                if ticket is None:
                    return
            if not self._wanted(ticket):
                self._close(ticket, EXPIRED, "No longer needed when its turn came.")
                if not ticket.future.done():
                    ticket.future.set_exception(
                        QueueRefused(REFUSED_EXPIRED, "No longer needed when its turn came."))
                continue
            if ticket.future.done():  # the caller is gone (cancelled meanwhile)
                self._close(ticket, EXPIRED, "The caller stopped waiting.")
                continue
            ticket.state = RUNNING
            ticket.started_at = time.time()
            self._running[ticket.id] = ticket
            if self.log:
                self.log.started(ticket.id, ticket.started_at)
            ticket.future.set_result(None)

    @staticmethod
    def _wanted(ticket: Ticket) -> bool:
        check = ticket.info.still_wanted
        if check is None:
            return True
        try:
            return bool(check())
        except Exception:
            logger.exception("still_wanted check failed; sending the request anyway")
            return True

    def _close(self, ticket: Ticket, state: str, error: str | None = None) -> None:
        ticket.state = state
        if self.log:
            self.log.finished(ticket.id, state, time.time(), error)
