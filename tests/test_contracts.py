"""Contract tests: the seams between parts, where a change on one side silently breaks the other.

1. Prompt <-> Python: every key Python reads from a model's JSON answer is a key the
   prompt's JSON example promises, and the reader still reads it (so this map
   cannot go stale without failing).
2. Main flow <-> side flow: the Korean guide reads only the keys that travel with
   the active option (OPTION_KEYS), only through the EditorialHost helpers, and
   writes only its own key.
3. Side flow <-> golden evaluator: what the flow stores is what `evaluate_guide` reads.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from app.orchestration.handoffs import CritiqueResult, DraftResult, EditorialResult
from app.orchestration.orchestrator import EditorialOrchestrator
from app.prompts import _FILES, load_prompt, prompt_json_example, prompt_json_keys
from app.side_flows import guide_eval, korean_guide
from app.states import Verdict

ROOT = Path(__file__).resolve().parent.parent

# prompt -> (files that parse its answer, keys they read). Add a row with every new JSON prompt.
PROMPT_CONTRACTS: dict[str, tuple[tuple[str, ...], set[str]]] = {
    "orchestrator": (("app/orchestration/orchestrator.py",), {"intent", "hotel", "needs_clarification", "target_version"}),
    "editorial": (("app/orchestration/handoffs.py", "app/orchestration/orchestrator.py"), {
        "status", "strongest_experience", "why_someone_would_want_it", "why_this_property_enables_it",
        "editorial_proposition", "possible_story_structure", "visual_storytelling_device",
        "research_questions", "supporting_evidence_ids", "evidence_dependencies", "working_title",
    }),
    "critic_editorial": (("app/agents/critic.py", "app/orchestration/orchestrator.py"), {
        "verdict", "reasons", "revision_instructions", "unresolved_concerns", "evidence_matches", "fail_reason",
        "central", "claim", "status", "impact",
    }),
    "critic_draft": (("app/agents/critic.py",), {"verdict", "reasons", "revision_instructions", "polish", "fail_reason"}),
    "critic_visual": (("app/agents/critic.py",), {"verdict", "reasons", "revision_instructions"}),
    "critique_summary": (("app/orchestration/orchestrator.py",), {"headline", "why", "to_fix", "next_step"}),
    "outline": (("app/orchestration/orchestrator.py",), {
        "title", "title_options", "angle", "why_readers_want_it", "proof_points", "avoid", "check_with_hotel",
        "insider_notes", "opening_hook", "stay_info",
    }),
    "writer": (("app/agents/writer.py",), {"headline", "standfirst", "body", "writer_notes", "source_notes"}),
    "humanizer": (("app/agents/writer.py",), {"headline", "standfirst", "body", "added_facts"}),
    "draft_fixes": (("app/agents/writer.py",), {"edits", "skipped"}),
    "layout_images": (("app/artifacts/layout.py", "app/orchestration/orchestrator.py"), {"hero", "hero_shot", "shots", "pull_quote"}),
    "dining_picks": (("app/orchestration/orchestrator.py",), {"picks", "fact_ids"}),
    "venue_notes": (("app/orchestration/orchestrator.py", "app/orchestration/draft_checks.py"), {
        "venues", "hotel_spaces", "closed_days", "open_on_trip",
    }),
    "logistics_extract": (("app/agents/logistics.py",), {"days", "stops", "time_given"}),
    "lesson_generalize": (("app/orchestration/orchestrator.py",), {"lesson", "why", "applies_to", "conflicts_with"}),
    "verify_fixes": (("app/orchestration/fix_check.py",), {"verdicts", "status", "reason"}),
    "option_judge": (("app/orchestration/option_panel.py",), {"scores", "label", "score", "strength", "weakness", "best", "take", "from", "what"}),
    "guide_ko": (("app/artifacts/guide_ko.py", "app/side_flows/korean_guide.py"), {
        "hotel_ko", "title", "summary", "theme", "travel_tip", "days", "day", "moments", "time", "text",
        "photo", "place", "hero_photo",
    }),
    "guide_ko_check": (("app/side_flows/korean_guide.py",), {"verdict", "fixes"}),
    "guide_place_photos": (("app/side_flows/korean_guide.py",), {"picks", "place", "pick"}),
    "guide_place_photos_look": (("app/side_flows/korean_guide.py",), {"picks", "place", "pick"}),
}


@pytest.mark.parametrize("prompt", sorted(PROMPT_CONTRACTS))
def test_python_reads_only_keys_the_prompt_promises(prompt):
    files, keys = PROMPT_CONTRACTS[prompt]
    promised = prompt_json_keys(prompt)
    assert promised, f"{prompt}: no JSON example found after its 'Return JSON' cue"
    assert keys <= promised, f"{prompt}: Python reads {sorted(keys - promised)} that the prompt never returns"
    source = "\n".join((ROOT / f).read_text(encoding="utf-8") for f in files)
    stale = {k for k in keys if f'"{k}"' not in source and k not in _dataclass_fields(prompt)}
    assert not stale, f"{prompt}: {sorted(stale)} no longer read in {files}; update PROMPT_CONTRACTS"


def _dataclass_fields(prompt: str) -> set[str]:
    """Keys read through a dataclass's from_dict (field names) rather than a quoted literal."""
    return {
        "editorial": set(EditorialResult.__dataclass_fields__),
        "writer": set(DraftResult.__dataclass_fields__),
    }.get(prompt, set())


