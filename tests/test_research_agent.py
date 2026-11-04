"""ResearchAgent resilience tests."""

from pathlib import Path

from app.agents.researcher import ResearchAgent
from app.config import Settings
from app.orchestration.handoffs import ResearchTask
from app.storage import Database
from app.tools.pages import FetchedPage
from app.tools.web import BRAND, SearchHit


class UnusedProvider:
    def complete(self, *args, **kwargs):  # pragma: no cover - guard rail
        raise AssertionError("LLM should not be called by this extraction test")

    def complete_with_search(self, *args, **kwargs):  # pragma: no cover - guard rail
        raise AssertionError("search should not be called by this extraction test")


def test_search_snippet_survives_blocked_authoritative_page(tmp_path: Path):
    settings = Settings(
        slack_bot_token="x", slack_app_token="x", zai_api_key="x",
        database_path=tmp_path / "research.db", artifacts_path=tmp_path / "artifacts",
    )
    db = Database(settings.database_path)
    project_id = db.create_project(
        hotel="Harbour Grand Kobe", hotel_slug="harbour-grand-kobe",
        request="research", user_id="U1", slack_channel="C", slack_thread_ts="C:1",
    )
    agent = ResearchAgent(settings, UnusedProvider(), db)
    hit = SearchHit(
        "https://www.ihg.com/intercontinental/kobe", "Harbour Grand Kobe",
        "The hotel is directly connected to Grand Front Kobe.", BRAND, "medium", grounded=True,
    )
    blocked = FetchedPage(
        url=hit.url, status=403, title="", text="", meta_description="",
        headings=(), images=(), links=(), fetched_ok=False, note="HTTP 403",
    )

    evidence, assets, sources = agent._extract_and_persist(
        project_id, [hit], {hit.url: blocked}
    )

    assert not assets
    assert evidence == [{
        "fact": "The hotel is directly connected to Grand Front Kobe.",
        "dimension": "search_result_snippet",
        "quote": "",
            "source_ids": ["S1"],
            "evidence_level": "search_snippet",
            "confidence": "search_snippet_only",
        }]
    assert sources[0]["fetched_ok"] is False
    assert sources[0]["facts"] == evidence


def test_ungrounded_unfetchable_candidate_is_dropped(tmp_path: Path):
    # Regression: with web search quota exhausted, the model wrote Marriott URLs and
    # snippets from memory; 403/404 pages kept those snippets as evidence.
    settings = Settings(
        slack_bot_token="x", slack_app_token="x", zai_api_key="x",
        database_path=tmp_path / "research.db", artifacts_path=tmp_path / "artifacts",
    )
    db = Database(settings.database_path)
    project_id = db.create_project(
        hotel="W Rome", hotel_slug="w-rome",
        request="research", user_id="U1", slack_channel="C", slack_thread_ts="C:1",
    )
    agent = ResearchAgent(settings, UnusedProvider(), db)
    hit = SearchHit(
        "https://www.marriott.com/en-us/hotels/romxr-w-rome/overview/", "W Rome",
        "Official page with rooms and dining.", BRAND, "medium",
    )
    missing = FetchedPage(
        url=hit.url, status=404, title="", text="", meta_description="",
        headings=(), images=(), links=(), fetched_ok=False, note="HTTP 404",
    )

    evidence, _assets, sources = agent._extract_and_persist(project_id, [hit], {hit.url: missing})

    assert evidence == []
    assert sources == []
    assert db.list_sources(project_id) == []


def test_search_tool_error_is_recorded_and_hits_are_ungrounded():
    from app.llm.base import LLMResponse, SearchResult
    from app.tools.web import WebSearchTool

    class QuotaProvider:
        def complete_with_search(self, *args, **kwargs):
            return LLMResponse(
                text='[{"url":"https://www.marriott.com/x/","title":"W","snippet":"from memory"}]',
                search_error="Weekly/Monthly Limit Exhausted.",
            )

    tool = WebSearchTool(QuotaProvider())
    hits = tool.search("W Rome")
    assert tool.last_search_error == "Weekly/Monthly Limit Exhausted."
    assert [hit.grounded for hit in hits] == [False]

    class LiveProvider:
        def complete_with_search(self, *args, **kwargs):
            return LLMResponse(
                text='[{"url":"https://whotels.com/rome/","title":"W","snippet":"s"}]',
                search_results=(SearchResult("https://whotels.com/rome/", "W Rome", "real"),),
            )

    hits = WebSearchTool(LiveProvider()).search("W Rome")
    assert [hit.grounded for hit in hits] == [True]


