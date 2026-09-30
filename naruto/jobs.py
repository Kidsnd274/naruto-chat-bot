"""Background jobs: retention cleanup, leaving unapproved groups, and the
model health check."""

import asyncio
import logging
import time
from typing import Awaitable, Callable

from naruto.db.chats import PENDING
from naruto.health import check_model
from naruto.services import Services

logger = logging.getLogger(__name__)

DAY = 86400
MAINTENANCE_INTERVAL_SECONDS = 3600
HEALTH_INTERVAL_SECONDS = 60

# Extra cleanup steps for features added later. Each gets the services and
# returns a short description of what it removed, or None.
CleanupStep = Callable[[Services], str | None]
cleanup_steps: list[CleanupStep] = []


def cleanup_logs(services: Services) -> str | None:
    days = services.settings["retention.logs_days"]
    if days <= 0:
        return None
    deleted = services.logs.delete_older_than(time.time() - days * DAY)
    return f"{deleted} log records" if deleted else None


def cleanup_agent_runs(services: Services) -> str | None:
    days = services.settings["retention.agent_runs_days"]
    if days <= 0:
        return None
    deleted = services.runs.delete_older_than(time.time() - days * DAY)
    return f"{deleted} agent runs" if deleted else None


def cleanup_import_previews(services: Services) -> str | None:
    if services.imports is None:
        return None
    discarded = services.imports.cleanup_stale_previews()
    return f"{discarded} unstarted import uploads" if discarded else None


async def leave_stale_pending(services: Services) -> int:
    hours = services.settings["behaviour.pending_leave_hours"]
    if hours <= 0 or services.access is None:
        return 0
    cutoff = time.time() - hours * 3600
    left = 0
    for chat in services.chats.list_by_status(PENDING):
        if chat.created_at < cutoff and chat.membership not in ("left", "kicked"):
            await services.access.leave(chat.chat_id, actor=f"auto-leave after {hours} h pending")
            left += 1
    return left


async def run_maintenance(services: Services) -> None:
    done = []
    for step in [cleanup_logs, cleanup_agent_runs, cleanup_import_previews, *cleanup_steps]:
        try:
            result = step(services)
        except Exception:
            logger.exception("Cleanup step %s failed", getattr(step, "__name__", step))
            continue
        if result:
            done.append(result)
    if done:
        logger.info("Retention cleanup removed %s", ", ".join(done))
    try:
        left = await leave_stale_pending(services)
        if left:
            logger.info("Left %s groups that stayed pending too long", left)
    except Exception:
        logger.exception("Leaving stale pending groups failed")


async def every(interval: float, job: Callable[[], Awaitable[None]], *, first_delay: float = 0) -> None:
    await asyncio.sleep(first_delay)
    while True:
        try:
            await job()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Background job failed")
        await asyncio.sleep(interval)


def start_background_jobs(services: Services) -> list[asyncio.Task]:
    return [
        asyncio.create_task(every(MAINTENANCE_INTERVAL_SECONDS,
                                  lambda: run_maintenance(services), first_delay=30)),
        asyncio.create_task(every(HEALTH_INTERVAL_SECONDS, lambda: check_model(services))),
    ]
