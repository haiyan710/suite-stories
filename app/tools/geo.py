"""Free OpenStreetMap services for itinerary logistics: Photon geocoding and OSRM routing.

No API key and no traffic data: OSRM returns free-flow driving and walking times,
and the logistics agent applies its own time-of-day allowances on top. Geocoding
results are cached in the database so each place is looked up once.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

USER_AGENT = "SuiteStoriesBot/1.0 (internal editorial tool)"
# Holiday walking pace; OSRM's foot profile is faster than a couple strolling in heat.
WALK_KMH = 4.5
# Stay well under the public servers' fair-use limits.
REQUEST_GAP_SECONDS = 0.5


@dataclass(frozen=True)
class Place:
    query: str
    name: str
    kind: str
    lat: float
    lon: float


@dataclass(frozen=True)
class Route:
    distance_km: float
    drive_min: float
    walk_min: float
    walk_km: float = 0.0


def haversine_km(a: Place, b: Place) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a.lat, a.lon, b.lat, b.lon))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


class GeoClient:
    def __init__(self, *, photon_url: str, osrm_url: str, db=None, timeout: float = 20.0) -> None:
        self._photon_url = photon_url
        self._osrm_url = osrm_url.rstrip("/")
        self._db = db
        self._timeout = timeout
        self._last_request = 0.0
        self._routes: dict[tuple[float, float, float, float], Route | None] = {}

    def _get(self, url: str, params: dict | None = None) -> dict:
        wait = REQUEST_GAP_SECONDS - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        try:
            response = httpx.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=self._timeout)
            response.raise_for_status()
            return response.json()
        finally:
            self._last_request = time.monotonic()

    def geocode(self, query: str, *, near: Place | None = None, airport: bool = False) -> Place | None:
        """The best OpenStreetMap match for a place name, biased towards `near` (the hotel)."""
        key = query.strip().lower() + ("|airport" if airport else "")
        if self._db is not None:
            row = self._db.geo_lookup(key)
            if row is not None:
                return Place(query, row["name"], row["kind"], row["lat"], row["lon"]) if row["found"] else None
        params: dict = {"q": query, "limit": 1}
        if near is not None:
            params.update(lat=near.lat, lon=near.lon)
        if airport:
            params["osm_tag"] = "aeroway:aerodrome"
        try:
            data = self._get(self._photon_url, params)
        except (httpx.HTTPError, ValueError):
            logger.warning("geocoding failed for %r", query, exc_info=True)
            return None  # not cached: a network error is not a "not found"
        features = data.get("features") or []
        place = None
        if features:
            props = features[0].get("properties") or {}
            lon, lat = (features[0].get("geometry") or {}).get("coordinates") or (None, None)
            if lat is not None and lon is not None:
                place = Place(query, str(props.get("name") or query), str(props.get("osm_value") or ""),
                              float(lat), float(lon))
        if self._db is not None:
            self._db.geo_store(
                key, found=place is not None, name=place.name if place else "",
                kind=place.kind if place else "", lat=place.lat if place else None,
                lon=place.lon if place else None,
            )
        return place

    def _osrm(self, profile: str, a: Place, b: Place) -> dict | None:
        url = f"{self._osrm_url}/{profile}/route/v1/driving/{a.lon},{a.lat};{b.lon},{b.lat}"
        try:
            routes = self._get(url, {"overview": "false"}).get("routes") or []
        except (httpx.HTTPError, ValueError):
            logger.warning("routing failed (%s) %s -> %s", profile, a.name, b.name, exc_info=True)
            return None
        return routes[0] if routes else None

    def route(self, a: Place, b: Place) -> Route | None:
        """Free-flow driving and walking time between two places (no traffic)."""
        key = (a.lat, a.lon, b.lat, b.lon)
        if key not in self._routes:
            car = self._osrm("routed-car", a, b)
            foot = self._osrm("routed-foot", a, b)
            walk_km = float(foot["distance"]) / 1000 if foot else 0.0
            self._routes[key] = Route(
                distance_km=round(float(car["distance"]) / 1000, 1),
                drive_min=float(car["duration"]) / 60,
                walk_min=max(float(foot["duration"]) / 60, walk_km / WALK_KMH * 60) if foot else float("inf"),
                walk_km=round(walk_km, 1),
            ) if car else None
        return self._routes[key]
