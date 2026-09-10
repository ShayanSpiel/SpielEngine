"""The host projection carries goal, evidence, memory, profile, attention,
and layout status on every model request — in both writable and read-only
runtimes (the host hook uses the read-only path)."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from company.commands import CleanCommandRuntime


def _make_home(directory: str) -> Path:
    home = Path(directory) / "home"
    home.mkdir()
    (home / ".agents" / "company").mkdir(parents=True)
    return home


def _insert_notification(runtime: CleanCommandRuntime, goal_id: str,
                          message: str) -> None:
    run = runtime.runs.current(goal_id)
    with runtime.database.connect() as connection:
        connection.execute("""INSERT INTO core_notifications
            (id,goal_id,run_id,intervention_id,kind,payload_json,status,
             created_at,acknowledged_at)
            VALUES (?,?,?,NULL,'owner_input_required',?,'pending',?,NULL)""",
            (f"notification-{message[:6]}", goal_id, run.id,
             json.dumps({"message": message,
                         "required_user_action": message}),
             "2026-01-01T00:00:00+00:00"))


class ContextProjectionTests(unittest.TestCase):
    def _runtime(self, home: Path, readonly: bool = False):
        database = home / ".spielos" / "state" / "company.sqlite"
        database.parent.mkdir(parents=True, exist_ok=True)
        return CleanCommandRuntime(database, readonly=readonly)

    def test_projection_carries_state_profile_attention_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            home = _make_home(directory)
            command = self._runtime(home)
            goal = command.runtime.create_goal(
                "Ship campaign", "outcome", "ge", 1, owner_id="director")
            command.set_profile_claim(
                namespace="identity", claim_key="name", value="Shayan")
            _insert_notification(command, goal.id,
                                  "approve the YouTube upload")

            projection = command.assemble_context(
                prompt="continue the campaign", owner_id="director")
            context = projection["context"]

            self.assertIn("Request: continue the campaign", context)
            self.assertIn("Goal: Ship campaign", context)
            # Owner voice: notification kinds render in owner words, and
            # every id rides the Machine reference line.
            self.assertIn("Attention: waiting on you: approve the YouTube upload",
                          context)
            self.assertNotIn("owner_input_required",
                             context.split("Machine reference:")[0])
            self.assertIn("Profile: identity.name=", context)
            self.assertIn("Layout: ok", context)
            self.assertIn(f"goal:{goal.id}", projection["sources"])

    def test_layout_drift_reaches_the_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            home = _make_home(directory)
            (home / ".agents" / "company" / "declarations.py").write_text("")
            command = self._runtime(home)
            projection = command.assemble_context(owner_id="director")
            self.assertIn("Layout: 1 violation", projection["context"])
            self.assertIn("company layout", projection["context"])

    def test_readonly_runtime_serves_the_same_projection(self):
        """The host hook opens the database read-only; it must still work."""
        with tempfile.TemporaryDirectory() as directory:
            home = _make_home(directory)
            command = self._runtime(home)
            goal = command.runtime.create_goal(
                "Ship campaign", "outcome", "ge", 1, owner_id="director")
            command.set_profile_claim(
                namespace="identity", claim_key="tone", value="direct")
            _insert_notification(command, goal.id, "approve the send")
            database = home / ".spielos" / "state" / "company.sqlite"
            command2 = CleanCommandRuntime(database, readonly=True)
            projection = command2.assemble_context(
                owner_id="director")
            self.assertIn("Goal: Ship campaign", projection["context"])
            self.assertIn("Attention:", projection["context"])
            self.assertIn("Profile: identity.tone=", projection["context"])
            # Read-only means byte-for-byte: the database file is untouched.
            self.assertEqual(sqlite3.connect(database).execute(
                "SELECT COUNT(*) FROM core_notifications"
            ).fetchone()[0], 1)

    def test_owner_profile_renders_across_categories(self):
        with tempfile.TemporaryDirectory() as directory:
            home = _make_home(directory)
            command = self._runtime(home)
            command.set_profile_claim(
                namespace="preferences", claim_key="timezone",
                value={"zone": "Europe/Berlin"})
            projection = command.assemble_context(owner_id="director")
            self.assertIn(
                'Profile: preferences.timezone={"zone": "Europe/Berlin"}',
                projection["context"])

    def test_readonly_projection_focus_follows_ready_priority(self):
        """F8: the host hook reads the projection read-only; its focus
        still follows the scheduler's ready() priority order, not
        creation order."""
        with tempfile.TemporaryDirectory() as directory:
            home = _make_home(directory)
            command = self._runtime(home)
            command.runtime.create_goal(
                "Low priority goal", "outcome", "ge", 1, owner_id="director",
                config={"priority": "low", "aggregation": "latest"})
            critical = command.runtime.create_goal(
                "Critical goal", "outcome", "ge", 1, owner_id="director",
                config={"priority": "critical", "aggregation": "latest"})
            database = home / ".spielos" / "state" / "company.sqlite"
            before = database.read_bytes()
            projection = CleanCommandRuntime(
                database, readonly=True).assemble_context(
                prompt="what is next", owner_id="director")
            self.assertEqual(projection["goal_id"], critical.id,
                             "the ready() priority order decides the focus")
            self.assertIn("Goal: Critical goal", projection["context"])
            self.assertEqual(database.read_bytes(), before,
                             "the read-only projection must not mutate the "
                             "database file")


if __name__ == "__main__":
    unittest.main()
