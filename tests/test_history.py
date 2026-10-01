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
    services.settings.set("retention.imported_messages_days", 30, actor="t")
    services.settings.set("history.chunk_tokens", 1000, actor="t")
    return ImportService(services, tmp_path / "imports")


async def upload(importer, rows=None):
    return await importer.create_from_upload(io.BytesIO(export(rows or half_year())),
                                             "result.json")


def summaries_only(importer, record, **changes):
    options = importer.default_options(record)
    options.raw = options.distill = False
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
    assert any("Tick “Replace them”" in e for e in plan.errors)
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


def test_history_moves_with_a_group_upgrade_and_outlives_retention(services):
    from naruto import jobs

    services.chats.upsert_seen(GROUP_ID)
    digest = services.history.add(
        chat_id=GROUP_ID, status="active", source="export", grouping=MONTH, timezone="UTC",
        period_start=ts(2020, 1, 1), period_end=ts(2020, 2, 1), first_message_at=None,
        last_message_at=None, message_count=3, import_id=None, period_id=None,
        fingerprint="x", text="- January 2020", limitations=[], actor="t")
    jobs.cleanup_imported_messages(services)
    jobs.cleanup_live_messages(services)
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


async def test_live_messages_wait_for_their_summary(services, recorded, monkeypatch):
    from naruto import jobs

    now = recorded
    monkeypatch.setattr(jobs.time, "time", lambda: now)
    services.settings.set("retention.live_messages_days", 1, actor="t")
    digest_read_everything(services)
    jobs.cleanup_live_messages(services)
    assert services.messages.count(GROUP_ID) == 6  # August is held for its summary

    services.llm = SummaryLLM()
    await history_module.LiveArchiver(services).run_due(now=now)
    jobs.cleanup_live_messages(services)
    # August is summarized; September is held until it is over.
    assert services.messages.count(GROUP_ID) == 1


async def test_a_month_not_summarized_within_the_hold_is_missed(services, recorded,
                                                                monkeypatch):
    from naruto import jobs

    later = ts(2026, 9, 9)  # August ended more than 7 days ago
    monkeypatch.setattr(jobs.time, "time", lambda: later)
    monkeypatch.setattr(history_module.time, "time", lambda: later)
    services.settings.set("retention.live_messages_days", 1, actor="t")
    digest_read_everything(services)
    assert jobs.mark_missed_live_months(services) == "1 live months missed their history summary"
    jobs.cleanup_live_messages(services)
    assert services.messages.count(GROUP_ID) == 1  # August's messages are gone now
    (period,) = services.history.live_periods(GROUP_ID)
    assert period.status == "failed" and period.error.startswith("Missed")
    services.llm = SummaryLLM()
    assert await history_module.LiveArchiver(services).run_due(now=later) == 0


async def test_a_failing_live_summary_backs_off_and_retries(services, recorded):
    now = recorded
    services.llm = SummaryLLM(fail=lambda call: True)
    archiver = history_module.LiveArchiver(services)
    assert await archiver.run_due(now=now) == 0
    (period,) = services.history.live_periods(GROUP_ID)
    assert period.status == "waiting" and "kept failing" in period.error
    services.llm = SummaryLLM()
    assert await archiver.run_due(now=now + 60) == 0  # backing off
    assert await archiver.run_due(now=now + 16 * 60) == 1
    assert len(active(services)) == 1


async def test_live_summaries_can_be_turned_off_per_chat(services, recorded, monkeypatch):
    from naruto import jobs

    services.settings.set_for_chat(GROUP_ID, "history.live_archive", False, actor="t")
    services.llm = SummaryLLM()
    assert await history_module.LiveArchiver(services).run_due(now=recorded) == 0
    monkeypatch.setattr(jobs.time, "time", lambda: recorded)
    services.settings.set("retention.live_messages_days", 1, actor="t")
    digest_read_everything(services)
    jobs.cleanup_live_messages(services)
    assert services.messages.count(GROUP_ID) == 0  # nothing held: all past retention


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
