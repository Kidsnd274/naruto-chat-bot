"""What an import will do, from the owner's choices on the preview page:
which messages to keep as raw chat, and which dates to summarize into history
digests (and how to group them). Imports never add memory notes or start the
rolling digest: those come from live chat only.

make_plan() works from the preview (sorted message dates and a per-day
histogram), so the estimate refreshes without reading the file again. The
same plan is checked again when the import starts, and its frozen form is
stored with the import so a resumed job does exactly the same.

Dates are local days in the configured time zone. A range includes its
whole end day; internally it is [start, end) in Unix time.
"""

import bisect
from dataclasses import dataclass, field
from datetime import date, timedelta
import math

from naruto.db.chats import Chat
from naruto.db.history import GROUPINGS, MONTH, RANGE, HistoryDigest
from naruto.memory.history import fingerprint, request_overhead, settings_hash
from naruto.periods import (
    calendar_period,
    day_start,
    describe_span,
    local_date,
    period_label,
    plan_periods,
)
from naruto.services import Services

LONG_RANGE_DAYS = 92  # one summary for longer than this loses a lot of detail


@dataclass
class ImportOptions:
    raw: bool = True
    raw_from: date | None = None
    raw_to: date | None = None
    archive: bool = True
    archive_from: date | None = None
    archive_to: date | None = None
    grouping: str = MONTH
    regenerate: bool = False  # rebuild summaries even when nothing changed
    replace: bool = False  # replace existing summaries these dates overlap
    replace_edited: bool = False  # ...including ones the owner edited

    @classmethod
    def defaults(cls, preview: dict, services: Services) -> "ImportOptions":
        """Raw: the whole export (however old). Summaries: the whole export,
        monthly, from the start of its first month (so that month is
        summarized as a whole, noting where the export begins)."""
        tz = services.timezone()
        first = local_date(preview["first_date"], tz)
        last = local_date(preview["last_date"], tz)
        return cls(raw=True, raw_from=first, raw_to=last, archive=True,
                   archive_from=first.replace(day=1), archive_to=last, grouping=MONTH)

    @classmethod
    def from_form(cls, form, defaults: "ImportOptions") -> "ImportOptions":
        """Form fields of the preview page. Without ``options`` in the form
        (the first page load), the defaults."""
        if not form.get("options"):
            return defaults

        def day(name: str) -> date | None:
            raw = str(form.get(name) or "").strip()
            try:
                return date.fromisoformat(raw) if raw else None
            except ValueError:
                return None

        grouping = str(form.get("grouping") or MONTH)
        return cls(
            raw=bool(form.get("raw")), raw_from=day("raw_from"), raw_to=day("raw_to"),
            archive=bool(form.get("archive")), archive_from=day("archive_from"),
            archive_to=day("archive_to"),
            grouping=grouping if grouping in GROUPINGS else MONTH,
            regenerate=bool(form.get("regenerate")),
            replace=bool(form.get("replace")), replace_edited=bool(form.get("replace_edited")),
        )


@dataclass
class PeriodPlan:
    start: int
    end: int
    label: str
    count: int
    tokens: int
    requests: int
    fingerprint: str | None  # None when it can't be predicted from the preview
    reuse: HistoryDigest | None = None  # an existing digest with the same content
    replaces: list[HistoryDigest] = field(default_factory=list)
    partial: bool = False  # the export doesn't cover the whole period