def test_zai_search_tool_result_parsing():
    from app.llm.zai_glm import ZaiGlmProvider

    provider = ZaiGlmProvider("x")
    quota = provider._parse_response({"content": [
        {"type": "server_tool_use", "name": "web_search_prime", "input": {}},
        {"type": "tool_result", "content": "[{'text': 'MCP error -429: {\"error\":{\"code\":\"1310\",\"message\":\"Weekly/Monthly Limit Exhausted.\"}}', 'type': 'text'}]"},
        {"type": "text", "text": "From memory: https://www.marriott.com/"},
    ]})
    assert quota.search_error == "Weekly/Monthly Limit Exhausted."
    assert quota.search_results == ()

    live = provider._parse_response({"content": [
        {"type": "tool_result", "content": [
            {"type": "text", "text": '[{"title":"W Rome","link":"https://whotels.com/rome/","content":"Hotel"}]'}
        ]},
        {"type": "text", "text": "[]"},
    ]})
    assert live.search_error == ""
    assert live.search_results[0].url == "https://whotels.com/rome/"


class AssessmentProvider:
    def complete(self, *args, **kwargs):
        from app.llm.base import LLMResponse
        return LLMResponse(text='[{"status":"verified","finding":"Official page confirms it.","source_urls":["https://example.com/miru"]}]')

    def complete_with_search(self, *args, **kwargs):  # pragma: no cover - guard rail
        raise AssertionError("not used")


def test_claim_assessment_requires_cited_supplied_source(tmp_path: Path):
    settings = Settings(
        slack_bot_token="x", slack_app_token="x", zai_api_key="x",
        database_path=tmp_path / "assessment.db", artifacts_path=tmp_path / "artifacts",
    )
    agent = ResearchAgent(settings, AssessmentProvider(), Database(settings.database_path))
    results = agent._assess_followup_claims([{
        "claim": "Does MIRU serve dinner?",
        "facts": [{"fact": "Dinner is served."}],
        "sources": [{
            "url": "https://example.com/miru", "title": "Official MIRU",
            "source_type": "official", "fetched_ok": True,
        }],
    }])
    assert results == [{
        "claim": "Does MIRU serve dinner?",
        "status": "verified",
        "finding": "Official page confirms it.",
        "sources": [{
            "url": "https://example.com/miru", "title": "Official MIRU",
            "source_type": "official",
        }],
    }]


def test_verification_queries_are_specific_to_the_claim():
    assert "farm sourcing" in ResearchAgent._verification_query(
        "Harbour Grand Kobe", "Does MIRU operate a farm-sourcing programme?"
    )
    assert "opening hours" in ResearchAgent._verification_query(
        "Harbour Grand Kobe", "Does MIRU serve dinner nightly?"
    )
    assert "reservations" in ResearchAgent._verification_query(
        "Harbour Grand Kobe", "Are English-language reservations available?"
    )
    assert "hospitality press" in ResearchAgent._verification_query(
        "Harbour Grand Kobe", "Does editorial coverage already exist?"
    )


def test_verification_queries_never_leak_another_property():
    # Regression: queries for W Rome were rewritten into Harbour Grand Kobe's MIRU searches.
    for question in (
        "Fetch the official dining page: are Giano Restaurant and LINO current venues?",
        "Is there editorial review coverage of the WOL rooftop?",
        "Confirm 'first W Hotels in Italy' from the press release or verified sources.",
    ):
        query = ResearchAgent._verification_query("W Rome", question)
        assert query.startswith('"W Rome"')
        assert "MIRU" not in query
    assert "Giano" in ResearchAgent._verification_query(
        "W Rome", "Fetch the official dining page: are Giano Restaurant and LINO current venues?"
    )


