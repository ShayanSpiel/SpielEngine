from __future__ import annotations

import json

from ..state import Database


class Observer:
    def __init__(self, database: Database):
        self.database = database

    def health(self) -> dict:
        with self.database.connect() as connection:
            def count(table: str, where: str = "") -> int:
                return connection.execute(
                    f"SELECT COUNT(*) FROM {table} {where}").fetchone()[0]
            return {
                "goals": count("core_goals"),
                "active_goals": count("core_goals", "WHERE status='active'"),
                "runs": count("core_runs"),
                "active_interventions": count(
                    "core_interventions", "WHERE status IN ('running','waiting')"),
                "open_work_orders": count(
                    "core_work_orders", "WHERE status IN ('open','claimed')"),
                "evidence": count("core_evidence"),
                "memory": count("core_memory"),
            }

    def trace(self, goal_id: str) -> dict:
        """Explain why execution happened by walking Goal → Run → Intervention."""
        with self.database.connect() as connection:
            goal = connection.execute(
                "SELECT * FROM core_goals WHERE id=?", (goal_id,)).fetchone()
            if goal is None:
                raise KeyError(f"unknown goal: {goal_id}")
            runs = connection.execute(
                "SELECT * FROM core_runs WHERE goal_id=? ORDER BY sequence", (goal_id,)
            ).fetchall()
            values = []
            for run in runs:
                interventions = connection.execute(
                    "SELECT * FROM core_interventions WHERE run_id=? ORDER BY created_at",
                    (run["id"],),
                ).fetchall()
                values.append({
                    "id": run["id"], "sequence": run["sequence"],
                    "stage": run["stage"], "status": run["status"],
                    "interventions": [{
                        "id": item["id"], "kind": item["kind"],
                        "description": item["description"], "status": item["status"],
                        "outcome": item["resolution_outcome"],
                        "context": json.loads(item["context_json"]),
                    } for item in interventions],
                })
        return {"goal": {"id": goal["id"], "name": goal["name"],
                         "status": goal["status"]}, "runs": values}

    def dashboard(self) -> dict:
        """One read-only company dashboard projection.

        Health counters, per-goal progress with stage/run/work-order state,
        pending attention, and workflow step positions — everything an owner
        or Director needs to see where the company stands, derived only
        from clean core tables.
        """
        with self.database.connect() as connection:
            goals = [dict(row) for row in connection.execute("""
                SELECT g.id, g.name, g.status, g.metric, g.target_json,
                       m.owner_id,
                       (SELECT COUNT(*) FROM core_evidence e
                         WHERE e.goal_id = g.id) AS evidence_count,
                       (SELECT COUNT(*) FROM core_work_orders w
                         WHERE w.goal_id = g.id
                           AND w.status IN ('open','claimed')) AS open_orders
                FROM core_goals g
                LEFT JOIN core_goal_metadata m ON m.goal_id = g.id
                ORDER BY CASE g.status WHEN 'active' THEN 0
                         WHEN 'paused' THEN 1 ELSE 2 END, g.created_at""")]
            for goal in goals:
                run = connection.execute("""SELECT id, sequence, stage,
                    status FROM core_runs WHERE goal_id=?
                    ORDER BY sequence DESC LIMIT 1""", (goal["id"],)).fetchone()
                if run is not None:
                    goal["run_id"] = run["id"]
                    goal["run_sequence"] = run["sequence"]
                    goal["stage"] = run["stage"]
                    goal["run_status"] = run["status"]
                    workflow_run = connection.execute("""
                        SELECT wr.id, wr.workflow_id, wr.current_step, wr.status,
                               (SELECT COUNT(*) FROM core_work_orders w
                                 WHERE w.workflow_run_id = wr.id) AS orders,
                               (SELECT steps_json FROM core_workflows f
                                 WHERE f.id = wr.workflow_id) AS steps_json
                        FROM core_workflow_runs wr WHERE wr.run_id=?
                        ORDER BY wr.created_at DESC LIMIT 1""", (run["id"],)).fetchone()
                    if workflow_run is not None:
                        goal["workflow_id"] = workflow_run["workflow_id"]
                        goal["workflow_status"] = workflow_run["status"]
                        goal["workflow_orders"] = workflow_run["orders"]
                        steps = json.loads(workflow_run["steps_json"] or "[]")
                        if steps:
                            goal["workflow_step"] = workflow_run["current_step"] + 1
                            goal["workflow_steps_total"] = len(steps)
            attention = [dict(row) for row in connection.execute("""
                SELECT id, goal_id, kind, payload_json, created_at
                FROM core_notifications WHERE status='pending'
                ORDER BY created_at LIMIT 20""")]
            for item in attention:
                item["payload"] = json.loads(item.pop("payload_json"))
            memory = {row[0]: row[1] for row in connection.execute("""
                SELECT scope, COUNT(*) FROM core_memory
                WHERE status='active' GROUP BY scope""")}
            # L3 repetition signal: the same bounded direct work executed
            # over and over is the strongest hint the work merits a
            # reusable Workflow. Three or more completed direct orders on
            # one goal, by the same agent, with the same instruction,
            # surface as one bounded entry (newest shapes first, at most
            # five). Read-only projection — no clustering, no embeddings.
            shapes: dict[tuple[str, str, str], int] = {}
            for row in connection.execute("""SELECT goal_id, agent_id, brief_json
                FROM core_work_orders
                WHERE step_id='direct' AND status='completed'"""):
                instruction = " ".join(
                    (json.loads(row["brief_json"]).get("instruction")
                     or "").split())
                if not instruction:
                    continue
                shape = (row["goal_id"], row["agent_id"], instruction)
                shapes[shape] = shapes.get(shape, 0) + 1
            repetition = [
                {"goal_id": goal_id, "agent_id": agent_id,
                 "completed_orders": count, "instruction": instruction,
                 "suggestion": "three similar direct WorkOrders — this work "
                               "may merit a reusable Workflow proposed "
                               "through adoption"}
                for (goal_id, agent_id, instruction), count
                in sorted(shapes.items(), key=lambda item: (-item[1], item[0]))
                if count >= 3][:5]
        return {"health": self.health(), "goals": goals,
                "attention": attention, "memory": memory,
                "repetition": repetition}
