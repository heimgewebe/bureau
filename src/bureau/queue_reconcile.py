from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import legacy
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
