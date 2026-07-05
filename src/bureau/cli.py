from __future__ import annotations

import argparse
import json
import os
import sqlite3
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
    runtime_drift_check,
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
    sub.add_parser("runtime-drift-check")
    sub.add_parser("conflicts")
    sub.add_parser("lifecycle")
    source_check = sub.add_parser("source-check")
    source_check.add_argument("source", choices=["weltgewebe"])
    source_check.add_argument("--repo", required=True)
    source_check.add_argument("--ref", default="origin/main")
    source_sync = sub.add_parser("source-sync")
    source_sync.add_argument("source", choices=["weltgewebe"])
    source_sync.add_argument("--repo", required=True)
    source_sync.add_argument("--ref", default="origin/main")
    source_sync.add_argument("--apply", action="store_true")
    promote = sub.add_parser("source-promote-plan")
    promote.add_argument("source", choices=["weltgewebe"])
    promote.add_argument("--task-id", required=True)
    sub.add_parser("close-ready")
    cabinet_graph = sub.add_parser("cabinet-graph")
    cabinet_graph.add_argument("--graph")
    cabinet_frontier = sub.add_parser("cabinet-frontier")
    cabinet_frontier.add_argument("--graph")
    cabinet_bridge_probe = sub.add_parser("cabinet-bridge-probe")
    cabinet_bridge_probe.add_argument("--bridge-policy")
    cabinet_promote = sub.add_parser("cabinet-promote")
    cabinet_promote.add_argument("--graph")
    cabinet_promote.add_argument("--frontier-export")
    cabinet_promote.add_argument("--candidate-id", required=True)
    cabinet_promote.add_argument("--task-id", required=True)
    cabinet_promote.add_argument("--initiative", required=True)
    cabinet_promote.add_argument("--target-proof", required=True)
    cabinet_promote.add_argument("--approve", action="store_true")
    cabinet_promote.add_argument("--write-task")
    cabinet_validate_task = sub.add_parser("cabinet-validate-task")
    cabinet_validate_task.add_argument("--task-file", required=True)
    cabinet_import_preview = sub.add_parser("cabinet-import-preview")
    cabinet_import_preview.add_argument("--task-file", required=True)
    cabinet_import_reviewed = sub.add_parser("cabinet-import-reviewed")
    cabinet_import_reviewed.add_argument("--task-file", required=True)
    cabinet_import_reviewed.add_argument("--reviewer", required=True)
    cabinet_import_reviewed.add_argument("--apply", action="store_true")
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


