"""Experience-led editorial specialist; deliberately has no web tools."""

from __future__ import annotations

import json

from ..llm.base import LLMMessage, LLMProvider
from ..llm.zai_glm import extract_json_block
from ..orchestration.handoffs import EditorialResult, EditorialTask
from ..orchestration.planning import compact_research_json
from ..prompts import load_prompt


class EditorialAgent:
    name = "editorial"

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider

    def run(self, task: EditorialTask) -> EditorialResult:
        prompt = (
            "Develop the strongest editorial opportunity from this bounded context.\n"
            + ("ORCHESTRATOR TASK BRIEF:\n" + task.brief.to_prompt() + "\n" if task.brief else "")
            + f"HOTEL: {task.hotel_name}\nUSER GOAL: {task.user_goal}\n"
            f"USER FEEDBACK: {task.user_feedback}\nCONSTRAINTS: {json.dumps(task.constraints)}\n"
            "RESEARCH PACKAGE:\n" + compact_research_json(
                task.research.to_dict(), budget_chars=60000,
                focus_text=json.dumps(
                    [task.current_concept, task.brief.to_prompt() if task.brief else ""], ensure_ascii=False,
                ),
            )
            + "\nCURRENT CONCEPT:\n" + json.dumps(task.current_concept, ensure_ascii=False)[:14000]
            + "\nCONCEPT MEMORY:\n" + json.dumps(task.concept_memory, ensure_ascii=False)[:6000]
        )
        system = load_prompt("editorial") + "\n\n" + load_prompt("methodology")
        response = self._provider.complete(
            [LLMMessage(role="user", content=prompt)], system=system,
            max_tokens=4200, temperature=0.65,
        )
        data = extract_json_block(response.text)
        if not isinstance(data, dict):
            # Long itinerary answers sometimes come back as prose; ask once for the JSON.
            response = self._provider.complete(
                [
                    LLMMessage(role="user", content=prompt),
                    LLMMessage(role="assistant", content=response.text[:6000]),
                    LLMMessage(role="user", content="Return that answer as the required JSON object only."),
                ],
                system=system, max_tokens=4200, temperature=0.2,
            )
            data = extract_json_block(response.text)
        if not isinstance(data, dict):
            return EditorialResult(status="failed", open_questions=["Editorial response was not structured."])
        result = EditorialResult.from_dict(data)
        previous_version = int(task.current_concept.get("version", 0) or 0)
        result.version = previous_version + 1
        return result
