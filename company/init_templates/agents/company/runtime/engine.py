"""The clean adaptive Goal loop and restart-safe scheduler boundary."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from ..agents.core import AgentExecutor
from ..context import GoalContext
from ..evidence import EvidenceRepository
from ..goals import Goal, GoalRepository
from ..memory import MemoryRepository
from ..resolution import ResolutionCycle, ResolutionOutcome
from ..resolution.core import InterventionRepository
from ..state import Database
from ..workflows import Workflow, WorkflowStep


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Owner voice (goal-director-voice): every owner-facing text speaks owner
# language — goals by name with human progress, evidence as outcome
# sentences, loop position in plain words, and named options instead of
# decision enums. Raw ids, metric keys, stage/status enums, payloads, and
# CLI answer syntax never enter owner-facing text: they ride the payload
# machine fields (and the projection's Machine reference line) for the
# Director alone.
# ---------------------------------------------------------------------------

#: Human glosses for the metric keys the runtime itself names; every
#: other metric renders as plain numbers (value of target) with no key.
METRIC_GLOSSES = {
    "weekly_sales": "customers per week",
}


def _human_words(value) -> str:
    """An underscored identifier as plain owner words."""
    return str(value).replace("_", " ")


def human_progress(metric, value, target) -> str:
    """Where a goal stands, in owner words: the value against the target —
    never the metric key, the operator, or a goal id. Plain numbers when
    the metric carries no human gloss."""
    if value is None:
        return "no reading yet"
    if isinstance(value, bool):
        return "achieved" if value else "not yet achieved"
    gloss = METRIC_GLOSSES.get(metric)
    if gloss:
        return f"{value} of {target} {gloss}"
    return f"{value} of {target}"


def _stage_doing(stage) -> str:
    """What a run is doing at one stage of the loop, in owner words."""
    return {"OBSERVE": "reviewing the evidence",
            "DECIDE": "choosing the next move",
            "ACT": "running the bounded work",
            "EVALUATE": "checking the result"}.get(
                str(stage), "advancing the goal")


def human_loop_position(stage, status) -> str:
    """Where a run stands in the loop, in owner words — never the
    stage/status enums."""
    if str(status) == "waiting":
        return f"parked while {_stage_doing(stage)}"
    return f"next step: {_stage_doing(stage)}"


def human_stage_doing(stage) -> str:
    """The owner-words name of one loop stage (RUNTIME_FAILURE_MESSAGE)."""
    return _stage_doing(stage)


def human_goal_status(status) -> str:
    """A goal status in owner words."""
    return {"active": "in progress", "complete": "achieved",
            "paused": "paused", "abandoned": "dropped"}.get(
                str(status), _human_words(status))


def human_decision(kind) -> str:
    """A decision kind in owner words."""
    return {"execute_workflow": "ran a candidate workflow",
            "request_agent": "assigned bounded direct work",
            "evaluate": "checked the result",
            "decision_request": "parked for a decision"}.get(
                str(kind), _human_words(kind))


def human_resolution(outcome) -> str:
    """A resolution outcome in owner words."""
    return {"RETURN_TO_GOAL": "finished",
            "ESCALATE_TO_GOAL": "escalated back to the goal",
            "HOST_WORK": "parked as host work",
            "ASK_USER": "waiting on the owner",
            "CONTINUE_LOCAL": "kept fixing locally",
            "SYSTEM_DEFECT": "opened a repair"}.get(
                str(outcome), _human_words(outcome))


def human_attention(kind) -> str:
    """A notification kind in owner words."""
    return {"owner_input_required": "waiting on you",
            "host_work_required": "in progress with its agent"}.get(
                str(kind), _human_words(kind))


def human_outcome(kind, payload) -> str:
    """One evidence item as an owner-voice outcome sentence — its own
    numbers in plain words, never a kind=json dump. The runtime's
    bookkeeping keys stay out of the sentence."""
    parts = [f"{_human_words(key)} {value}"
             for key, value in sorted((payload or {}).items())
             if key not in ("source", "validity")]
    if parts:
        return ", ".join(parts)
    return _human_words(kind)


def _workflow_label(workflow_id) -> str:
    """The owner-voice label of a workflow id: its own name, last
    segment, underscores as spaces."""
    return _human_words(str(workflow_id).rsplit(":", 1)[-1]) or "workflow"


# After this many consecutive ESCALATE_TO_GOAL outcomes the goal parks for
# the owner instead of churning another run (a deterministic controller
# that re-decides the same failing intervention would otherwise livelock).
ESCALATION_PARK_THRESHOLD = 3
ESCALATION_PARK_MESSAGE = (
    "Goal '{name}' hit the same wall {threshold} times running — repeated "
    "escalation needs an owner decision: change the approach, supply missing "
    "context, or pause the goal")

# The DECIDE intelligence boundary: a goal the runtime cannot decide for the
# owner parks a decision_request instead of inventing bounded work.
DECIDE_REQUEST_DECISION = (
    "Choose the next bounded step: run one of the candidate workflows, or "
    "assign bounded direct work with a concrete instruction.")
DECIDE_REQUEST_AFTER = (
    "Answer in your own words and the Director records it; the run then "
    "executes the chosen step, and external actions still park for "
    "approval first.")

# The stall boundary: a goal whose evaluated metric stops moving while
# DECIDE keeps making the identical decision parks for the owner instead
# of chaining identical runs forever.
STALL_PARK_MESSAGE = (
    "Goal '{name}' has stopped moving: {progress} across {count} evaluated "
    "runs that all made the same decision. Continue, adjust, or leave "
    "parked.")
REVIEW_PARK_MESSAGE = (
    "Goal '{name}' reached its owner review checkpoint at run {sequence} — "
    "the owner asked to look at it every {interval} runs. Continue, adjust, "
    "or leave parked.")
PARK_DECISION = "Continue as-is, change the goal, or pause it."
PARK_AFTER = ("Say continue and the Director opens the next run now; "
              "leaving it parked holds the goal.")

# The F1 failure boundary: a goal whose stage raises parks with durable
# runtime_failure evidence and one owner ask instead of aborting the
# scheduler tick or crash-looping the background watcher.
RUNTIME_FAILURE_MESSAGE = (
    "Goal '{name}' run {sequence} failed while {doing}: {error}. The run "
    "is parked so sibling goals keep advancing.")
RUNTIME_FAILURE_DECISION = (
    "Fix what failed and let the run continue, or pause the goal")

# Host dispatch (separate HOST WORK from OWNER INPUT): a parked
# WorkOrder is work its assigned host-side Agent executes — never an
# owner ask. The owner sees normal execution language; internals stay
# out of it unless they ask for debugging.
HOST_WORK_MESSAGE = (
    "Working on '{name}': step '{step}' is with Agent {agent}.")

# The self-repair boundary: one classified structural defect opens a
# bounded system-improvement Goal immediately — never a second identical
# broken execution — and the original goal resumes automatically once
# the repair carries acceptance evidence.
DEFECT_REPAIR_MESSAGE = (
    "The '{workflow}' step '{step}' had a {kind} defect. Fixed it and "
    "resumed '{goal}'.")

# The fixable-churn boundary (F5): an executor that keeps marking the same
# intervention fixable exhausts its local budget over and over; after
# ESCALATION_PARK_THRESHOLD consecutive exhaustions the run parks for the
# owner instead of churning the identical fix loop.
FIXABLE_PARK_MESSAGE = (
    "Goal '{name}' is stuck in the same local fix loop: '{description}' "
    "exhausted its local retry budget {threshold} times in a row without "
    "completing. Change how this work runs, or pause the goal.")
FIXABLE_PARK_DECISION = (
    "Fix how this work runs so the step can complete, or pause the goal")


def _owner_ask(message: str, why: str, decision: str, after: str, *,
               goal=None, machine: dict | None = None,
               answer_syntax=None) -> dict:
    """The one owner-ask shape: what is needed, why now, what decision the
    owner must make, and what happens after they answer.

    The owner-facing four fields carry no ids, metric keys, enums, or CLI
    answer syntax. Those ride the payload instead — the goal subject the
    host renderers narrate (``goal.name``), the ``machine`` ids/enums, and
    the ``answer_syntax`` the Director records the owner's answer with.
    """
    payload = {"message": message, "why": why, "decision": decision,
               "after": after, "required_user_action": message}
    if goal is not None:
        payload["goal"] = {"id": goal.id, "name": goal.name}
    if machine:
        payload["machine"] = machine
    if answer_syntax:
        payload["answer_syntax"] = answer_syntax
    return payload


def _host_work_ask(goal, message: str, intervention=None, *,
                   repair: bool = False) -> dict:
    """The host-dispatch payload shape (issue #5).

    A parked WorkOrder is HOST WORK: the assigned Agent — a host-side
    persona such as the Director or an installed worker — executes it,
    and the owner is not interrupted. The payload keeps the same
    four-field shape (the host renders it if the owner asks), but the
    addressee is the agent, the decision is execution, and the message
    stays in owner-facing execution language.
    """
    agent = (intervention and _intervention_agent(intervention)
             or goal.owner_id)
    return {
        "message": message,
        "why": (f"a step of '{goal.name}' is ready for its assigned "
                "Agent; this is host work, not an owner decision"),
        "decision": (f"Execute the parked work order for Agent {agent} "
                     "and complete it with its declared evidence"),
        "after": ("the run continues automatically; live external "
                  "actions still park for approval first"),
        "repair": repair,
        "required_user_action": None,
        "goal": {"id": goal.id, "name": goal.name},
        "machine": {"goal_id": goal.id, "agent": agent,
                    "repair": repair},
    }


def _intervention_agent(intervention) -> str | None:
    return ((intervention.context or {}).get("agent_id")
            if intervention is not None else None)


def _encode(value):
    """JSON-encode one Run payload column (dataclass or plain value)."""
    if value is None:
        return None
    if hasattr(value, "__dict__"):
        value = value.__dict__
    return json.dumps(value)


def _insert_next_run(connection, goal_id: str, status: str) -> str:
    """Insert the goal's next OBSERVE run with one bounded retry.

    ``MAX(sequence)+1`` races between workers, so the UNIQUE(goal_id,
    sequence) constraint can abort the second writer. Recompute once and
    retry the INSERT once; a second trip means the goal is being
    sequenced faster than this writer can name a slot — a real error,
    re-raised.
    """
    attempt = 0
    while True:
        sequence = connection.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM core_runs WHERE goal_id=?",
            (goal_id,)).fetchone()[0]
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        stamp = _now()
        try:
            connection.execute("""INSERT INTO core_runs
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (run_id, goal_id, sequence, GoalStage.OBSERVE.value, status,
                 None, None, None, stamp, stamp))
            return run_id
        except sqlite3.IntegrityError:
            attempt += 1
            if attempt >= 2:
                raise


