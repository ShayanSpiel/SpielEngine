"""Load installed Agent declarations from the canonical user layer.

``agents/installed/*.json`` is one of the six preserved owner layers: each
file declares one replaceable Agent (id, skills, permissions, produces,
connections). This module makes those declarations visible to the runtime —
``CleanCommandRuntime`` passes them as the ``agents=`` dict of
``GoalRuntime``/``ResolutionCycle`` — so workflow steps resolve to their
declared identity instead of a bare ``Agent(agent_id)``. Loading is
best-effort per file: an unparseable declaration is skipped, never fatal.
"""

from __future__ import annotations

import json
from pathlib import Path

from .core import Agent

#: Keys of an installed declaration that map onto the Agent record.
_TUPLE_KEYS = ("skill_ids", "capability_ids", "permissions", "produces",
               "connection_ids")


def _candidate_roots(project_root: Path | None) -> list[Path]:
    """Installed layers to probe, most specific first."""
    from ..runtime.paths import find_project_root

    roots: list[Path] = []
    if project_root is not None:
        home = Path(project_root)
        roots.append(home / ".agents" / "company" / "agents" / "installed")
    root = Path(project_root) if project_root is not None else find_project_root()
    roots.append(root / ".agents" / "company" / "agents" / "installed")
    # A flat source checkout has no .agents tree; its installed layer is
    # the agents package's own installed/ folder — <repo>/company/agents/
    # installed (parents[1] is the company package), anchored on this file
    # so the probe lands beside the loader, never in a sibling of it.
    roots.append(Path(__file__).resolve().parents[1] / "agents" / "installed")
    seen, unique = set(), []
    for item in roots:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def installed_agents_root(project_root: Path | None = None) -> Path:
    """The installed Agent layer for one home (first that exists)."""
    for candidate in _candidate_roots(project_root):
        if candidate.is_dir():
            return candidate
    return _candidate_roots(project_root)[0]


def _declaration_to_agent(data: dict) -> Agent | None:
    agent_id = data.get("id")
    if not isinstance(agent_id, str) or not agent_id:
        return None
    values: dict[str, object] = {}
    description = data.get("description")
    if isinstance(description, str):
        values["description"] = description
    for key in _TUPLE_KEYS:
        raw = data.get(key)
        if isinstance(raw, str):
            raw = (raw,)
        if raw is not None:
            values[key] = tuple(item for item in raw if isinstance(item, str))
    return Agent(agent_id, **values)


def available_agents(project_root: Path | None = None) -> dict[str, Agent]:
    """Every installed Agent declaration keyed by id (empty is fine)."""
    root = installed_agents_root(project_root)
    agents: dict[str, Agent] = {}
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue  # a broken declaration never blocks the runtime
        if not isinstance(data, dict):
            continue
        agent = _declaration_to_agent(data)
        if agent is not None:
            agents[agent.id] = agent
    return agents


__all__ = ["available_agents", "installed_agents_root"]
