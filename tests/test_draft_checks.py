"""Deterministic draft checks: dates fixed in Python, the rest flagged as revisions."""

from __future__ import annotations

from datetime import date

from app.orchestration.calendar import TripCalendar
from app.orchestration.draft_checks import apply_calendar, check_draft

CAL = TripCalendar.build(date(2027, 2, 6), 4, event="Seollal")


def test_day_headings_get_the_real_date_and_tk_notes_the_real_weekday():
    body = (
        "Intro.\n\n## Day 1 · Friday 5 February · Seollal eve\nDinner [TK: confirm Friday dinner hours].\n\n"
        "## Day 2: Seollal\nGallery [TK: Saturday hours]. Open Tuesday to Sunday.\n\n## Insider notes\n[TK: Monday]"
    )
    fixed = apply_calendar(body, CAL)
    assert "## Day 1 · Saturday 6 February · Seollal eve" in fixed
    assert "[TK: confirm Saturday dinner hours]" in fixed
    assert "## Day 2 · Sunday 7 February · Seollal" in fixed and "[TK: Sunday hours]" in fixed
    assert "Open Tuesday to Sunday" in fixed  # plain prose is left alone
    assert "[TK: Monday]" in fixed  # outside a day section
    assert apply_calendar(body, None) == body


def test_wrong_weekdays_and_dates_outside_the_trip_are_flagged():
    issues = check_draft("We fly on Friday 6 February and leave on Friday 5 February.", calendar=CAL)
    assert any("6 February 2027 is a Saturday" in i for i in issues)
    assert any('"Friday 5 February" is outside the trip' in i for i in issues)
    assert not check_draft("Seollal falls on Sunday 7 February.", calendar=CAL)


def test_banned_phrases_and_implausible_car_times():
    body = "One reviewer loved it. The car takes about 110–195 minutes. The hill-town day trip is about 160-200 minutes by train."
    issues = check_draft(body, banned_phrases=["one reviewer", "Time Out"])
    assert any('remove "one reviewer"' in i for i in issues)
    assert not any("Time Out" in i for i in issues)
    assert sum("too long for a car ride" in i for i in issues) == 1
