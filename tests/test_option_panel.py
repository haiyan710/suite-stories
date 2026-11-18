"""A panel of independent judges ranks saved options and suggests what to combine."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from app.llm.base import LLMResponse
from app.orchestration.option_panel import PanelResult, format_panel, judge_options
from test_orchestrator import _option_editorial, setup_orchestrator

OPTIONS = [
    {"label": "A", "title": "Alpha", "editorial": {"editorial_proposition": "Alpha proposition"}},
    {"label": "B", "title": "Beta", "editorial": {"editorial_proposition": "Beta proposition"}},
    {"label": "C", "title": "Gamma", "editorial": {"editorial_proposition": "Gamma proposition"}},
]


class Judges:
    """Answers judge prompts by lens; anything else goes to the wrapped provider."""

    def __init__(self, answers, inner=None):
        self.answers, self.inner, self.seen = answers, inner, []

    def complete(self, messages, *, system=None, **kwargs):
        if system and system.startswith("# Judge: compare"):
            payload = json.loads(messages[-1].content)
            self.seen.append(payload)
            answer = self.answers.get(payload["lens"])
            if isinstance(answer, Exception):
                raise answer
            return LLMResponse(text=json.dumps(answer) if answer is not None else "not json")
        return self.inner.complete(messages, system=system, **kwargs)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def _answer(scores, best, take=()):
    return {"scores": [{"label": k, "score": v, "strength": f"{k} strong", "weakness": f"{k} weak"}
                       for k, v in scores.items()],
            "best": best, "take": [{"from": f, "what": w} for f, w in take]}


def test_judges_rank_by_mean_and_keep_what_to_take():
    judges = Judges({
        "booking": _answer({"A": 6, "B": 9, "C": 5}, "B", [("C", "C's Day 2 market morning goes into B's Day 2.")]),
        "evidence": _answer({"A": 7, "B": 8, "C": 6}, "B"),
        "editorial": _answer({"A": 8, "B": 7, "C": 9}, "C", [("A", "A's opening line becomes B's standfirst.")]),
    })
    panel = judge_options(judges, OPTIONS, evidence="[]", context={"hotel": "Hotel X"})
    assert [r["label"] for r in panel.ranking] == ["B", "A", "C"]
    assert panel.recommended == "B" and panel.ranking[0]["mean"] == 8.0 and panel.split
    assert [t["from"] for t in panel.take] == ["C", "A"]
    assert panel.combine_request().startswith("combine B and C and A: keep B as the base")
    # Judges are independent: none sees another's scores.
    assert all("scores" not in json.dumps(p["options"]) for p in judges.seen)
    text = format_panel(panel)
    assert "Recommended: *option B*" in text and "judges split" in text and "combine as recommended" in text
    assert PanelResult.from_dict(panel.to_dict()) == panel


def test_a_broken_judge_is_skipped_and_too_few_answers_give_no_ranking():
    good = _answer({"A": 5, "B": 8, "C": 6}, "B")
    panel = judge_options(Judges({"booking": good, "evidence": RuntimeError("down"), "editorial": good}),
                          OPTIONS, evidence="", context={})
    assert panel.judges == 2 and panel.recommended == "B" and not panel.split and not panel.take
    partial = {"scores": [{"label": "A", "score": 9}], "best": "A"}  # misses B and C
    assert judge_options(Judges({"booking": good, "evidence": partial, "editorial": None}),
                         OPTIONS, evidence="", context={}) is None
    assert judge_options(Judges({}), OPTIONS[:1], evidence="", context={}) is None


def test_options_run_activates_the_judges_pick_and_combines_as_recommended(tmp_path: Path):
    o, db, _r, editorial, *_ = setup_orchestrator(
        tmp_path, ['{"intent":"RESEARCH_PROPERTY","hotel":"Hotel X"}'] * 3,
        editorial_results=[_option_editorial(t) for t in ("Alpha", "Beta", "Gamma", "Delta")],
    )
    o._settings = replace(o._settings, option_panel=True)
    o._provider = Judges({
        lens: _answer({"A": 6, "B": 9, "C": 7}, "B", [("C", "C's evening walk closes B's Day 1.")])
        for lens in ("booking", "evidence", "editorial")
    }, inner=o._provider)
    outcome = o.handle_message("Research Hotel X and give me 3 options", user_id="U1", thread_key="C:panel")
    pid = outcome.project_id
    assert "The judges' pick" in outcome.markdown and "B is active (the judges' pick)" in outcome.markdown
    concept = db.project_state(pid)["concept"]
    assert concept["active_option"] == "B" and concept["editorial"]["working_title"] == "Beta"
    assert concept["options_panel"]["recommended"] == "B"

    # Switching keeps the panel; ranking again leaves the active option alone.
    o.handle_message("use option A", user_id="U1", thread_key="C:panel")
    assert db.project_state(pid)["concept"]["options_panel"]["recommended"] == "B"
    ranked = o.handle_message("rank the options", user_id="U1", thread_key="C:panel")
    assert "Option A is still active" in ranked.markdown

    combined = o.handle_message("combine as recommended", user_id="U1", thread_key="C:panel")
    assert "*Option D* (combining B, C)" in combined.markdown
    note = " ".join(editorial.tasks[-1].brief.explicit_instructions)
    assert "keep B as the base" in note and "evening walk" in note
    concept = db.project_state(pid)["concept"]
    assert concept["active_option"] == "D" and concept["options_panel"]["recommended"] == "B"


def test_combine_as_recommended_without_a_suggestion_says_so(tmp_path: Path):
    o, db, _r, _e, *_ = setup_orchestrator(
        tmp_path, ['{"intent":"RESEARCH_PROPERTY","hotel":"Hotel X"}'] * 3,
        editorial_results=[_option_editorial(t) for t in ("Alpha", "Beta")],
    )
    o.handle_message("Research Hotel X and give me 2 options", user_id="U1", thread_key="C:none")
    reply = o.handle_message("kết hợp theo đề xuất", user_id="U1", thread_key="C:none")
    assert "no combination to suggest" in reply.markdown
