from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from . import legacy, task_specs
from .core import Dispatcher, Registry, StateStore

FRONTIER_SCHEMA_VERSION = 1
EXECUTABLE_LANES = ("now", "next", "later")
PROJECTION_LANES = (*EXECUTABLE_LANES, "blocked", "in_flight", "closeout")
TERMINAL_TASK_STATES = {"verified", "cancelled", "superseded"}
OPEN_TASK_STATES = {"inbox", "planned", "ready", "blocked", "stale"}
LEGACY_QUEUE_REASON = "task is not queued in registry/queue.json"
ACTOR_CAPABILITY_PREFIX = "missing capabilities: "
EXECUTION_REASON_PREFIX = "execution is "
PRIORITY_ORDER = {"now": 0, "next": 1, "later": 2}


@dataclass(frozen=True)
class FrontierPolicy:
    """Deterministic scheduling policy for the projected operational frontier."""

    now_floor: int = 2
    now_target: int = 4
    max_now_promotions: int = 4
    work_ball_limit: int = 4

    def __post_init__(self) -> None:
        if self.now_floor < 1:
            raise ValueError("now_floor must be at least one")
        if self.now_target < self.now_floor:
            raise ValueError("now_target must be greater than or equal to now_floor")
        if self.max_now_promotions < 1:
            raise ValueError("max_now_promotions must be at least one")
        if self.work_ball_limit < 1:
            raise ValueError("work_ball_limit must be at least one")


def _task_from_spec(spec: dict[str, Any], digest: str) -> legacy.Task:
    acceptance: list[dict[str, Any]] = []
    for index, criterion in enumerate(spec.get("acceptance", []), 1):
        if isinstance(criterion, str):
            acceptance.append({"id": f"criterion-{index}", "assertion": criterion})
        elif isinstance(criterion, dict):
            acceptance.append(dict(criterion))
        else:
            raise legacy.StateError(
                f"invalid acceptance criterion in authoritative TaskSpec {spec.get('id')!r}"
            )
    priority = spec.get("priority") if isinstance(spec.get("priority"), dict) else {}
    return legacy.Task(
        id=str(spec.get("id", "")),
        initiative=str(spec.get("initiative", "")),
        title=str(spec.get("title", "")),
        state=str(spec.get("state", "")),
        depends_on=tuple(spec.get("depends_on", [])),
        capabilities=tuple(sorted(set(spec.get("required_capabilities", [])))),
        lane=str(priority.get("lane", "later")),
        rank=int(priority.get("rank", 1000)),
        execution=dict(spec.get("execution", {})),
        claims=tuple(legacy.Claim.from_raw(value) for value in spec.get("claims", [])),
        acceptance=tuple(acceptance),
        raw=spec,
        sha256=digest,
    )


def _authoritative_registry(
    registry: Registry, store: StateStore
) -> tuple[Registry, dict[str, Any], dict[str, dict[str, Any]]]:
    """Overlay the Registry catalogue with current StateStore TaskSpec revisions.

    Resources, initiatives and schemas remain code/Registry contracts. Task payloads
    are operational truth from T003's append-only StateStore. A completely empty
    TaskSpec store is treated as a legacy bootstrap fixture only; once one StateStore
    TaskSpec exists, Git-only task payloads never silently re-enter authority.
    """

    with store.connect() as connection:
        try:
            projection = task_specs.current_projection(connection)
        except task_specs.TaskSpecError as exc:
            raise legacy.StateError(str(exc)) from exc

    state_tasks = projection["tasks"]
    if not state_tasks:
        revisions = {
            task.id: {
                "revision": None,
                "spec_sha256": task.sha256,
                "spec": task.raw,
            }
            for task in registry.tasks.values()
        }
        authority = {
            "kind": "legacy-git-bootstrap",
            "task_count": len(revisions),
            "task_spec_root_sha256": legacy.sha256_json(
                {task_id: item["spec_sha256"] for task_id, item in sorted(revisions.items())}
            ),
            "git_projection_only_task_ids": [],
            "does_not_establish": ["state_store_task_spec_authority"],
        }
        return registry, authority, revisions

    tasks: dict[str, legacy.Task] = {}
    revisions: dict[str, dict[str, Any]] = {}
    for task_id, item in sorted(state_tasks.items()):
        spec = item["spec"]
        digest = str(item["spec_sha256"])
        if task_specs.task_spec_digest(spec) != digest:
            raise legacy.StateError(f"authoritative TaskSpec digest drift for {task_id}")
        task = _task_from_spec(spec, digest)
        if task.id != task_id:
            raise legacy.StateError(f"authoritative TaskSpec id drift for {task_id}")
        tasks[task_id] = task
        revisions[task_id] = {
            "revision": int(item["revision"]),
            "spec_sha256": digest,
            "spec": spec,
        }

    for task in tasks.values():
        if task.initiative not in registry.initiatives:
            raise legacy.StateError(
                f"authoritative TaskSpec {task.id} references unknown initiative {task.initiative}"
            )
        for dependency in task.depends_on:
            if dependency not in tasks:
                raise legacy.StateError(
                    f"authoritative TaskSpec {task.id} references unknown dependency {dependency}"
                )
        for claim in task.claims:
            if claim.resource not in registry.resources:
                raise legacy.StateError(
                    f"authoritative TaskSpec {task.id} references unknown resource {claim.resource}"
                )

    projected = copy.copy(registry)
    projected.tasks = tasks
    git_only = sorted(set(registry.tasks) - set(tasks))
    authority = {
        "kind": "bureau-state-store-task-specs",
        "task_count": len(tasks),
        "task_spec_root_sha256": task_specs.projection_root(projection),
        "git_projection_only_task_ids": git_only,
        "does_not_establish": ["git_task_payload_authority"],
    }
    return projected, authority, revisions


