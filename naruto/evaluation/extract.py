"""Turn a real Telegram Desktop export into a case skeleton: the messages
before a chosen message become the chat, and that message the trigger.
Fill in ``expect`` by hand afterwards. Keep the result outside the
repository: it contains real messages."""

from collections import deque
from pathlib import Path

from naruto.importer.export_parser import ExportReader


class ExtractError(ValueError):
    pass


def extract_case(export_path: Path, trigger_id: int, *, before: int = 60,
                 case_id: str | None = None, category: str = "other",
                 bot_username: str = "naruto_bot") -> dict:
    window: deque = deque(maxlen=before)
    trigger = None
    with Path(export_path).open("rb") as handle:
        reader = ExportReader(handle)
        for message in reader.messages():
            if message.is_service:
                continue
            if message.id == trigger_id:
                trigger = message
                break
            window.append(message)
    if trigger is None:
        raise ExtractError(f"Message {trigger_id} is not in the export.")

    kept_ids = {m.id for m in window}
    members = {}

    def entry(message) -> dict:
        if message.sender_id is not None and message.sender_id > 0:
            members.setdefault(message.sender_id, {"id": message.sender_id, "name": message.sender_name})
        item = {"id": message.id, "from": message.sender_name, "date": message.date,
                "text": message.text}
        if message.sender_id is not None:
            item["from_id"] = message.sender_id
        if message.media_kind:
            item["media"] = message.media_kind
        if message.reply_to_id in kept_ids:
            item["reply_to"] = message.reply_to_id
        return item

    messages = [entry(m) for m in window]
    trigger_entry = entry(trigger)
    if f"@{bot_username}" not in trigger_entry["text"]:
        trigger_entry["text"] = f"@{bot_username} {trigger_entry['text']}".strip()
    return {
        "id": case_id or f"{Path(export_path).parent.name}-{trigger_id}",
        "category": category,
        "description": "TODO: what this case tests.",
        "chat": {"title": reader.header.name, "type": "group"},
        "members": list(members.values()),
        "messages": messages,
        "trigger": trigger_entry,
        "expect": {"manual": "TODO: what a good answer does (or add automatic checks)."},
    }
