from __future__ import annotations

import importlib
import inspect
import os
import sys
from pathlib import Path
from typing import Any

from .adapters import Observation


class GrabowskiTaskAdapter:
    """In-process adapter for Grabowski's persistent task API."""

    system = "grabowski-task"

    def __init__(self, source_root: Path | None = None):
        configured = os.environ.get("BUREAU_GRABOWSKI_SRC")
        candidate = source_root or (Path(configured) if configured else None)
        if candidate is None:
            default = Path.home() / "repos/grabowski/src"
            candidate = default if default.is_dir() else None
        if candidate is not None:
            resolved = str(candidate.expanduser().resolve())
            if resolved not in sys.path:
                sys.path.insert(0, resolved)
        self.tasks = importlib.import_module("grabowski_tasks")

    @staticmethod
    def _record(value: dict[str, Any]) -> dict[str, Any]:
        nested = value.get("task")
        return nested if isinstance(nested, dict) else value

    def dispatch(self, request: dict[str, Any]) -> str:
        start = self.tasks.grabowski_task_start
        kwargs: dict[str, Any] = {
            "host": request["host"],
            "argv": list(request["argv"]),
            "cwd": request.get("cwd"),
            "runtime_seconds": int(request.get("runtime_seconds", 7200)),
            "resume_policy": request.get("resume_policy", "verify-then-retry"),
            "cpu_weight": int(request.get("cpu_weight", 100)),
            "io_weight": int(request.get("io_weight", 100)),
            "memory_max_bytes": request.get("memory_max_bytes"),
            "resource_keys": list(request.get("resource_keys", [])),
        }
        parameters = inspect.signature(start).parameters
        for key in ("origin_ref", "request_id"):
            if key in parameters:
                kwargs[key] = request[key]
        result = self._record(start(**kwargs))
        task_id = result.get("task_id") or result.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise RuntimeError("Grabowski did not return a task id")
        return task_id

    def recover(self, request_id: str) -> str | None:
        finder = getattr(self.tasks, "grabowski_task_find_by_request_id", None)
        if finder is None:
            return None
        result = finder(request_id)
        if result is None:
            return None
        record = self._record(result)
        task_id = record.get("task_id") or record.get("id")
        return task_id if isinstance(task_id, str) and task_id else None

    def cancel(self, external_id: str) -> dict[str, Any]:
        return self._record(self.tasks.grabowski_task_cancel(external_id))

    def resume(self, external_id: str) -> dict[str, Any]:
        return self._record(self.tasks.grabowski_task_resume(external_id))

    def observe(self, external_id: str) -> Observation:
        record = self._record(self.tasks.grabowski_task_status(external_id))
        state = str(record.get("state", "unknown"))
        mapped = {
            "launching": "running",
            "running": "running",
            "completed": "succeeded",
            "failed": "failed",
            "cancelled": "cancelled",
            "interrupted": "interrupted",
        }.get(state, "unknown")
        return Observation(mapped, record)
