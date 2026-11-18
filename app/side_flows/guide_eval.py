"""Golden evaluation of a Korean hotel guide: checks Python can run on the stored output.

The live script (`scripts/run_golden_guide.py`) runs the side flow on a copy of a
real thread and scores what it stored in `concept_state.guide_ko`; the unit tests
score hand-built guides. Every check is a property of a good page, never an
expected answer: the shape (N+1 days, timed moments), each photo used once,
destination photos credited and dated in the trip's season, no daylight sight
after sunset in a short-day season and no open-air evening out in winter, the
title carrying the trip length, and a bounded number of [확인 필요] notes.
Vocabulary here is generic (parks, gardens, rooftops), never one hotel's or city's.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from ..artifacts.guide_ko import guide_check_items, guide_jsonld, validate_guide
from ..tools.commons import _month
from ..tools.season import season_months

# Sights that need daylight: a winter evening there is a walk in the dark.
DAYLIGHT_SIGHTS = re.compile(
    r"공원|정원|가든|해변|전망대|야외|숲|park\b|garden|beach|viewpoint|lookout|vineyard|포도밭", re.I,
)
# Open-air evenings that do not suit a cold season (a rooftop dinner in February).
OPEN_AIR = re.compile(r"루프탑|테라스|야외|rooftop|terrace|open-air|al fresco", re.I)
# Sunset before this hour makes the evening (저녁) slot dark.
EARLY_SUNSET_HOUR = 18

DEFAULT_REQUIRED = ("shape", "photos_once", "season_photos", "daylight", "credits")


@dataclass(frozen=True)
class GuideCheck:
    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class GuideEvaluation:
    checks: tuple[GuideCheck, ...]
    score: int
    total: int
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "failures": [c.name for c in self.checks if not c.passed]}


def _days(guide: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [d for d in guide.get("days") or [] if isinstance(d, dict)]


def _moments(day: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [m for m in day.get("moments") or [] if isinstance(m, dict)]


def _photo_uses(guide: Mapping[str, Any]) -> list[str]:
    uses = [str(guide.get("hero_photo") or "")]
    for day in _days(guide):
        uses.append(str(day.get("photo") or ""))
        uses += [str(m.get("photo") or "") for m in _moments(day)]
    return [u for u in uses if u]


def _hour(clock: str) -> int:
    match = re.match(r"(\d{1,2}):\d{2}", str(clock or ""))
    return int(match.group(1)) if match else 24


def evaluate_guide(stored: Mapping[str, Any], criteria: Mapping[str, Any] | None = None) -> GuideEvaluation:
    """Score a stored guide (`concept_state.guide_ko`) against the fixture's criteria."""
    criteria = dict(criteria or {})
    guide = dict(stored.get("guide") or {})
    nights = int(stored.get("nights") or 0)
    season = dict(stored.get("season") or {})
    credits = dict(stored.get("photo_credits") or {})
    days = _days(guide)
    checks: list[GuideCheck] = []

    issues = validate_guide(guide, nights=nights)
    checks.append(GuideCheck("shape", not issues, "; ".join(issues[:4])))

    repeated = [key for key, n in Counter(_photo_uses(guide)).items() if n > 1]
    checks.append(GuideCheck("photos_once", not repeated, f"used more than once: {', '.join(repeated)}" if repeated else ""))

    allowed = season_months([str(d) for d in season.get("dates") or []]) if season.get("dates") else set()
    off_season = [
        f"{c.get('place') or key} ({c.get('taken')})" for key, c in credits.items()
        if allowed and _month(str(c.get("taken") or "")) and _month(str(c.get("taken") or "")) not in allowed
    ]
    checks.append(GuideCheck(
        "season_photos", not off_season, f"dated outside the trip's season: {', '.join(off_season)}" if off_season else "",
    ))

    sunsets = {int(d.get("day") or 0): _hour(d.get("sunset")) for d in season.get("daylight") or [] if isinstance(d, dict)}
    cold = str(season.get("season") or "") == "winter" or int((season.get("typical") or {}).get("high_c", 99)) < 15
    dark: list[str] = []
    for index, day in enumerate(days, 1):
        number = int(day.get("day") or index)
        for moment in _moments(day):
            if moment.get("time") != "저녁":
                continue
            words = f"{moment.get('title', '')} {moment.get('text', '')} {moment.get('place', '')}"
            if sunsets.get(number, 24) < EARLY_SUNSET_HOUR and DAYLIGHT_SIGHTS.search(words):
                dark.append(f"Day {number} 저녁 '{moment.get('title')}' (sunset {sunsets[number]}h)")
            elif cold and OPEN_AIR.search(words):
                dark.append(f"Day {number} 저녁 '{moment.get('title')}' is open-air in the cold season")
    checks.append(GuideCheck("daylight", not dark, "; ".join(dark)))

    placed = set(_photo_uses(guide))
    uncredited = [
        key for key, c in credits.items()
        if key in placed and not all(str(c.get(k) or "").strip() for k in ("licence", "page_url"))
    ]
    checks.append(GuideCheck("credits", not uncredited, f"missing licence or source: {', '.join(uncredited)}" if uncredited else ""))

    length = f"{nights}박 {nights + 1}일" if nights else ""
    checks.append(GuideCheck(
        "title_length", not length or length in str(guide.get("title") or ""),
        f"title lacks '{length}'" if length and length not in str(guide.get("title") or "") else "",
    ))

    listed = len(guide_jsonld(guide)["itinerary"]["itemListElement"])
    checks.append(GuideCheck("jsonld", listed == len(days) > 0, f"{listed} itinerary items for {len(days)} days"))

    open_items = len(guide_check_items(guide))
    limit = int(criteria.get("max_check_items", 20))
    checks.append(GuideCheck("check_items", open_items <= limit, f"{open_items} [확인 필요] (limit {limit})"))

    min_places = int(criteria.get("min_destination_photos", 0))
    used_places = len([key for key in credits if key in placed])
    checks.append(GuideCheck(
        "destination_photos", used_places >= min_places, f"{used_places} placed (minimum {min_places})",
    ))

    required = set(criteria.get("required") or DEFAULT_REQUIRED)
    score = sum(1 for c in checks if c.passed)
    passed = all(c.passed for c in checks if c.name in required) and score >= int(criteria.get("min_score", 0))
    return GuideEvaluation(tuple(checks), score, len(checks), passed)
