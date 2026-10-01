"""Background jobs: retention cleanup, leaving unapproved groups, the model
health check, due reminders, admin-rights checks and memory upkeep (digests
and notes)."""

import asyncio
import logging
import time
from typing import Awaitable, Callable

from naruto.db.chats import PENDING
from naruto.db.messages import IMPORT, LIVE
from naruto.health import check_model
from naruto.services import Services

logger = logging.getLogger(__name__)

DAY = 86400
MAINTENANCE_INTERVAL_SECONDS = 3600
HEALTH_INTERVAL_SECONDS = 60
REMINDER_INTERVAL_SECONDS = 30
RIGHTS_INTERVAL_SECONDS = 6 * 3600
UNREAD_GRACE_DAYS = 7

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


def cleanup_model_requests(services: Services) -> str | None:
    """The queue page's history follows the agent-run retention."""
    days = services.settings["retention.agent_runs_days"]
    if days <= 0:
        return None
    deleted = services.requests.delete_older_than(time.time() - days * DAY)
    return f"{deleted} model request records" if deleted else None


def _expire_messages(services: Services, source: str, days: int) -> int:
    """Delete ``source`` messages older than ``days``. Messages the digest
    hasn't read into memory yet get UNREAD_GRACE_DAYS more, so nothing is
    lost while the model is busy or down."""
    cutoff = int(time.time() - days * DAY)
    grace_cutoff = cutoff - UNREAD_GRACE_DAYS * DAY
    deleted = 0
    for (chat_id,) in services.db.query(
            "SELECT DISTINCT chat_id FROM messages WHERE source = ? AND date < ?",
            (source, cutoff)):
        digest = services.digests.get(chat_id)
        read_until = digest.last_message_date if digest else None
        if read_until is None:
            threshold = grace_cutoff
        else:
            threshold = min(cutoff, max(read_until + 1, grace_cutoff))
        deleted += services.messages.delete_for_chat(chat_id, before=threshold, source=source)
    return deleted


def cleanup_live_messages(services: Services) -> str | None:
    days = services.settings["retention.live_messages_days"]
    if days <= 0:
        return None
    deleted = _expire_messages(services, LIVE, days)
    return f"{deleted} live messages" if deleted else None


def cleanup_imported_messages(services: Services) -> str | None:
    days = services.settings["retention.imported_messages_days"]
    if days <= 0:
        return None
    deleted = _expire_messages(services, IMPORT, days)
    return f"{deleted} imported messages" if deleted else None


def cleanup_reminders(services: Services) -> str | None:
    """Sent and cancelled reminders follow the live-message retention."""
    days = services.settings["retention.live_messages_days"]
    if days <= 0:
        return None
    deleted = services.reminders.delete_finished_before(int(time.time() - days * DAY))
    return f"{deleted} old reminders" if deleted else None


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
        # Only groups the bot is actually in (an import can create a pending
        # group before the bot joins).
        if chat.created_at < cutoff and chat.membership in ("member", "administrator", "restricted"):
            await services.access.leave(chat.chat_id, actor=f"auto-leave after {hours} h pending")
            left += 1
    return left


async def refresh_rights(services: Services, *, older_than: float) -> None:
    checked = await services.access.refresh_rights(older_than=older_than)
    if checked:
        logger.info("Checked the admin rights in %s chats", checked)


async def run_maintenance(services: Services) -> None:
    done = []
    for step in [cleanup_logs, cleanup_agent_runs, cleanup_model_requests, cleanup_live_messages,
                 cleanup_imported_messages, cleanup_reminders, cleanup_import_previews,
                 *cleanup_steps]:
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


def start_background_jobs(services: Services, *, reminders=None) -> list[asyncio.Task]:
    """``reminders`` sends due reminders (the Telegram bot's ReminderSender)."""
    tasks = [
        asyncio.create_task(every(MAINTENANCE_INTERVAL_SECONDS,
                                  lambda: run_maintenance(services), first_delay=30)),
        asyncio.create_task(every(HEALTH_INTERVAL_SECONDS, lambda: check_model(services))),
    ]
    if reminders is not None:
        tasks.append(asyncio.create_task(every(REMINDER_INTERVAL_SECONDS, reminders.send_due,
                                               first_delay=5)))
    if services.access is not None:
        # Soon after start (rights that were never checked, or changed while
        # the bot was offline), then a few times a day.
        tasks.append(asyncio.create_task(every(
            RIGHTS_INTERVAL_SECONDS,
            lambda: refresh_rights(services, older_than=RIGHTS_INTERVAL_SECONDS - 60),
            first_delay=10)))
    if services.keeper is not None:
        tasks.append(asyncio.create_task(services.keeper.run_forever()))
    return tasks
