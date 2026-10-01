"""Import flow: upload, preview, matching and the import job."""

import asyncio
import io
import json
from pathlib import Path

import pytest

from naruto.db.imports import DISCARDED, DONE, FAILED, PAUSED, PREVIEW, REPLACED, RUNNING
from naruto.db.messages import IMPORT, LIVE, NewMessage
from naruto.importer import service as service_module
from naruto.importer.planning import ImportOptions
from naruto.importer.service import ImportProblem, ImportService, UploadTooLarge, dates_path

FIXTURE = Path(__file__).parent / "fixtures" / "export_basic_group.json"
CHAT = -4001
SEP_1 = 1788228060   # first message in the fixture
SEP_2 = 1788310800   # last message in the fixture
NOW = SEP_2 + 5 * 86400


@pytest.fixture
def importer(services, tmp_path, monkeypatch):
    monkeypatch.setattr(service_module.time, "time", lambda: NOW)
    services.settings.set("retention.imported_messages_days", 0, actor="t")  # keep all
    services.settings.set("import.distill_memory", False, actor="t")  # see test_memory.py
    return ImportService(services, tmp_path / "imports")


async def upload(importer, content: bytes | None = None, name="result.json"):
    return await importer.create_from_upload(io.BytesIO(content or FIXTURE.read_bytes()), name)


def raw_only(importer, record, **changes) -> ImportOptions:
    """Import the messages only (summaries need a model: see test_history.py)."""
    options = importer.default_options(record)
    options.archive = options.distill = False
    for name, value in changes.items():
        setattr(options, name, value)
    return options


async def run_import(importer, record, chat_id=CHAT, options=None):
    await importer.start(record.id, chat_id, options=options or raw_only(importer, record))
    await asyncio.gather(*importer._tasks.values())
    return importer.repo.get(record.id)


# ------------------------------------------------------------------ upload

async def test_upload_creates_a_preview(importer):
    record = await upload(importer)
    assert record.status == PREVIEW and Path(record.file_path).exists()
    assert (record.export_name, record.export_type, record.export_id) == ("BBQ crew", "private_group", 4001)
    preview = record.preview
    assert (preview["total"], preview["service"], preview["unreadable"]) == (12, 2, 1)
    assert (preview["first_date"], preview["last_date"]) == (SEP_1, SEP_2)
    assert preview["candidate_ids"] == [CHAT]
    assert preview["participants"][0] == {"id": 7, "name": "Alice Tan", "count": 4}
    assert preview["participant_count"] == 5
    assert preview["tz"] == "UTC" and sum(day[1] for day in preview["days"]) == 12
    assert [day[1] for day in preview["days"]] == [11, 1]  # Sep 1, Sep 2
    assert all(day[2] > 0 and len(day[3]) == 16 for day in preview["days"])
    assert importer.message_dates(record) == sorted(importer.message_dates(record))
    assert len(importer.message_dates(record)) == 12
    assert dates_path(record.file_path).exists()


async def test_upload_size_limit(importer, services):
    services.settings.set("import.max_upload_mb", 1, actor="t")
    with pytest.raises(UploadTooLarge, match="1 MB"):
        await upload(importer, b" " * (2 * 1024 * 1024))
    assert list(importer.upload_dir.iterdir()) == []


async def test_non_group_and_invalid_exports_are_rejected(importer):
    personal = json.dumps({"name": "Alice", "type": "personal_chat", "id": 7, "messages": []})
    with pytest.raises(ImportProblem, match="Only group exports"):
        await upload(importer, personal.encode())
    with pytest.raises(ImportProblem, match="not valid JSON"):
        await upload(importer, b"{broken")
    records = importer.repo.recent()
    assert all(r.status == FAILED and r.file_path is None for r in records)
    assert list(importer.upload_dir.iterdir()) == []


# ---------------------------------------------------------------- matching

async def test_match_by_id_alias_name_or_suggest(importer, services):
    record = await upload(importer)
    match = importer.match(record)
    assert match.chat is None and match.suggested_chat_id == CHAT

    services.chats.upsert_seen(-555, title="bbq CREW")
    match = importer.match(record)
    assert match.how == "name" and match.chat.chat_id == -555

    services.chats.upsert_seen(CHAT, title="Renamed")
    services.chats.migrate(CHAT, -1004001)  # the group became a supergroup
    match = importer.match(record)
    assert match.how == "id" and match.chat.chat_id == -1004001


