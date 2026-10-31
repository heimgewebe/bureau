from __future__ import annotations

import importlib
import inspect
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from .adapters import Observation


_OPERATOR_EXECUTABLE = re.compile(
    r"(?:^|\s)path=(/nix/store/[a-z0-9]{32}-[^ ;\r\n]+/bin/grabowski-operator)(?=\s|;)"
)

_TASK_WORKER = r'''
import contextlib
import io
import json
import sys

request = json.load(sys.stdin)
method = request["method"]
allowed = {
    "_probe",
    "grabowski_task_start",
    "grabowski_task_find_by_request_id",
    "grabowski_task_status",
    "grabowski_task_cancel",
    "grabowski_task_resume",
}
if method not in allowed:
    raise SystemExit("unsupported Grabowski task method")
with contextlib.redirect_stdout(io.StringIO()):
    import grabowski_tasks
    if method == "_probe":
        result = {"python": list(sys.version_info[:2])}
    else:
        function = getattr(grabowski_tasks, method, None)
        if function is None and method == "grabowski_task_find_by_request_id":
            result = None
        elif function is None:
            raise RuntimeError("Grabowski task method unavailable")
        else:
            result = function(*request["args"], **request["kwargs"])
print(json.dumps({"result": result}, ensure_ascii=False, allow_nan=False))
'''


def _active_grabowski_python() -> Path | None:
    # Follow the active NixOS service, not a stale mutable MCP release manifest.
    try:
        result = subprocess.run(
            [
                "systemctl", "show", "grabowski-operator.service",
                "-p", "ActiveState", "-p", "ExecStart",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode or "ActiveState=active" not in result.stdout.splitlines():
        return None
    match = _OPERATOR_EXECUTABLE.search(result.stdout)
    if match is None:
        return None
    interpreter = Path(match.group(1)).with_name("python3")
    return interpreter if interpreter.is_file() else None


class _NixTasksBridge:
    # The active Grabowski Nix Python owns its native extension ABI.
    def __init__(self, interpreter: Path) -> None:
        self.interpreter = interpreter
        self._call("_probe")

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        payload = json.dumps(
            {"method": method, "args": args, "kwargs": kwargs},
            ensure_ascii=False,
            allow_nan=False,
        )
        try:
            completed = subprocess.run(
                [str(self.interpreter), "-I", "-B", "-c", _TASK_WORKER],
                input=payload,
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                "Grabowski task call timed out; outcome unknown, reconcile before retry"
            ) from exc
        except OSError as exc:
            raise RuntimeError("Grabowski task interpreter unavailable") from exc
        if completed.returncode:
            raise RuntimeError(
                "Grabowski task call failed in its native Python runtime: "
                + completed.stderr.strip()[-600:]
            )
        if len(completed.stdout) > 8 * 1024 * 1024:
            raise RuntimeError("Grabowski task response exceeded size limit")
        try:
            response = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "Grabowski task response was not JSON; reconcile before retry"
            ) from exc
        if not isinstance(response, dict) or set(response) != {"result"}:
            raise RuntimeError("Grabowski task response contract invalid")
        return response["result"]

    def grabowski_task_start(self, **kwargs: Any) -> dict[str, Any]:
        return self._call("grabowski_task_start", **kwargs)

    def grabowski_task_find_by_request_id(self, request_id: str) -> Any:
        return self._call("grabowski_task_find_by_request_id", request_id)

    def grabowski_task_status(self, task_id: str) -> dict[str, Any]:
        return self._call("grabowski_task_status", task_id)

    def grabowski_task_cancel(self, task_id: str) -> dict[str, Any]:
        return self._call("grabowski_task_cancel", task_id)

    def grabowski_task_resume(self, task_id: str) -> dict[str, Any]:
        return self._call("grabowski_task_resume", task_id)


class GrabowskiTaskAdapter:
    """Adapter for Grabowski's persistent task API, with native Python isolation."""

    system = "grabowski-task"
    aliases = ("grabowski-job",)

    def __init__(
        self, source_root: Path | None = None, *, prefer_live_operator: bool = False
    ):
        interpreter = _active_grabowski_python() if prefer_live_operator else None
        if interpreter is not None:
            self.tasks = _NixTasksBridge(interpreter)
            return
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
            if key in parameters and key in request:
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
