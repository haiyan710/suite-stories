"""The app's state machines, in one place.

These lifecycles used to be bare strings scattered across the orchestrator, the
Slack layer and the database. Each is now a `StrEnum` whose values are the
strings already stored in SQLite (so no migration is needed and comparisons with
stored strings keep working), plus the transitions the app actually makes.

`transition()` checks a move against its table. It never raises: an unexpected
move is logged (and counted, for tests) and still applied, because refusing a
write in the middle of a Slack reply would lose the user's work. Tests assert
that the real flows only make listed moves.

Machines
--------
- SessionStatus   one Slack thread's conversation (thread_sessions.status)
- ProjectStage    one project's progress (projects.status and orchestration.current_stage)
- ConceptStatus   one concept's editorial lifecycle (concept_state.status, concepts.status)
- Verdict         a critic's decision (CritiqueResult.verdict)
- DraftStage      a sample draft's QA state, derived from concept_state.sample_draft
- VersionStatus   design and artifact versions (design_versions / artifacts .status)
"""

from __future__ import annotations

import logging
from collections import Counter
from enum import StrEnum
from typing import Any, Mapping

logger = logging.getLogger(__name__)

# (machine, old, new) -> count of moves not in the tables; tests read and reset it.
unexpected_transitions: Counter[tuple[str, str, str]] = Counter()


class SessionStatus(StrEnum):
    IDLE = "idle"                                      # created, or reset after a restart
    RESEARCHING = "researching"                        # a job is running for this thread
    AWAITING_CLARIFICATION = "awaiting_clarification"  # the bot asked a question (pending_question_*)
    COMPLETE = "complete"                              # the last request finished
    CANCELLED = "cancelled"                            # the user said stop


class ProjectStage(StrEnum):
    RUNNING = "running"                    # legacy default for new rows
    PLANNING = "planning"                  # plan made, no specialist result yet
    RESEARCHING = "researching"
    RESEARCH_READY = "research_ready"      # evidence gathered, no concept passed
    RESEARCH_VERIFIED = "research_verified"
    RESEARCH_BLOCKED = "research_blocked"  # not enough verified evidence to go on
    CONCEPT_READY = "concept_ready"
    CONCEPT_CRITIQUED = "concept_critiqued"
    DESIGN_READY = "design_ready"
    ARTIFACT_READY = "artifact_ready"
    COMPLETED = "completed"                # legacy single-agent runs
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"            # was researching when the bot stopped


class ConceptStatus(StrEnum):
    GENERATED = "generated"
    SHORTLISTED = "shortlisted"
    APPROVED = "approved"
    PUBLISHED = "published"
    REJECTED = "rejected"
    TEST = "test"  # from a "[test]" thread: never changes, never steers real runs


class Verdict(StrEnum):
    PASS = "PASS"
    PASS_WITH_REVISIONS = "PASS_WITH_REVISIONS"
    FAIL = "FAIL"


class DraftStage(StrEnum):
    NONE = "none"
    FAILED = "failed"            # QA failed: needs a new draft
    NEEDS_FIXES = "needs_fixes"  # QA passed with revisions still open
    PASSED = "passed"
    LAID_OUT = "laid_out"        # passed (or revisable) and a layout was built


class VersionStatus(StrEnum):
    CURRENT = "current"
    APPROVED = "approved"
    SUPERSEDED = "superseded"


# A concept the team can draft, lay out or design from.
DRAFTABLE_CONCEPTS = frozenset({ConceptStatus.SHORTLISTED, ConceptStatus.APPROVED, ConceptStatus.PUBLISHED})
# A concept that counts as a passed option.
PASSED_CONCEPTS = frozenset({ConceptStatus.SHORTLISTED, ConceptStatus.APPROVED})

_ANY_SESSION = frozenset(SessionStatus)
TRANSITIONS: dict[str, dict[str, frozenset[str]]] = {
    # A thread can be picked up again from any state by a new message, so every
    # move between sessions is legal; the invariants live elsewhere:
    # RESEARCHING only while a job runs (reset on startup), AWAITING_CLARIFICATION
    # only with a pending question (cleared when stale or answered).
    "session": {status: _ANY_SESSION for status in SessionStatus},
    "concept": {
        ConceptStatus.GENERATED: frozenset({ConceptStatus.SHORTLISTED, ConceptStatus.APPROVED, ConceptStatus.REJECTED}),
        ConceptStatus.SHORTLISTED: frozenset({
            ConceptStatus.SHORTLISTED, ConceptStatus.APPROVED, ConceptStatus.PUBLISHED, ConceptStatus.REJECTED,
        }),
        ConceptStatus.APPROVED: frozenset({
            ConceptStatus.APPROVED, ConceptStatus.SHORTLISTED, ConceptStatus.PUBLISHED, ConceptStatus.REJECTED,
        }),
        ConceptStatus.PUBLISHED: frozenset({ConceptStatus.PUBLISHED}),
        # A pass in a later critique revives a rejected concept.
        ConceptStatus.REJECTED: frozenset({ConceptStatus.REJECTED, ConceptStatus.SHORTLISTED, ConceptStatus.APPROVED}),
        ConceptStatus.TEST: frozenset({ConceptStatus.TEST}),
    },
}


