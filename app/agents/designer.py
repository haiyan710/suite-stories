"""Visual direction specialist; no artifact-write permission."""

from __future__ import annotations

import json

from ..llm.base import LLMMessage, LLMProvider
from ..llm.zai_glm import extract_json_block
from ..orchestration.handoffs import DesignSpec, DesignTask
from ..orchestration.planning import compact_research_json
from ..prompts import load_prompt


class DesignAgent:
    name = "designer"

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider

    def run(self, task: DesignTask) -> DesignSpec:
        prompt = (
            "Create or revise the visual direction.\n"
            + ("ORCHESTRATOR TASK BRIEF:\n" + task.brief.to_prompt() + "\n" if task.brief else "")
            + "CONCEPT:\n"
            + json.dumps(task.concept, ensure_ascii=False)[:8000]
            + "\nRESEARCH:\n" + compact_research_json(task.research or {}, budget_chars=30000)
            + "\nASSETS:\n" + json.dumps(task.assets, ensure_ascii=False)[:8000]
            + "\nPREVIOUS DESIGN:\n" + json.dumps(task.previous_design, ensure_ascii=False)[:6000]
            + "\nUSER FEEDBACK:\n" + task.user_feedback[:3000]
        )
        response = self._provider.complete(
            [LLMMessage(role="user", content=prompt)], system=load_prompt("designer"),
            max_tokens=4200, temperature=0.55,
        )
        data = extract_json_block(response.text)
        if not isinstance(data, dict):
            raise RuntimeError("DesignAgent returned an unstructured result")
        data.setdefault("editorial_objective", task.concept.get("editorial_proposition", ""))
        data.setdefault("page_narrative", [])
        data.setdefault("visual_grammar", "")
        data.setdefault("section_structure", [])
        data.setdefault("image_roles", [])
        data.setdefault("typography_direction", "")
        data.setdefault("spacing_and_rhythm", "")
        data.setdefault("interaction_direction", "")
        data.setdefault("responsive_notes", "")
        data.setdefault("asset_mapping", [])
        data.setdefault("things_to_avoid", [])
        data.setdefault("builder_instructions", [])
        result = DesignSpec.from_dict(data)
        result.version = int(task.previous_design.get("version", 0) or 0) + 1
        return result
