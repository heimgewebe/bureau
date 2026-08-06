"""Keep the canonical Bureau Now lane supplied with structurally runnable work.

The Now lane is a prioritisation surface, not an authority bypass. This module only
moves existing canonical tasks from Next to Now. Claim, approval, capability, lease,
open-PR and runtime gates remain authoritative at pickup time.
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
from .v2 import Registry

SCHEMA_VERSION = 1
ACTOR_CAPABILITY_PREFIX = "missing capabilities: "
EXECUTION_REASON_PREFIX = "execution is "


@dataclass(frozen=True)
class NowRefillPolicy:
    """Hysteresis policy for bounded Next-to-Now promotion."""

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


def _actor_dependent_reason(reason: str) -> bool:
    if reason.startswith(ACTOR_CAPABILITY_PREFIX):
        return True
    if not reason.startswith(EXECUTION_REASON_PREFIX):
        return False
    execution = reason.removeprefix(EXECUTION_REASON_PREFIX)
    mode, separator, _policy = execution.partition("/")
    return bool(separator) and mode != "manual"


def _structural_reasons(item: dict[str, Any]) -> list[str]:
    return [
        str(reason)
        for reason in item.get("claim_reasons", [])
        if not _actor_dependent_reason(str(reason))
    ]


def _candidate(item: dict[str, Any], *, lane: str) -> bool:
    return (
        item.get("queue_lane") == lane
        and item.get("effective_state") == "ready"
        and not _structural_reasons(item)
    )


def _lane_order(registry: Registry, lane: str) -> dict[str, int]:
    queue = legacy.read_json(_queue_path(registry))
    return {
        str(task_id): index
        for index, task_id in enumerate(queue.get("lanes", {}).get(lane, []))
    }


def build_now_refill_report(
    registry: Registry,
    store: StateStore,
    *,
    policy: NowRefillPolicy | None = None,
    _check_runtime: bool = True,
) -> dict[str, Any]:
    """Build a revision-bound, read-only refill decision.

    Capabilities and review policy are actor-dependent and therefore do not decide
    queue placement. All structural claim blockers still exclude a candidate:
    dependencies, child gates, active runs, initiative limits, resource conflicts,
    open-PR overlap, queue absence and non-ready state.

    The decision is bound to the same runtime-execution truth the claim path checks
    at ``claim_intent`` time (Bureau checkout drift, dirty working tree, StateStore
    availability). ``_check_runtime`` is a private knob used only by
    :func:`apply_now_refill`'s own post-write consistency read, where the working
    tree is expected to carry our own not-yet-committed change.
    """
    selected_policy = policy or NowRefillPolicy()
    dispatcher = Dispatcher(registry, store)
    runtime_truth = dispatcher._runtime_execution_truth() if _check_runtime else None
    runtime_blocked = bool(_check_runtime and (runtime_truth or {}).get("execution_blocked"))
    frontier = dispatcher.frontier(_all_declared_capabilities(registry))
    now_items = [item for item in frontier if _candidate(item, lane="now")]
    next_items = [item for item in frontier if _candidate(item, lane="next")]
    next_order = _lane_order(registry, "next")
    next_items.sort(
        key=lambda item: (
            next_order.get(str(item.get("task_id")), 10**9),
            registry.tasks[str(item["task_id"])].rank,
            str(item["task_id"]),
        )
    )

    triggered = len(now_items) < selected_policy.floor
    shortage = max(0, selected_policy.target - len(now_items)) if triggered else 0
    promotion_limit = 0 if runtime_blocked else min(shortage, selected_policy.max_promotions)
    selected = next_items[:promotion_limit]
    blockers: list[str] = []
    if runtime_blocked:
        blockers.append("runtime-execution-blocked")
    elif triggered and not selected:
        blockers.append("no-structurally-runnable-next-task")

    queue = legacy.read_json(_queue_path(registry))
    git_head = _git_head(registry.root)
    promotions = [
        {
            "task_id": str(item["task_id"]),
            "title": str(item["title"]),
            "from_lane": "next",
            "to_lane": "now",
            "structural_reasons": [],
            "actor_dependent_reasons": [
                str(reason)
                for reason in item.get("claim_reasons", [])
                if _actor_dependent_reason(str(reason))
            ],
        }
        for item in selected
    ]
    if runtime_blocked:
        status = "blocked"
    elif not triggered:
        status = "satisfied"
    elif promotions:
        status = "refill-planned"
    else:
        status = "blocked"
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "bureau_now_lane_refill_report",
        "status": status,
        "policy": {
            "floor": selected_policy.floor,
            "target": selected_policy.target,
            "max_promotions": selected_policy.max_promotions,
        },
        "registry": {
            "root": str(registry.root),
            "git_head": git_head,
            "queue_sha256_before": _queue_sha256(queue),
        },
        "runtime": runtime_truth,
        "metrics": {
            "queued_now_count": len(queue.get("lanes", {}).get("now", [])),
            "queued_next_count": len(queue.get("lanes", {}).get("next", [])),
            "structurally_runnable_now_count": len(now_items),
            "structurally_runnable_next_count": len(next_items),
            "shortage_to_target": shortage,
            "promotion_count": len(promotions),
        },
        "promotions": promotions,
        "blockers": blockers,
        "boundaries": [
            "queue promotion does not claim or start a task",
            "pickup-time approval, capability, lease, PR and runtime gates remain authoritative",
            "only existing ready tasks move from Next to Now",
        ],
    }
    report["report_sha256"] = legacy.sha256_json(report)
    return report


def _apply_promotions(queue: dict[str, Any], promotions: list[dict[str, Any]]) -> dict[str, Any]:
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


def apply_now_refill(
    registry: Registry,
    store: StateStore,
    *,
    authority: str,
    policy: NowRefillPolicy | None = None,
) -> dict[str, Any]:
    """Apply one bounded refill with revision/hash binding and rollback."""
    if not authority.strip():
        raise legacy.StateError("explicit queue-refill authority is required")
    report = build_now_refill_report(registry, store, policy=policy)
    if report["status"] != "refill-planned":
        return {
            **report,
            "applied": False,
            "changed": False,
            "authority": authority,
        }

    queue_path = _queue_path(registry)
    before_text = queue_path.read_text(encoding="utf-8")
    queue_before = legacy.read_json(queue_path)
    if _queue_sha256(queue_before) != report["registry"]["queue_sha256_before"]:
        raise legacy.StateError("queue changed after Now-refill report generation")
    if _git_head(registry.root) != report["registry"]["git_head"]:
        raise legacy.StateError("registry Git head changed after Now-refill report generation")
    if Dispatcher(registry, store)._runtime_execution_truth().get("execution_blocked"):
        raise legacy.StateError("runtime execution is blocked; refusing Now-refill apply")
    expected = _apply_promotions(queue_before, report["promotions"])
    legacy.atomic_write(queue_path, json.dumps(expected, ensure_ascii=False, indent=2) + "\n")
    try:
        if _git_head(registry.root) != report["registry"]["git_head"]:
            raise legacy.StateError("registry Git head changed during Now refill")
        registry_after = Registry.load(registry.root)
        # The working tree now carries our own uncommitted promotion; skip the
        # runtime-execution (checkout-dirty) gate for this internal consistency
        # read only. The gate already ran, unblocked, immediately before the write.
        post = build_now_refill_report(
            registry_after, store, policy=policy, _check_runtime=False
        )
        expected_minimum = min(
            report["policy"]["floor"],
            report["metrics"]["structurally_runnable_now_count"]
            + report["metrics"]["promotion_count"],
        )
        if post["metrics"]["structurally_runnable_now_count"] < expected_minimum:
            raise legacy.StateError("Now-refill post-readback lost a promoted task")
    except Exception:
        legacy.atomic_write(queue_path, before_text)
        raise
    return {
        **report,
        "applied": True,
        "changed": True,
        "authority": authority,
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
