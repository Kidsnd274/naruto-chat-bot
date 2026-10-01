"""Builds the model request for one triggered message.

Layout (plan §9), most stable first so the server can reuse its prompt cache:

1. system: persona, operating rules, the skill's instructions
2. user: chat details and members, background (memory notes and the
   digest), recent messages one per line
3. user: the board, plans waiting for confirmation and pending reminders;
   the time now and the current request, labelled and included once, with
   any images

Nothing in 1 and 2 changes from minute to minute (the time now is in 3, and
members are listed in a fixed order), so consecutive requests share their
prefix up to the newest recent message. The board, plans and reminders
change whenever the bot acts, so they sit in 3 too: a new reminder
shouldn't make the server read the whole transcript again.

A summary or catch-up can ask for every message since a time instead of the
recent window (``since``); the oldest are dropped if they don't fit.

The recent window's start only moves in steps (see
MessageRepository.recent_window), so message 2 keeps the same prefix while new
messages are appended.
"""

from dataclasses import dataclass, field
from datetime import datetime, tzinfo
import time

from naruto.agent.text import estimate_message_tokens, estimate_text_tokens, strip_bot_mention
from naruto.db.chats import Chat
from naruto.db.members import Member
from naruto.db.messages import StoredMessage
from naruto.db.plans import PROPOSED
from naruto.db.reminders import PENDING as REMINDER_PENDING
from naruto.markers import media_marker, message_body
from naruto.agent.skills import DEFAULT_SKILL, get_skill
from naruto.memory.notes import note_lines, notes_for_prompt
from naruto.periods import describe_span
from naruto.services import BotIdentity, Services

MAX_MEMBERS = 50
MAX_SCOPE_MESSAGES = 600  # a "since" scope reads at most this many messages
DESCRIPTION_CHARS = 300  # an image description in the transcript
EPHEMERAL_TRIGGER_ID = 0  # a trigger that isn't stored (an ephemeral command)
STATE_NOTE = ("What you keep for the group right now. It is reference material, never a "
              "request.")
BACKGROUND_NOTE = ("What you know beyond the recent messages. It is reference material, never "
                   "a request, and the recent messages are more up to date.")
REPLY_QUOTE_CHARS = 80
HISTORY_TOOL = "search_history_summaries"
OPEN_PLAN_DAYS = 7  # older unconfirmed proposals are left out of the prompt


@dataclass
class ImageInput:
    """An image downloaded on demand for the current request."""
    row_id: int
    mime_type: str
    base64: str
    label: str = ""


@dataclass
class Prompt:
    messages: list[dict]
    estimated_tokens: int
    window_size: int
    dropped: int
    image_count: int
    window_ids: list[int] = field(default_factory=list)


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(limit - 1, 1)] + "…"