async def test_estimate_counts_overlap_and_retention(importer, services):
    services.chats.upsert_seen(CHAT)
    services.messages.insert_live(NewMessage(
        chat_id=CHAT, origin_chat_id=CHAT, source=LIVE, message_id=1, sender_name="A",
        date=SEP_1 + 250))  # after the first three export messages
    record = await upload(importer)
    plan = importer.plan(record, CHAT, raw_only(importer, record))
    assert (plan.raw_selected, plan.raw_eligible, plan.raw_live) == (12, 3, 9)
    assert importer.plan(record, -999, raw_only(importer, record)).raw_live == 0

    services.settings.set("retention.imported_messages_days", 5, actor="t")  # cutoff Sep 2 09:00
    plan = importer.plan(record, -999, raw_only(importer, record))
    assert (plan.raw_eligible, plan.raw_too_old) == (1, 0)  # the default dates start on Sep 2
    options = raw_only(importer, record, raw_from=plan.options.raw_from.replace(day=1))
    plan = importer.plan(record, -999, options)
    assert (plan.raw_selected, plan.raw_eligible, plan.raw_too_old) == (12, 1, 11)


# ------------------------------------------------------------------ import

async def test_import_stores_messages_roster_and_replies(importer, services):
    services.chats.upsert_seen(CHAT, title="BBQ crew")
    record = await run_import(importer, await upload(importer))

    assert record.status == DONE and record.imported == 12 and record.processed == 12
    assert record.skipped_service == 2 and record.file_path is None
    assert list(importer.upload_dir.iterdir()) == []
    assert services.messages.count(CHAT, IMPORT) == 12

    page = services.messages.browse(CHAT, query="Same place")
    reply = page.messages[0]
    target = services.messages.get(reply.reply_to_row_id)
    assert target.text == "Is everyone free Saturday for the BBQ?"
    assert reply.edit_date == 1788228180 and reply.import_id == record.id

    sticker = services.messages.browse(CHAT, sender_id=7).messages
    assert any(m.media_kind == "sticker" and m.media_meta == {"emoji": "😂"} for m in sticker)
    alice = services.members.get(CHAT, 7)
    assert (alice.display_name, alice.source, alice.first_seen_at) == ("Alice Tan", "import", SEP_1)
    assert services.members.get(CHAT, 10).display_name == "Deleted Account"
    assert services.members.get(CHAT, -1004001) is None


async def test_import_skips_messages_at_or_after_first_live_message(importer, services):
    services.chats.upsert_seen(CHAT)
    services.messages.insert_live(NewMessage(
        chat_id=CHAT, origin_chat_id=CHAT, source=LIVE, message_id=1, sender_name="A",
        date=SEP_1 + 250))
    record = await run_import(importer, await upload(importer))
    assert (record.imported, record.skipped_overlap) == (3, 9)
    assert record.last_date < SEP_1 + 250


async def test_import_keeps_only_the_retention_window(importer, services):
    services.chats.upsert_seen(CHAT)
    services.settings.set("retention.imported_messages_days", 5, actor="t")  # cutoff = Sep 2 09:00
    record = await upload(importer)
    options = raw_only(importer, record)
    options.raw_from = options.raw_from.replace(day=1)
    record = await run_import(importer, record, options=options)
    assert (record.imported, record.skipped_retention, record.skipped_range) == (1, 11, 0)


async def test_import_keeps_only_the_chosen_dates(importer, services):
    services.chats.upsert_seen(CHAT)
    record = await upload(importer)
    options = raw_only(importer, record)
    options.raw_from = options.raw_to  # Sep 2 only
    record = await run_import(importer, record, options=options)
    assert (record.imported, record.skipped_range) == (1, 11)


async def test_a_narrower_reimport_replaces_only_its_dates(importer, services):
    services.chats.upsert_seen(CHAT)
    first = await run_import(importer, await upload(importer))
    assert services.messages.count(CHAT, IMPORT) == 12
    record = await upload(importer)
    options = raw_only(importer, record)
    options.raw_from = options.raw_to  # Sep 2 only
    second = await run_import(importer, record, options=options)
    assert second.imported == 1
    assert services.messages.count(CHAT, IMPORT) == 12  # Sep 1 from the first, Sep 2 new
    assert services.messages.count_import(first.id) == 11
    assert importer.repo.get(first.id).status == DONE  # still has messages


