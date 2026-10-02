"""History digests from imports: periods, reading a period in parts,
checkpoints and resuming, reuse and replacement, and the live boundary."""

import asyncio
from datetime import datetime, timezone
import io
import json
import re
from zoneinfo import ZoneInfo

import pytest

from fakes import GROUP_ID
from naruto.db.history import MONTH, RANGE, WEEK
from naruto.importer.service import ImportService
from naruto.llm import ChatResult, LLMError
from naruto.memory import history as history_module
from naruto.periods import describe_span, period_label, plan_periods

UTC = timezone.utc


def ts(*parts) -> int:
    return int(datetime(*parts, tzinfo=UTC).timestamp())


def export(messages: list[tuple], chat_id: int = 4001) -> bytes:
    """messages: (id, unix time, sender id, name, text)."""
    return json.dumps({"name": "BBQ crew", "type": "private_group", "id": chat_id, "messages": [
        {"id": mid, "type": "message", "date_unixtime": str(when), "from": name,
         "from_id": f"user{uid}", "text": text} for mid, when, uid, name, text in messages
    ]}).encode()


def half_year() -> list[tuple]:
    """Jan–Jun 2024, nothing in March, a busy May."""
    rows = []
    mid = 100
    for month in (1, 2, 4, 6):
        for day in (3, 17):
            mid += 1
            rows.append((mid, ts(2024, month, day, 12), 7, "Alice", f"month {month} day {day}"))
    for i in range(40):
        mid += 1
        rows.append((mid, ts(2024, 5, 1 + i % 28, 9, i), 8, "Bob",
                     f"May message {i:02d} " + "about the bbq plans " * 12))
    return rows


class SummaryLLM:
    """Answers summary requests with a line naming the period and part, and
    records every request. ``fail`` decides per call whether to raise."""

    def __init__(self, fail=None):
        self.calls: list[dict] = []
        self.fail = fail or (lambda call: False)
        self.in_flight = self.waiting = 0

    async def chat(self, messages, *, reasoning=None, max_tokens=None, tools=None,
                   stream=False, background=False, info=None):
        user = messages[1]["content"]
        call = {"messages": messages, "info": info, "background": background, "user": user}
        self.calls.append(call)
        if self.fail(call):
            raise LLMError("server down")
        period = re.search(r"^Period: (.*)$", user, re.M).group(1)
        part = re.search(r"## Messages \(part (\d+) of (\d+)", user)
        lines = len([line for line in user.split("## Messages")[1].splitlines()[1:] if line])
        carried = "carried" if "## Summary so far" in user else "fresh"
        return ChatResult(text=f"- {period}: part {part.group(1)} of {part.group(2)}, "
                               f"{lines} lines, {carried}", reasoning=None, model="m",
                          latency_ms=1, usage=None, finish_reason="stop")


@pytest.fixture
def importer(services, tmp_path, monkeypatch):
    monkeypatch.setattr(history_module, "RETRY_DELAYS_SECONDS", (0, 0))
    services.settings.set("history.chunk_tokens", 2000, actor="t")
    # 1,000 tokens of messages per request, whatever the prompt around them
    # takes (test_a_request_fits_its_tokens checks that part).
    monkeypatch.setattr(history_module.HistoryWriter, "message_budget",
                        lambda self, chat, period, opts: 1000)
    return ImportService(services, tmp_path / "imports")


async def upload(importer, rows=None):
    return await importer.create_from_upload(io.BytesIO(export(rows or half_year())),
                                             "result.json")


def summaries_only(importer, record, **changes):
    options = importer.default_options(record)
    options.raw = False
    for name, value in changes.items():
        setattr(options, name, value)
    return options


async def run(importer, record, options, chat_id=GROUP_ID):
    await importer.start(record.id, chat_id, options=options)
    await asyncio.gather(*importer._tasks.values())
    return importer.repo.get(record.id)


def active(services):
    return services.history.for_chat(GROUP_ID)[0]


# ----------------------------------------------------------------- periods

def test_months_weeks_and_ranges():
    start, end = ts(2024, 1, 15), ts(2024, 3, 10)
    assert plan_periods(start, end, MONTH, UTC) == [
        (start, ts(2024, 2, 1)), (ts(2024, 2, 1), ts(2024, 3, 1)), (ts(2024, 3, 1), end)]
    weeks = plan_periods(ts(2024, 1, 3), ts(2024, 1, 20), WEEK, UTC)
    assert weeks[0] == (ts(2024, 1, 3), ts(2024, 1, 8))  # Monday 8 Jan starts the next one
    assert weeks[1] == (ts(2024, 1, 8), ts(2024, 1, 15)) and len(weeks) == 3
    assert plan_periods(start, end, RANGE, UTC) == [(start, end)]
    assert plan_periods(end, start, MONTH, UTC) == []

    london = ZoneInfo("Europe/London")  # clocks go forward on 31 Mar 2024
    march, april = plan_periods(int(datetime(2024, 3, 1, tzinfo=london).timestamp()),
                                int(datetime(2024, 5, 1, tzinfo=london).timestamp()), MONTH,
                                london)
    assert march[1] - march[0] == 31 * 86400 - 3600
    assert april[1] - april[0] == 30 * 86400
    assert datetime.fromtimestamp(april[0], london).hour == 0


