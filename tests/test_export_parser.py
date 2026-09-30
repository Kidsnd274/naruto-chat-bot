"""Telegram Desktop export parsing, against a synthetic fixture with the
structure of a real export (the real sample stays outside the repository)."""

import io
import json
from pathlib import Path

import pytest

from naruto.importer.export_parser import ExportError, ExportHeader, ExportReader, build_text, peer_id

FIXTURE = Path(__file__).parent / "fixtures" / "export_basic_group.json"


@pytest.fixture
def parsed():
    with FIXTURE.open("rb") as handle:
        reader = ExportReader(handle)
        messages = {m.id: m for m in reader.messages()}
    return reader, messages


def test_header_and_chat_id_mapping(parsed):
    reader, _ = parsed
    header = reader.header
    assert (header.name, header.type, header.id) == ("BBQ crew", "private_group", 4001)
    assert header.is_group and header.bot_api_chat_ids() == [-4001]
    assert ExportHeader("x", "public_supergroup", 123).bot_api_chat_ids() == [-100123]
    assert ExportHeader("x", "unknown", 123).bot_api_chat_ids() == [-123, -100123]
    assert not ExportHeader("x", "personal_chat", 1).is_group
    assert ExportHeader().bot_api_chat_ids() == []


def test_all_readable_messages_come_back_in_order(parsed):
    reader, messages = parsed
    assert list(messages) == sorted(messages)
    assert 1000016 not in messages and reader.skipped == 1  # no date_unixtime


def test_plain_message(parsed):
    _, messages = parsed
    m = messages[1000002]
    assert (m.sender_id, m.sender_name, m.date, m.text) == (
        7, "Alice Tan", 1788228060, "Is everyone free Saturday for the BBQ?")
    assert m.media_kind is None and m.reply_to_id is None and not m.is_service


def test_text_array_edit_and_reply(parsed):
    _, messages = parsed
    m = messages[1000005]
    assert m.text == "Yes! Same place as https://example.com/pit ok?"
    assert m.edit_date == 1788228180 and m.reply_to_id == 1000002


def test_media_markers(parsed):
    _, messages = parsed
    assert (messages[1000006].media_kind, messages[1000006].text) == ("photo", "the pit last time")
    assert messages[1000006].media_meta == {"width": 1280, "height": 960}
    assert (messages[1000007].media_kind, messages[1000007].media_meta) == ("sticker", {"emoji": "😂"})
    assert messages[1000008].media_kind == "animation"
    assert messages[1000010].media_meta == {"file_name": "plan.pdf", "mime_type": "application/pdf"}
    assert messages[1000011].media_meta == {"question": "What time?"}
    assert messages[1000012].media_kind == "voice"
    assert messages[1000013].media_kind == "location"
    assert messages[1000017].media_meta == {"name": "Jon Lee"}


def test_senders_forwards_and_foreign_replies(parsed):
    _, messages = parsed
    assert messages[1000009].forwarded_from == "Weather Channel"
    assert messages[1000012].sender_name == "Deleted Account" and messages[1000012].sender_id == 10
    assert messages[1000013].sender_id == -1004001  # posted as the group
    assert messages[1000014].reply_to_id is None  # reply to another chat


def test_service_messages(parsed):
    _, messages = parsed
    created = messages[1000001]
    assert created.is_service and created.service_action == "create_group"
    assert (created.sender_id, created.sender_name) == (7, "Alice Tan")
    assert messages[1000015].service_action == "pin_message"


@pytest.mark.parametrize("value,expected", [
    ("user123", 123), ("channel55", -10055), ("chat9", -9), ("bot1", None), (None, None), ("user", None),
])
def test_peer_id(value, expected):
    assert peer_id(value) == expected


def test_build_text_falls_back_to_entities():
    assert build_text({"text_entities": [{"type": "plain", "text": "a"}, {"type": "bold", "text": "b"}]}) == "ab"
    assert build_text({}) == ""


def test_header_after_messages_is_still_read():
    data = {"messages": [{"id": 1, "type": "message", "date_unixtime": "5", "from": "A",
                          "from_id": "user1", "text": "x"}],
            "name": "Late", "type": "private_supergroup", "id": 77}
    reader = ExportReader(io.BytesIO(json.dumps(data).encode()))
    assert [m.text for m in reader.messages()] == ["x"]
    assert reader.header.bot_api_chat_ids() == [-10077]


@pytest.mark.parametrize("content,message", [
    (b"{not json", "not valid JSON"),
    (b'{"name": "x", "chats": {"list": []}}', "No messages found"),
])
def test_bad_files(content, message):
    with pytest.raises(ExportError, match=message):
        list(ExportReader(io.BytesIO(content)).messages())
