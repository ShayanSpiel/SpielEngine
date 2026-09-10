"""Behavioral test suite for the SpielOS clean-core harness (source tree).

Ported from the SpielOS-Website home audit (2026-09-03,
AUDIT-BEHAVIOR-REPORT.md): every test asserts one intended UX behavior
end-to-end against throwaway SQLite databases. Department-dependent
classes run against fixture declarations from ``tests/fixtures/departments``
(loaded through the ``SPIELOS_TEST_DEPARTMENTS_DIR`` seam); the shipped
product still carries zero departments by design.

The original suite pinned eight defects as KNOWN-DEFECT tests; those pins
are inverted here because D1–D8 are fixed in this source tree:

- D1 executor identity: orders are claimed and completed with the bare
  agent id — the exact stored claimant string; no alias spelling is
  accepted.
- D2 memory writes: ``tasks --complete --learning`` and ``memory add``
  persist workflow/strategy memory through the lineage-enforcing remember().
- D3 undeclared metrics: ``goal create`` rejects metrics the owner
  Department does not declare.
- D4 escalation livelock: after three consecutive escalated runs the goal
  parks for the owner instead of churning new runs.
- D5 run-scoped approvals: ``approve --scope run`` satisfies every later
  intervention of the same run.
- D6 installed agents: ``agents/installed/*.json`` declarations load into
  the ResolutionCycle.
- D7 approval-only steps: gates with nothing to produce auto-advance once
  approved instead of parking a work order.
- D8 readonly snapshots: cached per (path, mtime, size); the database
  file stays byte-for-byte untouched.

Audit F5+F6 pins (goal-decide-stall):

- F5 decision identity: a flat metric with the identical repeated
  decision parks at stall_threshold even when each run records evidence;
  a changed decision chains; a fixable executor that exhausts its local
  budget repeatedly parks after the escalation threshold consecutive
  exhaustions.
- F6 DECIDE decides from run history: a candidate workflow whose most
  recent execution on the goal left the metric flat is excluded, the
  first remaining candidate in declaration order runs, and an empty
  candidate set parks a decision_request. decide() never writes the
  Workflow definition; the controller/executor seams are injectable and
  GoalContext carries children, blockers, and the recent decisions.

Run:  PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest \\
          company.tests.test_harness_behavior -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# The source tree itself resolves as a home (flat `company/` checkout), so
# no sys.path bootstrap is needed; CLI subprocess tests pin PYTHONPATH to
# the repo root and SPIELOS_HOME to a temp home so vendored lookup does not
# walk into a real home.
os.environ.setdefault("SPIELOS_HOME", str(REPO))

with_departments = __import__(
    "unittest").skipUnless(
        FIXTURES.is_dir(), "department fixtures not present")


def temp_db() -> Path:
    handle = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    handle.close()
    path = Path(handle.name)
    path.unlink()
    return path


from company.agents.core import Agent, AgentEvidence, AgentResult, FunctionExecutor  # noqa: E402
from company.commands.goal_runtime import (  # noqa: E402
    AssignmentExecutor,
    CatalogController,
    CleanCommandRuntime,
)
from company.context.core import codex_hook_output  # noqa: E402
from company.runtime.engine import (  # noqa: E402
    Decision,
    Evaluation,
    GoalRuntime,
    GoalStage,
)
from company.runtime.registry import departments  # noqa: E402
from company.runtime.util import compare  # noqa: E402
from company.state import Database  # noqa: E402
from company.workflows.core import Workflow, WorkflowStep  # noqa: E402


class ScriptedExecutor:
    """Test double for the host: parks, succeeds, fixes, or escalates on cue.

    The real host (the Director) reacts to a WorkOrder by doing the work and
    completing it through ``tasks --complete``. ScriptedExecutor drives the
    same AgentExecutor seam programmatically.
    """

    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls: list[tuple[str, str]] = []

    def execute(self, agent, order):
        self.calls.append((agent.id, order.id))
        if self.script:
            return self.script.pop(0)
        return AgentResult("ask_user",
                           message=f"WorkOrder {order.id} is ready for Agent {agent.id}")


def completing_executor(payload=None, workflow_learning=None):
    """Executor that always completes the current step with evidence."""

    class _CompleteAll:
        def execute(self, agent, order):
            kinds = tuple(order.brief.get("evidence_kinds")
                         or ((order.brief.get("evidence_kind"),)
                             if order.brief.get("evidence_kind") else ()))
            kinds = kinds or ("intervention_result",)
            return AgentResult(
                "completed",
                evidence=tuple(AgentEvidence(k, dict(payload or {}))
                               for k in kinds),
                workflow_learning=workflow_learning,
            )
    return _CompleteAll()


class HarnessCase(unittest.TestCase):
    """Shared: fresh CleanCommandRuntime on a throwaway database."""

    def setUp(self):
        self.db = temp_db()
        self.runtime = CleanCommandRuntime(self.db)
        self.engine = self.runtime.runtime

    def tearDown(self):
        self.db.unlink(missing_ok=True)

    def new_goal(self, name="G", owner="director", metric="m",
                 operator="ge", target=1, parent_id=None, priority=None,
                 aggregation=None, workflow=None):
        config = {}
        if aggregation:
            config["aggregation"] = aggregation
        if priority:
            config["priority"] = priority
        if workflow:
            config["workflow"] = workflow
        config = config or {"aggregation": "latest"}
        row = self.runtime.create_goal(name=name, owner_id=owner, metric=metric,
                                       operator=operator, target=target,
                                       parent_id=parent_id, config=config)
        self.goal_id = row["id"]
        return row

    def other_goal(self, name="Other", metric="m_other"):
        """Create a secondary Goal WITHOUT rebinding self.goal_id."""
        return self.runtime.create_goal(
            name=name, owner_id="director", metric=metric, operator="ge",
            target=1, config={"aggregation": "latest"})

    def tick_until(self, predicate, budget=40):
        for _ in range(budget):
            self.runtime.tick(max_advances=50)
            if predicate():
                return True
        return predicate()

    def decide_bounded_work(self, instruction="produce the metric evidence",
                            agent="director", evidence_kind=None):
        """Answer a parked decision_request with bounded direct work (the
        DECIDE boundary: direct work exists only after the owner names it)."""
        return self.runtime.decide_goal(
            self.goal_id, "request_agent", agent=agent,
            instruction=instruction, evidence_kind=evidence_kind)

    def park_bounded_direct_work(self, instruction="produce the metric evidence",
                                 agent="director", evidence_kind=None):
        """Tick to the DECIDE park, answer it, and return the parked order."""
        self.tick_until(lambda: (self.current_run().stage == GoalStage.DECIDE
                                 and self.current_run().status == "waiting"))
        self.decide_bounded_work(instruction, agent, evidence_kind)
        orders = self.active_orders()
        assert orders, "answering the decision_request must park the work order"
        return orders[0]

    def current_run(self):
        return self.runtime.runs.current(self.goal_id)

    def active_orders(self):
        return self.runtime.work_orders(status="active", goal_id=self.goal_id)


# =========================================================================
# 1. HOST INPUT -> GOAL -> THE PERSISTED LOOP  (seo fixtures)
# =========================================================================

@with_departments
class TestGoalLoopLifecycle(HarnessCase):
    """Owner asks the Director for an outcome; a measurable Goal runs."""

    @classmethod
    def setUpClass(cls):
        os.environ["SPIELOS_TEST_DEPARTMENTS_DIR"] = str(
            FIXTURES / "departments")

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("SPIELOS_TEST_DEPARTMENTS_DIR", None)

    def test_goal_create_persists_goal_and_first_ready_run(self):
        self.new_goal(name="Map opportunities", owner="seo",
                      metric="keyword_opportunities", target=1,
                      aggregation="count", workflow="keyword-research")
        goal = self.runtime.goals.get(self.goal_id)
        run = self.runtime.runs.current(self.goal_id)
        self.assertEqual(goal.status, "active")
        self.assertEqual((run.sequence, run.stage, run.status),
                         (1, GoalStage.OBSERVE, "ready"),
                         "a new Goal must immediately own one ready OBSERVE run")

    def test_full_loop_completes_goal_when_evidence_meets_target(self):
        self.new_goal(name="Map opportunities", owner="seo",
                      metric="keyword_opportunities", target=1,
                      aggregation="count", workflow="keyword-research")
        self.engine.resolution.executor = completing_executor(
            payload={"keyword_opportunities": 1})
        done = self.tick_until(
            lambda: self.runtime.goals.get(self.goal_id).status == "complete")
        self.assertTrue(done, "goal must complete once evidence meets target")
        evidence = self.runtime.evidence.for_goal(self.goal_id)
        self.assertTrue(any(e.kind == "keyword_opportunity" for e in evidence),
                        "the completing step's evidence must be the kind the "
                        "department declares for the metric")

    def test_incomplete_goal_creates_next_run(self):
        self.new_goal(name="Map opportunities", owner="seo",
                      metric="keyword_opportunities", target=10,
                      aggregation="count", workflow="keyword-research")
        self.engine.resolution.executor = completing_executor(
            payload={"keyword_opportunities": 1})
        progressed = self.tick_until(
            lambda: self.runtime.runs.current(self.goal_id).sequence > 1)
        self.assertTrue(progressed,
                        "a not-yet-met Goal must open its next OBSERVE run")

    def test_undeclared_department_metric_is_rejected_at_create(self):
        # D3 fixed: a goal whose metric is not declared by its department
        # can never be proven; creation fails with the declared list.
        with self.assertRaises(ValueError) as caught:
            self.new_goal(name="Publish one article", owner="seo",
                          metric="articles_published", target=1,
                          aggregation="count", workflow="article")
        self.assertIn("articles_published", str(caught.exception))
        self.assertIn("keyword_opportunities", str(caught.exception),
                      "the error must list the declared metrics")

    def test_advance_parks_work_order_for_host_when_no_department(self):
        # DECIDE boundary: a departmentless goal the runtime cannot decide
        # parks a decision_request for the owner instead of inventing
        # content-free bounded work.
        self.new_goal(name="Weekly sales", owner="director", metric="weekly_sales")
        self.runtime.tick(max_advances=10)
        run = self.current_run()
        self.assertEqual((run.stage, run.status), (GoalStage.DECIDE, "waiting"))
        self.assertEqual(run.decision.kind, "decision_request")
        self.assertEqual(self.active_orders(), [],
                         "no content-free work order may exist for a park")
        attention = self.runtime.attention(goal_id=self.goal_id)
        self.assertEqual(len(attention), 1)
        self.assertIn("Weekly sales", attention[0]["message"])

    def test_stage_persistence_is_one_step_per_advance(self):
        self.new_goal(owner="seo", metric="keyword_opportunities", target=1,
                      aggregation="count", workflow="keyword-research")
        before = self.current_run()
        self.runtime.once(self.goal_id)
        after = self.current_run()
        self.assertNotEqual((before.stage, before.status),
                            (after.stage, after.status),
                            "one advance must move exactly one stage boundary")
        self.assertEqual(after.sequence, before.sequence)

    def test_paused_goal_is_not_scheduled(self):
        self.new_goal(metric="weekly_sales")
        self.runtime.goals.set_status(self.goal_id, "paused")
        self.runtime.tick(max_advances=10)
        ready_ids = [r.goal_id for r in self.runtime.runs.ready()]
        self.assertNotIn(self.goal_id, ready_ids,
                         "paused Goals must never be scheduled")

    def test_completed_goal_advance_is_a_noop(self):
        self.new_goal(owner="seo", metric="keyword_opportunities", target=1,
                      aggregation="count", workflow="keyword-research")
        self.engine.resolution.executor = completing_executor(
            payload={"keyword_opportunities": 1})
        self.tick_until(lambda: self.runtime.goals.get(self.goal_id).status == "complete")
        result = self.runtime.once(self.goal_id)
        self.assertEqual(result["goal"]["goal_status"], "achieved")


# =========================================================================
# 2. WORKFLOW EXECUTION: steps, approvals, evidence, completion
# =========================================================================

@with_departments
class TestWorkflowExecution(HarnessCase):

    @classmethod
    def setUpClass(cls):
        os.environ["SPIELOS_TEST_DEPARTMENTS_DIR"] = str(
            FIXTURES / "departments")

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("SPIELOS_TEST_DEPARTMENTS_DIR", None)

    def setUp(self):
        super().setUp()
        self.new_goal(name="Send outreach batch", owner="outbound",
                      metric="email_batches_sent", target=1,
                      workflow="email-outreach")

    def test_decision_binds_the_requested_department_workflow(self):
        self.runtime.tick(max_advances=5)
        run = self.current_run()
        self.assertIsNotNone(run.decision)
        self.assertEqual(run.decision.kind, "execute_workflow")
        self.assertEqual(run.decision.workflow_id, "outbound:email-outreach",
                         "the requested department workflow must be bound "
                         "into the persisted Decision")

    def test_workflow_parks_for_approval_before_external_send(self):
        self.engine.resolution.executor = completing_executor(payload={})
        parked = self.tick_until(
            lambda: any("approval required: send" in item.get("message", "")
                        for item in self.runtime.attention(goal_id=self.goal_id)))
        self.assertTrue(parked, "live external send must park for approval first")
        send_orders = [o for o in self.runtime.work_orders(goal_id=self.goal_id)
                       if o["step_id"] == "send"]
        self.assertEqual(send_orders, [],
                         "no work order may exist for the send step before "
                         "the owner approves")

    def test_approval_grants_exact_key_and_resumes(self):
        self.engine.resolution.executor = completing_executor(payload={})
        self.tick_until(lambda: self.runtime.attention(goal_id=self.goal_id))
        self.runtime.approve(self.goal_id, note="owner approves send",
                              keys=("send",))
        with self.runtime.connect() as connection:
            rows = [(r[0], r[1]) for r in connection.execute(
                "SELECT key,status FROM core_approvals").fetchall()]
        self.assertIn(("send", "approved"), rows,
                      "approve must persist the exact declared key")
        self.engine.resolution.executor = completing_executor(
            payload={"email_batches_sent": 1, "events": []})
        proceeded = self.tick_until(
            lambda: self.runtime.goals.get(self.goal_id).status == "complete"
            or any(o["step_id"] == "send" for o in
                   self.runtime.work_orders(goal_id=self.goal_id)))
        self.assertTrue(proceeded, "after approval the workflow must proceed")

    def test_approval_only_step_does_not_park_a_work_order(self):
        # D7 fixed: after /approve, the approve step (a gate with nothing to
        # produce) must auto-advance; the send step's own order appears
        # immediately, with no intervening approve-step order.
        self.engine.resolution.executor = completing_executor(payload={})
        self.tick_until(lambda: self.runtime.attention(goal_id=self.goal_id))
        self.runtime.approve(self.goal_id, keys=("send",))
        self.engine.resolution.executor = completing_executor(
            payload={"email_batches_sent": 1})
        self.tick_until(
            lambda: any(o["step_id"] == "send" for o in
                        self.runtime.work_orders(goal_id=self.goal_id)))
        opened = [o["step_id"] for o in
                  self.runtime.work_orders(goal_id=self.goal_id)]
        self.assertNotIn("approve", opened,
                         "an approval-only step is a gate, not work: no "
                         "work order may open for it")

    def test_run_scoped_approval_covers_later_interventions(self):
        # D5 fixed: --scope run grants the key against the run itself
        # (intervention_id NULL); the repository's run-key fallback then
        # satisfies every later intervention of the SAME run.
        self.engine.resolution.executor = completing_executor(payload={})
        self.tick_until(lambda: self.runtime.attention(goal_id=self.goal_id))
        self.runtime.approve(self.goal_id, keys=("send",), scope="run")
        with self.runtime.connect() as connection:
            scopes = connection.execute(
                "SELECT key,intervention_id FROM core_approvals").fetchall()
        for row in scopes:
            if row["key"] == "send":
                self.assertIsNone(row["intervention_id"],
                                  "run-scoped grants are stored intervention-free")

    def test_approvals_do_not_leak_into_the_next_run(self):
        self.engine.resolution.executor = completing_executor(payload={})
        self.tick_until(lambda: self.runtime.attention(goal_id=self.goal_id))
        self.runtime.approve(self.goal_id, keys=("send",))
        self.engine.resolution.executor = completing_executor(
            payload={"email_batches_sent": 1})
        self.tick_until(
            lambda: self.runtime.runs.current(self.goal_id).sequence > 1
            or self.runtime.goals.get(self.goal_id).status == "complete")
        if self.runtime.goals.get(self.goal_id).status != "complete":
            parked = self.tick_until(
                lambda: bool(self.runtime.attention(goal_id=self.goal_id)))
            self.assertTrue(parked,
                            "the next run must re-park for its own approval")

    def test_workflow_memory_learning_saved_with_run_lineage(self):
        self.engine.resolution.executor = completing_executor(
            payload={"email_batches_sent": 1},
            workflow_learning="Personalized hooks raise reply rates")
        self.tick_until(
            lambda: self.runtime.goals.get(self.goal_id).status == "complete")
        learned = [m for m in self.runtime.memories(limit=50)
                   if m["scope"] == "workflow"]
        self.assertTrue(learned,
                        "executor workflow_learning must persist as "
                        "workflow-scope memory with run lineage")
        if learned:
            self.assertIsNotNone(learned[0]["run_id"])

    def test_deterministic_completion_writes_no_new_strategy_memory(self):
        # F7(c) pin — strategy memory stays selective: the deterministic
        # CatalogController never fabricates strategy_learning, so a goal
        # driven to completion end to end writes ZERO new strategy rows
        # (owner direction or evidence-backed host distillation remain the
        # only strategy writers). Uses the gate-free keyword-research
        # flow so the deterministic loop can complete on its own.
        seo_goal = self.runtime.create_goal(
            name="Map opportunities", owner_id="seo",
            metric="keyword_opportunities", operator="ge", target=1,
            config={"aggregation": "count", "workflow": "keyword-research"})
        goal_id = seo_goal["id"]
        with self.runtime.connect() as connection:
            before = connection.execute(
                "SELECT COUNT(*) FROM core_memory WHERE scope='strategy'"
            ).fetchone()[0]
        self.engine.resolution.executor = completing_executor(
            payload={"keyword_opportunities": 1})
        done = self.tick_until(
            lambda: self.runtime.goals.get(goal_id).status == "complete")
        self.assertTrue(done, "the goal must complete for the pin to bite")
        with self.runtime.connect() as connection:
            after = connection.execute(
                "SELECT COUNT(*) FROM core_memory WHERE scope='strategy'"
            ).fetchone()[0]
        self.assertEqual(before, after,
                         "completing a goal via the deterministic controller "
                         "must write no new strategy memory")

    def test_parked_workflow_ask_carries_workflow_memory(self):
        # F7(b) pins: the parked workflow ask omits the learning line while
        # no workflow memory exists, then carries it once a step's
        # --learning persists workflow memory — rendered by the
        # AssignmentExecutor's message, and by the engine's ASK_USER
        # payload for any executor that omits it (a scripted host below).
        # L1 (intentional extension): the WorkOrder BRIEF carries the
        # workflow's active claims in its bounded `memory` key too — the
        # ask text renders the learning for the owner, the brief carries
        # it for the executor, and the next workflow order's brief is
        # where learning becomes causal.
        self.tick_until(lambda: self.active_orders())
        first = self.active_orders()[0]
        asks = self.runtime.notifications(goal_id=self.goal_id)
        self.assertEqual(len(asks), 1)
        self.assertNotIn("Workflow learning:",
                          asks[0]["payload"]["message"],
                          "with no workflow memory recorded the line is "
                          "absent")
        self.assertEqual(first["brief"]["memory"], [],
                         "the first execution's brief carries no learning")
        self.runtime.complete_work_order(
            first["id"], first["agent_id"],
            [{"kind": "intervention_result", "payload": {}}],
            learning="warm intros convert better than cold blasts")
        self.engine.resolution.executor = ScriptedExecutor([])
        self.tick_until(lambda: self.active_orders())
        orders = self.runtime.work_orders(goal_id=self.goal_id, limit=20)
        taught = [order for order in orders
                  if order["id"] != first["id"]
                  and order["status"] in ("open", "claimed")]
        self.assertTrue(taught,
                        "completing one step must park the workflow's next "
                        "step for the taught brief to exist")
        self.assertIn("warm intros convert better than cold blasts",
                      taught[-1]["brief"]["memory"],
                      "the next WorkOrder brief carries the workflow's "
                      "recorded learning")
        asks = self.runtime.notifications(goal_id=self.goal_id)
        self.assertEqual(len(asks), 1, "one ask per parked step")
        message = asks[0]["payload"]["message"]
        self.assertIn("Workflow learning:", message)
        self.assertIn("warm intros convert better than cold blasts", message,
                      "the step's --learning reaches the next parked ask")

    def test_approve_acknowledges_the_answered_ask_and_resumes(self):
        # F9(a) pin: approving a parked approval ask retires its pending
        # notification (an approved ask stops re-delivering) BEFORE the
        # run resumes, so only a new gate's ask can reappear as pending.
        # The completing executor is installed before the approve: the
        # resumed run executes the approved send step synchronously
        # inside the approve call itself.
        self.engine.resolution.executor = completing_executor(payload={})
        self.tick_until(lambda: self.runtime.attention(goal_id=self.goal_id))
        pending = self.runtime.notifications(goal_id=self.goal_id)
        self.assertEqual(len(pending), 1)
        notification_id = pending[0]["id"]
        self.engine.resolution.executor = completing_executor(
            payload={"email_batches_sent": 1})
        self.runtime.approve(self.goal_id, keys=("send",))
        with self.runtime.connect() as connection:
            status = connection.execute(
                "SELECT status FROM core_notifications WHERE id=?",
                (notification_id,)).fetchone()[0]
        self.assertEqual(status, "acknowledged",
                         "the approved ask is acknowledged, not re-delivered")
        self.assertEqual(self.runtime.notifications(goal_id=self.goal_id), [],
                         "no pending ask remains after the approval")
        # And the run resumed: the approved send step executed and the
        # workflow can complete the goal.
        proceeded = self.tick_until(
            lambda: self.runtime.goals.get(self.goal_id).status == "complete")
        self.assertTrue(proceeded, "the approved run must resume and proceed")

    def test_tasks_complete_learning_persists_workflow_memory(self):
        # D2 fixed: the documented host flow can write workflow memory.
        # Park the first workflow step for the host (as AssignmentExecutor
        # does), then complete it with --learning like the CLI would.
        self.engine.resolution.executor = ScriptedExecutor([])
        self.tick_until(lambda: self.active_orders())
        order = self.active_orders()[0]
        self.runtime.complete_work_order(
            order["id"], order["agent_id"],
            [{"kind": "lead_batch", "payload": {"leads": []}}],
            learning="Select queries perform better with fresh Supabase cohorts")
        learned = [m for m in self.runtime.memories(limit=50)
                   if m["scope"] == "workflow"]
        self.assertTrue(learned,
                        "tasks --complete --learning must persist "
                        "workflow-scope memory")
        if learned:
            self.assertEqual(learned[0]["run_id"], order["run_id"])
            self.assertEqual(learned[0]["goal_id"], order["goal_id"])

    def test_evidence_kinds_gate_step_completion(self):
        # Steps that declare evidence kinds must receive exactly them.
        self.engine.resolution.executor = completing_executor(payload={})
        self.tick_until(lambda: self.runtime.attention(goal_id=self.goal_id)
                        or self.active_orders())
        orders = self.active_orders()
        if orders and orders[0]["brief"].get("evidence_kinds"):
            required = set(orders[0]["brief"]["evidence_kinds"])
            from company.work_orders import WorkOrderRepository
            repo = WorkOrderRepository(Database(self.db))
            order = repo.get(orders[0]["id"])
            with self.assertRaises(ValueError):
                repo.complete_with_evidence(
                    order.id, {}, executor_id=order.claimed_by,
                    kind="wrong_kind", payload={})


# =========================================================================
# 3. MEMORY: scopes, triggers, lineage, supersession
# =========================================================================

class TestMemoryBehavior(HarnessCase):

    def setUp(self):
        super().setUp()
        self.new_goal(metric="m")
        self.run_id = self.current_run().id

    def _evidence(self, kind="m", payload=None):
        return self.runtime.evidence.record(
            goal_id=self.goal_id, run_id=self.run_id, kind=kind,
            payload=payload or {"m": 1})

    def test_owner_memory_saved_without_evidence_via_profile_set(self):
        record = self.runtime.set_profile_claim(
            namespace="layout", claim_key="canonical-folders",
            value={"rule": "one canonical layer per concept"})
        self.assertEqual(record["scope"], "owner")
        self.assertIn("layout.canonical-folders", record["claim"])

    def test_profile_set_supersedes_previous_claim_of_same_key(self):
        self.runtime.set_profile_claim(namespace="outbound", claim_key="tone",
                                       value="direct")
        second = self.runtime.set_profile_claim(namespace="outbound",
                                                 claim_key="tone", value="warmer")
        owner = self.runtime.owner_memory()
        active = [m for m in owner if m["claim_key"] == "tone"
                  and m["status"] == "active"]
        self.assertEqual(len(active), 1,
                         "only one active owner claim per key may remain")
        self.assertIn('"warmer"', second["claim"])

    def test_non_owner_memory_requires_evidence_and_lineage(self):
        evidence = self._evidence()
        record = self.runtime.memory.remember(
            "workflow", "hook-before-pain works", evidence_ids=(evidence.id,),
            goal_id=self.goal_id, run_id=self.run_id,
            workflow_id="outbound:email-outreach")
        self.assertEqual(record.scope, "workflow")
        self.assertEqual(record.evidence_ids, (evidence.id,))

    def test_non_owner_memory_rejected_without_evidence(self):
        with self.assertRaises(ValueError):
            self.runtime.memory.remember("strategy", "no proof",
                                         goal_id=self.goal_id,
                                         run_id=self.run_id)

    def test_non_owner_memory_rejected_without_goal_run_lineage(self):
        evidence = self._evidence()
        with self.assertRaises(ValueError):
            self.runtime.memory.remember("workflow", "orphan learning",
                                         evidence_ids=(evidence.id,))

    def test_memory_evidence_must_belong_to_same_goal_and_run(self):
        other = self.other_goal(name="G2")
        other_run = self.runtime.runs.current(other["id"])
        evidence = self.runtime.evidence.record(
            goal_id=other["id"], run_id=other_run.id, kind="m_other", payload={})
        with self.assertRaises(ValueError):
            self.runtime.memory.remember(
                "workflow", "cross-goal learning", evidence_ids=(evidence.id,),
                goal_id=self.goal_id, run_id=self.run_id)

    def test_invalid_scope_rejected(self):
        with self.assertRaises(ValueError):
            self.runtime.memory.remember("department", "not a scope")

    def test_supersession_only_within_same_scope(self):
        evidence = self._evidence()
        first = self.runtime.memory.remember(
            "strategy", "v1", evidence_ids=(evidence.id,),
            goal_id=self.goal_id, run_id=self.run_id)
        second = self.runtime.memory.remember(
            "strategy", "v2", evidence_ids=(evidence.id,),
            goal_id=self.goal_id, run_id=self.run_id,
            supersedes_id=first.id)
        self.assertEqual(second.status, "active")
        self.assertEqual(self.runtime.memory.get(first.id).status, "superseded")

    def test_relevant_memory_scoping(self):
        evidence = self._evidence()
        self.runtime.memory.remember("owner", "owner claim")
        self.runtime.memory.remember(
            "workflow", "wf learning", evidence_ids=(evidence.id,),
            goal_id=self.goal_id, run_id=self.run_id, workflow_id="w1")
        self.runtime.memory.remember(
            "strategy", "strategy learning", evidence_ids=(evidence.id,),
            goal_id=self.goal_id, run_id=self.run_id)
        relevant = self.runtime.memory.relevant(
            goal_id=self.goal_id, workflow_id="w1", limit=10)
        scopes = [m.scope for m in relevant]
        for expected in ("owner", "workflow", "strategy"):
            self.assertIn(expected, scopes)
        without_workflow = self.runtime.memory.relevant(
            goal_id=self.goal_id, workflow_id=None, limit=10)
        self.assertNotIn("workflow", [m.scope for m in without_workflow],
                         "workflow memory applies only with its workflow_id")

    def test_same_owner_and_metric_is_not_a_strategy_relation(self):
        # Issue #7: sharing an owner and a metric is NOT strategy
        # relevance — two campaigns can carry both while pursuing
        # materially different strategies. Only explicit structure
        # (parent/child/supports/shared parent) relates goals.
        self.new_goal(name="Campaign A", metric="m")
        sibling = self.runtime.create_goal(
            name="Campaign B", owner_id="director", metric="m",
            operator="ge", target=1, config={"aggregation": "latest"})
        sibling_run = self.runtime.runs.current(sibling["id"])
        sibling_evidence = self.runtime.evidence.record(
            goal_id=sibling["id"], run_id=sibling_run.id, kind="m",
            payload={"m": 1})
        self.runtime.memory.remember(
            "strategy", "campaign B learned this already",
            evidence_ids=(sibling_evidence.id,), goal_id=sibling["id"],
            run_id=sibling_run.id)
        claims = [item.claim for item in self.runtime.memory.relevant(
            goal_id=self.goal_id, limit=20)]
        self.assertNotIn("campaign B learned this already", claims,
                         "same owner + same metric alone must not leak "
                         "strategy between campaigns")

    def test_shared_parent_sibling_learning_is_relevant(self):
        # The positive pin (issue #7): genuine siblings — children of the
        # same parent — DO share strategy learning.
        parent = self.runtime.create_goal(
            name="Parent", owner_id="director", metric="parent_metric",
            operator="ge", target=1, config={"aggregation": "latest"})
        self.new_goal(name="Focus", metric="m", parent_id=parent["id"])
        sibling = self.runtime.create_goal(
            name="Genuine sibling", owner_id="director", metric="m",
            operator="ge", target=1, parent_id=parent["id"],
            config={"aggregation": "latest"})
        sibling_run = self.runtime.runs.current(sibling["id"])
        evidence = self.runtime.evidence.record(
            goal_id=sibling["id"], run_id=sibling_run.id, kind="m",
            payload={"m": 1})
        self.runtime.memory.remember(
            "strategy", "genuine sibling learned this",
            evidence_ids=(evidence.id,), goal_id=sibling["id"],
            run_id=sibling_run.id)
        claims = [item.claim for item in self.runtime.memory.relevant(
            goal_id=self.goal_id, limit=20)]
        self.assertIn("genuine sibling learned this", claims,
                     "strategy claims reach genuine (shared-parent) siblings")

    def test_strategy_memory_written_by_goal_evaluation_with_evidence(self):
        evidence = self._evidence()
        from company.runtime.engine import Evaluation
        self.engine._commit_evaluation(
            self.engine.goals.get(self.goal_id),
            self.engine.runs.current(self.goal_id),
            Evaluation(False, {}, "summary",
                       strategy_learning="retarget ICP segment",
                       evidence_ids=(evidence.id,)))
        strategy = [m for m in self.runtime.memories(limit=20)
                    if m["scope"] == "strategy"]
        self.assertTrue(strategy,
                        "strategy_learning must persist with run lineage")

    def _commit_evaluation(self, learning, evidence_ids):
        from company.runtime.engine import Evaluation
        return self.engine._commit_evaluation(
            self.engine.goals.get(self.goal_id),
            self.engine.runs.current(self.goal_id),
            Evaluation(False, {}, "s", strategy_learning=learning,
                       evidence_ids=evidence_ids))

    def test_strategy_learning_without_evidence_is_refused(self):
        # The guard lives in GoalRuntime.advance (every real path);
        # _commit_evaluation persists what it is given, so probe advance().
        self._drive_to_evaluate()

        class GuardedController(type(self.engine.controller)):
            def evaluate(inner, context, decision, ev):
                from company.runtime.engine import Evaluation
                return Evaluation(False, {}, "s", strategy_learning="x",
                                  evidence_ids=())

        original = self.engine.controller
        self.engine.controller = GuardedController(self.engine.database)
        try:
            with self.assertRaises(ValueError):
                self.engine.advance(self.goal_id)
        finally:
            self.engine.controller = original

    def test_strategy_evidence_must_belong_to_the_evaluated_run(self):
        other = self.other_goal(name="G2")
        other_run = self.runtime.runs.current(other["id"])
        other_evidence = self.runtime.evidence.record(
            goal_id=other["id"], run_id=other_run.id, kind="m_other", payload={})
        self._drive_to_evaluate()

        class GuardedController(type(self.engine.controller)):
            def evaluate(inner, context, decision, ev):
                from company.runtime.engine import Evaluation
                return Evaluation(False, {}, "s", strategy_learning="x",
                                  evidence_ids=(other_evidence.id,))

        original = self.engine.controller
        self.engine.controller = GuardedController(self.engine.database)
        try:
            with self.assertRaises(ValueError):
                self.engine.advance(self.goal_id)
        finally:
            self.engine.controller = original

    def _drive_to_evaluate(self):
        """Park the DECIDE ask, answer it with bounded direct work, complete
        the parked order, and land on the EVALUATE/running boundary where
        the guard probe can act."""
        self.engine.resolution.executor = ScriptedExecutor([])
        for _ in range(12):
            self.runtime.tick(max_advances=30)
            run = self.current_run()
            if (run.stage == GoalStage.DECIDE and run.status == "waiting"):
                self.decide_bounded_work()
                run = self.current_run()
            if run.stage == GoalStage.EVALUATE and run.status == "running":
                return
            if run.status == "waiting":
                order = self.active_orders()[0]
                self.runtime.complete_work_order(
                    order["id"], order["agent_id"],
                    [{"kind": "m", "payload": {"m": 0}}])
                run = self.current_run()
                if run.stage == GoalStage.EVALUATE and run.status == "running":
                    return
        run = self.current_run()
        self.assertEqual((run.stage, run.status),
                         (GoalStage.EVALUATE, "running"))

    def test_direct_ask_carries_goal_relevant_memory(self):
        # F7(b) pin: a direct (non-workflow) parked ask carries the goal's
        # relevant memory claims instead of workflow learning.
        # L1 (intentional extension): the WorkOrder BRIEF carries the same
        # goal-relevant claims in its bounded `memory` key, so an executor
        # that reads the brief (not only the ask text) still gets them.
        self.runtime.memory.remember(
            "strategy", "call the champion before the close",
            evidence_ids=(self._evidence(payload={"m": 0}).id,),
            goal_id=self.goal_id, run_id=self.run_id)
        order = self.park_bounded_direct_work(instruction="close one deal")
        self.assertEqual(order["step_id"], "direct")
        self.assertIn("call the champion before the close",
                      order["brief"]["memory"],
                      "the direct order's brief carries the goal-relevant "
                      "claims for the executor")
        asks = self.runtime.notifications(goal_id=self.goal_id)
        self.assertEqual(len(asks), 1)
        message = asks[0]["payload"]["message"]
        self.assertIn("Relevant memory: call the champion before the close",
                      message,
                      "the direct ask carries the goal's relevant claims")

    def test_direct_ask_omits_the_memory_line_when_no_claims_exist(self):
        # F7(b): with no goal-relevant claims recorded, the direct ask
        # omits the line cleanly.
        # L1 (intentional extension): the brief's `memory` key stays an
        # empty bounded list — the empty case is a stable shape, not a
        # missing key.
        order = self.park_bounded_direct_work(instruction="close one deal")
        self.assertEqual(order["brief"]["memory"], [],
                         "no claims recorded means no claims carried")
        asks = self.runtime.notifications(goal_id=self.goal_id)
        self.assertEqual(len(asks), 1)
        self.assertNotIn("Relevant memory:", asks[0]["payload"]["message"])
        self.assertNotIn("Workflow learning:", asks[0]["payload"]["message"])

    def test_engine_direct_ask_appends_goal_relevant_memory(self):
        # F7(b): the engine's ASK_USER payload carries the goal-relevant
        # claims even when the executor's message omits them (a scripted
        # host executor parks without reading memory).
        self.runtime.memory.remember(
            "strategy", "call the champion before the close",
            evidence_ids=(self._evidence(payload={"m": 0}).id,),
            goal_id=self.goal_id, run_id=self.run_id)
        self.engine.resolution.executor = ScriptedExecutor([])
        self.park_bounded_direct_work(instruction="close one deal")
        message = self.runtime.notifications(
            goal_id=self.goal_id)[0]["payload"]["message"]
        self.assertIn("is ready for Agent", message,
                      "the scripted executor's message is the base")
        self.assertIn("Relevant memory: call the champion before the close",
                      message,
                      "the engine's ask payload appends the goal-relevant "
                      "claims the executor omitted")

    def test_memory_add_writes_workflow_and_strategy_with_lineage(self):
        # D2 fixed: `memory add` reaches both scopes with the engine guards.
        evidence = self._evidence()
        workflow_memory = self.runtime.add_memory(
            "workflow", "batch throttling at 25/hour held delivery",
            evidence_ids=[evidence.id], goal_id=self.goal_id,
            run_id=self.run_id, workflow_id="outbound:email-outreach")
        self.assertEqual(workflow_memory.scope, "workflow")
        self.assertEqual(workflow_memory.evidence_ids, (evidence.id,))
        strategy_memory = self.runtime.add_memory(
            "strategy", "double opt-in lifts reply quality",
            evidence_ids=[evidence.id], goal_id=self.goal_id,
            run_id=self.run_id)
        self.assertEqual(strategy_memory.scope, "strategy")

    def test_memory_add_refuses_orphan_and_cross_run_evidence(self):
        evidence = self._evidence()
        with self.assertRaises(ValueError):
            self.runtime.add_memory("strategy", "no lineage",
                                    evidence_ids=[evidence.id])
        other = self.other_goal(name="G2")
        other_run = self.runtime.runs.current(other["id"])
        cross = self.runtime.evidence.record(
            goal_id=other["id"], run_id=other_run.id, kind="m_other", payload={})
        with self.assertRaises(ValueError):
            self.runtime.add_memory(
                "workflow", "cross-run claim", evidence_ids=[cross.id],
                goal_id=self.goal_id, run_id=self.run_id)

    def test_memory_add_rejects_owner_scope(self):
        with self.assertRaises(ValueError):
            self.runtime.add_memory("owner", "owner claims use profile set")


# =========================================================================
# 4. CONTEXT ASSEMBLY (what the host hooks inject per request)
# =========================================================================

class TestContextAssembly(HarnessCase):

    def setUp(self):
        super().setUp()
        self.new_goal(name="One sale per week", metric="weekly_sales")
        self.runtime.set_profile_claim(namespace="owner", claim_key="pref",
                                       value="concise reports")

    def test_context_contains_prompt_goal_memory_evidence(self):
        self.runtime.add_evidence(self.goal_id, kind="weekly_sales",
                                  source="host", payload={"weekly_sales": 0})
        projection = self.runtime.assemble_context(
            prompt="what should I do next?", owner_id="director")
        self.assertIn("what should I do next?", projection["context"])
        self.assertIn("One sale per week", projection["context"])
        # Owner voice: the goal renders with human progress and the
        # evidence as an outcome sentence; the metric key rides the
        # Machine reference line at the end, never the human lines.
        self.assertIn("0 of 1 customers per week", projection["context"])
        self.assertIn("weekly sales 0", projection["context"])
        human, _, machine = projection["context"].partition(
            "Machine reference:")
        self.assertNotIn("weekly_sales", human)
        self.assertIn("weekly_sales", machine)
        self.assertIn("owner.pref", projection["context"],
                      "owner memory must be injected into host context")
        self.assertEqual(projection["goal_id"], self.goal_id)
        self.assertTrue(projection["sources"])

    def test_readonly_context_does_not_mutate_database(self):
        before = self.db.read_bytes()
        CleanCommandRuntime(self.db, readonly=True).assemble_context(
            prompt="x", owner_id="director")
        self.assertEqual(before, self.db.read_bytes(),
                         "readonly context must not touch the database file")

    def test_readonly_snapshot_is_cached_per_database_version(self):
        # D8 fixed: repeated read-only projections over an unchanged
        # database reuse one scratch snapshot instead of copying per request.
        CleanCommandRuntime._SNAPSHOT_CACHE.clear()
        for _ in range(3):
            CleanCommandRuntime(self.db, readonly=True).assemble_context(
                prompt="x", owner_id="director")
        self.assertEqual(len(CleanCommandRuntime._SNAPSHOT_CACHE), 1,
                         "one unchanged database must yield one snapshot")
        # and a mutated database is re-copied, never stale.
        self.runtime.add_evidence(self.goal_id, kind="weekly_sales",
                                  source="host", payload={"weekly_sales": 1})
        projection = CleanCommandRuntime(self.db, readonly=True).assemble_context(
            prompt="x", owner_id="director")
        # Owner voice: human lines only; the metric key stays in the
        # Machine reference line the read-only projection also carries.
        human, _, machine = projection["context"].partition(
            "Machine reference:")
        self.assertNotIn("weekly_sales", human)
        self.assertIn("weekly_sales", machine)

    def test_codex_hook_output_shape(self):
        projection = self.runtime.assemble_context(prompt="hi",
                                                   owner_id="director")
        payload = codex_hook_output(projection, "UserPromptSubmit")
        self.assertTrue(payload["continue"])
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"],
                         "UserPromptSubmit")
        self.assertIn("hi", payload["hookSpecificOutput"]["additionalContext"])

    def test_context_token_budget_truncates(self):
        projection = self.runtime.assemble_context(
            prompt="x" * 200, owner_id="director", token_budget=10)
        self.assertLessEqual(len(projection["context"]), 44)

    def test_context_scopes_to_requested_owner(self):
        self.new_goal(name="Director owned goal", metric="articles",
                      aggregation="latest")
        projection = self.runtime.assemble_context(prompt="p", owner_id="director")
        self.assertIn("One sale per week", projection["context"])


# =========================================================================
# 5. WORK ORDERS: claiming, lease, executor identity, evidence
# =========================================================================

class TestWorkOrderContract(HarnessCase):

    def setUp(self):
        super().setUp()
        self.new_goal(metric="m")
        # DECIDE boundary: the departmentless goal first parks a
        # decision_request; answering it with bounded direct work is what
        # parks the order every D1 identity test runs against.
        order = self.park_bounded_direct_work(
            instruction="produce the metric evidence")
        self.order_row = order
        self.order_id = order["id"]

    def test_runtime_claims_orders_with_the_bare_agent_id(self):
        # D1 fixed: the runtime claims with the agent id the notification
        # names, so the documented flow completes without ceremony.
        self.assertEqual(self.order_row["claimed_by"], "director",
                         "orders must be claimable by the documented host "
                         "identity (the bare agent id)")

    def test_complete_with_agent_id_succeeds_while_lease_holds(self):
        # D1 fixed (inverted pin): the documented flow works immediately.
        result = self.runtime.complete_work_order(
            self.order_id, "director",
            [{"kind": "m", "payload": {"m": 1}}])
        self.assertEqual(result["work_order"]["status"], "completed")
        run = self.current_run()
        self.assertEqual(run.stage, GoalStage.EVALUATE,
                         "direct completion must wake the run into EVALUATE")

    def test_complete_with_executor_prefix_identity_is_refused(self):
        # D1 pinned to the current contract: claimant identity is the
        # exact stored claimed_by string. The historical 'executor:<agent>'
        # spelling names a different claimant, so completing with it must
        # raise — only the bare agent id (or the goal owner) completes.
        with self.assertRaises(RuntimeError):
            self.runtime.complete_work_order(
                self.order_id, "executor:director",
                [{"kind": "m", "payload": {"m": 1}}])
        order = self.runtime.work_order(self.order_id)
        self.assertEqual(order["status"], "claimed",
                         "the refused completion must leave the order "
                         "untouched for its real claimant")

    def test_complete_with_wrong_identity_is_refused(self):
        with self.assertRaises(RuntimeError):
            self.runtime.complete_work_order(
                self.order_id, "someone-else",
                [{"kind": "m", "payload": {"m": 1}}])

    def test_completing_order_records_evidence_with_lineage(self):
        self.runtime.complete_work_order(
            self.order_id, "director",
            [{"kind": "m", "payload": {"m": 1}}])
        evidence = self.runtime.evidence.for_goal(self.goal_id)
        self.assertTrue(all(e.goal_id == self.goal_id for e in evidence))
        self.assertTrue(any(e.payload == {"m": 1} for e in evidence))

    def test_multiple_evidence_items_recorded_atomically(self):
        result = self.runtime.complete_work_order(
            self.order_id, "director",
            [{"kind": "m", "payload": {"m": 1}},
             {"kind": "detail", "payload": {"note": "n"}}])
        self.assertEqual(result["work_order"]["status"], "completed")
        kinds = [e.kind for e in self.runtime.evidence.for_run(
            self.current_run().id)]
        self.assertIn("detail", kinds)

    def test_lease_expiry_allows_reclaim_by_host_agent(self):
        with self.runtime.connect() as connection:
            connection.execute(
                "UPDATE core_work_orders SET lease_expires_at=? WHERE id=?",
                ("2000-01-01T00:00:00+00:00", self.order_id))
        claimed = self.runtime.claim_work_order(self.order_id, "director")
        self.assertEqual(claimed["claimed_by"], "director")
        done = self.runtime.complete_work_order(
            self.order_id, "director", [{"kind": "m", "payload": {"m": 1}}])
        self.assertEqual(done["work_order"]["status"], "completed")

    def test_expired_lease_cannot_be_stolen_by_a_foreign_agent(self):
        # F4: the lease/steal behavior survives, but an expired claim is
        # re-claimable only by the declared agent — a foreign identity is
        # refused even when the lease has lapsed.
        with self.runtime.connect() as connection:
            connection.execute(
                "UPDATE core_work_orders SET lease_expires_at=? WHERE id=?",
                ("2000-01-01T00:00:00+00:00", self.order_id))
        with self.assertRaises(RuntimeError) as caught:
            self.runtime.claim_work_order(self.order_id, "someone-else")
        self.assertIn("declared for agent 'director'", str(caught.exception))
        order = self.runtime.work_order(self.order_id)
        self.assertEqual((order["status"], order["claimed_by"]),
                         ("claimed", "director"),
                         "the refused steal must leave the order for its "
                         "declared agent")

    def test_foreign_claimant_claim_and_complete_are_refused(self):
        # F4: a foreign identity can neither claim nor complete an order
        # that belongs to its declared agent.
        with self.assertRaises(RuntimeError) as caught:
            self.runtime.claim_work_order(self.order_id, "someone-else")
        self.assertIn("declared for agent 'director'", str(caught.exception))
        with self.assertRaises(RuntimeError):
            self.runtime.complete_work_order(
                self.order_id, "someone-else",
                [{"kind": "m", "payload": {"m": 1}}])
        order = self.runtime.work_order(self.order_id)
        self.assertEqual((order["status"], order["claimed_by"]),
                         ("claimed", "director"),
                         "the refused attempts must leave the order "
                         "untouched for its declared agent")

    def test_owner_cannot_claim_a_workflow_step_order_for_another_agent(self):
        # F4: no owner override. A workflow step's order belongs to its
        # declared agent; even the goal owner is a foreign claimant there.
        from company.workflows import WorkflowRepository
        from company.resolution.core import InterventionRepository
        from company.work_orders import WorkOrderRepository
        database = Database(self.db)
        WorkflowRepository(database).save(Workflow(
            "f4-owner-flow", "F4 owner-override refusal", (
                WorkflowStep("step-one", "worker-agent", "do the step",
                             evidence_kind="m"),)))
        run = self.current_run()
        intervention = InterventionRepository(database).create(
            goal_id=self.goal_id, run_id=run.id, kind="execute_workflow",
            description="F4 owner-override refusal",
            context={"workflow_id": "f4-owner-flow"})
        workflow_run = WorkflowRepository(database).start(
            "f4-owner-flow", goal_id=self.goal_id, run_id=run.id,
            intervention_id=intervention.id)
        repo = WorkOrderRepository(database)
        order = repo.open(
            goal_id=self.goal_id, run_id=run.id,
            intervention_id=intervention.id,
            workflow_run_id=workflow_run.id, step_id="step-one",
            agent_id="worker-agent",
            brief={"instruction": "do the step", "evidence_kind": "m"})
        for action in (lambda: repo.claim(order.id, "director"),
                       lambda: repo.complete(order.id, {"done": True},
                                              executor_id="director"),
                       lambda: repo.fail(order.id, "boom",
                                         executor_id="director"),
                       lambda: repo.renew(order.id, "director"),
                       lambda: repo.complete_with_evidence(
                           order.id, {"done": True}, executor_id="director",
                           kind="m", payload={"m": 1})):
            with self.assertRaises(RuntimeError, msg=action) as caught:
                action()
            self.assertIn("declared for agent 'worker-agent'",
                          str(caught.exception))
        claimed = repo.claim(order.id, "worker-agent")
        self.assertEqual((claimed.status, claimed.claimed_by),
                         ("claimed", "worker-agent"),
                         "the declared agent claims its own order")

    def test_open_order_is_not_auto_claimed_at_completion(self):
        # F4: the documented flow is claim-then-complete. Completing an
        # open order raises instead of silently claiming it under an
        # arbitrary identity.
        with self.runtime.connect() as connection:
            connection.execute(
                "UPDATE core_work_orders SET status='open',claimed_by=NULL,"
                "claimed_at=NULL,lease_expires_at=NULL WHERE id=?",
                (self.order_id,))
        with self.assertRaises(RuntimeError) as caught:
            self.runtime.complete_work_order(
                self.order_id, "director", [{"kind": "m", "payload": {"m": 1}}])
        self.assertIn("claim it with", str(caught.exception))
        self.assertEqual(
            self.runtime.work_order(self.order_id)["status"], "open",
            "the refused completion must leave the order open")

    def test_declared_agent_claim_then_complete_flow(self):
        # F4: the declared agent's end-to-end flow — claim, then complete
        # — works and wakes the run.
        self.runtime.complete_work_order(
            self.order_id, "director", [{"kind": "m", "payload": {"m": 1}}])
        self.assertEqual(
            self.runtime.work_order(self.order_id)["status"], "completed")
        self.assertEqual(self.current_run().stage, GoalStage.EVALUATE,
                         "completing the claimed order wakes the run")

    def test_owner_completes_direct_order_whose_agent_is_the_owner(self):
        # F4: direct orders whose declared agent IS the goal owner are
        # completed by the owner by construction — that is the declared
        # agent executing, not an override.
        order = self.runtime.work_order(self.order_id)
        self.assertEqual(order["agent_id"], "director",
                         "the departmentless direct order declares the owner")
        result = self.runtime.complete_work_order(
            self.order_id, "director", [{"kind": "m", "payload": {"m": 1}}])
        self.assertEqual(result["work_order"]["status"], "completed")

    def test_completion_requires_evidence(self):
        with self.assertRaises(ValueError):
            self.runtime.complete_work_order(self.order_id, "director", [])


# =========================================================================
# 6. NOTIFICATIONS / ATTENTION (the OpenCode plugin surface)
# =========================================================================

class TestNotificationsAndAttention(HarnessCase):

    def setUp(self):
        super().setUp()
        self.new_goal(metric="m")

    def test_ask_user_creates_pending_owner_input_required_notification(self):
        # Intentional semantic change (issue #5 of goal-d62825bb0b23):
        # an ordinary DECIDE park for a goal nothing can decide is HOST
        # reasoning — the Director agent answers it with `goal decide` —
        # so its notification kind is host_work_required. The structured
        # four-field ask shape is preserved: message, why, decision,
        # after, and the required action (the `goal decide` answer).
        self.runtime.tick(max_advances=10)
        pending = self.runtime.notifications(goal_id=self.goal_id)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["kind"], "host_work_required")
        self.assertIn("required_user_action", pending[0]["payload"])
        # Owner voice: the four owner-facing fields carry no CLI answer
        # syntax; the exact commands ride the payload for the Director,
        # which records the answer through the CLI itself.
        self.assertNotIn("company goal decide",
                         pending[0]["payload"]["after"])
        self.assertIn("company goal decide",
                      pending[0]["payload"]["answer_syntax"][
                          "execute_workflow"])
        self.assertIn("external actions still park for approval first",
                      pending[0]["payload"]["after"])

    def test_attention_maps_pending_notifications(self):
        self.runtime.tick(max_advances=10)
        attention = self.runtime.attention(goal_id=self.goal_id)
        self.assertEqual(len(attention), 1)
        self.assertIn("message", attention[0])

    def test_acknowledge_only_pending(self):
        self.runtime.tick(max_advances=10)
        item = self.runtime.notifications(goal_id=self.goal_id)[0]
        self.runtime.acknowledge_notification(item["id"])
        with self.assertRaises(ValueError):
            self.runtime.acknowledge_notification(item["id"])

    def test_workflow_completion_acknowledges_pending_attention(self):
        order = self.park_bounded_direct_work()
        self.runtime.complete_work_order(
            order["id"], "director",
            [{"kind": "m", "payload": {"m": 1}}])
        self.assertEqual(self.runtime.notifications(goal_id=self.goal_id), [],
                         "answering a parked ask must clear its attention")

    def test_company_snapshot_surfaces_attention_and_work_orders(self):
        self.runtime.tick(max_advances=10)
        snapshot = self.runtime.company_snapshot()
        self.assertIn("attention", snapshot)
        self.assertIn("work_orders", snapshot)
        self.assertEqual(snapshot["counts"]["active"], 1)

    def test_unread_results_surface_completed_runs(self):
        order = self.park_bounded_direct_work()
        self.runtime.complete_work_order(
            order["id"], "director",
            [{"kind": "m", "payload": {"m": 1}}])
        self.runtime.tick(max_advances=10)  # EVALUATE -> run complete
        results = self.runtime.unread_results(goal_id=self.goal_id)
        self.assertTrue(results,
                        "completed runs with evaluations must surface as "
                        "unread results")


# =========================================================================
# 7. REGISTRIES: departments, installed agents, metric utilities
# =========================================================================

@with_departments
class TestRegistries(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ["SPIELOS_TEST_DEPARTMENTS_DIR"] = str(
            FIXTURES / "departments")

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("SPIELOS_TEST_DEPARTMENTS_DIR", None)

    def test_fixture_departments_import_as_clean_declarations(self):
        found = departments()
        expected = {"analytics", "client_delivery", "content", "design",
                    "outbound", "seo", "videography"}
        self.assertEqual(set(found), expected,
                         "the fixture tree must mirror a real home's "
                         "department layer")

    def test_every_workflow_step_binds_agent_and_data(self):
        for manifest in departments().values():
            for workflow in manifest.workflows:
                self.assertTrue(workflow.steps,
                                f"{manifest.id}:{workflow.id} has no steps")
                for step in workflow.steps:
                    self.assertTrue(step.agent_id,
                                    f"{manifest.id}:{workflow.id}:{step.id} "
                                    "lacks an agent")
                    self.assertTrue(step.instruction,
                                    f"{manifest.id}:{workflow.id}:{step.id} "
                                    "lacks instructions")

    def test_department_evidence_metrics_declared_for_goal_metrics(self):
        manifests = departments()
        self.assertIn("email_batches_sent",
                      manifests["outbound"].evidence_metrics)
        self.assertEqual(
            manifests["outbound"].evidence_metrics["email_batches_sent"],
            ("provider_events",),
            "goal metrics must map to the evidence kinds that prove them")

    def test_metric_util_operators(self):
        self.assertTrue(compare(5, "ge", 1))
        self.assertTrue(compare(1, "le", 1))
        self.assertTrue(compare(2, "eq", 2))
        self.assertFalse(compare(1, "gt", 2))
        # F9(c): an unknown operator raises instead of silently failing
        # closed — a typo must surface, not read as "target missed".
        with self.assertRaises(ValueError) as caught:
            compare(1, "bogus", 0)
        self.assertIn("bogus", str(caught.exception))

    def test_installed_agents_load_into_the_resolution_cycle(self):
        # D6 fixed: installed agent declarations reach the cycle. The layer
        # is <home>/.agents/company/agents/installed (canonical user layer).
        from company.agents import available_agents
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            installed = home / ".agents" / "company" / "agents" / "installed"
            installed.mkdir(parents=True)
            (installed / "seo-operator.json").write_text(json.dumps({
                "id": "seo-operator",
                "description": "SEO operator",
                "skill_ids": ["seo"],
                "permissions": ["write_evidence"],
                "produces": ["seo_audit", "seo_report"],
            }))
            (installed / "broken.json").write_text("{not json")
            agents = available_agents(home)
        self.assertIn("seo-operator", agents,
                      "installed declarations must load as Agent records")
        agent = agents["seo-operator"]
        self.assertEqual(agent.skill_ids, ("seo",))
        self.assertEqual(agent.produces, ("seo_audit", "seo_report"))
        self.assertNotIn("broken", agents,
                         "an unparseable declaration is skipped, not fatal")

    def test_flat_checkout_fallback_finds_the_source_installed_layer(self):
        # A flat source checkout has no .agents tree: its installed layer
        # is the agents package's own installed/ folder. A declaration
        # placed there must be visible to available_agents() when the
        # probed home has no .agents layer of its own.
        import company.agents.loader as loader
        from company.agents import available_agents
        installed = Path(loader.__file__).resolve().parent / "installed"
        declaration = installed / "loader-pin-declaration.json"
        with tempfile.TemporaryDirectory() as directory:
            try:
                installed.mkdir(parents=True, exist_ok=True)
                declaration.write_text(json.dumps(
                    {"id": "loader-pin-agent", "skill_ids": ["pin"]}))
                # The probe order must end at the source checkout's own
                # agents/installed, never at a sibling of the package.
                self.assertEqual(installed, loader._candidate_roots(
                    Path(directory))[-1])
                agents = available_agents(Path(directory))
            finally:
                declaration.unlink(missing_ok=True)
        self.assertIn("loader-pin-agent", agents,
                      "available_agents() must find a declaration placed "
                      "in company/agents/installed under a source-checkout "
                      "layout")

    def test_runtime_passes_installed_agents_to_resolution(self):
        # D6 wiring: CleanCommandRuntime plumbs available_agents() through
        # GoalRuntime into ResolutionCycle.agents. Point the loader at a
        # fixture home so the assertion does not depend on this checkout.
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            installed = home / ".agents" / "company" / "agents" / "installed"
            installed.mkdir(parents=True)
            (installed / "wired-agent.json").write_text(json.dumps(
                {"id": "wired-agent", "skill_ids": ["outbound-email"]}))
            from company.agents import available_agents
            self.assertIn("wired-agent", available_agents(home))
            runtime_db = home / ".spielos" / "state" / "company.sqlite"
            runtime = CleanCommandRuntime(runtime_db)
            self.assertIn("wired-agent", runtime.runtime.resolution.agents,
                          "CleanCommandRuntime must load installed agents "
                          "into the resolution cycle")


# =========================================================================
# 8. SCHEMA INTEGRITY: lineage triggers, evidence immutability
# =========================================================================

class TestSchemaIntegrity(HarnessCase):

    def setUp(self):
        super().setUp()
        self.new_goal(metric="m")
        self.run_id = self.current_run().id

    def test_evidence_is_immutable(self):
        self.runtime.add_evidence(self.goal_id, kind="m", source="host",
                                  payload={"m": 1})
        with self.assertRaises(Exception):
            with self.runtime.connect() as connection:
                connection.execute("UPDATE core_evidence SET kind='hacked'")

    def test_evidence_cannot_be_deleted(self):
        self.runtime.add_evidence(self.goal_id, kind="m", source="host",
                                  payload={"m": 1})
        with self.assertRaises(Exception):
            with self.runtime.connect() as connection:
                connection.execute("DELETE FROM core_evidence")

    def test_intervention_lineage_mismatch_aborts(self):
        other = self.new_goal(name="G2", metric="m2")
        with self.assertRaises(Exception):
            with self.runtime.connect() as connection:
                connection.execute(
                    "INSERT INTO core_interventions VALUES "
                    "(?,?,?,?,?,?,?,?,?,?)",
                    ("i-bad", other["id"], self.run_id, "k", "d", "running",
                     None, "{}", "2026-01-01", "2026-01-01"))

    def test_memory_scope_check_constraint(self):
        with self.assertRaises(Exception):
            with self.runtime.connect() as connection:
                connection.execute(
                    "INSERT INTO core_memory (id,scope,claim,goal_id,run_id,"
                    "intervention_id,workflow_id,evidence_ids_json,created_at)"
                    " VALUES ('m-bad','department','c',NULL,NULL,NULL,NULL,"
                    "'[]','2026')")

    def test_approval_lineage_mismatch_aborts(self):
        other = self.new_goal(name="G3", metric="m3")
        with self.assertRaises(Exception):
            with self.runtime.connect() as connection:
                connection.execute(
                    "INSERT INTO core_approvals VALUES (?,?,?,?,?,?,?,?,?)",
                    ("a-bad", other["id"], self.run_id, None, "k",
                     "approved", None, "2026", "2026"))


# =========================================================================
# 9. RESOLUTION OUTCOMES: fixable, escalate, ask_user, budget, D4 parking
# =========================================================================

class TestResolutionOutcomes(HarnessCase):

    def setUp(self):
        super().setUp()
        self.new_goal(metric="m")

    def _drive_with(self, script, budget=15):
        """Drive the departmentless goal, answering each DECIDE park with
        bounded direct work so the scripted executor actually runs."""
        self.engine.resolution.executor = ScriptedExecutor(script)
        for _ in range(budget):
            self.runtime.tick(max_advances=30)
            run = self.current_run()
            if run.stage == GoalStage.DECIDE and run.status == "waiting":
                self.decide_bounded_work()
                run = self.current_run()
            if run.status in {"waiting", "complete"} or run.sequence > 1:
                return run
        return self.current_run()

    def test_fixable_failures_retry_locally_with_iteration_evidence(self):
        run = self._drive_with([
            AgentResult("fixable", message="transient"),
            AgentResult("completed", evidence=(AgentEvidence("m", {"m": 1}),)),
        ])
        kinds = [e.kind for e in self.runtime.evidence.for_goal(self.goal_id)]
        self.assertIn("resolution_iteration", kinds,
                      "local fixes must be recorded as iteration evidence")
        self.assertEqual(run.sequence, 1,
                         "a fixed-locally failure must not consume the run")

    def test_escalation_completes_run_and_opens_next_goal_run(self):
        run = self._drive_with([AgentResult("escalate", message="invalid")])
        self.assertGreater(run.sequence, 1,
                           "escalation must return control to a fresh run")
        with self.runtime.connect() as connection:
            outcome = connection.execute(
                "SELECT resolution_outcome FROM core_interventions"
            ).fetchone()[0]
        self.assertEqual(outcome, "ESCALATE_TO_GOAL")

    def test_repeated_escalation_parks_after_threshold(self):
        # D4 fixed (inverted pin): three consecutive escalations park the
        # goal for the owner instead of spinning new runs forever. Each
        # new run first parks a DECIDE ask (the DECIDE boundary), which the
        # driver answers with bounded direct work.
        from company.runtime.engine import ESCALATION_PARK_THRESHOLD
        self.engine.resolution.executor = ScriptedExecutor(
            [AgentResult("escalate", message="boom")] * 200)
        for _ in range(40):
            self.runtime.tick(max_advances=50)
            run = self.current_run()
            if run.stage == GoalStage.DECIDE and run.status == "waiting":
                self.decide_bounded_work()
            elif run.status == "waiting":
                break  # the escalation park
        runs = len(self.runtime.runs._get_all(self.goal_id)) \
            if hasattr(self.runtime.runs, "_get_all") else None
        with self.runtime.connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM core_runs WHERE goal_id=?",
                (self.goal_id,)).fetchone()[0]
            status = connection.execute("""SELECT status FROM core_runs
                WHERE goal_id=? ORDER BY sequence DESC LIMIT 1""",
                (self.goal_id,)).fetchone()[0]
        self.assertLessEqual(count, ESCALATION_PARK_THRESHOLD + 1,
                             "escalation must stop opening runs")
        self.assertEqual(status, "waiting",
                         "the escalated goal must park for the owner")
        attention = self.runtime.attention(goal_id=self.goal_id)
        self.assertTrue(any("escalation" in item.get("message", "").lower()
                            for item in attention),
                        "a parked escalation must surface owner attention")

    def test_ask_user_parks_run_and_notification(self):
        run = self._drive_with([])
        self.assertEqual((run.stage, run.status), (GoalStage.ACT, "waiting"))
        self.assertEqual(len(self.runtime.attention(goal_id=self.goal_id)), 1)

    def test_resume_continues_parked_run_after_host_answer(self):
        self._drive_with([])
        run = self.current_run()
        self.assertEqual(run.status, "waiting")
        order = self.active_orders()[0]
        self.runtime.complete_work_order(
            order["id"], "director",
            [{"kind": "m", "payload": {"m": 1}}])
        run = self.current_run()
        self.assertEqual(run.stage, GoalStage.EVALUATE,
                         "answering the parked ask must continue the run")

    def test_local_iteration_budget_yields_continue_local(self):
        # When the host keeps fixing forever without completing, the cycle
        # must stop at max_local_iterations and park, not loop forever.
        self.engine.resolution.executor = ScriptedExecutor(
            [AgentResult("fixable", message="forever")] * 200)
        self.engine.resolution.max_local_iterations = 3
        for _ in range(10):
            self.runtime.tick(max_advances=30)
        run = self.current_run()
        self.assertIn(run.status, {"waiting", "ready", "running"})


# =========================================================================
# 9a. DECIDE DECIDES FROM RUN HISTORY + INJECTABLE SEAMS (audit F6)
# =========================================================================

HISTORY_LAB_DEPARTMENT = '''"""Minimal two-candidate Department for the DECIDE-from-history pins."""

from __future__ import annotations

from ...workflows import Workflow, WorkflowStep


class HistoryLabDepartment:
    department_id = "history-lab"
    id = "history-lab"
    version = "1.0.0"
    description = "two candidate workflows for run-history decisions"
    agent_ids = ("director",)
    workflows = (
        Workflow("alpha", "Workflow A", (
            WorkflowStep("a-one", "director", "run workflow A",
                         evidence_kind="twin_metric"),),
            department_id="history-lab"),
        Workflow("beta", "Workflow B", (
            WorkflowStep("b-one", "director", "run workflow B",
                         evidence_kind="twin_metric"),),
            department_id="history-lab"),
    )
    evidence_metrics = {"twin_metric": ("twin_metric",)}
    goal_schema = {"metrics": ["twin_metric"]}
'''


@with_departments
class TestDecideFromHistory(HarnessCase):
    """F6: DECIDE chooses among candidate workflows using run history.

    A candidate whose most recent execution on this goal completed without
    moving the metric is excluded; the first remaining candidate in
    declaration order runs; when no candidate remains, DECIDE parks a
    decision_request instead of forcing a choice. ``decide()`` never
    writes the Workflow definition — the definition travels with the
    Decision and becomes durable at ACT, or when the owner adopts.
    """

    @classmethod
    def setUpClass(cls):
        # A minimal inline two-workflow declaration through the same
        # SPIELOS_TEST_DEPARTMENTS_DIR seam the fixtures use; the shipped
        # fixtures are never modified.
        import shutil

        cls._departments = Path(tempfile.mkdtemp(prefix="spielos-history-lab-"))
        package = cls._departments / "history_lab"
        package.mkdir()
        (package / "__init__.py").write_text("")
        (package / "department.py").write_text(HISTORY_LAB_DEPARTMENT)
        os.environ["SPIELOS_TEST_DEPARTMENTS_DIR"] = str(cls._departments)
        cls.addClassCleanup(shutil.rmtree, cls._departments, True)

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("SPIELOS_TEST_DEPARTMENTS_DIR", None)

    def setUp(self):
        super().setUp()
        self.new_goal(name="Twin metric", owner="history-lab",
                      metric="twin_metric", target=1, aggregation="latest")
        self.engine.resolution.executor = completing_executor(
            payload={"twin_metric": 0})

    def _decisions_by_sequence(self) -> dict[int, str | None]:
        with self.runtime.connect() as connection:
            rows = connection.execute(
                """SELECT sequence,decision_json FROM core_runs
                   WHERE goal_id=? ORDER BY sequence""",
                (self.goal_id,)).fetchall()
        return {row[0]: (json.loads(row[1]).get("kind")
                         if row[1] else None) for row in rows}

    def _workflow_decisions(self) -> dict[int, str]:
        with self.runtime.connect() as connection:
            rows = connection.execute(
                """SELECT sequence,decision_json FROM core_runs
                   WHERE goal_id=? AND decision_json IS NOT NULL
                   ORDER BY sequence""", (self.goal_id,)).fetchall()
        return {row[0]: json.loads(row[1]).get("workflow_id")
                for row in rows if json.loads(row[1]).get("workflow_id")}

    def _workflow_definitions(self) -> list[str]:
        with self.runtime.connect() as connection:
            return [row[0] for row in connection.execute(
                "SELECT id FROM core_workflows ORDER BY id")]

    def _drive_to_park_or_sequence(self, sequence: int) -> None:
        from company.runtime.engine import GoalStage
        for _ in range(60):
            self.runtime.tick(max_advances=50)
            run = self.current_run()
            if (run.stage == GoalStage.DECIDE and run.status == "waiting"
                    and run.decision is not None
                    and run.decision.kind == "decision_request"):
                return
            if run.sequence >= sequence and run.stage == GoalStage.OBSERVE:
                return

    def test_flat_workflow_is_excluded_and_the_next_candidate_runs(self):
        # F6 pin: workflow A's first execution leaves twin_metric at 0, so
        # the next DECIDE excludes A and selects B in declaration order.
        self._drive_to_park_or_sequence(2)
        decisions = self._workflow_decisions()
        self.assertEqual(decisions.get(1), "history-lab:alpha",
                         "a fresh goal binds the first declared candidate")
        self.assertEqual(decisions.get(2), "history-lab:beta",
                         "after A left the metric flat, DECIDE selects B")

    def test_when_every_candidate_is_excluded_decide_parks(self):
        # F6 pin: after A and B both leave the metric flat, no candidate
        # remains and DECIDE parks a decision_request instead of forcing
        # a choice.
        self._drive_to_park_or_sequence(3)
        decisions = self._workflow_decisions()
        self.assertEqual(sorted(decisions.values()),
                         ["history-lab:alpha", "history-lab:beta"])
        run = self.current_run()
        self.assertEqual((run.stage, run.status), (GoalStage.DECIDE, "waiting"))
        self.assertEqual(run.decision.kind, "decision_request")
        attention = self.runtime.attention(goal_id=self.goal_id)
        self.assertEqual(len(attention), 1, "one owner ask for the park")
        request = (run.decision.context or {}).get("decision_request") or {}
        offered = [f"{item['id']}:{workflow_id}"
                   for item in request.get("candidates", {}).get("departments", [])
                   for workflow_id in item.get("workflows", [])]
        self.assertIn("history-lab:alpha", offered,
                      "the owner may still choose an excluded candidate")

    def test_decide_never_writes_the_workflow_definition(self):
        # F6 pin: core_workflows stays empty through a department-owned
        # DECIDE; the definition the Decision declared becomes durable
        # only when the run reaches ACT (or the owner adopts).
        self.runtime.once(self.goal_id)  # OBSERVE -> DECIDE
        self.runtime.once(self.goal_id)  # DECIDE chose; the run moves to ACT
        run = self.current_run()
        self.assertEqual((run.stage, run.status), (GoalStage.ACT, "running"))
        self.assertEqual(run.decision.kind, "execute_workflow")
        self.assertEqual(run.decision.workflow_id, "history-lab:alpha")
        self.assertEqual(self._workflow_definitions(), [],
                         "DECIDE must not write to core_workflows")
        self.runtime.once(self.goal_id)  # ACT persists the declaration
        self.assertEqual(self._workflow_definitions(), ["history-lab:alpha"],
                         "ACT persists the definition the Decision declared")

    def test_a_parked_decide_adds_no_workflow_rows_until_adoption(self):
        # F6 pin: through a parked decision_request the table never grows;
        # `goal decide` adoption is what binds and writes next.
        self._drive_to_park_or_sequence(3)
        self.assertEqual(sorted(self._workflow_decisions().values()),
                         ["history-lab:alpha", "history-lab:beta"])
        definitions = self._workflow_definitions()
        for _ in range(3):
            self.runtime.tick(max_advances=30)  # the park is idempotent
        self.assertEqual(self._workflow_definitions(), definitions,
                         "a parked DECIDE writes no workflow definitions")
        # Adoption binds the owner's chosen workflow and parks its work.
        self.engine.resolution.executor = AssignmentExecutor()
        self.runtime.decide_goal(self.goal_id, "execute_workflow",
                                 workflow="history-lab:alpha")
        run = self.current_run()
        self.assertEqual((run.decision.kind, run.decision.workflow_id),
                         ("execute_workflow", "history-lab:alpha"),
                         "adoption binds the owner's chosen workflow")
        orders = self.active_orders()
        self.assertEqual([order["step_id"] for order in orders], ["a-one"],
                         "the adopted workflow parks its first step")

    def test_a_moved_workflow_is_not_excluded(self):
        # F6 pin: exclusion compares the metric before and after the
        # workflow run — a workflow that moved the metric stays a
        # candidate and DECIDE re-selects it.
        self.engine.resolution.executor = completing_executor(
            payload={"twin_metric": 1})
        self._drive_to_park_or_sequence(2)
        self.assertEqual(self.runtime.goals.get(self.goal_id).status,
                         "complete",
                         "a workflow that moves the metric completes the goal")


class _RecordingController:
    """Controller double for the injectable-seam pins: decides bounded
    direct work the default AssignmentExecutor parks for the host, and
    records every GoalContext it is handed."""

    def __init__(self, completes: bool = True):
        self.completes = completes
        self.contexts: list = []

    def observe(self, context):
        self.contexts.append(("observe", context))
        return {context.goal.metric: 0}

    def decide(self, context, observation):
        self.contexts.append(("decide", context))
        return Decision("request_agent", "produce the metric evidence", None,
                        {"agent_id": "director",
                         "evidence_kind": context.goal.metric})

    def evaluate(self, context, decision, evidence):
        self.contexts.append(("evaluate", context))
        return Evaluation(self.completes and bool(evidence),
                          {context.goal.metric: 1 if self.completes else 0},
                          "bounded direct work completed")


class TestInjectableSeams(HarnessCase):
    """F6: the GoalController/AgentExecutor seams are injectable at
    CleanCommandRuntime, with today's defaults when omitted."""

    def test_a_custom_controller_drives_a_run_end_to_end(self):
        controller = _RecordingController()
        runtime = CleanCommandRuntime(self.db, controller=controller)
        row = runtime.create_goal(
            name="Seam goal", owner_id="director", metric="seam_metric",
            operator="ge", target=1, config={"aggregation": "latest"})
        goal_id = row["id"]
        runtime.tick(max_advances=10)
        orders = runtime.work_orders(status="active", goal_id=goal_id)
        self.assertEqual(len(orders), 1,
                         "the injected controller's decision parks work")
        runtime.complete_work_order(
            orders[0]["id"], "director",
            [{"kind": "seam_metric", "payload": {"seam_metric": 1}}])
        runtime.tick(max_advances=10)
        self.assertEqual(runtime.goals.get(goal_id).status, "complete",
                         "the custom controller drives the run end to end")
        self.assertTrue(any(stage == "evaluate" for stage, _ in
                            controller.contexts),
                        "the injected controller evaluated the run")
        self.assertEqual(runtime.runtime.controller, controller,
                         "the runtime uses the injected controller seam")

    def test_a_custom_executor_is_injected_at_the_same_seam(self):
        class _Parked:
            def execute(self, agent, order):
                return AgentResult(
                    "ask_user", message=f"injected executor parked {order.id}")

        executor = _Parked()
        runtime = CleanCommandRuntime(self.db, executor=executor)
        row = runtime.create_goal(
            name="Executor seam goal", owner_id="director", metric="m",
            operator="ge", target=1, config={"aggregation": "latest"})
        goal_id = row["id"]
        runtime.tick(max_advances=10)
        # A departmentless goal parks a decision_request first; answer it
        # so the injected executor runs its order.
        runtime.decide_goal(goal_id, "request_agent", agent="director",
                            instruction="produce the metric evidence")
        attention = runtime.attention(goal_id=goal_id)
        self.assertTrue(any("injected executor parked" in item.get("message", "")
                            for item in attention),
                        "the injected executor executed the work order")
        self.assertEqual(runtime.runtime.resolution.executor, executor,
                         "the runtime uses the injected executor seam")

    def test_the_default_seams_are_unchanged_when_omitted(self):
        runtime = CleanCommandRuntime(self.db)
        self.assertIsInstance(runtime.runtime.controller, CatalogController)
        self.assertIsInstance(runtime.runtime.resolution.executor,
                              AssignmentExecutor)

    def test_context_carries_children_blockers_and_decisions(self):
        controller = _RecordingController(completes=False)
        runtime = CleanCommandRuntime(self.db, controller=controller)
        row = runtime.create_goal(
            name="Focus goal", owner_id="director", metric="focus_metric",
            operator="ge", target=1, config={"aggregation": "latest"})
        goal_id = row["id"]
        runtime.create_goal(
            name="Child goal", owner_id="director", metric="child_metric",
            operator="ge", target=1, parent_id=goal_id,
            config={"aggregation": "latest"})
        paused = runtime.create_goal(
            name="Paused child", owner_id="director", metric="paused_metric",
            operator="ge", target=1, parent_id=goal_id,
            config={"aggregation": "latest"})
        runtime.goals.set_status(paused["id"], "paused")
        blocker = runtime.create_goal(
            name="Blocking goal", owner_id="director", metric="block_metric",
            operator="ge", target=1, config={"aggregation": "latest"})
        runtime.goals.add_block(blocker["id"], goal_id)
        # A blocked goal is never scheduled, so drive its loop directly:
        # two full cycles (DECIDE -> ACT parks the order -> the host
        # completes it -> EVALUATE chains) build the run history the next
        # DECIDE context must carry.
        runtime.runtime.resolution.executor = completing_executor(
            payload={"focus_metric": 0})
        for _ in range(2):
            runtime.once(goal_id)  # OBSERVE -> DECIDE
            runtime.once(goal_id)  # DECIDE -> ACT parks the direct order
            for order in runtime.work_orders(status="active", goal_id=goal_id):
                runtime.complete_work_order(
                    order["id"], "director",
                    [{"kind": "focus_metric",
                      "payload": {"focus_metric": 0}}])
            runtime.once(goal_id)  # EVALUATE -> chains the next run
        runtime.once(goal_id)  # the next run reaches DECIDE
        decide_contexts = [context for stage, context in controller.contexts
                           if stage == "decide" and context.goal.id == goal_id]
        self.assertTrue(decide_contexts, "DECIDE ran with the injected seam")
        context = decide_contexts[-1]
        self.assertEqual([item.name for item in context.children],
                         ["Child goal"],
                         "only the active children are carried")
        self.assertEqual([item.name for item in context.blockers],
                         ["Blocking goal"],
                         "the incomplete blockers are carried")
        decisions = context.decisions
        self.assertTrue(decisions, "the decision history is carried")
        self.assertEqual([item["sequence"] for item in decisions],
                         sorted((item["sequence"] for item in decisions),
                                reverse=True),
                         "the decision history is most recent first")
        self.assertEqual(decisions[0]["kind"], "request_agent")
        self.assertEqual(decisions[0]["resolution_outcome"],
                         "RETURN_TO_GOAL",
                         "each entry carries the run's resolution outcome")
        self.assertEqual(decisions[-1]["sequence"], 1)
        self.assertLessEqual(len(decisions), 3,
                             "at most the last 3 decided runs are carried")


