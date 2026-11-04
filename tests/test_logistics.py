"""Logistics checker: leg maths and rules run offline on a fake map."""

from __future__ import annotations

import json

from app.agents.logistics import LogisticsAgent, LogisticsTask, traffic_band
from app.llm.base import LLMResponse
from app.tools.geo import Place, Route

HOTEL = Place("Hotel", "Hotel", "hotel", 13.744, 100.547)
PLACES = {
    "Park": Place("Park", "Park", "park", 13.731, 100.541),            # ~1.6 km
    "Museum": Place("Museum", "Museum", "museum", 13.749, 100.528),    # ~2 km
    "Far Restaurant": Place("Far", "Far Restaurant", "restaurant", 13.739, 100.508),
    "Airport": Place("Airport", "Airport", "aerodrome", 13.69, 100.75),
    "Elsewhere": Place("Elsewhere", "Elsewhere", "sight", 48.85, 2.35),  # another continent
}
ROUTES = {"Airport": Route(30.0, 40, 400), "Park": Route(1.6, 6, 14), "Museum": Route(4.3, 7, 33), "Far Restaurant": Route(8.0, 20, 90)}


class FakeGeo:
    def geocode(self, query, *, near=None, airport=False):
        if query == "Hotel":
            return HOTEL
        return PLACES.get(query.split(",")[0])

    def route(self, a, b):
        other = b if a is HOTEL else a
        if a is not HOTEL and b is not HOTEL:
            other = b
        return ROUTES.get(other.name, Route(3.0, 10, 40))


class StopsProvider:
    def __init__(self, days):
        self.days = days

    def complete(self, messages, *, system=None, **kwargs):
        assert "logistics check" in (system or "").lower()
        return LLMResponse(text=json.dumps({"city": "Bangkok", "days": self.days}))


def stop(name, time, kind, given=False):
    return {"name": name, "query": f"{name}, Bangkok", "time": time, "time_given": given, "kind": kind}


def test_short_hop_is_a_walk_and_far_dinner_in_rush_hour_does_not_fit():
    days = [{"day": 2, "stops": [
        stop("Hotel", "09:00", "hotel"), stop("Park", "10:00", "park"),
        stop("Hotel", "17:00", "hotel"), stop("Far Restaurant", "17:30", "restaurant", given=True),
        stop("Hotel", "21:30", "hotel"),
    ]}]
    result = LogisticsAgent(StopsProvider(days), FakeGeo()).run(LogisticsTask(hotel="Hotel", plan={}))
    assert result.status == "complete"
    park = result.legs[0]
    assert park.mode == "walk" and park.walk_min == 14 and park.verdict == "ok"
    dinner = result.legs[2]
    assert dinner.mode == "car" and dinner.traffic == "evening rush hour"
    assert dinner.drive_min == (45, 80)  # 20 min free flow x 2.0-3.5 + overhead
    assert dinner.verdict == "not_feasible"
    assert any("does not fit before 17:30" in issue for issue in result.issues)
    assert "Day 2" in result.lines()[0] and "[DOES NOT FIT]" in result.lines()[0]


def test_unknown_and_far_away_places_are_reported_not_routed():
    days = [{"day": 1, "stops": [stop("Hotel", "15:00", "hotel"), stop("Elsewhere", "16:00", "sight"),
                                 stop("Nowhere", "18:00", "sight")]}]
    result = LogisticsAgent(StopsProvider(days), FakeGeo()).run(LogisticsTask(hotel="Hotel", plan={}))
    assert result.unresolved == ["Elsewhere", "Nowhere"]
    assert all(leg.verdict == "unknown" for leg in result.legs)
    assert not result.issues


def test_too_much_transit_for_a_slow_traveller_is_flagged():
    days = [{"day": 3, "stops": [stop("Hotel", "08:00", "hotel"), stop("Far Restaurant", "12:30", "restaurant"),
                                 stop("Hotel", "17:00", "hotel")]}]  # 35-55 + 45-80 min
    agent = LogisticsAgent(StopsProvider(days), FakeGeo())
    relaxed = agent.run(LogisticsTask(hotel="Hotel", plan={}))
    couples = agent.run(LogisticsTask(hotel="Hotel", plan={}, segment="couples"))
    assert not any("in transit" in i for i in relaxed.issues)
    assert any("in transit, not counting airport transfers (limit 120" in i for i in couples.issues)


def test_airport_transfers_do_not_count_against_the_daily_limit():
    days = [{"day": 4, "stops": [stop("Hotel", "08:00", "hotel"), stop("Airport", "13:00", "airport")]}]
    result = LogisticsAgent(StopsProvider(days), FakeGeo()).run(LogisticsTask(hotel="Hotel", plan={}, segment="couples"))
    assert result.day_travel_min[4][1] > 60
    assert not any("in transit" in i for i in result.issues)


def test_traffic_bands():
    assert traffic_band(8 * 60)[1] == "morning rush hour"
    assert traffic_band(13 * 60)[1] == "daytime traffic"
    assert traffic_band(22 * 60)[1] == "light traffic"


def test_unreadable_plan_or_missing_hotel_is_unavailable():
    assert LogisticsAgent(StopsProvider([]), FakeGeo()).run(LogisticsTask(hotel="Hotel", plan={})).status == "unavailable"
    days = [{"day": 1, "stops": [stop("Hotel", "15:00", "hotel")]}]
    assert LogisticsAgent(StopsProvider(days), FakeGeo()).run(
        LogisticsTask(hotel="Unknown Hotel", plan={})).status == "unavailable"


def test_walk_time_never_beats_a_holiday_pace(monkeypatch):
    from app.tools import geo

    client = geo.GeoClient(photon_url="x", osrm_url="y")
    answers = {"routed-car": {"distance": 800, "duration": 180}, "routed-foot": {"distance": 750, "duration": 240}}
    monkeypatch.setattr(client, "_osrm", lambda profile, a, b: answers[profile])
    route = client.route(HOTEL, PLACES["Park"])
    assert route.walk_km == 0.8 and round(route.walk_min) == 10  # 0.75 km at 4.5 km/h, not OSRM's 4 min