def _queue_lane(registry: Registry, task_id: str) -> str | None:
    position = registry.positions.get(task_id)
    if position is None:
        return None
    for lane, lane_index in legacy.LANE_ORDER.items():
        if lane_index == position[0]:
            return lane
    return None


def _actor_dependent_reason(reason: str) -> bool:
    if reason.startswith(ACTOR_CAPABILITY_PREFIX):
        return True
    if reason.startswith(EXECUTION_REASON_PREFIX):
        execution = reason.removeprefix(EXECUTION_REASON_PREFIX)
        mode, separator, _policy = execution.partition("/")
        return bool(separator) and mode != "manual"
    lowered = reason.casefold()
    return "approval" in lowered or "authority-bound reviewed plan" in lowered


def _split_reasons(reasons: list[str]) -> tuple[list[str], list[str]]:
    actor: list[str] = []
    structural: list[str] = []
    for raw in reasons:
        reason = str(raw)
        if reason == LEGACY_QUEUE_REASON:
            continue
        target = actor if _actor_dependent_reason(reason) else structural
        if reason not in target:
            target.append(reason)
    return sorted(structural), sorted(actor)


def _compact_runtime(runtime_truth: dict[str, Any] | None) -> dict[str, Any]:
    if runtime_truth is None:
        return {
            "checked": False,
            "execution_blocked": False,
            "status": "not-checked",
            "blocker_codes": [],
        }
    return {
        "checked": True,
        "execution_blocked": runtime_truth.get("execution_blocked") is True,
        "status": runtime_truth.get("status"),
        "drift_classification": runtime_truth.get("drift_classification"),
        "blocker_codes": sorted(str(item) for item in runtime_truth.get("blocker_codes", [])),
    }


def _priority_key(task: legacy.Task) -> tuple[int, int, str]:
    return PRIORITY_ORDER.get(task.lane, len(PRIORITY_ORDER)), task.rank, task.id


