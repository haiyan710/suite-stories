"""Integration-style tests for deterministic specialist handoffs."""

from __future__ import annotations

from pathlib import Path

from app.artifacts import ArtifactManager
from app.config import Settings
from app.llm.base import LLMResponse
from app.orchestration.handoffs import (
    CritiqueResult, DesignSpec, EditorialResult, ResearchResult,
)
from app.orchestration.orchestrator import EditorialOrchestrator
from app.storage import Database


class RouteProvider:
    def __init__(self, routes):
        self.routes = list(routes)
        self.calls = 0
        self.systems = []

    def complete(self, messages, *, system=None, **kwargs):
        self.calls += 1
        self.systems.append(system or "")
        if system and "# Suite Stories orchestrator" in system:
            return LLMResponse(text=self.routes.pop(0))
        return LLMResponse(text="Direct answer from saved project state.")

    def complete_with_search(self, messages, **kwargs):
        raise AssertionError("orchestrator provider must not browse")


class FakeResearcher:
    name = "researcher"

    def __init__(self):
        self.tasks = []
        self.progress = lambda _stage: None

    def set_progress_listener(self, callback):
        self.progress = callback

    def run(self, task):
        self.tasks.append(task)
        version = int(task.existing_research.get("version", 0) or 0) + 1 if task.existing_research else 1
        verification_claims = []
        if task.followup_questions:
            statuses = ("verified", "unsupported", "uncertain", "verified")
            verification_claims = [{
                "claim": claim,
                "status": statuses[index % len(statuses)],
                "finding": f"Finding for claim {index + 1}",
                "sources": ([{
                    "url": f"https://example.com/source-{index + 1}",
                    "title": f"Source {index + 1}",
                    "source_type": "official",
                }] if statuses[index % len(statuses)] != "uncertain" else []),
            } for index, claim in enumerate(task.followup_questions)]
        return ResearchResult(
            status="complete", property_identity={"hotel": task.hotel_name},
            verified_facts=[{"fact": "Verified fact", "source_ids": ["S1"], "confidence": "verified_official"}],
            experience_signals=[{"signal": "A repeatable guest rhythm", "source_ids": ["S1"], "confidence": "verified_official"}],
            available_assets=[{"url": "https://example.com/room.jpg", "bucket": "room"}],
            source_records=[{"sid": "S1", "url": "https://example.com"}],
            verification_claims=verification_claims,
            version=version,
        )


class FakeEditorial:
    name = "editorial"

    def __init__(self, results=None):
        self.tasks = []
        self.results = list(results or [])

    def run(self, task):
        self.tasks.append(task)
        if self.results:
            return self.results.pop(0)
        return EditorialResult(
            status="complete", strongest_experience="Use one calm room as the recurring return point.",
            why_someone_would_want_it="The trip changes without daily repacking.",
            why_this_property_enables_it="Verified access and room evidence support it.",
            editorial_proposition="A changing city, one stable return",
            possible_story_structure="alternate destination movement with the same-room return",
            visual_storytelling_device="repeat the same room frame",
            working_title="The Return Point", version=1,
        )


class FakeDesigner:
    name = "designer"

    def __init__(self):
        self.tasks = []

    def run(self, task):
        self.tasks.append(task)
        return DesignSpec(
            editorial_objective="Show change against constancy",
            page_narrative=["depart", "change", "return"],
            visual_grammar="repeat one room frame between destination chapters",
            section_structure=[{"id": "hero"}], image_roles=[],
            typography_direction="restrained editorial serif",
            spacing_and_rhythm="long pauses", interaction_direction="minimal",
            responsive_notes="preserve repeated frame", asset_mapping=[],
            things_to_avoid=["card grids"], builder_instructions=["use semantic sections"],
        )


class FakeCritic:
    name = "critic"

    def __init__(self, results=None):
        self.tasks = []
        self.results = list(results or [])

    def run(self, task):
        self.tasks.append(task)
        if self.results:
            return self.results.pop(0)
        return CritiqueResult(verdict="PASS", reasons=["Specific and demonstrable"])


class FakeBuilder:
    name = "builder"

    def __init__(self, manager):
        self.tasks = []
        self.manager = manager

    def run(self, task):
        self.tasks.append(task)
        return self.manager.create_html(
            project_id=task.project_id, hotel_slug=task.hotel_slug,
            html='<html><head><link rel="stylesheet" href="./styles.css"></head><body>Mockup</body></html>',
            css="body{font-family:serif}", js="",
            source_design_version=task.design_spec.version,
        )


def setup_orchestrator(tmp_path: Path, routes, editorial_results=None, critic_results=None):
    settings = Settings(
        slack_bot_token="x", slack_app_token="x", zai_api_key="x",
        database_path=tmp_path / "orchestrator.db",
        artifacts_path=tmp_path / "artifacts",
        allowed_user_ids=frozenset({"U1"}),
    )
    db = Database(settings.database_path)
    provider = RouteProvider(routes)
    researcher = FakeResearcher()
    editorial = FakeEditorial(editorial_results)
    designer = FakeDesigner()
    critic = FakeCritic(critic_results)
    manager = ArtifactManager(settings.artifacts_path, db)
    builder = FakeBuilder(manager)
    orchestrator = EditorialOrchestrator(
        settings, provider, db, researcher=researcher, editorial=editorial,
        designer=designer, builder=builder, critic=critic, artifacts=manager,
    )
    return orchestrator, db, researcher, editorial, designer, builder, critic


def seed_project(db: Database, *, thread="C:1", with_design=False):
    pid = db.create_project(
        hotel="Harbour Grand Kobe", hotel_slug="harbour-grand-kobe",
        request="research", user_id="U1", slack_channel="C", slack_thread_ts=thread,
    )
    research = ResearchResult(
        status="complete", property_identity={"hotel": "Harbour Grand Kobe"},
        verified_facts=[{"fact": "fact"}], available_assets=[],
    )
    editorial = EditorialResult(
        status="complete", strongest_experience="experience",
        why_someone_would_want_it="desire", why_this_property_enables_it="specificity",
        editorial_proposition="proposition", possible_story_structure="structure",
        visual_storytelling_device="device", working_title="Concept",
    )
    db.update_project_state(pid, "research", research.to_dict())
    db.update_project_state(pid, "concept", {
        "status": "approved", "version": 1, "editorial": editorial.to_dict(),
    })
    if with_design:
        spec = FakeDesigner().run(type("T", (), {})())
        spec.status = "approved"
        spec.version = db.save_design_version(pid, spec.to_dict(), status="approved")
        db.update_project_state(pid, "design", {
            "status": "approved", "version": spec.version, "spec": spec.to_dict(),
        })
    db.upsert_session(
        thread, thread_ts=thread.split(":")[-1], channel_id="C", user_id="U1",
        status="complete", current_property="Harbour Grand Kobe",
        research_project_id=pid,
    )
    return pid


def test_1_meta_question_calls_no_specialist(tmp_path: Path):
    o, _db, researcher, editorial, designer, builder, critic = setup_orchestrator(
        tmp_path, ['{"intent":"META_QUESTION"}']
    )
    outcome = o.handle_message("Explain your role.", user_id="U1", thread_key="C:1")
    assert "hotel editorial orchestrator" in outcome.markdown
    assert "hotel project" in outcome.markdown
    assert not any(word in outcome.markdown.lower() for word in (
        "watches", "jewelry", "fashion", "collectibles", "acquisition",
    ))
    assert o.last_trace == []
    assert o._provider.calls == 1  # intent classification only; role copy is deterministic
    assert not researcher.tasks and not editorial.tasks and not designer.tasks and not builder.tasks and not critic.tasks


def test_state_question_routes_to_concise_project_status(tmp_path: Path):
    o, _db, researcher, editorial, designer, builder, critic = setup_orchestrator(
        tmp_path, ['{"intent":"META_QUESTION"}']
    )
    outcome = o.handle_message(
        "What state are you currently holding in this thread?",
        user_id="U1", thread_key="C:status",
    )
    assert outcome.markdown == "There is no active project in this thread."
    assert outcome.action == "GET_PROJECT_STATUS"
    assert o.last_trace == []
    assert not researcher.tasks and not editorial.tasks and not designer.tasks and not builder.tasks and not critic.tasks


def test_2_research_path_is_research_editorial_critic_only(tmp_path: Path):
    o, _db, _r, _e, designer, builder, _c = setup_orchestrator(
        tmp_path, ['{"intent":"RESEARCH_PROPERTY","hotel":"Harbour Grand Kobe"}']
    )
    outcome = o.handle_message(
        "Research Harbour Grand Kobe and find an experience.", user_id="U1",
        thread_key="C:2", channel_id="C", thread_ts="2",
    )
    assert outcome.project_id
    assert o.last_trace == ["researcher", "editorial", "critic_editorial"]
    assert not designer.tasks and not builder.tasks


def test_3_followup_uses_state_without_research(tmp_path: Path):
    o, db, researcher, editorial, designer, builder, critic = setup_orchestrator(
        tmp_path, ['{"intent":"GENERAL_CONVERSATION"}']
    )
    seed_project(db, thread="C:3")
    outcome = o.handle_message("Why did you choose that?", user_id="U1", thread_key="C:3")
    assert "Direct answer" in outcome.markdown
    assert o.last_trace == []
    assert not researcher.tasks and not editorial.tasks and not designer.tasks and not builder.tasks and not critic.tasks


def test_4_develop_concept_reuses_research(tmp_path: Path):
    o, db, researcher, editorial, _d, _b, _c = setup_orchestrator(
        tmp_path, ['{"intent":"DEVELOP_CONCEPT"}']
    )
    seed_project(db, thread="C:4")
    o.handle_message("Develop this further.", user_id="U1", thread_key="C:4")
    assert o.last_trace == ["editorial", "critic_editorial"]
    assert not researcher.tasks
    assert editorial.tasks[0].current_concept


def test_verify_outstanding_claims_is_research_only_and_preserves_concept(tmp_path: Path):
    o, db, researcher, editorial, designer, builder, critic = setup_orchestrator(
        tmp_path, ['{"intent":"CONTINUE_RESEARCH"}']
    )
    pid = seed_project(db, thread="C:verify")
    state = db.project_state(pid)
    concept_before = state["concept"]
    concept_before["editorial"]["open_questions"] = [
        "Does MIRU operate a farm-sourcing programme?",
        "Does MIRU serve dinner nightly?",
        "Are English menus and reservations available?",
        "What editorial coverage of MIRU already exists?",
    ]
    db.update_project_state(pid, "concept", concept_before)
    concept_before = db.project_state(pid)["concept"]

    outcome = o.handle_message(
        "Verify all four outstanding claims using authoritative sources. Do not change the concept.",
        user_id="U1", thread_key="C:verify",
    )

    assert outcome.action == "VERIFY_RESEARCH_GAPS"
    assert o.last_trace == ["researcher", "researcher"]
    assert researcher.tasks[0].followup_questions == concept_before["editorial"]["open_questions"]
    assert db.project_state(pid)["concept"] == concept_before
    assert "Verified facts" in outcome.markdown
    assert "Unsupported claims" in outcome.markdown
    assert "Still uncertain" in outcome.markdown
    assert "concept has not been changed" in outcome.markdown
    assert "**Verified facts**\n\n•" in outcome.markdown
    assert not editorial.tasks and not designer.tasks and not builder.tasks and not critic.tasks