def test_followup_search_uses_official_then_alternate_source_strategies(tmp_path: Path):
    settings = Settings(
        slack_bot_token="x", slack_app_token="x", zai_api_key="x",
        database_path=tmp_path / "fallback.db", artifacts_path=tmp_path / "artifacts",
    )
    agent = ResearchAgent(settings, UnusedProvider(), Database(settings.database_path))

    class SearchRecorder:
        def __init__(self):
            self.queries = []

        def search(self, query, **kwargs):
            self.queries.append(query)
            return [SearchHit("https://harbourgrandkobe.com/en/restaurant/miru-grill/", "MIRU", "Current menu")]

    recorder = SearchRecorder()
    agent._search = recorder
    base = {
        "source_records": [{
            "url": "https://harbourgrandkobe.com/en/", "source_type": "official", "fetched_ok": True,
        }]
    }
    task = ResearchTask(
        project_id=1, hotel_name="Harbour Grand Kobe", original_request="verify",
        existing_research=base, verification_round=1,
    )
    first = agent._followup_hits(task, "Does MIRU serve dinner nightly?")
    assert recorder.queries[0].startswith("site:harbourgrandkobe.com")
    assert first[0].source_type == "official"

    recorder.queries.clear()
    task.verification_round = 2
    agent._followup_hits(task, "Does MIRU serve dinner nightly?")
    assert "alternate-language" in recorder.queries[0]
    assert "reputable hospitality" in recorder.queries[1]


def test_followup_does_not_prioritize_unfetched_stale_official_host(tmp_path: Path):
    settings = Settings(
        slack_bot_token="x", slack_app_token="x", zai_api_key="x",
        database_path=tmp_path / "stale.db", artifacts_path=tmp_path / "artifacts",
    )
    agent = ResearchAgent(settings, UnusedProvider(), Database(settings.database_path))

    class SearchRecorder:
        def __init__(self):
            self.queries = []

        def search(self, query, **kwargs):
            self.queries.append(query)
            return []

    recorder = SearchRecorder()
    agent._search = recorder
    task = ResearchTask(
        project_id=1, hotel_name="Hotel X", original_request="verify",
        existing_research={"source_records": [{
            "url": "https://old.hotelx.example/", "source_type": "official", "fetched_ok": False,
        }]}, verification_round=1,
    )

    agent._followup_hits(task, "Does the restaurant serve dinner?")

    assert "Hotel X" in recorder.queries[0]
    assert "site:old.hotelx.example" not in recorder.queries[0]


def test_synthesis_cannot_upgrade_fact_without_valid_source_id():
    normalized = ResearchAgent._normalize_synthesized_evidence(
        [{"fact": "Unsupported promotion", "confidence": "verified_official", "source_ids": ["S99"]}],
        [{"fact": "Weak snippet", "confidence": "search_snippet_only", "source_ids": ["S1"]}],
    )
    assert normalized[0]["confidence"] == "inferred"


def test_fetched_property_domain_discovered_in_fallback_becomes_official(tmp_path: Path):
    settings = Settings(
        slack_bot_token="x", slack_app_token="x", zai_api_key="x",
        database_path=tmp_path / "official.db", artifacts_path=tmp_path / "artifacts",
    )
    db = Database(settings.database_path)
    pid = db.create_project(hotel="Harbour Grand Kobe", request="r")
    agent = ResearchAgent(settings, UnusedProvider(), db)
    hit = SearchHit("https://harbourgrandkobe.com/en/", "Harbour Grand Kobe", "", source_type="other")
    page = FetchedPage(
        url=hit.url, status=200, title="Harbour Grand Kobe",
        text="Harbour Grand Kobe " * 80, meta_description="Official hotel site",
        headings=("Harbour Grand Kobe",), images=(), links=(), fetched_ok=True,
    )
    agent._extract_page_evidence = lambda *args: [{
        "fact": "The hotel is in Kobe.", "source_ids": ["S1"]
    }]
    evidence, _assets, sources = agent._extract_and_persist(
        pid, [hit], {hit.url: page}, hotel_name="Harbour Grand Kobe"
    )
    assert sources[0]["source_type"] == "official"
    assert evidence[0]["confidence"] == "verified_official"


