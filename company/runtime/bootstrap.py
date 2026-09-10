"""``company init`` — scaffold a self-contained SpielOS harness home.

Creates a vendored copy of the harness spine plus the private state tree in
a target directory (default: cwd), so the resulting repository has zero
runtime dependency on the installed package:

    .agents/company/**     runtime spine + optional Departments
    .spielos/state|data|artifacts/
    .spielos/.env.example  credential contract
    .opencode/**           host adapter (plugin, commands, agents)
    .codex/agents/**       Codex adapter
    opencode.json          host config (generic; no provider keys)
    AGENTS.md              harness operating doc

Template source resolution order:
1. ``SPIELOS_TEMPLATE_DIR`` (a directory containing ``agents/``,
   ``opencode/``, ``codex/``, ``dot-env-example``);
2. the bundled ``company/init_templates/`` package data;
3. the installed ``spielos`` distribution (pipx/uv) when this code runs
   vendored inside a home — refresh reads the newest release from there;
4. a vendored repository checkout this package was imported from.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from .config import VERSION
from .paths import package_vendored_root, validate_home_destination

# Owner-created content that `spielos update` must never touch, per tree
# (relative paths inside that tree). Everything the release ships is
# vendored: it is tracked in .spielos/vendored.json, refreshed on update,
# and pruned when a newer release stops shipping it. In a home with no
# manifest yet (created before the manifest era), the guard cannot know
# what an older release vendored, so it preserves every path the current
# release itself does not ship and refreshes the ones it does.
USER_LAYER_PREFIXES = {
    "agents": (
        "company/departments/",
        "company/skills/",
        "company/capabilities/",
        "company/connections/",
        "company/strategy/",
        "company/agents/installed/",
    ),
    "opencode": (
        "agents/", "commands/", "plugins/", "skills/",
        "package.json", "package-lock.json", "opencode.json", "opencode.jsonc",
    ),
    "codex": ("agents/", "hooks/", "config.toml"),
}

VENDORED_MANIFEST = Path(".spielos") / "vendored.json"


def _template_files(src: Path) -> list[str]:
    files: list[str] = []
    for path in sorted(src.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts \
                and path.suffix != ".pyc":
            files.append(path.relative_to(src).as_posix())
    return files


def _load_manifest(root: Path) -> dict[str, list[str]] | None:
    """Vendored file list written by the previous init/update, if any."""
    manifest = root / VENDORED_MANIFEST
    if not manifest.is_file():
        return None
    try:
        data = json.loads(manifest.read_text())
        files = data.get("files") if isinstance(data, dict) else None
        if not isinstance(files, dict):
            return None
        return {str(tree): [str(item) for item in items or []]
                for tree, items in files.items() if isinstance(items, list)}
    except (json.JSONDecodeError, OSError):
        return None


def _write_manifest(root: Path, entries: dict[str, list[str]]) -> None:
    path = root / VENDORED_MANIFEST
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": 1, "spielos": VERSION, "files": entries}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _canonical_opencode_json() -> dict:
    # Plugins under .opencode/plugins/ are auto-discovered by the host; the
    # loader rejects file-path entries in the "plugin"/"plugins" keys.
    return {"$schema": "https://opencode.ai/config.json",
            "default_agent": "director"}


def _merge_opencode_json(path: Path) -> list[str]:
    """Targeted fix-up of owner configuration; never clobber other keys."""
    try:
        config = json.loads(path.read_text())
        if not isinstance(config, dict):
            config = {}
    except (json.JSONDecodeError, OSError):
        config = {}
    changed: list[str] = []
    if "default_agent" not in config:
        config["default_agent"] = "director"
        changed.append("default_agent")
    for key in ("plugin", "plugins"):
        entries = config.get(key)
        if not isinstance(entries, list):
            continue
        kept = [item for item in entries
                if not (isinstance(item, str)
                        and item.strip().replace("./", "", 1)
                        .startswith(".opencode/"))]
        if kept != entries:
            if kept:
                config[key] = kept
            else:
                config.pop(key, None)
            changed.append(key)
    if changed:
        path.write_text(json.dumps(config, indent=2) + "\n")
        return [f"{path} ({', '.join(sorted(set(changed)))})"]
    return []


def _spielos_launcher_venv() -> Path | None:
    """Virtualenv backing the ``spielos`` launcher on PATH, if resolvable."""
    import shutil

    exe = shutil.which("spielos")
    if not exe:
        return None
    try:
        with open(exe, encoding="utf-8", errors="replace") as handle:
            first = handle.readline().strip()
    except OSError:
        return None
    if not first.startswith("#!"):
        return None
    import shlex

    parts = shlex.split(first[2:])
    if parts and Path(parts[0]).name == "env":
        parts = parts[1:]
        while parts and parts[0].startswith("-"):
            parts = parts[1:]
    if not parts:
        return None
    python = Path(parts[0]).expanduser()
    if not python.exists() or not python.name.startswith("python"):
        return None
    venv = python.resolve().parents[1]
    return None if venv == venv.parent else venv


def _installed_distribution_templates() -> Path | None:
    """init_templates shipped inside the installed ``spielos`` distribution.

    A vendored home has no bundled templates of its own, yet its runtime
    must be able to refresh from the newest release. The installing tool's
    environment (pipx, uv) is not on this interpreter's ``sys.path``, so
    look for it directly: other ``sys.path`` entries first, then the
    ``spielos`` launcher's virtualenv, then conventional install homes.
    """
    import os
    import sys

    here = Path(__file__).resolve()
    bases: list[Path] = []

    # 1. Other sys.path entries (works when templates are merely shadowed).
    for entry in list(sys.path):
        if entry:
            bases.append(Path(entry).expanduser())

    # 2. The virtualenv behind the spielos launcher.
    launcher_venv = _spielos_launcher_venv()
    if launcher_venv is not None:
        bases.append(launcher_venv)

    # 3. Conventional pipx / uv tool locations.
    home = Path.home()
    pipx_home = os.environ.get("PIPX_HOME", "").strip()
    pipx_roots = [Path(pipx_home)] if pipx_home else []
    pipx_roots += [home / ".local" / "pipx",
                   home / "Library" / "Application Support" / "pipx",
                   home / ".local" / "share" / "pipx"]
    for root in pipx_roots:
        bases.append(root / "venvs" / "spielos")
    uv_tools = os.environ.get("UV_TOOL_DIR", "").strip()
    uv_roots = [Path(uv_tools)] if uv_tools else []
    uv_roots.append(home / ".local" / "share" / "uv" / "tools")
    for root in uv_roots:
        bases.append(root / "spielos")

    seen: set[Path] = set()
    for base in bases:
        try:
            base = base.expanduser().resolve()
        except OSError:
            continue
        if base in seen or not base.is_dir():
            continue
        seen.add(base)
        if here.is_relative_to(base):
            continue  # the tree we are running from (e.g. <home>/.agents)
        candidates = [base]
        for pattern in ("lib/python*/site-packages", "Lib/site-packages"):
            candidates.extend(sorted(base.glob(pattern)))
        for site_packages in candidates:
            packaged = site_packages / "company" / "init_templates"
            if ((packaged / "agents").is_dir()
                    and (packaged / "hosts").is_dir()):
                return packaged
    return None


def template_root() -> Path:
    """Template source resolution (see module docstring)."""
    import os

    env_value = os.environ.get("SPIELOS_TEMPLATE_DIR", "").strip()
    if env_value:
        env = Path(env_value).expanduser()
        if env.is_dir():
            return env
        raise FileNotFoundError(f"SPIELOS_TEMPLATE_DIR is not a directory: {env}")
    bundled = Path(__file__).resolve().parents[1] / "init_templates"
    if bundled.is_dir():
        return bundled
    installed = _installed_distribution_templates()
    if installed is not None:
        return installed
    vendored = package_vendored_root()
    if vendored is not None and (vendored / ".agents" / "company").is_dir():
        staged = vendored / ".agents" / "company" / "init_templates"
        if staged.is_dir():
            return staged
    raise FileNotFoundError(
        "No init templates found for this home. Update through the installed "
        "`spielos` command (pipx upgrade spielos, then `spielos update --dir "
        "<home>`), or run from a source checkout, or set SPIELOS_TEMPLATE_DIR "
        "to a directory containing agents/, hosts/, dot-env-example.")


def _copy_tree(src: Path, dst: Path, overwrite: bool = False,
               skip=None) -> list[str]:
    written: list[str] = []
    for path in sorted(src.rglob("*")):
        if path.is_dir():
            continue
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        rel = path.relative_to(src).as_posix()
        if skip is not None and skip(rel):
            continue
        target = dst / rel
        if target.exists() and not overwrite:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        written.append(str(target))
    return written


def _prune_stale(src: Path, dst: Path, skip=None) -> list[str]:
    """Delete vendored files that the current templates no longer ship.

    An ``update`` must leave the vendored spine exactly like a fresh init:
    files left behind by an older release (for example a removed runtime
    module) are deleted. Bytecode is always cleaned; the preserved owner
    layers are never touched.
    """
    removed: list[str] = []
    keep: set[str] = set()
    for path in src.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts \
                and path.suffix != ".pyc":
            rel = path.relative_to(src).as_posix()
            if skip is None or not skip(rel):
                keep.add(rel)
    if not dst.is_dir():
        return removed
    for path in sorted(dst.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(dst).as_posix()
        if rel in keep:
            continue
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            path.unlink(missing_ok=True)
            removed.append(str(path))
            continue
        if skip is not None and skip(rel):
            continue
        if "node_modules" in path.parts:
            continue
        path.unlink(missing_ok=True)
        removed.append(str(path))
    for path in sorted((p for p in dst.rglob("*") if p.is_dir()),
                       reverse=True):
        if "node_modules" in path.parts:
            continue
        try:
            path.rmdir()
        except OSError:
            pass
    return removed


def scaffold(target: Path | None = None, *, force: bool = False,
             on_phase=None) -> dict:
    """Materialize a complete harness home. Returns a receipt dict.

    ``on_phase(label)`` is called before each materialization chunk so an
    interactive driver can show progress; it is optional plumbing and
    never changes what gets written.
    """
    root = validate_home_destination(target or Path.cwd())
    templates = template_root()
    written: list[str] = []
    notify = on_phase or (lambda label: None)

    agents_dst = root / ".agents"
    existing_home = (agents_dst / "company" / "runtime").exists()
    if existing_home and not force:
        raise FileExistsError(
            "this folder already has a SpielOS home (.agents/company); "
            "re-run with --force to overwrite")

    manifest = _load_manifest(root) if existing_home else None

    def vendored_entries() -> dict[str, list[str]]:
        entries = {"agents": _template_files(templates / "agents")}
        for name in ("opencode", "codex"):
            source = templates / "hosts" / name
            entries[name] = _template_files(source) if source.is_dir() else []
        return entries

    def layer_guard(tree: str):
        """True for owner-created files in preserved layers (never vendored)."""
        prefixes = USER_LAYER_PREFIXES.get(tree, ())
        tracked = set(manifest.get(tree, ())) if manifest is not None else None
        # Rel paths the current release itself ships in this tree — the
        # only paths a home with no manifest can prove vendored.
        shipped = (set(vendored_entries().get(tree, ()))
                   if tracked is None else None)

        def is_user(rel: str) -> bool:
            if tracked is None:
                # A pre-manifest home has no vendored history to consult:
                # a path the current release itself ships is vendored and
                # refreshes; every other path could be owner content (or
                # stale residue that cannot be proven vendored), so it is
                # preserved. The manifest this update writes resolves it.
                return rel not in shipped
            if not any(rel.startswith(prefix) for prefix in prefixes):
                return False
            return rel not in tracked

        return is_user

    notify("Vendoring harness spine")
    if existing_home and force:
        # An update removes stale vendored files so the spine matches a
        # fresh init exactly; owner-created files in the user layers are
        # skipped, as is anything the current release still ships.
        removed = _prune_stale(templates / "agents", root / ".agents",
                                skip=layer_guard("agents"))
        written.extend(f"removed {item}" for item in removed)
    written += _copy_tree(templates / "agents", root / ".agents",
                          overwrite=force)
    notify("Installing host adapters (OpenCode, Codex)")
    # Host adapters.
    for name in ("opencode", "codex"):
        src = templates / "hosts" / name
        if not src.is_dir():
            continue
        if existing_home and force:
            written.extend(f"removed {item}" for item in _prune_stale(
                src, root / ("." + name), skip=layer_guard(name)))
        written += _copy_tree(src, root / ("." + name), overwrite=force)

    # Private state/data/artifact trees (empty on purpose).
    notify("Creating private state tree (.spielos)")
    for rel in (".spielos/state", ".spielos/data", ".spielos/artifacts"):
        (root / rel).mkdir(parents=True, exist_ok=True)

    # Record exactly what this release vendored, so the next update can
    # tell owner-created files from stale vendored residue.
    notify("Recording vendored manifest")
    _write_manifest(root, vendored_entries())
    written.append(str(root / VENDORED_MANIFEST))

    # Credential contract example.
    notify("Writing credential contract")
    env_example = templates / "dot-env-example"
    if env_example.is_file():
        dest = root / ".spielos" / ".env.example"
        if force or not dest.exists():
            shutil.copy2(env_example, dest)
            written.append(str(dest))

    # Host config: generic, no analytics/provider keys baked in. Existing
    # owner configuration is fixed up in place, never overwritten.
    notify("Writing host config (opencode.json)")
    opencode_json = root / "opencode.json"
    if opencode_json.exists():
        written += _merge_opencode_json(opencode_json)
    else:
        opencode_json.write_text(
            json.dumps(_canonical_opencode_json(), indent=2) + "\n")
        written.append(str(opencode_json))

    # Harness operating doc for hosts (AGENTS.md), website-agnostic.
    notify("Writing operating doc (AGENTS.md)")
    agents_md = root / "AGENTS.md"
    if agents_md.exists():
        current = agents_md.read_text()
        if _LAYOUT_MARKER not in current:
            agents_md.write_text(
                current.rstrip("\n") + "\n\n" + _LAYOUT_CONTRACT_SECTION)
            written.append(str(agents_md))
    else:
        agents_md.write_text(_AGENTS_MD)
        written.append(str(agents_md))

    gitignore = root / ".gitignore"
    ignore_block = _GITIGNORE.strip() + "\n"
    if gitignore.exists():
        current = gitignore.read_text()
        missing = [line for line in ignore_block.splitlines()
                   if line and line not in current]
        if missing:
            gitignore.write_text(current.rstrip("\n") + "\n\n" + "\n".join(missing) + "\n")
            written.append(str(gitignore))
    else:
        gitignore.write_text(ignore_block)
        written.append(str(gitignore))

    return {
        "root": str(root),
        "template_source": str(templates),
        "files_written": len(written),
        "next_steps": [
            "cd " + str(root),
            "opencode (run /agents, select the Director agent) "
            "or codex (talk to the Director agent)",
            "The Director already sees your company state; just talk to it.",
            "Create a clean Department only when its Workflow contract is ready.",
            "Set credentials in .spielos/.env (see .spielos/.env.example).",
            "Audit layout any time: PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.agents "
            "python3 -B -m company layout",
        ],
    }


_GITIGNORE = """
# Company runtime: credentials, private inputs, state, generated work
.spielos/.env
.spielos/.env.*
.spielos/data/
.spielos/state/
.spielos/artifacts/
.spielos/*.sqlite
.spielos/*.sqlite-*
.spielos/*.db
.spielos/*.db-*
.agents/*.db
.agents/*.sqlite
**/__pycache__/
*.pyc
*.pid
*.log
node_modules/
"""

_AGENTS_MD = """# Agent operating rules — harness section

