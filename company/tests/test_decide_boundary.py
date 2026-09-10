"""The DECIDE intelligence boundary, owner-facing asks, goal-tree
projection, memory teaching, and the stall boundary.

Pins for the 10.3.0 system-improvement goal (DECIDE is the reasoning seam):

- A goal no Department can decide parks a structured ``decision_request``
  (run DECIDE/waiting, one owner ask) and never creates a content-free
  WorkOrder or Intervention.
- ``goal decide`` answers it with a validated candidate workflow or with
  bounded direct work whose instruction is mandatory.
- ``goal resume`` opens stall/review parks and refuses DECIDE parks.
- Stalled goals park after N flat evaluated runs that all made the same
  decision (decision identity, audit F5): a changed decision chains, and
  evidence alone no longer cancels the stall.
- A fixable executor that exhausts its local budget over and over parks
  after the escalation threshold consecutive exhaustions instead of
  churning the identical fix loop.
- Progressing goals chain automatically with no per-run gate.
- Every owner ask carries what/why/decision/after in plain owner language.
- Owner voice (goal-director-voice): owner-facing texts and the context
  projection speak owner language — goals by name with human progress,
  evidence as outcome sentences, loop position in plain words, options as
  named choices. No raw goal/evidence ids, stage or decision enums,
  metric keys, operator/target pairs, JSON dumps, or CLI answer syntax in
  owner-facing text; the ids/keys/enums ride the payload machine fields
  and the projection's one Machine reference line for the Director alone.
- ``assemble_context`` renders the whole active goal tree and recent
  memory across all three scopes.
- Updating a home refreshes the vendored spine only: parked Runs,
  Interventions, WorkOrders, and notifications are preserved untouched.

Run:  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.agents python3 -B -m unittest \\
          company.tests.test_decide_boundary -v
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"

os.environ.setdefault("SPIELOS_HOME", str(REPO))

with_departments = __import__("unittest").skipUnless(
    FIXTURES.is_dir(), "department fixtures not present")


def temp_db() -> Path:
    handle = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    handle.close()
    path = Path(handle.name)
    path.unlink(missing_ok=True)
    return path


from company.commands.goal_runtime import CleanCommandRuntime  # noqa: E402
from company.runtime.engine import (  # noqa: E402
    Decision, Evaluation, GoalRuntime, GoalStage)
from company.runtime.util import compare  # noqa: E402


class _FlatController:
    """Controller double: the runtime decides nothing, evaluation is flat.

    Drives the loop through DECIDE/ACT/EVALUATE without a Department or
    host work, so the stall boundary can be exercised deterministically.
    """

    def __init__(self, database):
        self.database = database

    def observe(self, context):
        return {context.goal.metric: 0}

    def decide(self, context, observation):
        if compare(observation.get(context.goal.metric, 0),
                   context.goal.operator, context.goal.target):
            return Decision("evaluate", "target met",
                            context={"result_ready": True})
        return Decision("evaluate", "evaluate without new work",
                        context={"result_ready": True})

    def evaluate(self, context, decision, evidence):
        return Evaluation(False, {context.goal.metric: 0},
                          "clean-core evidence evaluated")


class _MovingController(_FlatController):
    def __init__(self, database):
        super().__init__(database)
        self.counter = 0

    def evaluate(self, context, decision, evidence):
        self.counter += 1
        return Evaluation(False, {context.goal.metric: self.counter},
                          "clean-core evidence evaluated")


class _DirectWorkController:
    """Controller double: always decides bounded direct work, so ACT
    reaches the resolution cycle without a Department or fixture."""

    def __init__(self, database):
        self.database = database

    def observe(self, context):
        return {context.goal.metric: 0}

    def decide(self, context, observation):
        return Decision("request_agent", "do the work", None,
                        {"agent_id": "director",
                         "evidence_kind": context.goal.metric})

    def evaluate(self, context, decision, evidence):
        return Evaluation(False, {context.goal.metric: 0},
                          "clean-core evidence evaluated")


class _FixableForever:
    """Executor double: always marks the work fixable, so the resolution
    cycle exhausts its local budget over and over (F5)."""

    def execute(self, agent, order):
        from company.agents.core import AgentResult
        return AgentResult("fixable", message="always fixable")


class _CompletingDirect:
    """Executor double: completes bounded direct work with evidence that
    leaves the metric flat, so decided runs reach EVALUATE (F5)."""

    def execute(self, agent, order):
        from company.agents.core import AgentEvidence, AgentResult
        kind = order.brief.get("evidence_kind") or "intervention_result"
        return AgentResult("completed", evidence=(
            AgentEvidence(kind, {kind: 0}),))


class _Ask:
    """Assertion helpers for the one owner-ask shape."""

    @staticmethod
    def assert_owner_ask(test, payload):
        for key in ("message", "why", "decision", "after",
                    "required_user_action"):
            value = payload.get(key)
            test.assertTrue(value and str(value).strip(),
                            f"owner ask is missing {key}")
            test.assertNotIn("must choose bounded work", str(value))
            test.assertNotIn("must produce", str(value))


class DecideBoundaryCase(unittest.TestCase):
    def setUp(self):
        self.db = temp_db()
        self.runtime = CleanCommandRuntime(self.db)
        self.engine = self.runtime.runtime

    def tearDown(self):
        self.db.unlink(missing_ok=True)

    def new_goal(self, name="Weekly sales", owner="director", metric="weekly_sales",
                 operator="ge", target=1, config=None):
        row = self.runtime.create_goal(
            name=name, owner_id=owner, metric=metric, operator=operator,
            target=target, config=config or {"aggregation": "latest"})
        self.goal_id = row["id"]
        return row

    def current_run(self):
        return self.runtime.runs.current(self.goal_id)

    def active_orders(self):
        return self.runtime.work_orders(status="active", goal_id=self.goal_id)

    def pending_asks(self):
        return self.runtime.notifications(goal_id=self.goal_id)

    def decision_request(self):
        return (self.current_run().decision.context or {}).get("decision_request") or {}

    def owner_asks(self):
        """Pending notifications that interrupt the OWNER (issue #5):
        approval gates, genuine owner boundaries, stalls, reviews, and
        runtime failures — never host-dispatched work."""
        return [item for item in self.runtime.notifications(goal_id=self.goal_id)
                if item["kind"] == "owner_input_required"]

    def host_work(self):
        return [item for item in self.runtime.notifications(goal_id=self.goal_id)
                if item["kind"] == "host_work_required"]


# =========================================================================
# 1. The park: structured decision_request, zero content-free work
# =========================================================================

class TestDecisionRequestPark(DecideBoundaryCase):

    def test_departmentless_goal_parks_a_decision_request(self):
        self.new_goal()
        self.runtime.tick(max_advances=10)
        run = self.current_run()
        self.assertEqual((run.stage, run.status), (GoalStage.DECIDE, "waiting"),
                         "a goal no Department can decide parks at DECIDE")
        self.assertEqual(run.decision.kind, "decision_request")
        request = self.decision_request()
        self.assertEqual(request["goal"]["id"], self.goal_id)
        self.assertEqual(request["goal"]["metric"], "weekly_sales")
        self.assertEqual(request["metric"], "weekly_sales")
        self.assertEqual(request["observation_value"], 0)
        self.assertEqual(request["valid_answers"], ["execute_workflow", "request_agent"])
        # Owner voice (goal-director-voice): the message names the goal and
        # its human progress with named options — no metric key, operator,
        # or target pair ever enters owner-facing text.
        self.assertIn("Weekly sales", request["message"])
        self.assertIn("0 of 1 customers per week", request["message"])
        self.assertIn("Run one of the candidate workflows",
                      request["message"])
        self.assertNotIn("weekly_sales", request["message"])
        self.assertNotIn("ge ", request["message"],
                         "no operator/target pair in owner-facing text")
        self.assertEqual(request["progress"], "0 of 1 customers per week")
        for key in ("answer_syntax", "candidates", "evidence", "memory",
                    "children", "blockers", "recent_runs"):
            self.assertIn(key, request)
        self.assertEqual(request["goal"]["owner_id"], "director")
        self.assertEqual(request["goal"]["aggregation"], "latest")

    def test_no_work_order_or_intervention_is_created_for_the_park(self):
        self.new_goal()
        self.runtime.tick(max_advances=10)
        self.assertEqual(self.active_orders(), [],
                         "a decision_request must never park a content-free "
                         "WorkOrder")
        with self.runtime.connect() as connection:
            interventions = connection.execute(
                "SELECT COUNT(*) FROM core_interventions WHERE goal_id=?",
                (self.goal_id,)).fetchone()[0]
            orders = connection.execute(
                "SELECT COUNT(*) FROM core_work_orders WHERE goal_id=?",
                (self.goal_id,)).fetchone()[0]
        self.assertEqual((interventions, orders), (0, 0))

    def test_the_park_carries_one_structured_ask(self):
        # Issue #1/#5: an UNDECIDED DECIDE park is HOST reasoning — the
        # Director agent answers it with `goal decide`; the owner is not
        # the default GoalController and is never the addressee.
        self.new_goal()
        self.runtime.tick(max_advances=10)
        asks = self.pending_asks()
        self.assertEqual(len(asks), 1, "exactly one ask per park")
        self.assertEqual(asks[0]["kind"], "host_work_required")
        payload = asks[0]["payload"]
        _Ask.assert_owner_ask(self, payload)
        self.assertEqual(payload["message"], self.current_run().decision.description)
        # Owner voice: the four owner-facing fields carry no CLI answer
        # syntax and no raw goal id — the Director records the answer
        # itself, and the exact commands stay in the payload.
        self.assertNotIn("company goal decide", payload["after"])
        self.assertNotIn("--kind", payload["after"])
        self.assertNotIn(self.goal_id, payload["after"])
        self.assertIn("external actions still park for approval first",
                      payload["after"])
        self.assertIn("company goal decide",
                      payload["answer_syntax"]["execute_workflow"],
                      "the CLI answer syntax stays in the payload for the "
                      "Director alone")
        self.assertEqual(payload["goal"]["name"], "Weekly sales")
        self.assertEqual(payload["machine"]["goal_id"], self.goal_id)
        self.assertEqual(payload["machine"]["metric"], "weekly_sales")

    def test_exhausted_candidates_park_a_genuine_owner_ask(self):
        # Issue #1: when every candidate approach on a department goal
        # has an active evidence-backed strategy lesson against it,
        # changing the strategy is a material owner choice — that park
        # alone is an owner ask, never the default.
        os.environ["SPIELOS_TEST_DEPARTMENTS_DIR"] = str(FIXTURES / "departments")
        try:
            # Fresh runtime with the fixture departments loaded.
            local = CleanCommandRuntime(self.db)
            row = local.create_goal(
                name="Exhausted", owner_id="seo",
                metric="keyword_opportunities", operator="ge", target=1,
                config={"aggregation": "count"})
            goal_id = row["id"]
            run = local.runs.current(goal_id)
            for workflow_id in ("seo:keyword-research", "seo:seo-content-brief",
                                "seo:technical-audit", "seo:seo-improvement",
                                "seo:search-performance"):
                local.memory.remember(
                    "strategy", f"avoid {workflow_id} for 'Exhausted': "
                    "it completed its work and keyword_opportunities "
                    "stayed at 0; prefer a different approach",
                    evidence_ids=(local.evidence.record(
                        goal_id=goal_id, run_id=run.id, kind="m",
                        payload={"m": 0}).id,),
                    goal_id=goal_id, run_id=run.id, workflow_id=workflow_id)
            local.tick(max_advances=10)
            asks = local.notifications(goal_id=goal_id)
            self.assertEqual(len(asks), 1)
            self.assertEqual(asks[0]["kind"], "owner_input_required",
                             "the exhausted boundary is a genuine owner ask")
            decision = local.runs.current(goal_id).decision
            self.assertEqual((decision.context or {}).get("owner_boundary"),
                             "exhausted")
        finally:
            os.environ.pop("SPIELOS_TEST_DEPARTMENTS_DIR", None)

# =========================================================================
# 2. Candidates: Departments that declare the metric
# =========================================================================

@with_departments
class TestDecideWithoutAsking(DecideBoundaryCase):
    """Issue #1: DECIDE decides; the owner is never the default
    GoalController. A goal whose metric a Department declares gets that
    workflow executed with no park and no ask."""

    def setUp(self):
        os.environ["SPIELOS_TEST_DEPARTMENTS_DIR"] = str(FIXTURES / "departments")
        super().setUp()

    def tearDown(self):
        os.environ.pop("SPIELOS_TEST_DEPARTMENTS_DIR", None)
        super().tearDown()

    def test_declared_metric_decides_a_workflow_with_zero_asks(self):
        # "Get 2 customers"-shaped acceptance: the runtime chooses a
        # reasonable bounded next intervention without asking.
        self.new_goal(name="Map opportunities", owner="director",
                      metric="keyword_opportunities", target=1)
        self.runtime.tick(max_advances=10)
        run = self.current_run()
        self.assertNotEqual((run.stage, run.status), (GoalStage.DECIDE, "waiting"),
                            "a decidable goal never parks at DECIDE")
        self.assertEqual(self.owner_asks(), [],
                         "the owner receives zero asks for ordinary work")
        # With the default park-for-host executor the first step parks as
        # HOST work — the assigned Agent executes it, not the owner.
        self.assertTrue(self.host_work(),
                        "the parked step is host work")
        self.assertIn(run.decision.kind, ("execute_workflow", "evaluate"))

    def test_ranking_prefers_a_strategy_preferred_workflow(self):
        # Issue #2 causality: an evidence-backed `prefer` lesson changes
        # the choice away from declaration order.
        self.new_goal(name="Ranked", owner="director",
                      metric="keyword_opportunities", target=1)
        run = self.current_run()
        self.runtime.memory.remember(
            "strategy",
            "prefer seo:seo-content-brief for 'Ranked': it moved "
            "keyword_opportunities to 5 versus seo:keyword-research's 0; "
            "prefer this approach for this goal",
            evidence_ids=(self.runtime.evidence.record(
                goal_id=self.goal_id, run_id=run.id, kind="m",
                payload={"m": 0}).id,),
            goal_id=self.goal_id, run_id=run.id,
            workflow_id="seo:seo-content-brief")
        self.runtime.tick(max_advances=10)
        self.assertEqual(self.current_run().decision.workflow_id,
                         "seo:seo-content-brief",
                         "the preferred approach wins over declaration order")

    def test_undecided_goal_parks_host_work_with_candidates(self):
        # A goal nothing can decide parks HOST work carrying the
        # structured candidates the answering host reasons over.
        self.new_goal(metric="nothing_declares_this")
        self.runtime.tick(max_advances=10)
        self.assertEqual(len(self.host_work()), 1)
        self.assertEqual(self.owner_asks(), [],
                         "an undecided park is host reasoning, not an "
                         "owner ask")
        request = self.decision_request()
        self.assertEqual(request["valid_answers"],
                         ["execute_workflow", "request_agent"])
        self.assertEqual(request["candidates"]["departments"], [],
                         "no Department declares an unknown metric")
        syntax = request["answer_syntax"]
        self.assertIn("--kind execute_workflow --workflow",
                      syntax["execute_workflow"])
        self.assertIn("--kind request_agent --agent", syntax["request_agent"])
        self.assertIn("--instruction", syntax["request_agent"])

    def test_installed_agents_are_candidates(self):
        self.new_goal(metric="nothing_declares_this")
        self.runtime.tick(max_advances=10)
        agents = self.decision_request()["candidates"]["agents"]
        self.assertIsInstance(agents, list)
        self.assertNotIn("director", agents,
                         "the goal owner is answered separately, not as an "
                         "installed Agent candidate")


# =========================================================================
# 3. goal decide execute_workflow: validated candidate adoption
# =========================================================================

@with_departments
class TestDecideGoalExecuteWorkflow(DecideBoundaryCase):

    def setUp(self):
        os.environ["SPIELOS_TEST_DEPARTMENTS_DIR"] = str(FIXTURES / "departments")
        super().setUp()

    def tearDown(self):
        os.environ.pop("SPIELOS_TEST_DEPARTMENTS_DIR", None)
        super().tearDown()

    def test_execute_workflow_refusal_lists_candidate_workflows(self):
        # The candidate filter stays the validation authority for
        # execute_workflow answers.
        self.new_goal(metric="nothing_decides_this")
        self.runtime.tick(max_advances=10)
        with self.assertRaises(ValueError) as caught:
            self.runtime.decide_goal(self.goal_id, "execute_workflow",
                                     workflow="seo:not-a-workflow")
        self.assertIn("candidate", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            self.runtime.decide_goal(self.goal_id, "execute_workflow",
                                     workflow="seo:keyword-research")
        self.assertIn("not one of the candidate", str(caught.exception))

    def test_request_agent_answer_binds_and_runs_bounded_direct_work(self):
        # The host answers an undecided park with bounded direct work;
        # the run resumes, the order parks for its assigned agent, and
        # the causal chain records the answered decision.
        self.new_goal(metric="nothing_decides_this")
        self.runtime.tick(max_advances=10)
        self.runtime.decide_goal(self.goal_id, "request_agent",
                                 agent="director",
                                 instruction="produce the metric evidence")
        run = self.current_run()
        self.assertEqual(run.decision.kind, "request_agent")
        self.assertEqual(run.decision.context["agent_id"], "director")
        orders = self.runtime.work_orders(goal_id=self.goal_id)
        self.assertEqual([item["step_id"] for item in orders], ["direct"],
                         "only the answered bounded work parks")
        self.assertEqual(len(self.host_work()), 1,
                         "the parked order is host work, not an owner ask")
        self.assertEqual(self.owner_asks(), [],
                         "ordinary bounded work never interrupts the owner")

    def test_decide_requires_the_parked_decision_request_state(self):
        # A goal nothing can decide parks; a decided goal is refused.
        self.new_goal(metric="nothing_decides_this")
        with self.assertRaises(ValueError) as caught:
            self.runtime.decide_goal(self.goal_id, "execute_workflow",
                                     workflow="seo:keyword-research")
        self.assertIn("decision_request", str(caught.exception))
        self.assertIn("OBSERVE/ready", str(caught.exception))
        self.new_goal(name="Answered", metric="nothing_decides_this_either")
        self.runtime.tick(max_advances=10)
        self.runtime.decide_goal(self.goal_id, "request_agent",
                                 agent="director",
                                 instruction="produce the metric evidence")
        with self.assertRaises(ValueError) as caught:
            self.runtime.decide_goal(self.goal_id, "execute_workflow",
                                     workflow="seo:keyword-research")
        self.assertIn("run 1 of", str(caught.exception))


# =========================================================================
# 4. goal decide request_agent: bounded direct work
# =========================================================================

class TestDecideGoalRequestAgent(DecideBoundaryCase):

    def test_request_agent_refuses_an_empty_instruction(self):
        self.new_goal()
        self.runtime.tick(max_advances=10)
        with self.assertRaises(ValueError) as caught:
            self.runtime.decide_goal(self.goal_id, "request_agent",
                                     agent="director", instruction="   ")
        self.assertIn("requires --instruction", str(caught.exception))
        self.assertEqual(self.active_orders(), [],
                         "a refused decide must park nothing")

    def test_request_agent_requires_an_allowed_agent(self):
        self.new_goal()
        self.runtime.tick(max_advances=10)
        with self.assertRaises(ValueError) as caught:
            self.runtime.decide_goal(self.goal_id, "request_agent",
                                     agent="someone-else",
                                     instruction="do the thing")
        self.assertIn("neither the goal owner", str(caught.exception))

    def test_request_agent_parks_the_exact_instruction_and_evidence_kind(self):
        self.new_goal()
        self.runtime.tick(max_advances=10)
        self.runtime.decide_goal(
            self.goal_id, "request_agent", agent="director",
            instruction="Close one enterprise deal this week",
            evidence_kind="deal_receipt")
        run = self.current_run()
        self.assertEqual((run.decision.kind, run.decision.description),
                         ("request_agent", "Close one enterprise deal this week"))
        self.assertEqual(run.decision.context["evidence_kind"], "deal_receipt")
        orders = self.active_orders()
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["brief"]["instruction"],
                         "Close one enterprise deal this week")
        self.assertEqual(orders[0]["brief"]["evidence_kind"], "deal_receipt")
        self.assertEqual(orders[0]["agent_id"], "director")
        # L1 (intentional extension): the direct order's brief carries
        # the goal-relevant memory key — here an empty bounded list, the
        # stable no-claims shape.
        self.assertEqual(orders[0]["brief"]["memory"], [])

    def test_request_agent_evidence_kind_defaults_to_the_goal_metric(self):
        self.new_goal()
        self.runtime.tick(max_advances=10)
        self.runtime.decide_goal(self.goal_id, "request_agent", agent="director",
                                 instruction="Close one deal")
        self.assertEqual(self.active_orders()[0]["brief"]["evidence_kind"],
                         "weekly_sales")

    def test_completing_the_direct_order_records_evidence_and_wakes_the_run(self):
        self.new_goal()
        self.runtime.tick(max_advances=10)
        self.runtime.decide_goal(self.goal_id, "request_agent", agent="director",
                                 instruction="Close one deal")
        order = self.active_orders()[0]
        self.runtime.complete_work_order(
            order["id"], "director",
            [{"kind": "weekly_sales", "payload": {"weekly_sales": 1}}],
            learning="Enterprise deals close faster with a named champion")
        run = self.current_run()
        self.assertEqual((run.stage, run.status),
                         (GoalStage.EVALUATE, "running"),
                         "completing the answered order wakes the run")
        evidence = self.runtime.evidence.for_goal(self.goal_id)
        self.assertTrue(any(item.payload == {"weekly_sales": 1}
                            for item in evidence))
        learned = [item for item in self.runtime.memories(limit=20)
                  if item["scope"] == "workflow"]
        self.assertTrue(any("named champion" in item["claim"]
                            for item in learned))
        self.assertEqual(self.pending_asks(), [],
                         "answering the parked ask clears its attention")

    def test_the_work_order_ask_names_the_documented_completion_flow(self):
        # Issue #5/#9: the parked order is HOST work in owner-facing
        # execution language; --learning is conditional guidance, never
        # a default instruction.
        self.new_goal()
        self.runtime.tick(max_advances=10)
        self.runtime.decide_goal(self.goal_id, "request_agent", agent="director",
                                 instruction="Close one deal")
        asks = self.pending_asks()
        self.assertEqual(len(asks), 1)
        self.assertEqual(asks[0]["kind"], "host_work_required")
        message = asks[0]["payload"]["message"]
        self.assertIn("Working on: Close one deal", message,
                     "the message is owner-facing execution language")
        self.assertIn("--complete", message)
        self.assertIn("only when", message,
                      "learning guidance is conditional, not a prompt to "
                      "always write memory")
        self.assertIn("external actions still park for approval first",
                      message)
        payload = asks[0]["payload"]
        for key in ("message", "why", "decision", "after"):
            self.assertTrue(payload.get(key) and str(payload[key]).strip(),
                            f"the host-work payload is missing {key}")
        self.assertIsNone(payload["required_user_action"],
                          "host work never claims an owner action")

    def test_department_without_workflows_parks_a_decision_request(self):
        # A Department that declares the metric but ships no workflows is
        # exactly the "must produce <metric>" case: the owner decides, the
        # runtime never invents the work.
        class _Workflowless:
            department_id = "workflowless"
            id = "workflowless"
            version = "1.0.0"
            description = "declares metrics, ships no workflows"
            workflows = ()
            evidence_metrics = {"weekly_sales": ("weekly_sales",)}
            goal_schema = {"metrics": ["weekly_sales"]}

        from company.runtime.registry import departments as load
        original = self.engine.controller.departments
        self.engine.controller.departments = {**original, "workflowless": _Workflowless()}
        try:
            self.new_goal(owner="workflowless", metric="weekly_sales")
            self.engine.advance(self.goal_id)
            self.engine.advance(self.goal_id)
            run = self.current_run()
            self.assertEqual((run.stage, run.status),
                             (GoalStage.DECIDE, "waiting"))
            self.assertEqual(run.decision.kind, "decision_request")
            self.assertNotIn("must produce", run.decision.description)
            request = self.decision_request()
            self.assertEqual([item["id"] for item in
                              request["candidates"]["departments"]],
                             ["workflowless"],
                             "the owner sees that no workflow exists")
            self.assertEqual(self.active_orders(), [])
        finally:
            self.engine.controller.departments = original


    def test_answered_decision_executes_only_after_the_decision_persists(self):
        # The run reaches ACT with a persisted request_agent Decision; the
        # parked order is the Decision's instruction, not invented work.
        self.new_goal()
        self.runtime.tick(max_advances=10)
        self.runtime.decide_goal(self.goal_id, "request_agent", agent="director",
                                 instruction="Close one deal")
        with self.runtime.connect() as connection:
            row = connection.execute(
                """SELECT decision_json FROM core_runs WHERE id=?""",
                (self.current_run().id,)).fetchone()
        decision = json.loads(row[0])
        self.assertEqual(decision["kind"], "request_agent")
        self.assertEqual(decision["context"]["agent_id"], "director")
        self.assertEqual(len(self.active_orders()), 1)


# =========================================================================
# 4b. Executor identity enforcement (audit F4): uninstalled agents are
#     refused upfront — escalate, never open a work order for them
# =========================================================================

class TestExecutorIdentityEnforcement(DecideBoundaryCase):
    """F4: a Run never executes work for an agent nobody declared.

    Workflow steps may name their owning Department's declared agents, an
    installed Agent, or the goal owner. A step (or direct assignment)
    naming anything else escalates immediately with a message that names
    the agent — before any WorkflowRun or WorkOrder is created.
    """

    def setUp(self):
        super().setUp()
        self.new_goal()

    @staticmethod
    def _workflow_of_steps(steps):
        from company.workflows import Workflow
        return Workflow("f4-workflow", "F4 executor identity probe", steps)

    def _bind_and_resolve(self, workflow):
        """Bind a Decision to ``workflow`` and resolve its ACT stage."""
        from company.workflows import WorkflowRepository
        WorkflowRepository(self.runtime.database).save(workflow)
        run = self.current_run()
        self.runtime.runs.update(
            run.id, stage=GoalStage.ACT, status="running",
            decision=Decision("execute_workflow", "F4 probe", "f4-workflow",
                              {"workflow_id": "f4-workflow"}))
        self.runtime.runtime.interventions.create(
            goal_id=self.goal_id, run_id=run.id, kind="execute_workflow",
            description="F4 probe", context={"workflow_id": "f4-workflow"})
        return self.runtime.runtime.resolution.resolve(
            self.runtime.runtime.interventions.active_for_run(run.id).id)

    def _order_count(self):
        with self.runtime.connect() as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM core_work_orders WHERE goal_id=?",
                (self.goal_id,)).fetchone()[0]

    def _workflow_run_count(self):
        with self.runtime.connect() as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM core_workflow_runs WHERE goal_id=?",
                (self.goal_id,)).fetchone()[0]

    def test_uninstalled_step_agent_escalates_with_no_work_order(self):
        # F4 pin (d): a step whose agent is neither installed, nor the
        # goal owner, nor declared by the owning Department escalates
        # before any WorkOrder exists, naming the agent, step, and
        # workflow.
        from company.workflows import WorkflowStep
        result = self._bind_and_resolve(self._workflow_of_steps((
            WorkflowStep("step-one", "ghost-agent", "no one declared me",
                         evidence_kind="weekly_sales"),)))
        self.assertEqual(result.outcome.value, "ESCALATE_TO_GOAL")
        for fragment in ("ghost-agent", "step-one", "f4-workflow"):
            self.assertIn(fragment, result.message,
                          "the defect message must name the uninstalled "
                          "agent, its step, and its workflow")
        self.assertIn("neither an installed Agent", result.message)
        self.assertEqual(self._order_count(), 0,
                         "no WorkOrder may be created for an uninstalled "
                         "step agent")
        self.assertEqual(self._workflow_run_count(), 0,
                         "the workflow run must not start for an "
                         "uninstalled step agent")

    def test_declared_department_agent_step_executes(self):
        # The declared agent flow works end to end: the owning Department
        # declared the agent, so the step runs and parks its order for
        # exactly that agent (claimed by the runtime pre-claim).
        from company.agents.core import Agent
        from company.workflows import Workflow, WorkflowStep
        self.engine.resolution.department_agents = {"f4-owner": ("f4-agent",)}
        self.engine.resolution.agents = {"f4-agent": Agent("f4-agent")}
        workflow = self._workflow_of_steps((
            WorkflowStep("step-one", "f4-agent", "declared agent step",
                         evidence_kind="weekly_sales"),))
        workflow = Workflow(workflow.id, workflow.name, workflow.steps,
                            department_id="f4-owner")
        result = self._bind_and_resolve(workflow)
        orders = self.active_orders()
        self.assertEqual(len(orders), 1,
                         "the declared agent's step parks exactly one order")
        self.assertEqual(orders[0]["agent_id"], "f4-agent")
        self.assertEqual(orders[0]["claimed_by"], "f4-agent",
                         "the runtime pre-claims with the declared agent id")
        self.assertIn(result.outcome.value, {"HOST_WORK", "CONTINUE_LOCAL",
                                             "RETURN_TO_GOAL"},
                      "the declared agent's flow must execute, not escalate")

    def test_installed_agent_step_executes_end_to_end(self):
        # The declared agent flow works end to end: an installed Agent
        # (the agents/installed layer) executes its step, and the host
        # completes the order under exactly that identity.
        from company.agents.core import Agent
        from company.workflows import WorkflowStep
        self.engine.resolution.agents = {"installed-agent": Agent(
            "installed-agent")}
        self._bind_and_resolve(self._workflow_of_steps((
            WorkflowStep("step-one", "installed-agent", "installed step",
                         evidence_kind="weekly_sales"),)))
        orders = self.active_orders()
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["agent_id"], "installed-agent")
        result = self.runtime.complete_work_order(
            orders[0]["id"], "installed-agent",
            [{"kind": "weekly_sales", "payload": {"weekly_sales": 1}}])
        self.assertEqual(result["work_order"]["status"], "completed")
        with self.assertRaises(RuntimeError):
            # A foreign identity cannot complete the next claim-then-complete
            # cycle even with the same evidence.
            self.runtime.claim_work_order(orders[0]["id"], "someone-else")

    def test_owner_named_step_agent_executes(self):
        # The goal owner is a valid executor by construction when a step
        # (or direct assignment) names them.
        from company.workflows import WorkflowStep
        result = self._bind_and_resolve(self._workflow_of_steps((
            WorkflowStep("step-one", "director", "owner-executed step",
                         evidence_kind="weekly_sales"),)))
        self.assertNotEqual(result.outcome.value, "ESCALATE_TO_GOAL")
        self.assertEqual(len(self.active_orders()), 1,
                         "the owner's own step parks its order for the owner")

    def test_uninstalled_direct_intervention_agent_is_a_wiring_defect(self):
        # Issue #4/#11: direct work assigned to an agent that is neither
        # installed nor the goal owner is a structural WIRING defect —
        # recorded as system_defect evidence for immediate repair, with
        # no WorkOrder and no blind re-decision.
        run = self.current_run()
        self.runtime.runs.update(
            run.id, stage=GoalStage.ACT, status="running",
            decision=Decision("request_agent", "ghost does the work", None,
                              {"agent_id": "ghost-agent",
                               "evidence_kind": "weekly_sales"}))
        self.runtime.runtime.interventions.create(
            goal_id=self.goal_id, run_id=run.id, kind="request_agent",
            description="ghost does the work",
            context={"agent_id": "ghost-agent",
                     "evidence_kind": "weekly_sales"})
        result = self.runtime.runtime.resolution.resolve(
            self.runtime.runtime.interventions.active_for_run(run.id).id)
        self.assertEqual(result.outcome.value, "SYSTEM_DEFECT")
        self.assertEqual(result.defect.kind, "wiring")
        self.assertIn("ghost-agent", result.message)
        self.assertIn("neither an installed Agent", result.message)
        self.assertEqual(self._order_count(), 0,
                         "no WorkOrder may be created for an uninstalled "
                         "direct-intervention agent")
        defects = [item for item in self.runtime.evidence.for_run(run.id)
                   if item.kind == "system_defect"]
        self.assertEqual(len(defects), 1,
                         "the defect is recorded durably for the repair")

    def test_owner_direct_intervention_executes(self):
        # The owner's own direct assignment executes: it parks the order
        # for the owner, who claims and completes it as the declared
        # agent (goal decide already validated the assignment).
        run = self.current_run()
        self.runtime.runs.update(
            run.id, stage=GoalStage.ACT, status="running",
            decision=Decision("request_agent", "owner does the work", None,
                              {"agent_id": "director",
                               "evidence_kind": "weekly_sales"}))
        self.runtime.runtime.interventions.create(
            goal_id=self.goal_id, run_id=run.id, kind="request_agent",
            description="owner does the work",
            context={"agent_id": "director",
                     "evidence_kind": "weekly_sales"})
        result = self.runtime.runtime.resolution.resolve(
            self.runtime.runtime.interventions.active_for_run(run.id).id)
        self.assertNotEqual(result.outcome.value, "ESCALATE_TO_GOAL")
        orders = self.active_orders()
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["agent_id"], "director",
                         "the direct order declares the owner as executor")
        self.assertEqual(orders[0]["claimed_by"], "director")
        done = self.runtime.complete_work_order(
            orders[0]["id"], "director",
            [{"kind": "weekly_sales", "payload": {"weekly_sales": 1}}])
        self.assertEqual(done["work_order"]["status"], "completed")


# =========================================================================
# 5. goal resume: stall parks continue, DECIDE parks decide
# =========================================================================

class TestGoalResume(DecideBoundaryCase):

    def test_resume_refuses_a_decision_request_park(self):
        self.new_goal()
        self.runtime.tick(max_advances=10)
        with self.assertRaises(ValueError) as caught:
            self.runtime.resume_goal(self.goal_id)
        self.assertIn("goal decide", str(caught.exception),
                      "the refusal must point at goal decide")
        self.assertEqual(self.current_run().status, "waiting",
                         "a refused resume must leave the park untouched")

    def test_resume_opens_a_stall_parked_run(self):
        self.new_goal(config={"aggregation": "latest"})
        self.engine.controller = _FlatController(self.engine.database)
        while self.current_run().status != "waiting":
            self.runtime.tick(max_advances=30)
        self.assertEqual(self.current_run().status, "waiting")
        self.assertEqual(len(self.pending_asks()), 1)
        result = self.runtime.resume_goal(self.goal_id)
        self.assertIn(result["run"]["status"], {"ready", "running"},
                      "resuming opens the parked run")
        self.assertEqual(self.pending_asks(), [],
                         "resuming retires the answered stall ask")

    def test_resume_refuses_a_goal_that_is_not_parked(self):
        self.new_goal()
        with self.assertRaises(ValueError) as caught:
            self.runtime.resume_goal(self.goal_id)
        self.assertIn("nothing to resume", str(caught.exception))

    def test_approve_step_scope_refuses_without_an_active_intervention(self):
        # F9(a) pin: a DECIDE park is answered with `goal decide`, not
        # approve — step-scoped approval without an active intervention
        # raises a clear error and grants nothing; the park and its one
        # ask stay exactly as they were.
        self.new_goal()
        self.runtime.tick(max_advances=10)
        self.assertEqual((self.current_run().stage,
                          self.current_run().status),
                         (GoalStage.DECIDE, "waiting"))
        with self.assertRaises(ValueError) as caught:
            self.runtime.approve(self.goal_id)
        self.assertIn("no active intervention to approve", str(caught.exception))
        self.assertIn("goal decide", str(caught.exception),
                      "the refusal must point at the DECIDE answer path")
        with self.runtime.connect() as connection:
            approvals = connection.execute(
                "SELECT COUNT(*) FROM core_approvals", ()).fetchone()[0]
        self.assertEqual(approvals, 0,
                         "a refused approve grants no keys")
        self.assertEqual(len(self.pending_asks()), 1,
                         "the park and its one ask stay untouched")

    def test_approve_run_scope_stays_coherent_without_an_intervention(self):
        # F9(a): run-wide grants keep working with intervention_id NULL —
        # a pre-grant for the run's later gates — even before any
        # intervention exists.
        self.new_goal()
        self.runtime.approve(self.goal_id, keys=("send",), scope="run")
        with self.runtime.connect() as connection:
            rows = connection.execute(
                "SELECT key,intervention_id FROM core_approvals").fetchall()
        self.assertEqual([(row[0], row[1]) for row in rows],
                         [("send", None)],
                         "run-scoped grants store intervention_id NULL")


# =========================================================================
# 6. The stall boundary: flat runs park, progressing runs never do
# =========================================================================

class TestStallBoundary(DecideBoundaryCase):

    def _drive_flat(self, count, config=None):
        """Advance one stage boundary at a time until the goal has ``count``
        evaluated runs (or parks)."""
        self.new_goal(config=config or {"aggregation": "latest"})
        self.engine.controller = _FlatController(self.engine.database)
        for _ in range(count * 6 + 8):
            if self._evaluated_count() >= count or self._parked_count():
                break
            self.engine.advance(self.goal_id)
        return self.current_run()

    def _evaluated_count(self) -> int:
        with self.runtime.connect() as connection:
            return connection.execute(
                """SELECT COUNT(*) FROM core_runs WHERE goal_id=?
                   AND evaluation_json IS NOT NULL""",
                (self.goal_id,)).fetchone()[0]

    def _parked_count(self) -> int:
        with self.runtime.connect() as connection:
            return connection.execute(
                """SELECT COUNT(*) FROM core_runs WHERE goal_id=?
                   AND status='waiting'""", (self.goal_id,)).fetchone()[0]

    def _run_states(self):
        with self.runtime.connect() as connection:
            return [(row[0], row[1]) for row in connection.execute(
                """SELECT sequence,status FROM core_runs WHERE goal_id=?
                   ORDER BY sequence""", (self.goal_id,))]

    def _evaluated_runs(self):
        with self.runtime.connect() as connection:
            return [dict(row) for row in connection.execute(
                """SELECT sequence,stage,status,evaluation_json FROM core_runs
                   WHERE goal_id=? ORDER BY sequence""",
                (self.goal_id,))]

    def test_three_flat_runs_park_the_next_one_with_a_stall_ask(self):
        self._drive_flat(3)
        rows = self._evaluated_runs()
        evaluated = [row for row in rows if row["evaluation_json"]]
        self.assertEqual(len(evaluated), 3,
                         "the flat goal must evaluate exactly three runs "
                         "before parking")
        self.assertEqual(rows[-1]["status"], "waiting",
                         "the fourth run is created parked, not ready")
        self.assertEqual(rows[-1]["stage"], "OBSERVE")
        asks = self.pending_asks()
        self.assertEqual(len(asks), 1)
        _Ask.assert_owner_ask(self, asks[0]["payload"])
        self.assertIn("stopped moving", asks[0]["payload"]["message"])
        # Owner voice: human progress, never the metric key; the resume
        # command rides the payload's answer_syntax, not owner text.
        self.assertIn("0 of 1 customers per week",
                      asks[0]["payload"]["message"])
        self.assertNotIn("weekly_sales", asks[0]["payload"]["message"])
        self.assertNotIn("company goal resume", asks[0]["payload"]["after"])
        self.assertIn("company goal resume",
                      asks[0]["payload"]["answer_syntax"]["resume"])
        self.assertEqual(asks[0]["payload"]["machine"]["metric"],
                         "weekly_sales")

    def test_two_flat_runs_still_chain(self):
        self._drive_flat(2)
        self.assertEqual([item[1] for item in self._run_states()],
                         ["complete", "complete", "ready"],
                         "fewer than stall_threshold flat runs chain "
                         "normally")

    def test_custom_stall_threshold_parks_after_two_flat_runs(self):
        self._drive_flat(2, config={"aggregation": "latest",
                                     "stall_threshold": 2})
        self.assertEqual([item[1] for item in self._run_states()],
                         ["complete", "complete", "waiting"])

    def test_review_every_parks_at_the_checkpoint(self):
        self.new_goal(config={"aggregation": "latest", "review_every": 2})
        self.engine.controller = _MovingController(self.engine.database)
        for _ in range(12):
            self.runtime.tick(max_advances=30)
        self.assertEqual(self._run_states(),
                         [(1, "complete"), (2, "complete"), (3, "waiting")],
                         "the review checkpoint parks the run after it")
        asks = self.pending_asks()
        self.assertEqual(len(asks), 1)
        self.assertIn("review checkpoint", asks[0]["payload"]["message"])
        # Owner voice: the cadence in owner words — the config key never
        # enters owner-facing text (it rides the machine payload).
        self.assertIn("every 2 runs", asks[0]["payload"]["message"])
        self.assertNotIn("review_every", asks[0]["payload"]["message"])
        self.assertEqual(asks[0]["payload"]["machine"]["review_every"], 2)
        self.assertIn("company goal resume",
                      asks[0]["payload"]["answer_syntax"]["resume"])

    def test_progressing_runs_chain_with_no_park_and_no_gate(self):
        # The pinned no-per-run-parking test: the metric moves each cycle,
        # so every run chains automatically and the only owner asks ever
        # created are real ones (none here).
        self.new_goal()
        self.engine.controller = _MovingController(self.engine.database)
        for _ in range(20):
            self.runtime.tick(max_advances=30)
        states = self._run_states()
        self.assertGreater(len(states), 5,
                           "a moving goal must keep chaining runs")
        self.assertEqual([item[1] for item in states[:-1]],
                         ["complete"] * (len(states) - 1),
                         "every evaluated run chains automatically")
        self.assertIn(states[-1][1], {"ready", "running", "complete"})
        self.assertEqual(self.pending_asks(), [],
                         "a progressing goal never creates an owner ask")

    def test_repeated_decision_parks_even_with_evidence_each_run(self):
        # F5 pin (replaces the zero-evidence carve-out): a flat metric with
        # the IDENTICAL decision repeated parks at stall_threshold even
        # when every run records evidence — evidence alone is not progress.
        self.new_goal()

        class _WorkButFlat(_FlatController):
            def evaluate(self, context, decision, evidence):
                return Evaluation(False, {context.goal.metric: 0},
                                  "clean-core evidence evaluated")

        self.engine.controller = _WorkButFlat(self.engine.database)
        self.engine.resolution.executor = _CompletingDirect()
        for _ in range(24):
            if self._evaluated_count() >= 3 or self._parked_count():
                break
            self.runtime.evidence.record(
                goal_id=self.goal_id, run_id=self.current_run().id,
                kind="weekly_sales", payload={"weekly_sales": 0})
            self.engine.advance(self.goal_id)
        states = self._run_states()
        self.assertEqual([item[1] for item in states[:-1]],
                          ["complete"] * (len(states) - 1),
                          "the first threshold-1 flat runs chain normally")
        self.assertEqual(states[-1][1], "waiting",
                         "a repeated decision on a flat metric parks even "
                         "though each run recorded evidence")
        asks = self.pending_asks()
        self.assertEqual(len(asks), 1)
        self.assertIn("same decision", asks[0]["payload"]["message"])
        _Ask.assert_owner_ask(self, asks[0]["payload"])

    def test_a_changed_decision_chains_without_parking(self):
        # F5 pin: a flat metric with a DIFFERENT decision each run chains —
        # DECIDE is still trying new bounded work, so the goal progresses.
        self.new_goal()

        class _ChangingDirect(_FlatController):
            counter = 0

            def decide(self, context, observation):
                self.counter += 1
                return Decision(
                    "request_agent", f"attempt {self.counter}", None,
                    {"agent_id": "director", "evidence_kind": "weekly_sales"})

            def evaluate(self, context, decision, evidence):
                return Evaluation(False, {context.goal.metric: 0},
                                  "clean-core evidence evaluated")

        self.engine.controller = _ChangingDirect(self.engine.database)
        self.engine.resolution.executor = _CompletingDirect()
        for _ in range(30):
            self.engine.advance(self.goal_id)
            if self._evaluated_count() >= 4 or self._parked_count():
                break
        self.assertEqual(self._evaluated_count(), 4,
                          "a changed decision on a flat metric keeps chaining")
        self.assertEqual(self.pending_asks(), [],
                         "changed decisions never create a stall ask")

    def test_same_kind_with_a_repeated_instruction_parks(self):
        # F5 pin: decision identity for direct work is kind+description —
        # a repeated instruction is the identical decision even when the
        # kind itself never changes.
        self.new_goal()

        class _SameKindRepeatedInstruction(_FlatController):
            def decide(self, context, observation):
                return Decision(
                    "request_agent", "close the very same deal", None,
                    {"agent_id": "director", "evidence_kind": "weekly_sales"})

            def evaluate(self, context, decision, evidence):
                return Evaluation(False, {context.goal.metric: 0},
                                  "clean-core evidence evaluated")

        self.engine.controller = _SameKindRepeatedInstruction(self.engine.database)
        self.engine.resolution.executor = _CompletingDirect()
        for _ in range(30):
            if self._evaluated_count() >= 3 or self._parked_count():
                break
            self.engine.advance(self.goal_id)
        self.assertEqual(self._parked_count(), 1,
                         "the identical instruction repeated on a flat metric "
                         "parks at the threshold")

    def test_fixable_loop_parks_after_consecutive_budget_exhaustions(self):
        # F5 pin: a fixable executor double parks after
        # ESCALATION_PARK_THRESHOLD consecutive local-budget exhaustions
        # of the same intervention, with a D4-style owner ask naming the
        # loop — and every exhaustion keeps its resolution_iteration
        # evidence.
        from company.agents.core import AgentResult
        from company.runtime.engine import ESCALATION_PARK_THRESHOLD
        budget = 2
        self.new_goal()
        self.engine.controller = _DirectWorkController(self.engine.database)
        self.engine.resolution.max_local_iterations = budget
        self.engine.resolution.executor = _FixableForever()
        for _ in range(20):
            self.engine.advance(self.goal_id)
            run = self.current_run()
            if run.status == "waiting" and run.stage == GoalStage.ACT:
                break
        run = self.current_run()
        self.assertEqual((run.stage, run.status), (GoalStage.ACT, "waiting"),
                         "the fixable loop must park the run for the owner")
        self.assertEqual(run.sequence, 1,
                         "the park opens no follow-on run")
        asks = self.pending_asks()
        self.assertEqual(len(asks), 1, "exactly one owner ask for the park")
        payload = asks[0]["payload"]
        _Ask.assert_owner_ask(self, payload)
        self.assertIn("fix loop", payload["message"])
        self.assertIn("do the work", payload["message"],
                      "the ask names the looping intervention")
        # Owner voice: no CLI syntax in owner-facing text.
        self.assertNotIn("company goal resume", payload["after"])
        self.assertIn("company goal resume",
                      payload["answer_syntax"]["resume"])
        iterations = [item for item in
                      self.runtime.evidence.for_goal(self.goal_id)
                      if item.kind == "resolution_iteration"]
        self.assertEqual(len(iterations),
                         ESCALATION_PARK_THRESHOLD * budget,
                         "each exhaustion records its iteration evidence")

    def test_a_met_target_never_parks(self):
        self.new_goal()
        self.engine.controller = _FlatController(self.engine.database)

        class _MetController(_FlatController):
            def evaluate(self, context, decision, evidence):
                return Evaluation(True, {context.goal.metric: 1}, "target met")

        self.engine.controller = _MetController(self.engine.database)
        self.runtime.tick(max_advances=30)
        self.assertEqual(self.runtime.goals.get(self.goal_id).status, "complete")
        self.assertEqual(self.pending_asks(), [])


# =========================================================================
# 7. Owner-facing texts across every park path
# =========================================================================

class TestOwnerFacingAsks(DecideBoundaryCase):

    def test_every_owner_ask_is_structured_and_content_free(self):
        # DECIDE park
        self.new_goal()
        self.runtime.tick(max_advances=10)
        parks = [self.pending_asks()[0]["payload"]]
        # Escalation park: answer the park with bounded direct work, then
        # let the executor hit the same wall until the goal parks.
        from company.agents.core import AgentResult

        class _Escalate:
            def execute(self, agent, order):
                return AgentResult("escalate", message="the wall")

        self.engine.resolution.executor = _Escalate()
        for _ in range(24):
            run = self.current_run()
            if run.status == "waiting" and run.stage == GoalStage.ACT:
                break  # the escalation park
            if (run.stage == GoalStage.DECIDE and run.status == "waiting"
                    and run.decision is not None
                    and run.decision.kind == "decision_request"):
                self.runtime.decide_goal(self.goal_id, "request_agent",
                                         agent="director",
                                         instruction="try the bounded step")
            else:
                self.runtime.tick(max_advances=10)
        parks.extend(item["payload"] for item in self.pending_asks()
                     if item["payload"] not in parks)
        for payload in parks:
            _Ask.assert_owner_ask(self, payload)
        self.assertTrue(any("escalation" in item["message"].lower()
                            for item in parks),
                        "the escalation park must stay identifiable")
        self.assertTrue(any("hit the same wall" in item["message"]
                            for item in parks))

    def test_no_owner_ask_uses_the_legacy_placeholder(self):
        # Issue #5: the parked order is host work; only genuine owner
        # asks (none here) carry the full owner-ask shape.
        self.new_goal()
        self.runtime.tick(max_advances=10)
        self.runtime.decide_goal(self.goal_id, "request_agent", agent="director",
                                 instruction="Close one deal")
        for item in self.pending_asks():
            self.assertEqual(item["kind"], "host_work_required")
            for key in ("message", "why", "decision", "after"):
                self.assertTrue(
                    item["payload"].get(key) and
                    str(item["payload"][key]).strip(),
                    f"the host-work payload is missing {key}")


# =========================================================================
# 8. Context projection: goal tree and cross-scope memory
# =========================================================================

class TestContextProjection(DecideBoundaryCase):

    def test_projection_renders_the_whole_goal_tree_nested(self):
        parent = self.new_goal(name="Parent outcome")
        self.runtime.create_goal(
            name="Child outcome", owner_id="director", metric="child_metric",
            operator="ge", target=1, parent_id=parent["id"],
            config={"aggregation": "latest"})
        self.runtime.create_goal(
            name="Second child", owner_id="director", metric="child_metric",
            operator="ge", target=1, parent_id=parent["id"],
            config={"aggregation": "latest"})
        # An owner filter that matches nothing: the tree still shows every
        # active goal, regardless of the owner_id filter.
        projection = self.runtime.assemble_context(prompt="what is next",
                                                  owner_id="nobody")
        context = projection["context"]
        self.assertIn("Goals:", context)
        self.assertIn("Parent outcome", context)
        self.assertIn("Child outcome", context)
        self.assertIn("Second child", context)
        tree = context.split("Goals:", 1)[1]
        parent_lines = [line for line in tree.splitlines()
                        if line.strip().startswith("Parent outcome")]
        child_lines = [line for line in tree.splitlines()
                       if line.strip().startswith(("Child outcome", "Second child"))]
        self.assertEqual(len(parent_lines), 1)
        self.assertEqual(len(child_lines), 2)
        self.assertTrue(all(not line.startswith("  ") for line in parent_lines),
                        "the parent renders at the tree root")
        self.assertTrue(all(line.startswith("  ") for line in child_lines),
                        "children must be indented under their parent")
        parent_index = tree.splitlines().index(parent_lines[0])
        for line in child_lines:
            self.assertGreater(tree.splitlines().index(line), parent_index,
                               "children render after their parent")
        self.assertIn(f"goal:{parent['id']}", projection["sources"])
        self.assertIn("Run 1", context)
        # Owner voice: the human lines carry no raw goal ids, metric keys,
        # or stage/status enums; the Machine reference line at the end
        # still carries every one of them for the Director.
        human, _, machine = context.partition("Machine reference:")
        self.assertNotIn(parent["id"], human)
        self.assertNotIn("child_metric", human)
        self.assertNotIn("OBSERVE", human)
        self.assertIn("child_metric", machine)
        self.assertIn(parent["id"], machine)
        self.assertIn("(OBSERVE/ready)", machine)

    def test_projection_names_blockers_of_the_focus_goal(self):
        self.new_goal(name="Focus outcome")
        blocker = self.runtime.create_goal(
            name="Unfinished blocker", owner_id="director", metric="block_metric",
            operator="ge", target=1, config={"aggregation": "latest"})
        self.runtime.goals.add_block(blocker["id"], self.goal_id)
        # F8: the blocked goal can never be ready, so the active blocker
        # is the ready() focus; pausing it leaves nothing ready and the
        # fallback focuses the blocked goal, whose blocker reaches the
        # projection through its Blocked by line.
        projection = self.runtime.assemble_context(prompt="what is next",
                                                  owner_id="director")
        self.assertEqual(projection["goal_id"], blocker["id"])
        self.runtime.goals.set_status(blocker["id"], "paused")
        projection = self.runtime.assemble_context(prompt="what is next",
                                                  owner_id="director")
        self.assertIn("Blocked by: Unfinished blocker", projection["context"])
        self.assertIn(f"goal:{blocker['id']}", projection["sources"])
        # A completed blocker is no longer blocking context.
        self.runtime.goals.set_status(blocker["id"], "complete")
        projection = self.runtime.assemble_context(prompt="what is next",
                                                   owner_id="director")
        self.assertNotIn("Blocked by:", projection["context"])

    def test_focus_follows_ready_priority_order_not_creation_order(self):
        # F8(a) pin: a low-priority Goal created FIRST is not the focus
        # when a critical-priority Goal is ready — the projection follows
        # the scheduler's runs.ready() order, not creation order.
        self.new_goal(name="Low priority goal",
                      config={"aggregation": "latest", "priority": "low"})
        critical = self.runtime.create_goal(
            name="Critical goal", owner_id="director", metric="weekly_sales",
            operator="ge", target=1,
            config={"aggregation": "latest", "priority": "critical"})
        ready_ids = [run.goal_id for run in self.runtime.runs.ready()]
        self.assertEqual(ready_ids.index(critical["id"]), 0)
        projection = self.runtime.assemble_context(prompt="what is next",
                                                  owner_id="director")
        self.assertEqual(projection["goal_id"], critical["id"],
                         "the ready() order decides the focus, not creation")
        self.assertIn("Goal: Critical goal", projection["context"])
        self.assertNotIn("Goal: Low priority goal", projection["context"])

    def test_fallback_focus_is_the_most_recently_updated_active_goal(self):
        # F8(a) pin: when nothing is ready (both goals park their DECIDE
        # ask), the focus falls back to the most recently updated active
        # goal — the one the loop touched last.
        first = self.new_goal(name="First parked")
        second = self.runtime.create_goal(
            name="Second parked", owner_id="director", metric="weekly_sales",
            operator="ge", target=1, config={"aggregation": "latest"})
        self.runtime.tick(max_advances=10)  # both park decision_request
        self.assertEqual([run.goal_id for run in self.runtime.runs.ready()],
                         [], "nothing is ready while both goals park")
        projection = self.runtime.assemble_context(owner_id="director")
        self.assertEqual(projection["goal_id"], second["id"],
                         "the most recently updated active goal is the "
                         "fallback focus")
        self.assertIn("Goal: Second parked", projection["context"])

    def test_projection_renders_only_relevant_memory_once(self):
        # Issue #6: ordinary reasoning context carries the goal's own
        # workflow/strategy claims (once, on the Relevant memory line)
        # and owner preferences once on the Profile line. An unrelated
        # goal's learning stays OUT, and no claim renders twice.
        self.new_goal()
        run = self.current_run()
        evidence = self.runtime.evidence.record(
            goal_id=self.goal_id, run_id=run.id, kind="weekly_sales",
            payload={"weekly_sales": 0})
        self.runtime.set_profile_claim(namespace="owner", claim_key="pref",
                                       value="concise reports")
        self.runtime.add_memory("workflow", "batch throttling at 25/hour held",
                                 evidence_ids=[evidence.id],
                                 goal_id=self.goal_id, run_id=run.id)
        self.runtime.add_memory("strategy", "double opt-in lifts reply quality",
                                evidence_ids=[evidence.id],
                                 goal_id=self.goal_id, run_id=run.id)
        other = self.runtime.create_goal(
            name="Unrelated", owner_id="director", metric="other_metric",
            operator="ge", target=1, config={"aggregation": "latest"})
        other_run = self.runtime.runs.current(other["id"])
        other_evidence = self.runtime.evidence.record(
            goal_id=other["id"], run_id=other_run.id, kind="other_metric",
            payload={"other_metric": 0})
        self.runtime.add_memory(
            "strategy", "unrelated campaign lesson stays private",
            evidence_ids=[other_evidence.id], goal_id=other["id"],
            run_id=other_run.id)
        projection = self.runtime.assemble_context(prompt="what is next",
                                                  owner_id="director")
        context = projection["context"]
        self.assertNotIn("Memory: ", context,
                         "no arbitrary company-wide memory line in ordinary "
                         "reasoning context")
        self.assertIn("Relevant memory:", context)
        self.assertIn("batch throttling at 25/hour held", context,
                      "the goal's own workflow learning renders")
        self.assertIn("double opt-in lifts reply quality", context,
                      "the goal's own strategy learning renders")
        self.assertNotIn("unrelated campaign lesson stays private", context,
                         "an unrelated goal's learning must not leak in")
        self.assertIn("Profile: owner.pref=", context,
                      "owner profile claims stay on their own line")
        self.assertEqual(context.count("concise reports"), 1,
                         "no owner claim renders twice (Profile is the only "
                         "line for owner memory)")
        rendered_claims = [item.claim for item in self.runtime.memory.relevant(
            goal_id=self.goal_id) if item.scope != "owner"]
        for claim in rendered_claims:
            self.assertEqual(context.count(claim), 1,
                             f"claim rendered more than once: {claim}")

    def test_projection_superseded_memory_is_not_rendered(self):
        self.new_goal()
        run = self.current_run()
        evidence = self.runtime.evidence.record(
            goal_id=self.goal_id, run_id=run.id, kind="weekly_sales",
            payload={"weekly_sales": 0})
        first = self.runtime.add_memory("workflow", "v1 claim",
                                        evidence_ids=[evidence.id],
                                        goal_id=self.goal_id, run_id=run.id)
        second = self.runtime.memory.remember(
            "workflow", "v2 claim", evidence_ids=(evidence.id,),
            goal_id=self.goal_id, run_id=run.id, supersedes_id=first.id)
        self.assertEqual(self.runtime.memory.get(first.id).status, "superseded")
        projection = self.runtime.assemble_context(prompt="what is next",
                                                  owner_id="director")
        self.assertNotIn("v1 claim", projection["context"])
        self.assertIn("v2 claim", projection["context"])


@with_departments
class TestContextProjectionDecisionLines(DecideBoundaryCase):
    """F8(b): the focus goal renders its recent decisions (last 3 runs:
    sequence, decision kind, resolution outcome) and the Departments
    declaring its metric."""

    def setUp(self):
        os.environ["SPIELOS_TEST_DEPARTMENTS_DIR"] = str(FIXTURES / "departments")
        super().setUp()

    def tearDown(self):
        os.environ.pop("SPIELOS_TEST_DEPARTMENTS_DIR", None)
        super().tearDown()

    def test_recent_decisions_and_declaring_departments_render(self):
        # A director-owned goal whose metric a fixture Department declares
        # (seo: keyword_opportunities): DECIDE chooses the workflow with
        # no park and no ask, the first step parks as host work, the
        # order completes, and the evaluated run chains the next ready
        # run — leaving the goal with real decision history.
        self.new_goal(metric="keyword_opportunities",
                      config={"aggregation": "latest",
                              "priority": "critical"})
        self.runtime.tick(max_advances=10)
        self.assertEqual(self.current_run().decision.kind, "execute_workflow",
                         "the decidable goal chooses its workflow itself")
        order = self.active_orders()[0]
        self.runtime.complete_work_order(
            order["id"], order["agent_id"],
            [{"kind": order["brief"].get("evidence_kind") or "keyword_opportunities",
              "payload": {"keyword_opportunities": 0}}])
        self.runtime.tick(max_advances=10)  # EVALUATE -> next run ready
        projection = self.runtime.assemble_context(
            prompt="what is next", owner_id="director")
        self.assertEqual(projection["goal_id"], self.goal_id)
        context = projection["context"]
        self.assertIn("Recent decisions:", context,
                      "the focus goal renders its decision history")
        # Owner voice: decision kinds and resolution outcomes render in
        # owner words; the enums ride the Machine reference line.
        self.assertIn("run 1 ran a candidate workflow", context)
        human, _, machine = context.partition("Machine reference:")
        self.assertNotIn("execute_workflow", human)
        self.assertIn("run 1 execute_workflow", machine)
        self.assertIn("Departments that can move this goal: seo", context,
                      "the Departments declaring the focus metric render")

    def test_decision_lines_stay_absent_without_content(self):
        # A brand-new goal has no decided runs and a metric nobody
        # declares: neither line renders (empty lines are omitted, not
        # rendered hollow).
        self.new_goal(metric="fresh_metric",
                      config={"aggregation": "latest"})
        projection = self.runtime.assemble_context(
            prompt="what is next", owner_id="director")
        context = projection["context"]
        self.assertNotIn("Recent decisions:", context)
        self.assertNotIn("Departments that can move this goal:", context)
        # The Machine reference line always rides along for the Director
        # (here it carries the focus goal alone) — but its content never
        # leaks into the human lines above it.
        self.assertIn("Machine reference:", context)
        human, _, machine = context.partition("Machine reference:")
        self.assertNotIn("fresh_metric", human)
        self.assertIn("fresh_metric", machine)


# =========================================================================
# 9. Bootstrap update: the spine refreshes, parked state never changes
# =========================================================================

class TestUpdatePreservesParkedState(unittest.TestCase):
    """`spielos update` rewrites the vendored spine only. Parked Runs,
    Interventions, WorkOrders, and notifications are the owner's state:
    an update must leave every one of them byte-identical, and the state
    it preserved must stay live for the runtime afterwards."""

    def _seed_home(self, home: Path) -> Path:
        from company.runtime.bootstrap import scaffold
        from company.state import Database

        scaffold(home)  # a real vendored home
        database = home / ".spielos" / "state" / "company.sqlite"
        Database(database)  # creates the clean-core schema
        stamp = "2026-01-01T00:00:00+00:00"
        raw = sqlite3.connect(database)
        raw.execute("PRAGMA foreign_keys=ON")
        raw.execute(
            "INSERT INTO core_goals VALUES (?,?,?,?,?,?,?,?,?)",
            ("goal-parked", "Parked goal", "m", "ge", "1", None, "active",
             stamp, stamp))
        raw.execute("INSERT INTO core_goal_metadata VALUES (?,?,?,?)",
                    ("goal-parked", "director", None,
                     json.dumps({"aggregation": "latest"})))
        raw.execute("""INSERT INTO core_runs VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    ("run-parked", "goal-parked", 1, "ACT", "waiting",
                     json.dumps({"m": 0, "evidence_ids": []}),
                     json.dumps({"kind": "request_agent",
                                 "description": "close one enterprise deal",
                                 "workflow_id": None,
                                 "context": {"agent_id": "director",
                                             "evidence_kind": "m"}}),
                     None, stamp, stamp))
        raw.execute("""INSERT INTO core_interventions VALUES
            (?,?,?,?,?,?,?,?,?,?)""",
            ("intervention-parked", "goal-parked", "run-parked",
             "request_agent", "close one enterprise deal", "waiting", None,
             json.dumps({"agent_id": "director", "evidence_kind": "m"}),
             stamp, stamp))
        raw.execute("""INSERT INTO core_work_orders
            (id,goal_id,run_id,intervention_id,workflow_run_id,agent_id,step_id,
             brief_json,status,claimed_by,claimed_at,lease_expires_at,attempt,
             result_json,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("work-parked", "goal-parked", "run-parked",
             "intervention-parked", None, "director", "direct",
             json.dumps({"instruction": "close one enterprise deal",
                         "evidence_kind": "m"}),
             "open", "director", stamp, None, 1, None, stamp, stamp))
        raw.execute("""INSERT INTO core_notifications
            (id,goal_id,run_id,intervention_id,kind,payload_json,status,
             created_at,acknowledged_at) VALUES (?,?,?,?,?,?,?,?,NULL)""",
            ("notification-parked", "goal-parked", "run-parked",
             "intervention-parked", "owner_input_required",
             json.dumps({"message": "close one enterprise deal",
                         "required_user_action": "close one enterprise deal"}),
             "pending", stamp))
        raw.commit()
        raw.close()
        return database

    @staticmethod
    def _parked_state(database: Path) -> dict:
        with sqlite3.connect(database) as connection:
            connection.row_factory = sqlite3.Row
            order = dict(connection.execute(
                "SELECT status,result_json FROM core_work_orders"
            ).fetchone())
            intervention = dict(connection.execute(
                "SELECT status,resolution_outcome FROM core_interventions"
            ).fetchone())
            run = dict(connection.execute(
                "SELECT stage,status,decision_json FROM core_runs"
            ).fetchone())
            notification = dict(connection.execute(
                "SELECT status FROM core_notifications").fetchone())
        return {"order": order, "intervention": intervention,
                "run": run, "notification": notification}

    def test_update_leaves_parked_direct_work_untouched(self):
        import tempfile

        from company.runtime.bootstrap import scaffold

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            database = self._seed_home(home)
            before = self._parked_state(database)

            scaffold(home, force=True)

            after = self._parked_state(database)
            self.assertEqual(before, after,
                             "an update refreshes the vendored spine only; "
                             "parked Runs, Interventions, WorkOrders, and "
                             "notifications are the owner's state")
            self.assertEqual(after["order"]["status"], "open",
                             "genuine bounded direct work stays open")
            self.assertEqual((after["run"]["stage"], after["run"]["status"]),
                             ("ACT", "waiting"))
            self.assertEqual(after["intervention"]["status"], "waiting")
            self.assertEqual(after["notification"]["status"], "pending")

            # The preserved state is still live: completing the direct
            # order (claim first — the documented flow; only the declared
            # agent may claim) wakes the parked run into EVALUATE.
            runtime = CleanCommandRuntime(database)
            runtime.claim_work_order("work-parked", "director")
            runtime.complete_work_order(
                "work-parked", "director",
                [{"kind": "m", "payload": {"m": 1}}])
            current = runtime.runs.current("goal-parked")
            self.assertEqual((current.stage, current.status),
                             (GoalStage.EVALUATE, "running"),
                             "state the update preserved stays usable")

    def test_second_update_is_also_a_no_op_for_state(self):
        import tempfile

        from company.runtime.bootstrap import scaffold

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            database = self._seed_home(home)
            scaffold(home, force=True)
            first = self._parked_state(database)
            scaffold(home, force=True)
            second = self._parked_state(database)
            self.assertEqual(first, second,
                             "repeated updates never rewrite owner state")


if __name__ == "__main__":
    unittest.main(verbosity=2)
