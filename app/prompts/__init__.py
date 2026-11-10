"""Prompt loader: methodology lives in editable markdown files."""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_FILES = {
    "triage": "triage.md",
    "methodology": "hotel_research.md",
    "critic": "concept_critic.md",
    "final": "final_response.md",
    "orchestrator": "orchestrator.md",
    "researcher": "researcher.md",
    "editorial": "editorial.md",
    "designer": "designer.md",
    "builder": "builder.md",
    "critic_editorial": "critic_editorial.md",
    "critic_visual": "critic_visual.md",
    "outline": "outline.md",
    "critique_summary": "critique_summary.md",
    "writer": "writer.md",
    "critic_draft": "critic_draft.md",
    "layout_images": "layout_images.md",
    "dining_picks": "dining_picks.md",
    "venue_notes": "venue_notes.md",
    "guest_experience": "guest_experience.md",
    "humanizer": "humanizer.md",
    "draft_fixes": "draft_fixes.md",
    "logistics_extract": "logistics_extract.md",
}


@lru_cache(maxsize=None)
def load_prompt(name: str, prompts_dir: Path | None = None) -> str:
    """Load a named prompt file. Edit the .md to change methodology."""
    filename = _FILES.get(name)
    if filename is None:
        raise KeyError(f"unknown prompt: {name!r} (available: {sorted(_FILES)})")
    base = prompts_dir or Path(__file__).resolve().parent
    text = (base / filename).read_text(encoding="utf-8")
    logger.debug("loaded prompt %s (%d chars)", name, len(text))
    return text