def test_5_html_mockup_calls_design_builder_visual_critic(tmp_path: Path):
    o, db, researcher, _e, designer, builder, critic = setup_orchestrator(
        tmp_path, ['{"intent":"CREATE_HTML_MOCKUP"}']
    )
    pid = seed_project(db, thread="C:5")
    outcome = o.handle_message("Make me a mockup.", user_id="U1", thread_key="C:5")
    assert o.last_trace == ["designer", "builder", "critic_visual"]
    assert not researcher.tasks
    assert designer.tasks and builder.tasks and critic.tasks[-1].mode == "VISUAL"
    assert Path(outcome.artifact_path, "index.html").exists()
    assert len(db.list_artifacts(pid)) == 1


def test_6_mockup_revision_creates_new_version_without_research(tmp_path: Path):
    o, db, researcher, _e, _d, _b, _c = setup_orchestrator(
        tmp_path, [
            '{"intent":"CREATE_HTML_MOCKUP"}',
            '{"intent":"REVISE_MOCKUP"}',
        ]
    )
    pid = seed_project(db, thread="C:6")
    o.handle_message("Make me a mockup.", user_id="U1", thread_key="C:6")
    outcome = o.handle_message("Too AI-looking. Make it editorial.", user_id="U1", thread_key="C:6")
    assert outcome.artifact_path.endswith("v002")
    assert not researcher.tasks
    assert [row["version"] for row in db.list_artifacts(pid)] == [1, 2]


def test_7_editorial_research_request_is_bounded_and_resumes(tmp_path: Path):
    needs = EditorialResult(status="needs_research", research_questions=["Verify guest access conditions"])
    complete = EditorialResult(
        status="complete", strongest_experience="experience", why_someone_would_want_it="desire",
        why_this_property_enables_it="specific", editorial_proposition="prop",
        possible_story_structure="structure", visual_storytelling_device="device",
    )
    o, _db, researcher, editorial, _d, _b, _c = setup_orchestrator(
        tmp_path, ['{"intent":"RESEARCH_PROPERTY","hotel":"Hotel X"}'], [needs, complete]
    )
    o.handle_message("Research Hotel X.", user_id="U1", thread_key="C:7")
    assert o.last_trace == ["researcher", "editorial", "researcher", "editorial", "critic_editorial"]
    assert len(researcher.tasks) == 2
    assert researcher.tasks[1].followup_questions == ["Verify guest access conditions"]
    assert len(editorial.tasks) == 2
    assert "materially different hypothesis" in editorial.tasks[1].constraints[-1]
    assert editorial.tasks[1].brief is not None
    assert "Do not use search_snippet_only" in editorial.tasks[1].brief.prohibited_actions[0]


def test_10_artifact_history_restore_preserves_later_version(tmp_path: Path):
    o, db, _r, _e, _d, _b, _c = setup_orchestrator(
        tmp_path, [
            '{"intent":"CREATE_HTML_MOCKUP"}',
            '{"intent":"REVISE_MOCKUP"}',
            '{"intent":"LOAD_PREVIOUS_PROJECT","target_version":1}',
        ]
    )
    pid = seed_project(db, thread="C:10")
    o.handle_message("Create mockup", user_id="U1", thread_key="C:10")
    o.handle_message("Revise mockup", user_id="U1", thread_key="C:10")
    outcome = o.handle_message("Use v1 again", user_id="U1", thread_key="C:10")
    assert "v1 is current" in outcome.markdown
    rows = db.list_artifacts(pid)
    assert len(rows) == 2
    assert db.current_artifact(pid)["version"] == 1


def test_user_facing_editorial_hides_internal_source_ids():
    editorial = EditorialResult(
        status="complete", strongest_experience="A real experience [S1].",
        why_someone_would_want_it="A reason [S2, S4].",
        why_this_property_enables_it="A property fact [S3].",
        editorial_proposition="Proposition", possible_story_structure="Structure",
        visual_storytelling_device="Device [S1]", working_title="Title",
        open_questions=["Verify detail [S9]"],
    )
    research = ResearchResult(
        status="complete", source_records=[{
            "url": "https://example.com", "title": "Friendly source",
            "source_type": "official", "fetched_ok": True,
        }],
    )
    text = EditorialOrchestrator._format_editorial(
        editorial, CritiqueResult(verdict="PASS", reasons=[]), "approved", research
    )
    assert "[S" not in text
    assert "https://example.com" in text


def test_pass_with_revisions_discloses_required_changes():
    editorial = EditorialResult(
        status="complete", strongest_experience="A vertical hotel evening.",
        why_someone_would_want_it="A complete night without transit.",
        why_this_property_enables_it="The venues are stacked in one tower.",
        editorial_proposition="One tower, one night",
        possible_story_structure="dinner, drinks, recovery",
        visual_storytelling_device="floor and hour captions",
        working_title="The Vertical Night",
    )
    critique = CritiqueResult(
        verdict="PASS_WITH_REVISIONS", reasons=["Core experience is verified."],
        revision_instructions=["Remove the exact 23:00 bath time until opening hours are verified."],
    )

    text = EditorialOrchestrator._format_editorial(
        editorial, critique, "shortlisted", ResearchResult(status="complete")
    )

    assert "Required revisions before development" in text
    assert "Remove the exact 23:00 bath time" in text


def test_editorial_sources_prefer_supporting_and_fetched_records():
    editorial = EditorialResult(
        status="complete", strongest_experience="Experience", working_title="Title",
        supporting_evidence_ids=["S3"],
    )
    research = ResearchResult(status="complete", source_records=[
        {"sid": "S1", "url": "https://stale.example", "title": "Stale", "source_type": "official", "fetched_ok": False},
        {"sid": "S2", "url": "https://fetched.example", "title": "Fetched", "source_type": "official", "fetched_ok": True},
        {"sid": "S3", "url": "https://support.example", "title": "Supporting", "source_type": "official", "fetched_ok": True},
    ])

    text = EditorialOrchestrator._format_editorial(
        editorial, CritiqueResult(verdict="PASS", reasons=[]), "shortlisted", research
    )

    assert "https://support.example" in text
    assert "https://fetched.example" not in text
    assert "https://stale.example" not in text


def test_artifact_payload_includes_deterministic_structure_checks(tmp_path: Path):
    directory = tmp_path / "v001"
    directory.mkdir()
    (directory / "index.html").write_text(
        '<!doctype html><html><body><section id="story"></section>'
        '<a href="#story">Story</a></body></html>', encoding="utf-8",
    )
    payload = EditorialOrchestrator._artifact_payload({"path": str(directory)})
    checks = payload["structural_validation"]
    assert checks["has_doctype"] is True
    assert checks["has_closing_html"] is True
    assert checks["dangling_fragment_targets"] == []


def test_regression_a_verification_locks_and_preserves_concept(tmp_path: Path):
    o, db, researcher, editorial, designer, builder, critic = setup_orchestrator(
        tmp_path, ['{"intent":"VERIFY_RESEARCH_GAPS"}']
    )
    pid = seed_project(db, thread="C:A")
    state = db.project_state(pid)
    state["concept"]["editorial"]["open_questions"] = [f"Claim {i}" for i in range(1, 5)]
    db.update_project_state(pid, "concept", state["concept"])
    before = db.project_state(pid)["concept"]

    outcome = o.handle_message(
        "Verify all four outstanding claims. Do not change the concept yet.",
        user_id="U1", thread_key="C:A",
    )

    assert all(name == "researcher" for name in o.last_trace)
    assert not editorial.tasks and not designer.tasks and not builder.tasks and not critic.tasks
    assert db.project_state(pid)["concept"] == before
    assert "Verified facts" in outcome.markdown
    assert "Unsupported claims" in outcome.markdown
    assert "Still uncertain" in outcome.markdown
    assert "Not evaluated because you asked me not to change the concept" in outcome.markdown
    orchestration = db.project_state(pid)["orchestration"]
    assert "editorial_agent=forbidden" in orchestration["active_constraints"]
    assert orchestration["task_status"] == "complete"


def test_regression_b_unsupported_central_premise_cannot_be_shortlisted(tmp_path: Path):
    bad = EditorialResult(
        status="complete", strongest_experience="Eat a farm-sourced Kansai dinner nightly.",
        why_someone_would_want_it="A culinary journey.",
        why_this_property_enables_it="MIRU allegedly sources from farms.",
        editorial_proposition="The farm comes to the table.",
        possible_story_structure="farm to plate", visual_storytelling_device="farm and dish pairs",
        working_title="Farm Nights", supporting_evidence_ids=["S9"],
        evidence_dependencies=[{"claim": "MIRU farm sourcing", "confidence": "search_snippet_only"}],
    )
    o, db, _r, _e, _d, _b, _c = setup_orchestrator(
        tmp_path, ['{"intent":"FIND_EDITORIAL_OPPORTUNITY"}'], [bad, bad]
    )
    pid = seed_project(db, thread="C:B")

    outcome = o.handle_message("Find the strongest experience.", user_id="U1", thread_key="C:B")

    assert outcome.project_id == pid
    assert db.project_state(pid)["concept"]["status"] == "rejected"
    assert "evidence-supported" in outcome.markdown
    statuses = [row["status"] for row in db._conn.execute(
        "SELECT status FROM concepts WHERE project_id=?", (pid,)
    ).fetchall()]
    assert "shortlisted" not in statuses


def test_publication_stage_dependency_note_does_not_force_central_evidence_failure():
    editorial = EditorialResult(
        status="complete", strongest_experience="A verified nightly bath ritual.",
        supporting_evidence_ids=["S1"],
        evidence_dependencies=[
            "Bath hours and photography permission are not in current evidence and must be confirmed before publication.",
            "An award is deliberately NOT used as a central claim.",
        ],
    )
    research = ResearchResult(status="complete", verified_facts=[{
        "fact": "The hotel has a Japanese-style bath.",
        "source_ids": ["S1"], "confidence": "verified_official",
    }])

    assert not EditorialOrchestrator._editorial_has_low_confidence_dependency(
        editorial, research
    )


def test_inferred_signal_cannot_downgrade_verified_fact_for_same_source_id():
    editorial = EditorialResult(
        status="complete", strongest_experience="A verified listening-room stay.",
        supporting_evidence_ids=["S8", "S11"],
    )
    research = ResearchResult(
        status="complete",
        verified_facts=[
            {"fact": "The collaboration room is offered one room per day.",
             "source_ids": ["S8"], "confidence": "verified_official"},
            {"fact": "The hotel has five dining venues.",
             "source_ids": ["S11"], "confidence": "verified_official"},
        ],
        experience_signals=[
            {"signal": "The room may create a listening ritual.",
             "source_ids": ["S8"], "confidence": "inferred"},
            {"signal": "The venues may support a full hotel day.",
             "source_ids": ["S11"], "confidence": "inferred"},
        ],
    )

    assert not EditorialOrchestrator._editorial_has_low_confidence_dependency(
        editorial, research
    )


def test_regression_c_material_unknown_triggers_targeted_research_before_editorial(tmp_path: Path):
    o, db, researcher, editorial, _d, _b, _c = setup_orchestrator(
        tmp_path, ['{"intent":"RESEARCH_PROPERTY","hotel":"Hotel X"}']
    )
    original_run = researcher.run

    def run_with_transport_gap(task):
        result = original_run(task)
        if not task.followup_questions:
            result.critical_unknowns = ["Verify the exact station connection and realistic transfer pattern."]
        else:
            result.critical_unknowns = []
        return result

    researcher.run = run_with_transport_gap
    o.handle_message("Research Hotel X and find one experience.", user_id="U1", thread_key="C:C")

    assert o.last_trace[:3] == ["researcher", "researcher", "editorial"]
    assert researcher.tasks[1].followup_questions == [
        "Verify the exact station connection and realistic transfer pattern."
    ]
    assert researcher.tasks[1].brief is not None
    assert "Verify the specified evidence gaps only" in researcher.tasks[1].brief.mission