async def test_reimport_replaces_the_previous_import(importer, services):
    services.chats.upsert_seen(CHAT)
    first = await run_import(importer, await upload(importer))
    second = await run_import(importer, await upload(importer))
    assert importer.repo.get(first.id).status == REPLACED
    assert second.status == DONE
    assert services.messages.count(CHAT, IMPORT) == 12


async def test_import_into_unknown_chat_creates_it_pending(importer, services):
    record = await run_import(importer, await upload(importer))
    chat = services.chats.get(CHAT)
    assert chat.status == "pending" and chat.title == "BBQ crew"
    assert record.chat_id == CHAT


async def test_bot_messages_in_export_are_marked(importer, services):
    data = json.loads(FIXTURE.read_text())
    data["messages"].append({"id": 2000000, "type": "message", "date_unixtime": str(SEP_2 + 60),
                             "from": "Naruto", "from_id": "user42", "text": "Believe it!"})
    services.chats.upsert_seen(CHAT)
    await run_import(importer, await upload(importer, json.dumps(data).encode()))
    bot_message = services.messages.browse(CHAT, query="Believe").messages[0]
    assert bot_message.from_bot
    assert services.members.get(CHAT, 42) is None


async def test_duplicate_export_ids_are_kept(importer, services):
    data = json.loads(FIXTURE.read_text())
    data["messages"].append(dict(data["messages"][1], text="same id again"))
    services.chats.upsert_seen(CHAT)
    record = await run_import(importer, await upload(importer, json.dumps(data).encode()))
    assert record.imported == 13
    assert services.messages.browse(CHAT, query="same id again").total == 1


async def test_failed_import_rolls_back(importer, services, monkeypatch):
    services.chats.upsert_seen(CHAT)

    def explode(import_id):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(services.messages, "resolve_import_replies", explode)
    record = await run_import(importer, await upload(importer))
    assert record.status == PAUSED and record.raw_status == "paused"
    assert "disk on fire" in record.error
    assert services.messages.count(CHAT, IMPORT) == 0
    assert Path(record.file_path).exists()  # kept so it can resume
    assert record.source_expires_at == record.paused_at + 7 * 86400

    monkeypatch.undo()
    monkeypatch.setattr(service_module.time, "time", lambda: NOW)
    importer.resume(record.id)
    await asyncio.gather(*importer._tasks.values())
    record = importer.repo.get(record.id)
    assert record.status == DONE and record.imported == 12 and record.file_path is None


async def test_start_rules(importer, services):
    record = await upload(importer)
    importer.discard(record.id)
    assert importer.repo.get(record.id).status == DISCARDED
    assert list(importer.upload_dir.iterdir()) == []
    with pytest.raises(ImportProblem):
        await importer.start(record.id, CHAT)
    with pytest.raises(ImportProblem):
        importer.discard(record.id)


async def test_recover_rolls_back_interrupted_imports(importer, services):
    record = await upload(importer)
    importer.repo.update(record.id, status=RUNNING, chat_id=CHAT)
    services.messages.insert_imported([NewMessage(
        chat_id=CHAT, origin_chat_id=CHAT, source=IMPORT, message_id=1, sender_name="A",
        date=SEP_1, import_id=record.id)])
    importer.recover()
    recovered = importer.repo.get(record.id)
    assert recovered.status == FAILED and "restart" in recovered.error
    assert services.messages.count(CHAT) == 0 and recovered.file_path is None


async def test_stale_previews_are_discarded(importer):
    record = await upload(importer)
    assert importer.cleanup_stale_previews() == 0
    importer.repo.update(record.id, created_at=record.created_at - 2 * 86400)
    assert importer.cleanup_stale_previews() == 1
    assert importer.repo.get(record.id).status == DISCARDED


