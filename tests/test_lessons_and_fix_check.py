"""The team's feedback becomes lessons; a critic's fixes are checked from the other side."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from app.llm.base import LLMResponse
from app.orchestration.fix_check import verify_fixes
from app.orchestration.handoffs import CritiqueResult
from test_orchestrator import _draft_setup, seed_project, setup_orchestrator


class ScriptedFast:
    """A fast model that answers by prompt: lessons, fix verdicts, else an empty object."""

    def __init__(self, lesson=None, verdicts=None):
        self.lesson, self.verdicts, self.calls = lesson, verdicts, []

    def complete(self, messages, *, system=None, **kwargs):
        self.calls.append((system or "")[:60])
        if system and "Turn the team's feedback" in system:
            return LLMResponse(text=json.dumps(self.lesson))
        if system and "Verify suggested fixes" in system:
            if isinstance(self.verdicts, Exception):
                raise self.verdicts
            return LLMResponse(text=json.dumps({"verdicts": self.verdicts}))
        return LLMResponse(text="{}")


def _lesson(rule="Use each photo at most once on a page.", applies_to=("guide",), conflicts=(), why="Repeats read as filler."):
    return {"lesson": rule, "why": why, "applies_to": list(applies_to), "conflicts_with": list(conflicts),
            "refines": [], "note": ""}


def test_feedback_becomes_a_general_lesson_for_the_flows_it_names(tmp_path: Path):
    o, db, *_ = setup_orchestrator(tmp_path, [])
    seed_project(db, thread="C:1")
    o._fast_provider = ScriptedFast(_lesson())
    reply = o.handle_message("feedback: ảnh này bị dùng lại 3 lần trong bài", user_id="U1", thread_key="C:1").markdown
    assert "Lesson #1" in reply and "Korean guide" in reply and "next run" in reply
    saved = db.lessons()[0]
    assert saved["feedback"].startswith("ảnh này") and saved["applies_to"] == ["guide"] and saved["why"]
    assert any(line.startswith("Lesson #1: Use each photo") for line in o._house_style_payload("H", "guide")["house_style"])
    assert not any("Lesson" in line for line in o._house_style_payload("H", "draft")["house_style"])


def test_a_conflicting_lesson_waits_and_can_replace_the_old_one(tmp_path: Path):
    o, db, *_ = setup_orchestrator(tmp_path, [])
    seed_project(db, thread="C:1")
    o._fast_provider = ScriptedFast(_lesson("Keep day summaries to one short sentence.", ("guide", "draft")))
    o.handle_message("feedback: summaries are too long", user_id="U1", thread_key="C:1")
    o._fast_provider = ScriptedFast(_lesson("Give each day summary two sentences.", ("guide",), conflicts=(1,)))
    reply = o.handle_message("feedback: summaries are too short now", user_id="U1", thread_key="C:1").markdown
    assert "goes against #1" in reply and "replace lesson #1 with #2" in reply
    assert [x["id"] for x in db.lessons()] == [1]  # the new one is on hold
    assert "Replaces" not in reply
    reply = o.handle_message("replace lesson #1 with #2", user_id="U1", thread_key="C:1").markdown
    assert "#2 replaces #1" in reply
    assert [x["id"] for x in db.lessons()] == [2]
    history = o.handle_message("lessons history", user_id="U1", thread_key="C:1").markdown
    assert "retired → #2" in history and "#1" in history
    assert "Retired lesson #2" in o.handle_message("remove lesson #2", user_id="U1", thread_key="C:1").markdown
    assert db.lessons() == []


def test_a_one_off_remark_is_not_saved_as_a_lesson(tmp_path: Path):
    o, db, *_ = setup_orchestrator(tmp_path, [])
    o._fast_provider = ScriptedFast({"lesson": "", "note": "it is a question about one photo"})
    reply = o.handle_message("feedback: is the Day 5 photo from February?", user_id="U1", thread_key="C:1").markdown
    assert reply.startswith("I didn't save a lesson") and db.lessons() == []


def test_concept_lessons_reach_the_editorial_brief(tmp_path: Path):
    o, db, *_ = setup_orchestrator(tmp_path, [])
    db.add_lesson("Lead with why the hotel suits the traveller.", why="Readers book on fit.",
                  applies_to=["concept"], feedback="x")
    assert o._lesson_lines("concept") == ["Lesson #1: Lead with why the hotel suits the traveller. (Why: Readers book on fit.)"]
    assert o._lesson_lines("guide") == []


def test_refuted_fixes_are_dropped_only_with_a_reason():
    fast = ScriptedFast(verdicts=[{"i": 0, "status": "refuted", "reason": "F12 names the restaurant"},
                                  {"i": 1, "status": "refuted", "reason": ""},
                                  {"i": 2, "status": "uncertain", "reason": "?"}])
    check = verify_fixes(fast, ["Replace Giano with [TK]", "Shorten Day 2", "Add a dish"],
                         text="t", evidence="e", context={})
    assert check.kept == ["Shorten Day 2", "Add a dish"]
    assert check.refuted == [{"fix": "Replace Giano with [TK]", "reason": "F12 names the restaurant"}]
    assert verify_fixes(ScriptedFast(verdicts=RuntimeError("down")), ["a", "b"], text="", evidence="", context={}).kept == ["a", "b"]


def test_the_draft_flow_drops_a_refuted_fix_before_the_rewrite(tmp_path: Path):
    revise = CritiqueResult(verdict="PASS_WITH_REVISIONS", reasons=["x"], revision_instructions=[
        "Replace 'Giano' with [TK: restaurant] (unsupported).", "Replace 'secret garden' with 'garden'."])
    o, db, pid, glm, _claude, _critic = _draft_setup(tmp_path, [revise, CritiqueResult(verdict="PASS", reasons=[])])
    o._settings = replace(o._settings, verify_fixes=True)
    o._fast_provider = ScriptedFast(verdicts=[{"i": 0, "status": "refuted", "reason": "S4 names Giano as the hotel's restaurant"}])
    outcome = o.handle_message("Write a sample draft", user_id="U1", thread_key="C:1")
    assert glm.tasks[1].qa_revisions == ["Replace 'secret garden' with 'garden'."]
    refuted = db.project_state(pid)["concept"]["sample_draft"]["qa_fixes"]["refuted"]
    assert refuted[0]["reason"].startswith("S4 names Giano")
    assert "1 suggested fix dropped after a second check" in outcome.markdown


def test_a_pass_whose_fixes_are_all_refuted_becomes_a_pass(tmp_path: Path):
    revise = CritiqueResult(verdict="PASS_WITH_REVISIONS", reasons=["x"], revision_instructions=["Cut the garden."])
    o, db, pid, glm, _claude, _critic = _draft_setup(tmp_path, [revise])
    o._settings = replace(o._settings, verify_fixes=True)
    o._fast_provider = ScriptedFast(verdicts=[{"i": 0, "status": "refuted", "reason": "the team's amendment keeps it"}])
    o.handle_message("Write a sample draft", user_id="U1", thread_key="C:1")
    assert len(glm.tasks) == 1  # no rewrite for a refuted fix
    assert db.project_state(pid)["concept"]["sample_draft"]["qa"]["verdict"] == "PASS"
