"""Slack attachments: extraction, routing, ingestion into research, startup catch-up."""

import io
from pathlib import Path

import httpx
from pypdf import PdfWriter

from app.orchestration.handoffs import ResearchResult
from app.tools import documents as docs_mod
from app.tools.documents import Document, extract_text, fetch_documents, slim_file
from app.slack_app import catch_up_missed_messages
from tests.test_orchestrator import seed_project, setup_orchestrator
from tests.test_slack_wiring import message_event, router_fixture


def test_extract_text_handles_html_named_text_file_and_plain_text():
    html = b"<html><head><title>Minutes</title></head><body><h1>W Rome</h1><p>Rooftop opens May.</p></body></html>"
    assert "Rooftop opens May." in extract_text("2026-09-03_W_Rome_First_Meeting.html", "text/plain", html)
    assert extract_text("notes.txt", "text/plain", b"plain notes") == "plain notes"


def test_extract_text_reads_pdf_pages():
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    # A blank page has no text: extraction must return empty, not raise.
    assert extract_text("deck.pdf", "application/pdf", buffer.getvalue()) == ""


def test_fetch_reports_missing_files_read_scope(monkeypatch):
    def handler(request):
        assert request.headers["Authorization"] == "Bearer xoxb-test"
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>Sign in</html>")

    real_client = httpx.Client
    monkeypatch.setattr(docs_mod.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    file = slim_file({"id": "F1", "name": "deck.pdf", "mimetype": "application/pdf", "size": 10,
                      "url_private_download": "https://files.slack.com/F1", "permalink": "https://slack/F1"})
    [doc] = fetch_documents([file], "xoxb-test")
    assert not doc.ok and "files:read" in doc.error
    assert doc.url == "https://slack/F1"


def test_router_accepts_file_share_and_passes_files(tmp_path: Path):
    router, db, executor = router_fixture(tmp_path)
    event = message_event(
        ts="200.1", text="<@B1> deck attached", subtype="file_share",
        files=[{"id": "F1", "name": "deck.pdf", "url_private_download": "https://x/F1"}],
    )
    assert router.route(event, {}, mentioned=True, bot_user_id="B1") == "start_or_continue_agent_session"
    assert executor.jobs[0]["files"][0]["id"] == "F1"


def test_reference_documents_are_filed_and_acknowledged(tmp_path: Path):
    o, db, researcher, editorial, _d, _b, _c = setup_orchestrator(
        tmp_path, ['{"intent":"GENERAL_CONVERSATION"}']
    )
    pid = seed_project(db, thread="C:1")
    ingested = []

    def ingest(project_id, research, documents):
        ingested.append((project_id, [d.name for d in documents]))
        research.verified_facts.append({"fact": "Rooftop opens in May", "source_ids": ["S9"],
                                        "confidence": "verified_official"})
        return research

    researcher.ingest_documents = ingest
    documents = [
        Document("F1", "minutes.html", "https://slack/F1", text="Rooftop opens in May"),
        Document("F2", "deck.pdf", "https://slack/F2", error="no readable text (likely scanned or image-only)"),
    ]
    outcome = o.handle_message("deck attached for reference", user_id="U1", thread_key="C:1", documents=documents)

    assert ingested == [(pid, ["minutes.html"])]
    stored = ResearchResult.from_dict(db.project_state(pid)["research"])
    assert any(f["fact"] == "Rooftop opens in May" for f in stored.verified_facts)
    # A reference-only share is filed and acknowledged, without re-running editorial.
    assert not editorial.tasks
    assert outcome.markdown.startswith("Added minutes.html to the Harbour Grand Kobe research: 1 facts")
    assert "deck.pdf: no readable text" in outcome.markdown


def test_catch_up_routes_only_unhandled_messages(tmp_path: Path):
    router, db, executor = router_fixture(tmp_path)
    db.upsert_session("C1:100.1", thread_ts="100.1", channel_id="C1", user_id="U1", status="complete")
    db.claim_slack_event("message:C1:100.2")  # already handled before the bot went offline

    class Client:
        def conversations_history(self, **kwargs):
            return {"messages": []}

        def conversations_replies(self, **kwargs):
            return {"messages": [
                {"ts": "100.1", "thread_ts": "100.1", "user": "U1", "text": "<@B1> research W Rome"},
                {"ts": "100.2", "thread_ts": "100.1", "user": "U1", "text": "handled"},
                {"ts": "100.3", "thread_ts": "100.1", "bot_id": "BX", "user": "B1", "text": "bot reply"},
                {"ts": "100.4", "thread_ts": "100.1", "user": "U1", "text": "<@B1> deck attached",
                 "subtype": "file_share", "files": [{"id": "F1", "name": "deck.pdf"}]},
            ]}

    routed = catch_up_missed_messages(Client(), router, db, hours=48, bot_user_id="B1")
    assert routed == 1
    assert [job["message_ts"] for job in executor.jobs] == ["100.4"]


def test_image_only_pdf_is_sent_to_transcriber(monkeypatch):
    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)

    def handler(request):
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=buffer.getvalue())

    real_client = httpx.Client
    monkeypatch.setattr(docs_mod.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    calls = []

    def transcriber(path, *, page_count):
        calls.append(page_count)
        assert path.read_bytes()[:5] == b"%PDF-"
        return "[page 1]\nRooftop pool\nVisual: terrace"

    file = slim_file({"id": "F2", "name": "deck.pdf", "mimetype": "application/pdf", "size": 10,
                      "url_private_download": "https://files.slack.com/F2"})
    [doc] = fetch_documents([file], "xoxb-test", pdf_transcriber=transcriber)
    assert calls == [3]
    assert doc.ok and "Rooftop pool" in doc.text


def test_research_role_uses_research_model(tmp_path: Path):
    from app.config import load_settings
    from app.llm import build_provider

    settings = load_settings(tmp_path / "none.env", {
        "SLACK_BOT_TOKEN": "x", "SLACK_APP_TOKEN": "x", "LLM_PROVIDER": "claude_cli",
        "CLAUDE_CLI_MODEL": "claude-opus-5-5",
    })
    assert build_provider(settings, role="research")._model == "claude-sonnet-5-5"
    assert build_provider(settings)._model == "claude-opus-5-5"


def test_parallel_extraction_keeps_source_order(tmp_path: Path):
    import threading
    import time

    from app.agents.researcher import ResearchAgent
    from app.config import Settings
    from app.storage import Database
    from app.tools.pages import FetchedPage
    from app.tools.web import OFFICIAL, SearchHit

    settings = Settings(slack_bot_token="x", slack_app_token="x", database_path=tmp_path / "r.db")
    db = Database(settings.database_path)
    pid = db.create_project(hotel="W Rome", hotel_slug="w-rome", request="r", user_id="U1",
                            slack_channel="C", slack_thread_ts="C:1")
    agent = ResearchAgent(settings, provider=None, db=db)
    active, peak = [0], [0]
    lock = threading.Lock()

    def slow_extract(sid, source_type, page):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.05 if sid == "S1" else 0.01)  # S1 finishes last
        with lock:
            active[0] -= 1
        return [{"fact": f"fact from {sid}", "source_ids": [sid]}]

    agent._extract_page_evidence = slow_extract
    hits = [SearchHit(f"https://whotels.com/rome/{i}", f"W Rome {i}", "", OFFICIAL, grounded=True) for i in range(4)]
    pages = {h.url: FetchedPage(h.url, 200, "W Rome", "W Rome hotel page", "", (), (), (), True) for h in hits}
    evidence, _assets, records = agent._extract_and_persist(pid, hits, pages, hotel_name="W Rome")
    assert [r["sid"] for r in records] == ["S1", "S2", "S3", "S4"]
    assert evidence[0]["fact"] == "fact from S1"
    assert peak[0] > 1