This repository runs on the SpielOS company harness: one durable loop
(GOAL -> OBSERVE -> DECIDE -> ACT -> EVALUATE) persisted in SQLite under
`.spielos/state/`. The runtime owns every Goal, approval, run, and evidence
record; Departments supply Agent-owned behavior only.

Authority and full documentation: `.agents/company/README.md`.

Open OpenCode (run `/agents`, select the Director agent) or Codex (talk to
the Director agent) and just talk to it — it already sees your company state.
The host injects fresh company state automatically; do not begin with a
manual status probe. If a request carries no SpielOS projection, host
injection failed: run the read-only `company status` once, tell the owner
that injection is broken, and never guess company state by reading files.

## OpenCode commands

| Command | Meaning |
|---|---|
| `/start [request or goal]` | Create, resume, or continue one Goal |
| `/stop [goal]` | Persistently stop automation |
| `/status [goal]` | Compact Company Snapshot or one Goal |
| `/approve <goal>` | Approve exactly one displayed parked action |
| `/help` | Explain the vocabulary and command surface |

## Company layout contract

<!-- spielos-layout-contract v1 -->

Company content lives in exactly these layers under `.agents/company/`:

| Layer | Contents |
|---|---|
| `departments/<id>/department.py` | one Department declaration per folder |
| `skills/<id>/SKILL.md` | one reusable Skill per folder |
| `capabilities/<id>/` | capability packages |
| `connections/` | connection registry and client modules |
| `strategy/` | canonical strategy documents |
| `agents/installed/` | installed worker Agents |

