"""CLI adapter for the canonical clean core."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from ..agents.core import AgentResult
from ..agents.loader import available_agents
from ..evidence import EvidenceRepository
from ..goals import GoalRepository
from ..layout import layout_summary
from ..memory import MemoryRepository
from ..resolution.core import ApprovalRepository
from ..state import Database
from ..observability import Observer
from ..work_orders import WorkOrderRepository
from ..workflows import Workflow, WorkflowRepository, WorkflowStep
from ..runtime.engine import (  # noqa: F401
    Decision, Evaluation, GoalRuntime, GoalStage,
    human_attention, human_decision, human_goal_status, human_outcome,
    human_loop_position, human_progress, human_resolution)
from ..runtime.registry import departments
from ..runtime.util import compare


def _declared_metrics(handler) -> set[str]:
    """Metrics a Department declares (the D3 rule, shared by goal creation
    and DECIDE candidate filtering)."""
    declared = set(getattr(handler, "evidence_metrics", {}) or ())
    schema = getattr(handler, "goal_schema", None) or {}
    declared.update(schema.get("metrics") or ())
    return declared


class CatalogController:
    """Translate portable Department declarations into clean Workflows.

    The intelligent DECIDE/EVALUATE boundary: deterministic mechanics,
    reasoning over goal/observation/evidence/memory/topology/candidates/
    history, one concrete typed Decision, and evidence-backed strategy
    distillation. Writes happen only through the engine's committed
    boundaries.
    """

    def __init__(self, database: Database, *, agents: dict | None = None,
                 memory: MemoryRepository | None = None):
        self.database = database
        self.goals = GoalRepository(database)
        self.workflows = WorkflowRepository(database)
        self.memory = memory or MemoryRepository(
            database, EvidenceRepository(database))
        self.departments = departments()
        self.agents = dict(agents or {})

    def observe(self, context) -> dict:
        goal = context.goal
        handler = self.departments.get(goal.owner_id)
        kinds = tuple((getattr(handler, "evidence_metrics", {}) or {}).get(goal.metric) or ())
        if kinds:
            matching = [item for item in context.evidence if item.kind in kinds]
        elif handler is not None:
            # The department owns this metric space but does not declare a
            # mapping for this metric. Match only evidence whose payload
            # carries the metric key itself; never count unrelated kinds.
            matching = [item for item in context.evidence
                        if isinstance(item.payload.get(goal.metric),
                                      (int, float, bool))]
        else:
            # Departmentless owner (director): every evidence item is a
            # direct answer to the goal.
            matching = list(context.evidence)
        candidates = [item.payload[goal.metric] for item in matching
                      if isinstance(item.payload.get(goal.metric), (int, float, bool))]
        aggregation = goal.aggregation
        if aggregation == "count":
            value = len(matching)
        elif aggregation == "sum":
            value = sum(item for item in candidates if not isinstance(item, bool))
        elif aggregation == "latest":
            value = candidates[-1] if candidates else 0
        elif aggregation == "max":
            numeric = [item for item in candidates if not isinstance(item, bool)]
            value = max(numeric) if numeric else 0
        elif aggregation == "min":
            numeric = [item for item in candidates if not isinstance(item, bool)]
            value = min(numeric) if numeric else 0
        elif aggregation == "boolean_all":
            value = bool(candidates) and all(item is True for item in candidates)
        else:
            value = any(item is True for item in candidates)
        if goal.owner_id == "director" and goal.metric == "all_children_achieved":
            children = [item for item in self.goals.list() if item.parent_id == goal.id]
            value = bool(children) and all(item.status == "complete" for item in children)
        return {goal.metric: value, "evidence_ids": [item.id for item in matching]}

    def decide(self, context, observation: dict) -> Decision:
        """The intelligent DECIDE boundary: reason over the goal, its
        observation, evidence, strategy and operational memory, run
        history (failed approaches and escalations), the goal topology,
        candidate Departments/Workflows/Agents, and learned structure —
        then return one concrete typed Decision.

        The owner is NEVER the default reasoner. A decision_request
        parks only at a genuine boundary: every candidate approach has
        been tried and judged (a material strategic choice), or nothing
        bounded is even declarable (host reasoning park — the Director
        agent answers with `goal decide`, and only relays to the owner
        when owner-only context or authority is genuinely required).
        """
        goal = context.goal
        if compare(observation.get(goal.metric, 0), goal.operator, goal.target):
            return Decision("evaluate", "Goal evidence meets its target",
                            context={"result_ready": True})
        # A bounded repair goal assigns its repair work directly: the
        # defect contract (kind, summary, workflow, step, allowed shape)
        # plus the acceptance requirement IS the concrete bounded
        # instruction — no Department declares 'acceptance_green', and
        # the repair never waits on a host park or an owner.
        repair = (goal.config or {}).get("repair")
        if isinstance(repair, dict):
            agent_id = goal.owner_id
            evidence_kind = goal.metric
            instruction = self._repair_instruction(goal, repair)
            return Decision("request_agent", instruction, None, context={
                "agent_id": agent_id, "evidence_kind": evidence_kind,
                "instruction": instruction, "repair": repair})
        requested = (goal.config or {}).get("workflow")
        # 1. Learned structure first (issue #3): a crystallized Workflow
        #    for this goal's repeated work shape is the neural path — the
        #    next equivalent request reuses it instead of rebuilding the
        #    procedure from zero.
        learned = self._crystallized_workflow(context, observation)
        if learned is not None:
            return learned
        # 2. Candidate workflows, ranked by strategy learning and run
        #    history instead of blind declaration order (issue #1/#2).
        candidates = self._candidates(goal)
        if requested:
            chosen = next(
                (item for item in candidates if item.id == requested), None)
            if chosen is not None:
                return self._workflow_decision(goal, chosen)
        ranked = [item for item in candidates
                  if not self._strategy_avoids(goal, item)
                  and not self._recently_escalated(goal, item)]
        preferred = [item for item in ranked if self._strategy_prefers(goal, item)]
        if preferred:
            return self._workflow_decision(goal, preferred[0])
        if ranked:
            return self._workflow_decision(goal, ranked[0])
        # 3. No usable candidate remains.
        if candidates:
            # Every approach was tried and judged: a genuine owner
            # boundary (material strategic choice), not a host park.
            request = self.decision_request(context, observation)
            return Decision("decision_request", request["message"], context={
                "decision_request": request, "agent_id": None,
                "evidence_kind": None, "owner_boundary": "exhausted"})
        request = self.decision_request(context, observation)
        return Decision("decision_request", request["message"], context={
            "decision_request": request, "agent_id": None,
            "evidence_kind": None, "owner_boundary": "undecided"})

    @staticmethod
    def _repair_instruction(goal, repair: dict) -> str:
        """One concrete, bounded repair instruction synthesized from the
        defect contract: what is broken, which definition, and the
        acceptance proof the repair must carry. The executing
        system-improvement Agent gets the same contract through the
        order brief's repair payload."""
        summary = repair.get("summary") or "a structural defect"
        workflow = repair.get("workflow_id") or "the affected definition"
        step = repair.get("step_id") or "its broken step"
        return (f"Repair the {repair.get('defect_kind', 'structural')} "
                f"defect in {workflow} (step {step}): {summary[:400]}. "
                "Fix only the defective behavior, keep the spine "
                "byte-identical between company/ and its template twin, "
                "and complete with behavioral acceptance evidence — "
                "payload key 'acceptance_green': true plus the revised "
                "workflow definition under 'workflow'.")

    #: Distillation conventions: the machine-checkable prefix that links
    #: a strategy lesson to the approach it judges (issue #2). The claim
    #: text stays human-readable AND deterministic to rank from.
    AVOID_PREFIX = "avoid "
    PREFER_PREFIX = "prefer "

    def _strategy_claims(self, goal, workflow_id: str) -> list[str]:
        """Active strategy claims linked to one candidate approach."""
        return [item.claim for item in self.memory.relevant(
            goal_id=goal.id, limit=50)
            if item.scope == "strategy" and item.workflow_id == workflow_id]

    def _strategy_avoids(self, goal, workflow) -> bool:
        """True when an evidence-backed strategy lesson says this
        approach underperformed on this goal."""
        workflow_id = self._workflow_id(goal, workflow)
        return any(claim.startswith(self.AVOID_PREFIX)
                   for claim in self._strategy_claims(goal, workflow_id))

    def _strategy_prefers(self, goal, workflow) -> bool:
        """True when an evidence-backed strategy lesson says this
        approach outperformed another on this goal."""
        workflow_id = self._workflow_id(goal, workflow)
        return any(claim.startswith(self.PREFER_PREFIX)
                   for claim in self._strategy_claims(goal, workflow_id))

    def _recently_escalated(self, goal, workflow) -> bool:
        """True when this workflow's most recent execution on this goal
        ended escalated — a failed approach (defects are repaired
        separately; an escalated execution says the approach could not
        complete its job for this goal)."""
        workflow_id = self._workflow_id(goal, workflow)
        with self.database.connect() as connection:
            row = connection.execute("""SELECT i.resolution_outcome
                FROM core_workflow_runs wr
                JOIN core_interventions i ON i.id=wr.intervention_id
                WHERE wr.workflow_id=? AND wr.goal_id=?
                ORDER BY wr.created_at DESC LIMIT 1""",
                (workflow_id, goal.id)).fetchone()
        return bool(row and row[0] == "ESCALATE_TO_GOAL")

    def _workflow_id(self, goal, workflow) -> str:
        department_id = workflow.department_id or goal.owner_id
        return f"{department_id}:{workflow.id}"

    def _workflow_decision(self, goal, workflow) -> Decision:
        workflow_id = self._workflow_id(goal, workflow)
        if not isinstance(workflow, Workflow):
            raise TypeError("Department workflows must use company.workflows.Workflow")
        # A persisted definition SUPERSEDES the declaration: repairs and
        # crystallized structure live in core_workflows (versioned, with
        # provenance), so DECIDE never re-declares an older shape over a
        # repaired one. The declaration only seeds the first adoption.
        definition = workflow
        try:
            persisted = self.workflows.get(workflow_id)
            if persisted.steps:
                definition = persisted
        except KeyError:
            pass
        # DECIDE never writes: the chosen definition travels with the
        # Decision and becomes durable when the run reaches ACT (the
        # engine persists it exactly like the Intervention) or when
        # `goal decide` adopts it for the owner.
        return Decision("execute_workflow", definition.name, workflow_id, context={
            "workflow": {"id": workflow_id, "name": definition.name,
                         "steps": [asdict(step) for step in definition.steps],
                         "department_id": definition.department_id,
                         "version": definition.version}})

    def _candidates(self, goal) -> tuple[Workflow, ...]:
        """Candidate workflows: the owning Department's for a Department
        goal, every metric-declaring Department's for a Director goal."""
        handler = self.departments.get(goal.owner_id)
        if handler is not None:
            return tuple(handler.workflows)
        candidates = []
        for manifest in self.departments.values():
            if goal.metric in _declared_metrics(manifest):
                candidates.extend(manifest.workflows)
        return tuple(candidates)

    # -----------------------------------------------------------------
    # Repetition crystallization (issue #3): repeated bounded direct work
    # becomes reusable Workflow structure with provenance. The shape is
    # the EXECUTION pattern — same agent, same evidence kind, same
    # payload-key signature — so materially equivalent work groups even
    # when the instructions differ. Three completed orders is enough
    # evidence that the pattern is genuinely reusable; fewer are not.
    # -----------------------------------------------------------------

    REPETITION_THRESHOLD = 3

    def _repetition_shapes(self, goal_id: str) -> dict[tuple, list[dict]]:
        """Completed direct orders of one goal grouped by execution
        shape. Deterministic SQL underneath; no clustering, no
        embeddings."""
        shapes: dict[tuple, list[dict]] = {}
        with self.database.connect() as connection:
            rows = connection.execute("""SELECT work.id, work.agent_id,
                work.brief_json, evidence.kind, evidence.payload_json
                FROM core_work_orders work
                JOIN core_evidence evidence ON evidence.work_order_id=work.id
                WHERE work.goal_id=? AND work.step_id='direct'
                  AND work.status='completed'""",
                (goal_id,)).fetchall()
        for row in rows:
            evidence_kind = row[3]
            try:
                brief = json.loads(row[2]) or {}
                instruction = brief.get("instruction") or ""
                payload = json.loads(row[4])
                signature = tuple(sorted(payload)) if isinstance(payload, dict) else ()
            except (json.JSONDecodeError, TypeError):
                instruction, signature = "", ()
            shape = (row[1], evidence_kind, signature)
            entry = {"order_id": row[0], "agent_id": row[1],
                     "evidence_kind": evidence_kind,
                     "instruction": instruction}
            shapes.setdefault(shape, []).append(entry)
        return shapes

    def _crystallized_workflow(self, context, observation: dict) -> Decision | None:
        """DECIDE over learned structure: return the Decision that reuses
        a crystallized Workflow for this goal's repeated work shape, or
        None when no shape has enough evidence yet.

        The fourth equivalent request does not rebuild the procedure:
        DECIDE declares the Workflow (adoption and provenance happen at
        ACT, exactly like any other declared workflow), and every later
        order's brief carries the workflow's operational learning.
        """
        goal = context.goal
        shapes = self._repetition_shapes(goal.id)
        for shape, entries in shapes.items():
            if len(entries) < self.REPETITION_THRESHOLD:
                continue
            agent_id, evidence_kind, _ = shape
            workflow_id = f"learned:{goal.id}:{evidence_kind or 'work'}"
            latest = entries[-1]["instruction"] or (
                f"produce {evidence_kind or 'the goal'} evidence for "
                f"'{goal.name}'")
            instruction = (f"{latest} (reusing the learned procedure formed "
                           f"from {len(entries)} equivalent executions)")
            step = {"id": "execute", "agent_id": agent_id,
                    "instruction": instruction,
                    "evidence_kind": evidence_kind,
                    "evidence_kinds": [evidence_kind] if evidence_kind else [],
                    "skill_ids": [], "connection_ids": [],
                    "requirements": {}, "approval_keys": []}
            return Decision(
                "execute_workflow",
                f"reuse the learned workflow for {goal.name}", workflow_id,
                context={"workflow": {
                    "id": workflow_id,
                    "name": f"Learned {evidence_kind or 'direct'} procedure",
                    "steps": [step], "department_id": None, "version": 1},
                    "crystallized": {
                        "from_orders": [item["order_id"] for item in entries],
                        "shape": {"agent_id": agent_id,
                                  "evidence_kind": evidence_kind},
                        "reason": (f"{len(entries)} materially equivalent "
                                   "direct executions formed this reusable "
                                   "workflow")}})
        return None

    def decision_request(self, context, observation: dict) -> dict:
        """The owner-facing ask a parked DECIDE renders: goal state, the
        evidence behind it, candidate Departments/workflows/agents, and the
        exact answer syntax. Pure reads — deterministic candidate filtering
        only, never a choice."""
        goal = context.goal
        with self.database.connect() as connection:
            children = [{"id": row["id"], "name": row["name"],
                         "status": row["status"]}
                        for row in connection.execute(
                            """SELECT g.id,g.name,g.status FROM core_goals g
                               WHERE g.parent_id=? ORDER BY g.created_at""",
                            (goal.id,))]
            blockers = [{"id": row["id"], "name": row["name"],
                         "status": row["status"]}
                        for row in connection.execute(
                            """SELECT g.id,g.name,g.status FROM core_goal_edges e
                               JOIN core_goals g ON g.id=e.source_goal_id
                               WHERE e.target_goal_id=? AND e.relation='blocks'
                                 AND g.status!='complete'""",
                            (goal.id,))]
            recent_runs = []
            for row in connection.execute(
                    """SELECT sequence,stage,status,evaluation_json FROM core_runs
                       WHERE goal_id=? ORDER BY sequence DESC LIMIT 5""",
                    (goal.id,)):
                summary = None
                if row["evaluation_json"]:
                    summary = (json.loads(row["evaluation_json"]) or {}).get("summary")
                recent_runs.append({"sequence": row["sequence"],
                                    "stage": row["stage"], "status": row["status"],
                                    "evaluation": summary})
        departments_out = []
        for department_id in sorted(self.departments):
            manifest = self.departments[department_id]
            if goal.metric in _declared_metrics(manifest):
                departments_out.append({
                    "id": manifest.id,
                    "name": manifest.description or manifest.id,
                    "workflows": [item.id for item in manifest.workflows]})
        agents_out = sorted(
            agent_id for agent_id in self.agents
            if agent_id and agent_id != goal.owner_id)
        value = observation.get(goal.metric, 0)
        instruction = "bounded direct work with a concrete instruction"
        # Owner voice (goal-director-voice): the message speaks owner
        # language — the goal by name and human progress, options as
        # named choices. Metric keys, operator/target pairs, ids, and the
        # CLI answer syntax stay in the structured fields for the
        # Director, which records the owner's answer through the CLI.
        message = (f"'{goal.name}' needs its next bounded step — we stand "
                   f"at {human_progress(goal.metric, value, goal.target)}. "
                   "Run one of the candidate workflows, or assign bounded "
                   "direct work with a concrete instruction.")
        return {
            "goal": {"id": goal.id, "name": goal.name, "metric": goal.metric,
                     "operator": goal.operator, "target": goal.target,
                     "aggregation": goal.aggregation, "owner_id": goal.owner_id},
            "metric": goal.metric,
            "observation_value": value,
            "progress": human_progress(goal.metric, value, goal.target),
            "evidence": [{"kind": item.kind, "payload_keys": sorted(item.payload),
                          "outcome": human_outcome(item.kind, item.payload)}
                          for item in context.evidence[-5:]],
            "memory": [item.claim for item in context.memory[:6]],
            "children": children,
            "blockers": blockers,
            "recent_runs": recent_runs,
            "candidates": {"departments": departments_out, "agents": agents_out},
            "valid_answers": ["execute_workflow", "request_agent"],
            "answer_syntax": {
                "execute_workflow": (
                    "company goal decide <goal_id> --kind execute_workflow "
                    "--workflow <department_id>:<workflow_id>"),
                "request_agent": (
                    f"company goal decide <goal_id> --kind request_agent "
                    f"--agent <agent_id> --instruction \"{instruction}\" "
                    "--evidence-kind <kind>")},
            "message": message,
        }

    def evaluate(self, context, decision: Decision, evidence: tuple) -> Evaluation:
        """The intelligent EVALUATE boundary (issue #2): determine whether
        anything strategically reusable was ACTUALLY learned, and persist
        exactly one evidence-backed strategy lesson when it was.

        Distillation rules (deterministic over the goal's own history —
        never fabricated, never written merely because a run completed):
        - This run executed approach X; an earlier run executed a
          different approach Y with a known outcome: the better approach
          gets one ``prefer``/``avoid`` lesson naming both outcomes.
        - No other approach ran yet, and X completed real work without
          moving the metric: one ``avoid`` lesson (a different approach
          is required for this goal).
        - Otherwise: no lesson — ordinary execution teaches nothing.
        """
        goal = context.goal
        observation = self.observe(context)
        value = observation.get(goal.metric, 0)
        learning, workflow_id = self._strategy_learning(
            goal, decision, evidence, value,
            baseline=self._run_baseline(context))
        return Evaluation(
            compare(value, goal.operator, goal.target),
            {goal.metric: value}, "clean-core evidence evaluated",
            strategy_learning=learning,
            strategy_workflow_id=workflow_id,
            evidence_ids=tuple(item.id for item in evidence))

    def _strategy_learning(self, goal, decision, evidence, value,
                           baseline=None):
        """Distill at most one justified strategy lesson for this run.

        Returns ``(claim, workflow_id)`` — the claim text carries the
        machine-checkable ``prefer``/``avoid`` prefix and the workflow id
        links the lesson to the approach it judges; ``(None, None)`` when
        nothing was learned.
        """
        if not evidence or decision is None or decision.kind != "execute_workflow":
            return None, None  # direct work and evaluate-only runs teach no strategy
        approach = decision.workflow_id
        history = self._approach_history(goal)
        prior = self._prior_approach(history, approach, goal.metric)
        if prior is not None:
            other, other_value = prior
            if self._materially_better(value, other_value):
                return (f"prefer {approach} for '{goal.name}': it moved "
                        f"{goal.metric} to {json.dumps(value)} versus "
                        f"{other}'s {json.dumps(other_value)}; prefer this "
                        "approach for this goal"), approach
            if self._materially_better(other_value, value):
                return (f"avoid {approach} for '{goal.name}': {other} moved "
                        f"{goal.metric} to {json.dumps(other_value)} versus "
                        f"this approach's {json.dumps(value)}; prefer {other} "
                        "for this goal"), approach
            # No approach distinguished itself: both completed real work
            # and neither moved the metric further. The comparative rule
            # teaches nothing here, but the flat-outcome rule below may
            # still condemn this approach — falling through keeps an
            # every-candidate-exhausted goal able to park for the owner.
        # Flat-outcome rule (the first approach and every tie that
        # followed it): a lesson is justified only when this run
        # completed real work and the metric did not move at all from
        # where this run started (its own OBSERVE value, else the last
        # earlier evaluation).
        if baseline is not None and baseline != value:
            return None, None  # the metric moved; nothing learned yet
        if baseline is None and value not in (None, 0, False):
            return None, None
        return (f"avoid {approach} for '{goal.name}': it completed its work "
                f"and {goal.metric} stayed at {json.dumps(value)}; prefer a "
                "different approach for this goal"), approach

    def _approach_history(self, goal) -> list[dict]:
        """Every decided, evaluated run of this goal: (sequence, approach
        identity, metric outcome) newest first."""
        history = []
        with self.database.connect() as connection:
            rows = connection.execute("""SELECT sequence,decision_json,
                evaluation_json FROM core_runs
                WHERE goal_id=? AND decision_json IS NOT NULL
                  AND evaluation_json IS NOT NULL
                ORDER BY sequence DESC""", (goal.id,)).fetchall()
        for row in rows:
            try:
                decision = json.loads(row[1])
                evaluation = json.loads(row[2])
            except (TypeError, json.JSONDecodeError):
                continue
            approach = decision.get("workflow_id")
            if approach is None:
                continue  # direct/evaluate runs are not approach comparisons
            history.append({
                "sequence": row[0], "approach": approach,
                "value": (evaluation.get("metrics") or {}).get(goal.metric)})
        return history

    def _prior_approach(self, history, approach, metric):
        """The most recent earlier run that executed a DIFFERENT
        approach with a known outcome, as ``(approach, value)``."""
        for item in history:
            if item["approach"] != approach and item["value"] is not None:
                return item["approach"], item["value"]
        return None

    def _run_baseline(self, context):
        """The metric value this run started from: its own OBSERVE value,
        else the most recent earlier evaluation. The honest zero-movement
        baseline — never a fabricated expectation."""
        goal = context.goal
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT observation_json FROM core_runs WHERE id=?",
                (context.run_id,)).fetchone()
        if row and row[0]:
            try:
                observed = json.loads(row[0]).get(goal.metric)
                if observed is not None:
                    return observed
            except (TypeError, json.JSONDecodeError):
                pass
        for item in self._approach_history(goal):
            if item["value"] is not None:
                return item["value"]
        return None

    @staticmethod
    def _materially_better(candidate, other) -> bool:
        """One outcome is strictly better than another on a shared
        numeric scale. Non-numeric comparisons never qualify."""
        if (isinstance(candidate, (int, float)) and not isinstance(candidate, bool)
                and isinstance(other, (int, float)) and not isinstance(other, bool)):
            return candidate > other
        return False


