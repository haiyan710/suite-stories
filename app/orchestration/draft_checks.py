"""Deterministic draft checks: what Python can verify or fix without a model.

The draft critic missed wrong weekdays and copied a stored calendar mistake. These
checks run on every draft before and after the critic:

- `apply_calendar` fixes what is mechanical: the date in each "## Day N" heading
  and the weekday inside each day's [TK: ...] notes.
- `check_draft` lists what needs a rewrite as revision instructions: a written
  date whose weekday is wrong or that falls outside the trip, banned phrases
  (house style), and car times too long to be a ride within a city.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date

from .calendar import MONTHS, WEEKDAYS, TripCalendar, _MONTH

_WEEKDAY_NAMES = "|".join(WEEKDAYS)
_DAY_HEADING = re.compile(r"^##\s*Day\s+(\d+)\b(.*)$", re.M)
# "Saturday 6 February", "Saturday, 6th February", "Saturday February 6"
_WEEKDAY_DATE = re.compile(
    rf"\b({_WEEKDAY_NAMES}),?\s+(?:(\d{{1,2}})(?:st|nd|rd|th)?\s+{_MONTH}|{_MONTH}\s+(\d{{1,2}})(?:st|nd|rd|th)?)",
    re.I,
)
_DATE_IN_HEADING = re.compile(
    rf"\s*·?\s*(?:(?:{_WEEKDAY_NAMES}),?\s+)?(?:\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH}|{_MONTH}\s+\d{{1,2}})(?:,?\s*\d{{4}})?\s*",
    re.I,
)
_CAR_TIME = re.compile(r"about\s+(\d{2,3})\s*(?:–|-|to)\s*(\d{2,3})\s*min(?:ute)?s?", re.I)
MAX_CAR_MINUTES = 150


def _sections(body: str) -> list[tuple[int | None, int, int]]:
    """(day number or None, start, end) spans of the body, split at '## ' headings."""
    marks = [(m.start(), m) for m in re.finditer(r"^##\s.*$", body, re.M)]
    spans: list[tuple[int | None, int, int]] = []
    prev_start, prev_day = 0, None
    for pos, m in marks:
        spans.append((prev_day, prev_start, pos))
        day = _DAY_HEADING.match(m.group(0))
        prev_day, prev_start = (int(day.group(1)) if day else None), pos
    spans.append((prev_day, prev_start, len(body)))
    return spans


def apply_calendar(body: str, calendar: TripCalendar | None) -> str:
    """Put each day's real date in its heading and its weekday in that day's [TK] notes."""
    if calendar is None or not body:
        return body

    def heading(m: re.Match) -> str:
        day = calendar.day(int(m.group(1)))
        if day is None:
            return m.group(0)
        rest = m.group(2)
        # Drop a date (and its separator) the writer put in, keep the title.
        rest = _DATE_IN_HEADING.sub(" · ", rest, count=1) if _DATE_IN_HEADING.search(rest) else rest
        rest = re.sub(r"^[\s·:–—-]+|[\s·:–—-]+$", "", rest)
        return f"## Day {m.group(1)} · {day.label}" + (f" · {rest}" if rest else "")

    body = _DAY_HEADING.sub(heading, body)
    out: list[str] = []
    for day_number, start, end in _sections(body):
        text = body[start:end]
        day = calendar.day(day_number) if day_number else None
        if day is not None:
            text = re.sub(
                r"\[TK[^\]]*\]",
                lambda tk: re.sub(rf"\b(?:{_WEEKDAY_NAMES})\b", day.weekday, tk.group(0)),
                text,
            )
        out.append(text)
    return "".join(out)


def _written_dates(text: str, year: int) -> list[tuple[str, str, date | None]]:
    """(matched text, weekday written, the date it names) for each 'Weekday D Month'."""
    found = []
    for m in _WEEKDAY_DATE.finditer(text):
        weekday = m.group(1).capitalize()
        day_num = m.group(2) or m.group(5)
        month_name = (m.group(3) or m.group(4) or "").lower().rstrip(".")
        try:
            named = date(year, MONTHS[month_name], int(day_num))
        except (KeyError, ValueError, TypeError):
            named = None
        found.append((m.group(0).rstrip("."), weekday, named))
    return found


def check_draft(
    body: str, *, headline: str = "", standfirst: str = "",
    calendar: TripCalendar | None = None, banned_phrases: Sequence[str] = (),
) -> list[str]:
    """Revision instructions for problems Python can see in a draft."""
    issues: list[str] = []
    text = "\n".join((headline, standfirst, body))
    if calendar is not None:
        trip = {d.date for d in calendar.days}
        first, last = calendar.days[0], calendar.days[-1]
        year = date.fromisoformat(calendar.start).year
        for written, weekday, named in _written_dates(text, year):
            if named is None:
                continue
            real = WEEKDAYS[named.weekday()]
            if real != weekday:
                issues.append(
                    f'"{written}" has the wrong weekday: {named.day} {named.strftime("%B")} {year} is a {real}. '
                    "Use the CALENDAR's dates and weekdays."
                )
            elif named.isoformat() not in trip and abs((named - date.fromisoformat(calendar.start)).days) <= 14:
                issues.append(
                    f'"{written}" is outside the trip, which runs from {first.label} to {last.label}. '
                    "Use the CALENDAR's dates and weekdays."
                )
    lowered = text.lower()
    for phrase in banned_phrases:
        phrase = phrase.strip()
        if phrase and re.search(rf"(?<!\w){re.escape(phrase.lower())}(?!\w)", lowered):
            issues.append(f'House style: remove "{phrase}" and say it directly, without naming the source.')
    for m in _CAR_TIME.finditer(body):
        low, high = int(m.group(1)), int(m.group(2))
        # The sentence it sits in: a train or day-trip time is not a car ride.
        start = max(body.rfind(".", 0, m.start()), body.rfind("\n", 0, m.start())) + 1
        stop = min([i for i in (body.find(".", m.end()), body.find("\n", m.end())) if i >= 0] or [len(body)])
        context = body[start:stop].lower()
        if high > MAX_CAR_MINUTES and not re.search(r"train|rail|flight|ferry|boat|day trip", context):
            issues.append(
                f'"{m.group(0)}" is too long for a car ride within the city; recheck it '
                "(airport transfers are usually well under two hours) or mark it [TK]."
            )
    return issues
