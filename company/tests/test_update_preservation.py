"""`spielos update` must preserve every owner layer while refreshing the spine.

Simulates two releases (template roots A and B built from the real
templates), a home with owner content in every user layer, and asserts the
A->B update refreshes vendored files, prunes stale vendored files, and keeps
every owner-created file — including legacy homes created before the
vendored manifest existed.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from company.__main__ import main

REAL_TEMPLATES = Path(__file__).resolve().parents[1] / "init_templates"

DEPARTMENT_SOURCE = '''"""Custom owner Department."""

WORKFLOWS = ()


class Custom:
    department_id = "custom"
    id = "custom"
    version = "1.0.0"
    description = "owner-owned department"
    workflows = WORKFLOWS
'''


def _copy_real_templates(target: Path) -> None:
    shutil.copytree(REAL_TEMPLATES, target)


class _TemplateEnv:
    """Point SPIELOS_TEMPLATE_DIR at a complete template root, verbatim."""

    def __init__(self, path: Path):
        self.path = path

    def __enter__(self) -> Path:
        self._old = os.environ.get("SPIELOS_TEMPLATE_DIR")
        os.environ["SPIELOS_TEMPLATE_DIR"] = str(self.path)
        return self.path

    def __exit__(self, *args) -> None:
        if self._old is None:
            os.environ.pop("SPIELOS_TEMPLATE_DIR", None)
        else:
            os.environ["SPIELOS_TEMPLATE_DIR"] = self._old


def _add_owner_content(home: Path) -> None:
    """Owner-created files in every user layer, on both hosts and spine."""
    agents = home / ".agents" / "company"
    (agents / "departments" / "custom").mkdir(parents=True)
    (agents / "departments" / "custom" / "department.py").write_text(DEPARTMENT_SOURCE)
    (agents / "skills" / "my-skill").mkdir(parents=True)
    (agents / "skills" / "my-skill" / "SKILL.md").write_text("# My skill\n")
    (agents / "capabilities" / "browser").mkdir(parents=True)
    (agents / "capabilities" / "browser" / "run.py").write_text("print('hi')\n")
    (agents / "connections" / "registry.py").write_text("# owner registry\n")
    (agents / "strategy").mkdir(parents=True, exist_ok=True)
    (agents / "strategy" / "growth.md").write_text("# Growth\n")
    (agents / "agents" / "installed").mkdir(parents=True, exist_ok=True)
    (agents / "agents" / "installed" / "worker.py").write_text("# worker\n")
    opencode = home / ".opencode"
    (opencode / "agents").mkdir(parents=True, exist_ok=True)
    (opencode / "commands").mkdir(parents=True, exist_ok=True)
    (opencode / "plugins").mkdir(parents=True, exist_ok=True)
    (opencode / "agents" / "my-agent.md").write_text("---\ndescription: mine\n---\n")
    (opencode / "commands" / "mine.md").write_text("my command\n")
    (opencode / "plugins" / "mine.ts").write_text("export default {}\n")
    (home / ".codex" / "agents").mkdir(parents=True, exist_ok=True)
    (home / ".codex" / "agents" / "custom.toml").write_text('name = "custom"\n')


def _owner_config(home: Path) -> None:
    """Owner-edited opencode.json and AGENTS.md with legacy keys."""
    (home / "opencode.json").write_text(json.dumps({
        "$schema": "https://opencode.ai/config.json",
        "default_agent": "director",
        "plugin": ["./.opencode/plugins/spielos-notifications.ts"],
        "model": "custom-owner-model",
        "mcp": {"servers": {"mine": {"type": "local"}}},
    }, indent=2) + "\n")
    agents_md = home / "AGENTS.md"
    agents_md.write_text(
        agents_md.read_text().rstrip("\n")
        + "\n\n## My custom rules\n\n- never delete this line\n")


class UpdatePreservationTests(unittest.TestCase):
    def _run_company(self, home: Path, *command: str) -> dict:
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-B", "-m", "company", *command], cwd=home,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                 "PYTHONPATH": str(home / ".agents")},
            capture_output=True, text=True, timeout=60)
        self.assertEqual(0, result.returncode, result.stderr or result.stdout)
        return json.loads(result.stdout)

    def _init(self, templates: Path, home: Path) -> None:
        with _TemplateEnv(templates) as _:
            self.assertEqual(0, main(["init", "--dir", str(home),
                                      "-y", "--json"]))

    def _update(self, templates: Path, home: Path) -> None:
        with _TemplateEnv(templates) as _:
            self.assertEqual(0, main(["update", "--dir", str(home),
                                      "--json"]))

    def test_update_preserves_owner_layers_and_refreshes_spine(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / "home"
            release_a, release_b = base / "a", base / "b"

            # Release A: like the current release plus one extra vendored
            # spine module that release B will stop shipping.
            _copy_real_templates(release_a)
            (release_a / "agents" / "company" / "legacy_module.py").write_text(
                "# removed in release B\n")
            # Release B: adds a new vendored spine file and changes one
            # vendored host file.
            _copy_real_templates(release_b)
            (release_b / "agents" / "company" / "new_module.py").write_text(
                "# added in release B\n")
            marker = release_b / "hosts" / "opencode" / "agents" / "director.md"
            marker.write_text(marker.read_text() + "\n<!-- release B -->\n")

            self._init(release_a, home)
            _add_owner_content(home)
            _owner_config(home)
            # Operational state must survive updates untouched.
            state = home / ".spielos" / "state" / "company.sqlite"
            self._run_company(home, "status")
            self.assertTrue(state.is_file())

            self._update(release_b, home)

            agents = home / ".agents" / "company"
            # Owner content survives in every user layer.
            for rel in (
                "departments/custom/department.py",
                "skills/my-skill/SKILL.md",
                "capabilities/browser/run.py",
                "connections/registry.py",
                "strategy/growth.md",
                "agents/installed/worker.py",
            ):
                self.assertTrue((agents / rel).is_file(), rel)
            self.assertEqual(DEPARTMENT_SOURCE,
                             (agents / "departments" / "custom" / "department.py").read_text())
            # Owner host files survive.
            for rel in ("agents/my-agent.md", "commands/mine.md", "plugins/mine.ts"):
                self.assertTrue((home / ".opencode" / rel).is_file(), rel)
            self.assertTrue((home / ".codex" / "agents" / "custom.toml").is_file())
            # Vendored files refresh: new module appears, changed director.md
            # content lands, stale vendored file is pruned.
            self.assertTrue((agents / "new_module.py").is_file())
            self.assertFalse((agents / "legacy_module.py").exists())
            self.assertIn("<!-- release B -->",
                          (home / ".opencode" / "agents" / "director.md").read_text())
            # The manifest now tracks release B's vendored set.
            manifest = json.loads(
                (home / ".spielos" / "vendored.json").read_text())
            self.assertIn("company/runtime/bootstrap.py",
                          manifest["files"]["agents"])
            self.assertIn("company/new_module.py",
                          manifest["files"]["agents"])
            self.assertNotIn("company/legacy_module.py",
                             manifest["files"]["agents"])
            self.assertIn("plugins/spielos-notifications.ts",
                          manifest["files"]["opencode"])
            self.assertIn("hooks.json", manifest["files"]["codex"])
            # Owner config is fixed up, never clobbered.
            config = json.loads((home / "opencode.json").read_text())
            self.assertEqual("custom-owner-model", config["model"])
            self.assertEqual({"servers": {"mine": {"type": "local"}}},
                             config["mcp"])
            self.assertNotIn("plugin", config)
            self.assertEqual("director", config["default_agent"])
            # Owner AGENTS.md rules survive and gain the layout contract.
            agents_md = (home / "AGENTS.md").read_text()
            self.assertIn("never delete this line", agents_md)
            self.assertIn("spielos-layout-contract", agents_md)
            # State survives.
            self.assertTrue(state.is_file())

    def test_pre_manifest_home_never_loses_user_layer_files(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / "home"
            release = base / "release"
            _copy_real_templates(release)

            self._init(release, home)
            # Legacy home: no manifest at all.
            (home / ".spielos" / "vendored.json").unlink()
            _add_owner_content(home)
            # A user file whose name looks like stale vendored residue.
            (home / ".agents" / "company" / "skills" / "legacy").mkdir()
            (home / ".agents" / "company" / "skills" / "legacy" / "SKILL.md") \
                .write_text("kept\n")

            self._update(release, home)

            agents = home / ".agents" / "company"
            self.assertTrue((agents / "skills" / "legacy" / "SKILL.md").is_file())
            for rel in (
                "departments/custom/department.py",
                "skills/my-skill/SKILL.md",
                "capabilities/browser/run.py",
                "connections/registry.py",
                "strategy/growth.md",
                "agents/installed/worker.py",
            ):
                self.assertTrue((agents / rel).is_file(), rel)
            self.assertTrue((home / ".opencode" / "plugins" / "mine.ts").is_file())

    def test_pre_manifest_home_refreshes_shipped_adapters_keeps_owner_files(self):
        """Pre-manifest homes: shipped adapters refresh, owner files stay.

        Homes created before the vendored manifest existed carry no history
        to consult, so `update` must still refresh every host-adapter file
        the current release itself ships (Director prompts, Codex hooks,
        the notifications plugin) to the new release bytes while keeping
        every owner file at a path the release does not ship — including
        .codex paths outside the classic user layers.
        """
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / "home"
            release = base / "release"
            _copy_real_templates(release)

            self._init(release, home)
            # Legacy home: no manifest at all.
            (home / ".spielos" / "vendored.json").unlink()

            # Owner content at paths the release does not ship.
            owner = {
                ".codex/agents/custom.toml": 'name = "custom"\n',
                ".codex/agents/other.toml": 'name = "other"\n',
                ".codex/workflow/scout.md": "# scout\n",
                ".opencode/agents/my-agent.md": "---\ndescription: mine\n---\n",
            }
            for rel, content in owner.items():
                path = home / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            # Diverged old-generation adapter content at template paths.
            stale = {
                ".codex/agents/director.toml": "# OLD director\n",
                ".codex/hooks.json": '{\n  "old": true\n}\n',
                ".codex/hooks/spielos-context.py": "# OLD hook\n",
                ".opencode/agents/director.md": "OLD director\n",
            }
            for rel, content in stale.items():
                (home / rel).write_text(content)

            self._update(release, home)

            # Every template-path adapter equals the release bytes.
            shipped = {
                ".codex/agents/director.toml":
                    release / "hosts" / "codex" / "agents" / "director.toml",
                ".codex/hooks.json":
                    release / "hosts" / "codex" / "hooks.json",
                ".codex/hooks/spielos-context.py":
                    release / "hosts" / "codex" / "hooks" / "spielos-context.py",
                ".codex/hooks/spielos-attention.py":
                    release / "hosts" / "codex" / "hooks" / "spielos-attention.py",
                ".opencode/agents/director.md":
                    release / "hosts" / "opencode" / "agents" / "director.md",
                ".opencode/plugins/spielos-notifications.ts":
                    release / "hosts" / "opencode"
                    / "plugins" / "spielos-notifications.ts",
            }
            for rel, source in shipped.items():
                self.assertEqual(
                    source.read_text(), (home / rel).read_text(),
                    f"{rel} must equal the release template bytes")
            # Every non-template owner file survives byte-identical.
            for rel, content in owner.items():
                self.assertTrue((home / rel).is_file(), rel)
                self.assertEqual(content, (home / rel).read_text(), rel)
            # The update restores the manifest, so the next update prunes
            # residue the current release no longer ships.
            self.assertTrue((home / ".spielos" / "vendored.json").is_file())

    def test_fresh_init_writes_manifest_and_canonical_config(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / "home"
            release = base / "release"
            _copy_real_templates(release)
            self._init(release, home)
            manifest = json.loads(
                (home / ".spielos" / "vendored.json").read_text())
            self.assertIn("company/runtime/bootstrap.py",
                          manifest["files"]["agents"])
            self.assertIn("plugins/spielos-notifications.ts",
                          manifest["files"]["opencode"])
            self.assertIn("hooks.json", manifest["files"]["codex"])
            config = json.loads((home / "opencode.json").read_text())
            self.assertEqual({"$schema": "https://opencode.ai/config.json",
                             "default_agent": "director"}, config)

    def test_real_templates_update_end_to_end(self):
        """The shipped release updating its own fresh home keeps owner files."""
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            self.assertEqual(0, main(["init", "--dir", str(home),
                                      "-y", "--json"]))
            _add_owner_content(home)
            _owner_config(home)
            self.assertEqual(0, main(["update", "--dir", str(home),
                                      "--json"]))
            agents = home / ".agents" / "company"
            self.assertTrue((agents / "departments" / "custom" / "department.py").is_file())
            self.assertTrue((agents / "skills" / "my-skill" / "SKILL.md").is_file())
            self.assertTrue((agents / "capabilities" / "browser" / "run.py").is_file())
            self.assertTrue((agents / "connections" / "registry.py").is_file())
            self.assertTrue((agents / "strategy" / "growth.md").is_file())
            self.assertTrue((agents / "agents" / "installed" / "worker.py").is_file())
            self.assertTrue((home / ".opencode" / "plugins" / "mine.ts").is_file())
            config = json.loads((home / "opencode.json").read_text())
            self.assertEqual("custom-owner-model", config["model"])
            self.assertNotIn("plugin", config)


if __name__ == "__main__":
    unittest.main()