class GoalStage(str, Enum):
    OBSERVE = "OBSERVE"
    DECIDE = "DECIDE"
    ACT = "ACT"
    EVALUATE = "EVALUATE"


@dataclass(frozen=True)
class Decision:
    kind: str
    description: str
    workflow_id: str | None = None
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Evaluation:
    goal_complete: bool
    metrics: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    strategy_learning: str | None = None
    #: The approach (workflow id) a strategy lesson is about, so the
    #: persisted memory row links the lesson to the candidate it judges.
    strategy_workflow_id: str | None = None
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class GoalRun:
    id: str
    goal_id: str
    sequence: int
    stage: GoalStage
    status: str
    observation: dict | None = None
    decision: Decision | None = None
    evaluation: Evaluation | None = None


class GoalController(Protocol):
    def observe(self, context: GoalContext) -> dict: ...
    def decide(self, context: GoalContext, observation: dict) -> Decision: ...
    def evaluate(self, context: GoalContext, decision: Decision,
                 evidence: tuple) -> Evaluation: ...


class RunRepository:
    def __init__(self, database: Database):
        self.database = database

    def create(self, goal_id: str) -> GoalRun:
        with self.database.connect() as connection:
            # One bounded retry on the UNIQUE(goal_id, sequence) race —
            # see _insert_next_run.
            run_id = _insert_next_run(connection, goal_id, "ready")
        return self.get(run_id)

    def get(self, run_id: str) -> GoalRun:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM core_runs WHERE id=?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown run: {run_id}")
        decision_data = json.loads(row["decision_json"]) if row["decision_json"] else None
        evaluation_data = json.loads(row["evaluation_json"]) if row["evaluation_json"] else None
        return GoalRun(
            row["id"], row["goal_id"], row["sequence"], GoalStage(row["stage"]),
            row["status"],
            json.loads(row["observation_json"]) if row["observation_json"] else None,
            Decision(**decision_data) if decision_data else None,
            Evaluation(**{**evaluation_data,
                          "evidence_ids": tuple(evaluation_data.get("evidence_ids") or ())})
            if evaluation_data else None,
        )

    def current(self, goal_id: str) -> GoalRun:
        with self.database.connect() as connection:
            row = connection.execute("""SELECT id FROM core_runs WHERE goal_id=?
                ORDER BY sequence DESC LIMIT 1""", (goal_id,)).fetchone()
        if row is None:
            raise KeyError(f"goal has no run: {goal_id}")
        return self.get(row[0])

    def update(self, run_id: str, *, stage: GoalStage | None = None,
               status: str | None = None, observation=None, decision=None,
               evaluation=None) -> GoalRun:
        current = self.get(run_id)
        values = {
            "stage": (stage or current.stage).value,
            "status": status or current.status,
            "observation": current.observation if observation is None else observation,
            "decision": current.decision if decision is None else decision,
            "evaluation": current.evaluation if evaluation is None else evaluation,
        }
        with self.database.connect() as connection:
            # Compare-and-swap on the stage/status just read: rowcount 0
            # means another worker already moved this run — that worker
            # owns it, so this writer changes nothing and returns the
            # current state idempotently instead of raising.
            connection.execute("""UPDATE core_runs SET stage=?,status=?,
                observation_json=?,decision_json=?,evaluation_json=?,updated_at=?
                WHERE id=? AND stage=? AND status=?""",
                (values["stage"], values["status"], _encode(values["observation"]),
                 _encode(values["decision"]), _encode(values["evaluation"]), _now(),
                 run_id, current.stage.value, current.status))
        return self.get(run_id)

    def transition(self, run: GoalRun, stage: GoalStage, status: str, *,
                   observation=None, decision=None, evaluation=None) -> bool:
        """Compare-and-swap one stage/status transition of a Run.

        The stage/status ``run`` carries — the ones read at the top of
        ``advance()`` — are this writer's claim on the run. The guarded
        UPDATE matches no row exactly when another worker already
        advanced it; that worker owns the run, so the loser writes
        nothing and the caller returns the current (re-read) state
        idempotently without raising and without duplicating any row.
        """
        with self.database.connect() as connection:
            updated = connection.execute("""UPDATE core_runs SET stage=?,status=?,
                observation_json=COALESCE(?,observation_json),
                decision_json=COALESCE(?,decision_json),
                evaluation_json=COALESCE(?,evaluation_json),updated_at=?
                WHERE id=? AND stage=? AND status=?""",
                (stage.value, status, _encode(observation), _encode(decision),
                 _encode(evaluation), _now(), run.id, run.stage.value, run.status))
            return bool(updated.rowcount)

    def ready(self) -> list[GoalRun]:
        with self.database.connect() as connection:
            ids = [row[0] for row in connection.execute("""SELECT r.id
                FROM core_runs r JOIN core_goals g ON g.id=r.goal_id
                LEFT JOIN core_goal_metadata m ON m.goal_id=g.id
                WHERE g.status='active' AND r.status IN ('ready','running')
                  AND r.sequence=(SELECT MAX(r2.sequence) FROM core_runs r2
                                  WHERE r2.goal_id=r.goal_id)
                  AND NOT EXISTS (
                    SELECT 1 FROM core_goal_edges e
                    JOIN core_goals prerequisite ON prerequisite.id=e.source_goal_id
                    WHERE e.target_goal_id=g.id AND e.relation='blocks'
                      AND prerequisite.status!='complete')
                ORDER BY CASE json_extract(m.config_json,'$.priority')
                    WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2
                    WHEN 'low' THEN 3 WHEN 'deferred' THEN 4 ELSE 2 END,
                    CASE WHEN m.deadline IS NULL THEN 1 ELSE 0 END,m.deadline,
                    r.updated_at,r.id""")]
        return [self.get(item) for item in ids]


