"""Calendar periods in the configured time zone: months, weeks starting on
Monday, or one range, and how to name them ("March 2021", "1–17 Jun 2024").
Used by history digests, the import plan and the prompt."""

from datetime import date, datetime, time as dtime, timedelta, tzinfo

MONTH = "month"
WEEK = "week"
RANGE = "range"


def local_date(ts: int, tz: tzinfo) -> date:
    return datetime.fromtimestamp(ts, tz).date()


def day_start(day: date, tz: tzinfo) -> int:
    return int(datetime.combine(day, dtime.min, tz).timestamp())


def _next_month(day: date) -> date:
    return date(day.year + (day.month == 12), day.month % 12 + 1, 1)


def plan_periods(start: int, end: int, grouping: str, tz: tzinfo) -> list[tuple[int, int]]:
    """[start, end) cut into calendar months, weeks starting on Monday, or
    left as one range. The first and last periods are clipped to it."""
    if end <= start:
        return []
    if grouping == RANGE:
        return [(start, end)]
    first = local_date(start, tz)
    cursor = first.replace(day=1) if grouping == MONTH else first - timedelta(days=first.weekday())
    periods = []
    while True:
        following = _next_month(cursor) if grouping == MONTH else cursor + timedelta(days=7)
        period_start, period_end = day_start(cursor, tz), day_start(following, tz)
        if period_start >= end:
            return periods
        periods.append((max(period_start, start), min(period_end, end)))
        cursor = following


def day_text(day: date, *, year: bool = True) -> str:
    """"14 Mar 2021"."""
    return f"{day.day} {day.strftime('%b %Y' if year else '%b')}"


def calendar_period(ts: int, grouping: str, tz: tzinfo) -> tuple[int, int]:
    """The whole month or week (Monday to Sunday) around ``ts``."""
    day = local_date(ts, tz)
    if grouping == MONTH:
        first = day.replace(day=1)
        return day_start(first, tz), day_start(_next_month(first), tz)
    first = day - timedelta(days=day.weekday())
    return day_start(first, tz), day_start(first + timedelta(days=7), tz)


def describe_span(start: int, end: int, tz: tzinfo) -> str:
    """"1–31 Mar 2021" style, with the end day inclusive."""
    first, last = local_date(start, tz), local_date(end - 1, tz)
    if first == last:
        return day_text(first)
    if (first.year, first.month) == (last.year, last.month):
        return f"{first.day}–{day_text(last)}"
    if first.year == last.year:
        return f"{day_text(first, year=False)} – {day_text(last)}"
    return f"{day_text(first)} – {day_text(last)}"


def period_label(start: int, end: int, grouping: str, tz: tzinfo) -> str:
    first, last = local_date(start, tz), local_date(end - 1, tz)
    whole_month = first.day == 1 and _next_month(first) - timedelta(days=1) == last
    if grouping == MONTH and whole_month:
        return first.strftime("%B %Y")
    if grouping == WEEK and last - first == timedelta(days=6):
        return f"Week of {day_text(first)}"
    return describe_span(start, end, tz)
