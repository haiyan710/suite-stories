"""Prompt loader: methodology lives in editable markdown files."""

from __future__ import annotations

import logging
import re
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
    "guide_ko": "guide_ko.md",
    "guide_ko_check": "guide_ko_check.md",
    "guide_journey": "guide_journey.md",
    "guide_place_photos": "guide_place_photos.md",
    "guide_place_photos_look": "guide_place_photos_look.md",
    "lesson_generalize": "lesson_generalize.md",
    "verify_fixes": "verify_fixes.md",
    "option_judge": "option_judge.md",
    "ab_judge": "ab_judge.md",
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


_JSON_CUE = re.compile(r"return (?:only )?json|json only|answer in json|reply with json", re.I)


def prompt_json_example(name: str) -> str:
    """The JSON example a prompt asks the model to return ("" when it has none).

    The first balanced {...} block after the "Return JSON" cue; the contract tests
    and the A/B script read the keys the prompt promises from it.
    """
    text = load_prompt(name)
    cue = _JSON_CUE.search(text)
    start = text.find("{", cue.end() if cue else 0)
    if start < 0:
        return ""
    depth = 0
    in_string = False
    for index in range(start, len(text)):
        char = text[index]
        if char == '"' and text[index - 1] != "\\":
            in_string = not in_string
        elif not in_string and char == "{":
            depth += 1
        elif not in_string and char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return ""


def prompt_json_keys(name: str) -> set[str]:
    """Every key (top-level and nested) in the prompt's JSON example."""
    return set(re.findall(r'"([A-Za-z_][\w-]*)"\s*:', prompt_json_example(name)))
