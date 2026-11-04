"""HotelResearchAgent: the research -> DNA -> territories -> critic pipeline.

The creative methodology itself lives in app/prompts/*.md; this module
only orchestrates: it decides which stage runs, assembles evidence,
enforces provenance (source ids are assigned and verified here, never
by the model), talks to storage, and emits progress events.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .config import Settings
from .formatting import (
    SourceLine,
    build_compact_sources,
    build_sources_block,
    strip_citations,
    validate_citations,
)
from .llm.base import LLMError, LLMMessage, LLMProvider
from .llm.zai_glm import extract_json_block
from .prompts import load_prompt
from .storage import ConceptRecord, Database, SessionRecord
from .tools.assets import AssetCandidate, find_candidate_assets, summarize_asset_inventory
from .tools.pages import FetchedPage, PageFetcher, condense_page_text, normalize_url
from .tools.web import (
    BRAND,
    DESTINATION,
    EDITORIAL_PRESS,
    PUBLICATION,
    OFFICIAL,
    REVIEW_SIGNAL,
    SearchHit,
    WebSearchTool,
    classify_source,
    confidence_for,
)

logger = logging.getLogger(__name__)

ProgressFn = Callable[[str], None]


def _keyword_overlap_warnings(
    same_hotel_concepts: Sequence[Any], hotel: str
) -> list[str]:
    """Deterministic anti-repetition signal: keywords over-used in prior
    concepts for this hotel must not anchor new work."""
    counts: dict[str, int] = {}
    for concept in same_hotel_concepts:
        # Draft/test output is context, not evidence that an idea was used.
        if getattr(concept, "status", "approved") not in {"shortlisted", "approved", "published"}:
            continue
        for kw in set(concept.keywords):
            counts[kw] = counts.get(kw, 0) + 1
    hot = sorted((k, n) for k, n in counts.items() if n >= 2)
    if not hot:
        return []
    listed = ", ".join(f"'{k}' (x{n})" for k, n in hot[:12])
    return [
        f"Keywords already anchoring previous {hotel} concepts: {listed}. "
        "New concepts must not lean on these again."
    ]

# How many pages we deep-read per category (kept small on purpose: quality > crawl).
_MAX_OFFICIAL_PAGES = 4
_MAX_OTHER_PAGES = 4
_MAX_EVIDENCE_PER_PAGE = 18


class AgentError(RuntimeError):
    """Pipeline failure with a user-presentable message."""


class AgentCancelled(AgentError):
    """Raised between bounded agent/tool steps after a user cancellation."""


ACTION_ALIASES = {
    "research": "RESEARCH_NEW_PROPERTY",
    "diagnose": "CRITIQUE_IDEA",
    "compare": "COMPARE_CONCEPTS",
    "prototype_review": "CRITIQUE_IDEA",
    "clarify": "CONTINUE_CURRENT_TASK",
    "meta": "META_QUESTION",
    "help": "HELP",
    "cancel": "CANCEL",
    "conversation": "GENERAL_CONVERSATION",
}
VALID_ACTIONS = {
    "RESEARCH_NEW_PROPERTY", "CONTINUE_CURRENT_TASK", "ANSWER_CLARIFICATION",
    "DEVELOP_IDEA", "CRITIQUE_IDEA", "COMPARE_CONCEPTS", "META_QUESTION",
    "HELP", "CANCEL", "GENERAL_CONVERSATION",
}
MAX_TOOL_STEPS = 10
MEMORY_WEIGHTS = {
    "published": 1.0, "approved": 0.9, "shortlisted": 0.55,
    "generated": 0.2, "rejected": 0.0,
}


@dataclass
class SourceRecord:
    sid: str
    url: str
    title: str
    source_type: str
    confidence: str
    fetched_ok: bool
    db_id: int | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ResearchOutcome:
    markdown: str
    clarification: str | None = None
    project_id: int | None = None
    used_searches: int = 0
    action: str = ""
    tool_steps: int = 0


class HotelResearchAgent:
    """Deprecated compatibility pipeline.

    Runtime Slack and CLI entry points use ``EditorialOrchestrator``. This
    class remains temporarily for existing regression coverage and migration
    safety; new behavior must be implemented in specialists/orchestration.
    """
    def __init__(
        self,
        settings: Settings,
        provider: LLMProvider,
        db: Database,
        *,
        fetcher: PageFetcher | None = None,
    ) -> None:
        self._settings = settings
        self._provider = provider
        self._db = db
        self._search = WebSearchTool(provider, publication_domain=settings.publication_domain)
        self._fetcher = fetcher or PageFetcher(timeout_seconds=settings.fetch_timeout_seconds)
        self._progress_callbacks: list[ProgressFn] = []
        self._current_request = ""
        self._cancel_check: Callable[[], bool] = lambda: False

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def handle_message(
        self,
        user_text: str,
        *,
        user_id: str = "",
        thread_key: str = "",
        history: Sequence[tuple[str, str]] = (),
        channel_id: str = "",
        thread_ts: str = "",
        cancel_check: Callable[[], bool] | None = None,
    ) -> ResearchOutcome:
        """Recover session, understand the action, then optionally use tools."""
        self._cancel_check = cancel_check or (lambda: False)
        session = self._db.get_session(thread_key) if thread_key else None
        if thread_key and session is None:
            session = self._db.upsert_session(
                thread_key, thread_ts=thread_ts or thread_key.rsplit(":", 1)[-1],
                channel_id=channel_id, user_id=user_id,
            )
        triage = self._triage(user_text, history, session)
        action = self._normalize_action(triage)

        # Pending questions have conversational precedence over a bare hotel
        # name. The original request remains the task being resumed.
        if session and session.status == "awaiting_clarification" and action not in {
            "CANCEL", "META_QUESTION", "HELP"
        }:
            action = "ANSWER_CLARIFICATION"

        if action == "CANCEL":
            if thread_key:
                self._db.upsert_session(
                    thread_key, status="cancelled", pending_question_type="",
                    pending_question_text="", current_goal="",
                )
            return ResearchOutcome(
                markdown="Stopped. I won't run any more research for this task unless you ask me to resume.",
                action=action,
            )

        if action in {"ANSWER_CLARIFICATION", "CONTINUE_CURRENT_TASK"} and session:
            triage = dict(triage)
            if not triage.get("hotel") and session.pending_question_type == "property_identity":
                triage["hotel"] = user_text.strip()
            if not triage.get("hotel"):
                triage["hotel"] = session.current_property
            original = session.original_request or user_text
            if triage.get("hotel"):
                self._db.clear_pending_question(thread_key)
                action = "RESEARCH_NEW_PROPERTY"
                user_text = f"{original}\nClarification supplied: {user_text}"
            else:
                return self._ask_clarification(
                    thread_key, triage, user_text, user_id=user_id,
                    channel_id=channel_id, thread_ts=thread_ts,
                )

        if action == "RESEARCH_NEW_PROPERTY":
            if triage.get("needs_clarification") or not triage.get("hotel"):
                return self._ask_clarification(
                    thread_key, triage, user_text, user_id=user_id,
                    channel_id=channel_id, thread_ts=thread_ts,
                )
            hotel = str(triage.get("hotel", "")).strip()
            if thread_key:
                current_original = session.original_request if session and session.original_request else user_text
                self._db.upsert_session(
                    thread_key, user_id=user_id, channel_id=channel_id or None,
                    thread_ts=thread_ts or None, status="researching",
                    current_property=hotel, original_request=current_original,
                    current_goal=str(triage.get("goal") or user_text)[:1000],
                    pending_question_type="", pending_question_text="",
                )
            outcome = self._run_research(
                triage, user_text, user_id=user_id, thread_key=thread_key
            )
            outcome.action = action
            if thread_key:
                if outcome.clarification:
                    self._db.upsert_session(
                        thread_key, status="awaiting_clarification",
                        pending_question_type="property_identity",
                        pending_question_text=outcome.clarification,
                    )
                    return outcome
                concepts = self._db.recent_concepts(
                    exclude_project_id=None, limit=20
                )
                ids = [c.id for c in concepts if c.source_project_id == outcome.project_id]
                self._db.upsert_session(
                    thread_key, status="complete", research_project_id=outcome.project_id,
                    latest_concept_ids=ids,
                    conversation_summary=self._compact_summary(history, user_text, outcome.markdown),
                )
            return outcome

        if action == "CRITIQUE_IDEA" and triage.get("intent") == "prototype_review":
            outcome = self._run_review(triage, user_text, user_id=user_id, thread_key=thread_key)
        elif action in {"DEVELOP_IDEA", "CRITIQUE_IDEA", "COMPARE_CONCEPTS",
                        "META_QUESTION", "HELP", "GENERAL_CONVERSATION"}:
            outcome = self._run_conversational_tool_loop(
                action, triage, user_text, history=history, session=session
            )
        else:
            outcome = self._run_conversational_tool_loop(
                "GENERAL_CONVERSATION", triage, user_text, history=history, session=session
            )
        outcome.action = action
        if thread_key:
            next_status = "developing" if action in {"DEVELOP_IDEA", "CRITIQUE_IDEA"} else (session.status if session else "idle")
            self._db.upsert_session(
                thread_key, status=next_status or "idle",
                conversation_summary=self._compact_summary(history, user_text, outcome.markdown),
            )
        return outcome

    # ------------------------------------------------------------------
    # triage
    # ------------------------------------------------------------------

    def _triage(
        self, user_text: str, history: Sequence[tuple[str, str]],
        session: SessionRecord | None = None,
    ) -> dict[str, Any]:
        context = ""
        if history:
            recent = "\n".join(f"{role}: {content[:400]}" for role, content in history[-6:])
            context = f"Recent thread context:\n{recent}\n\n"
        session_text = ""
        if session:
            session_text = (
                "Session state:\n"
                f"status={session.status}\nproperty={session.current_property}\n"
                f"original_request={session.original_request[:600]}\n"
                f"current_goal={session.current_goal[:400]}\n"
                f"pending_question_type={session.pending_question_type}\n"
                f"pending_question={session.pending_question_text[:300]}\n"
                f"summary={session.conversation_summary[:800]}\n\n"
            )
        response = self._provider.complete(
            [LLMMessage(role="user", content=f"{session_text}{context}Message:\n{user_text}")],
            system=load_prompt("triage"),
            max_tokens=700,
            temperature=0.1,
        )
        data = extract_json_block(response.text)
        if not isinstance(data, dict):
            logger.warning("triage returned unparseable output; defaulting to clarify")
            return {"action": "GENERAL_CONVERSATION", "intent": "conversation"}
        return data

    def _normalize_action(self, triage: dict[str, Any]) -> str:
        action = str(triage.get("action", "")).upper()
        if action in VALID_ACTIONS:
            return action
        return ACTION_ALIASES.get(str(triage.get("intent", "")).lower(), "GENERAL_CONVERSATION")

    def _ask_clarification(
        self, thread_key: str, triage: dict[str, Any], user_text: str, *,
        user_id: str, channel_id: str, thread_ts: str,
    ) -> ResearchOutcome:
        question = str(triage.get("clarification") or "Which property should I research? Give me the hotel name.")
        if thread_key:
            existing = self._db.get_session(thread_key)
            self._db.upsert_session(
                thread_key, thread_ts=thread_ts or None, channel_id=channel_id or None,
                user_id=user_id, status="awaiting_clarification",
                original_request=(existing.original_request if existing and existing.original_request else user_text),
                current_goal=str(triage.get("goal") or user_text)[:1000],
                pending_question_type="property_identity", pending_question_text=question,
            )
        return ResearchOutcome(markdown="", clarification=question, action="RESEARCH_NEW_PROPERTY")

    @staticmethod
    def _compact_summary(
        history: Sequence[tuple[str, str]], user_text: str, assistant_text: str
    ) -> str:
        parts = [f"{r}: {c[:500]}" for r, c in history[-4:]]
        parts.extend((f"user: {user_text[:700]}", f"assistant: {assistant_text[:1200]}"))
        return "\n".join(parts)[-4000:]

    # ------------------------------------------------------------------
    # bounded conversational tool loop
    # ------------------------------------------------------------------

    def _run_conversational_tool_loop(
        self, action: str, triage: dict[str, Any], user_text: str, *,
        history: Sequence[tuple[str, str]], session: SessionRecord | None,
    ) -> ResearchOutcome:
        """Let the model answer or request a named tool, bounded to 10 steps.

        Web tools are never called merely because a Slack event arrived. The
        router and then the model must explicitly select them. Existing
        research/memory are cheap local tools and are preferred for follow-ups.
        """
        available = (
            "load_existing_research, search_concept_memory, search_web, "
            "search_official_property, search_destination, search_publication, read_page"
        )
        state = {
            "action": action,
            "property": session.current_property if session else "",
            "goal": session.current_goal if session else "",
            "status": session.status if session else "idle",
            "pending_question": session.pending_question_text if session else "",
            "conversation_summary": session.conversation_summary if session else "",
        }
        recent = "\n".join(f"{r}: {c[:1200]}" for r, c in history[-6:])
        messages = [LLMMessage(role="user", content=(
            "CURRENT SESSION:\n" + json.dumps(state, ensure_ascii=False, indent=1)
            + "\n\nRECENT THREAD:\n" + recent
            + "\n\nCURRENT USER MESSAGE:\n" + user_text
        ))]
        system = (
            "You are Suite Stories' conversational hotel research and editorial copilot. "
            "Answer the current action naturally. You may either answer directly in prose, "
            "or request exactly one tool by returning ONLY JSON: "
            '{"type":"tool","tool":"<name>","arguments":{...}}. '
            f"Available tools: {available}. After a tool result, decide again. "
            "Prefer existing research for follow-ups. META_QUESTION, HELP, and ordinary "
            "conversation normally require zero tools. Never claim prior-work overlap unless "
            "a search_concept_memory result identifies the hotel, concept, lifecycle status, "
            "and concrete overlap. Generated memory is weak context; rejected memory cannot "
            "block; approved/published memory is strong but still not an automatic veto. "
            "Generic shared moments are not material duplication. Do not describe tools you "
            "did not actually call. If evidence is insufficient, say so plainly.\n\n"
            + load_prompt("methodology")
        )
        tool_steps = 0
        comparison_matches: list[dict[str, Any]] | None = None

        # Comparison questions need evidence injected before the model can make
        # any duplication claim. This local lookup is not web research.
        if action == "COMPARE_CONCEPTS":
            query = user_text + "\n" + "\n".join(c for _, c in history[-3:])
            result = self.search_concept_memory(
                query, hotel=session.current_property if session else ""
            )
            comparison_matches = result
            messages.append(LLMMessage(role="assistant", content=(
                '{"type":"tool","tool":"search_concept_memory","arguments":{}}'
            )))
            messages.append(LLMMessage(role="user", content="TOOL RESULT:\n" + json.dumps(result, ensure_ascii=False)))
            tool_steps += 1

        for _ in range(MAX_TOOL_STEPS - tool_steps):
            if self._cancel_check():
                raise AgentCancelled("Research was cancelled.")
            response = self._provider.complete(
                messages, system=system, max_tokens=3000, temperature=0.4
            )
            payload = extract_json_block(response.text)
            if not (isinstance(payload, dict) and payload.get("type") == "tool"):
                text = response.text.strip()
                if not text:
                    raise AgentError("The conversational response came back empty; please try again.")
                if action == "COMPARE_CONCEPTS":
                    concrete = [
                        m for m in (comparison_matches or []) if m.get("specific_overlap")
                    ]
                    if not concrete:
                        text = (
                            "I can't substantiate the earlier duplication claim from the concept registry. "
                            "I found no identifiable prior concept with a concrete overlap, so memory should "
                            "not be used to reject this direction."
                        )
                    else:
                        match = concrete[0]
                        required = (
                            str(match["previous_hotel"]), str(match["previous_concept"]),
                            str(match["status"]),
                        )
                        if not all(value.lower() in text.lower() for value in required):
                            text += (
                                "\n\nRegistry evidence: the closest identifiable match is "
                                f"{match['previous_concept']} for {match['previous_hotel']} "
                                f"(status: {match['status']}). The concrete shared elements are "
                                + ", ".join(match["specific_overlap"])
                                + f"; overlap type: {', '.join(match['overlap_type'])}. "
                                "This is a comparison signal, not an automatic veto."
                            )
                return ResearchOutcome(markdown=text, tool_steps=tool_steps)
            tool_name = str(payload.get("tool", ""))
            arguments = payload.get("arguments", {})
            if not isinstance(arguments, dict):
                arguments = {}
            result = self._execute_agent_tool(tool_name, arguments, session=session)
            tool_steps += 1
            messages.append(LLMMessage(role="assistant", content=json.dumps(payload, ensure_ascii=False)))
            messages.append(LLMMessage(role="user", content=(
                f"TOOL RESULT ({tool_name}):\n" + json.dumps(result, ensure_ascii=False)[:12000]
            )))
        raise AgentError(f"I stopped after {MAX_TOOL_STEPS} tool steps to avoid an unbounded research loop.")

    def _execute_agent_tool(
        self, name: str, arguments: dict[str, Any], *, session: SessionRecord | None
    ) -> Any:
        hotel = str(arguments.get("hotel") or (session.current_property if session else ""))
        if name == "load_existing_research":
            project_id = arguments.get("project_id") or (session.research_project_id if session else None)
            row = self._db.get_project(int(project_id)) if project_id else None
            if row is None:
                return {"found": False, "reason": "no completed research is attached to this thread"}
            return {
                "found": True, "project_id": row["id"], "hotel": row["hotel"],
                "research_snapshot": row["research_snapshot"], "property_dna": row["property_dna"],
            }
        if name == "search_concept_memory":
            return self.search_concept_memory(str(arguments.get("query", "")), hotel=hotel)
        if name == "search_web":
            self._notify("official")
            return [h.__dict__ for h in self._search.search(str(arguments.get("query", hotel)))]
        if name == "search_official_property":
            self._notify("official")
            return [h.__dict__ for h in self._search.search_official_hotel(hotel)]
        if name == "search_destination":
            self._notify("coverage")
            return [h.__dict__ for h in self._search.search_destination_context(hotel, str(arguments.get("city", "")))]
        if name == "search_publication":
            self._notify("coverage")
            return [h.__dict__ for h in self._search.search_publication(hotel)]
        if name == "read_page":
            page = self._fetcher.fetch(str(arguments.get("url", "")))
            return {
                "url": page.url, "title": page.title, "fetched_ok": page.fetched_ok,
                "text": condense_page_text(page)[:10000], "note": page.note,
            }
        return {"error": f"unknown tool: {name}"}

    def search_concept_memory(self, query: str, *, hotel: str = "", limit: int = 8) -> list[dict[str, Any]]:
        """Return identifiable lifecycle-aware matches, never a vague veto."""
        query_tokens = self._meaningful_tokens(query)
        records = self._db.recent_concepts(limit=200)
        ranked: list[tuple[float, ConceptRecord, list[str], list[str]]] = []
        for concept in records:
            body = " ".join((
                concept.name, concept.proposition, concept.experience,
                concept.narrative_device, concept.visual_device,
                concept.story_structure, " ".join(concept.keywords),
            ))
            overlap = sorted(query_tokens & self._meaningful_tokens(body))
            dimensions: list[str] = []
            if query_tokens & self._meaningful_tokens(concept.narrative_device + " " + concept.story_structure):
                dimensions.append("structural")
            if query_tokens & self._meaningful_tokens(concept.experience + " " + concept.proposition):
                dimensions.append("experiential")
            if query_tokens & self._meaningful_tokens(concept.visual_device + " " + concept.visual_language):
                dimensions.append("visual")
            if query_tokens & self._meaningful_tokens(concept.central_tension):
                dimensions.append("commercial")
            if overlap and not dimensions:
                dimensions.append("superficial")
            lexical = len(overlap) / max(1, min(len(query_tokens), 12))
            hotel_bonus = 0.15 if hotel and hotel.lower() in concept.hotel.lower() else 0.0
            score = min(1.0, lexical + hotel_bonus) * MEMORY_WEIGHTS.get(concept.status, 0.2)
            if overlap or hotel_bonus:
                ranked.append((score, concept, overlap, dimensions))
        ranked.sort(key=lambda item: (item[0], item[1].id), reverse=True)
        return [
            {
                "previous_hotel": c.hotel, "previous_concept": c.name,
                "status": c.status, "source_project_id": c.source_project_id,
                "specific_overlap": overlap,
                "overlap_type": dimensions,
                "similarity_reason": (
                    "shared meaningful terms: " + ", ".join(overlap)
                    if overlap else "same-property context only; no concrete concept overlap found"
                ),
                "weighted_signal": round(score, 3),
                "can_block": c.status in {"approved", "published"} and score >= 0.35,
            }
            for score, c, overlap, dimensions in ranked[:limit]
        ]

    @staticmethod
    def _meaningful_tokens(text: str) -> set[str]:
        stop = {"this", "that", "with", "from", "have", "what", "why", "your", "about",
                "hotel", "idea", "concept", "work", "previous", "using", "more", "into"}
        return {
            token.lower() for token in re.findall(r"[A-Za-zÀ-ÿ0-9]{4,}", text)
            if token.lower() not in stop
        }

    # ------------------------------------------------------------------
    # research pipeline
    # ------------------------------------------------------------------

    def _run_research(
        self, triage: dict[str, Any], user_text: str, *, user_id: str, thread_key: str
    ) -> ResearchOutcome:
        hotel = self._db.resolve_alias(str(triage.get("hotel", "")).strip()) or str(triage.get("hotel", "")).strip()
        if not hotel:
            return ResearchOutcome(markdown="", clarification="Which property should I research?")
        city_hint = str(triage.get("city_hint", ""))
        audience = str(triage.get("audience", ""))
        constraints = [str(c) for c in triage.get("constraints", []) if c] or []
        article_url = str(triage.get("article_url", "")).strip()
        deep_mode = bool(triage.get("deep", False))

        self._notify("start")
        self._current_request = user_text

        project_id = self._db.create_project(
            hotel=hotel, request=user_text, user_id=user_id, slack_thread_ts=thread_key
        )

        try:
            sources, page_by_sid, assets = self._collect_evidence(
                project_id, hotel, city_hint, article_url
            )
            if not any(s.fetched_ok for s in sources):
                self._db.complete_project(project_id, status="failed")
                return ResearchOutcome(
                    markdown="", project_id=project_id,
                    clarification=(
                        "I couldn't confidently identify that property. "
                        "Can you confirm the full hotel name or share its website?"
                    ),
                )

            snapshot = self._stage_snapshot(sources)
            self._db.complete_project(
                project_id, research_snapshot=json.dumps(snapshot)[:20000]
            )

            dna = self._stage_dna(snapshot)
            classification = self._stage_classify(snapshot, dna)

            coverage = self._coverage_analysis(sources)
            registry_text, overlap_warnings = self._concept_registry(project_id, hotel)

            candidates = self._stage_territories(
                snapshot, dna, classification, coverage, assets, registry_text, overlap_warnings,
                audience=audience, constraints=constraints,
            )
            self._notify("validate")

            self._notify("compare")
            reviews, selection, rationale = self._stage_critic(
                snapshot, dna, classification, candidates, registry_text
            )
            candidates, selection, rationale = self._maybe_revise(
                snapshot, dna, classification, coverage, assets, registry_text, overlap_warnings,
                audience, constraints, candidates, reviews, selection, rationale,
            )

            final_md = self._compose_final(
                hotel, snapshot, dna, classification, coverage, candidates,
                selection, rationale, assets, sources, deep_mode,
            )

            self._persist_concepts(
                project_id, hotel, snapshot, candidates, selection, user_id=user_id
            )
            self._db.complete_project(
                project_id, status="completed",
                city=snapshot.get("hotel_identity", {}).get("city", ""),
                country=snapshot.get("hotel_identity", {}).get("country", ""),
                brand=snapshot.get("hotel_identity", {}).get("brand", ""),
                property_dna=json.dumps(dna)[:20000],
            )
            return ResearchOutcome(markdown=final_md, project_id=project_id)

        except AgentCancelled:
            self._db.complete_project(project_id, status="cancelled")
            raise
        except AgentError:
            self._db.complete_project(project_id, status="failed")
            raise
        except LLMError as exc:
            self._db.complete_project(project_id, status="failed")
            raise AgentError(f"The model service failed while researching ({exc}). Please try again.") from exc

    # ------------------------------------------------------------------
    # evidence collection
    # ------------------------------------------------------------------

    def _collect_evidence(
        self, project_id: int, hotel: str, city_hint: str, article_url: str
    ) -> tuple[list[SourceRecord], dict[str, FetchedPage], list[AssetCandidate]]:
        self._notify("official")
        official_hits = self._search.search_official_hotel(hotel)
        candidate_urls: list[tuple[str, str]] = []  # (url, intended source_type)

        for hit in official_hits:
            if hit.source_type == OFFICIAL and len([u for u, t in candidate_urls if t == OFFICIAL]) < 2:
                candidate_urls.append((hit.url, OFFICIAL))
            elif hit.source_type == BRAND and len([u for u, t in candidate_urls if t == BRAND]) < 2:
                candidate_urls.append((hit.url, BRAND))
        if article_url:
            candidate_urls.insert(0, (article_url, PUBLICATION))

        self._notify("coverage")
        ls_hits = self._search.search_publication(hotel)
        for hit in ls_hits[:1]:
            if hit.source_type == PUBLICATION:
                candidate_urls.append((hit.url, PUBLICATION))
        destination_hits = self._search.search_destination_context(hotel, city_hint)
        for hit in destination_hits:
            if hit.source_type == DESTINATION and len([u for u, t in candidate_urls if t == DESTINATION]) < 2:
                candidate_urls.append((hit.url, DESTINATION))
        editorial_hits = self._search.search_editorial_coverage(hotel)
        for hit in editorial_hits:
            if hit.source_type == EDITORIAL_PRESS and len([u for u, t in candidate_urls if t == EDITORIAL_PRESS]) < 2:
                candidate_urls.append((hit.url, EDITORIAL_PRESS))

        # Dedup by normalized URL, keep first occurrence.
        seen: set[str] = set()
        planned: list[tuple[str, str]] = []
        for url, stype in candidate_urls:
            norm = normalize_url(url)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            planned.append((url, stype))
        planned = planned[: _MAX_OFFICIAL_PAGES + _MAX_OTHER_PAGES]

        # Fetch (parallel), then extract evidence from successful fetches.
        pages: dict[str, FetchedPage] = {}
        with ThreadPoolExecutor(max_workers=self._settings.max_concurrent_fetches) as pool:
            future_to = {
                pool.submit(self._fetcher.fetch, url): (url, stype) for url, stype in planned
            }
            for future in as_completed(future_to):
                url, stype = future_to[future]
                try:
                    pages[url] = future.result()
                except Exception as exc:  # defensive: fetch() already guards
                    logger.warning("fetch %s raised %s", url, exc)
                    pages[url] = None  # type: ignore[assignment]

        sources: list[SourceRecord] = []
        page_by_sid: dict[str, FetchedPage] = {}
        assets: list[AssetCandidate] = []

        def register(url: str, stype: str, page: FetchedPage | None, snippet: str = "", title: str = "") -> SourceRecord:
            sid = f"S{len(sources) + 1}"
            page_ok = bool(page and page.fetched_ok)
            final_type = stype
            if stype == OFFICIAL and page_ok:
                final_type = OFFICIAL
            elif stype == OFFICIAL:
                final_type = classify_source(url, publication_domain=self._settings.publication_domain)
            record = SourceRecord(
                sid=sid, url=url,
                title=title or (page.title if page_ok else snippet[:120]),
                source_type=final_type,
                confidence=confidence_for(final_type, fetched_ok=page_ok),
                fetched_ok=page_ok,
            )
            record.db_id = self._db.upsert_source(
                project_id=project_id, url=url, url_normalized=normalize_url(url),
                title=record.title, source_type=final_type,
                confidence=record.confidence, fetched_ok=page_ok,
            )
            sources.append(record)
            if page_ok:
                page_by_sid[sid] = page
                if final_type in (OFFICIAL, BRAND):
                    assets.extend(find_candidate_assets(page, page_source_type=final_type))
            return record

        for url, stype in planned:
            page = pages.get(url)
            register(url, stype, page)

        # Extract evidence for fetched pages in parallel (LLM calls).
        fetchable = [(s, page_by_sid[s.sid]) for s in sources if s.sid in page_by_sid]
        with ThreadPoolExecutor(max_workers=self._settings.max_concurrent_fetches) as pool:
            future_to_rec = {
                pool.submit(self._stage_evidence, record, page): record for record, page in fetchable
            }
            for future in as_completed(future_to_rec):
                record = future_to_rec[future]
                try:
                    record.evidence = future.result()
                except LLMError as exc:
                    logger.warning("evidence extraction failed for %s: %s", record.url, exc)
                if record.db_id is not None:
                    self._db.add_evidence_to_source(
                        record.db_id,
                        "\n".join(f"- [{e.get('dimension','')}] {e.get('fact','')}" for e in record.evidence),
                    )
        return sources, page_by_sid, assets

    def _stage_evidence(self, record: SourceRecord, page: FetchedPage) -> list[dict[str, Any]]:
        condensed = condense_page_text(page)
        prompt = (
            f"Perform STAGE: EVIDENCE.\n\n"
            f"SOURCE METADATA: type={record.source_type}, url={record.url}\n\n"
            f"PAGE CONTENT:\n{condensed}"
        )
        response = self._provider.complete(
            [LLMMessage(role="user", content=prompt)],
            system=load_prompt("methodology"),
            max_tokens=3000,
            temperature=0.2,
        )
        data = extract_json_block(response.text)
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            # tolerate {"facts": [...]} / {"evidence": [...]} wrappers
            items = next((v for v in data.values() if isinstance(v, list)), [])
        else:
            items = []
        return [item for item in items if isinstance(item, dict) and item.get("fact")][:_MAX_EVIDENCE_PER_PAGE]

    # ------------------------------------------------------------------
    # synthesis stages
    # ------------------------------------------------------------------

    def _sources_digest(self, sources: list[SourceRecord]) -> str:
        lines = []
        for s in sources:
            lines.append(f"{s.sid} [{s.source_type}/{s.confidence}] {s.title or s.url}\n   {s.url}")
            for e in s.evidence[:12]:
                quote = f'  "{e.get("quote", "")}"' if e.get("quote") else ""
                lines.append(f"   - ({e.get('dimension', 'other')}) {e.get('fact', '')}{quote}")
        return "\n".join(lines)

    def _stage_snapshot(self, sources: list[SourceRecord]) -> dict[str, Any]:
        prompt = (
            "Perform STAGE: SNAPSHOT.\n\nEVIDENCE BASE:\n"
            + self._sources_digest(sources)
        )
        response = self._provider.complete(
            [LLMMessage(role="user", content=prompt)],
            system=load_prompt("methodology"),
            max_tokens=3000,
            temperature=0.3,
        )
        data = extract_json_block(response.text)
        if not isinstance(data, dict) or not data.get("observations"):
            raise AgentError("Could not synthesize a research snapshot from the collected evidence.")
        valid_ids = {s.sid for s in sources}
        for obs in data.get("observations", []):
            obs["source_ids"] = [sid for sid in obs.get("source_ids", []) if sid in valid_ids]
        return data

    def _stage_dna(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        prompt = "Perform STAGE: DNA.\n\nSNAPSHOT:\n" + json.dumps(snapshot, indent=1)
        response = self._provider.complete(
            [LLMMessage(role="user", content=prompt)],
            system=load_prompt("methodology"),
            max_tokens=2000,
            temperature=0.4,
        )
        data = extract_json_block(response.text)
        if not isinstance(data, dict) or not data.get("forces"):
            raise AgentError("Could not distill a property DNA from the snapshot.")
        return data

    def _stage_classify(self, snapshot: dict[str, Any], dna: dict[str, Any]) -> dict[str, Any]:
        prompt = (
            "Perform STAGE: CLASSIFY.\n\n"
            "SNAPSHOT:\n" + json.dumps(snapshot, indent=1)
            + "\n\nPROPERTY DNA:\n" + json.dumps(dna, indent=1)
        )
        response = self._provider.complete(
            [LLMMessage(role="user", content=prompt)],
            system=load_prompt("methodology"),
            max_tokens=1800,
            temperature=0.35,
        )
        data = extract_json_block(response.text)
        if not isinstance(data, dict):
            return {"stay_scenarios": [], "supporting_facts": [], "fact_only": []}
        data.setdefault("stay_scenarios", [])
        data.setdefault("supporting_facts", [])
        data.setdefault("fact_only", [])
        return data

    def _coverage_analysis(self, sources: list[SourceRecord]) -> str:
        ls_sources = [s for s in sources if s.source_type == PUBLICATION]
        if not ls_sources:
            return "No existing coverage in our publication was found for this property."
        lines = []
        for s in ls_sources:
            lines.append(f"Existing coverage {s.sid}: {s.title} ({s.url})")
            for e in s.evidence:
                lines.append(f"  - {e.get('fact', '')}")
        return "\n".join(lines)

    def _concept_registry(self, project_id: int, hotel: str) -> tuple[str, list[str]]:
        same = self._db.concepts_for_hotel(hotel, exclude_project_id=project_id)
        recent = [c for c in self._db.recent_concepts(exclude_project_id=project_id, limit=30) if c not in same]
        if not same and not recent:
            return "(registry is empty - this is the first project)", []
        lines = []
        if same:
            lines.append("PREVIOUS CONCEPTS FOR THIS HOTEL:")
            lines.extend(c.as_summary_line() for c in same)
        if recent:
            lines.append("\nRECENT CONCEPTS FOR OTHER HOTELS (avoid structural repetition):")
            lines.extend(c.as_summary_line() for c in recent[:20])
        warnings = _keyword_overlap_warnings(same, hotel)
        lines.append(
            "\nLIFECYCLE POLICY: published/approved=strong signal; shortlisted=moderate; "
            "generated=weak reference only; rejected=negative memory and never an automatic block. "
            "Anti-repetition is not an automatic veto. A material duplication claim must name the "
            "hotel, concept, status, exact overlap, and whether it is structural, experiential, "
            "visual, or commercial."
        )
        return "\n".join(lines), warnings

    def _stage_territories(
        self,
        snapshot: dict[str, Any],
        dna: dict[str, Any],
        classification: dict[str, Any],
        coverage: str,
        assets: list[AssetCandidate],
        registry_text: str,
        overlap_warnings: list[str],
        *,
        audience: str,
        constraints: list[str],
        fix_notes: str = "",
    ) -> list[dict[str, Any]]:
        sections = [
            "Perform STAGE: TERRITORIES.",
            "\nSNAPSHOT:\n" + json.dumps(snapshot, indent=1),
            "\nPROPERTY DNA:\n" + json.dumps(dna, indent=1),
            "\nCLASSIFICATION (possible stay experiences + supporting/fact-only):\n"
            + json.dumps(classification, indent=1),
            "\nEXISTING COVERAGE:\n" + coverage,
            "\nASSET INVENTORY (real images found on official/brand pages):\n" + summarize_asset_inventory(assets),
            "\nPREVIOUS CONCEPT REGISTRY:\n" + registry_text,
        ]
        if overlap_warnings:
            sections.append("\nREGISTRY OVERLAP WARNINGS:\n" + "\n".join(overlap_warnings))
        if audience:
            sections.append(f"\nREQUESTED AUDIENCE: {audience} (do not stereotype)")
        if constraints:
            sections.append("\nUSER CONSTRAINTS:\n" + "\n".join(f"- {c}" for c in constraints))
        if fix_notes:
            sections.append("\nCRITIC REVISION INSTRUCTIONS (mandatory):\n" + fix_notes)
        response = self._provider.complete(
            [LLMMessage(role="user", content="\n".join(sections))],
            system=load_prompt("methodology"),
            max_tokens=5000,
            temperature=0.85,
        )
        data = extract_json_block(response.text)
        if isinstance(data, dict) and isinstance(data.get("candidates"), list):
            return [
                c for c in data["candidates"]
                if isinstance(c, dict) and c.get("plain_idea")
            ]
        return []

    def _stage_critic(
        self,
        snapshot: dict[str, Any],
        dna: dict[str, Any],
        classification: dict[str, Any],
        candidates: list[dict[str, Any]],
        registry_text: str,
    ) -> tuple[list[dict[str, Any]], list[str], dict[str, str]]:
        prompt = "\n".join(
            [
                "Perform STAGE: CRITIC.",
                "\nPROPERTY DNA:\n" + json.dumps(dna, indent=1),
                "\nCLASSIFICATION:\n" + json.dumps(classification, indent=1),
                "\nSNAPSHOT:\n" + json.dumps(snapshot, indent=1)[:6000],
                "\nCANDIDATES:\n" + json.dumps(candidates, indent=1),
                "\nREGISTRY:\n" + registry_text,
            ]
        )
        response = self._provider.complete(
            [LLMMessage(role="user", content=prompt)],
            system=load_prompt("critic"),
            max_tokens=3500,
            temperature=0.2,
        )
        data = extract_json_block(response.text)
        if not isinstance(data, dict):
            return [], [], {}
        reviews = [r for r in data.get("reviews", []) if isinstance(r, dict)]
        propositions = {c.get("plain_idea") for c in candidates}
        selection = [p for p in data.get("selection", []) if p in propositions]
        rationale = data.get("ranking_rationale", {}) if isinstance(data.get("ranking_rationale"), dict) else {}
        return reviews, selection, rationale

    def _maybe_revise(
        self, snapshot, dna, classification, coverage, assets, registry_text, overlap_warnings,
        audience, constraints, candidates, reviews, selection, rationale,
    ) -> tuple[list[dict[str, Any]], list[str], dict[str, str]]:
        if len(selection) >= 2 or not candidates:
            return candidates, selection, rationale
        fixes = [r.get("fix", "") for r in reviews if r.get("fix")]
        if not fixes:
            return candidates, selection, rationale
        revised = self._stage_territories(
            snapshot, dna, classification, coverage, assets, registry_text, overlap_warnings,
            audience=audience, constraints=constraints,
            fix_notes="\n".join(f"- {f}" for f in fixes),
        )
        if not revised:
            return candidates, selection, rationale
        reviews2, selection2, rationale2 = self._stage_critic(
            snapshot, dna, classification, revised, registry_text
        )
        if len(selection2) > len(selection):
            return revised, selection2, rationale2
        return candidates, selection, rationale

    # ------------------------------------------------------------------
    # final composition
    # ------------------------------------------------------------------

    def _compose_final(
        self,
        hotel: str,
        snapshot: dict[str, Any],
        dna: dict[str, Any],
        classification: dict[str, Any],
        coverage: str,
        candidates: list[dict[str, Any]],
        selection: list[str],
        rationale: dict[str, str],
        assets: list[AssetCandidate],
        sources: list[SourceRecord],
        deep_mode: bool,
    ) -> str:
        chosen = [c for c in candidates if c.get("plain_idea") in selection]
        ordered = sorted(chosen, key=lambda c: selection.index(c["plain_idea"]))
        mode = "DEEP" if deep_mode else "NORMAL"
        honest_gap = ""
        if not ordered:
            honest_gap = (
                "\n\nNOTE: the critic passed no candidate. Tell the user honestly "
                "what the research does not yet support and what would unlock it "
                "(existing article, press kit, asset access). Do not invent concepts."
            )
        registry = "\n".join(
            f"[S{s.sid}] {s.source_type}/{s.confidence} — {s.title or s.url}: {s.url}" for s in sources
        )
        prompt = "\n".join(
            [
                f"Perform STAGE: FINAL, MODE: {mode}, for {hotel}.",
                f"The user's original request: {self._current_request}",
                "\nSNAPSHOT:\n" + json.dumps(snapshot, indent=1),
                "\nPROPERTY DNA:\n" + json.dumps(dna, indent=1),
                "\nCLASSIFICATION:\n" + json.dumps(classification, indent=1),
                "\nEXISTING COVERAGE ANALYSIS:\n" + coverage,
                "\nSELECTED CONCEPTS (in rank order, post-critic):\n"
                + (json.dumps(ordered, indent=1) if ordered else "(none passed the critic)"),
                "\nRANKING RATIONALE:\n" + json.dumps(rationale, indent=1),
                "\nASSET INVENTORY:\n" + summarize_asset_inventory(assets),
                "\nSOURCE REGISTRY (used for [S#] citations in DEEP mode only; "
                "never write a Sources section yourself):\n" + registry,
                honest_gap,
            ]
        )
        response = self._provider.complete(
            [LLMMessage(role="user", content=prompt)],
            system=load_prompt("final"),
            max_tokens=6000 if deep_mode else 3500,
            temperature=0.5,
        )
        text = response.text.strip()
        if not text:
            raise AgentError("The final response came back empty; please try again.")
        return self._append_sources(text, sources, deep=deep_mode)

    def _append_sources(self, final_md: str, sources: list[SourceRecord], *, deep: bool = False) -> str:
        # Models sometimes write a stub sources section despite instructions;
        # the authoritative block is built from the database below.
        body = re.split(
            r"^#{1,6}\s*(7\.?\s*)?Sources\s*:?\s*$", final_md, maxsplit=1, flags=re.IGNORECASE | re.MULTILINE
        )[0]
        lines = [SourceLine(sid=s.sid, title=s.title, url=s.url, source_type=s.source_type) for s in sources]
        if deep:
            valid_sids = {s.sid for s in sources}
            body, removed = validate_citations(body, valid_sids)
            block = build_sources_block(lines)
            if removed:
                block += f"\n\n({len(removed)} citation(s) referenced unknown sources and were removed.)"
        else:
            # Normal copilot mode: no opaque ids in the visible message.
            body = strip_citations(body)
            block = build_compact_sources(lines)
        return f"{body.rstrip()}\n\n{block}"

    def _persist_concepts(
        self,
        project_id: int,
        hotel: str,
        snapshot: dict[str, Any],
        candidates: list[dict[str, Any]],
        selection: list[str],
        *,
        user_id: str,
    ) -> None:
        identity = snapshot.get("hotel_identity", {})
        ranking_labels = ["strongest", "alternative", "experimental"]
        for rank, proposition in enumerate(selection):
            concept = next(
                (c for c in candidates if c.get("plain_idea") == proposition), None
            )
            if not concept:
                continue
            needs = concept.get("assets", {}) or {}
            keywords = re.findall(
                r"[A-Za-zÀ-ÿ0-9]{3,}",
                f"{concept.get('plain_idea','')} {concept.get('the_experience','')} "
                f"{concept.get('editorial_device','')}",
            )
            self._db.insert_concept(
                project_id=project_id, hotel=hotel,
                city=identity.get("city", ""), country=identity.get("country", ""),
                brand=identity.get("brand", ""), user_id=user_id,
                name=concept.get("working_title", "") or concept.get("plain_idea", ""),
                proposition=str(concept.get("the_experience", ""))[:500],
                central_tension=concept.get("booking_motivation", ""),
                territory=concept.get("editorial_device", ""),
                visual_language="; ".join(needs.get("verified", [])[:5]) if isinstance(needs, dict) else "",
                story_structure=str(concept.get("the_experience", ""))[:500],
                keywords=[k.lower() for k in keywords[:12]],
                ranking=ranking_labels[rank] if rank < len(ranking_labels) else "",
                status="generated",
                experience=str(concept.get("the_experience", ""))[:1000],
                narrative_device=str(concept.get("editorial_device", ""))[:1000],
                visual_device=str(concept.get("visual_grammar", ""))[:1000],
                source_project_id=project_id,
            )

    # ------------------------------------------------------------------
    # diagnose / compare
    # ------------------------------------------------------------------

    def _run_diagnose(self, triage: dict, user_text: str, *, user_id: str, thread_key: str) -> ResearchOutcome:
        idea = triage.get("idea_text") or user_text
        prior = self._db.latest_project_for_thread(thread_key)
        context = ""
        if prior is not None and prior["property_dna"]:
            context = "\nPRIOR PROJECT DNA:\n" + prior["property_dna"][:4000]
        registry_text, _ = self._concept_registry(-1, prior["hotel"] if prior else "")
        prompt = "Perform STAGE: DIAGNOSE.\n\nIDEA:\n" + idea[:6000] + context + "\n\nREGISTRY:\n" + registry_text
        response = self._provider.complete(
            [LLMMessage(role="user", content=prompt)],
            system=load_prompt("critic"),
            max_tokens=1800,
            temperature=0.3,
        )
        data = extract_json_block(response.text)
        if not isinstance(data, dict):
            return ResearchOutcome(markdown="I could not analyze that idea — could you paste the concept text directly?")
        lines = ["*Why this reads as generic*", ""]
        for d in data.get("diagnosis", []):
            lines.append(f"• {d}")
        if data.get("why_it_feels_generated"):
            lines.append(f"\nDominant pattern: {data['why_it_feels_generated']}")
        if data.get("salvageable") and data.get("stronger_direction"):
            lines.append("\n*A stronger direction*")
            lines.append(str(data["stronger_direction"]))
        elif data.get("stronger_direction"):
            lines.append("\n*Next step*")
            lines.append(str(data["stronger_direction"]))
        return ResearchOutcome(markdown="\n".join(lines))

    def _run_compare(self, triage: dict, user_text: str, *, user_id: str, thread_key: str) -> ResearchOutcome:
        idea = triage.get("idea_text") or user_text
        hotel = triage.get("hotel", "")
        registry_text, _ = self._concept_registry(-1, hotel or (self._db.latest_project_for_thread(thread_key) or {"hotel": ""})["hotel"])
        prompt = "Perform STAGE: COMPARE.\n\nNEW IDEA:\n" + idea[:6000] + "\n\nREGISTRY:\n" + registry_text
        response = self._provider.complete(
            [LLMMessage(role="user", content=prompt)],
            system=load_prompt("critic"),
            max_tokens=1500,
            temperature=0.2,
        )
        data = extract_json_block(response.text)
        if not isinstance(data, dict):
            return ResearchOutcome(markdown="I could not run that comparison — try naming the hotel or pasting both concepts.")
        verdict = str(data.get("verdict", "overlap")).upper()
        lines = [f"*Comparison verdict: {verdict}*", ""]
        for a in data.get("analysis", []):
            lines.append(f"• {a}")
        for o in data.get("overlaps", []):
            lines.append(f"⚠ overlap: {o}")
        if data.get("recommendation"):
            lines.append(f"\n*Recommendation*: {data['recommendation']}")
        return ResearchOutcome(markdown="\n".join(lines))

    def _run_review(self, triage: dict, user_text: str, *, user_id: str, thread_key: str) -> ResearchOutcome:
        """Prototype/article review: critique as an editorial director would."""
        content = triage.get("idea_text") or user_text
        prior = self._db.latest_project_for_thread(thread_key)
        context = ""
        if prior is not None and prior["property_dna"]:
            context = "\nPRIOR PROJECT DNA (same thread, for property-specific grounding):\n" + prior["property_dna"][:3000]
        prompt = (
            "Perform STAGE: REVIEW.\n\nCONTENT TO REVIEW:\n"
            + content[:12000] + context
        )
        response = self._provider.complete(
            [LLMMessage(role="user", content=prompt)],
            system=load_prompt("critic"),
            max_tokens=2500,
            temperature=0.3,
        )
        data = extract_json_block(response.text)
        if not isinstance(data, dict):
            return ResearchOutcome(
                markdown="I couldn't parse that for review — try pasting the text directly "
                "rather than a link or screenshot."
            )
        lines = []
        if data.get("works"):
            lines.append("*What works*")
            lines.extend(f"• {w}" for w in data["works"])
        if data.get("generic"):
            lines.append("\n*What feels generic / AI-generated*")
            lines.extend(f"• {g}" for g in data["generic"])
        if data.get("lost_specificity"):
            lines.append("\n*Where it loses property specificity*")
            lines.extend(f"• {l}" for l in data["lost_specificity"])
        if data.get("changes"):
            lines.append("\n*What I'd change (in priority order)*")
            for i, c in enumerate(data["changes"], 1):
                lines.append(f"{i}. {c}")
        if data.get("verdict"):
            lines.append(f"\n{data['verdict']}")
        return ResearchOutcome(markdown="\n".join(lines))

    # ------------------------------------------------------------------
    # misc
    # ------------------------------------------------------------------

    def _notify(self, stage: str) -> None:
        if self._cancel_check():
            raise AgentCancelled("Research was cancelled.")
        for cb in list(self._progress_callbacks):
            try:
                cb(stage)
            except Exception:
                logger.exception("progress callback failed")

    def add_progress_listener(self, cb: ProgressFn) -> None:
        self._progress_callbacks.append(cb)

    def remove_progress_listener(self, cb: ProgressFn) -> None:
        try:
            self._progress_callbacks.remove(cb)
        except ValueError:
            pass
