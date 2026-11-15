"""Pure scheduling logic with no I/O - easily testable in isolation.

Slot-based repetition model:
  A schedule means "do this `count` times per `unit`". Each unit period has
  exactly `count` slot days, spread evenly and anchored to the schedule's
  start date. On a slot day the item is "due" (unless the period target was
  already reached).

When explicit `slot_days` are provided (JSON-encoded list of weekday 0-6 or
month day 1-31) the spread logic is bypassed and those exact days are used.
"""

from __future__ import annotations

import calendar
import json
from datetime import date, timedelta

REPEAT_UNITS = ("day", "week", "month")
FREQUENCY_UNITS = ("day", "week", "month", "year")

_DEFAULT_SPAN = {
    "day": 1,
    "week": 7,
    "month": 0,  # resolved to days-in-month
    "year": 366,
}


def spread_indices(count: int, span: int) -> set[int]:
    """Distribute `count` slot indices evenly across a span of `span` units.

    Returns 0-based indices and always includes index 0 (the schedule anchor,
    i.e. its start date is always a slot day). Examples:
        spread_indices(2, 7)  -> {0, 4}   (e.g. Mon & Fri for a Monday anchor)
        spread_indices(3, 7)  -> {0, 2, 5}
        spread_indices(5, 30) -> {0, 6, 12, 18, 24}
    """
    if span <= 0:
        return set()
    if count >= span:
        return set(range(span))
    return {round(i * span / count) for i in range(count)}


def days_in_month(d: date) -> int:
    return calendar.monthrange(d.year, d.month)[1]


def start_of_week(d: date) -> date:
    """Monday-start ISO week."""
    return d - timedelta(days=d.weekday())


def add_duration(start: date, count: int, unit: str) -> date:
    """Add a calendar duration to a date (no I/O, pure math)."""
    if unit == "day":
        return start + timedelta(days=count)
    if unit == "week":
        return start + timedelta(weeks=count)
    if unit == "month":
        total = start.year * 12 + (start.month - 1) + count
        year, month = divmod(total, 12)
        month += 1
        day = min(start.day, days_in_month(date(year, month, 1)))
        return date(year, month, day)
    if unit == "year":
        total = start.year * 12 + (start.month - 1) + count * 12
        year, month = divmod(total, 12)
        month += 1
        day = min(start.day, days_in_month(date(year, month, 1)))
        return date(year, month, day)
    raise ValueError(f"unsupported duration unit: {unit}")


def parse_slot_days(raw: str | None) -> list[int] | None:
    """Decode stored slot_days (JSON). Returns None when absent/invalid."""
    if not raw:
        return None
    try:
        days = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(days, list) or not days:
        return None
    parsed: list[int] = []
    for d in days:
        if isinstance(d, int):
            parsed.append(d)
        elif isinstance(d, str) and d.isdigit():
            parsed.append(int(d))
    return parsed or None


def matches_days(d: date, unit: str, slot_days: list[int] | None) -> bool:
    """Whether `d` is one of the explicit chosen days for a unit.

    Falls back to "any day" when no explicit days are set (legacy routines).
    """
    if slot_days is None:
        return True
    if unit == "week":
        return d.weekday() in slot_days
    if unit == "month":
        return d.day in slot_days
    return True


def period_bounds(d: date, unit: str) -> tuple[date, date]:
    """Return the [start, end] calendar period containing `d` for a unit."""
    if unit == "day":
        return d, d
    if unit == "week":
        start = start_of_week(d)
        return start, start + timedelta(days=6)
    if unit == "month":
        return d.replace(day=1), d.replace(day=days_in_month(d))
    if unit == "year":
        return date(d.year, 1, 1), date(d.year, 12, 31)
    raise ValueError(f"unsupported period unit: {unit}")


def is_slot_day(
    d: date,
    count: int,
    unit: str,
    start_date: date,
    end_date: date | None = None,
    slot_days: list[int] | None = None,
) -> bool:
    """Whether `d` is a slot day for a schedule anchored at `start_date`."""
    if d < start_date:
        return False
    if end_date is not None and d > end_date:
        return False
    if unit == "day":
        return True
    if slot_days:
        if unit == "week":
            return d.weekday() in slot_days
        if unit == "month":
            return d.day in slot_days
    if unit == "week":
        offsets = spread_indices(count, 7)
        return (d.weekday() - start_date.weekday()) % 7 in offsets
    if unit == "month":
        span = days_in_month(d)
        anchor = (start_date.day - 1) % span
        offsets = spread_indices(count, span)
        return (d.day - 1 - anchor) % span in offsets
    if unit == "year":
        span = 366
        anchor = start_date.timetuple().tm_yday - 1
        offsets = spread_indices(count, span)
        return (d.timetuple().tm_yday - 1 - anchor) % span in offsets
    raise ValueError(f"unsupported schedule unit: {unit}")


def remaining_in_period(
    d: date,
    count: int,
    unit: str,
    start_date: date,
    end_date: date | None,
    completed_dates: list[str],
) -> int:
    """Slots still remaining in the period containing `d`."""
    if d < start_date:
        return 0
    if end_date is not None and d > end_date:
        return 0
    p_start, p_end = period_bounds(d, unit)
    done = sum(1 for iso in completed_dates if p_start.isoformat() <= iso <= p_end.isoformat())
    return max(count - done, 0)