class AssignmentExecutor:
    """Park work for an external Host; never execute a capability implicitly."""

    def __init__(self, memory=None):
        # F7(b): minimal read access only — when a memory repository is
        # attached, the parked ask's message carries the claims the
        # assigned work should build on. Pure reads; the executor stays
        # a pure parker and never writes.
        self.memory = memory

    def _memory_line(self, order) -> str | None:
        """The learning line for one parked ask: the workflow's own
        workflow-scope claims for a workflow order, goal-relevant claims
        (never owner profile claims) for a direct one. ``None`` when no
        claims exist or no memory handle is attached."""
        if self.memory is None:
            return None
        workflow_id = None
        if order.workflow_run_id:
            with self.memory.database.connect() as connection:
                row = connection.execute(
                    "SELECT workflow_id FROM core_workflow_runs WHERE id=?",
                    (order.workflow_run_id,)).fetchone()
            workflow_id = row[0] if row else None
        if workflow_id:
            claims = [item.claim for item in self.memory.relevant(
                scope="workflow", workflow_id=workflow_id)]
            label = "Workflow learning"
        else:
            claims = [item.claim for item in self.memory.relevant(
                goal_id=order.goal_id) if item.scope != "owner"]
            label = "Relevant memory"
        if not claims:
            return None
        return f"{label}: " + "; ".join(claims)

    def execute(self, agent, order) -> AgentResult:
        """Park one WorkOrder for the HOST to dispatch (issue #5).

        The assigned Agent — a host-side persona — executes this work;
        the owner is not the addressee and is never interrupted. The
        learning guidance (issue #9) is conditional by design: complete
        with Evidence, and include ``--learning`` if and only if this
        execution produced something genuinely reusable.
        """
        kinds = tuple(order.brief.get("evidence_kinds") or ())
        if not kinds and order.brief.get("evidence_kind"):
            kinds = (order.brief["evidence_kind"],)
        kinds = kinds or ("intervention_result",)
        message = (
            f"Working on: {order.brief.get('instruction')}. "
            f"Produce evidence kind(s) {', '.join(kinds)} and complete "
            f"with `tasks {order.id} --complete {agent.id} "
            "--evidence '[...]'` — add `--learning '<claim>'` only when "
            "this execution taught something genuinely reusable. On "
            "completion the run advances; external actions still park "
            "for approval first.")
        line = self._memory_line(order)
        if line is not None:
            message = f"{message}\n{line}"
        return AgentResult("host_work", message=message)


