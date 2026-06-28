from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .adapters import AdapterRegistry
from .core import (
    BureauError,
    Claim,
    Dispatcher,
    NoEligibleTask,
    Registry,
    StateStore,
    cleanup_workspace,
    close_ready_initiatives,
    complete_run,
    create_workspace,
    fail_run,
    grabowski_handoff,
    lifecycle_diagnostics,
    preserve_workspace,
    verification_stamp,
    workspace_status,
)


def emit(value: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))
    elif isinstance(value, list):
        for item in value:
            print(json.dumps(item, ensure_ascii=False, sort_keys=True))
    elif isinstance(value, dict):
        for key, item in value.items():
            rendered = (
                json.dumps(item, ensure_ascii=False, sort_keys=True)
                if isinstance(item, (dict, list))
                else item
            )
            print(f"{key}: {rendered}")
    else:
        print(value)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="bureau")
    result.add_argument("--root", default=".")
    result.add_argument("--state-db")
    result.add_argument("--state-root")
    result.add_argument("--json", action="store_true")
    result.add_argument("--grabowski-source")
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    sub.add_parser("status")
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--repair", action="store_true")
    sub.add_parser("conflicts")
    sub.add_parser("lifecycle")
    sub.add_parser("close-ready")
    frontier = sub.add_parser("frontier")
    frontier.add_argument("--capability", action="append", default=[])
    explain = sub.add_parser("explain-next")
    explain.add_argument("--capability", action="append", default=[])
    claim = sub.add_parser("claim-next")
    claim.add_argument("--worker", required=True)
    claim.add_argument("--kind", default="interactive-agent")
    claim.add_argument("--capability", action="append", default=[])
    checkout = sub.add_parser("checkout-next")
    checkout.add_argument("--worker", required=True)
    checkout.add_argument("--kind", default="interactive-agent")
    checkout.add_argument("--capability", action="append", default=[])
    checkout.add_argument("--base-dir")
    checkout.add_argument("--dispatch", action="store_true")
    sub.add_parser("runs")
    run = sub.add_parser("run")
    run.add_argument("run_id")
    bind = sub.add_parser("bind")
    bind.add_argument("run_id")
    bind.add_argument("--system", required=True)
    bind.add_argument("--external-id", required=True)
    heartbeat = sub.add_parser("heartbeat")
    heartbeat.add_argument("run_id")
    heartbeat.add_argument("--worker")
    expand = sub.add_parser("claim-expand")
    expand.add_argument("run_id")
    expand.add_argument("--resource", required=True)
    expand.add_argument("--mode", required=True, choices=["read", "write", "exclusive", "capacity"])
    expand.add_argument("--amount", type=int, default=1)
    expand.add_argument("--isolation", default="none")
    expand.add_argument("--reason", required=True)
    reconcile = sub.add_parser("reconcile")
    reconcile.add_argument("--stale-after", type=int, default=900)
    complete = sub.add_parser("complete")
    complete.add_argument("run_id")
    complete.add_argument("--evidence", required=True)
    fail = sub.add_parser("fail")
    fail.add_argument("run_id")
    fail.add_argument("--error", required=True)
    handoff = sub.add_parser("handoff")
    handoff.add_argument("run_id")
    workspace = sub.add_parser("workspace-create")
    workspace.add_argument("run_id")
    workspace.add_argument("--base-dir")
    workspace_show = sub.add_parser("workspace-status")
    workspace_show.add_argument("run_id")
    workspace_clean = sub.add_parser("workspace-cleanup")
    workspace_clean.add_argument("run_id")
    workspace_clean.add_argument("--force", action="store_true")
    workspace_keep = sub.add_parser("workspace-preserve")
    workspace_keep.add_argument("run_id")
    workspace_keep.add_argument("--reason", required=True)
    stamp = sub.add_parser("verification-stamp")
    stamp.add_argument("task_id")
    return result


def default_grabowski_source() -> Path | None:
    configured_manifest = os.environ.get("BUREAU_GRABOWSKI_MANIFEST")
    manifest = (
        Path(configured_manifest).expanduser()
        if configured_manifest
        else Path.home() / ".local/share/grabowski-mcp/deployment-manifest.json"
    )
    try:
        deployment = json.loads(manifest.read_text(encoding="utf-8"))
        release = Path(deployment["immutable_release_path"]).expanduser().resolve()
        tasks_module = Path(deployment["module_paths"]["grabowski_tasks"]).expanduser().resolve()
        if (
            tasks_module.name == "grabowski_tasks.py"
            and tasks_module.is_file()
            and tasks_module.is_relative_to(release)
        ):
            return tasks_module.parent
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        pass
    checkout = Path.home() / "repos/grabowski/src"
    return checkout if checkout.is_dir() else None


