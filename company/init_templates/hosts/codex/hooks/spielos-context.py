#!/usr/bin/env python3
"""Read-only Codex adapter for SpielOS context v2.

Injects one bounded company projection (goal, evidence, memory, profile,
attention, layout status) as developer context on every user prompt and
after compaction. Fails open: the session must survive any error here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


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
    """Resolve the home root: hook cwd first, then this script's own home.

    The script ships at <home>/.codex/hooks/spielos-context.py, so its own
    location anchors the home even when Codex runs from a subfolder.
    """
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
        from company.context.core import codex_hook_output

        database = root / ".spielos" / "state" / "company.sqlite"
        if not database.is_file():
            return 0
        event_name = str(request.get("hook_event_name")
                         or request.get("hookEventName") or "UserPromptSubmit")
        projection = CleanCommandRuntime(database, readonly=True).assemble_context(
            prompt=str(request.get("prompt") or ""),
            owner_id="director")
        print(json.dumps(codex_hook_output(projection, event_name), ensure_ascii=False))
    except Exception:
        # Host startup must remain available if state is absent or outdated.
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
