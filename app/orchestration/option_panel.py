"""A panel of independent judges ranks a thread's options (A-D) and says what to borrow.

Each option already passed the editorial critic on its own; nothing compared them.
Three judges, each with one lens (the booking traveller, the evidence and plan,
the editor), score every option 1-10 without seeing each other's scores. Python
averages them, ranks the options, notes when the judges split, and keeps the
judges' suggestions of what the winner could take from another option, which
becomes a ready "combine" request. A judge that fails is skipped; with fewer
than two answers there is no ranking.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any, Sequence

from ..agent import AgentCancelled
from ..llm.base import LLMMessage, LLMProvider
from ..llm.zai_glm import extract_json_block
from ..prompts import load_prompt

logger = logging.getLogger(__name__)

JUDGES: tuple[tuple[str, str], ...] = (
    ("booking", "The Korean luxury traveller deciding where to book: which option gives clearer, "
                "property-specific reasons to stay at this hotel and a trip they can picture and want?"),
    ("evidence", "The fact-checker and trip planner: which option's central claims rest on stronger evidence, "
                 "with fewer [TK]/unknowns, and a plan that is realistic in time, travel and season?"),
    ("editorial", "The editor-in-chief: which angle is more distinctive (it would not survive swapping the "
                  "hotel name), fresher against recent concepts, with a stronger title and opening?"),
)
MIN_JUDGES = 2


@dataclass(frozen=True)
class PanelResult:
    ranking: list[dict[str, Any]]  # [{label, title, mean, scores: {lens: n}, strengths, weaknesses}], best first
    recommended: str
    votes: dict[str, str]  # lens -> best label
    take: list[dict[str, str]] = field(default_factory=list)  # [{from, what}] for the recommended option
    judges: int = 0

    @property
    def split(self) -> bool:
        return len(set(self.votes.values())) > 1

    def combine_request(self) -> str:
        """A "combine" request for the recommended option plus what to take from others ("" if none)."""
        if not self.take:
            return ""
        labels = [self.recommended, *dict.fromkeys(t["from"] for t in self.take)]
        parts = "; ".join(f"from {t['from']}: {t['what']}" for t in self.take)
        return f"combine {' and '.join(labels)}: keep {self.recommended} as the base; {parts}"

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "split": self.split, "combine_request": self.combine_request()}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "PanelResult | None":
        if not data or not data.get("ranking"):
            return None
        return cls(
            ranking=list(data["ranking"]), recommended=str(data.get("recommended") or ""),
            votes=dict(data.get("votes") or {}), take=list(data.get("take") or []),
            judges=int(data.get("judges") or 0),
        )


def option_digest(option: dict[str, Any]) -> dict[str, Any]:
    """What a judge sees of one saved option: the concept, the critic's view and the outline."""
    editorial = option.get("editorial") or {}
    critic = option.get("critic_result") or {}
    outline = option.get("outline") or {}
    return {
        "label": option.get("label"),
        "title": option.get("title") or outline.get("title") or editorial.get("working_title"),
        "concept": {k: str(editorial.get(k) or "")[:900] for k in (
            "editorial_proposition", "strongest_experience", "why_someone_would_want_it",
            "why_this_property_enables_it", "possible_story_structure",
        )},
        "critic": {"verdict": critic.get("verdict"), "reasons": (critic.get("reasons") or [])[:4]},
        "outline": json.dumps(outline, ensure_ascii=False)[:2500],
        "combined_from": option.get("combined_from") or [],
    }