def test_targeted_research_budget_is_shared_across_research_and_editorial_loops(tmp_path: Path):
    failures = [
        CritiqueResult(
            verdict="FAIL", fail_reason="EVIDENCE_DEPENDENCY",
            reasons=["Needs stronger evidence."], unresolved_concerns=["Verify the transport claim."],
        ),
        CritiqueResult(
            verdict="FAIL", fail_reason="EVIDENCE_DEPENDENCY",
            reasons=["Still unsupported."], unresolved_concerns=["Verify the transport claim."],
        ),
    ]
    o, _db, researcher, _editorial, _d, _b, _c = setup_orchestrator(
        tmp_path, ['{"intent":"RESEARCH_PROPERTY","hotel":"Hotel X"}'],
        critic_results=failures,
    )
    original_run = researcher.run

    def always_unresolved(task):
        result = original_run(task)
        result.critical_unknowns = ["Verify the transport claim."]
        return result

    researcher.run = always_unresolved
    o.handle_message("Research Hotel X.", user_id="U1", thread_key="C:budget")
    assert len(researcher.tasks) == 3  # one broad pass + two targeted passes, never a third
    assert o._targeted_research_rounds_used == 2


def test_pre_editorial_verification_reserves_a_round_for_editorial_questions(tmp_path: Path):
    # W Rome regression: generic unknowns stayed unresolved, the pre-editorial loop spent
    # the whole budget on them, and Editorial's specific questions were never researched.
    needs = EditorialResult(
        status="needs_research", research_questions=["Fetch the official dining page."]
    )
    complete = EditorialResult(
        status="complete", strongest_experience="experience", why_someone_would_want_it="desire",
        why_this_property_enables_it="specific", editorial_proposition="prop",
        possible_story_structure="structure", visual_storytelling_device="device",
    )
    o, _db, researcher, editorial, _d, _b, _c = setup_orchestrator(
        tmp_path, ['{"intent":"RESEARCH_PROPERTY","hotel":"Hotel X"}'], [needs, complete]
    )
    original_run = researcher.run

    def always_unresolved(task):
        result = original_run(task)
        result.critical_unknowns = ["Property's exact location within the city."]
        return result

    researcher.run = always_unresolved
    o.handle_message("Research Hotel X.", user_id="U1", thread_key="C:reserve")
    assert any(
        task.followup_questions == ["Fetch the official dining page."] for task in researcher.tasks
    )
    assert len(editorial.tasks) == 2
    assert o._targeted_research_rounds_used == 2


def test_evidence_gap_failure_lists_gaps_as_bullets(tmp_path: Path):
    needs = EditorialResult(
        status="needs_research",
        research_questions=["Verify the pool season.", "Verify the rooftop bar hours."],
    )
    o, _db, _researcher, _editorial, _d, _b, _c = setup_orchestrator(
        tmp_path, ['{"intent":"RESEARCH_PROPERTY","hotel":"Hotel X"}'], [needs, needs, needs]
    )
    outcome = o.handle_message("Research Hotel X.", user_id="U1", thread_key="C:gaps")
    assert "• Verify the pool season." in outcome.markdown
    assert "• Verify the rooftop bar hours." in outcome.markdown
    assert "; " not in outcome.markdown


def test_incomplete_editorial_output_is_retried_before_critic(tmp_path: Path):
    incomplete = EditorialResult(
        status="complete", strongest_experience="Two city halves from one hotel.",
        why_someone_would_want_it="",
        why_this_property_enables_it="The hotel sits above the station.",
        editorial_proposition="Two cities, one room",
        possible_story_structure="descend and return",
        visual_storytelling_device="horizontal and vertical pairs",
    )
    complete = EditorialResult(
        status="complete", strongest_experience="A listening-room stay.",
        why_someone_would_want_it="A scarce cultural stay worth booking now.",
        why_this_property_enables_it="The verified collaboration room exists only here.",
        editorial_proposition="The hotel room becomes the listening event.",
        possible_story_structure="morning, dusk, and final nightly listen",
        visual_storytelling_device="repeat the listening position across the day",
    )
    o, db, _researcher, editorial, _d, _b, critic = setup_orchestrator(
        tmp_path,
        ['{"intent":"RESEARCH_PROPERTY","hotel":"Harbour Grand Kobe"}'],
        [incomplete, complete],
    )

    outcome = o.handle_message(
        "Research Harbour Grand Kobe and find one stay experience.",
        user_id="U1", thread_key="C:contract",
    )

    assert len(editorial.tasks) == 2
    assert len(critic.tasks) == 1
    assert "structurally incomplete" in editorial.tasks[1].constraints[0]
    assert editorial.tasks[1].current_concept["strongest_experience"] == incomplete.strongest_experience
    assert "Repair the supplied incomplete candidate" in editorial.tasks[1].brief.explicit_instructions[-1]
    assert db.project_state(outcome.project_id)["concept"]["status"] == "shortlisted"


def test_final_incomplete_candidate_after_research_wait_is_repaired(tmp_path: Path):
    # W Rome regression: attempt 1 asked for research, attempt 2 was a strong candidate
    # missing one field, and it was discarded because no attempts were left to repair it.
    needs = EditorialResult(status="needs_research", research_questions=["Fetch the dining page."])
    incomplete = EditorialResult(
        status="complete", strongest_experience="A curated tasting circuit.",
        why_someone_would_want_it="",
        why_this_property_enables_it="The brand publishes its own city taste guide.",
        editorial_proposition="Stay inside the tastemaker's city.",
        possible_story_structure="three days, three venues, one return",
        visual_storytelling_device="facade and interior pair",
    )
    repaired = EditorialResult(**{**incomplete.to_dict(), "why_someone_would_want_it": "Taste without research."})
    o, db, _researcher, editorial, _d, _b, critic = setup_orchestrator(
        tmp_path, ['{"intent":"RESEARCH_PROPERTY","hotel":"Hotel X"}'],
        [needs, incomplete, repaired],
    )
    outcome = o.handle_message("Research Hotel X.", user_id="U1", thread_key="C:final-repair")

    assert len(editorial.tasks) == 3
    assert "structurally incomplete" in editorial.tasks[2].constraints[0]
    assert editorial.tasks[2].current_concept["strongest_experience"] == incomplete.strongest_experience
    assert len(critic.tasks) == 1
    assert db.project_state(outcome.project_id)["concept"]["status"] == "shortlisted"


def test_two_incomplete_editorial_attempts_report_missing_field_not_evidence_gaps(tmp_path: Path):
    incomplete = EditorialResult(
        status="complete", strongest_experience="A seasonal dining rhythm.",
        why_someone_would_want_it="",
        why_this_property_enables_it="Five venues run concurrent seasonal menus.",
        editorial_proposition="Autumn served throughout one hotel.",
        possible_story_structure="morning through night",
        visual_storytelling_device="one ingredient across five tables",
        research_questions=["Confirm 2027 dates", "Recheck an unrelated award"],
    )
    o, _db, _researcher, editorial, _d, _b, critic = setup_orchestrator(
        tmp_path,
        ['{"intent":"RESEARCH_PROPERTY","hotel":"Hotel X"}'],
        [incomplete, incomplete],
    )

    outcome = o.handle_message(
        "Research Hotel X and find one stay experience.",
        user_id="U1", thread_key="C:incomplete-twice",
    )

    assert len(editorial.tasks) == 2
    assert not critic.tasks
    assert "why someone would want it" in outcome.markdown
    assert "2027" not in outcome.markdown
    assert "Remaining evidence gaps" not in outcome.markdown


def test_unverified_central_critic_match_forces_evidence_failure(tmp_path: Path):
    candidate = EditorialResult(
        status="complete", strongest_experience="Sleep inside an artist's work.",
        why_someone_would_want_it="It is a scarce cultural night.",
        why_this_property_enables_it="The hotel lists a limited collaboration plan.",
        editorial_proposition="The room becomes the artwork.",
        possible_story_structure="night, late evening, dawn",
        visual_storytelling_device="the same room at three hours",
        working_title="Inside the Work",
    )
    false_pass = CritiqueResult(
        verdict="PASS_WITH_REVISIONS", reasons=["The plan itself is verified."],
        evidence_matches=[{
            "claim": "The guest sleeps inside the artist's work.",
            "status": "unverified", "central": True,
        }],
    )
    o, db, _researcher, _editorial, _d, _b, critic = setup_orchestrator(
        tmp_path,
        ['{"intent":"RESEARCH_PROPERTY","hotel":"Hotel X"}',
         '{"intent":"GENERAL_CONVERSATION"}'],
        [candidate, candidate], [false_pass, false_pass],
    )

    outcome = o.handle_message(
        "Research Hotel X and find one stay experience.",
        user_id="U1", thread_key="C:central-gap",
    )

    assert len(critic.tasks) == 2
    assert "Almost there: Inside the Work" in outcome.markdown
    assert "Confirm with the hotel to unlock it" in outcome.markdown
    assert "sleeps inside the artist's work" in outcome.markdown
    assert "Hotel confirmed:" in outcome.markdown
    assert db.project_state(outcome.project_id)["concept"]["status"] == "rejected"
    workflow = o.handle_message("Show workflow", user_id="U1", thread_key="C:central-gap")
    assert "attempt 1/2" in workflow.markdown
    assert "attempt 2/2" in workflow.markdown


def test_revisable_central_detail_does_not_override_critic_pass(tmp_path: Path):
    candidate = EditorialResult(
        status="complete", strongest_experience="A vertical evening within one hotel.",
        why_someone_would_want_it="Dinner, drinks, and recovery require no transit.",
        why_this_property_enables_it="The verified venues occupy different floors.",
        editorial_proposition="One tower serves the whole night.",
        possible_story_structure="check-in, dinner, nightcap, recovery",
        visual_storytelling_device="floor and hour captions",
        working_title="The Vertical Night",
    )
    pass_with_revision = CritiqueResult(
        verdict="PASS_WITH_REVISIONS", reasons=["The core venue sequence is verified."],
        evidence_matches=[{
            "claim": "The bath is open at exactly 23:00.",
            "status": "unverified", "central": True, "impact": "revisable",
        }],
        revision_instructions=["Remove the exact hour until opening times are verified."],
    )
    revised = EditorialResult(
        status="complete", strongest_experience="A vertical evening within one hotel.",
        why_someone_would_want_it="Dinner, drinks, and recovery require no transit.",
        why_this_property_enables_it="The verified venues occupy different floors.",
        editorial_proposition="One tower serves the whole night.",
        possible_story_structure="check-in, dinner, nightcap, recovery when operating hours allow",
        visual_storytelling_device="floor captions without unverified times",
        working_title="The Vertical Night",
    )
    o, db, _researcher, editorial, _d, _b, critic = setup_orchestrator(
        tmp_path,
        ['{"intent":"RESEARCH_PROPERTY","hotel":"Hotel X"}'],
        [candidate, revised], [pass_with_revision, CritiqueResult(verdict="PASS", reasons=["Revisions applied."])],
    )

    outcome = o.handle_message(
        "Research Hotel X and find one stay experience.",
        user_id="U1", thread_key="C:revisable-gap",
    )

    assert len(editorial.tasks) == 2
    assert len(critic.tasks) == 2
    assert editorial.tasks[1].current_concept["working_title"] == "The Vertical Night"
    assert "Critic required revisions" in editorial.tasks[1].constraints[0]
    assert "Critic: PASS" in outcome.markdown
    assert db.project_state(outcome.project_id)["concept"]["status"] == "shortlisted"


