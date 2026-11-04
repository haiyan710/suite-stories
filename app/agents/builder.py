"""Static HTML implementation specialist; only this agent writes artifacts."""

from __future__ import annotations

import json
import re

from ..artifacts import ArtifactManager
from ..llm.base import LLMMessage, LLMProvider
from ..llm.zai_glm import extract_json_block
from ..orchestration.handoffs import ArtifactResult, BuildTask
from ..prompts import load_prompt


class BuilderAgent:
    name = "builder"

    def __init__(self, provider: LLMProvider, artifacts: ArtifactManager) -> None:
        self._provider = provider
        self._artifacts = artifacts

    def run(self, task: BuildTask) -> ArtifactResult:
        prompt = (
            "Build one standalone static index.html. Put maintainable CSS in a <style> element "
            "and minimal JavaScript, only if useful, in a <script> element. Keep the output "
            "compact enough to finish.\n"
            + ("ORCHESTRATOR TASK BRIEF:\n" + task.brief.to_prompt() + "\n" if task.brief else "")
            + "CONCEPT:\n" + json.dumps(task.concept, ensure_ascii=False)[:7000]
            + "\nDESIGN SPEC:\n" + json.dumps(task.design_spec.to_dict(), ensure_ascii=False)[:14000]
            + "\nASSETS:\n" + json.dumps(task.assets, ensure_ascii=False)[:8000]
            + "\nPREVIOUS ARTIFACT:\n" + json.dumps(task.previous_artifact, ensure_ascii=False)[:4000]
            + "\nCONSTRAINTS:\n" + json.dumps(task.technical_constraints)
        )
        response = self._provider.complete(
            [LLMMessage(role="user", content=prompt)], system=load_prompt("builder"),
            max_tokens=16000, temperature=0.25,
        )
        html, css, js, notes = _extract_build_output(response.text)
        if not html or "</html>" not in html.lower():
            raise RuntimeError("BuilderAgent did not return valid HTML")
        return self._artifacts.create_html(
            project_id=task.project_id, hotel_slug=task.hotel_slug,
            html=html, css=css, js=js, source_design_version=task.design_spec.version,
            notes=notes,
        )


_FENCED_HTML_RE = re.compile(r"```(?:html)?\s*(<!doctype\s+html\b.*?</html>)\s*```", re.I | re.S)


def _extract_build_output(text: str) -> tuple[str, str, str, list[str]]:
    """Accept compact standalone HTML, with legacy JSON kept for compatibility."""
    data = extract_json_block(text)
    if isinstance(data, dict) and str(data.get("html", "")).strip():
        return (
            str(data["html"]).strip(), str(data.get("css", "")).strip(),
            str(data.get("js", "")).strip(), [str(n) for n in data.get("notes", [])],
        )
    match = _FENCED_HTML_RE.search(text)
    if match:
        return match.group(1).strip(), "", "", ["Standalone HTML with inline CSS/JS."]
    start = text.lower().find("<!doctype html")
    end = text.lower().rfind("</html>")
    if start >= 0 and end >= start:
        return text[start : end + len("</html>")].strip(), "", "", [
            "Standalone HTML with inline CSS/JS."
        ]
    return "", "", "", []