def test_labels():
    assert period_label(ts(2024, 2, 1), ts(2024, 3, 1), MONTH, UTC) == "February 2024"
    assert period_label(ts(2024, 2, 5), ts(2024, 2, 12), WEEK, UTC) == "Week of 5 Feb 2024"
    assert period_label(ts(2024, 2, 17), ts(2024, 3, 1), MONTH, UTC) == "17–29 Feb 2024"
    assert describe_span(ts(2023, 12, 30), ts(2024, 1, 2), UTC) == "30 Dec 2023 – 1 Jan 2024"


# ------------------------------------------------------------ summarizing

async def test_old_history_becomes_dated_summaries(services, importer):
    services.llm = SummaryLLM()
    record = await upload(importer)
    plan = importer.plan(record, GROUP_ID, summaries_only(importer, record))
    assert plan.ok and plan.archive_count == 48 and plan.archive_not_raw == 48
    assert [p.label for p in plan.active_periods] == [
        "January 2024", "February 2024", "April 2024", "May 2024", "1–17 Jun 2024"]
    assert plan.active_periods[-1].partial  # the export ends on 17 June

    record = await run(importer, record, summaries_only(importer, record))
    assert record.status == "done" and record.archive_status == "done"
    assert (record.archive_total, record.archive_done) == (5, 5)
    assert record.raw_status == "skipped" and services.messages.count(GROUP_ID) == 0
    assert record.file_path is None and list(importer.upload_dir.iterdir()) == []
    digests = active(services)
    assert [d.period_start for d in digests] == [ts(2024, 1, 1), ts(2024, 2, 1), ts(2024, 4, 1),
                                                 ts(2024, 5, 1), ts(2024, 6, 1)]
    january = digests[0]
    assert january.text == "- January 2024 (1–31 Jan 2024): part 1 of 1, 2 lines, fresh"
    assert (january.message_count, january.first_message_at, january.source) == (
        2, ts(2024, 1, 3, 12), "export")
    assert january.import_id == record.id
    assert january.limitations == [
        "The export starts on 3 Jan 2024; earlier messages of this period aren't in it."]
    assert digests[1].limitations == []
    assert digests[-1].period_end == ts(2024, 6, 18)  # the chosen dates end with the export
    assert services.digests.get(GROUP_ID) is None  # the rolling digest isn't touched
    call = services.llm.calls[0]
    assert call["background"] and call["info"].task == "history"
    assert call["info"].period_id and call["info"].chunk == 1
    assert "January 2024" in call["messages"][0]["content"]  # {period} filled in
    runs = services.runs.recent(chat_id=GROUP_ID)[0]
    assert {run.skill for run in runs} == {"history"} and all(r.status == "ok" for r in runs)


async def test_a_busy_month_is_read_in_parts_in_date_order(services, importer):
    services.llm = SummaryLLM()
    rows = half_year()
    rows.reverse()  # an export that isn't in date order
    rows.append((999, ts(2024, 5, 1, 9, 0), 9, "Wei", "same minute as May message 00"))
    record = await upload(importer, rows)
    record = await run(importer, record, summaries_only(importer, record))
    may = [c for c in services.llm.calls if "May 2024" in c["user"]]
    parts = int(re.search(r"part 1 of (\d+)", may[0]["user"]).group(1))
    assert parts >= 3 and len(may) == parts
    assert "## Summary so far" not in may[0]["user"]
    assert f"## Summary so far (part 1 of {parts})" in may[1]["user"]
    assert "- May 2024 (1–31 May 2024): part 1 of" in may[1]["user"]  # the carried summary
    first_lines = may[0]["user"].split("## Messages")[1].splitlines()[1:3]
    assert "May message 00" in first_lines[0] and "same minute" in first_lines[1]
    digest = next(d for d in active(services) if d.period_start == ts(2024, 5, 1))
    assert digest.message_count == 41 and digest.text.endswith("carried")
    lines = sum(len(c["user"].split("## Messages")[1].splitlines()[1:]) for c in may)
    assert lines == 41  # every message read exactly once


async def test_reuploading_the_same_export_reuses_its_summaries(services, importer):
    services.llm = SummaryLLM()
    first = await upload(importer)
    await run(importer, first, summaries_only(importer, first))
    before = {d.id for d in active(services)}
    calls = len(services.llm.calls)
    again = await upload(importer)
    plan = importer.plan(again, GROUP_ID, summaries_only(importer, again))
    assert plan.ok and plan.work_periods == []  # nothing to do
    record = await run(importer, again, summaries_only(importer, again))
    assert record.status == "done" and len(services.llm.calls) == calls
    assert {d.id for d in active(services)} == before
    periods = services.history.periods_for_import(record.id)
    assert {p.status for p in periods} == {"reused"}

    record = await upload(importer)
    options = summaries_only(importer, record, regenerate=True, replace=True)
    record = await run(importer, record, options)
    assert len(services.llm.calls) > calls and not ({d.id for d in active(services)} & before)


