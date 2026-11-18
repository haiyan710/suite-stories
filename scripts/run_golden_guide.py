#!/usr/bin/env python3
"""Live golden run of the Korean hotel guide side flow on a copy of a real thread.

Copies the bot's database with sqlite backup() (the live DB is never written),
runs "hotel guide" through the orchestrator on the chosen project's thread, and
scores the stored `concept_state.guide_ko` with `evaluate_guide` against
tests/fixtures/golden_guide.json. Uses the network (Claude CLI, Commons,
Open-Meteo) and costs usage; unit tests stay offline.

    .venv/bin/python scripts/run_golden_guide.py              # latest draftable project
    .venv/bin/python scripts/run_golden_guide.py --project 12
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings  # noqa: E402
from app.llm import build_provider  # noqa: E402
from app.orchestration.orchestrator import EditorialOrchestrator  # noqa: E402
from app.side_flows.guide_eval import evaluate_guide  # noqa: E402
from app.states import DRAFTABLE_CONCEPTS  # noqa: E402
from app.storage import Database  # noqa: E402


def _pick_project(db: Database, wanted: int | None) -> dict:
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", type=int, default=None, help="project id (default: latest draftable)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    fixture = json.loads((ROOT / "tests/fixtures/golden_guide.json").read_text(encoding="utf-8"))
    settings = load_settings(require_slack=False)
    stamp = time.strftime("%Y%m%d_%H%M%S")

    with tempfile.TemporaryDirectory(prefix="suite-stories-golden-guide-") as temp:
        copy = Path(temp) / "golden.db"
        source = sqlite3.connect(settings.database_path)
        target = sqlite3.connect(copy)
        source.backup(target)
        source.close()
        target.close()
        isolated = replace(settings, database_path=copy, artifacts_path=Path(temp) / "artifacts")
        provider = build_provider(isolated)
        db = Database(copy)
        project = _pick_project(db, args.project)
        orchestrator = EditorialOrchestrator(
            isolated, provider, db, research_provider=build_provider(isolated, role="research"),
        )
        started = time.monotonic()
        orchestrator.add_progress_listener(
            lambda stage: print(f"[{time.monotonic() - started:6.1f}s] {stage}", flush=True)
        )
        # Projects store the thread key ("<channel>:<ts>") in slack_thread_ts.
        thread_key = project["slack_thread_ts"]
        try:
            outcome = orchestrator.handle_message(
                fixture["request"], user_id="golden", thread_key=thread_key,
                channel_id=project["slack_channel"], thread_ts=thread_key.rsplit(":", 1)[-1],
            )
            stored = (db.project_state(int(project["id"])).get("concept") or {}).get("guide_ko") or {}
            evaluation = evaluate_guide(stored, fixture)
            report = {
                "case": fixture["case"], "project_id": project["id"],
                "elapsed_seconds": round(time.monotonic() - started, 1),
                "evaluation": evaluation.to_dict(), "response": outcome.markdown,
                "guide": stored.get("guide"), "photo_credits": stored.get("photo_credits"),
                "refuted_fixes": stored.get("refuted_fixes"),
            }
        finally:
            getattr(provider, "close", lambda: None)()
            db.close()
    output = ROOT / "data" / f"golden_guide_{stamp}.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for check in evaluation.checks:
        print(f"{'PASS' if check.passed else 'FAIL'}  {check.name:<20} {check.detail}")
    print(f"\nScore {evaluation.score}/{evaluation.total}, {'passed' if evaluation.passed else 'FAILED'}. Report: {output}")
    return 0 if evaluation.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