def _judge(
    provider: LLMProvider, lens: str, question: str, digests: list[dict[str, Any]], evidence: str,
    context: dict[str, Any],
) -> dict[str, Any] | None:
    try:
        response = provider.complete(
            [LLMMessage(role="user", content=json.dumps({
                "lens": lens, "question": question, "options": digests, "context": context, "evidence": evidence,
            }, ensure_ascii=False))],
            system=load_prompt("option_judge"), max_tokens=1500, temperature=0.0,
        )
    except AgentCancelled:
        raise
    except Exception:
        logger.warning("Option judge %s failed", lens, exc_info=True)
        return None
    data = extract_json_block(response.text)
    labels = {d["label"] for d in digests}
    if not isinstance(data, dict):
        return None
    scores = {}
    for row in data.get("scores") or []:
        if not isinstance(row, dict) or str(row.get("label")) not in labels:
            continue
        try:
            scores[str(row["label"])] = (max(1, min(10, int(row.get("score")))), row)
        except (TypeError, ValueError):
            continue
    if set(scores) != labels:
        return None
    best = str(data.get("best") or "")
    if best not in labels:
        best = max(scores, key=lambda label: scores[label][0])
    take = [
        {"from": str(t.get("from")), "what": str(t.get("what") or "").strip()[:240]}
        for t in data.get("take") or []
        if isinstance(t, dict) and str(t.get("from")) in labels and str(t.get("what") or "").strip()
    ]
    return {"scores": scores, "best": best, "take": take}


def judge_options(
    provider: LLMProvider, options: Sequence[dict[str, Any]], *, evidence: str, context: dict[str, Any],
    judges: Sequence[tuple[str, str]] = JUDGES,
) -> PanelResult | None:
    """Rank saved options with independent judges; None with fewer than two options or answers."""
    digests = [option_digest(o) for o in options if o.get("label")]
    if len(digests) < 2:
        return None
    with ThreadPoolExecutor(max_workers=len(judges)) as pool:
        answers = list(pool.map(
            lambda j: (j[0], _judge(provider, j[0], j[1], digests, evidence, context)), judges,
        ))
    answers = [(lens, a) for lens, a in answers if a]
    if len(answers) < MIN_JUDGES:
        return None
    ranking = []
    for digest in digests:
        label = digest["label"]
        rows = {lens: a["scores"][label] for lens, a in answers}
        ranking.append({
            "label": label, "title": str(digest.get("title") or ""),
            "mean": round(sum(score for score, _ in rows.values()) / len(rows), 1),
            "scores": {lens: score for lens, (score, _) in rows.items()},
            "strengths": [str(r.get("strength") or "")[:200] for _, r in rows.values() if r.get("strength")],
            "weaknesses": [str(r.get("weakness") or "")[:200] for _, r in rows.values() if r.get("weakness")],
        })
    votes = {lens: a["best"] for lens, a in answers}
    vote_count = {label: list(votes.values()).count(label) for label in (d["label"] for d in digests)}
    ranking.sort(key=lambda r: (-r["mean"], -vote_count[r["label"]], r["label"]))
    recommended = ranking[0]["label"]
    take: list[dict[str, str]] = []
    for _lens, answer in answers:
        for item in answer["take"]:
            if item["from"] != recommended and item["from"] not in {t["from"] for t in take}:
                take.append(item)
    return PanelResult(ranking=ranking, recommended=recommended, votes=votes, take=take[:2], judges=len(answers))


def format_panel(panel: PanelResult) -> str:
    """The panel's verdict as a short Slack block."""
    lenses = {"booking": "traveller", "evidence": "evidence", "editorial": "editor"}
    lines = ["*The judges' pick*"]
    for row in panel.ranking:
        scores = ", ".join(f"{lenses.get(k, k)} {v}" for k, v in row["scores"].items())
        lines.append(f"{row['label']} {row['mean']}/10 ({scores}): {row['title'] or 'untitled'}")
    best = panel.ranking[0]
    lines.append(
        f"Recommended: *option {panel.recommended}*"
        + (f": {best['strengths'][0]}" if best["strengths"] else "")
        + (" The judges split: " + ", ".join(f"{lenses.get(k, k)} → {v}" for k, v in panel.votes.items()) + "."
           if panel.split else "")
    )
    if best["weaknesses"]:
        lines.append(f"Watch: {best['weaknesses'][0]}")
    for item in panel.take:
        lines.append(f"Worth taking from {item['from']}: {item['what']}")
    if panel.take:
        lines.append("_Say \"combine as recommended\" to build that version, or \"use option X\" to pick another._")
    return "\n".join(lines)