async def test_replacing_summaries_needs_permission_and_is_all_at_once(services, importer):
    services.llm = SummaryLLM()
    first = await upload(importer)
    await run(importer, first, summaries_only(importer, first))
    old = {d.id: d for d in active(services)}
    january = min(old.values(), key=lambda d: d.period_start)
    services.history.edit(january.id, "- January: corrected by the owner", actor="owner")

    services.settings.set("history.digest_max_chars", 2000, actor="t")  # changes every summary
    record = await upload(importer)
    plan = importer.plan(record, GROUP_ID, summaries_only(importer, record))
    assert any("Select “Replace existing summaries”" in e for e in plan.errors)
    plan = importer.plan(record, GROUP_ID, summaries_only(importer, record, replace=True))
    assert any("edited by you" in e for e in plan.errors)
    weekly = summaries_only(importer, record, replace=True, replace_edited=True,
                            grouping=WEEK)
    plan = importer.plan(record, GROUP_ID, weekly)
    assert plan.ok and len(plan.replaced_digests) == 5

    overlapping_at_some_point = []

    class Watching(SummaryLLM):
        async def chat(self, messages, **kwargs):
            # Old digests are swapped out only once all their replacements
            # are done, so active digests never overlap (no double count).
            digests = active(services)
            overlapping_at_some_point.extend(
                (a.id, b.id) for a in digests for b in digests
                if a.id < b.id and a.overlaps(b.period_start, b.period_end))
            return await super().chat(messages, **kwargs)

    services.llm = Watching()
    record = await run(importer, record, weekly)
    assert record.status == "done"
    assert overlapping_at_some_point == []
    now_active = active(services)
    assert {d.grouping for d in now_active} == {"week"}
    assert not set(old) & {d.id for d in now_active}
    assert services.history.get(january.id).status == "replaced"


async def test_a_partial_overlap_is_refused(services, importer):
    services.llm = SummaryLLM()
    first = await upload(importer)
    await run(importer, first, summaries_only(importer, first))
    record = await upload(importer)
    options = summaries_only(importer, record, replace=True, regenerate=True)
    options.archive_from = options.archive_from.replace(day=15)  # mid-January
    plan = importer.plan(record, GROUP_ID, options)
    assert any("partly outside these dates" in e and "1–31 Jan 2024" in e for e in plan.errors)


async def test_a_failing_period_pauses_and_resumes_without_redoing_work(services, importer):
    down = {"value": True}
    services.llm = SummaryLLM(fail=lambda call: down["value"] and "April 2024" in call["user"])
    record = await upload(importer)
    record = await run(importer, record, summaries_only(importer, record))
    assert record.status == "paused" and record.archive_status == "paused"
    assert "April" in record.archive_error or "Apr" in record.archive_error
    assert "kept failing: server down" in record.archive_error
    assert record.archive_done == 2  # January and February are kept
    assert len(active(services)) == 2
    statuses = [p.status for p in services.history.periods_for_import(record.id)]
    assert statuses == ["done", "done", "failed", "waiting", "waiting"]
    assert len([c for c in services.llm.calls if "April 2024" in c["user"]]) == 3

    down["value"] = False
    calls = len(services.llm.calls)
    importer.resume(record.id)
    await asyncio.gather(*importer._tasks.values())
    record = importer.repo.get(record.id)
    assert record.status == "done" and len(active(services)) == 5
    redone = [c for c in services.llm.calls[calls:] if "January 2024" in c["user"]]
    assert redone == []


async def test_a_restart_continues_from_the_last_part(services, importer, tmp_path):
    first_service = importer

    class StopAfterFirstMayPart(SummaryLLM):
        async def chat(self, messages, **kwargs):
            result = await super().chat(messages, **kwargs)
            if "May 2024" in messages[1]["content"]:
                first_service._stopping.set()  # the bot shuts down now
            return result

    services.llm = StopAfterFirstMayPart()
    record = await upload(importer)
    await importer.start(record.id, GROUP_ID, options=summaries_only(importer, record))
    await asyncio.gather(*importer._tasks.values())
    record = importer.repo.get(record.id)
    assert record.status == "running" and record.file_path
    may = next(p for p in services.history.periods_for_import(record.id)
               if p.period_start == ts(2024, 5, 1))
    assert may.chunks_done == 1 and may.consumed > 0 and may.partial

    services.llm = SummaryLLM()
    restarted = ImportService(services, tmp_path / "imports")
    restarted.recover()
    restarted.resume_interrupted()
    await asyncio.gather(*restarted._tasks.values())
    record = restarted.repo.get(record.id)
    assert record.status == "done" and len(active(services)) == 5
    resumed = [c for c in services.llm.calls if "May 2024" in c["user"]]
    assert "part 2 of" in resumed[0]["user"] and "## Summary so far" in resumed[0]["user"]


async def test_summaries_stop_where_live_recording_began(services, importer):
    services.llm = SummaryLLM()
    services.chats.upsert_seen(GROUP_ID, title="BBQ crew")
    services.chats.set_status(GROUP_ID, "enabled")
    services.chats.set_recording_since(GROUP_ID, ts(2024, 5, 15))
    record = await upload(importer)
    plan = importer.plan(record, GROUP_ID, summaries_only(importer, record))
    assert plan.archive_end == ts(2024, 5, 15) and plan.archive_after_boundary > 0
    record = await run(importer, record, summaries_only(importer, record))
    may = active(services)[-1]
    assert (may.period_start, may.period_end) == (ts(2024, 5, 1), ts(2024, 5, 15))
    assert any("Live recording began on 15 May 2024" in note for note in may.limitations)
    assert all(d.period_end <= ts(2024, 5, 15) for d in active(services))


