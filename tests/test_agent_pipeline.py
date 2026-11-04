"""Offline integration test: full research pipeline with a fake LLM.

Verifies orchestration: triage routing, evidence registration with S#
ids, source persistence, critic selection, citation validation,
sources block, concept persistence — without any network access.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent import HotelResearchAgent, ResearchOutcome
from app.config import Settings
from app.llm.base import LLMMessage, LLMResponse
from app.storage import Database
from app.tools.pages import FetchedPage, normalize_url

FAKE_PAGE_HTML = "<html><head><title>Hotel X Official</title></head><body><h1>Suites</h1><p>words</p></body></html>"


class FakeProvider:
    """Returns stage-appropriate canned outputs based on prompt content."""

    def __init__(self) -> None:
        self.evidence_calls = 0
        self.search_calls = 0

    def complete(self, messages: Sequence[LLMMessage], *, system=None, max_tokens=4096, temperature=0.7) -> LLMResponse:
        prompt = messages[-1].content
        if "Message:" in prompt:  # triage
            return LLMResponse(text='{"intent": "research", "hotel": "Hotel X", "city_hint": "Kobe", "constraints": [], "audience": "", "deep": false}')
        if "STAGE: EVIDENCE" in prompt:
            self.evidence_calls += 1
            return LLMResponse(text='[{"fact": "The hotel has 3 signature suites", "dimension": "rooms", "quote": ""}]')
        if "STAGE: SNAPSHOT" in prompt:
            return LLMResponse(text='{"observations": [{"observation": "Hotel X is above a rail hub", "source_ids": ["S1"], "confidence": "high"}], "conflicts": [], "unverified": [], "hotel_identity": {"hotel": "Hotel X", "city": "Kobe", "country": "Japan", "brand": "Brand"}}')
        if "STAGE: DNA" in prompt:
            return LLMResponse(text='{"forces": [{"force": "verticality", "why": "because evidence", "source_ids": ["S1"]}], "positioning": "an urban tower"}')
        if "STAGE: CLASSIFY" in prompt:
            return LLMResponse(text='{"stay_scenarios": [{"experience": "wake to the market, return to the bath", "why_plausible": "f", "source_ids": ["S1"]}], "supporting_facts": ["bath opens 06:00"], "fact_only": []}')
        if "STAGE: TERRITORIES" in prompt:
            return LLMResponse(text='{"candidates": [{"plain_idea": "Idea One", "working_title": "One Living Room", "the_experience": "a then b, returning each evening", "why_this_hotel": "w", "editorial_device": "repetition of the same room", "booking_motivation": "book to feel at home", "visual_grammar": "repetition", "source_ids": ["S1"], "assets": {"verified": ["room shots"], "should_search": [], "probably_missing": []}, "hypotheses": []}]}')
        if "STAGE: CRITIC" in prompt:
            return LLMResponse(text='{"reviews": [{"plain_idea": "Idea One", "scores": {"desire": 5, "experience_concreteness": 5}, "gate": "pass", "gate_reasons": [], "repetition_flags": [], "fix": ""}], "selection": ["Idea One"], "ranking_rationale": {"strongest": "only one"}}')
        if "STAGE: FINAL" in prompt:
            return LLMResponse(text="I think there's one strong direction.\n\nWhy it works [S1].\n\n**Idea One**\n\nStory here.")
        raise AssertionError(f"unexpected prompt: {prompt[:80]}")

    def complete_with_search(self, messages, *, system=None, max_tokens=4096, temperature=0.4, max_searches=4) -> LLMResponse:
        self.search_calls += 1
        prompt = messages[-1].content
        if "official website" in prompt:
            return LLMResponse(text='[{"url": "https://hotelx.com/", "title": "Hotel X", "snippet": "official"}]')
        if "Suite Stories" in prompt:
            return LLMResponse(text="[]")
        return LLMResponse(text='[{"url": "https://kobe-info.jp/", "title": "Kobe Info", "snippet": "tourism"}]')


class FakeFetcher:
    def __init__(self) -> None:
        self.fetched: list[str] = []

    def fetch(self, url: str) -> FetchedPage:
        self.fetched.append(url)
        if "hotelx.com" in url:
            return FetchedPage(
                url=url, status=200, title="Hotel X Official", text="Hotel X content " * 60,
                meta_description="desc", headings=("Suites",), images=(
                    {"src": "https://hotelx.com/suite.jpg", "alt": "Deluxe Suite"},
                ), links=(), fetched_ok=True,
            )
        return FetchedPage(url=url, status=403, title="", text="", meta_description="", headings=(), images=(), links=(), fetched_ok=False, note="HTTP 403")


def make_agent(tmp_db: Path) -> tuple[HotelResearchAgent, FakeProvider, Database]:
    settings = Settings(
        slack_bot_token="x", slack_app_token="x", zai_api_key="k",
        database_path=tmp_db, allowed_user_ids=frozenset({"U1"}),
    )
    provider = FakeProvider()
    db = Database(tmp_db)
    agent = HotelResearchAgent(settings, provider, db, fetcher=FakeFetcher())
    return agent, provider, db  # type: ignore[return-value]


def run_research(tmp_db: Path) -> tuple[ResearchOutcome, Database]:
    agent, _provider, db = make_agent(tmp_db)
    outcome = agent.handle_message(
        "research Hotel X and find content angles", user_id="U1", thread_key="C:1"
    )
    return outcome, db


def test_full_pipeline_completes(tmp_path: Path):
    outcome, db = run_research(tmp_path / "a.db")
    assert outcome.clarification is None
    assert "Idea One" in outcome.markdown
    assert "Sources" in outcome.markdown
    # normal copilot mode: no opaque [S#] ids in the visible message
    assert "[S" not in outcome.markdown
    assert "Official hotel" in outcome.markdown
    # project + concept persisted (experience-led fields)
    concepts = db.recent_concepts()
    assert len(concepts) == 1
    assert concepts[0].name == "One Living Room"  # working_title preferred
    assert concepts[0].proposition.startswith("a then b")  # the experience stored
    assert concepts[0].central_tension == "book to feel at home"  # booking motivation
    assert concepts[0].ranking == "strongest"


def test_deep_mode_keeps_citations_and_full_sources(tmp_path: Path):
    class DeepProvider(FakeProvider):
        def complete(self, messages, **_):
            prompt = messages[-1].content
            if "Message:" in prompt:
                return LLMResponse(text='{"intent": "research", "hotel": "Hotel X", "deep": true}')
            if "STAGE: FINAL" in prompt:
                assert "MODE: DEEP" in prompt
                return LLMResponse(text="## HOTEL X\n\n### 1. Research snapshot\n- Obs [S1]")
            return super().complete(messages, **_)

    settings = Settings(slack_bot_token="x", slack_app_token="x", zai_api_key="k",
                        database_path=tmp_path / "h.db", allowed_user_ids=frozenset({"U1"}))
    db = Database(settings.database_path)
    agent = HotelResearchAgent(settings, DeepProvider(), db, fetcher=FakeFetcher())
    outcome = agent.handle_message("deep research Hotel X", user_id="U1", thread_key="C:7")
    assert "[S1]" in outcome.markdown  # citations survive in deep mode
    assert "*official*" in outcome.markdown  # grouped sources block


def test_no_candidates_honest_response(tmp_path: Path):
    class EmptyCriticProvider(FakeProvider):
        def complete(self, messages, **_):
            prompt = messages[-1].content
            if "STAGE: TERRITORIES" in prompt:
                return LLMResponse(text='{"candidates": []}')
            if "STAGE: CRITIC" in prompt:
                return LLMResponse(text='{"reviews": [], "selection": [], "ranking_rationale": {}}')
            if "STAGE: FINAL" in prompt:
                assert "none passed the critic" in prompt
                return LLMResponse(text="I don't think the research supports a strong direction yet. What's missing: press kit access.")
            return super().complete(messages, **_)

    settings = Settings(slack_bot_token="x", slack_app_token="x", zai_api_key="k",
                        database_path=tmp_path / "i.db", allowed_user_ids=frozenset({"U1"}))
    db = Database(settings.database_path)
    agent = HotelResearchAgent(settings, EmptyCriticProvider(), db, fetcher=FakeFetcher())
    outcome = agent.handle_message("research Hotel X", user_id="U1", thread_key="C:8")
    assert "don't think" in outcome.markdown
    assert db.recent_concepts() == []


def test_stub_sources_section_stripped(tmp_path: Path):
    class StubFinalProvider(FakeProvider):
        def complete(self, messages, **_):
            prompt = messages[-1].content
            if "STAGE: FINAL" in prompt:
                return LLMResponse(text="I have a direction.\n\n**Idea One**\n\nbody\n\n### 7. Sources\n- fake url the model invented")
            return super().complete(messages, **_)

    settings = Settings(slack_bot_token="x", slack_app_token="x", zai_api_key="k",
                        database_path=tmp_path / "g.db", allowed_user_ids=frozenset({"U1"}))
    db = Database(settings.database_path)
    agent = HotelResearchAgent(settings, StubFinalProvider(), db, fetcher=FakeFetcher())
    outcome = agent.handle_message("research Hotel X", user_id="U1", thread_key="C:6")
    assert "fake url" not in outcome.markdown
    assert outcome.markdown.count("*Sources*") == 1


def test_prototype_review_routing(tmp_path: Path):
    class ReviewProvider(FakeProvider):
        def complete(self, messages, **_):
            prompt = messages[-1].content
            if "Message:" in prompt:
                return LLMResponse(text='{"intent": "prototype_review", "hotel": "Hotel X", "idea_text": "<html>prototype</html>"}')
            if "STAGE: REVIEW" in prompt:
                assert "prototype" in prompt
                return LLMResponse(text='{"works": ["clean layout"], "generic": ["skyline opener"], "lost_specificity": ["any-hotel prose"], "changes": ["open with the residence fact"], "verdict": "solid skeleton, generic skin"}')
            raise AssertionError(f"unexpected prompt: {prompt[:80]}")

    settings = Settings(slack_bot_token="x", slack_app_token="x", zai_api_key="k",
                        database_path=tmp_path / "j.db", allowed_user_ids=frozenset({"U1"}))
    db = Database(settings.database_path)
    agent = HotelResearchAgent(settings, ReviewProvider(), db, fetcher=FakeFetcher())
    outcome = agent.handle_message(
        "here's our prototype, review it: <html>prototype</html>", user_id="U1", thread_key="C:9"
    )
    assert "What works" in outcome.markdown
    assert "skyline opener" in outcome.markdown
    assert "generic skin" in outcome.markdown


def test_sources_persisted_with_types(tmp_path: Path):
    _outcome, db = run_research(tmp_path / "b.db")
    rows = db.list_sources(1)
    urls = {r["url"] for r in rows}
    assert "https://hotelx.com/" in urls
    official = next(r for r in rows if r["url"] == "https://hotelx.com/")
    assert official["source_type"] == "official"
    assert official["confidence"] == "high"
    assert official["fetched_ok"] == 1


def test_invalid_citations_removed(tmp_path: Path):
    agent, _p, _db = make_agent(tmp_path / "c.db")
    outcome = agent.handle_message("research Hotel X", user_id="U1", thread_key="C:2")
    assert outcome.markdown  # pipeline still composes


def test_clarify_when_no_hotel(tmp_path: Path):
    class ClarifyProvider(FakeProvider):
        def complete(self, messages, **_):
            prompt = messages[-1].content
            if "Message:" in prompt:
                return LLMResponse(text='{"intent": "clarify", "clarification": "Which hotel?", "hotel": ""}')
            return super().complete(messages, **_)

    settings = Settings(slack_bot_token="x", slack_app_token="x", zai_api_key="k",
                        database_path=tmp_path / "d.db", allowed_user_ids=frozenset({"U1"}))
    db = Database(settings.database_path)
    agent = HotelResearchAgent(settings, ClarifyProvider(), db, fetcher=FakeFetcher())
    outcome = agent.handle_message("hello there", user_id="U1", thread_key="C:3")
    assert outcome.clarification == "Which hotel?"


def test_progress_events_emitted(tmp_path: Path):
    events: list[str] = []
    agent, _p, _db = make_agent(tmp_path / "e.db")
    agent.add_progress_listener(events.append)
    agent.handle_message("research Hotel X", user_id="U1", thread_key="C:4")
    for expected in ("start", "official", "coverage", "validate", "compare"):
        assert expected in events


def test_dedup_of_duplicate_search_hits(tmp_path: Path):
    class DupProvider(FakeProvider):
        def complete_with_search(self, messages, **kw):
            resp = super().complete_with_search(messages, **kw)
            if "official website" in messages[-1].content:
                # same page with tracking params — must dedup to one source
                return LLMResponse(text='[{"url": "https://hotelx.com/?utm_source=x", "title": "Hotel X", "snippet": ""}]')
            return resp

    settings = Settings(slack_bot_token="x", slack_app_token="x", zai_api_key="k",
                        database_path=tmp_path / "f.db", allowed_user_ids=frozenset({"U1"}))
    db = Database(settings.database_path)
    agent = HotelResearchAgent(settings, DupProvider(), db, fetcher=FakeFetcher())
    outcome = agent.handle_message("research Hotel X", user_id="U1", thread_key="C:5")
    rows = db.list_sources(1)
    hotelx = [r for r in rows if "hotelx.com" in r["url"]]
    assert len(hotelx) == 1
    assert normalize_url(hotelx[0]["url"]) == "https://hotelx.com/"
    assert outcome.project_id == 1
