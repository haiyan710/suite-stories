"""Side flow: the Korean hotel guide (web itinerary page).

The main flow is the editorial one (research -> concept -> outline -> English
sample draft -> magazine layouts). A side flow runs beside it on demand and
never changes its state: it reads the thread's approved concept, outline,
latest draft, stop notes and research, and writes its own output under its own
key in concept_state. A failure in a side flow leaves the editorial work as it was.

This one produces the page the booking site publishes for Korean travellers: a
hotel-centred N-night plan with timed moments per day (오전 / 오후 / 저녁), the
hotel's photos, credited destination photos from Wikimedia Commons in the trip's
season, and the trip's daylight and typical weather. It runs only on Claude
(the hotel's own material may be in the evidence), never on GLM.

Trigger: "hotel guide", "Korean guide", "호텔 가이드", "bản tiếng Hàn" in a
thread whose concept is draftable (see `matches`).
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from ..agent import AgentCancelled
from ..artifacts import ArtifactManager
from ..artifacts.guide_ko import (
    apply_place_photos, dedupe_photos, fill_empty_days, guide_check_items, guide_jsonld, guide_places,
    hotel_tip_quotes_ok, is_outdoor, render_guide_html, render_journey_html, validate_guide, validate_journey,
)
from ..config import Settings
from ..llm.base import LLMMessage, LLMProvider
from ..llm.zai_glm import extract_json_block
from ..orchestration.fix_check import verify_fixes
from ..orchestration.handoffs import ResearchResult
from ..orchestration.planning import compact_research_json, fact_text
from ..prompts import load_prompt
from ..states import DRAFTABLE_CONCEPTS
from ..storage import Database
from ..tools.commons import CommonsPhotos
from ..tools.geo import GeoClient
from ..tools.season import SeasonClient, month_names, season_months

logger = logging.getLogger(__name__)

TRIGGER = re.compile(
    r"\b(?:hotel|web|site)[ -]guide\b|\bkorean (?:guide|version|page)\b|호텔 ?가이드|"
    r"한국어 ?(?:가이드|버전|페이지)|tiếng hàn|bản hàn",
    re.I,
)
# The denser hotel-journey page: a story intro, season tips and 4-8 moments a day.
JOURNEY_TRIGGER = re.compile(
    r"\bhotel[ -]journey\b|\bjourney (?:page|guide)\b|호텔 ?저니|저니 ?페이지|bản journey|trang journey",
    re.I,
)
# Commons photos per place that Claude opens to judge the season (downloaded first).
LOOK_CANDIDATES = 4


def matches(text: str, state: dict[str, Any]) -> bool:
    """A guide or journey request in a thread whose concept the team can draft from."""
    return bool(TRIGGER.search(text) or JOURNEY_TRIGGER.search(text)) and (
        (state.get("concept") or {}).get("status") in DRAFTABLE_CONCEPTS
    )


def wants_journey(text: str) -> bool:
    return bool(JOURNEY_TRIGGER.search(text))


@dataclass(frozen=True)
class PageKind:
    """The two pages this side flow writes; they share inputs, photos, season and checks."""

    key: str  # concept_state key and file suffix
    prompt: str
    label: str
    validate: Callable[..., list[str]]
    render: Callable[..., str]
    max_tokens: int


GUIDE = PageKind("guide_ko", "guide_ko", "Korean hotel guide", validate_guide, render_guide_html, 6000)
JOURNEY = PageKind("guide_journey", "guide_journey", "Korean hotel journey", validate_journey, render_journey_html, 14000)


@dataclass
class SideFlowResult:
    markdown: str
    attachments: list[str] = field(default_factory=list)


class EditorialHost(Protocol):
    """What a side flow reads from the editorial flow (helpers, never its state machine)."""

    _trip_nights: int

    def _cleared_images(self, research: ResearchResult) -> list[dict[str, Any]]: ...
    def _journey_research(self, project_id: int, state: dict[str, Any], research: ResearchResult) -> ResearchResult: ...
    def _closed_space_photos(self, photos: dict[str, Any], state: dict[str, Any]) -> list[str]: ...
    def _gated_research(self, research: ResearchResult) -> ResearchResult: ...
    def _house_style_payload(self, hotel: str, target: str = ...) -> dict[str, Any]: ...


class KoreanGuideFlow:
    """One run of the Korean hotel guide for one thread.

    Built per request with the providers and web clients the orchestrator holds,
    so tests can swap any of them.
    """

    def __init__(
        self, host: EditorialHost, *, settings: Settings, db: Database, provider: LLMProvider,
        fast_provider: LLMProvider, check_provider: LLMProvider | None = None,
        place_photos: CommonsPhotos | None = None,
        season: SeasonClient | None = None, geo: GeoClient | None = None,
        step: Callable[[str], None] = lambda _label: None,
    ) -> None:
        self._host = host
        self._settings = settings
        self._db = db
        self._provider = provider
        self._fast_provider = fast_provider
        # The check against the plan; STEP_MODELS may give it another model than the fast one.
        self._check_provider = check_provider or fast_provider
        self._place_photos = place_photos
        self._season = season
        self._geo = geo
        self._step = step

    def _write_guide(self, payload: dict[str, Any], kind: PageKind = GUIDE) -> dict[str, Any]:
        try:
            response = self._provider.complete(
                [LLMMessage(role="user", content=json.dumps(payload, ensure_ascii=False))],
                system=load_prompt(kind.prompt), max_tokens=kind.max_tokens, temperature=0.3,
            )
        except AgentCancelled:
            raise
        except Exception:
            logger.warning("Korean guide writing failed", exc_info=True)
            return {}
        data = extract_json_block(response.text)
        return data if isinstance(data, dict) and data.get("days") else {}

    def _trip_season(self, hotel: str, piece_format: dict[str, Any]) -> dict[str, Any]:
        """Daylight and typical weather on the trip dates, computed, for the guide's timing and tips."""
        dates = [str(d.get("date")) for d in (piece_format.get("calendar") or {}).get("days") or [] if d.get("date")]
        if not self._season or not dates:
            return {}
        lat = lon = None
        if self._geo and hotel:
            try:
                place = self._geo.geocode(hotel)
                if place:
                    lat, lon = place.lat, place.lon
            except Exception:
                logger.warning("Hotel geocoding for the season failed", exc_info=True)
        try:
            return self._season.trip_season(dates, lat=lat, lon=lon)
        except Exception:
            logger.warning("Season lookup failed", exc_info=True)
            return {}

    def _find_place_photos(
        self, guide: dict[str, Any], season: dict[str, Any] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Freely licensed photos of the guide's outside sights: {place: asset}.

        Commons search is loose and a date says little about how a photo looks, so
        Claude opens the top candidates of each place and picks one in the trip's
        season, or none. A provider that cannot read images picks from the titles.
        """
        places = guide_places(guide)
        if not self._place_photos or not places:
            return {}
        self._step("finding destination photos")
        dates = list((season or {}).get("dates") or [])
        months = season_months(dates) if dates else set()
        names = month_names(dates) if dates else []
        candidates = {
            place: self._place_photos.candidates(place, months=months, month_names=names) for place in places
        }
        candidates = {place: found for place, found in candidates.items() if found}
        if not candidates:
            return {}
        moments = {
            str(m.get("place") or "").strip(): f"{m.get('title')}: {m.get('text')}"
            for d in guide.get("days") or [] if isinstance(d, dict)
            for m in d.get("moments") or [] if isinstance(m, dict)
        }
        look = getattr(self._fast_provider, "look_at_images", None)
        try:
            if callable(look):
                # Dates are not enough (late February already looks like spring), so
                # Claude opens the top candidates and judges the pixels.
                candidates = {
                    place: [p for p in (self._place_photos.download(x) for x in found[:LOOK_CANDIDATES]) if p]
                    for place, found in candidates.items()
                }
                listing = [
                    {"place": place, "moment": moments.get(place, ""), "photos": [
                        {"i": i, "path": p.local_path, "title": p.title.removeprefix("File:"), "taken": p.taken[:10]}
                        for i, p in enumerate(found)
                    ]} for place, found in candidates.items() if found
                ]
                paths = [Path(x["path"]) for entry in listing for x in entry["photos"]]
                text = look(paths, system=load_prompt("guide_place_photos_look"), task=json.dumps({
                    "trip_dates": dates, "season": (season or {}).get("season"), "places": listing,
                }, ensure_ascii=False))
            else:
                text = self._fast_provider.complete(
                    [LLMMessage(role="user", content=json.dumps({
                        "trip": {k: (season or {}).get(k) for k in ("month", "season")}, "places": [
                        {"place": place, "moment": moments.get(place, ""), "candidates": [
                            {"i": i, "title": p.title.removeprefix("File:"), "description": p.description[:200],
                             "taken": p.taken}
                            for i, p in enumerate(found)
                        ]} for place, found in candidates.items()
                    ]}, ensure_ascii=False))],
                    system=load_prompt("guide_place_photos"), max_tokens=800, temperature=0.0,
                ).text
            picks = (extract_json_block(text) or {}).get("picks") or []
        except AgentCancelled:
            raise
        except Exception:
            logger.warning("Destination photo pick failed", exc_info=True)
            return {}
        rejected = [p.get("place") for p in picks if isinstance(p, dict) and str(p.get("pick")) == "-1"]
        if rejected:
            logger.info("No in-season photo for %s", rejected)
        chosen: dict[str, dict[str, Any]] = {}
        for pick in picks if isinstance(picks, list) else []:
            place = str((pick or {}).get("place") or "").strip() if isinstance(pick, dict) else ""
            found = candidates.get(place) or []
            try:
                index = int(pick.get("pick"))
            except (TypeError, ValueError):
                continue
            if not 0 <= index < len(found):
                continue
            photo = found[index] if found[index].local_path else self._place_photos.download(found[index])
            if photo:
                chosen[place] = {
                    "local_path": photo.local_path, "credit": photo.credit, "evidence": place,
                    "title": photo.title.removeprefix("File:"), "page_url": photo.page_url,
                    "artist": photo.artist, "licence": photo.licence, "licence_url": photo.licence_url,
                    "taken": photo.taken, "source": "wikimedia_commons",
                }
        return chosen

    def run(self, project_id: int, state: dict[str, Any], *, journey: bool = False) -> SideFlowResult:
        """Write the page from the thread's concept, draft and research; store it in its own key.

        Claude writes it (hotel material may be in the evidence), Python checks its shape,
        a fast-model check compares it with the plan, and one fix pass follows. The journey
        page (journey=True) is the denser format: a story intro, season tips and 4-8 moments
        a day; the first one for a thread runs one research round for cafés, bars, shopping
        and lunch spots, since a dense day needs more named places than the editorial plan has.
        """
        kind = JOURNEY if journey else GUIDE
        concept_state = dict(state.get("concept") or {})
        research = ResearchResult.from_dict(state.get("research") or {})
        if journey and not concept_state.get(JOURNEY.key):
            self._step("finding cafés, bars, shopping and lunch spots")
            try:
                research = self._host._journey_research(project_id, state, research)
            except AgentCancelled:
                raise
            except Exception:
                logger.warning("Journey research failed; writing from the evidence there is", exc_info=True)
        gated = self._host._gated_research(research)
        hotel = str(state.get("project", {}).get("hotel", ""))
        piece_format = dict(concept_state.get("format") or {})
        nights = int(piece_format.get("nights") or 0) or self._host._trip_nights or 3
        photos = {f"p{i}": asset for i, asset in enumerate(self._host._cleared_images(research), 1)}
        # Spaces closed on the trip dates never appear; outdoor ones only where the writer
        # places a moment in them, never to fill a gap.
        for key in self._host._closed_space_photos(photos, state):
            photos.pop(key, None)
        outdoor = {key for key, asset in photos.items() if is_outdoor(asset)}
        draft = concept_state.get("sample_draft") or {}
        concept = concept_state.get("editorial") or {}
        season = self._trip_season(hotel, piece_format)
        plan = {
            "concept": {k: concept.get(k) for k in (
                "working_title", "editorial_proposition", "strongest_experience", "possible_story_structure",
                "why_someone_would_want_it", "why_this_property_enables_it",
            )},
            "outline": concept_state.get("outline") or {},
            "draft": {k: draft.get(k) for k in ("headline", "standfirst", "body")} if draft.get("body") else {},
            "amendments": concept_state.get("amendments") or [],
        }
        focus = json.dumps(plan, ensure_ascii=False)
        payload = {
            "hotel": hotel, "format": {**piece_format, "nights": nights}, **plan,
            "stop_notes": concept_state.get("venues") or {},
            "season": season,
            "house_style": self._host._house_style_payload(hotel, "guide")["house_style"],
            "photos": [{"id": key, "shows": str(a.get("evidence") or a.get("page_context") or "")[:200],
                        "outdoor": key in outdoor}
                       for key, a in photos.items()],
            "evidence": compact_research_json(
                gated.to_dict(), budget_chars=28000 if journey else 20000, focus_text=focus,
            ),
        }
        hotel_facts = [
            fact_text(item) for item in gated.verified_facts
            if isinstance(item, dict) and item.get("evidence_level") == "hotel_document"
        ]
        if journey:
            payload["hotel_document_facts"] = hotel_facts[:60]
        self._step("writing the page")
        guide = self._write_guide(payload, kind)
        if not guide:
            return SideFlowResult(
                f"I couldn't write the {kind.label} this time. Please try again.",
            )
        fixes = kind.validate(guide, nights=nights)
        refuted: list[dict[str, str]] = []
        self._step("checking it against the plan")
        try:
            response = self._check_provider.complete(
                [LLMMessage(role="user", content=json.dumps({
                    "format": "journey" if journey else "guide",
                    "guide": guide, "plan": plan, "stop_notes": payload["stop_notes"], "season": season,
                    "house_style": payload["house_style"],
                    "evidence": compact_research_json(gated.to_dict(), budget_chars=14000, focus_text=focus),
                }, ensure_ascii=False))],
                system=load_prompt("guide_ko_check"), max_tokens=2000, temperature=0.0,
            )
            check = extract_json_block(response.text)
            if isinstance(check, dict) and str(check.get("verdict")).upper() == "FIX":
                suggested = [str(f) for f in check.get("fixes") or [] if str(f).strip()]
                if suggested and self._settings.verify_fixes:
                    # The check's fixes are tested from the other side; Python's shape fixes are not.
                    self._step("double-checking the suggested fixes")
                    verified = verify_fixes(
                        self._fast_provider, suggested, text=json.dumps(guide, ensure_ascii=False),
                        evidence=compact_research_json(gated.to_dict(), budget_chars=14000, focus_text=focus),
                        context={"plan": plan, "stop_notes": payload["stop_notes"], "season": season,
                                 "house_style": payload["house_style"]},
                    )
                    suggested, refuted = verified.kept, verified.refuted
                fixes += suggested
        except AgentCancelled:
            raise
        except Exception:
            logger.warning("Korean guide check failed", exc_info=True)
        applied = 0
        if fixes:
            self._step("applying the fixes")
            revised = self._write_guide({**payload, "previous": guide, "fixes": fixes}, kind)
            if revised and len(kind.validate(revised, nights=nights)) <= len(kind.validate(guide, nights=nights)):
                guide, applied = revised, len(fixes)
        tips_note = ""
        if journey and guide.get("season_tips_from_hotel") and not hotel_tip_quotes_ok(
            [str(q) for q in guide.get("hotel_tip_quotes") or []], hotel_facts,
        ):
            # Tips are credited to the hotel only when the hotel itself sent them.
            guide["season_tips_from_hotel"] = False
            tips_note = "season tips not credited to the hotel (no hotel document says them)"
        hotel_ids = list(photos)
        guide = dedupe_photos(guide)
        places = self._find_place_photos(guide, season)
        place_ids = {place: f"w{i}" for i, place in enumerate(places, 1)}
        photos.update({place_ids[place]: asset for place, asset in places.items()})
        guide = fill_empty_days(apply_place_photos(guide, place_ids), hotel_ids, outdoor=outdoor)
        remaining = kind.validate(guide, nights=nights)
        checks = guide_check_items(guide)
        out_dir = (
            self._settings.artifacts_path / ArtifactManager.safe_slug(hotel or "hotel") / "guides"
            / time.strftime("%Y%m%d-%H%M%S")
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        slug = ArtifactManager.safe_slug(hotel or "hotel")
        suffix = "hotel-journey-ko" if journey else "hotel-guide-ko"
        html_path = out_dir / f"{slug}-{suffix}.html"
        json_path = out_dir / f"{slug}-{suffix}.json"
        html_path.write_text(kind.render(
            guide, photos=photos, hotel=hotel,
            note="검토용 초안 · [확인 필요] 표시는 호텔 확인 후 수정 · 호텔 사진: 호텔 제공 이미지"
                 + (" · 관광지 사진: Wikimedia Commons (각 사진에 저작자·라이선스 표기)" if places else ""),
        ), encoding="utf-8")
        credits = {
            place_ids[place]: {k: a.get(k, "") for k in ("title", "page_url", "artist", "licence", "licence_url", "taken")} | {"place": place}
            for place, a in places.items()
        }
        json_path.write_text(json.dumps(
            {"guide": guide, "jsonld": guide_jsonld(guide), "photo_credits": credits}, ensure_ascii=False, indent=2,
        ), encoding="utf-8")
        concept_state[kind.key] = {
            "guide": guide, "html": str(html_path), "json": str(json_path), "fixes_applied": applied,
            "open_issues": remaining, "checks": checks, "nights": nights, "photo_credits": credits,
            "refuted_fixes": refuted,
            "season": season, "style": "journey" if journey else "guide",
        }
        self._db.update_project_state(project_id, "concept", concept_state)
        used = {str(guide.get("hero_photo") or "")} | {
            str(x.get("photo") or "") for d in guide.get("days") or [] if isinstance(d, dict)
            for x in [d, *[m for m in d.get("moments") or [] if isinstance(m, dict)]]
        }
        moments = sum(len(d.get("moments") or []) for d in guide.get("days") or [] if isinstance(d, dict))
        lines = [f"*{guide.get('title')}*", str(guide.get("summary") or guide.get("criteria_line") or "")]
        notes = [
            f"• {len(guide.get('days') or [])} days" + (f", {moments} moments" if journey else "")
            + f", {len(used & set(hotel_ids))} hotel photos placed"
            + (f", {len(used & set(place_ids.values()))} destination photos from Wikimedia Commons "
               "(free licences, credited on the page)" if places else ""),
            f"• Check: {'passed' if not fixes else f'{applied} fixes applied'}"
            + (f"; still open: {'; '.join(remaining[:3])}" if remaining else "")
            + (f"; {len(refuted)} suggested fix{'es' if len(refuted) > 1 else ''} dropped after a second check"
               if refuted else ""),
        ]
        if season.get("month"):
            sunsets = [d["sunset"] for d in season.get("daylight") or []]
            typical = season.get("typical") or {}
            notes.append(
                f"• Timed for {season['month']} ({season.get('season')})"
                + (f": sunset about {sunsets[0]}–{sunsets[-1]}" if sunsets else "")
                + (f", typically {typical['low_c']}–{typical['high_c']}°C" if typical else "")
            )
        if checks:
            notes.append(f"• {len(checks)} [확인 필요] items to confirm with the hotel")
        if journey:
            notes.append(
                "• Season tips credited to the hotel (from its own documents)" if guide.get("season_tips_from_hotel")
                else f"• Season tips not credited to the hotel{' (the hotel sent no such tips)' if tips_note else ''}"
            )
        lines.append("\n".join(notes))
        lines.append(
            f"_{kind.label} below: HTML in the site's {'hotel-journey' if journey else 'itinerary'} layout and JSON "
            "for the site. The English draft and layouts in this thread are unchanged._"
        )
        return SideFlowResult(
            "\n\n".join(l for l in lines if l), 
            attachments=[str(html_path), str(json_path)],
        )