def transition(machine: str, old: Any, new: Any, *, subject: str = "") -> Any:
    """Return `new`, logging a move the machine's table does not list."""
    table = TRANSITIONS.get(machine)
    old_s, new_s = str(old or ""), str(new or "")
    if table is not None and old_s and old_s in table and new_s not in table[old_s]:
        unexpected_transitions[(machine, old_s, new_s)] += 1
        logger.warning("unexpected %s transition %s -> %s %s", machine, old_s, new_s, subject)
    return new


def status_after_critique(current: str, verdict: str) -> ConceptStatus | None:
    """The concept lifecycle change a stand-alone re-critique implies, or None to keep it."""
    if current == ConceptStatus.TEST:
        return None
    if verdict == Verdict.FAIL:
        return None if current in {ConceptStatus.REJECTED, ConceptStatus.PUBLISHED} else ConceptStatus.REJECTED
    if verdict in {Verdict.PASS, Verdict.PASS_WITH_REVISIONS} and current in {
        "", ConceptStatus.GENERATED, ConceptStatus.REJECTED,
    }:
        return ConceptStatus.SHORTLISTED
    return None


def draft_stage(concept_state: Mapping[str, Any]) -> DraftStage:
    """Where the thread's sample draft stands, from what is stored."""
    draft = concept_state.get("sample_draft") or {}
    if not draft.get("body"):
        return DraftStage.NONE
    verdict = str((draft.get("qa") or {}).get("verdict") or "")
    if verdict == Verdict.FAIL:
        return DraftStage.FAILED
    if draft.get("layout_html"):
        return DraftStage.LAID_OUT
    return DraftStage.NEEDS_FIXES if verdict == Verdict.PASS_WITH_REVISIONS else DraftStage.PASSED


def next_actions(state: Mapping[str, Any], session_status: str = "") -> list[str]:
    """What the team can say next, from the thread's stored state (for status replies)."""
    if session_status == SessionStatus.AWAITING_CLARIFICATION:
        return ["Answer the question above so research can continue."]
    if not state:
        return ["Start with \"@bot research <hotel>\" (add a trip length, dates or a traveller type if you have one)."]
    project = state.get("project") or {}
    concept = state.get("concept") or {}
    stage = str(project.get("status") or "")
    if stage == ProjectStage.INTERRUPTED:
        return ["The last run was interrupted by a restart: say \"continue research\" to pick it up."]
    if stage == ProjectStage.RESEARCH_BLOCKED:
        return ["Share the hotel's deck or facts (\"Hotel confirmed: …\"), then ask me to re-evaluate the concept."]
    if not state.get("research"):
        return ["Say \"continue research\"."]
    status = str(concept.get("status") or "")
    if status not in DRAFTABLE_CONCEPTS:
        return [
            "Ask for an editorial angle (\"find a story\"), or \"3 options\" for different angles.",
            "Share the hotel's deck or photos to add evidence first.",
        ]
    actions: list[str] = []
    options = concept.get("options") or []
    if len(options) > 1:
        others = [o.get("label") for o in options if o.get("label") != concept.get("active_option")]
        actions.append(
            f"Option {concept.get('active_option')} is active; \"use option {others[0]}\" switches, "
            f"\"combine {concept.get('active_option')} and {others[0]}: …\" merges."
        )
    stage_of_draft = draft_stage(concept)
    if stage_of_draft is DraftStage.NONE:
        actions.append("\"write a sample draft\" (add any correction in the same message).")
    elif stage_of_draft is DraftStage.FAILED:
        actions.append("The draft failed QA: \"write a sample draft again. <what to change>\".")
    elif stage_of_draft is DraftStage.NEEDS_FIXES:
        actions.append("\"write a sample draft again\" to apply the open fixes, or \"make the layout\" as it is.")
    elif stage_of_draft is DraftStage.PASSED:
        actions.append("\"make the layout\" (or \"all four layouts\"); share hotel photos first to use them.")
    else:
        actions.append("Fill the [TK] and [ASK] items with the hotel, then paste \"Hotel confirmed: …\".")
    return actions
