"""Discover declarative Department packages for the canonical clean core."""

import importlib
import os
import pkgutil
import sys
from pathlib import Path

from .. import departments as department_package
from ..departments import DepartmentManifest


def _fixture_paths() -> list[str]:
    """Extra department package paths from ``SPIELOS_TEST_DEPARTMENTS_DIR``.

    The source product ships zero departments by design; behavioral tests
    point this variable at a fixture tree (``<id>/department.py``) and the
    declarations load under the real ``company.departments`` package so
    their relative imports resolve exactly like a home's. The variable is
    a test seam only — homes never set it.
    """
    fixture = os.environ.get("SPIELOS_TEST_DEPARTMENTS_DIR", "").strip()
    if not fixture:
        return []
    from pathlib import Path

    root = Path(fixture).expanduser().resolve()
    return [str(root)] if root.is_dir() else []


#: Overlay roots this module itself inserted into the package path (and
#: must remove again on the next discovery): the live layer stays intact.
_OVERLAY_PATHS: set[str] = set()


def _search_paths() -> list[str]:
    """Department package paths: the live layer plus the test overlay.

    Fixture paths are inserted into ``company.departments.__path__`` so
    ``import company.departments.<id>.department`` resolves there and the
    declarations' relative imports (``from ...workflows import ...``)
    behave exactly like in a home. The package path is REBUILT from the
    live layer plus the current fixtures on every call: a previous
    overlay's path (possibly deleted) never lingers, and any cached
    fixture modules are dropped from the module cache first so a fresh
    overlay with the same package name reloads its current source.
    """
    # Rebuild from the live layer plus the current overlay: a previous
    # overlay's path (possibly deleted) never lingers.
    live = [path for path in department_package.__path__
            if path not in _OVERLAY_PATHS]
    _OVERLAY_PATHS.clear()
    paths = [*live]
    for fixture in _fixture_paths():
        if fixture not in paths:
            # Prepend: the fixture tree wins over a same-named live module,
            # which never happens in a home (the source ships none).
            paths.insert(0, fixture)
            _OVERLAY_PATHS.add(fixture)
    department_package.__path__ = paths
    # Drop cached overlay modules so a fresh overlay with the same
    # package name reloads its current source instead of shadowing it
    # with a stale module from a deleted directory.
    prefix = f"{department_package.__name__}."
    for name in [name for name in sys.modules
                 if name != department_package.__name__
                 and name.startswith(prefix)
                 and any(name.startswith(f"{prefix}{part}.")
                         or name == f"{prefix}{part}"
                         for part in _overlay_names())]:
        del sys.modules[name]
    return paths


def _overlay_names() -> set[str]:
    """Package names present in the current overlay roots (importable
    ``<root>/<name>/department.py``), used to invalidate their cached
    modules."""
    names: set[str] = set()
    for fixture in _fixture_paths():
        for candidate in Path(fixture).iterdir():
            if ((candidate / "department.py").is_file()):
                names.add(candidate.name)
    return names


def departments() -> dict[str, DepartmentManifest]:
    """Return portable declarations only; execution belongs to GoalRuntime."""

    installed: dict[str, DepartmentManifest] = {}
    for module_info in pkgutil.iter_modules(_search_paths()):
        if module_info.name.startswith("_"):
            continue
        module_name = f"{department_package.__name__}.{module_info.name}.department"
        try:
            module = importlib.import_module(module_name)
        except (ImportError, AttributeError, SyntaxError):
            # A declaration package that no longer imports against the clean
            # contracts is skipped, not fatal: the CLI must keep answering
            # while such packages await a clean rebuild.
            continue
        candidates = [value for value in vars(module).values()
                      if isinstance(value, type)
                      and value.__module__ == module.__name__
                      and getattr(value, "department_id", None)]
        if len(candidates) != 1:
            raise ValueError(f"{module.__name__} must export exactly one Department")
        declaration = candidates[0]()
        department_id = declaration.department_id or declaration.id
        workflows = tuple(getattr(declaration, "workflows", ()) or ())
        skill_ids = tuple(dict.fromkeys(
            skill for workflow in workflows
            for step in workflow.steps for skill in step.skill_ids))
        connection_ids = tuple(dict.fromkeys(
            connection for workflow in workflows
            for step in workflow.steps
            for connection in step.connection_ids))
        manifest = DepartmentManifest(
            department_id, getattr(declaration, "version", "0.0.0"),
            getattr(declaration, "description", ""), workflows,
            tuple(getattr(declaration, "agent_ids", ()) or ()), skill_ids,
            connection_ids,
            dict(getattr(declaration, "evidence_metrics", {}) or {}),
            dict(getattr(declaration, "goal_schema", {}) or {}),
            dict(getattr(declaration, "workflow_agents", {}) or {}))
        if manifest.id in installed:
            raise ValueError(f"duplicate Department: {manifest.id}")
        installed[manifest.id] = manifest
    return installed
