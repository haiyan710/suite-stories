"""The app's state machines: enums match stored values, flows make only listed moves."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.orchestration.handoffs import CritiqueResult
from app.states import (
    ConceptStatus, DraftStage, ProjectStage, SessionStatus, Verdict, draft_stage, next_actions,
    status_after_critique, transition, unexpected_transitions,
)
from app.storage import Database
from test_orchestrator import _draft_setup, seed_project, setup_orchestrator


@pytest.fixture(autouse=True)
def _clean_counter():
    unexpected_transitions.clear()
    yield
    unexpected_transitions.clear()


def test_enum_values_are_the_strings_already_stored():
    # These exact strings are in live databases: renaming a value would orphan rows.
    assert {s.value for s in SessionStatus} >= {"idle", "researching", "awaiting_clarification", "complete", "cancelled"}
    assert {s.value for s in ConceptStatus} == {"generated", "shortlisted", "approved", "published", "rejected", "test"}
    assert {"research_ready", "concept_ready", "concept_critiqued", "artifact_ready"} <= {s.value for s in ProjectStage}
    assert SessionStatus.COMPLETE == "complete" and f"{Verdict.PASS}" == "PASS"


def test_unexpected_moves_are_logged_and_counted_but_applied():
    assert transition("concept", "test", "shortlisted") == "shortlisted"
    assert unexpected_transitions[("concept", "test", "shortlisted")] == 1
    assert transition("concept", "rejected", "shortlisted") == "shortlisted"  # a later pass revives
    assert transition("concept", "", "shortlisted") == "shortlisted"  # first save: no old state
    assert len(unexpected_transitions) == 1


def test_critique_lifecycle():
    assert status_after_critique("shortlisted", "FAIL") == ConceptStatus.REJECTED
    assert status_after_critique("published", "FAIL") is None
    assert status_after_critique("rejected", "PASS") == ConceptStatus.SHORTLISTED
    assert status_after_critique("shortlisted", "PASS") is None
    assert status_after_critique("test", "FAIL") is None


def test_draft_stage_is_derived_from_the_stored_draft():
    assert draft_stage({}) is DraftStage.NONE
    draft = {"body": "x", "qa": {"verdict": "PASS"}}
    assert draft_stage({"sample_draft": draft}) is DraftStage.PASSED
    assert draft_stage({"sample_draft": {**draft, "qa": {"verdict": "PASS_WITH_REVISIONS"}}}) is DraftStage.NEEDS_FIXES
    assert draft_stage({"sample_draft": {**draft, "qa": {"verdict": "FAIL"}, "layout_html": "p"}}) is DraftStage.FAILED
    assert draft_stage({"sample_draft": {**draft, "layout_html": "p"}}) is DraftStage.LAID_OUT


def test_next_actions_follow_the_state():
    assert "research" in next_actions({})[0]
    assert "Answer the question" in next_actions({"project": {}}, SessionStatus.AWAITING_CLARIFICATION)[0]
    assert "interrupted" in next_actions({"project": {"status": "interrupted"}, "research": {"x": 1}})[0]
    shortlisted = {"project": {}, "research": {"x": 1}, "concept": {"status": "shortlisted"}}
    assert "write a sample draft" in next_actions(shortlisted)[0]
    passed = {**shortlisted, "concept": {"status": "shortlisted", "sample_draft": {"body": "x", "qa": {"verdict": "PASS"}}}}
    assert "make the layout" in next_actions(passed)[0]
    options = {**shortlisted, "concept": {"status": "shortlisted", "active_option": "A",
                                          "options": [{"label": "A"}, {"label": "B"}]}}
    assert "use option B" in next_actions(options)[0]


def test_startup_recovery_frees_sessions_and_projects_left_mid_run(tmp_path: Path):
    db = Database(tmp_path / "x.db")
    db.upsert_session("C:1", thread_ts="1", status=SessionStatus.RESEARCHING)
    db.upsert_session("C:2", thread_ts="2", status=SessionStatus.COMPLETE)
    pid = db.create_project(hotel="Hotel X", request="r")
    db.update_project_state(pid, "research", {}, status=ProjectStage.RESEARCHING)
    assert db.recover_interrupted() == (1, 1)
    assert db.get_session("C:1").status == "idle" and db.get_session("C:2").status == "complete"
    assert db.project_state(pid)["project"]["status"] == "interrupted"
    assert db.recover_interrupted() == (0, 0)


def test_a_stale_question_no_longer_swallows_the_next_request(tmp_path: Path):
    o, db, pid, glm, _claude, critic = _draft_setup(tmp_path, [CritiqueResult(verdict="PASS", reasons=[])])
    db.upsert_session("C:1", thread_ts="1", status=SessionStatus.AWAITING_CLARIFICATION,
                      pending_question_type="property_identity", pending_question_text="Which hotel?")
    with db.transaction() as conn:
        conn.execute("UPDATE thread_sessions SET updated_at=datetime('now','-2 days') WHERE thread_key='C:1'")
    outcome = o.handle_message("Write a sample draft", user_id="U1", thread_key="C:1")
    assert outcome.action == "WRITE_SAMPLE_DRAFT" and glm.tasks


def test_an_explicit_command_is_not_taken_as_the_answer(tmp_path: Path):
    o, db, pid, glm, _claude, critic = _draft_setup(tmp_path, [CritiqueResult(verdict="PASS", reasons=[])])
    db.upsert_session("C:1", thread_ts="1", status=SessionStatus.AWAITING_CLARIFICATION,
                      pending_question_type="property_identity", pending_question_text="Which hotel?")
    outcome = o.handle_message("Write a sample draft", user_id="U1", thread_key="C:1")
    assert outcome.action == "WRITE_SAMPLE_DRAFT" and glm.tasks


def test_status_reply_shows_the_draft_stage_and_what_to_do_next(tmp_path: Path):
    o, db, pid, glm, _claude, critic = _draft_setup(tmp_path, [CritiqueResult(verdict="PASS", reasons=[])])
    o.handle_message("Write a sample draft", user_id="U1", thread_key="C:1")
    reply = o._project_status(db.project_state(pid)).markdown
    assert "Draft: passed" in reply and "*Next*" in reply and "make the layout" in reply


def test_real_flows_make_only_listed_concept_moves(tmp_path: Path):
    o, db, *_ = setup_orchestrator(
        tmp_path, ['{"intent":"RESEARCH_PROPERTY","hotel":"Hotel X"}', '{"intent":"CRITIQUE_CONCEPT"}'],
        critic_results=[CritiqueResult(verdict="PASS", reasons=["ok"]),
                        CritiqueResult(verdict="FAIL", reasons=["weak"], fail_reason="CONCEPTUAL_WEAKNESS")],
    )
    o.handle_message("Research Hotel X and find one stay experience.", user_id="U1", thread_key="C:flow")
    o.handle_message("critique this concept", user_id="U1", thread_key="C:flow")
    assert not unexpected_transitions, dict(unexpected_transitions)
