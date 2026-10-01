"""Distilling group memory from an imported history.

The chosen dates of the export are read in chunks, including messages older
than the retention period (which the import itself doesn't keep), and the
model proposes notes for each chunk. Progress is kept after every chunk, so
a restart or a pause continues with the next one. Model requests run at
background priority.
"""

import asyncio
from datetime import datetime
import logging
from typing import Callable

from naruto.db.imports import ImportRecord, ImportRepository
from naruto.db.memory import IMPORT
from naruto.llm import LLMError, RequestNotRun
from naruto.memory.history import ArchiveLine, ExportSource, SourceStopped, chunk_lines
from naruto.memory.keeper import fill
from naruto.memory.notes import (
    MemoryOutputError,
    apply_note_actions,
    note_lines,
    parse_json_object,
)
from naruto.model_queue import REFUSED_CANCELLED, RequestInfo
from naruto.services import Services

logger = logging.getLogger(__name__)

MAX_CONSECUTIVE_FAILURES = 3

# What Distiller.distill() returns.
DISTILLED = "done"
DISTILL_STOPPED = "stopped"  # shutting down or paused: continue later


class DistillStopped(Exception):
    pass


class DistillFailed(Exception):
    """Several chunks in a row failed; the message says why."""


def export_chunks(services: Services, path: str, *, since: int | None = None,
                  until: int | None = None, chunk_tokens: int, max_chars: int,
                  stopping: Callable[[], bool] | None = None) -> list[list[ArchiveLine]]:
    """The export's messages in [since, until), oldest first, in chunks.
    Reads the whole file, so call it in a worker thread; it gives up with
    DistillStopped once ``stopping()`` says so. The split is the same every
    time for the same file and settings, so a resumed job skips the chunks
    it already read."""
    source = ExportSource(path, start=since if since is not None else -(1 << 62),
                          end=until if until is not None else 1 << 62, stopping=stopping)
    try:
        source.load()
    except SourceStopped:
        raise DistillStopped from None
    readable = [line for line in source.lines if line.body]
    return chunk_lines(readable, chunk_tokens, max_chars)


class Distiller:
    def __init__(self, services: Services, repo: ImportRepository):
        self.services = services
        self.repo = repo

    def _prompt(self, chat_id: int, chunk: list[ArchiveLine], index: int, total: int,
                max_chars: int) -> list[dict]:
        services = self.services
        bot = services.status.bot
        tz = services.timezone()
        system = fill(services.settings["memory.distill_instructions"],
                      bot_name=bot.name if bot else "Naruto")
        people = [m.display_name + (f" (also called {', '.join(m.aliases)})" if m.aliases else "")
                  for m in services.members.list(chat_id) if not (bot and m.user_id == bot.id)]
        notes = services.notes.for_chat(chat_id)
        names = services.people.display_names({line.sender_id for line in chunk})
        lines = [f"{names.get(line.sender_id, line.sender_name)} "
                 f"({datetime.fromtimestamp(line.date, tz).strftime('%a %d %b %Y, %H:%M')}): "
                 f"{line.body[:max_chars]}" for line in chunk]
        content = "\n".join([
            "## People",
            *([f"- {name}" for name in people[:80]] or ["(none known)"]),
            "",
            "## Notes already kept",
            *(note_lines(services, notes) or ["(none yet)"]),
            "",
            f"## Messages (part {index} of {total}, oldest first)",
            *lines,
        ])
        return [{"role": "system", "content": system}, {"role": "user", "content": content}]

    async def distill(self, record: ImportRecord, chat_id: int, *, since: int | None,
                      until: int | None, chunk_tokens: int, max_chars: int,
                      should_stop: Callable[[], bool]) -> str:
        """Read the export's [since, until) into notes, from the chunk after
        ``record.distill_done``. Returns DISTILLED or DISTILL_STOPPED; raises
        DistillFailed when chunks keep failing."""
        services = self.services
        settings = services.settings.for_chat(chat_id)
        # Parsing a large export takes a while: keep it off the event loop
        # so the bot and the web admin stay responsive.
        try:
            chunks = await asyncio.to_thread(
                export_chunks, services, record.file_path, since=since, until=until,
                chunk_tokens=chunk_tokens, max_chars=max_chars, stopping=should_stop)
        except DistillStopped:
            return DISTILL_STOPPED
        start = min(record.distill_done, len(chunks))
        self.repo.update(record.id, distill_status="running", distill_total=len(chunks),
                         distill_error=None)
        actor = f"import {record.id}"
        bot = services.status.bot
        added = record.notes_added
        failures = failed_parts = 0
        last_error: Exception | None = None

        def give_up(message: str) -> DistillFailed:
            # Resume with the chunks of this run of failures, not after them.
            self.repo.update(record.id, distill_done=index - failures)
            return DistillFailed(message)

        index = start
        for index in range(start + 1, len(chunks) + 1):
            if should_stop():
                return DISTILL_STOPPED
            prompt = self._prompt(chat_id, chunks[index - 1], index, len(chunks), max_chars)
            try:
                result = await services.llm.chat(
                    prompt, reasoning=settings["memory.reasoning"],
                    max_tokens=settings["memory.max_output_tokens"], background=True,
                    info=RequestInfo(task="distill", chat_id=chat_id, import_id=record.id,
                                     chunk=index))
                data = parse_json_object(result.text)
            except RequestNotRun as exc:
                failures += 1
                if exc.reason == REFUSED_CANCELLED:
                    raise give_up("Its request was cancelled on the queue page.") from None
                raise give_up(str(exc)) from None
            except (LLMError, MemoryOutputError) as exc:
                failures += 1
                failed_parts += 1
                last_error = exc
                logger.warning("Distilling part %s of import %s failed: %s", index, record.id,
                               exc, extra={"chat_id": chat_id})
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    raise give_up(f"The model kept failing: {exc}") from None
            else:
                failures = 0
                counts = apply_note_actions(services, chat_id, data.get("notes") or [],
                                            created_by=IMPORT, actor=actor,
                                            bot_id=bot.id if bot else None,
                                            allow_changes=settings["memory.auto_notes"])
                added += counts["added"]
            self.repo.update(record.id, distill_done=index, notes_added=added)
        if chunks and failures and failures == len(chunks) - start:
            raise give_up(f"The model kept failing: {last_error}")
        self.repo.update(record.id, distill_error=(
            f"{failed_parts} of {len(chunks)} parts couldn't be read." if failed_parts else None))
        logger.info("Import %s distilled into %s notes", record.id, added,
                    extra={"chat_id": chat_id})
        return DISTILLED
