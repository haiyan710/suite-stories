"""Non-leaking regression evaluation for experience-led editorial output.

Property and destination vocabulary comes from the golden fixture, never from code.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .planning import EDITORIAL_ALLOWED_CONFIDENCE, evidence_confidence


@dataclass(frozen=True)
class GoldenEvaluation:
    scores: dict[str, int]
    total: int
    passed: bool
    failures: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_golden_result(
    *, editorial: dict[str, Any], research: dict[str, Any], critic: dict[str, Any],
    pass_threshold: int = 28,
    property_terms: tuple[str, ...] = ("hotel",),
    destination_terms: tuple[str, ...] = ("destination",),
) -> GoldenEvaluation:
    """Score quality traits after generation; no expected answer is fed to agents."""
    experience = str(editorial.get("strongest_experience") or "")
    desire = str(editorial.get("why_someone_would_want_it") or "")
    property_reason = str(editorial.get("why_this_property_enables_it") or "")
    proposition = str(editorial.get("editorial_proposition") or "")
    story = str(editorial.get("possible_story_structure") or "")
    visual = str(editorial.get("visual_storytelling_device") or "")
    combined = " ".join((experience, desire, property_reason, proposition, story, visual)).lower()
    generic_phrases = ("unforgettable luxury", "world-class amenities", "luxury escape", "something for everyone")

    facts = list(research.get("verified_facts") or [])
    supporting = {str(value) for value in editorial.get("supporting_evidence_ids") or []}
    safe_ids = {
        str(sid) for fact in facts
        if evidence_confidence(fact) in EDITORIAL_ALLOWED_CONFIDENCE
        for sid in (fact.get("source_ids", []) if isinstance(fact, dict) else [])
    }
    scores = {
        "experience_strength": 5 if len(experience) >= 80 and any(word in combined for word in ("day", "return", "stay", "night", "base")) else 3 if experience else 0,
        "booking_desire": 5 if len(desire) >= 90 else 3 if desire else 0,
        "property_specificity": 5 if len(property_reason) >= 90 and any(word in property_reason.lower() for word in property_terms) else 3 if property_reason else 0,
        "evidence_quality": 5 if supporting and supporting.issubset(safe_ids) else 3 if facts else 0,
        "destination_relationship": 5 if research.get("destination_relationship") and any(word in combined for word in destination_terms) else 3 if research.get("destination_relationship") else 0,
        "editorial_originality": 5 if critic.get("verdict") == "PASS" and not any(p in combined for p in generic_phrases) else 3 if critic.get("verdict") == "PASS_WITH_REVISIONS" else 1,
        "visual_story_potential": 5 if len(visual) >= 60 and len(story) >= 60 else 3 if visual and story else 0,
        "non_genericness": 5 if not any(p in combined for p in generic_phrases) and len(property_reason) >= 90 else 2,
    }
    total = sum(scores.values())
    failures = tuple(name for name, score in scores.items() if score < 3)
    return GoldenEvaluation(
        scores=scores, total=total,
        passed=total >= pass_threshold and not failures,
        failures=failures,
    )