@dataclass
class ImportPlan:
    options: ImportOptions
    tz_name: str
    raw_boundary: int | None  # raw messages stop here (recording began, first live message)
    boundary: int | None  # summaries stop here (recording began)
    # Raw messages.
    raw_span: tuple[int, int] | None = None  # the chosen dates
    raw_start: int | None = None  # the effective range kept
    raw_end: int | None = None
    raw_selected: int = 0
    raw_eligible: int = 0
    raw_live: int = 0  # excluded by the live-recording boundary
    raw_replaced: int = 0
    # History summaries.
    archive_start: int | None = None
    archive_end: int | None = None
    archive_selected: int = 0
    archive_count: int = 0
    archive_after_boundary: int = 0
    archive_not_raw: int = 0
    periods: list[PeriodPlan] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def active_periods(self) -> list[PeriodPlan]:
        return [p for p in self.periods if p.count]

    @property
    def work_periods(self) -> list[PeriodPlan]:
        return [p for p in self.active_periods if p.reuse is None]

    @property
    def archive_requests(self) -> int:
        return sum(p.requests for p in self.work_periods)

    @property
    def archive_tokens(self) -> int:
        return sum(p.tokens for p in self.work_periods)

    @property
    def replaced_digests(self) -> list[HistoryDigest]:
        seen: dict[int, HistoryDigest] = {}
        for period in self.work_periods:
            for digest in period.replaces:
                seen[digest.id] = digest
        return sorted(seen.values(), key=lambda d: d.period_start)

    @property
    def edited_replaced(self) -> list[HistoryDigest]:
        return [d for d in self.replaced_digests if d.edited]

    def frozen(self, services: Services) -> dict:
        """What the import job needs, stored with the import (it doesn't
        change if settings or the chat change while the job runs)."""
        settings = services.settings
        options = self.options
        return {
            "tz": self.tz_name,
            "raw_boundary": self.raw_boundary,
            "boundary": self.boundary,
            "raw": {"enabled": options.raw and self.raw_eligible > 0,
                    "from": _iso(options.raw_from), "to": _iso(options.raw_to),
                    "selected": list(self.raw_span) if self.raw_span else None,
                    "start": self.raw_start, "end": self.raw_end},
            "archive": {"enabled": options.archive and self.archive_count > 0,
                        "from": _iso(options.archive_from), "to": _iso(options.archive_to),
                        "grouping": options.grouping, "start": self.archive_start,
                        "end": self.archive_end, "regenerate": options.regenerate,
                        "replace": options.replace, "replace_edited": options.replace_edited},
            "chunk_tokens": settings["history.chunk_tokens"],
            "max_chars": settings["context.max_message_chars"],
            "settings_hash": settings_hash(settings),
        }


def _iso(day: date | None) -> str | None:
    return day.isoformat() if day else None


class Dates:
    """The export's message times, sorted, for exact counts."""

    def __init__(self, dates: list[int]):
        self.dates = dates

    def count(self, start: int | None, end: int | None) -> int:
        if start is not None and end is not None and end <= start:
            return 0
        low = 0 if start is None else bisect.bisect_left(self.dates, start)
        high = len(self.dates) if end is None else bisect.bisect_left(self.dates, end)
        return max(0, high - low)


def _span_days(first: date | None, last: date | None, tz) -> tuple[int, int] | None:
    if first is None or last is None:
        return None
    return day_start(first, tz), day_start(last + timedelta(days=1), tz)


def make_plan(preview: dict, dates: list[int], options: ImportOptions, chat: Chat | None,
              services: Services, *, chat_id: int | None) -> ImportPlan:
    settings = services.settings
    tz = services.timezone()
    tz_name = settings["general.timezone"] or "server"
    first_live = services.messages.first_live_date(chat_id) if chat_id is not None else None
    recording = chat.recording_since if chat is not None else None
    known = [ts for ts in (recording, first_live) if ts is not None]
    raw_boundary = min(known) if known else None
    boundary = recording if recording is not None else first_live
    plan = ImportPlan(options=options, tz_name=tz_name, raw_boundary=raw_boundary,
                      boundary=boundary)
    counts = Dates(dates)
    days = preview.get("days") or []

    if not options.raw and not options.archive:
        plan.errors.append("Choose at least one: import messages, or make history summaries.")

    # ------------------------------------------------------------- raw
    raw_span = None
    if options.raw:
        raw_span = _span_days(options.raw_from, options.raw_to, tz)
        if raw_span is None:
            plan.errors.append("Messages to import: choose both dates.")
        elif raw_span[1] <= raw_span[0]:
            plan.errors.append("Messages to import: the start date is after the end date.")
            raw_span = None
    if raw_span is not None:
        start, end = plan.raw_span = raw_span
        plan.raw_selected = counts.count(start, end)
        if raw_boundary is not None:
            plan.raw_live = counts.count(max(start, raw_boundary), end)
        keep_end = min(end, raw_boundary) if raw_boundary is not None else end
        if keep_end > start:
            plan.raw_start, plan.raw_end = start, keep_end
            plan.raw_eligible = counts.count(start, keep_end)
            if chat_id is not None:
                plan.raw_replaced = int(services.db.scalar(
                    "SELECT COUNT(*) FROM messages WHERE chat_id = ? AND source = 'import' "
                    "AND date >= ? AND date < ?", (chat_id, start, keep_end)) or 0)
        if not plan.raw_eligible:
            plan.errors.append(
                "Messages to import: none of the chosen dates can be kept"
                + (f" ({plan.raw_live} are excluded by the live-recording boundary)."
                   if plan.raw_live else " (there are no messages in them)."))

    # --------------------------------------------------------- summaries
    archive_span = None
    if options.archive:
        archive_span = _span_days(options.archive_from, options.archive_to, tz)
        if archive_span is None:
            plan.errors.append("History summaries: choose both dates.")
        elif archive_span[1] <= archive_span[0]:
            plan.errors.append("History summaries: the start date is after the end date.")
            archive_span = None
    if archive_span is not None:
        start, end = archive_span
        effective_end = min(end, boundary) if boundary is not None else end
        plan.archive_selected = counts.count(start, end)
        if boundary is not None:
            plan.archive_after_boundary = counts.count(max(start, boundary), end)
        if effective_end > start:
            plan.archive_start, plan.archive_end = start, effective_end
            plan.archive_count = counts.count(start, effective_end)
            kept_raw = 0
            if plan.raw_start is not None:
                kept_raw = counts.count(max(start, plan.raw_start),
                                        min(effective_end, plan.raw_end))
            plan.archive_not_raw = plan.archive_count - kept_raw
            _plan_periods(plan, counts, days, preview, chat_id, services, tz, tz_name)
        if not plan.archive_count:
            if plan.archive_after_boundary and boundary is not None:
                plan.errors.append(
                    "History summaries: all the chosen messages are from after live recording "
                    f"began ({describe_span(boundary, boundary + 1, tz)}); live chat gets "
                    "its own monthly summaries.")
            else:
                plan.errors.append("History summaries: there are no messages in these dates.")

    return plan