def _active_run_projection(rows: list[Any]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        result.append(
            {
                "run_id": str(row["run_id"]),
                "state": str(row["state"]),
                "worker_id": str(row["worker_id"]),
            }
        )
    return sorted(result, key=lambda item: (item["state"], item["run_id"]))


def _initial_lane(
    *,
    task: legacy.Task,
    effective_state: str,
    structural_reasons: list[str],
    active_runs: list[dict[str, Any]],
) -> str | None:
    if active_runs:
        if any(item["state"] == "verifying" for item in active_runs):
            return "closeout"
        return "in_flight"
    if effective_state in TERMINAL_TASK_STATES:
        return None
    if effective_state in {"blocked", "stale"}:
        return "blocked"
    if effective_state == "ready":
        return "blocked" if structural_reasons else (
            task.lane if task.lane in EXECUTABLE_LANES else "later"
        )
    if effective_state in {"planned", "inbox"}:
        state_reason = f"state is {effective_state}"
        other_structural = [reason for reason in structural_reasons if reason != state_reason]
        return "blocked" if other_structural else "later"
    return "blocked"


def _reservations_for_tasks(tasks: list[legacy.Task]) -> list[legacy.Reservation]:
    reservations: list[legacy.Reservation] = []
    for task in tasks:
        reservations.extend(
            legacy.Reservation(task.id, claim.resource, claim.mode, claim.amount)
            for claim in task.claims
        )
    return reservations


def _conflicts_with_selected(
    registry: Registry, task: legacy.Task, selected: list[legacy.Task]
) -> bool:
    reservations = _reservations_for_tasks(selected)
    return any(
        legacy.claim_conflicts(claim, reservations, registry.resources)
        for claim in task.claims
    )


def _diverse_selection(
    registry: Registry,
    candidates: list[legacy.Task],
    *,
    limit: int,
    seed: list[legacy.Task] | None = None,
    fill_conflicting: bool,
) -> list[legacy.Task]:
    if limit <= 0:
        return []
    selected = list(seed or [])
    chosen: list[legacy.Task] = []
    for task in candidates:
        if len(chosen) >= limit:
            break
        if _conflicts_with_selected(registry, task, selected):
            continue
        chosen.append(task)
        selected.append(task)
    if fill_conflicting and len(chosen) < limit:
        chosen_ids = {task.id for task in chosen}
        for task in candidates:
            if len(chosen) >= limit:
                break
            if task.id in chosen_ids:
                continue
            chosen.append(task)
            chosen_ids.add(task.id)
    return chosen


def _lane_cards(cards: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    lanes = {lane: [] for lane in PROJECTION_LANES}
    for card in cards:
        lane = card.get("projected_lane")
        if lane in lanes:
            lanes[str(lane)].append(card)
    return lanes


def compatibility_queue(
    projection: dict[str, Any], *, queue_policy: str = "skip-blocked"
) -> dict[str, Any]:
    """Render the legacy Git queue from the authoritative dynamic projection."""

    lanes = projection.get("lanes", {})
    return {
        "schema_version": 1,
        "queue_policy": queue_policy,
        "lanes": {
            lane: [str(item["task_id"]) for item in lanes.get(lane, [])]
            for lane in EXECUTABLE_LANES
        },
    }


def build_frontier_projection(
    registry: Registry,
    store: StateStore,
    *,
    capabilities: set[str] | None = None,
    policy: FrontierPolicy | None = None,
    resource: str | None = None,
    check_runtime: bool = True,
) -> dict[str, Any]:
    """Project the operational Bureau frontier without queue admission authority.

    TaskSpecs come from the StateStore when T003 data exists. The checked-in
    ``registry/queue.json`` is observed only as a compatibility projection and is
    explicitly removed from claim reasons. Heavy claim blockers are reused from
    the existing Dispatcher so dependencies, children, active runs, resources,
    open PRs and policy gates keep one implementation.
    """

    selected_policy = policy or FrontierPolicy()
    authoritative, authority, revisions = _authoritative_registry(registry, store)
    if resource is not None and resource not in authoritative.resources:
        raise legacy.StateError(f"unknown resource filter: {resource}")
    declared_capabilities = {
        capability
        for task in authoritative.tasks.values()
        for capability in task.capabilities
    }
    actor_capabilities = set(declared_capabilities if capabilities is None else capabilities)
    dispatcher = Dispatcher(authoritative, store)
    open_pr_reservations = dispatcher._open_pr_reservations(strict=False)
    with store.connect() as connection:
        runs = list(store.active_runs(connection))
        reservations = store.reservations(connection) + open_pr_reservations
        overlays = store.overlays(connection, authoritative)
    runtime_truth = dispatcher._runtime_execution_truth() if check_runtime else None
    compact_runtime = _compact_runtime(runtime_truth)
    runtime_reason = None
    if compact_runtime["execution_blocked"]:
        codes = ", ".join(compact_runtime["blocker_codes"])
        runtime_reason = "runtime execution blocked" + (f": {codes}" if codes else "")

    runs_by_task: dict[str, list[Any]] = {}
    for row in runs:
        runs_by_task.setdefault(str(row["task_id"]), []).append(row)

    cards: list[dict[str, Any]] = []
    tasks_by_id: dict[str, legacy.Task] = {}
    for task in sorted(authoritative.tasks.values(), key=_priority_key):
        if resource is not None and not any(
            legacy.overlaps(claim.resource, resource, authoritative.resources)
            for claim in task.claims
        ):
            continue
        tasks_by_id[task.id] = task
        raw_reasons = dispatcher.reasons(
            task,
            actor_capabilities,
            runs,
            reservations,
            overlays,
            projection_resource=resource,
        )
        structural, actor = _split_reasons(raw_reasons)
        if runtime_reason is not None and runtime_reason not in structural:
            structural.append(runtime_reason)
            structural.sort()
        effective_state = overlays.get(task.id, task.state)
        active_runs = _active_run_projection(runs_by_task.get(task.id, []))
        lane = _initial_lane(
            task=task,
            effective_state=effective_state,
            structural_reasons=structural,
            active_runs=active_runs,
        )
        structurally_eligible = (
            effective_state == "ready"
            and not structural
            and not active_runs
            and not compact_runtime["execution_blocked"]
        )
        card = {
            "task_id": task.id,
            "title": task.title,
            "initiative": task.initiative,
            "task_spec_revision": revisions[task.id]["revision"],
            "task_spec_sha256": revisions[task.id]["spec_sha256"],
            "declared_state": task.state,
            "effective_state": effective_state,
            "priority": {"lane": task.lane, "rank": task.rank},
            "projected_lane": lane,
            "projected_from_lane": None,
            "projection_reason": "priority-and-gates",
            "compatibility_queue_lane": _queue_lane(registry, task.id),
            "claim_resources": [claim.resource for claim in task.claims],
            "active_runs": active_runs,
            "structural_reasons": structural,
            "actor_dependent_reasons": actor,
            "structurally_eligible": structurally_eligible,
            "actor_eligible": not actor,
            "claim_eligible": structurally_eligible and not actor,
            "terminal": effective_state in TERMINAL_TASK_STATES,
        }
        cards.append(card)

    # Hysteresis is part of the projection, not a mutation. When Now supply drops
    # below the floor, deterministically borrow structurally runnable Next tasks.
    base_now = [
        card
        for card in cards
        if card["projected_lane"] == "now" and card["structurally_eligible"]
    ]
    next_cards = [
        card
        for card in cards
        if card["projected_lane"] == "next" and card["structurally_eligible"]
    ]
    promotion_count = 0
    if len(base_now) < selected_policy.now_floor:
        shortage = max(0, selected_policy.now_target - len(base_now))
        promotion_limit = min(shortage, selected_policy.max_now_promotions)
        base_tasks = [tasks_by_id[str(card["task_id"])] for card in base_now]
        next_tasks = [tasks_by_id[str(card["task_id"])] for card in next_cards]
        promoted_tasks = _diverse_selection(
            authoritative,
            next_tasks,
            limit=promotion_limit,
            seed=base_tasks,
            fill_conflicting=True,
        )
        promoted_ids = {task.id for task in promoted_tasks}
        promotion_count = len(promoted_ids)
        for card in cards:
            if card["task_id"] not in promoted_ids:
                continue
            card["projected_from_lane"] = "next"
            card["projected_lane"] = "now"
            card["projection_reason"] = "domain-supply-refill"

    lane_order = {lane: index for index, lane in enumerate(PROJECTION_LANES)}
    cards.sort(
        key=lambda card: (
            lane_order.get(str(card.get("projected_lane")), len(lane_order)),
            int(card["priority"]["rank"]),
            str(card["task_id"]),
        )
    )
    lanes = _lane_cards(cards)

    runnable_cards = [
        card
        for lane in EXECUTABLE_LANES
        for card in lanes[lane]
        if card["structurally_eligible"]
    ]
    runnable_tasks = [tasks_by_id[str(card["task_id"])] for card in runnable_cards]
    work_ball_tasks = _diverse_selection(
        authoritative,
        runnable_tasks,
        limit=selected_policy.work_ball_limit,
        fill_conflicting=False,
    )
    card_by_id = {str(card["task_id"]): card for card in cards}
    work_balls = [
        {
            "task_id": task.id,
            "projected_lane": card_by_id[task.id]["projected_lane"],
            "priority": card_by_id[task.id]["priority"],
            "claim_resources": card_by_id[task.id]["claim_resources"],
            "actor_dependent_reasons": card_by_id[task.id]["actor_dependent_reasons"],
        }
        for task in work_ball_tasks
    ]

    material: dict[str, Any] = {
        "schema_version": FRONTIER_SCHEMA_VERSION,
        "kind": "bureau_dynamic_frontier_projection",
        "authority": authority,
        "queue_authoritative": False,
        "queue_role": "compatibility_projection_only",
        "actor": {"capabilities": sorted(actor_capabilities)},
        "runtime": compact_runtime,
        "policy": {
            "now_floor": selected_policy.now_floor,
            "now_target": selected_policy.now_target,
            "max_now_promotions": selected_policy.max_now_promotions,
            "work_ball_limit": selected_policy.work_ball_limit,
        },
        "resource": resource,
        "lanes": lanes,
        "work_balls": work_balls,
        "summary": {
            **{f"{lane}_count": len(lanes[lane]) for lane in PROJECTION_LANES},
            "terminal_excluded_count": sum(1 for card in cards if card["terminal"]),
            "structurally_eligible_count": sum(
                1 for card in cards if card["structurally_eligible"]
            ),
            "actor_eligible_count": sum(1 for card in cards if card["actor_eligible"]),
            "claim_eligible_count": sum(1 for card in cards if card["claim_eligible"]),
            "now_refill_promotion_count": promotion_count,
            "work_ball_count": len(work_balls),
        },
        "boundaries": [
            "StateStore TaskSpec revisions are operational task authority when present",
            "registry/queue.json never gates membership in the projected frontier",
            "actor-dependent blockers do not erase structurally runnable supply",
            "terminal tasks are excluded from executable projection lanes",
            "claim and dispatch mutation remain a later T005 concern",
        ],
    }
    material["compatibility_queue"] = compatibility_queue(
        material, queue_policy=registry.queue_policy
    )
    material["projection_sha256"] = legacy.sha256_json(material)
    return material
