"""Keeps each chat's digest and group memory up to date in the background.

A chat's digest is updated once enough new messages arrived, after a quiet
gap, or on request (/summary). One update reads the messages the digest
hasn't seen yet (bounded by tokens), rewrites the digest and applies the
note changes the model proposes. Model requests run at background priority,
so replies to people go first. Each update is traced as an agent run.
"""

import asyncio
from collections import defaultdict
from datetime import datetime
import logging
import time

from naruto.agent.context import ContextBuilder
from naruto.agent.text import estimate_text_tokens
from naruto.db.chats import ENABLED, Chat
from naruto.db.memory import BOT
from naruto.db.messages import StoredMessage
from naruto.llm import LLMError
from naruto.memory.notes import (
    MemoryOutputError,
    apply_note_actions,
    note_lines,
    parse_json_object,
)
from naruto.services import BotIdentity, Services

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 60
RETRY_AFTER_SECONDS = 15 * 60
QUIET_MIN_MESSAGES = 10
MAX_UNREAD_BATCH = 2000
AUTO_NOTES_OFF = "\n\nAutomatic notes are turned off: always answer with an empty notes list."


def fill(template: str, **values) -> str:
    """Fill {name} placeholders without touching the JSON braces around them."""
    for name, value in values.items():
        template = template.replace("{" + name + "}", str(value))
    return template


