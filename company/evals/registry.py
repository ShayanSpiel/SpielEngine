"""Eval suite registry with Department auto-discovery."""

from __future__ import annotations

import importlib
import pkgutil

from .. import departments as department_package
from ..runtime.registry import _search_paths
from .models import EvalSuite

_REGISTRY: dict[str, EvalSuite] = {}
_DISCOVERED = False


def register_suite(suite: EvalSuite) -> None:
    """Register one suite; duplicate ids are rejected loudly."""
    if not isinstance(suite, EvalSuite):
        raise TypeError("register_suite expects an EvalSuite")
    if suite.id in _REGISTRY:
        raise ValueError(f"eval suite '{suite.id}' is already registered")
    _REGISTRY[suite.id] = suite


def discover_suites() -> dict[str, EvalSuite]:
    """Discover suites colocated at ``departments/<id>/evals.py``."""
    global _DISCOVERED
    if _DISCOVERED:
        return _REGISTRY
    # The SAME overlay-aware package paths the runtime registry uses:
    # the live layer plus the current test overlay, rebuilt every call,
    # so eval discovery never disagrees with department discovery.
    for module_info in pkgutil.iter_modules(_search_paths()):
        if module_info.name.startswith("_"):
            continue
        module_name = f"{department_package.__name__}.{module_info.name}.evals"
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as error:
            if error.name == module_name:
                continue
            raise
        for suite in tuple(getattr(module, "EVAL_SUITES", ()) or ()):
            register_suite(suite)
    _DISCOVERED = True
    return _REGISTRY


def suites() -> dict[str, EvalSuite]:
    return discover_suites()


def get_suite(suite_id: str) -> EvalSuite:
    try:
        return discover_suites()[suite_id]
    except KeyError as exc:
        raise KeyError(
            f"unknown eval suite '{suite_id}'; registered: "
            f"{', '.join(sorted(discover_suites()))}") from exc


__all__ = ["discover_suites", "get_suite", "register_suite", "suites"]
