"""The learning loop: memory is causal or it is decoration (audit L1-L4).

Pins for the learning-architecture system-improvement Goal:

- L1 (A) causal injection: a workflow step completing with ``--learning``
  persists workflow-scope memory, and the NEXT WorkOrder opened for the
  same workflow carries those claims inside its brief (bounded, newest
  ~5) — a double-executor probe proves Run 2 behaves differently with
  the learning present and identically to Run 1 without it. Direct
  WorkOrder briefs carry the goal's relevant non-owner claims, which
  since the direct-memory fix include the goal's own direct-work
  lessons (workflow-scope, ``workflow_id`` NULL): completing a direct
  order with ``--learning`` makes the SAME goal's next direct order
  causally carry that lesson, while another workflow's claims and
  another goal's lessons never reach it.
- L1 (B) relevance topology: active strategy claims reach the same
  Goal, siblings (same owner+metric), parent, child, and
  supports-related Goals through explicit SQL joins; an unrelated Goal
  receives nothing — asserted both ways. No embeddings.
- L2 (C) producers: strategy memory is written only by owner direction
  or evidence-backed host distillation; the deterministic controller
  fabricates none; 20 execution evidence events with exactly 2 reusable
  lessons write exactly 2 memories, not 20.
- L3 (E/F) repetition signal + evolution lineage: three similar
  completed direct WorkOrders surface as a bounded repetition entry;
  adopting a revised workflow bumps its version while a historical
  WorkflowRun keeps its exact version and steps snapshot, and the
  revision reason+evidence persist as active workflow-scope memory.
- L4 (G/H) hygiene: the retire verb flips an active claim out of the
  active set without deleting the row or its evidence, and relevant()
  excludes it; the classification write paths keep their guards (owner
  scope only via profile set, memory add refuses owner scope,
  workflow/strategy memory require evidence and lineage).

Run:  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.agents python3 -B -m unittest \\
          company.tests.test_learning_loop -v
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"

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


from company.agents.core import Agent, AgentEvidence, AgentResult  # noqa: E402
from company.commands.goal_runtime import CleanCommandRuntime  # noqa: E402
from company.runtime.engine import GoalStage  # noqa: E402
from company.workflows import Workflow, WorkflowRepository, WorkflowStep  # noqa: E402


class LearningLoopCase(unittest.TestCase):
    """Shared: fresh CleanCommandRuntime on a throwaway database."""

    def setUp(self):
        self.db = temp_db()
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

    def active_orders(self):
        return self.runtime.work_orders(status="active", goal_id=self.goal_id)

    def tick_until(self, predicate, budget=40):
        for _ in range(budget):
            self.runtime.tick(max_advances=50)
            if predicate():
                return True
        return predicate()

    def park_bounded_direct_work(self, instruction="close one deal",
                                 agent="director", evidence_kind=None):
        """Tick to the DECIDE park, answer it, and return the parked order."""
        self.tick_until(lambda: (self.current_run().stage == GoalStage.DECIDE
                                 and self.current_run().status == "waiting"))
        self.runtime.decide_goal(
            self.goal_id, "request_agent", agent=agent,
            instruction=instruction, evidence_kind=evidence_kind)
        orders = self.active_orders()
        assert orders, "answering the decision_request must park the work order"
        return orders[0]

    def _strategy_evidence(self, goal_id, payload=None):
        run = self.runtime.runs.current(goal_id)
        return self.runtime.evidence.record(
            goal_id=goal_id, run_id=run.id, kind="m",
            payload=payload or {self.runtime.goals.get(goal_id).metric: 1})


# =========================================================================
# A. L1 causal injection: learning reaches the next WorkOrder brief
# =========================================================================

@with_departments
class TestCausalWorkflowLearning(LearningLoopCase):
    """Work happens, learning persists, the NEXT execution differs."""

    @classmethod
    def setUpClass(cls):
        os.environ["SPIELOS_TEST_DEPARTMENTS_DIR"] = str(
            FIXTURES / "departments")

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("SPIELOS_TEST_DEPARTMENTS_DIR", None)

    def setUp(self):
        super().setUp()
        # An unmeetable target so the loop chains runs instead of
        # completing; the requested workflow pins the choice (requested
        # candidates bypass the F6 flat-history exclusion), and the
        # gate-free keyword-research flow needs no approvals.
        self.new_goal(name="Map opportunities", owner="seo",
                      metric="keyword_opportunities", target=999,
                      config={"aggregation": "count",
                              "workflow": "keyword-research"})

    class _LearningExecutor:
        """Double executor: what it does depends on the brief's memory.

        Writes one workflow lesson on the first execution (when the
        brief carries no claims) and then executes differently —
        ``applied_learning`` — once the same workflow runs again and the
        brief carries the recorded lesson. The double is the causal
        probe: behavior must differ between the empty brief and the
        taught one.
        """

        LESSON = "Prefer measured search-console rows over seed guesses"

        def __init__(self):
            self.executions: list[tuple[str, tuple[str, ...], bool]] = []

        def execute(self, agent, order):
            claims = tuple(order.brief.get("memory") or ())
            self.executions.append((order.step_id, claims, bool(claims)))
            kinds = tuple(order.brief.get("evidence_kinds")
                         or ((order.brief.get("evidence_kind"),)
                             if order.brief.get("evidence_kind") else ()))
            return AgentResult(
                "completed",
                evidence=tuple(
                    AgentEvidence(kind, {"keyword_opportunities": 0,
                                         "applied_learning": bool(claims)})
                    for kind in (kinds or ("intervention_result",))),
                workflow_learning=None if claims else self.LESSON)

    def _drive_two_executions(self, executor):
        for _ in range(60):
            self.runtime.tick(max_advances=50)
            firsts = [item for item in executor.executions
                      if item[0] == "seeds"]
            if len(firsts) >= 2:
                return firsts
        return [item for item in executor.executions if item[0] == "seeds"]

    def test_run_two_behaves_differently_with_the_learning_present(self):
        # The causal pin (item A): the SAME workflow executes again on a
        # new run/intervention; the WorkOrder opened for the second
        # execution carries the workflow's recorded learning inside its
        # brief, and an executor double proves behavior differs with
        # the learning present versus absent.
        executor = self._LearningExecutor()
        self.engine.resolution.executor = executor
        firsts = self._drive_two_executions(executor)
        self.assertGreaterEqual(
            len(firsts), 2,
            "the workflow must execute twice (two runs) for the causal "
            "double to bite")
        first_step, first_claims, first_applied = firsts[0]
        second_step, second_claims, second_applied = firsts[1]
        self.assertEqual(first_step, "seeds")
        self.assertEqual(first_claims, (),
                         "run 1 starts with no recorded learning")
        self.assertFalse(first_applied)
        self.assertIn(self._LearningExecutor.LESSON, second_claims,
                      "run 2's WorkOrder brief carries the workflow's "
                      "recorded learning claim")
        self.assertTrue(second_applied,
                        "with the learning present the executor double "
                        "behaves differently — memory changed execution")

    def test_the_next_orders_brief_carries_the_claim_string(self):
        # The brief carries the claim strings themselves (never memory
        # ids): the persisted WorkOrder row for the second execution
        # carries the lesson in brief_json.
        executor = self._LearningExecutor()
        self.engine.resolution.executor = executor
        self._drive_two_executions(executor)
        orders = [order for order in self.runtime.work_orders(
            goal_id=self.goal_id, limit=50) if order["step_id"] == "seeds"]
        self.assertGreaterEqual(len(orders), 2)
        self.assertEqual(orders[0]["brief"].get("memory"), [],
                         "the first execution's brief carries no claims")
        self.assertIn(self._LearningExecutor.LESSON,
                      orders[-1]["brief"]["memory"],
                      "the next execution's persisted brief carries the "
                      "workflow's active learning claims")
        self.assertNotIn("memory-", json.dumps(orders[-1]["brief"]),
                         "briefs carry claim strings, not memory ids")

    def test_brief_memory_is_bounded_to_the_newest_five(self):
        # A long-lived workflow cannot grow its brief without limit: the
        # brief carries at most the newest five claims, proved through a
        # real WorkOrder opened for the workflow after the extra claims
        # exist.
        executor = self._LearningExecutor()
        self.engine.resolution.executor = executor
        self._drive_two_executions(executor)
        # Record six more workflow-scope claims on this workflow through
        # the real memory add path, so seven active claims exist.
        run = self.current_run()
        workflow_id = "seo:keyword-research"
        extra = []
        for index in range(6):
            evidence = self.runtime.evidence.record(
                goal_id=self.goal_id, run_id=run.id, kind="m",
                payload={"keyword_opportunities": 0})
            extra.append(self.runtime.add_memory(
                "workflow", f"extra lesson {index}",
                evidence_ids=[evidence.id], goal_id=self.goal_id,
                run_id=run.id, workflow_id=workflow_id))
        self.assertEqual(len(extra), 6)
        self.assertEqual(len(self.runtime.memory.relevant(
            scope="workflow", workflow_id=workflow_id)), 7,
            "the workflow now carries seven active claims")
        # Drive the next real execution and read the brief the seam
        # opened for it.
        seeds_before = len([item for item in executor.executions
                            if item[0] == "seeds"])
        for _ in range(40):
            self.runtime.tick(max_advances=50)
            seeds = [item for item in executor.executions
                     if item[0] == "seeds"]
            if len(seeds) > seeds_before:
                break
        seeds = [item for item in executor.executions if item[0] == "seeds"]
        self.assertGreater(len(seeds), seeds_before,
                           "the workflow must execute again with the "
                           "seven claims recorded")
        taught_brief = seeds[-1][1]
        self.assertEqual(len(taught_brief), 5,
                         "the opened order's brief is bounded to five "
                         "claims")
        self.assertIn("extra lesson 5", taught_brief,
                      "the bound keeps the NEWEST claims")
        self.assertNotIn(self._LearningExecutor.LESSON, taught_brief,
                         "the oldest claim drops off first")


class TestDirectBriefMemory(LearningLoopCase):
    """L1: direct WorkOrder briefs carry the goal's relevant non-owner
    claims — its own direct-work lessons (workflow_id NULL) included,
    other workflows' claims and other goals' lessons excluded."""

    def test_direct_brief_carries_goal_relevant_claims_not_owner_claims(self):
        # Strategy learning recorded on the goal reaches the direct
        # order's brief; owner profile claims never do (they are not
        # operational context for the work).
        self.new_goal()
        run = self.current_run()
        evidence = self.runtime.evidence.record(
            goal_id=self.goal_id, run_id=run.id, kind="m", payload={"m": 0})
        self.runtime.memory.remember(
            "strategy", "call the champion before the close",
            evidence_ids=(evidence.id,), goal_id=self.goal_id, run_id=run.id)
        self.runtime.set_profile_claim(
            namespace="outbound", claim_key="tone", value="direct")
        order = self.park_bounded_direct_work(instruction="close one deal")
        self.assertEqual(order["step_id"], "direct")
        brief = order["brief"]
        self.assertIn("call the champion before the close",
                      brief["memory"],
                      "the direct brief carries the goal-relevant claims")
        self.assertEqual([claim for claim in brief["memory"]
                          if "outbound.tone" in claim], [],
                         "owner profile claims never reach a work brief")

    def test_direct_brief_memory_is_bounded_and_empty_when_no_claims(self):
        self.new_goal()
        order = self.park_bounded_direct_work(instruction="close one deal")
        self.assertEqual(order["brief"]["memory"], [],
                         "with no claims recorded the brief carries an "
                         "empty bounded list")

    def test_workflow_claims_do_not_reach_a_direct_brief_by_goal(self):
        # Contract updated for the direct-memory fix (live-tested
        # defect): a workflow-scope claim with a REAL workflow_id still
        # applies only with that workflow_id — a direct order for the
        # goal does not inherit another workflow's claims, and a
        # workflow_id-NULL lesson of a DIFFERENT goal never reaches this
        # goal's query either. The goal's own workflow_id-NULL lessons
        # DO reach its direct briefs — pinned separately below.
        self.new_goal()
        run = self.current_run()
        evidence = self.runtime.evidence.record(
            goal_id=self.goal_id, run_id=run.id, kind="m", payload={"m": 0})
        self.runtime.memory.remember(
            "workflow", "warm intros convert better",
            evidence_ids=(evidence.id,), goal_id=self.goal_id,
            run_id=run.id, workflow_id="outbound:email-outreach")
        other = self.runtime.create_goal(
            name="Other", owner_id="director", metric="m_other",
            operator="ge", target=1, config={"aggregation": "latest"})
        other_run = self.runtime.runs.current(other["id"])
        other_evidence = self.runtime.evidence.record(
            goal_id=other["id"], run_id=other_run.id, kind="m_other",
            payload={"m_other": 0})
        self.runtime.memory.remember(
            "workflow", "another goal's direct lesson",
            evidence_ids=(other_evidence.id,), goal_id=other["id"],
            run_id=other_run.id)
        order = self.park_bounded_direct_work(instruction="close one deal")
        self.assertEqual(order["step_id"], "direct")
        self.assertEqual(order["brief"]["memory"], [],
                         "workflow claims with a real workflow_id reach "
                         "only that workflow's briefs, and a different "
                         "goal's workflow_id-NULL lessons never reach "
                         "this goal's direct orders")

    def test_a_direct_brief_carries_the_goals_own_direct_work_lessons(self):
        # The new contract's positive half (the fix for the orphaned
        # direct-work learning, live-tested on the probe goal): a
        # workflow-scope claim with workflow_id NULL recorded on THIS
        # goal is the goal's own operational learning and reaches this
        # goal's direct briefs exactly.
        self.new_goal()
        run = self.current_run()
        evidence = self.runtime.evidence.record(
            goal_id=self.goal_id, run_id=run.id, kind="m", payload={"m": 0})
        self.runtime.memory.remember(
            "workflow", "validate the domain before enriching",
            evidence_ids=(evidence.id,), goal_id=self.goal_id,
            run_id=run.id)
        order = self.park_bounded_direct_work(instruction="close one deal")
        self.assertEqual(order["step_id"], "direct")
        self.assertIn("validate the domain before enriching",
                      order["brief"]["memory"],
                      "the goal's own direct-work lessons "
                      "(workflow_id NULL) reach its direct briefs")

    def test_direct_learning_is_causal_for_the_next_direct_order(self):
        # The direct->direct causal pin (acceptance B): complete one
        # direct order with --learning through the real write path,
        # drive the SAME goal to its next direct order, and assert the
        # new order's brief memory carries the lesson — while a
        # DIFFERENT goal's direct brief does not. This is the seam that
        # was orphaned before the fix: the claim was stored with full
        # lineage but no goal query could ever retrieve it.
        self.new_goal(name="Causal direct", metric="m", target=999)
        order = self.park_bounded_direct_work(
            instruction="produce the metric evidence")
        self.runtime.complete_work_order(
            order["id"], "director",
            [{"kind": "m", "payload": {"m": 1}}],
            learning="run one lesson: validate before enriching")
        claims = [item.claim for item in self.runtime.memory.relevant(
            goal_id=self.goal_id)]
        self.assertIn("run one lesson: validate before enriching", claims,
                      "the direct completion's lesson is retrievable by "
                      "goal query immediately after the write")
        # Drive the SAME goal to its next direct order.
        next_order = self.park_bounded_direct_work(
            instruction="produce the metric evidence again")
        self.assertEqual(next_order["step_id"], "direct")
        self.assertNotEqual(next_order["id"], order["id"])
        self.assertIn("run one lesson: validate before enriching",
                      next_order["brief"]["memory"],
                      "the SAME goal's next direct order carries the "
                      "lesson its previous direct order learned — "
                      "direct-work learning is causal")
        # A DIFFERENT goal's direct brief does not carry it.
        other = self.runtime.create_goal(
            name="Other goal", owner_id="director", metric="m_other",
            operator="ge", target=1, config={"aggregation": "latest"})
        saved = self.goal_id
        self.goal_id = other["id"]
        foreign_order = self.park_bounded_direct_work(
            instruction="unrelated work")
        self.goal_id = saved
        self.assertEqual(foreign_order["step_id"], "direct")
        self.assertNotIn("run one lesson: validate before enriching",
                         foreign_order["brief"]["memory"],
                         "a different goal's direct brief carries none "
                         "of this goal's direct-work lessons")


# =========================================================================
# B. L1 relevance topology: parent, child, supports — never unrelated
# =========================================================================

class TestStrategyTopology(LearningLoopCase):
    """Strategy claims reach structurally related Goals only."""

    def setUp(self):
        super().setUp()
        # Topology: Parent <- Focus <- Child, plus a supports partner and
        # a goal with no structural relation at all.
        self.parent = self.runtime.create_goal(
            name="Parent", owner_id="director", metric="parent_metric",
            operator="ge", target=1, config={"aggregation": "latest"})
        self.focus = self.new_goal(name="Focus", metric="m",
                                   parent_id=self.parent["id"])
        self.child = self.runtime.create_goal(
            name="Child", owner_id="director", metric="child_metric",
            operator="ge", target=1, parent_id=self.goal_id,
            config={"aggregation": "latest"})
        self.supporter = self.runtime.create_goal(
            name="Supporter", owner_id="director", metric="supporter_metric",
            operator="ge", target=1, config={"aggregation": "latest"})
        self.runtime.goals.add_support(self.supporter["id"], self.goal_id)
        self.unrelated = self.runtime.create_goal(
            name="Unrelated", owner_id="director", metric="unrelated_metric",
            operator="ge", target=1, config={"aggregation": "latest"})

    def _claim_on(self, goal_id, claim):
        run = self.runtime.runs.current(goal_id)
        evidence = self.runtime.evidence.record(
            goal_id=goal_id, run_id=run.id, kind="m", payload={"m": 1})
        return self.runtime.memory.remember(
            "strategy", claim, evidence_ids=(evidence.id,),
            goal_id=goal_id, run_id=run.id)

    def _relevant_claims(self, goal_id):
        return [item.claim for item in self.runtime.memory.relevant(
            goal_id=goal_id, limit=20)]

    def test_strategy_claims_reach_parent_child_and_supports_goals(self):
        # A strategy claim recorded on the focus goal reaches its parent,
        # its child, and its supports partner (both directions of the
        # edge are covered by the two queries below).
        self._claim_on(self.goal_id, "focus goal learned this")
        for goal_id, name in ((self.parent["id"], "parent"),
                              (self.child["id"], "child"),
                              (self.supporter["id"], "supports partner")):
            self.assertIn("focus goal learned this",
                          self._relevant_claims(goal_id),
                          f"the claim must reach the {name} goal")

    def test_strategy_claims_flow_both_directions_on_supports_edges(self):
        # A claim recorded on the supporter reaches the goal it supports.
        self._claim_on(self.supporter["id"], "supporter learned this")
        self.assertIn("supporter learned this",
                      self._relevant_claims(self.goal_id),
                      "supports edges are bidirectional for strategy "
                      "relevance")

    def test_unrelated_goal_receives_nothing_both_ways(self):
        # Unrelated: no edge, different owner+metric, no parent link.
        # Asserted both ways — the unrelated goal gets none of the
        # focus's claims, and the focus gets none of its claims.
        self._claim_on(self.goal_id, "focus goal learned this")
        self._claim_on(self.unrelated["id"], "unrelated learned this")
        unrelated_claims = self._relevant_claims(self.unrelated["id"])
        self.assertNotIn("focus goal learned this", unrelated_claims,
                         "an unrelated goal receives no strategy claims "
                         "from the focus goal")
        focus_claims = self._relevant_claims(self.goal_id)
        self.assertNotIn("unrelated learned this", focus_claims,
                         "the focus goal receives none of the unrelated "
                         "goal's claims")

    def test_own_and_sibling_claims_still_reach(self):
        # The F7 sibling join (same owner+metric) and the goal's own
        # claims stay intact on top of the new topology joins.
        sibling = self.runtime.create_goal(
            name="Sibling", owner_id="director", metric="m",
            operator="ge", target=1, config={"aggregation": "latest"})
        self._claim_on(self.goal_id, "focus goal learned this")
        self._claim_on(sibling["id"], "sibling learned this")
        claims = self._relevant_claims(self.goal_id)
        self.assertIn("focus goal learned this", claims,
                      "the goal's own claims still reach it")
        self.assertIn("sibling learned this", claims,
                      "sibling-goal learning (same owner+metric) is "
                      "preserved")

    def test_blocks_edges_do_not_reach(self):
        # Topology relevance is supports-only: a blocking relationship
        # is a scheduling dependency, not shared strategy.
        blocker = self.runtime.create_goal(
            name="Blocker", owner_id="director", metric="blocker_metric",
            operator="ge", target=1, config={"aggregation": "latest"})
        self.runtime.goals.add_block(blocker["id"], self.goal_id)
        self._claim_on(blocker["id"], "blocker learned this")
        self.assertNotIn("blocker learned this",
                          self._relevant_claims(self.goal_id),
                          "blocks edges do not join strategy relevance")


# =========================================================================
# C. L2 producers: selective, never fabricated, never per event
# =========================================================================

class TestSelectiveProducers(LearningLoopCase):
    """20 execution events with 2 lessons write 2 memories, not 20."""

    def test_twenty_events_two_lessons_write_two_memories(self):
        # The real write paths: evidence add records execution events
        # (no memory), tasks --complete --learning persists exactly one
        # workflow claim when a lesson exists, and completing without a
        # lesson persists nothing. Selectivity is the pin: 20 events
        # must not become 20 memories.
        self.new_goal(name="Selective")
        memories_before = [m for m in self.runtime.memories(limit=100)
                           if m["status"] == "active"]
        events = 0
        lessons = 0
        for index in range(3):
            # Three bounded direct orders: two carry a lesson, one does
            # not (the conditional-producer rule).
            self.tick_until(lambda: (self.current_run().stage
                                     == GoalStage.DECIDE
                                     and self.current_run().status
                                     == "waiting"))
            self.runtime.decide_goal(
                self.goal_id, "request_agent", agent="director",
                instruction=f"produce the metric evidence {index}")
            order = self.active_orders()[0]
            learning = None
            if index < 2:
                learning = f"reusable lesson {index}"
                lessons += 1
            self.runtime.complete_work_order(
                order["id"], "director",
                [{"kind": "m", "payload": {"m": 1}}], learning=learning)
            events += 1
            # The order's completion records one evidence event.
        # Pad the remaining events through the real `evidence add` path.
        run = self.current_run()
        while events < 20:
            self.runtime.add_evidence(self.goal_id, kind="m",
                                     source="probe",
                                     payload={"m": 1})
            events += 1
        self.assertEqual(events, 20)
        self.assertEqual(lessons, 2)
        memories_after = [m for m in self.runtime.memories(limit=100)
                         if m["status"] == "active"]
        written = [m for m in memories_after
                   if m["id"] not in {b["id"] for b in memories_before}]
        self.assertEqual(len(written), 2,
                         "exactly the two genuinely reusable lessons "
                         "persist as memory — not one row per event")
        self.assertEqual({m["scope"] for m in written}, {"workflow"})
        self.assertEqual(
            sorted(m["claim"] for m in written),
            ["reusable lesson 0", "reusable lesson 1"])
        self.assertEqual([m for m in written if m["scope"] == "strategy"],
                         [],
                         "the deterministic completion path never "
                         "fabricates strategy memory")

    def test_no_lesson_no_memory_row(self):
        # Completing without --learning persists no memory at all: the
        # producer is conditional, never per completion.
        self.new_goal(name="Quiet")
        order = self.park_bounded_direct_work(
            instruction="produce the metric evidence")
        self.runtime.complete_work_order(
            order["id"], "director",
            [{"kind": "m", "payload": {"m": 1}}])
        self.assertEqual([m for m in self.runtime.memories(limit=100)
                           if m["status"] == "active"], [],
                          "a completion with no lesson writes no memory")

    def test_owner_strategy_direction_is_a_valid_producer(self):
        # The two legitimate strategy producers: owner direction
        # (memory add --scope strategy with lineage) and evidence-backed
        # host distillation at the evaluate boundary (the engine guard).
        self.new_goal(name="Strategy producers")
        run = self.current_run()
        evidence = self.runtime.evidence.record(
            goal_id=self.goal_id, run_id=run.id, kind="m", payload={"m": 1})
        record = self.runtime.add_memory(
            "strategy", "pivot ICP to mid-market services",
            evidence_ids=[evidence.id], goal_id=self.goal_id, run_id=run.id)
        self.assertEqual(record.scope, "strategy")
        self.assertEqual(record.evidence_ids, (evidence.id,))
        self.assertIn("pivot ICP to mid-market services",
                      [item.claim for item in self.runtime.memory.relevant(
                          goal_id=self.goal_id)])


# =========================================================================
# E. L3 repetition signal: three similar direct orders surface
# =========================================================================

class TestRepetitionSignal(LearningLoopCase):
    """The read model names repeated direct work that merits a Workflow."""

    def _complete_similar_order(self, instruction="close one deal this week"):
        self.tick_until(lambda: (self.current_run().stage == GoalStage.DECIDE
                                 and self.current_run().status == "waiting"))
        self.runtime.decide_goal(
            self.goal_id, "request_agent", agent="director",
            instruction=instruction)
        order = self.active_orders()[0]
        self.runtime.claim_work_order(order["id"], "director")
        self.runtime.complete_work_order(
            order["id"], "director",
            [{"kind": "weekly_sales", "payload": {"weekly_sales": 0}}])
        return order

    def test_three_similar_direct_orders_surface_one_repetition_entry(self):
        self.new_goal(name="Repetitive", metric="weekly_sales")
        for _ in range(3):
            self._complete_similar_order()
        board = self.runtime.observe()
        entries = [item for item in board["repetition"]
                   if item["goal_id"] == self.goal_id]
        self.assertEqual(len(entries), 1,
                         "exactly one bounded repetition entry for the goal")
        entry = entries[0]
        self.assertEqual(entry["completed_orders"], 3)
        self.assertEqual(entry["agent_id"], "director")
        self.assertEqual(entry["instruction"], "close one deal this week")
        self.assertIn("Workflow", entry["suggestion"],
                      "the entry suggests the work may merit a reusable "
                      "Workflow")

    def test_two_similar_orders_do_not_surface(self):
        # The signal starts at three: two similar orders are ordinary.
        self.new_goal(name="Barely repetitive", metric="weekly_sales")
        for _ in range(2):
            self._complete_similar_order()
        board = self.runtime.observe()
        self.assertEqual([item for item in board["repetition"]
                          if item["goal_id"] == self.goal_id], [],
                          "two similar orders do not surface a signal")

    def test_different_instructions_do_not_group(self):
        # The simplest defensible similarity: same goal, same agent,
        # same instruction (whitespace-normalized). Different
        # instructions are different work.
        self.new_goal(name="Varied", metric="weekly_sales")
        for index in range(3):
            self._complete_similar_order(
                instruction=f"close deal number {index}")
        board = self.runtime.observe()
        self.assertEqual([item for item in board["repetition"]
                          if item["goal_id"] == self.goal_id], [],
                          "three different instructions are not repetition")

    def test_repetition_is_bounded(self):
        # At most five entries surface, whatever the history holds.
        self.new_goal(name="Bounded", metric="weekly_sales")
        goals = [self.goal_id]
        for index in range(1, 8):
            other = self.runtime.create_goal(
                name=f"Repetitive {index}", owner_id="director",
                metric="weekly_sales", operator="ge", target=999,
                config={"aggregation": "latest"})
            goals.append(other["id"])
        for goal_id in goals:
            saved = self.goal_id
            self.goal_id = goal_id
            for _ in range(3):
                self._complete_similar_order()
            self.goal_id = saved
        board = self.runtime.observe()
        self.assertLessEqual(len(board["repetition"]), 5,
                             "the repetition list is bounded")


# =========================================================================
# F. L3 evolution lineage: version bump, historical snapshots, reason
# =========================================================================

class TestEvolutionLineage(LearningLoopCase):
    """Revision history stays exact while the definition moves forward."""

    def setUp(self):
        super().setUp()
        self.new_goal(name="Evolving", metric="m")
        self.repository = WorkflowRepository(self.runtime.database)

    def _start_run(self, workflow):
        run = self.current_run()
        intervention = self.runtime.interventions.create(
            goal_id=self.goal_id, run_id=run.id, kind="execute_workflow",
            description="lineage probe", context={})
        return self.repository.start(
            workflow.id, goal_id=self.goal_id, run_id=run.id,
            intervention_id=intervention.id)

    def test_revising_a_workflow_bumps_its_version(self):
        first = Workflow("evolution:probe", "Probe", (
            WorkflowStep("one", "director", "original step",
                         evidence_kind="m"),), "evolution")
        saved = self.repository.save(first)
        self.assertEqual(saved.version, 1)
        revised = Workflow("evolution:probe", "Probe revised", (
            WorkflowStep("one", "director", "revised step",
                         evidence_kind="m"),), "evolution")
        bumped = self.repository.save(revised)
        self.assertEqual(bumped.version, 2,
                         "adopting a revised workflow bumps the version")

    def test_a_historical_run_keeps_its_exact_version_and_snapshot(self):
        first = Workflow("evolution:snapshot", "Snapshot", (
            WorkflowStep("one", "director", "original step",
                         evidence_kind="m"),), "evolution")
        self.repository.save(first)
        historical = self._start_run(first)
        self.assertEqual(historical.workflow_version, 1)
        # Adopt the revision AFTER the run executed: the definition moves
        # forward, the historical run does not.
        self.repository.save(Workflow(
            "evolution:snapshot", "Snapshot", (
                WorkflowStep("one", "director", "revised step",
                             evidence_kind="m"),
                WorkflowStep("two", "director", "added step",
                             evidence_kind="m")), "evolution"))
        current = self.repository.get("evolution:snapshot")
        self.assertEqual(current.version, 2)
        reread = self.repository.run(historical.id)
        self.assertEqual(reread.workflow_version, 1,
                         "a historical WorkflowRun keeps its exact "
                         "workflow_version after the bump")
        self.assertEqual([step.instruction for step in reread.steps],
                         ["original step"],
                         "a historical WorkflowRun keeps its exact steps "
                         "snapshot after the bump")

    def test_revision_reason_and_evidence_persist_as_workflow_memory(self):
        # A revision is proposed through adoption with reason+evidence:
        # the reason and the justifying evidence ids persist as one
        # active workflow-scope claim on that workflow, written through
        # the real memory path with the workflow's lineage.
        workflow = Workflow("evolution:memory", "Memory probe", (
            WorkflowStep("one", "director", "original step",
                         evidence_kind="m"),), "evolution")
        self.repository.save(workflow)
        run = self.current_run()
        first_evidence = self.runtime.evidence.record(
            goal_id=self.goal_id, run_id=run.id, kind="m", payload={"m": 0})
        second_evidence = self.runtime.evidence.record(
            goal_id=self.goal_id, run_id=run.id, kind="m", payload={"m": 0})
        revision_reason = ("add a validation step: run 1 completed "
                           "without proving the map")
        claim = self.runtime.add_memory(
            "workflow", f"revised to v2 — {revision_reason}",
            evidence_ids=[first_evidence.id, second_evidence.id],
            goal_id=self.goal_id, run_id=run.id,
            workflow_id=workflow.id)
        self.assertEqual(claim.scope, "workflow")
        self.assertEqual(claim.workflow_id, workflow.id)
        self.assertEqual(claim.evidence_ids,
                         (first_evidence.id, second_evidence.id))
        claims = [item.claim for item in self.runtime.memory.relevant(
            scope="workflow", workflow_id=workflow.id)]
        self.assertIn(f"revised to v2 — {revision_reason}", claims,
                      "the revision claim with its reason is retrievable "
                      "on that workflow's lineage")


# =========================================================================
# G/H. L4 hygiene: retire, write-path consolidation, classification
# =========================================================================

class TestMemoryRetire(LearningLoopCase):
    """The retire verb: one writer, row and evidence preserved."""

    def setUp(self):
        super().setUp()
        self.new_goal(name="Hygiene")
        run = self.current_run()
        evidence = self.runtime.evidence.record(
            goal_id=self.goal_id, run_id=run.id, kind="m", payload={"m": 1})
        self.record = self.runtime.memory.remember(
            "workflow", "an architecture invariant, not reusable learning",
            evidence_ids=(evidence.id,), goal_id=self.goal_id, run_id=run.id)

    def test_retire_flips_the_claim_out_of_the_active_set(self):
        retired = self.runtime.memory.retire(self.record.id)
        self.assertNotEqual(retired.status, "active",
                             "retiring flips the claim to a non-active "
                             "status")
        self.assertEqual(retired.status, "superseded",
                         "retire reuses the existing non-active status "
                         "value — no schema change")

    def test_retire_preserves_the_row_and_its_evidence(self):
        retired = self.runtime.memory.retire(self.record.id)
        reread = self.runtime.memory.get(self.record.id)
        self.assertEqual(reread.claim, retired.claim)
        self.assertEqual(reread.evidence_ids, self.record.evidence_ids,
                         "the evidence rows stay attached, untouched")
        with self.runtime.connect() as connection:
            rows = connection.execute(
                "SELECT COUNT(*) FROM core_memory WHERE id=?",
                (self.record.id,)).fetchone()[0]
        self.assertEqual(rows, 1, "the row is never deleted")

    def test_relevant_excludes_retired_claims(self):
        run = self.current_run()
        kept = self.runtime.memory.remember(
            "strategy", "genuine operational lesson that stays",
            evidence_ids=(self.runtime.evidence.record(
                goal_id=self.goal_id, run_id=run.id, kind="m",
                payload={"m": 1}).id,),
            goal_id=self.goal_id, run_id=run.id)
        self.runtime.memory.retire(self.record.id)
        claims = [item.claim for item in self.runtime.memory.relevant(
            goal_id=self.goal_id, limit=20)]
        self.assertNotIn(self.record.claim, claims,
                         "relevant() excludes the retired claim")
        self.assertIn(kept.claim, claims,
                      "active claims stay reachable after a retire")

    def test_retire_is_idempotent_and_unknown_ids_raise(self):
        first = self.runtime.memory.retire(self.record.id)
        second = self.runtime.memory.retire(self.record.id)
        self.assertEqual(first.status, second.status,
                         "retiring an already-retired claim is idempotent")
        with self.assertRaises(KeyError):
            self.runtime.memory.retire("memory-doesnotexist")

    def test_one_writer_both_completion_paths_share_it(self):
        # L4 consolidation: the executor path and the CLI
        # tasks --complete --learning path persist workflow learning
        # through the ONE ResolutionCycle writer — both records carry
        # the same write shape (workflow scope, the order's evidence,
        # full lineage, and the WorkflowRun's workflow_id).
        self.engine.resolution.executor = _WorkflowStepCompleter()
        # Executor path: a workflow step completing with workflow_learning.
        workflow = Workflow("hygiene:probe", "Probe", (
            WorkflowStep("one", "director", "step one",
                         evidence_kind="m"),), "hygiene")
        WorkflowRepository(self.runtime.database).save(workflow)
        run = self.current_run()
        intervention = self.runtime.interventions.create(
            goal_id=self.goal_id, run_id=run.id, kind="execute_workflow",
            description="probe", context={"workflow_id": workflow.id})
        self.engine.resolution.agents = {"director": Agent("director")}
        before = {m["id"] for m in self.runtime.memories(limit=100)}
        self.engine.resolution.resolve(intervention.id)
        executor_memories = [m for m in self.runtime.memories(limit=100)
                             if m["scope"] == "workflow"
                             and m["id"] not in before]
        self.assertEqual(
            len(executor_memories), 1,
            "the executor path persists exactly its one workflow lesson")
        self.assertIn("one seam", executor_memories[0]["claim"])
        # CLI path: tasks --complete --learning on a direct order parked
        # for the host (a parking executor, so the order waits like the
        # real host flow does).
        self.engine.resolution.executor = _ParkingExecutor()
        self.goal_id = self.new_goal(name="CLI path")["id"]
        order = self.park_bounded_direct_work(
            instruction="produce the metric evidence")
        self.runtime.complete_work_order(
            order["id"], "director",
            [{"kind": "m", "payload": {"m": 1}}],
            learning="CLI completions should surface learning")
        cli_memories = [m for m in self.runtime.memories(limit=100)
                        if m["scope"] == "workflow"
                        and m["goal_id"] == self.goal_id
                        and m["id"] not in before]
        self.assertEqual(len(cli_memories), 1)
        # Same write shape: full lineage recorded by the one writer, and
        # each claim is grounded in the evidence of its own completion
        # (verified through the repository, which carries evidence ids).
        for record in executor_memories + cli_memories:
            self.assertIsNotNone(record["goal_id"])
            self.assertIsNotNone(record["run_id"])
            self.assertIsNotNone(record["intervention_id"])
            self.assertTrue(self.runtime.memory.get(
                record["id"]).evidence_ids,
                "workflow memory carries its evidence")
        self.assertFalse(hasattr(self.runtime, "_remember_workflow_learning"),
                         "the CLI's duplicate writer is gone; the "
                         "ResolutionCycle method is the single authority")


class _WorkflowStepCompleter:
    """Executor double: completes a workflow step with workflow learning."""

    def execute(self, agent, order):
        kinds = tuple(order.brief.get("evidence_kinds")
                     or ((order.brief.get("evidence_kind"),)
                         if order.brief.get("evidence_kind") else ()))
        return AgentResult(
            "completed",
            evidence=tuple(AgentEvidence(kind or "m", {"m": 1})
                           for kind in (kinds or ("m",))),
            workflow_learning="executor path writes through one seam")


class _ParkingExecutor:
    """Executor double: parks every order for the host to complete."""

    def execute(self, agent, order):
        return AgentResult(
            "ask_user", message=f"WorkOrder {order.id} is ready")


class TestClassificationWritePaths(LearningLoopCase):
    """H: the guards stay the single classification authority."""

    def test_profile_set_writes_owner_scope_only(self):
        record = self.runtime.set_profile_claim(
            namespace="layout", claim_key="canonical-folders",
            value={"rule": "one canonical layer per concept"})
        self.assertEqual(record["scope"], "owner",
                         "profile set writes owner scope only")
        self.assertEqual(record["evidence_ids"], (),
                         "owner claims need no evidence")

    def test_memory_add_refuses_owner_scope(self):
        self.new_goal(name="Refused")
        with self.assertRaises(ValueError) as caught:
            self.runtime.add_memory("owner", "owner claims use profile set")
        self.assertIn("profile set", str(caught.exception))

    def test_workflow_memory_requires_evidence_and_lineage(self):
        self.new_goal(name="Guarded")
        run = self.current_run()
        with self.assertRaises(ValueError):
            self.runtime.add_memory("workflow", "no evidence",
                                    goal_id=self.goal_id, run_id=run.id)
        evidence = self.runtime.evidence.record(
            goal_id=self.goal_id, run_id=run.id, kind="m", payload={"m": 1})
        with self.assertRaises(ValueError):
            self.runtime.add_memory("workflow", "no lineage",
                                    evidence_ids=[evidence.id])

    def test_strategy_memory_requires_evidence_and_lineage(self):
        self.new_goal(name="Guarded strategy")
        run = self.current_run()
        with self.assertRaises(ValueError):
            self.runtime.add_memory("strategy", "no evidence",
                                    goal_id=self.goal_id, run_id=run.id)
        evidence = self.runtime.evidence.record(
            goal_id=self.goal_id, run_id=run.id, kind="m", payload={"m": 1})
        with self.assertRaises(ValueError):
            self.runtime.add_memory("strategy", "no lineage",
                                    evidence_ids=[evidence.id])

    def test_memory_evidence_must_belong_to_the_same_goal_and_run(self):
        self.new_goal(name="Guarded A")
        other = self.runtime.create_goal(
            name="Guarded B", owner_id="director", metric="m_other",
            operator="ge", target=1, config={"aggregation": "latest"})
        other_run = self.runtime.runs.current(other["id"])
        cross = self.runtime.evidence.record(
            goal_id=other["id"], run_id=other_run.id, kind="m_other",
            payload={})
        with self.assertRaises(ValueError):
            self.runtime.add_memory(
                "workflow", "cross-goal claim", evidence_ids=[cross.id],
                goal_id=self.goal_id, run_id=self.current_run().id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
