"""Manifest-bound dispatcher for the five Bureau cycle scheduler stages."""

from __future__ import annotations

import importlib
import sys
from collections.abc import Sequence
from typing import Protocol, cast

STAGE_TARGETS = {
    "discovery": ("bureau_cycle.discovery_runner", "main"),
    "curator": ("bureau_cycle.curator_runner", "main"),
    "operator": ("bureau_cycle.operator_runner", "main"),
    "verifier": ("bureau_cycle.verifier_runner", "run"),
    "closure": ("bureau.closure_runner", "main"),
}


class _StageCallable(Protocol):
    def __call__(self) -> int: ...


def run_stage(stage: str, argv: Sequence[str] = ()) -> int:
    target = STAGE_TARGETS.get(stage)
    if target is None:
        raise ValueError(f"unsupported Bureau cycle stage: {stage}")
    module_name, callable_name = target
    module = importlib.import_module(module_name)
    entrypoint = cast(_StageCallable, getattr(module, callable_name))
    previous_argv = sys.argv
    sys.argv = [module_name, *argv]
    try:
        return int(entrypoint())
    finally:
        sys.argv = previous_argv
