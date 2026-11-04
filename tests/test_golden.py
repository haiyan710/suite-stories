from __future__ import annotations

import json
from pathlib import Path

from app.orchestration.golden import evaluate_golden_result
from app.orchestration.handoffs import CritiqueResult, EditorialResult
from test_orchestrator import seed_project, setup_orchestrator


def test_golden_case_evaluates_quality_without_prompt_leakage(tmp_path: Path):
    fixture = json.loads((Path(__file__).parent / "fixtures" / "golden_case.json").read_text())
    editorial = EditorialResult(
        status="complete",
        strongest_experience=(
            "Use Harbour Grand Kobe as one settled base: travel into a different part of "
            "Kansai by day, then return to the same room and recovery routine each evening."
        ),
        why_someone_would_want_it=(
            "It turns a fragmented multi-city itinerary into a calmer stay: Kyoto, Nara, Himeji, "
            "and Kobe can change each day without repeated hotel changes, luggage moves, or check-ins."
        ),
        why_this_property_enables_it=(
            "The hotel's verified Harbourland transport context and repeatable in-property recovery "
            "facilities make it a credible fixed base rather than a generic Kobe hotel claim."
        ),
        editorial_proposition="A changing Kansai journey anchored by one reliable Kobe return.",
        possible_story_structure=(
            "Each chapter departs toward a different destination, follows one day outside the hotel, "
            "and ends with the same return sequence, making continuity visible across the stay."
        ),
        visual_storytelling_device=(
            "Pair changing destination landscapes with one repeated room-or-recovery frame, so the "
            "visual system makes movement and stability legible without an amenity grid."
        ),
        working_title="One Base, Many Kansai Days",
        supporting_evidence_ids=["S1"],
    )
    o, db, _r, _e, _d, _b, _c = setup_orchestrator(
        tmp_path, ['{"intent":"FIND_EDITORIAL_OPPORTUNITY"}'],
        [editorial], [CritiqueResult(verdict="PASS", reasons=["Tangible and demonstrable."])],
    )
    pid = seed_project(db, thread="C:golden")
    state = db.project_state(pid)
    state["research"]["verified_facts"] = [{
        "fact": "The hotel is in Harbourland with verified transport access.",
        "source_ids": ["S1"], "confidence": "verified_official",
    }]
    state["research"]["destination_relationship"] = [
        "The Kobe base supports realistic day travel across Kansai."
    ]
    db.update_project_state(pid, "research", state["research"])

    outcome = o.handle_message(fixture["request"], user_id="U1", thread_key="C:golden")
    saved = db.project_state(pid)
    evaluation = evaluate_golden_result(
        editorial=saved["concept"]["editorial"], research=saved["research"],
        critic=saved["concept"]["critic_result"], pass_threshold=fixture["pass_threshold"],
        property_terms=tuple(fixture["property_terms"]),
        destination_terms=tuple(fixture["destination_terms"]),
    )

    assert evaluation.passed, evaluation.to_dict()
    assert saved["concept"]["status"] == "shortlisted"
    assert "One Base, Many Kansai Days" in outcome.markdown
    # The fixture contains criteria only; no expected concept or answer is sent to an agent brief.
    serialized_briefs = json.dumps([brief.to_dict() for brief in o.last_briefs])
    assert "One Base, Many Kansai Days" not in serialized_briefs