# =========================================================================
# 9b. RUNTIME CORRECTNESS: per-goal failure isolation + single-owner
#     Run claims (audit F1 + F2)
# =========================================================================

class _DirectWorkController:
    """Controller double: always decides bounded direct work the scripted
    executor can complete, so DECIDE→ACT→EVALUATE all run without a
    Department or fixture dependency."""

    def observe(self, context):
        return {context.goal.metric: 0}

    def decide(self, context, observation):
        return Decision("request_agent", "produce the metric evidence", None,
                        {"agent_id": "director", "evidence_kind": "m"})

    def evaluate(self, context, decision, evidence):
        return Evaluation(False, {context.goal.metric: 0},
                          "clean-core evidence evaluated")


class TestPerGoalFailureIsolation(HarnessCase):
    """F1: one broken goal cannot starve the scheduler."""

    def setUp(self):
        super().setUp()
        self.goal_a = self.new_goal(name="Broken goal", metric="m_a")
        self.goal_b = self.other_goal(name="Healthy goal B", metric="m_b")
        self.goal_c = self.other_goal(name="Healthy goal C", metric="m_c")

    def test_one_raising_goal_parks_while_siblings_advance(self):
        # A controller/Department that raises inside one goal's DECIDE must
        # park that goal (runtime_failure evidence + one owner ask, run
        # waiting) while the sibling goals in the same tick still advance.
        broken = self.goal_a["id"]

        class _FailingForGoal:
            """Wraps the real controller; raises for goal A's DECIDE only."""

            def __init__(self, inner):
                self.inner = inner

            def observe(self, context):
                return self.inner.observe(context)

            def decide(self, context, observation):
                if context.goal.id == broken:
                    raise RuntimeError(
                        "department catalog exploded for this goal")
                return self.inner.decide(context, observation)

            def evaluate(self, context, decision, evidence):
                return self.inner.evaluate(context, decision, evidence)

        original = self.engine.controller
        self.engine.controller = _FailingForGoal(original)
        try:
            result = self.engine.tick(max_advances=10)
        finally:
            self.engine.controller = original

        # The tick itself stays coherent instead of raising.
        self.assertIsInstance(result, dict)
        self.assertIn("advanced", result)
        self.assertIn("quiescent", result)

        # The broken goal parked: run waiting, durable runtime_failure
        # evidence, exactly one pending owner ask.
        run_a = self.runtime.runs.current(broken)
        self.assertEqual(run_a.status, "waiting",
                         "a raising goal must park its run at waiting")
        with self.runtime.connect() as connection:
            failures = connection.execute(
                "SELECT COUNT(*) FROM core_evidence WHERE run_id=? "
                "AND kind='runtime_failure'", (run_a.id,)).fetchone()[0]
            asks = connection.execute(
                "SELECT COUNT(*) FROM core_notifications WHERE goal_id=? "
                "AND kind='owner_input_required' AND status='pending'",
                (broken,)).fetchone()[0]
        self.assertEqual(failures, 1,
                         "the failure must be recorded exactly once as "
                         "runtime_failure evidence")
        self.assertEqual(asks, 1,
                         "exactly one owner ask per failing run")
        self.assertNotIn(run_a.id,
                          [r.id for r in self.runtime.runs.ready()],
                          "a parked failing run must stop being scheduled")

        # The sibling goals still advanced: each left OBSERVE.
        for healthy in (self.goal_b, self.goal_c):
            run = self.runtime.runs.current(healthy["id"])
            self.assertNotEqual(run.stage, GoalStage.OBSERVE,
                                f"{healthy['name']} must still advance")
        # The failing goal's DECIDE never produced a decision: it parked
        # at the stage whose seam raised.
        self.assertEqual(run_a.stage, GoalStage.DECIDE)
        self.assertIsNone(run_a.decision,
                          "a raising DECIDE must not persist a decision")


