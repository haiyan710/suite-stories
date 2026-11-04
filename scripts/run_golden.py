#!/usr/bin/env python3
"""Run the non-leaking live Kobe planning/editorial golden regression."""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings  # noqa: E402
from app.llm import build_provider  # noqa: E402
from app.orchestration.golden import evaluate_golden_result  # noqa: E402
from app.orchestration.orchestrator import EditorialOrchestrator  # noqa: E402
from app.slack_dns import install_slack_socket_dns_fallback  # noqa: E402
from app.storage import Database  # noqa: E402


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    fixture = json.loads((ROOT / "tests/fixtures/golden_case.json").read_text(encoding="utf-8"))
    settings = load_settings(require_slack=False)
    install_slack_socket_dns_fallback()
    stamp = time.strftime("%Y%m%d_%H%M%S")

    with tempfile.TemporaryDirectory(prefix="suite-stories-golden-") as temp:
        isolated = replace(
            settings,
            database_path=Path(temp) / "golden.db",
            artifacts_path=Path(temp) / "artifacts",
        )
        provider = build_provider(isolated)
        db = Database(isolated.database_path)
        orchestrator = EditorialOrchestrator(
            isolated, provider, db, research_provider=build_provider(isolated, role="research"),
        )
        thread_key = f"golden:{stamp}"
        started = time.monotonic()
        orchestrator.add_progress_listener(
            lambda stage: print(f"[{time.monotonic() - started:6.1f}s] {stage}", flush=True)
        )
        try:
            outcome = orchestrator.handle_message(
                fixture["request"], user_id="golden", thread_key=thread_key,
                channel_id="golden", thread_ts=stamp,
            )
            row = db.project_for_thread(thread_key)
            if row is None:
                raise RuntimeError("No project was created")
            state = db.project_state(int(row["id"]))
            concept = state.get("concept") or {}
            editorial = concept.get("editorial") or {}
            critic = concept.get("critic_result") or {}
            evaluation = evaluate_golden_result(
                editorial=editorial, research=state.get("research") or {}, critic=critic,
                pass_threshold=int(fixture["pass_threshold"]),
                property_terms=tuple(fixture["property_terms"]),
                destination_terms=tuple(fixture["destination_terms"]),
            )
            report = {
                "case": fixture["case"],
                "model": isolated.glm_model,
                "elapsed_seconds": round(time.monotonic() - started, 1),
                "research_plan": orchestrator.last_plan.to_dict() if orchestrator.last_plan else {},
                "specialist_sequence": orchestrator.last_trace,
                "briefs": [brief.to_dict() for brief in orchestrator.last_briefs],
                "final_recommended_experience": editorial,
                "critic_result": critic,
                "evaluation": evaluation.to_dict(),
                "response": outcome.markdown,
            }
            output = ROOT / "data" / f"golden_case_{stamp}.json"
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(report, ensure_ascii=False, indent=2))
            print(f"\nReport: {output}")
            return 0 if evaluation.passed and concept.get("status") == "shortlisted" else 1
        finally:
            provider.close()
            db.close()


if __name__ == "__main__":
    raise SystemExit(main())
