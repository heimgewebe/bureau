from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from . import legacy
from .approval import require_approval, reviewed_plan_approval
from .core import Dispatcher, Registry, StateStore
from .frontier import EXECUTABLE_LANES, build_frontier_projection

TERMINAL_STATES = {"verified", "cancelled", "superseded"}


def _validate_resource_filter(registry: Registry, resource: str | None) -> None:
    if resource is not None and resource not in registry.resources:
        raise legacy.StateError(f"unknown resource filter: {resource}")


def _queue_path(registry: Registry) -> Path:
    return registry.root / "registry" / "queue.json"


def _read_queue(registry: Registry) -> dict[str, Any]:
    return legacy.read_json(_queue_path(registry))


def _queue_sha256(queue: dict[str, Any]) -> str:
    return legacy.sha256_json(queue)


def _queue_text(queue: dict[str, Any]) -> str:
    return json.dumps(queue, ensure_ascii=False, indent=2) + "\n"


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


def _require_bound_git_head(root: Path, planned_git_head: Any) -> str:
    if not isinstance(planned_git_head, str) or not planned_git_head:
        raise legacy.StateError("queue reconcile plan lacks a bound registry git head")
    current_git_head = _git_head(root)
    if not current_git_head:
        raise legacy.StateError("current registry git head is unavailable")
    if current_git_head != planned_git_head:
        raise legacy.StateError(
            "registry git head changed since queue reconcile plan was generated"
        )
    return current_git_head


def _require_bound_registry_root(root: Path, planned_root: Any) -> None:
    if not isinstance(planned_root, str) or not planned_root:
        raise legacy.StateError("queue reconcile plan lacks a bound registry root")
    if Path(planned_root).expanduser().resolve() != root.resolve():
        raise legacy.StateError("queue reconcile plan registry root does not match apply root")