def test_compact_research_keeps_newest_and_document_facts_within_budget():
    import json

    from app.orchestration.planning import compact_research_json

    old = [{"fact": f"old fact {i} " + "x" * 200, "source_ids": ["S1"], "confidence": "verified_official"}
           for i in range(400)]
    duplicate = {"fact": "old fact 0 " + "x" * 200, "source_ids": ["S1"], "confidence": "verified_official"}
    document = {"fact": "Giano is the Sicilian restaurant", "source_ids": ["S40"],
                "confidence": "verified_official", "evidence_level": "hotel_document"}
    newest = {"fact": "Rooftop pool reopens in May", "source_ids": ["S70"], "confidence": "verified_official"}
    research = {"verified_facts": old + [duplicate, document, newest],
                "source_records": [{"sid": "S1", "url": "u", "facts": old}]}

    payload = compact_research_json(research, budget_chars=20000)
    data = json.loads(payload)  # still valid JSON, never cut mid-string
    assert len(payload) <= 20000
    assert data["verified_facts"][0]["fact"] == "Giano is the Sicilian restaurant"
    assert data["verified_facts"][1]["fact"] == "Rooftop pool reopens in May"
    assert data["verified_facts_omitted"] > 0
    assert "facts" not in data["source_records"][0]
    texts = [f["fact"] for f in data["verified_facts"]]
    assert len(texts) == len(set(texts))