def _plan_periods(plan: ImportPlan, counts: Dates, days: list, preview: dict,
                  chat_id: int | None, services: Services, tz, tz_name: str) -> None:
    options = plan.options
    settings = services.settings
    key = settings_hash(settings)
    chunk_tokens = max(settings["history.chunk_tokens"] - request_overhead(settings), 500)
    export_first, export_last = preview.get("first_date"), preview.get("last_date")
    for start, end in plan_periods(plan.archive_start, plan.archive_end, options.grouping, tz):
        count = counts.count(start, end)
        in_period = [day for day in days if start <= day[0] < end]
        tokens = sum(day[2] for day in in_period)
        whole = calendar_period(start, options.grouping, tz) if options.grouping != RANGE \
            else (start, end)
        clipped_at_boundary = plan.boundary is not None and end == plan.boundary
        day_aligned = start == day_start(local_date(start, tz), tz) and \
            end == day_start(local_date(end, tz), tz)
        prediction = None
        if count and day_aligned and not clipped_at_boundary:
            prediction = fingerprint(options.grouping, tz_name, start, end, key,
                                     [(day[0], day[3]) for day in in_period])
        period = PeriodPlan(
            start=start, end=end, label=period_label(start, end, options.grouping, tz),
            count=count, tokens=tokens,
            requests=max(1, math.ceil(tokens / chunk_tokens)) if count else 0,
            fingerprint=prediction,
            partial=bool(count) and (start > whole[0] or end < whole[1] or (
                export_first is not None and export_first > start + 86400) or (
                export_last is not None and export_last < end - 86400)))
        if count and chat_id is not None:
            existing = services.history.overlapping(chat_id, start, end)
            same = [d for d in existing if (d.period_start, d.period_end) == (start, end)
                    and prediction is not None and d.fingerprint == prediction]
            if same and not options.regenerate:
                period.reuse = same[0]
            else:
                period.replaces = existing
        plan.periods.append(period)

    replaced = plan.replaced_digests
    for digest in replaced:
        if digest.period_start < plan.archive_start or digest.period_end > plan.archive_end:
            span = describe_span(digest.period_start, digest.period_end, tz)
            plan.errors.append(
                f"History summaries: an existing summary covers {span}, partly outside these "
                "dates. Include all of it, or leave it out, so it isn't replaced by part of "
                "itself.")
    if replaced and not options.replace:
        plan.errors.append(
            f"History summaries: {len(replaced)} existing "
            f"summar{'ies need' if len(replaced) != 1 else 'y needs'} replacement. "
            "Select “Replace existing summaries” to continue, or change the dates.")
    edited = plan.edited_replaced
    if edited and options.replace and not options.replace_edited:
        plan.errors.append(
            f"History summaries: {len(edited)} of the summaries to replace "
            f"{'were' if len(edited) != 1 else 'was'} edited by you. "
            "Allow replacing summaries you edited to continue.")
    if options.grouping == RANGE and plan.archive_end - plan.archive_start > \
            LONG_RANGE_DAYS * 86400:
        months = round((plan.archive_end - plan.archive_start) / (30.4 * 86400))
        plan.warnings.append(f"One summary for about {months} months loses a lot of detail; "
                             "monthly summaries keep more.")
    partial = sum(1 for p in plan.active_periods if p.partial)
    if partial:
        plan.warnings.append(f"{partial} period{'s are' if partial != 1 else ' is'} only partly "
                             "covered by this export; their summaries say so.")
