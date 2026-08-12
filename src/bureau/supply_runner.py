"""Close the claimable-supply loop between the canonical dispatcher and the Registry.

`bureau.task_supply` can only preview: it demands an already authoritative frontier
snapshot bound to an exact Registry revision. Nothing produced that snapshot, so the
supply report the agent frontier reads never existed and a starved claimable frontier
stayed a read-only observation. This stage observes the authoritative frontier through
the canonical dispatcher, persists it as a revision-bound snapshot, writes the supply
report to the path `bureau-agent-frontier` already consumes, and publishes the bounded
fallback plan only when explicit mutation authority is granted.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import Dispatcher
from .cycle_contract import (
    CONTRACT_VERSION,
    SCHEMA_VERSION,
    atomic_json,
    cycle_id,
    utc_now,
    validate_receipt,
)
from .read_only_state import ReadOnlyStateStore
from .task_supply import (
    SupplyError,
    SupplyPolicy,
    _git_head,
    build_registry_supply_report,
    file_sha256,
    publish_supply_plan,
)
from .v2 import Registry

CYCLE_SCHEMA_VERSION = 1
CYCLE_KIND = "bureau_task_supply_cycle_result"
SNAPSHOT_KIND = "bureau_authoritative_frontier_snapshot"
GIT_HEAD_RE = re.compile(r"^[0-9a-f]{40}$")
SNAPSHOT_FIELDS = (
    "task_id",
    "title",
    "effective_state",
    "queue_lane",
    "eligible",
    "closure_bridge",
    "claim_reasons",
    "reasons",
)


def default_state_root() -> Path:
    return Path(
        os.environ.get(
            "BUREAU_TASK_SUPPLY_STATE_ROOT",
            Path.home() / ".local/state/bureau-task-supply",
        )
    ).expanduser()


def report_path(state_root: Path) -> Path:
    return state_root / "latest-report.json"


def snapshot_path(state_root: Path) -> Path:
    return state_root / "frontier-snapshot.json"


@dataclass(frozen=True)
class FrontierObservation:
    """One authoritative, revision-bound dispatcher observation."""

    frontier: tuple[dict[str, Any], ...]
    runtime_healthy: bool
    runtime_blocker_codes: tuple[str, ...]
    capabilities: tuple[str, ...]


def _projected_item(item: dict[str, Any]) -> dict[str, Any]:
    return {key: item[key] for key in SNAPSHOT_FIELDS if key in item}


def observe_authoritative_frontier(
    *,
    registry_root: Path,
    capabilities: Sequence[str],
    state_db: Path | None = None,
    state_store_root: Path | None = None,
) -> FrontierObservation:
    """Read the frontier the canonical claim path would evaluate, with its runtime gate."""
    selected = tuple(sorted({str(item) for item in capabilities if str(item)}))
    if not selected:
        raise SupplyError("at least one worker capability is required")
    registry = Registry.load(registry_root)
    store = ReadOnlyStateStore(state_db, state_store_root)
    dispatcher = Dispatcher(registry, store)
    # Same truth the claim path gates on, so supply health cannot diverge from dispatch.
    runtime_truth = dispatcher._runtime_execution_truth()
    frontier = dispatcher.frontier(set(selected))
    return FrontierObservation(
        frontier=tuple(_projected_item(item) for item in frontier),
        runtime_healthy=runtime_truth.get("execution_blocked") is not True,
        runtime_blocker_codes=tuple(
            str(code) for code in runtime_truth.get("blocker_codes", []) if code
        ),
        capabilities=selected,
    )


def _write_snapshot(
    path: Path,
    observation: FrontierObservation,
    *,
    registry_root: Path,
    registry_head: str,
    queue_sha256: str,
    generated_at: str,
) -> str:
    atomic_json(
        path,
        {
            "schema_version": CYCLE_SCHEMA_VERSION,
            "kind": SNAPSHOT_KIND,
            "generated_at": generated_at,
            "registry": {
                "root": str(registry_root),
                "head": registry_head,
                "queue_sha256": queue_sha256,
            },
            "capabilities": list(observation.capabilities),
            "runtime_healthy": observation.runtime_healthy,
            "frontier": [dict(item) for item in observation.frontier],
        },
    )
    return file_sha256(path)


def _publication_readback(
    observation: FrontierObservation, created_task_ids: Sequence[str]
) -> list[dict[str, Any]]:
    by_task = {str(item.get("task_id")): item for item in observation.frontier}
    result = []
    for task_id in created_task_ids:
        item = by_task.get(task_id)
        result.append(
            {
                "task_id": task_id,
                "present_in_frontier": item is not None,
                "effective_state": None if item is None else item.get("effective_state"),
                "queue_lane": None if item is None else item.get("queue_lane"),
                "claimable": bool(item is not None and item.get("eligible") is True),
                "reasons": [] if item is None else list(item.get("claim_reasons") or []),
            }
        )
    return result


def run_supply_cycle(
    *,
    registry_root: Path,
    capabilities: Sequence[str],
    state_root: Path | None = None,
    state_db: Path | None = None,
    state_store_root: Path | None = None,
    policy: SupplyPolicy | None = None,
    approval_available: bool = False,
    mutation_authority: bool = False,
    publish: bool = False,
    environment_blockers: Sequence[str] = (),
    generated_at: str | None = None,
    registry_head: str | None = None,
    acceptance_contracts: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    observer: Callable[..., FrontierObservation] = observe_authoritative_frontier,
    head_reader: Callable[[Path], str] = _git_head,
) -> dict[str, Any]:
    """Observe, report, and — only under explicit authority — publish bounded refill."""
    selected_root = (state_root or default_state_root()).expanduser()
    resolved_registry_root = registry_root.expanduser().resolve()
    now = generated_at or utc_now()
    selected_head_reader = head_reader
    if registry_head is not None:
        if not isinstance(registry_head, str) or GIT_HEAD_RE.fullmatch(registry_head) is None:
            raise SupplyError("registry_head must be an exact lowercase 40-character Git commit")
        if publish:
            raise SupplyError(
                "manifest-bound registry_head is read-only; publication requires "
                "a Git-bound Registry"
            )
        head = registry_head

        def selected_head_reader(_root: Path) -> str:
            return head
    else:
        head = head_reader(resolved_registry_root)
    queue_digest = file_sha256(resolved_registry_root / "registry/queue.json")
    observation = observer(
        registry_root=resolved_registry_root,
        capabilities=capabilities,
        state_db=state_db,
        state_store_root=state_store_root,
    )
    snapshot_file = snapshot_path(selected_root)
    snapshot_digest = _write_snapshot(
        snapshot_file,
        observation,
        registry_root=resolved_registry_root,
        registry_head=head,
        queue_sha256=queue_digest,
        generated_at=now,
    )
    blockers = list(environment_blockers)
    blockers.extend(f"runtime-blocker:{code}" for code in observation.runtime_blocker_codes)
    report = build_registry_supply_report(
        registry_root=resolved_registry_root,
        frontier=list(observation.frontier),
        policy=policy,
        approval_available=approval_available,
        runtime_healthy=observation.runtime_healthy,
        mutation_authority=mutation_authority,
        environment_blockers=tuple(blockers),
        frontier_registry_head=head,
        frontier_queue_sha256=queue_digest,
        frontier_snapshot_sha256=snapshot_digest,
        acceptance_contracts=acceptance_contracts,
        head_reader=selected_head_reader,
    )
    atomic_json(report_path(selected_root), report)

    plan = report["publication_plan"]
    publication: dict[str, Any] = {
        "attempted": False,
        "status": "preview-only",
        "created_task_ids": [],
        "post_publication_readback": [],
    }
    if not publish:
        publication["reason"] = "publication was not requested"
    elif not mutation_authority:
        publication["reason"] = "registry mutation authority is not granted"
    elif plan.get("status") != "authorized":
        publication["reason"] = "publication plan is not authorized and blocker-free"
    else:
        publication["attempted"] = True
        try:
            result = publish_supply_plan(
                plan,
                mutation_authorized=True,
                expected_plan_sha256=str(plan["plan_sha256"]),
                head_reader=selected_head_reader,
            )
        except SupplyError as exc:
            publication["status"] = "failed"
            publication["reason"] = str(exc)
        else:
            publication["status"] = "published"
            publication["reason"] = "bounded canonical refill published under explicit authority"
            publication["result"] = result
            publication["created_task_ids"] = list(result["created_task_ids"])
            publication["post_publication_readback"] = _publication_readback(
                observer(
                    registry_root=resolved_registry_root,
                    capabilities=capabilities,
                    state_db=state_db,
                    state_store_root=state_store_root,
                ),
                result["created_task_ids"],
            )
    return {
        "schema_version": CYCLE_SCHEMA_VERSION,
        "kind": CYCLE_KIND,
        "generated_at": now,
        "status": report["status"],
        "registry": report["registry"],
        "capabilities": list(observation.capabilities),
        "runtime_healthy": observation.runtime_healthy,
        "mutation_authority_observed": mutation_authority,
        "metrics": report["metrics"],
        "blockers": report["blockers"],
        "publication": publication,
        "report_path": str(report_path(selected_root)),
        "report_sha256": report["report_sha256"],
        "snapshot_path": str(snapshot_file),
        "snapshot_sha256": snapshot_digest,
        "does_not_establish": [
            "claimability of published work before the normal claim gates run again",
            "permission to bypass leases, capabilities, runtime health, or open-PR guards",
            "merge or deployment authority",
        ],
    }


def _cycle_result(summary: dict[str, Any]) -> str:
    if summary["status"] == "blocked":
        return "blocked"
    if summary["publication"]["status"] == "failed":
        return "failed"
    if summary["publication"]["status"] == "published":
        return "completed"
    if summary["status"] == "satisfied":
        return "idle"
    return "partial"


def _next_action(summary: dict[str, Any]) -> str:
    if summary["status"] == "satisfied":
        return "claimable supply is at or above its floor; dispatch normal work"
    if summary["status"] == "blocked":
        return "resolve the exact supply blockers before requesting mutation authority"
    if summary["publication"]["status"] == "published":
        return "re-run the normal claim path against the published canonical tasks"
    return "review the bounded publication plan and grant mutation authority for it"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bureau-task-supply-runner")
    parser.add_argument("--registry-root", default=".")
    parser.add_argument("--capability", action="append", default=[], required=True)
    parser.add_argument("--state-root", help="task-supply artifact root")
    parser.add_argument("--bureau-state-db")
    parser.add_argument("--bureau-state-root")
    parser.add_argument("--floor", type=int, default=8)
    parser.add_argument("--refill-target", type=int, default=12)
    parser.add_argument("--max-new-per-cycle", type=int, default=4)
    parser.add_argument("--bucket-hours", type=int, default=24)
    parser.add_argument("--approval-available", action="store_true")
    parser.add_argument("--mutation-authority", action="store_true")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--environment-blocker", action="append", default=[])
    args = parser.parse_args(argv)
    selected_root = (
        Path(args.state_root).expanduser() if args.state_root else default_state_root()
    )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    selected_cycle = cycle_id()
    started_at = utc_now()
    degraded = False
    evidence: list[dict[str, Any]] = []
    summary: dict[str, Any] | None = None
    try:
        summary = run_supply_cycle(
            registry_root=Path(args.registry_root).expanduser(),
            capabilities=args.capability,
            state_root=selected_root,
            state_db=Path(args.bureau_state_db).expanduser() if args.bureau_state_db else None,
            state_store_root=(
                Path(args.bureau_state_root).expanduser() if args.bureau_state_root else None
            ),
            policy=SupplyPolicy(
                floor=args.floor,
                refill_target=args.refill_target,
                max_new_per_cycle=args.max_new_per_cycle,
                bucket_hours=args.bucket_hours,
            ),
            approval_available=args.approval_available,
            mutation_authority=args.mutation_authority,
            publish=args.publish,
            environment_blockers=tuple(args.environment_blocker),
        )
        result = _cycle_result(summary)
        degraded = result in {"blocked", "failed"}
        evidence.append(
            {
                "kind": "task_supply_report",
                "path": summary["report_path"],
                "report_sha256": summary["report_sha256"],
                "status": summary["status"],
                "metrics": summary["metrics"],
                "blockers": summary["blockers"],
                "publication_status": summary["publication"]["status"],
                "created_task_ids": summary["publication"]["created_task_ids"],
            }
        )
        next_action = _next_action(summary)
    except Exception as exc:  # receipt first, crash never silent
        degraded = True
        result = "failed"
        evidence.append({"kind": "task_supply_error", "error": str(exc)[:2000]})
        next_action = "repair the task-supply stage before trusting claimable-supply counts"
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "cycle_id": selected_cycle,
        "stage": "watchdog",
        "run_id": f"task-supply-{stamp}",
        "trigger": "local-task-supply",
        "started_at": started_at,
        "finished_at": utc_now(),
        "lifecycle_state": "terminal",
        "result": result,
        "degraded": degraded,
        "evidence": evidence,
        "next_action": next_action,
        "receipt_path": str(selected_root / "runs" / f"{stamp}-task-supply.json"),
    }
    errors = validate_receipt(receipt, expected_stage="watchdog", expected_cycle_id=selected_cycle)
    if errors:
        raise RuntimeError("task supply receipt contract failed: " + "; ".join(errors))
    atomic_json(Path(receipt["receipt_path"]), receipt)
    atomic_json(selected_root / "latest.json", receipt)
    print(
        json.dumps(
            {
                "status": result,
                "supply_status": None if summary is None else summary["status"],
                "degraded": degraded,
                "report": None if summary is None else summary["report_path"],
                "receipt": receipt["receipt_path"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    if result == "failed":
        return 1
    return 2 if result == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