def test_every_prompt_that_asks_for_json_has_a_readable_example():
    cue = re.compile(r"return (?:only )?json|json only", re.I)
    without = sorted(n for n in _FILES if cue.search(load_prompt(n)) and not prompt_json_keys(n))
    # Prompts whose answer is a fenced or free-form block, parsed by its own code.
    assert set(without) <= {"builder", "designer", "final"}, without
    assert set(PROMPT_CONTRACTS) <= set(_FILES)


def test_critic_prompts_name_exactly_the_app_verdicts():
    values = {v.value for v in Verdict}
    for prompt in ("critic_editorial", "critic_draft", "critic_visual"):
        example = prompt_json_example(prompt)
        named = set(re.findall(r"PASS_WITH_REVISIONS|PASS|FAIL", example))
        assert named == values, (prompt, named)
    assert set(CritiqueResult.__dataclass_fields__) >= {"verdict", "reasons", "revision_instructions", "polish"}


def test_the_editorial_contract_fields_are_all_in_the_prompt():
    required = set(inspect.getsource(EditorialOrchestrator._editorial_contract_gaps).split('"')[1::2])
    required &= set(EditorialResult.__dataclass_fields__)
    assert required and required <= prompt_json_keys("editorial"), sorted(required - prompt_json_keys("editorial"))


# ---- main flow <-> side flow -----------------------------------------------------------------

SIDE_FLOW = (ROOT / "app/side_flows/korean_guide.py").read_text(encoding="utf-8")


def test_the_side_flow_reads_only_what_travels_with_the_active_option():
    read = set(re.findall(r'concept_state\.get\("(\w+)"\)', SIDE_FLOW))
    read |= set(re.findall(r'\(state\.get\("concept"\) or \{\}\)\.get\("(\w+)"\)', SIDE_FLOW))
    assert read, "the side flow reads nothing from concept_state?"
    assert read <= set(EditorialOrchestrator.OPTION_KEYS), sorted(read - set(EditorialOrchestrator.OPTION_KEYS))


def test_the_side_flow_writes_only_its_own_key():
    written = set(re.findall(r'concept_state\["(\w+)"\]\s*=', SIDE_FLOW))
    assert written == {"guide_ko"}, written
    stages = set(re.findall(r'update_project_state\([^,]+,\s*"(\w+)"', SIDE_FLOW))
    assert stages == {"concept"}, stages
    assert "status=" not in "".join(re.findall(r"update_project_state\([^)]*\)", SIDE_FLOW))
    assert "guide_ko" not in EditorialOrchestrator.OPTION_KEYS  # the guide never moves concept state


def test_the_side_flow_uses_only_declared_host_helpers():
    used = set(re.findall(r"self\._host\.(\w+)", SIDE_FLOW))
    declared = {
        name for name in vars(korean_guide.EditorialHost)
        if not name.startswith("__") and name not in {"_is_protocol", "_is_runtime_protocol"}
    } | set(getattr(korean_guide.EditorialHost, "__annotations__", {}))
    assert used <= declared, sorted(used - declared)
    missing = {name for name in used if not hasattr(EditorialOrchestrator, name) and name != "_trip_nights"}
    assert not missing, missing


def test_the_side_flow_reads_draft_and_concept_fields_that_exist():
    draft_keys = set(re.findall(r'draft\.get\(k\) for k in \(([^)]*)\)', SIDE_FLOW)[0].replace('"', "").replace(" ", "").split(","))
    assert {k for k in draft_keys if k} <= set(DraftResult.__dataclass_fields__)
    concept_block = re.search(r'"concept": \{k: concept\.get\(k\) for k in \((.*?)\)\}', SIDE_FLOW, re.S).group(1)
    concept_keys = set(re.findall(r'"(\w+)"', concept_block))
    assert concept_keys <= set(EditorialResult.__dataclass_fields__), sorted(concept_keys - set(EditorialResult.__dataclass_fields__))


# ---- side flow <-> golden evaluator ----------------------------------------------------------

def test_the_golden_evaluator_reads_what_the_side_flow_stores(tmp_path):
    from PIL import Image
    from test_guide_ko import FakeCommons, GuideProvider, PickProvider, guide
    from test_orchestrator import _draft_setup

    o, db, pid, *_ = _draft_setup(tmp_path, [CritiqueResult(verdict="PASS", reasons=[])])
    image = tmp_path / "sight.jpg"
    Image.new("RGB", (1600, 1000)).save(image)
    concept = dict(db.project_state(pid)["concept"])
    concept["format"] = {"type": "itinerary", "nights": 3}
    db.update_project_state(pid, "concept", concept)
    written = guide(hero_photo="")
    written["days"][1]["moments"][0].update(title="분수 광장", place="Fountain Square, Harbourtown")
    o._provider = GuideProvider([written])
    o._fast_provider = PickProvider({"verdict": "PASS", "fixes": []})
    o._place_photos = FakeCommons(image)
    o.handle_message("hotel guide", user_id="U1", thread_key="C:1")
    stored = db.project_state(pid)["concept"]["guide_ko"]
    read = set(re.findall(r'stored\.get\("(\w+)"\)', inspect.getsource(guide_eval.evaluate_guide)))
    assert read <= set(stored), sorted(read - set(stored))
    credit_keys = set(re.findall(r'c\.get\("(\w+)"\)', inspect.getsource(guide_eval.evaluate_guide)))
    assert credit_keys <= set(stored["photo_credits"]["w1"]), sorted(credit_keys - set(stored["photo_credits"]["w1"]))
    result = guide_eval.evaluate_guide(stored, {"max_check_items": 20})
    assert result.score == result.total, result.to_dict()