def test_test_thread_concepts_stay_out_of_concept_memory(tmp_path: Path):
    candidate = EditorialResult(
        status="complete", strongest_experience="A vertical evening within one hotel.",
        why_someone_would_want_it="Dinner, drinks, and recovery require no transit.",
        why_this_property_enables_it="The verified venues occupy different floors.",
        editorial_proposition="One tower serves the whole night.",
        possible_story_structure="check-in, dinner, nightcap, recovery",
        visual_storytelling_device="floor captions",
        working_title="The Vertical Night",
    )
    o, db, _researcher, _editorial, _d, _b, _critic = setup_orchestrator(
        tmp_path,
        ['{"intent":"RESEARCH_PROPERTY","hotel":"Hotel X"}'],
        [candidate], [CritiqueResult(verdict="PASS", reasons=["Verified."])],
    )

    outcome = o.handle_message(
        "[test] Research Hotel X and find one stay experience.",
        user_id="U1", thread_key="C:test-thread",
    )

    assert "not saved to concept memory" in outcome.markdown
    assert db.project_state(outcome.project_id)["concept"]["status"] == "shortlisted"
    assert db.recent_concepts() == []
    assert db.project_is_test(outcome.project_id)
    concept_id = db.latest_concept_id(outcome.project_id)
    db.update_concept_status(concept_id, "approved")
    assert db.recent_concepts() == []


def test_default_trip_length_makes_a_hotel_recommendation_itinerary(tmp_path: Path):
    from dataclasses import replace

    candidate = EditorialResult(
        status="complete", strongest_experience="Three nights based at the hotel.",
        why_someone_would_want_it="First-time visitors who want a calm base.",
        why_this_property_enables_it="Verified rooms and restaurant.",
        editorial_proposition="Book it for the base, not the bar.",
        possible_story_structure="Day 1: arrive. Day 2: city. Day 3: slow. Day 4: depart.",
        visual_storytelling_device="one map per day", working_title="Three Nights",
    )
    o, db, _researcher, editorial, _d, _b, critic = setup_orchestrator(
        tmp_path, ['{"intent":"RESEARCH_PROPERTY","hotel":"Hotel X"}'],
        [candidate], [CritiqueResult(verdict="PASS", reasons=["Verified."])],
    )
    o._settings = replace(o._settings, default_trip_nights=3)

    outcome = o.handle_message("Research Hotel X.", user_id="U1", thread_key="C:default-trip")

    brief = editorial.tasks[0].brief.to_prompt()
    assert "hotel recommendation with a 3-night (4-day) itinerary" in brief
    assert "FORMAT: hotel recommendation with a 3-night itinerary" in critic.tasks[0].brief.to_prompt()
    assert db.project_state(outcome.project_id)["concept"]["format"] == {"type": "itinerary", "nights": 3}


def test_pasted_hotel_reply_becomes_a_hotel_document():
    reply = EditorialOrchestrator._pasted_hotel_reply(
        "<@UBOT> Hotel confirmed: art tours run every Saturday for all guests, free of charge.", "C:1",
    )
    assert reply is not None and reply.ok
    assert reply.text.startswith("art tours run every Saturday")
    assert EditorialOrchestrator._pasted_hotel_reply("khách sạn xác nhận: tour nghệ thuật vẫn chạy hằng tuần", "C:1")
    assert EditorialOrchestrator._pasted_hotel_reply("Research the hotel tours", "C:1") is None


def test_broken_revision_falls_back_to_the_candidate_the_critic_passed(tmp_path: Path):
    candidate = EditorialResult(
        status="complete", strongest_experience="A vertical evening within one hotel.",
        why_someone_would_want_it="Dinner, drinks, and recovery require no transit.",
        why_this_property_enables_it="The verified venues occupy different floors.",
        editorial_proposition="One tower serves the whole night.",
        possible_story_structure="check-in, dinner, nightcap, recovery",
        visual_storytelling_device="floor captions", working_title="The Vertical Night",
    )
    needs_fixes = CritiqueResult(
        verdict="PASS_WITH_REVISIONS", reasons=["Core verified."],
        revision_instructions=["Drop the exact bar hour."],
    )
    o, db, _researcher, editorial, _d, _b, critic = setup_orchestrator(
        tmp_path, ['{"intent":"RESEARCH_PROPERTY","hotel":"Hotel X"}'],
        [candidate, EditorialResult(status="failed", open_questions=["Editorial response was not structured."])],
        [needs_fixes],
    )

    outcome = o.handle_message("Research Hotel X.", user_id="U1", thread_key="C:broken-revision")

    assert len(editorial.tasks) == 2 and len(critic.tasks) == 1
    concept = db.project_state(outcome.project_id)["concept"]
    assert concept["status"] == "shortlisted"
    assert concept["editorial"]["working_title"] == "The Vertical Night"
    assert concept["critic_result"]["revision_instructions"] == ["Drop the exact bar hour."]


def test_dining_picks_keep_only_restaurants_named_in_cited_facts(tmp_path: Path):
    import json
    from types import SimpleNamespace

    o, *_ = setup_orchestrator(tmp_path, [])
    o._trip_nights = 3
    seen: list[list[dict]] = []
    research = ResearchResult(status="complete", verified_facts=[
        {"fact": "Nusara, a Thai fine dining restaurant in the Old Town, holds one Michelin star.",
         "source_ids": ["S1"], "confidence": "verified_authoritative_secondary"},
        {"fact": "Potong in Chinatown ranked 13 on the 50 Best list.",
         "source_ids": ["S2"], "confidence": "verified_authoritative_secondary"},
        {"fact": "Some blog says a cafe is nice.", "source_ids": ["S3"], "confidence": "search_snippet_only"},
    ])

    class Picker:
        def complete(self, messages, **_kwargs):
            seen.append(json.loads(messages[0].content)["facts"])
            return SimpleNamespace(text=json.dumps({"picks": [
                {"name": "Nusara", "area": "Old Town", "why": "One Michelin star.", "caveat": "", "fact_ids": ["F1"]},
                {"name": "Invented Bistro", "area": "", "why": "", "caveat": "", "fact_ids": ["F2"]},
            ]}))

    o._fast_provider = Picker()
    picks = o._dining_picks("Hotel X", research)

    assert len(seen[0]) == 2  # the snippet-only fact never reaches the picker
    assert [p["name"] for p in picks] == ["Nusara"]
    note = o._format_dining_picks(picks)
    assert "DINNER PICKS" in note and "Nusara (Old Town)" in note


def test_experience_research_runs_once_per_project(tmp_path: Path):
    from dataclasses import replace

    o, db, researcher, _editorial, _d, _b, _critic = setup_orchestrator(
        tmp_path, ['{"intent":"RESEARCH_PROPERTY","hotel":"Hotel X"}'],
    )
    o._settings = replace(o._settings, experience_research=True)

    o.handle_message("Research Hotel X.", user_id="U1", thread_key="C:experience")

    experience = [t for t in researcher.tasks if t.experience]
    assert len(experience) == 1
    assert any("Insider tips" in q for q in experience[0].followup_questions)
    stored = ResearchResult(status="complete", verified_facts=[{"fact": "A guest review says the concierge rebooked dinner.",
                                                                 "evidence_level": "guest_report"}])
    assert not o._needs_experience_research(stored)
    assert EditorialOrchestrator._step_label("researcher", "research_experience").startswith("Research: guest reviews")


def test_traveller_segment_is_parsed_and_carried_into_the_plan(tmp_path: Path):
    from dataclasses import replace

    parse = EditorialOrchestrator._parse_segment
    assert parse("Plan 3 nights at Hotel X for a honeymoon couple") == "couples"
    assert parse("lịch trình cho gia đình có trẻ em") == "families"
    assert parse("a golf trip") == "golf"
    assert parse("Research Hotel X") == ""
    assert "TRAVELLER: plan this trip for couples and honeymooners" in (
        EditorialOrchestrator._itinerary_instruction(3, "couples")
    )
    assert "pick the one traveller type" in EditorialOrchestrator._itinerary_instruction(3)

    candidate = EditorialResult(
        status="complete", strongest_experience="Three nights based at the hotel.",
        why_someone_would_want_it="Honeymooners who want a quiet suite.",
        why_this_property_enables_it="Verified suites and spa.",
        editorial_proposition="Book it for the suite and the spa.",
        possible_story_structure="Day 1: arrive. Day 2: spa. Day 3: city. Day 4: depart.",
        visual_storytelling_device="one photo per day", working_title="Slow Honeymoon",
    )
    o, db, _researcher, editorial, _d, _b, critic = setup_orchestrator(
        tmp_path, ['{"intent":"RESEARCH_PROPERTY","hotel":"Hotel X"}'],
        [candidate], [CritiqueResult(verdict="PASS", reasons=["Verified."])],
    )
    o._settings = replace(o._settings, default_trip_nights=3)

    outcome = o.handle_message("Research Hotel X for a honeymoon couple.", user_id="U1", thread_key="C:segment")

    assert "for couples and honeymooners" in editorial.tasks[0].brief.to_prompt()
    assert "itinerary for couples and honeymooners" in critic.tasks[0].brief.to_prompt()
    piece = db.project_state(outcome.project_id)["concept"]["format"]
    assert piece["type"] == "itinerary" and piece["nights"] == 3
    assert piece["segment"] == "couples and honeymooners"
    assert "romantic" in piece["segment_voice"] and "60% informative" in piece["tone_mix"]
    assert "romantic" in editorial.tasks[0].brief.to_prompt()


def test_central_evidence_blockers_respect_impact():
    def blockers(*matches):
        return EditorialOrchestrator._central_evidence_blockers(CritiqueResult(
            verdict="PASS_WITH_REVISIONS", reasons=[], evidence_matches=list(matches),
        ))

    corrected = {"claim": "Fits any group size", "status": "contradicted", "central": True, "impact": "revisable"}
    missing = {"claim": "The suite exists", "status": "contradicted", "central": True, "impact": "existential"}
    legacy = {"claim": "No impact given", "status": "contradicted", "central": True}
    side = {"claim": "Side detail", "status": "contradicted", "central": False}
    assert blockers(corrected, side) == []
    assert blockers(missing) == ["The suite exists"]
    assert blockers(legacy) == ["No impact given"]


def test_regression_d_conceptual_failure_requests_different_hypothesis(tmp_path: Path):
    weak = EditorialResult(
        status="complete", strongest_experience="Eat at the Michelin restaurant.",
        why_someone_would_want_it="It is Michelin-starred.",
        why_this_property_enables_it="The hotel has the restaurant.",
        editorial_proposition="Michelin dining", possible_story_structure="restaurant tour",
        visual_storytelling_device="dish gallery", working_title="Michelin Stay",
    )
    strong = EditorialResult(
        status="complete", strongest_experience="Use one base for changing destination days.",
        why_someone_would_want_it="Explore without repacking.",
        why_this_property_enables_it="Verified transport and recovery facilities support the rhythm.",
        editorial_proposition="Many days, one return", possible_story_structure="depart and return",
        visual_storytelling_device="changing landscapes and one repeated base", working_title="One Return",
    )
    critiques = [
        CritiqueResult(verdict="FAIL", fail_reason="CONCEPTUAL_WEAKNESS", reasons=["Amenity dressed as a concept."]),
        CritiqueResult(verdict="PASS", reasons=["Tangible and property-specific."]),
    ]
    o, db, _r, editorial, _d, _b, _c = setup_orchestrator(
        tmp_path, ['{"intent":"FIND_EDITORIAL_OPPORTUNITY"}'], [weak, strong], critiques
    )
    pid = seed_project(db, thread="C:D")

    outcome = o.handle_message("Find a better experience.", user_id="U1", thread_key="C:D")

    assert len(editorial.tasks) == 2
    assert "materially different hypothesis" in editorial.tasks[1].constraints[0]
    assert "One Return" in outcome.markdown
    assert db.project_state(pid)["concept"]["status"] == "shortlisted"


