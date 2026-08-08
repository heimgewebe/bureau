"""Legacy Now-lane compatibility projection for Bureau Control Plane v3.

The operational lane decision is made by :mod:`bureau.frontier` from StateStore
TaskSpecs and live blockers. This module keeps the old bounded Next-to-Now surface
for compatibility consumers; writing ``registry/queue.json`` never establishes
admission, claim or dispatch authority.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import legacy
from .core import Dispatcher, StateStore
from .frontier import FrontierPolicy, build_frontier_projection
from .v2 import Registry

SCHEMA_VERSION = 2


@dataclass(frozen=True)
class NowRefillPolicy:
    """Hysteresis policy retained for legacy Now-refill callers."""

    floor: int = 2
    target: int = 4
    max_promotions: int = 4

    def __post_init__(self) -> None:
        if self.floor < 1:
            raise ValueError("Now floor must be at least one")
        if self.target < self.floor:
            raise ValueError("Now target must be greater than or equal to the floor")
        if self.max_promotions < 1:
            raise ValueError("max_promotions must be at least one")


def _queue_path(registry: Registry) -> Path:
    return registry.root / "registry" / "queue.json"


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
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None


def _all_declared_capabilities(registry: Registry) -> set[str]:
    return {
        capability
        for task in registry.tasks.values()
        for capability in task.capabilities
    }


def _frontier_policy(policy: NowRefillPolicy) -> FrontierPolicy:
    return FrontierPolicy(
        now_floor=policy.floor,
        now_target=policy.target,
        max_now_promotions=policy.max_promotions,
        work_ball_limit=max(policy.target, policy.max_promotions),
    )


def _queue_positions(queue: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for lane in legacy.LANE_ORDER:
        for task_id in queue.get("lanes", {}).get(lane, []):
            result[str(task_id)] = lane
    return result


def _apply_promotions(
    queue: dict[str, Any], promotions: list[dict[str, Any]]
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
    for promotion in promotions:
        task_id = str(promotion["task_id"])
        for lane in legacy.LANE_ORDER:
            lanes[lane] = [item for item in lanes[lane] if item != task_id]
        lanes["now"].append(task_id)
    return updated


def _projected_refill(
    frontier: dict[str, Any], queue: dict[str, Any]
) -> tuple[int, int, list[dict[str, Any]], list[dict[str, Any]]]:
    positions = _queue_positions(queue)
    raw_promotions: list[dict[str, Any]] = []
    base_now_count = 0
    for item in frontier["lanes"]["now"]:
        if not item.get("structurally_eligible"):
            continue
        if item.get("projected_from_lane") == "next":
            raw_promotions.append(
                {
                    "task_id": str(item["task_id"]),
                    "title": str(item["title"]),
                    "from_lane": "next",
                    "to_lane": "now",
                    "structural_reasons": list(item["structural_reasons"]),
                    "actor_dependent_reasons": list(item["actor_dependent_reasons"]),
                }
            )
        else:
            base_now_count += 1
    structurally_runnable_next_count = sum(
        1
        for item in frontier["lanes"]["next"]
        if item.get("structurally_eligible")
    ) + len(raw_promotions)
    needed_promotions = [
        item
        for item in raw_promotions
        if positions.get(str(item["task_id"])) != "now"
    ]
    return (
        base_now_count,
        structurally_runnable_next_count,
        raw_promotions,
        needed_promotions,
    )


def build_now_refill_report(
    registry: Registry,
    store: StateStore,
    *,
    policy: NowRefillPolicy | None = None,
    _check_runtime: bool = True,
) -> dict[str, Any]:
    """Project the old Now-refill decision from the authoritative frontier.

    Runtime failure blocks effects but does not erase structural supply metrics.
    When a compatibility queue already reflects the projected refill, the result
    is ``satisfied`` even though the dynamic frontier continues to derive those
    same Next-to-Now promotions from TaskSpec priority and live supply.
    """

    selected_policy = policy or NowRefillPolicy()
    queue = legacy.read_json(_queue_path(registry))
    decision_frontier = build_frontier_projection(
        registry,
        store,
        capabilities=_all_declared_capabilities(registry),
        policy=_frontier_policy(selected_policy),
        check_runtime=_check_runtime,
    )
    runtime_blocked = decision_frontier["runtime"].get("execution_blocked") is True
    structural_frontier = (
        build_frontier_projection(
            registry,
            store,
            capabilities=_all_declared_capabilities(registry),
            policy=_frontier_policy(selected_policy),
            check_runtime=False,
        )
        if runtime_blocked
        else decision_frontier
    )
    (
        base_now_count,
        structurally_runnable_next_count,
        raw_promotions,
        needed_promotions,
    ) = _projected_refill(structural_frontier, queue)
    triggered = base_now_count < selected_policy.floor
    shortage = max(0, selected_policy.target - base_now_count) if triggered else 0
    blockers: list[str] = []
    if runtime_blocked:
        blockers.append("runtime-execution-blocked")
    elif triggered and not raw_promotions:
        blockers.append("no-structurally-runnable-next-task")
    if runtime_blocked:
        status = "blocked"
        promotions: list[dict[str, Any]] = []
    elif needed_promotions:
        status = "refill-planned"
        promotions = needed_promotions
    elif raw_promotions or not triggered:
        status = "satisfied"
        promotions = []
    else:
        status = "blocked"
        promotions = []

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "bureau_now_lane_refill_report",
        "status": status,
        "authority": "bureau_dynamic_frontier_projection",
        "queue_authoritative": False,
        "queue_role": "compatibility_projection_only",
        "policy": {
            "floor": selected_policy.floor,
            "target": selected_policy.target,
            "max_promotions": selected_policy.max_promotions,
        },
        "registry": {
            "root": str(registry.root),
            "git_head": _git_head(registry.root),
            "queue_sha256_before": _queue_sha256(queue),
        },
        "runtime": decision_frontier["runtime"],
        "frontier_projection_sha256": structural_frontier["projection_sha256"],
        "runtime_projection_sha256": decision_frontier["projection_sha256"],
        "metrics": {
            "queued_now_count": len(queue.get("lanes", {}).get("now", [])),
            "queued_next_count": len(queue.get("lanes", {}).get("next", [])),
            "structurally_runnable_now_count": base_now_count + len(raw_promotions),
            "structurally_runnable_next_count": structurally_runnable_next_count,
            "shortage_to_target": shortage,
            "projected_promotion_count": len(raw_promotions),
            "compatibility_promotion_count": len(promotions),
        },
        "promotions": promotions,
        "blockers": blockers,
        "boundaries": [
            "Now supply is derived from the StateStore-backed dynamic frontier",
            "runtime blockers suppress effects without erasing structural supply",
            "registry/queue.json is compatibility output, not admission authority",
            "queue projection does not claim or start a task",
            "pickup-time approval, capability, lease, PR and runtime gates remain authoritative",
        ],
    }
    report["report_sha256"] = legacy.sha256_json(report)
    return report


def apply_now_refill(
    registry: Registry,
    store: StateStore,
    *,
    authority: str,
    policy: NowRefillPolicy | None = None,
) -> dict[str, Any]:
    """Materialize one bounded compatibility refill with rollback."""

    if not authority.strip():
        raise legacy.StateError("explicit queue-refill authority is required")
    selected_policy = policy or NowRefillPolicy()
    report = build_now_refill_report(registry, store, policy=selected_policy)
    if report["status"] != "refill-planned":
        return {
            **report,
            "applied": False,
            "changed": False,
            "authority_evidence": authority,
        }

    # Re-observe runtime/frontier immediately before the first file effect.
    guard = build_now_refill_report(registry, store, policy=selected_policy)
    if guard["runtime"].get("execution_blocked") is True:
        raise legacy.StateError("runtime changed to blocked before Now-refill effect")
    if (
        guard["frontier_projection_sha256"] != report["frontier_projection_sha256"]
        or guard["promotions"] != report["promotions"]
    ):
        raise legacy.StateError("dynamic frontier changed before Now-refill effect")

    queue_path = _queue_path(registry)
    before_text = queue_path.read_text(encoding="utf-8")
    queue_before = legacy.read_json(queue_path)
    if _queue_sha256(queue_before) != report["registry"]["queue_sha256_before"]:
        raise legacy.StateError("queue changed after Now-refill report generation")
    if _git_head(registry.root) != report["registry"]["git_head"]:
        raise legacy.StateError("registry Git head changed after Now-refill report generation")
    expected = _apply_promotions(queue_before, report["promotions"])
    legacy.atomic_write(
        queue_path, json.dumps(expected, ensure_ascii=False, indent=2) + "\n"
    )
    try:
        if _git_head(registry.root) != report["registry"]["git_head"]:
            raise legacy.StateError("registry Git head changed during Now refill")
        registry_after = Registry.load(registry.root)
        post = build_now_refill_report(
            registry_after,
            store,
            policy=selected_policy,
            _check_runtime=False,
        )
        if post["status"] != "satisfied":
            raise legacy.StateError("Now-refill compatibility post-readback is not satisfied")
    except Exception:
        legacy.atomic_write(queue_path, before_text)
        raise
    return {
        **report,
        "applied": True,
        "changed": True,
        "authority_evidence": authority,
        "queue_sha256_after": _queue_sha256(expected),
        "post_readback": post,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bureau-now-refill")
    parser.add_argument("--root", default=".")
    parser.add_argument("--state-db")
    parser.add_argument("--state-root")
    parser.add_argument("--floor", type=int, default=2)
    parser.add_argument("--target", type=int, default=4)
    parser.add_argument("--max-promotions", type=int, default=4)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--authority", default="")
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser().resolve()
    registry = Registry.load(root)
    store = StateStore(
        Path(args.state_db).expanduser() if args.state_db else None,
        Path(args.state_root).expanduser() if args.state_root else None,
    )
    policy = NowRefillPolicy(
        floor=args.floor,
        target=args.target,
        max_promotions=args.max_promotions,
    )
    if args.apply:
        result = apply_now_refill(
            registry,
            store,
            authority=args.authority,
            policy=policy,
        )
    else:
        result = build_now_refill_report(registry, store, policy=policy)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 2 if result["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
