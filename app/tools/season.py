"""The trip's season in numbers: daylight and typical weather on the trip dates.

A winter plan that puts a garden visit after dark, or a rooftop dinner in
February, reads wrong to a reader who knows the season. Sunrise and sunset come
from the same dates last year (they shift by a minute or so between years) and
the typical high, low and rain from the same dates over the last few years, via
Open-Meteo's free historical archive (no key). These are typical values, never a
forecast, and are written as "about".
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import httpx

logger = logging.getLogger(__name__)

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
USER_AGENT = "SuiteStoriesBot/1.0 (internal editorial tool)"
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December")


def season_name(month: int, lat: float | None) -> str:
    northern = ("winter", "winter", "spring", "spring", "spring", "summer", "summer", "summer",
                "autumn", "autumn", "autumn", "winter")[month - 1]
    if lat is not None and lat < 0:
        return {"winter": "summer", "summer": "winter", "spring": "autumn", "autumn": "spring"}[northern]
    return northern


# Meteorological seasons as month groups (the same groups in both hemispheres; only the names swap).
_SEASON_GROUPS = ({12, 1, 2}, {3, 4, 5}, {6, 7, 8}, {9, 10, 11})


def season_months(dates: list[str]) -> set[int]:
    """The months that look like the trip: those of its own season.

    A February trip allows December to February; March already shows spring and
    April a different city, so photos dated then are not used.
    """
    months = {date.fromisoformat(d).month for d in dates}
    return set().union(*(group for group in _SEASON_GROUPS if group & months))


def month_names(dates: list[str]) -> list[str]:
    """The trip's own months by name, in order ("February")."""
    seen: list[str] = []
    for d in dates:
        name = MONTHS[date.fromisoformat(d).month - 1]
        if name not in seen:
            seen.append(name)
    return seen


def _years_back(day: str, years: int) -> str:
    d = date.fromisoformat(day)
    try:
        return d.replace(year=d.year - years).isoformat()
    except ValueError:  # 29 February
        return d.replace(year=d.year - years, day=28).isoformat()


class SeasonClient:
    def __init__(self, *, archive_url: str = ARCHIVE_URL, years: int = 3, timeout: float = 20.0) -> None:
        self._url = archive_url
        self._years = years
        self._timeout = timeout

    def _daily(self, lat: float, lon: float, start: str, end: str) -> dict[str, list]:
        response = httpx.get(self._url, params={
            "latitude": lat, "longitude": lon, "start_date": start, "end_date": end, "timezone": "auto",
            "daily": "sunrise,sunset,temperature_2m_max,temperature_2m_min,precipitation_sum",
        }, headers={"User-Agent": USER_AGENT}, timeout=self._timeout)
        response.raise_for_status()
        return response.json().get("daily") or {}

    def trip_season(self, dates: list[str], *, lat: float | None, lon: float | None) -> dict[str, Any]:
        """Season, daylight per day and typical weather for the trip dates (only the month without a location)."""
        if not dates:
            return {}
        first = date.fromisoformat(dates[0])
        season: dict[str, Any] = {
            "month": MONTHS[first.month - 1], "season": season_name(first.month, lat), "dates": list(dates),
        }
        if lat is None or lon is None:
            return season
        today = date.today()
        offset = max(1, first.year - today.year + (1 if (first.month, first.day) >= (today.month, today.day) else 0))
        highs: list[float] = []
        lows: list[float] = []
        rainy = total = 0
        daylight: list[dict[str, str]] = []
        for back in range(offset, offset + self._years):
            start, end = _years_back(dates[0], back), _years_back(dates[-1], back)
            try:
                daily = self._daily(lat, lon, start, end)
            except Exception:
                logger.warning("Season lookup failed for %s..%s", start, end, exc_info=True)
                continue
            highs += [t for t in daily.get("temperature_2m_max") or [] if t is not None]
            lows += [t for t in daily.get("temperature_2m_min") or [] if t is not None]
            rain = [p for p in daily.get("precipitation_sum") or [] if p is not None]
            rainy += sum(1 for p in rain if p >= 1.0)
            total += len(rain)
            if not daylight:
                daylight = [
                    {"day": i, "date": d, "sunrise": str(rise)[-5:], "sunset": str(sets)[-5:]}
                    for i, (d, rise, sets) in enumerate(
                        zip(dates, daily.get("sunrise") or [], daily.get("sunset") or []), 1,
                    )
                ]
        if daylight:
            season["daylight"] = daylight
        if highs and lows:
            season["typical"] = {
                "high_c": round(sum(highs) / len(highs)), "low_c": round(sum(lows) / len(lows)),
                "rainy_days": f"about {round(rainy / total * len(dates))} of {len(dates)} days" if total else "",
                "basis": f"the same dates over the last {self._years} years; typical, not a forecast",
            }
        return season
