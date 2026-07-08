from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

TERMINAL_STATES = {"verified", "cancelled", "superseded"}
COMPLETION_STATES = {"verified"}
SATISFIED_STATUSES = {"satisfied", "implemented", "closed", "verified"}


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _task_documents(root: Path) -> dict[str, dict[str, Any]]:
    task_dir = root / "registry" / "tasks"
    result: dict[str, dict[str, Any]] = {}
    for path in sorted(task_dir.glob("*.json")):
        raw = _load_json(path)
        task_id = raw.get("id")
        if isinstance(task_id, str):
            result[task_id] = raw
    return result


def _queued_tasks(root: Path) -> set[str]:
    queue_path = root / "registry" / "queue.json"
    if not queue_path.exists():
        return set()
    raw = _load_json(queue_path)
    queued: set[str] = set()
    lanes = raw.get("lanes", {})
    if isinstance(lanes, dict):
        for values in lanes.values():
            if isinstance(values, list):
                queued.update(item for item in values if isinstance(item, str))
    return queued


def _registry_truth(task: dict[str, Any]) -> dict[str, Any]:
    metadata = task.get("metadata", {})
    if not isinstance(metadata, dict):
        return {}
    truth = metadata.get("registry_truth", {})
    return truth if isinstance(truth, dict) else {}


def _has_closure_evidence(truth: dict[str, Any]) -> bool:
    evidence = truth.get("evidence")
    return isinstance(evidence, list) and any(isinstance(item, dict) for item in evidence)


def _baseline_commit_status(repository: str, commit: str) -> tuple[str, str | None]:
    repo_path = Path(repository).expanduser()
    if not repo_path.exists():
        return "unknown", "working_repository_missing"
    if not repo_path.is_dir():
        return "unknown", "working_repository_not_directory"
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_path), "cat-file", "-e", f"{commit}^{{commit}}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "unknown", f"probe_error:{type(exc).__name__}"
    return ("present", None) if completed.returncode == 0 else ("missing", "commit_not_found")


def registry_truth_diagnostics(
    root: str | Path,
    *,
    probe_baselines: bool = True,
) -> dict[str, Any]:
    """Return read-only registry-vs-evidence diagnostics.

    This deliberately separates hard evidence/state contradictions from stale
    metadata warnings. A dead baseline on planned work is actionable context; a
    dead baseline on verified work is a hard freshness contradiction because the
    stored completion evidence can no longer be replayed from the declared base.
    """

    root_path = Path(root)
    tasks = _task_documents(root_path)
    queued = _queued_tasks(root_path)
    findings: list[dict[str, Any]] = []

    for task_id, task in sorted(tasks.items()):
        state = task.get("state")
        truth = _registry_truth(task)
        truth_status = truth.get("status")
        if truth_status in SATISFIED_STATUSES:
            if state not in TERMINAL_STATES:
                findings.append(
                    {
                        "severity": "error",
                        "issue": "satisfied_task_non_terminal",
                        "task_id": task_id,
                        "state": state,
                        "registry_truth_status": truth_status,
                    }
                )
            if task_id in queued:
                findings.append(
                    {
                        "severity": "error",
                        "issue": "satisfied_task_still_queued",
                        "task_id": task_id,
                        "state": state,
                        "registry_truth_status": truth_status,
                    }
                )
            if not _has_closure_evidence(truth):
                findings.append(
                    {
                        "severity": "warning",
                        "issue": "registry_truth_without_machine_evidence",
                        "task_id": task_id,
                        "state": state,
                        "registry_truth_status": truth_status,
                    }
                )

        execution = task.get("execution", {})
        if probe_baselines and isinstance(execution, dict):
            baseline_commit = execution.get("baseline_commit")
            working_repository = execution.get("working_repository") or execution.get("cwd")
            if isinstance(baseline_commit, str) and isinstance(working_repository, str):
                status, reason = _baseline_commit_status(working_repository, baseline_commit)
                if status != "present":
                    findings.append(
                        {
                            "severity": "error"
                            if state in COMPLETION_STATES
                            else "warning",
                            "issue": "baseline_commit_not_present",
                            "task_id": task_id,
                            "state": state,
                            "working_repository": working_repository,
                            "baseline_commit": baseline_commit,
                            "baseline_status": status,
                            "reason": reason,
                        }
                    )

    errors = [item for item in findings if item.get("severity") == "error"]
    warnings = [item for item in findings if item.get("severity") == "warning"]
    return {
        "schema_version": 1,
        "healthy": not errors,
        "root": str(root_path),
        "counts": {
            "tasks": len(tasks),
            "queued_tasks": len(queued),
            "findings": len(findings),
            "errors": len(errors),
            "warnings": len(warnings),
        },
        "findings": findings,
        "errors": errors,
        "warnings": warnings,
        "does_not_establish": [
            "runtime_correctness",
            "task_completion_without_evidence",
            "merge_readiness",
            "test_sufficiency",
        ],
    }
