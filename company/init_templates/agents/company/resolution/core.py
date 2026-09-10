"""Execute, fix, retry, and validate an Intervention without spawning Goals."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from ..agents.core import Agent, AgentEvidence, AgentExecutor
from ..evidence import EvidenceRepository
from ..goals import GoalRepository
from ..memory import Memory, MemoryRepository
from ..state import Database
from ..work_orders import WorkOrderRepository
from ..workflows import WorkflowRepository


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


UNINSTALLED_AGENT_MESSAGE = (
    "workflow {workflow_id} step {step_id} declares agent {agent_id!r}, "
    "which is neither an installed Agent, nor the goal owner, nor declared "
    "by the Department owning the workflow — refusing to execute work for "
    "an undeclared executor")
UNINSTALLED_DIRECT_AGENT_MESSAGE = (
    "intervention assigns agent {agent_id!r}, which is neither an installed "
    "Agent nor the goal owner — refusing to execute work for an undeclared "
    "executor")


class ResolutionOutcome(str, Enum):
    CONTINUE_LOCAL = "CONTINUE_LOCAL"
    RETURN_TO_GOAL = "RETURN_TO_GOAL"
    ESCALATE_TO_GOAL = "ESCALATE_TO_GOAL"
    ASK_USER = "ASK_USER"
    # Host dispatch, never an owner ask: the assigned Agent (a host-side
    # persona) executes the parked WorkOrder and completes it.
    HOST_WORK = "HOST_WORK"
    # A structural defect in the Workflow or its wiring: the engine
    # opens a bounded system-improvement Goal, repairs, and resumes.
    SYSTEM_DEFECT = "SYSTEM_DEFECT"


@dataclass(frozen=True)
class Intervention:
    id: str
    goal_id: str
    run_id: str
    kind: str
    description: str
    status: str
    context: dict
    resolution_outcome: str | None = None


@dataclass(frozen=True)
class ResolutionResult:
    outcome: ResolutionOutcome
    intervention: Intervention
    message: str = ""
    #: The classified structural defect for a SYSTEM_DEFECT outcome.
    defect: "StructuralDefect | None" = None


class InterventionRepository:
    def __init__(self, database: Database):
        self.database = database

    def create(self, *, goal_id: str, run_id: str, kind: str,
               description: str, context: dict | None = None) -> Intervention:
        intervention_id = f"intervention-{uuid.uuid4().hex[:12]}"
        stamp = _now()
        with self.database.connect() as connection:
            connection.execute("INSERT INTO core_interventions VALUES (?,?,?,?,?,?,?,?,?,?)",
                (intervention_id, goal_id, run_id, kind, description, "running", None,
                 json.dumps(context or {}), stamp, stamp))
        return self.get(intervention_id)

    def get(self, intervention_id: str) -> Intervention:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM core_interventions WHERE id=?", (intervention_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown intervention: {intervention_id}")
        return Intervention(row["id"], row["goal_id"], row["run_id"], row["kind"],
                            row["description"], row["status"],
                            json.loads(row["context_json"]), row["resolution_outcome"])

    def active_for_run(self, run_id: str) -> Intervention | None:
        with self.database.connect() as connection:
            row = connection.execute("""SELECT id FROM core_interventions
                WHERE run_id=? AND status IN ('running','waiting')
                ORDER BY created_at DESC LIMIT 1""", (run_id,)).fetchone()
        return None if row is None else self.get(row[0])

    def finish(self, intervention_id: str, outcome: ResolutionOutcome,
               *, message: str = "") -> Intervention:
        status = "complete" if outcome == ResolutionOutcome.RETURN_TO_GOAL else (
            "waiting" if outcome == ResolutionOutcome.ASK_USER else (
            "running" if outcome == ResolutionOutcome.CONTINUE_LOCAL else "escalated"))
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT context_json FROM core_interventions WHERE id=?", (intervention_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown intervention: {intervention_id}")
            context = json.loads(row[0])
            if message:
                context["resolution_message"] = message
            connection.execute("""UPDATE core_interventions
                SET status=?,resolution_outcome=?,context_json=?,updated_at=? WHERE id=?""",
                (status, outcome.value, json.dumps(context), _now(), intervention_id))
        return self.get(intervention_id)


class ApprovalRepository:
    def __init__(self, database: Database):
        self.database = database

    def status(self, run_id: str, key: str, *, intervention_id: str | None = None) -> str | None:
        """Approval state for one key.

        An intervention-scoped lookup that finds nothing falls back to a
        run-scoped grant (intervention_id NULL, the ``core_run_approval_key``
        index): ``approve --scope run`` satisfies every later intervention
        of the same run. A new run never inherits a grant.
        """
        with self.database.connect() as connection:
            if intervention_id is None:
                row = connection.execute("""SELECT status FROM core_approvals
                    WHERE run_id=? AND key=? ORDER BY intervention_id IS NULL,updated_at DESC
                    LIMIT 1""", (run_id, key)).fetchone()
            else:
                row = connection.execute("""SELECT status FROM core_approvals
                    WHERE run_id=? AND key=? AND intervention_id=?""",
                    (run_id, key, intervention_id)).fetchone()
                if row is None:
                    row = connection.execute("""SELECT status FROM core_approvals
                        WHERE run_id=? AND key=? AND intervention_id IS NULL""",
                        (run_id, key)).fetchone()
        return None if row is None else row[0]

    def grant(self, *, goal_id: str, run_id: str, key: str,
              intervention_id: str | None = None, note: str | None = None) -> None:
        stamp = _now()
        with self.database.connect() as connection:
            existing = connection.execute("""SELECT id FROM core_approvals
                WHERE run_id=? AND key=? AND
                  ((? IS NULL AND intervention_id IS NULL) OR intervention_id=?)""",
                (run_id, key, intervention_id, intervention_id)).fetchone()
            if existing:
                connection.execute("""UPDATE core_approvals SET status='approved',
                    note=?,updated_at=? WHERE id=?""", (note, stamp, existing[0]))
            else:
                connection.execute("INSERT INTO core_approvals VALUES (?,?,?,?,?,?,?,?,?)",
                    (f"approval-{uuid.uuid4().hex[:12]}", goal_id, run_id,
                     intervention_id, key, "approved", note, stamp, stamp))


@dataclass(frozen=True)
class StructuralDefect:
    """One classified structural defect (issue: immediate self-repair).

    ``kind`` is one of:
    - ``step_contract``  — the step declares evidence kinds its executor
      cannot produce (declared vs actual contradiction);
    - ``wiring``         — the declared executor is missing or lacks the
      declared skills/connections/capabilities;
    - ``behavior``       — the executor itself reports defective
      Workflow behavior (wrong format, bad UX, missing validation).
    """

    kind: str
    summary: str
    workflow_id: str | None = None
    step_id: str | None = None


class ResolutionCycle:
    """Own Workflow execution until a meaningful boundary is reached."""

    def __init__(self, database: Database, executor: AgentExecutor, *,
                 agents: dict[str, Agent] | None = None, max_local_iterations: int = 50,
                 department_agents: dict[str, tuple[str, ...]] | None = None):
        self.database = database
        self.executor = executor
        self.agents = dict(agents or {})
        #: Department-declared executor ids keyed by department id (F4).
        #: Workflow steps may execute for an agent their owning
        #: Department declares; everything else must be installed or be
        #: the goal owner. The CLI adapter wires this from the loaded
        #: Department manifests.
        self.department_agents = dict(department_agents or {})
        self.max_local_iterations = max_local_iterations
        self.interventions = InterventionRepository(database)
        self.workflows = WorkflowRepository(database)
        self.work_orders = WorkOrderRepository(database)
        self.evidence = EvidenceRepository(database)
        self.memory = MemoryRepository(database, self.evidence)
        self.approvals = ApprovalRepository(database)
        self.goals = GoalRepository(database)

    def _wiring_defect(self, step, workflow_id: str | None = None) -> "StructuralDefect | None":
        """Issue #11: enforce an INSTALLED agent's declared capabilities.

        A declared executor that is installed (``agents/installed``) is
        only permitted to execute a step it declares the skills,
        connections, and capabilities for. A bare agent id (a Department
        persona that is not installed, or the goal owner) carries no
        declaration to enforce and passes — existence is validated
        separately by ``_executor_is_valid``.
        """
        agent = self.agents.get(step.agent_id)
        if agent is None:
            return None
        missing_skills = [item for item in step.skill_ids
                          if item not in agent.skill_ids]
        missing_connections = [item for item in step.connection_ids
                               if item not in agent.connection_ids]
        missing_capabilities = [
            item for item in (step.requirements or {}).get("capabilities", ())
            if item not in agent.capability_ids]
        problems = []
        if missing_skills:
            problems.append(f"skills {missing_skills}")
        if missing_connections:
            problems.append(f"connections {missing_connections}")
        if missing_capabilities:
            problems.append(f"capabilities {missing_capabilities}")
        if not problems:
            return None
        return StructuralDefect(
            "wiring",
            f"workflow step {step.id!r} requires {', '.join(problems)} "
            f"that installed agent {step.agent_id!r} does not declare — "
            "the step's declared wiring does not match its executor",
            workflow_id=workflow_id, step_id=step.id)

    def _executor_is_valid(self, agent_id: str, owner_id: str,
                           department_declared: tuple[str, ...] = ()) -> bool:
        """F4: refuse to execute for an undeclared or uninstalled agent.

        A valid executor identity is exact-string one of: an installed
        Agent (``agents/installed`` declarations, ``self.agents``), the
        goal owner, or — for workflow steps — an agent the owning
        Department declares in its manifest. Every other identity
        escalates instead of executing.
        """
        return (agent_id in self.agents
                or agent_id == owner_id
                or agent_id in (department_declared or ()))

    #: How many memory claims a WorkOrder brief carries at most (L1):
    #: the newest active claims for the work being opened, bounded so a
    #: long-lived Workflow cannot grow its brief without limit.
    BRIEF_MEMORY_LIMIT = 5

    def _brief_memory(self, *, workflow_id: str | None = None,
                      goal_id: str | None = None) -> list[str]:
        """The learning one WorkOrder brief carries (L1: causal memory
        injection).

        A workflow order carries the workflow's own active workflow-scope
        claims; a direct order carries the goal-relevant claims that are
        not owner profile preferences. Newest first, bounded to
        ``BRIEF_MEMORY_LIMIT`` claim strings — the executor reads the
        claims themselves, never memory ids. Pure read.
        """
        if workflow_id:
            claims = self.memory.relevant(
                scope="workflow", workflow_id=workflow_id,
                limit=self.BRIEF_MEMORY_LIMIT)
        else:
            claims = [item for item in self.memory.relevant(
                goal_id=goal_id, limit=20) if item.scope != "owner"]
            claims = claims[:self.BRIEF_MEMORY_LIMIT]
        return [item.claim for item in claims]

    def remember_workflow_learning(self, order, learning: str,
                                    evidence_ids) -> Memory:
        """The one workflow-learning writer (L4).

        Both paths that persist workflow memory — the executor path (a
        step completing with ``workflow_learning``) and the CLI ``tasks
        --complete --learning`` flow — go through this single writer, so
        the write shape has one authority: workflow scope, the order's
        own evidence, full Goal/Run/Intervention lineage, and the
        WorkflowRun's workflow_id. ``MemoryRepository.remember`` stays
        the enforcing guard underneath.
        """
        workflow_id = None
        if order.workflow_run_id:
            with self.database.connect() as connection:
                row = connection.execute(
                    "SELECT workflow_id FROM core_workflow_runs WHERE id=?",
                    (order.workflow_run_id,)).fetchone()
            workflow_id = row[0] if row else None
        return self.memory.remember(
            "workflow", learning, evidence_ids=tuple(evidence_ids),
            goal_id=order.goal_id, run_id=order.run_id,
            intervention_id=order.intervention_id, workflow_id=workflow_id)

    def resolve(self, intervention_id: str) -> ResolutionResult:
        intervention = self.interventions.get(intervention_id)
        workflow_id = intervention.context.get("workflow_id")
        if not workflow_id:
            return self._resolve_direct(intervention)
        workflow = self.workflows.get(workflow_id)
        workflow_run = self.workflows.active_for_intervention(intervention.id)
        # F4: validate every step's declared executor BEFORE starting the
        # workflow run or opening any WorkOrder — a step naming an agent
        # that is neither installed, nor the goal owner, nor declared by
        # the Department owning the workflow escalates immediately, and
        # no order and no pre-claim is created for it. A resumed run
        # validates its own persisted step snapshot.
        pending_steps = (workflow_run.steps if workflow_run is not None
                         else workflow.steps)
        owner_id = self.goals.get(intervention.goal_id).owner_id
        department_declared = self.department_agents.get(
            workflow.department_id or "", ())
        for step in pending_steps:
            if self._executor_is_valid(step.agent_id, owner_id,
                                       department_declared):
                # Issue #11: an installed agent is only permitted to
                # execute a step it declares the skills, connections,
                # and capabilities for. A wiring mismatch is a structural
                # defect — repair the declaration, never execute it.
                wiring = self._wiring_defect(step, workflow_id)
                if wiring is not None:
                    return self._defect(intervention, wiring)
                continue
            return self._finish(
                intervention, ResolutionOutcome.ESCALATE_TO_GOAL,
                UNINSTALLED_AGENT_MESSAGE.format(
                    workflow_id=workflow_id, step_id=step.id,
                    agent_id=step.agent_id))
        if workflow_run is None:
            workflow_run = self.workflows.start(
                workflow_id, goal_id=intervention.goal_id, run_id=intervention.run_id,
                intervention_id=intervention.id)

        for _ in range(self.max_local_iterations):
            workflow_run = self.workflows.run(workflow_run.id)
            workflow = self.workflows.get(workflow_run.workflow_id)
            if (workflow_run.status == "complete"
                    or workflow_run.current_step >= len(workflow_run.steps)):
                return self._finish(intervention, ResolutionOutcome.RETURN_TO_GOAL,
                                    "workflow completed and validated")
            step = workflow_run.steps[workflow_run.current_step]
            missing_approvals = [key for key in step.approval_keys
                if self.approvals.status(
                    intervention.run_id, key,
                    intervention_id=intervention.id) != "approved"]
            if missing_approvals:
                self.workflows.set_status(workflow_run.id, "waiting")
                return self._finish(intervention, ResolutionOutcome.ASK_USER,
                                    f"approval required: {missing_approvals[0]}")

            # An approval-only step (declares approval keys, produces no
            # evidence) is a gate, not work: once its keys are granted,
            # advance the workflow instead of parking a work order for it.
            if step.approval_keys and not step.evidence_kinds:
                self.workflows.set_status(workflow_run.id, "running")
                self.workflows.advance(workflow_run.id)
                continue

            prior = self.work_orders.for_workflow_run(workflow_run.id)
            completed = [item for item in prior
                         if item.step_id == step.id and item.status == "completed"]
            if completed:
                self.workflows.set_status(workflow_run.id, "running")
                self.workflows.advance(workflow_run.id)
                continue

            order = self.work_orders.open(
                goal_id=intervention.goal_id, run_id=intervention.run_id,
                intervention_id=intervention.id, workflow_run_id=workflow_run.id,
                step_id=step.id, agent_id=step.agent_id,
                brief={"instruction": step.instruction,
                       "evidence_kind": step.evidence_kind,
                       "evidence_kinds": list(step.evidence_kinds),
                       "skill_ids": list(step.skill_ids),
                       "connection_ids": list(step.connection_ids),
                       "requirements": dict(step.requirements),
                       "memory": self._brief_memory(
                           workflow_id=workflow.id)})
            if order.status == "open":
                # Claim with the bare agent id: the documented host flow
                # (`tasks <id> --complete <agent_id>`) and the notification
                # text both name the agent, so the order must be claimable
                # under that identity.
                order = self.work_orders.claim(order.id, step.agent_id)
            agent = self.agents.get(step.agent_id, Agent(step.agent_id))
            result = self.executor.execute(agent, order)
            if result.status == "completed":
                evidence = tuple(result.evidence) or (AgentEvidence(
                    result.evidence_kind or step.evidence_kind or "workflow_result",
                    result.payload),)
                required = set(step.evidence_kinds)
                supplied = {item.kind for item in evidence}
                if required and not required.issubset(supplied):
                    # A step contract its executor cannot satisfy is a
                    # structural defect (issue #4/#8): declared vs actual
                    # behavior contradicts. Repair the definition, do
                    # not retry the same broken shape.
                    self.work_orders.fail(order.id, "step evidence contract mismatch",
                                          executor_id=order.claimed_by or "")
                    return self._defect(intervention, StructuralDefect(
                        "step_contract",
                        f"step {step.id!r} declares evidence kinds "
                        f"{sorted(required)} but its executor completed "
                        f"with {sorted(supplied)}; the step contract "
                        "contradicts the actual behavior",
                        workflow_id=workflow_run.workflow_id, step_id=step.id))
                order, evidence_ids = self.work_orders.complete_with_evidence(
                    order.id, result.payload, executor_id=order.claimed_by or "",
                    kind=evidence[0].kind, payload=evidence[0].payload,
                    evidence_items=[(item.kind, item.payload) for item in evidence],
                    advance_workflow=True)
                if result.workflow_learning:
                    self.remember_workflow_learning(
                        order, result.workflow_learning, evidence_ids)
                continue
            if result.status == "fixable":
                self.work_orders.fail(order.id, result.message or "local failure",
                                      executor_id=order.claimed_by or "")
                self.evidence.record(
                    goal_id=intervention.goal_id, run_id=intervention.run_id,
                    intervention_id=intervention.id, workflow_run_id=workflow_run.id,
                    work_order_id=order.id, kind="resolution_iteration",
                    payload={"status": "fixed_locally", "message": result.message})
                continue
            if result.status == "escalate":
                return self._finish(intervention, ResolutionOutcome.ESCALATE_TO_GOAL,
                                    result.message or "Goal-level decision is invalid")
            if result.status == "defect":
                # The executor reports a structural defect in the
                # Workflow's behavior (wrong format, bad UX, missing
                # validation): repair immediately, never retry.
                return self._defect(intervention, StructuralDefect(
                    "behavior",
                    result.message or "the executor reported a structural "
                    "Workflow defect",
                    workflow_id=workflow_run.workflow_id, step_id=step.id))
            if result.status == "host_work":
                self.workflows.set_status(workflow_run.id, "waiting")
                return self._finish(intervention, ResolutionOutcome.HOST_WORK,
                                    result.message or f"work order {order.id} is "
                                    "ready for its assigned Agent")
            return self._finish(intervention, ResolutionOutcome.ASK_USER,
                                result.message or "user context or authority required")

        return self._finish(intervention, ResolutionOutcome.CONTINUE_LOCAL,
                            "local iteration budget reached; resume Resolution")

    def _resolve_direct(self, intervention: Intervention) -> ResolutionResult:
        if intervention.context.get("result_ready"):
            return self._finish(intervention, ResolutionOutcome.RETURN_TO_GOAL,
                                "bounded intervention result is ready")
        agent_id = intervention.context.get("agent_id")
        if not agent_id:
            return self._finish(intervention, ResolutionOutcome.ASK_USER,
                                "intervention requires a Workflow or Agent")
        # F4: refuse direct work for an executor that is neither installed
        # nor the goal owner BEFORE opening any WorkOrder. Department
        # declarations never cover direct assignments — the owner or an
        # installed Agent executes those.
        owner_id = self.goals.get(intervention.goal_id).owner_id
        if not self._executor_is_valid(agent_id, owner_id):
            # An assigned direct executor that does not exist is a
            # structural wiring defect (issue #4), not a goal-strategy
            # problem: repair the assignment, do not re-decide blind.
            return self._defect(intervention, StructuralDefect(
                "wiring",
                UNINSTALLED_DIRECT_AGENT_MESSAGE.format(agent_id=agent_id)))
        with self.database.connect() as connection:
            completed = connection.execute("""SELECT work.id
                FROM core_work_orders AS work
                WHERE work.intervention_id=? AND work.step_id='direct'
                  AND work.status='completed'
                  AND EXISTS (
                    SELECT 1 FROM core_evidence AS evidence
                    WHERE evidence.work_order_id=work.id)
                ORDER BY work.created_at DESC LIMIT 1""",
                (intervention.id,)).fetchone()
        if completed is not None:
            return self._finish(intervention, ResolutionOutcome.RETURN_TO_GOAL,
                                "direct intervention completed and validated")
        for _ in range(self.max_local_iterations):
            order = self.work_orders.open(
                goal_id=intervention.goal_id, run_id=intervention.run_id,
                intervention_id=intervention.id, agent_id=agent_id,
                step_id="direct", brief={"instruction": intervention.description,
                                         "evidence_kind": intervention.context.get(
                                             "evidence_kind", "intervention_result"),
                                         "memory": self._brief_memory(
                                             goal_id=intervention.goal_id)})
            if order.status == "open":
                order = self.work_orders.claim(order.id, agent_id)
            result = self.executor.execute(self.agents.get(agent_id, Agent(agent_id)), order)
            if result.status == "completed":
                evidence = tuple(result.evidence) or (AgentEvidence(
                    result.evidence_kind or intervention.context.get(
                        "evidence_kind", "intervention_result"), result.payload),)
                order, evidence_ids = self.work_orders.complete_with_evidence(
                    order.id, result.payload, executor_id=order.claimed_by or "",
                    kind=evidence[0].kind, payload=evidence[0].payload,
                    evidence_items=[(item.kind, item.payload) for item in evidence])
                if result.workflow_learning:
                    # Issue #10: direct AgentResult learning goes through
                    # the ONE canonical operational-learning writer —
                    # the same one the CLI and workflow paths use — so a
                    # reusable direct lesson never disappears.
                    self.remember_workflow_learning(
                        order, result.workflow_learning, evidence_ids)
                return self._finish(intervention, ResolutionOutcome.RETURN_TO_GOAL,
                                    "direct intervention completed and validated")
            if result.status == "fixable":
                self.work_orders.fail(order.id, result.message or "local failure",
                                      executor_id=order.claimed_by or "")
                self.evidence.record(
                    goal_id=intervention.goal_id, run_id=intervention.run_id,
                    intervention_id=intervention.id, work_order_id=order.id,
                    kind="resolution_iteration",
                    payload={"status": "fixed_locally", "message": result.message})
                continue
            if result.status == "escalate":
                return self._finish(intervention, ResolutionOutcome.ESCALATE_TO_GOAL,
                                    result.message or "Goal-level decision is invalid")
            if result.status == "defect":
                return self._defect(intervention, StructuralDefect(
                    "behavior", result.message or "the executor reported a "
                    "structural defect in this work"))
            if result.status == "host_work":
                return self._finish(intervention, ResolutionOutcome.HOST_WORK,
                                    result.message or f"work order {order.id} is "
                                    "ready for its assigned Agent")
            return self._finish(intervention, ResolutionOutcome.ASK_USER,
                                result.message or "user context or authority required")
        return self._finish(intervention, ResolutionOutcome.CONTINUE_LOCAL,
                            "local iteration budget reached; resume Resolution")

    def _defect(self, intervention: Intervention,
                defect: StructuralDefect) -> ResolutionResult:
        """Classify one structural defect durably and hand it to the engine.

        The defect Evidence row (kind ``system_defect``) is the proof the
        bounded system-improvement Goal is built from; the engine opens
        that goal, repairs, and resumes this intervention's run. Defect
        detection is immediate — one broken execution is enough.
        """
        self.evidence.record(
            goal_id=intervention.goal_id, run_id=intervention.run_id,
            intervention_id=intervention.id, kind="system_defect",
            payload={"defect_kind": defect.kind, "summary": defect.summary,
                     "workflow_id": defect.workflow_id,
                     "step_id": defect.step_id})
        return ResolutionResult(ResolutionOutcome.SYSTEM_DEFECT, intervention,
                                defect.summary, defect=defect)

    def _finish(self, intervention: Intervention, outcome: ResolutionOutcome,
                message: str) -> ResolutionResult:
        # GoalRuntime persists this outcome together with the Run transition.
        return ResolutionResult(outcome, intervention, message)
