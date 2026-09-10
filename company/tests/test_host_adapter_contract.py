"""Host adapter contracts: the OpenCode plugin template must load under the
V2 loader, the Codex hooks must inject context and surface attention, and
the shipped templates stay pinned to the current host contracts (the
repo's live `.opencode` plugin follows the shipped template on the next
`spielos update`)."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from company.commands import CleanCommandRuntime

REPO = Path(__file__).resolve().parents[2]
TEMPLATE_HOSTS = REPO / "company" / "init_templates" / "hosts"
PLUGIN = TEMPLATE_HOSTS / "opencode" / "plugins" / "spielos-notifications.ts"


class OpenCodePluginContractTests(unittest.TestCase):
    def test_plugin_uses_the_v2_default_export_contract(self):
        """The loader requires `export default { id, setup }`.

        The 1.x named-export shape fails schema validation with
        "Missing key at [\\"default\\"]" and the host degrades silently to
        a coding agent with no company state.
        """
        source = PLUGIN.read_text()
        self.assertIn('id: "spielos-notifications"', source)
        self.assertIn("export default plugin", source)
        self.assertIn("const setup = async (ctx", source)

    def test_plugin_has_no_runtime_imports(self):
        """A fresh home has no node_modules; only type-stripped imports pass."""
        for line in PLUGIN.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("import ") and "import type" not in stripped:
                if "from" in stripped or "require(" in stripped:
                    self.fail(f"runtime import in plugin: {stripped}")

    def test_plugin_resolves_the_sessions_home_not_the_servers(self):
        """A server may be started in a different folder than the session it
        serves; the adapter must find the session's own home. The 2026-09-04
        failure ("can't load context") was exactly a server started in
        ShayanSpiel.Github.io serving SpielOS1 sessions with no injection.
        """
        source = PLUGIN.read_text()
        self.assertIn("sessionDirectoryFrom", source)
        self.assertIn("homeCandidates", source)
        self.assertIn(".agents/company/__main__.py", source)
        self.assertIn("SPIELOS_HOME", source)
        # V2 only: the retired 1.x entry points are gone (the retired names
        # are spelled in fragments so this file stays residue-free).
        self.assertNotIn("SpielOS" + "Context", source)
        self.assertNotIn("pluginDirectory" + "Fallback", source)
        self.assertNotIn("experimental.chat.system." + "transform", source)

    def test_plugin_injects_context_and_surfaces_attention(self):
        source = PLUGIN.read_text()
        self.assertIn('ctx.session.hook("context"', source)
        self.assertIn('"--owner", "director"', source)
        self.assertIn("session.idle", source)
        self.assertIn('"notifications", "list"', source)
        self.assertIn('"notifications", "ack"', source)
        # Injection failure must be reported to the model, not swallowed.
        self.assertIn("injection failed", source)

    def test_template_plugin_is_v2_only(self):
        """The shipped template carries the V2 contract and nothing else.

        The repo's live `.opencode` copy is home state, not the template:
        it picks this contract up on the next `spielos update`.
        """
        source = PLUGIN.read_text()
        self.assertIn("export default plugin", source)
        exports = [line.strip() for line in source.splitlines()
                   if line.strip().startswith("export ")]
        self.assertEqual(["export default plugin"], exports,
                         "the plugin must ship exactly one export: the V2 "
                         "default object")
        # The retired 1.x names are spelled in fragments so this file
        # stays residue-free under the legacy sweep.
        for token in ("SpielOS" + "Context",
                      "pluginDirectory" + "Fallback",
                      "experimental.chat.system." + "transform"):
            self.assertNotIn(token, source)

    def test_repo_opencode_json_has_no_file_path_plugin_entry(self):
        config = json.loads((REPO / "opencode.json").read_text())
        self.assertNotIn("plugin", config)
        self.assertNotIn("plugins", config)
        self.assertEqual("director", config["default_agent"])


class CodexHostContractTests(unittest.TestCase):
    def test_hooks_json_registers_context_and_attention(self):
        config = json.loads(
            (TEMPLATE_HOSTS / "codex" / "hooks.json").read_text())
        hooks = config["hooks"]
        self.assertIn("UserPromptSubmit", hooks)
        self.assertEqual(
            "compact", hooks["SessionStart"][0]["matcher"])
        self.assertIn("Stop", hooks)
        commands = [entry["command"]
                    for group in hooks.values()
                    for item in group for entry in item["hooks"]]
        for command in commands:
            self.assertTrue(command.startswith("python3 "),
                            f"must use PATH python3 (>=3.11), not /usr/bin/python3 (3.9): {command}")

    def test_hook_scripts_anchor_the_home_from_their_own_location(self):
        for name in ("spielos-context.py", "spielos-attention.py"):
            source = (TEMPLATE_HOSTS / "codex" / "hooks" / name).read_text()
            self.assertIn("Path(__file__).resolve().parents[2]", source)

    def test_hooks_treat_only_marker_carriers_as_homes(self):
        """A home candidate carries a runnable spine — the vendored
        marker `.agents/company/__main__.py` or the flat
        `company/__main__.py` (the OpenCode adapter's homeAt check that
        survived the 2026-09-04 server-cwd incident). A stub
        `.agents/company/` without the marker (agent state in a hybrid
        source checkout) must never win home resolution: in script mode
        sys.path[0] is the hook's own folder, so the flat spine at the
        root is the only importable one.
        """
        for name in ("spielos-context.py", "spielos-attention.py"):
            source = (TEMPLATE_HOSTS / "codex" / "hooks" / name).read_text()
            self.assertIn('.agents/company/__main__.py', source)
            self.assertIn('company/__main__.py', source)
            self.assertNotIn(
                '(candidate / ".agents" / "company").is_dir()',
                source,
                "a bare .agents/company directory is not a vendored home")

    def _hook_fixture(self, directory: str) -> tuple[Path, Path]:
        """Build a runnable hybrid home under *directory*.

        `.agents/company/` is a stub (installed-agent state only, no
        marker) while the true spine is the flat `company/` package at
        the root — the layout of this source checkout, which previously
        broke home resolution.
        """
        home = Path(directory) / "hybrid"
        (home / ".agents" / "company" / "agents" / "installed").mkdir(
            parents=True)
        (home / ".agents" / "company" / "agents" / "installed"
         / "system-improvement.json").write_text(
             '{"agent_id": "system-improvement"}')
        shutil.copytree(TEMPLATE_HOSTS / "codex" / "hooks",
                        home / ".codex" / "hooks")
        shutil.copytree(REPO / "company", home / "company",
                        ignore=shutil.ignore_patterns("__pycache__", "tests",
                                                     "init_templates"))
        (home / "docs" / "deep").mkdir(parents=True)
        return home, home / "docs" / "deep"

    def _run_context_hook(self, script: Path, cwd: Path) -> tuple[int, str]:
        """Run the hook exactly as the Codex host does: the request names
        the session cwd and the command runs from the home root."""
        import subprocess
        request = json.dumps({
            "hook_event_name": "UserPromptSubmit",
            "cwd": str(cwd),
            "prompt": "hi",
        })
        completed = subprocess.run(
            ["python3", str(script)], input=request,
            capture_output=True, text=True, cwd=str(script.parents[2]))
        return completed.returncode, completed.stdout

    def test_context_hook_injects_from_hybrid_checkouts_and_subfolders(self):
        """Acceptance for goal-981857b2ebcb: the hook resolves the flat
        spine of a hybrid checkout (stub `.agents` present) from both the
        home root and a nested subfolder, and stays fail-open (exit 0,
        no output) when state is absent.
        """
        with tempfile.TemporaryDirectory() as directory:
            home, subfolder = self._hook_fixture(directory)
            runtime = CleanCommandRuntime(
                home / ".spielos" / "state" / "company.sqlite")
            runtime.create_goal(
                name="Fixture hybrid goal", metric="outcome", operator="ge",
                target=1, owner_id="director")

            for cwd in (home, subfolder):
                code, stdout = self._run_context_hook(
                    home / ".codex" / "hooks" / "spielos-context.py", cwd)
                self.assertEqual(0, code, "the hook must fail open")
                self.assertIn("Fixture hybrid goal", stdout,
                              f"no projection from cwd={cwd}")
                payload = json.loads(stdout)
                self.assertTrue(
                    payload["hookSpecificOutput"]["additionalContext"])

            # Absent state: still exit 0 with no output.
            (home / ".spielos" / "state" / "company.sqlite").unlink()
            code, stdout = self._run_context_hook(
                home / ".codex" / "hooks" / "spielos-context.py", home)
            self.assertEqual((0, ""), (code, stdout))

    def test_context_hook_injects_from_pure_vendored_homes(self):
        """Acceptance for goal-981857b2ebcb: a pure vendored home (marker
        `.agents/company/__main__.py` present, no flat spine) still
        injects from the home root and from a subfolder."""
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "vendored"
            shutil.copytree(
                TEMPLATE_HOSTS.parent / "agents" / "company",
                home / ".agents" / "company",
                ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copytree(TEMPLATE_HOSTS / "codex" / "hooks",
                            home / ".codex" / "hooks")
            (home / "docs" / "deep").mkdir(parents=True)
            runtime = CleanCommandRuntime(
                home / ".spielos" / "state" / "company.sqlite")
            runtime.create_goal(
                name="Fixture vendored goal", metric="outcome",
                operator="ge", target=1, owner_id="director")

            for cwd in (home, home / "docs" / "deep"):
                code, stdout = self._run_context_hook(
                    home / ".codex" / "hooks" / "spielos-context.py", cwd)
                self.assertEqual(0, code, "the hook must fail open")
                self.assertIn("Fixture vendored goal", stdout,
                              f"no projection from cwd={cwd}")
                self.assertIn(
                    "Fixture vendored goal",
                    json.loads(stdout)["hookSpecificOutput"]["additionalContext"])

    def test_attention_hook_anchors_the_vendored_spine(self):
        """The Stop hook shares the marker discipline: a stub `.agents`
        never wins, and pending attention still surfaces from a vendored
        home; absent state stays silent with exit 0."""
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "vendored"
            shutil.copytree(
                TEMPLATE_HOSTS.parent / "agents" / "company",
                home / ".agents" / "company",
                ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copytree(TEMPLATE_HOSTS / "codex" / "hooks",
                            home / ".codex" / "hooks")
            database = home / ".spielos" / "state" / "company.sqlite"
            runtime = CleanCommandRuntime(database)
            goal = runtime.create_goal(
                name="Fixture attention goal", metric="outcome", operator="ge",
                target=1, owner_id="director")
            run = runtime.runs.current(goal["id"])
            with runtime.database.connect() as connection:
                connection.execute("""INSERT INTO core_notifications
                    (id,goal_id,run_id,intervention_id,kind,payload_json,
                     status,created_at,acknowledged_at)
                    VALUES (?,?,?,NULL,'owner_input_required',?,
                            'pending',?,NULL)""",
                    (f"notification-{goal['id']}", goal["id"], run.id,
                     json.dumps({"message": "approve the send",
                                 "required_user_action": "approve"}),
                     "2026-01-01T00:00:00+00:00"))
            import subprocess
            completed = subprocess.run(
                ["python3", str(home / ".codex" / "hooks"
                                / "spielos-attention.py")],
                input="{}", capture_output=True, text=True, cwd=str(home))
            self.assertEqual(0, completed.returncode)
            self.assertIn("approve the send", completed.stdout)

            # Absent state: exit 0, no output.
            database.unlink()
            completed = subprocess.run(
                ["python3", str(home / ".codex" / "hooks"
                                / "spielos-attention.py")],
                input="{}", capture_output=True, text=True, cwd=str(home))
            self.assertEqual((0, ""), (completed.returncode, completed.stdout))

    def test_hook_scripts_compile(self):
        for name in ("spielos-context.py", "spielos-attention.py"):
            path = TEMPLATE_HOSTS / "codex" / "hooks" / name
            compile(path.read_text(), str(path), "exec")

    def test_attention_hook_filters_reportable_kinds(self):
        source = (TEMPLATE_HOSTS / "codex" / "hooks" / "spielos-attention.py").read_text()
        # The deliberate two-kind surface (host-work separation): the
        # Director renders owner asks to the owner and executes host
        # work itself, so both kinds reach the host session.
        self.assertIn(
            'REPORTABLE = {"owner_input_required", "host_work_required"}',
            source)
        self.assertIn("systemMessage", source)

    def test_director_toml_carries_the_full_contract(self):
        source = (TEMPLATE_HOSTS / "codex" / "agents" / "director.toml").read_text()
        flat = " ".join(source.split())
        for marker in (
            "Layout contract (never break)",
            "company layout",
            "host injection failed",
            "system-improvement Goal",
            "Never invent folders or files outside these layers",
            "PYTHONPATH=.agents",
        ):
            self.assertIn(marker, flat)

    def test_all_three_director_adopters_teach_the_memory_taxonomy(self):
        """L2 (item D): the precise memory taxonomy in both hosts.

        The OpenCode director template, the repo's live OpenCode
        director, and the Codex director.toml all teach the same keys:
        owner preferences go to `profile set`, owner strategic direction
        during tasks goes to `memory add --scope strategy`, operational
        lessons go to `tasks --complete --learning`, brief-carried
        learning is honored, nothing is announced when nothing was
        learned, revisions go through adoption, and the memory surface
        includes the retire verb.
        """
        targets = {
            "opencode template": TEMPLATE_HOSTS / "opencode" / "agents" / "director.md",
            "live opencode": REPO / ".opencode" / "agents" / "director.md",
            "codex": TEMPLATE_HOSTS / "codex" / "agents" / "director.toml",
        }
        for name, path in targets.items():
            self.assertTrue(path.is_file(), f"missing adopter: {name}")
            flat = " ".join(path.read_text().split())
            for marker in (
                "profile set",
                "--scope strategy",
                "--learning",
                "Never announce memory when nothing was learned",
                "never invent a lesson",
                "proposed through adoption",
                "memory retire",
            ):
                self.assertIn(marker, flat,
                              f"{name} must teach the memory taxonomy key "
                              f"{marker!r}")
            self.assertIn("tasks <id> --complete <agent>", flat,
                          f"{name} must name the operational-lesson path")

    def test_all_three_director_adopters_teach_the_owner_voice(self):
        """goal-director-voice: the owner-voice contract in all three
        Director adopters.

        The Director speaks owner language by default — no raw goal,
        evidence, or notification identifiers, no stage or decision enums,
        no metric keys, no JSON dumps unless the owner asks — narrating
        the goal, what we have tried so far, what I remember, my read,
        then the ask, and recording the owner's plain-words answer
        through the CLI itself. Every id, metric key, and enum rides the
        projection's Machine reference line for the Director alone.
        """
        targets = {
            "opencode template": TEMPLATE_HOSTS / "opencode" / "agents" / "director.md",
            "live opencode": REPO / ".opencode" / "agents" / "director.md",
            "codex": TEMPLATE_HOSTS / "codex" / "agents" / "director.toml",
        }
        for name, path in targets.items():
            self.assertTrue(path.is_file(), f"missing adopter: {name}")
            flat = " ".join(path.read_text().split())
            for marker in (
                "Owner voice (never break)",
                "no raw goal, evidence, or notification identifiers",
                "no stage or decision enums",
                "no metric keys",
                "what we have tried so far",
                "what I remember",
                "my read or hypothesis",
                "then the ask",
                "answers in plain words",
                "record the answer through the CLI itself",
                "Machine reference",
            ):
                self.assertIn(marker, flat,
                              f"{name} must teach the owner-voice key "
                              f"{marker!r}")
            self.assertNotIn("goal-9973ebf760d3:", flat,
                              f"{name} must not pin live ids as contract "
                              "markers")

    def test_live_opencode_director_mirrors_the_template_voice(self):
        """goal-director-voice: the repo's live OpenCode director carries
        the same owner-voice section the shipped template ships, so the
        contract stays identical before and after `spielos update`."""
        template = (TEMPLATE_HOSTS / "opencode" / "agents" / "director.md"
                    ).read_text()
        live = (REPO / ".opencode" / "agents" / "director.md").read_text()
        template_section = template.split("## Owner voice (never break)", 1)[1]
        template_section = template_section.split("##", 1)[0]
        live_section = live.split("## Owner voice (never break)", 1)[1]
        live_section = live_section.split("##", 1)[0]
        self.assertEqual(
            " ".join(template_section.split()),
            " ".join(live_section.split()),
            "the live director's owner-voice section mirrors the template")

    def test_director_memory_surface_includes_retire(self):
        """L4: the command-surface line names the retire verb in both
        adopters, so owners discover the hygiene path from the prompt."""
        for path in (TEMPLATE_HOSTS / "opencode" / "agents" / "director.md",
                     REPO / ".opencode" / "agents" / "director.md",
                     TEMPLATE_HOSTS / "codex" / "agents" / "director.toml"):
            flat = " ".join(path.read_text().split())
            self.assertIn("memory summary|owner|workflows|strategy|retire",
                          flat)

    def test_repo_codex_tree_is_byte_identical_with_template(self):
        live = REPO / ".codex"
        for template_file in sorted((TEMPLATE_HOSTS / "codex").rglob("*")):
            if not template_file.is_file():
                continue
            relative = template_file.relative_to(TEMPLATE_HOSTS / "codex")
            live_file = live / relative
            self.assertTrue(live_file.is_file(), f"missing {relative}")
            self.assertEqual(template_file.read_bytes(), live_file.read_bytes(),
                             f"diverged: {relative}")


class HomeShippingTests(unittest.TestCase):
    """Fresh homes must receive the full Director contract on both hosts."""

    def setUp(self):
        import os
        import tempfile

        import company.__main__ as entry
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"
        self.assertEqual(0, entry.main(["init", "--dir", str(self.home),
                                        "-y", "--json"]))
        self._entry = entry
        self._env = os.environ.get("SPIELOS_TEMPLATE_DIR")
        if self._env:
            os.environ.pop("SPIELOS_TEMPLATE_DIR", None)

    def tearDown(self):
        import os

        if self._env is not None:
            os.environ["SPIELOS_TEMPLATE_DIR"] = self._env
        self._tmp.cleanup()

    def test_opencode_home_receives_all_three_agents(self):
        agents = self.home / ".opencode" / "agents"
        for name in ("director.md", "system-improvement.md",
                     "department-runner.md"):
            self.assertTrue((agents / name).is_file(), name)
        director = (agents / "director.md").read_text()
        flat = " ".join(director.split())
        for marker in ("Layout contract (never break)",
                       "company layout",
                       "host injection failed",
                       "system-improvement agent",
                       "Never invent folders or files outside these layers"):
            self.assertIn(marker, flat)
        improvement = " ".join((agents / "system-improvement.md").read_text().split())
        self.assertIn("exact list of allowed files", improvement)

    def test_codex_home_receives_director_and_hooks(self):
        self.assertTrue((self.home / ".codex" / "agents" / "director.toml").is_file())
        self.assertTrue((self.home / ".codex" / "hooks" / "spielos-context.py").is_file())
        self.assertTrue((self.home / ".codex" / "hooks" / "spielos-attention.py").is_file())

    def test_repo_agent_definitions_match_shipped_templates(self):
        for name in ("system-improvement.md", "department-runner.md"):
            live = REPO / ".opencode" / "agents" / name
            template = TEMPLATE_HOSTS / "opencode" / "agents" / name
            self.assertEqual(live.read_bytes(), template.read_bytes(), name)


if __name__ == "__main__":
    unittest.main()
