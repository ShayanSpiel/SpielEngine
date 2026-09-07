"""Canonical home layout contract and drift audit.

The layout is the product contract: owner content lives in exactly six user
layers under ``.agents/company/``; everything else is the vendored spine.
:func:`audit` reports structural drift without executing anything, and
:func:`layout_summary` renders the one-line status injected into every model
request.
"""

from __future__ import annotations

from pathlib import Path

# Folders allowed directly under .agents/company/: the vendored spine plus
# the six canonical user layers (user layers survive `spielos update`).
VENDORED_ROOT_FOLDERS = frozenset({
    "agents", "assets", "capabilities", "commands", "connections", "context",
    "departments", "evidence", "evals", "goals", "memory",
    "observability", "resolution", "runtime", "skills", "state", "strategy",
    "work_orders", "workflows",
})

# Files the vendored spine may ship directly under .agents/company/.
VENDORED_ROOT_FILES = frozenset({
    "__init__.py", "__main__.py", "ARCHITECTURE.md", "cli.py",
    "dot-env-example", "layout.py", "README.md",
})

# Spine modules that live at the root of a user layer (for example
# skills/core.py); user content in those layers is folder-shaped. registry.py
# is user-created in connections/.
VENDORED_LAYER_FILES = frozenset({"__init__.py", "core.py"})

# Names owned by host agents; a Skill or Department must never take one.
RESERVED_AGENT_NAMES = frozenset({
    "director", "department-runner", "system-improvement", "build", "plan",
    "general",
})

# The six canonical user layers (survive `spielos update`).
USER_LAYERS = (
    "departments", "skills", "capabilities", "connections", "strategy",
    "agents/installed",
)


def _violation(kind: str, root: Path, path: Path, detail: str, fix: str) -> dict:
    return {"kind": kind, "path": str(path.relative_to(root)),
            "detail": detail, "fix": fix}


def _layer_counts(company: Path) -> dict:
    def count_dirs(layer: Path, marker: str | None) -> int:
        if not layer.is_dir():
            return 0
        if marker is None:
            return sum(1 for item in layer.iterdir() if item.is_dir()
                       and item.name != "__pycache__")
        return sum(1 for item in layer.iterdir()
                   if item.is_dir() and (item / marker).is_file())

    def count_files(layer: Path, layer_name: str | None = None) -> int:
        if not layer.is_dir():
            return 0
        vendored = VENDORED_LAYER_FILES
        return sum(1 for item in layer.iterdir() if item.is_file()
                   and not item.name.startswith(".")
                   and item.name not in vendored)

    return {
        "departments": count_dirs(company / "departments", "department.py"),
        "skills": count_dirs(company / "skills", "SKILL.md"),
        "capabilities": count_dirs(company / "capabilities", None),
        "connections": count_files(company / "connections", "connections"),
        "strategy": count_files(company / "strategy"),
        "agents_installed": count_files(company / "agents" / "installed"),
    }


def audit(root: Path) -> dict:
    """Return the structural layout report for one home root (read-only)."""
    root = Path(root)
    company = root / ".agents" / "company"
    if not company.is_dir():
        return {"ok": True, "violations": [], "layers": {}}
    violations: list[dict] = []

    for entry in sorted(company.iterdir()):
        if entry.name == "__pycache__":
            continue
        if entry.is_dir():
            if entry.name not in VENDORED_ROOT_FOLDERS:
                violations.append(_violation(
                    "invented_layer", root, entry,
                    "company content belongs in the six canonical layers",
                    "move it into departments/, skills/, capabilities/, "
                    "connections/, strategy/, or agents/installed/"))
        elif entry.name not in VENDORED_ROOT_FILES:
            violations.append(_violation(
                "invented_root_file", root, entry,
                "files at the company root are vendored spine only",
                "move it into its canonical layer or delete it"))

    for path in sorted(company.rglob("_*")):
        if path.is_dir() and "__pycache__" not in path.parts:
            violations.append(_violation(
                "reserved_namespace", root, path,
                "underscore folders are reserved for the vendored spine",
                "delete it or move the content into a canonical layer"))

    departments = company / "departments"
    if departments.is_dir():
        for entry in sorted(departments.iterdir()):
            if entry.name == "__pycache__":
                continue
            if entry.is_file():
                if entry.name not in VENDORED_LAYER_FILES:
                    violations.append(_violation(
                        "stray_department_file", root, entry,
                        "a Department is a package folder, not a loose file",
                        "move it inside a department folder"))
                continue
            if not (entry / "department.py").is_file():
                violations.append(_violation(
                    "department_without_declaration", root, entry,
                    "a Department folder must declare department.py",
                    f"create {entry.name}/department.py"))
            if entry.name in RESERVED_AGENT_NAMES:
                violations.append(_violation(
                    "reserved_name", root, entry,
                    "this name belongs to a host agent, not a Department",
                    "delete it or choose another name"))

    skills = company / "skills"
    if skills.is_dir():
        vendored_skills = VENDORED_LAYER_FILES
        for entry in sorted(skills.iterdir()):
            if entry.name == "__pycache__":
                continue
            if entry.is_file():
                if entry.name not in vendored_skills:
                    violations.append(_violation(
                        "skill_not_folder", root, entry,
                        "a Skill is skills/<id>/SKILL.md",
                        "move it into skills/<id>/SKILL.md"))
                continue
            if not (entry / "SKILL.md").is_file():
                violations.append(_violation(
                    "skill_without_definition", root, entry,
                    "a Skill folder must contain SKILL.md",
                    f"create {entry.name}/SKILL.md"))
            if entry.name in RESERVED_AGENT_NAMES:
                violations.append(_violation(
                    "reserved_name", root, entry,
                    "this name belongs to a host agent; the Director is the "
                    "host prompt, not a Skill",
                    "delete it"))

    return {"ok": not violations, "violations": violations,
            "layers": _layer_counts(company)}


def layout_summary(root: Path) -> str:
    """One bounded line for context injection; never raises."""
    try:
        result = audit(root)
    except OSError:
        return "unknown (layout audit failed; run `company layout`)"
    if result["ok"]:
        return "ok"
    return (f"{len(result['violations'])} violation(s) — run "
            "`company layout` for detail and resolve before adding content")
