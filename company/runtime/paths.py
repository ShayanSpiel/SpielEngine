"""Single derivation of the SpielOS project root.

Resolution order — deterministic first:

1. **Vendored anchor**: if this very file lives inside a real SpielOS home
   (its checkout root contains ``.spielos/`` or ``.agents/``), that root is
   returned unconditionally. This is the deterministic contract: every
   process — plugin, host, or command — resolves to the same home no
   matter what its cwd is.
2. ``SPIELOS_HOME`` environment variable (explicit override for installed
   console-script usage).
3. Nearest ancestor of the current working directory that looks like a
   SpielOS home. Only used when the package is *installed* (site-packages)
   and therefore has no vendored home of its own.
4. Last resort: the current working directory (fresh ``spielos init`` runs
   before any state exists).
"""

from __future__ import annotations

import os
from pathlib import Path


def _looks_like_home(candidate: Path) -> bool:
    return ((candidate / ".spielos").is_dir()
            or (candidate / ".agents" / "company").is_dir())


def package_vendored_root() -> Path | None:
    """Project root implied by this file's location, when vendored.

    Detects both layouts:
      vendored:  <home>/.agents/company/runtime/paths.py   -> <home>
      flat:      <repo>/company/runtime/paths.py           -> <repo>  (this product repo)
    """
    file = Path(__file__).resolve()
    vendored = file.parents[3]
    if (vendored / ".agents" / "company").is_dir() and _looks_like_home(vendored):
        return vendored
    flat = file.parents[2]
    if ((flat / "company" / "runtime").is_dir()
            and not _in_site_packages(flat)):
        return flat
    return None


def find_project_root() -> Path:
    """Resolve the active SpielOS project root (see module docstring)."""
    vendored = package_vendored_root()
    if vendored is not None and not _in_site_packages(vendored):
        return vendored

    env_home = os.environ.get("SPIELOS_HOME")
    if env_home:
        return Path(env_home).expanduser().resolve()

    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if _looks_like_home(candidate):
            return candidate

    return cwd


def selected_project_root(value: str | Path | None = None) -> Path:
    """Return an exact user-selected home, or use normal home discovery.

    Explicit command destinations must never walk to an ancestor or fall back
    to the Python package location. Mutating CLI commands use this boundary.
    """

    if value is None:
        return find_project_root()
    return Path(value).expanduser().resolve()


def virtual_environment_root(candidate: str | Path) -> Path | None:
    """Return the containing virtualenv root, if *candidate* is inside one."""

    path = Path(candidate).expanduser().resolve()
    for current in (path, *path.parents):
        if (current / "pyvenv.cfg").is_file():
            return current
    return None


def validate_home_destination(candidate: str | Path) -> Path:
    """Reject accidental harness installation inside unsafe destinations."""

    path = Path(candidate).expanduser().resolve()
    if ".Trash" in path.parts:
        raise ValueError(
            "refusing to install a SpielOS home inside macOS Trash; "
            "select a project folder with --dir PATH")
    venv = virtual_environment_root(path)
    if venv is not None:
        raise ValueError(
            f"refusing to install a SpielOS home inside Python virtualenv {venv}; "
            "select your project folder with --dir PATH")
    return path


def _in_site_packages(home: Path) -> bool:
    return "site-packages" in str(home) or "/.venv/" in str(home) + "/"