def test_fetched_lookalike_domain_is_rejected_as_official(tmp_path: Path):
    settings = Settings(
        slack_bot_token="x", slack_app_token="x", zai_api_key="x",
        database_path=tmp_path / "lookalike.db", artifacts_path=tmp_path / "artifacts",
    )
    db = Database(settings.database_path)
    pid = db.create_project(hotel="Harbour Grand Kobe", request="r")
    agent = ResearchAgent(settings, UnusedProvider(), db)
    hit = SearchHit("https://www.hg-kobe.com/", "Kobe company", "", source_type="official")
    page = FetchedPage(
        url=hit.url, status=200, title="Kobe International Commercial Co., Ltd.",
        text="International trading company in Kobe. " * 60, meta_description="Commercial company",
        headings=("Company",), images=(), links=(), fetched_ok=True,
    )
    agent._extract_page_evidence = lambda *args: (_ for _ in ()).throw(
        AssertionError("identity-mismatched page must not be extracted")
    )

    evidence, _assets, sources = agent._extract_and_persist(
        pid, [hit], {hit.url: page}, hotel_name="Harbour Grand Kobe"
    )

    assert evidence == []
    assert sources[0]["source_type"] == "other"
    assert sources[0]["facts"] == []


def test_property_fetch_validation_requires_matching_page_identity():
    bad_hit = SearchHit("https://hg-kobe.com/", "Candidate", "", source_type="official")
    good_hit = SearchHit("https://harbourgrandkobe.com/", "Candidate", "", source_type="official")
    bad_page = FetchedPage(
        url=bad_hit.url, status=200, title="Kobe International Commercial Co.",
        text="Trading company in Kobe " * 20, meta_description="", headings=(),
        images=(), links=(), fetched_ok=True,
    )
    good_page = FetchedPage(
        url=good_hit.url, status=200, title="Harbour Grand Kobe",
        text="Harbour Grand Kobe luxury hotel " * 20, meta_description="", headings=(),
        images=(), links=(), fetched_ok=True,
    )

    assert not ResearchAgent._has_valid_fetched_property_page(
        [bad_hit], {bad_hit.url: bad_page}, "Harbour Grand Kobe"
    )
    assert ResearchAgent._has_valid_fetched_property_page(
        [good_hit], {good_hit.url: good_page}, "Harbour Grand Kobe"
    )


def test_alternate_language_page_is_allowed_on_previously_validated_official_host(tmp_path: Path):
    settings = Settings(
        slack_bot_token="x", slack_app_token="x", zai_api_key="x",
        database_path=tmp_path / "alternate.db", artifacts_path=tmp_path / "artifacts",
    )
    db = Database(settings.database_path)
    pid = db.create_project(hotel="Harbour Grand Kobe", request="r")
    agent = ResearchAgent(settings, UnusedProvider(), db)
    hit = SearchHit("https://harbourgrandkobe.com/stay/", "客室のご案内", "", source_type="official")
    page = FetchedPage(
        url=hit.url, status=200, title="客室のご案内 - インターコンチネンタルホテル大阪",
        text="大阪の景色を望む客室です。" * 60, meta_description="客室", headings=(),
        images=(), links=(), fetched_ok=True,
    )
    agent._extract_page_evidence = lambda *args: [{
        "fact": "The page describes hotel rooms.", "source_ids": ["S1"]
    }]

    evidence, _assets, sources = agent._extract_and_persist(
        pid, [hit], {hit.url: page}, hotel_name="Harbour Grand Kobe",
        trusted_hosts={"harbourgrandkobe.com"},
    )

    assert sources[0]["source_type"] == "official"
    assert evidence[0]["confidence"] == "verified_official"


