"""Adversarial check of a checker's suggested fixes before they are applied.

A critic (the draft QA, the Korean guide's check) lists fixes and the flow used
to apply them all. Some are plausible but wrong: removing a fact the evidence
supports, undoing a correction the team made. One fast call takes the other
side on each fix; a refuted fix is dropped (and listed in the reply), confirmed
and uncertain ones are applied. Fixes computed in Python (dates, banned phrases,
structure) are facts and never go through here. On any error every fix is kept.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from ..agent import AgentCancelled
from ..llm.base import LLMMessage, LLMProvider
from ..llm.zai_glm import extract_json_block
from ..prompts import load_prompt

logger = logging.getLogger(__name__)


@dataclass
class FixCheck:
    kept: list[str]
    refuted: list[dict[str, str]] = field(default_factory=list)  # {"fix", "reason"}


def verify_fixes(
    provider: LLMProvider, fixes: list[str], *, text: str, evidence: str, context: dict[str, Any],
) -> FixCheck:
    """Keep the fixes that are not refuted; refuted ones come back with the reason."""
    fixes = [str(f) for f in fixes if str(f).strip()]
    if not fixes:
        return FixCheck(kept=[])
    try:
        response = provider.complete(
            [LLMMessage(role="user", content=json.dumps({
                "text": text, "fixes": [{"i": i, "fix": f} for i, f in enumerate(fixes)],
                "evidence": evidence, "context": context,
            }, ensure_ascii=False))],
            system=load_prompt("verify_fixes"), max_tokens=2000, temperature=0.0,
        )
        data = extract_json_block(response.text)
    except AgentCancelled:
        raise
    except Exception:
        logger.warning("Fix verification failed; keeping every fix", exc_info=True)
        return FixCheck(kept=fixes)
    verdicts = data.get("verdicts") if isinstance(data, dict) else None
    if not isinstance(verdicts, list):
        return FixCheck(kept=fixes)
    refuted: dict[int, str] = {}
    for verdict in verdicts:
        if not isinstance(verdict, dict) or str(verdict.get("status")).lower() != "refuted":
            continue
        try:
            index = int(verdict.get("i"))
        except (TypeError, ValueError):
            continue
        reason = str(verdict.get("reason") or "").strip()
        if 0 <= index < len(fixes) and reason:  # a refutation needs a reason
            refuted[index] = reason
    return FixCheck(
        kept=[f for i, f in enumerate(fixes) if i not in refuted],
        refuted=[{"fix": fixes[i], "reason": refuted[i]} for i in sorted(refuted)],
    )
