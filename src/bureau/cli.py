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
    StateError,
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
from .lease_contract import bureau_lease_contract, diagnose_bureau_resource_keys
from .live_register import (
    apply_live_promote_plan,
    live_register_export,
    live_register_list,
    live_register_record,
    live_retention_report,
    write_live_promote_plan,
)
from .rlens_policy import evaluate_registry_rlens_policy
from .runtime_identity import bureau_runtime_identity, require_mutation_compatible

_CLI_RUNTIME_IDENTITY: dict[str, Any] | None = None
_CLI_JSON_ENVELOPE = False


def _json_value_with_identity(value: Any) -> Any:
    if _CLI_RUNTIME_IDENTITY is None:
        return value
    if _CLI_JSON_ENVELOPE:
        return {
            "schema_version": 1,
            "runtime_identity": _CLI_RUNTIME_IDENTITY,
            "result": value,
        }
    if isinstance(value, dict) and "runtime_identity" not in value:
        return {**value, "runtime_identity": _CLI_RUNTIME_IDENTITY}
    return value


def emit(value: Any, as_json: bool) -> None:
    if as_json:
        print(
            json.dumps(
                _json_value_with_identity(value),
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
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
    result.add_argument("--json-envelope", action="store_true")
    result.add_argument("--grabowski-source")
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    sub.add_parser("runtime-identity")
    sub.add_parser("status")
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--repair", action="store_true")
    sub.add_parser("runtime-drift-check")
    lease_contract = sub.add_parser("lease-contract")
    lease_contract.add_argument("--operation", dest="operation")
    lease_contract.add_argument("--subject")
    lease_contract.add_argument(
        "--phase",
        choices=["work", "worktree-admin", "merge", "emergency-recovery"],
        default="work",
    )
    lease_contract.add_argument("--resource-key", action="append", default=[])
    lease_contract.add_argument("--ttl-seconds", type=int)
    lease_contract.add_argument("--justification")
    lease_contract.add_argument("--expected-head")
    lease_contract.add_argument("--expected-state")
    queue_reconcile = sub.add_parser("queue-reconcile")
    queue_reconcile.add_argument("--resource")
    queue_reconcile.add_argument("--write-plan")
    queue_reconcile.add_argument("--apply-plan")
    worktree_hygiene = sub.add_parser("worktree-hygiene")
    worktree_hygiene.add_argument("--max-count", type=int, default=25)
    worktree_hygiene.add_argument("--candidate", action="append", default=[])
    worktree_hygiene.add_argument("--write-plan")
    worktree_hygiene.add_argument("--apply-plan")
    registry_truth = sub.add_parser("registry-truth")
    registry_truth.add_argument("--strict", action="store_true")
    registry_truth.add_argument("--no-baseline-probe", action="store_true")
    sub.add_parser("conflicts")
    rlens_policy = sub.add_parser("rlens-policy")
    rlens_policy.add_argument("--strict", action="store_true")
    rlens_policy.add_argument("--task-id")
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

    frontier = sub.add_parser("frontier")
    frontier.add_argument("--capability", action="append", default=[])
    frontier.add_argument("--resource")
    explain = sub.add_parser("explain-next")
    explain.add_argument("--capability", action="append", default=[])
    explain.add_argument("--resource")
    what_now = sub.add_parser("what-now")
    what_now.add_argument("--capability", action="append", default=[])
    what_now.add_argument("--resource")
    what_now.add_argument("--limit", type=int, default=5)
    repo_balls = sub.add_parser("repo-balls")
    repo_balls.add_argument("--capability", action="append", default=[])
    live_register = sub.add_parser("live-register")
    live_register.add_argument(
        "--kind",
        required=True,
        choices=["thread_focus", "candidate_task", "focus_override"],
    )
    live_register.add_argument("--title", required=True)
    live_register.add_argument("--source", default="operator")
    live_register.add_argument("--thread-id")
    live_register.add_argument("--worker-id")
    live_register.add_argument("--repo")
    live_register.add_argument("--task-id")
    live_register.add_argument("--candidate-id")
    live_register.add_argument("--supersedes-event-id", type=int)
    live_register.add_argument(
        "--status",
        choices=["active", "paused", "closed", "observed", "promoted", "dropped"],
    )
    live_register.add_argument(
        "--promotion-required",
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    live_register.add_argument("--note")
    live_register.add_argument(
        "--catalog-validation",
        choices=["strict", "deferred"],
        default="strict",
    )
    live_list = sub.add_parser("live-list")
    live_list.add_argument("--kind", choices=["thread_focus", "candidate_task", "focus_override"])
    live_list.add_argument("--repo")
    live_list.add_argument("--thread-id")
    live_list.add_argument("--limit", type=int, default=50)
    live_conflicts = sub.add_parser("live-conflicts")
    live_conflicts.add_argument("--capability", action="append", default=[])
    live_conflicts.add_argument("--repo")
    live_conflicts.add_argument("--limit", type=int, default=100)
    live_promote = sub.add_parser("live-promote-plan")
    live_promote.add_argument("--event-id", type=int)
    live_promote.add_argument("--initiative")
    live_promote.add_argument("--task-id")
    live_promote.add_argument("--write-plan")
    live_promote.add_argument("--apply-plan")
    live_export = sub.add_parser("live-export")
    live_export.add_argument("--format", choices=["chronik"], default="chronik")
    live_export.add_argument("--repo")
    live_export.add_argument("--limit", type=int, default=100)
    live_retention = sub.add_parser("live-retention")
    live_retention.add_argument("--limit", type=int, default=500)
    claim = sub.add_parser("claim-next")
    claim.add_argument("--worker", required=True)
    claim.add_argument("--kind", default="interactive-agent")
    claim.add_argument("--capability", action="append", default=[])
    claim.add_argument("--resource")
    checkout = sub.add_parser("checkout-next")
    checkout.add_argument("--worker", required=True)
    checkout.add_argument("--kind", default="interactive-agent")
    checkout.add_argument("--capability", action="append", default=[])
    checkout.add_argument("--resource")
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
    github_observe = sub.add_parser("github-observe")
    github_observe.add_argument("--repo")
    github_observe.add_argument("--task-id")
    projection = sub.add_parser("status-projection")
    projection.add_argument("--repo")
    projection.add_argument("--github-observations")
    projection.add_argument("--skip-github", action="store_true")
    projection.add_argument("--github-max-age", type=int, default=3600)
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


def _state_path(args: argparse.Namespace) -> Path:
    if args.state_db:
        return Path(args.state_db).expanduser()
    if args.state_root:
        return Path(args.state_root).expanduser() / "bureau.sqlite3"
    configured = os.environ.get("BUREAU_STATE_DIR", "~/.local/state/bureau")
    return Path(configured).expanduser() / "bureau.sqlite3"


_READ_ONLY_COMMANDS = frozenset(
    {
        "check",
        "runtime-identity",
        "runtime-drift-check",
        "lease-contract",
        "registry-truth",
        "rlens-policy",
        "source-check",
        "source-promote-plan",
        "github-observe",
        "status-projection",
    }
)


def _command_mutates(args: argparse.Namespace) -> bool:
    command = args.command
    if command == "worktree-hygiene":
        return bool(args.write_plan or args.apply_plan)
    if command == "source-sync":
        return bool(args.apply)
    # Fail closed for every command not explicitly proven read-only. This also
    # makes newly added commands mutation-gated until they are classified.
    return command not in _READ_ONLY_COMMANDS


def main(argv: list[str] | None = None) -> int:
    global _CLI_JSON_ENVELOPE, _CLI_RUNTIME_IDENTITY
    args = parser().parse_args(argv)
    try:
        root = Path(args.root)

        state_path = Path(args.state_db).expanduser() if args.state_db else None
        state_root = Path(args.state_root).expanduser() if args.state_root else None
        _CLI_RUNTIME_IDENTITY = bureau_runtime_identity(root, state_path=_state_path(args))
        operational_registry = bool(
            _CLI_RUNTIME_IDENTITY.get("registry", {}).get("bureau_project")
        )
        _CLI_JSON_ENVELOPE = (
            args.json_envelope
            or os.environ.get("BUREAU_JSON_ENVELOPE") == "1"
            or operational_registry
        )
        if args.command == "runtime-identity":
            emit({"status": "ok"}, args.json)
            return 0
        if _command_mutates(args):
            blocked = require_mutation_compatible(_CLI_RUNTIME_IDENTITY)
            if blocked is not None:
                emit(blocked, args.json)
                return 2
        if args.command == "lease-contract":
            try:
                if args.resource_key:
                    if args.operation or args.subject:
                        raise ValueError(
                            "--resource-key diagnosis cannot be combined with "
                            "--operation or --subject"
                        )
                    value = diagnose_bureau_resource_keys(
                        args.resource_key,
                        phase=args.phase,
                        ttl_seconds=args.ttl_seconds,
                        justification=args.justification,
                        expected_head=args.expected_head,
                        expected_state=args.expected_state,
                    )
                else:
                    value = bureau_lease_contract(args.operation, subject=args.subject)
            except ValueError as exc:
                raise StateError(str(exc)) from exc
            emit(value, args.json)
            return 0
        if args.command in {"live-list", "live-export", "live-retention"}:
            store = StateStore(state_path, state_root)
            if args.command == "live-list":
                value = live_register_list(
                    store,
                    kind=args.kind,
                    repo=args.repo,
                    thread_id=args.thread_id,
                    limit=args.limit,
                )
            elif args.command == "live-export":
                value = live_register_export(
                    store, repo=args.repo, limit=args.limit, export_format=args.format
                )
            else:
                value = live_retention_report(store, limit=args.limit)
            emit(value, args.json)
            return 0
        if args.command == "live-register" and args.catalog_validation == "deferred":
            store = StateStore(state_path, state_root)
            value = live_register_record(
                None,
                store,
                kind=args.kind,
                title=args.title,
                source=args.source,
                thread_id=args.thread_id,
                worker_id=args.worker_id,
                repo=args.repo,
                task_id=args.task_id,
                candidate_id=args.candidate_id,
                supersedes_event_id=args.supersedes_event_id,
                status=args.status,
                promotion_required=args.promotion_required,
                note=args.note,
                catalog_validation="deferred",
            )
            emit(value, args.json)
            return 0
        if args.command == "runtime-drift-check":
            value = runtime_drift_check(root, state_db=state_path, state_root=state_root)
            emit(value, args.json)
            return 0
        if args.command == "worktree-hygiene":
            from .worktree_hygiene import (
                apply_worktree_cleanup_plan,
                worktree_hygiene_report,
                write_worktree_cleanup_plan,
            )

            if args.write_plan and args.apply_plan:
                raise StateError("use either --write-plan or --apply-plan, not both")
            if args.write_plan:
                if not args.candidate:
                    raise StateError("--write-plan requires at least one --candidate")
                value = write_worktree_cleanup_plan(
                    root, args.candidate, args.write_plan, max_count=args.max_count
                )
            elif args.apply_plan:
                if args.candidate:
                    raise StateError("--candidate cannot be combined with --apply-plan")
                value = apply_worktree_cleanup_plan(root, args.apply_plan)
            else:
                if args.candidate:
                    raise StateError("--candidate requires --write-plan")
                value = worktree_hygiene_report(root, max_count=args.max_count)
            emit(value, args.json)
            return 0
        if args.command == "registry-truth":
            from .registry_truth import registry_truth_diagnostics

            value = registry_truth_diagnostics(
                root, probe_baselines=not args.no_baseline_probe
            )
            emit(value, args.json)
            return 1 if args.strict and not value["healthy"] else 0
        registry = Registry.load(root)

        if args.command == "rlens-policy":
            value = evaluate_registry_rlens_policy(registry.tasks)
            if args.task_id:
                value["tasks"] = [
                    item for item in value["tasks"] if item["task_id"] == args.task_id
                ]
                value["blockers"] = [
                    item for item in value["blockers"] if item["task_id"] == args.task_id
                ]
                value["summary"] = {
                    "tasks": len(value["tasks"]),
                    "blockers": len(value["blockers"]),
                    "policy_missing": sum(
                        1 for item in value["tasks"] if item["status"] == "policy-missing"
                    ),
                }
            emit(value, args.json)
            return 1 if args.strict and value["blockers"] else 0

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
        if args.command == "github-observe":
            from .github_observer import filter_observation_by_task, observe_pull_requests

            value = observe_pull_requests(
                root,
                repository=args.repo,
                registry=registry,
                state_db=state_path,
                state_root=state_root,
            )
            if args.task_id:
                value = filter_observation_by_task(value, args.task_id)
            emit(value, args.json)
            return 0 if value["healthy"] and value.get("binding_healthy", True) else 1
        if args.command == "status-projection":
            from .github_observer import observe_pull_requests
            from .status_projection import status_projection

            if args.skip_github:
                github = None
            elif args.github_observations:
                github = json.loads(
                    Path(args.github_observations).expanduser().read_text(encoding="utf-8")
                )
            else:
                github = observe_pull_requests(
                    root,
                    repository=args.repo,
                    registry=registry,
                    state_db=state_path,
                    state_root=state_root,
                )
            value = status_projection(
                root,
                registry=registry,
                state_db=state_path,
                state_root=state_root,
                github=github,
                github_max_age_seconds=args.github_max_age,
            )
            emit(value, args.json)
            return 0
        store = StateStore(state_path, state_root)
        adapter_registry = adapters(args)
        dispatcher = Dispatcher(registry, store, adapter_registry, enforce_runtime_gate=True)
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
            value = dispatcher.frontier(set(args.capability), resource=args.resource)
        elif args.command == "explain-next":
            value = dispatcher.explain_next(set(args.capability), resource=args.resource)
        elif args.command == "what-now":
            value = dispatcher.what_now(
                set(args.capability), resource=args.resource, limit=args.limit
            )
        elif args.command == "repo-balls":
            value = dispatcher.repo_balls(set(args.capability))
        elif args.command == "queue-reconcile":
            from .queue_reconcile import (
                apply_queue_reconcile_plan,
                queue_reconcile_report,
                write_queue_reconcile_plan,
            )

            if args.write_plan and args.apply_plan:
                raise StateError("use either --write-plan or --apply-plan, not both")
            if args.write_plan:
                value = write_queue_reconcile_plan(
                    registry, store, args.write_plan, resource=args.resource
                )
            elif args.apply_plan:
                value = apply_queue_reconcile_plan(
                    registry, store, args.apply_plan, resource=args.resource
                )
            else:
                value = queue_reconcile_report(registry, store, resource=args.resource)
        elif args.command == "live-register":
            value = live_register_record(
                registry,
                store,
                kind=args.kind,
                title=args.title,
                source=args.source,
                thread_id=args.thread_id,
                worker_id=args.worker_id,
                repo=args.repo,
                task_id=args.task_id,
                candidate_id=args.candidate_id,
                supersedes_event_id=args.supersedes_event_id,
                status=args.status,
                promotion_required=args.promotion_required,
                note=args.note,
                catalog_validation=args.catalog_validation,
            )
        elif args.command == "live-conflicts":
            value = dispatcher.live_conflicts(
                set(args.capability), resource=args.repo, limit=args.limit
            )
        elif args.command == "live-promote-plan":
            if args.write_plan and args.apply_plan:
                raise StateError("use either --write-plan or --apply-plan, not both")
            if args.write_plan:
                if args.event_id is None or not args.initiative:
                    raise StateError("--event-id and --initiative are required with --write-plan")
                value = write_live_promote_plan(
                    registry,
                    store,
                    event_id=args.event_id,
                    initiative=args.initiative,
                    task_id=args.task_id,
                    path=args.write_plan,
                )
            elif args.apply_plan:
                value = apply_live_promote_plan(registry, path=args.apply_plan)
            else:
                raise StateError("live-promote-plan requires --write-plan or --apply-plan")
        elif args.command == "claim-next":
            try:
                value = dispatcher.claim_next(
                    args.worker,
                    tuple(sorted(set(args.capability))),
                    args.kind,
                    resource=args.resource,
                )
                if value.get("status") == "runtime-drift-blocked":
                    emit(value, args.json)
                    return 2
            except NoEligibleTask as exc:
                value = {
                    "status": "no-eligible-task",
                    "detail": str(exc),
                    "explain_next": dispatcher.explain_next(
                        set(args.capability), resource=args.resource
                    ),
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
                    resource=args.resource,
                )
                if value.get("status") == "runtime-drift-blocked":
                    emit(value, args.json)
                    return 2
            except NoEligibleTask as exc:
                value = {
                    "status": "no-eligible-task",
                    "detail": str(exc),
                    "explain_next": dispatcher.explain_next(
                        set(args.capability), resource=args.resource
                    ),
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