class TestSingleOwnerRunClaims(unittest.TestCase):
    """F2: two workers over one database cannot double-advance a Run.

    Every stage/status transition is a compare-and-swap: the worker whose
    guarded UPDATE matches no row (because the other worker moved the
    run first) returns the current state idempotently — no raise, no
    duplicated Intervention, WorkflowRun, WorkOrder, or decision row.
    """

    def setUp(self):
        self.db = temp_db()
        # Two independent runtimes (two workers) over the same database
        # file, same controller/executor types.
        self.runtime1 = CleanCommandRuntime(self.db)
        self.runtime2 = CleanCommandRuntime(self.db)
        self.engine1 = self.runtime1.runtime
        self.engine2 = self.runtime2.runtime
        for engine in (self.engine1, self.engine2):
            engine.controller = _DirectWorkController()
            engine.resolution.executor = completing_executor(
                payload={"m": 1})
        row = self.runtime1.create_goal(
            name="Contested goal", owner_id="director", metric="m",
            operator="ge", target=1,
            config={"aggregation": "latest"})
        self.goal_id = row["id"]

    def tearDown(self):
        self.db.unlink(missing_ok=True)

    def _counts(self):
        """Row invariants that must never grow when a CAS loses."""
        with self.runtime1.connect() as connection:
            runs = connection.execute(
                "SELECT COUNT(*) FROM core_runs WHERE goal_id=?",
                (self.goal_id,)).fetchone()[0]
            interventions = connection.execute(
                "SELECT COUNT(*) FROM core_interventions WHERE goal_id=?",
                (self.goal_id,)).fetchone()[0]
            workflow_runs = connection.execute(
                "SELECT COUNT(*) FROM core_workflow_runs WHERE goal_id=?",
                (self.goal_id,)).fetchone()[0]
            work_orders = connection.execute(
                "SELECT COUNT(*) FROM core_work_orders WHERE goal_id=?",
                (self.goal_id,)).fetchone()[0]
        return {"runs": runs, "interventions": interventions,
                "workflow_runs": workflow_runs, "work_orders": work_orders}

    def _decision_row(self):
        with self.runtime1.connect() as connection:
            return connection.execute(
                "SELECT decision_json FROM core_runs WHERE id=?",
                (self.runtime1.runs.current(self.goal_id).id,)).fetchone()[0]

    def test_cas_lost_advance_returns_current_status_without_raising(self):
        # Worker 2 holds the DECIDE claim; while it is mid-decide, worker
        # 1 steals the DECIDE→ACT transition underneath it. Worker 2's
        # compare-and-swap matches no row: it must return the current
        # status idempotently — no raise, no second decision, no rows.
        self.engine1.advance(self.goal_id)  # OBSERVE→DECIDE
        self.assertEqual(self.engine1.runs.current(self.goal_id).stage,
                         GoalStage.DECIDE)
        stolen = {}

        class _RacingDecide(_DirectWorkController):
            """Worker 2's decide seam: worker 1 steals the transition."""

            def __init__(self, engine):
                self.engine = engine

            def decide(self, context, observation):
                stolen["state"] = self.engine.advance(context.goal.id)
                return Decision("request_agent", "worker 2 lost the race",
                                None, {"agent_id": "director",
                                       "evidence_kind": "m"})

        self.engine2.controller = _RacingDecide(self.engine1)
        try:
            loser = self.engine2.advance(self.goal_id)
        finally:
            self.engine2.controller = _DirectWorkController()

        # No raise; the loser sees the current (re-read) state.
        self.assertEqual(loser["run"].stage, GoalStage.ACT)
        self.assertEqual(loser["run"].status, "running")
        self.assertEqual(loser["run"].sequence, 1)
        # The winner's decision is the only persisted one — the loser's
        # racing decision never overwrote it.
        self.assertIn("produce the metric evidence", self._decision_row())
        self.assertNotIn("worker 2 lost the race", self._decision_row())
        self.assertEqual(self._counts(),
                         {"runs": 1, "interventions": 0,
                          "workflow_runs": 0, "work_orders": 0})

    def test_alternating_workers_never_duplicate_run_rows(self):
        # Drive both workers alternately through the whole loop; at every
        # step the structural invariants hold: at most one Intervention
        # per run-stage transition, exactly the expected WorkflowRun and
        # WorkOrder rows, and one stage boundary per advance.
        before = self.engine1.runs.current(self.goal_id)
        self.assertEqual((before.stage, before.status),
                         (GoalStage.OBSERVE, "ready"))
        # OBSERVE→DECIDE on worker 1, then a quick double-advance on the
        # same instance: the second call sees the CAS-guarded state.
        self.engine1.advance(self.goal_id)
        self.engine1.advance(self.goal_id)  # DECIDE→ACT (fresh claim)
        run = self.engine1.runs.current(self.goal_id)
        self.assertEqual((run.stage, run.status), (GoalStage.ACT, "running"))
        self.assertEqual(self._counts()["interventions"], 0)

        # ACT on worker 2: the one Intervention is created, the executor
        # completes the direct order, the run reaches EVALUATE.
        state = self.engine2.advance(self.goal_id)
        self.assertEqual(state["run"].stage, GoalStage.EVALUATE)
        with self.runtime2.connect() as connection:
            interventions = connection.execute(
                "SELECT COUNT(*) FROM core_interventions WHERE run_id=?",
                (state["run"].id,)).fetchone()[0]
        self.assertLessEqual(interventions, 1,
                             "never two Interventions for one run-stage "
                             "transition")
        self.assertEqual(self._counts(),
                         {"runs": 1, "interventions": 1,
                          "workflow_runs": 0, "work_orders": 1})

        # EVALUATE on worker 1: the run completes and chains exactly one
        # follow-on run.
        self.engine1.advance(self.goal_id)
        counts = self._counts()
        self.assertEqual(counts["runs"], 2,
                         "the completed run chains exactly one follow-on run")
        self.assertEqual(counts["interventions"], 1)
        current = self.engine1.runs.current(self.goal_id)
        self.assertEqual((current.stage, current.sequence),
                         (GoalStage.OBSERVE, 2))

    def test_parked_act_readvanced_by_both_workers_duplicates_nothing(self):
        # The AssignmentExecutor parks ACT as HOST WORK (intentional
        # semantic change, issue #5 of goal-d62825bb0b23); both workers
        # then re-advance the parked run. The idempotent CAS plus the
        # intervention-keyed notification conflict keep every row
        # singular: no duplicate orders, asks, or interventions.
        for engine in (self.engine1, self.engine2):
            engine.resolution.executor = AssignmentExecutor()
        self.engine1.advance(self.goal_id)  # OBSERVE→DECIDE
        self.engine1.advance(self.goal_id)  # DECIDE→ACT
        parked = self.engine1.advance(self.goal_id)  # ACT parks host work
        self.assertEqual((parked["run"].stage, parked["run"].status),
                         (GoalStage.ACT, "waiting"))
        self.assertEqual(self._counts(),
                         {"runs": 1, "interventions": 1,
                          "workflow_runs": 0, "work_orders": 1})
        with self.runtime1.connect() as connection:
            asks = connection.execute(
                """SELECT COUNT(*) FROM core_notifications
                   WHERE goal_id=? AND kind='host_work_required'
                     AND status='pending'""", (self.goal_id,)).fetchone()[0]
        self.assertEqual(asks, 1, "exactly one host-work ask for the parked run")
        with self.runtime1.connect() as connection:
            owner_asks = connection.execute(
                """SELECT COUNT(*) FROM core_notifications
                   WHERE goal_id=? AND kind='owner_input_required'
                     AND status='pending'""", (self.goal_id,)).fetchone()[0]
        self.assertEqual(owner_asks, 0,
                         "ordinary parked work never asks the owner")

        # Both workers re-advance the parked run: idempotent, no growth.
        self.engine2.advance(self.goal_id)
        self.engine1.advance(self.goal_id)
        self.assertEqual(self._counts(),
                         {"runs": 1, "interventions": 1,
                          "workflow_runs": 0, "work_orders": 1},
                         "re-advancing a parked run duplicates nothing")
        with self.runtime2.connect() as connection:
            asks = connection.execute(
                """SELECT COUNT(*) FROM core_notifications
                   WHERE goal_id=? AND kind='host_work_required'
                     AND status='pending'""", (self.goal_id,)).fetchone()[0]
        self.assertEqual(asks, 1, "the parked ask stays singular")



