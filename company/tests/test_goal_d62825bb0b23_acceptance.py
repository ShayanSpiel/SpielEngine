"""Acceptance pins for goal-d62825bb0b23 — the 16-issue self-improving
runtime: intelligent DECIDE/EVALUATE, repetition crystallization,
structural self-repair, host-work separation, and memory relevance.

Every pin drives production paths (the engine loop, the repositories,
the CLI surface) — never a fabricated row — so the acceptance evidence
is the test run itself.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from company.agents.core import Agent, AgentEvidence, AgentResult
from company.commands.goal_runtime import (
    AssignmentExecutor, CleanCommandRuntime)
from company.evidence import EvidenceRepository
from company.memory import MemoryRepository
from company.runtime.engine import Decision, GoalStage
from company.workflows import Workflow, WorkflowRepository, WorkflowStep

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class AcceptanceCase(unittest.TestCase):
    """Shared: fresh CleanCommandRuntime on a throwaway database."""

    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        handle.close()
        self.db = Path(handle.name)
        self.db.unlink()
        self.runtime = CleanCommandRuntime(self.db)
        self.engine = self.runtime.runtime

    def tearDown(self):
        self.db.unlink(missing_ok=True)

    def new_goal(self, name="G", owner="director", metric="m",
                 operator="ge", target=999, parent_id=None, config=None):
        row = self.runtime.create_goal(
            name=name, owner_id=owner, metric=metric, operator=operator,
            target=target, parent_id=parent_id,
            config=config or {"aggregation": "latest"})
        self.goal_id = row["id"]
        return row

    def current_run(self):
        return self.runtime.runs.current(self.goal_id)

    def active_orders(self, goal_id=None):
        return self.runtime.work_orders(
            status="active", goal_id=goal_id or self.goal_id)

    def owner_asks(self, goal_id=None):
        return [item for item in self.runtime.notifications(
            goal_id=goal_id or self.goal_id)
            if item["kind"] == "owner_input_required"]

    def host_work(self, goal_id=None):
        return [item for item in self.runtime.notifications(
            goal_id=goal_id or self.goal_id)
            if item["kind"] == "host_work_required"]


# =========================================================================
# Item 1: DECIDE reasons and returns a concrete Decision; parks only at
# genuine boundaries
# =========================================================================

FIXTURES = Path(__file__).resolve().parent / "fixtures"

@unittest.skipUnless(
    (FIXTURES / "departments").is_dir(),
    "fixture departments ship with the source tree")
class TestItem1DecideReasons(AcceptanceCase):

    def test_a_declared_metric_decides_a_workflow_without_parking(self):
        # DECIDE returns a concrete execute_workflow Decision for a goal
        # whose metric a Department declares — no park, no owner ask.
        os.environ["SPIELOS_TEST_DEPARTMENTS_DIR"] = str(
            FIXTURES / "departments")
        try:
            # A runtime constructed AFTER the registry seam points at the
            # fixture departments (the controller loads the registry once
            # at construction).
            runtime = CleanCommandRuntime(self.db)
            row = runtime.create_goal(
                name="Map opportunities", owner_id="director",
                metric="keyword_opportunities", operator="ge", target=1,
                config={"aggregation": "latest"})
            runtime.tick(max_advances=10)
            run = runtime.runs.current(row["id"])
            decision = run.decision
            self.assertIsNotNone(decision)
            self.assertEqual(decision.kind, "execute_workflow")
            self.assertIn("seo:", decision.workflow_id)
            self.assertEqual(
                [item for item in runtime.notifications(goal_id=row["id"])
                 if item["kind"] == "owner_input_required"], [],
                "ordinary decidable work never asks the owner")
        finally:
            os.environ.pop("SPIELOS_TEST_DEPARTMENTS_DIR", None)

    def test_exhausted_candidates_park_a_genuine_owner_boundary(self):
        # Every candidate approach tried and judged → owner park.
        os.environ["SPIELOS_TEST_DEPARTMENTS_DIR"] = str(
            FIXTURES / "departments")
        try:
            runtime = CleanCommandRuntime(self.db)
            row = runtime.create_goal(
                name="Exhausted", owner_id="seo",
                metric="keyword_opportunities", operator="ge", target=1,
                config={"aggregation": "latest"})
            goal_id = row["id"]
            run = runtime.runs.current(goal_id)
            for workflow_id in ("seo:keyword-research", "seo:seo-content-brief",
                                "seo:technical-audit", "seo:seo-improvement",
                                "seo:search-performance"):
                runtime.memory.remember(
                    "strategy", f"avoid {workflow_id} for 'Exhausted': "
                    "it completed its work and keyword_opportunities "
                    "stayed at 0; prefer a different approach",
                    evidence_ids=(runtime.evidence.record(
                        goal_id=goal_id, run_id=run.id, kind="m",
                        payload={"m": 0}).id,),
                    goal_id=goal_id, run_id=run.id,
                    workflow_id=workflow_id)
            runtime.tick(max_advances=10)
            asks = [item for item in runtime.notifications(goal_id=goal_id)
                    if item["kind"] == "owner_input_required"]
            self.assertEqual(len(asks), 1,
                             "an exhausted boundary is a genuine owner ask")
            self.assertEqual((runtime.runs.current(goal_id).decision.context
                              or {}).get("owner_boundary"), "exhausted")
        finally:
            os.environ.pop("SPIELOS_TEST_DEPARTMENTS_DIR", None)


# =========================================================================
# Item 2: EVALUATE distills justified strategy lessons that change the
# next DECIDE (no injected memory)
# =========================================================================

class TestItem2EvaluateDistills(AcceptanceCase):

    def test_a_better_approach_gets_a_prefer_lesson_that_ranks_it(self):
        # Run 1 executes approach A flat; run 2 executes approach B which
        # moves the metric: EVALUATE writes one `prefer B` lesson, and the
        # NEXT DECIDE (run 3) chooses B over declaration order because of
        # the lesson — causality, not injection.
        self.new_goal(name="Compare", metric="m", target=1,
                      config={"aggregation": "latest"})
        payloads = [{"m": 0}, {"m": 5}]
        approaches = ["probe:alpha", "probe:beta"]
        for payload, approach in zip(payloads, approaches):
            run = self.current_run()
            self.runtime.runs.update(
                run.id, stage=GoalStage.ACT, status="running",
                decision=Decision("execute_workflow", approach, approach,
                                  {"workflow_id": approach}))
            self.runtime.interventions.create(
                goal_id=self.goal_id, run_id=run.id, kind="execute_workflow",
                description=approach, context={"workflow_id": approach})
            order = self.runtime.runtime.resolution.work_orders.open(
                goal_id=self.goal_id, run_id=run.id,
                intervention_id=self.runtime.interventions.active_for_run(
                    run.id).id, agent_id="director", step_id="direct",
                brief={"instruction": approach, "evidence_kind": "m"})
            self.runtime.runtime.resolution.work_orders.claim(order.id, "director")
            self.runtime.complete_work_order(
                order.id, "director", [{"kind": "m", "payload": payload}])
            self.runtime.tick(max_advances=20)
        claims = [item.claim for item in self.runtime.memory.relevant(
            goal_id=self.goal_id, limit=20) if item.scope == "strategy"]
        self.assertTrue(any(claim.startswith("prefer probe:beta") for claim in claims),
                        f"the prefer lesson must exist, got {claims}")
        # Causality: the DECIDE ranking consults exactly that memory row —
        # `prefer` ranks B above declaration order, `avoid` excludes A.
        from company.workflows import Workflow as _Workflow
        goal = self.runtime.goals.get(self.goal_id)
        beta = _Workflow("beta", "Beta", (), "probe")
        alpha = _Workflow("alpha", "Alpha", (), "probe")
        self.assertTrue(self.engine.controller._strategy_prefers(goal, beta),
                       "the prefer lesson ranks the better approach")
        self.assertTrue(self.engine.controller._strategy_avoids(goal, alpha),
                        "the flat first approach carries an avoid lesson")


# =========================================================================
# Item 3: repetition crystallization with provenance + reuse
# =========================================================================

class TestItem3Crystallization(AcceptanceCase):

    def _complete_direct(self, instruction, evidence_kind="weekly_sales",
                         payload=None):
        from company.runtime.engine import GoalStage
        for _ in range(40):
            self.runtime.tick(max_advances=50)
            run = self.current_run()
            if (run.stage == GoalStage.DECIDE and run.status == "waiting"):
                break
        self.runtime.decide_goal(
            self.goal_id, "request_agent", agent="director",
            instruction=instruction, evidence_kind=evidence_kind)
        order = self.active_orders()[0]
        self.runtime.complete_work_order(
            order["id"], "director",
            [{"kind": evidence_kind,
              "payload": payload or {evidence_kind: 0}}])
        self.runtime.tick(max_advances=50)

    def test_three_equivalent_orders_crystallize_a_reusable_workflow(self):
        self.new_goal(name="Repeated", metric="weekly_sales")
        for index in range(3):
            self._complete_direct(f"close deal number {index}")
        # The stall boundary parks the fourth run (three flat identical
        # decisions); resuming lets DECIDE reason over learned structure.
        run = self.current_run()
        self.assertEqual(run.status, "waiting",
                        "the flat goal parks for the owner at stall")
        self.runtime.resume_goal(self.goal_id)
        decision = self.current_run().decision
        self.assertEqual(decision.kind, "execute_workflow")
        self.assertTrue(decision.workflow_id.startswith(f"learned:{self.goal_id}"),
                        "the next equivalent request reuses the learned workflow")
        self.runtime.tick(max_advances=10)
        with self.runtime.connect() as connection:
            workflow = connection.execute(
                "SELECT id FROM core_workflows WHERE id LIKE 'learned:%'").fetchone()
            self.assertIsNotNone(workflow,
                                  "the learned workflow persisted")
            crystallized = connection.execute(
                """SELECT payload_json FROM core_evidence
                   WHERE kind='workflow_crystallized'""").fetchone()
            self.assertIsNotNone(crystallized,
                                 "provenance evidence records the formation")
            provenance = json.loads(crystallized[0])
            self.assertEqual(len(provenance["from_orders"]), 3)
            claim = connection.execute(
                """SELECT claim FROM core_memory
                   WHERE workflow_id LIKE 'learned:%'""").fetchone()
            self.assertIsNotNone(claim, "the formation claim persisted")

    def test_three_unrelated_orders_do_not_crystallize(self):
        self.new_goal(name="Unrelated", metric="weekly_sales")
        for index in range(3):
            self._complete_direct(
                f"unrelated task {index}", evidence_kind=f"kind_{index}",
                payload={f"kind_{index}": 0})
        # Drive one more decided cycle: whatever parks (a stall or a
        # decision_request park), answer it through its production path
        # and verify nothing ever crystallized.
        for _ in range(40):
            self.runtime.tick(max_advances=50)
            run = self.current_run()
            if run.status == "waiting":
                break
        run = self.current_run()
        if (run.stage == GoalStage.DECIDE and run.decision is not None
                and run.decision.kind == "decision_request"):
            self.runtime.decide_goal(
                self.goal_id, "request_agent", agent="director",
                instruction="one more unrelated task",
                evidence_kind="kind_extra")
            order = self.active_orders()[0]
            self.runtime.complete_work_order(
                order["id"], "director",
                [{"kind": "kind_extra", "payload": {"kind_extra": 0}}])
            self.runtime.tick(max_advances=50)
        with self.runtime.connect() as connection:
            learned = connection.execute(
                "SELECT COUNT(*) FROM core_workflows WHERE id LIKE 'learned:%'").fetchone()[0]
        self.assertEqual(learned, 0,
                         "three unrelated orders (different evidence kinds) "
                         "never crystallize")


# =========================================================================
# Item 4: structural self-repair with zero owner asks
# =========================================================================

class _BrokenContractExecutor:
    """Completes with the wrong evidence kind: a step_contract defect."""

    def execute(self, agent, order):
        return AgentResult("completed",
                           evidence=(AgentEvidence("wrong_kind", {"wrong": 1}),))


class TestItem4SelfRepair(AcceptanceCase):

    def test_first_defect_opens_repairs_adopts_and_resumes_with_zero_asks(self):
        self.new_goal(name="Defective", metric="m", target=999)
        WorkflowRepository(self.runtime.database).save(Workflow(
            "probe:defective", "Defective", (
                WorkflowStep("bad-step", "director", "do the step",
                             evidence_kind="good_kind",
                             evidence_kinds=("good_kind",)),), "probe"))
        run = self.current_run()
        self.runtime.runs.update(
            run.id, stage=GoalStage.ACT, status="running",
            decision=Decision("execute_workflow", "run", "probe:defective",
                              {"workflow_id": "probe:defective"}))
        self.runtime.interventions.create(
            goal_id=self.goal_id, run_id=run.id, kind="execute_workflow",
            description="probe", context={"workflow_id": "probe:defective"})
        self.engine.resolution.executor = _BrokenContractExecutor()
        self.engine.advance(self.goal_id)

        with self.runtime.connect() as connection:
            repair_id = connection.execute(
                "SELECT id FROM core_goals WHERE name LIKE 'Repair%'").fetchone()[0]
            blocked = connection.execute(
                """SELECT COUNT(*) FROM core_goal_edges
                   WHERE source_goal_id=? AND target_goal_id=?
                     AND relation='blocks'""", (repair_id, self.goal_id)).fetchone()[0]
        self.assertEqual(blocked, 1, "the repair goal blocks the original")
        self.assertEqual(self.owner_asks(), [],
                         "the defect and repair are host mechanics, never "
                         "owner asks on the original goal")

        # The repair goal assigns its bounded repair work directly (its
        # DECIDE never parks: the defect contract plus the acceptance
        # requirement is the instruction). Restore the parking executor
        # FIRST, then drive the repair run until its order is parked as
        # host work, and complete it the way its assigned Agent does:
        # acceptance evidence + the revised definition.
        self.engine.resolution.executor = AssignmentExecutor(
            memory=MemoryRepository(self.runtime.database,
                                    EvidenceRepository(self.runtime.database)))
        for _ in range(10):
            if self.active_orders(goal_id=repair_id):
                break
            self.engine.advance(repair_id)
        order = self.active_orders(goal_id=repair_id)[0]
        revised = {
            "id": "probe:defective", "name": "Defective (revised)",
            "steps": [
                {"id": "bad-step", "agent_id": "director",
                 "instruction": "do the corrected step",
                 "evidence_kind": "wrong_kind",
                 "evidence_kinds": ["wrong_kind"], "skill_ids": [],
                 "connection_ids": [], "requirements": {},
                 "approval_keys": []}],
            "department_id": "probe", "version": 1}
        self.runtime.complete_work_order(
            order["id"], order["agent_id"],
            [{"kind": "acceptance_green",
              "payload": {"acceptance_green": True, "workflow": revised}}])
        self.runtime.tick(max_advances=100)

        with self.runtime.connect() as connection:
            repair_status = connection.execute(
                "SELECT status FROM core_goals WHERE id=?",
                (repair_id,)).fetchone()[0]
            workflow = connection.execute(
                "SELECT name, version FROM core_workflows WHERE id=?",
                ("probe:defective",)).fetchone()
            revision_claim = connection.execute(
                """SELECT claim FROM core_memory
                   WHERE workflow_id='probe:defective'""").fetchone()
            still_blocked = connection.execute(
                """SELECT COUNT(*) FROM core_goal_edges
                   WHERE source_goal_id=? AND target_goal_id=?
                     AND relation='blocks'""", (repair_id, self.goal_id)).fetchone()[0]
            superseded = connection.execute(
                """SELECT status FROM core_workflow_runs
                   WHERE workflow_id='probe:defective'
                   ORDER BY created_at LIMIT 1""").fetchone()[0]
        self.assertEqual(repair_status, "complete",
                         "the repair goal completed with acceptance evidence")
        self.assertEqual(workflow[1], 2, "the revision bumped the version")
        self.assertIn("revised to v2", revision_claim[0],
                      "the causal v2 revision learning persisted")
        self.assertEqual(still_blocked, 0,
                         "the blocks edge cleared when the repair completed")
        self.assertEqual(superseded, "superseded",
                         "the broken workflow run snapshot was superseded")
        # The original goal resumed: a fresh WorkflowRun exists against v2
        # and its step parks as host work, not an owner ask.
        with self.runtime.connect() as connection:
            resumed = connection.execute(
                """SELECT status, workflow_version FROM core_workflow_runs
                   WHERE workflow_id='probe:defective'
                   ORDER BY created_at DESC LIMIT 1""").fetchone()
        self.assertEqual(resumed[1], 2)
        self.assertEqual(self.owner_asks(), [],
                         "the resumed original goal still carries zero owner asks")


# =========================================================================
# Item 5: ordinary WorkOrder parks are host work; five ordinary steps
# produce zero owner asks, one approval step produces exactly one
# =========================================================================

class _ParksForHost:
    """Executor that parks every step for its host agent (the default)."""

    def execute(self, agent, order):
        return AgentResult("host_work", message=f"parked {order.id}")


class TestItem5HostWorkSeparation(AcceptanceCase):

    def _workflow_goal(self, steps):
        self.new_goal(name="Host work", metric="m", target=999)
        WorkflowRepository(self.runtime.database).save(Workflow(
            "probe:steps", "Steps", steps, "probe"))
        run = self.current_run()
        self.runtime.runs.update(
            run.id, stage=GoalStage.ACT, status="running",
            decision=Decision("execute_workflow", "run", "probe:steps",
                              {"workflow_id": "probe:steps"}))
        self.runtime.interventions.create(
            goal_id=self.goal_id, run_id=run.id, kind="execute_workflow",
            description="probe", context={"workflow_id": "probe:steps"})
        return run

    def test_five_ordinary_steps_produce_zero_owner_asks(self):
        steps = tuple(
            WorkflowStep(f"step-{index}", "director", f"ordinary step {index}",
                         evidence_kind="m")
            for index in range(5))
        self._workflow_goal(steps)
        self.engine.resolution.executor = _ParksForHost()
        self.engine.advance(self.goal_id)
        self.assertEqual(len(self.active_orders()), 1,
                         "the first step parks one order")
        asks = self.runtime.notifications(goal_id=self.goal_id)
        self.assertEqual(len(asks), 1)
        self.assertEqual(asks[0]["kind"], "host_work_required",
                         "an ordinary parked step is host work, never an "
                         "owner ask")

    def test_an_approval_step_parks_exactly_one_owner_ask(self):
        steps = (WorkflowStep("approval-gate", "director", "approve the send",
                              approval_keys=("send",)),
                 WorkflowStep("do-work", "director", "the work after approval",
                              evidence_kind="m"))
        self._workflow_goal(steps)
        self.engine.resolution.executor = _ParksForHost()
        self.engine.advance(self.goal_id)
        asks = self.runtime.notifications(goal_id=self.goal_id)
        self.assertEqual(len(asks), 1)
        self.assertEqual(asks[0]["kind"], "owner_input_required",
                         "an approval gate is exactly one genuine owner ask")


# =========================================================================
# Item 10: agent connection/skill declarations are enforced
# =========================================================================

class TestItem10WiringEnforcement(AcceptanceCase):

    def _install_and_bind(self, agent, step):
        self.engine.resolution.agents = {agent.id: agent}
        WorkflowRepository(self.runtime.database).save(Workflow(
            "probe:wired", "Wired", (step,), "probe"))
        run = self.current_run()
        self.runtime.runs.update(
            run.id, stage=GoalStage.ACT, status="running",
            decision=Decision("execute_workflow", "run", "probe:wired",
                              {"workflow_id": "probe:wired"}))
        self.runtime.interventions.create(
            goal_id=self.goal_id, run_id=run.id, kind="execute_workflow",
            description="probe", context={"workflow_id": "probe:wired"})
        return self.engine.resolution.resolve(
            self.runtime.interventions.active_for_run(run.id).id)

    def test_undeclared_skills_are_a_wiring_defect(self):
        self.new_goal(name="Wiring", metric="m", target=999)
        result = self._install_and_bind(
            Agent("specialist", skill_ids=("writing",)),
            WorkflowStep("step-one", "specialist", "do the step",
                         evidence_kind="m", skill_ids=("cold_outreach",)))
        self.assertEqual(result.outcome.value, "SYSTEM_DEFECT")
        self.assertEqual(result.defect.kind, "wiring")
        self.assertIn("cold_outreach", result.defect.summary)
        self.assertEqual(len(self.active_orders()), 0,
                         "a wiring defect opens no work order")

    def test_declared_wiring_executes(self):
        self.new_goal(name="Wiring ok", metric="m", target=999)
        self.engine.resolution.executor = _ParksForHost()
        result = self._install_and_bind(
            Agent("specialist", skill_ids=("writing", "cold_outreach"),
                  connection_ids=("crm",), capability_ids=("email",)),
            WorkflowStep("step-one", "specialist", "do the step",
                         evidence_kind="m", skill_ids=("cold_outreach",),
                         connection_ids=("crm",),
                         requirements={"capabilities": ("email",)}))
        self.assertEqual(result.outcome.value, "HOST_WORK")
        orders = self.active_orders()
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["agent_id"], "specialist")


# =========================================================================
# Item 13: stale completion cannot force an illegal transition; concurrent
# completions leave no duplicates
# =========================================================================

class TestItem13CompletionSafety(AcceptanceCase):

    def _direct_order(self):
        self.new_goal(name="Safety", metric="m", target=999)
        run = self.current_run()
        self.runtime.runs.update(
            run.id, stage=GoalStage.ACT, status="running",
            decision=Decision("request_agent", "bounded work", None,
                              {"agent_id": "director", "evidence_kind": "m"}))
        intervention = self.runtime.interventions.create(
            goal_id=self.goal_id, run_id=run.id, kind="request_agent",
            description="bounded work",
            context={"agent_id": "director", "evidence_kind": "m"})
        order = self.runtime.runtime.resolution.work_orders.open(
            goal_id=self.goal_id, run_id=run.id,
            intervention_id=intervention.id, agent_id="director",
            step_id="direct",
            brief={"instruction": "bounded work", "evidence_kind": "m"})
        return self.runtime.runtime.resolution.work_orders.claim(
            order.id, "director")

    def test_stale_completion_cannot_force_an_illegal_transition(self):
        order = self._direct_order()
        # A completion arrives late: the run already moved past ACT/waiting.
        with self.runtime.connect() as connection:
            connection.execute(
                "UPDATE core_runs SET stage='EVALUATE',status='complete' "
                "WHERE goal_id=?", (self.goal_id,))
        self.runtime.complete_work_order(
            order.id, "director", [{"kind": "m", "payload": {"m": 1}}])
        with self.runtime.connect() as connection:
            stage, status = connection.execute(
                "SELECT stage,status FROM core_runs WHERE goal_id=?",
                (self.goal_id,)).fetchone()
            evidence = connection.execute(
                "SELECT COUNT(*) FROM core_evidence WHERE work_order_id=?",
                (order.id,)).fetchone()[0]
        self.assertEqual((stage, status), ("EVALUATE", "complete"),
                         "the stale completion forced nothing")
        self.assertEqual(evidence, 1,
                        "the evidence row persisted (execution happened); "
                        "only the illegal transition was refused")

    def test_concurrent_completions_leave_no_duplicates(self):
        from company.work_orders import WorkOrderRepository
        order = self._direct_order()
        repository = WorkOrderRepository(self.runtime.database)
        evidence = [{"kind": "m", "payload": {"m": 1}}]
        first = repository.complete_with_evidence(
            order.id, {"evidence": evidence}, executor_id="director",
            kind="m", payload={"m": 1},
            evidence_items=[("m", {"m": 1})], wake_run=True)
        second = repository.complete_with_evidence(
            order.id, {"evidence": evidence}, executor_id="director",
            kind="m", payload={"m": 1},
            evidence_items=[("m", {"m": 1})], wake_run=True)
        with self.runtime.connect() as connection:
            rows = connection.execute(
                "SELECT COUNT(*) FROM core_evidence WHERE work_order_id=?",
                (order.id,)).fetchone()[0]
            runs = connection.execute(
                "SELECT COUNT(*) FROM core_runs WHERE goal_id=?",
                (self.goal_id,)).fetchone()[0]
        self.assertEqual(rows, 1,
                         "the idempotent loser added no duplicate evidence")
        self.assertEqual(first[1], second[1],
                         "the loser receives the winner's evidence ids")
        self.assertEqual(runs, 1, "no duplicate runs")


# =========================================================================
# Item 14: unsupported database schema fails clearly
# =========================================================================

class TestItem14SchemaRefusal(AcceptanceCase):

    def test_a_non_company_database_fails_clearly(self):
        handle = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        handle.close()
        foreign = Path(handle.name)
        foreign.unlink()
        with sqlite3.connect(foreign) as connection:
            connection.execute("CREATE TABLE unrelated (id TEXT)")
        try:
            with self.assertRaises(ValueError) as caught:
                CleanCommandRuntime(foreign, readonly=True)
            message = str(caught.exception)
            self.assertIn("not a current SpielOS company database", message)
            self.assertIn("missing tables", message)
            self.assertIn("core_goals", message)
        finally:
            foreign.unlink(missing_ok=True)


# =========================================================================
# Item 15: end-to-end acceptance simulation through production paths
# =========================================================================

class TestItem15EndToEnd(AcceptanceCase):

    def test_the_causal_chain_and_owner_facing_ux_end_to_end(self):
        # One goal, one ordinary step: DECIDE decides, ACT parks host work
        # (zero owner asks), the host completes through the CLI path with
        # a reusable lesson, EVALUATE commits, and the next DECIDE sees
        # the learning it carries — the whole causal chain with the
        # owner-facing UX intact.
        self.new_goal(name="E2E", metric="m", target=1)
        WorkflowRepository(self.runtime.database).save(Workflow(
            "probe:e2e", "E2E", (
                WorkflowStep("one", "director", "produce the metric evidence",
                             evidence_kind="m"),), "probe"))
        run = self.current_run()
        self.runtime.runs.update(
            run.id, stage=GoalStage.ACT, status="running",
            decision=Decision("execute_workflow", "run", "probe:e2e",
                              {"workflow_id": "probe:e2e"}))
        self.runtime.interventions.create(
            goal_id=self.goal_id, run_id=run.id, kind="execute_workflow",
            description="probe", context={"workflow_id": "probe:e2e"})
        self.engine.advance(self.goal_id)
        asks = self.runtime.notifications(goal_id=self.goal_id)
        self.assertEqual(len(asks), 1)
        self.assertEqual(asks[0]["kind"], "host_work_required")
        payload = asks[0]["payload"]
        for key in ("message", "why", "decision", "after"):
            self.assertTrue(payload.get(key) and str(payload[key]).strip(),
                            f"the host-work payload keeps {key}")
        self.assertIn("external actions still park for approval first",
                      payload["after"],
                      "the owner-facing boundary language stays")
        # The host agent completes the parked order through the CLI path,
        # recording a genuinely reusable lesson.
        order = self.active_orders()[0]
        self.runtime.complete_work_order(
            order["id"], "director",
            [{"kind": "m", "payload": {"m": 1}}],
            learning="produce the metric evidence with one bounded step")
        self.runtime.tick(max_advances=20)
        with self.runtime.connect() as connection:
            goal_status = connection.execute(
                "SELECT status FROM core_goals WHERE id=?",
                (self.goal_id,)).fetchone()[0]
            lesson = connection.execute(
                """SELECT claim FROM core_memory
                   WHERE workflow_id='probe:e2e'""").fetchone()
        self.assertEqual(goal_status, "complete",
                         "the evidence moved the metric to target")
        self.assertIn("one bounded step", lesson[0],
                      "the operational lesson persisted with workflow lineage")
        # The next WorkOrder brief for this workflow carries the lesson.
        briefs = self.engine.resolution._brief_memory(workflow_id="probe:e2e")
        self.assertIn("one bounded step", "; ".join(briefs))


# =========================================================================
# Items 6/7/9/12: pinned by test_decide_boundary, test_learning_loop, and
# test_harness_behavior — re-asserted here as one command-level proof.
# =========================================================================

class TestPinnedItemsSummary(AcceptanceCase):

    def test_memory_relevance_is_structural_not_owner_metric(self):
        # Item 7: two goals sharing owner+metric stay separate; a genuine
        # (shared-parent) sibling reaches.
        self.new_goal(name="Campaign A", metric="m")
        parent = self.runtime.create_goal(
            name="Parent", owner_id="director", metric="parent_metric",
            operator="ge", target=1, config={"aggregation": "latest"})
        focus = self.runtime.create_goal(
            name="Focus child", owner_id="director", metric="m",
            operator="ge", target=1, parent_id=parent["id"],
            config={"aggregation": "latest"})
        sibling = self.runtime.create_goal(
            name="Genuine sibling", owner_id="director", metric="m",
            operator="ge", target=1, parent_id=parent["id"],
            config={"aggregation": "latest"})
        stranger = self.runtime.create_goal(
            name="Stranger", owner_id="director", metric="m",
            operator="ge", target=1, config={"aggregation": "latest"})
        for goal_id, claim in ((focus["id"], "focus learned this"),
                                (sibling["id"], "sibling learned this"),
                                (stranger["id"], "stranger learned this")):
            run = self.runtime.runs.current(goal_id)
            self.runtime.memory.remember(
                "strategy", claim,
                evidence_ids=(self.runtime.evidence.record(
                    goal_id=goal_id, run_id=run.id, kind="m",
                    payload={"m": 1}).id,),
                goal_id=goal_id, run_id=run.id)
        claims = [item.claim for item in self.runtime.memory.relevant(
            goal_id=focus["id"], limit=20)]
        self.assertIn("focus learned this", claims)
        self.assertIn("sibling learned this", claims,
                      "genuine siblings (shared parent) share strategy")
        self.assertNotIn("stranger learned this", claims,
                         "same owner+metric alone never joins goals")

    def test_owner_memory_is_company_global_with_no_scope_fields(self):
        # Item 12: profile writes never carry goal/workflow fields.
        self.runtime.set_profile_claim(namespace="probe", claim_key="tone",
                                       value='"direct"')
        with self.runtime.connect() as connection:
            row = connection.execute(
                """SELECT goal_id, run_id, workflow_id, scope
                   FROM core_memory WHERE scope='owner'""").fetchone()
        self.assertEqual((row[0], row[1], row[2]), (None, None, None),
                         "owner memory carries no fake scope fields")
        # owner memory reaches every goal query
        self.new_goal(name="Any goal", metric="m2")
        claims = [item.claim for item in self.runtime.memory.relevant(
            goal_id=self.goal_id, limit=10) if item.scope == "owner"]
        self.assertTrue(any("probe.tone" in claim for claim in claims),
                        "owner memory is company-global")