Never invent folders or files outside these layers (no `_lib/`,
`_strategy/`, `declarations.py`, or a duplicate Director skill — the
Director is the host agent prompt, not a Skill). Department-owned subfolders
inside their package are fine. Audit drift with `company layout` and
resolve every violation before adding new content.

## Rules

- Live external actions always park for explicit approval.
- Departments are declarations; Agents execute only claimed WorkOrders.
- Owner, workflow, and strategy Memory must retain its required lineage.
- Structural changes (Departments, Workflows, or the vendored spine) go
  through one bounded system-improvement Goal with exact allowed files and
  acceptance evidence — never improvise them directly.
"""

_LAYOUT_MARKER = "<!-- spielos-layout-contract v1 -->"

_LAYOUT_CONTRACT_SECTION = """## Company layout contract

<!-- spielos-layout-contract v1 -->

Company content lives in exactly these layers under `.agents/company/`:

| Layer | Contents |
|---|---|
| `departments/<id>/department.py` | one Department declaration per folder |
| `skills/<id>/SKILL.md` | one reusable Skill per folder |
| `capabilities/<id>/` | capability packages |
| `connections/` | connection registry and client modules |
| `strategy/` | canonical strategy documents |
| `agents/installed/` | installed worker Agents |

Never invent folders or files outside these layers (no `_lib/`,
`_strategy/`, `declarations.py`, or a duplicate Director skill — the
Director is the host agent prompt, not a Skill). Department-owned subfolders
inside their package are fine. Audit drift with `company layout` and
resolve every violation before adding new content.
"""