class GoalRuntime:
    """Composes isolated repositories; owns only Goal control and scheduling."""

    def __init__(self, path: str | Path, controller: GoalController,
                 executor: AgentExecutor, *, agents=None, max_local_iterations: int = 50,
                 readonly: bool = False):
        self.database = Database(path, readonly=readonly)
        self.controller = controller
        self.goals = GoalRepository(self.database)
        self.runs = RunRepository(self.database)
        self.evidence = EvidenceRepository(self.database)
        self.memory = MemoryRepository(self.database, self.evidence)
        self.resolution = ResolutionCycle(
            self.database, executor, agents=agents,
            max_local_iterations=max_local_iterations)
        self.interventions = InterventionRepository(self.database)

    def create_goal(self, name: str, metric: str, operator: str, target: Any, *,
                    parent_id: str | None = None, goal_id: str | None = None,
                    owner_id: str = "goal-runtime", deadline: str | None = None,
                    config: dict[str, Any] | None = None) -> Goal:
        return self.goals.create_with_initial_run(
            name, metric, operator, target, parent_id=parent_id, goal_id=goal_id,
            owner_id=owner_id, deadline=deadline, config=config)

    def advance(self, goal_id: str) -> dict:
        goal = self.goals.get(goal_id)
        run = self.runs.current(goal_id)  # this writer's claim on the run
        if goal.status != "active":
            return self.status(goal_id)
        context = self._context(goal, run)
        if run.stage == GoalStage.OBSERVE:
            try:
                observation = self.controller.observe(context)
            except Exception as exc:
                # F1: a raising controller parks this one goal instead of
                # escaping the runtime.
                return self._park_runtime_failure(goal, run, exc)
            if not self.runs.transition(run, GoalStage.DECIDE, "running",
                                        observation=observation):
                return self.status(goal_id)  # another worker owns this run
        elif run.stage == GoalStage.DECIDE:
            if (run.status == "waiting" and run.decision is not None
                    and run.decision.kind == "decision_request"):
                # Already parked for the owner: advancing is idempotent.
                return self.status(goal_id)
            try:
                decision = self.controller.decide(context, run.observation or {})
            except Exception as exc:
                return self._park_runtime_failure(goal, run, exc)
            if not isinstance(decision, Decision):
                raise TypeError("GoalController.decide must return Decision")
            if decision.kind == "decision_request":
                # DECIDE intelligence boundary: the runtime refuses to
                # invent bounded work. Park the run on the ask itself —
                # no Intervention and no WorkOrder is ever created for a
                # decision_request — until `goal decide` answers it.
                self._park_decision_request(goal, run, decision)
                return self.status(goal_id)
            if not self.runs.transition(run, GoalStage.ACT, "running",
                                        decision=decision):
                return self.status(goal_id)  # another worker owns this run
        elif run.stage == GoalStage.ACT:
            if run.decision is None:
                raise RuntimeError("ACT requires a persisted Goal decision")
            # F6: DECIDE is a pure read, so the Workflow definition a
            # Decision declares becomes durable HERE — the same moment the
            # Intervention does. `goal decide` adoption writes the same
            # row for owner-answered decisions.
            self._persist_declared_workflow(run.decision, goal, run.id)
            intervention = self.interventions.active_for_run(run.id)
            if intervention is None:
                intervention_context = dict(run.decision.context)
                intervention_context.pop("workflow", None)
                if run.decision.workflow_id:
                    intervention_context["workflow_id"] = run.decision.workflow_id
                intervention = self.interventions.create(
                    goal_id=goal.id, run_id=run.id, kind=run.decision.kind,
                    description=run.decision.description, context=intervention_context)
            try:
                result = self.resolution.resolve(intervention.id)
            except Exception as exc:
                # The executor/Department seam raised: park this goal.
                return self._park_runtime_failure(goal, run, exc)
            self._commit_resolution(goal, run, result)
        else:
            if run.decision is None:
                raise RuntimeError("EVALUATE requires a persisted Goal decision")
            evidence = tuple(self.evidence.for_run(run.id))
            try:
                evaluation = self.controller.evaluate(context, run.decision, evidence)
            except Exception as exc:
                return self._park_runtime_failure(goal, run, exc)
            if not isinstance(evaluation, Evaluation):
                raise TypeError("GoalController.evaluate must return Evaluation")
            if evaluation.strategy_learning and not evaluation.evidence_ids:
                raise ValueError("strategy learning requires evidence from this Run")
            self._commit_evaluation(goal, run, evaluation)
        return self.status(goal_id)

    def _park_runtime_failure(self, goal: Goal, run: GoalRun,
                              exc: Exception) -> dict:
        """F1: park a goal whose stage raised, then return its status.

        The failure is never swallowed: durable ``runtime_failure``
        Evidence with Goal/Run lineage records what raised, exactly one
        pending ``owner_input_required`` ask carries the standard
        what/why/decision/after owner shape (never more than one pending
        for the run), and the run parks at ``waiting`` — which
        runs.ready() excludes — so the scheduler stops rescheduling the
        failure while sibling goals keep advancing.
        """
        stamp = _now()
        self.evidence.record(
            goal_id=goal.id, run_id=run.id, kind="runtime_failure",
            payload={"error": str(exc), "error_type": type(exc).__name__})
        payload = _owner_ask(
            message=RUNTIME_FAILURE_MESSAGE.format(
                name=goal.name, sequence=run.sequence,
                doing=human_stage_doing(run.stage.value), error=str(exc)),
            why=(f"run {run.sequence} of '{goal.name}' failed while "
                 f"{human_stage_doing(run.stage.value)} and was parked; "
                 "parking it keeps the scheduler and every sibling goal "
                 "advancing instead of crash-looping on the same failure"),
            decision=RUNTIME_FAILURE_DECISION,
            after=("once it is fixed the Director reopens the run; the "
                   "recorded failure keeps the error inspectable"),
            goal=goal,
            machine={"goal_id": goal.id, "run_id": run.id,
                     "stage": run.stage.value, "status": "waiting",
                     "error_type": type(exc).__name__},
            answer_syntax={"resume": f"company goal resume {goal.id}"})
        self.runs.update(run.id, status="waiting")
        with self.database.connect() as connection:
            parked = connection.execute("""SELECT COUNT(*) FROM core_notifications
                WHERE run_id=? AND intervention_id IS NULL
                  AND kind='owner_input_required' AND status='pending'""",
                (run.id,)).fetchone()[0]
            if not parked:
                connection.execute("""INSERT INTO core_notifications
                    (id,goal_id,run_id,intervention_id,kind,payload_json,status,
                     created_at,acknowledged_at) VALUES (?,?,?,?,?,?,'pending',?,NULL)""",
                    (f"notification-{uuid.uuid4().hex[:12]}", goal.id, run.id, None,
                     "owner_input_required", json.dumps(payload), stamp))
        return self.status(goal.id)

    def _persist_declared_workflow(self, decision: Decision, goal: Goal,
                                    run_id: str) -> None:
        """Persist the Workflow definition a DECIDE Decision carries (F6).

        ``decide()`` never writes: the controller chooses a candidate and
        declares its definition in the Decision context, and this — ACT —
        is where that declaration becomes durable, exactly like the
        Intervention. ``goal decide`` adoption writes the same row for
        owner-answered decisions, and an already-persisted workflow saves
        as a no-op (or bumps its version when the declaration changed).
        """
        declared = (decision.context or {}).get("workflow")
        if decision.context is not None:
            decision.context["goal_id"] = goal.id
            decision.context["run_id"] = run_id
        crystallized = (decision.context or {}).get("crystallized")
        if (decision.kind != "execute_workflow" or not decision.workflow_id
                or not isinstance(declared, dict)):
            return
        steps = tuple(
            WorkflowStep(**item) for item in declared.get("steps") or ())
        self.resolution.workflows.save(Workflow(
            decision.workflow_id, declared.get("name") or decision.workflow_id,
            steps, declared.get("department_id"), declared.get("version") or 1))
        if crystallized:
            # Provenance (issue #3): the provenance Evidence row and the
            # workflow-scope formation claim explain WHY this Workflow
            # exists — which repeated executions formed it — grounded in
            # this run so the causal chain stays reconstructable.
            evidence = self.evidence.record(
                goal_id=goal.id, run_id=run_id,
                kind="workflow_crystallized",
                payload={"workflow_id": decision.workflow_id,
                         "from_orders": crystallized.get("from_orders") or [],
                         "shape": crystallized.get("shape") or {},
                         "reason": crystallized.get("reason") or ""})
            if run_id:
                self.memory.remember(
                    "workflow",
                    f"{decision.workflow_id} formed from "
                    f"{len(crystallized.get('from_orders') or [])} materially "
                    f"equivalent direct executions: "
                    f"{crystallized.get('reason')}",
                    evidence_ids=(evidence.id,), goal_id=goal.id,
                    run_id=run_id, workflow_id=decision.workflow_id)

    def _commit_resolution(self, goal: Goal, run: GoalRun, result) -> None:
        stamp = _now()
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT context_json FROM core_interventions WHERE id=?",
                (result.intervention.id,)).fetchone()
            if row is None:
                raise RuntimeError("Resolution Intervention disappeared")
            context = json.loads(row[0])
            context["resolution_message"] = result.message
            if result.outcome == ResolutionOutcome.RETURN_TO_GOAL:
                intervention_status, stage, run_status = "complete", GoalStage.EVALUATE, "running"
            elif result.outcome == ResolutionOutcome.CONTINUE_LOCAL:
                intervention_status, stage, run_status = "running", GoalStage.ACT, "ready"
                # F5: a fixable executor that exhausts its local budget
                # over and over parks after ESCALATION_PARK_THRESHOLD
                # consecutive exhaustions instead of churning the same
                # fix loop forever. The count below already includes this
                # exhaustion: its resolution_iteration evidence rows were
                # recorded before this commit.
                if self._consecutive_local_exhaustions(
                        connection, result.intervention.id,
                        self.resolution.max_local_iterations
                        ) >= ESCALATION_PARK_THRESHOLD:
                    intervention_status, stage, run_status = (
                        "waiting", GoalStage.ACT, "waiting")
            elif result.outcome == ResolutionOutcome.ASK_USER:
                intervention_status, stage, run_status = "waiting", GoalStage.ACT, "waiting"
            elif result.outcome == ResolutionOutcome.HOST_WORK:
                # Host dispatch (issue #5): the assigned Agent executes
                # this work; the run parks waiting exactly like an ask,
                # but the notification is host work, never an owner ask.
                intervention_status, stage, run_status = "waiting", GoalStage.ACT, "waiting"
            elif result.outcome == ResolutionOutcome.SYSTEM_DEFECT:
                # Structural self-repair (issue #4): the defecting
                # intervention is closed for good (its decision declared
                # the broken shape) while the bounded system-improvement
                # Goal repairs; the resumed run re-decides against the
                # repaired definition through a fresh intervention.
                intervention_status, stage, run_status = (
                    "escalated", GoalStage.ACT, "waiting")
            else:
                intervention_status, stage, run_status = "escalated", GoalStage.ACT, "complete"
            # F2: the stage/status read when this advance began are the
            # claim on the run. Losing this compare-and-swap means another
            # worker already advanced it, so this writer commits nothing
            # further for the run.
            if result.outcome == ResolutionOutcome.SYSTEM_DEFECT:
                # Open the bounded system-improvement Goal for this
                # defect INSIDE the same transaction that parks the run,
                # then mark the defective workflow run superseded so the
                # resumed intervention re-executes against the repaired
                # definition instead of its broken snapshot.
                self._open_defect_repair(connection, goal, run,
                                         result.intervention, result.defect)
                claimed = connection.execute(
                    """UPDATE core_runs SET stage=?,status='waiting',updated_at=?
                       WHERE id=? AND stage=? AND status=?""",
                    (GoalStage.ACT.value, stamp, run.id,
                     run.stage.value, run.status)).rowcount
            elif result.outcome == ResolutionOutcome.ESCALATE_TO_GOAL:
                consecutive = self._consecutive_escalations(
                    connection, goal.id, run.sequence) + 1
                if consecutive >= ESCALATION_PARK_THRESHOLD:
                    # Stop the churn: park the run in the ASK_USER shape
                    # (run waiting, no follow-up run) until the owner
                    # answers, instead of opening a fresh run that a
                    # deterministic controller would fail the same way.
                    claimed = connection.execute(
                        """UPDATE core_runs SET stage=?,status='waiting',updated_at=?
                           WHERE id=? AND stage=? AND status=?""",
                        (GoalStage.ACT.value, stamp, run.id,
                         run.stage.value, run.status)).rowcount
                    intervention_status = "waiting"
                else:
                    evaluation = Evaluation(False, {}, result.message)
                    claimed = connection.execute(
                        """UPDATE core_runs SET status='complete',evaluation_json=?,
                           updated_at=? WHERE id=? AND stage=? AND status=?""",
                        (json.dumps(evaluation.__dict__), stamp, run.id,
                         run.stage.value, run.status)).rowcount
            else:
                claimed = connection.execute(
                    """UPDATE core_runs SET stage=?,status=?,updated_at=?
                       WHERE id=? AND stage=? AND status=?""",
                    (stage.value, run_status, stamp, run.id,
                     run.stage.value, run.status)).rowcount
            if not claimed:
                return  # another worker owns this run
            connection.execute("""UPDATE core_interventions
                SET status=?,resolution_outcome=?,context_json=?,updated_at=? WHERE id=?""",
                (intervention_status, result.outcome.value, json.dumps(context), stamp,
                 result.intervention.id))
            if result.outcome == ResolutionOutcome.ESCALATE_TO_GOAL:
                if intervention_status == "waiting":
                    payload = _owner_ask(
                        message=ESCALATION_PARK_MESSAGE.format(
                            name=goal.name, threshold=ESCALATION_PARK_THRESHOLD),
                        why=(f"the last {ESCALATION_PARK_THRESHOLD} runs of "
                             f"'{goal.name}' each escalated the same "
                             "goal-level decision; a deterministic controller "
                             "would repeat the failure forever"),
                        decision=("Change the approach, supply the missing "
                                  "context, or pause the goal"),
                        after=PARK_AFTER,
                        goal=goal,
                        machine={"goal_id": goal.id, "run_id": run.id,
                                 "stage": run.stage.value,
                                 "status": "waiting",
                                 "escalations": ESCALATION_PARK_THRESHOLD},
                        answer_syntax={"resume": f"company goal resume {goal.id}"})
                    connection.execute("""INSERT INTO core_notifications
                        (id,goal_id,run_id,intervention_id,kind,payload_json,status,
                         created_at,acknowledged_at) VALUES (?,?,?,?,?,?,?,?,NULL)
                        ON CONFLICT(intervention_id,kind) DO UPDATE SET
                          payload_json=excluded.payload_json,status='pending',
                          acknowledged_at=NULL""",
                        (f"notification-{uuid.uuid4().hex[:12]}", goal.id, run.id,
                         result.intervention.id, "owner_input_required",
                         json.dumps(payload), "pending", stamp))
                else:
                    _insert_next_run(connection, goal.id, "ready")
            elif result.outcome == ResolutionOutcome.ASK_USER:
                payload = self._ask_payload(goal, run, result.message,
                                            result.intervention)
                connection.execute("""INSERT INTO core_notifications
                    (id,goal_id,run_id,intervention_id,kind,payload_json,status,
                     created_at,acknowledged_at) VALUES (?,?,?,?,?,?,?,?,NULL)
                    ON CONFLICT(intervention_id,kind) DO UPDATE SET
                      payload_json=excluded.payload_json,status='pending',
                      acknowledged_at=NULL""",
                    (f"notification-{uuid.uuid4().hex[:12]}", goal.id, run.id,
                     result.intervention.id, "owner_input_required",
                     json.dumps(payload), "pending", stamp))
            elif result.outcome == ResolutionOutcome.HOST_WORK:
                # Host dispatch (issue #5): one host_work notification —
                # the assigned Agent (a host-side persona) executes the
                # parked order. The payload keeps the structured
                # what/why/decision/after shape for the host, rendered in
                # owner-facing execution language, and the owner is never
                # the addressee.
                payload = _host_work_ask(
                    goal=goal, message=result.message,
                    intervention=result.intervention)
                connection.execute("""INSERT INTO core_notifications
                    (id,goal_id,run_id,intervention_id,kind,payload_json,status,
                     created_at,acknowledged_at) VALUES (?,?,?,?,?,?,?,?,NULL)
                    ON CONFLICT(intervention_id,kind) DO UPDATE SET
                      payload_json=excluded.payload_json,status='pending',
                      acknowledged_at=NULL""",
                    (f"notification-{uuid.uuid4().hex[:12]}", goal.id, run.id,
                     result.intervention.id, "host_work_required",
                     json.dumps(payload), "pending", stamp))
            elif result.outcome == ResolutionOutcome.SYSTEM_DEFECT:
                # Structural self-repair: one host_work notification in
                # owner-facing language. The repair goal's execution and
                # the automatic resume are runtime mechanics — the owner
                # is not asked to notice, restart, or approve them.
                payload = _host_work_ask(
                    goal=goal, message=DEFECT_REPAIR_MESSAGE.format(
                        workflow=_workflow_label(
                            result.defect.workflow_id or "workflow"),
                        step=_human_words(result.defect.step_id or "step"),
                        kind=result.defect.kind.replace("_", " "),
                        goal=goal.name),
                    intervention=result.intervention,
                    repair=True)
                connection.execute("""INSERT INTO core_notifications
                    (id,goal_id,run_id,intervention_id,kind,payload_json,status,
                     created_at,acknowledged_at) VALUES (?,?,?,?,?,?,?,?,NULL)
                    ON CONFLICT(intervention_id,kind) DO UPDATE SET
                      payload_json=excluded.payload_json,status='pending',
                      acknowledged_at=NULL""",
                    (f"notification-{uuid.uuid4().hex[:12]}", goal.id, run.id,
                     result.intervention.id, "host_work_required",
                     json.dumps(payload), "pending", stamp))
            elif (result.outcome == ResolutionOutcome.CONTINUE_LOCAL
                    and intervention_status == "waiting"):
                # F5: the fixable-churn park — one owner ask naming the
                # loop, no follow-on run.
                payload = _owner_ask(
                    message=FIXABLE_PARK_MESSAGE.format(
                        name=goal.name, description=result.intervention.description,
                        threshold=ESCALATION_PARK_THRESHOLD),
                    why=(f"the executor kept marking the work of run "
                         f"{run.sequence} fixable, so every resolution pass "
                         f"spent its whole local budget and resumed; after "
                         f"{ESCALATION_PARK_THRESHOLD} consecutive "
                         "exhaustions of the same intervention the runtime "
                         "parks instead of churning the identical loop"),
                    decision=FIXABLE_PARK_DECISION,
                    after=("once the loop is addressed the Director reopens "
                           "the run; every retry keeps its evidence"),
                    goal=goal,
                    machine={"goal_id": goal.id, "run_id": run.id,
                             "stage": run.stage.value, "status": "waiting",
                             "intervention_id": result.intervention.id,
                             "exhaustions": ESCALATION_PARK_THRESHOLD},
                    answer_syntax={"resume": f"company goal resume {goal.id}"})
                connection.execute("""INSERT INTO core_notifications
                    (id,goal_id,run_id,intervention_id,kind,payload_json,status,
                     created_at,acknowledged_at) VALUES (?,?,?,?,?,?,?,?,NULL)
                    ON CONFLICT(intervention_id,kind) DO UPDATE SET
                      payload_json=excluded.payload_json,status='pending',
                      acknowledged_at=NULL""",
                    (f"notification-{uuid.uuid4().hex[:12]}", goal.id, run.id,
                     result.intervention.id, "owner_input_required",
                     json.dumps(payload), "pending", stamp))

    def _open_defect_repair(self, connection, goal: Goal, run: GoalRun,
                             intervention, defect) -> None:
        """Open the bounded system-improvement Goal for one structural
        defect, inside the transaction that parks the defective run.

        The repair Goal is created complete-with-initial-run, carries
        the exact defect (kind, summary, workflow, step, allowed shape)
        in its config, is linked to the original goal through a
        ``blocks`` edge (the original stays parked until the repair
        completes), and its DECIDE assigns the repair to the
        system-improvement Agent when one is installed. The original
        run is resumed automatically when the repair goal completes with
        acceptance evidence (``_commit_evaluation``), so the owner never
        has to notice the repair finished.
        """
        # Convergence guard (one repair per defect): a repeat defect
        # after a COMPLETED repair means the fix did not take — parking
        # as a runtime failure is the genuine owner boundary, never a
        # second repair goal. An INCOMPLETE repair for the same contract
        # already owns this defect: park the run, open nothing, and that
        # repair's completion wakes the run.
        stamp = _now()
        prior = connection.execute(
            """SELECT g.id, g.status FROM core_goals g
               JOIN core_goal_metadata m ON m.goal_id=g.id
               WHERE json_extract(m.config_json,'$.repair.workflow_id')=?
                 AND json_extract(m.config_json,'$.repair.step_id')=?
                 AND json_extract(m.config_json,'$.repair.blocked_goal_id')=?""",
            (defect.workflow_id, defect.step_id, goal.id)).fetchall()
        if prior:
            if all(row[1] == "complete" for row in prior):
                self._park_unconverged_defect(
                    connection, goal, run, intervention, defect)
            if intervention is not None:
                connection.execute("""UPDATE core_workflow_runs
                    SET status='superseded',updated_at=?
                    WHERE intervention_id=? AND status IN ('running','waiting')""",
                    (stamp, intervention.id))
            return
        repair_goal_id = f"goal-{uuid.uuid4().hex[:12]}"
        repair_config = {
            "repair": {
                "defect_kind": defect.kind,
                "summary": defect.summary,
                "workflow_id": defect.workflow_id,
                "step_id": defect.step_id,
                "blocked_goal_id": goal.id,
            },
            "allowed_files": [],  # bounded by the system-improvement Agent
            "acceptance": ("behavioral acceptance evidence proving the "
                           "repaired behavior, payload key "
                           "'acceptance_green': true"),
        }
        repair_name = (f"Repair {defect.kind.replace('_', ' ')} defect in "
                       f"{defect.workflow_id or 'the runtime'}")
        connection.execute(
            "INSERT INTO core_goals VALUES (?,?,?,?,?,?,?,?,?)",
            (repair_goal_id, repair_name, "acceptance_green", "ge", "1",
             None, "active", stamp, stamp))
        connection.execute(
            "INSERT INTO core_goal_metadata VALUES (?,?,?,?)",
            (repair_goal_id, self._repair_agent(), None,
             json.dumps(repair_config)))
        connection.execute(
            "INSERT INTO core_runs VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"run-{uuid.uuid4().hex[:12]}", repair_goal_id, 1,
             GoalStage.OBSERVE.value, "ready", None, None, None, stamp, stamp))
        # The repair blocks the original goal until it completes; the
        # parked waiting run stays owned by the defecting intervention.
        connection.execute(
            "INSERT OR IGNORE INTO core_goal_edges VALUES (?,?,?,?)",
            (repair_goal_id, goal.id, "blocks", stamp))
        # The defective WorkflowRun is superseded: the resumed run starts
        # a fresh WorkflowRun from the repaired definition instead of
        # continuing the broken snapshot.
        if intervention is not None:
            connection.execute("""UPDATE core_workflow_runs
                SET status='superseded',updated_at=?
                WHERE intervention_id=? AND status IN ('running','waiting')""",
                (stamp, intervention.id))

    def _park_unconverged_defect(self, connection, goal: Goal, run: GoalRun,
                                 intervention, defect) -> None:
        """Park a run whose step re-defects after a completed repair.

        The repair already ran with acceptance evidence and the same
        behavior still breaks: that is a runtime failure the owner must
        see, not another repair goal. Durable ``system_defect`` evidence
        records the repeat defect and one pending ``owner_input_required``
        ask (what/why/decision/after) carries it; the run is parked
        waiting by the caller, so the scheduler stops rescheduling it.
        """
        connection.execute(
            "INSERT INTO core_evidence VALUES (?,?,?,?,?,?,?,?,?)",
            (f"evidence-{uuid.uuid4().hex[:12]}", goal.id, run.id,
             intervention.id if intervention is not None else None, None,
             None, "system_defect",
             json.dumps({"defect_kind": defect.kind,
                         "summary": defect.summary,
                         "workflow_id": defect.workflow_id,
                         "step_id": defect.step_id,
                         "repeat_after_repair": True}),
             _now()))
        payload = _owner_ask(
            message=(f"The '{_workflow_label(defect.workflow_id or 'workflow')}' "
                     f"step '{_human_words(defect.step_id or 'step')}' broke "
                     f"again after its repair had already been completed and "
                     f"accepted on '{goal.name}'"),
            why=(f"a completed repair with acceptance evidence exists for "
                 f"this defect and the same behavior still fails — opening "
                 f"another identical repair would repeat forever"),
            decision=("Fix what keeps breaking it and let the run continue, "
                      "or pause the goal"),
            after=("once it is fixed the Director reopens the run; the "
                   "recorded repeat defect keeps the evidence inspectable"),
            goal=goal,
            machine={"goal_id": goal.id, "run_id": run.id,
                     "stage": GoalStage.ACT.value, "status": "waiting",
                     "workflow_id": defect.workflow_id,
                     "step_id": defect.step_id,
                     "repeat_defect": True},
            answer_syntax={"resume": f"company goal resume {goal.id}"})
        connection.execute("""INSERT INTO core_notifications
            (id,goal_id,run_id,intervention_id,kind,payload_json,status,
             created_at,acknowledged_at) VALUES (?,?,?,?,?,?,?,?,NULL)""",
            (f"notification-{uuid.uuid4().hex[:12]}", goal.id, run.id,
             intervention.id if intervention is not None else None,
             "owner_input_required", json.dumps(payload), "pending", _now()))

    def _repair_agent(self) -> str:
        """The owner id the repair goal routes to: the installed
        system-improvement Agent when present, else the Director."""
        return ("system-improvement" if "system-improvement" in
                (self.resolution.agents or {}) else "director")

    def _park_decision_request(self, goal: Goal, run: GoalRun,
                               decision: Decision) -> None:
        """Persist a decision_request park: run DECIDE/waiting plus the
        structured owner ask. No Intervention and no WorkOrder is created —
        the persisted Decision is what `goal decide` validates against."""
        stamp = _now()
        park = self.runs.update(run.id, stage=GoalStage.DECIDE, status="waiting",
                               decision=decision)
        if (park.stage, park.status) != (GoalStage.DECIDE, "waiting"):
            return  # another worker owns this run
        boundary = (decision.context or {}).get("owner_boundary") or "undecided"
        request = dict((decision.context or {}).get("decision_request") or {})
        digest = request.get("evidence") or []
        tried = "; ".join(
            str(item.get("outcome") or item.get("kind") or "?")
            for item in digest) or "nothing recorded yet"
        runs_count = len(request.get("recent_runs") or [])
        progress = human_progress(goal.metric,
                                  request.get("observation_value"),
                                  goal.target)
        payload = _owner_ask(
            message=decision.description,
            why=(f"'{goal.name}' stands at {progress}; what we have tried "
                 f"so far: {tried}. {runs_count} run(s) so far. "
                 + ("Every candidate approach has been tried and judged on "
                    "this goal — changing the strategy is an owner-level "
                    "material choice."
                    if boundary == "exhausted" else
                    "No Department can decide the next bounded step on the "
                    "owner's behalf.")),
            decision=DECIDE_REQUEST_DECISION,
            after=DECIDE_REQUEST_AFTER,
            goal=goal,
            machine={"goal_id": goal.id, "run_id": run.id,
                     "stage": run.stage.value, "status": run.status,
                     "metric": goal.metric, "operator": goal.operator,
                     "target": goal.target, "boundary": boundary},
            answer_syntax=request.get("answer_syntax"))
        # The park's addressee (issue #1/#5): an EXHAUSTED park is a
        # genuine owner boundary — the owner must change the approach.
        # An UNDECIDED park is HOST reasoning: the Director agent reads
        # the same structured ask and answers with `goal decide`, and
        # only relays to the owner when owner-only context or authority
        # is genuinely required. The owner is never the default
        # GoalController.
        kind = ("owner_input_required" if boundary == "exhausted"
                else "host_work_required")
        with self.database.connect() as connection:
            parked = connection.execute("""SELECT COUNT(*) FROM core_notifications
                WHERE run_id=? AND intervention_id IS NULL
                  AND kind=? AND status='pending'""",
                (run.id, kind)).fetchone()[0]
            if not parked:
                connection.execute("""INSERT INTO core_notifications
                    (id,goal_id,run_id,intervention_id,kind,payload_json,status,
                     created_at,acknowledged_at) VALUES (?,?,?,?,?,?,'pending',?,NULL)""",
                    (f"notification-{uuid.uuid4().hex[:12]}", goal.id, run.id, None,
                     kind, json.dumps(payload), stamp))

    def _ask_memory_line(self, goal: Goal, intervention) -> str | None:
        """F7(b): the learning a parked ASK_USER ask carries — the
        workflow's own workflow-scope claims for a workflow park, the
        goal-relevant claims (never owner profile claims) for direct
        work. Pure read; ``None`` when no claims exist so the ask omits
        the line cleanly."""
        workflow_id = ((intervention.context or {}).get("workflow_id")
                       if intervention is not None else None)
        if workflow_id:
            claims = [item.claim for item in self.memory.relevant(
                scope="workflow", workflow_id=workflow_id)]
            label = "Workflow learning"
        else:
            claims = [item.claim for item in self.memory.relevant(
                goal_id=goal.id) if item.scope != "owner"]
            label = "Relevant memory"
        if not claims:
            return None
        return f"{label}: " + "; ".join(claims)

    def _ask_payload(self, goal: Goal, run: GoalRun, message: str,
                     intervention=None) -> dict:
        """Owner-ask shape for a parked ASK_USER boundary (approval or work).

        Parked asks carry memory (F7(b)): the learning line is appended
        to the message unless the executor already rendered it, so an
        executor that omits it still gets an informative payload while
        one that reads the claims itself is not duplicated.
        """
        line = self._ask_memory_line(goal, intervention)
        if line is not None and line.split(":", 1)[0] + ":" not in message:
            message = f"{message}\n{line}"
        if message.startswith("approval required:"):
            key = message.split(":", 1)[1].strip()
            return _owner_ask(
                message=message,
                why=(f"a Workflow step of '{goal.name}' needs explicit owner "
                     "authority before it can run"),
                decision=("Approve exactly this action, or hold it back"),
                after=("the run continues its Workflow; live external actions "
                       "still only run after approval"),
                goal=goal,
                machine={"goal_id": goal.id, "run_id": run.id,
                         "stage": run.stage.value, "status": "waiting",
                         "approval_key": key},
                answer_syntax={
                    "approve": f"company approve {goal.id} --key {key}",
                    "approve_run": (f"company approve {goal.id} --key {key} "
                                    "--scope run")})
        return _owner_ask(
            message=message,
            why=(f"run {run.sequence} of '{goal.name}' parked bounded work "
                 "for the host; nothing executes implicitly"),
            decision=("Execute the parked work order and complete it with "
                      "its declared evidence"),
            after=("on completion the run advances; external actions still "
                   "park for approval first"),
            goal=goal,
            machine={"goal_id": goal.id, "run_id": run.id,
                     "stage": run.stage.value, "status": "waiting"})

    @staticmethod
    def _consecutive_escalations(connection, goal_id: str, sequence: int) -> int:
        """Count consecutive earlier runs of this goal that ended escalated.

        An escalated run is terminal in ACT (stage never reaches
        EVALUATE): stage='ACT' AND status='complete'. Any run that ended
        another way — an evaluated completion (stage EVALUATE) or a park
        for the owner (status waiting) — breaks the streak, so only
        back-to-back ESCALATE_TO_GOAL completions accumulate.
        """
        rows = connection.execute("""SELECT stage,status FROM core_runs
            WHERE goal_id=? AND sequence<? ORDER BY sequence DESC""",
            (goal_id, sequence)).fetchall()
        streak = 0
        for row in rows:
            if row["stage"] == "ACT" and row["status"] == "complete":
                streak += 1
            else:
                break
        return streak

    @staticmethod
    def _consecutive_local_exhaustions(connection, intervention_id: str,
                                       budget: int) -> int:
        """Count local-budget exhaustions already persisted for one
        Intervention (F5), derived deterministically from the data the
        resolution cycle already persists.

        Every fixable executor result inside the local loop records one
        ``resolution_iteration`` Evidence row on the Intervention, and one
        exhaustion consumes the whole ``max_local_iterations`` budget of
        them — including the exhaustion being committed now, whose rows
        are recorded before the commit. The count is therefore the
        trailing run of consecutive ``resolution_iteration`` rows (newest
        backwards, stopping at the first Evidence row of any other kind)
        divided by the budget.
        """
        rows = connection.execute("""SELECT kind FROM core_evidence
            WHERE intervention_id=? ORDER BY created_at DESC,rowid DESC""",
            (intervention_id,)).fetchall()
        trailing = 0
        for row in rows:
            if row[0] != "resolution_iteration":
                break
            trailing += 1
        budget = int(budget) if budget and int(budget) > 0 else 1
        return trailing // budget

    def _commit_evaluation(self, goal: Goal, run: GoalRun,
                           evaluation: Evaluation) -> None:
        stamp = _now()
        with self.database.connect() as connection:
            # F2: guard the run completion with the claim this writer read
            # at the top of advance(); a rowcount of 0 means another
            # worker owns the run and this writer commits nothing further.
            claimed = connection.execute("""UPDATE core_runs SET status='complete',
                evaluation_json=?,updated_at=? WHERE id=? AND stage=? AND status=?""",
                (json.dumps(evaluation.__dict__), stamp, run.id,
                 run.stage.value, run.status)).rowcount
            if not claimed:
                return  # another worker owns this run
            if evaluation.strategy_learning:
                marks = ",".join("?" for _ in evaluation.evidence_ids)
                rows = connection.execute(
                    f"SELECT id FROM core_evidence WHERE run_id=? AND id IN ({marks})",
                    (run.id, *evaluation.evidence_ids)).fetchall()
                if len(rows) != len(evaluation.evidence_ids):
                    raise ValueError("strategy learning evidence must belong to this Run")
                connection.execute("""INSERT INTO core_memory
                    (id,scope,claim,goal_id,run_id,intervention_id,workflow_id,
                     evidence_ids_json,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    (f"memory-{uuid.uuid4().hex[:12]}", "strategy",
                     evaluation.strategy_learning, goal.id, run.id, None,
                     evaluation.strategy_workflow_id,
                     json.dumps(evaluation.evidence_ids), stamp))
            if evaluation.goal_complete:
                connection.execute("UPDATE core_goals SET status='complete',updated_at=? WHERE id=?",
                                   (stamp, goal.id))
            else:
                park = self._evaluation_park(connection, goal, run, evaluation)
                if park is not None:
                    park_id = _insert_next_run(connection, goal.id, "waiting")
                    connection.execute("""INSERT INTO core_notifications
                        (id,goal_id,run_id,intervention_id,kind,payload_json,status,
                         created_at,acknowledged_at) VALUES (?,?,?,?,?,?,'pending',?,NULL)""",
                        (f"notification-{uuid.uuid4().hex[:12]}", goal.id, park_id,
                         None, "owner_input_required", json.dumps(park), stamp))
                else:
                    _insert_next_run(connection, goal.id, "ready")
        if evaluation.goal_complete:
            # The atomic adoption boundary (issue #8) runs after the
            # evaluation commits: a completed repair goal adopts the
            # revised Workflow, writes the revision learning, and wakes
            # the original blocked run in ONE transaction, so the
            # revision, its version, and its provenance never disagree.
            self._adopt_repair(goal, run)

    def _adopt_repair(self, goal: Goal, run: GoalRun) -> None:
        """Atomic adoption when a bounded repair Goal completes (issue #4/#8).

        Reads the repair contract from the completing goal's config; when
        its acceptance evidence carries a revised workflow definition,
        validates it, persists it through the one WorkflowRepository
        writer (version bump on change — historical WorkflowRun snapshots
        stay untouched), records the revision learning grounded in the
        acceptance evidence, and wakes the original blocked run — all in
        ONE transaction, so the revised definition, its version, and its
        provenance can never disagree. Runs only when the completing
        goal IS a repair goal; ordinary completions pay one config read.
        """
        repair = (goal.config or {}).get("repair")
        if not isinstance(repair, dict):
            return
        blocked_goal_id = repair.get("blocked_goal_id")
        revision = None
        for evidence in self.evidence.for_run(run.id):
            payload = evidence.payload or {}
            if isinstance(payload.get("workflow"), dict):
                revision = payload["workflow"]
        if revision is None:
            return  # a repair without a revised definition adopts nothing
        declared = dict(revision)
        workflow_id = declared.get("id") or repair.get("workflow_id")
        if not workflow_id:
            return
        steps = tuple(
            WorkflowStep(**item) for item in declared.get("steps") or ())
        try:
            existing = self.resolution.workflows.get(workflow_id)
        except KeyError:
            existing = None
        saved = self.resolution.workflows.save(Workflow(
            workflow_id, declared.get("name") or workflow_id, steps,
            declared.get("department_id"), declared.get("version") or 1))
        if existing is not None and existing.steps != saved.steps:
            lesson = (f"revised to v{saved.version} because the "
                      f"{repair.get('defect_kind', 'structural')} defect "
                      f"'{(repair.get('summary') or '')[:120]}' was exposed "
                      "by one execution and fixed with behavioral "
                      "acceptance evidence")
            self._remember_repair_learning(
                goal, run, workflow_id, lesson)
        if blocked_goal_id:
            stamp = _now()
            # The parked run RETRIES the repaired definition: its stale
            # pre-defect decision (the one that declared the broken
            # shape) is re-bound to the repaired workflow id with NO
            # "workflow" declaration in its context, so the retry
            # executes the persisted v2 through a fresh intervention
            # instead of re-persisting the older shape over the repair
            # or re-ranking candidates that would pick a different
            # approach than the one just repaired.
            retry = Decision(
                "execute_workflow",
                f"execute repaired {_workflow_label(workflow_id)}",
                workflow_id,
                context={"workflow_id": workflow_id, "repair_resume": True})
            with self.database.connect() as connection:
                connection.execute("""UPDATE core_runs
                    SET stage=?,status='ready',decision_json=?,updated_at=?
                    WHERE goal_id=? AND status='waiting'
                      AND sequence=(SELECT MAX(sequence) FROM core_runs
                                    WHERE goal_id=?)""",
                    (GoalStage.ACT.value, json.dumps(retry.__dict__), stamp,
                     blocked_goal_id, blocked_goal_id))
                connection.execute("""DELETE FROM core_goal_edges
                    WHERE source_goal_id=? AND target_goal_id=? AND relation='blocks'""",
                    (goal.id, blocked_goal_id))

    def _remember_repair_learning(self, goal: Goal, run: GoalRun,
                                  workflow_id: str, lesson: str) -> None:
        """Persist one revision-learning claim through the canonical
        workflow-learning writer, with the workflow id supplied
        explicitly (a repair goal's order is direct work, so the
        WorkflowRun lookup cannot derive it)."""
        evidence_ids = tuple(
            item.id for item in self.evidence.for_run(run.id))
        self.memory.remember(
            "workflow", lesson, evidence_ids=evidence_ids,
            goal_id=goal.id, run_id=run.id, workflow_id=workflow_id)

    @staticmethod
    def _decision_identity(decision) -> tuple | None:
        """Identity of one Decision for the stall comparison (F5): the
        same workflow for ``execute_workflow`` decisions, the same kind
        and instruction for every other kind. ``None`` never matches, so
        an unidentified decision always chains."""
        if decision is None:
            return None
        if decision.kind == "execute_workflow":
            return ("execute_workflow", decision.workflow_id)
        return (decision.kind, decision.description)

    @classmethod
    def _evaluation_park(cls, connection, goal: Goal, run: GoalRun,
                         evaluation: Evaluation) -> dict | None:
        """The stall boundary: the owner-ask payload when this evaluation
        must park instead of chaining the next run, else ``None``.

        Decision identity (F5): the metric never moved AND the trailing
        ``stall_threshold-1`` evaluated runs made the same decision this
        run made — the same workflow, or the same kind and instruction —
        so another identical run cannot move it either. Evidence alone no
        longer counts as progress; only a changed decision does. A goal
        that met its target never parks here, and the ``review_every``
        checkpoint parks first, unchanged.
        """
        config = goal.config or {}
        interval = config.get("review_every")
        if isinstance(interval, int) and interval > 0 and run.sequence % interval == 0:
            return _owner_ask(
                message=REVIEW_PARK_MESSAGE.format(name=goal.name,
                                                   sequence=run.sequence,
                                                   interval=interval),
                why=(f"run {run.sequence} of '{goal.name}' reached the goal's "
                     f"review checkpoint (every {interval} runs); the owner "
                     "asked to look at the goal at this cadence"),
                decision=PARK_DECISION,
                after=PARK_AFTER,
                goal=goal,
                machine={"goal_id": goal.id, "run_id": run.id,
                         "stage": "OBSERVE", "status": "waiting",
                         "park": "review", "review_every": interval},
                answer_syntax={"resume": f"company goal resume {goal.id}"})
        threshold = config.get("stall_threshold", 3)
        if not isinstance(threshold, int) or threshold < 2:
            threshold = 3
        value = evaluation.metrics.get(goal.metric)
        identity = cls._decision_identity(run.decision)
        rows = connection.execute("""SELECT decision_json,evaluation_json
            FROM core_runs
            WHERE goal_id=? AND sequence<? AND evaluation_json IS NOT NULL
              AND decision_json IS NOT NULL
            ORDER BY sequence DESC""", (goal.id, run.sequence)).fetchall()
        trailing: list = []
        for row in rows:
            try:
                decision_data = json.loads(row[0])
                evaluation_data = json.loads(row[1])
            except (TypeError, json.JSONDecodeError):
                break
            trailing.append((
                (evaluation_data.get("metrics") or {}).get(goal.metric),
                cls._decision_identity(Decision(**decision_data))))
            if len(trailing) >= threshold - 1:
                break
        repeated = (value is not None and identity is not None
                    and len(trailing) == threshold - 1
                    and all(metric == value and other == identity
                            for metric, other in trailing))
        if not repeated:
            return None
        return _owner_ask(
            message=STALL_PARK_MESSAGE.format(
                name=goal.name,
                progress=human_progress(goal.metric, value, goal.target),
                count=threshold),
            why=(f"'{goal.name}' has held {human_progress(goal.metric, value, goal.target)} "
                 f"across the last {threshold} evaluated runs and every one "
                 "of them made the same decision, so repeating it cannot move "
                 "the goal forward; only a changed decision chains — "
                 "evidence alone no longer counts as progress"),
            decision=PARK_DECISION,
            after=PARK_AFTER,
            goal=goal,
            machine={"goal_id": goal.id, "run_id": run.id,
                     "stage": "OBSERVE", "status": "waiting",
                     "metric": goal.metric, "operator": goal.operator,
                     "target": goal.target, "value": value,
                     "park": "stall", "stall_threshold": threshold},
            answer_syntax={"resume": f"company goal resume {goal.id}"})

    def resume(self, goal_id: str) -> dict:
        run = self.runs.current(goal_id)
        if run.status == "waiting":
            self.runs.update(run.id, status="ready")
        return self.advance(goal_id)

    def tick(self, max_advances: int = 100) -> dict:
        advanced = []
        for _ in range(max_advances):
            ready = self.runs.ready()
            if not ready:
                break
            progress = False
            for run in ready:
                before = (run.stage, run.status, run.sequence)
                try:
                    state = self.advance(run.goal_id)
                except Exception as exc:
                    # F1: one raising goal parks with durable runtime_failure
                    # evidence and one owner ask; its siblings in this tick
                    # still advance. KeyboardInterrupt and SystemExit are
                    # BaseException and pass through untouched.
                    state = self._park_runtime_failure(
                        self.goals.get(run.goal_id), run, exc)
                current = self.runs.current(run.goal_id)
                after = (current.stage, current.status, current.sequence)
                if after != before:
                    progress = True
                    advanced.append(state)
            if not progress:
                break
        return {"advanced": advanced, "quiescent": not self.runs.ready()}

    def status(self, goal_id: str) -> dict:
        goal = self.goals.get(goal_id)
        run = self.runs.current(goal_id)
        intervention = self.interventions.active_for_run(run.id)
        return {"goal": goal, "run": run, "intervention": intervention,
                "evidence": self.evidence.for_run(run.id)}

    def _context(self, goal: Goal, run: GoalRun) -> GoalContext:
        """Assemble what DECIDE reads: the goal, its run, evidence, memory,
        the goal's active children, its blockers, and the decision history
        of its last 3 decided runs. Read-only and deterministic —
        populating it never writes."""
        workflow_id = run.decision.workflow_id if run.decision else None
        with self.database.connect() as connection:
            children = tuple(
                self.goals.get(row[0]) for row in connection.execute(
                    """SELECT g.id FROM core_goals g
                       WHERE g.parent_id=? AND g.status='active'
                       ORDER BY g.created_at,g.id""", (goal.id,)))
            blockers = tuple(
                self.goals.get(row[0]) for row in connection.execute(
                    """SELECT g.id FROM core_goal_edges e
                       JOIN core_goals g ON g.id=e.source_goal_id
                       WHERE e.target_goal_id=? AND e.relation='blocks'
                         AND g.status!='complete'
                       ORDER BY g.created_at,g.id""", (goal.id,)))
            decisions = []
            for row in connection.execute(
                    """SELECT id,sequence,decision_json FROM core_runs
                       WHERE goal_id=? AND decision_json IS NOT NULL
                       ORDER BY sequence DESC LIMIT 3""", (goal.id,)):
                decision_data = json.loads(row["decision_json"])
                outcome = connection.execute(
                    """SELECT resolution_outcome FROM core_interventions
                       WHERE run_id=? ORDER BY created_at DESC,rowid DESC
                       LIMIT 1""", (row["id"],)).fetchone()
                decisions.append({
                    "sequence": row["sequence"],
                    "kind": decision_data.get("kind"),
                    "resolution_outcome": outcome[0] if outcome else None})
        return GoalContext(
            goal, run.id, tuple(self.evidence.for_goal(goal.id)),
            tuple(self.memory.relevant(goal_id=goal.id, workflow_id=workflow_id)),
            children=children, blockers=blockers,
            decisions=tuple(decisions))
