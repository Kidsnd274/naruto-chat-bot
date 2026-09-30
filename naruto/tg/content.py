"""Turn python-telegram-bot messages into stored-message records."""

from dataclasses import dataclass

from naruto import markers
from naruto.db.messages import LIVE, NewMessage

REPLY_SNIPPET_CHARS = 200


@dataclass
class Sender:
    id: int | None
    name: str
    username: str | None
    is_bot: bool


def sender_of(message) -> Sender:
    """Anonymous admins and channels post as a chat; everyone else as a user."""
    sender_chat = getattr(message, "sender_chat", None)
    if sender_chat is not None:
        return Sender(sender_chat.id, sender_chat.title or "Anonymous",
                      getattr(sender_chat, "username", None), False)
    user = getattr(message, "from_user", None)
    if user is not None:
        name = user.full_name or user.username or f"User {user.id}"
        return Sender(user.id, name, user.username, bool(getattr(user, "is_bot", False)))
    return Sender(None, "Unknown", None, False)


@dataclass
class MediaRef:
    kind: str
    file_id: str | None = None
    file_unique_id: str | None = None
    meta: dict | None = None


def _file_ref(kind: str, obj, **meta) -> MediaRef:
    return MediaRef(
        kind=kind,
        file_id=getattr(obj, "file_id", None),
        file_unique_id=getattr(obj, "file_unique_id", None),
        meta={k: v for k, v in meta.items() if v not in (None, "")},
    )


def _seconds(value) -> int | None:
    if value is None:
        return None
    if hasattr(value, "total_seconds"):
        return int(value.total_seconds())
    return int(value)


def media_of(message) -> MediaRef | None:
    """The media reference (Telegram file_id, never the bytes)."""
    photo = getattr(message, "photo", None)
    if photo:
        largest = photo[-1]
        return _file_ref(markers.PHOTO, largest, width=getattr(largest, "width", None),
                         height=getattr(largest, "height", None))
    sticker = getattr(message, "sticker", None)
    if sticker is not None:
        return _file_ref(markers.STICKER, sticker, emoji=getattr(sticker, "emoji", None),
                         set_name=getattr(sticker, "set_name", None),
                         animated=bool(getattr(sticker, "is_animated", False)) or None,
                         video=bool(getattr(sticker, "is_video", False)) or None)
    animation = getattr(message, "animation", None)
    if animation is not None:
        return _file_ref(markers.ANIMATION, animation,
                         mime_type=getattr(animation, "mime_type", None),
                         duration=_seconds(getattr(animation, "duration", None)))
    for attribute, kind in (("video", markers.VIDEO), ("video_note", markers.VIDEO_NOTE),
                            ("voice", markers.VOICE)):
        obj = getattr(message, attribute, None)
        if obj is not None:
            return _file_ref(kind, obj, mime_type=getattr(obj, "mime_type", None),
                             duration=_seconds(getattr(obj, "duration", None)))
    audio = getattr(message, "audio", None)
    if audio is not None:
        return _file_ref(markers.AUDIO, audio, title=getattr(audio, "title", None),
                         file_name=getattr(audio, "file_name", None),
                         duration=_seconds(getattr(audio, "duration", None)))
    document = getattr(message, "document", None)
    if document is not None:
        return _file_ref(markers.DOCUMENT, document,
                         file_name=getattr(document, "file_name", None),
                         mime_type=getattr(document, "mime_type", None),
                         file_size=getattr(document, "file_size", None))
    poll = getattr(message, "poll", None)
    if poll is not None:
        return MediaRef(markers.POLL, meta={"question": getattr(poll, "question", "")})
    venue = getattr(message, "venue", None)
    if venue is not None:
        return MediaRef(markers.VENUE, meta={"title": getattr(venue, "title", "")})
    if getattr(message, "location", None) is not None:
        return MediaRef(markers.LOCATION, meta={})
    contact = getattr(message, "contact", None)
    if contact is not None:
        name = " ".join(filter(None, [getattr(contact, "first_name", None),
                                      getattr(contact, "last_name", None)]))
        return MediaRef(markers.CONTACT, meta={"name": name})
    dice = getattr(message, "dice", None)
    if dice is not None:
        return MediaRef(markers.DICE, meta={"emoji": getattr(dice, "emoji", None),
                                            "value": getattr(dice, "value", None)})
    return None


def forwarded_from(message) -> str | None:
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        return None
    for attribute in ("sender_user", "sender_chat", "chat"):
        obj = getattr(origin, attribute, None)
        if obj is not None:
            return (getattr(obj, "full_name", None) or getattr(obj, "title", None)
                    or getattr(obj, "username", None) or "someone")
    return getattr(origin, "sender_user_name", None) or "someone"


def reply_snippet(reply_to_message, bot_id: int | None) -> str:
    """``Name: text`` for a replied-to message, used when the bot never
    stored that message itself."""
    sender = sender_of(reply_to_message)
    name = "you" if bot_id is not None and sender.id == bot_id else sender.name
    media = media_of(reply_to_message)
    text = (getattr(reply_to_message, "text", None)
            or getattr(reply_to_message, "caption", None) or "")
    body = markers.message_body(text.strip(), media.kind if media else None,
                                media.meta if media else None)
    if len(body) > REPLY_SNIPPET_CHARS:
        body = body[:REPLY_SNIPPET_CHARS - 1] + "…"
    return f"{name}: {body}" if body else name


def _timestamp(value) -> int | None:
    if value is None:
        return None
    return int(value.timestamp()) if hasattr(value, "timestamp") else int(value)


def to_new_message(message, *, chat_id: int, bot_id: int | None) -> NewMessage:
    """Build a live record. ``chat_id`` is the logical chat (after alias
    resolution); the origin is the chat the message was actually sent in."""
    sender = sender_of(message)
    media = media_of(message)
    reply = getattr(message, "reply_to_message", None)
    text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
    return NewMessage(
        chat_id=chat_id,
        origin_chat_id=message.chat_id,
        source=LIVE,
        message_id=message.message_id,
        sender_id=sender.id,
        sender_name=sender.name,
        sender_username=sender.username,
        from_bot=bot_id is not None and sender.id == bot_id,
        thread_id=getattr(message, "message_thread_id", None),
        date=_timestamp(getattr(message, "date", None)) or 0,
        edit_date=_timestamp(getattr(message, "edit_date", None)),
        text=text,
        media_kind=media.kind if media else None,
        media_file_id=media.file_id if media else None,
        media_file_unique_id=media.file_unique_id if media else None,
        media_meta=(media.meta or {}) if media else {},
        forwarded_from=forwarded_from(message),
        reply_to_message_id=reply.message_id if reply is not None else None,
        reply_to_snippet=reply_snippet(reply, bot_id) if reply is not None else None,
    )


def ephemeral_message_id(message) -> int | None:
    """Set on ephemeral commands (Bot API 10.2+). python-telegram-bot 22.8
    does not know the field, so it lands in api_kwargs."""
    value = (getattr(message, "api_kwargs", None) or {}).get("ephemeral_message_id")
    return int(value) if value is not None else None
