from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import legacy
from .approval import require_approval, reviewed_plan_approval
from .core import Registry, StateStore

OPEN_STATES = {"inbox", "planned", "ready", "blocked", "stale"}
TERMINAL_STATES = {"verified", "cancelled", "superseded"}


@dataclass(frozen=True)
class TaskSnapshot:
    task: legacy.Task
    effective_state: str
    queue_lane: str | None
    priority_lane: str
    priority_rank: int


def _validate_resource_filter(registry: Registry, resource: str | None) -> None:
    if resource is not None and resource not in registry.resources:
        raise legacy.StateError(f"unknown resource filter: {resource}")


def _task_matches_resource(
    registry: Registry, task: legacy.Task, resource: str | None
) -> bool:
    if resource is None:
        return True
    return any(
        legacy.overlaps(claim.resource, resource, registry.resources)
        for claim in task.claims
    )


def _queue_lane(registry: Registry, task_id: str) -> str | None:
    position = registry.positions.get(task_id)
    if position is None:
        return None
    for lane, lane_index in legacy.LANE_ORDER.items():
        if lane_index == position[0]:
            return lane
    return None


def _repo_resources(registry: Registry) -> list[legacy.Resource]:
    result = [
        resource
        for resource in registry.resources.values()
        if resource.id.startswith("repo.")
    ]
    if result:
        return sorted(result, key=lambda item: item.id)
    return sorted(
        (
            resource
            for resource in registry.resources.values()
            if resource.type == "git-repository" and resource.id != "repo"
        ),
        key=lambda item: item.id,
    )


def _snapshot_tasks(
    registry: Registry, store: StateStore, resource: str | None
) -> list[TaskSnapshot]:
    with store.connect() as connection:
        overlays = store.overlays(connection, registry)
    snapshots: list[TaskSnapshot] = []
    for task in registry.ordered_tasks():
        if not _task_matches_resource(registry, task, resource):
            continue
        snapshots.append(
            TaskSnapshot(
                task=task,
                effective_state=overlays.get(task.id, task.state),
                queue_lane=_queue_lane(registry, task.id),
                priority_lane=task.lane,
                priority_rank=task.rank,
            )
        )
    return snapshots


def _finding(
    *,
    code: str,
    severity: str,
    task: legacy.Task,
    message: str,
    recommendation: str,
    queue_lane: str | None,
    priority_lane: str,
    effective_state: str,
    rule: str,
    proposed_action: dict[str, Any] | None = None,
) -> dict[str, Any]:
    finding = {
        "code": code,
        "severity": severity,
        "task_id": task.id,
        "title": task.title,
        "effective_state": effective_state,
        "queue_lane": queue_lane,
        "priority_lane": priority_lane,
        "claim_resources": [claim.resource for claim in task.claims],
        "message": message,
        "rule": rule,
        "recommendation": recommendation,
    }
    if proposed_action is not None:
        finding["proposed_action"] = proposed_action
    return finding


