"""Typed plans, task briefs, evidence gates, and safe orchestration traces."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Literal, Sequence


SpecialistName = Literal["research", "editorial", "design", "builder", "critic"]
StepStatus = Literal["pending", "running", "complete", "failed", "blocked", "skipped"]


class ResponseMode(StrEnum):
    CONVERSATIONAL = "CONVERSATIONAL"
    RESEARCH_VERIFICATION = "RESEARCH_VERIFICATION"
    EDITORIAL_RECOMMENDATION = "EDITORIAL_RECOMMENDATION"
    CRITIQUE = "CRITIQUE"
    DESIGN_BRIEF = "DESIGN_BRIEF"
    ARTIFACT_DELIVERY = "ARTIFACT_DELIVERY"
    STATUS = "STATUS"


class EvidenceConfidence(StrEnum):
    VERIFIED_OFFICIAL = "verified_official"
    VERIFIED_AUTHORITATIVE_SECONDARY = "verified_authoritative_secondary"
    CORROBORATED_SECONDARY = "corroborated_secondary"
    SEARCH_SNIPPET_ONLY = "search_snippet_only"
    INFERRED = "inferred"
    UNSUPPORTED = "unsupported"


EDITORIAL_ALLOWED_CONFIDENCE = {
    EvidenceConfidence.VERIFIED_OFFICIAL.value,
    EvidenceConfidence.VERIFIED_AUTHORITATIVE_SECONDARY.value,
    EvidenceConfidence.CORROBORATED_SECONDARY.value,
}


@dataclass
class PlanStep:
    id: str
    specialist: SpecialistName
    objective: str
    depends_on: list[str] = field(default_factory=list)
    required_inputs: list[str] = field(default_factory=list)
    output_contract: str = ""
    success_criteria: list[str] = field(default_factory=list)
    stop_if: list[str] = field(default_factory=list)
    status: StepStatus = "pending"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OrchestrationPlan:
    user_goal: str
    project_id: str | None
    property_name: str | None
    current_stage: str
    desired_outcome: str
    known_verified: list[str] = field(default_factory=list)
    known_uncertain: list[str] = field(default_factory=list)
    critical_unknowns: list[str] = field(default_factory=list)
    steps: list[PlanStep] = field(default_factory=list)
    completion_criteria: list[str] = field(default_factory=list)
    response_mode: str = ResponseMode.CONVERSATIONAL.value

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def step(self, step_id: str) -> PlanStep | None:
        return next((step for step in self.steps if step.id == step_id), None)

    def set_status(self, step_id: str, status: StepStatus) -> None:
        step = self.step(step_id)
        if step is not None:
            step.status = status


@dataclass
class SpecialistTaskBrief:
    task_id: str
    project_id: str
    specialist: str
    mission: str
    user_goal: str
    current_context: str
    inputs: dict[str, Any] = field(default_factory=dict)
    verified_facts: list[str] = field(default_factory=list)
    unresolved_questions: list[str] = field(default_factory=list)
    explicit_instructions: list[str] = field(default_factory=list)
    prohibited_actions: list[str] = field(default_factory=list)
    required_output: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_prompt(self) -> str:
        """Compact, auditable task direction; never contains hidden reasoning."""
        blocks = [
            f"TASK ID\n{self.task_id}",
            f"MISSION\n{self.mission}",
            f"USER GOAL\n{self.user_goal}",
            f"CURRENT CONTEXT\n{self.current_context}",
        ]
        for title, values in (
            ("VERIFIED FACTS", self.verified_facts),
            ("UNRESOLVED QUESTIONS", self.unresolved_questions),
            ("EXPLICIT INSTRUCTIONS", self.explicit_instructions),
            ("PROHIBITED ACTIONS", self.prohibited_actions),
            ("REQUIRED OUTPUT", self.required_output),
            ("ACCEPTANCE CRITERIA", self.acceptance_criteria),
        ):
            if values:
                blocks.append(title + "\n" + "\n".join(f"- {value}" for value in values))
        return "\n\n".join(blocks)


@dataclass
class SpecialistEvaluation:
    answered_task: bool
    complete: bool
    evidence_safe: bool
    new_questions: list[str] = field(default_factory=list)
    next_action: str = ""
    reason_code: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def fact_text(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("fact") or item.get("signal") or item.get("claim") or "").strip()
    return str(item).strip()


def evidence_confidence(item: Any) -> str:
    if not isinstance(item, dict):
        return EvidenceConfidence.INFERRED.value
    return str(item.get("confidence") or item.get("evidence_level") or "inferred").lower()


def split_evidence(items: list[Any]) -> tuple[list[str], list[str]]:
    verified: list[str] = []
    uncertain: list[str] = []
    for item in items:
        text = fact_text(item)
        if not text:
            continue
        if evidence_confidence(item) in EDITORIAL_ALLOWED_CONFIDENCE:
            verified.append(text)
        else:
            uncertain.append(text)
    return verified, uncertain


_COMPACT_LIST_KEYS = ("verified_facts", "experience_signals")


_NAME_TOKEN = re.compile(r"\b[A-Z][\w'’-]{3,}")


def focus_names(focus_text: str, facts: Sequence[Any]) -> set[str]:
    """Distinctive proper names in a text (a concept, a draft) that the facts also use.

    Names common across the research (the city, the hotel) carry no signal, so a
    name only counts when at most a few facts mention it.
    """
    names = set(_NAME_TOKEN.findall(focus_text or ""))
    if not names:
        return set()
    texts = [fact_text(item) for item in facts]
    limit = max(3, len(texts) // 20)
    return {name for name in names if 0 < sum(name in text for text in texts) <= limit}


def compact_research_json(research: dict[str, Any], *, budget_chars: int, focus_text: str = "") -> str:
    """Serialize a research package for a specialist prompt without losing the newest evidence.

    Research accumulates across runs (follow-up rounds and documents append to
    the end), so slicing the JSON string kept only the oldest facts. Instead:
    deduplicate facts, put hotel-supplied documents first, then facts naming the
    distinctive places in `focus_text` (the concept or draft under review, so a
    reviewer sees the evidence for the restaurants and sights it names), then
    newest-first; drop the bulky provenance copies, and trim whole facts until it fits.
    """
    data = dict(research)
    for key in _COMPACT_LIST_KEYS:
        items = [item for item in data.get(key) or [] if fact_text(item)]
        seen: set[str] = set()
        unique: list[Any] = []
        for item in reversed(items):  # newest first
            norm = " ".join(fact_text(item).lower().split())
            if norm not in seen:
                seen.add(norm)
                unique.append(item)
        documents = [i for i in unique if isinstance(i, dict) and i.get("evidence_level") == "hotel_document"]
        names = focus_names(focus_text, unique) if focus_text else set()
        focused = sorted(
            (i for i in unique if i not in documents and any(n in fact_text(i) for n in names)),
            key=lambda i: -sum(n in fact_text(i) for n in names),
        )
        data[key] = documents + focused + [i for i in unique if i not in documents and i not in focused]
    data["source_records"] = [
        {k: record.get(k) for k in ("sid", "url", "title", "source_type", "fetched_ok")}
        for record in data.get("source_records") or [] if isinstance(record, dict)
    ]
    assets = data.get("available_assets") or []
    data["available_assets"] = assets[:15]
    if len(assets) > 15:
        data["available_assets_total"] = len(assets)

    def dump() -> str:
        return json.dumps(data, ensure_ascii=False)

    text = dump()
    while len(text) > budget_chars:
        longest = max(_COMPACT_LIST_KEYS + ("source_records",), key=lambda k: len(data.get(k) or []))
        if not data.get(longest):
            return text[:budget_chars]
        dropped = max(1, len(data[longest]) // 10)
        data[longest] = data[longest][:-dropped]
        data[f"{longest}_omitted"] = int(data.get(f"{longest}_omitted", 0)) + dropped
        text = dump()
    return text