async def test_cancelling_keeps_finished_summaries_and_drops_staged_ones(services, importer):
    services.llm = SummaryLLM()
    first = await upload(importer)
    await run(importer, first, summaries_only(importer, first))
    old = {d.id for d in active(services)}
    services.llm = SummaryLLM(fail=lambda call: "June" in call["user"] or "Jun" in call["user"])
    record = await upload(importer)
    options = summaries_only(importer, record, replace=True, regenerate=True, grouping=RANGE)
    options.archive_from = options.archive_from.replace(month=1, day=1)
    record = await run(importer, record, options)
    assert record.status == "paused"
    assert importer.cancel_unfinished(record.id) == "Cancelled the rest of the import."
    record = importer.repo.get(record.id)
    assert record.archive_status == "cancelled" and record.file_path is None
    assert {d.id for d in active(services)} == old  # the old ones stay
    assert services.history.for_chat(GROUP_ID, status="staged")[1] == 0


async def replace_weekly(services, importer, llm, **changes):
    """Summarize monthly, then start replacing it weekly with ``llm``."""
    services.llm = SummaryLLM()
    first = await upload(importer)
    await run(importer, first, summaries_only(importer, first))
    old = {d.id for d in active(services)}
    services.settings.set("history.digest_max_chars", 2000, actor="t")  # new summaries
    record = await upload(importer)
    services.llm = llm
    options = summaries_only(importer, record, replace=True, grouping=WEEK, **changes)
    return old, await run(importer, record, options)


async def test_pausing_a_replacement_keeps_its_finished_parts(services, importer):
    holder = {}

    class PauseAfterTwoWeeks(SummaryLLM):
        async def chat(self, messages, **kwargs):
            result = await super().chat(messages, **kwargs)
            if len(self.calls) == 2:
                importer.pause(holder["id"])
            return result

    real_start = importer.start

    async def start(import_id, *args, **kwargs):
        holder["id"] = import_id
        return await real_start(import_id, *args, **kwargs)

    importer.start = start
    old, record = await replace_weekly(services, importer, PauseAfterTwoWeeks())
    assert record.status == "paused" and record.archive_status == "paused"
    first, second = services.history.periods_for_import(record.id)[:2]
    # The first week is kept as a staged summary; the second, paused during
    # its last request, keeps that request's result as its checkpoint.
    assert first.status == "done" and services.history.get(first.digest_id).status == "staged"
    assert second.status == "waiting" and second.partial and second.chunks_done == 1
    assert {d.id for d in active(services)} == old  # the old summaries stay in use

    calls = len(services.llm.calls)
    importer.resume(record.id)
    await asyncio.gather(*importer._tasks.values())
    record = importer.repo.get(record.id)
    assert record.status == "done" and {d.grouping for d in active(services)} == {"week"}
    redone = [c for c in services.llm.calls[calls:]
              if any(c["user"] == done["user"] for done in services.llm.calls[:2])]
    assert redone == []
    assert services.history.for_chat(GROUP_ID, status="staged")[1] == 0


async def test_a_summary_deleted_before_publishing_is_made_again(services, importer):
    class DeleteTheFirstWeek(SummaryLLM):
        async def chat(self, messages, **kwargs):
            if len(self.calls) == 3:
                first_week = services.history.for_chat(GROUP_ID, status="staged")[0][0]
                services.history.delete(first_week.id)
            return await super().chat(messages, **kwargs)

    old, record = await replace_weekly(services, importer, DeleteTheFirstWeek())
    assert record.status == "paused" and "went missing" in record.archive_error
    # January and February are replaced together (a week spans both), so
    # neither is replaced in part; April to June were replaced meanwhile.
    january, february = sorted(old)[:2]
    assert services.history.get(january).status == "active"
    assert services.history.get(february).status == "active"
    early = [d for d in active(services) if d.period_start < ts(2024, 3, 1)]
    assert {d.id for d in early} == {january, february}

    importer.resume(record.id)
    await asyncio.gather(*importer._tasks.values())
    record = importer.repo.get(record.id)
    assert record.status == "done" and {d.grouping for d in active(services)} == {"week"}
    periods = services.history.periods_for_import(record.id)
    assert all(p.status == "done" and p.digest_id for p in periods)
    digest_ids = [p.digest_id for p in periods]
    assert len(digest_ids) == len(set(digest_ids))


async def test_an_owner_edit_during_a_replacement_wins(services, importer):
    class OwnerEditsJanuary(SummaryLLM):
        async def chat(self, messages, **kwargs):
            if len(self.calls) == 1:
                january = active(services)[0]
                services.history.edit(january.id, "- January, as the owner remembers it",
                                      actor="owner")
            return await super().chat(messages, **kwargs)

    old, record = await replace_weekly(services, importer, OwnerEditsJanuary())
    assert record.status == "done"
    assert any("edited while this ran" in note for note in record.limitations)
    january = services.history.get(min(old))
    assert january.status == "active" and january.text.startswith("- January, as the owner")
    assert services.history.for_chat(GROUP_ID, status="staged")[1] == 0
    assert all(d.grouping == "month" for d in active(services)
               if d.overlaps(january.period_start, january.period_end))


