"""Judge connector abstraction for the evals Lego piece.

A JudgeConnector is the pluggable seam between the eval engine and whoever
supplies verdicts.  AgentJudgeConnector (the shipped default) is honest in
this harness: it renders a structured EvalRequest (criterion text +
payload excerpts), the calling agent supplies the verdicts, the connector
validates the shape, and the engine computes the report.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .models import EvalSuite


@runtime_checkable
class JudgeConnector(Protocol):
    """The pluggable verdict-supply seam used by the eval engine."""

    id: str

    def render_request(self, suite: EvalSuite, payload: dict[str, Any]) -> dict[str, Any]:
        """Structured, judge-readable rendering of the suite + payload items."""
        ...

    def validate(self, suite: EvalSuite, payload: dict[str, Any],
                 verdicts_raw: dict[str, Any]) -> list[str]:
        """Return shape errors for a raw verdict document (empty = valid)."""
        ...


class AgentJudgeConnector:
    """Default connector: the calling agent is the honest judge.

    The connector renders the request, validates the supplied verdicts, and
    delegates report computation to the engine (run_suite).  It never invents
    verdicts itself.
    """

    id = "agent:cli"

    def render_request(self, suite: EvalSuite, payload: dict[str, Any]) -> dict[str, Any]:
        from .engine import render_request

        return render_request(suite, payload)

    def validate(self, suite: EvalSuite, payload: dict[str, Any],
                 verdicts_raw: dict[str, Any]) -> list[str]:
        from .engine import validate_verdicts

        return validate_verdicts(suite, payload, verdicts_raw)
