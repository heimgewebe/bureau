"""Read-only Bureau status projection board.

Implements BUR-2026-005-T004: one JSON surface that combines registry state,
runtime state, workspaces, receipts and GitHub observations per task. The
projection only reads. Unknown stays unknown, stale stays stale, blocked stays
blocked; nothing here verifies tasks, mutates the queue, merges or cleans up.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .github_observer import (
    BINDING_AMBIGUOUS,
    CI_UNKNOWN,
    observation_is_stale,
)
from .v2 import (
    Registry,
    _read_only_overlays,
    _read_only_state_rows,
    _runtime_state_db_path,
)

STATUS_PROJECTION_SCHEMA_VERSION = 1

ACTIVE_RUN_STATES = {"assigned", "running", "verifying"}

DEFAULT_GITHUB_MAX_AGE_SECONDS = 3600

PROJECTION_DOES_NOT_ESTABLISH = (
    "task_completion",
    "merge_readiness",
    "ci_sufficiency",
    "runtime_correctness",
    "security_correctness",
    "automatic_merge_authority",
    "automatic_completion_authority",
    "dispatcher_authority",
)

GITHUB_FIELDS = (
    "binding",
    "confidence",
    "number",
    "url",
    "state",
    "is_draft",
    "head_ref",
    "head_sha",
    "base_ref",
    "checks",
    "review_decision",
    "review_blocked",
    "merge_state",
    "ambiguous_reason",
    "observed_at",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_workspaces(state_path: Path) -> dict[str, dict[str, Any]]:
    if not state_path.is_file():
        return {}
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{state_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT * FROM workspaces").fetchall()
    except sqlite3.Error:
        return {}
    finally:
        if connection is not None:
            connection.close()
    return {row["run_id"]: dict(row) for row in rows}


def _public_run(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": row.get("run_id"),
        "task_id": row.get("task_id"),
        "worker": row.get("worker_id"),
        "state": row.get("state"),
        "heartbeat_at": row.get("heartbeat_at"),
        "external_system": row.get("external_system"),
        "external_id": row.get("external_id"),
        "external_state": row.get("external_state"),
        "external_observed_at": row.get("external_observed_at"),
        "workspace_path": row.get("workspace_path"),
        "workspace_branch": row.get("workspace_branch"),
    }


def _public_workspace(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": row.get("run_id"),
        "workspace_path": row.get("workspace_path"),
        "branch": row.get("branch"),
        "baseline_commit": row.get("baseline_commit"),
        "state": row.get("state"),
        "updated_at": row.get("updated_at"),
    }


def _public_receipt(run_id: str, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "receipt_sha256": row.get("receipt_sha256"),
        "created_at": row.get("created_at"),
        "establishes": "run evidence only, not task completion",
    }


def _public_github(observation: dict[str, Any]) -> dict[str, Any]:
    return {field: observation.get(field) for field in GITHUB_FIELDS}


def _queue_lane(registry: Registry, task_id: str) -> str | None:
    for lane, task_ids in registry.queue.items():
        if task_id in task_ids:
            return lane
    return None


def status_projection(
    root: Path,
    *,
    registry: Registry | None = None,
    state_db: Path | None = None,
    state_root: Path | None = None,
    github: dict[str, Any] | None = None,
    github_max_age_seconds: float = DEFAULT_GITHUB_MAX_AGE_SECONDS,
    now: str | None = None,
) -> dict[str, Any]:
    """Project per-task status from registry, runtime state and observations."""
    generated_at = now or _utc_now()
    if registry is None:
        registry = Registry.load(root)
    state_path = _runtime_state_db_path(state_db, state_root)
    state = _read_only_state_rows(state_path)
    state_available = bool(state.get("available"))
    rows = state.get("rows", {}) if state_available else {}
    workspaces = _read_workspaces(state_path) if state_available else {}
    overlays = (
        _read_only_overlays(registry, rows.get("task_status", []))
        if state_available
        else {}
    )
    runs_by_task: dict[str, list[dict[str, Any]]] = {}
    runs_by_id: dict[str, dict[str, Any]] = {}
    for row in rows.get("runs", []):
        task_id = row.get("task_id")
        if isinstance(task_id, str):
            runs_by_task.setdefault(task_id, []).append(row)
        run_id = row.get("run_id")
        if isinstance(run_id, str):
            runs_by_id[run_id] = row
    receipts_by_run = {
        row["run_id"]: row for row in rows.get("receipts", []) if row.get("run_id")
    }

    github_observed = github is not None
    github_healthy = bool(github.get("healthy")) if github_observed else False
    github_blocked_reason = github.get("blocked_reason") if github_observed else None
    github_binding_healthy = (
        bool(github.get("binding_healthy", True))
        if github_observed and github_healthy
        else None
    )
    github_hard_findings = (
        [item for item in github.get("hard_findings", []) if isinstance(item, dict)]
        if github_observed and github_healthy
        else []
    )
    github_stale = (
        github_observed
        and github_healthy
        and observation_is_stale(
            github, max_age_seconds=github_max_age_seconds, now=generated_at
        )
    )
    observations_by_task: dict[str, list[dict[str, Any]]] = {}
    if github_observed:
        for observation in github.get("pull_requests", []):
            task_id = observation.get("task_id")
            if isinstance(task_id, str) and task_id:
                observations_by_task.setdefault(task_id, []).append(observation)

    projection_findings: list[dict[str, Any]] = []
    if github_observed and github_healthy and github_binding_healthy is False:
        projection_findings.append(
            {
                "severity": "blocker",
                "code": "github-binding-unhealthy",
                "message": "GitHub observation contains ambiguous PR bindings",
                "hard_findings": github_hard_findings,
            }
        )

    tasks: list[dict[str, Any]] = []
    hard_findings = sum(
        1 for finding in projection_findings if finding["severity"] == "blocker"
    )
    for task in sorted(registry.tasks.values(), key=lambda item: item.id):
        findings: list[dict[str, Any]] = []
        unknowns: list[str] = []
        stale_reasons: list[str] = []
        blocked_reasons: list[str] = []

        effective_state = overlays.get(task.id, task.state)
        if effective_state == "stale":
            stale_reasons.append("verification-stale")
        if not state_available:
            unknowns.append("runtime-state-unavailable")

        queue_lane = _queue_lane(registry, task.id)
        if queue_lane is None and task.state in {"inbox", "planned", "ready", "blocked"}:
            findings.append(
                {
                    "severity": "warning",
                    "code": "task-priority-not-queued",
                    "message": (
                        f"task declares advisory priority lane '{task.lane}' "
                        "but is not dispatchable because registry/queue.json "
                        "is the queue canon"
                    ),
                    "declared_lane": task.lane,
                    "queue_canonical": True,
                }
            )

        task_runs = runs_by_task.get(task.id, [])
        active = [row for row in task_runs if row.get("state") in ACTIVE_RUN_STATES]
        active_run = _public_run(active[0]) if active else None
        if len(active) > 1:
            findings.append(
                {
                    "severity": "blocker",
                    "code": "multiple-active-runs",
                    "message": "more than one active run recorded for this task",
                    "run_ids": sorted(str(row.get("run_id")) for row in active),
                }
            )
        workspace = None
        for row in task_runs:
            run_id = row.get("run_id")
            if isinstance(run_id, str) and run_id in workspaces:
                workspace = _public_workspace(workspaces[run_id])
                break
        receipts = [
            _public_receipt(str(row.get("run_id")), receipts_by_run[row["run_id"]])
            for row in task_runs
            if row.get("run_id") in receipts_by_run
        ]

        task_github: dict[str, Any] | None = None
        if not github_observed:
            unknowns.append("github-not-observed")
        elif not github_healthy:
            blocked_reasons.append(
                f"github-observation-blocked: {github_blocked_reason or 'unknown'}"
            )
        else:
            bound = observations_by_task.get(task.id, [])
            if len(bound) == 1:
                task_github = _public_github(bound[0])
            elif len(bound) > 1:
                task_github = {
                    "binding": BINDING_AMBIGUOUS,
                    "confidence": None,
                    "ambiguous_reason": "multiple-open-prs-for-task",
                    "candidates": sorted(item.get("number") for item in bound),
                }
            if task_github is not None:
                if task_github.get("binding") == BINDING_AMBIGUOUS:
                    findings.append(
                        {
                            "severity": "blocker",
                            "code": "github-binding-ambiguous",
                            "message": task_github.get("ambiguous_reason")
                            or "ambiguous GitHub binding",
                        }
                    )
                checks = task_github.get("checks") or {}
                if checks.get("summary") == CI_UNKNOWN:
                    unknowns.append("ci-unknown")
                if github_stale:
                    stale_reasons.append("github-observation-stale")
                if (
                    str(task_github.get("state", "")).upper() == "MERGED"
                    and effective_state != "verified"
                ):
                    findings.append(
                        {
                            "severity": "warning",
                            "code": "merged-pr-without-bureau-verification",
                            "message": (
                                "a merged PR is bound to this task but the task has "
                                "no current Bureau verification; merge is a GitHub "
                                "fact, not task completion"
                            ),
                        }
                    )

        hard_findings += sum(
            1 for finding in findings if finding["severity"] == "blocker"
        )
        hard_findings += len(blocked_reasons) + len(stale_reasons)
        tasks.append(
            {
                "task_id": task.id,
                "title": task.title,
                "initiative": task.initiative,
                "queue_lane": queue_lane,
                "registry_state": task.state,
                "effective_state": effective_state,
                "active_run": active_run,
                "workspace": workspace,
                "receipts": receipts,
                "github": task_github,
                "findings": findings,
                "unknowns": unknowns,
                "stale_reasons": stale_reasons,
                "blocked_reasons": blocked_reasons,
            }
        )

    healthy = hard_findings == 0
    return {
        "schema_version": STATUS_PROJECTION_SCHEMA_VERSION,
        "generated_at": generated_at,
        "root": str(Path(root).resolve()),
        "state_root": str(state_path.parent),
        "state_store": {
            "available": state_available,
            "path": str(state_path),
            "error": None if state_available else state.get("error"),
        },
        "github_observation": {
            "observed": github_observed,
            "healthy": github_healthy,
            "repository": github.get("repository") if github_observed else None,
            "blocked_reason": github_blocked_reason,
            "binding_healthy": github_binding_healthy,
            "hard_findings": github_hard_findings,
            "observed_at": github.get("observed_at") if github_observed else None,
            "stale": github_stale,
        },
        "healthy": healthy,
        "findings": projection_findings,
        "tasks": tasks,
        "does_not_establish": list(PROJECTION_DOES_NOT_ESTABLISH),
    }