def adapters(args: argparse.Namespace) -> AdapterRegistry:
    registry = AdapterRegistry()
    source = args.grabowski_source or os.environ.get("BUREAU_GRABOWSKI_SRC")
    candidate = Path(source).expanduser() if source else default_grabowski_source()
    if candidate is not None:
        try:
            from .grabowski_adapter import GrabowskiTaskAdapter

            registry.add(GrabowskiTaskAdapter(candidate))
        except Exception as exc:
            registry.mark_unavailable("grabowski-task", exc)
    return registry


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        registry = Registry.load(Path(args.root))
        state_path = Path(args.state_db).expanduser() if args.state_db else None
        state_root = Path(args.state_root).expanduser() if args.state_root else None
        store = StateStore(state_path, state_root)
        adapter_registry = adapters(args)
        dispatcher = Dispatcher(registry, store, adapter_registry)
        if args.command == "check":
            value = {
                "valid": True,
                **registry.summary(),
                "state": store.integrity(),
                "adapters": adapter_registry.status(),
            }
        elif args.command == "status":
            value = {
                **registry.summary(),
                "runs": store.list_runs(),
                "lifecycle": lifecycle_diagnostics(registry, store),
                "adapters": adapter_registry.status(),
            }
        elif args.command == "doctor":
            value = {**dispatcher.doctor(args.repair), "adapters": adapter_registry.status()}
        elif args.command == "conflicts":
            value = dispatcher.conflict_matrix()
        elif args.command == "lifecycle":
            value = lifecycle_diagnostics(registry, store)
        elif args.command == "close-ready":
            value = close_ready_initiatives(registry, store)
        elif args.command == "frontier":
            value = dispatcher.frontier(set(args.capability))
        elif args.command == "explain-next":
            value = dispatcher.explain_next(set(args.capability))
        elif args.command == "claim-next":
            value = dispatcher.claim_next(
                args.worker, tuple(sorted(set(args.capability))), args.kind
            )
        elif args.command == "checkout-next":
            base = Path(args.base_dir).expanduser() if args.base_dir else None
            value = dispatcher.checkout_next(
                args.worker,
                tuple(sorted(set(args.capability))),
                args.kind,
                base,
                args.dispatch,
            )
        elif args.command == "runs":
            value = store.list_runs()
        elif args.command == "run":
            value = store.run(args.run_id)
        elif args.command == "bind":
            value = store.bind(args.run_id, args.system, args.external_id)
        elif args.command == "heartbeat":
            value = store.heartbeat(args.run_id, args.worker)
        elif args.command == "claim-expand":
            value = dispatcher.expand_claim(
                args.run_id,
                Claim(args.resource, args.mode, args.amount, args.isolation),
                args.reason,
            )
        elif args.command == "reconcile":
            value = dispatcher.reconcile(args.stale_after)
        elif args.command == "complete":
            evidence = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
            value = complete_run(registry, store, args.run_id, evidence)
        elif args.command == "fail":
            value = fail_run(store, args.run_id, args.error)
        elif args.command == "handoff":
            value = grabowski_handoff(registry, store, args.run_id)
        elif args.command == "workspace-create":
            base = Path(args.base_dir).expanduser() if args.base_dir else None
            value = create_workspace(registry, store, args.run_id, base)
        elif args.command == "workspace-status":
            value = workspace_status(store, args.run_id)
        elif args.command == "workspace-cleanup":
            value = cleanup_workspace(store, args.run_id, args.force)
        elif args.command == "workspace-preserve":
            value = preserve_workspace(store, args.run_id, args.reason)
        elif args.command == "verification-stamp":
            value = verification_stamp(registry, store, args.task_id)
        else:
            raise AssertionError(args.command)
        emit(value, args.json)
        return 0
    except NoEligibleTask as exc:
        emit({"status": "no-eligible-task", "detail": str(exc)}, args.json)
        return 3
    except BureauError as exc:
        print(f"bureau: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