def _repo_focus(registry: Registry, snapshots: list[TaskSnapshot]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for repo in _repo_resources(registry):
        repo_items = [
            item
            for item in snapshots
            if any(
                legacy.overlaps(claim.resource, repo.id, registry.resources)
                for claim in item.task.claims
            )
        ]
        open_items = [item for item in repo_items if item.effective_state in OPEN_STATES]
        lanes = {
            lane: [item.task.id for item in repo_items if item.queue_lane == lane]
            for lane in legacy.LANE_ORDER
        }
        current_ball = None
        for lane in legacy.LANE_ORDER:
            if lanes[lane]:
                task_id = lanes[lane][0]
                task = registry.tasks[task_id]
                current_ball = {"task_id": task.id, "title": task.title, "queue_lane": lane}
                break
        result[repo.id] = {
            "open_task_count": len(open_items),
            "queued_task_count": sum(len(ids) for ids in lanes.values()),
            "lanes": lanes,
            "current_ball": current_ball,
        }
    return result


SAFE_APPLY_OPERATIONS = {"add_to_queue"}
SAFE_APPLY_LANES = {"now", "next"}


def _queue_path(registry: Registry) -> Path:
    return registry.root / "registry" / "queue.json"


def _read_queue(registry: Registry) -> dict[str, Any]:
    return legacy.read_json(_queue_path(registry))


def _queue_sha256(queue: dict[str, Any]) -> str:
    return legacy.sha256_json(queue)


def _git_head(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        )
    except Exception:
        return None
    return result.stdout.strip() or None


def _plan_actions(report: dict[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for finding in report["findings"]:
        proposed = finding.get("proposed_action")
        if not isinstance(proposed, dict):
            continue
        operation = proposed.get("operation")
        target_lane = proposed.get("target_lane")
        task_id = finding.get("task_id")
        if (
            operation not in SAFE_APPLY_OPERATIONS
            or target_lane not in SAFE_APPLY_LANES
            or not isinstance(task_id, str)
        ):
            continue
        key = (operation, target_lane, task_id)
        if key in seen:
            continue
        seen.add(key)
        actions.append(
            {
                "operation": operation,
                "target_lane": target_lane,
                "task_id": task_id,
                "source_finding_code": finding.get("code"),
                "effective_state": finding.get("effective_state"),
                "priority_lane": finding.get("priority_lane"),
            }
        )
    return actions


def _apply_actions_to_queue(
    queue: dict[str, Any], actions: list[dict[str, Any]]
) -> dict[str, Any]:
    updated = {
        **queue,
        "lanes": {
            lane: list(task_ids)
            for lane, task_ids in queue.get("lanes", {}).items()
        },
    }
    lanes = updated.setdefault("lanes", {})
    for lane in legacy.LANE_ORDER:
        lanes.setdefault(lane, [])
    for action in actions:
        task_id = action["task_id"]
        target_lane = action["target_lane"]
        for lane in legacy.LANE_ORDER:
            lanes[lane] = [item for item in lanes[lane] if item != task_id]
        lanes[target_lane].append(task_id)
    return updated


def queue_reconcile_plan(
    registry: Registry,
    store: StateStore,
    *,
    resource: str | None = None,
) -> dict[str, Any]:
    """Create a reviewed-apply plan for safe queue-reconcile mutations.

    The plan is inert until a reviewer edits ``review.status`` to ``reviewed``.
    It binds the dry-run report and pre-apply queue hash so apply can refuse
    stale plans instead of silently mutating a drifted queue.
    """
    report = queue_reconcile_report(registry, store, resource=resource)
    queue_before = _read_queue(registry)
    actions = _plan_actions(report)
    queue_after = _apply_actions_to_queue(queue_before, actions)
    return {
        "schema_version": 1,
        "command": "queue-reconcile-plan",
        "created_at": legacy.utc_now(),
        "resource": resource,
        "registry": {
            "root": str(registry.root),
            "git_head": _git_head(registry.root),
            "queue_sha256_before": _queue_sha256(queue_before),
        },
        "dry_run_report_sha256": legacy.sha256_json(report),
        "actions": actions,
        "expected_queue_after": queue_after,
        "expected_queue_after_sha256": _queue_sha256(queue_after),
        "review": {
            "required": True,
            "status": "pending",
            "instructions": (
                "Review actions and expected_queue_after. To apply, set status "
                "to reviewed and add reviewer plus reviewed_at."
            ),
        },
        "does_not_establish": [
            "dispatch_authority",
            "task_claim",
            "task_completion",
            "merge_readiness",
        ],
    }


def write_queue_reconcile_plan(
    registry: Registry,
    store: StateStore,
    path: str | Path,
    *,
    resource: str | None = None,
) -> dict[str, Any]:
    plan = queue_reconcile_plan(registry, store, resource=resource)
    target = Path(path).expanduser()
    legacy.atomic_write(target, legacy.canonical_json(plan) + "\n")
    return {**plan, "path": str(target)}


def _load_reviewed_plan(path: str | Path) -> dict[str, Any]:
    plan = legacy.read_json(Path(path).expanduser())
    if plan.get("schema_version") != 1 or plan.get("command") != "queue-reconcile-plan":
        raise legacy.StateError("queue reconcile plan has unsupported schema or command")
    review = plan.get("review")
    if not isinstance(review, dict) or review.get("status") != "reviewed":
        raise legacy.StateError("queue reconcile plan is not reviewed")
    if not review.get("reviewer") or not review.get("reviewed_at"):
        raise legacy.StateError("reviewed queue reconcile plan requires reviewer and reviewed_at")
    plan["approval"] = require_approval(
        "queue_mutation",
        reviewed_plan_approval(
            reviewer=str(review["reviewer"]),
            reference=str(Path(path).expanduser()),
            approved=True,
        ),
    )
    return plan


def apply_queue_reconcile_plan(
    registry: Registry,
    store: StateStore,
    path: str | Path,
    *,
    resource: str | None = None,
) -> dict[str, Any]:
    """Apply a reviewed queue reconcile plan with dry-run parity and rollback.

    Only deterministic add-to-now/add-to-next actions generated by
    queue_reconcile_plan are applied. The function refuses stale plans and
    restores the original queue if post-apply validation fails.
    """
    plan = _load_reviewed_plan(path)
    if plan.get("resource") != resource:
        raise legacy.StateError("queue reconcile plan resource does not match apply resource")
    current_report = queue_reconcile_report(registry, store, resource=resource)
    current_queue = _read_queue(registry)
    current_queue_sha = _queue_sha256(current_queue)
    if current_queue_sha != plan.get("registry", {}).get("queue_sha256_before"):
        raise legacy.StateError("queue changed since queue reconcile plan was generated")
    if legacy.sha256_json(current_report) != plan.get("dry_run_report_sha256"):
        raise legacy.StateError("queue reconcile findings changed since plan review")
    expected = plan.get("expected_queue_after")
    if not isinstance(expected, dict):
        raise legacy.StateError("queue reconcile plan lacks expected_queue_after")
    expected_sha = _queue_sha256(expected)
    if expected_sha != plan.get("expected_queue_after_sha256"):
        raise legacy.StateError("queue reconcile plan expected queue hash mismatch")
    recomputed = _apply_actions_to_queue(current_queue, plan.get("actions", []))
    if legacy.sha256_json(recomputed) != expected_sha:
        raise legacy.StateError("queue reconcile plan actions do not match expected queue")

    queue_path = _queue_path(registry)
    before_text = queue_path.read_text(encoding="utf-8")
    legacy.atomic_write(queue_path, legacy.canonical_json(expected) + "\n")
    try:
        registry_after = Registry.load(registry.root)
        from .core import Dispatcher
        from .registry_truth import registry_truth_diagnostics

        _ = registry_after.summary()
        state_integrity = store.integrity()
        doctor = Dispatcher(registry_after, store).doctor(False)
        registry_truth = registry_truth_diagnostics(registry.root)
        gates = {
            "bureau_check": (
                state_integrity["integrity"] == "ok"
                and not state_integrity["foreign_key_errors"]
            ),
            "doctor_healthy": doctor["healthy"],
            "registry_truth_healthy": registry_truth["healthy"],
        }
        if not all(gates.values()):
            raise legacy.StateError("post-apply gates failed: " + legacy.canonical_json(gates))
    except Exception:
        legacy.atomic_write(queue_path, before_text)
        raise
    return {
        "schema_version": 1,
        "command": "queue-reconcile-apply",
        "applied": True,
        "resource": resource,
        "path": str(Path(path).expanduser()),
        "queue_sha256_before": current_queue_sha,
        "queue_sha256_after": expected_sha,
        "actions": plan.get("actions", []),
        "approval": plan.get("approval"),
        "post_gates": gates,
        "does_not_establish": [
            "dispatch_authority",
            "task_claim",
            "task_completion",
            "merge_readiness",
        ],
    }


def queue_reconcile_report(
    registry: Registry, store: StateStore, *, resource: str | None = None
) -> dict[str, Any]:
    """Return a read-only queue freshness report.

    The report compares advisory task priority with the canonical queue without
    mutating either. It is intentionally diagnostic-only.
    """
    _validate_resource_filter(registry, resource)
    snapshots = _snapshot_tasks(registry, store, resource)
    findings: list[dict[str, Any]] = []

    for item in snapshots:
        task = item.task
        if item.queue_lane is not None and item.effective_state in TERMINAL_STATES:
            findings.append(
                _finding(
                    code="terminal-task-in-queue",
                    severity="error",
                    task=task,
                    message="Terminal task remains in registry/queue.json.",
                    recommendation="remove_from_queue",
                    rule="queued_terminal_tasks_are_invalid",
                    proposed_action={"operation": "remove_from_queue", "target_lane": None},
                    queue_lane=item.queue_lane,
                    priority_lane=item.priority_lane,
                    effective_state=item.effective_state,
                )
            )
        if item.queue_lane == "now" and item.effective_state != "ready":
            findings.append(
                _finding(
                    code="now-task-not-ready",
                    severity="error",
                    task=task,
                    message="Task is queued in now but is not ready.",
                    recommendation="move_to_next_or_repair_state",
                    rule="queue_now_requires_ready_state",
                    proposed_action={
                        "operation": "move_from_now",
                        "allowed_target_lanes": ["next", "later"],
                        "alternative": (
                            "change_task_state_to_ready_if_acceptance_"
                            "preconditions_are_met"
                        ),
                    },
                    queue_lane=item.queue_lane,
                    priority_lane=item.priority_lane,
                    effective_state=item.effective_state,
                )
            )
        if (
            item.queue_lane is None
            and item.effective_state == "ready"
            and item.priority_lane == "now"
        ):
            findings.append(
                _finding(
                    code="unqueued-ready-priority-now",
                    severity="warning",
                    task=task,
                    message="Ready task has advisory priority now but is absent from queue.",
                    recommendation="promote_to_now",
                    rule="ready_priority_now_should_be_queued_or_explained",
                    proposed_action={"operation": "add_to_queue", "target_lane": "now"},
                    queue_lane=item.queue_lane,
                    priority_lane=item.priority_lane,
                    effective_state=item.effective_state,
                )
            )
        if (
            item.queue_lane is None
            and item.effective_state in {"planned", "ready"}
            and item.priority_lane == "next"
        ):
            findings.append(
                _finding(
                    code="unqueued-open-priority-next",
                    severity="warning",
                    task=task,
                    message="Open task has advisory priority next but is absent from queue.",
                    recommendation="promote_to_next",
                    rule="open_priority_next_should_be_queued_or_explained",
                    proposed_action={"operation": "add_to_queue", "target_lane": "next"},
                    queue_lane=item.queue_lane,
                    priority_lane=item.priority_lane,
                    effective_state=item.effective_state,
                )
            )
        if item.queue_lane == "later" and item.priority_lane in {"now", "next"}:
            findings.append(
                _finding(
                    code="queued-later-priority-now-or-next",
                    severity="warning",
                    task=task,
                    message="Queued lane later disagrees with advisory now/next priority.",
                    recommendation="review_lane",
                    rule="canonical_queue_lane_should_match_current_priority_or_document_drift",
                    proposed_action={
                        "operation": "review_lane",
                        "allowed_target_lanes": ["now", "next", "later"],
                    },
                    queue_lane=item.queue_lane,
                    priority_lane=item.priority_lane,
                    effective_state=item.effective_state,
                )
            )

    repo_focus = _repo_focus(registry, snapshots)
    for repo_id, item in repo_focus.items():
        if item["open_task_count"] > 0 and item["current_ball"] is None:
            findings.append(
                {
                    "code": "repo-without-current-ball",
                    "severity": "info",
                    "resource": repo_id,
                    "message": "Repository has open tasks but no queued repository ball.",
                    "recommendation": "review_queue_focus",
                    "open_task_count": item["open_task_count"],
                }
            )

    queue_counts = {
        lane: sum(1 for item in snapshots if item.queue_lane == lane)
        for lane in legacy.LANE_ORDER
    }
    summary = {
        "queued_now": queue_counts["now"],
        "queued_next": queue_counts["next"],
        "queued_later": queue_counts["later"],
        "findings": len(findings),
        "promote_to_now_candidates": sum(
            1 for item in findings if item.get("recommendation") == "promote_to_now"
        ),
        "promote_to_next_candidates": sum(
            1 for item in findings if item.get("recommendation") == "promote_to_next"
        ),
        "lane_mismatch_candidates": sum(
            1 for item in findings if item.get("recommendation") == "review_lane"
        ),
        "blockers": sum(1 for item in findings if item.get("severity") == "error"),
    }
    return {
        "schema_version": 1,
        "command": "queue-reconcile",
        "read_only": True,
        "queue_canonical": True,
        "resource": resource,
        "summary": summary,
        "findings": findings,
        "repo_focus": repo_focus,
        "does_not_establish": [
            "queue_mutation",
            "lane_promotion",
            "dispatch_authority",
            "merge_authority",
            "completion_authority",
        ],
    }