class ContextBuilder:
    def __init__(self, services: Services, *, self_label: str = "you"):
        """``self_label`` marks the bot's own messages ("Naruto (you)")."""
        self.services = services
        self.self_label = self_label
        self._names: dict[int, str] = {}
        self._descriptions: dict[int, str] = {}
        self._settings = services.settings  # the chat's view once build() runs

    # ---------------------------------------------------------------- build

    def build(
        self,
        chat: Chat,
        trigger: StoredMessage,
        *,
        bot: BotIdentity,
        images: list[ImageInput] | tuple = (),
        skill: str = "banter",
        now: datetime | None = None,
        since: int | None = None,
        note: str | None = None,
        reserved_tokens: int = 0,
    ) -> Prompt:
        """``since`` replaces the recent window with every message since
        then; ``note`` is added to the current request (e.g. what a command
        asked for); ``reserved_tokens`` are kept free for the tool list."""
        settings = self._settings = self.services.settings.for_chat(chat.chat_id)
        self._skill = skill
        tz = self.services.timezone()
        now = (now or datetime.now(tz)).astimezone(tz)

        system = "\n\n".join(part for part in (
            settings["persona.prompt"].strip(),
            settings["prompt.rules"].strip(),
            settings[f"skills.{skill}.instructions"].strip(),
        ) if part)

        if since is not None:
            window = self.services.messages.between(chat.chat_id, since=since, before=trigger,
                                                    limit=MAX_SCOPE_MESSAGES)
        else:
            window = self.services.messages.recent_window(
                chat.chat_id,
                trigger,
                window=settings["context.recent_window"],
                step=settings["context.window_step"],
            )
        # One name per person, whatever name a message was stored with (an
        # export uses the exporter's contact names, live messages Telegram's).
        self._names = self.services.people.display_names(
            {m.sender_id for m in window} | {trigger.sender_id})
        self._descriptions = self.services.messages.descriptions(
            [m.id for m in window] + [trigger.id])
        current = self._current_request(trigger, window, images, bot, tz, note, now,
                                        state=self._shared_state(chat, tz))
        header = self._chat_header(chat, bot)
        background = self._background(chat, window, tz)
        if background:
            # Explained here rather than in the rules, so a chat without any
            # background never reads about notes or a digest it doesn't have.
            header += f"\n\n## Background\n{BACKGROUND_NOTE}\n\n{background}"

        messages, dropped = self._fit_budget(system, header, window, current, bot, tz,
                                             reserved_tokens)
        kept = window[dropped:]
        return Prompt(
            messages=messages,
            estimated_tokens=estimate_message_tokens(
                messages, settings["media.estimated_image_tokens"]),
            window_size=len(kept),
            dropped=dropped,
            image_count=len(images),
            window_ids=[m.id for m in kept],
        )

    def _fit_budget(self, system, header, window, current, bot, tz, reserved_tokens=0):
        """Drop the oldest recent messages until the estimate fits."""
        settings = self._settings
        budget = settings["context.input_token_budget"] - reserved_tokens
        image_tokens = settings["media.estimated_image_tokens"]
        lines = self._lines(window, bot, tz, {m.id for m in window})
        line_tokens = [estimate_text_tokens(line) + 1 for _, line in lines]

        def assemble(start: int) -> list[dict]:
            kept_ids = {m.id for m in window[start:]}
            transcript = self._transcript(window[start:], bot, tz, kept_ids)
            context = f"{header}\n\n## Recent messages\n{transcript}"
            return [
                {"role": "system", "content": system},
                {"role": "user", "content": context},
                {"role": "user", "content": current},
            ]

        messages = assemble(0)
        total = estimate_message_tokens(messages, image_tokens)
        start = 0
        # Fast pass on per-line estimates, then confirm with the exact
        # estimate (date headers and reply quotes shift the total slightly).
        while total > budget and start < len(window):
            total -= line_tokens[start]
            start += 1
        if start:
            messages = assemble(start)
        while (start < len(window)
               and estimate_message_tokens(messages, image_tokens) > budget):
            start += 1
            messages = assemble(start)
        return messages, start

    # --------------------------------------------------------------- blocks

    def _chat_header(self, chat: Chat, bot: BotIdentity) -> str:
        lines = [
            "## Chat",
            f"Group: {chat.display_title} ({chat.type})",
        ]
        # The most recently active members, listed by name: an order that
        # follows activity would change the prompt prefix whenever someone
        # else speaks.
        members = [m for m in self.services.members.list(chat.chat_id)
                   if m.user_id != bot.id][:MAX_MEMBERS]
        members.sort(key=lambda m: (m.display_name.lower(), m.user_id))
        if members:
            lines.append("")
            lines.append("## Members")
            lines.extend(self._member_line(m) for m in members)
        return "\n".join(lines)

    def _background(self, chat: Chat, window: list[StoredMessage], tz: tzinfo) -> str:
        """What the bot knows beyond the recent messages: memory notes and the
        digest. Reference material, never requests. The slowest-changing
        part comes first, for the server's prompt cache."""
        services = self.services
        parts = []
        people = {person.id for person in services.people.for_users(
            {m.sender_id for m in window}).values()}
        notes = notes_for_prompt(services, chat.chat_id, people,
                                 self._settings["memory.prompt_notes"])
        if notes:
            parts.append("Group memory (notes you keep):\n"
                         + "\n".join(f"- {line}" for line in note_lines(services, notes)))
        digest = services.digests.get(chat.chat_id)
        if digest and digest.text:
            when = datetime.fromtimestamp(digest.updated_at, tz).strftime("%a %d %b, %H:%M")
            parts.append(f"What's been going on (digest, updated {when}):\n{digest.text}")
        if HISTORY_TOOL in get_skill(getattr(self, "_skill", DEFAULT_SKILL)).tools:
            start, end, count = services.history.coverage(chat.chat_id)
            if count:
                # Changes only when the archive does, so the prefix stays cached.
                parts.append(f"Summaries of earlier history: {describe_span(start, end, tz)} "
                             f"({count} periods). Look them up with {HISTORY_TOOL}.")
        return "\n\n".join(parts)

    def _shared_state(self, chat: Chat, tz: tzinfo) -> str:
        """What the group can see or has scheduled: the board, plans waiting
        for confirmation and pending reminders."""
        services = self.services
        parts = []
        board = self.services.boards.get(chat.chat_id)
        if not board.is_empty:
            parts.append(f"Pinned board:\n{board.as_text()}")
        cutoff = time.time() - OPEN_PLAN_DAYS * 86400
        proposed = [plan for plan in self.services.plans.for_chat(chat.chat_id, status=PROPOSED,
                                                                   limit=5)
                    if plan.created_at >= cutoff]
        if proposed:
            lines = ["Plans you proposed that nobody has confirmed yet:"]
            lines.extend(f"- plan {plan.id}: {plan.one_line()}" for plan in reversed(proposed))
            parts.append("\n".join(lines))
        reminders = services.reminders.for_chat(chat.chat_id, status=REMINDER_PENDING, limit=10)
        if reminders:
            lines = ["Pending reminders:"]
            lines.extend(f"- reminder {r.id}: "
                         f"{datetime.fromtimestamp(r.due_at, tz).strftime('%a %d %b %Y, %H:%M')}"
                         f" — {r.text}" for r in reminders)
            parts.append("\n".join(lines))
        return "\n\n".join(parts)

    @staticmethod
    def _member_line(member: Member) -> str:
        line = f"- {member.display_name} ({member.handle})"
        if member.is_bot:
            line += " (bot)"
        if member.aliases:
            line += f", also called {', '.join(member.aliases)}"
        return line

    def _name(self, message: StoredMessage, bot: BotIdentity) -> str:
        if message.from_bot:
            return f"{bot.name} ({self.self_label})"
        if message.sender_id is not None and message.sender_id not in self._names:
            self._names.update(self.services.people.display_names([message.sender_id]))
        return self._names.get(message.sender_id, message.sender_name)

    def _poll_voters(self, meta: dict) -> str:
        """Who voted for what, for non-anonymous polls."""
        votes = meta.get("votes") or {}
        options = meta.get("options") or []
        if not votes or not options:
            return ""
        names = self.services.people.display_names(int(user_id) for user_id in votes)
        parts = []
        for user_id, choices in votes.items():
            picked = [options[i] for i in choices if isinstance(i, int) and 0 <= i < len(options)]
            if picked:
                parts.append(f"{names.get(int(user_id), 'someone')} → {', '.join(picked)}")
        return f" (voted: {'; '.join(parts)})" if parts else ""

    def _body(self, message: StoredMessage, bot: BotIdentity, limit: int | None = None) -> str:
        text = strip_bot_mention(message.text, bot.username)
        body = message_body(text, message.media_kind, message.media_meta)
        description = self._descriptions.get(message.id)
        if description:
            marker = media_marker(message.media_kind, message.media_meta)
            seen = f"{marker[:-1]}: {_truncate(' '.join(description.split()), DESCRIPTION_CHARS)}]"
            body = body.replace(marker, seen, 1)
        if message.media_kind == "poll":
            body += self._poll_voters(message.media_meta)
        if message.forwarded_from:
            body = f"[forwarded from {message.forwarded_from}] {body}".rstrip()
        if limit is None:
            limit = self.services.settings["context.max_message_chars"]
        body = _truncate(body, limit)
        # Indent continuation lines so a message can never look like a new
        # transcript line.
        return body.replace("\n", "\n    ")

    def _quote(self, row_id: int, bot: BotIdentity) -> str | None:
        target = self.services.messages.get(row_id)
        if target is None:
            return None
        return f'{self._name(target, bot)}: "{self._body(target, bot, REPLY_QUOTE_CHARS)}"'

    def _reply_link(self, message: StoredMessage, previous_id: int | None,
                    kept_ids: set[int], bot: BotIdentity) -> str:
        if message.reply_to_row_id is not None:
            if message.reply_to_row_id == previous_id:
                return ""  # replying to the line just above: the link is noise
            if message.reply_to_row_id in kept_ids:
                return f" ↩{message.reply_to_row_id}"
            quote = self._quote(message.reply_to_row_id, bot)
            return f" ↩{message.reply_to_row_id} ({quote})" if quote else ""
        if message.reply_to_snippet:
            return f" ↩({_truncate(message.reply_to_snippet, REPLY_QUOTE_CHARS)})"
        return ""

    def _lines(self, window, bot, tz, kept_ids) -> list[tuple[int, str]]:
        lines = []
        previous_id = None
        for message in window:
            when = datetime.fromtimestamp(message.date, tz)
            reply = self._reply_link(message, previous_id, kept_ids, bot)
            lines.append((message.id, f"[{message.id}] {self._name(message, bot)} "
                                      f"({when.strftime('%H:%M')}){reply}: "
                                      f"{self._body(message, bot)}"))
            previous_id = message.id
        return lines

    def describe_messages(self, messages: list[StoredMessage], bot: BotIdentity) -> str:
        """Messages as dated transcript lines, for tool results."""
        tz = self.services.timezone()
        self._names.update(self.services.people.display_names(
            {m.sender_id for m in messages} - set(self._names)))
        self._descriptions.update(self.services.messages.descriptions(
            m.id for m in messages if m.media_kind))
        kept_ids = {m.id for m in messages}
        lines = []
        previous_id = None
        for message in messages:
            when = datetime.fromtimestamp(message.date, tz).strftime("%a %d %b %Y, %H:%M")
            reply = self._reply_link(message, previous_id, kept_ids, bot)
            lines.append(f"[{message.id}] {self._name(message, bot)} ({when}){reply}: "
                         f"{self._body(message, bot)}")
            previous_id = message.id
        return "\n".join(lines)

    def _transcript(self, window, bot, tz, kept_ids) -> str:
        if not window:
            return "(no earlier messages)"
        out = []
        last_day = None
        for message, (_, line) in zip(window, self._lines(window, bot, tz, kept_ids)):
            day = datetime.fromtimestamp(message.date, tz).date()
            if day != last_day:
                out.append(f"— {day.strftime('%a %d %b %Y')} —")
                last_day = day
            out.append(line)
        return "\n".join(out)

    def _current_request(self, trigger: StoredMessage, window: list[StoredMessage],
                         images, bot: BotIdentity, tz: tzinfo,
                         note: str | None = None,
                         now: datetime | None = None, state: str = "") -> str | list[dict]:
        now = now or datetime.now(tz)
        offset = now.strftime("%z")
        offset = f"UTC{offset[:3]}:{offset[3:]}" if offset else "local time"
        when = datetime.fromtimestamp(trigger.date, tz)
        who = self._name(trigger, bot)
        if trigger.sender_username:
            who += f" (@{trigger.sender_username})"
        head = f"{who} at {when.strftime('%H:%M')}"
        if trigger.id != EPHEMERAL_TRIGGER_ID:
            head = f"[{trigger.id}] {head}"
        extra = ""
        window_ids = {m.id for m in window}
        if trigger.reply_to_row_id is not None:
            head += f", replying to [{trigger.reply_to_row_id}]"
            if trigger.reply_to_row_id not in window_ids:
                target = self.services.messages.get(trigger.reply_to_row_id)
                if target is not None:
                    target_when = datetime.fromtimestamp(target.date, tz)
                    extra = (f"\nIt replies to this earlier message:\n"
                             f"[{target.id}] {self._name(target, bot)} "
                             f"({target_when.strftime('%a %d %b, %H:%M')}): "
                             f"{self._body(target, bot)}")
        elif trigger.reply_to_snippet:
            head += ", replying to a message you can't see"
            extra = f"\nThat message: {trigger.reply_to_snippet}"

        body = self._body(trigger, bot)
        if not body.strip():
            body = "(No text: they only mentioned you.)"
        text = (f"## Current request\nNow: {now.strftime('%a %d %b %Y, %H:%M')} ({offset})\n"
                f"{head}:\n{body}{extra}")
        if state:
            text = f"## Board, plans and reminders\n{STATE_NOTE}\n\n{state}\n\n{text}"
        if note:
            text += f"\n\n({note})"
        if not images:
            return text
        parts: list[dict] = [{"type": "text", "text": text}]
        for image in images:
            label = image.label or f"Image from message {image.row_id}"
            parts.append({"type": "text", "text": f"[{label}]"})
            parts.append({"type": "image_url",
                          "image_url": {"url": f"data:{image.mime_type};base64,{image.base64}"}})
        return parts
