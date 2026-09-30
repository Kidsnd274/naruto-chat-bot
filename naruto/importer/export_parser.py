"""Streaming parser for Telegram Desktop "Machine-readable JSON" exports
(result.json).

Confirmed against a real basic-group export (plan §6): top-level ``name``,
``type``, ``id`` and ``messages``; per message ``id``, ``type``,
``date_unixtime`` (a string), ``from``, ``from_id`` (``user123``), ``text``
(a string, or a list mixing strings and entity objects) and optional media
fields. Fields documented but not in that sample (``reply_to_message_id``,
service messages) are handled too; unknown fields are ignored.

The file is read with ijson, so a large export is never loaded into memory.
"""

from dataclasses import dataclass, field
from typing import IO, Iterator

import ijson

from naruto import markers

GROUP_TYPES = ("private_group", "private_supergroup", "public_supergroup")
SUPERGROUP_TYPES = ("private_supergroup", "public_supergroup")
FILE_NOT_INCLUDED = "(File not included"


class ExportError(ValueError):
    """The file is not a usable group export; the message is shown to the owner."""


@dataclass
class ExportHeader:
    name: str = ""
    type: str = ""
    id: int | None = None

    @property
    def is_group(self) -> bool:
        return self.type in GROUP_TYPES

    def bot_api_chat_ids(self) -> list[int]:
        """Bot API chat IDs this export may correspond to. Basic groups map
        to -id; supergroups to -100 followed by the id."""
        if self.id is None:
            return []
        basic = -self.id
        supergroup = int(f"-100{self.id}")
        if self.type in SUPERGROUP_TYPES:
            return [supergroup]
        if self.type == "private_group":
            return [basic]
        return [basic, supergroup]


@dataclass
class ExportMessage:
    id: int
    date: int
    sender_id: int | None
    sender_name: str
    text: str = ""
    edit_date: int | None = None
    media_kind: str | None = None
    media_meta: dict = field(default_factory=dict)
    forwarded_from: str | None = None
    reply_to_id: int | None = None
    service_action: str | None = None

    @property
    def is_service(self) -> bool:
        return self.service_action is not None


def peer_id(value) -> int | None:
    """``user123`` -> 123; ``channel123`` / ``chat123`` -> Bot API style
    negative IDs; anything else -> None."""
    if not isinstance(value, str):
        return None
    for prefix, convert in (("user", lambda n: n),
                            ("channel", lambda n: int(f"-100{n}")),
                            ("chat", lambda n: -n)):
        if value.startswith(prefix) and value[len(prefix):].isdigit():
            return convert(int(value[len(prefix):]))
    return None


def _timestamp(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_text(raw: dict) -> str:
    """Join ``text`` parts (strings and entity objects), falling back to
    ``text_entities``."""
    text = raw.get("text")
    if isinstance(text, str):
        return text
    if isinstance(text, list):
        return "".join(part if isinstance(part, str) else str(part.get("text", ""))
                       for part in text if isinstance(part, (str, dict)))
    entities = raw.get("text_entities")
    if isinstance(entities, list):
        return "".join(str(e.get("text", "")) for e in entities if isinstance(e, dict))
    return ""


def _media(raw: dict) -> tuple[str | None, dict]:
    def clean(**meta):
        return {k: v for k, v in meta.items() if v not in (None, "")}

    media_type = raw.get("media_type")
    if "photo" in raw:
        return markers.PHOTO, clean(width=raw.get("width"), height=raw.get("height"))
    if media_type == "sticker":
        return markers.STICKER, clean(emoji=raw.get("sticker_emoji"))
    if media_type == "animation":
        return markers.ANIMATION, {}
    if media_type == "video_file":
        return markers.VIDEO, clean(duration=raw.get("duration_seconds"))
    if media_type == "video_message":
        return markers.VIDEO_NOTE, clean(duration=raw.get("duration_seconds"))
    if media_type == "voice_message":
        return markers.VOICE, clean(duration=raw.get("duration_seconds"))
    if media_type == "audio_file":
        title = " – ".join(filter(None, [raw.get("performer"), raw.get("title")]))
        return markers.AUDIO, clean(title=title, file_name=raw.get("file_name"))
    if "file" in raw or "file_name" in raw:
        return markers.DOCUMENT, clean(file_name=raw.get("file_name"),
                                       mime_type=raw.get("mime_type"))
    poll = raw.get("poll")
    if isinstance(poll, dict):
        return markers.POLL, clean(question=poll.get("question"))
    if raw.get("place_name"):
        return markers.VENUE, clean(title=raw.get("place_name"))
    if isinstance(raw.get("location_information"), dict):
        return markers.LOCATION, {}
    contact = raw.get("contact_information")
    if isinstance(contact, dict):
        name = " ".join(filter(None, [contact.get("first_name"), contact.get("last_name")]))
        return markers.CONTACT, clean(name=name)
    return None, {}


def to_message(raw: dict) -> ExportMessage | None:
    """Convert one raw export message. Returns None for entries without a
    usable id or date."""
    message_id = raw.get("id")
    date = _timestamp(raw.get("date_unixtime"))
    if not isinstance(message_id, int) or date is None:
        return None
    kind = raw.get("type", "message")
    if kind == "service":
        sender_id = peer_id(raw.get("actor_id"))
        name = raw.get("actor") or "Someone"
        return ExportMessage(id=message_id, date=date, sender_id=sender_id,
                             sender_name=str(name), service_action=str(raw.get("action") or "service"))
    media_kind, media_meta = _media(raw)
    reply_to = raw.get("reply_to_message_id")
    return ExportMessage(
        id=message_id,
        date=date,
        sender_id=peer_id(raw.get("from_id")),
        sender_name=str(raw.get("from") or "Deleted Account"),
        text=build_text(raw),
        edit_date=_timestamp(raw.get("edited_unixtime")),
        media_kind=media_kind,
        media_meta=media_meta,
        forwarded_from=raw.get("forwarded_from") or None,
        # A reply to another chat carries reply_to_peer_id; it can't be linked.
        reply_to_id=reply_to if isinstance(reply_to, int) and "reply_to_peer_id" not in raw else None,
    )


class ExportReader:
    """Reads the header and yields messages in one pass.

    ``header`` is filled in as the top-level fields are read; Telegram
    Desktop writes them before ``messages``, so it is complete by the first
    message.
    """

    def __init__(self, handle: IO[bytes]):
        self.handle = handle
        self.header = ExportHeader()
        self.skipped = 0  # entries that could not be read

    def messages(self) -> Iterator[ExportMessage]:
        builder = None
        saw_messages = False
        try:
            for prefix, event, value in ijson.parse(self.handle, use_float=True):
                if builder is not None:
                    builder.event(event, value)
                    if prefix == "messages.item" and event == "end_map":
                        message = to_message(builder.value)
                        builder = None
                        if message is None:
                            self.skipped += 1
                        else:
                            yield message
                    continue
                if prefix == "messages.item" and event == "start_map":
                    builder = ijson.ObjectBuilder()
                    builder.event(event, value)
                elif prefix == "messages" and event == "start_array":
                    saw_messages = True
                elif prefix == "name" and event == "string":
                    self.header.name = value
                elif prefix == "type" and event == "string":
                    self.header.type = value
                elif prefix == "id" and event == "number":
                    self.header.id = int(value)
        except ijson.JSONError as exc:
            raise ExportError(f"The file is not valid JSON ({exc}).") from None
        if not saw_messages:
            raise ExportError(
                "No messages found. Export a single chat as “Machine-readable JSON” "
                "from Telegram Desktop (the file is called result.json)."
            )
