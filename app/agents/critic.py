"""Editorial and visual critic with no research or write tools."""

from __future__ import annotations

import json

from ..llm.base import LLMMessage, LLMProvider
from ..llm.zai_glm import extract_json_block
from ..orchestration.handoffs import CritiqueResult, CritiqueTask
from ..orchestration.planning import compact_research_json
from ..prompts import load_prompt


class CriticAgent:
    name = "critic"

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider

    def run(self, task: CritiqueTask) -> CritiqueResult:
        prompt = (
            ("ORCHESTRATOR TASK BRIEF:\n" + task.brief.to_prompt() + "\n" if task.brief else "")
            + f"MODE={task.mode}\nCONCEPT:\n{json.dumps(task.concept, ensure_ascii=False)[:9000]}"
            f"\nRESEARCH:\n{compact_research_json(
                task.research or {}, budget_chars=30000,
                focus_text=json.dumps([task.concept, task.draft], ensure_ascii=False),
            )}"
            f"\nDESIGN:\n{json.dumps(task.design_spec, ensure_ascii=False)[:9000]}"
            # HTML is often 15–25 KB. Truncating it made the critic report
            # missing closing tags and sections that were present later in the
            # file, so preserve the full MVP artifact plus validation metadata.
            f"\nARTIFACT:\n{json.dumps(task.artifact, ensure_ascii=False)[:32000]}"
            f"\nCONCEPT MEMORY:\n{json.dumps(task.concept_memory, ensure_ascii=False)[:7000]}"
            + (
                f"\nOUTLINE:\n{json.dumps(task.outline, ensure_ascii=False)[:9000]}"
                f"\nDRAFT:\n{json.dumps(task.draft, ensure_ascii=False)[:12000]}"
                if task.mode == "DRAFT" else ""
            )
        )
        prompt_name = {
            "EDITORIAL": "critic_editorial", "VISUAL": "critic_visual", "DRAFT": "critic_draft",
        }.get(task.mode, "critic_visual")
        response = self._provider.complete(
            [LLMMessage(role="user", content=prompt)], system=load_prompt(prompt_name),
            max_tokens=2600, temperature=0.2,
        )
        data = extract_json_block(response.text)
        if not isinstance(data, dict):
            return CritiqueResult(
                verdict="PASS_WITH_REVISIONS",
                reasons=["Critic response could not be parsed."],
                unresolved_concerns=["Manual review required."],
            )
        verdict = str(data.get("verdict", "PASS_WITH_REVISIONS")).upper()
        if verdict not in {"PASS", "PASS_WITH_REVISIONS", "FAIL"}:
            verdict = "PASS_WITH_REVISIONS"
        return CritiqueResult(
            verdict=verdict, reasons=list(data.get("reasons") or []),
            revision_instructions=list(data.get("revision_instructions") or []),
            unresolved_concerns=list(data.get("unresolved_concerns") or []),
            evidence_matches=list(data.get("evidence_matches") or []),
            fail_reason=str(data.get("fail_reason") or ""),
            polish=[str(x) for x in data.get("polish") or [] if str(x).strip()][:4],
        )