def test_regression_e_strong_concept_is_shortlisted(tmp_path: Path):
    o, db, _r, _e, _d, _b, _c = setup_orchestrator(
        tmp_path, ['{"intent":"FIND_EDITORIAL_OPPORTUNITY"}']
    )
    pid = seed_project(db, thread="C:E")
    outcome = o.handle_message("Find the strongest experience.", user_id="U1", thread_key="C:E")
    assert db.project_state(pid)["concept"]["status"] == "shortlisted"
    assert "Critic: PASS" in outcome.markdown


def test_regression_f_uncertainty_explanation_preserves_stage(tmp_path: Path):
    o, db, researcher, editorial, designer, builder, critic = setup_orchestrator(
        tmp_path, ['{"intent":"GENERAL_CONVERSATION"}']
    )
    pid = seed_project(db, thread="C:F")
    state = db.project_state(pid)
    state["research"]["uncertainties"] = ["The official page could not be retrieved."]
    db.update_project_state(pid, "research", state["research"])
    before = db.project_state(pid)["concept"]

    outcome = o.handle_message("Why is this evidence uncertain?", user_id="U1", thread_key="C:F")

    assert "Direct answer" in outcome.markdown
    assert o.last_trace == []
    assert db.project_state(pid)["concept"] == before
    assert not researcher.tasks and not editorial.tasks and not designer.tasks and not builder.tasks and not critic.tasks


def test_regression_g_design_brief_uses_selected_proposition(tmp_path: Path):
    o, db, researcher, _e, designer, _b, _c = setup_orchestrator(
        tmp_path, ['{"intent":"CREATE_VISUAL_DIRECTION"}']
    )
    pid = seed_project(db, thread="C:G")
    outcome = o.handle_message("Create the visual direction.", user_id="U1", thread_key="C:G")
    assert outcome.project_id == pid
    assert o.last_trace == ["designer"]
    assert designer.tasks[0].concept["editorial_proposition"] == "proposition"
    assert designer.tasks[0].brief is not None
    assert "Selected proposition: proposition" in designer.tasks[0].brief.current_context
    assert not researcher.tasks


def test_show_workflow_returns_safe_delegation_summary(tmp_path: Path):
    o, db, _r, _e, _d, _b, _c = setup_orchestrator(
        tmp_path, [
            '{"intent":"FIND_EDITORIAL_OPPORTUNITY"}',
            '{"intent":"GENERAL_CONVERSATION"}',
        ]
    )
    seed_project(db, thread="C:workflow")
    o.handle_message("Find an experience.", user_id="U1", thread_key="C:workflow")
    outcome = o.handle_message("show workflow", user_id="U1", thread_key="C:workflow")
    assert "EditorialAgent" in outcome.markdown
    assert "CriticAgent" in outcome.markdown
    assert outcome.markdown.count("EditorialAgent —") == 1
    assert outcome.markdown.count("CriticAgent —") == 1
    assert "chain-of-thought" not in outcome.markdown.lower()


def test_search_outage_is_disclosed_in_reply(tmp_path: Path):
    o, _db, researcher, _e, _d, _b, _c = setup_orchestrator(
        tmp_path, ['{"intent":"RESEARCH_PROPERTY","hotel":"Hotel X"}']
    )
    researcher.search_error = "Weekly/Monthly Limit Exhausted."
    researcher.reset_search_status = lambda: None
    outcome = o.handle_message("Research Hotel X.", user_id="U1", thread_key="C:search-down")
    assert "Live web search was unavailable" in outcome.markdown
    assert "Weekly/Monthly Limit Exhausted." in outcome.markdown


def test_shortlisted_concept_is_sent_as_compact_writing_outline():
    from types import SimpleNamespace

    editorial = EditorialResult(
        status="complete", working_title="Rome by Day", strongest_experience="long paragraph " * 50,
        supporting_evidence_ids=["S1"],
    )
    critique = SimpleNamespace(verdict="PASS_WITH_REVISIONS", revision_instructions=["x" * 400] * 11)
    research = ResearchResult(status="complete", source_records=[
        {"sid": "S1", "url": "https://example.com/dining", "title": "Dining", "fetched_ok": True},
    ])
    outline = {
        "title": "Rome by Day, the Garden by Evening", "angle": "Days in Rome, evenings in the garden.",
        "why_readers_want_it": ["One fixed place to come back to."],
        "outline": ["Open: the plan [S41]", "Morning: breakfast", "Evening: aperitivo", "Close: late garden"],
        "proof_points": ["Garden sits between Giano and the W Lounge."],
        "avoid": ["Do not call it a secret garden."] * 6,
        "check_with_hotel": ["Garden months?"],
    }
    text = EditorialOrchestrator._format_outline(outline, editorial, critique, "shortlisted", research)
    assert text.startswith("**Rome by Day, the Garden by Evening**")
    assert "1. Open: the plan\n" in text  # source ids stripped
    assert text.count("Do not call it a secret garden.") == 4  # capped
    assert "long paragraph" not in text and "x" * 400 not in text
    assert "<https://example.com/dining|Dining>" in text
    assert len(text) < 1500


def test_outline_failure_falls_back_to_full_format(tmp_path: Path):
    o, *_ = setup_orchestrator(tmp_path, ['not json'])
    editorial = EditorialResult(status="complete", working_title="T", strongest_experience="E")
    from types import SimpleNamespace
    critique = SimpleNamespace(verdict="PASS", revision_instructions=[], unresolved_concerns=[])
    assert o._editorial_outline(editorial, critique, ResearchResult(status="complete")) is None


def test_each_specialist_step_emits_a_progress_label(tmp_path: Path):
    o, *_ = setup_orchestrator(tmp_path, ['{"intent":"RESEARCH_PROPERTY","hotel":"Hotel X"}'])
    stages = []
    o.add_progress_listener(stages.append)
    o.handle_message("Research Hotel X.", user_id="U1", thread_key="C:progress")
    steps = [s.removeprefix("step:") for s in stages if s.startswith("step:")]
    assert steps[0] == "Research: official sources, coverage and destination context"
    assert any(s.startswith("Editorial: building the recommendation") for s in steps)
    assert any(s.startswith("Critic: checking the recommendation") for s in steps)
    assert EditorialOrchestrator._step_label("editorial", "editorial_02").endswith("(round 2)")


def test_critique_reply_is_condensed_to_a_short_note():
    from types import SimpleNamespace

    critique = SimpleNamespace(verdict="FAIL", reasons=["long " * 200] * 5)
    summary = {
        "headline": "Fails: half the daily rhythm depends on a venue not shown to be open [S37].",
        "why": ["Zucchero is not confirmed as operating.", "Overlaps the garden concept.",
                "Dinner in-house nightly is a weak desire.", "Fourth reason."],
        "to_fix": ["Confirm Zucchero is open and where it is."],
        "next_step": "Ask the hotel about Zucchero.",
    }
    text = EditorialOrchestrator._format_critique_summary(summary, critique)
    assert text.startswith("**Critic: Fail**")
    assert "[S37]" not in text
    assert "Fourth reason." not in text  # capped at 3
    assert "long long" not in text
    assert text.endswith("_Next: Ask the hotel about Zucchero._")
    assert len(text) < 700


def test_failed_recritique_marks_concept_rejected_in_state_and_memory(tmp_path: Path):
    fail = CritiqueResult(verdict="FAIL", reasons=["Central venue not confirmed."])
    o, db, *_ = setup_orchestrator(
        tmp_path, ['{"intent":"CRITIQUE_CONCEPT"}'], critic_results=[fail]
    )
    pid = seed_project(db, thread="C:1")
    concept_id = db.insert_concept(
        project_id=pid, source_project_id=pid, hotel="Harbour Grand Kobe", name="Concept",
        proposition="p", experience="e", narrative_device="n", visual_device="v",
        story_structure="s", central_tension="t", keywords=[], status="approved",
    )
    outcome = o.handle_message("Critique this concept again.", user_id="U1", thread_key="C:1")
    assert db.project_state(pid)["concept"]["status"] == "rejected"
    assert db.recent_concepts(limit=5)[0].id == concept_id
    assert db.recent_concepts(limit=5)[0].status == "rejected"
    assert "Concept status updated to rejected" in outcome.markdown


def test_status_after_critique_rules():
    rule = EditorialOrchestrator._status_after_critique
    assert rule("shortlisted", "FAIL") == "rejected"
    assert rule("published", "FAIL") is None
    assert rule("rejected", "PASS_WITH_REVISIONS") == "shortlisted"
    assert rule("approved", "PASS") is None


def test_sample_draft_requests_are_recognised_narrowly():
    is_draft = EditorialOrchestrator._is_sample_draft_request
    for text in ("Write a sample draft", "can you write the article", "draft the piece please",
                 "viết bài mẫu cho concept này", "sample editorial for this"):
        assert is_draft(text), text
    for text in ("review this draft article", "here's our current article draft", "Research W Rome"):
        assert not is_draft(text), text


class FakeWriter:
    name = "writer"

    def __init__(self, label):
        self.label = label
        self.tasks = []

    def run(self, task):
        from app.orchestration.handoffs import DraftResult
        self.tasks.append(task)
        return DraftResult(status="complete", headline="Garden Evenings", standfirst="Two buildings, one garden.",
                           body="## Morning\nBreakfast at Giano.\n\nEvening in the garden.", tk_items=["garden hours"],
                           model=self.label)


def _draft_setup(tmp_path, critic_results, *, private=False):
    o, db, _r, _e, _d, _b, critic = setup_orchestrator(
        tmp_path, ['{"intent":"WRITE_SAMPLE_DRAFT"}', '{"intent":"WRITE_SAMPLE_DRAFT"}'],
        critic_results=critic_results,
    )
    glm, claude = FakeWriter("GLM-5.3-Flash"), FakeWriter("claude-opus-5-5")
    o._writer, o._private_writer = glm, claude
    pid = seed_project(db, thread="C:1")
    state = db.project_state(pid)
    concept = dict(state["concept"])
    concept["outline"] = {"title": "Garden Evenings", "avoid": ["Do not call it a secret garden."]}
    db.update_project_state(pid, "concept", concept)
    if private:
        research = dict(state["research"])
        research["source_records"] = [{"sid": "S9", "title": "deck.pdf (shared in Slack)", "private": True}]
        db.update_project_state(pid, "research", research)
    return o, db, pid, glm, claude, critic


def test_sample_draft_uses_glm_writer_and_one_qa_revision(tmp_path: Path):
    revise = CritiqueResult(verdict="PASS_WITH_REVISIONS", reasons=["x"],
                            revision_instructions=["Replace 'secret garden' with 'garden'."])
    passed = CritiqueResult(verdict="PASS", reasons=[])
    o, db, pid, glm, claude, critic = _draft_setup(tmp_path, [revise, passed])
    outcome = o.handle_message("Write a sample draft", user_id="U1", thread_key="C:1")

    assert len(glm.tasks) == 2 and not claude.tasks
    assert glm.tasks[1].qa_revisions == ["Replace 'secret garden' with 'garden'."]
    assert glm.tasks[0].outline["avoid"] == ["Do not call it a secret garden."]
    assert [t.mode for t in critic.tasks] == ["DRAFT", "DRAFT"]
    assert "**Morning**" in outcome.markdown and "QA by Claude: pass" in outcome.markdown
    assert "Drafted by GLM-5.3-Flash" in outcome.markdown
    assert db.project_state(pid)["concept"]["sample_draft"]["headline"] == "Garden Evenings"