# =========================================================================
# 10. GOAL RELATIONSHIPS & SCHEDULING (supports/blocks/priority)
# =========================================================================

class TestGoalTopologyAndScheduling(HarnessCase):

    def test_blocks_edge_prevents_ready_run(self):
        primary = self.new_goal(name="Primary")
        blocked = self.new_goal(name="Blocked")
        self.runtime.goals.add_block(primary["id"], blocked["id"])
        ready = [r.goal_id for r in self.runtime.runs.ready()]
        self.assertNotIn(blocked["id"], ready,
                         "a Goal blocked by an incomplete prerequisite must "
                         "never appear ready")

    def test_priority_orders_ready_runs(self):
        self.new_goal(name="low", priority="low")
        critical = self.new_goal(name="crit", priority="critical")
        ready = [r.goal_id for r in self.runtime.runs.ready()]
        others = [g["id"] for g in self.runtime.goal_summaries()
                  if g["id"] != critical["id"]]
        self.assertTrue(ready.index(critical["id"]) < max(
            ready.index(g) for g in others if g in ready),
            "critical priority must be scheduled first")

    def test_topology_audit_detects_missing_parent(self):
        # Create a real orphan goal: the audit must flag it. Written with
        # a raw connection (no FK pragma) to simulate a parent row that
        # vanished in an older database.
        import sqlite3
        parent = self.new_goal(name="P")
        self.new_goal(name="C", parent_id=parent["id"])
        goal_id = "goal-orphan-1"
        raw = sqlite3.connect(self.db)
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute(
            "INSERT INTO core_goals VALUES (?,?,?,?,?,?,?,?,?)",
            (goal_id, "Orphan", "m", "ge", "1", "goal-missing-parent",
             "active", "2026-01-01", "2026-01-01"))
        raw.execute(
            "INSERT INTO core_goal_metadata VALUES (?,?,?,?)",
            (goal_id, "director", None, "{}"))
        raw.commit()
        raw.close()
        audit = self.runtime.topology_audit()
        kinds = [d["kind"] for d in audit["defects"]]
        self.assertIn("missing_parent", kinds)
        defect = next(d for d in audit["defects"]
                      if d["kind"] == "missing_parent")
        self.assertEqual(defect["parent_id"], "goal-missing-parent")

    def test_multiple_independent_root_goals_are_not_a_defect(self):
        # F9(b) pin: a healthy home with several independent root goals
        # reports its root ids with NO disconnected_non_primary_root
        # defect — only a single-root home names a canonical root.
        first = self.new_goal(name="First root")
        second = self.new_goal(name="Second root")
        third = self.runtime.create_goal(
            name="Child of second", owner_id="director", metric="m",
            operator="ge", target=1, parent_id=second["id"],
            config={"aggregation": "latest"})
        audit = self.runtime.topology_audit()
        self.assertEqual(sorted(audit["root_goal_ids"]),
                         sorted([first["id"], second["id"]]),
                         "the independent root ids are reported")
        self.assertEqual(audit["defects"], [],
                         "independent roots are not a topology defect")
        self.assertIsNone(audit["canonical_root_goal_id"],
                          "a multi-root home names no canonical root")

    def test_cycle_edges_rejected(self):
        a = self.new_goal(name="A")
        b = self.new_goal(name="B")
        self.runtime.goals.add_block(a["id"], b["id"])
        with self.assertRaises(ValueError):
            self.runtime.goals.add_block(b["id"], a["id"])

    def test_parent_cycle_rejected(self):
        a = self.new_goal(name="A")
        b = self.new_goal(name="B", parent_id=a["id"])
        with self.assertRaises(ValueError):
            self.runtime.goals.set_parent(a["id"], b["id"])

    def test_all_children_achieved_rollup(self):
        parent = self.new_goal(name="All children",
                               metric="all_children_achieved",
                               operator="eq", target=1)
        child = self.new_goal(name="child", parent_id=parent["id"])
        context = self.engine._context(
            self.engine.goals.get(parent["id"]),
            self.engine.runs.current(parent["id"]))
        observation = self.engine.controller.observe(context)
        self.assertFalse(observation["all_children_achieved"])
        self.engine.goals.set_status(child["id"], "complete")
        context = self.engine._context(
            self.engine.goals.get(parent["id"]),
            self.engine.runs.current(parent["id"]))
        observation = self.engine.controller.observe(context)
        self.assertTrue(observation["all_children_achieved"])


