from __future__ import annotations

from typing import Any

from . import legacy
from .v2 import Dispatcher, frontier_runtime_truth, lifecycle_diagnostics

MISSING_QUEUE_LANE_RANK = len(legacy.LANE_ORDER) + 1


def _priority_lane(task: legacy.Task) -> str | None:
    priority = task.raw.get("priority")
    if not isinstance(priority, dict):
        return None
    lane = priority.get("lane")
    return lane if isinstance(lane, str) else None


def _priority_rank(task: legacy.Task) -> int:
    priority = task.raw.get("priority")
    if not isinstance(priority, dict):
        return task.rank
    rank = priority.get("rank", task.rank)
    return rank if isinstance(rank, int) else task.rank


def _lane_sort_value(lane: str | None) -> int:
    if lane is None:
        return MISSING_QUEUE_LANE_RANK
    return legacy.LANE_ORDER.get(lane, MISSING_QUEUE_LANE_RANK)


def _position_sort_value(registry: legacy.Registry, task_id: str) -> int:
    position = registry.positions.get(task_id)
    if position is None:
        return 10_000_000
    return position[1]


def _dependency_states(registry: legacy.Registry, task: legacy.Task) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for dependency in task.depends_on:
        dependency_task = registry.tasks.get(dependency)
        result.append(
            {
                "task_id": dependency,
                "state": dependency_task.state if dependency_task is not None else "missing",
            }
        )
    return result


def _claim_documents(task: legacy.Task) -> list[dict[str, Any]]:
    return [
        {
            "resource": claim.resource,
            "mode": claim.mode,
            "amount": claim.amount,
            "isolation": claim.isolation,
        }
        for claim in task.claims
    ]


def _rank_key(
    registry: legacy.Registry, item: dict[str, Any]
) -> tuple[int, int, int, int, int, str]:
    task_id = str(item["task_id"])
    task = registry.tasks[task_id]
    return (
        0 if item.get("eligible") is True else 1,
        _lane_sort_value(item.get("queue_lane")),
        _position_sort_value(registry, task_id),
        _lane_sort_value(_priority_lane(task)),
        _priority_rank(task),
        task_id,
    )


def _ranked_item(registry: legacy.Registry, item: dict[str, Any], rank: int) -> dict[str, Any]:
    task_id = str(item["task_id"])
    task = registry.tasks[task_id]
    position = registry.positions.get(task_id)
    return {
        "rank": rank,
        "task_id": task_id,
        "title": item["title"],
        "eligible": item["eligible"],
        "effective_state": item["effective_state"],
        "queue_lane": item["queue_lane"],
        "reasons": item["reasons"],
        "rank_basis": {
            "registry_state": task.state,
            "effective_state": item["effective_state"],
            "queue_lane": item["queue_lane"],
            "queue_index": position[1] if position is not None else None,
            "priority_lane": _priority_lane(task),
            "priority_rank": _priority_rank(task),
            "depends_on": _dependency_states(registry, task),
            "resource_claims": _claim_documents(task),
        },
    }


def what_now_report(
    dispatcher: Dispatcher,
    capabilities: set[str],
    *,
    resource: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Return a deterministic next-work report from registry and runtime state.

    The report intentionally avoids conversational or hand-written memory. It is
    derived from the registry, the operational state store, dependency state, and
    repository/resource reservations that the Dispatcher already knows how to
    observe.
    """
    if limit < 1:
        raise legacy.StateError("limit must be at least 1")
    frontier = dispatcher.frontier(capabilities, resource=resource)
    lifecycle = lifecycle_diagnostics(dispatcher.registry, dispatcher.store)
    ranked_frontier = sorted(frontier, key=lambda item: _rank_key(dispatcher.registry, item))
    ranked = [
        _ranked_item(dispatcher.registry, item, rank=index + 1)
        for index, item in enumerate(ranked_frontier[:limit])
    ]
    eligible = [item for item in ranked if item["eligible"] is True]
    selected = eligible[0] if eligible else None
    blocked = [item for item in ranked if item["eligible"] is not True]
    runtime_truth = frontier_runtime_truth(frontier, lifecycle)
    return {
        "command": "what-now",
        "read_only": True,
        "resource": resource,
        "capabilities": sorted(capabilities),
        "source_authority": [
            "registry/tasks/*.json",
            "registry/queue.json",
            "bureau state store",
            "dispatcher resource reservations",
        ],
        "selected": selected,
        "eligible": eligible,
        "blocked": blocked,
        "ranked": ranked,
        "summary": {
            "ranked_count": len(ranked),
            "eligible_ranked_count": len(eligible),
            "blocked_ranked_count": len(blocked),
            "frontier_count": len(frontier),
            "selected_task_id": selected["task_id"] if selected else None,
        },
        "runtime_truth": runtime_truth,
        "lifecycle": lifecycle,
    }