def test_layout_request_lays_out_the_saved_draft_without_redrafting(tmp_path: Path):
    o, db, pid, glm, _claude, critic = _draft_setup(tmp_path, [CritiqueResult(verdict="PASS", reasons=[])])
    o.handle_message("Write a sample draft", user_id="U1", thread_key="C:1")
    assert len(glm.tasks) == 1 and len(critic.tasks) == 1

    outcome = o.handle_message("Make the layout with photos for this draft", user_id="U1", thread_key="C:1")

    assert len(glm.tasks) == 1 and len(critic.tasks) == 1
    assert outcome.attachments and Path(outcome.attachments[0]).is_file()
    page = Path(outcome.attachments[0]).read_text()
    assert "Garden Evenings" in page and 'class="cover plain"' in page
    assert db.project_state(pid)["concept"]["sample_draft"]["layout_html"] == outcome.attachments[0]


def test_sample_draft_with_hotel_documents_stays_with_claude(tmp_path: Path):
    o, _db, _pid, glm, claude, _critic = _draft_setup(tmp_path, [CritiqueResult(verdict="PASS", reasons=[])], private=True)
    outcome = o.handle_message("Write a sample draft", user_id="U1", thread_key="C:1")
    assert claude.tasks and not glm.tasks
    assert "stay with Claude" in outcome.markdown


def test_fallback_provider_switches_to_claude_on_glm_failure():
    from app.llm import FallbackProvider, LLMError, LLMMessage, LLMResponse

    class Down:
        def complete(self, messages, **kwargs):
            raise LLMError("quota exhausted")

    class Up:
        def complete(self, messages, **kwargs):
            return LLMResponse(text="ok", model="claude")

    provider = FallbackProvider(Down(), Up())
    assert provider.complete([LLMMessage("user", "hi")]).text == "ok"
    assert provider.last_used == "fallback"


def test_outline_is_persisted_with_the_shortlisted_concept(tmp_path: Path):
    o, db, *_ = setup_orchestrator(tmp_path, ['{"intent":"RESEARCH_PROPERTY","hotel":"Hotel X"}'])

    class OutlineProvider:
        def complete(self, messages, **kwargs):
            return LLMResponse(text='{"title":"T","angle":"A","outline":["Open: x"]}')

    o._fast_provider = OutlineProvider()
    outcome = o.handle_message("Research Hotel X.", user_id="U1", thread_key="C:outline")
    assert outcome.markdown.startswith("**T**")
    assert db.project_state(outcome.project_id)["concept"]["outline"]["angle"] == "A"


def test_writer_counts_tk_placeholders_from_the_body_not_the_model_list():
    import json

    from app.agents.writer import WriterAgent
    from app.orchestration.handoffs import DraftTask

    class Provider:
        def complete(self, messages, **kwargs):
            return LLMResponse(text=json.dumps({
                "headline": "H", "standfirst": "S",
                "body": "Giano opens at [TK: dinner start time].\n\nThe garden [TK: season] waits.",
                "tk_items": ["a", "b", "c", "d", "e"],
            }), model="GLM-5.3-Flash")

    draft = WriterAgent(Provider()).run(DraftTask(
        project_id=1, hotel_name="W Rome", user_request="draft", concept={}, outline={}, research={},
    ))
    assert draft.tk_items == ["[TK: dinner start time]", "[TK: season]"]


def test_trip_length_is_parsed_from_the_request():
    parse = EditorialOrchestrator._parse_trip_nights
    assert parse("Plan a 4-night itinerary for W Rome") == 4
    assert parse("lịch trình 4 đêm ở Rome") == 4
    assert parse("a 5 day trip") == 4
    assert parse("Research W Rome and find one stay experience") == 0
    assert parse("1 night") == 0


def test_itinerary_request_shapes_editorial_and_is_saved_with_the_concept(tmp_path: Path):
    o, db, researcher, editorial, *_ = setup_orchestrator(
        tmp_path, ['{"intent":"RESEARCH_PROPERTY","hotel":"Hotel X"}']
    )
    outcome = o.handle_message("Research Hotel X and plan a 4-night itinerary.", user_id="U1", thread_key="C:trip")
    assert researcher.tasks[0].trip_nights == 4
    instructions = " ".join(editorial.tasks[0].brief.explicit_instructions)
    assert "4-night (5-day) itinerary" in instructions
    assert db.project_state(outcome.project_id)["concept"]["format"] == {"type": "itinerary", "nights": 4}


def test_direct_answers_see_the_latest_draft_tk_gaps(tmp_path: Path):
    o, db, *_ = setup_orchestrator(tmp_path, ['{"intent":"GENERAL_CONVERSATION"}'])
    pid = seed_project(db, thread="C:1")
    state = db.project_state(pid)
    research = dict(state["research"])
    research["verified_facts"] = [{"fact": "x" * 300, "confidence": "verified_official"}] * 400
    db.update_project_state(pid, "research", research)
    concept = dict(state["concept"])
    concept["editorial"] = {**concept["editorial"], "possible_story_structure": "Day plan " * 3000}
    concept["sample_draft"] = {"headline": "Four Nights", "tk_items": ["[TK: official check-in time]"],
                               "qa": {"verdict": "PASS_WITH_REVISIONS"}}
    db.update_project_state(pid, "concept", concept)

    context = EditorialOrchestrator._answer_context(db.project_state(pid))
    assert "[TK: official check-in time]" in context
    assert len(context) < 27000


def test_turning_a_concept_into_an_itinerary_researches_venues_first(tmp_path: Path):
    o, db, researcher, editorial, *_ = setup_orchestrator(
        tmp_path, ['{"intent":"FIND_EDITORIAL_OPPORTUNITY"}']
    )
    seed_project(db, thread="C:1")
    o.handle_message("Turn this into a 4-night itinerary.", user_id="U1", thread_key="C:1")
    assert researcher.tasks, "venue research should run before editorial"
    venue_task = researcher.tasks[0]
    assert venue_task.trip_nights == 4
    assert any("Restaurants" in q for q in venue_task.followup_questions)
    assert o.last_trace[:2] == ["researcher", "editorial"]
    assert EditorialOrchestrator._step_label("researcher", "research_itinerary").startswith("Research: restaurants")


class HumanizeProvider:
    def __init__(self, reply):
        self.reply = reply
        self.systems = []

    def complete(self, messages, *, system=None, **kwargs):
        self.systems.append(system or "")
        return LLMResponse(text=self.reply, model="claude-sonnet-5-5")


def _humanize_draft():
    from app.orchestration.handoffs import DraftResult
    return DraftResult(
        status="complete", headline="Above Bangkok | Park Hotel", standfirst="A tower hotel.",
        body="## Day 1 · Arrival\n**3pm · Settle in**\nNestled high above, the room boasts a view — truly a haven. "
             "Ask about [TK: check-in time].\n\n## Insider notes\nBest time: dusk.",
        model="GLM-5.3-Flash",
    )


def test_final_qa_fixes_apply_only_exact_unique_passages():
    import json as _json
    from app.agents.writer import WriterAgent
    draft = _humanize_draft()
    reply = _json.dumps({"edits": [
        {"field": "body", "find": "truly a haven", "replace": "high above the city"},
        {"field": "body", "find": "not in the draft", "replace": "x"},
        {"field": "body", "find": "## Insider notes", "replace": "## Notes"},
        {"field": "standfirst", "find": "A tower hotel.", "replace": "A tower hotel above the mall."},
    ], "skipped": ["Cut Day 3: needs a rebuild."]})
    fixed, applied, skipped = WriterAgent(HumanizeProvider(reply)).apply_fixes(draft, ["a", "b", "c", "d", "e"])
    assert applied == 2 and skipped == ["Cut Day 3: needs a rebuild."]
    assert "high above the city" in fixed.body and "## Insider notes" in fixed.body
    assert fixed.standfirst == "A tower hotel above the mall." and fixed.tk_items == ["[TK: check-in time]"]


def test_last_qa_round_instructions_are_applied_to_the_draft(tmp_path: Path):
    import json as _json
    from app.agents.writer import WriterAgent
    revise = CritiqueResult(verdict="PASS_WITH_REVISIONS", reasons=["x"],
                            revision_instructions=["Replace 'secret garden' with 'garden'."])
    o, db, pid, glm, _claude, critic = _draft_setup(tmp_path, [revise, revise])
    fixes = HumanizeProvider(_json.dumps({"edits": [], "skipped": []}))
    o._humanizer = WriterAgent(fixes)
    outcome = o.handle_message("Write a sample draft", user_id="U1", thread_key="C:1")
    assert any("final fixes" in sys_ or "editor's last revision" in sys_ for sys_ in fixes.systems)
    saved = db.project_state(pid)["concept"]["sample_draft"]
    assert saved["qa_fixes"] == {"applied": 0, "total": 1, "skipped": [], "rounds": 1}
    assert "Replace 'secret garden'" in outcome.markdown  # nothing applied: the instruction is still shown
    assert len(critic.tasks) == 2  # nothing applied, so no re-check


def test_draft_is_rechecked_after_final_fixes(tmp_path: Path):
    import json as _json
    from app.agents.writer import WriterAgent
    revise = CritiqueResult(verdict="PASS_WITH_REVISIONS", reasons=["x"],
                            revision_instructions=["Replace 'secret garden' with 'garden'."])
    passed = CritiqueResult(verdict="PASS", reasons=[])
    o, db, pid, glm, _claude, critic = _draft_setup(tmp_path, [revise, revise, passed])
    o._humanizer = WriterAgent(HumanizeProvider(_json.dumps(
        {"edits": [{"field": "headline", "find": "Garden", "replace": "Garden"}], "skipped": []})))
    outcome = o.handle_message("Write a sample draft", user_id="U1", thread_key="C:1")
    assert len(critic.tasks) == 3
    assert "QA by Claude: pass (re-checked after 1 final fixes)" in outcome.markdown
    assert db.project_state(pid)["concept"]["sample_draft"]["qa"]["verdict"] == "PASS"


def test_wording_polish_does_not_block_a_pass_but_is_listed(tmp_path: Path):
    passed = CritiqueResult(verdict="PASS", reasons=[], polish=["Use 'the two of you' once."])
    o, db, pid, glm, _claude, critic = _draft_setup(tmp_path, [passed])
    outcome = o.handle_message("Write a sample draft", user_id="U1", thread_key="C:1")
    assert len(glm.tasks) == 1 and len(critic.tasks) == 1
    assert "QA by Claude: pass**" in outcome.markdown
    assert "Optional wording polish" in outcome.markdown and "the two of you" in outcome.markdown


def test_wording_polish_on_a_pass_is_applied_and_rechecked(tmp_path: Path):
    import json as _json
    from app.agents.writer import WriterAgent
    polished = CritiqueResult(verdict="PASS", reasons=[], polish=["Replace 'Garden' in the headline."])
    passed = CritiqueResult(verdict="PASS", reasons=[])
    o, db, pid, glm, _claude, critic = _draft_setup(tmp_path, [polished, passed])
    o._humanizer = WriterAgent(HumanizeProvider(_json.dumps(
        {"edits": [{"field": "headline", "find": "Garden", "replace": "Garden"}], "skipped": []})))
    outcome = o.handle_message("Write a sample draft", user_id="U1", thread_key="C:1")
    assert len(critic.tasks) == 2
    assert "QA by Claude: pass (re-checked after 1 final fixes)" in outcome.markdown
    assert "Optional wording polish" not in outcome.markdown