def _lane_positions(queue: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for lane in legacy.LANE_ORDER:
        for task_id in queue.get("lanes", {}).get(lane, []):
            task_id = str(task_id)
            if task_id in result:
                raise legacy.StateError(f"task {task_id} appears twice in compatibility queue")
            result[task_id] = lane
    return result


def _card_index(projection: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for lane in projection.get("lanes", {}).values():
        if not isinstance(lane, list):
            continue
        for item in lane:
            if isinstance(item, dict) and isinstance(item.get("task_id"), str):
                result[item["task_id"]] = item
    return result


def _scoped_compatibility_queue(
    registry: Registry,
    current: dict[str, Any],
    projection: dict[str, Any],
    resource: str | None,
) -> dict[str, Any]:
    desired = projection["compatibility_queue"]
    if resource is None:
        return desired
    scoped_ids = set(_card_index(projection))
    for task in registry.tasks.values():
        if any(
            legacy.overlaps(claim.resource, resource, registry.resources)
            for claim in task.claims
        ):
            scoped_ids.add(task.id)
    merged = {
        **current,
        "lanes": {
            lane: [
                str(task_id)
                for task_id in current.get("lanes", {}).get(lane, [])
                if str(task_id) not in scoped_ids
            ]
            for lane in legacy.LANE_ORDER
        },
    }
    for lane in EXECUTABLE_LANES:
        merged["lanes"][lane].extend(
            str(task_id) for task_id in desired.get("lanes", {}).get(lane, [])
        )
    return merged


def _finding(
    *,
    code: str,
    severity: str,
    task_id: str,
    current_lane: str | None,
    desired_lane: str | None,
    card: dict[str, Any] | None,
    proposed_action: dict[str, Any],
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "task_id": task_id,
        "title": None if card is None else card.get("title"),
        "effective_state": None if card is None else card.get("effective_state"),
        "queue_lane": current_lane,
        "projected_lane": desired_lane,
        "priority_lane": (
            None if card is None else card.get("priority", {}).get("lane")
        ),
        "claim_resources": [] if card is None else list(card.get("claim_resources", [])),
        "structural_reasons": [] if card is None else list(card.get("structural_reasons", [])),
        "actor_dependent_reasons": (
            [] if card is None else list(card.get("actor_dependent_reasons", []))
        ),
        "rule": "git_queue_must_match_dynamic_frontier_compatibility_projection",
        "recommendation": "materialize_compatibility_projection",
        "proposed_action": proposed_action,
    }


def _repo_focus(registry: Registry, projection: dict[str, Any]) -> dict[str, Any]:
    cards = _card_index(projection)
    result: dict[str, Any] = {}
    repo_resources = sorted(
        (
            resource
            for resource in registry.resources.values()
            if resource.id.startswith("repo.")
        ),
        key=lambda item: item.id,
    )
    for repo in repo_resources:
        matching = [
            card
            for card in cards.values()
            if any(
                legacy.overlaps(str(resource), repo.id, registry.resources)
                for resource in card.get("claim_resources", [])
            )
        ]
        lanes = {
            lane: [
                str(card["task_id"])
                for card in matching
                if card.get("projected_lane") == lane
            ]
            for lane in EXECUTABLE_LANES
        }
        current_ball = None
        for lane in EXECUTABLE_LANES:
            if lanes[lane]:
                current_ball = {"task_id": lanes[lane][0], "queue_lane": lane}
                break
        result[repo.id] = {
            "open_task_count": len(matching),
            "queued_task_count": sum(len(value) for value in lanes.values()),
            "lanes": lanes,
            "current_ball": current_ball,
        }
    return result


def queue_reconcile_report(
    registry: Registry,
    store: StateStore,
    *,
    resource: str | None = None,
    _check_runtime: bool = True,
) -> dict[str, Any]:
    """Compare the checked-in queue with the authoritative dynamic projection.

    The report never treats ``registry/queue.json`` as admission truth. It is a
    compatibility drift report whose target is rendered by :mod:`bureau.frontier`.
    """

    _validate_resource_filter(registry, resource)
    projection = build_frontier_projection(
        registry,
        store,
        resource=resource,
        check_runtime=_check_runtime,
    )
    current = _read_queue(registry)
    desired = _scoped_compatibility_queue(registry, current, projection, resource)
    current_positions = _lane_positions(current)
    desired_positions = _lane_positions(desired)
    cards = _card_index(projection)
    findings: list[dict[str, Any]] = []

    for task_id in sorted(set(current_positions) | set(desired_positions)):
        current_lane = current_positions.get(task_id)
        desired_lane = desired_positions.get(task_id)
        if current_lane == desired_lane:
            continue
        card = cards.get(task_id)
        if desired_lane is None:
            code = (
                "terminal-task-in-queue"
                if card is None or card.get("terminal") is True
                else "compatibility-task-not-in-frontier"
            )
            action = {"operation": "remove_from_queue", "target_lane": None}
        elif current_lane is None and desired_lane == "now":
            code = "unqueued-ready-priority-now"
            action = {"operation": "add_to_queue", "target_lane": "now"}
        elif current_lane is None and desired_lane == "next":
            code = "unqueued-open-priority-next"
            action = {"operation": "add_to_queue", "target_lane": "next"}
        elif current_lane == "later" and desired_lane in {"now", "next"}:
            code = "queued-later-priority-now-or-next"
            action = {"operation": "move_in_queue", "target_lane": desired_lane}
        else:
            code = "compatibility-queue-lane-drift"
            action = {"operation": "move_in_queue", "target_lane": desired_lane}
        findings.append(
            _finding(
                code=code,
                severity="warning",
                task_id=task_id,
                current_lane=current_lane,
                desired_lane=desired_lane,
                card=card,
                proposed_action=action,
            )
        )

    for lane in EXECUTABLE_LANES:
        current_ids = [str(item) for item in current.get("lanes", {}).get(lane, [])]
        desired_ids = [str(item) for item in desired.get("lanes", {}).get(lane, [])]
        if current_ids == desired_ids or set(current_ids) != set(desired_ids):
            continue
        findings.append(
            {
                "code": "compatibility-queue-order-drift",
                "severity": "warning",
                "lane": lane,
                "current_task_ids": current_ids,
                "projected_task_ids": desired_ids,
                "rule": "git_queue_order_must_match_dynamic_frontier_projection",
                "recommendation": "materialize_compatibility_projection",
                "proposed_action": {"operation": "reorder_lane", "target_lane": lane},
            }
        )

    summary = {
        "queued_now": len(current.get("lanes", {}).get("now", [])),
        "queued_next": len(current.get("lanes", {}).get("next", [])),
        "queued_later": len(current.get("lanes", {}).get("later", [])),
        "projected_now": len(desired.get("lanes", {}).get("now", [])),
        "projected_next": len(desired.get("lanes", {}).get("next", [])),
        "projected_later": len(desired.get("lanes", {}).get("later", [])),
        "findings": len(findings),
        "promote_to_now_candidates": sum(
            1 for item in findings if item.get("projected_lane") == "now"
        ),
        "promote_to_next_candidates": sum(
            1 for item in findings if item.get("projected_lane") == "next"
        ),
        "lane_mismatch_candidates": sum(
            1 for item in findings if item.get("code") == "compatibility-queue-lane-drift"
        ),
        "blockers": 0,
        "compatibility_converged": _queue_sha256(current) == _queue_sha256(desired),
    }
    return {
        "schema_version": 2,
        "command": "queue-reconcile",
        "read_only": True,
        "queue_canonical": False,
        "queue_authoritative": False,
        "queue_role": "compatibility_projection_only",
        "resource": resource,
        "summary": summary,
        "findings": findings,
        "repo_focus": _repo_focus(registry, projection),
        "frontier_projection_sha256": projection["projection_sha256"],
        "frontier_authority": projection["authority"],
        "compatibility_queue": desired,
        "does_not_establish": [
            "queue_admission_authority",
            "lane_promotion_authority",
            "dispatch_authority",
            "merge_authority",
            "completion_authority",
        ],
    }


def _plan_actions(report: dict[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None, str]] = set()
    for finding in report.get("findings", []):
        proposed = finding.get("proposed_action")
        if not isinstance(proposed, dict):
            continue
        operation = str(proposed.get("operation"))
        target_lane = proposed.get("target_lane")
        task_id = finding.get("task_id")
        identity = str(task_id) if isinstance(task_id, str) else f"lane:{finding.get('lane')}"
        key = (operation, target_lane if isinstance(target_lane, str) else None, identity)
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


def queue_reconcile_plan(
    registry: Registry,
    store: StateStore,
    *,
    resource: str | None = None,
) -> dict[str, Any]:
    report = queue_reconcile_report(registry, store, resource=resource)
    queue_before = _read_queue(registry)
    expected = report["compatibility_queue"]
    return {
        "schema_version": 2,
        "command": "queue-reconcile-plan",
        "created_at": legacy.utc_now(),
        "resource": resource,
        "registry": {
            "root": str(registry.root),
            "git_head": _git_head(registry.root),
            "queue_sha256_before": _queue_sha256(queue_before),
        },
        "frontier_projection_sha256": report["frontier_projection_sha256"],
        "dry_run_report_sha256": legacy.sha256_json(report),
        "actions": _plan_actions(report),
        "expected_queue_after": expected,
        "expected_queue_after_sha256": _queue_sha256(expected),
        "review": {
            "required": True,
            "status": "pending",
            "instructions": (
                "Review the dynamic-frontier compatibility projection. To materialize it, "
                "set status to reviewed and add reviewer plus reviewed_at."
            ),
        },
        "does_not_establish": [
            "queue_admission_authority",
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
    if plan.get("schema_version") != 2 or plan.get("command") != "queue-reconcile-plan":
        raise legacy.StateError("queue reconcile plan has unsupported schema or command")
    review = plan.get("review")
    if not isinstance(review, dict) or review.get("status") != "reviewed":
        raise legacy.StateError("queue reconcile plan is not reviewed")
    if not review.get("reviewer") or not review.get("reviewed_at"):
        raise legacy.StateError("reviewed queue reconcile plan requires reviewer and reviewed_at")
    plan_reference = str(Path(path).expanduser())
    plan["approval"] = require_approval(
        "queue_mutation",
        reviewed_plan_approval(
            reviewer=str(review["reviewer"]),
            reference=plan_reference,
            approved=True,
            scope="queue_mutation",
        ),
        expected_reference=plan_reference,
    )
    return plan


def apply_queue_reconcile_plan(
    registry: Registry,
    store: StateStore,
    path: str | Path,
    *,
    resource: str | None = None,
) -> dict[str, Any]:
    """Materialize the reviewed compatibility queue with rollback on drift."""

    plan = _load_reviewed_plan(path)
    if plan.get("resource") != resource:
        raise legacy.StateError("queue reconcile plan resource does not match apply resource")
    plan_registry = plan.get("registry")
    if not isinstance(plan_registry, dict):
        raise legacy.StateError("queue reconcile plan lacks a registry binding")
    _require_bound_registry_root(registry.root, plan_registry.get("root"))
    planned_git_head = plan_registry.get("git_head")
    current_git_head = _require_bound_git_head(registry.root, planned_git_head)
    current_queue = _read_queue(registry)
    current_queue_sha = _queue_sha256(current_queue)
    if current_queue_sha != plan_registry.get("queue_sha256_before"):
        raise legacy.StateError("queue changed since queue reconcile plan was generated")
    current_report = queue_reconcile_report(registry, store, resource=resource)
    if legacy.sha256_json(current_report) != plan.get("dry_run_report_sha256"):
        raise legacy.StateError("dynamic frontier changed since queue plan review")
    if current_report["frontier_projection_sha256"] != plan.get("frontier_projection_sha256"):
        raise legacy.StateError("frontier projection changed since queue plan review")
    if _plan_actions(current_report) != plan.get("actions"):
        raise legacy.StateError("queue reconcile actions changed since plan review")
    expected = plan.get("expected_queue_after")
    if not isinstance(expected, dict):
        raise legacy.StateError("queue reconcile plan lacks expected_queue_after")
    expected_sha = _queue_sha256(expected)
    if expected_sha != plan.get("expected_queue_after_sha256"):
        raise legacy.StateError("queue reconcile plan expected queue hash mismatch")
    if expected != current_report["compatibility_queue"]:
        raise legacy.StateError("reviewed queue differs from current dynamic projection")
    _require_bound_git_head(registry.root, planned_git_head)
    if expected_sha == current_queue_sha:
        return {
            "schema_version": 2,
            "command": "queue-reconcile-apply",
            "applied": False,
            "no_op": True,
            "queue_authoritative": False,
            "resource": resource,
            "path": str(Path(path).expanduser()),
            "registry_git_head": current_git_head,
            "queue_sha256_before": current_queue_sha,
            "queue_sha256_after": expected_sha,
            "actions": [],
            "approval": plan.get("approval"),
            "post_gates": None,
        }

    queue_path = _queue_path(registry)
    before_text = queue_path.read_text(encoding="utf-8")
    legacy.atomic_write(queue_path, _queue_text(expected))
    try:
        _require_bound_git_head(registry.root, planned_git_head)
        registry_after = Registry.load(registry.root)
        from .registry_truth import registry_truth_diagnostics

        state_integrity = store.integrity()
        doctor = Dispatcher(registry_after, store).doctor(False)
        registry_truth = registry_truth_diagnostics(registry.root)
        post_report = queue_reconcile_report(
            registry_after,
            store,
            resource=resource,
            _check_runtime=False,
        )
        gates = {
            "bureau_check": (
                state_integrity["integrity"] == "ok"
                and not state_integrity["foreign_key_errors"]
            ),
            "doctor_healthy": doctor["healthy"],
            "registry_truth_healthy": registry_truth["healthy"],
            "compatibility_queue_converged": post_report["summary"][
                "compatibility_converged"
            ],
        }
        required = {
            key: gates[key]
            for key in (
                "bureau_check",
                "registry_truth_healthy",
                "compatibility_queue_converged",
            )
        }
        if not all(required.values()):
            raise legacy.StateError(
                "post-apply gates failed: "
                + legacy.canonical_json({"required": required, "observed": gates})
            )
        _require_bound_git_head(registry.root, planned_git_head)
    except Exception:
        legacy.atomic_write(queue_path, before_text)
        raise
    return {
        "schema_version": 2,
        "command": "queue-reconcile-apply",
        "applied": True,
        "no_op": False,
        "queue_authoritative": False,
        "resource": resource,
        "path": str(Path(path).expanduser()),
        "registry_git_head": current_git_head,
        "queue_sha256_before": current_queue_sha,
        "queue_sha256_after": expected_sha,
        "actions": plan.get("actions", []),
        "approval": plan.get("approval"),
        "post_gates": gates,
        "post_gate_policy": {
            "required": [
                "bureau_check",
                "registry_truth_healthy",
                "compatibility_queue_converged",
            ],
            "observed_only": ["doctor_healthy"],
        },
        "does_not_establish": [
            "queue_admission_authority",
            "dispatch_authority",
            "task_claim",
            "task_completion",
            "merge_readiness",
        ],
    }