class MemoryKeeper:
    def __init__(self, services: Services):
        self.services = services
        self._requested: set[int] = set()
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    def request_update(self, chat_id: int) -> None:
        """Update this chat's digest at the next check, whatever the counts."""
        self._requested.add(chat_id)

    def _identity(self) -> BotIdentity:
        return self.services.status.bot or BotIdentity(id=0, username="", name="Naruto")

    # ------------------------------------------------------------- schedule

    def due_chats(self, now: float | None = None) -> list[Chat]:
        now = now or time.time()
        settings = self.services.settings
        every = settings["memory.digest_every_messages"]
        quiet = settings["memory.digest_quiet_minutes"] * 60
        due = []
        for chat in self.services.chats.list_by_status(ENABLED):
            digest = self.services.digests.get(chat.chat_id)
            requested = chat.chat_id in self._requested
            if (digest and digest.failed_at and now - digest.failed_at < RETRY_AFTER_SECONDS
                    and not requested):
                continue
            unread, newest = self.services.digests.unread_count(chat.chat_id, digest)
            if not unread:
                self._requested.discard(chat.chat_id)
                continue
            if (requested or unread >= every
                    or (quiet and unread >= QUIET_MIN_MESSAGES and newest <= now - quiet)):
                due.append(chat)
        return due

    async def run_due(self) -> int:
        updated = 0
        for chat in self.due_chats():
            self._requested.discard(chat.chat_id)
            if await self.update(chat) is not None:
                updated += 1
        return updated

    async def run_forever(self) -> None:
        await asyncio.sleep(CHECK_INTERVAL_SECONDS / 2)
        while True:
            try:
                await self.run_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Memory upkeep failed")
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

    # --------------------------------------------------------------- update

    def _batch(self, chat: Chat, messages: list[StoredMessage] | None) -> tuple[list, bool]:
        """The unread messages that fit the token budget, and whether more
        are waiting."""
        if messages is None:
            digest = self.services.digests.get(chat.chat_id)
            messages = self.services.digests.unread(chat.chat_id, digest, limit=MAX_UNREAD_BATCH)
        budget = self.services.settings["memory.digest_input_tokens"]
        max_chars = self.services.settings["context.max_message_chars"]
        used, batch = 0, []
        for message in messages:
            cost = estimate_text_tokens(message.text[:max_chars]) + 12
            if batch and used + cost > budget:
                return batch, True
            batch.append(message)
            used += cost
        return batch, False

    def build_prompt(self, chat: Chat, batch: list[StoredMessage], *, more: bool) -> list[dict]:
        services = self.services
        settings = services.settings
        bot = self._identity()
        system = fill(settings["memory.instructions"], bot_name=bot.name,
                      digest_max_chars=settings["memory.digest_max_chars"])
        if not settings["memory.auto_notes"]:
            system += AUTO_NOTES_OFF
        builder = ContextBuilder(services, self_label="the assistant")
        tz = services.timezone()
        digest = services.digests.get(chat.chat_id)
        members = [m for m in services.members.list(chat.chat_id) if m.user_id != bot.id][:50]
        notes = services.notes.for_chat(chat.chat_id, limit=settings["memory.max_notes_per_chat"])
        parts = [
            "## Chat",
            f"Group: {chat.display_title}",
            f"Now: {datetime.now(tz).strftime('%a %d %b %Y, %H:%M')}",
            "",
            "## Members",
            *([f"- {m.display_name}" + (f" (also called {', '.join(m.aliases)})" if m.aliases
                                        else "") for m in members] or ["(none known)"]),
            "",
            "## Current digest",
            (digest.text if digest and digest.text else "(none yet)"),
            "",
            "## Notes",
            *(note_lines(services, notes) or ["(none yet)"]),
            "",
            f"## New messages ({len(batch)}, oldest first)",
            builder.describe_messages(batch, bot),
        ]
        if more:
            parts += ["", "(More messages follow in the next update.)"]
        return [{"role": "system", "content": system},
                {"role": "user", "content": "\n".join(parts)}]

    async def update(self, chat: Chat, *, messages: list[StoredMessage] | None = None,
                     actor: str | None = None) -> str | None:
        """Read the next batch of unread messages (or ``messages``) into the
        digest and notes. Returns the new digest text, or None on failure.
        Updates of one chat never overlap."""
        async with self._locks[chat.chat_id]:
            return await self._update(chat, messages, actor)

    async def _update(self, chat: Chat, messages: list[StoredMessage] | None,
                      actor: str | None) -> str | None:
        services = self.services
        settings = services.settings
        batch, more = self._batch(chat, messages)
        if not batch:
            return None
        prompt = self.build_prompt(chat, batch, more=more)
        run_id = services.runs.start(chat_id=chat.chat_id, skill="digest")
        services.runs.update(run_id, prompt=prompt, window_size=len(batch),
                             prompt_tokens=sum(estimate_text_tokens(m["content"]) for m in prompt))
        try:
            result = await services.llm.chat(prompt, reasoning=settings["memory.reasoning"],
                                              max_tokens=settings["memory.max_output_tokens"],
                                              background=True)
        except LLMError as exc:
            logger.warning("Digest update failed: %s", exc, extra={"chat_id": chat.chat_id})
            services.digests.set_error(chat.chat_id, str(exc))
            services.runs.update(run_id, status="error", error=str(exc))
            return None
        outcome = dict(model=result.model, reasoning=result.reasoning, response=result.text,
                       usage=result.usage, latency_ms=result.latency_ms,
                       finish_reason=result.finish_reason, model_requests=1)
        try:
            data = parse_json_object(result.text)
            text = str(data.get("digest") or "").strip()
            if not text:
                raise MemoryOutputError("The answer had no digest.")
        except MemoryOutputError as exc:
            logger.warning("Digest update gave no usable answer: %s", exc,
                           extra={"chat_id": chat.chat_id})
            services.digests.set_error(chat.chat_id, str(exc))
            services.runs.update(run_id, status="error", error=str(exc), **outcome)
            return None
        limit = settings["memory.digest_max_chars"]
        if len(text) > limit * 2:
            text = text[: limit * 2].rsplit("\n", 1)[0]
        actor = actor or f"bot (run {run_id})"
        services.digests.save(chat.chat_id, text, actor=actor, last=batch[-1])
        counts = apply_note_actions(
            services, chat.chat_id, data.get("notes") or [], created_by=BOT, actor=actor,
            bot_id=self._identity().id, allow_add=settings["memory.auto_notes"],
            known_row_ids={m.id for m in batch})
        services.runs.update(run_id, status="ok", **outcome)
        logger.info("Digest updated from %s messages (%s notes added, %s updated)", len(batch),
                    counts["added"], counts["updated"], extra={"chat_id": chat.chat_id})
        return text
