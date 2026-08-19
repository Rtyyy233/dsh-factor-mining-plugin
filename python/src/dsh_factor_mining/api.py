# coding=utf-8
"""In-process service API used by tests and by the JSON-RPC bridge.

This module is intentionally thin: the bridge owns transport and lifecycle,
while the domain logic stays in the data/factor/library/state modules.
"""
from __future__ import annotations

from .data.adapters import DataConfig, build_factor_env, normalize_environment, probe_file
from .factor import audit as audit_mod
from .factor.causality import check_causality
from .factor.evaluate import (
    evaluate,
    evaluate_batch,
    evaluate_composite,
    evaluate_selection,
    evaluate_test,
    evaluate_walk_forward,
)
from .library.contract import UserLibrary

__all__ = [
    "DataConfig",
    "UserLibrary",
    "audit_mod",
    "build_factor_env",
    "check_causality",
    "evaluate",
    "evaluate_batch",
    "evaluate_composite",
    "evaluate_selection",
    "evaluate_test",
    "evaluate_walk_forward",
    "normalize_environment",
    "probe_file",
]
