"""Unit tests: SQLite storage (temp db), dedup, concepts, aliases, history."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.storage import Database


def make_db() -> tuple[Database, Path]:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    path = Path(tmp.name)
    return Database(path), path


class TestProjectsAndSources:
    def test_project_lifecycle(self):
        db, _ = make_db()
        pid = db.create_project(hotel="Harbour Grand Kobe", request="test")
        db.complete_project(pid, city="Kobe", status="completed", research_snapshot="{}")
        rows = db.list_sources(pid)
        assert rows == []

    def test_source_dedup_by_normalized_url(self):
        db, _ = make_db()
        pid = db.create_project(hotel="H", request="r")
        first = db.upsert_source(
            project_id=pid, url="https://a.com/x?utm_source=n", url_normalized="https://a.com/x",
            title="First", source_type="official",
        )
        dup = db.upsert_source(
            project_id=pid, url="https://a.com/x", url_normalized="https://a.com/x",
            title="Duplicate", source_type="official",
        )
        assert first is not None
        assert dup is None
        assert len(db.list_sources(pid)) == 1

    def test_evidence_update(self):
        db, _ = make_db()
        pid = db.create_project(hotel="H", request="r")
        sid = db.upsert_source(project_id=pid, url="https://a.com", url_normalized="https://a.com")
        db.add_evidence_to_source(sid, "- [design] fact")
        row = db.list_sources(pid)[0]
        assert "fact" in row["evidence"]

    def test_orchestration_state_and_safe_trace_roundtrip(self):
        db, _ = make_db()
        pid = db.create_project(hotel="H", request="r")
        state = {
            "active_goal": "verify claims", "active_constraints": ["concept_mutation=forbidden"],
            "current_stage": "research_verification", "current_hypothesis": "Concept",
            "open_questions": ["Claim A"], "latest_user_instruction": "Do not change it",
        }
        db.update_project_state(pid, "orchestration", state)
        db.append_workflow_trace(
            project_id=pid, thread_key="C:1", goal="verify claims",
            step_id="research_verify_01", specialist="researcher",
            brief_summary="Verify Claim A", result_status="partial",
            evaluation={"reason_code": "UNRESOLVED_CLAIMS"},
            next_action="alternate_research", response_mode="RESEARCH_VERIFICATION",
        )
        assert db.project_state(pid)["orchestration"] == state
        rows = db.workflow_trace(project_id=pid)
        assert rows[0]["step_id"] == "research_verify_01"
        assert "UNRESOLVED_CLAIMS" in rows[0]["evaluation"]


class TestConcepts:
    def test_insert_and_fetch(self):
        db, _ = make_db()
        pid = db.create_project(hotel="Harbour Grand Kobe", request="r")
        db.insert_concept(
            project_id=pid, hotel="Harbour Grand Kobe", name="Vertical Kitchen",
            proposition="p", central_tension="t", territory="e",
            keywords=["vertical", "kitchen"], ranking="strongest",
        )
        same = db.concepts_for_hotel("harbour grand kobe")
        assert len(same) == 1
        assert same[0].name == "Vertical Kitchen"
        assert "vertical" in same[0].keywords
        line = same[0].as_summary_line()
        assert "Vertical Kitchen" in line and "proposition:" in line

    def test_recent_excludes_project(self):
        db, _ = make_db()
        p1 = db.create_project(hotel="A Hotel", request="r")
        p2 = db.create_project(hotel="B Hotel", request="r")
        db.insert_concept(project_id=p1, hotel="A Hotel", name="C1")
        db.insert_concept(project_id=p2, hotel="B Hotel", name="C2")
        recent = db.recent_concepts(exclude_project_id=p2)
        assert [c.name for c in recent] == ["C1"]


class TestAliases:
    def test_alias_roundtrip(self):
        db, _ = make_db()
        db.add_alias("HG Kobe", "Harbour Grand Kobe")
        assert db.resolve_alias("HG Kobe") == "Harbour Grand Kobe"
        assert db.resolve_alias("Ritz Madrid") == "Ritz Madrid"


class TestThreadHistory:
    def test_append_and_read(self):
        db, _ = make_db()
        db.append_thread_message("C:1", "user", "research X")
        db.append_thread_message("C:1", "assistant", "result")
        history = db.thread_history("C:1")
        assert [role for role, _ in history] == ["user", "assistant"]
        assert history[0][1] == "research X"

    def test_scoped_per_thread(self):
        db, _ = make_db()
        db.append_thread_message("C:1", "user", "one")
        db.append_thread_message("C:2", "user", "two")
        assert len(db.thread_history("C:1")) == 1

    def test_raw_history_is_bounded(self):
        db, _ = make_db()
        for i in range(30):
            db.append_thread_message("C:bounded", "user", str(i))
        history = db.thread_history("C:bounded", limit=100)
        assert len(history) == 20
        assert history[0][1] == "10"

    def test_latest_project_for_thread(self):
        db, _ = make_db()
        db.create_project(hotel="H", request="r", slack_thread_ts="C:9")
        row = db.latest_project_for_thread("C:9")
        assert row is not None and row["hotel"] == "H"
        assert db.latest_project_for_thread("C:none") is None
