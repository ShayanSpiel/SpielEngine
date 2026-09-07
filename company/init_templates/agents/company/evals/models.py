"""Core model for the reusable LLM-as-judge evals Lego piece.

An eval suite is a reusable harness building block for a Department,
Workflow, mechanical gate, Connection, or Artifact: abstract, registered, and
duplicatable across Departments. One suite encodes a quality standard for one
kind of payload (e.g. campaign copy, outbound email sample) and a judge
produces a structured EvalReport that can gate a machine step.

Vocabulary (kept deliberately small):

- EvalCriterion — one check with a source-file grounding, severity block|warn.
- EvalSuite     — an ordered set of criteria for one payload_kind.
- EvalVerdict   — one judge decision for one criterion on one item.
- EvalReport    — the full, computed result for one payload.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

# JSON key used on the wire for a verdict's pass flag; the Python attribute is
# `passed` because `pass` is a reserved word.
PASS_KEY = "pass"


@dataclass(frozen=True)
class EvalCriterion:
    """One bounded quality check, grounded in a canonical source file."""

    id: str
    name: str
    description: str
    source: str  # relative path to the canonical strategy/skill source, e.g. ".agents/company/strategy/icp.md"
    severity: str = "block"  # "block" gates the item; "warn" is advisory only

    def __post_init__(self) -> None:
        if self.severity not in {"block", "warn"}:
            raise ValueError(f"criterion severity must be block or warn, got {self.severity!r}")
        if not self.id or not self.name or not self.description or not self.source:
            raise ValueError("criterion needs id, name, description, and source")


@dataclass(frozen=True)
class EvalSuite:
    """One named eval standard for one payload kind of one Department."""

    id: str
    name: str
    scope: str
    department_id: str
    payload_kind: str  # e.g. "campaign_manifest", "email_sample"
    criteria: tuple[EvalCriterion, ...]
    thresholds: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    validity: str = "business"  # evidence validity recorded with the report
    # Splits a payload into judgeable items: [(item_id, item_slice), ...].
    item_selector: Callable[[dict[str, Any]], list[tuple[str, dict[str, Any]]]] | None = None
    # Derives the stable payload_id recorded on the report (default: batch_id/id).
    payload_id_selector: Callable[[dict[str, Any]], str] | None = None

    def __post_init__(self) -> None:
        if not self.id or not self.scope or not self.department_id or not self.payload_kind:
            raise ValueError("suite needs id, scope, department_id, and payload_kind")
        if not self.criteria:
            raise ValueError(f"suite {self.id} needs at least one criterion")
        ids = [criterion.id for criterion in self.criteria]
        if len(ids) != len(set(ids)):
            raise ValueError(f"suite {self.id} has duplicate criterion ids")
        if self.validity not in {"business", "technical_only"}:
            raise ValueError(f"suite {self.id} validity must be business or technical_only")
        if len({criterion.id for criterion in self.criteria}) != len(self.criteria):
            raise ValueError(f"suite {self.id} criteria must be unique")

    def select_items(self, payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        if self.item_selector is not None:
            return list(self.item_selector(payload))
        fallback_id = str(payload.get("id") or payload.get("item_id") or "item")
        return [(fallback_id, payload)]

    def payload_id(self, payload: dict[str, Any]) -> str:
        if self.payload_id_selector is not None:
            return str(self.payload_id_selector(payload))
        return str(payload.get("batch_id") or payload.get("id") or "payload")


@dataclass(frozen=True)
class EvalVerdict:
    """One judge decision: criterion_id + pass/score/reason/evidence refs."""

    criterion_id: str
    passed: bool
    score: float  # 0.0 - 1.0
    reason: str
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.criterion_id or not self.reason:
            raise ValueError("verdict needs criterion_id and a reason")
        if not isinstance(self.passed, bool):
            raise ValueError(f"verdict {self.criterion_id} pass must be a boolean")
        try:
            score = float(self.score)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"verdict {self.criterion_id} score must be numeric") from exc
        if not 0.0 <= score <= 1.0:
            raise ValueError(f"verdict {self.criterion_id} score must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "criterion_id": self.criterion_id,
            PASS_KEY: self.passed,
            "score": round(float(self.score), 4),
            "reason": self.reason,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class EvalReport:
    """The computed result for one payload judged against one suite."""

    suite_id: str
    payload_id: str
    payload_kind: str
    per_item: dict[str, tuple[EvalVerdict, ...]]  # item_id -> verdicts (suite order)
    per_item_pass: dict[str, bool]
    overall: bool
    thresholds: dict[str, Any]
    generated_at: str
    judge_connector: str
    validity: str

    def failed_criteria(self) -> list[str]:
        """Human-readable failed-criterion labels in suite order."""
        failures: list[str] = []
        for item_id, verdicts in self.per_item.items():
            for verdict in verdicts:
                if not verdict.passed:
                    failures.append(f"{item_id}:{verdict.criterion_id}")
        return failures


def suite_spec(suite: EvalSuite) -> dict[str, Any]:
    """Stable serializable suite description (embedded in every EvalRequest)."""
    return {
        "id": suite.id,
        "name": suite.name,
        "scope": suite.scope,
        "department_id": suite.department_id,
        "payload_kind": suite.payload_kind,
        "description": suite.description,
        "validity": suite.validity,
        "thresholds": dict(suite.thresholds),
        "criteria": [
            {
                "id": criterion.id,
                "name": criterion.name,
                "description": criterion.description,
                "source": criterion.source,
                "severity": criterion.severity,
            }
            for criterion in suite.criteria
        ],
    }


def verdicts_to_dict(verdicts: tuple[EvalVerdict, ...]) -> dict[str, Any]:
    """criterion_id -> verdict dict, in suite order."""
    return {verdict.criterion_id: verdict.to_dict() for verdict in verdicts}


def report_to_evidence(report: EvalReport) -> dict[str, Any]:
    """The evidence-store payload shape for a kind=eval_report record.

    The quality gate reads: suite_id, payload_id, overall, per_item (for the
    failed-criterion attention errors), thresholds, judge_connector, validity.
    """
    return {
        "suite_id": report.suite_id,
        "payload_id": report.payload_id,
        "payload_kind": report.payload_kind,
        "overall": report.overall,
        "per_item_pass": dict(report.per_item_pass),
        "per_item": {
            item_id: verdicts_to_dict(verdicts)
            for item_id, verdicts in report.per_item.items()
        },
        "thresholds": dict(report.thresholds),
        "judge_connector": report.judge_connector,
        "validity": report.validity,
        "generated_at": report.generated_at,
    }
