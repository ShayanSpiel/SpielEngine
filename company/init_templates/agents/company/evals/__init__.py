"""Reusable evals (LLM-as-judge) for Department quality gates.

One eval suite encodes a quality standard for one payload kind; a judge
supplies structured verdicts; the engine computes an EvalReport that machine
steps (e.g. the content-campaign quality_gate) require as evidence before a
campaign can advance.

Public surface:

- models: EvalCriterion, EvalSuite, EvalVerdict, EvalReport
- judge:  AgentJudgeConnector (honest default), JudgeConnector (protocol)
- engine: run_suite, render_request, validate_verdicts, compute_report
- registry: register_suite, discover_suites, suites, get_suite
- serialization: suite_spec, report_to_evidence
"""

from .engine import (
    compute_report,
    render_request,
    run_suite,
    validate_verdicts,
)
from .judge import AgentJudgeConnector, JudgeConnector
from .models import (
    EvalCriterion,
    EvalReport,
    EvalSuite,
    EvalVerdict,
    report_to_evidence,
    suite_spec,
)
from .registry import discover_suites, get_suite, register_suite, suites

__all__ = [
    "AgentJudgeConnector",
    "EvalCriterion",
    "EvalReport",
    "EvalSuite",
    "EvalVerdict",
    "JudgeConnector",
    "compute_report",
    "discover_suites",
    "get_suite",
    "register_suite",
    "render_request",
    "report_to_evidence",
    "run_suite",
    "suite_spec",
    "suites",
    "validate_verdicts",
]
