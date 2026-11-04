"""Per-hotel verified-fact cache: storage, reuse in new threads, freshness, privacy."""

import sqlite3
from pathlib import Path

from app.orchestration.handoffs import ResearchResult
from app.storage import Database
from tests.test_orchestrator import setup_orchestrator


def _research(n: int, *, private_doc: bool = False) -> dict:
    records = [
        {"sid": "S1", "url": "https://whotels.com/rome/", "title": "W Rome", "source_type": "official", "fetched_ok": True},
        {"sid": "S2", "url": "https://slack/F1", "title": "deck.pdf (shared in Slack)", "source_type": "official",
         "fetched_ok": True, "private": private_doc},
    ]
    facts = [{"fact": f"Fact number {i}", "source_ids": ["S1"], "confidence": "verified_official"} for i in range(n)]
    facts.append({"fact": "Snippet-only claim", "source_ids": ["S1"], "confidence": "search_snippet_only"})
    facts.append({"fact": "Deck fact", "source_ids": ["S2"], "confidence": "verified_official",
                  "evidence_level": "hotel_document"})
    return ResearchResult(status="complete", verified_facts=facts, source_records=records).to_dict()


def _project(db: Database, hotel: str = "W Rome") -> int:
    return db.create_project(hotel=hotel, hotel_slug="w-rome", request="r", user_id="U1",
                             slack_channel="C", slack_thread_ts="C:x")


def test_saving_research_caches_editorial_grade_facts_only(tmp_path: Path):
    db = Database(tmp_path / "c.db")
    pid = _project(db)
    db.update_project_state(pid, "research", _research(3, private_doc=True))
    rows = db.cached_hotel_facts("w rome", max_age_days=60)
    texts = {row["fact"] for row in rows}
    assert "Snippet-only claim" not in texts
    assert {"Fact number 0", "Deck fact"} <= texts
    assert next(r for r in rows if r["fact"] == "Deck fact")["private"] == 1


def test_reused_facts_are_not_re_dated(tmp_path: Path):
    db = Database(tmp_path / "c.db")
    pid = _project(db)
    db.update_project_state(pid, "research", _research(1))
    with db.transaction() as conn:
        conn.execute("UPDATE hotel_facts SET retrieved_at='2026-01-01 00:00:00'")
    reused = _research(1)
    for fact in reused["verified_facts"]:
        fact["cached_from"] = "2026-01-01 00:00:00"
    db.update_project_state(pid, "research", reused)
    assert db.cached_hotel_facts("W Rome", max_age_days=60) == []


def test_second_thread_for_a_hotel_reuses_the_cache(tmp_path: Path):
    o, db, researcher, editorial, *_ = setup_orchestrator(
        tmp_path, ['{"intent":"RESEARCH_PROPERTY","hotel":"W Rome"}'] * 3
    )
    seed = _project(db)
    db.update_project_state(seed, "research", _research(25, private_doc=True))
    stages = []
    o.add_progress_listener(stages.append)

    o.handle_message("Research W Rome.", user_id="U1", thread_key="C:new")
    assert researcher.tasks == []  # no initial web research
    research = editorial.tasks[0].research
    assert any(f.get("cached_from") for f in research.verified_facts)
    assert any(r.get("private") for r in research.source_records)  # GLM rule still applies
    assert any("reusing" in s for s in stages)

    o.handle_message("Research W Rome again from scratch.", user_id="U1", thread_key="C:fresh")
    assert len(researcher.tasks) == 1


def test_thin_cache_falls_back_to_research(tmp_path: Path):
    o, db, researcher, *_ = setup_orchestrator(tmp_path, ['{"intent":"RESEARCH_PROPERTY","hotel":"W Rome"}'])
    db.update_project_state(_project(db), "research", _research(5))
    o.handle_message("Research W Rome.", user_id="U1", thread_key="C:thin")
    assert len(researcher.tasks) >= 1


def test_upgrade_from_schema_4_backfills_the_cache(tmp_path: Path):
    path = tmp_path / "old.db"
    db = Database(path)
    pid = _project(db)
    db.update_project_state(pid, "research", _research(2))
    db.close()
    raw = sqlite3.connect(path)
    raw.execute("DELETE FROM hotel_facts")
    raw.execute("UPDATE meta SET value='4' WHERE key='schema_version'")
    raw.commit()
    raw.close()
    upgraded = Database(path)
    assert len(upgraded.cached_hotel_facts("W Rome", max_age_days=60)) == 3


def test_cache_keeps_property_facts_ahead_of_newer_destination_facts(tmp_path: Path):
    o, db, *_ = setup_orchestrator(tmp_path, [])
    pid = _project(db)
    db.update_project_state(pid, "research", _research(30))  # official W Rome page facts
    with db.transaction() as conn:
        conn.execute("UPDATE hotel_facts SET retrieved_at=datetime('now', '-5 days')")
    venues = ResearchResult(status="complete", source_records=[
        {"sid": "S1", "url": "https://example.com/rome-food", "title": "Rome food", "source_type": "destination"},
    ], verified_facts=[
        {"fact": f"Venue fact {i}", "source_ids": ["S1"], "confidence": "verified_authoritative_secondary"}
        for i in range(500)
    ]).to_dict()
    db.update_project_state(pid, "research", venues)

    research, _age = o._research_from_cache("W Rome")
    texts = [f["fact"] for f in research.verified_facts]
    assert len(texts) == 400
    property_facts = [t for t in texts if not t.startswith("Venue fact")]
    assert texts[: len(property_facts)] == property_facts  # all property facts come first
    assert {"Fact number 0", "Fact number 29", "Deck fact"} <= set(property_facts)


def test_cache_keeps_guest_reports_past_the_cut(tmp_path: Path):
    o, db, *_ = setup_orchestrator(tmp_path, [])
    pid = _project(db)
    db.update_project_state(pid, "research", _research(30))
    guests = ResearchResult(status="complete", source_records=[
        {"sid": "S1", "url": "https://blog.example/review", "title": "Review", "source_type": "other"},
    ], verified_facts=[
        {"fact": f"A guest review says detail {i}.", "source_ids": ["S1"],
         "confidence": "corroborated_secondary", "evidence_level": "guest_report"}
        for i in range(20)
    ]).to_dict()
    db.update_project_state(pid, "research", guests)
    with db.transaction() as conn:
        conn.execute("UPDATE hotel_facts SET retrieved_at=datetime('now', '-5 days')")
    venues = ResearchResult(status="complete", source_records=[
        {"sid": "S1", "url": "https://example.com/rome-food", "title": "Rome food", "source_type": "destination"},
    ], verified_facts=[
        {"fact": f"Venue fact {i}", "source_ids": ["S1"], "confidence": "verified_authoritative_secondary"}
        for i in range(500)
    ]).to_dict()
    db.update_project_state(pid, "research", venues)

    research, _age = o._research_from_cache("W Rome")
    reports = [f for f in research.verified_facts if f.get("evidence_level") == "guest_report"]
    assert len(reports) == 20