async def test_pause_stops_a_job_waiting_for_the_model(services, importer):
    waiting = asyncio.Event()
    stopped = []

    class Waits(SummaryLLM):
        async def chat(self, messages, **kwargs):
            self.calls.append(messages)
            waiting.set()
            try:
                await asyncio.Event().wait()  # no slot: background work is paused
            except asyncio.CancelledError:
                stopped.append(True)
                raise

    services.llm = Waits()
    record = await upload(importer)
    await importer.start(record.id, GROUP_ID, options=summaries_only(importer, record))
    await asyncio.wait_for(waiting.wait(), 5)
    importer.pause(record.id)
    await asyncio.wait_for(asyncio.gather(*importer._tasks.values()), 5)
    record = importer.repo.get(record.id)
    assert record.status == "paused" and record.archive_error == "Paused by you."
    assert stopped == [True]
    assert [p.status for p in services.history.periods_for_import(record.id)][0] == "waiting"
    assert not services.history_locks[GROUP_ID].locked()

    waiting.clear()
    importer.resume(record.id)
    await asyncio.wait_for(waiting.wait(), 5)
    assert importer.cancel_unfinished(record.id) == "Stopping."
    await asyncio.wait_for(asyncio.gather(*importer._tasks.values()), 5)
    record = importer.repo.get(record.id)
    assert record.archive_status == "cancelled" and record.file_path is None


async def test_cancel_while_waiting_for_the_chat(services, importer):
    services.llm = SummaryLLM()
    record = await upload(importer)
    lock = services.history_locks[GROUP_ID]
    await lock.acquire()  # the live archiver is busy with this chat
    try:
        await importer.start(record.id, GROUP_ID, options=summaries_only(importer, record))
        for _ in range(5):
            await asyncio.sleep(0)
        importer.cancel_unfinished(record.id)
        await asyncio.wait_for(asyncio.gather(*importer._tasks.values()), 5)
    finally:
        lock.release()
    record = importer.repo.get(record.id)
    assert record.archive_status == "cancelled" and record.file_path is None
    assert services.llm.calls == []


def test_history_moves_with_a_group_upgrade(services):
    services.chats.upsert_seen(GROUP_ID)
    digest = services.history.add(
        chat_id=GROUP_ID, status="active", source="export", grouping=MONTH, timezone="UTC",
        period_start=ts(2020, 1, 1), period_end=ts(2020, 2, 1), first_message_at=None,
        last_message_at=None, message_count=3, import_id=None, period_id=None,
        fingerprint="x", text="- January 2020", limitations=[], actor="t")
    services.chats.migrate(GROUP_ID, -1004001)
    moved = services.history.get(digest.id)
    assert moved.chat_id == -1004001 and moved.status == "active"
    assert services.history.search(-1004001, query="January")[1] == 1


# ------------------------------------------------------------------ lookup

def add_digest(services, chat_id, start, end, text, *, edited=False, **extra):
    digest = services.history.add(
        chat_id=chat_id, status="active", source="export", grouping=MONTH, timezone="UTC",
        period_start=start, period_end=end, first_message_at=start + 3600,
        last_message_at=end - 3600, message_count=10, import_id=None, period_id=None,
        fingerprint="x", text=text, limitations=extra.get("limitations", []), actor="t")
    if edited:
        services.history.edit(digest.id, text + " (fixed)", actor="owner")
    return digest


@pytest.fixture
def archive(services):
    services.chats.upsert_seen(GROUP_ID, title="BBQ crew")
    services.chats.set_status(GROUP_ID, "enabled")
    for month, text in ((6, "- Planned a camping trip to Pulau Ubin"),
                        (7, "- The camping trip happened; Wei forgot the tent"),
                        (8, "- Talked about the National Day BBQ")):
        add_digest(services, GROUP_ID, ts(2021, month, 1), ts(2021, month + 1, 1), text,
                   limitations=["The export starts on 3 Jun 2021."] if month == 6 else [])
    add_digest(services, -999, ts(2021, 7, 1), ts(2021, 8, 1), "- Another group's camping")


async def lookup(services, **args):
    from fakes import FakeBot, ScriptedLLM, tool_call
    from naruto.agent.runner import AgentRunner, RunRequest
    from naruto.db.messages import LIVE, NewMessage

    trigger = services.messages.insert_live(NewMessage(
        chat_id=GROUP_ID, origin_chat_id=GROUP_ID, source=LIVE, message_id=99, sender_id=7,
        sender_name="Alice", date=ts(2026, 9, 1), text="@naruto_bot what happened in 2021?"))
    llm = ScriptedLLM([tool_call("search_history_summaries", args)], "Here's what I found.")
    runner = AgentRunner(services, FakeBot(), llm=llm)
    await runner.run(RunRequest(chat=services.chats.get(GROUP_ID), trigger=trigger,
                                bot=services.status.bot))
    return llm, [m["content"] for m in llm.calls[1]["messages"] if m["role"] == "tool"][0]


