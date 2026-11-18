#!/usr/bin/env python3
"""A/B one step's model on a real thread: the same input, two models, scored and judged blind.

Each arm runs on its own copy of the bot's database (sqlite backup(); the live DB
is never written) with STEP_MODELS set for one step. Per arm and run it records
the time, the step's own checks (the golden guide checks for guide steps, the
outline's required parts for the outline), and then a judge on the main model
compares the two outputs blind (order shuffled) with `ab_judge.md`. Keep the
cheaper model for a step only when it keeps the checks and does not lose the
judging; set the winner in STEP_MODELS.

    .venv/bin/python scripts/ab_models.py --step guide_write --arms default,claude-sonnet-5-5
    .venv/bin/python scripts/ab_models.py --step outline --arms default,claude-opus-5-5 --runs 2

"default" is the step's usual model (main for guide_write/option_judge, fast for
guide_check/outline). Uses the network and Claude usage; unit tests stay offline.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sqlite3
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings  # noqa: E402
from app.llm import STEP_ROLES, build_provider, build_step_providers  # noqa: E402
from app.llm.base import LLMMessage  # noqa: E402
from app.llm.zai_glm import extract_json_block  # noqa: E402
from app.orchestration.calendar import TripCalendar  # noqa: E402
from app.orchestration.handoffs import CritiqueResult, EditorialResult, ResearchResult  # noqa: E402
from app.orchestration.orchestrator import EditorialOrchestrator  # noqa: E402
from app.prompts import load_prompt  # noqa: E402
from app.side_flows.guide_eval import evaluate_guide  # noqa: E402
from app.states import DRAFTABLE_CONCEPTS  # noqa: E402
from app.storage import Database  # noqa: E402

CRITERIA = {
    "guide_write": "A Korean web itinerary page: natural, short 합니다체 sentences; every moment concrete and "
                   "true to the plan; timing that suits the season; the hotel in every day; [확인 필요] only "
                   "where the plan truly lacks the fact.",
    "guide_check": "The same page after the check and its fixes: fewer real problems left, nothing correct "
                   "removed or made vaguer.",
    "outline": "A writing outline for an editor: a headline that makes the reader want the trip, a clear angle, "
               "a day-by-day outline true to the concept and the critic's restrictions, proof points the "
               "evidence supports, a useful stay-info line and insider notes.",
}
OUTLINE_PARTS = ("title", "title_options", "angle", "why_readers_want_it", "outline", "proof_points",
                 "opening_hook", "stay_info", "insider_notes")


def _copy_db(source: Path, target: Path) -> None:
    src, dst = sqlite3.connect(source), sqlite3.connect(target)
    src.backup(dst)
    src.close()
    dst.close()


def _pick_project(db: Database, wanted: int | None) -> dict[str, Any]:
    rows = db._conn.execute(  # noqa: SLF001 - a script reading its own copy
        "SELECT id, hotel, slack_channel, slack_thread_ts FROM projects ORDER BY id DESC"
    ).fetchall()
    for row in rows:
        if wanted and int(row["id"]) != wanted:
            continue
        concept = db.project_state(int(row["id"])).get("concept") or {}
        if concept.get("status") in DRAFTABLE_CONCEPTS and row["slack_thread_ts"]:
            return dict(row)
    raise SystemExit("No project with a draftable concept" + (f" with id {wanted}" if wanted else ""))


def _outline_checks(outline: dict[str, Any]) -> dict[str, Any]:
    present = [k for k in OUTLINE_PARTS if outline.get(k)]
    options = outline.get("title_options") or []
    return {"parts": f"{len(present)}/{len(OUTLINE_PARTS)}", "missing": [k for k in OUTLINE_PARTS if k not in present],
            "title_options": len(options) if isinstance(options, list) else 0,
            "passed": len(present) == len(OUTLINE_PARTS)}


def run_arm(settings, step: str, arm: str, project: dict[str, Any], temp: Path) -> dict[str, Any]:
    copy = temp / f"{arm.replace('/', '_')}-{time.monotonic_ns()}.db"
    _copy_db(settings.database_path, copy)
    arm_settings = replace(
        settings, database_path=copy, artifacts_path=temp / "artifacts",
        step_models=() if arm == "default" else ((step, arm),),
    )
    db = Database(copy)
    orchestrator = EditorialOrchestrator(
        arm_settings, build_provider(arm_settings), db,
        research_provider=build_provider(arm_settings, role="research"),
        step_providers=build_step_providers(arm_settings),
    )
    pid = int(project["id"])
    started = time.monotonic()
    try:
        if step in {"guide_write", "guide_check"}:
            thread_key = project["slack_thread_ts"]  # projects store the thread key "<channel>:<ts>"
            orchestrator.handle_message("hotel guide", user_id="ab", thread_key=thread_key,
                                        channel_id=project["slack_channel"], thread_ts=thread_key.rsplit(":", 1)[-1])
            stored = (db.project_state(pid).get("concept") or {}).get("guide_ko") or {}
            evaluation = evaluate_guide(stored)
            output = stored.get("guide") or {}
            checks = {**evaluation.to_dict(), "fixes_applied": stored.get("fixes_applied"),
                      "open_issues": stored.get("open_issues"), "check_items": len(stored.get("checks") or [])}
            passed = evaluation.passed
        elif step == "outline":
            state = db.project_state(pid)
            concept = state.get("concept") or {}
            piece_format = concept.get("format") or {}
            orchestrator._trip_nights = int(piece_format.get("nights") or 0)  # noqa: SLF001 - the request shape
            orchestrator._calendar = TripCalendar.from_dict(piece_format.get("calendar"))  # noqa: SLF001
            critique = CritiqueResult(**{
                k: v for k, v in (concept.get("critic_result") or {"verdict": "PASS", "reasons": []}).items()
                if k in CritiqueResult.__dataclass_fields__
            })
            output = orchestrator._editorial_outline(  # noqa: SLF001
                EditorialResult.from_dict(concept.get("editorial") or {}), critique,
                ResearchResult.from_dict(state.get("research") or {}),
                logistics=concept.get("logistics"), venues=concept.get("venues"),
            ) or {}
            checks = _outline_checks(output)
            passed = checks["passed"]
        else:
            raise SystemExit(f"No A/B runner for step {step!r} yet")
    finally:
        db.close()
    return {"arm": arm, "seconds": round(time.monotonic() - started, 1), "passed": passed, "checks": checks,
            "output": output}


def judge(provider, step: str, plan: dict[str, Any], a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Blind pairwise verdict; returns the winning arm name, or 'tie'."""
    pair = [a, b]
    random.shuffle(pair)
    response = provider.complete(
        [LLMMessage(role="user", content=json.dumps({
            "step": step, "criteria": CRITERIA[step], "plan": plan,
            "X": pair[0]["output"], "Y": pair[1]["output"],
        }, ensure_ascii=False))],
        system=load_prompt("ab_judge"), max_tokens=1200, temperature=0.0,
    )
    verdict = extract_json_block(response.text) or {}
    winner = {"X": pair[0]["arm"], "Y": pair[1]["arm"]}.get(str(verdict.get("winner")), "tie")
    return {"winner": winner, "margin": verdict.get("margin"), "reasons": verdict.get("reasons") or [],
            "problems": {pair[0]["arm"]: verdict.get("x_problems") or [], pair[1]["arm"]: verdict.get("y_problems") or []}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--step", required=True, choices=sorted(STEP_ROLES))
    parser.add_argument("--arms", default="default,claude-sonnet-5-5", help="two arms: 'default' or a model id")
    parser.add_argument("--project", type=int, default=None)
    parser.add_argument("--runs", type=int, default=1)
    args = parser.parse_args()
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    if len(arms) != 2:
        raise SystemExit("--arms takes exactly two arms")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    settings = load_settings(require_slack=False)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    source = Database(settings.database_path)
    try:
        project = _pick_project(source, args.project)
        concept = source.project_state(int(project["id"])).get("concept") or {}
    finally:
        source.close()
    draft = concept.get("sample_draft") or {}
    plan = {
        "concept": {k: (concept.get("editorial") or {}).get(k) for k in (
            "editorial_proposition", "strongest_experience", "possible_story_structure")},
        "critic": {k: (concept.get("critic_result") or {}).get(k) for k in ("verdict", "revision_instructions")},
        "outline": concept.get("outline") if args.step != "outline" else None,
        "draft": {k: str(draft.get(k) or "")[:6000] for k in ("headline", "body")} if draft.get("body") else None,
    }
    judge_provider = build_provider(settings)
    runs: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="suite-stories-ab-") as temp:
        for number in range(1, args.runs + 1):
            results = []
            for arm in arms:
                print(f"run {number}: {args.step} with {arm} ...", flush=True)
                results.append(run_arm(settings, args.step, arm, project, Path(temp)))
                print(f"  {results[-1]['seconds']}s, checks {'passed' if results[-1]['passed'] else 'FAILED'}", flush=True)
            verdict = judge(judge_provider, args.step, plan, *results) if all(r["output"] for r in results) else {
                "winner": "tie", "reasons": ["an arm produced no output"]}
            print(f"  judge: {verdict['winner']} ({verdict.get('margin')})", flush=True)
            runs.append({"run": number, "arms": results, "judge": verdict})
    summary = {
        arm: {
            "mean_seconds": round(sum(r["seconds"] for run in runs for r in run["arms"] if r["arm"] == arm) / len(runs), 1),
            "checks_passed": sum(1 for run in runs for r in run["arms"] if r["arm"] == arm and r["passed"]),
            "judge_wins": sum(1 for run in runs if run["judge"]["winner"] == arm),
        } for arm in arms
    }
    summary["ties"] = sum(1 for run in runs if run["judge"]["winner"] == "tie")
    report = {"step": args.step, "project_id": project["id"], "arms": arms, "runs": runs, "summary": summary}
    output = ROOT / "data" / f"ab_{args.step}_{stamp}.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(f"Report: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
