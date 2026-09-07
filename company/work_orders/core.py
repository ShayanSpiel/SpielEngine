from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ..state import Database


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _claimant_matches(claimed_by: str | None, executor_id: str) -> bool:
    """Exact claimant identity: the stored ``claimed_by`` string only.

    A claimant is whoever the repository recorded at claim time — always
    the order's declared agent, because only that agent can claim. No
    alias and no normalization is applied.
    """
    return claimed_by == executor_id


def _declared_executor(agent_id: str, executor_id: str) -> bool:
    """F4: an order executes only for its declared agent.

    ``agent_id`` is the declared executor identity — the workflow step's
    agent, or the goal owner for direct orders the owner assigned to
    themself (the owner completes those by construction, not by
    override). Any other identity, including the goal owner on another
    agent's order, is a foreign claimant.
    """
    return agent_id == executor_id


@dataclass(frozen=True)
class WorkOrder:
    id: str
    goal_id: str
    run_id: str
    intervention_id: str
    agent_id: str
    brief: dict[str, Any]
    status: str
    workflow_run_id: str | None = None
    step_id: str | None = None
    claimed_by: str | None = None
    claimed_at: str | None = None
    lease_expires_at: str | None = None
    attempt: int = 1
    result: dict[str, Any] | None = None


class WorkOrderRepository:
    def __init__(self, database: Database):
        self.database = database

    def open(self, *, goal_id: str, run_id: str, intervention_id: str,
             agent_id: str, brief: dict, workflow_run_id: str | None = None,
             step_id: str | None = None) -> WorkOrder:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("""SELECT id FROM core_work_orders
                WHERE intervention_id=? AND COALESCE(workflow_run_id,'')=COALESCE(?,'')
                  AND COALESCE(step_id,'')=COALESCE(?,'')
                  AND status IN ('open','claimed') ORDER BY created_at DESC LIMIT 1""",
                (intervention_id, workflow_run_id, step_id)).fetchone()
            if existing:
                order_id = existing[0]
            else:
                attempt = connection.execute("""SELECT COALESCE(MAX(attempt),0)+1
                    FROM core_work_orders WHERE intervention_id=?
                      AND COALESCE(workflow_run_id,'')=COALESCE(?,'')
                      AND COALESCE(step_id,'')=COALESCE(?,'')""",
                    (intervention_id, workflow_run_id, step_id)).fetchone()[0]
                order_id = f"work-{uuid.uuid4().hex[:12]}"
                stamp = _now()
                connection.execute("""INSERT INTO core_work_orders
                    (id,goal_id,run_id,intervention_id,workflow_run_id,agent_id,
                     step_id,brief_json,status,claimed_by,claimed_at,lease_expires_at,
                     attempt,result_json,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (order_id, goal_id, run_id, intervention_id, workflow_run_id,
                     agent_id, step_id, json.dumps(brief), "open", None, None, None,
                     attempt, None, stamp, stamp))
        return self.get(order_id)

    def get(self, order_id: str) -> WorkOrder:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM core_work_orders WHERE id=?", (order_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown work order: {order_id}")
        return WorkOrder(row["id"], row["goal_id"], row["run_id"],
                         row["intervention_id"], row["agent_id"],
                         json.loads(row["brief_json"]), row["status"],
                         row["workflow_run_id"], row["step_id"], row["claimed_by"],
                         row["claimed_at"], row["lease_expires_at"], row["attempt"],
                         json.loads(row["result_json"]) if row["result_json"] else None)

    def claim(self, order_id: str, executor_id: str, *, lease_seconds: int = 300) -> WorkOrder:
        """Claim an order; only its declared agent may.

        An open order claims for its declared agent. A claim whose lease
        expired can be re-claimed (the lease/steal behavior), but still
        only by the same declared agent — an expired lease never widens
        who may execute the order. A claim held by a foreign identity
        (only reachable by pre-F4 databases) stays untouched.
        """
        stamp = datetime.now(timezone.utc)
        expires = stamp + timedelta(seconds=lease_seconds)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT agent_id FROM core_work_orders WHERE id=?",
                (order_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown work order: {order_id}")
            if not _declared_executor(row["agent_id"], executor_id):
                raise RuntimeError(
                    f"work order is declared for agent {row['agent_id']!r}, "
                    f"not {executor_id!r}")
            changed = connection.execute("""UPDATE core_work_orders
                SET status='claimed',claimed_by=?,claimed_at=?,lease_expires_at=?,updated_at=?
                WHERE id=? AND agent_id=? AND (status='open' OR (status='claimed' AND
                  (lease_expires_at IS NULL OR lease_expires_at<=?)))""",
                (executor_id, stamp.isoformat(), expires.isoformat(), stamp.isoformat(),
                 order_id, executor_id, stamp.isoformat())).rowcount
        if changed != 1:
            current = self.get(order_id)
            if (current.status == "claimed"
                    and _claimant_matches(current.claimed_by, executor_id)):
                return current
            raise RuntimeError("work order is not open")
        return self.get(order_id)

    def renew(self, order_id: str, executor_id: str, *, lease_seconds: int = 300) -> WorkOrder:
        stamp = datetime.now(timezone.utc)
        expires = stamp + timedelta(seconds=lease_seconds)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT status,claimed_by,agent_id FROM core_work_orders WHERE id=?",
                (order_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown work order: {order_id}")
            if not _declared_executor(row["agent_id"], executor_id):
                raise RuntimeError(
                    f"work order is declared for agent {row['agent_id']!r}, "
                    f"not {executor_id!r}")
            if (row["status"] != "claimed"
                    or not _claimant_matches(row["claimed_by"], executor_id)):
                raise RuntimeError("only the claiming Agent executor can renew a WorkOrder")
            connection.execute("""UPDATE core_work_orders
                SET lease_expires_at=?,updated_at=?
                WHERE id=?""", (expires.isoformat(), stamp.isoformat(), order_id))
        return self.get(order_id)

    def complete(self, order_id: str, result: dict, *, executor_id: str) -> WorkOrder:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT status,claimed_by,agent_id FROM core_work_orders WHERE id=?",
                (order_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown work order: {order_id}")
            if not _declared_executor(row["agent_id"], executor_id):
                raise RuntimeError(
                    f"work order is declared for agent {row['agent_id']!r}, "
                    f"not {executor_id!r}")
            if (row["status"] == "claimed"
                    and _claimant_matches(row["claimed_by"], executor_id)):
                connection.execute("""UPDATE core_work_orders
                    SET status='completed',result_json=?,updated_at=? WHERE id=?""",
                    (json.dumps(result), _now(), order_id))
                return self.get(order_id)
        current = self.get(order_id)
        if current.status == "completed":
            return current
        raise RuntimeError("only the claiming Agent executor can complete a WorkOrder")

    def complete_with_evidence(self, order_id: str, result: dict, *, executor_id: str,
                               kind: str, payload: dict,
                               evidence_items: list[tuple[str, dict]] | None = None,
                               advance_workflow: bool = False,
                               wake_run: bool = False) -> tuple[WorkOrder, tuple[str, ...]]:
        """Atomically complete an order, record Evidence, and advance its execution."""

        items = evidence_items or [(kind, payload)]
        evidence_ids = tuple(f"evidence-{uuid.uuid4().hex[:12]}" for _ in items)
        stamp = _now()
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM core_work_orders WHERE id=?", (order_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown work order: {order_id}")
            if not _declared_executor(row["agent_id"], executor_id):
                raise RuntimeError(
                    f"work order is declared for agent {row['agent_id']!r}, "
                    f"not {executor_id!r}")
            if (row["status"] != "claimed"
                    or not _claimant_matches(row["claimed_by"], executor_id)):
                raise RuntimeError("only the claiming Agent executor can complete a WorkOrder")
            required_kinds = set(json.loads(row["brief_json"]).get("evidence_kinds") or ())
            supplied_kinds = {item_kind for item_kind, _ in items}
            if required_kinds and not required_kinds.issubset(supplied_kinds):
                missing = sorted(required_kinds - supplied_kinds)
                raise ValueError(
                    "WorkOrder completion is missing required Evidence: "
                    + ", ".join(missing))
            connection.execute("""UPDATE core_work_orders
                SET status='completed',result_json=?,lease_expires_at=NULL,updated_at=?
                WHERE id=?""", (json.dumps(result), stamp, order_id))
            connection.executemany("""INSERT INTO core_evidence
                (id,goal_id,run_id,intervention_id,workflow_run_id,work_order_id,
                 kind,payload_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)""", [
                    (evidence_id, row["goal_id"], row["run_id"],
                     row["intervention_id"], row["workflow_run_id"], order_id,
                     item_kind, json.dumps(item_payload), stamp)
                    for evidence_id, (item_kind, item_payload)
                    in zip(evidence_ids, items)
                ])
            if advance_workflow and row["workflow_run_id"]:
                workflow_run = connection.execute(
                    "SELECT current_step,steps_json FROM core_workflow_runs WHERE id=?",
                    (row["workflow_run_id"],)).fetchone()
                if workflow_run is None:
                    raise RuntimeError("WorkOrder WorkflowRun disappeared")
                next_step = workflow_run["current_step"] + 1
                status = ("complete" if next_step >= len(json.loads(
                    workflow_run["steps_json"])) else "running")
                connection.execute("""UPDATE core_workflow_runs
                    SET current_step=?,status=?,updated_at=? WHERE id=?""",
                    (next_step, status, stamp, row["workflow_run_id"]))
            connection.execute("""UPDATE core_notifications
                SET status='acknowledged',acknowledged_at=?
                WHERE intervention_id=? AND status='pending'""",
                (stamp, row["intervention_id"]))
            is_direct = row["workflow_run_id"] is None and row["step_id"] == "direct"
            if is_direct:
                connection.execute("""UPDATE core_interventions
                    SET status='complete',resolution_outcome='RETURN_TO_GOAL',updated_at=?
                    WHERE id=?""", (stamp, row["intervention_id"]))
                connection.execute("""UPDATE core_runs
                    SET stage='EVALUATE',status='running',updated_at=? WHERE id=?""",
                    (stamp, row["run_id"]))
            elif wake_run:
                connection.execute("""UPDATE core_runs SET status='ready',updated_at=?
                    WHERE id=? AND status='waiting'""", (stamp, row["run_id"]))
        return self.get(order_id), evidence_ids

    def fail(self, order_id: str, error: str, *, executor_id: str) -> WorkOrder:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT status,claimed_by,agent_id FROM core_work_orders WHERE id=?",
                (order_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown work order: {order_id}")
            if not _declared_executor(row["agent_id"], executor_id):
                raise RuntimeError(
                    f"work order is declared for agent {row['agent_id']!r}, "
                    f"not {executor_id!r}")
            if (row["status"] != "claimed"
                    or not _claimant_matches(row["claimed_by"], executor_id)):
                raise RuntimeError("only the claiming Agent executor can fail a WorkOrder")
            connection.execute("""UPDATE core_work_orders
                SET status='failed',result_json=?,updated_at=? WHERE id=?""",
                (json.dumps({"error": error}), _now(), order_id))
        return self.get(order_id)

    def for_workflow_run(self, workflow_run_id: str) -> list[WorkOrder]:
        with self.database.connect() as connection:
            ids = [row[0] for row in connection.execute("""SELECT id
                FROM core_work_orders WHERE workflow_run_id=? ORDER BY created_at""",
                (workflow_run_id,))]
        return [self.get(item) for item in ids]