async def test_the_bot_looks_up_old_summaries_of_its_own_chat(services, archive):
    llm, result = await lookup(services, query="camping trip")
    assert "search_history_summaries" in [t["function"]["name"] for t in llm.calls[0]["tools"]]
    assert "not the original messages" in result
    assert "### July 2021" in result and "Wei forgot the tent" in result
    assert "### June 2021" in result and "Note: The export starts on 3 Jun 2021." in result
    assert "Another group" not in result  # another chat's archive stays out
    background = llm.calls[0]["messages"][1]["content"]
    assert "Summaries of earlier history: 1 Jun – 31 Aug 2021 (3 periods)" in background

    _, by_date = await lookup(services, since="2021-08", until="2021-08")
    assert "National Day" in by_date and "camping" not in by_date
    services.settings.set("history.lookup_results", 1, actor="t")
    _, paged = await lookup(services, since="2021")
    assert "### June 2021" in paged and "Page 1 of 3. Ask for page 2" in paged
    _, second = await lookup(services, since="2021", page=2)
    assert "### July 2021" in second
    _, nothing = await lookup(services, query="skiing")
    assert "No history summaries about 'skiing'" in nothing
    assert "Summaries cover 1 Jun – 31 Aug 2021 (3 periods)" in nothing
    _, bad = await lookup(services, since="summer")
    assert bad.startswith("Error: since must look like 2021")


async def test_skills_without_the_tool_dont_hear_about_it(services, archive):
    from naruto.agent.context import ContextBuilder
    from naruto.db.messages import LIVE, NewMessage

    trigger = services.messages.insert_live(NewMessage(
        chat_id=GROUP_ID, origin_chat_id=GROUP_ID, source=LIVE, message_id=99, sender_id=7,
        sender_name="Alice", date=ts(2026, 9, 1), text="/plan"))
    chat = services.chats.get(GROUP_ID)
    plan_prompt = ContextBuilder(services).build(chat, trigger, bot=services.status.bot,
                                                 skill="plan")
    assert "Summaries of earlier history" not in plan_prompt.messages[1]["content"]
    banter = ContextBuilder(services).build(chat, trigger, bot=services.status.bot)
    assert "Summaries of earlier history" in banter.messages[1]["content"]


# ------------------------------------------------------------ live archive

def live(services, message_id, when, text, sender=(7, "Alice")):
    from naruto.db.messages import LIVE, NewMessage

    return services.messages.insert_live(NewMessage(
        chat_id=GROUP_ID, origin_chat_id=GROUP_ID, source=LIVE, message_id=message_id,
        sender_id=sender[0], sender_name=sender[1], date=when, text=text))


def digest_read_everything(services):
    """The rolling digest has read every message, so its own grace for
    unread messages doesn't keep any: only the history hold is left."""
    newest = services.messages.get(services.db.scalar(
        "SELECT id FROM messages WHERE chat_id = ? ORDER BY date DESC, id DESC LIMIT 1",
        (GROUP_ID,)))
    services.digests.save(GROUP_ID, "- now", actor="t", last=newest)


@pytest.fixture
def recorded(services, monkeypatch):
    """A chat recorded live since 10 Aug 2026, with messages in August and
    September; "now" is 3 Sep 2026."""
    monkeypatch.setattr(history_module, "RETRY_DELAYS_SECONDS", (0, 0))
    services.chats.upsert_seen(GROUP_ID, title="BBQ crew")
    services.chats.set_status(GROUP_ID, "enabled")
    services.chats.set_recording_since(GROUP_ID, ts(2026, 8, 10))
    for day in range(10, 31, 5):
        live(services, day, ts(2026, 8, day, 20), f"August {day}: BBQ talk")
    live(services, 100, ts(2026, 9, 1, 9), "September already")
    return ts(2026, 9, 3)


async def test_a_finished_month_of_live_chat_is_summarized(services, recorded):
    now = recorded
    services.llm = SummaryLLM()
    archiver = history_module.LiveArchiver(services)
    assert await archiver.run_due(now=now) == 1
    (digest,) = active(services)
    assert (digest.source, digest.grouping) == ("live", "month")
    assert (digest.period_start, digest.period_end) == (ts(2026, 8, 10), ts(2026, 9, 1))
    assert digest.message_count == 5 and "10–31 Aug 2026" in digest.text
    assert "Live recording began on 10 Aug 2026" in digest.limitations[0]
    assert services.llm.calls[0]["info"].task == "live_archive"
    assert await archiver.run_due(now=now) == 0  # September isn't over


async def test_live_messages_are_kept_whether_summarized_or_not(services, recorded,
                                                                monkeypatch):
    from naruto import jobs

    much_later = ts(2027, 6, 1)  # months after August ended
    monkeypatch.setattr(jobs.time, "time", lambda: much_later)
    digest_read_everything(services)
    await jobs.run_maintenance(services)
    assert services.messages.count(GROUP_ID) == 6  # nothing expires
    assert services.history.live_periods(GROUP_ID) == []  # and nothing is marked missed

    services.llm = SummaryLLM()
    archiver = history_module.LiveArchiver(services)
    assert await archiver.run_due(now=much_later) == 1  # August, late but complete
    (digest,) = active(services)
    assert digest.message_count == 5 and not any("expired" in n for n in digest.limitations)
    await jobs.run_maintenance(services)
    assert services.messages.count(GROUP_ID) == 6  # summarized messages stay too


