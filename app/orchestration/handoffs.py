"""Typed, compact handoff contracts between the orchestrator and specialists."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from .planning import SpecialistTaskBrief


JsonDict = dict[str, Any]


@dataclass
class ResearchTask:
    project_id: int
    hotel_name: str
    original_request: str
    city_hint: str = ""
    audience: str = ""
    article_url: str = ""
    constraints: list[str] = field(default_factory=list)
    followup_questions: list[str] = field(default_factory=list)
    existing_research: JsonDict = field(default_factory=dict)
    brief: SpecialistTaskBrief | None = None
    verification_round: int = 1
    # >0 asks for extra destination coverage for a multi-day itinerary.
    trip_nights: int = 0
    # Follow-up questions about the destination (venues, sights) rather than hotel claims:
    # searched in destination guides and press instead of the hotel's own site.
    destination: bool = False
    # Follow-up questions about what staying there is like (guest reviews, insider tips,
    # stories): searched in press, blogs and review sites; facts become guest reports.
    experience: bool = False


@dataclass
class ResearchResult:
    status: Literal["complete", "partial", "needs_clarification", "failed"]
    property_identity: JsonDict = field(default_factory=dict)
    verified_facts: list[JsonDict] = field(default_factory=list)
    experience_signals: list[JsonDict] = field(default_factory=list)
    destination_relationship: list[str] = field(default_factory=list)
    audience_relevance: list[str] = field(default_factory=list)
    existing_publication_content: list[str] = field(default_factory=list)
    available_assets: list[JsonDict] = field(default_factory=list)
    source_records: list[JsonDict] = field(default_factory=list)
    uncertainties: list[str] = field(default_factory=list)
    contradictions: list[str] = field(default_factory=list)
    recommended_followup_questions: list[str] = field(default_factory=list)
    property_dna: JsonDict = field(default_factory=dict)
    classification: JsonDict = field(default_factory=dict)
    verification_claims: list[JsonDict] = field(default_factory=list)
    critical_unknowns: list[str] = field(default_factory=list)
    version: int = 1

    def to_dict(self) -> JsonDict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: JsonDict) -> "ResearchResult":
        fields = cls.__dataclass_fields__
        return cls(**{k: v for k, v in data.items() if k in fields})


@dataclass
class EditorialTask:
    project_id: int
    hotel_name: str
    research: ResearchResult
    user_goal: str
    current_concept: JsonDict = field(default_factory=dict)
    user_feedback: str = ""
    concept_memory: list[JsonDict] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    brief: SpecialistTaskBrief | None = None


@dataclass
class EditorialResult:
    status: Literal["complete", "needs_research", "no_strong_direction", "failed"]
    strongest_experience: str = ""
    why_someone_would_want_it: str = ""
    why_this_property_enables_it: str = ""
    editorial_proposition: str = ""
    possible_story_structure: str = ""
    visual_storytelling_device: str = ""
    supporting_hotel_elements: list[str] = field(default_factory=list)
    required_assets: list[str] = field(default_factory=list)
    weak_alternatives: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    research_questions: list[str] = field(default_factory=list)
    candidate_hypotheses: list[JsonDict] = field(default_factory=list)
    supporting_evidence_ids: list[str] = field(default_factory=list)
    evidence_dependencies: list[JsonDict] = field(default_factory=list)
    working_title: str = ""
    version: int = 1

    def to_dict(self) -> JsonDict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: JsonDict) -> "EditorialResult":
        fields = cls.__dataclass_fields__
        return cls(**{k: v for k, v in data.items() if k in fields})


@dataclass
class DesignTask:
    project_id: int
    concept: JsonDict
    research: JsonDict
    assets: list[JsonDict]
    user_feedback: str = ""
    previous_design: JsonDict = field(default_factory=dict)
    brief: SpecialistTaskBrief | None = None


@dataclass
class DesignSpec:
    editorial_objective: str
    page_narrative: list[str]
    visual_grammar: str
    section_structure: list[JsonDict]
    image_roles: list[JsonDict]
    typography_direction: str
    spacing_and_rhythm: str
    interaction_direction: str
    responsive_notes: str
    asset_mapping: list[JsonDict]
    things_to_avoid: list[str]
    builder_instructions: list[str]
    status: Literal["draft", "approved", "superseded"] = "draft"
    version: int = 1

    def to_dict(self) -> JsonDict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: JsonDict) -> "DesignSpec":
        fields = cls.__dataclass_fields__
        return cls(**{k: v for k, v in data.items() if k in fields})


@dataclass
class BuildTask:
    project_id: int
    hotel_slug: str
    concept: JsonDict
    design_spec: DesignSpec
    assets: list[JsonDict]
    previous_artifact: JsonDict = field(default_factory=dict)
    technical_constraints: list[str] = field(default_factory=list)
    brief: SpecialistTaskBrief | None = None


@dataclass
class ArtifactResult:
    artifact_id: int | None
    project_id: int
    version: int
    status: str
    path: str
    files: list[str]
    source_design_version: int
    preview_path: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class CritiqueTask:
    project_id: int
    mode: Literal["EDITORIAL", "VISUAL", "DRAFT"]
    concept: JsonDict
    research: JsonDict = field(default_factory=dict)
    design_spec: JsonDict = field(default_factory=dict)
    artifact: JsonDict = field(default_factory=dict)
    draft: JsonDict = field(default_factory=dict)
    outline: JsonDict = field(default_factory=dict)
    concept_memory: list[JsonDict] = field(default_factory=list)
    brief: SpecialistTaskBrief | None = None


@dataclass
class DraftTask:
    project_id: int
    hotel_name: str
    user_request: str
    concept: JsonDict
    outline: JsonDict
    research: JsonDict
    concept_revisions: list[str] = field(default_factory=list)
    previous_draft: JsonDict = field(default_factory=dict)
    qa_revisions: list[str] = field(default_factory=list)
    brief: SpecialistTaskBrief | None = None
    # {"type": "itinerary", "nights": N} for a multi-day trip piece; empty for a single-experience piece.
    format: JsonDict = field(default_factory=dict)


@dataclass
class DraftResult:
    status: Literal["complete", "failed"]
    headline: str = ""
    standfirst: str = ""
    body: str = ""
    tk_items: list[str] = field(default_factory=list)
    writer_notes: str = ""
    model: str = ""
    # For the editor: [ASK <contact>: ...] notes in the body, and claim -> S# ids (never shown to readers).
    ask_items: list[str] = field(default_factory=list)
    source_notes: list[dict] = field(default_factory=list)

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class CritiqueResult:
    verdict: Literal["PASS", "PASS_WITH_REVISIONS", "FAIL"]
    reasons: list[str]
    revision_instructions: list[str] = field(default_factory=list)
    unresolved_concerns: list[str] = field(default_factory=list)
    evidence_matches: list[JsonDict] = field(default_factory=list)
    fail_reason: str = ""
    # Draft QA only: wording suggestions that do not block a PASS.
    polish: list[str] = field(default_factory=list)

    def to_dict(self) -> JsonDict:
        return asdict(self)
