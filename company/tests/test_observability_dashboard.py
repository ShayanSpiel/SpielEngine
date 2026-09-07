"""Observability surface: the shipped dashboard must be reachable.

The `company observe` command exposes the Observer read-model (health,
dashboard, causal trace). Before 10.2.1 the module shipped as dead code —
present in every home but unreachable from any command.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from company.commands import CleanCommandRuntime
from company.observability import Observer
from company.runtime.engine import GoalStage


class ObservabilityTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path

        handle = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        handle.close()
        self.db = Path(handle.name)
        self.db.unlink()
        self.runtime = CleanCommandRuntime(self.db)

    def tearDown(self):
        self.db.unlink(missing_ok=True)

    def test_dashboard_renders_goal_run_and_attention_state(self):
        goal = self.runtime.create_goal(
            name="One sale", owner_id="director", metric="weekly_sales",
            operator="ge", target=1, config={"aggregation": "latest"})
        self.runtime.tick(max_advances=10)
        board = self.runtime.observe()
        self.assertEqual(board["health"]["goals"], 1)
        self.assertEqual(board["health"]["active_goals"], 1)
        # DECIDE boundary: a departmentless goal parks a decision_request
        # (run DECIDE/waiting) and creates no work order until the owner
        # answers it.
        self.assertEqual(board["health"]["open_work_orders"], 0)
        row = next(item for item in board["goals"]
                   if item["id"] == goal["id"])
        self.assertEqual(row["stage"], GoalStage.DECIDE.value)
        self.assertEqual(row["run_status"], "waiting")
        self.assertEqual(row["open_orders"], 0)
        self.assertTrue(board["attention"],
                        "the parked decision_request must surface as "
                        "attention")

    def test_dashboard_renders_answered_direct_work(self):
        """After `goal decide` answers the park, the dashboard shows the
        host's parked work order in ACT/waiting again."""
        from company.runtime.engine import GoalStage

        goal = self.runtime.create_goal(
            name="Answered sale", owner_id="director", metric="weekly_sales",
            operator="ge", target=1, config={"aggregation": "latest"})
        self.runtime.tick(max_advances=10)
        self.runtime.decide_goal(
            goal["id"], "request_agent", agent="director",
            instruction="close one deal this week")
        board = self.runtime.observe()
        self.assertEqual(board["health"]["open_work_orders"], 1)
        row = next(item for item in board["goals"]
                   if item["id"] == goal["id"])
        self.assertEqual(row["stage"], GoalStage.ACT.value)
        self.assertEqual(row["run_status"], "waiting")
        self.assertEqual(row["open_orders"], 1)

    def test_dashboard_counts_memory_by_scope(self):
        goal = self.runtime.create_goal(
            name="G", owner_id="director", metric="m",
            operator="ge", target=1, config={"aggregation": "latest"})
        self.runtime.set_profile_claim(namespace="ns", claim_key="k", value="v")
        board = self.runtime.observe()
        self.assertEqual(board["memory"].get("owner"), 1)

    def test_trace_explains_the_causal_chain(self):
        goal = self.runtime.create_goal(
            name="Trace me", owner_id="director", metric="m",
            operator="ge", target=1, config={"aggregation": "latest"})
        self.runtime.tick(max_advances=10)
        # DECIDE boundary: the parked run carries the decision_request,
        # not an Intervention — the causal chain is Goal -> Run -> Decision.
        trace = self.runtime.observe(goal_id=goal["id"])
        self.assertEqual(trace["goal"]["name"], "Trace me")
        self.assertEqual(len(trace["runs"]), 1)
        run = trace["runs"][0]
        self.assertEqual(run["stage"], "DECIDE")
        self.assertEqual(run["interventions"], [])

    def test_trace_explains_the_answered_causal_chain(self):
        """Answering the park yields the Goal -> Run -> Intervention ->
        WorkOrder chain the trace renders."""
        goal = self.runtime.create_goal(
            name="Trace the answer", owner_id="director", metric="m",
            operator="ge", target=1, config={"aggregation": "latest"})
        self.runtime.tick(max_advances=10)
        self.runtime.decide_goal(
            goal["id"], "request_agent", agent="director",
            instruction="produce the metric evidence")
        trace = self.runtime.observe(goal_id=goal["id"])
        run = trace["runs"][0]
        self.assertEqual(run["interventions"][0]["status"], "waiting")
        self.assertIn("resolution_message",
                      run["interventions"][0]["context"])

    def test_health_projection_is_compact(self):
        self.runtime.create_goal(
            name="G", owner_id="director", metric="m",
            operator="ge", target=1, config={"aggregation": "latest"})
        health = self.runtime.observe(health=True)
        self.assertEqual(set(health), {
            "goals", "active_goals", "runs", "active_interventions",
            "open_work_orders", "evidence", "memory"})

    def test_observer_module_is_shipped_and_importable(self):
        # The regression this guards against: observability shipping as
        # dead code that no command reaches.
        import company
        from pathlib import Path

        root = Path(company.__path__[0])
        self.assertTrue((root / "observability" / "read_model.py").is_file())
        self.assertTrue(hasattr(Observer, "dashboard"))
        self.assertTrue(hasattr(CleanCommandRuntime, "observe"))

    def test_dashboard_renders_workflow_position(self):
        """A goal bound to a department workflow must show its step position.

        Regression: the first dashboard SQL never selected the workflow
        run id and crashed on real homes with workflow runs.
        """
        import os

        fixtures = Path(__file__).resolve().parent / "fixtures" / "departments"
        if not fixtures.is_dir():
            self.skipTest("department fixtures not present")
        os.environ["SPIELOS_TEST_DEPARTMENTS_DIR"] = str(fixtures)
        try:
            runtime = CleanCommandRuntime(self.db)
            # Force the catalog to see the fixture departments.
            from company.commands.goal_runtime import CatalogController
            runtime.runtime.controller = CatalogController(runtime.database)
            runtime.create_goal(
                name="Send outreach", owner_id="outbound",
                metric="email_batches_sent", operator="ge", target=1,
                config={"aggregation": "count", "workflow": "email-outreach"})
            runtime.tick(max_advances=50)
            board = runtime.observe()
            row = next(item for item in board["goals"]
                       if item["name"] == "Send outreach")
            self.assertIn("workflow_id", row)
            self.assertEqual(row["workflow_id"], "outbound:email-outreach")
            self.assertGreaterEqual(row["workflow_steps_total"], 4,
                                    "email-outreach has 6 steps")
            self.assertGreaterEqual(row["workflow_step"], 1)
            self.assertIn(row["workflow_status"], {"running", "waiting"},
                          "the dashboard must surface the live workflow "
                          "status whether it is stepping or parked")
        finally:
            os.environ.pop("SPIELOS_TEST_DEPARTMENTS_DIR", None)

    def test_observe_is_read_only(self):
        from pathlib import Path

        goal = self.runtime.create_goal(
            name="G", owner_id="director", metric="m",
            operator="ge", target=1, config={"aggregation": "latest"})
        self.runtime.tick(max_advances=10)
        before = self.db.read_bytes()
        CleanCommandRuntime(self.db, readonly=True).observe()
        CleanCommandRuntime(self.db, readonly=True).observe(goal_id=goal["id"])
        self.assertEqual(before, self.db.read_bytes(),
                         "observe must never mutate the database file")


if __name__ == "__main__":
    unittest.main()