async def test_a_failing_month_is_marked_failed_and_later_months_go_ahead(services, recorded):
    now = ts(2026, 10, 3)  # August and September are both over
    services.llm = SummaryLLM(fail=lambda call: "Aug" in call["user"])
    archiver = history_module.LiveArchiver(services)
    assert await archiver.run_due(now=now) == 0
    (august,) = services.history.live_periods(GROUP_ID)
    assert august.status == "failed" and "kept failing" in august.error
    assert august.attempts == 3 and len(services.llm.calls) == 3
    assert await archiver.run_due(now=now + 60) == 0  # backing off between failures
    assert await archiver.run_due(now=now + 16 * 60) == 1  # September, not August again
    assert [d.period_start for d in active(services)] == [ts(2026, 9, 1)]
    assert len([c for c in services.llm.calls if "Aug" in c["user"]]) == 3

    # Only the owner's Retry tries August again, with three new attempts.
    restarted = history_module.LiveArchiver(services)
    assert await restarted.run_due(now=now + 60 * 60) == 0
    assert restarted.retry(august.id) and not restarted.retry(august.id)
    services.llm = SummaryLLM()
    assert await restarted.run_due(now=now + 61 * 60) == 1
    assert len(active(services)) == 2


async def test_failed_attempts_count_across_a_restart(services, recorded):
    services.llm = SummaryLLM(fail=lambda call: True)
    archiver = history_module.LiveArchiver(services)
    (start, end) = archiver.due_month(services.chats.get(GROUP_ID), recorded)
    period = services.history.add_period(
        chat_id=GROUP_ID, source="live", import_id=None, grouping=MONTH, timezone="UTC",
        period_start=start, period_end=end, message_count=5, fingerprint=None)
    services.history.update_period(period.id, attempts=2, error="server down")  # then a restart
    assert await history_module.LiveArchiver(services).run_due(now=recorded) == 0
    assert len(services.llm.calls) == 1  # one attempt was left
    assert services.history.get_period(period.id).status == "failed"


async def test_a_cut_short_or_empty_answer_is_not_a_summary(services, recorded):
    answers = iter([ChatResult(text="- August: the BBQ was", reasoning=None, model="m",
                               latency_ms=1, usage=None, finish_reason="length"),
                    ChatResult(text="   ", reasoning=None, model="m", latency_ms=1,
                               usage=None, finish_reason="stop"),
                    ChatResult(text="- August: BBQ at Alice's", reasoning=None, model="m",
                               latency_ms=1, usage=None, finish_reason="stop")])

    class Answers(SummaryLLM):
        async def chat(self, messages, **kwargs):
            self.calls.append({"user": messages[1]["content"]})
            return next(answers)

    services.llm = Answers()
    assert await history_module.LiveArchiver(services).run_due(now=recorded) == 1
    (digest,) = active(services)
    assert digest.text == "- August: BBQ at Alice's" and len(services.llm.calls) == 3


async def test_a_month_starts_over_when_read_messages_change(services, recorded):
    stop = {"after": 1}
    archiver = history_module.LiveArchiver(services)
    services.settings.set("history.chunk_tokens", 2000, actor="t")

    class StopAfterOnePart(SummaryLLM):
        async def chat(self, messages, **kwargs):
            result = await super().chat(messages, **kwargs)
            stop["after"] -= 1
            if stop["after"] == 0:
                archiver.stop()  # shutting down after this part
            return result

    services.llm = StopAfterOnePart()
    monkey_budget = history_module.HistoryWriter.message_budget
    history_module.HistoryWriter.message_budget = lambda self, chat, period, opts: 30
    try:
        assert await archiver.run_due(now=recorded) == 0
        (period,) = services.history.live_periods(GROUP_ID)
        assert period.status == "waiting" and period.consumed == 1 and period.partial
        # The message already read is edited (same number of messages).
        services.messages.apply_edit(GROUP_ID, 10, text="August 10: no BBQ after all",
                                     edit_date=ts(2026, 9, 2))
        services.llm = SummaryLLM()
        assert await history_module.LiveArchiver(services).run_due(now=recorded) == 1
    finally:
        history_module.HistoryWriter.message_budget = monkey_budget
    first = services.llm.calls[0]["user"]
    assert "## Summary so far" not in first and "no BBQ after all" in first
    assert "part 1 of" in first


async def test_deleting_read_messages_restarts_the_month_and_says_so(services, recorded,
                                                                     monkeypatch):
    archiver = history_module.LiveArchiver(services)
    monkeypatch.setattr(history_module.HistoryWriter, "message_budget",
                        lambda self, chat, period, opts: 30)  # one message per part

    class StopAfterOnePart(SummaryLLM):
        async def chat(self, messages, **kwargs):
            archiver.stop()
            return await super().chat(messages, **kwargs)

    services.llm = StopAfterOnePart()
    assert await archiver.run_due(now=recorded) == 0
    (period,) = services.history.live_periods(GROUP_ID)
    assert period.consumed == 1
    services.messages.delete_for_chat(GROUP_ID, before=ts(2026, 8, 12), source="live")
    services.llm = SummaryLLM()
    assert await history_module.LiveArchiver(services).run_due(now=recorded) == 1
    assert "## Summary so far" not in services.llm.calls[0]["user"]
    (digest,) = active(services)
    assert digest.message_count == 4
    assert "1 of this month's messages were deleted before it was summarized." in \
        digest.limitations


async def test_turning_monthly_summaries_off_stops_before_publishing(services, recorded):
    class TurnedOffMeanwhile(SummaryLLM):
        async def chat(self, messages, **kwargs):
            result = await super().chat(messages, **kwargs)
            services.settings.set_for_chat(GROUP_ID, "history.live_archive", False, actor="o")
            return result

    services.llm = TurnedOffMeanwhile()
    assert await history_module.LiveArchiver(services).run_due(now=recorded) == 0
    assert active(services) == []
    (period,) = services.history.live_periods(GROUP_ID)
    assert period.status == "waiting"  # its work is kept for when it's turned on again


