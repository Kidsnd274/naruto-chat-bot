"""Builds the model request for one triggered message.

Layout (plan §9), most stable first so the server can reuse its prompt cache:

1. system: persona, operating rules, the skill's instructions
2. user: chat details and members, background (later: memory notes and
   digest), recent messages one per line
3. user: the current request, labelled and included once, with any images

The recent window's start only moves in steps (see
MessageRepository.recent_window), so message 2 keeps the same prefix while new
messages are appended.
"""

from dataclasses import dataclass, field
from datetime import datetime, tzinfo

from naruto.agent.text import estimate_message_tokens, estimate_text_tokens, strip_bot_mention
from naruto.db.chats import Chat
from naruto.db.members import Member
from naruto.db.messages import StoredMessage
from naruto.markers import message_body
from naruto.services import BotIdentity, Services

MAX_MEMBERS = 50
REPLY_QUOTE_CHARS = 80


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
    def __init__(self, services: Services):
        self.services = services

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
    ) -> Prompt:
        settings = self.services.settings
        tz = self.services.timezone()
        now = (now or datetime.now(tz)).astimezone(tz)

        system = "\n\n".join(part for part in (
            settings["persona.prompt"].strip(),
            settings["prompt.rules"].strip(),
            settings[f"skills.{skill}.instructions"].strip(),
        ) if part)

        window = self.services.messages.recent_window(
            chat.chat_id,
            trigger,
            window=settings["context.recent_window"],
            step=settings["context.window_step"],
        )
        current = self._current_request(trigger, window, images, bot, tz)
        header = self._chat_header(chat, now, bot)

        messages, dropped = self._fit_budget(system, header, window, current, bot, tz)
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

    def _fit_budget(self, system, header, window, current, bot, tz):
        """Drop the oldest recent messages until the estimate fits."""
        settings = self.services.settings
        budget = settings["context.input_token_budget"]
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

    def _chat_header(self, chat: Chat, now: datetime, bot: BotIdentity) -> str:
        offset = now.strftime("%z")
        offset = f"UTC{offset[:3]}:{offset[3:]}" if offset else "local time"
        lines = [
            "## Chat",
            f"Group: {chat.display_title} ({chat.type})",
            f"Now: {now.strftime('%a %d %b %Y, %H:%M')} ({offset})",
        ]
        members = [m for m in self.services.members.list(chat.chat_id)
                   if m.user_id != bot.id][:MAX_MEMBERS]
        if members:
            lines.append("")
            lines.append("## Members")
            lines.extend(self._member_line(m) for m in members)
        return "\n".join(lines)

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
            return f"{bot.name} (you)"
        return message.sender_name

    def _body(self, message: StoredMessage, bot: BotIdentity, limit: int | None = None) -> str:
        text = strip_bot_mention(message.text, bot.username)
        body = message_body(text, message.media_kind, message.media_meta)
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
                         images, bot: BotIdentity, tz: tzinfo) -> str | list[dict]:
        when = datetime.fromtimestamp(trigger.date, tz)
        who = trigger.sender_name
        if trigger.sender_username:
            who += f" (@{trigger.sender_username})"
        head = f"[{trigger.id}] {who} at {when.strftime('%H:%M')}"
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
        text = f"## Current request\n{head}:\n{body}{extra}"
        if not images:
            return text
        parts: list[dict] = [{"type": "text", "text": text}]
        for image in images:
            label = image.label or f"Image from message {image.row_id}"
            parts.append({"type": "text", "text": f"[{label}]"})
            parts.append({"type": "image_url",
                          "image_url": {"url": f"data:{image.mime_type};base64,{image.base64}"}})
        return parts
