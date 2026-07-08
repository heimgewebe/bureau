from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

TERMINAL_STATES = {"verified", "cancelled", "superseded"}
COMPLETION_STATES = {"verified"}
SATISFIED_STATUSES = {"satisfied", "implemented", "closed", "verified"}
AI_SUMMARY_KINDS = {"ai_summary", "llm_summary", "prose_summary", "summary"}
HASH_BINDING_KEYS = {
    "sha256",
    "content_sha256",
    "diff_sha256",
    "evidence_sha256",
    "receipt_sha256",
    "task_sha256",
    "plan_sha256",
    "command_sha256",
}
COMMIT_BINDING_KEYS = {
    "commit",
    "commit_sha",
    "git_commit",
    "head_sha",
    "merge_commit",
    "merge_commit_sha",
}
SOURCE_REFERENCE_KEYS = {
    "authority",
    "source_authority",
    "tool",
    "system",
    "repository",
    "path",
    "file",
    "url",
    "source_ref",
    "command",
    "run_id",
    "receipt_id",
    "workflow_run_id",
    "implementation_pr",
    "implementation_prs",
    "pull_request",
    "pull_requests",
    "pr",
    "number",
}
HEXISH_RE = re.compile(r"^[0-9a-f]{12,64}$", re.IGNORECASE)


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


def _metadata(task: dict[str, Any]) -> dict[str, Any]:
    metadata = task.get("metadata", {})
    return metadata if isinstance(metadata, dict) else {}


def _registry_truth(task: dict[str, Any]) -> dict[str, Any]:
    truth = _metadata(task).get("registry_truth", {})
    return truth if isinstance(truth, dict) else {}


def _walk_dicts(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk_dicts(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _walk_dicts(item)


def _has_named_key(value: Any, keys: set[str]) -> bool:
    for item in _walk_dicts(value):
        if any(str(key) in keys for key in item):
            return True
    return False


def _hash_bound_value(value: Any) -> bool:
    if isinstance(value, str):
        return bool(HEXISH_RE.fullmatch(value))
    if isinstance(value, list | tuple):
        return any(_hash_bound_value(item) for item in value)
    if isinstance(value, dict):
        for key, item in value.items():
            name = str(key)
            if name in HASH_BINDING_KEYS and item not in (None, "", [], {}):
                return True
            if name in COMMIT_BINDING_KEYS and _hash_bound_value(item):
                return True
            if _hash_bound_value(item):
                return True
    return False


def _has_verification_hash_binding(metadata: dict[str, Any]) -> bool:
    verification = metadata.get("verification")
    if not isinstance(verification, dict):
        return False
    return all(isinstance(verification.get(key), str) and verification[key].strip() for key in (
        "task_sha256",
        "plan_sha256",
    ))


def _registry_truth_evidence(truth: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = truth.get("evidence")
    if not isinstance(evidence, list):
        return []
    return [item for item in evidence if isinstance(item, dict)]


def _evidence_item_has_type(item: dict[str, Any]) -> bool:
    kind = item.get("kind") or item.get("type")
    return isinstance(kind, str) and bool(kind.strip())


def _evidence_item_is_ai_summary(item: dict[str, Any]) -> bool:
    kind = item.get("kind") or item.get("type")
    return isinstance(kind, str) and kind.strip().lower() in AI_SUMMARY_KINDS


def _evidence_item_has_task_or_pr_binding(item: dict[str, Any], task_id: str) -> bool:
    kind = str(item.get("kind") or item.get("type") or "").lower()
    if kind in {"pull_request", "github_pull_request", "pr"}:
        has_pr = any(key in item for key in ("number", "pull_request", "url", "pr"))
        task_values = item.get("task_id") or item.get("task_ids") or item.get("task_binding")
        return has_pr and task_values not in (None, "", [], {})
    if "task" in kind:
        return item.get("task_id") in {task_id, None} or item.get("task_id") not in ("", [])
    return True


def _strong_evidence_item(item: dict[str, Any], task_id: str) -> bool:
    return (
        _evidence_item_has_type(item)
        and not _evidence_item_is_ai_summary(item)
        and _has_named_key(item, SOURCE_REFERENCE_KEYS)
        and _hash_bound_value(item)
        and _evidence_item_has_task_or_pr_binding(item, task_id)
    )


def _has_machine_closeout_evidence(
    task_id: str,
    metadata: dict[str, Any],
    truth: dict[str, Any],
) -> bool:
    if any(_strong_evidence_item(item, task_id) for item in _registry_truth_evidence(truth)):
        return True
    # Legacy closeouts often record implementation PRs and verification hashes
    # directly in metadata rather than under metadata.registry_truth.evidence.
    # Treat that as machine-readable enough, but not as runtime truth.
    has_source_ref = _has_named_key(metadata, SOURCE_REFERENCE_KEYS)
    has_hash_binding = _has_verification_hash_binding(metadata) or _hash_bound_value(metadata)
    return has_source_ref and has_hash_binding


def _has_ai_summary_only_evidence(truth: dict[str, Any]) -> bool:
    evidence = _registry_truth_evidence(truth)
    return bool(evidence) and not any(not _evidence_item_is_ai_summary(item) for item in evidence)


def _has_closure_evidence(task_id: str, metadata: dict[str, Any], truth: dict[str, Any]) -> bool:
    return _has_machine_closeout_evidence(task_id, metadata, truth)


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


def _verified_closeout_findings(
    task_id: str,
    task: dict[str, Any],
    metadata: dict[str, Any],
    truth: dict[str, Any],
    queued: set[str],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    state = task.get("state")
    if state not in TERMINAL_STATES:
        return findings
    if task_id in queued:
        findings.append(
            {
                "severity": "error",
                "issue": "terminal_task_still_queued",
                "task_id": task_id,
                "state": state,
            }
        )
    if state != "verified":
        return findings
    if not _has_verification_hash_binding(metadata):
        findings.append(
            {
                "severity": "error",
                "issue": "verified_task_without_hash_binding",
                "task_id": task_id,
                "state": state,
            }
        )
    if _has_ai_summary_only_evidence(truth):
        findings.append(
            {
                "severity": "error",
                "issue": "verified_task_ai_summary_only_evidence",
                "task_id": task_id,
                "state": state,
            }
        )
    if not _has_machine_closeout_evidence(task_id, metadata, truth):
        findings.append(
            {
                "severity": "error",
                "issue": "verified_task_without_machine_closeout_evidence",
                "task_id": task_id,
                "state": state,
            }
        )
    return findings


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
        metadata = _metadata(task)
        truth = _registry_truth(task)
        truth_status = truth.get("status")
        findings.extend(_verified_closeout_findings(task_id, task, metadata, truth, queued))
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
            if not _has_closure_evidence(task_id, metadata, truth):
                findings.append(
                    {
                        "severity": "error" if state in COMPLETION_STATES else "warning",
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