async def test_a_month_keeps_its_dates_when_the_time_zone_changes(services, recorded):
    archiver = history_module.LiveArchiver(services)
    services.llm = SummaryLLM(fail=lambda call: True)
    services.history.add_period(
        chat_id=GROUP_ID, source="live", import_id=None, grouping=MONTH, timezone="UTC",
        period_start=ts(2026, 8, 10), period_end=ts(2026, 9, 1), message_count=5,
        fingerprint=None)
    services.settings.set("general.timezone", "Asia/Singapore", actor="t")
    assert archiver.due_month(services.chats.get(GROUP_ID), recorded) == \
        (ts(2026, 8, 10), ts(2026, 9, 1))


def test_completing_a_period_twice_keeps_one_summary(services):
    period = services.history.add_period(
        chat_id=GROUP_ID, source="export", import_id=None, grouping=MONTH, timezone="UTC",
        period_start=ts(2024, 1, 1), period_end=ts(2024, 2, 1), message_count=3,
        fingerprint="f")
    common = dict(status="active", first_message_at=None, last_message_at=None,
                  message_count=3, limitations=[], actor="t")
    first = services.history.complete_period(period, text="- January", **common)
    again = services.history.complete_period(period, text="- January, retried", **common)
    assert again.id == first.id and again.text == "- January"
    assert services.history.get_period(period.id).digest_id == first.id
    with pytest.raises(Exception, match="UNIQUE"):
        services.history.add(chat_id=GROUP_ID, status="staged", source="export",
                             grouping=MONTH, timezone="UTC", period_start=0, period_end=1,
                             first_message_at=None, last_message_at=None, message_count=0,
                             import_id=None, period_id=period.id, fingerprint="", text="x",
                             limitations=[], actor="t")


def test_a_crash_while_completing_leaves_no_summary_behind(services, monkeypatch):
    period = services.history.add_period(
        chat_id=GROUP_ID, source="export", import_id=None, grouping=MONTH, timezone="UTC",
        period_start=ts(2024, 1, 1), period_end=ts(2024, 2, 1), message_count=3,
        fingerprint="f")
    real = services.history.update_period

    def crash(period_id, **fields):
        if fields.get("status") == "done":
            raise RuntimeError("power cut")
        return real(period_id, **fields)

    monkeypatch.setattr(services.history, "update_period", crash)
    with pytest.raises(RuntimeError):
        services.history.complete_period(period, status="active", first_message_at=None,
                                         last_message_at=None, message_count=3, text="- Jan",
                                         limitations=[], actor="t")
    assert services.history.for_chat(GROUP_ID, status=None)[1] == 0
    assert services.history.get_period(period.id).status == "waiting"


async def test_a_request_fits_its_tokens(services, recorded):
    from naruto.agent.text import estimate_text_tokens

    services.settings.set("history.chunk_tokens", 2500, actor="t")
    for i in range(60):
        live(services, 200 + i, ts(2026, 8, 12, 10, i), f"message {i} " + "plans " * 40)
    services.llm = SummaryLLM()
    assert await history_module.LiveArchiver(services).run_due(now=recorded) == 1
    sizes = [sum(estimate_text_tokens(m["content"]) for m in c["messages"])
             for c in services.llm.calls]
    assert len(sizes) > 1 and max(sizes) <= 2500

    services.settings.set("history.chunk_tokens", 2000, actor="t")
    services.settings.set("history.digest_max_chars", 6000, actor="t")
    writer = history_module.HistoryWriter(services)
    period = services.history.live_periods(GROUP_ID)[0]
    opts = history_module.WriteOptions(chunk_tokens=2000, max_chars=1500, timezone=UTC,
                                       tz_name="UTC", actor="t")
    with pytest.raises(history_module.HistoryFailed, match="no room for messages"):
        writer.message_budget(services.chats.get(GROUP_ID), period, opts)


async def test_live_summaries_can_be_turned_off_per_chat(services, recorded):
    services.settings.set_for_chat(GROUP_ID, "history.live_archive", False, actor="t")
    services.llm = SummaryLLM()
    assert await history_module.LiveArchiver(services).run_due(now=recorded) == 0
    assert services.llm.calls == []


async def test_an_import_and_live_summaries_meet_at_the_recording_start(services, recorded,
                                                                         tmp_path):
    services.llm = SummaryLLM()
    rows = [(i, ts(2026, 7, 1 + i, 12), 7, "Alice", f"July {i}") for i in range(1, 20)]
    rows += [(100 + i, ts(2026, 8, 1 + i, 12), 7, "Alice", f"August {i}") for i in range(20)]
    importer = ImportService(services, tmp_path / "imports")
    record = await upload(importer, rows)
    record = await run(importer, record, summaries_only(importer, record))
    await history_module.LiveArchiver(services).run_due(now=recorded)
    spans = [(d.source, d.period_start, d.period_end) for d in active(services)]
    assert spans == [("export", ts(2026, 7, 1), ts(2026, 8, 1)),
                     ("export", ts(2026, 8, 1), ts(2026, 8, 10)),
                     ("live", ts(2026, 8, 10), ts(2026, 9, 1))]
