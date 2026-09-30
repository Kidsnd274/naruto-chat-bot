"""Text helpers for prompts and model output (carried over from app/bot.py)."""

import json
import re

# Optional [REPLY] prefix the model uses to ask for its message to be sent as
# a threaded reply to the trigger. Tolerates whitespace and markdown wrapping
# (e.g. **[REPLY]**) since local models sometimes decorate the marker.
_REPLY_MARKER = re.compile(r"^\s*\**\s*\[reply\]\s*\**\s*", re.IGNORECASE)

# The model sometimes imitates the transcript format and starts its answer
# with "[123] Naruto (you) (18:05):".
_TRANSCRIPT_PREFIX = re.compile(r"^\s*\[\d+\]\s*[^\n:]{0,80}?\([^)\n]*\)\s*:\s*")

# Keys of the JSON the bot asks for in background requests (digest updates,
# import distillation). A chat reply that starts with such an object is the
# model answering in the wrong format, never something to post.
INTERNAL_JSON_KEYS = frozenset({"digest", "notes"})
_FENCE_OPEN = re.compile(r"^\s*```(?:json)?\s*", re.IGNORECASE)
_FENCE_CLOSE = re.compile(r"^\s*```")

# Portable estimate for OpenAI-compatible servers with different tokenizers.
_BYTES_PER_TOKEN = 4
_MESSAGE_OVERHEAD_TOKENS = 4
_REPLY_PRIMER_TOKENS = 2


def parse_reply_marker(text: str) -> tuple[bool, str]:
    """Detect a leading [REPLY] marker. Returns (should_reply, cleaned_text)."""
    if not text:
        return False, text
    match = _REPLY_MARKER.match(text)
    if match:
        return True, text[match.end():]
    return False, text


def strip_internal_json(text: str) -> tuple[str, str | None]:
    """Remove a JSON object in an internal format (e.g. ``{"digest": "",
    "notes": []}``, optionally in a code fence) from the start of a chat
    reply. A ``reply`` string inside it, or text after it, is kept. Returns
    ``(text, removed)``; ``removed`` is None when nothing was removed."""
    body = text or ""
    fence = _FENCE_OPEN.match(body)
    if fence:
        body = body[fence.end():]
    body = body.lstrip()
    if not body.startswith("{"):
        return text, None
    try:
        value, end = json.JSONDecoder().raw_decode(body)
    except json.JSONDecodeError:
        return text, None
    if not isinstance(value, dict) or not INTERNAL_JSON_KEYS & set(value):
        return text, None
    rest = body[end:]
    if fence:
        rest = _FENCE_CLOSE.sub("", rest, count=1)
    reply = value.get("reply")
    kept = "\n\n".join(part for part in (
        reply.strip() if isinstance(reply, str) else "", rest.strip()) if part)
    return kept, body[:end]


def clean_model_output(text: str, bot_name: str = "") -> tuple[bool, str]:
    """Strip imitated transcript prefixes and the [REPLY] marker (which may
    come before or after such a prefix). Returns (should_reply, text)."""
    text = text or ""
    should_reply, text = parse_reply_marker(text)
    text = _TRANSCRIPT_PREFIX.sub("", text, count=1)
    if bot_name:
        text = re.sub(rf"^\s*{re.escape(bot_name)}\s*(\(you\))?\s*:\s*", "", text,
                      count=1, flags=re.IGNORECASE)
    if not should_reply:
        should_reply, text = parse_reply_marker(text)
    return should_reply, text.strip()


def strip_bot_mention(text: str, bot_username: str) -> str:
    """Remove @bot_username. The stored history stays faithful to what people
    typed; the handle is hidden from the model because local models tend to
    mirror it back into their replies."""
    if not text or not bot_username:
        return text
    cleaned = re.sub(rf"@{re.escape(bot_username)}\b", "", text, flags=re.IGNORECASE)
    if cleaned == text:
        return text
    cleaned = re.sub(r" {2,}", " ", cleaned)
    return "\n".join(line.strip() for line in cleaned.split("\n")).strip()


def estimate_text_tokens(text: str) -> int:
    return (len(text.encode("utf-8")) + _BYTES_PER_TOKEN - 1) // _BYTES_PER_TOKEN


def estimate_message_tokens(messages: list[dict], image_tokens: int) -> int:
    """Estimate request tokens without a model-specific tokenizer. Images
    count a configured cost rather than their Base64 length."""
    total = _REPLY_PRIMER_TOKENS
    for message in messages:
        content = message.get("content", "")
        images = 0
        if isinstance(content, list):
            text = "".join(p.get("text", "") for p in content if p.get("type") == "text")
            images = sum(1 for p in content if p.get("type") == "image_url")
        else:
            text = str(content)
        total += _MESSAGE_OVERHEAD_TOKENS + estimate_text_tokens(text) + images * image_tokens
    return total


def without_image_data(messages: list[dict]) -> list[dict]:
    """Copy of a request with image data URLs replaced by a short summary,
    safe to log or store in traces."""
    result = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            result.append(dict(message))
            continue
        parts = []
        for part in content:
            if part.get("type") != "image_url":
                parts.append(dict(part))
                continue
            url = (part.get("image_url") or {}).get("url", "")
            mime_type, size_kb = "image", 0
            if url.startswith("data:") and ";base64," in url:
                header, encoded = url.split(",", 1)
                mime_type = header[5:].split(";", 1)[0] or "image"
                size_kb = max((len(encoded) * 3 // 4) - encoded.count("="), 0) // 1024
            parts.append({"type": "image_url", "image_url": {"url": f"<{mime_type}, {size_kb} KB>"}})
        result.append({**message, "content": parts})
    return result