# =========================================================================
# 11. EVALS (LLM-as-judge Lego piece)
# =========================================================================

class TestEvalEngine(unittest.TestCase):

    def _suite(self, criteria, thresholds=None, item_selector=None):
        from company.evals.models import EvalCriterion, EvalSuite
        return EvalSuite(
            id="s", name="S", scope="x", department_id="content",
            payload_kind="campaign_manifest", criteria=criteria,
            thresholds=thresholds or {},
            item_selector=item_selector)

    def test_suite_validation_and_report_computation(self):
        from company.evals.models import EvalCriterion
        from company.evals.engine import run_suite, report_to_evidence
        suite = self._suite(
            (EvalCriterion("c1", "One", "d", "src"),
             EvalCriterion("c2", "Two", "d", "src")),
            thresholds={"all_pass": True},
            item_selector=lambda payload: [(i["item_id"], i)
                                           for i in payload["items"]])
        payload = {"batch_id": "b1", "items": [{"item_id": "it1"}]}
        verdicts = {"items": {"it1": {
            "c1": {"pass": True, "score": 1.0, "reason": "ok"},
            "c2": {"pass": False, "score": 0.4, "reason": "weak"}}}}
        report = run_suite(suite, payload, verdicts)
        self.assertFalse(report.overall)
        self.assertEqual(report.failed_criteria(), ["it1:c2"])
        evidence = report_to_evidence(report)
        self.assertFalse(evidence["overall"])
        self.assertIn("per_item", evidence)

    def test_invalid_verdict_document_rejected(self):
        from company.evals.models import EvalCriterion
        from company.evals.engine import run_suite
        suite = self._suite((EvalCriterion("c1", "One", "d", "src"),))
        with self.assertRaises(ValueError):
            run_suite(suite, {"items": []}, {"items": {"missing": {}}})

    def test_warn_criteria_advisory_unless_all_pass(self):
        from company.evals.models import EvalCriterion
        from company.evals.engine import run_suite
        suite = self._suite((EvalCriterion("c1", "One", "d", "src",
                                           severity="warn"),))
        payload = {"id": "i"}
        verdicts = {"items": {"i": {"c1": {"pass": False, "score": 0.1,
                                           "reason": "warn only"}}}}
        report = run_suite(suite, payload, verdicts)
        self.assertTrue(report.overall,
                        "warn criteria must not gate by default")

    def test_all_pass_threshold_makes_warn_criteria_gate(self):
        from company.evals.models import EvalCriterion
        from company.evals.engine import run_suite
        suite = self._suite((EvalCriterion("c1", "One", "d", "src",
                                           severity="warn"),),
                            thresholds={"all_pass": True})
        payload = {"id": "i"}
        verdicts = {"items": {"i": {"c1": {"pass": False, "score": 0.1,
                                           "reason": "warn gates now"}}}}
        report = run_suite(suite, payload, verdicts)
        self.assertFalse(report.overall,
                         "all_pass=true must make even warn criteria gate")


