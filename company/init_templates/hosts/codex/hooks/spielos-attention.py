#!/usr/bin/env python3
"""Codex Stop hook: surface pending SpielOS attention when a turn ends.

The OpenCode plugin has session.idle; the Codex equivalent is this Stop
hook. Stays silent when nothing needs the owner, fails open on any error.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Two attention kinds reach the host session: genuine owner asks
# and host-dispatched work (a parked WorkOrder its assigned
# Agent executes). The Director renders the former to the owner
# and executes the latter.
REPORTABLE = {"owner_input_required", "host_work_required"}


def _looks_like_home(candidate: Path) -> bool:
    """A candidate is a home only when it carries a runnable spine: the
    vendored marker `.agents/company/__main__.py` or the flat
    `company/__main__.py` — the OpenCode adapter's homeAt check that
    survived the 2026-09-04 server-cwd incident. A stub `.agents/company/`
    without `__main__.py` (agent state in a hybrid source checkout) is
    not a vendored home.
    """
    return ((candidate / ".agents/company/__main__.py").is_file()
            or (candidate / "company/__main__.py").is_file())


def _vendored_spine(root: Path) -> bool:
    """The spine under `root/.agents` is importable only with its marker
    file; without it the flat `root/company` spine is the true one."""
    return (root / ".agents/company/__main__.py").is_file()


def _root(request: dict) -> Path:
    candidates: list[Path] = []
    raw_cwd = str(request.get("cwd") or "")
    if raw_cwd:
        candidates.append(Path(raw_cwd).expanduser())
    candidates.append(Path(__file__).resolve().parents[2])
    candidates.append(Path.cwd())
    for candidate in candidates:
        try:
            probes = (candidate.resolve(), *candidate.resolve().parents)
        except OSError:
            continue
        for probe in probes:
            if _looks_like_home(probe):
                return probe
    return Path.cwd()


def main() -> int:
    try:
        try:
            request = json.load(sys.stdin)
        except json.JSONDecodeError:
            request = {}
        root = _root(request)
        vendored = root / ".agents" if _vendored_spine(root) else None
        sys.path.insert(0, str(vendored if vendored is not None else root))
        from company.commands import CleanCommandRuntime

        database = root / ".spielos" / "state" / "company.sqlite"
        if not database.is_file():
            return 0
        rows = CleanCommandRuntime(database, readonly=True).notifications(
            status="pending", limit=20)
        pending = [row for row in rows if row.get("kind") in REPORTABLE]
        if pending:
            summary = "; ".join(
                f"{row.get('payload', {}).get('message') or row.get('kind')}"
                for row in pending[:5])
            print(json.dumps({
                "systemMessage": (
                    f"SpielOS needs your input ({len(pending)} item(s)): {summary}. "
                    "Ask the Director or run: PYTHONDONTWRITEBYTECODE=1 "
                    "PYTHONPATH=.agents python3 -B -m company notifications list"),
            }, ensure_ascii=False))
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
