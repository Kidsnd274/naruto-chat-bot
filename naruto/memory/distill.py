"""Distilling group memory from an imported history (plan §6, step 5).

The whole export is read in chunks, including messages older than the
retention period (which the import itself doesn't keep), and the model
proposes notes for each chunk. Afterwards, if the chat has no digest yet,
the first one is built from the import's most recent days. Model requests
run at background priority.
"""

import asyncio
from datetime import datetime
import logging
import threading

from naruto.agent.text import estimate_text_tokens
from naruto.db.imports import ImportRecord, ImportRepository
from naruto.db.memory import IMPORT
from naruto.importer.export_parser import ExportReader
from naruto.llm import LLMError
from naruto.markers import message_body
from naruto.memory.keeper import fill
from naruto.memory.notes import (
    MemoryOutputError,
    apply_note_actions,
    note_lines,
    parse_json_object,
)
from naruto.services import Services

logger = logging.getLogger(__name__)

MAX_CONSECUTIVE_FAILURES = 3


class DistillStopped(Exception):
    pass


def export_chunks(services: Services, path: str, *, before: int | None,
                  chunk_tokens: int, stopping: threading.Event | None = None) -> list[list[str]]:
    """The export's messages (before ``before``) as dated lines, in chunks.
    Reads the whole file, so call it in a worker thread; it gives up with
    DistillStopped once ``stopping`` is set."""
    tz = services.timezone()
    max_chars = services.settings["context.max_message_chars"]
    lines: list[tuple[int | None, str, int, str]] = []
    with open(path, "rb") as handle:
        for count, message in enumerate(ExportReader(handle).messages()):
            if stopping is not None and count % 1000 == 0 and stopping.is_set():
                raise DistillStopped
            if message.is_service or (before is not None and message.date >= before):
                continue
            body = message_body(message.text, message.media_kind, message.media_meta)
            if not body:
                continue
            lines.append((message.sender_id, message.sender_name, message.date,
                          " ".join(body.split())[:max_chars]))
    names = services.people.display_names({sender for sender, *_ in lines})
    chunks: list[list[str]] = []
    current: list[str] = []
    used = 0
    for sender_id, sender_name, date, body in lines:
        when = datetime.fromtimestamp(date, tz).strftime("%a %d %b %Y, %H:%M")
        line = f"{names.get(sender_id, sender_name)} ({when}): {body}"
        cost = estimate_text_tokens(line) + 1
        if current and used + cost > chunk_tokens:
            chunks.append(current)
            current, used = [], 0
        current.append(line)
        used += cost
    if current:
        chunks.append(current)
    return chunks


class Distiller:
    def __init__(self, services: Services, repo: ImportRepository):
        self.services = services
        self.repo = repo

    def _prompt(self, chat_id: int, chunk: list[str], index: int, total: int) -> list[dict]:
        services = self.services
        bot = services.status.bot
        system = fill(services.settings["memory.distill_instructions"],
                      bot_name=bot.name if bot else "Naruto")
        people = [m.display_name + (f" (also called {', '.join(m.aliases)})" if m.aliases else "")
                  for m in services.members.list(chat_id) if not (bot and m.user_id == bot.id)]
        notes = services.notes.for_chat(chat_id)
        content = "\n".join([
            "## People",
            *([f"- {name}" for name in people[:80]] or ["(none known)"]),
            "",
            "## Notes already kept",
            *(note_lines(services, notes) or ["(none yet)"]),
            "",
            f"## Messages (part {index} of {total}, oldest first)",
            *chunk,
        ])
        return [{"role": "system", "content": system}, {"role": "user", "content": content}]

    async def distill(self, record: ImportRecord, chat_id: int, stopping: threading.Event) -> None:
        services = self.services
        settings = services.settings.for_chat(chat_id)
        first_live = services.messages.first_live_date(chat_id)
        # Parsing a large export takes a while: keep it off the event loop
        # so the bot and the web admin stay responsive.
        chunks = await asyncio.to_thread(
            export_chunks, services, record.file_path, before=first_live,
            chunk_tokens=settings["import.distill_chunk_tokens"], stopping=stopping)
        self.repo.update(record.id, distill_status="running", distill_total=len(chunks),
                         distill_done=0, notes_added=0, distill_error=None)
        actor = f"import {record.id}"
        bot = services.status.bot
        added = failures = failed_parts = 0
        last_error: Exception | None = None
        for index, chunk in enumerate(chunks, 1):
            if stopping.is_set():
                raise DistillStopped
            prompt = self._prompt(chat_id, chunk, index, len(chunks))
            try:
                result = await services.llm.chat(prompt, reasoning=settings["memory.reasoning"],
                                                  max_tokens=settings["memory.max_output_tokens"],
                                                  background=True)
                data = parse_json_object(result.text)
            except (LLMError, MemoryOutputError) as exc:
                failures += 1
                failed_parts += 1
                last_error = exc
                logger.warning("Distilling part %s of import %s failed: %s", index, record.id,
                               exc, extra={"chat_id": chat_id})
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    raise
            else:
                failures = 0
                counts = apply_note_actions(services, chat_id, data.get("notes") or [],
                                            created_by=IMPORT, actor=actor,
                                            bot_id=bot.id if bot else None,
                                            allow_changes=settings["memory.auto_notes"])
                added += counts["added"]
            self.repo.update(record.id, distill_done=index, notes_added=added)
        if chunks and failed_parts == len(chunks):
            raise last_error
        await self._first_digest(record, chat_id)
        self.repo.update(record.id, distill_status="done", distill_error=(
            f"{failed_parts} of {len(chunks)} parts couldn't be read." if failed_parts else None))
        logger.info("Import %s distilled into %s notes", record.id, added,
                    extra={"chat_id": chat_id})

    async def _first_digest(self, record: ImportRecord, chat_id: int) -> None:
        """If the chat has no digest yet, build one from the import's last days."""
        services = self.services
        digest = services.digests.get(chat_id)
        keeper = services.keeper
        chat = services.chats.get(chat_id)
        if keeper is None or chat is None or (digest and digest.text):
            return
        window = services.settings["import.digest_window_days"] * 86400
        last = services.db.scalar(
            "SELECT MAX(date) FROM messages WHERE import_id = ?", (record.id,))
        if last is None:
            return
        messages = services.messages.search(chat_id, None, since=last - window, until=last + 1,
                                            limit=2000)
        messages = [m for m in reversed(messages) if m.import_id == record.id]
        if messages:
            await keeper.update(chat, messages=messages, actor=f"import {record.id}")