@with_departments
class TestShippedEvalSuites(unittest.TestCase):
    """Fixture suites (content/design) must import with real sources."""

    @classmethod
    def setUpClass(cls):
        os.environ["SPIELOS_TEST_DEPARTMENTS_DIR"] = str(
            FIXTURES / "departments")

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("SPIELOS_TEST_DEPARTMENTS_DIR", None)

    def test_content_and_design_suites_importable_with_real_sources(self):
        # Eval criteria ground in source files. In a real home those live in
        # the preserved user layers; in the source fixture tree the
        # referenced paths are rewritten to the fixture home layout, so we
        # assert importability and structure (ids, departments, criteria).
        from company.evals.registry import suites
        found = suites()
        self.assertTrue(any(s.department_id == "content" for s in found.values()))
        self.assertTrue(any(s.department_id == "design" for s in found.values()))
        for suite in found.values():
            for criterion in suite.criteria:
                self.assertTrue(criterion.id and criterion.description,
                                f"{suite.id} has a hollow criterion")


# =========================================================================
# 12. CLI SURFACE (the documented host command vocabulary)
# =========================================================================

class TestCLISurface(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import json as jsonlib
        import subprocess
        cls.json = jsonlib
        cls.subprocess = subprocess
        handle = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        handle.close()
        cls.db = Path(handle.name)
        cls.db.unlink()
        cls.run_cli("goal", "create", "--name", "CLI goal",
                    "--owner", "director", "--metric", "m",
                    "--target", "1")

    @classmethod
    def run_cli(cls, *args, expect_ok=True):
        env = dict(os.environ, PYTHONPATH=str(REPO),
                   PYTHONDONTWRITEBYTECODE="1",
                   SPIELOS_HOME=str(REPO))
        result = cls.subprocess.run(
            [sys.executable, "-B", "-m", "company", "--db", str(cls.db), *args],
            cwd=str(REPO), env=env, capture_output=True, text=True,
            timeout=180)
        if expect_ok:
            assert result.returncode == 0, f"CLI {args} failed: {result.stderr}"
            try:
                return cls.json.loads(result.stdout)
            except cls.json.JSONDecodeError:
                return result.stdout
        return result

    @classmethod
    def tearDownClass(cls):
        cls.db.unlink(missing_ok=True)

    def test_goal_create_through_cli(self):
        listing = self.run_cli("goal", "list", "--json")
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["name"], "CLI goal")

    def test_status_snapshot_json(self):
        snapshot = self.run_cli("status", "--json")
        self.assertIn("counts", snapshot)
        self.assertEqual(snapshot["counts"]["active"], 1)

    def test_context_command_json(self):
        projection = self.run_cli("context", "--prompt", "hello", "--json")
        self.assertIn("hello", projection["context"])

    def test_runner_tick_quiesces_after_park(self):
        tick = self.run_cli("runner", "tick", "--json")
        self.assertTrue(tick["quiescent"],
                        "tick parks work for the host and then goes quiet")

    def test_notifications_list_json(self):
        # Intentional semantic change (issue #5 of goal-d62825bb0b23):
        # an ordinary DECIDE park for a goal nothing can decide is HOST
        # reasoning — the Director agent answers it with `goal decide` —
        # so its notification kind is host_work_required, never an
        # owner ask.
        self.run_cli("runner", "tick", "--json")
        rows = self.run_cli("notifications", "list", "--json")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "host_work_required")

    def test_tasks_complete_by_agent_id_documented_flow_succeeds(self):
        # D1 fixed (inverted pin): the documented host flow completes an
        # order the runtime claimed, using the agent id, first try. The
        # DECIDE boundary parks a decision_request first, so the flow goes
        # through `goal decide` before the order exists.
        goal_id = self.run_cli("goal", "list", "--json")[0]["id"]
        self.run_cli("goal", "decide", goal_id, "--kind", "request_agent",
                     "--agent", "director",
                     "--instruction", "produce the metric evidence")
        orders = self.run_cli("tasks", "--json")
        order_id, agent_id = orders[0]["id"], orders[0]["agent_id"]
        result = self.run_cli("tasks", order_id, "--complete", agent_id,
                              "--evidence", '[{"kind":"m","payload":{"m":1}}]')
        self.assertEqual(result["work_order"]["status"], "completed")

    def test_memory_summary_scopes(self):
        summary = self.run_cli("memory", "summary", "--json")
        self.assertEqual(set(summary["durable_memory"]),
                         {"owner", "workflow", "strategy"})

    def test_profile_set_and_list(self):
        self.run_cli("profile", "set", "--namespace", "ns", "--key", "k",
                     "--value", '"v"')
        listing = self.run_cli("profile", "list", "--json")
        self.assertTrue(any(item["claim_key"] == "k" for item in listing))

    def test_tasks_complete_learning_flag_writes_workflow_memory(self):
        # D2 fixed, through the real CLI: --learning persists workflow
        # memory grounded in the evidence just recorded. Uses its own goal
        # so other CLI tests' completed orders do not consume the park. The
        # DECIDE boundary is answered with `goal decide` first.
        self.run_cli("goal", "create", "--name", "Learning goal",
                     "--owner", "director", "--metric", "m",
                     "--target", "1")
        self.run_cli("runner", "tick", "--json")
        goals = [goal for goal in self.run_cli("goal", "list", "--json")
                 if goal["name"] == "Learning goal"]
        self.run_cli("goal", "decide", goals[0]["id"], "--kind", "request_agent",
                     "--agent", "director",
                     "--instruction", "produce learning evidence")
        orders = [o for o in self.run_cli("tasks", "--json")
                  if o["goal_id"] != "CLI goal" or o["status"] in ("open", "claimed")]
        orders = [o for o in orders if o["agent_id"] == "director"]
        self.assertTrue(orders, "a parked order must exist to complete")
        order_id, agent_id = orders[0]["id"], orders[0]["agent_id"]
        self.run_cli("tasks", order_id, "--complete", agent_id,
                     "--evidence", '[{"kind":"m","payload":{"m":2}}]',
                     "--learning", "CLI completions should surface learning")
        memories = self.run_cli("memory", "workflows", "--json")
        self.assertTrue(any("CLI completions" in item["claim"]
                            for item in memories),
                        "tasks --complete --learning must persist workflow "
                        "memory reachable from memory workflows")

    def test_goal_create_rejects_undeclared_department_metric_via_cli(self):
        # D3 fixed through the real CLI with fixture departments.
        env_fixture = dict(os.environ)
        env_fixture["SPIELOS_TEST_DEPARTMENTS_DIR"] = str(
            FIXTURES / "departments")
        import subprocess
        result = self.subprocess.run(
            [sys.executable, "-B", "-m", "company", "--db", str(self.db),
             "goal", "create", "--name", "Bad metric", "--owner", "seo",
             "--metric", "articles_published", "--target", "1"],
            cwd=str(REPO),
            env=dict(env_fixture, PYTHONPATH=str(REPO),
                     PYTHONDONTWRITEBYTECODE="1",
                     SPIELOS_HOME=str(REPO)),
            capture_output=True, text=True, timeout=60)
        self.assertNotEqual(result.returncode, 0,
                             "goal create must refuse undeclared metrics")
        self.assertIn("does not declare metric", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
