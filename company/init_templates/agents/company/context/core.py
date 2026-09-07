from dataclasses import dataclass
from typing import Any

from ..evidence import Evidence
from ..goals import Goal
from ..memory import Memory


@dataclass(frozen=True)
class GoalContext:
    """What DECIDE reads: the goal, its run, evidence, memory, the
    goal's active children and blockers, and the recent decision
    history (the last 3 decided runs' kind and resolution outcome).
    Read-only and deterministic — populating it never writes."""

    goal: Goal
    run_id: str
    evidence: tuple[Evidence, ...] = ()
    memory: tuple[Memory, ...] = ()
    children: tuple[Goal, ...] = ()
    blockers: tuple[Goal, ...] = ()
    decisions: tuple[dict[str, Any], ...] = ()


def codex_hook_output(projection: dict[str, Any], event_name: str) -> dict[str, Any]:
    """Render clean context for the Codex host hook."""

    return {"continue": True, "hookSpecificOutput": {
        "hookEventName": event_name,
        "additionalContext": projection["context"],
    }}
