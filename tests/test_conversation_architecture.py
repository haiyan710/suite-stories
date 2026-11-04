"""Deterministic acceptance tests for sessions, routing, memory, and Slack events."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import pytest

from app.agent import AgentError, HotelResearchAgent, ResearchOutcome
from app.config import Settings
from app.llm.base import LLMMessage, LLMResponse
from app.slack_app import accepts_slack_event
from app.storage import Database


class RouterProvider:
    def __init__(self, actions: list[str], conversational: str = "I help research hotels and develop evidence-led editorial experiences.") -> None:
        self.actions = list(actions)
        self.conversational = conversational
        self.search_calls = 0

    def complete(self, messages: Sequence[LLMMessage], *, system=None, **kwargs) -> LLMResponse:
        if system and "Conversational action router" in system:
            return LLMResponse(text=self.actions.pop(0))
        return LLMResponse(text=self.conversational)

    def complete_with_search(self, messages, **kwargs) -> LLMResponse:
        self.search_calls += 1
        return LLMResponse(text="[]")


def make_agent(tmp_path: Path, provider: RouterProvider) -> tuple[HotelResearchAgent, Database]:
    db = Database(tmp_path / "conversation.db")
    settings = Settings(
        slack_bot_token="x", slack_app_token="x", zai_api_key="x",
        database_path=tmp_path / "conversation.db", allowed_user_ids=frozenset({"U1"}),
    )
    return HotelResearchAgent(settings, provider, db), db


def test_meta_question_uses_zero_research_tools(tmp_path: Path):
    provider = RouterProvider([
        '{"action":"META_QUESTION","intent":"meta","hotel":"","suggested_tools":[]}'
    ])
    agent, db = make_agent(tmp_path, provider)
    progress: list[str] = []
    agent.add_progress_listener(progress.append)

    outcome = agent.handle_message(
        "Explain your role.", user_id="U1", thread_key="C:1", channel_id="C", thread_ts="1"
    )

    assert "help research hotels" in outcome.markdown
    assert outcome.action == "META_QUESTION"
    assert provider.search_calls == 0
    assert progress == []
    assert db.latest_project_for_thread("C:1") is None


def test_develop_idea_reuses_context_without_web_search(tmp_path: Path):
    provider = RouterProvider([
        '{"action":"DEVELOP_IDEA","intent":"conversation","hotel":"Hotel X",'
        '"suggested_tools":[]}'
    ], conversational="The river can become the main experience by structuring the stay around its changing light.")
    agent, db = make_agent(tmp_path, provider)
    db.upsert_session(
        "C:idea", status="complete", current_property="Hotel X",
        original_request="Research Hotel X", conversation_summary="assistant: Previous concept",
    )
    progress: list[str] = []
    agent.add_progress_listener(progress.append)
    outcome = agent.handle_message(
        "This is too generic. Try again using the research you already have.",
        user_id="U1", thread_key="C:idea",
    )
    assert "river" in outcome.markdown
    assert outcome.action == "DEVELOP_IDEA"
    assert provider.search_calls == 0
    assert progress == []


def test_pending_clarification_resumes_original_task(tmp_path: Path):
    provider = RouterProvider([
        '{"action":"RESEARCH_NEW_PROPERTY","intent":"research","hotel":"",'
        '"needs_clarification":true,"clarification":"Which hotel?","goal":"find an experience"}',
        '{"action":"GENERAL_CONVERSATION","intent":"conversation","hotel":""}',
    ])
    agent, db = make_agent(tmp_path, provider)
    captured: dict[str, str] = {}

    def fake_research(triage, user_text, **kwargs):
        captured["hotel"] = triage["hotel"]
        captured["request"] = user_text
        return ResearchOutcome(markdown="resumed")

    agent._run_research = fake_research  # type: ignore[method-assign]
    first = agent.handle_message(
        "Research Capella Bangkok and find an experience.", user_id="U1",
        thread_key="C:2", channel_id="C", thread_ts="2",
    )
    assert first.clarification == "Which hotel?"
    assert db.get_session("C:2").status == "awaiting_clarification"  # type: ignore[union-attr]

    second = agent.handle_message(
        "The Silveri Hotel Hong Kong - MGallery Collection", user_id="U1",
        thread_key="C:2", channel_id="C", thread_ts="2",
    )
    assert second.markdown == "resumed"
    assert captured["hotel"] == "The Silveri Hotel Hong Kong - MGallery Collection"
    assert "Research Capella Bangkok" in captured["request"]
    assert db.get_session("C:2").pending_question_text == ""  # type: ignore[union-attr]


def test_concept_lifecycle_changes_blocking_weight(tmp_path: Path):
    provider = RouterProvider([])
    agent, db = make_agent(tmp_path, provider)
    project = db.create_project(hotel="Harbour Grand Kobe", request="test")
    concept_id = db.insert_concept(
        project_id=project, hotel="Harbour Grand Kobe", name="Evening Sky Return",
        proposition="Return from Kobe each evening to the high-floor bath",
        narrative_device="repeat the same sunset return", status="generated",
    )

    generated = agent.search_concept_memory(
        "return each evening for a high-floor bath", hotel="Harbour Grand Kobe"
    )[0]
    assert generated["status"] == "generated"
    assert generated["can_block"] is False

    db.update_concept_status(concept_id, "published")
    published = agent.search_concept_memory(
        "return each evening for a high-floor bath", hotel="Harbour Grand Kobe"
    )[0]
    assert published["status"] == "published"
    assert published["weighted_signal"] > generated["weighted_signal"]
    assert published["can_block"] is True


def test_duplication_result_contains_required_evidence(tmp_path: Path):
    provider = RouterProvider([])
    agent, db = make_agent(tmp_path, provider)
    project = db.create_project(hotel="The Silveri Hong Kong", request="test")
    db.insert_concept(
        project_id=project, hotel="The Silveri Hong Kong", name="Threshold to the Island",
        proposition="Move from airport threshold to island calm",
        narrative_device="threshold sequence", status="approved",
    )
    match = agent.search_concept_memory("airport threshold sequence into calm")[0]
    assert match["previous_hotel"] == "The Silveri Hong Kong"
    assert match["previous_concept"] == "Threshold to the Island"
    assert match["status"] == "approved"
    assert match["specific_overlap"]
    assert "structural" in match["overlap_type"]
    assert match["similarity_reason"]


def test_tool_loop_stops_at_bound(tmp_path: Path):
    provider = RouterProvider(
        ['{"action":"META_QUESTION","intent":"meta"}'],
        conversational='{"type":"tool","tool":"load_existing_research","arguments":{}}',
    )
    agent, _db = make_agent(tmp_path, provider)
    with pytest.raises(AgentError, match="10 tool steps"):
        agent.handle_message("What are you doing?", user_id="U1", thread_key="C:3")


def test_cancel_updates_session_without_search(tmp_path: Path):
    provider = RouterProvider(['{"action":"CANCEL","intent":"cancel"}'])
    agent, db = make_agent(tmp_path, provider)
    db.upsert_session("C:stop", status="researching", current_property="Hotel X")
    outcome = agent.handle_message("Stop.", user_id="U1", thread_key="C:stop")
    assert outcome.action == "CANCEL"
    assert db.get_session("C:stop").status == "cancelled"  # type: ignore[union-attr]
    assert provider.search_calls == 0


def test_slack_plain_thread_reply_and_event_dedup(tmp_path: Path):
    db = Database(tmp_path / "slack.db")
    unrelated = {"channel": "C", "thread_ts": "9", "channel_type": "channel", "user": "U1"}
    assert accepts_slack_event(unrelated, mentioned=False, db=db) is False

    db.upsert_session("C:9", thread_ts="9", channel_id="C", user_id="U1", status="awaiting_clarification")
    assert accepts_slack_event(unrelated, mentioned=False, db=db) is True
    assert accepts_slack_event({**unrelated, "bot_id": "B1"}, mentioned=False, db=db) is False
    assert db.claim_slack_event("Ev123") is True
    assert db.claim_slack_event("Ev123") is False
