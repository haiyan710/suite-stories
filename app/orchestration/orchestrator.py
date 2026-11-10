"""Deterministic coordinator for the Suite Stories specialist agents."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Sequence

from ..agent import AgentCancelled, AgentError
from ..agents import BuilderAgent, CriticAgent, DesignAgent, EditorialAgent, ResearchAgent, WriterAgent
from ..agents.logistics import LogisticsAgent, LogisticsTask
from ..artifacts import ArtifactManager
from ..artifacts.layout import render_editorial_html, split_sections
from ..config import Settings
from ..llm.base import LLMError, LLMMessage, LLMProvider
from ..llm.zai_glm import extract_json_block
from ..prompts import load_prompt
from ..storage import ConceptRecord, Database, SessionRecord
from ..tools.documents import Document
from ..tools.geo import GeoClient
from ..tools.web import BRAND, DESTINATION, EDITORIAL_PRESS, PUBLICATION, OFFICIAL
from .calendar import TripCalendar, parse_trip_calendar
from .draft_checks import apply_calendar, check_draft
from .handoffs import (
    ArtifactResult, BuildTask, CritiqueResult, CritiqueTask, DesignSpec, DesignTask, DraftResult, DraftTask,
    EditorialResult, EditorialTask, ResearchResult, ResearchTask,
)
from .intents import Intent, VALID_INTENTS
from .planning import (
    EDITORIAL_ALLOWED_CONFIDENCE, OrchestrationPlan, PlanStep, ResponseMode,
    SpecialistEvaluation, SpecialistTaskBrief, compact_research_json, evidence_confidence, fact_text,
    focus_names, split_evidence,
)

logger = logging.getLogger(__name__)
ProgressFn = Callable[[str], None]
# Progress events with this prefix carry a ready-to-show step description.
STEP_PROGRESS_PREFIX = "step:"
MAX_TARGETED_RESEARCH_ROUNDS = 2
MAX_EDITORIAL_ATTEMPTS = 2
MAX_VISUAL_REVISION_CYCLES = 2
MAX_DRAFT_FIX_ROUNDS = 2


@dataclass
class OrchestratorOutcome:
    markdown: str
    clarification: str | None = None
    project_id: int | None = None
    action: str = ""
    artifact_path: str = ""
    # Local files (e.g. a layout PDF) the Slack layer uploads after the text reply.
    attachments: list[str] = field(default_factory=list)


class EditorialOrchestrator:
    """The only conversational brain exposed to Slack."""

    def __init__(
        self, settings: Settings, provider: LLMProvider, db: Database, *,
        researcher: ResearchAgent | None = None,
        editorial: EditorialAgent | None = None,
        designer: DesignAgent | None = None,
        builder: BuilderAgent | None = None,
        critic: CriticAgent | None = None,
        artifacts: ArtifactManager | None = None,
        research_provider: LLMProvider | None = None,
        writer: WriterAgent | None = None,
        writer_provider: LLMProvider | None = None,
        private_writer: WriterAgent | None = None,
        logistics: LogisticsAgent | None = None,
    ) -> None:
        self._settings = settings
        self._provider = provider
        self._db = db
        self._artifacts = artifacts or ArtifactManager(settings.artifacts_path, db)
        self._researcher = researcher or ResearchAgent(settings, research_provider or provider, db)
        # Cheap, fast model for condensing results into the Slack reply.
        self._fast_provider = research_provider or provider
        self._editorial = editorial or EditorialAgent(provider)
        # Sample drafts: the writer may be a secondary model (GLM) that only sees public
        # evidence; concepts built on hotel-shared documents use the Claude writer.
        self._writer = writer or WriterAgent(writer_provider or provider)
        self._private_writer = private_writer or WriterAgent(provider)
        # Removes AI-writing tells from each draft; Claude, so private drafts may use it.
        self._humanizer = WriterAgent(self._fast_provider)
        # Itinerary travel times from free OpenStreetMap services; off in hand-built test settings.
        self._logistics = logistics or (
            LogisticsAgent(self._fast_provider, GeoClient(
                photon_url=settings.photon_url, osrm_url=settings.osrm_url, db=db,
            )) if settings.logistics_check else None
        )
        self._designer = designer or DesignAgent(provider)
        self._builder = builder or BuilderAgent(provider, self._artifacts)
        self._critic = critic or CriticAgent(provider)
        self._progress_callbacks: list[ProgressFn] = []
        self._cancel_check: Callable[[], bool] = lambda: False
        self.last_trace: list[str] = []
        self.last_plan: OrchestrationPlan | None = None
        self.last_briefs: list[SpecialistTaskBrief] = []
        self._active_thread_key = ""
        self._active_goal = ""
        self._response_mode = ResponseMode.CONVERSATIONAL.value
        self._layout_photo_count = 0
        self._trip_nights = 0
        self._trip_nights_explicit = 0
        self._segment = ""
        # The trip's real dates, from the request or the stored concept (see calendar.py).
        self._calendar: TripCalendar | None = None
        self._calendar_from_request = False
        self._today = None  # tests pin "today" so relative dates stay stable
        self._dining_note = ""
        self._test_run = False
        self._targeted_research_rounds_used = 0

    def add_progress_listener(self, callback: ProgressFn) -> None:
        self._progress_callbacks.append(callback)

    def remove_progress_listener(self, callback: ProgressFn) -> None:
        try:
            self._progress_callbacks.remove(callback)
        except ValueError:
            pass

    @staticmethod
    def _is_sample_draft_request(value: str) -> bool:
        normalized = re.sub(r"\s+", " ", value.lower())
        return bool(re.search(
            r"\bsample (draft|article|piece|editorial|copy)\b"
            r"|\bwrite (a |the |this |it |up )?(sample )?(draft|piece|article)\b"
            r"|\bdraft (a|the|this) (piece|article)\b"
            r"|viết (bài|nháp)|bài mẫu",
            normalized,
        ))

    # Traveller types from the platform's editorial briefs:
    # (label, activities the plan should favour, the vocabulary of their voice, trigger words).
    TRAVELLER_SEGMENTS: dict[str, tuple[str, str, str, str]] = {
        "families": ("families", "kids' clubs, connecting rooms, pools, easy pacing and early dinners",
                     "together, discovery, wonder, easy, safe, shared memories, the little ones",
                     r"\bfamil|\bkids?\b|children|gia đình|trẻ em|가족"),
        "couples": ("couples and honeymooners",
                    "time alone together: suites, private dining, couples' spa, sunset and night views, quiet "
                    "tables for two, unhurried mornings; avoid crowded group sightseeing at peak hours",
                    "romantic, together, just the two of you, first (first trip as a married couple, first "
                    "sunset), fresh start, love, celebrate, intimate, quiet, unhurried, toast",
                    r"couple|honeymoon|anniversar|romantic|cặp đôi|trăng mật|kỷ niệm|커플|신혼"),
        "slow": ("travellers in their 50s and 60s", "a slower pace, culture, comfort and short transfers",
                 "unhurried, comfortable, considered, savour, at ease, well looked after",
                 r"\b[56]0s\b|senior|older travel|slow travel|người lớn tuổi|trung niên|시니어"),
        "wellness": ("wellness travellers", "spa, retreats, programmes, rest and early mornings",
                     "restore, breathe, balance, renew, calm, reset",
                     r"wellness|retreat|yoga|detox|chăm sóc sức khỏe|웰니스"),
        "design": ("design and culture lovers", "architecture, art, heritage and the hotel's own story",
                   "craft, light, material, detail, story, made by hand",
                   r"design lover|architecture|\bart lover|heritage|kiến trúc|nghệ thuật|디자인"),
        "golf": ("golfers", "courses, tee times and golf-trip pacing",
                 "the round, the course, early tee time, unwind after the 18th", r"\bgolf|골프"),
        "gastronomy": ("food-led travellers", "signature restaurants, chef's tables and markets",
                       "flavour, the table, chef, discovery, appetite, the first bite",
                       r"foodie|food lover|gastronom|culinary|ẩm thực|미식"),
        # One piece for every kind of reader, or a party that mixes them: the hotel is the shared
        # base and each day gives each group its own pace (a client's favourite version did this).
        "mixed": ("a mixed party: grandparents, couples, siblings and friends, and solo travellers",
                  "a shared base and shared meals, with each day split by pace: rest, short walks and the "
                  "hotel's lounges for grandparents; fuller days out for the walkers; a special dinner for "
                  "couples; a walkable city for solo travellers",
                  "together, at your own pace, everyone, come back to the same table, a room of your own",
                  r"\ball\b[\w\s-]{0,25}?(?:traveller|traveler|reader|customer|guest|audience)s?\s*"
                  r"(?:groups|types|segments)|every kind of (?:\w+ )?travell?er|mixed (?:party|group)|multi-?generation|"
                  r"tất cả các nhóm|mọi nhóm khách|모든 고객|모든 여행자"),
    }
    MIXED_LANES = "**For grandparents ·**, **For the walkers ·**, **For couples ·**, **For solo travellers ·**"

    @classmethod
    def _parse_segment(cls, value: str) -> str:
        """The traveller type a request names ("for a honeymoon couple", "cho gia đình"), else ''."""
        text = value.lower()
        matches = [
            key for key, (_label, _focus, _voice, pattern) in cls.TRAVELLER_SEGMENTS.items()
            if re.search(pattern, text)
        ]
        if "mixed" in matches or len(matches) > 1:
            # "for families, couples and solo travellers" is a mixed party, not the first one named.
            return "mixed"
        return matches[0] if matches else ""

    def _trip_format(self) -> dict[str, Any]:
        if not self._trip_nights:
            return {}
        piece: dict[str, Any] = {"type": "itinerary", "nights": self._trip_nights}
        if self._segment:
            piece.update(self._segment_format(self._segment))
        calendar = self._calendar_for(self._trip_nights)
        if calendar:
            piece["calendar"] = calendar.to_dict()
        return piece

    def _calendar_for(self, nights: int) -> TripCalendar | None:
        """The active calendar, stretched or cut to the piece's length."""
        if self._calendar is None or not nights:
            return None
        return self._calendar if self._calendar.nights == nights else self._calendar.with_nights(nights)

    def _resolve_calendar(self, user_text: str, project_state: dict[str, Any]) -> None:
        """Dates from this message, else the calendar stored with the thread's concept."""
        stored_format = (project_state.get("concept") or {}).get("format") or {}
        nights = self._trip_nights_explicit or int(stored_format.get("nights") or 0) or self._trip_nights
        parsed = parse_trip_calendar(
            user_text, nights=nights, nights_explicit=bool(self._trip_nights_explicit), today=self._today,
        )
        self._calendar_from_request = parsed is not None
        self._calendar = parsed or TripCalendar.from_dict(stored_format.get("calendar"))
        if parsed and parsed.from_range and not self._trip_nights_explicit:
            # "6 to 10 February" fixes the length as surely as "4 nights".
            self._trip_nights_explicit = self._trip_nights = parsed.nights

    @classmethod
    def _segment_format(cls, segment: str) -> dict[str, str]:
        label, focus, voice, _pattern = cls.TRAVELLER_SEGMENTS[segment]
        piece = {
            "segment": label, "segment_activities": focus, "segment_voice": voice,
            "tone_mix": f"about 60% informative, 40% written for {label} in their voice",
        }
        if segment == "mixed":
            piece["segment_lanes"] = cls.MIXED_LANES
        return piece

    @staticmethod
    def _parse_trip_nights(value: str) -> int:
        """Trip length asked for in the message ("4-night", "4 đêm", "5 days" -> 4), else 0."""
        text = value.lower()
        nights = re.search(r"\b(\d{1,2})\s*-?\s*(?:nights?|đêm)\b", text)
        if nights:
            count = int(nights.group(1))
        else:
            days = re.search(r"\b(\d{1,2})\s*-?\s*(?:days?|ngày)\b", text)
            count = int(days.group(1)) - 1 if days else 0
        return count if 2 <= count <= 14 else 0

    _HOTEL_REPLY = re.compile(
        r"^\s*(?:<@[^>]+>\s*)?(?:hotel (?:confirmed|replied|says)|(?:the )?hotel's (?:reply|answer)"
        r"|khách sạn (?:xác nhận|trả lời))\s*[:：]\s*(.+)$",
        re.I | re.S,
    )

    @classmethod
    def _pasted_hotel_reply(cls, value: str, thread_key: str) -> Document | None:
        """'Hotel confirmed: ...' pasted in a thread becomes hotel-supplied evidence."""
        match = cls._HOTEL_REPLY.match(value)
        if not match or len(match.group(1).strip()) < 10:
            return None
        stamp = int(time.time())
        return Document(
            file_id=f"hotel-reply-{stamp}", name="Hotel reply (pasted)",
            url=f"slack://{thread_key}/hotel-reply-{stamp}", text=match.group(1).strip(),
        )

    @staticmethod
    def _is_test_marker(value: str) -> bool:
        """'[test]' in a thread keeps its concepts out of the anti-repetition memory."""
        return "[test]" in value.lower()

    @staticmethod
    def _wants_fresh_research(value: str) -> bool:
        return bool(re.search(
            r"\b(fresh research|from scratch|re-?research|research again|ignore (the )?cache|new research)\b",
            value.lower(),
        ))

    def _research_from_cache(self, hotel: str) -> tuple[ResearchResult, int] | None:
        """Rebuild a research package from the per-hotel fact cache, or None if it is too thin.

        Cached facts keep their confidence and source; each source gets a fresh S# for
        this project. Facts are marked `cached_from` so saving them does not re-date them.
        """
        days = self._settings.fact_cache_days
        if days <= 0:
            return None
        rows = self._db.cached_hotel_facts(hotel, max_age_days=days)
        public_official = any(
            row["source_type"] in {OFFICIAL, BRAND} and not row["private"] for row in rows
        )
        if len(rows) < self._settings.fact_cache_min_facts or not public_official:
            return None
        # Property facts (official/brand pages, hotel documents) first, then the rest;
        # newest first within each group. A pure recency cut kept the latest venue and
        # itinerary facts and dropped the hotel's own core facts.
        # Guest reports get their own share: ranked with "the rest" they fell past the
        # cut, so every cached thread re-ran the guest-experience research.
        prop = [r for r in rows if r["source_type"] in {OFFICIAL, BRAND} or r["evidence_level"] == "hotel_document"]
        guest = [r for r in rows if r not in prop and r["evidence_level"] == "guest_report"]
        rest = [r for r in rows if r not in prop and r not in guest]
        chosen = prop[:240] + guest[:100]
        ranked = chosen + rest[:max(0, 400 - len(chosen))]
        records: dict[str, dict[str, Any]] = {}
        facts: list[dict[str, Any]] = []
        for row in ranked:
            record = records.get(row["source_url"])
            if record is None:
                record = {
                    "sid": f"S{len(records) + 1}", "url": row["source_url"], "title": row["source_title"],
                    "source_type": row["source_type"], "confidence": row["confidence"], "fetched_ok": True,
                    "private": bool(row["private"]), "facts": [],
                }
                records[row["source_url"]] = record
            fact = {
                "fact": row["fact"], "dimension": row["dimension"], "source_ids": [record["sid"]],
                "confidence": row["confidence"], "evidence_level": row["evidence_level"],
                "cached_from": row["retrieved_at"],
            }
            record["facts"].append(fact)
            facts.append(fact)
        newest = str(rows[0]["retrieved_at"])[:10]
        try:
            age_days = (time.time() - time.mktime(time.strptime(newest, "%Y-%m-%d"))) // 86400
        except ValueError:
            age_days = 0
        research = ResearchResult(
            status="complete", property_identity={"hotel": hotel}, verified_facts=facts,
            source_records=list(records.values()),
            classification=f"Reused {len(facts)} verified facts from the hotel fact cache.",
        )
        return research, max(int(age_days), 0)

    def _itinerary_research(
        self, project_id: int, state: dict[str, Any], research: ResearchResult,
    ) -> ResearchResult:
        project = state.get("project") or {}
        hotel = str(project.get("hotel", ""))
        identity = research.property_identity if isinstance(research.property_identity, dict) else {}
        city = str(project.get("city") or identity.get("city") or f"the city around {hotel}")
        # Destination guides, not the hotel's own pages: chain sites are often blocked
        # and never list the outside venues a day-by-day plan needs.
        who = f" suited to {self.TRAVELLER_SEGMENTS[self._segment][0]}" if self._segment else ""
        questions = [
            f"Restaurants and cafés within walking distance of {hotel}{who}: names, cuisine, reservations",
            f"Destination restaurants in {city} worth a special dinner (Michelin Guide, awards), with their area",
            f"Main museums, temples and sights in {city}{who}: opening days, hours and booking rules",
            f"Neighbourhood walks, markets and shopping near {hotel}{who}",
            f"Day trips or half-day excursions from {city} suited to a {self._trip_nights}-night stay",
            f"Getting around {city} from {hotel}: transit lines and airport transfer",
        ]
        research = self._call(
            "researcher", self._researcher.run,
            ResearchTask(
                project_id=project_id, hotel_name=hotel, original_request=self._active_goal,
                followup_questions=questions, existing_research=research.to_dict(),
                trip_nights=self._trip_nights, destination=True,
            ), project_id, step_id="research_itinerary",
        )
        self._db.update_project_state(project_id, "research", research.to_dict())
        return research

    def _needs_experience_research(self, research: ResearchResult) -> bool:
        return self._settings.experience_research and not any(
            isinstance(item, dict) and item.get("evidence_level") == "guest_report"
            for item in research.verified_facts
        )

    def _experience_research(
        self, project_id: int, state: dict[str, Any], research: ResearchResult,
    ) -> ResearchResult:
        """What staying there is like: guest reviews, insider tips, stories, peer comparisons.

        Official pages and guides give the brochure; the detail that makes a careful
        traveller book (how the concierge helped, which room to ask for, the hotel's
        history) comes from people who stayed. Facts are tagged `guest_report`.
        """
        project = state.get("project") or {}
        hotel = str(project.get("hotel", ""))
        identity = research.property_identity if isinstance(research.property_identity, dict) else {}
        city = str(project.get("city") or identity.get("city") or "the same city")
        questions = [
            f"Guest reviews of a stay at {hotel}: service, staff, concierge, housekeeping, breakfast, rooms",
            f"Insider tips for staying at {hotel}: which room or view to ask for, best time for the pool, spa or breakfast, what not to miss",
            f"History and stories of {hotel}: famous guests, films, art, collections, rankings and awards",
            f"{hotel} compared with other luxury hotels in {city}",
            f"{hotel} 후기",
        ]
        research = self._call(
            "researcher", self._researcher.run,
            ResearchTask(
                project_id=project_id, hotel_name=hotel, original_request=self._active_goal,
                followup_questions=questions, existing_research=research.to_dict(), experience=True,
            ), project_id, step_id="research_experience",
        )
        self._db.update_project_state(project_id, "research", research.to_dict())
        return research

    @classmethod
    def _itinerary_instruction(cls, nights: int, segment: str = "") -> str:
        days = nights + 1
        if segment:
            label, focus, voice, _pattern = cls.TRAVELLER_SEGMENTS[segment]
            traveller = (
                f"TRAVELLER: plan this trip for {label}. Every day's activities must suit them: favour {focus}. "
                "Choose the room, venues, times of day and pacing for them, frame each moment as what it means "
                f"for them (not a list of sights), and make why_someone_would_want_it about them. Their voice "
                f"draws on words like: {voice}. "
            )
        else:
            traveller = (
                "TRAVELLER: pick the one traveller type this hotel suits best (families; couples and "
                "honeymooners; travellers in their 50s and 60s; wellness; design and culture; golf; "
                "food-led) and plan for them; name it in why_someone_would_want_it. "
            )
        return traveller + (
            f"FORMAT: a hotel recommendation with a {nights}-night ({days}-day) itinerary, the way a travel "
            "advisor would plan the trip and make the case for booking this hotel. READER: Korean luxury "
            "travellers who compare their options closely and book when they can see why a hotel fits the trip; "
            "they want the practical, property-specific detail a listing or brochure leaves out, so they "
            "arrive with the right expectations. This is promotional editorial: present the hotel at its "
            "best and leave out drawbacks (an inconvenience may appear only as a helpful tip, e.g. how "
            "guests get around). Use guest reports for service texture, insider tips and the hotel's "
            "stories (history, famous guests, collections, rankings), attributed. Name the room type to "
            "book when the evidence supports one. The spine is the booking "
            "case, not a single niche experience: who this hotel is right for, and the 2-3 verified, "
            "property-specific reasons it is worth booking over other luxury hotels in the city (location, "
            "rooms, food, wellness, views, service). Put the booking case in editorial_proposition and "
            "why_this_property_enables_it, the ideal guest and their reason in why_someone_would_want_it, and "
            "the stay as a whole in strongest_experience. Lay the trip out day by day in possible_story_structure "
            f"('Day 1: ...' to 'Day {days}: ...'), each day with morning, afternoon and evening. The hotel must be "
            "part of every day (where the day starts, returns, eats or ends). Name outside restaurants, sights or "
            "day trips only when they are in the evidence; where a day needs one the evidence lacks, write "
            "[TK: kind of venue needed] instead of naming one. Use the destination evidence fully: each day names "
            "specific restaurants, sights or areas from it with a reason they fit, since a day that is mostly "
            "[TK] is not a usable plan. Each day has its own character and one highlight the others do not; "
            "never build the plan around a repeated routine (e.g. 'every day goes out and comes back the same "
            "way'). Vary the days (pace, neighbourhood, one slower "
            "day) and make arrival and departure days realistic. Build the case on facts that hold today "
            "(rooms, location, restaurants and facilities); keep an unconfirmed programme (a tour, a class, a "
            "seasonal offer) optional so the plan still works without it."
        )

    @staticmethod
    def _is_layout_request(value: str) -> bool:
        normalized = re.sub(r"\s+", " ", value.lower())
        return bool(re.search(
            r"\blayout\b|\bpdf\b|with (photos|images|pictures)|có ảnh|bố cục|dàn trang", normalized,
        ))

    @staticmethod
    def _uses_private_documents(research: ResearchResult) -> bool:
        return any(
            record.get("private") or str(record.get("title", "")).endswith("(shared in Slack)")
            for record in research.source_records if isinstance(record, dict)
        )

    def _draft_flow(self, project_id: int, state: dict[str, Any], request: str) -> OrchestratorOutcome:
        """Writer drafts a sample article from the concept + outline; the critic QA's it (one revision)."""
        concept_state = dict(state.get("concept") or {})
        concept = concept_state.get("editorial") or {}
        if not concept or concept_state.get("status") not in {"shortlisted", "approved", "published"}:
            return OrchestratorOutcome(
                "There's no shortlisted concept in this thread to draft from yet.", project_id=project_id,
            )
        research = ResearchResult.from_dict(state.get("research") or {})
        gated = self._gated_research(research)
        concept_critique = CritiqueResult(**{
            k: v for k, v in (concept_state.get("critic_result") or {"verdict": "PASS", "reasons": []}).items()
            if k in CritiqueResult.__dataclass_fields__
        })
        outline = concept_state.get("outline") or self._editorial_outline(
            EditorialResult.from_dict(concept), concept_critique, research,
        ) or {}
        private = self._uses_private_documents(research)
        saved = dict(concept_state.get("sample_draft") or {})
        if (
            saved.get("body") and self._is_layout_request(request)
            and not self._is_sample_draft_request(request) and not self._trip_nights_explicit
        ):
            # "Make the layout" for an existing draft lays out that draft; re-drafting
            # took minutes and replaced the text the user had just read.
            draft = DraftResult(**{k: v for k, v in saved.items() if k in DraftResult.__dataclass_fields__})
            critique = CritiqueResult(**{
                k: v for k, v in (saved.get("qa") or {"verdict": "PASS", "reasons": []}).items()
                if k in CritiqueResult.__dataclass_fields__
            })
            try:
                page = self._build_layout(project_id, state, research, draft, critique)
            except Exception:
                logger.exception("layout build failed")
                return OrchestratorOutcome(
                    "I couldn't build the layout this time. Please try again.", project_id=project_id,
                )
            concept_state["sample_draft"] = {**saved, "layout_html": str(page)}
            self._db.update_project_state(project_id, "concept", concept_state)
            return OrchestratorOutcome(
                self._format_draft_with_layout(
                    draft, critique, private=bool(saved.get("private_sources")), photos=self._layout_photo_count,
                    fixes=saved.get("qa_fixes"),
                ),
                project_id=project_id, attachments=[str(page)],
            )
        writer = self._private_writer if private else self._writer
        piece_format = dict(concept_state.get("format") or {})
        if self._trip_nights_explicit:
            piece_format = {"type": "itinerary", "nights": self._trip_nights_explicit}
        if self._segment and piece_format.get("type") == "itinerary":
            piece_format.update(self._segment_format(self._segment))
        calendar = (
            self._calendar_for(int(piece_format.get("nights") or 0))
            if piece_format.get("type") == "itinerary" else None
        )
        if calendar:
            piece_format["calendar"] = calendar.to_dict()
            if self._calendar_from_request:
                # A corrected calendar replaces the stored one for every later step.
                concept_state["format"] = {**dict(concept_state.get("format") or {}), "calendar": calendar.to_dict()}
        amendments = list(concept_state.get("amendments") or [])
        amendment = self._amendment_from(request)
        if amendment and amendment not in amendments:
            amendments.append(amendment)
            concept_state["amendments"] = amendments
        logistics = dict(concept_state.get("logistics") or {})
        if not logistics and piece_format.get("type") == "itinerary":
            # Concepts shortlisted before the check existed, or when it was unavailable.
            logistics = self._check_logistics(
                project_id, str(state.get("project", {}).get("hotel", "")),
                {"outline": outline, "concept": {"possible_story_structure": concept.get("possible_story_structure")}},
            )
            if logistics:
                concept_state["logistics"] = logistics
        if logistics:
            # Refresh the reader-facing summary too: the critic reads it, and a re-check can change it.
            outline = {
                **outline, "getting_around": self._getting_around(logistics),
                "travel_estimates": logistics.get("lines", []), "travel_fixes": logistics.get("issues", []),
            }
        if calendar:
            outline = {**outline, "calendar": calendar.lines()}
        if amendments:
            outline = {**outline, "user_amendments": amendments}
        hotel_name = str(state.get("project", {}).get("hotel", ""))
        outline = {**outline, **self._house_style_payload(hotel_name)}
        venues = dict(concept_state.get("venues") or {})
        if not venues and piece_format.get("type") == "itinerary":
            venues = self._venue_notes(project_id, hotel_name, concept, research)
            if venues:
                concept_state["venues"] = venues
        if venues:
            outline = {**outline, "venues": venues}
        draft: DraftResult | None = None
        critique: Any = None
        qa_revisions: list[str] = []
        for attempt in (1, 2):
            draft = self._call("writer", writer.run, DraftTask(
                project_id=project_id, hotel_name=str(state.get("project", {}).get("hotel", "")),
                user_request=request, concept=concept, outline=outline, research=gated.to_dict(),
                concept_revisions=list(concept_critique.revision_instructions or []),
                previous_draft=draft.to_dict() if draft and attempt > 1 else {},
                qa_revisions=qa_revisions, format=piece_format,
            ), project_id, step_id=f"writer_{attempt:02d}")
            if draft.status != "complete":
                return OrchestratorOutcome(
                    "I couldn't produce a usable sample draft this time. Please try again.", project_id=project_id,
                )
            if self._settings.humanize_drafts:
                # Before QA, so the critic checks the text the team will read.
                draft = self._call(
                    "humanizer", lambda d: self._humanizer.humanize(d, piece_format=piece_format),
                    draft, project_id, step_id=f"humanizer_{attempt:02d}",
                )
            draft = self._calendar_fixed(self._pin_headline(draft, outline, qa_revisions), calendar)
            critique = self._call("critic_draft", self._critic.run, CritiqueTask(
                project_id=project_id, mode="DRAFT", concept=concept, research=gated.to_dict(),
                outline=outline, draft=draft.to_dict(),
            ), project_id, step_id=f"critic_draft_{attempt:02d}")
            critique = self._with_draft_checks(critique, draft, calendar, venues)
            if critique.verdict == "PASS" or not critique.revision_instructions:
                break
            qa_revisions = list(critique.revision_instructions)
        fixes: dict[str, Any] = {}
        for fix_round in range(1, MAX_DRAFT_FIX_ROUNDS + 1):
            # The last QA round's instructions would otherwise never reach the text; its
            # wording polish is applied too, so the team gets the polished version. A later
            # round runs only for substantive fixes the re-check found, never for polish alone.
            instructions = list(critique.revision_instructions) if critique.verdict == "PASS_WITH_REVISIONS" else []
            if fix_round > 1 and not instructions:
                break
            instructions += list(getattr(critique, "polish", None) or []) if critique.verdict != "FAIL" else []
            if not instructions:
                break
            suffix = "" if fix_round == 1 else f"_{fix_round:02d}"
            draft, applied, skipped = self._call(
                "draft_fixes", lambda d: self._humanizer.apply_fixes(d, instructions), draft, project_id,
                step_id=f"draft_fixes{suffix}",
            )
            draft = self._calendar_fixed(self._pin_headline(draft, outline, instructions), calendar)
            fixes = {
                "applied": fixes.get("applied", 0) + applied,
                "total": fixes.get("total", 0) + len(instructions),
                "skipped": skipped, "rounds": fix_round,
            }
            if not applied:
                break
            # One more check so the reply's verdict describes the text the team receives.
            critique = self._call("critic_draft", self._critic.run, CritiqueTask(
                project_id=project_id, mode="DRAFT", concept=concept, research=gated.to_dict(),
                outline=outline, draft=draft.to_dict(),
            ), project_id, step_id=f"critic_draft_final{suffix}")
            critique = self._with_draft_checks(critique, draft, calendar, venues)
        polish = list(getattr(critique, "polish", None) or [])
        if fixes.get("applied") and polish and critique.verdict != "FAIL":
            # Every re-check finds a little new polish; apply the last one without another
            # check (edits are exact, unique and add no facts) so the loop ends here.
            draft, polished, polish_skipped = self._call(
                "draft_fixes", lambda d: self._humanizer.apply_fixes(d, polish), draft, project_id,
                step_id="draft_polish",
            )
            draft = self._calendar_fixed(self._pin_headline(draft, outline, polish), calendar)
            if polished:
                fixes["polished"] = polished
                critique = replace(critique, polish=list(polish_skipped))
        if outline:
            concept_state["outline"] = {
                k: v for k, v in outline.items()
                if k not in {
                    "travel_estimates", "travel_fixes", "calendar", "user_amendments", "house_style", "contacts", "venues",
                }
            }
        concept_state["sample_draft"] = {
            **draft.to_dict(), "qa": critique.to_dict(), "qa_fixes": fixes, "private_sources": private,
        }
        attachments: list[str] = []
        text = self._format_draft(draft, critique, private=private, fixes=fixes)
        if self._is_layout_request(request):
            try:
                page = self._build_layout(project_id, state, research, draft, critique)
                attachments.append(str(page))
                concept_state["sample_draft"]["layout_html"] = str(page)
                text = self._format_draft_with_layout(
                    draft, critique, private=private, photos=self._layout_photo_count, fixes=fixes,
                )
            except Exception:
                logger.exception("layout build failed")
                text += "\n\n_I couldn't build the layout this time; the draft text is above._"
        self._db.update_project_state(project_id, "concept", concept_state)
        return OrchestratorOutcome(text, project_id=project_id, attachments=attachments)

    @staticmethod
    def _calendar_fixed(draft: DraftResult, calendar: TripCalendar | None) -> DraftResult:
        """Real dates in the day headings and the right weekday in each day's [TK] notes."""
        body = apply_calendar(draft.body, calendar)
        if body == draft.body:
            return draft
        return replace(draft, body=body, tk_items=re.findall(r"\[TK[^\]]*\]", body))

    def _with_draft_checks(
        self, critique: Any, draft: DraftResult, calendar: TripCalendar | None, venues: dict[str, Any] | None = None,
    ) -> Any:
        """Add what Python can see (dates, house style, travel times) to the critic's fixes."""
        issues = check_draft(
            draft.body, headline=draft.headline, standfirst=draft.standfirst, calendar=calendar,
            banned_phrases=self._banned_phrases(), venues=venues,
        )
        new = [issue for issue in issues if issue not in (critique.revision_instructions or [])]
        if not new:
            return critique
        verdict = "PASS_WITH_REVISIONS" if critique.verdict == "PASS" else critique.verdict
        return replace(critique, verdict=verdict, revision_instructions=[*(critique.revision_instructions or []), *new])

    @staticmethod
    def _hotel_photo_lines(research: ResearchResult, limit: int = 24) -> list[str]:
        """Short descriptions of the hotel-supplied photos, so the plan can use what will be shown."""
        lines = []
        for asset in research.available_assets:
            if isinstance(asset, dict) and asset.get("rights") == "hotel_supplied":
                text = " ".join(str(asset.get("evidence") or asset.get("page_context") or "").split())
                if text and text not in lines:
                    lines.append(text[:140])
        return lines[:limit]

    def _banned_phrases(self) -> list[str]:
        rows = [str(r["rule"]) for r in self._db.style_rules() if r["kind"] == "banned"]
        return list(dict.fromkeys([*rows, *self._settings.banned_phrases]))

    def _house_style_payload(self, hotel: str) -> dict[str, Any]:
        """House rules, banned phrases and who to ask, for the outline, writer and draft critic."""
        rules = [str(r["rule"]) for r in self._db.style_rules() if r["kind"] != "banned"]
        banned = self._banned_phrases()
        if banned:
            rules.append("Never write: " + "; ".join(f'"{b}"' for b in banned) + ".")
        payload: dict[str, Any] = {"house_style": rules}
        contacts = self._db.contacts(hotel) if hotel else {}
        if contacts:
            payload["contacts"] = contacts
        return payload

    _STYLE_ADD = re.compile(r"^(?:add\s+)?(?:style rule|house style rule|house style)\s*:\s*(.+)$", re.I | re.S)
    _STYLE_BAN = re.compile(r"^(?:banned phrase|ban phrase|never write)\s*:\s*(.+)$", re.I | re.S)
    _STYLE_LIST = re.compile(r"^(?:show\s+|list\s+)?(?:the\s+)?(?:style rules|house style)\s*\??$", re.I)
    _STYLE_DROP = re.compile(r"^(?:remove|delete|drop)\s+style rule\s*#?(\d+)\s*$", re.I)
    _CONTACT = re.compile(r"^(hotel contact|editor)\s*:\s*([^\n]{1,60})$", re.I)

    def _style_command(self, user_text: str, user_id: str, project_row: Any) -> str | None:
        """Reply to a house-style or contact command, or None when the message is not one."""
        text = self._plain_command(user_text)
        if (m := self._STYLE_ADD.match(text)):
            rule_id = self._db.add_style_rule(m.group(1), kind="rule", created_by=user_id)
            return f"Added house style rule #{rule_id}. Drafts from now on follow it."
        if (m := self._STYLE_BAN.match(text)):
            phrases = [p.strip().strip('"') for p in re.split(r"[|;]", m.group(1)) if p.strip()]
            ids = [self._db.add_style_rule(p, kind="banned", created_by=user_id) for p in phrases]
            return f"Banned {', '.join(repr(p) for p in phrases)} (#{', #'.join(map(str, ids))})."
        if self._STYLE_LIST.match(text):
            rows = self._db.style_rules()
            rules = [f"#{r['id']} {r['rule']}" for r in rows if r["kind"] != "banned"]
            banned = [f"#{r['id']} \"{r['rule']}\"" for r in rows if r["kind"] == "banned"]
            lines = ["*House style*", *[f"• {r}" for r in rules]]
            if banned:
                lines += ["*Never write*", " · ".join(banned)]
            lines.append("_Add one with \"style rule: ...\" or \"banned phrase: ...\"; remove with \"remove style rule #N\"._")
            return "\n".join(lines)
        if (m := self._STYLE_DROP.match(text)):
            done = self._db.remove_style_rule(int(m.group(1)))
            return f"Removed style rule #{m.group(1)}." if done else f"There is no active style rule #{m.group(1)}."
        if (m := self._CONTACT.match(text)):
            role = "hotel" if m.group(1).lower().startswith("hotel") else "editor"
            name = m.group(2).strip()
            if role == "hotel":
                if not project_row:
                    return "Tell me the hotel contact inside a hotel's thread, so I know which hotel it is for."
                self._db.set_contact(str(project_row["hotel"]), "hotel", name)
                return f"Noted: {name} is the contact at {project_row['hotel']}. Drafts will ask {name} by name."
            self._db.set_contact("", "editor", name)
            return f"Noted: {name} is the editor who raises [ASK] notes with the hotel."
        return None

    @staticmethod
    def _plain_command(text: str) -> str:
        """Message text without mentions or the "Sent using <app>" footer some Slack apps add."""
        text = re.sub(r"<@[^>]+>", "", text or "")
        return re.sub(r"\s*\*?Sent using\*?.*$", "", text, flags=re.S | re.I).strip()

    @classmethod
    def _amendment_from(cls, request: str) -> str:
        """The correction a draft request carries beyond 'write a sample draft', if any."""
        text = cls._plain_command(request)
        rest = re.sub(
            r"\b(please\s+)?(write|redo|re-?write|make)\s+(a\s+|the\s+|this\s+)?(sample\s+)?"
            r"(draft|piece|article|layout)(\s+again)?\b[.!:,]?",
            "", text, count=1, flags=re.I,
        ).strip()
        return text[:800] if len(rest) >= 25 else ""

    @staticmethod
    def _pin_headline(draft: DraftResult, outline: dict[str, Any], qa_revisions: list[str]) -> DraftResult:
        """Keep the outline's chosen headline; the writer and humanizer drift to weaker ones.

        Only a QA instruction about the headline may change it.
        """
        title = str((outline or {}).get("title") or "").strip()
        if " | " not in title or draft.headline == title:
            return draft
        if any("headline" in str(item).lower() for item in qa_revisions):
            return draft
        return replace(draft, headline=title)

    def _cleared_images(self, research: ResearchResult) -> list[dict[str, Any]]:
        """Only hotel-supplied photos are cleared for layouts; web images are not."""
        return [
            asset for asset in research.available_assets
            if isinstance(asset, dict) and asset.get("rights") == "hotel_supplied"
            and Path(str(asset.get("local_path") or "")).is_file()
        ]

    @staticmethod
    def _hero_candidates(catalogue: dict[str, dict[str, Any]]) -> list[str]:
        """Photos big enough for a full-page cover, largest first (collage tiles look pixelated)."""
        def size(asset: dict[str, Any]) -> tuple[int, int]:
            return int(asset.get("width") or 0), int(asset.get("height") or 0)
        ranked = sorted(catalogue, key=lambda k: size(catalogue[k])[0] * size(catalogue[k])[1], reverse=True)
        big = [k for k in ranked if size(catalogue[k])[0] >= 1400 and catalogue[k].get("orientation") == "landscape"]
        return big or ranked[:5]

    def _build_layout(
        self, project_id: int, state: dict[str, Any], research: ResearchResult,
        draft: DraftResult, critique: Any,
    ) -> Path:
        photos = self._cleared_images(research)
        self._notify(STEP_PROGRESS_PREFIX + (
            "Layout: placing the hotel's photos on the page" if photos
            else "Layout: setting the page with empty photo slots (no hotel-supplied photos in this thread)"
        ))
        catalogue = {f"img{i}": asset for i, asset in enumerate(photos, 1)}
        sections = split_sections(draft.body)
        plan: dict[str, Any] = {}
        # Even without photos the model writes a shot brief for each empty photo slot.
        if sections:
            payload = {
                "headline": draft.headline, "standfirst": draft.standfirst,
                "sections": [
                    {"section": i, "heading": s.heading, "opening": " ".join(s.paragraphs)[:400]}
                    for i, s in enumerate(sections)
                ],
                "photos": [
                    {
                        "id": key, "orientation": a.get("orientation"), "shows": a.get("evidence", ""),
                        "deck_page_text": a.get("page_context", ""),
                    }
                    for key, a in catalogue.items()
                ],
                "hero_candidates": self._hero_candidates(catalogue),
                "draft_sentences": draft.body[:6000],
            }
            try:
                response = self._fast_provider.complete(
                    [LLMMessage(role="user", content=json.dumps(payload, ensure_ascii=False))],
                    system=load_prompt("layout_images"), max_tokens=1500, temperature=0.2,
                )
                data = extract_json_block(response.text)
                plan = data if isinstance(data, dict) else {}
            except LLMError:
                logger.exception("layout photo plan failed; using empty slots")
        if catalogue and str(plan.get("hero")) not in self._hero_candidates(catalogue):
            plan["hero"] = self._hero_candidates(catalogue)[0]
        used = {str(plan.get("hero"))} | {
            str(item.get("image")) for item in plan.get("sections") or [] if isinstance(item, dict)
        }
        self._layout_photo_count = len(used & set(catalogue))
        notes = [f"QA by Claude: {str(critique.verdict).replace('_', ' ').lower()}"]
        if draft.tk_items:
            notes.append(f"{len(draft.tk_items)} TK gaps highlighted in yellow")
        hotel = str(state.get("project", {}).get("hotel", ""))
        page = render_editorial_html(
            hotel=hotel, headline=draft.headline or "Sample draft", standfirst=draft.standfirst,
            body=draft.body, images=catalogue, plan=plan, footer_notes=notes,
        )
        out_dir = self._settings.artifacts_path / ArtifactManager.safe_slug(hotel or "hotel") / "layouts" / time.strftime("%Y%m%d-%H%M%S")
        out_dir.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9]+", "-", draft.headline or "sample-draft").strip("-")[:60] or "sample-draft"
        html_path = out_dir / f"{name}.html"
        html_path.write_text(page, encoding="utf-8")
        return html_path

    @staticmethod
    def _format_draft_with_layout(
        draft: DraftResult, critique: Any, *, private: bool, photos: int, fixes: dict[str, Any] | None = None,
    ) -> str:
        lines = [f"**{draft.headline or 'Sample draft'}**"]
        if draft.standfirst:
            lines.append(f"_{draft.standfirst}_")
        qa = EditorialOrchestrator._qa_lines(critique, fixes)
        if draft.tk_items:
            qa.append(f"• {len(draft.tk_items)} [TK] gaps, highlighted in the layout")
        lines.append("\n".join(qa))
        writer = "Claude" if private or "claude" in draft.model.lower() else (draft.model or "GLM")
        lines.append(
            f"_Layout below (HTML, {photos} hotel photos, opens in Slack or a browser). "
            "Open it in a browser to drop photos into the empty slots, then use Save with photos. "
            f"Drafted by {writer}._"
        )
        return "\n\n".join(lines)

    @staticmethod
    def _qa_lines(critique: Any, fixes: dict[str, Any] | None) -> list[str]:
        verdict = str(critique.verdict).replace("_", " ").lower()
        applied = (fixes or {}).get("applied")
        polished = (fixes or {}).get("polished")
        note = f"re-checked after {applied} final fixes" if applied else ""
        if polished:
            note += f", then {polished} wording edits"
        qa = [f"**QA by Claude: {verdict}" + (f" ({note})**" if note else "**")]
        if critique.verdict != "PASS":
            qa += [f"• {item}" for item in (critique.revision_instructions or critique.reasons or [])[:3]]
        qa += [f"• Not applied: {item}" for item in ((fixes or {}).get("skipped") or [])[:2]]
        polish = list(getattr(critique, "polish", None) or [])
        if polish:
            qa.append("_Optional wording polish:_")
            qa += [f"• {item}" for item in polish[:3]]
        return qa

    @staticmethod
    def _format_draft(
        draft: DraftResult, critique: Any, *, private: bool, fixes: dict[str, Any] | None = None,
    ) -> str:
        body = re.sub(r"(?m)^##\s*(.+)$", r"**\1**", draft.body.strip())
        parts = [f"**{draft.headline or 'Sample draft'}**"]
        if draft.standfirst:
            parts.append(f"_{draft.standfirst}_")
        parts.append(body)
        qa = EditorialOrchestrator._qa_lines(critique, fixes)
        if draft.tk_items:
            qa.append(f"• {len(draft.tk_items)} [TK] gaps to fill from the hotel")
        asks = re.findall(r"\[ASK\b[^\]]*\]", draft.body)
        if asks:
            qa.append(f"• {len(asks)} [ASK] notes for the editor to raise with the hotel")
        parts.append("\n".join(qa))
        writer = "Claude" if private or "claude" in draft.model.lower() else (draft.model or "GLM")
        note = f"_Drafted by {writer}"
        if private:
            note += " (this concept uses hotel-shared documents, which stay with Claude)"
        parts.append(note + "._")
        return "\n\n".join(parts)

    @staticmethod
    def _step_label(specialist: str, step_id: str) -> str:
        """User-facing description of a pipeline step for the Slack progress log."""
        attempt = re.search(r"_(\d+)$", step_id)
        suffix = f" (round {int(attempt.group(1))})" if attempt and int(attempt.group(1)) > 1 else ""
        if step_id == "research_experience":
            return "Research: guest reviews, insider tips and the hotel's stories"
        if step_id == "research_itinerary":
            return "Research: restaurants, sights and day trips for the itinerary"
        if step_id == "research_venues":
            return "Research: why each stop is worth it, and when it is open"
        if step_id == "ingest_documents":
            return "Research: adding the attached documents to the evidence"
        if step_id.startswith("research_verify"):
            return "Research: verifying open evidence gaps" + suffix
        if step_id.startswith(("research_editorial", "research_critic")):
            return "Research: checking facts the recommendation depends on"
        if specialist == "researcher":
            return "Research: official sources, coverage and destination context"
        if specialist == "editorial":
            return "Editorial: building the recommendation" + suffix
        if specialist == "writer":
            return "Writer: drafting a sample article" + suffix
        if specialist == "logistics":
            return "Logistics: checking routes and travel times between stops"
        if step_id == "draft_polish":
            return "Writer: applying the editor's wording polish"
        if specialist == "draft_fixes":
            return "Writer: applying the editor's last fixes"
        if specialist == "humanizer":
            return "Writer: removing AI-sounding phrasing" + suffix
        if step_id.startswith("critic_draft_final"):
            return "Critic: re-checking the draft after the last fixes"
        if specialist == "critic_draft":
            return "Critic: checking the draft against the evidence" + suffix
        if specialist == "critic_editorial":
            return "Critic: checking the recommendation against the evidence" + suffix
        if specialist == "designer":
            return "Design: building the visual direction"
        if specialist == "builder":
            return "Build: writing the HTML mockup"
        if specialist == "critic_visual":
            return "Critic: reviewing the mockup" + suffix
        return specialist.replace("_", " ").capitalize()

    def _notify(self, stage: str) -> None:
        if self._cancel_check():
            raise AgentCancelled("Task cancelled")
        for callback in list(self._progress_callbacks):
            callback(stage)

    def handle_message(
        self, user_text: str, *, user_id: str = "", thread_key: str = "",
        history: Sequence[tuple[str, str]] = (), channel_id: str = "",
        thread_ts: str = "", cancel_check: Callable[[], bool] | None = None,
        documents: Sequence[Document] = (),
    ) -> OrchestratorOutcome:
        started = time.monotonic()
        self.last_trace = []
        self.last_plan = None
        self.last_briefs = []
        self._active_thread_key = thread_key
        self._active_goal = user_text
        self._dining_note = ""
        self._test_run = any(
            self._is_test_marker(text) for role, text in [("user", user_text), *history] if role == "user"
        )
        self._targeted_research_rounds_used = 0
        self._cancel_check = cancel_check or (lambda: False)
        if hasattr(self._researcher, "set_progress_listener"):
            self._researcher.set_progress_listener(self._notify)
        if hasattr(self._researcher, "reset_search_status"):
            self._researcher.reset_search_status()
        session = self._db.get_session(thread_key) if thread_key else None
        if thread_key and session is None:
            session = self._db.upsert_session(
                thread_key, thread_ts=thread_ts or thread_key.rsplit(":", 1)[-1],
                channel_id=channel_id, user_id=user_id,
            )
        project_row = self._db.project_for_thread(thread_key) if thread_key else None
        project_state = self._db.project_state(project_row["id"]) if project_row else {}
        style_reply = self._style_command(user_text, user_id, project_row)
        if style_reply is not None:
            return OrchestratorOutcome(style_reply, project_id=project_row["id"] if project_row else None)
        route = self._classify(user_text, history, session, project_state)
        self._trip_nights_explicit = self._parse_trip_nights(user_text)
        self._segment = self._parse_segment(user_text)
        self._trip_nights = self._trip_nights_explicit or max(int(self._settings.default_trip_nights or 0), 0)
        self._resolve_calendar(user_text, project_state)
        intent = str(route.get("intent", Intent.GENERAL_CONVERSATION)).upper()
        if self._is_project_status_question(user_text):
            intent = Intent.GET_PROJECT_STATUS
        elif self._is_workflow_debug_question(user_text):
            intent = Intent.SHOW_WORKFLOW
        elif self._is_sample_draft_request(user_text) or self._is_layout_request(user_text):
            intent = Intent.WRITE_SAMPLE_DRAFT
        elif self._is_research_verification_request(user_text, project_state):
            intent = Intent.VERIFY_RESEARCH_GAPS
        if intent not in VALID_INTENTS:
            intent = Intent.GENERAL_CONVERSATION
        document_note = ""
        documents_ack = ""
        hotel_reply = self._pasted_hotel_reply(user_text, thread_key) if project_row else None
        if hotel_reply:
            # The answer to a near-miss's open questions: add it as evidence and re-run.
            documents = (*documents, hotel_reply)
            intent = Intent.FIND_EDITORIAL_OPPORTUNITY
        if documents:
            before = self._evidence_counts(project_state)
            ingested, document_note = self._ingest_documents(documents, project_row, project_state)
            if ingested:
                project_state = self._db.project_state(project_row["id"])
                names = ", ".join(d.name for d in documents if d.usable)
                user_text += f"\n\n[New hotel-supplied documents added to the research: {names}.]"
                if intent in {Intent.GENERAL_CONVERSATION, Intent.META_QUESTION, Intent.HELP}:
                    # Files shared "for reference" are filed, not acted on: re-running the
                    # editorial for every attachment is slow and churns the concept.
                    documents_ack = self._documents_ack(names, before, project_state)
                elif intent in {Intent.CONTINUE_RESEARCH, Intent.ANSWER_CLARIFICATION}:
                    intent = Intent.FIND_EDITORIAL_OPPORTUNITY
        if session and session.status == "awaiting_clarification" and intent not in {
            Intent.CANCEL, Intent.META_QUESTION, Intent.HELP, Intent.GET_PROJECT_STATUS,
            Intent.SHOW_WORKFLOW,
        }:
            intent = Intent.ANSWER_CLARIFICATION
        try:
            if documents_ack:
                outcome = OrchestratorOutcome(documents_ack, project_id=int(project_row["id"]))
            else:
                outcome = self._dispatch(
                    intent, route, user_text, user_id=user_id, thread_key=thread_key,
                    channel_id=channel_id, thread_ts=thread_ts, history=history,
                    session=session, project_row=project_row, project_state=project_state,
                )
            outcome.action = intent
            if document_note:
                outcome.markdown = (outcome.markdown + "\n\n" + document_note).strip()
            search_error = getattr(self._researcher, "search_error", "")
            if search_error and outcome.markdown:
                outcome.markdown += (
                    "\n\n_⚠️ Live web search was unavailable for this run "
                    f"({search_error}). I only used pages I could open directly, so "
                    "coverage may be thin and some sources may be missing._"
                )
            if thread_key and outcome.markdown:
                self._db.upsert_session(
                    thread_key,
                    conversation_summary=self._compact_summary(history, user_text, outcome.markdown),
                )
            logger.info(
                "orchestration thread=%s project_id=%s intent=%s agents=%s duration_ms=%d result_status=success",
                thread_key, outcome.project_id, intent, ",".join(self.last_trace) or "none",
                int((time.monotonic() - started) * 1000),
            )
            return outcome
        except Exception:
            logger.exception(
                "orchestration thread=%s project_id=%s intent=%s agents=%s duration_ms=%d result_status=failed",
                thread_key, project_row["id"] if project_row else None, intent,
                ",".join(self.last_trace) or "none", int((time.monotonic() - started) * 1000),
            )
            raise

    @staticmethod
    def _evidence_counts(state: dict[str, Any]) -> tuple[int, int]:
        """(facts, cleared photos) in a project's research, as plain numbers."""
        research = state.get("research") or {}
        assets = research.get("available_assets") or []
        photos = sum(1 for a in assets if isinstance(a, dict) and a.get("rights") == "hotel_supplied")
        return len(research.get("verified_facts") or []), photos

    @staticmethod
    def _documents_ack(names: str, before: tuple[int, int], state: dict[str, Any]) -> str:
        after = EditorialOrchestrator._evidence_counts(state)
        facts, photos = after[0] - before[0], after[1] - before[1]
        hotel = str(state.get("project", {}).get("hotel", "the hotel"))
        photo_text = f" and {photos} photos cleared for layouts" if photos > 0 else ""
        return (
            f"Added {names} to the {hotel} research: {facts} facts{photo_text}.\n\n"
            "Ask me to *re-evaluate the concept*, *write a sample draft*, or *make a layout with photos* to use them."
        )

    def _ingest_documents(
        self, documents: Sequence[Document], project_row: Any, project_state: dict[str, Any],
    ) -> tuple[bool, str]:
        """Fold attachments into the thread's research. Returns (ingested, note for the reply)."""
        readable = [d for d in documents if d.usable]
        problems = [f"• {d.name}: {d.error or 'no readable text'}" for d in documents if not d.usable]
        if readable and (project_row is None or not project_state.get("research")):
            problems += [f"• {d.name}: there is no researched hotel in this thread yet" for d in readable]
            readable = []
        ingested = False
        if readable and hasattr(self._researcher, "ingest_documents"):
            project_id = int(project_row["id"])
            research = self._call(
                "researcher",
                lambda existing: self._researcher.ingest_documents(project_id, existing, readable),
                ResearchResult.from_dict(project_state["research"]), project_id,
                step_id="ingest_documents",
            )
            self._db.update_project_state(project_id, "research", research.to_dict())
            ingested = True
        note = ("_I couldn't use these attachments:_\n" + "\n".join(problems)) if problems else ""
        return ingested, note

    def _classify(
        self, user_text: str, history: Sequence[tuple[str, str]],
        session: SessionRecord | None, project_state: dict[str, Any],
    ) -> dict[str, Any]:
        context = {
            "session": {
                "status": session.status if session else "idle",
                "property": session.current_property if session else "",
                "goal": session.current_goal if session else "",
                "pending_question": session.pending_question_text if session else "",
            },
            "project": {
                "id": project_state.get("project", {}).get("id"),
                "hotel": project_state.get("project", {}).get("hotel", ""),
                "research_status": project_state.get("research", {}).get("status", ""),
                "concept_status": project_state.get("concept", {}).get("status", ""),
                "design_status": project_state.get("design", {}).get("status", ""),
                "artifact_version": project_state.get("artifact", {}).get("version"),
            },
            "recent": [{"role": r, "content": c[:600]} for r, c in history[-6:]],
            "message": user_text,
        }
        response = self._provider.complete(
            [LLMMessage(role="user", content=json.dumps(context, ensure_ascii=False))],
            system=load_prompt("orchestrator"), max_tokens=900, temperature=0.1,
        )
        data = extract_json_block(response.text)
        return data if isinstance(data, dict) else {"intent": Intent.GENERAL_CONVERSATION}

    def _dispatch(
        self, intent: str, route: dict[str, Any], user_text: str, *, user_id: str,
        thread_key: str, channel_id: str, thread_ts: str,
        history: Sequence[tuple[str, str]], session: SessionRecord | None,
        project_row: Any, project_state: dict[str, Any],
    ) -> OrchestratorOutcome:
        if intent == Intent.CANCEL:
            if thread_key:
                self._db.upsert_session(
                    thread_key, status="cancelled", current_goal="",
                    pending_question_type="", pending_question_text="",
                )
            return OrchestratorOutcome("Stopped. I preserved the project state so you can resume later.")

        if intent in {Intent.META_QUESTION, Intent.HELP, Intent.GENERAL_CONVERSATION}:
            return self._answer_direct(user_text, history, project_state, intent)

        if intent == Intent.GET_PROJECT_STATUS:
            return self._project_status(project_state)

        if intent == Intent.SHOW_WORKFLOW:
            return self._workflow_summary(project_state, thread_key)

        if intent == Intent.VERIFY_RESEARCH_GAPS:
            if project_row is None:
                return OrchestratorOutcome(
                    "I don't have an active hotel project whose claims I can verify yet."
                )
            return self._verification_flow(
                int(project_row["id"]), project_state, user_text, thread_key=thread_key
            )

        if intent == Intent.ANSWER_CLARIFICATION:
            route = dict(route)
            if session and session.pending_question_type == "property_identity":
                route["hotel"] = route.get("hotel") or user_text.strip()
                user_text = f"{session.original_request}\nClarification: {user_text}"
                intent = Intent.RESEARCH_PROPERTY
                self._db.clear_pending_question(thread_key)

        if intent in {Intent.RESEARCH_PROPERTY, Intent.CONTINUE_RESEARCH}:
            hotel = str(route.get("hotel") or (session.current_property if session else "")).strip()
            if route.get("needs_clarification") or not hotel:
                return self._clarify(
                    route, user_text, user_id=user_id, thread_key=thread_key,
                    channel_id=channel_id, thread_ts=thread_ts,
                )
            return self._research_flow(
                hotel, route, user_text, user_id=user_id, thread_key=thread_key,
                channel_id=channel_id, thread_ts=thread_ts, project_row=project_row,
            )

        if project_row is None:
            return OrchestratorOutcome(
                markdown="I don't have an active hotel project in this thread yet. Name the property first.",
                clarification="Which property should this project use?",
            )
        project_id = int(project_row["id"])

        if intent in {Intent.FIND_EDITORIAL_OPPORTUNITY, Intent.DEVELOP_CONCEPT}:
            return self._editorial_flow(
                project_id, project_state, user_text,
                approve=(intent == Intent.DEVELOP_CONCEPT),
            )
        if intent == Intent.WRITE_SAMPLE_DRAFT:
            return self._draft_flow(project_id, project_state, user_text)
        if intent in {Intent.CRITIQUE_CONCEPT, Intent.COMPARE_CONCEPTS}:
            return self._critique_concept(project_id, project_state)
        if intent == Intent.CREATE_VISUAL_DIRECTION:
            return self._design_flow(project_id, project_state, user_text)
        if intent == Intent.CREATE_HTML_MOCKUP:
            return self._mockup_flow(project_id, project_state, user_text, revise=False)
        if intent in {Intent.REVISE_MOCKUP, Intent.CRITIQUE_MOCKUP}:
            return self._mockup_flow(project_id, project_state, user_text, revise=True)
        if intent == Intent.LOAD_PREVIOUS_PROJECT:
            version = int(route.get("target_version") or 0)
            if version <= 0:
                return OrchestratorOutcome("Which artifact version should I restore?", project_id=project_id)
            artifact = self._artifacts.restore(project_id, version)
            self._db.update_project_state(project_id, "artifact", artifact.to_dict())
            return OrchestratorOutcome(
                f"Artifact v{version} is current again. Later versions are still preserved.",
                project_id=project_id, artifact_path=artifact.path,
            )
        return self._answer_direct(user_text, history, project_state, intent)

    def _begin_plan(
        self, *, project_id: int, state: dict[str, Any], intent: str,
        user_goal: str, constraints: list[str] | None = None,
        concept_locked: bool = False,
    ) -> OrchestrationPlan:
        research = state.get("research") or {}
        verified, low_confidence = split_evidence(list(research.get("verified_facts") or []))
        unknowns = list(research.get("critical_unknowns") or research.get("uncertainties") or [])
        mode = self._response_mode_for(intent)
        step_specs: dict[str, list[PlanStep]] = {
            Intent.VERIFY_RESEARCH_GAPS: [
                PlanStep(
                    "research_verify_01", "research", "Verify the requested claims only.",
                    required_inputs=["current research", "saved open questions"],
                    output_contract="Claim-by-claim evidence classifications with sources.",
                    success_criteria=["Each claim is verified, unsupported, or uncertain."],
                    stop_if=["Two bounded source strategies are exhausted."],
                ),
                PlanStep(
                    "research_verify_02", "research", "Try an alternate bounded source strategy.",
                    depends_on=["research_verify_01"],
                    output_contract="Updated claim verification classifications.",
                    success_criteria=["Previously unresolved claims receive new authoritative evidence."],
                    stop_if=["No materially different source path remains."],
                ),
            ],
            Intent.RESEARCH_PROPERTY: [
                PlanStep("research_01", "research", "Build an experience-capable evidence base."),
                PlanStep("research_verify_01", "research", "Close material evidence gaps before ideation.", depends_on=["research_01"]),
                PlanStep("research_verify_02", "research", "Use one alternate bounded source strategy if material gaps remain.", depends_on=["research_verify_01"]),
                PlanStep("editorial_01", "editorial", "Generate and select evidence-safe experience hypotheses.", depends_on=["research_01"]),
                PlanStep("critic_01", "critic", "Independently gate the selected proposition.", depends_on=["editorial_01"]),
                PlanStep("editorial_02", "editorial", "Try a materially different hypothesis if needed.", depends_on=["critic_01"]),
                PlanStep("critic_02", "critic", "Gate the materially different second proposition.", depends_on=["editorial_02"]),
            ],
            Intent.CONTINUE_RESEARCH: [
                PlanStep("research_01", "research", "Extend the current evidence base."),
                PlanStep("research_verify_01", "research", "Close material evidence gaps.", depends_on=["research_01"]),
                PlanStep("research_verify_02", "research", "Use one alternate bounded source strategy if material gaps remain.", depends_on=["research_verify_01"]),
                PlanStep("editorial_01", "editorial", "Select an evidence-safe experience.", depends_on=["research_verify_01"]),
                PlanStep("critic_01", "critic", "Gate the proposition.", depends_on=["editorial_01"]),
                PlanStep("editorial_02", "editorial", "Try a materially different hypothesis if needed.", depends_on=["critic_01"]),
                PlanStep("critic_02", "critic", "Gate the materially different second proposition.", depends_on=["editorial_02"]),
            ],
            Intent.FIND_EDITORIAL_OPPORTUNITY: [
                PlanStep("editorial_01", "editorial", "Generate and select evidence-safe hypotheses."),
                PlanStep("critic_01", "critic", "Gate the selected proposition.", depends_on=["editorial_01"]),
                PlanStep("editorial_02", "editorial", "Try a different hypothesis after conceptual failure.", depends_on=["critic_01"]),
                PlanStep("critic_02", "critic", "Gate the materially different second proposition.", depends_on=["editorial_02"]),
            ],
            Intent.DEVELOP_CONCEPT: [
                PlanStep("editorial_01", "editorial", "Develop the selected evidence-safe hypothesis."),
                PlanStep("critic_01", "critic", "Gate the developed proposition.", depends_on=["editorial_01"]),
            ],
            Intent.CREATE_VISUAL_DIRECTION: [
                PlanStep("design_01", "design", "Derive visual grammar from the selected proposition."),
            ],
            Intent.CREATE_HTML_MOCKUP: [
                PlanStep("design_01", "design", "Complete any missing design specification."),
                PlanStep("builder_01", "builder", "Implement the approved design specification.", depends_on=["design_01"]),
                PlanStep("critic_visual_01", "critic", "Inspect the artifact against concept and design.", depends_on=["builder_01"]),
            ],
        }
        steps = [PlanStep(**step.to_dict()) for step in step_specs.get(intent, [])]
        plan = OrchestrationPlan(
            user_goal=user_goal, project_id=str(project_id),
            property_name=str(state.get("project", {}).get("hotel") or "") or None,
            current_stage=str((state.get("orchestration") or {}).get("current_stage") or "planning"),
            desired_outcome=mode.replace("_", " ").title(),
            known_verified=verified[:30],
            known_uncertain=(low_confidence + list(research.get("uncertainties") or []))[:30],
            critical_unknowns=unknowns[:20], steps=steps,
            completion_criteria=["User goal is answered.", "Evidence constraints are respected.", "Specialist output is inspected before response."],
            response_mode=mode,
        )
        self.last_plan = plan
        self._response_mode = mode
        orchestration_state = {
            "active_goal": user_goal,
            "active_constraints": list(constraints or []),
            "current_stage": "planning",
            "current_hypothesis": (state.get("concept") or {}).get("editorial", {}).get("working_title", ""),
            "open_questions": unknowns,
            "latest_user_instruction": user_goal,
            "concept_locked": concept_locked,
            "task_status": "running",
            "response_mode": mode,
            "plan": plan.to_dict(),
        }
        self._db.update_project_state(project_id, "orchestration", orchestration_state)
        self._db.append_workflow_trace(
            project_id=project_id, thread_key=self._active_thread_key, goal=user_goal,
            step_id="plan", result_status="created", evaluation={"step_count": len(steps)},
            next_action=steps[0].id if steps else "respond", response_mode=mode,
        )
        return plan

    def _finish_plan(
        self, project_id: int, state: dict[str, Any], *, stage: str,
        open_questions: list[str] | None = None,
    ) -> None:
        if self.last_plan:
            for step in self.last_plan.steps:
                if step.status == "pending":
                    step.status = "skipped"
            self.last_plan.current_stage = stage
        prior = self._db.project_state(project_id).get("orchestration") or {}
        prior.update({
            "current_stage": stage,
            "open_questions": list(open_questions or []),
            "task_status": "complete",
            "concept_locked": False,
            "plan": self.last_plan.to_dict() if self.last_plan else prior.get("plan", {}),
        })
        self._db.update_project_state(project_id, "orchestration", prior)

    @staticmethod
    def _response_mode_for(intent: str) -> str:
        if intent == Intent.VERIFY_RESEARCH_GAPS:
            return ResponseMode.RESEARCH_VERIFICATION.value
        if intent in {Intent.RESEARCH_PROPERTY, Intent.CONTINUE_RESEARCH, Intent.FIND_EDITORIAL_OPPORTUNITY, Intent.DEVELOP_CONCEPT}:
            return ResponseMode.EDITORIAL_RECOMMENDATION.value
        if intent in {Intent.CRITIQUE_CONCEPT, Intent.COMPARE_CONCEPTS, Intent.CRITIQUE_MOCKUP}:
            return ResponseMode.CRITIQUE.value
        if intent == Intent.CREATE_VISUAL_DIRECTION:
            return ResponseMode.DESIGN_BRIEF.value
        if intent in {Intent.CREATE_HTML_MOCKUP, Intent.REVISE_MOCKUP}:
            return ResponseMode.ARTIFACT_DELIVERY.value
        if intent in {Intent.GET_PROJECT_STATUS, Intent.SHOW_WORKFLOW}:
            return ResponseMode.STATUS.value
        return ResponseMode.CONVERSATIONAL.value

    def _register_brief(self, brief: SpecialistTaskBrief) -> SpecialistTaskBrief:
        self.last_briefs.append(brief)
        return brief

    def _research_brief(
        self, *, project_id: int, hotel: str, user_goal: str,
        known_research: ResearchResult | None = None,
        questions: list[str] | None = None, task_id: str = "research_01",
        verification_only: bool = False,
    ) -> SpecialistTaskBrief:
        known = known_research or ResearchResult(status="partial")
        verified, uncertain = split_evidence(known.verified_facts)
        investigate = questions or [
            "Exact relationship between the property and its transport/neighbourhood context.",
            "Realistic destination patterns enabled during a multi-night stay.",
            "Repeatable guest experiences, including food, wellness, residence, and recovery rhythms.",
            "Property characteristics that materially change how a guest experiences the destination.",
            "Existing editorial coverage and authentic visual assets.",
        ]
        prohibited = [
            "Do not invent editorial concepts or slogans.",
            "Do not infer guest behaviour from facilities alone.",
            "Do not convert search snippets into verified facts.",
        ]
        if verification_only:
            prohibited += ["Do not evaluate, revise, replace, or reject the current concept."]
        return self._register_brief(SpecialistTaskBrief(
            task_id=task_id, project_id=str(project_id), specialist="research",
            mission=("Verify the specified evidence gaps only." if verification_only else "Build an evidence base capable of supporting experience-led Suite Stories editorial."),
            user_goal=user_goal,
            current_context=f"Property: {hotel}. Research version: {known.version}.",
            inputs={"property": hotel, "questions": investigate},
            verified_facts=verified[:25], unresolved_questions=(investigate + uncertain)[:30],
            explicit_instructions=[
                "Search each question specifically and use bounded alternate-source fallbacks.",
                "Distinguish official facts, authoritative secondary evidence, snippets, inference, and unsupported claims.",
                "Record exact source provenance and current status.",
            ],
            prohibited_actions=prohibited,
            required_output=["Verified facts", "Experience signals", "Uncertainties", "Contradictions", "Assets", "Sources", "Critical unknowns"],
            acceptance_criteria=["Every important fact has source IDs and an evidence confidence class.", "Failed fetches trigger alternate source discovery rather than repeated identical retries."],
        ))

    def _editorial_brief(
        self, *, project_id: int, hotel: str, user_goal: str,
        research: ResearchResult, attempt: int, prior_failure: str = "",
    ) -> SpecialistTaskBrief:
        verified, uncertain = split_evidence(research.verified_facts + research.experience_signals)
        instructions = [
            "Consider 3–6 hypotheses internally before selecting the strongest.",
            "Find a real stay rhythm with hotel-specific advantage, booking desire, and natural visual potential.",
            "Return supporting evidence IDs for the selected proposition.",
        ]
        if self._trip_nights:
            instructions.append(self._itinerary_instruction(self._trip_nights, self._segment))
            calendar = self._calendar_for(self._trip_nights)
            if calendar:
                instructions.append(calendar.instruction())
        photos = self._hotel_photo_lines(research)
        if photos:
            instructions.append(
                "HOTEL PHOTOS cleared for the layout (plan moments these can illustrate, without claiming "
                "anything a photo shows that the evidence does not): " + "; ".join(photos) + "."
            )
        if self._dining_note:
            instructions.append(self._dining_note)
        if attempt > 1 and "critic required revisions" in prior_failure.lower():
            instructions.append(
                "Revise the supplied candidate in place. Apply every critic instruction, remove or "
                "qualify unsupported wording, preserve the evidence-safe core, and return every "
                "required editorial field as non-empty text."
            )
        elif attempt > 1 and "structurally incomplete" in prior_failure.lower():
            instructions.append(
                "Repair the supplied incomplete candidate. Preserve its evidence-safe core if useful, "
                "but return every required editorial field as non-empty text, especially the field(s) "
                "named in the prior failure."
            )
        elif attempt > 1:
            instructions.append("Generate a materially different hypothesis; do not rename or cosmetically polish the failed idea.")
        return self._register_brief(SpecialistTaskBrief(
            task_id=f"editorial_{attempt:02d}", project_id=str(project_id), specialist="editorial",
            mission="Discover the strongest evidence-supported stay experience worth turning into Suite Stories editorial.",
            user_goal=user_goal, current_context=f"Property: {hotel}. Editorial attempt {attempt}. Prior failure: {prior_failure or 'none'}.",
            inputs={"property": hotel, "attempt": attempt}, verified_facts=verified[:40],
            unresolved_questions=(list(research.critical_unknowns) + list(research.uncertainties) + uncertain)[:30],
            explicit_instructions=instructions,
            prohibited_actions=[
                "Do not use search_snippet_only, inferred, unsupported, or unresolved claims as a central premise.",
                "Do not return an amenity summary, USP list, demographic segment, or metaphor based on one feature.",
            ],
            required_output=["Candidate hypotheses for internal comparison", "One strongest proposition", "Desirability", "Property specificity", "Evidence IDs", "Story and visual mechanism", "Weaknesses"],
            acceptance_criteria=["Central proposition uses only editorial-safe evidence.", "The experience is tangible, bookable, property-specific, and visually expressible."],
        ))

    def _critic_brief(
        self, *, project_id: int, mode: str, user_goal: str, task_id: str,
    ) -> SpecialistTaskBrief:
        return self._register_brief(SpecialistTaskBrief(
            task_id=task_id, project_id=str(project_id), specialist="critic",
            mission="Independently gate the specialist result without trying to rescue it.",
            user_goal=user_goal, current_context=f"Critique mode: {mode}." + (
                f" FORMAT: hotel recommendation with a {self._trip_nights}-night itinerary"
                + (f" for {self.TRAVELLER_SEGMENTS[self._segment][0]}." if self._segment else ".")
                if self._trip_nights and mode == "EDITORIAL" else ""
            ) + (
                " " + calendar.instruction()
                if mode == "EDITORIAL" and (calendar := self._calendar_for(self._trip_nights)) else ""
            ),
            explicit_instructions=["Test factual support, actual guest experience, booking desire, property specificity, originality, natural editorial device, and asset feasibility."],
            prohibited_actions=["Do not browse.", "Do not silently repair the proposition."],
            required_output=["PASS, PASS_WITH_REVISIONS, or FAIL", "Specific reasons", "Fail reason code", "Actionable next step"],
            acceptance_criteria=["Evidence-dependent central claims fail with EVIDENCE_DEPENDENCY.", "Feature-only ideas fail with CONCEPTUAL_WEAKNESS."],
        ))

    def _design_brief(self, project_id: int, concept: dict[str, Any], assets: list[dict[str, Any]], feedback: str) -> SpecialistTaskBrief:
        return self._register_brief(SpecialistTaskBrief(
            task_id="design_01", project_id=str(project_id), specialist="design",
            mission="Derive the visual/editorial system from the selected proposition.",
            user_goal=self._active_goal, current_context=f"Selected proposition: {concept.get('editorial_proposition') or concept.get('working_title', '')}",
            inputs={"story_mechanism": concept.get("possible_story_structure", ""), "visual_device": concept.get("visual_storytelling_device", ""), "authentic_assets": assets, "feedback": feedback},
            explicit_instructions=["Define the visual constant, visual variable, sequence, reader experience, responsive behavior, and asset mapping."],
            prohibited_actions=["Do not invent a new hotel-story concept.", "Do not use generic luxury page patterns."],
            required_output=["Concept-derived visual grammar", "Section order", "Asset mapping", "Interaction and responsive rules"],
            acceptance_criteria=["Every major design choice traces to the selected proposition."],
        ))

    def _builder_brief(self, project_id: int, concept: dict[str, Any], spec: DesignSpec, assets: list[dict[str, Any]]) -> SpecialistTaskBrief:
        return self._register_brief(SpecialistTaskBrief(
            task_id="builder_01", project_id=str(project_id), specialist="builder",
            mission="Implement the approved design specification without reinterpreting it.",
            user_goal=self._active_goal, current_context=f"Concept: {concept.get('working_title') or concept.get('editorial_proposition', '')}. Design v{spec.version}.",
            inputs={"section_order": spec.page_narrative, "section_structure": spec.section_structure, "asset_mapping": spec.asset_mapping, "authentic_assets": assets},
            explicit_instructions=["Follow the visual hierarchy, narrative sequence, interactions, responsive rules, and artifact version requirements exactly."],
            prohibited_actions=["Do not reinvent the proposition, information architecture, or art direction.", "Do not invent assets or hotel claims."],
            required_output=["Complete standalone semantic responsive HTML"],
            acceptance_criteria=["Artifact implements the supplied design spec and ends with valid closing HTML."],
        ))

    @staticmethod
    def _gated_research(research: ResearchResult) -> ResearchResult:
        data = research.to_dict()
        low: list[str] = []
        for key in ("verified_facts", "experience_signals"):
            allowed = []
            for item in data.get(key, []) or []:
                if evidence_confidence(item) in EDITORIAL_ALLOWED_CONFIDENCE:
                    allowed.append(item)
                elif fact_text(item):
                    low.append(fact_text(item))
            data[key] = allowed
        data["uncertainties"] = list(dict.fromkeys(list(data.get("uncertainties") or []) + low))
        return ResearchResult.from_dict(data)

    def _record_evaluation(
        self, project_id: int, brief: SpecialistTaskBrief,
        evaluation: SpecialistEvaluation, result_status: str,
    ) -> None:
        self._db.append_workflow_trace(
            project_id=project_id, thread_key=self._active_thread_key,
            goal=self._active_goal, step_id=brief.task_id, specialist=brief.specialist,
            brief_summary=brief.mission, result_status=result_status,
            evaluation=evaluation.to_dict(), next_action=evaluation.next_action,
            response_mode=self._response_mode,
        )

    @staticmethod
    def _editorial_has_low_confidence_dependency(
        editorial: EditorialResult, research: ResearchResult,
    ) -> bool:
        for dependency in editorial.evidence_dependencies:
            if isinstance(dependency, dict):
                if dependency.get("central") is False:
                    continue
                confidence = str(dependency.get("confidence") or dependency.get("status") or "inferred").lower()
                if confidence not in EDITORIAL_ALLOWED_CONFIDENCE:
                    return True
            else:
                text = str(dependency).strip().lower()
                if not text:
                    continue
                # Free-form dependency notes often document deliberately
                # quarantined or publication-stage details. Only force a hard
                # gate when the note explicitly says the central proposition
                # itself rests on unresolved/low-confidence evidence.
                safe_negations = ("not used as a central", "does not depend", "irrelevant to this proposition")
                unsafe_markers = ("unverified", "unresolved", "search_snippet", "search snippet", "inferred", "unsupported", "depends")
                if not any(marker in text for marker in safe_negations) and "central" in text and any(
                    marker in text for marker in unsafe_markers
                ):
                    return True
        if not editorial.supporting_evidence_ids:
            return False
        confidence_by_sid: dict[str, str] = {}
        confidence_rank = {
            "unsupported": 0,
            "inferred": 1,
            "search_snippet_only": 2,
            "corroborated_secondary": 3,
            "verified_authoritative_secondary": 4,
            "verified_official": 5,
        }
        for item in research.verified_facts + research.experience_signals:
            confidence = evidence_confidence(item)
            for sid in item.get("source_ids", []) if isinstance(item, dict) else []:
                key = str(sid)
                previous = confidence_by_sid.get(key, "unsupported")
                if confidence_rank.get(confidence, 0) > confidence_rank.get(previous, 0):
                    confidence_by_sid[key] = confidence
        for sid in editorial.supporting_evidence_ids:
            if confidence_by_sid.get(str(sid), "inferred") not in EDITORIAL_ALLOWED_CONFIDENCE:
                return True
        return False

    def _verification_flow(
        self, project_id: int, state: dict[str, Any], user_text: str, *, thread_key: str,
    ) -> OrchestratorOutcome:
        """Verify current concept gaps without invoking or mutating editorial state."""
        research_data = state.get("research") or {}
        if not research_data:
            return OrchestratorOutcome(
                "This project has no research package to extend yet.", project_id=project_id
            )
        editorial = (state.get("concept") or {}).get("editorial") or {}
        questions = [
            str(item).strip() for item in editorial.get("open_questions", [])
            if str(item).strip()
        ]
        if not questions:
            questions = [user_text.strip()]
        hotel = str(state.get("project", {}).get("hotel", "")).strip()
        self._begin_plan(
            project_id=project_id, state=state, intent=Intent.VERIFY_RESEARCH_GAPS,
            user_goal=user_text,
            constraints=["concept_mutation=forbidden", "editorial_agent=forbidden", "design_agent=forbidden", "builder_agent=forbidden"],
            concept_locked=True,
        )
        research = ResearchResult.from_dict(research_data)
        for round_number in range(1, MAX_TARGETED_RESEARCH_ROUNDS + 1):
            brief = self._research_brief(
                project_id=project_id, hotel=hotel, user_goal=user_text,
                known_research=research, questions=questions[:4],
                task_id=f"research_verify_{round_number:02d}", verification_only=True,
            )
            research = self._call(
                "researcher", self._researcher.run,
                ResearchTask(
                    project_id=project_id, hotel_name=hotel, original_request=user_text,
                    followup_questions=questions[:4], existing_research=research.to_dict(),
                    constraints=["Verify only the requested claims. Do not develop, critique, or alter the concept."],
                    brief=brief, verification_round=round_number,
                ), project_id, step_id=brief.task_id,
            )
            self._targeted_research_rounds_used = round_number
            unresolved = [
                item for item in research.verification_claims
                if str(item.get("status", "")).lower() == "uncertain"
            ]
            evaluation = SpecialistEvaluation(
                answered_task=bool(research.verification_claims),
                complete=len(research.verification_claims) >= min(4, len(questions)),
                evidence_safe=True,
                new_questions=[str(item.get("claim", "")) for item in unresolved],
                next_action=("alternate_research" if unresolved and round_number < MAX_TARGETED_RESEARCH_ROUNDS else "synthesize_verification"),
                reason_code=("UNRESOLVED_CLAIMS" if unresolved else "CLAIMS_CLASSIFIED"),
            )
            self._record_evaluation(project_id, brief, evaluation, research.status)
            if not unresolved:
                break
        self._db.update_project_state(project_id, "research", research.to_dict())
        if thread_key:
            self._db.upsert_session(thread_key, status="complete", research_project_id=project_id)
        self._finish_plan(
            project_id, state, stage="research_verified",
            open_questions=[str(item.get("claim", "")) for item in research.verification_claims if item.get("status") == "uncertain"],
        )
        return OrchestratorOutcome(
            self._format_verification_report(research.verification_claims),
            project_id=project_id,
        )

    def _research_flow(
        self, hotel: str, route: dict[str, Any], user_text: str, *, user_id: str,
        thread_key: str, channel_id: str, thread_ts: str, project_row: Any,
    ) -> OrchestratorOutcome:
        if project_row is None or str(project_row["hotel"]).lower() != hotel.lower():
            project_id = self._db.create_project(
                hotel=hotel, hotel_slug=self._slug(hotel), request=user_text,
                user_id=user_id, slack_channel=channel_id, slack_thread_ts=thread_key,
            )
        else:
            project_id = int(project_row["id"])
        state = self._db.project_state(project_id)
        self._begin_plan(
            project_id=project_id, state=state, intent=Intent.RESEARCH_PROPERTY,
            user_goal=user_text, constraints=list(route.get("constraints") or []),
        )
        self._db.upsert_session(
            thread_key, status="researching", current_property=hotel,
            original_request=user_text, current_goal=str(route.get("goal") or user_text),
            research_project_id=project_id, pending_question_type="", pending_question_text="",
        )
        self._notify("start")
        brief = self._research_brief(
            project_id=project_id, hotel=hotel, user_goal=user_text, task_id="research_01"
        )
        cached = None if self._wants_fresh_research(user_text) else self._research_from_cache(hotel)
        if cached is not None:
            research, age_days = cached
            self._notify(
                STEP_PROGRESS_PREFIX + f"Research: reusing {len(research.verified_facts)} verified facts "
                f"from earlier {hotel} research (newest {age_days} days old). Say 'fresh research' to redo it"
            )
        else:
            research = self._call(
                "researcher", self._researcher.run,
                ResearchTask(
                    project_id=project_id, hotel_name=hotel, original_request=user_text,
                    city_hint=str(route.get("city_hint", "")),
                    audience=str(route.get("audience", "")),
                    constraints=list(route.get("constraints") or []),
                    brief=brief, trip_nights=self._trip_nights,
                ), project_id, step_id=brief.task_id,
            )
        if research.status != "needs_clarification":
            self._db.update_project_state(project_id, "research", research.to_dict())
            state = self._db.project_state(project_id)
            if self._trip_nights:
                research = self._itinerary_research(project_id, state, research)
            if self._needs_experience_research(research):
                research = self._experience_research(project_id, state, research)
        initial_eval = SpecialistEvaluation(
            answered_task=bool(research.verified_facts or research.uncertainties),
            complete=research.status in {"complete", "partial"},
            evidence_safe=all(evidence_confidence(item) != "unsupported" for item in research.verified_facts),
            new_questions=list(research.critical_unknowns),
            next_action=("targeted_research" if research.critical_unknowns else "evidence_gate"),
            reason_code=("CRITICAL_UNKNOWNS" if research.critical_unknowns else "RESEARCH_READY_FOR_GATE"),
        )
        self._record_evaluation(project_id, brief, initial_eval, research.status)
        self._db.update_project_state(project_id, "research", research.to_dict(), status="researching")
        if research.status == "needs_clarification":
            question = research.recommended_followup_questions[0]
            self._db.upsert_session(
                thread_key, status="awaiting_clarification",
                pending_question_type="property_identity", pending_question_text=question,
            )
            return OrchestratorOutcome("", clarification=question, project_id=project_id)
        # Keep one round of the shared budget for Editorial's evidence requests: those
        # are concrete and actionable, while pre-editorial unknowns are broad and often
        # unresolvable (blocked sites), so spending every round here starves Editorial.
        for round_number in range(1, MAX_TARGETED_RESEARCH_ROUNDS):
            questions = list(research.critical_unknowns)[:4]
            if not questions:
                break
            followup_brief = self._research_brief(
                project_id=project_id, hotel=hotel, user_goal=user_text,
                known_research=research, questions=questions,
                task_id=f"research_verify_{round_number:02d}", verification_only=True,
            )
            research = self._call(
                "researcher", self._researcher.run,
                ResearchTask(
                    project_id=project_id, hotel_name=hotel, original_request=user_text,
                    followup_questions=questions, existing_research=research.to_dict(),
                    brief=followup_brief, verification_round=round_number,
                ), project_id, step_id=followup_brief.task_id,
            )
            self._targeted_research_rounds_used = round_number
            self._record_evaluation(
                project_id, followup_brief,
                SpecialistEvaluation(
                    answered_task=bool(research.verification_claims), complete=True,
                    evidence_safe=True, new_questions=list(research.critical_unknowns),
                    next_action=("targeted_research" if research.critical_unknowns and round_number < MAX_TARGETED_RESEARCH_ROUNDS - 1 else "evidence_gate"),
                    reason_code=("UNRESOLVED_CLAIMS" if research.critical_unknowns else "GAPS_CLOSED"),
                ), research.status,
            )
            self._db.update_project_state(project_id, "research", research.to_dict())
            if not research.critical_unknowns:
                break
        gated = self._gated_research(research)
        if not gated.verified_facts and not gated.experience_signals:
            self._finish_plan(project_id, state, stage="research_blocked", open_questions=research.critical_unknowns or research.uncertainties)
            self._db.upsert_session(thread_key, status="complete", research_project_id=project_id)
            return OrchestratorOutcome(
                "I completed the evidence pass, but there is not enough verified evidence to develop a defensible stay experience yet.\n\n"
                "Still unresolved:\n" + "\n".join(f"• {q}" for q in (research.critical_unknowns or research.uncertainties)[:8]),
                project_id=project_id,
            )
        outcome = self._run_editorial(project_id, hotel, research, user_text, {}, approve=False)
        self._db.upsert_session(
            thread_key, status="complete", research_project_id=project_id,
            latest_concept_ids=([outcome[1]] if outcome[1] else []),
        )
        self._finish_plan(project_id, state, stage="concept_ready" if outcome[1] else "research_ready", open_questions=research.critical_unknowns)
        return OrchestratorOutcome(outcome[0], project_id=project_id)

    def _editorial_flow(
        self, project_id: int, state: dict[str, Any], feedback: str, *, approve: bool,
    ) -> OrchestratorOutcome:
        research_data = state.get("research") or {}
        if not research_data:
            return OrchestratorOutcome("This project has no reusable research yet.", project_id=project_id)
        research = ResearchResult.from_dict(research_data)
        hotel = str(state.get("project", {}).get("hotel", ""))
        current = state.get("concept", {}).get("editorial") or state.get("concept", {})
        stored_format = (state.get("concept") or {}).get("format") or {}
        if self._trip_nights and (
            self._trip_nights_explicit or stored_format.get("type") != "itinerary"
            or (self._segment and stored_format.get("segment") != self.TRAVELLER_SEGMENTS[self._segment][0])
        ):
            # A single-experience project has little destination evidence; without it an
            # itinerary is mostly [TK] placeholders, so gather venues before editorial runs.
            # An itinerary project already researched them, unless a new length was asked.
            research = self._itinerary_research(project_id, state, research)
        if self._needs_experience_research(research):
            research = self._experience_research(project_id, state, research)
        if self.last_plan is None:
            self._begin_plan(
                project_id=project_id, state=state,
                intent=(Intent.DEVELOP_CONCEPT if approve else Intent.FIND_EDITORIAL_OPPORTUNITY),
                user_goal=feedback,
            )
        text, _concept_id = self._run_editorial(
            project_id, hotel, research, feedback, current, approve=approve
        )
        self._finish_plan(
            project_id, state, stage="concept_ready" if _concept_id else "research_ready",
            open_questions=research.critical_unknowns,
        )
        return OrchestratorOutcome(text, project_id=project_id)

    _DINING_WORDS = re.compile(
        r"restaurant|dining|dinner|michelin|bib gourmand|50 best|chef|cuisine|tasting menu", re.I,
    )

    # Facts that may say a hotel space is seasonal (open-air spaces, reopening dates).
    _SEASON_WORDS = re.compile(
        r"season|summer|winter|spring|autumn|reopen|open from|closed|rooftop|terrace|pool|courtyard|"
        r"garden|open-air|outdoor|deck", re.I,
    )
    # Proper names in a plan: capitalised runs, allowing Romance/English particles inside.
    _PROPER_NAME = re.compile(
        r"\b[A-Z][\w'’.-]+(?:\s+(?:[A-Z][\w'’.-]+|di|de|del|della|degli|dei|da|of|the|and|e|la|le|il))*"
    )
    _NOT_A_NAME = re.compile(
        r"^(?:Day|Morning|Afternoon|Evening|Night|Dinner|Lunch|Breakfast|Late|Early|Midday|Arrival|Departure|"
        r"Then|The|A|An|For|After|Before|Back|Return|TK|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b"
    )

    def _venue_notes(
        self, project_id: int, hotel: str, concept: dict[str, Any], research: ResearchResult,
        *, allow_research: bool = True,
    ) -> dict[str, Any]:
        """Why each stop is worth it, when it is closed, a dish, and which hotel spaces are open.

        One fast call over the gated facts that name the plan's places (and seasonal
        hotel facts). A note is kept only if its name appears in a fact it cites. Places
        with no reason to go get one short research round, then the notes are redone.
        """
        if not self._settings.venue_notes:
            return {}
        plan = " ".join(str(concept.get(k) or "") for k in ("possible_story_structure", "strongest_experience"))
        if not plan.strip():
            return {}
        names = {
            n.strip(" .'’") for n in self._PROPER_NAME.findall(plan)
            if len(n) > 3 and not self._NOT_A_NAME.match(n) and n.lower() not in hotel.lower()
        }
        facts, seen = [], set()
        for item in self._gated_research(research).verified_facts:
            text = fact_text(item) if isinstance(item, dict) else ""
            if not text or text in seen:
                continue
            lowered = text.lower()
            if any(n.lower() in lowered for n in names) or self._SEASON_WORDS.search(text):
                seen.add(text)
                facts.append(text)
        facts = facts[-160:]
        if not facts:
            return {}
        catalogue = [{"id": f"F{i}", "fact": text} for i, text in enumerate(facts, 1)]
        calendar = self._calendar_for(self._trip_nights)
        self._notify(STEP_PROGRESS_PREFIX + "Checking each stop: why go, closed days and seasons")
        try:
            response = self._fast_provider.complete(
                [LLMMessage(role="user", content=json.dumps({
                    "hotel": hotel, "calendar": calendar.lines() if calendar else [],
                    "plan": plan[:6000], "facts": catalogue,
                }, ensure_ascii=False))],
                system=load_prompt("venue_notes"), max_tokens=3000, temperature=0.1,
            )
        except AgentCancelled:
            raise
        except Exception:
            logger.warning("venue notes failed", exc_info=True)
            return {}
        data = extract_json_block(response.text)
        if not isinstance(data, dict):
            return {}
        by_id = {entry["id"]: entry["fact"].lower() for entry in catalogue}
        weekdays = {d.lower(): d for d in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")}

        def cited(entry: dict[str, Any]) -> list[str]:
            return [by_id[str(i)] for i in entry.get("facts") or [] if str(i) in by_id]

        venues = []
        for entry in data.get("venues") or []:
            name = str(entry.get("name") or "").strip() if isinstance(entry, dict) else ""
            if not name or not any(name.lower() in fact for fact in cited(entry)):
                continue
            venues.append({
                "name": name, "kind": str(entry.get("kind") or ""),
                **{k: str(entry.get(k) or "").strip() for k in ("hook", "dish", "booking", "free_or_fee", "night")},
                "closed_days": [
                    weekdays[str(d).strip().lower().rstrip("s")] for d in entry.get("closed_days") or []
                    if str(d).strip().lower().rstrip("s") in weekdays
                ],
            })
        spaces = [
            {"name": str(entry.get("name")).strip(), "open_on_trip": str(entry.get("open_on_trip")),
             "note": str(entry.get("note") or "").strip()}
            for entry in data.get("hotel_spaces") or []
            if isinstance(entry, dict) and entry.get("name") and cited(entry)
            and str(entry.get("open_on_trip")) in {"yes", "no", "unknown"}
        ]
        missing = [str(m).strip() for m in data.get("missing") or [] if str(m).strip()][:6]
        notes = {"venues": venues, "hotel_spaces": spaces} if venues or spaces else {}
        if missing and allow_research:
            questions = [
                f"{name} (near {hotel}): one specific reason it is worth visiting (its story, a famous work, "
                "the view, how it looks at night), its opening days and closures, and what to book ahead"
                for name in missing
            ]
            research = self._call(
                "researcher", self._researcher.run,
                ResearchTask(
                    project_id=project_id, hotel_name=hotel, original_request=self._active_goal,
                    followup_questions=questions, existing_research=research.to_dict(),
                    trip_nights=self._trip_nights, destination=True,
                ), project_id, step_id="research_venues",
            )
            self._db.update_project_state(project_id, "research", research.to_dict())
            return self._venue_notes(project_id, hotel, concept, research, allow_research=False) or notes
        return notes

    def _dining_picks(self, hotel: str, research: ResearchResult) -> list[dict[str, str]]:
        """Up to 3 named outside dinner restaurants for an itinerary, from gated evidence only.

        The editorial tends to leave dinners as [TK] even when the destination research
        names restaurants, so one fast call picks them up front. A pick is kept only if
        its name appears in a fact it cites, so a restaurant cannot be invented here.
        """
        seen: set[str] = set()
        facts = []
        for item in self._gated_research(research).verified_facts:
            text = fact_text(item) if isinstance(item, dict) else ""
            if (
                text and text not in seen and self._DINING_WORDS.search(text)
                and item.get("evidence_level") != "hotel_document"
            ):
                seen.add(text)
                facts.append(item)
        facts = facts[-80:]
        if len(facts) < 2:
            return []
        catalogue = [
            {"id": f"F{index}", "fact": fact_text(item), "source": self._source_label(research, item)}
            for index, item in enumerate(facts, 1)
        ]
        self._notify(STEP_PROGRESS_PREFIX + "Research: choosing dinner restaurants for the itinerary")
        try:
            response = self._fast_provider.complete(
                [LLMMessage(role="user", content=json.dumps(
                    {"hotel": hotel, "nights": self._trip_nights, "facts": catalogue}, ensure_ascii=False,
                ))],
                system=load_prompt("dining_picks"), max_tokens=1500, temperature=0.2,
            )
        except AgentCancelled:
            raise
        except Exception:
            logger.warning("dining picks failed", exc_info=True)
            return []
        data = extract_json_block(response.text)
        raw = data.get("picks") if isinstance(data, dict) else data if isinstance(data, list) else []
        by_id = {entry["id"]: entry for entry in catalogue}
        picks: list[dict[str, str]] = []
        for pick in (raw or [])[:3]:
            if not isinstance(pick, dict):
                continue
            name = str(pick.get("name") or "").strip()
            cited = [by_id[str(i)] for i in pick.get("fact_ids") or [] if str(i) in by_id]
            if name and any(name.lower() in entry["fact"].lower() for entry in cited):
                picks.append({
                    "name": name, "area": str(pick.get("area") or "").strip(),
                    "why": str(pick.get("why") or "").strip(),
                    "caveat": str(pick.get("caveat") or "").strip(), "source": cited[0]["source"],
                })
        return picks

    @staticmethod
    def _format_dining_picks(picks: Sequence[dict[str, str]]) -> str:
        if not picks:
            return ""
        lines = [
            f"- {p['name']}" + (f" ({p['area']})" if p["area"] else "") + f": {p['why']}"
            + (f" Check: {p['caveat']}." if p["caveat"] else "") + (f" [{p['source']}]" if p["source"] else "")
            for p in picks
        ]
        return (
            "DINNER PICKS, chosen from the destination evidence: when an evening dines outside the hotel, use "
            "one of these named restaurants instead of a [TK], paired with that day's area where possible, "
            "keeping each check as a [TK] note. Not every evening must go out: an evening at the hotel's own "
            "restaurant or bar is often the stronger reason to book:\n" + "\n".join(lines)
        )

    def _save_candidate(
        self, project_id: int, hotel: str, editorial: EditorialResult, critique: Any,
        research: ResearchResult, *, approve: bool,
    ) -> tuple[str, int | None]:
        """Shortlist a candidate the critic passed: state, concept memory, outline reply."""
        lifecycle = "approved" if approve else "shortlisted"
        concept_state = {
            "status": lifecycle, "version": editorial.version,
            "editorial": editorial.to_dict(), "critic_result": critique.to_dict(),
            "format": self._trip_format(),
        }
        self._db.update_project_state(project_id, "concept", concept_state, status="concept_ready")
        test_run = self._test_run or self._db.project_is_test(project_id)
        concept_id = self._db.insert_concept(
            project_id=project_id, source_project_id=project_id, hotel=hotel,
            name=editorial.working_title or editorial.editorial_proposition or editorial.strongest_experience[:80],
            proposition=editorial.editorial_proposition,
            experience=editorial.strongest_experience,
            narrative_device=editorial.possible_story_structure,
            visual_device=editorial.visual_storytelling_device,
            story_structure=editorial.possible_story_structure,
            central_tension=editorial.why_someone_would_want_it,
            keywords=list(self._tokens(" ".join((
                editorial.strongest_experience, editorial.editorial_proposition,
                editorial.visual_storytelling_device,
            ))))[:12], status="test" if test_run else lifecycle,
        )
        note = "\n_Test thread: not saved to concept memory._" if test_run else ""
        logistics: dict[str, Any] = {}
        if self._trip_nights:
            logistics = self._check_logistics(project_id, hotel, {"concept": {
                k: v for k, v in editorial.to_dict().items()
                if k in {"possible_story_structure", "strongest_experience", "editorial_proposition"}
            }})
            if logistics:
                concept_state["logistics"] = logistics
            venues = self._venue_notes(project_id, hotel, editorial.to_dict(), research)
            if venues:
                concept_state["venues"] = venues
        outline = self._editorial_outline(
            editorial, critique, research, logistics=logistics, venues=concept_state.get("venues"),
        )
        if outline:
            if logistics:
                outline["getting_around"] = self._getting_around(logistics)
            concept_state["outline"] = outline
            self._db.update_project_state(project_id, "concept", concept_state)
            return self._format_outline(
                outline, editorial, critique, lifecycle, research, itinerary=bool(self._trip_nights),
            ) + note, concept_id
        return self._format_editorial(editorial, critique, lifecycle, research) + note, concept_id

    def _run_editorial(
        self, project_id: int, hotel: str, research: ResearchResult, user_goal: str,
        current_concept: dict[str, Any], *, approve: bool,
    ) -> tuple[str, int | None]:
        memory = self._memory_payload(project_id)
        if self._trip_nights and not self._dining_note:
            self._dining_note = self._format_dining_picks(self._dining_picks(hotel, research))
        prior_failure = ""
        retry_concept: dict[str, Any] = {}
        last_editorial: EditorialResult | None = None
        last_critique: Any = None
        last_contract_gaps: list[str] = []
        repair_attempted = False
        final_repair_granted = False
        # A candidate the critic passed with revisions; kept in case the revision
        # pass itself breaks (unparseable or incomplete answer) and never reaches a critic.
        revisable: tuple[EditorialResult, Any] | None = None
        # One slot beyond the creative budget, used only to repair a final candidate that
        # is complete in substance but missing required fields (e.g. after a research wait).
        for attempt in range(1, MAX_EDITORIAL_ATTEMPTS + 2):
            if attempt > MAX_EDITORIAL_ATTEMPTS and not final_repair_granted:
                break
            # A critique only belongs to the candidate it actually evaluated.
            # This prevents a first candidate's rejection being persisted next
            # to an uncritiqued second candidate that requested more research.
            last_critique = None
            gated = self._gated_research(research)
            brief = self._editorial_brief(
                project_id=project_id, hotel=hotel, user_goal=user_goal,
                research=gated, attempt=attempt, prior_failure=prior_failure,
            )
            constraints = []
            if prior_failure:
                if retry_concept:
                    if "critic required revisions" in prior_failure.lower():
                        constraints.append(
                            f"The supplied candidate requires editorial revision: {prior_failure} "
                            "Apply every instruction without changing the evidence-safe core."
                        )
                    else:
                        constraints.append(
                            f"The supplied candidate was incomplete: {prior_failure} "
                            "Repair it and return non-empty values for every required editorial field."
                        )
                else:
                    constraints.append(f"Previous candidate failed: {prior_failure}. Produce a materially different hypothesis.")
            task = EditorialTask(
                project_id=project_id, hotel_name=hotel, research=gated,
                user_goal=user_goal,
                current_concept=(current_concept if attempt == 1 else retry_concept),
                user_feedback=(
                    user_goal if current_concept and attempt == 1 else
                    prior_failure if retry_concept else ""
                ),
                concept_memory=memory, constraints=constraints, brief=brief,
            )
            editorial = self._call(
                "editorial", self._editorial.run, task, project_id, step_id=brief.task_id
            )
            last_editorial = editorial
            # Only a completed candidate must satisfy the full editorial
            # contract. A needs_research result intentionally contains
            # questions instead of a finished proposition, while
            # no_strong_direction is a valid terminal outcome.
            contract_gaps = (
                self._editorial_contract_gaps(editorial)
                if editorial.status == "complete"
                else []
            )
            last_contract_gaps = contract_gaps
            editorial_eval = SpecialistEvaluation(
                answered_task=editorial.status != "failed" and not contract_gaps,
                complete=editorial.status in {"complete", "no_strong_direction"} and not contract_gaps,
                evidence_safe=not self._editorial_has_low_confidence_dependency(editorial, research),
                new_questions=list(editorial.research_questions or editorial.open_questions),
                next_action=("editorial_retry" if contract_gaps else (
                    "targeted_research" if editorial.status == "needs_research" else "critic"
                )),
                reason_code=("INCOMPLETE_OUTPUT" if contract_gaps else editorial.status.upper()),
            )
            self._record_evaluation(project_id, brief, editorial_eval, editorial.status)

            if contract_gaps:
                retry_concept = editorial.to_dict()
                prior_failure = (
                    "The candidate was structurally incomplete and omitted required fields: "
                    + ", ".join(contract_gaps)
                    + ". The next candidate must fully explain the experience, desire, property advantage, story, and visual device."
                )
                if repair_attempted:
                    break
                repair_attempted = True
                final_repair_granted = attempt >= MAX_EDITORIAL_ATTEMPTS
                continue

            if editorial.status == "needs_research" and editorial.research_questions:
                if self._targeted_research_rounds_used >= MAX_TARGETED_RESEARCH_ROUNDS:
                    break
                self._targeted_research_rounds_used += 1
                research_round = self._targeted_research_rounds_used
                research_brief = self._research_brief(
                    project_id=project_id, hotel=hotel, user_goal=user_goal,
                    known_research=research, questions=editorial.research_questions,
                    task_id=f"research_editorial_{research_round:02d}", verification_only=True,
                )
                research = self._call(
                    "researcher", self._researcher.run,
                    ResearchTask(
                        project_id=project_id, hotel_name=hotel, original_request=user_goal,
                        followup_questions=editorial.research_questions,
                        existing_research=research.to_dict(), brief=research_brief,
                        verification_round=research_round,
                    ), project_id, step_id=research_brief.task_id,
                )
                self._db.update_project_state(project_id, "research", research.to_dict())
                self._record_evaluation(
                    project_id, research_brief,
                    SpecialistEvaluation(
                        answered_task=bool(research.verification_claims), complete=True,
                        evidence_safe=True, new_questions=list(research.critical_unknowns),
                        next_action="editorial_retry", reason_code="EDITORIAL_EVIDENCE_FOLLOWUP",
                    ), research.status,
                )
                prior_failure = "The first hypothesis depended on unresolved evidence."
                continue
            if editorial.status in {"no_strong_direction", "failed"}:
                prior_failure = "No concrete, desirable, property-specific experience was produced."
                if attempt < MAX_EDITORIAL_ATTEMPTS:
                    continue
                break

            critic_brief = self._critic_brief(
                project_id=project_id, mode="EDITORIAL", user_goal=user_goal,
                task_id=f"critic_{attempt:02d}",
            )
            critique = self._call(
                "critic_editorial", self._critic.run,
                CritiqueTask(
                    project_id=project_id, mode="EDITORIAL", concept=editorial.to_dict(),
                    research=self._gated_research(research).to_dict(), concept_memory=memory,
                    brief=critic_brief,
                ), project_id, step_id=critic_brief.task_id,
            )
            revisable = None
            central_evidence_gap = bool(self._central_evidence_blockers(critique))
            if self._editorial_has_low_confidence_dependency(editorial, research) or central_evidence_gap:
                critique.verdict = "FAIL"
                critique.fail_reason = "EVIDENCE_DEPENDENCY"
                gate_reason = (
                    "The critic identified an unverified or contradicted central claim, so the "
                    "candidate cannot be shortlisted."
                    if central_evidence_gap else
                    "The central proposition depends on evidence that is not editorial-safe."
                )
                if gate_reason not in critique.reasons:
                    critique.reasons.append(gate_reason)
            last_critique = critique
            self._record_evaluation(
                project_id, critic_brief,
                SpecialistEvaluation(
                    answered_task=True, complete=True,
                    evidence_safe=critique.fail_reason != "EVIDENCE_DEPENDENCY",
                    new_questions=list(critique.unresolved_concerns),
                    next_action=("editorial_revision" if (
                        critique.verdict == "PASS_WITH_REVISIONS"
                        and critique.revision_instructions
                        and attempt < MAX_EDITORIAL_ATTEMPTS
                    ) else "save_concept" if critique.verdict != "FAIL" else (
                        "targeted_research" if critique.fail_reason == "EVIDENCE_DEPENDENCY" else "new_hypothesis"
                    )), reason_code=critique.fail_reason or critique.verdict,
                ), critique.verdict,
            )
            if (
                critique.verdict == "PASS_WITH_REVISIONS"
                and critique.revision_instructions
                and attempt < MAX_EDITORIAL_ATTEMPTS
            ):
                revisable = (editorial, critique)
                retry_concept = editorial.to_dict()
                prior_failure = (
                    "Critic required revisions: "
                    + " ".join(str(item) for item in critique.revision_instructions)
                )
                continue
            if critique.verdict != "FAIL":
                return self._save_candidate(project_id, hotel, editorial, critique, research, approve=approve)

            prior_failure = "; ".join(critique.reasons) or critique.fail_reason or "Critic rejected the candidate."
            if critique.fail_reason == "EVIDENCE_DEPENDENCY" and self._targeted_research_rounds_used < MAX_TARGETED_RESEARCH_ROUNDS:
                questions = list(critique.unresolved_concerns or editorial.open_questions or editorial.research_questions)
                if questions:
                    self._targeted_research_rounds_used += 1
                    research_round = self._targeted_research_rounds_used
                    research_brief = self._research_brief(
                        project_id=project_id, hotel=hotel, user_goal=user_goal,
                        known_research=research, questions=questions,
                        task_id=f"research_editorial_{research_round:02d}", verification_only=True,
                    )
                    research = self._call(
                        "researcher", self._researcher.run,
                        ResearchTask(
                            project_id=project_id, hotel_name=hotel, original_request=user_goal,
                            followup_questions=questions, existing_research=research.to_dict(),
                            brief=research_brief, verification_round=research_round,
                        ), project_id, step_id=research_brief.task_id,
                    )
                    self._db.update_project_state(project_id, "research", research.to_dict())
            if attempt < MAX_EDITORIAL_ATTEMPTS:
                continue

        if revisable is not None:
            editorial, critique = revisable
            return self._save_candidate(project_id, hotel, editorial, critique, research, approve=approve)
        if last_editorial is not None:
            rejected_state = {
                "status": "rejected", "version": last_editorial.version,
                "editorial": last_editorial.to_dict(),
                "critic_result": last_critique.to_dict() if last_critique else {},
            }
            self._db.update_project_state(project_id, "concept", rejected_state, status="research_ready")
        if last_contract_gaps:
            labels = {
                "strongest_experience": "strongest experience",
                "why_someone_would_want_it": "why someone would want it",
                "why_this_property_enables_it": "why this property enables it",
                "editorial_proposition": "editorial proposition",
                "possible_story_structure": "story structure",
                "visual_storytelling_device": "visual storytelling device",
            }
            missing = ", ".join(labels.get(field, field) for field in last_contract_gaps)
            return (
                "I couldn't produce a complete evidence-supported editorial recommendation after "
                "two bounded attempts. The final candidate was rejected because it omitted required "
                f"content: {missing}.",
                None,
            )
        subject = "hotel recommendation and itinerary" if self._trip_nights else "stay experience"
        heading, items = "", []
        if last_critique is not None and last_critique.fail_reason == "EVIDENCE_DEPENDENCY":
            blockers = self._central_evidence_blockers(last_critique)
            if blockers and last_editorial is not None:
                return self._format_near_miss(last_editorial, blockers), None
            heading = "Blocking central claims"
            items = blockers or list(last_critique.unresolved_concerns or [])[:4]
        elif last_critique is not None and last_critique.reasons:
            heading, items = "Critic decision", list(last_critique.reasons[:2])
        else:
            heading = "Remaining evidence gaps"
            items = list(last_editorial.research_questions if last_editorial else [])
        detail = ""
        if items:
            shown = items[:5]
            detail = f"\n\n*{heading}:*\n" + "\n".join(f"• {item}" for item in shown)
            if len(items) > len(shown):
                detail += f"\n• …and {len(items) - len(shown)} more"
        return (
            f"I couldn't find a sufficiently strong, evidence-supported {subject} after two bounded editorial attempts."
            + detail,
            None,
        )

    @staticmethod
    def _format_near_miss(editorial: EditorialResult, blockers: Sequence[str]) -> str:
        """The best candidate held back only by facts the hotel can confirm, and how to unlock it."""
        title = editorial.working_title or "Best candidate"
        pitch = editorial.editorial_proposition or editorial.strongest_experience
        if len(pitch) > 400:
            pitch = pitch[:400].rsplit(" ", 1)[0] + "…"
        lines = [
            f"*Almost there: {title}*", pitch, "",
            "*Confirm with the hotel to unlock it:*",
            *[f"• {claim}" for claim in blockers[:4]], "",
            "Paste the hotel's answer here starting with `Hotel confirmed:`, or share their fact sheet "
            "or email as a file, and I'll re-check it with that evidence.",
        ]
        return "\n".join(lines)

    @staticmethod
    def _editorial_contract_gaps(editorial: EditorialResult) -> list[str]:
        required = {
            "strongest_experience": editorial.strongest_experience,
            "why_someone_would_want_it": editorial.why_someone_would_want_it,
            "why_this_property_enables_it": editorial.why_this_property_enables_it,
            "editorial_proposition": editorial.editorial_proposition,
            "possible_story_structure": editorial.possible_story_structure,
            "visual_storytelling_device": editorial.visual_storytelling_device,
        }
        return [name for name, value in required.items() if not str(value).strip()]

    @staticmethod
    def _central_evidence_blockers(critique: Any) -> list[str]:
        """Return only central evidence gaps that can invalidate the experience.

        Missing impact metadata stays fail-closed for backward compatibility;
        the critic prompt now explicitly distinguishes revisable details. A
        contradicted claim the critic marks revisable (an overstatement the
        evidence corrects, e.g. an unbounded group size where the source gives
        a maximum) is a copy fix, not a missing experience.
        """
        blockers: list[str] = []
        for match in getattr(critique, "evidence_matches", []) or []:
            if not isinstance(match, dict) or match.get("central") is not True:
                continue
            status = str(match.get("status") or "").strip().lower()
            impact = str(match.get("impact") or "existential").strip().lower()
            if status in {"unverified", "contradicted"} and impact != "revisable":
                blockers.append(str(match.get("claim") or "Unverified central claim"))
        return blockers

    def _critique_concept(self, project_id: int, state: dict[str, Any]) -> OrchestratorOutcome:
        concept = state.get("concept", {}).get("editorial") or {}
        if self.last_plan is None:
            self._begin_plan(
                project_id=project_id, state=state, intent=Intent.CRITIQUE_CONCEPT,
                user_goal=self._active_goal,
            )
        brief = self._critic_brief(
            project_id=project_id, mode="EDITORIAL", user_goal=self._active_goal,
            task_id="critic_01",
        )
        critique = self._call(
            "critic_editorial", self._critic.run,
            CritiqueTask(
                project_id=project_id, mode="EDITORIAL", concept=concept,
                research=state.get("research") or {}, concept_memory=self._memory_payload(project_id),
                brief=brief,
            ), project_id, step_id=brief.task_id,
        )
        self._record_evaluation(
            project_id, brief,
            SpecialistEvaluation(
                answered_task=True, complete=True,
                evidence_safe=critique.fail_reason != "EVIDENCE_DEPENDENCY",
                new_questions=list(critique.unresolved_concerns), next_action="respond",
                reason_code=critique.fail_reason or critique.verdict,
            ), critique.verdict,
        )
        self._finish_plan(project_id, state, stage="concept_critiqued")
        status_note = self._apply_critique_status(project_id, state, critique)
        summary = self._summarize_critique(critique, concept)
        if summary:
            text = self._format_critique_summary(summary, critique)
        else:
            reasons = "\n".join(f"- {reason}" for reason in critique.reasons)
            text = f"Editorial verdict: {critique.verdict}\n\n{reasons}"
        return OrchestratorOutcome(text + status_note, project_id=project_id)

    @staticmethod
    def _status_after_critique(current: str, verdict: str) -> str | None:
        """Lifecycle change implied by a stand-alone re-critique, or None to keep it."""
        if verdict == "FAIL":
            return None if current in {"rejected", "published"} else "rejected"
        if verdict in {"PASS", "PASS_WITH_REVISIONS"} and current in {"", "generated", "rejected"}:
            return "shortlisted"
        return None

    def _apply_critique_status(self, project_id: int, state: dict[str, Any], critique: Any) -> str:
        """Keep the concept lifecycle (project state + concept memory) in line with the latest critique."""
        concept_state = dict(state.get("concept") or {})
        if not concept_state:
            return ""
        new_status = self._status_after_critique(str(concept_state.get("status") or ""), str(critique.verdict))
        if new_status is None:
            return ""
        concept_state["status"] = new_status
        concept_state["critic_result"] = critique.to_dict() if hasattr(critique, "to_dict") else {}
        self._db.update_project_state(project_id, "concept", concept_state)
        concept_id = self._db.latest_concept_id(project_id)
        if concept_id is not None:
            self._db.update_concept_status(concept_id, new_status)
        return f"\n\n_Concept status updated to {new_status}._"

    def _summarize_critique(self, critique: Any, concept: dict[str, Any]) -> dict[str, Any] | None:
        """Condense a full critique into a short actionable note (one fast LLM call)."""
        payload = {
            "concept": {k: concept.get(k) for k in ("working_title", "strongest_experience", "editorial_proposition")},
            "critique": {
                "verdict": critique.verdict,
                "fail_reason": getattr(critique, "fail_reason", ""),
                "reasons": list(critique.reasons or []),
                "revision_instructions": list(getattr(critique, "revision_instructions", []) or []),
                "unresolved_concerns": list(getattr(critique, "unresolved_concerns", []) or []),
            },
        }
        try:
            self._notify(STEP_PROGRESS_PREFIX + "Writing up the critique")
            response = self._fast_provider.complete(
                [LLMMessage(role="user", content=json.dumps(payload, ensure_ascii=False))],
                system=load_prompt("critique_summary"), max_tokens=900, temperature=0.2,
            )
        except AgentCancelled:
            raise
        except Exception:
            logger.exception("critique summary failed; falling back to the full critique")
            return None
        data = extract_json_block(response.text)
        if not isinstance(data, dict) or not data.get("headline") or not data.get("why"):
            return None
        return data

    @staticmethod
    def _format_critique_summary(summary: dict[str, Any], critique: Any) -> str:
        clean = EditorialOrchestrator._strip_source_ids

        def items(key: str, limit: int) -> list[str]:
            values = summary.get(key) or []
            return [clean(str(v)) for v in (values if isinstance(values, list) else [values]) if str(v).strip()][:limit]

        verdict = str(critique.verdict).replace("_", " ").title()
        parts = [f"**Critic: {verdict}**", clean(str(summary.get("headline", "")))]
        for heading, values in (("Why", items("why", 3)), ("To fix", items("to_fix", 3))):
            if values:
                parts.append(f"**{heading}**\n" + "\n".join(f"• {v}" for v in values))
        next_step = clean(str(summary.get("next_step") or ""))
        if next_step:
            parts.append(f"_Next: {next_step}_")
        return "\n\n".join(part for part in parts if part)

    def _design_flow(
        self, project_id: int, state: dict[str, Any], feedback: str,
    ) -> OrchestratorOutcome:
        concept_state = state.get("concept") or {}
        concept = concept_state.get("editorial") or {}
        if not concept:
            return OrchestratorOutcome("There is no developed concept to design yet.", project_id=project_id)
        if concept_state.get("status") not in {"shortlisted", "approved"}:
            return OrchestratorOutcome(
                "The current concept has not passed the editorial gate, so I won't start visual direction yet.",
                project_id=project_id,
            )
        if self.last_plan is None:
            self._begin_plan(
                project_id=project_id, state=state, intent=Intent.CREATE_VISUAL_DIRECTION,
                user_goal=feedback,
            )
        spec = self._make_design(project_id, state, feedback)
        self._finish_plan(project_id, state, stage="design_ready")
        return OrchestratorOutcome(
            "Visual direction is ready.\n\n"
            f"Visual grammar: {spec.visual_grammar}\n\n"
            f"Page narrative: {' → '.join(spec.page_narrative)}",
            project_id=project_id,
        )

    def _make_design(self, project_id: int, state: dict[str, Any], feedback: str) -> DesignSpec:
        prior = state.get("design", {}).get("spec") or {}
        research = state.get("research") or {}
        concept = state.get("concept", {}).get("editorial") or {}
        assets = list(research.get("available_assets") or [])
        brief = self._design_brief(project_id, concept, assets, feedback)
        spec = self._call(
            "designer", self._designer.run,
            DesignTask(
                project_id=project_id, concept=concept, research=research,
                assets=assets, user_feedback=feedback, previous_design=prior,
                brief=brief,
            ), project_id, step_id=brief.task_id,
        )
        self._record_evaluation(
            project_id, brief,
            SpecialistEvaluation(
                answered_task=bool(spec.visual_grammar and spec.page_narrative),
                complete=bool(spec.section_structure), evidence_safe=True,
                next_action="save_design", reason_code="DESIGN_SPEC_READY",
            ), spec.status,
        )
        version = self._db.save_design_version(
            project_id, spec.to_dict(), status="approved", feedback=feedback
        )
        spec.version = version
        spec.status = "approved"
        self._db.update_project_state(project_id, "design", {
            "status": "approved", "version": version, "spec": spec.to_dict(), "feedback": feedback,
        }, status="design_ready")
        return spec

    def _mockup_flow(
        self, project_id: int, state: dict[str, Any], feedback: str, *, revise: bool,
    ) -> OrchestratorOutcome:
        concept = state.get("concept", {}).get("editorial") or {}
        if not concept:
            return OrchestratorOutcome("There is no concept to build yet.", project_id=project_id)
        if state.get("concept", {}).get("status") not in {"shortlisted", "approved"}:
            return OrchestratorOutcome(
                "The current concept has not passed the editorial gate, so I won't build it yet.",
                project_id=project_id,
            )
        if self.last_plan is None:
            self._begin_plan(
                project_id=project_id, state=state,
                intent=(Intent.REVISE_MOCKUP if revise else Intent.CREATE_HTML_MOCKUP),
                user_goal=feedback,
            )
        design_data = state.get("design", {}).get("spec") or {}
        if revise:
            current_artifact = self._db.current_artifact(project_id)
            if current_artifact is None:
                return OrchestratorOutcome("There is no mockup to revise yet.", project_id=project_id)
            visual_feedback = self._call(
                "critic_visual", self._critic.run,
                CritiqueTask(
                    project_id=project_id, mode="VISUAL", concept=concept,
                    research=state.get("research") or {}, design_spec=design_data,
                    artifact=self._artifact_payload(dict(current_artifact)),
                    brief=self._critic_brief(
                        project_id=project_id, mode="VISUAL", user_goal=feedback,
                        task_id="critic_visual_existing",
                    ),
                ), project_id,
            )
            feedback = feedback + "\n" + "\n".join(visual_feedback.revision_instructions)
            spec = self._make_design(project_id, state, feedback)
        elif design_data:
            spec = DesignSpec.from_dict(design_data)
        else:
            spec = self._make_design(project_id, state, feedback="")

        artifact: ArtifactResult | None = None
        critique = None
        prior = self._db.current_artifact(project_id)
        for cycle in range(MAX_VISUAL_REVISION_CYCLES + 1):
            try:
                assets = list((state.get("research") or {}).get("available_assets") or [])
                builder_brief = self._builder_brief(project_id, concept, spec, assets)
                artifact = self._call(
                    "builder", self._builder.run,
                    BuildTask(
                        project_id=project_id,
                        hotel_slug=str(state.get("project", {}).get("hotel_slug") or self._slug(state.get("project", {}).get("hotel", "project"))),
                        concept=concept, design_spec=spec,
                        assets=assets,
                        previous_artifact=dict(prior) if prior else {},
                        brief=builder_brief,
                    ), project_id, step_id=builder_brief.task_id,
                )
                self._record_evaluation(
                    project_id, builder_brief,
                    SpecialistEvaluation(
                        answered_task=bool(artifact.path), complete=bool(artifact.files),
                        evidence_safe=True, next_action="visual_critique",
                        reason_code="ARTIFACT_BUILT",
                    ), artifact.status,
                )
            except Exception as exc:
                logger.warning("builder failed while preserving project_id=%s: %s", project_id, exc)
                return OrchestratorOutcome(
                    "The concept and design are intact, but HTML generation failed. "
                    "Nothing was overwritten; you can retry the mockup stage.",
                    project_id=project_id,
                )
            critic_brief = self._critic_brief(
                project_id=project_id, mode="VISUAL", user_goal=feedback,
                task_id=f"critic_visual_{cycle + 1:02d}",
            )
            critique = self._call(
                "critic_visual", self._critic.run,
                CritiqueTask(
                    project_id=project_id, mode="VISUAL", concept=concept,
                    research=state.get("research") or {}, design_spec=spec.to_dict(),
                    artifact=self._artifact_payload(artifact.to_dict()),
                    brief=critic_brief,
                ), project_id, step_id=critic_brief.task_id,
            )
            self._record_evaluation(
                project_id, critic_brief,
                SpecialistEvaluation(
                    answered_task=True, complete=True, evidence_safe=True,
                    new_questions=list(critique.unresolved_concerns),
                    next_action=("deliver" if critique.verdict == "PASS" else "revise_design"),
                    reason_code=critique.verdict,
                ), critique.verdict,
            )
            if critique.verdict == "PASS" or cycle >= MAX_VISUAL_REVISION_CYCLES:
                break
            revised_state = dict(state)
            revised_state["design"] = {"spec": spec.to_dict()}
            spec = self._make_design(
                project_id, revised_state, "\n".join(critique.revision_instructions)
            )
            prior = self._db.current_artifact(project_id)
        assert artifact is not None and critique is not None
        artifact_state = artifact.to_dict() | {
            "critic_result": critique.to_dict(), "status": "current",
        }
        self._db.update_project_state(project_id, "artifact", artifact_state, status="artifact_ready")
        self._finish_plan(project_id, state, stage="artifact_ready")
        changes = "\n".join(f"- {x}" for x in critique.reasons[:4])
        unresolved = "\n".join(f"- {x}" for x in critique.unresolved_concerns[:3])
        message = (
            f"Mockup v{artifact.version} is ready.\n\nVisual verdict: {critique.verdict}."
            + (f"\n\nWhat changed / what works:\n{changes}" if changes else "")
            + (f"\n\nUnresolved:\n{unresolved}" if unresolved else "")
            + f"\n\nArtifact: {artifact.path}"
        )
        return OrchestratorOutcome(
            message, project_id=project_id, artifact_path=artifact.path
        )

    def _answer_direct(
        self, user_text: str, history: Sequence[tuple[str, str]],
        state: dict[str, Any], intent: str,
    ) -> OrchestratorOutcome:
        if intent in {Intent.META_QUESTION, Intent.HELP}:
            return self._meta_answer(state)
        prompt = (
            f"INTENT={intent}\nUSER={user_text}\nRECENT="
            + json.dumps(list(history[-5:]), ensure_ascii=False)
            + "\nPROJECT STATE=" + self._answer_context(state)
            + "\nAnswer concisely in English by default. Use another language only when the "
            "user explicitly asks for that output language; the language of their message alone "
            "is not a request to switch languages. Do not claim you called a specialist."
        )
        response = self._provider.complete(
            [LLMMessage(role="user", content=prompt)],
            system=(
                "You are the orchestrator for an internal Suite Stories HOTEL research and "
                "editorial ideation workflow. The product researches hotels, discovers sellable "
                "stay experiences, develops editorial concepts, creates visual directions, and "
                "builds versioned HTML story mockups. It is not a luxury-goods sourcing, shopping, "
                "acquisition, watches, jewelry, fashion, or collectibles service. Explain current "
                "state or prior decisions only from supplied state. Always answer in English unless "
                "the user explicitly requests another output language. Never start research in "
                "this response."
            ), max_tokens=2200, temperature=0.35,
        )
        return OrchestratorOutcome(
            response.text.strip() or "I don't have enough saved context to answer that yet.",
            project_id=state.get("project", {}).get("id"),
        )

    @staticmethod
    def _answer_context(state: dict[str, Any]) -> str:
        """Project state for a direct answer: the concept and latest draft first, research compacted.

        Slicing the whole state JSON dropped the concept and draft (they come after the
        large research block), so questions about the draft were answered by guesswork.
        """
        project = {k: v for k, v in (state.get("project") or {}).items() if not str(k).endswith("_state")}
        concept = dict(state.get("concept") or {})
        draft = dict(concept.pop("sample_draft", None) or {})
        latest_draft = {
            "headline": draft.get("headline", ""),
            "tk_gaps": draft.get("tk_items", []),
            "qa_verdict": (draft.get("qa") or {}).get("verdict", ""),
            "qa_revisions": (draft.get("qa") or {}).get("revision_instructions", []),
            "writer": draft.get("model", ""),
            "layout_file": draft.get("layout_html", ""),
        } if draft else {}
        # Most specific first: the draft and its gaps, then the concept, then the rest.
        head = json.dumps({
            "latest_sample_draft": latest_draft, "concept": concept, "project": project,
            "design": state.get("design", {}), "artifact": state.get("artifact", {}),
        }, ensure_ascii=False, default=str)[:14000]
        research = compact_research_json(state.get("research") or {}, budget_chars=12000)
        return head + "\nRESEARCH=" + research

    @staticmethod
    def _meta_answer(state: dict[str, Any]) -> OrchestratorOutcome:
        project = state.get("project") or {}
        if project:
            current = (
                f"Current thread: {project.get('hotel', 'hotel project')} "
                f"(project {project.get('id', '—')})."
            )
        else:
            current = "Current thread: no hotel project has been started yet."
        return OrchestratorOutcome(
            "I'm the Suite Stories hotel editorial orchestrator. I coordinate a focused workflow "
            "for researching hotels and turning verified property experiences into editorial "
            "concepts and visual stories.\n\n"
            "I can:\n"
            "- Research a hotel and its destination using evidence-backed sources.\n"
            "- Identify and develop one property-specific stay experience worth promoting.\n"
            "- Critique the concept for desirability, specificity, and factual support.\n"
            "- Create a visual direction and a versioned HTML editorial mockup.\n"
            "- Revise or restore earlier mockup versions without restarting the research.\n\n"
            f"{current}",
            project_id=project.get("id"),
        )

    def _project_status(self, state: dict[str, Any]) -> OrchestratorOutcome:
        if not state:
            return OrchestratorOutcome("There is no active project in this thread.")
        project = state.get("project", {})
        return OrchestratorOutcome(
            f"Project {project.get('id')}: {project.get('hotel')}\n"
            f"Research: {state.get('research', {}).get('status', 'not started')}\n"
            f"Concept: {state.get('concept', {}).get('status', 'not started')}\n"
            f"Design: {state.get('design', {}).get('status', 'not started')}\n"
            f"Artifact: v{state.get('artifact', {}).get('version', '—')}",
            project_id=project.get("id"),
        )

    def _workflow_summary(self, state: dict[str, Any], thread_key: str) -> OrchestratorOutcome:
        project = state.get("project") or {}
        project_id = project.get("id")
        rows = self._db.workflow_trace(
            project_id=int(project_id) if project_id is not None else None,
            thread_key=thread_key, limit=40,
        )
        if not rows:
            return OrchestratorOutcome("No specialist workflow has been recorded for this thread yet.", project_id=project_id)
        latest_goal = str(rows[0]["goal"] or "")
        relevant = [row for row in reversed(rows) if str(row["goal"] or "") == latest_goal]
        display_names = {
            "researcher": "ResearchAgent", "editorial": "EditorialAgent",
            "critic_editorial": "CriticAgent", "critic_visual": "CriticAgent",
            "designer": "DesignAgent", "builder": "BuilderAgent",
            "research": "ResearchAgent", "critic": "CriticAgent",
        }
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        order: list[tuple[str, str]] = []
        for row in relevant:
            specialist = str(row["specialist"] or "")
            if not specialist:
                continue
            label = display_names.get(specialist, specialist)
            key = (str(row["step_id"]), label)
            if key not in grouped:
                grouped[key] = {
                    "label": label,
                    "mission": str(row["brief_summary"] or row["next_action"] or "specialist task"),
                    "result_status": str(row["result_status"] or ""),
                    "evaluation": {},
                }
                order.append(key)
            record = grouped[key]
            if row["brief_summary"]:
                record["mission"] = str(row["brief_summary"])
            if row["result_status"]:
                record["result_status"] = str(row["result_status"])
            try:
                evaluation = json.loads(str(row["evaluation"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                evaluation = {}
            if evaluation:
                record["evaluation"] = evaluation

        totals: dict[str, int] = {}
        for key in order:
            label = str(grouped[key]["label"])
            totals[label] = totals.get(label, 0) + 1
        positions: dict[str, int] = {}
        lines: list[str] = []
        for key in order:
            record = grouped[key]
            label = str(record["label"])
            positions[label] = positions.get(label, 0) + 1
            annotations: list[str] = []
            if totals[label] > 1:
                noun = "attempt" if label == "EditorialAgent" else "pass"
                annotations.append(f"{noun} {positions[label]}/{totals[label]}")
            evaluation = record["evaluation"]
            reason = str(evaluation.get("reason_code") or "")
            next_action = str(evaluation.get("next_action") or "")
            if reason == "INCOMPLETE_OUTPUT":
                annotations.append("incomplete output; retried")
            elif next_action == "critic":
                annotations.append("complete; sent to CriticAgent")
            elif next_action == "editorial_revision":
                annotations.append("revisions requested; returned to EditorialAgent")
            elif next_action == "save_concept":
                annotations.append(f"{record['result_status']}; saved")
            suffix = f" ({'; '.join(annotations)})" if annotations else ""
            lines.append(f"{len(lines) + 1}. {label} — {record['mission']}{suffix}")
        orchestration = state.get("orchestration") or {}
        constraints = list(orchestration.get("active_constraints") or [])
        notes = []
        if "editorial_agent=forbidden" in constraints:
            notes.append("EditorialAgent was not called because concept mutation was forbidden for the task.")
        if "design_agent=forbidden" in constraints:
            notes.append("DesignAgent was not called for this research-only task.")
        if "builder_agent=forbidden" in constraints:
            notes.append("BuilderAgent was not called for this research-only task.")
        body = "This turn:\n" + ("\n".join(lines) if lines else "No specialists were called.")
        if notes:
            body += "\n\n" + "\n".join(notes)
        return OrchestratorOutcome(body, project_id=project_id)

    def _clarify(
        self, route: dict[str, Any], user_text: str, *, user_id: str,
        thread_key: str, channel_id: str, thread_ts: str,
    ) -> OrchestratorOutcome:
        question = str(route.get("clarification") or "Which hotel should I research?")
        self._db.upsert_session(
            thread_key, thread_ts=thread_ts or None, channel_id=channel_id or None,
            user_id=user_id, status="awaiting_clarification",
            original_request=user_text, current_goal=str(route.get("goal") or user_text),
            pending_question_type="property_identity", pending_question_text=question,
        )
        return OrchestratorOutcome("", clarification=question)

    def _call(
        self, name: str, fn: Callable[[Any], Any], task: Any, project_id: int,
        *, step_id: str = "",
    ) -> Any:
        if self._cancel_check():
            raise AgentCancelled("Task cancelled")
        started = time.monotonic()
        self.last_trace.append(name)
        brief = getattr(task, "brief", None)
        resolved_step = step_id or (brief.task_id if brief else name)
        self._notify(STEP_PROGRESS_PREFIX + self._step_label(name, resolved_step))
        if self.last_plan:
            self.last_plan.set_status(resolved_step, "running")
        brief_summary = brief.mission if brief else ""
        logger.info(
            "orchestration_step project_id=%s goal=%s step=%s specialist=%s brief=%s status=running",
            project_id, self._active_goal[:160], resolved_step, name, brief_summary[:240],
        )
        try:
            result = fn(task)
            if self.last_plan:
                self.last_plan.set_status(resolved_step, "complete")
            result_status = str(getattr(result, "status", getattr(result, "verdict", "complete")))
            self._db.append_workflow_trace(
                project_id=project_id, thread_key=self._active_thread_key,
                goal=self._active_goal, step_id=resolved_step, specialist=name,
                brief_summary=brief_summary, result_status=result_status,
                next_action="inspect_result", response_mode=self._response_mode,
            )
            logger.info(
                "specialist project_id=%s agent=%s duration_ms=%d result_status=success",
                project_id, name, int((time.monotonic() - started) * 1000),
            )
            return result
        except Exception:
            if self.last_plan:
                self.last_plan.set_status(resolved_step, "failed")
            self._db.append_workflow_trace(
                project_id=project_id, thread_key=self._active_thread_key,
                goal=self._active_goal, step_id=resolved_step, specialist=name,
                brief_summary=brief_summary, result_status="failed",
                next_action="stop_or_recover", response_mode=self._response_mode,
            )
            logger.exception(
                "specialist project_id=%s agent=%s duration_ms=%d result_status=failed",
                project_id, name, int((time.monotonic() - started) * 1000),
            )
            raise

    def _memory_payload(self, project_id: int) -> list[dict[str, Any]]:
        return [
            {
                "hotel": c.hotel, "concept": c.name, "status": c.status,
                "experience": c.experience, "proposition": c.proposition,
                "narrative_device": c.narrative_device, "visual_device": c.visual_device,
                "source_project_id": c.source_project_id,
                "signal": "strong" if c.status in {"approved", "published"} else (
                    "moderate" if c.status == "shortlisted" else "weak"
                ),
            }
            for c in self._db.recent_concepts(exclude_project_id=project_id, limit=30)
        ]

    def _check_logistics(self, project_id: int, hotel: str, plan: dict[str, Any]) -> dict[str, Any]:
        """Travel times between an itinerary's stops; {} when the check is off or fails."""
        if self._logistics is None or not hotel:
            return {}
        try:
            result = self._call("logistics", self._logistics.run, LogisticsTask(
                hotel=hotel, plan=plan, segment=self._segment,
            ), project_id, step_id="logistics")
        except AgentCancelled:
            raise
        except Exception:
            logger.exception("logistics check failed; continuing without it")
            return {}
        return result.to_dict() if result.status == "complete" else {}

    @staticmethod
    def _getting_around(logistics: dict[str, Any]) -> list[str]:
        issues = [str(i) for i in logistics.get("issues") or []]
        checked = sum(1 for leg in logistics.get("legs") or [] if leg.get("verdict") != "unknown")
        if not issues:
            return [f"All {checked} checked legs fit the plan's times (map estimates, no live traffic)."]
        return issues[:3] + ["Map estimates, no live traffic: confirm transfers with the hotel."]

    def _editorial_outline(
        self, editorial: EditorialResult, critique: Any, research: ResearchResult,
        *, logistics: dict[str, Any] | None = None, venues: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Condense the concept + critic notes into a writing framework (one fast LLM call)."""
        supporting = {str(sid) for sid in editorial.supporting_evidence_ids}
        facts = [item for item in research.verified_facts if isinstance(item, dict)]
        # Venues the concept names (restaurants, sights) whose evidence it did not cite by id.
        names = focus_names(json.dumps(editorial.to_dict(), ensure_ascii=False), facts)
        evidence = [
            {"fact": fact_text(item), "source": self._source_label(research, item)}
            for item in facts
            if set(map(str, item.get("source_ids") or [])) & supporting
            or item.get("evidence_level") in {"hotel_document", "guest_report"}
            or any(name in fact_text(item) for name in names)
        ][:100]
        payload = {
            "concept": {k: v for k, v in editorial.to_dict().items() if k in {
                "working_title", "strongest_experience", "why_someone_would_want_it",
                "why_this_property_enables_it", "editorial_proposition", "possible_story_structure",
                "visual_storytelling_device", "supporting_hotel_elements", "open_questions",
            }},
            "critic_notes": {
                "verdict": critique.verdict,
                "revision_instructions": list(critique.revision_instructions or []),
                "unresolved_concerns": list(getattr(critique, "unresolved_concerns", []) or []),
            },
            "evidence": evidence,
            "hotel": str((research.property_identity or {}).get("hotel") or "")
            if isinstance(research.property_identity, dict) else "",
            "format": (
                {**self._trip_format(), "days": self._trip_nights + 1}
                if self._trip_nights else {"type": "single_experience"}
            ),
        }
        if logistics:
            payload["logistics"] = {"issues": logistics.get("issues", []), "legs": logistics.get("lines", [])}
        payload["house_style"] = self._house_style_payload(payload["hotel"])["house_style"]
        if venues:
            payload["venues"] = venues
        try:
            self._notify(STEP_PROGRESS_PREFIX + "Writing it up as a short outline")
            response = self._fast_provider.complete(
                [LLMMessage(role="user", content=json.dumps(payload, ensure_ascii=False))],
                system=load_prompt("outline"), max_tokens=1500, temperature=0.2,
            )
        except AgentCancelled:
            raise
        except Exception:
            logger.exception("outline condensation failed; falling back to the full format")
            return None
        data = extract_json_block(response.text)
        if not isinstance(data, dict) or not data.get("angle") or not data.get("outline"):
            return None
        options = [str(t).strip() for t in data.get("title_options") or [] if str(t).strip()]
        if options and not str(data.get("title") or "").strip():
            data["title"] = options[0]
        return data

    @staticmethod
    def _source_label(research: ResearchResult, item: dict[str, Any]) -> str:
        sids = {str(sid) for sid in item.get("source_ids") or []}
        for record in research.source_records:
            if str(record.get("sid")) in sids:
                return str(record.get("title") or record.get("url") or "")
        return ""

    @staticmethod
    def _format_outline(
        outline: dict[str, Any], editorial: EditorialResult, critique: Any, lifecycle: str,
        research: ResearchResult, *, itinerary: bool = False,
    ) -> str:
        clean = EditorialOrchestrator._strip_source_ids

        def items(key: str, limit: int) -> list[str]:
            values = outline.get(key) or []
            return [clean(str(v)) for v in (values if isinstance(values, list) else [values]) if str(v).strip()][:limit]

        title = clean(str(outline.get("title") or editorial.working_title or editorial.editorial_proposition))
        parts = [f"**{title}**", clean(str(outline.get("angle", "")))]
        others = [t for t in items("title_options", 5) if t != title][:3]
        if others:
            parts.append("_Other headlines:_ " + " / ".join(others))
        if str(outline.get("opening_hook") or "").strip():
            parts.append(f"_Opening hook:_ {clean(str(outline['opening_hook']))}")
        if itinerary and str(outline.get("stay_info") or "").strip():
            parts.append(f"_{clean(str(outline['stay_info']))}_")
        sections = (
            ("Why book it" if itinerary else "Why readers will want it", items("why_readers_want_it", 3), False),
            ("Day by day" if itinerary else "Story outline", items("outline", 15), True),
            ("Getting around", items("getting_around", 4), False),
            ("Proof points", items("proof_points", 8 if itinerary else 5), False),
            ("Insider notes", items("insider_notes", 5), False),
            ("Don't claim", items("avoid", 4), False),
            ("Check with the hotel", items("check_with_hotel", 3), False),
        )
        for heading, values, numbered in sections:
            if values:
                lines = [f"{i}. {v}" if numbered else f"• {v}" for i, v in enumerate(values, 1)]
                parts.append(f"**{heading}**\n" + "\n".join(lines))
        verdict = str(critique.verdict).replace("_", " ").lower()
        footer = f"_Concept {lifecycle} · critic: {verdict}_"
        links = EditorialOrchestrator._editorial_source_links(editorial, research, limit=4)
        if links:
            footer = "Sources: " + " · ".join(links) + "\n" + footer
        parts.append(footer)
        return "\n\n".join(part for part in parts if part)

    @staticmethod
    def _editorial_source_links(
        editorial: EditorialResult, research: ResearchResult, *, limit: int = 5,
    ) -> list[str]:
        supporting = {str(sid) for sid in editorial.supporting_evidence_ids}

        def usable(source: dict[str, Any]) -> bool:
            title = str(source.get("title") or "").lower().replace(" ", "")
            return bool(
                source.get("fetched_ok") and source.get("url")
                and not any(marker in title for marker in ("404", "notfound", "pagenotfound"))
            )

        preferred = [
            source for source in research.source_records
            if str(source.get("sid")) in supporting and usable(source)
        ]
        validated = [
            source for source in research.source_records
            if usable(source) and source not in preferred
            and source.get("source_type") in {OFFICIAL, BRAND, PUBLICATION, DESTINATION, EDITORIAL_PRESS}
        ]
        # When the editorial specialist named supporting evidence, show only
        # those sources. Adding unrelated fetched authority pages makes a weak
        # claim look better sourced than it is. The validated fallback exists
        # only for legacy results that did not emit supporting IDs.
        candidates = preferred if supporting else validated
        selected: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for source in candidates:
            normalized = str(source.get("url", "")).rstrip("/")
            if not normalized or normalized in seen_urls:
                continue
            seen_urls.add(normalized)
            selected.append(source)
            if len(selected) >= limit:
                break
        return [
            f"<{s.get('url')}|{str(s.get('title') or s.get('source_type') or s.get('url')).replace(' (shared in Slack)', '')}>"
            for s in selected
        ]

    @staticmethod
    def _format_editorial(
        editorial: EditorialResult, critique: Any, lifecycle: str,
        research: ResearchResult,
    ) -> str:
        clean = EditorialOrchestrator._strip_source_ids
        title = clean(editorial.working_title or editorial.editorial_proposition)
        text = (
            "I think there's one experience worth developing here.\n\n"
            f"**{title}**\n\n{clean(editorial.strongest_experience)}\n\n"
            f"Why someone would want it: {clean(editorial.why_someone_would_want_it)}\n\n"
            f"Why this property: {clean(editorial.why_this_property_enables_it)}\n\n"
            f"Editorial device: {clean(editorial.visual_storytelling_device)}\n\n"
            f"Concept status: {lifecycle}. Critic: {critique.verdict}."
        )
        if editorial.open_questions:
            text += "\n\nStill to verify:\n" + "\n".join(
                f"- {clean(q)}" for q in editorial.open_questions
            )
        if critique.verdict == "PASS_WITH_REVISIONS" and critique.revision_instructions:
            text += "\n\nRequired revisions before development:\n" + "\n".join(
                f"- {clean(item)}" for item in critique.revision_instructions
            )
        links = [f"- {link}" for link in EditorialOrchestrator._editorial_source_links(editorial, research)]
        if links:
            text += "\n\n**Sources**\n" + "\n".join(links)
        return text

    @staticmethod
    def _strip_source_ids(value: str) -> str:
        """Keep evidence IDs internal; user-facing source links follow separately."""
        return re.sub(
            r"\s*\[S\d+(?:\s*,\s*S\d+)*\]", "", str(value), flags=re.I
        ).strip()

    @staticmethod
    def _format_verification_report(claims: list[dict[str, Any]]) -> str:
        if not claims:
            return (
                "I completed the research pass, but I couldn't establish any of the claims from "
                "authoritative evidence. The concept has not been changed.\n\n"
                "**Impact on current concept**\n\n"
                "Not evaluated because you asked me not to change the concept."
            )
        headings = (
            ("verified", "Verified facts"),
            ("unsupported", "Unsupported claims"),
            ("uncertain", "Still uncertain"),
        )
        sections = ["Claim verification complete. The concept has not been changed."]
        for status, heading in headings:
            matching = [item for item in claims if str(item.get("status", "")).lower() == status]
            lines = []
            for item in matching:
                claim = EditorialOrchestrator._strip_source_ids(str(item.get("claim", "")))
                finding = EditorialOrchestrator._strip_source_ids(str(item.get("finding", "")))
                links = []
                for source in item.get("sources", []) or []:
                    if not isinstance(source, dict) or not source.get("url"):
                        continue
                    label = source.get("title") or source.get("source_type") or "Source"
                    links.append(f"<{source['url']}|{label}>")
                suffix = f" Sources: {', '.join(links)}" if links else ""
                lines.append(f"• {claim}: {finding}{suffix}")
            sections.append(f"**{heading}**\n\n" + ("\n".join(lines) if lines else "• None."))
        sections.append(
            "**Impact on current concept**\n\n"
            "Not evaluated because you asked me not to change the concept."
        )
        return "\n\n".join(sections)

    @staticmethod
    def _artifact_payload(artifact: dict[str, Any]) -> dict[str, Any]:
        payload = dict(artifact)
        path = Path(str(artifact.get("path", "")))
        if path.is_dir():
            for name in ("index.html", "styles.css", "script.js"):
                file = path / name
                if file.exists():
                    payload[name] = file.read_text(encoding="utf-8")[:30000]
            html = str(payload.get("index.html", ""))
            ids = set(re.findall(r'\bid=["\']([^"\']+)', html, flags=re.I))
            fragments = set(re.findall(r'\bhref=["\']#([^"\']+)', html, flags=re.I))
            payload["structural_validation"] = {
                "byte_length": len(html.encode("utf-8")),
                "has_doctype": html.lstrip().lower().startswith("<!doctype html>"),
                "has_closing_html": html.rstrip().lower().endswith("</html>"),
                "dangling_fragment_targets": sorted(fragments - ids),
            }
        return payload

    @staticmethod
    def _compact_summary(
        history: Sequence[tuple[str, str]], user_text: str, assistant_text: str,
    ) -> str:
        items = [f"{r}: {c[:500]}" for r, c in history[-4:]]
        items.extend((f"user: {user_text[:700]}", f"assistant: {assistant_text[:1200]}"))
        return "\n".join(items)[-4000:]

    @staticmethod
    def _slug(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:80] or "project"

    @staticmethod
    def _tokens(value: str) -> set[str]:
        return {t.lower() for t in re.findall(r"[A-Za-zÀ-ÿ0-9]{4,}", value)}

    @staticmethod
    def _is_project_status_question(value: str) -> bool:
        normalized = re.sub(r"\s+", " ", value.lower()).strip(" .?!")
        return any(phrase in normalized for phrase in (
            "what state", "current state", "project status", "where are we",
            "what are you holding", "what do you currently hold",
        ))

    @staticmethod
    def _is_research_verification_request(
        value: str, project_state: dict[str, Any],
    ) -> bool:
        if not project_state:
            return False
        normalized = re.sub(r"\s+", " ", value.lower())
        verification_terms = (
            "verify", "fact-check", "fact check", "substantiate", "validate the claim",
            "validate these claims", "check the claim", "check these claims",
        )
        evidence_terms = (
            "claim", "claims", "outstanding", "uncertain", "unverified", "evidence",
            "authoritative source", "authoritative sources",
        )
        return (
            any(term in normalized for term in verification_terms)
            and any(term in normalized for term in evidence_terms)
        )

    @staticmethod
    def _is_workflow_debug_question(value: str) -> bool:
        normalized = re.sub(r"\s+", " ", value.lower()).strip(" .?!")
        return normalized in {
            "show workflow", "show the workflow", "what did you delegate",
            "what did you delegate this turn", "show delegation", "show orchestration",
        }
