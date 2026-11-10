"""Sample-draft writer: turns an approved concept + outline into a draft article."""

from __future__ import annotations

import json
import logging
import re

from ..artifacts.layout import strip_direction_notes
from ..llm.base import LLMMessage, LLMProvider
from ..llm.zai_glm import extract_json_block
from ..orchestration.handoffs import DraftResult, DraftTask
from ..orchestration.planning import compact_research_json
from ..prompts import load_prompt

logger = logging.getLogger(__name__)

# An editor's note asking the hotel contact for something ("[ASK ANNA: ...]").
ASK_NOTE = r"\[ASK\b[^\]]*\]"


def drop_echo_paragraphs(body: str) -> str:
    """Remove a stray paragraph whose sentences all already appear earlier in the piece.

    Models sometimes leave a copy of an intro sentence on its own line between days.
    """
    kept: list[str] = []
    for paragraph in re.split(r"\n\s*\n", body):
        text = paragraph.strip()
        sentences = [x for x in re.split(r"(?<=[.!?])\s+", text) if x]
        earlier = "\n".join(kept)
        if text and not text.startswith(("#", "**", "_")) and sentences and all(x in earlier for x in sentences):
            continue
        kept.append(paragraph)
    return "\n\n".join(kept).strip()


class WriterAgent:
    name = "writer"

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider

    def run(self, task: DraftTask) -> DraftResult:
        prompt = (
            ("ORCHESTRATOR TASK BRIEF:\n" + task.brief.to_prompt() + "\n" if task.brief else "")
            + f"HOTEL: {task.hotel_name}\nUSER REQUEST: {task.user_request}\n"
            + f"FORMAT: {json.dumps(task.format or {'type': 'single_experience'})}\n"
            + "CONCEPT:\n" + json.dumps(task.concept, ensure_ascii=False)[:8000]
            + "\nOUTLINE:\n" + json.dumps(task.outline, ensure_ascii=False)[:9000]
            + "\nCONCEPT CRITIC REVISIONS:\n" + json.dumps(task.concept_revisions, ensure_ascii=False)[:4000]
            + "\nEVIDENCE:\n" + compact_research_json(
                task.research, budget_chars=24000,
                focus_text=json.dumps([task.concept, task.outline], ensure_ascii=False),
            )
            + (
                "\nPREVIOUS DRAFT:\n" + json.dumps(task.previous_draft, ensure_ascii=False)[:9000]
                + "\nQA REVISIONS TO APPLY:\n" + json.dumps(task.qa_revisions, ensure_ascii=False)
                if task.previous_draft else ""
            )
        )
        response = self._provider.complete(
            [LLMMessage(role="user", content=prompt)], system=load_prompt("writer"),
            max_tokens=4000, temperature=0.6,
        )
        data = extract_json_block(response.text)
        if not isinstance(data, dict) or not str(data.get("body") or "").strip():
            return DraftResult(status="failed", writer_notes="The writer returned no usable draft.")
        body = drop_echo_paragraphs(strip_direction_notes(str(data.get("body") or "")))
        return DraftResult(
            status="complete",
            headline=str(data.get("headline") or ""),
            standfirst=str(data.get("standfirst") or ""),
            body=body,
            # The body is the source of truth; the model's own list drifted from it.
            tk_items=re.findall(r"\[TK[^\]]*\]", body),
            ask_items=re.findall(ASK_NOTE, body),
            source_notes=[n for n in data.get("source_notes") or [] if isinstance(n, dict)][:40],
            writer_notes=str(data.get("writer_notes") or ""),
            model=response.model,
        )

    def humanize(self, draft: DraftResult, *, piece_format: dict | None = None) -> DraftResult:
        """Rewrite the draft once to remove AI-writing tells (humanizer.md).

        The rewrite must keep the structure the layout and QA rely on: every [TK],
        every subhead in order, and roughly the same length. Otherwise the original
        draft is kept.
        """
        payload = {
            "format": piece_format or {"type": "single_experience"},
            "headline": draft.headline, "standfirst": draft.standfirst, "body": draft.body,
        }
        try:
            response = self._provider.complete(
                [LLMMessage(role="user", content=json.dumps(payload, ensure_ascii=False))],
                system=load_prompt("humanizer"), max_tokens=4000, temperature=0.4,
            )
        except Exception:
            logger.exception("humanizer call failed; keeping the draft as written")
            return draft
        data = extract_json_block(response.text)
        if not isinstance(data, dict):
            return draft
        body = drop_echo_paragraphs(strip_direction_notes(str(data.get("body") or "")))
        added = [str(x) for x in data.get("added_facts") or [] if str(x).strip()]
        problem = (
            f"rewrite admits added facts: {added[:3]}" if added
            else self._humanize_problem(draft.body, body)
        )
        if problem:
            logger.info("humanizer rewrite rejected: %s", problem)
            return draft
        headline = str(data.get("headline") or "").strip() or draft.headline
        if " | " in draft.headline and " | " not in headline:
            headline = draft.headline
        return DraftResult(
            status="complete", headline=headline,
            standfirst=str(data.get("standfirst") or "").strip() or draft.standfirst,
            body=body, tk_items=re.findall(r"\[TK[^\]]*\]", body), ask_items=re.findall(ASK_NOTE, body),
            writer_notes=draft.writer_notes, model=draft.model,
        )

    def apply_fixes(self, draft: DraftResult, instructions: list[str]) -> tuple[DraftResult, int, list[str]]:
        """Apply the final QA instructions as exact find/replace edits (draft_fixes.md).

        Returns the edited draft, how many edits were applied and the instructions
        the model skipped. An edit is dropped unless its passage occurs exactly once,
        and subheads are never touched, so the layout's sections stay intact.
        """
        payload = {"headline": draft.headline, "standfirst": draft.standfirst, "body": draft.body,
                   "instructions": instructions}
        try:
            response = self._provider.complete(
                [LLMMessage(role="user", content=json.dumps(payload, ensure_ascii=False))],
                system=load_prompt("draft_fixes"), max_tokens=3000, temperature=0.0,
            )
        except Exception:
            logger.exception("draft fixes call failed; keeping the draft as written")
            return draft, 0, list(instructions)
        data = extract_json_block(response.text)
        if not isinstance(data, dict):
            return draft, 0, list(instructions)
        fields = {"headline": draft.headline, "standfirst": draft.standfirst, "body": draft.body}
        applied = 0
        for edit in data.get("edits") or []:
            if not isinstance(edit, dict) or edit.get("field") not in fields:
                continue
            name, find, new = edit["field"], str(edit.get("find") or ""), str(edit.get("replace") or "")
            text = fields[name]
            if not find.strip() or text.count(find) != 1 or "## " in find or "## " in new:
                logger.info("draft fix skipped (passage not found once): %r", find[:80])
                continue
            if name == "headline" and " | " in text and " | " not in new:
                continue
            fields[name] = text.replace(find, new)
            applied += 1
        body = drop_echo_paragraphs(re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+\n", "\n", fields["body"]))).strip()
        skipped = [str(x) for x in data.get("skipped") or [] if str(x).strip()]
        fixed = DraftResult(
            status="complete", headline=fields["headline"].strip(), standfirst=fields["standfirst"].strip(),
            body=body, tk_items=re.findall(r"\[TK[^\]]*\]", body), ask_items=re.findall(ASK_NOTE, body),
            writer_notes=draft.writer_notes, model=draft.model,
        )
        return fixed, applied, skipped

    @staticmethod
    def _humanize_problem(original: str, rewritten: str) -> str:
        if not rewritten.strip():
            return "empty body"
        if sorted(re.findall(r"\[TK[^\]]*\]", original)) != sorted(re.findall(r"\[TK[^\]]*\]", rewritten)):
            return "TK placeholders changed"
        if sorted(re.findall(ASK_NOTE, original)) != sorted(re.findall(ASK_NOTE, rewritten)):
            return "ASK notes changed"
        heads = lambda text: [line.strip() for line in text.splitlines() if line.strip().startswith("## ")]
        if heads(original) != heads(rewritten):
            return "subheads changed"
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", rewritten) if p.strip()]
        if len(paragraphs) != len(set(paragraphs)) or any(
            p != q and p in q for p in paragraphs if len(p.split()) < 12 for q in paragraphs
        ):
            return "repeated paragraph"
        before, after = len(original.split()), len(rewritten.split())
        if before and not 0.75 <= after / before <= 1.2:
            return f"length changed {before}->{after} words"
        return ""
