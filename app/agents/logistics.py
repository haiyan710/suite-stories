"""Logistics checker: are an itinerary's legs realistic in time and transport?

One fast-model call lists each day's stops; everything after that is Python:
OpenStreetMap geocoding and OSRM routing (free-flow drive and walk times), a
time-of-day traffic allowance, and fixed rules for whether a leg fits between
the plan's times and whether a day spends too long in transit. The result is
an estimate, never a sourced fact: copy says "about".
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from ..llm.base import LLMMessage, LLMProvider
from ..llm.zai_glm import extract_json_block
from ..prompts import load_prompt
from ..tools.geo import GeoClient, Place, haversine_km

logger = logging.getLogger(__name__)

SOURCE_NOTE = "Estimates from OpenStreetMap routing (no live traffic data), with a time-of-day traffic allowance."

# OSRM times are free-flow. (low, high) multipliers by arrival time; no city data, so broad
# bands that lean high. The first bands (2.0-3.5x) turned an airport transfer of about
# an hour into "110-195 minutes"; these keep a margin without inventing gridlock.
TRAFFIC_BANDS = (
    ((7 * 60, 9 * 60 + 30), (1.4, 2.0), "morning rush hour"),
    ((16 * 60 + 30, 19 * 60 + 30), (1.4, 2.0), "evening rush hour"),
    ((9 * 60 + 30, 16 * 60 + 30), (1.2, 1.6), "daytime traffic"),
)
OFF_PEAK = ((1.0, 1.3), "light traffic")
# Farther than this from a station (or by a mode the plan names), a leg is a rail or boat
# trip: road times say nothing about it, so it is listed as unchecked instead of "long".
INTERCITY_KM = 30
RAIL_MODES = {"train", "rail", "boat", "ferry"}
# Getting a car to the door, parking, finding the entrance.
CAR_OVERHEAD_MIN = (5, 10)
WALK_MAX_MIN = 15
WALK_OPTION_MAX_MIN = 30
LONG_LEG_MIN = 60
# How long a guest stays at a stop before moving on, when the plan gives no end time.
DWELL_MIN = {
    "hotel": 0, "airport": 0, "station": 0, "restaurant": 90, "bar": 60, "sight": 75,
    "museum": 75, "park": 45, "market": 60, "area": 90, "spa": 90,
}
# Daily time in cars before a plan stops feeling like a holiday.
DAILY_TRAVEL_LIMIT_MIN = {"slow": 120, "couples": 120, "wellness": 120}
DEFAULT_DAILY_TRAVEL_LIMIT_MIN = 180
# A match farther than this from the hotel is a different place with the same name.
MAX_DISTANCE_KM = 150


@dataclass
class LogisticsTask:
    hotel: str
    plan: dict[str, Any]
    segment: str = ""


@dataclass
class Leg:
    day: int
    origin: str
    destination: str
    arrive: str = ""
    distance_km: float = 0.0
    walk_min: int = 0
    walk_km: float = 0.0
    drive_min: tuple[int, int] = (0, 0)
    traffic: str = ""
    mode: str = ""
    verdict: str = "ok"  # ok | tight | not_feasible | unknown
    note: str = ""

    def line(self) -> str:
        if self.verdict == "unknown":
            return f"{self.origin} → {self.destination}: not checked ({self.note})"
        lo, hi = self.drive_min
        if self.mode == "walk":
            how = f"walk about {self.walk_min} min ({self.walk_km or self.distance_km} km)"
        else:
            how = f"car about {lo}-{hi} min in {self.traffic} ({self.distance_km} km)"
            if self.walk_min <= WALK_OPTION_MAX_MIN:
                how += f", or walk about {self.walk_min} min"
        flag = {"tight": " [TIGHT]", "not_feasible": " [DOES NOT FIT]"}.get(self.verdict, "")
        return f"{self.origin} → {self.destination}: {how}{flag}" + (f". {self.note}" if self.note else "")


@dataclass
class LogisticsResult:
    status: str  # complete | unavailable
    legs: list[Leg] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    day_travel_min: dict[int, tuple[int, int]] = field(default_factory=dict)
    source: str = SOURCE_NOTE

    def lines(self) -> list[str]:
        """One line per day, for the outline, the writer and the draft critic."""
        out = []
        for day in sorted({leg.day for leg in self.legs}):
            legs = "; ".join(leg.line() for leg in self.legs if leg.day == day)
            lo, hi = self.day_travel_min.get(day, (0, 0))
            out.append(f"Day {day} ({lo}-{hi} min of travel): {legs}")
        return out

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["lines"] = self.lines()
        return data


def _minutes(value: str) -> int | None:
    match = re.match(r"^\s*(\d{1,2}):(\d{2})", str(value or ""))
    return int(match.group(1)) * 60 + int(match.group(2)) if match else None


def _round5(value: float) -> int:
    return max(5, int(5 * round(value / 5)))


def traffic_band(arrive_min: int | None) -> tuple[tuple[float, float], str]:
    if arrive_min is None:
        return (1.4, 2.2), "daytime traffic"
    for (start, end), factors, label in TRAFFIC_BANDS:
        if start <= arrive_min < end:
            return factors, label
    return OFF_PEAK


class LogisticsAgent:
    name = "logistics"

    def __init__(self, provider: LLMProvider, geo: GeoClient) -> None:
        self._provider = provider
        self._geo = geo

    def run(self, task: LogisticsTask) -> LogisticsResult:
        days = self._extract(task)
        if not days:
            return LogisticsResult(status="unavailable", issues=["The plan's stops could not be read."])
        hotel = self._geo.geocode(task.hotel)
        if hotel is None:
            return LogisticsResult(status="unavailable", issues=[f"{task.hotel} was not found on the map."])
        return self.evaluate(days, hotel, task.hotel, segment=task.segment)

    def _extract(self, task: LogisticsTask) -> list[dict[str, Any]]:
        response = self._provider.complete(
            [LLMMessage(role="user", content=json.dumps({"hotel": task.hotel, **task.plan}, ensure_ascii=False))],
            system=load_prompt("logistics_extract"), max_tokens=2500, temperature=0.0,
        )
        data = extract_json_block(response.text)
        days = data.get("days") if isinstance(data, dict) else None
        return [d for d in days or [] if isinstance(d, dict) and isinstance(d.get("stops"), list)]

    def _locate(self, stop: dict[str, Any], hotel: Place, hotel_name: str) -> Place | None:
        if str(stop.get("kind")) == "hotel":
            return hotel
        query = str(stop.get("query") or stop.get("name") or "").strip()
        if not query:
            return None
        place = self._geo.geocode(query, near=hotel, airport=str(stop.get("kind")) == "airport")
        if place is None and "," in query:
            place = self._geo.geocode(query.split(",")[0], near=hotel, airport=str(stop.get("kind")) == "airport")
        if place is not None and haversine_km(place, hotel) > MAX_DISTANCE_KM:
            return None
        return place

    def evaluate(
        self, days: list[dict[str, Any]], hotel: Place, hotel_name: str, *, segment: str = "",
    ) -> LogisticsResult:
        result = LogisticsResult(status="complete")
        limit = DAILY_TRAVEL_LIMIT_MIN.get(segment, DEFAULT_DAILY_TRAVEL_LIMIT_MIN)
        for day in days:
            number = int(day.get("day") or 0)
            stops = []
            for stop in day["stops"]:
                if not isinstance(stop, dict):
                    continue
                place = self._locate(stop, hotel, hotel_name)
                label = hotel_name if str(stop.get("kind")) == "hotel" else str(stop.get("name") or stop.get("query"))
                if place is None and label not in result.unresolved:
                    result.unresolved.append(label)
                stops.append((stop, place, label))
            total = [0, 0]
            leisure = 0  # airport and station transfers cannot be cut, so they do not count against the limit
            for (a, pa, la), (b, pb, lb) in zip(stops, stops[1:]):
                leg = self._leg(number, a, pa, la, b, pb, lb)
                if leg is None:
                    continue
                result.legs.append(leg)
                if leg.verdict != "unknown":
                    if leg.mode == "walk":
                        total[0] += leg.walk_min
                        total[1] += leg.walk_min
                    else:
                        total[0] += leg.drive_min[0]
                        total[1] += leg.drive_min[1]
                    if not {str(a.get("kind")), str(b.get("kind"))} & {"airport", "station"}:
                        leisure += leg.walk_min if leg.mode == "walk" else leg.drive_min[1]
                if leg.verdict == "not_feasible":
                    result.issues.append(
                        f"Day {number}: {la} → {lb} does not fit before {leg.arrive} "
                        f"(car about {leg.drive_min[0]}-{leg.drive_min[1]} min in {leg.traffic}). "
                        "Move the time, swap the order or drop a stop."
                    )
                elif leg.verdict == "tight":
                    result.issues.append(
                        f"Day {number}: {la} → {lb} is tight for {leg.arrive} "
                        f"(car about {leg.drive_min[0]}-{leg.drive_min[1]} min in {leg.traffic}); leave a buffer."
                    )
                elif leg.mode != "walk" and leg.drive_min[1] >= LONG_LEG_MIN and str(b.get("kind")) != "airport" \
                        and str(a.get("kind")) != "airport":
                    result.issues.append(
                        f"Day {number}: {la} → {lb} is a long ride (about {leg.drive_min[0]}-{leg.drive_min[1]} min "
                        f"in {leg.traffic}); say so in the copy or plan it outside rush hour."
                    )
            result.day_travel_min[number] = (total[0], total[1])
            if leisure > limit:
                result.issues.append(
                    f"Day {number}: up to {leisure} min in transit, not counting airport transfers "
                    f"(limit {limit} for this traveller); "
                    "group stops by area or cut one."
                )
        return result

    def _leg(
        self, day: int, a: dict[str, Any], pa: Place | None, la: str,
        b: dict[str, Any], pb: Place | None, lb: str,
    ) -> Leg | None:
        arrive = str(b.get("time") or "")
        if pa is None or pb is None:
            if la == lb:
                return None
            return Leg(day, la, lb, arrive=arrive, verdict="unknown", note="place not found on the map")
        if haversine_km(pa, pb) < 0.15:
            return None
        planned = str(b.get("mode") or "").lower()
        kinds = {str(a.get("kind")), str(b.get("kind"))}
        if haversine_km(pa, pb) > INTERCITY_KM and (planned in RAIL_MODES or "station" in kinds):
            return Leg(
                day, la, lb, arrive=arrive, distance_km=round(haversine_km(pa, pb), 1), mode=planned or "train",
                verdict="unknown", note="a train or boat trip: use the timetable, road times do not apply",
            )
        route = self._geo.route(pa, pb)
        if route is None:
            return Leg(day, la, lb, arrive=arrive, verdict="unknown", note="no route found")
        t_a, t_b = _minutes(str(a.get("time"))), _minutes(arrive)
        (f_lo, f_hi), traffic = traffic_band(t_b)
        drive = (
            _round5(route.drive_min * f_lo + CAR_OVERHEAD_MIN[0]),
            _round5(route.drive_min * f_hi + CAR_OVERHEAD_MIN[1]),
        )
        walk = int(round(route.walk_min)) if route.walk_min != float("inf") else 999
        mode = "walk" if walk <= WALK_MAX_MIN else "car"
        note = ""
        if planned in {"train", "metro", "subway", "skytrain", "boat", "ferry"}:
            note = f"plan says {planned}; rail and boat times are not checked"
        travel = (walk, walk) if mode == "walk" else drive
        verdict = "ok"
        if t_a is not None and t_b is not None and t_b > t_a:
            gap = t_b - t_a - DWELL_MIN.get(str(a.get("kind")), 60)
            given = bool(a.get("time_given") or b.get("time_given"))
            if travel[0] > gap + (0 if given else 60):
                verdict = "not_feasible"
            elif given and travel[1] > gap:
                verdict = "tight"
        return Leg(
            day, la, lb, arrive=arrive, distance_km=route.distance_km, walk_min=walk, walk_km=route.walk_km,
            drive_min=drive, traffic=traffic, mode=mode, verdict=verdict, note=note,
        )
