"""Text markers for media, shared by live recording and history import, so a
message reads the same wherever it came from: ``[photo]``, ``[sticker 😂]``,
``[GIF]``, ``[file: plan.pdf]``."""

PHOTO = "photo"
STICKER = "sticker"
ANIMATION = "animation"
VIDEO = "video"
VIDEO_NOTE = "video_note"
VOICE = "voice"
AUDIO = "audio"
DOCUMENT = "document"
POLL = "poll"
LOCATION = "location"
VENUE = "venue"
CONTACT = "contact"
DICE = "dice"
OTHER = "other"

# Kinds whose content can be shown to a vision model.
VISUAL_KINDS = {PHOTO, STICKER, ANIMATION, VIDEO, VIDEO_NOTE}


def media_marker(kind: str | None, meta: dict | None = None) -> str:
    if not kind:
        return ""
    meta = meta or {}
    match kind:
        case "photo":
            return "[photo]"
        case "sticker":
            emoji = (meta.get("emoji") or "").strip()
            return f"[sticker {emoji}]" if emoji else "[sticker]"
        case "animation":
            return "[GIF]"
        case "video":
            return "[video]"
        case "video_note":
            return "[video message]"
        case "voice":
            return "[voice message]"
        case "audio":
            title = (meta.get("title") or meta.get("file_name") or "").strip()
            return f"[audio: {title}]" if title else "[audio]"
        case "document":
            name = (meta.get("file_name") or "").strip()
            return f"[file: {name}]" if name else "[file]"
        case "poll":
            question = (meta.get("question") or "").strip()
            options = meta.get("options") or []
            if not options:
                return f"[poll: {question}]" if question else "[poll]"
            counts = list(meta.get("counts") or [])
            counts += [0] * (len(options) - len(counts))
            state = " (closed)" if meta.get("closed") else ""
            choices = ", ".join(f"{option} {count}" for option, count in zip(options, counts))
            return f"[poll{state}: {question} — votes: {choices}]"
        case "location":
            return "[location]"
        case "venue":
            title = (meta.get("title") or "").strip()
            return f"[venue: {title}]" if title else "[venue]"
        case "contact":
            name = (meta.get("name") or "").strip()
            return f"[contact: {name}]" if name else "[contact]"
        case "dice":
            emoji = meta.get("emoji") or "🎲"
            value = meta.get("value")
            return f"[dice {emoji} {value}]" if value is not None else f"[dice {emoji}]"
        case _:
            return "[media]"


def message_body(text: str, kind: str | None, meta: dict | None = None) -> str:
    """Marker followed by the text or caption."""
    marker = media_marker(kind, meta)
    if marker and text:
        return f"{marker} {text}"
    return marker or text