def read_only_state_integrity(args: argparse.Namespace) -> dict[str, Any]:
    if args.state_db:
        state_path = Path(args.state_db).expanduser()
    elif args.state_root:
        state_path = Path(args.state_root).expanduser() / "bureau.sqlite3"
    else:
        state_path = Path(os.environ.get("BUREAU_STATE_DIR", "~/.local/state/bureau")).expanduser()
        state_path = state_path / "bureau.sqlite3"
    if not state_path.is_file():
        return {"available": False, "path": str(state_path), "error": "missing"}
    try:
        connection = sqlite3.connect(f"file:{state_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign = [dict(row) for row in connection.execute("PRAGMA foreign_key_check")]
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.Error as exc:
        return {
            "available": False,
            "path": str(state_path),
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if "connection" in locals():
            connection.close()
    return {
        "available": True,
        "path": str(state_path),
        "integrity": integrity,
        "foreign_key_errors": foreign,
        "schema_version": version,
    }


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        root = Path(args.root)
        if args.command in {"cabinet-graph", "cabinet-frontier"}:
            from .cabinet_graph import (
                DEFAULT_GRAPH_PATH,
                CabinetGraphError,
                frontier_export,
                graph_report,
            )

            graph_path = args.graph or DEFAULT_GRAPH_PATH
            try:
                value = (
                    frontier_export(graph_path)
                    if args.command == "cabinet-frontier"
                    else graph_report(graph_path)
                )
            except CabinetGraphError as exc:
                print(f"bureau: {exc}", file=sys.stderr)
                return 2
            emit(value, args.json)
            return 0
        if args.command == "cabinet-bridge-probe":
            from .cabinet_bridge import (
                DEFAULT_BRIDGE_POLICY_PATH,
                CabinetBridgeError,
                bridge_probe,
            )

            bridge_policy_path = args.bridge_policy or DEFAULT_BRIDGE_POLICY_PATH
            try:
                value = bridge_probe(bridge_policy_path)
            except CabinetBridgeError as exc:
                print(f"bureau: {exc}", file=sys.stderr)
                return 2
            emit(value, args.json)
            return 0
        if args.command == "cabinet-promote":
            from .cabinet_graph import (
                DEFAULT_GRAPH_PATH,
                CabinetGraphError,
                frontier_export,
                load_frontier_export,
                promote_frontier_candidate,
            )
            from .cabinet_promotion_write import write_promotion_task

            try:
                if args.graph and args.frontier_export:
                    raise CabinetGraphError(
                        "cabinet-promote accepts either --graph or --frontier-export, not both"
                    )
                export = (
                    load_frontier_export(args.frontier_export)
                    if args.frontier_export
                    else frontier_export(args.graph or DEFAULT_GRAPH_PATH)
                )
                value = promote_frontier_candidate(
                    export,
                    candidate_id=args.candidate_id,
                    task_id=args.task_id,
                    initiative=args.initiative,
                    target_proof=args.target_proof,
                    approve=args.approve,
                )
                if args.write_task:
                    value = {
                        **value,
                        "write": write_promotion_task(value, args.write_task),
                    }
            except CabinetGraphError as exc:
                print(f"bureau: {exc}", file=sys.stderr)
                return 2
            emit(value, args.json)
            return 0
        if args.command == "cabinet-validate-task":
            from .cabinet_graph import CabinetGraphError
            from .cabinet_promotion_write import validate_promotion_task_file

            try:
                value = validate_promotion_task_file(args.task_file)
            except CabinetGraphError as exc:
                print(f"bureau: {exc}", file=sys.stderr)
                return 2
            emit(value, args.json)
            return 0
        state_path = Path(args.state_db).expanduser() if args.state_db else None
        state_root = Path(args.state_root).expanduser() if args.state_root else None
        if args.command == "runtime-drift-check":
            value = runtime_drift_check(root, state_db=state_path, state_root=state_root)
            emit(value, args.json)
            return 0
        registry = Registry.load(root)
        if args.command == "cabinet-import-preview":
            from .cabinet_graph import CabinetGraphError
            from .cabinet_promotion_write import preview_promotion_task_import_file

            try:
                value = preview_promotion_task_import_file(args.task_file, registry=registry)
            except CabinetGraphError as exc:
                print(f"bureau: {exc}", file=sys.stderr)
                return 2
            emit(value, args.json)
            return 0
        if args.command == "cabinet-import-reviewed":
            from .cabinet_graph import CabinetGraphError
            from .cabinet_promotion_write import import_reviewed_promotion_task_file

            try:
                value = import_reviewed_promotion_task_file(
                    args.task_file,
                    registry=registry,
                    reviewer=args.reviewer,
                    apply=args.apply,
                )
            except CabinetGraphError as exc:
                print(f"bureau: {exc}", file=sys.stderr)
                return 2
            emit(value, args.json)
            return 0
        if args.command in {"source-check", "source-sync", "source-promote-plan"}:
            from .weltgewebe_source import source_check, source_promote_plan, source_sync

            if args.command == "source-check":
                value = source_check(args.repo, args.ref)
            elif args.command == "source-sync":
                value = source_sync(root, args.repo, args.ref, apply=args.apply)
                if args.apply:
                    Registry.load(root)
            else:
                value = source_promote_plan(root, registry, args.source, args.task_id)
            emit(value, args.json)
            return 0
        if args.command == "check":
            value = {
                "valid": True,
                **registry.summary(),
                "state": read_only_state_integrity(args),
                "adapters": adapters(args).status(),
            }
            emit(value, args.json)
            return 0
        store = StateStore(state_path, state_root)
        adapter_registry = adapters(args)
        dispatcher = Dispatcher(registry, store, adapter_registry)
        if args.command == "status":
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
            try:
                value = dispatcher.claim_next(
                    args.worker, tuple(sorted(set(args.capability))), args.kind
                )
            except NoEligibleTask as exc:
                value = {
                    "status": "no-eligible-task",
                    "detail": str(exc),
                    "explain_next": dispatcher.explain_next(set(args.capability)),
                }
                emit(value, args.json)
                return 1
        elif args.command == "checkout-next":
            base = Path(args.base_dir).expanduser() if args.base_dir else None
            try:
                value = dispatcher.checkout_next(
                    args.worker,
                    tuple(sorted(set(args.capability))),
                    args.kind,
                    base,
                    args.dispatch,
                )
            except NoEligibleTask as exc:
                value = {
                    "status": "no-eligible-task",
                    "detail": str(exc),
                    "explain_next": dispatcher.explain_next(set(args.capability)),
                }
                emit(value, args.json)
                return 1
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
