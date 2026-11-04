#!/usr/bin/env python3
"""Interactive CLI for the hotel research agent (no Slack needed).

Usage:
    python scripts/chat_cli.py

Reads ZAI_API_KEY from the environment or .env. Great for testing the
full pipeline (including live search) before wiring Slack up.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent import AgentError  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.llm import build_provider  # noqa: E402
from app.orchestration.orchestrator import EditorialOrchestrator  # noqa: E402
from app.storage import Database  # noqa: E402

PROGRESS = {
    "start": "… starting research",
    "official": "… researching official property sources",
    "coverage": "… checking our publication's coverage + destination context",
    "validate": "… validating territories against evidence",
    "compare": "… comparing against previous concepts",
}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        settings = load_settings(require_slack=False)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    provider = build_provider(settings)
    db = Database(settings.database_path)
    agent = EditorialOrchestrator(
        settings, provider, db, research_provider=build_provider(settings, role="research"),
    )
    agent.add_progress_listener(lambda stage: print(f"\n[progress] {PROGRESS.get(stage, stage)}"))

    print(f"hotel-research CLI (provider: {settings.llm_provider}, db: {settings.database_path})")
    print("Type a request, or 'exit' to quit. Example:")
    print("  research Park Hyatt Bangkok and plan a 3-night stay for a couple\n")

    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line.lower() in {"exit", "quit"}:
            break
        try:
            history = db.thread_history("cli:session")
            outcome = agent.handle_message(line, user_id="cli", thread_key="cli:session", history=history)
            if outcome.clarification:
                print(f"\n🤔 {outcome.clarification}\n")
                continue
            print("\n" + outcome.markdown + "\n")
            db.append_thread_message("cli:session", "user", line)
            db.append_thread_message("cli:session", "assistant", outcome.markdown[:4000])
        except AgentError as exc:
            print(f"\n⚠ {exc}\n")

    provider.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