def test_second_fix_round_runs_only_for_substantive_fixes_found_by_the_recheck(tmp_path: Path):
    import json as _json
    from app.agents.writer import WriterAgent
    revise = CritiqueResult(verdict="PASS_WITH_REVISIONS", reasons=["x"],
                            revision_instructions=["Replace 'Garden' in the headline."])
    o, db, pid, glm, _claude, critic = _draft_setup(tmp_path, [revise, revise, revise, revise])
    o._humanizer = WriterAgent(HumanizeProvider(_json.dumps(
        {"edits": [{"field": "headline", "find": "Garden", "replace": "Garden"}], "skipped": []})))
    outcome = o.handle_message("Write a sample draft", user_id="U1", thread_key="C:1")
    # two QA rounds, a re-check after each of the two fix rounds, and no third round
    assert len(critic.tasks) == 4
    assert "QA by Claude: pass with revisions (re-checked after 2 final fixes)" in outcome.markdown
    assert db.project_state(pid)["concept"]["sample_draft"]["qa_fixes"]["rounds"] == 2


def test_polish_from_the_last_recheck_is_applied_without_another_check(tmp_path: Path):
    import json as _json
    from app.agents.writer import WriterAgent
    revise = CritiqueResult(verdict="PASS_WITH_REVISIONS", reasons=["x"],
                            revision_instructions=["Replace 'Garden' in the headline."])
    passed = CritiqueResult(verdict="PASS", reasons=[], polish=["Plainer word for 'Garden'."])
    o, db, pid, glm, _claude, critic = _draft_setup(tmp_path, [revise, revise, passed])
    o._humanizer = WriterAgent(HumanizeProvider(_json.dumps(
        {"edits": [{"field": "headline", "find": "Garden", "replace": "Garden"}], "skipped": []})))
    outcome = o.handle_message("Write a sample draft", user_id="U1", thread_key="C:1")
    assert len(critic.tasks) == 3
    assert "QA by Claude: pass (re-checked after 1 final fixes, then 1 wording edits)" in outcome.markdown
    assert "Optional wording polish" not in outcome.markdown
    saved = db.project_state(pid)["concept"]["sample_draft"]
    assert saved["qa_fixes"]["polished"] == 1 and saved["qa"]["polish"] == []


def test_humanizer_keeps_structure_and_rewrites_tells():
    import json as _json
    from app.agents.writer import WriterAgent
    body = ("## Day 1 · Arrival\n**3pm · Settle in**\nThe room is high up and has a view of the city. "
            "Ask about [TK: check-in time].\n\n## Insider notes\nBest time: dusk.")
    provider = HumanizeProvider(_json.dumps({"headline": "Above Bangkok | Park Hotel", "standfirst": "A tower hotel.",
                                             "body": body}))
    result = WriterAgent(provider).humanize(_humanize_draft())
    assert "humanizer" in provider.systems[0].lower()
    assert result.body == body and result.tk_items == ["[TK: check-in time]"]
    assert result.model == "GLM-5.3-Flash"


def test_humanizer_rewrite_that_drops_a_tk_or_subhead_is_rejected():
    import json as _json
    from app.agents.writer import WriterAgent
    original = _humanize_draft()
    lost_tk = original.body.replace("[TK: check-in time]", "at 3pm")
    lost_head = original.body.replace("## Insider notes\n", "")
    for body in (lost_tk, lost_head, ""):
        provider = HumanizeProvider(_json.dumps({"headline": "x", "body": body}))
        assert WriterAgent(provider).humanize(original) is original


def test_sample_draft_is_humanized_before_qa_when_enabled(tmp_path: Path):
    o, _db, _pid, glm, _claude, critic = _draft_setup(tmp_path, [CritiqueResult(verdict="PASS", reasons=[])])
    from dataclasses import replace as _replace
    o._settings = _replace(o._settings, humanize_drafts=True)
    seen = []

    class Humanizer:
        def humanize(self, draft, *, piece_format=None):
            from dataclasses import replace
            seen.append(draft.body)
            return replace(draft, body=draft.body.replace("Evening in the garden.", "Evenings are spent in the garden."))

    o._humanizer = Humanizer()
    outcome = o.handle_message("Write a sample draft", user_id="U1", thread_key="C:1")
    assert seen and len(glm.tasks) == 1
    assert "Evenings are spent in the garden." in critic.tasks[0].draft["body"]
    assert "Evenings are spent in the garden." in outcome.markdown


def test_humanizer_drops_a_stray_repeated_line():
    import json as _json
    from app.agents.writer import WriterAgent
    original = _humanize_draft()
    body = original.body.replace("## Insider notes", "Ask about the view.\n\n## Insider notes").replace(
        "Nestled high above, the room boasts a view — truly a haven.", "The room is high up. Ask about the view.")
    provider = HumanizeProvider(_json.dumps({"headline": "x", "body": body}))
    result = WriterAgent(provider).humanize(original)
    assert result is not original and "view.\n\n## Insider" not in result.body
    assert "The room is high up. Ask about the view." in result.body


def test_stray_paragraph_that_repeats_earlier_sentences_is_dropped():
    from app.agents.writer import drop_echo_paragraphs
    body = ("The hotel sits above the mall. A skywalk links it to the BTS.\n\n## Day 2\n**Morning · Park**\nWalk.\n\n"
            "A skywalk links it to the BTS.\n\n## Day 3\nMore.")
    assert drop_echo_paragraphs(body) == body.replace("A skywalk links it to the BTS.\n\n## Day 3", "## Day 3")


def test_itinerary_draft_gets_travel_estimates_and_fixes(tmp_path: Path):
    from app.agents.logistics import Leg, LogisticsResult

    o, db, pid, glm, _claude, critic = _draft_setup(tmp_path, [CritiqueResult(verdict="PASS", reasons=[])])
    concept = dict(db.project_state(pid)["concept"])
    concept["format"] = {"type": "itinerary", "nights": 3}
    db.update_project_state(pid, "concept", concept)

    class FakeLogistics:
        def __init__(self):
            self.tasks = []

        def run(self, task):
            self.tasks.append(task)
            return LogisticsResult(
                status="complete",
                legs=[Leg(2, "Hotel", "Dinner", arrive="17:30", distance_km=8.0, walk_min=90,
                          drive_min=(40, 60), traffic="evening rush hour", mode="car", verdict="not_feasible")],
                issues=["Day 2: Hotel → Dinner does not fit before 17:30."], day_travel_min={2: (40, 60)},
            )

    o._logistics = FakeLogistics()
    o.handle_message("Write a sample draft", user_id="U1", thread_key="C:1")

    assert o._logistics.tasks and "outline" in o._logistics.tasks[0].plan
    sent = glm.tasks[0].outline
    assert sent["travel_fixes"] == ["Day 2: Hotel → Dinner does not fit before 17:30."]
    assert "car about 40-60 min in evening rush hour" in sent["travel_estimates"][0]
    assert critic.tasks[0].outline["travel_fixes"]
    saved = db.project_state(pid)["concept"]
    assert saved["logistics"]["issues"] and "travel_fixes" not in saved["outline"]
    # The re-check replaces a stale reader-facing summary the critic would otherwise quote.
    assert critic.tasks[0].outline["getting_around"][0].startswith("Day 2: Hotel → Dinner does not fit")
    assert saved["outline"]["getting_around"] == critic.tasks[0].outline["getting_around"]

    # A second draft reuses the stored check instead of hitting the map again.
    o.handle_message("Write a sample draft", user_id="U1", thread_key="C:1")
    assert len(o._logistics.tasks) == 1


def test_draft_keeps_the_outlines_chosen_headline():
    from app.orchestration.handoffs import DraftResult
    from app.orchestration.orchestrator import EditorialOrchestrator as O

    outline = {"title": "Intensity outside, intimacy upstairs | Hotel"}
    drifted = DraftResult(status="complete", headline="Four days, lobby on 10 | Hotel", body="x")
    assert O._pin_headline(drifted, outline, []).headline == outline["title"]
    # Only a QA instruction about the headline may change it.
    assert O._pin_headline(drifted, outline, ["Change the headline to ..."]).headline == drifted.headline
    assert O._pin_headline(drifted, {"title": "No hotel part"}, []).headline == drifted.headline


def test_outline_shows_other_headlines_and_the_opening_hook():
    from types import SimpleNamespace

    editorial = EditorialResult(status="complete", working_title="W")
    critique = SimpleNamespace(verdict="PASS", revision_instructions=[])
    outline = {
        "title_options": ["Above the city | Hotel", "On old embassy land | Hotel", "Two nights up high | Hotel"],
        "title": "On old embassy land | Hotel", "angle": "A tower stay.",
        "opening_hook": "The hotel stands on land that was once part of the British Embassy.",
        "outline": ["Day 1 · Arrival: settle in"],
    }
    text = EditorialOrchestrator._format_outline(
        outline, editorial, critique, "shortlisted", ResearchResult(status="complete"), itinerary=True,
    )
    assert text.startswith("**On old embassy land | Hotel**")
    assert "_Other headlines:_ Above the city | Hotel / Two nights up high | Hotel" in text
    assert "_Opening hook:_ The hotel stands on land" in text


def test_humanizer_rewrite_that_admits_added_facts_is_rejected():
    import json as _json
    from app.agents.writer import WriterAgent
    original = _humanize_draft()
    body = original.body.replace("Nestled high above, the room boasts a view — truly a haven.",
                                 "The room is high up and the lift takes you straight there.")
    provider = HumanizeProvider(_json.dumps({"body": body, "added_facts": ["the lift takes you straight there"]}))
    assert WriterAgent(provider).humanize(original) is original


def test_a_holiday_request_gives_the_editorial_a_computed_calendar_and_stores_it(tmp_path: Path):
    from datetime import date
    o, db, researcher, editorial, *_ = setup_orchestrator(
        tmp_path, ['{"intent":"RESEARCH_PROPERTY","hotel":"Hotel X"}']
    )
    o._today = date(2026, 11, 10)
    outcome = o.handle_message("Research Hotel X and plan a 4-night stay over Seollal 2027.",
                               user_id="U1", thread_key="C:trip")
    instructions = " ".join(editorial.tasks[0].brief.explicit_instructions)
    assert "CALENDAR" in instructions and "Day 2 · Sunday 7 February 2027 · Seollal" in instructions
    stored = db.project_state(outcome.project_id)["concept"]["format"]
    assert stored["nights"] == 4 and stored["calendar"]["start"] == "2027-02-06"


class DatedWriter(FakeWriter):
    def run(self, task):
        from app.orchestration.handoffs import DraftResult
        self.tasks.append(task)
        return DraftResult(
            status="complete", headline="Garden Evenings", standfirst="We arrive on Friday 5 February.",
            body="Intro.\n\n## Day 1 · Friday 5 February · Arrival\nDinner [TK: Friday dinner hours].\n\n## Day 2\nSeollal.",
            model=self.label,
        )