def test_destination_followup_skips_blocked_hosts_and_prefers_guides(tmp_path: Path):
    from app.tools.web import EDITORIAL_PRESS, OTHER

    settings = Settings(
        slack_bot_token="x", slack_app_token="x", zai_api_key="x",
        database_path=tmp_path / "research.db", artifacts_path=tmp_path / "artifacts",
    )
    agent = ResearchAgent(settings, UnusedProvider(), Database(settings.database_path))
    queries: list[str] = []

    class FakeSearch:
        def search(self, query, **_kwargs):
            queries.append(query)
            return [
                SearchHit("https://www.hyatt.com/hotel/area-attractions", "Area", "", BRAND, "medium", grounded=True),
                SearchHit("https://smallblog.example/eat", "Eat", "", OTHER, "low", grounded=True),
                SearchHit("https://guide.michelin.com/th/en/bangkok/restaurants", "Guide", "", OTHER, "low", grounded=True),
            ]

    agent._search = FakeSearch()
    task = ResearchTask(
        project_id=1, hotel_name="Hotel X", original_request="plan",
        followup_questions=["Restaurants near Hotel X"], destination=True,
        existing_research={"source_records": [
            {"url": "https://www.hyatt.com/hotel", "fetched_ok": False},
        ]},
    )

    hits = agent._followup_hits(task, "Restaurants near Hotel X")

    assert [h.url for h in hits] == [
        "https://guide.michelin.com/th/en/bangkok/restaurants", "https://smallblog.example/eat",
    ]
    assert hits[0].source_type == EDITORIAL_PRESS
    assert not any("official hotel brand page" in q for q in queries)


def test_experience_followup_tags_guest_reports_and_keeps_open_questions(tmp_path: Path):
    import json as _json
    from types import SimpleNamespace

    from app.tools.web import EDITORIAL_PRESS

    settings = Settings(
        slack_bot_token="x", slack_app_token="x", zai_api_key="x",
        database_path=tmp_path / "research.db", artifacts_path=tmp_path / "artifacts",
    )
    db = Database(settings.database_path)
    project_id = db.create_project(
        hotel="Hotel X", hotel_slug="hotel-x", request="r", user_id="U1", slack_channel="C", slack_thread_ts="C:1",
    )
    prompts: list[str] = []

    class Extractor:
        def complete(self, messages, system="", **_kwargs):
            prompts.append(system)
            return SimpleNamespace(text=_json.dumps([
                {"fact": "A guest review says the concierge rebooked an omakase at short notice.", "dimension": "service"},
            ]))

    agent = ResearchAgent(settings, Extractor(), db)

    class FakeSearch:
        def search(self, query, **_kwargs):
            return [SearchHit("https://press.example/hotel-x-review", "Review", "", EDITORIAL_PRESS, "medium", grounded=True)]

    agent._search = FakeSearch()
    page = FetchedPage(
        url="https://press.example/hotel-x-review", status=200, title="Hotel X review", text="We stayed at Hotel X.",
        meta_description="", headings=(), images=(), links=(), fetched_ok=True,
    )
    agent._fetch_hits = lambda hits: {page.url: page}
    task = ResearchTask(
        project_id=project_id, hotel_name="Hotel X", original_request="r", experience=True,
        followup_questions=["Guest reviews of a stay at Hotel X"],
        existing_research={"status": "complete", "critical_unknowns": ["Spa hours"], "source_records": []},
    )

    result = agent.run(task)

    assert result.critical_unknowns == ["Spa hours"]
    report = [f for f in result.verified_facts if f.get("evidence_level") == "guest_report"]
    assert report and report[0]["fact"].startswith("A guest review says")
    assert any("Guest experience extraction" in p for p in prompts)


def test_a_page_the_model_refuses_yields_no_facts_instead_of_failing(tmp_path: Path):
    from app.llm.base import LLMError

    settings = Settings(
        slack_bot_token="x", slack_app_token="x", zai_api_key="x",
        database_path=tmp_path / "research.db", artifacts_path=tmp_path / "artifacts",
    )

    class Refusing:
        def complete(self, *_args, **_kwargs):
            raise LLMError("claude CLI run failed: API Error: safeguards flagged this message")

    agent = ResearchAgent(settings, Refusing(), Database(settings.database_path))
    page = FetchedPage(
        url="https://example.com/page", status=200, title="t", text="text", meta_description="",
        headings=(), images=(), links=(), fetched_ok=True,
    )
    assert agent._extract_page_evidence("S1", "other", page) == []
    assert agent._extract_guest_experience("S1", page, "Hotel X") == []