class CleanCommandRuntime:
    """CLI projection backed exclusively by clean-core records."""

    # Read-only scratch snapshots are cached per (path, mtime, size) at
    # process scope: every model request used to copy the whole database.
    _SNAPSHOT_CACHE: dict[tuple[str, float, int], Path] = {}
    _SNAPSHOT_ROOT: Path | None = None

    def __init__(self, path: str | Path, *, readonly: bool = False,
                 controller=None, executor=None):
        self.path = Path(path)
        self.readonly = readonly
        self._readonly_scratch = None
        database_path = self.path
        database_readonly = readonly
        if readonly:
            # Read commands use a scratch snapshot so the requested
            # database stays byte-for-byte read-only. A live database is
            # copied verbatim and validated against the CURRENT schema:
            # an unsupported schema fails clearly here instead of being
            # silently patched up into a fresh empty one. A database
            # that does not exist yet (a fresh home) snapshots empty and
            # initializes normally. Snapshots are cached per (path,
            # mtime, size) so repeated host requests over an unchanged
            # database cost one copy, not one per request.
            self._readonly_scratch = self._cached_snapshot(self.path)
            database_path = self._readonly_scratch / "empty.sqlite"
            database_readonly = False
            if self.path.exists():
                self._verify_schema(database_path)
        self.database = Database(database_path, readonly=database_readonly)
        agents = available_agents(self._home_from_database())
        # F6: the GoalController/AgentExecutor seams are injectable; the
        # defaults are exactly today's clean adapter.
        controller = controller or CatalogController(self.database, agents=agents)
        # F7(b): the default executor reads the memory claims a parked
        # ask should carry (a second read-only repository handle over
        # the same database; the executor never writes).
        executor = executor or AssignmentExecutor(
            memory=MemoryRepository(self.database,
                                    EvidenceRepository(self.database)))
        self.runtime = GoalRuntime(
            database_path, controller, executor, agents=agents,
            readonly=database_readonly)
        # F4: workflow steps execute only for agents their Department
        # declares, an installed Agent, or the goal owner. The loaded
        # Department manifests are the declaration source for the first
        # two; direct assignments are validated against the installed
        # layer and the goal owner alone. An injected controller without
        # declarations contributes no department agents.
        self.runtime.resolution.department_agents = {
            manifest.id: tuple(manifest.agent_ids or ())
            for manifest in (getattr(controller, "departments", None) or {}).values()}
        self.goals = self.runtime.goals
        self.runs = self.runtime.runs
        self.interventions = self.runtime.interventions
        self.evidence = self.runtime.evidence
        self.memory = self.runtime.memory
        self.work_orders_repository = WorkOrderRepository(self.database)
        self.workflows_repository = WorkflowRepository(self.database)
        self.approvals = ApprovalRepository(self.database)

    @classmethod
    def _cached_snapshot(cls, live: Path) -> Path:
        """One scratch copy of the live database per (path, mtime, size).

        Falls back to an empty snapshot when the database does not exist
        yet (fresh homes). Stale entries are evicted so a changed database
        is re-copied; each runtime still reads its snapshot read-write via
        SQLite while the live file is never opened for writing.
        """
        stamp = ((live.stat().st_mtime, live.stat().st_size)
                 if live.exists() else (0.0, 0))
        key = (str(live), *stamp)
        cached = cls._SNAPSHOT_CACHE.get(key)
        if cached is not None and (cached / "empty.sqlite").is_file():
            return cached
        if cls._SNAPSHOT_ROOT is None:
            cls._SNAPSHOT_ROOT = Path(tempfile.mkdtemp(prefix="spielos-readonly-"))
            import atexit
            atexit.register(shutil.rmtree, cls._SNAPSHOT_ROOT, True)
        # Evict stale snapshots of the same database (bounded cache).
        for other in [k for k in cls._SNAPSHOT_CACHE
                      if k[0] == key[0] and k != key]:
            cls._SNAPSHOT_CACHE.pop(other, None)
        scratch = cls._SNAPSHOT_ROOT / f"snapshot-{abs(hash(key))}"
        scratch.mkdir(parents=True, exist_ok=True)
        destination_path = scratch / "empty.sqlite"
        if live.exists():
            source = sqlite3.connect(f"file:{live}?mode=ro", uri=True)
            destination = sqlite3.connect(destination_path)
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()
        else:
            destination_path.touch()
        cls._SNAPSHOT_CACHE[key] = scratch
        return scratch

    #: The core tables a current-schema company database must carry.
    REQUIRED_TABLES = frozenset({
        "core_goals", "core_goal_metadata", "core_goal_edges", "core_runs",
        "core_interventions", "core_workflows", "core_workflow_runs",
        "core_work_orders", "core_evidence", "core_memory", "core_approvals",
        "core_notifications"})

    @classmethod
    def _verify_schema(cls, snapshot_database: Path) -> None:
        """Fail clearly on an unsupported schema (issue #15).

        This is a fresh clean architecture: no migrations, no legacy
        aliases, no old-schema compatibility branches. A copied
        database that lacks the current core tables is not a SpielOS
        company database and never gets silently re-initialized into
        looking like one.
        """
        with sqlite3.connect(snapshot_database) as connection:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        missing = sorted(cls.REQUIRED_TABLES - tables)
        if missing:
            raise ValueError(
                f"database is not a current SpielOS company database: "
                f"missing tables {', '.join(missing)}. This runtime "
                "supports no older schema — re-create the home with "
                "`spielos init` or point --db at a current database.")

    def _home_from_database(self):
        """The home implied by the canonical database layout, when present.

        The state database lives at ``<home>/.spielos/state/company.sqlite``
        (or ``<state>/company.sqlite`` in tests), so its third parent names
        the home whose ``agents/installed`` layer this runtime executes for.
        Any other layout falls back to normal home discovery inside the
        loader.
        """
        parents = Path(self.path).absolute().parents
        return parents[2] if len(parents) >= 3 else None

    @staticmethod
    def _operator(value: str) -> str:
        return {"ge": "ge", ">=": "ge", "gt": "gt", ">": "gt",
                "eq": "eq", "==": "eq", "le": "le", "<=": "le",
                "lt": "lt", "<": "lt"}.get(value, value)

    @staticmethod
    def _validate_goal_metric(owner_id: str, metric: str) -> None:
        """Refuse goals a Department can never prove (D3).

        A goal whose owner is a declared Department must use a metric that
        department declares (in ``evidence_metrics`` or
        ``goal_schema["metrics"]``). Otherwise the goal would accept any
        evidence — or worse, none could ever satisfy it. Director-owned
        goals keep the departmentless DECIDE boundary: they park a
        decision_request for the owner instead.
        """
        handler = departments().get(owner_id)
        if handler is None:
            return
        declared = _declared_metrics(handler)
        if metric in declared:
            return
        listed = ", ".join(sorted(declared)) or "(none declared)"
        raise ValueError(
            f"Department {owner_id!r} does not declare metric {metric!r}; "
            f"declared metrics: {listed}")

    def _require_writable(self) -> None:
        if self.readonly:
            raise PermissionError("clean-core runtime was opened read-only")

    def connect(self):
        return self.database.connect()

    def create_goal(self, **values) -> dict:
        self._require_writable()
        self._validate_goal_metric(values.get("owner_id") or "goal-runtime",
                                   values["metric"])
        goal = self.runtime.create_goal(
            values["name"], values["metric"], self._operator(values["operator"]),
            values["target"], parent_id=values.get("parent_id"),
            goal_id=values.get("goal_id"), owner_id=values.get("owner_id") or "goal-runtime",
            deadline=values.get("deadline"), config=values.get("config") or {})
        for target in (values.get("config") or {}).get("supports_goal_ids") or ():
            self.goals.add_support(goal.id, target)
        for target in (values.get("config") or {}).get("blocks_goal_ids") or ():
            self.goals.add_block(goal.id, target)
        return self._goal(goal)

    def _goal(self, goal) -> dict:
        run = self.runs.current(goal.id)
        return {"id": goal.id, "name": goal.name, "owner_id": goal.owner_id,
                "metric": goal.metric, "operator": goal.operator, "target": goal.target,
                "aggregation": goal.aggregation,
                "deadline": goal.deadline, "parent_id": goal.parent_id,
                "goal_status": "achieved" if goal.status == "complete" else goal.status,
                "config": goal.config or {}, "run_id": run.id, "run_type": "execution",
                "run_status": run.status, "stage": run.stage.value,
                "step": run.stage.value.lower(), "evidence_count": len(self.evidence.for_run(run.id)),
                "evidence_validity": "technical_only", "runtime_updated_at": "",
                "why_next": "Continue the persisted clean-core Run."}

    def status(self, goal_id: str) -> dict:
        goal = self.goals.get(goal_id); run = self.runs.current(goal_id)
        evidence = [asdict(item) for item in self.evidence.for_run(run.id)]
        goal_row = self._goal(goal)
        run_row = {"id": run.id, "sequence": run.sequence, "run_type": "execution",
                   "status": run.status, "evidence_validity": "technical_only"}
        attention = self.attention(goal_id=goal_id)
        return {"goal": goal_row, "run": run_row,
                "evidence": evidence, "evaluation": None if run.evaluation is None
                else asdict(run.evaluation),
                "pending_notifications": self.notifications(goal_id=goal_id),
                "attention": attention,
                "work_orders": self.work_orders(status="active", goal_id=goal_id)}

    def goal_summaries(self, *, statuses=None, goal_id=None, limit=100):
        values = [self._goal(item) for item in self.goals.list()
                  if goal_id is None or item.id == goal_id]
        if statuses:
            values = [item for item in values if item["goal_status"] in statuses]
        return values[:limit]

    def company_snapshot(self, recent_limit=5):
        values = self.goal_summaries(limit=100)
        active = [item for item in values if item["goal_status"] == "active"]
        paused = [item for item in values if item["goal_status"] == "paused"]
        terminal = [item for item in values
                    if item["goal_status"] in {"achieved", "abandoned"}]
        counts = {key: sum(item["goal_status"] == key for item in values)
                  for key in ("active", "achieved", "abandoned", "expired")}
        counts["total"] = len(values)
        with self.database.connect() as connection:
            goal_links = [dict(row) for row in connection.execute(
                "SELECT source_goal_id,target_goal_id,relation FROM core_goal_edges")]
        return {"counts": counts, "focus_goal": active[0] if active else None,
                "attention": self.attention(),
                "work_orders": self.work_orders(status="active", limit=20),
                "active_goals": active, "paused_goals": paused,
                "unread_results": self.unread_results(),
                "recent_memory": self.memories(limit=20),
                "goal_links": goal_links,
                "support_links": [item for item in goal_links
                                  if item["relation"] == "supports"],
                "block_links": [item for item in goal_links
                                if item["relation"] == "blocks"],
                "recent_results": terminal[:recent_limit]}

    def topology_audit(self):
        goals = self.goals.list()
        by_id = {item.id: item for item in goals}
        roots = sorted(item.id for item in goals if not item.parent_id)
        defects = []
        for goal in goals:
            if goal.parent_id and goal.parent_id not in by_id:
                defects.append({"goal_id": goal.id, "kind": "missing_parent",
                                "parent_id": goal.parent_id})
            seen, current = {goal.id}, goal
            while current.parent_id and current.parent_id in by_id:
                if current.parent_id in seen:
                    defects.append({"goal_id": goal.id, "kind": "parent_cycle"})
                    break
                seen.add(current.parent_id)
                current = by_id[current.parent_id]
        with self.database.connect() as connection:
            edges = [dict(row) for row in connection.execute(
                "SELECT source_goal_id,target_goal_id,relation FROM core_goal_edges")]
        for relation in ("supports", "blocks"):
            graph = {goal_id: set() for goal_id in by_id}
            for edge in (item for item in edges if item["relation"] == relation):
                source, target = edge["source_goal_id"], edge["target_goal_id"]
                if source not in by_id or target not in by_id:
                    defects.append({"goal_id": target, "kind": f"missing_{relation}_goal",
                                    "source_goal_id": source,
                                    "target_goal_id": target})
                    continue
                graph[source].add(target)
                if (relation == "blocks" and by_id[source].status == "abandoned"
                        and by_id[target].status == "active"):
                    defects.append({
                        "goal_id": target,
                        "kind": "permanently_blocked_by_abandoned_goal",
                        "blocker_goal_id": source})
            visiting, visited = set(), set()
            def visit(goal_id):
                if goal_id in visiting:
                    defects.append({"goal_id": goal_id,
                                    "kind": f"{relation[:-1]}_cycle"})
                    return
                if goal_id in visited:
                    return
                visiting.add(goal_id)
                for target in graph[goal_id]:
                    visit(target)
                visiting.remove(goal_id)
                visited.add(goal_id)
            for goal_id in sorted(graph):
                visit(goal_id)
        # F9(b): several independent root Goals are a healthy home, not
        # a defect — the root ids stay reported (with a canonical root
        # only when exactly one exists); genuine structural defects
        # (parent cycles, missing parents, missing edge goals,
        # abandoned blockers) keep flagging above.
        return {"goal_count": len(goals), "root_goal_ids": roots,
                "canonical_root_goal_id": roots[0] if len(roots) == 1 else None,
                "defects": defects}

    def approve(self, goal_id, note="", keys=(), scope="step"):
        """Grant approval keys and resume a waiting run.

        ``scope="step"`` (default) binds each granted key to the current
        intervention; the next run re-parks for its own approval. 
        ``scope="run"`` grants run-wide keys (intervention_id NULL): the
        repository's run-key fallback then satisfies every later
        intervention of the SAME run, so one approval carries a multi-step
        run through all of its remaining gates.

        F9(a): approving answers the current intervention's pending
        owner ask — its notification is acknowledged BEFORE the run
        resumes, so an approved ask stops re-delivering while the next
        gate's own ask (parked by the resumed run) stays pending. A
        ``--scope step`` approval without an active intervention is a
        clear error: a DECIDE park is answered with ``goal decide`` and
        a stalled or review-parked run with ``goal resume``, not
        approve; ``--scope run`` stays coherent for run-wide pre-grants.
        """
        self._require_writable()
        if scope not in {"step", "run"}:
            raise ValueError("approve scope must be 'step' or 'run'")
        run = self.runs.current(goal_id)
        intervention = self.interventions.active_for_run(run.id)
        if scope == "step" and intervention is None:
            raise ValueError(
                f"goal {goal_id} has no active intervention to approve for "
                f"--scope step: run {run.sequence} is "
                f"{run.stage.value}/{run.status}. Answer a DECIDE park with "
                f"`company goal decide {goal_id} --kind ...`, resume a "
                f"stalled or review-parked run with `company goal resume "
                f"{goal_id}`, or grant run-wide keys with --scope run.")
        granted = set(keys)
        if intervention is not None:
            workflow_run = self.runtime.resolution.workflows.active_for_intervention(
                intervention.id)
            if workflow_run is not None:
                if workflow_run.current_step < len(workflow_run.steps):
                    required = workflow_run.steps[workflow_run.current_step].approval_keys
                    if required and not granted:
                        granted.update(required)
        if not granted:
            granted.add("execute")
        for key in granted:
            self.approvals.grant(
                goal_id=goal_id, run_id=run.id, key=key,
                intervention_id=None if (scope == "run" or intervention is None)
                else intervention.id,
                note=note)
        if intervention is not None:
            # The approved ask is answered: retire its pending
            # notification before the resume so only a NEW gate's ask
            # (parked by the resumed run) can re-deliver as pending.
            with self.database.connect() as connection:
                connection.execute("""UPDATE core_notifications
                    SET status='acknowledged',acknowledged_at=?
                    WHERE intervention_id=? AND kind='owner_input_required'
                      AND status='pending'""",
                    (datetime.now(timezone.utc).isoformat(), intervention.id))
        if run.status == "waiting":
            self.runtime.resume(goal_id)
        return self.status(goal_id)

    def decide_goal(self, goal_id: str, kind: str, *, workflow: str | None = None,
                    agent: str | None = None, instruction: str | None = None,
                    evidence_kind: str | None = None):
        """Answer a decision_request park with one concrete bounded step.

        ``--kind execute_workflow`` adopts one of the candidate Departments'
        workflows (``<department_id>:<workflow_id>``); ``--kind request_agent``
        assigns bounded direct work — the instruction is mandatory, which is
        the anti-content-free rule. Answering resumes the run immediately,
        exactly like ``engine.resume``.
        """
        self._require_writable()
        goal = self.goals.get(goal_id)
        run = self.runs.current(goal_id)
        decision = run.decision
        if (run.stage != GoalStage.DECIDE or run.status != "waiting"
                or decision is None or decision.kind != "decision_request"):
            raise ValueError(
                f"goal decide answers a Run parked at DECIDE on a "
                f"decision_request; run {run.sequence} of {goal_id} is "
                f"{run.stage.value}/{run.status}"
                + (f" with a {decision.kind} decision" if decision else ""))
        request = dict((decision.context or {}).get("decision_request") or {})
        candidates = request.get("candidates") or {}
        if kind == "execute_workflow":
            offered = [f"{item['id']}:{workflow_id}"
                       for item in candidates.get("departments") or []
                       for workflow_id in item.get("workflows") or []]
            if workflow not in offered:
                raise ValueError(
                    f"workflow {workflow!r} is not one of the candidate "
                    "workflows for this decision_request: "
                    + (", ".join(offered) or "(none declared this metric)"))
            answer = self._adopt_department_workflow(goal_id, workflow)
        elif kind == "request_agent":
            agent_id = (agent or "").strip()
            allowed = sorted(set(candidates.get("agents") or ())
                             | set(self.runtime.resolution.agents or {}))
            if not agent_id:
                raise ValueError(
                    "request_agent requires --agent (the goal owner or an "
                    "installed Agent: " + (", ".join(allowed) or goal.owner_id) + ")")
            if agent_id != goal.owner_id and agent_id not in allowed:
                raise ValueError(
                    f"agent {agent_id!r} is neither the goal owner "
                    f"({goal.owner_id!r}) nor an installed Agent"
                    + (": " + ", ".join(allowed) if allowed else ""))
            text = (instruction or "").strip()
            if not text:
                raise ValueError(
                    "request_agent requires --instruction: one bounded, "
                    "concrete instruction the Agent can execute; the runtime "
                    "never parks content-free work")
            answer = Decision(
                "request_agent", text, None,
                {"agent_id": agent_id, "evidence_kind": evidence_kind or goal.metric,
                 "instruction": text})
        else:
            raise ValueError("decision kind must be execute_workflow or request_agent")
        self.runs.update(run.id, stage=GoalStage.ACT, status="ready", decision=answer)
        self._acknowledge_run_asks(run.id)
        self.runtime.advance(goal_id)
        return self.status(goal_id)

    def _adopt_department_workflow(self, goal_id: str, workflow_id: str) -> Decision:
        """Bind a candidate Department workflow to the goal's current run."""
        goal = self.goals.get(goal_id)
        department_id, _, declared_id = workflow_id.partition(":")
        handler = departments().get(department_id)
        if handler is None:
            raise ValueError(f"unknown candidate department: {department_id!r}")
        workflow = next((item for item in handler.workflows
                         if item.id == declared_id), None)
        if workflow is None or not isinstance(workflow, Workflow):
            raise ValueError(
                f"department {department_id!r} declares no workflow "
                f"{declared_id!r}")
        self.workflows_repository.save(Workflow(
            workflow_id, workflow.name, workflow.steps, department_id,
            workflow.version))
        return Decision("execute_workflow",
                        f"execute {workflow.name} ({department_id})", workflow_id)

    def _acknowledge_run_asks(self, run_id: str) -> None:
        """Retire the owner ask a parked run carried once it is answered."""
        with self.database.connect() as connection:
            connection.execute("""UPDATE core_notifications
                SET status='acknowledged',acknowledged_at=?
                WHERE run_id=? AND status='pending'""",
                (datetime.now(timezone.utc).isoformat(), run_id))

    def resume_goal(self, goal_id: str):
        """Open the next run of a parked goal (the stall/review 'continue').

        A DECIDE park is refused: it needs a concrete decision, not a
        resume — `goal decide` answers it.
        """
        self._require_writable()
        run = self.runs.current(goal_id)
        if run.status != "waiting":
            raise ValueError(
                f"goal {goal_id} is not parked: run {run.sequence} is "
                f"{run.stage.value}/{run.status}; nothing to resume")
        if (run.stage == GoalStage.DECIDE and run.decision is not None
                and run.decision.kind == "decision_request"):
            raise ValueError(
                f"goal {goal_id} is waiting for a concrete decision, not a "
                f"resume: answer the pending ask with `company goal decide "
                f"{goal_id} --kind execute_workflow --workflow <id>` or "
                f"`--kind request_agent --agent <id> --instruction "
                f"'<bounded instruction>' --evidence-kind <kind>`")
        self._acknowledge_run_asks(run.id)
        self.runtime.resume(goal_id)
        return self.status(goal_id)

    def add_evidence(self, goal_id, *, kind, source, payload, validity=None):
        self._require_writable()
        run = self.runs.current(goal_id)
        intervention = self.interventions.active_for_run(run.id)
        self.evidence.record(goal_id=goal_id, run_id=run.id, kind=kind,
                             intervention_id=None if intervention is None else intervention.id,
                             payload={**payload, "source": source,
                                      "validity": validity or "technical_only"})
        return self.status(goal_id)

    def once(self, goal_id, holder=None):
        self._require_writable()
        self.runtime.advance(goal_id)
        return self.status(goal_id)

    def tick(self, max_advances=100):
        self._require_writable()
        return self.runtime.tick(max_advances=max_advances)

    def watch(self, interval_seconds=5.0, goal_id=None, max_ticks=None):
        ticks = 0
        while max_ticks is None or ticks < max_ticks:
            result = (self.once(goal_id) if goal_id
                      else self.tick(max_advances=100))
            ticks += 1
            yield result
            if max_ticks is None or ticks < max_ticks:
                time.sleep(interval_seconds)

    def work_order(self, order_id):
        return self._order(self.work_orders_repository.get(order_id))

    def work_orders(self, status=None, goal_id=None, limit=100):
        with self.database.connect() as connection:
            clauses, args = [], []
            if goal_id:
                clauses.append("goal_id=?"); args.append(goal_id)
            if status in {"active", "open", "claimed"}:
                clauses.append("status IN ('open','claimed')" if status == "active" else "status=?")
                if status != "active": args.append(status)
            sql = "SELECT id FROM core_work_orders"
            if clauses: sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY created_at LIMIT ?"; args.append(limit)
            ids = [row[0] for row in connection.execute(sql, args)]
        return [self.work_order(item) for item in ids]

    def _order(self, order):
        return {**asdict(order), "needed": 1,
                "accepts_evidence": [order.brief.get("evidence_kind")]
                if order.brief.get("evidence_kind") else [], "why_next": "Agent assignment"}

    def claim_work_order(self, work_order_id, agent_id):
        self._require_writable()
        return self._order(self.work_orders_repository.claim(work_order_id, agent_id))

    def complete_work_order(self, work_order_id, agent_id, evidence,
                            learning=None):
        """Complete one order atomically; optionally persist learning.

        The documented flow is claim-then-complete: an order must already
        be claimed by exactly ``agent_id`` — the order's declared agent
        (the runtime pre-claims workflow-step orders with their declared
        agent, and direct orders carry the owner's own identity when the
        owner assigned the work to themself). An open order is refused:
        claiming it first is a separate, explicit step, and a foreign
        identity raises instead of silently taking the order over.
        ``learning`` (the ``tasks --complete --learning`` flag) persists
        workflow-scope memory grounded in the evidence just recorded, with
        full Goal/Run/Intervention/Workflow lineage enforced by
        MemoryRepository.remember — the same guard the engine path uses.
        """
        self._require_writable()
        order = self.work_orders_repository.get(work_order_id)
        if order.status == "open":
            raise RuntimeError(
                f"work order {work_order_id} is open: claim it with "
                f"`tasks {work_order_id} --claim {order.agent_id}` "
                f"(only its declared agent {order.agent_id!r}) before "
                "completing")
        if (order.claimed_by or "") != agent_id:
            raise RuntimeError(
                f"work order is claimed by {order.claimed_by!r}, not {agent_id!r}")
        if not evidence:
            raise ValueError("completing a clean-core WorkOrder requires Evidence")
        first = evidence[0]
        order, evidence_ids = self.work_orders_repository.complete_with_evidence(
            work_order_id, {"evidence": evidence}, executor_id=agent_id,
            kind=first["kind"], payload=first.get("payload") or {},
            evidence_items=[(item["kind"], item.get("payload") or {})
                            for item in evidence],
            advance_workflow=bool(order.workflow_run_id), wake_run=True)
        if learning:
            # L4: the one workflow-learning writer lives on the
            # ResolutionCycle; the CLI path and the executor path share it.
            self.runtime.resolution.remember_workflow_learning(
                order, learning, evidence_ids)
        return {"work_order": self._order(order)}

    def retire_memory(self, memory_id: str):
        """CLI memory hygiene path (`memory retire <id>`): flip one active
        claim out of the active set without deleting the row or its
        evidence. The single writer is MemoryRepository.retire."""
        self._require_writable()
        return self.memory.retire(memory_id)

    def add_memory(self, scope, claim, evidence_ids=(), goal_id=None,
                   run_id=None, intervention_id=None, workflow_id=None):
        """CLI memory write path (`memory add`) for workflow/strategy scope.

        The engine guards stay the single authority: scope validity,
        evidence presence, and Goal/Run/Intervention lineage are enforced
        by MemoryRepository.remember; strategy learning additionally
        requires evidence from the named run.
        """
        self._require_writable()
        if scope not in {"workflow", "strategy"}:
            raise ValueError("memory add supports workflow and strategy scope; "
                             "owner memory is written with profile set")
        return self.memory.remember(
            scope, claim, evidence_ids=tuple(evidence_ids), goal_id=goal_id,
            run_id=run_id, intervention_id=intervention_id,
            workflow_id=workflow_id)

    def memories(self, limit=100, **_kwargs):
        """Project memory rows with their causal evidence citation — the
        ids that explain WHY each claim persisted (memory is only ever
        written with evidence lineage; the projection keeps it visible)."""
        with self.database.connect() as connection:
            return [dict(row) for row in connection.execute("""SELECT id,scope,claim,
                goal_id,run_id,intervention_id,workflow_id,confidence,status,
                supersedes_id,created_at,evidence_ids_json FROM core_memory
                ORDER BY created_at DESC LIMIT ?""", (limit,))]

    @staticmethod
    def _profile(memory):
        key, separator, raw = memory.claim.partition(" = ")
        namespace, dot, claim_key = key.partition(".")
        try:
            value = json.loads(raw) if separator else memory.claim
        except json.JSONDecodeError:
            value = raw
        return {**asdict(memory), "namespace": namespace if dot else "owner",
                "claim_key": claim_key if dot else key, "value": value}

    def set_profile_claim(self, *, namespace, claim_key, value, **_kwargs):
        """Write one owner preference, constraint, or authority claim.

        Owner memory is company-global: it never carries goal or
        workflow scoping (those fields stay NULL), so retrieval cannot
        imply a scope the write never had.
        """
        self._require_writable()
        prefix = f"{namespace}.{claim_key} = "
        current = next((item for item in self.memory.relevant(
            scope="owner", limit=200) if item.claim.startswith(prefix)), None)
        memory = self.memory.remember(
            "owner", prefix + json.dumps(value, sort_keys=True),
            supersedes_id=current.id if current else None)
        return self._profile(memory)

    def owner_memory(self, *, limit=200, **_kwargs):
        return tuple(self._profile(item) for item in self.memory.relevant(
            scope="owner", limit=limit))

    def clean_memory_summary(self):
        records = self.memories(limit=200)
        active = [item for item in records if item["status"] == "active"]
        by_scope = {scope: [item for item in active if item["scope"] == scope]
                    for scope in ("owner", "workflow", "strategy")}
        return {"schema_version": 3, "durable_memory": by_scope,
                "counts": {scope: len(items) for scope, items in by_scope.items()}}

    def _focus_goal(self, owner_id=None):
        """F8(a): the projection's focus Goal.

        Ready runs come first, in exactly ``runs.ready()`` priority order
        (deadline/priority-aware — the same order the scheduler uses; no
        ad-hoc re-implementation). When nothing is ready the focus falls
        back to the most recently updated active Goal (the one the loop
        touched last), still honoring the owner filter.
        """
        candidates = {item.id: item for item in self.goals.list()
                      if item.status == "active"
                      and (not owner_id or item.owner_id == owner_id)}
        for run in self.runs.ready():
            if run.goal_id in candidates:
                return candidates[run.goal_id]
        if not candidates:
            return None
        ids = list(candidates)
        marks = ",".join("?" for _ in ids)
        with self.database.connect() as connection:
            row = connection.execute(
                f"""SELECT g.id FROM core_goals g
                    JOIN core_runs r ON r.goal_id=g.id AND r.sequence=(
                        SELECT MAX(r2.sequence) FROM core_runs r2
                        WHERE r2.goal_id=g.id)
                    WHERE g.id IN ({marks}) AND g.status='active'
                    ORDER BY r.updated_at DESC,g.created_at DESC,g.id LIMIT 1""",
                ids).fetchone()
        if row is not None:
            return candidates[row[0]]
        return candidates[ids[0]]

    def _recent_decisions(self, goal_id: str, limit: int = 3) -> list[dict]:
        """The last ``limit`` decided runs of one Goal (most recent
        first): sequence, decision kind, and resolution outcome — the
        same shape the engine's DECIDE context carries."""
        decisions = []
        with self.database.connect() as connection:
            for row in connection.execute(
                    """SELECT id,sequence,decision_json FROM core_runs
                       WHERE goal_id=? AND decision_json IS NOT NULL
                       ORDER BY sequence DESC LIMIT ?""", (goal_id, limit)):
                outcome = connection.execute(
                    """SELECT resolution_outcome FROM core_interventions
                       WHERE run_id=? ORDER BY created_at DESC,rowid DESC
                       LIMIT 1""", (row["id"],)).fetchone()
                decisions.append({
                    "sequence": row["sequence"],
                    "kind": json.loads(row["decision_json"]).get("kind"),
                    "resolution_outcome": outcome[0] if outcome else None})
        return decisions

    def _declaring_departments(self, metric: str) -> list[str]:
        """Departments whose declarations prove this metric (the D3
        rule, reusing the controller's loaded registry)."""
        registry = (getattr(self.runtime.controller, "departments", None)
                    or departments())
        return sorted(department_id for department_id, manifest
                      in registry.items() if metric in _declared_metrics(manifest))

    def assemble_context(self, *, prompt="", owner_id=None, workflow_id=None,
                         token_budget=None, **_kwargs):
        goal = self._focus_goal(owner_id)
        run = self.runs.current(goal.id) if goal else None
        evidence = self.evidence.for_goal(goal.id)[-20:] if goal else []
        memory = self.memory.relevant(
            goal_id=goal.id if goal else None, workflow_id=workflow_id, limit=20)
        lines = [f"Request: {prompt}" if prompt else "Current clean-core context"]
        machine: list[str] = []
        sources = []
        if goal and run:
            value = self._latest_metric(goal, run)
            lines.append(
                f"Goal: {goal.name} — "
                f"{human_progress(goal.metric, value, goal.target)}, "
                f"{human_loop_position(run.stage.value, run.status)} "
                f"(Run {run.sequence})")
            machine.append(
                f"goal={goal.id} run={run.id} ({run.stage.value}/{run.status}) "
                f"metric={goal.metric} {goal.operator} {json.dumps(goal.target)}")
            sources.append(f"goal:{goal.id}")
        # F8(b): the focus Goal's recent decisions and the Departments
        # declaring its metric — rendered only when each has content, in
        # owner words (the decision kinds and resolution outcomes are
        # enums; the Machine reference line carries them verbatim).
        decisions = self._recent_decisions(goal.id) if goal else []
        if decisions:
            lines.append("Recent decisions: " + "; ".join(
                f"run {item['sequence']} {human_decision(item['kind'])}"
                + (f", {human_resolution(item['resolution_outcome'])}"
                   if item['resolution_outcome'] else ", unresolved")
                for item in decisions))
            machine.append("recent decisions: " + "; ".join(
                f"run {item['sequence']} {item['kind']} "
                f"{item['resolution_outcome'] or 'unresolved'}"
                for item in decisions))
        declaring = self._declaring_departments(goal.metric) if goal else []
        if declaring:
            lines.append("Departments that can move this goal: "
                         + ", ".join(declaring))
            machine.append(f"departments declaring {goal.metric}: "
                           + ", ".join(declaring))
        # The whole active goal tree, not only the owner filter's slice:
        # parents first, children indented beneath them.
        tree_lines, tree_ids, tree_machine = self._goal_tree()
        if tree_lines:
            lines.append("Goals:")
            lines.extend(tree_lines)
            machine.extend(tree_machine)
            sources.extend(tree_ids)
        blocked = self._blocked_by(goal.id) if goal else []
        if blocked:
            lines.append("Blocked by: " + "; ".join(
                f"{item['name']} — {human_goal_status(item['status'])}"
                for item in blocked))
            machine.append("blocked by: " + "; ".join(
                f"{item['id']} ({item['status']})" for item in blocked))
            sources.extend(f"goal:{item['id']}" for item in blocked)
        if evidence:
            lines.append("Evidence: " + "; ".join(
                human_outcome(item.kind, item.payload) for item in evidence))
            machine.append("evidence: " + "; ".join(
                f"{item.id}={item.kind} "
                f"{json.dumps(item.payload, sort_keys=True)}"
                for item in evidence))
            sources.extend(item.id for item in evidence)
        # F6 relevance: ordinary reasoning context carries ONLY what
        # applies to this Goal/Workflow — owner claims render once on
        # the Profile line, and the Relevant memory line carries the
        # goal/workflow claims that apply. `company memory summary`
        # intentionally shows every company claim; reasoning never does.
        relevant_claims = [item for item in memory if item.scope != "owner"]
        if relevant_claims:
            lines.append("Relevant memory: " + "; ".join(
                item.claim for item in relevant_claims))
            machine.append("memory: " + "; ".join(
                item.id for item in relevant_claims))
            sources.extend(item.id for item in relevant_claims)
        attention = self.attention(limit=5)
        if attention:
            lines.append("Attention: " + "; ".join(
                f"{human_attention(item.get('kind'))}: "
                f"{item.get('message') or 'owner input required'}"
                for item in attention))
            machine.append("attention: " + "; ".join(
                f"{item.get('id')} ({item.get('kind')})" for item in attention))
            sources.extend(str(item.get("id")) for item in attention)
        profile = self.owner_memory(limit=8)
        if profile:
            lines.append("Profile: " + "; ".join(
                f"{item['namespace']}.{item['claim_key']}="
                f"{json.dumps(item['value'], sort_keys=True)}"
                for item in profile))
        if goal:
            orders = self.work_orders(status="active", goal_id=goal.id,
                                      limit=10)
            if orders:
                machine.append("work orders: " + "; ".join(
                    f"{item['id']} (step {item['step_id']}, agent "
                    f"{item['agent_id']})" for item in orders))
        home = self.path.parents[2] if len(self.path.parents) >= 3 else None
        if home is not None:
            lines.append("Layout: " + layout_summary(home))
        if machine:
            # Owner voice (goal-director-voice): the narration above is
            # what the owner reads; every id, metric key, payload, and
            # enum the Director may need rides this one compact line at
            # the end, quoted only when the owner asks for technical
            # detail.
            lines.append("Machine reference: " + " · ".join(machine))
        rendered = "\n".join(lines)
        if token_budget:
            rendered = rendered[:max(1, int(token_budget)) * 4]
        return {"context": rendered, "sources": sources,
                "goal_id": goal.id if goal else None,
                "run_id": run.id if run else None,
                "workflow_id": workflow_id}

    def _latest_metric(self, goal, run):
        """The most recent value of the goal's metric: this run's own
        observation when it has one, else the last evaluated run's
        outcome, else zero. A pure read for the owner-voice progress
        line."""
        if run is not None and run.observation:
            value = run.observation.get(goal.metric)
            if value is not None:
                return value
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT evaluation_json FROM core_runs
                   WHERE goal_id=? AND evaluation_json IS NOT NULL
                   ORDER BY sequence DESC LIMIT 1""", (goal.id,)).fetchone()
        if row is not None and row[0]:
            value = ((json.loads(row[0]) or {}).get("metrics") or {}).get(
                goal.metric)
            if value is not None:
                return value
        return 0

    def _goal_tree(self, limit: int = 12):
        """Render every active Goal as a parent-first tree with children
        indented, in owner words: name, status in plain words, and where
        its run stands — never goal ids or metric keys. Returns (lines,
        goal ids, machine entries); a Goal without a run renders without
        its run segment."""
        active = [item for item in self.goals.list() if item.status == "active"]
        by_parent: dict[str | None, list] = {}
        for item in active:
            by_parent.setdefault(item.parent_id, []).append(item)
        lines: list[str] = []
        ids: list[str] = []
        machine: list[str] = []
        roots = by_parent.get(None, []) + [
            item for item in active
            if item.parent_id is not None and item.parent_id not in
            {other.id for other in active}]
        def render(goal, depth: int) -> None:
            if len(ids) >= limit:
                return
            try:
                run = self.runs.current(goal.id)
                segment = (f", {human_loop_position(run.stage.value, run.status)}"
                           f" (Run {run.sequence})")
                machine_segment = (f", run {run.sequence} "
                                   f"({run.stage.value}/{run.status})")
            except KeyError:
                segment, machine_segment = "", ""
            lines.append(f"{'  ' * depth}{goal.name} — "
                         f"{human_goal_status(goal.status)}{segment}")
            machine.append(f"goal={goal.id} ({goal.name}, {goal.status}, "
                           f"metric {goal.metric}{machine_segment})")
            ids.append(f"goal:{goal.id}")
            for child in by_parent.get(goal.id, []):
                render(child, depth + 1)
        for root in roots:
            render(root, 0)
            if len(ids) >= limit:
                break
        return lines, ids, machine

    def _blocked_by(self, goal_id: str) -> list[dict]:
        with self.database.connect() as connection:
            return [dict(row) for row in connection.execute(
                """SELECT g.id,g.name,g.status FROM core_goal_edges e
                   JOIN core_goals g ON g.id=e.source_goal_id
                   WHERE e.target_goal_id=? AND e.relation='blocks'
                     AND g.status!='complete'""", (goal_id,))]

    def notifications(self, status="pending", limit=100, goal_id=None, **_kwargs):
        clauses, args = ["status=?"], [status]
        if goal_id:
            clauses.append("goal_id=?"); args.append(goal_id)
        args.append(limit)
        with self.database.connect() as connection:
            rows = connection.execute("""SELECT * FROM core_notifications WHERE """
                + " AND ".join(clauses) + " ORDER BY created_at LIMIT ?", args)
            return [{**dict(row), "payload": json.loads(row["payload_json"])}
                    for row in rows]

    def acknowledge_notification(self, notification_id):
        self._require_writable()
        with self.database.connect() as connection:
            updated = connection.execute("""UPDATE core_notifications
                SET status='acknowledged', acknowledged_at=?
                WHERE id=? AND status='pending'""",
                (datetime.now(timezone.utc).isoformat(), notification_id))
            if not updated.rowcount:
                raise ValueError(f"unknown pending notification: {notification_id}")
        return {"id": notification_id, "status": "acknowledged"}

    def attention(self, limit=100, goal_id=None, **_kwargs):
        return [{"id": item["id"], "kind": item["kind"],
                 **item["payload"]} for item in self.notifications(
                     goal_id=goal_id, limit=limit)]

    def observe(self, goal_id=None, health=False):
        """Read-only observability projection (the company dashboard).

        ``observe`` alone renders the full dashboard: health counters,
        per-goal progress (stage, run, workflow position, open work
        orders), pending attention, and memory totals. ``--goal`` renders
        the causal trace for one Goal; ``--health`` renders only counters.
        """
        observer = Observer(self.database)
        if goal_id:
            return observer.trace(goal_id)
        if health:
            return observer.health()
        return observer.dashboard()

    def unread_results(self, goal_id=None, **_kwargs):
        clauses, args = ["r.status='complete'"], []
        if goal_id:
            clauses.append("r.goal_id=?"); args.append(goal_id)
        with self.database.connect() as connection:
            return [dict(row) for row in connection.execute("""SELECT r.id,r.goal_id,
                r.evaluation_json AS result FROM core_runs r WHERE """
                + " AND ".join(clauses) + " ORDER BY r.updated_at DESC LIMIT 20", args)]