def test_a_corrected_calendar_in_a_draft_request_overrides_the_stored_concept(tmp_path: Path):
    from datetime import date
    passed = CritiqueResult(verdict="PASS", reasons=[])
    o, db, pid, glm, _claude, critic = _draft_setup(tmp_path, [passed, passed, passed, passed])
    o._today = date(2026, 11, 10)
    o._writer = glm = DatedWriter("GLM-5.3-Flash")
    concept = dict(db.project_state(pid)["concept"])
    concept["format"] = {"type": "itinerary", "nights": 4}
    db.update_project_state(pid, "concept", concept)

    request = ("Write a sample draft again with the right calendar: Day 1 Sat 6 Feb 2027 (fly in), "
               "Day 2 Sun 7 Feb (Seollal), Day 5 Wed 10 Feb (fly home).")
    o.handle_message(request, user_id="U1", thread_key="C:1")

    task = glm.tasks[0]
    assert task.format["calendar"]["start"] == "2027-02-06"
    assert task.outline["calendar"][1].startswith("Day 2 · Sunday 7 February 2027")
    assert task.outline["user_amendments"] == [request]
    assert critic.tasks[0].outline["user_amendments"] == [request]
    saved = db.project_state(pid)["concept"]
    assert saved["format"]["calendar"]["start"] == "2027-02-06" and saved["amendments"] == [request]
    assert "calendar" not in saved["outline"] and "user_amendments" not in saved["outline"]
    body = saved["sample_draft"]["body"]
    # Python fixes the mechanical part...
    assert "## Day 1 · Saturday 6 February · Arrival" in body and "[TK: Saturday dinner hours]" in body
    assert "## Day 2 · Sunday 7 February" in body
    # ...and turns the rest into a revision the critic missed.
    first_check = critic.tasks[0]
    assert first_check.draft["body"].startswith("Intro.")
    assert len(glm.tasks) == 2  # the date check sent the draft back once
    assert any("outside the trip" in r for r in glm.tasks[1].qa_revisions)

    # A later plain request keeps the stored calendar and amendment.
    o.handle_message("Write a sample draft", user_id="U1", thread_key="C:1")
    assert glm.tasks[-1].format["calendar"]["start"] == "2027-02-06"
    assert glm.tasks[-1].outline["user_amendments"] == [request]


def test_amendments_are_only_the_extra_instructions():
    assert EditorialOrchestrator._amendment_from("Write a sample draft") == ""
    assert EditorialOrchestrator._amendment_from("<@U1> write a sample draft again.") == ""
    note = "Write a sample draft. Keep Day 4 inside the city (no day trip)."
    assert EditorialOrchestrator._amendment_from(note) == note


def test_a_request_for_every_kind_of_traveller_is_a_mixed_party_with_lanes():
    parse = EditorialOrchestrator._parse_segment
    assert parse("Plan Seollal for all Korean traveller groups") == "mixed"
    assert parse("for families, couples and solo travellers") == "mixed"
    assert parse("for a honeymoon couple") == "couples"
    assert parse("open to all guests") == ""
    piece = EditorialOrchestrator._segment_format("mixed")
    assert "For grandparents" in piece["segment_lanes"]
    assert "segment_lanes" not in EditorialOrchestrator._segment_format("couples")


def test_house_style_and_contacts_can_be_set_from_slack_and_reach_the_writer(tmp_path: Path):
    passed = CritiqueResult(verdict="PASS", reasons=[])
    o, db, pid, glm, _claude, critic = _draft_setup(tmp_path, [passed, passed])
    assert "Never name a source" in o.handle_message("style rules", user_id="U1", thread_key="C:1").markdown
    reply = o.handle_message("style rule: Prices in euros, never won.", user_id="U1", thread_key="C:1").markdown
    import re as _re
    rule_id = int(_re.search(r"#(\d+)", reply).group(1))
    assert "Banned" in o.handle_message("banned phrase: hidden gem | must-see", user_id="U1", thread_key="C:1").markdown
    assert "contact at" in o.handle_message(
        "hotel contact: Anna\n*Sent using* <@U9|Claude>", user_id="U1", thread_key="C:1").markdown
    o.handle_message("editor: Minji", user_id="U1", thread_key="C:1")

    o.handle_message("Write a sample draft", user_id="U1", thread_key="C:1")
    outline = glm.tasks[-1].outline
    assert "Prices in euros, never won." in outline["house_style"]
    assert any('"hidden gem"' in r and '"one reviewer"' in r for r in outline["house_style"])
    assert outline["contacts"] == {"editor": "Minji", "hotel": "Anna"}
    assert critic.tasks[-1].outline["contacts"]["hotel"] == "Anna"
    assert "house_style" not in db.project_state(pid)["concept"]["outline"]

    assert "Removed" in o.handle_message(f"remove style rule #{rule_id}", user_id="U1", thread_key="C:1").markdown
    assert all(r["rule"] != "Prices in euros, never won." for r in db.style_rules())


def test_a_named_source_in_the_copy_is_sent_back_by_the_house_style_check(tmp_path: Path):
    passed = CritiqueResult(verdict="PASS", reasons=[])
    o, db, pid, glm, _claude, critic = _draft_setup(tmp_path, [passed, passed, passed, passed])

    class AttributingWriter(FakeWriter):
        def run(self, task):
            from app.orchestration.handoffs import DraftResult
            self.tasks.append(task)
            return DraftResult(status="complete", headline="Garden Evenings", standfirst="One garden.",
                               body="## Morning\nOne reviewer loved breakfast at Giano. [ASK ANNA: a dish to order]",
                               model=self.label)

    o._writer = glm = AttributingWriter("GLM-5.3-Flash")
    outcome = o.handle_message("Write a sample draft", user_id="U1", thread_key="C:1")
    assert any('remove "one reviewer"' in r for r in glm.tasks[1].qa_revisions)
    assert "1 [ASK] notes for the editor" in outcome.markdown


def test_layout_renders_ask_notes_for_the_editor():
    from app.artifacts.layout import _inline
    html = _inline("Order the pasta [ASK ANNA: two winter dishes] tonight.")
    assert '<mark class="ask"><b>Ask Anna</b> two winter dishes</mark>' in html


def test_venue_notes_keep_only_cited_stops_and_research_the_missing_ones_once(tmp_path: Path):
    import json as _json
    from dataclasses import replace as _replace
    from app.llm.base import LLMResponse

    o, db, researcher, *_ = setup_orchestrator(tmp_path, ['{"intent":"GENERAL_CONVERSATION"}'])
    o._settings = _replace(o._settings, venue_notes=True)
    o._trip_nights = 4
    pid = seed_project(db, thread="C:v")
    research = ResearchResult(status="complete", verified_facts=[
        {"fact": "City Gallery holds the river statue and is closed on Mondays.",
         "confidence": "verified_authoritative_secondary", "source_ids": ["S1"]},
        {"fact": "The hotel's rooftop reopens in mid-May.", "confidence": "verified_official", "source_ids": ["S2"]},
    ])
    answers = [
        {"venues": [
            {"name": "City Gallery", "hook": "the river statue", "closed_days": ["Mondays"], "facts": ["F1"]},
            {"name": "Invented Bistro", "hook": "Great pasta", "facts": ["F1"]},
        ], "hotel_spaces": [{"name": "rooftop", "open_on_trip": "no", "note": "reopens mid-May", "facts": ["F2"]}],
         "missing": ["Owl Villa"]},
        {"venues": [{"name": "City Gallery", "hook": "the river statue", "closed_days": ["Monday"], "facts": ["F1"]}],
         "hotel_spaces": [], "missing": ["Owl Villa"]},
    ]

    class Fast:
        def __init__(self):
            self.calls = 0

        def complete(self, messages, *, system=None, **kwargs):
            self.calls += 1
            return LLMResponse(text=_json.dumps(answers[min(self.calls - 1, 1)]))

    o._fast_provider = Fast()
    concept = {"possible_story_structure": "Day 2: City Gallery. Day 4: Owl Villa and Invented Bistro."}
    notes = o._venue_notes(pid, "Harbour Grand Kobe", concept, research)
    assert [v["name"] for v in notes["venues"]] == ["City Gallery"]
    assert notes["venues"][0]["closed_days"] == ["Monday"]
    # One research round for Owl Villa; the fake researcher adds no facts, so the
    # second pass has nothing to read and the first notes are kept.
    assert o._fast_provider.calls == 1
    assert researcher.tasks[-1].destination and "Owl Villa" in researcher.tasks[-1].followup_questions[0]


def test_closed_days_and_closed_hotel_spaces_are_checked_against_the_calendar():
    from datetime import date
    from app.orchestration.calendar import TripCalendar
    from app.orchestration.draft_checks import check_draft
    cal = TripCalendar.build(date(2027, 2, 6), 4)
    venues = {"venues": [{"name": "City Gallery", "closed_days": ["Monday"]}],
              "hotel_spaces": [{"name": "rooftop", "open_on_trip": "no", "note": "reopens mid-May"}]}
    body = ("## Day 2 · Sunday 7 February\nCity Gallery.\n\n## Day 3 · Monday 8 February\n"
            "Back to the City Gallery, then drinks on the rooftop.\n\nIn the warmer months, come back for the rooftop.")
    issues = check_draft(body, calendar=cal, venues=venues)
    assert any("closed on Mondays, but Day 3" in i for i in issues)
    assert sum("rooftop is not open" in i for i in issues) == 1
    assert not check_draft("In the warmer months, the rooftop is the place for a drink.", calendar=cal, venues=venues)


def test_humanizer_and_fixes_keep_the_writers_source_notes():
    import json as _json
    from app.agents.writer import WriterAgent
    from app.orchestration.handoffs import DraftResult
    draft = DraftResult(status="complete", headline="H | Hotel", standfirst="S", body="## Day 1\nPasta at Giano.",
                        source_notes=[{"claim": "Pasta at Giano", "sources": ["S3"]}])
    fixer = WriterAgent(HumanizeProvider(_json.dumps({"edits": [{"field": "body", "find": "Pasta", "replace": "Fresh pasta"}]})))
    fixed, applied, _ = fixer.apply_fixes(draft, ["Say fresh pasta."])
    assert applied == 1 and fixed.source_notes == draft.source_notes


def test_layout_styles_are_read_from_the_request(tmp_path: Path):
    o, *_ = setup_orchestrator(tmp_path, ['{"intent":"GENERAL_CONVERSATION"}'])
    assert o._layout_styles("make the layout") == ["classic"]  # the Settings dataclass default
    assert o._layout_styles("make the layout with photos side by side") == ["zigzag"]
    assert o._layout_styles("layout with the photos inside the text") == ["inline"]
    assert o._layout_styles("make all four layouts") == ["zigzag", "image_first", "inline", "hybrid"]


def test_all_four_layouts_are_attached_for_a_saved_draft(tmp_path: Path):
    o, db, pid, glm, _claude, critic = _draft_setup(tmp_path, [CritiqueResult(verdict="PASS", reasons=[])])
    o.handle_message("Write a sample draft", user_id="U1", thread_key="C:1")
    outcome = o.handle_message("Make all four layouts for this draft", user_id="U1", thread_key="C:1")
    assert len(outcome.attachments) == 4 and all(Path(p).is_file() for p in outcome.attachments)
    assert [Path(p).stem.rsplit("-", 1)[-1] for p in outcome.attachments] == ["zigzag", "image_first", "inline", "hybrid"]


def test_photos_of_spaces_closed_on_the_trip_go_to_the_closing_strip():
    catalogue = {
        "img1": {"evidence": "Rooftop plunge pool with blue tiles", "page_context": "WET Deck"},
        "img2": {"evidence": "Walnut-panelled restaurant", "page_context": "Giano"},
    }
    state = {"concept": {"venues": {"hotel_spaces": [
        {"name": "WET Deck", "open_on_trip": "no"}, {"name": "Giano", "open_on_trip": "yes"}]}}}
    assert EditorialOrchestrator._closed_space_photos(catalogue, state) == ["img1"]
