"""Trip calendar: the dates and weekdays of an itinerary, computed in Python.

Models get dates wrong (a live run put a holiday on the wrong weekday) and then
every later step copied the stored mistake. The calendar is
worked out here from the request, stored with the concept, and handed to every
specialist as the one source of truth for "Day N" dates, weekdays and holidays.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

MONTHS = {
    name: number
    for number, names in enumerate((
        ("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"), ("may",),
        ("june", "jun"), ("july", "jul"), ("august", "aug"), ("september", "sep", "sept"),
        ("october", "oct"), ("november", "nov"), ("december", "dec"),
    ), start=1)
    for name in names
}
_MONTH = r"(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)\.?"
_WEEKDAY = r"(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(?:day|nesday|urday|sday)?\.?"
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

# Korean public holidays that drive outbound travel, as (first day, last day, substitute days).
# Lunar dates shift every year: extend this table for new years and check it against the
# official calendar (월력요항) when it is published.
KOREAN_HOLIDAYS: dict[str, dict[int, tuple[str, str, tuple[str, ...]]]] = {
    "Seollal": {
        2026: ("2026-02-16", "2026-02-18", ()),
        2027: ("2027-02-06", "2027-02-08", ("2027-02-09",)),
        2028: ("2028-01-25", "2028-01-27", ()),
    },
    "Chuseok": {
        2026: ("2026-09-24", "2026-09-26", ()),
        2027: ("2027-09-14", "2027-09-16", ()),
        2028: ("2028-10-02", "2028-10-04", ("2028-10-05",)),
    },
}
HOLIDAY_ALIASES = {
    "Seollal": r"seollal|설날|\b설\b|lunar new year|korean new year",
    "Chuseok": r"chuseok|추석|korean thanksgiving",
}


@dataclass
class CalendarDay:
    day: int
    date: str  # ISO
    weekday: str
    notes: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        d = date.fromisoformat(self.date)
        return f"{self.weekday} {d.day} {d.strftime('%B')}"

    def line(self) -> str:
        year = date.fromisoformat(self.date).year
        notes = f" · {', '.join(self.notes)}" if self.notes else ""
        return f"Day {self.day} · {self.label} {year}{notes}"


@dataclass
class TripCalendar:
    start: str  # ISO date of Day 1
    nights: int
    event: str = ""
    from_range: bool = False  # the request gave both ends, so it fixes the trip length
    days: list[CalendarDay] = field(default_factory=list)

    @classmethod
    def build(cls, start: date, nights: int, *, event: str = "", from_range: bool = False) -> "TripCalendar":
        holidays = holiday_notes(start.year) | holiday_notes(start.year + 1)
        days = []
        for offset in range(max(nights, 0) + 1):
            current = start + timedelta(days=offset)
            notes = list(holidays.get(current.isoformat(), []))
            if current.weekday() == 6 and current.day <= 7:
                notes.append("first Sunday of the month")
            if offset == 0:
                notes.insert(0, "arrival")
            elif offset == nights:
                notes.insert(0, "departure")
            days.append(CalendarDay(offset + 1, current.isoformat(), WEEKDAYS[current.weekday()], notes))
        return cls(start.isoformat(), nights, event, from_range, days)

    def with_nights(self, nights: int) -> "TripCalendar":
        return self.build(date.fromisoformat(self.start), nights, event=self.event, from_range=self.from_range)

    def day(self, number: int) -> CalendarDay | None:
        return next((d for d in self.days if d.day == number), None)

    def lines(self) -> list[str]:
        return [d.line() for d in self.days]

    def instruction(self) -> str:
        return (
            "CALENDAR (computed from the request; it overrides any date or weekday in the concept, outline "
            "or earlier drafts): " + " | ".join(self.lines()) + ". Put each day's date in its heading "
            "('Day N · <Weekday> <D> <Month> · <title>'), give weekdays in hours checks ('[TK: Sunday "
            "hours]'), and plan around the closures and free days these weekdays bring."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start, "nights": self.nights, "event": self.event, "from_range": self.from_range,
            "days": [{"day": d.day, "date": d.date, "weekday": d.weekday, "notes": list(d.notes)} for d in self.days],
            "lines": self.lines(),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "TripCalendar | None":
        if not isinstance(data, dict) or not data.get("start"):
            return None
        try:
            return cls.build(
                date.fromisoformat(str(data["start"])), int(data.get("nights") or 0),
                event=str(data.get("event") or ""), from_range=bool(data.get("from_range")),
            )
        except (TypeError, ValueError):
            return None


def holiday_notes(year: int) -> dict[str, list[str]]:
    """ISO date -> holiday notes for one year (Korean public holidays in KOREAN_HOLIDAYS)."""
    notes: dict[str, list[str]] = {}
    for name, years in KOREAN_HOLIDAYS.items():
        if year not in years:
            continue
        first, last, substitutes = years[year]
        a, b = date.fromisoformat(first), date.fromisoformat(last)
        middle = a + (b - a) / 2
        current = a
        while current <= b:
            label = name if current == middle else f"{name} holiday" + (" (eve)" if current < middle else "")
            notes.setdefault(current.isoformat(), []).append(f"{label}, Korean public holiday")
            current += timedelta(days=1)
        for sub in substitutes:
            notes.setdefault(sub, []).append(f"substitute holiday for {name}, Korean public holiday")
    return notes


def _year_for(month: int, day: int, today: date) -> int:
    """The next occurrence of a month/day with no year given."""
    candidate = date(today.year, month, day)
    return today.year if candidate >= today else today.year + 1


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def find_dates(text: str, today: date) -> list[date]:
    """Calendar dates written in the text, in order (ISO, '6 Feb 2027', 'February 6, 2027', '6–10 Feb')."""
    lowered = text.lower()
    found: list[tuple[int, date]] = []
    for m in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", lowered):
        if (d := _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))):
            found.append((m.start(), d))
    # "6–10 February 2027" / "6 to 10 Feb"
    for m in re.finditer(
        rf"\b(\d{{1,2}})\s*(?:–|—|-|to|until)\s*(?:{_WEEKDAY}\s+)?(\d{{1,2}})\s+{_MONTH}(?:,?\s*(\d{{4}}))?", lowered,
    ):
        month = MONTHS[m.group(3)]
        year = int(m.group(4)) if m.group(4) else _year_for(month, int(m.group(1)), today)
        for group, pos in ((1, m.start(1)), (2, m.start(2))):
            if (d := _safe_date(year, month, int(m.group(group)))):
                found.append((pos, d))
    # "6 February 2027", "Sat 6 Feb"
    for m in re.finditer(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+{_MONTH}(?:,?\s*(\d{{4}}))?", lowered):
        month = MONTHS[m.group(2)]
        year = int(m.group(3)) if m.group(3) else _year_for(month, int(m.group(1)), today)
        if (d := _safe_date(year, month, int(m.group(1)))):
            found.append((m.start(), d))
    # "February 6, 2027", "Feb 6"
    for m in re.finditer(rf"\b{_MONTH}\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s*(\d{{4}}))?", lowered):
        month = MONTHS[m.group(1)]
        year = int(m.group(3)) if m.group(3) else _year_for(month, int(m.group(2)), today)
        if (d := _safe_date(year, month, int(m.group(2)))):
            found.append((m.start(), d))
    seen: set[date] = set()
    ordered = []
    for _, d in sorted(found, key=lambda item: item[0]):
        if d not in seen:
            seen.add(d)
            ordered.append(d)
    return ordered


def find_holiday(text: str, today: date) -> tuple[str, date] | None:
    """('Seollal', first holiday day) for a request naming a Korean holiday, with or without a year."""
    lowered = text.lower()
    for name, pattern in HOLIDAY_ALIASES.items():
        m = re.search(rf"(?:{pattern})(?:\s*(20\d\d))?", lowered)
        if not m:
            continue
        year = int(m.group(1)) if m.group(1) else None
        if year is None:
            near = re.search(rf"(20\d\d)\s*(?:{pattern})", lowered)
            year = int(near.group(1)) if near else None
        years = KOREAN_HOLIDAYS[name]
        if year is None:
            year = next((y for y in sorted(years) if date.fromisoformat(years[y][1]) >= today), None)
        if year in years:
            return name, date.fromisoformat(years[year][0])
    return None


def parse_trip_calendar(
    text: str, *, nights: int, nights_explicit: bool = False, today: date | None = None,
) -> TripCalendar | None:
    """The trip calendar a request describes, or None when it names no dates or known holiday.

    Two or more dates fix the trip (first to last) unless an explicit length disagrees,
    in which case the first date starts it; one date starts it. A named Korean
    holiday starts the trip on the holiday's first day. `nights` is the length to use
    when the request does not give both ends.
    """
    today = today or date.today()
    dates = [d for d in find_dates(text, today) if d >= today]
    if len(dates) >= 2:
        start, end = min(dates), max(dates)
        span = (end - start).days
        if 1 <= span <= 21:
            holiday = find_holiday(text, today)
            event = holiday[0] if holiday else ""
            if nights_explicit and nights and span != nights:
                # "a 4-night stay over Sat 6 to Tue 9 Feb": the range is the holiday, not the trip.
                return TripCalendar.build(start, nights, event=event)
            return TripCalendar.build(start, span, event=event, from_range=True)
    holiday = find_holiday(text, today)
    if dates:
        return TripCalendar.build(dates[0], nights, event=holiday[0] if holiday else "")
    if holiday and nights:
        return TripCalendar.build(holiday[1], nights, event=holiday[0])
    return None
