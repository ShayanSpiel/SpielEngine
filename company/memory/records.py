from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from ..evidence import EvidenceRepository
from ..state import Database


@dataclass(frozen=True)
class Memory:
    id: str
    scope: str
    claim: str
    evidence_ids: tuple[str, ...]
    goal_id: str | None = None
    run_id: str | None = None
    intervention_id: str | None = None
    workflow_id: str | None = None
    confidence: float = 1.0
    status: str = "active"
    supersedes_id: str | None = None


class MemoryRepository:
    def __init__(self, database: Database, evidence: EvidenceRepository):
        self.database = database
        self.evidence = evidence

    def remember(self, scope: str, claim: str, *, evidence_ids=(), goal_id=None,
                 run_id=None, intervention_id=None, workflow_id=None,
                 confidence: float = 1.0, supersedes_id: str | None = None) -> Memory:
        if scope not in {"owner", "workflow", "strategy"}:
            raise ValueError(f"invalid memory scope: {scope}")
        ids = tuple(dict.fromkeys(evidence_ids))
        if scope != "owner" and not ids:
            raise ValueError(f"{scope} memory requires evidence")
        if scope != "owner" and (not goal_id or not run_id):
            raise ValueError(f"{scope} memory requires Goal and Run lineage")
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("memory confidence must be between 0.0 and 1.0")
        superseded = self.get(supersedes_id) if supersedes_id else None
        if superseded is not None and superseded.scope != scope:
            raise ValueError("memory can supersede only the same scope")
        records = [self.evidence.get(item) for item in ids]
        if goal_id and any(item.goal_id != goal_id for item in records):
            raise ValueError("memory evidence must belong to its causal Goal")
        if run_id and any(item.run_id != run_id for item in records):
            raise ValueError("memory evidence must belong to its causal run")
        if intervention_id and any(
                item.intervention_id != intervention_id for item in records):
            raise ValueError("memory evidence must belong to its causal Intervention")
        memory_id = f"memory-{uuid.uuid4().hex[:12]}"
        with self.database.connect() as connection:
            if superseded is not None:
                connection.execute("""UPDATE core_memory SET status='superseded'
                    WHERE id=? AND status='active'""", (superseded.id,))
            connection.execute("""INSERT INTO core_memory
                (id,scope,claim,goal_id,run_id,intervention_id,workflow_id,
                 evidence_ids_json,created_at,confidence,status,supersedes_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (memory_id, scope, claim, goal_id, run_id, intervention_id,
                 workflow_id, json.dumps(ids), datetime.now(timezone.utc).isoformat(),
                 confidence, "active", supersedes_id))
        return self.get(memory_id)

    def get(self, memory_id: str) -> Memory:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM core_memory WHERE id=?", (memory_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown memory: {memory_id}")
        return Memory(row["id"], row["scope"], row["claim"],
                      tuple(json.loads(row["evidence_ids_json"])), row["goal_id"],
                      row["run_id"], row["intervention_id"], row["workflow_id"],
                      row["confidence"], row["status"], row["supersedes_id"])

    def retire(self, memory_id: str) -> Memory:
        """Retire one claim: flip it out of the active set for good.

        Memory hygiene (L4): a claim that is an architecture invariant
        owned by code and docs — not reusable operational learning — is
        retired, never deleted. The row and its evidence stay exactly as
        recorded, and ``relevant()`` stops returning it (it selects
        ``status='active'`` only).

        One writer, one status flip, no schema change: the existing
        ``status`` column admits exactly two values ('active' and
        'superseded'), so retiring reuses 'superseded' — the non-active
        value the schema already owns. A retired claim is distinguished
        from a genuine supersession chain by carrying no successor row:
        nothing points at it with ``supersedes_id``.
        """
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT status FROM core_memory WHERE id=?", (memory_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown memory: {memory_id}")
            connection.execute(
                "UPDATE core_memory SET status='superseded' "
                "WHERE id=? AND status='active'", (memory_id,))
        return self.get(memory_id)

    def relevant(self, *, limit: int = 20, scope: str | None = None,
                 goal_id: str | None = None,
                 workflow_id: str | None = None) -> list[Memory]:
        """Active memory claims that apply to one Goal or Workflow.

        Owner claims always apply. Strategy claims apply to their own
        Goal and — the goal topology — to every Goal it is structurally
        related to: siblings (same owner and metric), its parent, its
        children, and both directions of a ``supports`` edge. So one
        Goal's evidenced learning informs the Goals it shares structure
        with, while unrelated Goals (no edge, different owner and
        metric, no parent link) receive nothing. Workflow claims apply
        only with their workflow_id. Explicit SQL joins only — no
        embeddings, no clustering.
        """
        if limit < 1:
            return []
        if scope is not None and scope not in {"owner", "workflow", "strategy"}:
            raise ValueError(f"invalid memory scope: {scope}")
        clauses, values = ["status='active'"], []
        applicable = ["scope='owner'"]
        if goal_id:
            # The strategy-scope set: this Goal's own claims plus the
            # active strategy claims of structurally related Goals —
            # siblings (same owner_id and metric, joined through
            # core_goals/core_goal_metadata), the parent, the children,
            # and supports-related Goals in both directions
            # (core_goal_edges relation='supports').
            applicable.append("""(scope='strategy' AND (
                goal_id=? OR goal_id IN (
                    SELECT sibling.id FROM core_goals sibling
                    JOIN core_goal_metadata sib ON sib.goal_id=sibling.id
                    JOIN core_goal_metadata focus ON focus.goal_id=?
                    WHERE sibling.metric=(SELECT metric FROM core_goals
                                           WHERE id=?)
                      AND sib.owner_id=focus.owner_id)
                OR goal_id IN (SELECT parent_id FROM core_goals
                               WHERE id=? AND parent_id IS NOT NULL)
                OR goal_id IN (SELECT id FROM core_goals WHERE parent_id=?)
                OR goal_id IN (SELECT source_goal_id FROM core_goal_edges
                               WHERE target_goal_id=? AND relation='supports')
                OR goal_id IN (SELECT target_goal_id FROM core_goal_edges
                               WHERE source_goal_id=? AND relation='supports')))""")
            values.extend([goal_id] * 7)
        if workflow_id:
            applicable.append("(scope='workflow' AND workflow_id=?)")
            values.append(workflow_id)
        clauses.append("(" + " OR ".join(applicable) + ")")
        if scope:
            clauses.append("scope=?")
            values.append(scope)
        values.append(limit)
        with self.database.connect() as connection:
            ids = [row[0] for row in connection.execute(
                """SELECT id FROM core_memory WHERE """ + " AND ".join(clauses) + """
                ORDER BY CASE scope WHEN 'owner' THEN 0 WHEN 'workflow' THEN 1 ELSE 2 END,
                         created_at DESC LIMIT ?""", values)]
        return [self.get(item) for item in ids]