async def test_shutdown_stops_a_running_import_to_resume_later(importer, services, tmp_path):
    services.chats.upsert_seen(CHAT)
    record = await upload(importer)
    importer._stopping.set()  # as if shutdown began before the first batch
    await importer.start(record.id, CHAT, options=raw_only(importer, record))
    await importer.shutdown()
    stopped = importer.repo.get(record.id)
    assert stopped.status == RUNNING and stopped.raw_status == "waiting"
    assert services.messages.count(CHAT) == 0 and Path(stopped.file_path).exists()

    restarted = ImportService(services, tmp_path / "imports")  # the next start
    restarted.recover()
    assert restarted.resume_interrupted() == 1
    await asyncio.gather(*restarted._tasks.values())
    done = restarted.repo.get(record.id)
    assert done.status == DONE and done.imported == 12 and done.file_path is None


async def test_paused_imports_expire(importer, services):
    services.chats.upsert_seen(CHAT)
    record = await upload(importer)
    await importer.start(record.id, CHAT, options=raw_only(importer, record))
    importer.pause(record.id)
    await asyncio.gather(*importer._tasks.values())
    paused = importer.repo.get(record.id)
    assert paused.status == PAUSED and paused.raw_status == "paused"
    assert services.messages.count(CHAT) == 0
    assert importer.expire_paused(now=paused.source_expires_at - 1) == 0
    assert importer.expire_paused(now=paused.source_expires_at) == 1
    expired = importer.repo.get(record.id)
    assert expired.status == FAILED and expired.raw_status == "expired"
    assert expired.file_path is None and list(importer.upload_dir.iterdir()) == []


async def test_unexpected_read_errors_clean_up(importer, monkeypatch):
    def broken(path):
        raise PermissionError("nope")

    monkeypatch.setattr(importer, "analyse", broken)
    with pytest.raises(ImportProblem, match="Could not read the file"):
        await upload(importer)
    assert importer.repo.recent()[0].status == FAILED
    assert list(importer.upload_dir.iterdir()) == []


# ---------------------------------------------------------------- identities

async def test_identity_rows_match_known_accounts(importer, services):
    services.members.upsert_live(CHAT, 7, "Alice T.", "alice")  # seen live already
    record = await upload(importer)
    rows = {row.user_id: row for row in importer.identity_rows(record)}
    assert set(rows) == {7, 8, 9, 10}  # not the group itself (-1004001)
    assert rows[7].person.display_name == "Alice T." and rows[7].suggested_name == "Alice Tan"
    assert rows[8].person is None and rows[8].suggested_name == "Bob"

    services.people.set_name(rows[7].person.id, "Ally")
    rows = {row.user_id: row for row in importer.identity_rows(record)}
    assert rows[7].suggested_name == "Ally"  # a chosen name beats the export's


async def test_apply_identities_names_and_merges(importer, services):
    services.chats.upsert_seen(CHAT)
    services.members.upsert_live(CHAT, 7, "Alice T.", "alice")
    alice = services.people.person_id_for(7)
    services.members.upsert_live(CHAT, 99, "Wei (new phone)", "wei2")
    wei = services.people.person_id_for(99)
    services.people.set_name(wei, "Wei")
    record = await upload(importer)

    await importer.start(record.id, CHAT, identities={
        7: {"name": "Alice Tan", "merge_into": None},
        8: {"name": "", "merge_into": None},             # keep the default name
        9: {"name": "Wei W", "merge_into": wei},          # same person as account 99
        12345: {"name": "not in export", "merge_into": None},
    })
    import asyncio
    await asyncio.gather(*importer._tasks.values())

    assert services.people.get(alice).display_name == "Alice Tan"
    assert services.people.for_user(8).display_name == "Bob"
    merged = services.people.for_user(9)
    assert merged.id == wei and {a.user_id for a in merged.accounts} == {9, 99}
    assert merged.display_name == "Wei"  # merged into someone who already had a name
    assert services.people.for_user(12345) is None
    context_names = services.people.display_names([7, 9, 99])
    assert context_names == {7: "Alice Tan", 9: "Wei", 99: "Wei"}


async def test_names_equal_to_telegram_names_are_not_pinned(importer, services):
    services.chats.upsert_seen(CHAT)
    services.members.upsert_live(CHAT, 8, "Bob", None)
    record = await upload(importer)
    importer.apply_identities(record, {8: {"name": "Bob", "merge_into": None}})
    assert services.people.for_user(8).name is None  # still follows Telegram renames