def test_layout_keeps_heading_with_photo_and_marks_tk(tmp_path: Path):
    from app.artifacts.layout import render_editorial_html, split_sections

    from PIL import Image

    photo = tmp_path / "garden.jpg"
    Image.new("RGB", (40, 30), "green").save(photo)
    body = "Intro line with [TK: opening hours].\n\n## The Garden\nFirst garden paragraph.\n\nSecond one."
    assert [s.heading for s in split_sections(body)] == ["", "The Garden"]
    page = render_editorial_html(
        hotel="W Rome", headline="H", standfirst="S", body=body,
        images={"img1": {"local_path": str(photo), "orientation": "landscape"}},
        plan={"hero": "img1", "sections": [{"section": 1, "image": "img1", "caption": "A garden."}]},
    )
    assert '<div class="lead"><h2>The Garden</h2><figure' in page
    assert "<p>First garden paragraph.</p></div><p>Second one.</p>" in page
    assert '<mark class="tk">[TK: opening hours]</mark>' in page
    # Photos are embedded so the single HTML file shows them when opened from Slack.
    assert page.count("data:image/jpeg;base64,") == 2
    assert "file://" not in page


def test_hero_candidates_prefer_large_landscape_photos():
    from app.orchestration.orchestrator import EditorialOrchestrator

    catalogue = {
        "img1": {"orientation": "landscape", "width": 600, "height": 400},
        "img2": {"orientation": "landscape", "width": 1800, "height": 1200},
        "img3": {"orientation": "portrait", "width": 1400, "height": 2400},
    }
    assert EditorialOrchestrator._hero_candidates(catalogue) == ["img2"]


def test_art_direction_notes_are_stripped_from_drafts():
    from app.artifacts.layout import split_sections, strip_direction_notes

    body = ("[IMAGE · full-bleed opener · p01_0.jpg · Two guests. [TK: room category]]\n\n"
            "Every morning begins at the door.\n\n[LAYOUT · route-line graphic · 2 min]\n\n## Day 1\nArrive.")
    cleaned = strip_direction_notes(body)
    assert "IMAGE" not in cleaned and "LAYOUT" not in cleaned
    assert cleaned.startswith("Every morning begins at the door.")
    assert [s.heading for s in split_sections(body)] == ["", "Day 1"]


def test_page_text_returns_one_page_without_visual_line():
    from app.tools.documents import page_text

    text = "[page 13]\nGiano\nSicilian cuisine\nVisual: walnut room\n\n[page 14]\nCitrico"
    assert page_text(text, 13) == "Giano Sicilian cuisine"


def test_layout_without_photos_has_slots_with_shot_briefs():
    from app.artifacts.layout import render_editorial_html

    body = ("Intro.\n\n## Day 1 · Arrival\nCheck in.\n\n## Day 2 · Slow day\nPool.\n\n"
            "## Insider notes\nBest time: dusk.")
    page = render_editorial_html(
        hotel="Park Hotel", headline="Above the city | Park Hotel", standfirst="S", body=body,
        images={}, plan={"hero_shot": "Tower at dusk", "shots": [{"section": 1, "shot": "Room with city view"}]},
    )
    assert 'class="cover plain"' in page and "Suggested: Tower at dusk" in page
    assert page.count('<figure class="slot inline" data-slot>') == 2  # the two days, not Insider notes
    assert "Suggested: Room with city view" in page
    assert "data-save" in page and "FileReader" in page
    assert "Photos supplied by the hotel." not in page


def test_a_shared_photo_file_becomes_a_hotel_supplied_layout_photo(tmp_path):
    import io
    import httpx
    from PIL import Image
    from app.tools.documents import _fetch_one, photo_caption_from_name

    buffer = io.BytesIO()
    Image.new("RGB", (2400, 1600), (120, 80, 40)).save(buffer, "TIFF")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=buffer.getvalue(),
                                                                    headers={"content-type": "image/tiff"}))
    file = {"id": "F7", "name": "GardenRestaurantSalotto1.tiff", "mimetype": "image/tiff", "size": 90_000_000,
            "url_private_download": "https://files.slack.com/F7", "permalink": "https://slack/F7"}
    with httpx.Client(transport=transport) as client:
        doc = _fetch_one(client, file, None, tmp_path)
    assert doc.kind == "photo" and doc.usable and not doc.ok
    [image] = doc.images
    assert max(image.width, image.height) == 1800 and image.caption == "Garden Restaurant Salotto"
    assert photo_caption_from_name("HOTEL EXTERIOR NIGHT.jpeg") == "HOTEL EXTERIOR NIGHT"

    with httpx.Client(transport=transport) as client:
        assert "larger than" in _fetch_one(client, {**file, "size": 300_000_000}, None, tmp_path).error
