"""Trip calendar: dates and weekdays worked out in Python, not by a model."""

from __future__ import annotations

from datetime import date

from app.orchestration.calendar import TripCalendar, holiday_notes, parse_trip_calendar

TODAY = date(2026, 11, 10)


def test_a_named_holiday_starts_the_trip_on_its_first_day_with_real_weekdays():
    cal = parse_trip_calendar("Plan a 4-night stay over Seollal 2027", nights=4, nights_explicit=True, today=TODAY)
    assert [d.label for d in cal.days] == [
        "Saturday 6 February", "Sunday 7 February", "Monday 8 February", "Tuesday 9 February", "Wednesday 10 February",
    ]
    assert "Seollal, Korean public holiday" in cal.day(2).notes
    assert "first Sunday of the month" in cal.day(2).notes
    assert any("substitute holiday" in n for n in cal.day(4).notes)
    assert cal.day(5).notes[0] == "departure" and not cal.from_range


def test_a_holiday_without_a_year_is_the_next_one():
    cal = parse_trip_calendar("a 3-night Chuseok trip", nights=3, nights_explicit=True, today=TODAY)
    assert cal.start == "2027-09-14" and cal.event == "Chuseok"


def test_a_date_range_fixes_the_trip_length():
    cal = parse_trip_calendar("Day 1 Sat 6 Feb ... Day 5 Wed 10 Feb", nights=3, today=TODAY)
    assert cal.from_range and cal.nights == 4 and cal.start == "2027-02-06"
    cal = parse_trip_calendar("February 6, 2027 - February 10, 2027", nights=3, today=TODAY)
    assert cal.nights == 4


def test_an_explicit_length_wins_over_a_range_that_describes_the_holiday():
    text = "Plan a 4-night stay over Seollal 2027 (Sat 6 to Tue 9 February, with leave added)."
    cal = parse_trip_calendar(text, nights=4, nights_explicit=True, today=TODAY)
    assert cal.start == "2027-02-06" and cal.nights == 4 and not cal.from_range


def test_no_dates_or_past_dates_give_no_calendar():
    assert parse_trip_calendar("Research Park Hyatt Bangkok for a couple", nights=3, today=TODAY) is None
    assert parse_trip_calendar("The hotel opened on 15 May 2025.", nights=3, today=TODAY) is None


def test_round_trip_and_new_length():
    cal = parse_trip_calendar("Seollal 2027", nights=4, today=TODAY)
    again = TripCalendar.from_dict(cal.to_dict())
    assert again.lines() == cal.lines()
    assert len(again.with_nights(2).days) == 3


def test_holiday_table_marks_the_middle_day_as_the_holiday():
    notes = holiday_notes(2027)
    assert notes["2027-02-06"][0].startswith("Seollal holiday (eve)")
    assert notes["2027-02-07"][0].startswith("Seollal,")
