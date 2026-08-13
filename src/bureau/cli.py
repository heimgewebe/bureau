from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .adapters import AdapterRegistry
from .approval import ApprovalRequired
from .closure_observer import (
    reconcile_state_evidence,
    record_manual_acceptance_authentication,
)
from .core import (
    BureauError,
    Claim,
    Dispatcher,
    NoEligibleTask,
    Registry,
    RunStateConflict,
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
from .effect_scope import (
    COORDINATION_STATE_MUTATION as _COMMAND_EFFECT_COORDINATION_STATE_MUTATION,
)
from .effect_scope import (
    READ_ONLY as _COMMAND_EFFECT_READ_ONLY,
)
from .effect_scope import (
    canonical_coordination_state_binding,
    classify_command_effect_scope,
)
from .effect_scope import (
    coordination_state_block as _coordination_state_block,
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
from .operator_intake import (
    DEFAULT_GRABOWSKI_RESOURCE_DB,
    OperatorIntakeError,
    candidate_assess,
    candidate_record_request,
    publication_preview,
    publish_task_proposal,
    read_json_object_file,
    review_task_proposal,
    task_propose,
)
from .read_only_state import ReadOnlyStateStore
from .resource_lifecycle import resource_lifecycle_contract
from .rlens_policy import evaluate_registry_rlens_policy
from .runtime_identity import bureau_runtime_identity, require_mutation_compatible
from .v2 import coordinated_claim_intent_readback, coordinated_claim_status

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


def _cli_error_payload(
    args: argparse.Namespace, exc: BureauError, *, code: str
) -> dict[str, Any]:
    command = str(getattr(args, "command", "unknown"))
    mutates = _command_mutates(args)
    return {
        "schema_version": 1,
        "kind": "bureau_cli_failure",
        "status": "failed",
        "code": code,
        "command": command,
        "detail": str(exc),
        "error_type": type(exc).__name__,
        "effect_started": mutates,
        "ambiguity": mutates,
        "retryable": False,
        "required_readback": (
            [f"bureau-command:{command}"] if mutates else []
        ),
        "does_not_establish": (
            ["effect_absence", "safe_retry"] if mutates else []
        ),
    }


def _emit_cli_error(
    args: argparse.Namespace, exc: BureauError, *, code: str
) -> None:
    if bool(getattr(args, "json", False) or getattr(args, "json_envelope", False)):
        emit(_cli_error_payload(args, exc, code=code), True)
        return
    print(f"bureau: {exc}", file=sys.stderr)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="bureau")
    result.add_argument("--root")
    result.add_argument("--state-db")
    result.add_argument("--state-root")
    result.add_argument("--json", action="store_true")
    result.add_argument("--json-envelope", action="store_true")
    result.add_argument("--grabowski-source")
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    sub.add_parser("runtime-identity")
    authority_inventory_parser = sub.add_parser("authority-inventory")
    authority_inventory_parser.add_argument("--skip-systemd", action="store_true")
    cycle_run = sub.add_parser("cycle-run")
    cycle_run.add_argument(
        "stage", choices=("discovery", "curator", "operator", "verifier", "closure")
    )
    cycle_run.add_argument("stage_args", nargs=argparse.REMAINDER)
    cycle_deployment = sub.add_parser("cycle-deployment")
    cycle_deployment.add_argument("--manifest", type=Path)
    cycle_deployment.add_argument("--canonical-root", type=Path)
    cycle_deployment.add_argument("--unit-root", type=Path)
    cycle_deployment.add_argument("--shim-root", type=Path)
    source_pr_bridge = sub.add_parser("source-pr-bridge")
    source_pr_bridge.add_argument("bridge_args", nargs=argparse.REMAINDER)
    state_backup = sub.add_parser("state-backup")
    state_backup.add_argument("backup_args", nargs=argparse.REMAINDER)
    sub.add_parser("status")
    doctor = sub.add_parser("doctor")
    doctor_mode = doctor.add_mutually_exclusive_group()
    doctor_mode.add_argument("--repair", action="store_true")
    doctor_mode.add_argument(
        "--inventory",
        choices=["broad-bureau-leases"],
        help="emit one stable machine-readable Doctor inventory",
    )
    migrate_leases = sub.add_parser("migrate-leases")
    migrate_mode = migrate_leases.add_mutually_exclusive_group(required=True)
    migrate_mode.add_argument("--dry-run", action="store_true")
    migrate_mode.add_argument("--apply-plan")
    migrate_leases.add_argument("--write-plan")
    migrate_leases.add_argument("--batch-size", type=int, default=5)
    migrate_leases.add_argument("--after-task-id")
    sub.add_parser("runtime-drift-check")
    resource_lifecycle = sub.add_parser("resource-lifecycle-contract")
    resource_lifecycle.add_argument("--kind", dest="resource_kind")
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
    queue_reconcile.add_argument("--allow-action-class", action="append")
    queue_reconcile.add_argument("--allow-finding-code", action="append")
    worktree_hygiene = sub.add_parser("worktree-hygiene")
    worktree_hygiene.add_argument("--max-count", type=int, default=25)
    worktree_hygiene.add_argument("--candidate", action="append", default=[])
    worktree_hygiene.add_argument("--write-plan")
    worktree_hygiene.add_argument("--apply-plan")
    state_root_artifacts = sub.add_parser("state-root-artifacts")
    state_root_artifacts.add_argument("--entry", action="append", default=[])
    state_root_artifacts.add_argument("--destination-root")
    state_root_artifacts.add_argument("--write-plan")
    state_root_artifacts.add_argument("--apply-plan")
    state_root_artifacts.add_argument("--rollback-receipt")
    registry_truth = sub.add_parser("registry-truth")
    registry_truth.add_argument("--strict", action="store_true")
    registry_truth.add_argument("--no-baseline-probe", action="store_true")
    registration_preflight = sub.add_parser("registry-registration-preflight")
    registration_preflight.add_argument("--repo-slug", required=True, metavar="OWNER/REPO")
    registration_preflight.add_argument("--task-json", required=True)
    registration_preflight.add_argument("--checked-base-sha", required=True)
    registration_preflight.add_argument("--base-ref", default="main")
    registration_preflight.add_argument("--pr-number", type=int)
    registration_preflight.add_argument("--head-sha")
    registration_preflight.add_argument("--receipt-out")
    sub.add_parser("conflicts")
    rlens_policy = sub.add_parser("rlens-policy")
    rlens_policy.add_argument("--strict", action="store_true")
    rlens_policy.add_argument("--task-id")
    sub.add_parser("lifecycle")
    lifecycle_reconcile = sub.add_parser("lifecycle-reconcile")
    lifecycle_reconcile.add_argument("--task-id")
    lifecycle_reconcile_apply = sub.add_parser("lifecycle-reconcile-apply")
    lifecycle_reconcile_apply.add_argument("--task-id")
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
    what_now.add_argument("--compact", action="store_true")
    repo_balls = sub.add_parser("repo-balls")
    repo_balls.add_argument("--capability", action="append", default=[])
    repo_scan = sub.add_parser("repo-scan")
    repo_scan.add_argument("--discovery-registry", type=Path)
    repo_scan.add_argument("--repo")
    repo_scan.add_argument("--dry-run", action="store_true")
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
    candidate_record_parser = sub.add_parser("operator-candidate-record")
    candidate_record_parser.add_argument("--request", required=True)
    candidate_assess_parser = sub.add_parser("operator-candidate-assess")
    candidate_assess_selector = candidate_assess_parser.add_mutually_exclusive_group(required=True)
    candidate_assess_selector.add_argument("--candidate-id")
    candidate_assess_selector.add_argument("--event-id", type=int)
    candidate_assess_selector.add_argument("--idempotency-key")
    candidate_assess_parser.add_argument("--initiative")
    candidate_assess_parser.add_argument("--task-id")
    task_propose_parser = sub.add_parser("operator-task-propose")
    task_propose_selector = task_propose_parser.add_mutually_exclusive_group(required=True)
    task_propose_selector.add_argument("--candidate-id")
    task_propose_selector.add_argument("--event-id", type=int)
    task_propose_parser.add_argument("--task-json", required=True)
    task_propose_parser.add_argument("--publishing-task-id", required=True)
    task_propose_parser.add_argument("--write-plan", required=True)
    task_propose_parser.add_argument("--unresolved-field", action="append", default=[])
    task_propose_parser.add_argument("--placeholder-justification")
    task_review_parser = sub.add_parser("operator-task-review")
    task_review_parser.add_argument("--plan", required=True)
    task_review_parser.add_argument("--reviewer", required=True)
    task_review_parser.add_argument("--proposal-sha256", required=True)
    task_publish_parser = sub.add_parser("operator-task-publish")
    task_publish_parser.add_argument("--plan", required=True)
    task_publish_mode = task_publish_parser.add_mutually_exclusive_group()
    task_publish_mode.add_argument("--preview", action="store_true")
    task_publish_mode.add_argument("--apply", action="store_true")
    task_publish_parser.add_argument("--lease-binding")
    task_publish_parser.add_argument("--resource-db")
    task_publish_parser.add_argument("--workspace-root")
    task_publish_parser.add_argument("--receipt")
    claim_intent = sub.add_parser("claim-intent")
    claim_intent.add_argument("--worker", required=True)
    claim_intent.add_argument("--kind", default="interactive-agent")
    claim_intent.add_argument("--capability", action="append", default=[])
    claim_intent.add_argument("--resource")
    claim_intent.add_argument("--task-id")
    claim_intent.add_argument("--base-dir")
    claim_approval = claim_intent.add_mutually_exclusive_group()
    claim_approval.add_argument("--approve", action="store_true")
    claim_approval.add_argument("--break-glass", action="store_true")
    claim_intent.add_argument(
        "--approval-source",
        default="cli claim-intent explicit approval",
    )
    claim_intent.add_argument("--idempotency-key")
    claim_readback = sub.add_parser("claim-intent-readback")
    claim_readback.add_argument("--idempotency-key", required=True)
    claim_commit = sub.add_parser("claim-commit")
    claim_commit.add_argument("--intent", required=True)
    claim_commit.add_argument("--lease-binding")
    claim_commit.add_argument("--workspace", action="store_true")
    claim_status = sub.add_parser("claim-coordination-status")
    claim_status.add_argument("run_id")
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
    acceptance_authenticate = sub.add_parser("acceptance-authenticate")
    acceptance_authenticate.add_argument("run_id")
    acceptance_authenticate.add_argument("criterion_id")
    acceptance_authenticate.add_argument("--expected-evidence-sha256", required=True)
    acceptance_authenticate.add_argument("--reviewer", required=True)
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
    github_observe.add_argument(
        "--repo-slug",
        metavar="OWNER/REPO",
        help="observe one explicit GitHub repository slug",
    )
    github_observe.add_argument(
        "--repo-resource",
        metavar="RESOURCE_ID",
        help="resolve one Bureau git-repository resource through its github_slug",
    )
    github_observe.add_argument(
        "--repo",
        dest="legacy_repo",
        metavar="OWNER/REPO",
        help="deprecated compatibility alias for --repo-slug",
    )
    github_observe.add_argument("--task-id")
    projection = sub.add_parser("status-projection")
    projection.add_argument("--repo")
    projection.add_argument("--github-observations")
    projection.add_argument("--skip-github", action="store_true")
    projection.add_argument("--github-max-age", type=int, default=3600)
    
    projection_repair = sub.add_parser("projection-repair")
    projection_repair_mode = projection_repair.add_mutually_exclusive_group(required=True)
    projection_repair_mode.add_argument("--assess", action="store_true")
    projection_repair_mode.add_argument("--apply", action="store_true")
    projection_repair.add_argument("--candidate-sha256")
    projection_repair.add_argument("--reviewer")
    projection_repair.add_argument("--authority-reference")
    projection_repair.add_argument("--reason")

    receipt_normalize = sub.add_parser("receipt-normalize")
    receipt_normalize_mode = receipt_normalize.add_mutually_exclusive_group(required=True)
    receipt_normalize_mode.add_argument("--dry-run", action="store_true")
    receipt_normalize_mode.add_argument("--apply", action="store_true")

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


def resolve_registry_root(configured: str | None) -> tuple[Path, str]:
    if configured is not None:
        return Path(configured).expanduser(), "explicit-cli"
    environment = os.environ.get("BUREAU_REGISTRY_ROOT")
    if environment:
        mode = os.environ.get("BUREAU_REGISTRY_ROOT_MODE", "explicit-environment")
        return Path(environment).expanduser(), mode
    return Path.cwd(), "ambient-cwd"


def _state_root_path(args: argparse.Namespace) -> Path:
    if args.state_root:
        return Path(args.state_root).expanduser()
    return _state_path(args).parent


_READ_ONLY_COMMANDS = frozenset(
    {
        "authority-inventory",
        "check",
        "conflicts",
        "cycle-deployment",
        "explain-next",
        "frontier",
        "runtime-identity",
        "runtime-drift-check",
        "lease-contract",
        "lifecycle",
        "lifecycle-reconcile",
        "live-conflicts",
        "live-export",
        "live-list",
        "live-retention",
        "operator-candidate-assess",
        "repo-balls",
        "repo-scan",
        "resource-lifecycle-contract",
        "registry-truth",
        "registry-registration-preflight",
        "rlens-policy",
        "run",
        "runs",
        "source-check",
        "source-promote-plan",
        "status",
        "github-observe",
        "claim-coordination-status",
        "claim-intent-readback",
        "status-projection",
        "verification-stamp",
        "what-now",
        "workspace-status",
        "receipt-normalize",
    }
)


def _command_mutates(args: argparse.Namespace) -> bool:
    command = args.command
    if command == "worktree-hygiene":
        return bool(args.write_plan or args.apply_plan)
    if command == "state-root-artifacts":
        return bool(args.write_plan or args.apply_plan or args.rollback_receipt)
    if command == "source-sync":
        return bool(args.apply)
    if command == "doctor":
        if getattr(args, "inventory", None) is not None:
            return False
        return bool(getattr(args, "repair", True))
    if command == "migrate-leases":
        if not hasattr(args, "apply_plan") or not hasattr(args, "write_plan"):
            return True
        return bool(args.apply_plan or args.write_plan)
    if command == "queue-reconcile":
        return bool(args.write_plan or args.apply_plan)
    if command == "live-promote-plan":
        return bool(args.write_plan or args.apply_plan)
    if command == "operator-task-publish":
        return bool(args.apply)
    if command == "projection-repair":
        return bool(args.apply)
    if command == "receipt-normalize":
        return bool(args.apply)
    # Fail closed for every command not explicitly proven read-only. This also
    # makes newly added commands mutation-gated until they are classified.
    return command not in _READ_ONLY_COMMANDS


def _command_effect_scope(args: argparse.Namespace) -> str:
    return classify_command_effect_scope(args.command, mutates=_command_mutates(args))


def _canonical_coordination_state_binding(
    args: argparse.Namespace,
    registry_root: Path,
    runtime_identity: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    return canonical_coordination_state_binding(
        state_root_value=getattr(args, "state_root", None),
        state_db_value=getattr(args, "state_db", None),
        registry_root=registry_root,
        runtime_identity=runtime_identity,
    )



def _parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    raw = list(sys.argv[1:] if argv is None else argv)
    value_options = {"--root", "--state-db", "--state-root", "--grabowski-source"}
    flag_options = {"--json", "--json-envelope"}
    command_index = 0
    while command_index < len(raw):
        value = raw[command_index]
        if value in value_options:
            command_index += 2
            continue
        if any(value.startswith(f"{option}=") for option in value_options):
            command_index += 1
            continue
        if value in flag_options:
            command_index += 1
            continue
        break
    if command_index < len(raw) and raw[command_index] == "source-pr-bridge":
        args = parser().parse_args(raw[: command_index + 1])
        args.bridge_args = raw[command_index + 1 :]
        return args
    if (
        command_index < len(raw)
        and raw[command_index] == "cycle-run"
        and len(raw) == command_index + 3
        and raw[-1] in {"-h", "--help"}
    ):
        return parser().parse_args([*raw[: command_index + 1], raw[-1]])
    return parser().parse_args(raw)


def main(argv: list[str] | None = None) -> int:
    global _CLI_JSON_ENVELOPE, _CLI_RUNTIME_IDENTITY
    args = _parse_arguments(argv)
    args.json = bool(args.json or args.json_envelope)
    try:
        root, registry_selection = resolve_registry_root(args.root)

        state_path = Path(args.state_db).expanduser() if args.state_db else None
        state_root = Path(args.state_root).expanduser() if args.state_root else None
        _CLI_RUNTIME_IDENTITY = bureau_runtime_identity(root, state_path=_state_path(args))
        _CLI_RUNTIME_IDENTITY["registry_selection"] = registry_selection
        operational_registry = bool(_CLI_RUNTIME_IDENTITY.get("registry", {}).get("bureau_project"))
        _CLI_JSON_ENVELOPE = (
            args.json_envelope
            or os.environ.get("BUREAU_JSON_ENVELOPE") == "1"
            or operational_registry
        )
        if args.command == "runtime-identity":
            emit({"status": "ok"}, args.json)
            return 0
        canonical_registry = _CLI_RUNTIME_IDENTITY.get("manifest", {}).get("canonical_registry", {})
        if (
            registry_selection == "canonical-runtime-default"
            and canonical_registry.get("valid") is not True
        ):
            emit(
                {
                    "schema_version": 1,
                    "status": "canonical-registry-invalid",
                    "reason_codes": canonical_registry.get("reasons", ["not-configured"]),
                    "runtime_identity": _CLI_RUNTIME_IDENTITY,
                    "does_not_establish": ["registry_truth", "safe_retry"],
                },
                args.json,
            )
            return 2
        if args.command == "authority-inventory":
            from .authority_inventory import authority_inventory

            value = authority_inventory(
                root,
                state_path=_state_path(args),
                probe_systemd=not args.skip_systemd,
            )
            emit(value, args.json)
            return 0 if value["complete"] else 2
        if args.command == "source-pr-bridge":
            module_value = _CLI_RUNTIME_IDENTITY.get("module")
            manifest_value = _CLI_RUNTIME_IDENTITY.get("manifest")
            registry_value = _CLI_RUNTIME_IDENTITY.get("registry")
            module_identity = module_value if isinstance(module_value, dict) else {}
            manifest_identity = manifest_value if isinstance(manifest_value, dict) else {}
            registry_identity = registry_value if isinstance(registry_value, dict) else {}
            canonical_value = manifest_identity.get("canonical_registry")
            canonical_registry = canonical_value if isinstance(canonical_value, dict) else {}
            if (
                module_identity.get("source_kind") != "immutable-release"
                or manifest_identity.get("valid") is not True
                or canonical_registry.get("valid") is not True
                or _CLI_RUNTIME_IDENTITY.get("registry_selection") != "canonical-runtime-default"
                or registry_identity.get("root") != canonical_registry.get("root")
                or str(root) != canonical_registry.get("root")
            ):
                emit(
                    {
                        "schema_version": 1,
                        "status": "immutable-runtime-required",
                        "reason_codes": ["source-pr-bridge-outside-manifest-release"],
                        "does_not_establish": ["bridge_execution", "safe_retry"],
                    },
                    args.json,
                )
                return 2
            from .source_pr_bridge import main as run_source_pr_bridge

            return run_source_pr_bridge(args.bridge_args)
        if args.command == "cycle-run":
            module_value = _CLI_RUNTIME_IDENTITY.get("module")
            manifest_value = _CLI_RUNTIME_IDENTITY.get("manifest")
            registry_value = _CLI_RUNTIME_IDENTITY.get("registry")
            module_identity = module_value if isinstance(module_value, dict) else {}
            manifest_identity = manifest_value if isinstance(manifest_value, dict) else {}
            registry_identity = registry_value if isinstance(registry_value, dict) else {}
            canonical_value = manifest_identity.get("canonical_registry")
            canonical_registry = canonical_value if isinstance(canonical_value, dict) else {}
            if (
                module_identity.get("source_kind") != "immutable-release"
                or manifest_identity.get("valid") is not True
                or canonical_registry.get("valid") is not True
                or _CLI_RUNTIME_IDENTITY.get("registry_selection") != "canonical-runtime-default"
                or registry_identity.get("root") != canonical_registry.get("root")
                or str(root) != canonical_registry.get("root")
            ):
                emit(
                    {
                        "schema_version": 1,
                        "status": "immutable-runtime-required",
                        "reason_codes": ["cycle-stage-outside-manifest-release"],
                        "does_not_establish": ["scheduler_execution", "safe_retry"],
                    },
                    args.json,
                )
                return 2
            from .cycle_stage import run_stage

            return run_stage(args.stage, args.stage_args)
        if args.command == "cycle-deployment":
            from .cycle_deployment import (
                CycleDeploymentError,
                audit_cycle_deployment,
            )

            manifest_path = (
                args.manifest
                or Path(
                    os.environ.get(
                        "BUREAU_RUNTIME_MANIFEST",
                        "~/.local/share/bureau/deployment-manifest.json",
                    )
                ).expanduser()
            )
            try:
                value = audit_cycle_deployment(
                    manifest_path=manifest_path,
                    canonical_root=args.canonical_root,
                    unit_root=args.unit_root or Path("~/.config/systemd/user").expanduser(),
                    shim_root=args.shim_root or Path("~/.local/libexec").expanduser(),
                )
            except CycleDeploymentError as exc:
                value = {
                    "schema_version": 1,
                    "kind": "bureau_cycle_deployment_audit",
                    "status": "invalid",
                    "activatable": False,
                    "read_only": True,
                    "self_heal": False,
                    "findings": [exc.finding()],
                }
                emit(value, args.json)
                return 2
            emit(value, args.json)
            return 0 if value["status"] == "ok" else 1
        if args.command == "state-backup":
            module_value = _CLI_RUNTIME_IDENTITY.get("module")
            manifest_value = _CLI_RUNTIME_IDENTITY.get("manifest")
            registry_value = _CLI_RUNTIME_IDENTITY.get("registry")
            module_identity = module_value if isinstance(module_value, dict) else {}
            manifest_identity = manifest_value if isinstance(manifest_value, dict) else {}
            registry_identity = registry_value if isinstance(registry_value, dict) else {}
            canonical_value = manifest_identity.get("canonical_registry")
            canonical_registry = canonical_value if isinstance(canonical_value, dict) else {}
            if (
                module_identity.get("source_kind") != "immutable-release"
                or manifest_identity.get("valid") is not True
                or canonical_registry.get("valid") is not True
                or _CLI_RUNTIME_IDENTITY.get("registry_selection") != "canonical-runtime-default"
                or registry_identity.get("root") != canonical_registry.get("root")
                or str(root) != canonical_registry.get("root")
            ):
                emit(
                    {
                        "schema_version": 1,
                        "status": "immutable-runtime-required",
                        "reason_codes": ["state-backup-outside-manifest-release"],
                        "does_not_establish": ["backup_execution", "safe_retry"],
                    },
                    args.json,
                )
                return 2
            from .state_backup import execute as execute_state_backup

            value = execute_state_backup(args.backup_args, adapter_registry=adapters(args))
            emit(value, args.json)
            return 0
        if args.command in {"live-register", "operator-candidate-record"}:
            append_registry = (
                Registry.load(root)
                if (args.command == "live-register" and args.catalog_validation == "strict")
                else None
            )
            request: dict[str, Any] | None = None
            if args.command == "operator-candidate-record":
                request = read_json_object_file(args.request, field="request")
                if request.get("catalog_validation", "strict") == "strict":
                    append_registry = Registry.load(root)
            store = StateStore(state_path, state_root)
            if args.command == "live-register":
                value = live_register_record(
                    append_registry,
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
            else:
                assert request is not None
                value = candidate_record_request(append_registry, store, request)
            emit(value, args.json)
            return 0
        effect_scope = _command_effect_scope(args)
        _CLI_RUNTIME_IDENTITY["command_effect_scope"] = effect_scope
        canonical_snapshot_selected = (
            registry_selection == "canonical-runtime-default"
            or _CLI_RUNTIME_IDENTITY.get("registry", {}).get("role") == "canonical-runtime-snapshot"
        )
        coordination_binding: dict[str, Any] | None = None
        if (
            effect_scope == _COMMAND_EFFECT_COORDINATION_STATE_MUTATION
            and canonical_snapshot_selected
        ):
            blocked, coordination_binding = _canonical_coordination_state_binding(
                args, root, _CLI_RUNTIME_IDENTITY
            )
            if blocked is not None:
                emit(blocked, args.json)
                return 2
            assert coordination_binding is not None
            state_root = Path(coordination_binding["state_root"])
            state_path = Path(coordination_binding["state_db"])
            _CLI_RUNTIME_IDENTITY["coordination_state_binding"] = coordination_binding
        elif effect_scope != _COMMAND_EFFECT_READ_ONLY:
            if registry_selection == "canonical-runtime-default":
                emit(
                    {
                        "schema_version": 1,
                        "status": "explicit-registry-root-required",
                        "reason_codes": ["canonical-registry-read-only"],
                        "required_action": "rerun with --root bound to a clean task worktree",
                        "runtime_identity": _CLI_RUNTIME_IDENTITY,
                        "does_not_establish": [
                            "mutation_authority",
                            "task_worktree_identity",
                        ],
                    },
                    args.json,
                )
                return 2
            blocked = require_mutation_compatible(_CLI_RUNTIME_IDENTITY)
            if blocked is not None:
                emit(blocked, args.json)
                return 2
        if args.command == "projection-repair":
            store = StateStore(state_path, state_root)
            if args.assess:
                value = store.projection_repair_candidate()
            else:
                missing = [
                    name
                    for name, value in (
                        ("candidate-sha256", args.candidate_sha256),
                        ("reviewer", args.reviewer),
                        ("authority-reference", args.authority_reference),
                        ("reason", args.reason),
                    )
                    if not value
                ]
                if missing:
                    raise StateError(
                        "projection repair apply requires " + ", ".join(missing)
                    )
                value = store.apply_projection_repair(
                    expected_candidate_sha256=args.candidate_sha256,
                    reviewer=args.reviewer,
                    reference=args.authority_reference,
                    reason=args.reason,
                )
            if coordination_binding is not None:
                value["coordination_state_binding"] = coordination_binding
            emit(value, args.json)
            return 0
        if args.command == "receipt-normalize":
            from .receipt_normalization import receipt_normalize
            value = receipt_normalize(
                Registry.load(root),
                StateStore(state_path, state_root),
                dry_run=args.dry_run,
                runtime_identity=_CLI_RUNTIME_IDENTITY,
            )
            emit(value, args.json)
            return 0
        if args.command == "resource-lifecycle-contract":
            try:
                value = resource_lifecycle_contract(args.resource_kind)
            except ValueError as exc:
                raise StateError(str(exc)) from exc
            emit(value, args.json)
            return 0
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
            store_class = (
                ReadOnlyStateStore
                if registry_selection == "canonical-runtime-default"
                else StateStore
            )
            store = store_class(state_path, state_root)
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
        if args.command == "runtime-drift-check":
            value = runtime_drift_check(
                root,
                state_db=state_path,
                state_root=state_root,
                runtime_identity=_CLI_RUNTIME_IDENTITY,
            )
            emit(value, args.json)
            return 0
        if args.command == "state-root-artifacts":
            from .state_root_artifacts import (
                apply_state_root_migration_plan,
                managed_state_root_inventory,
                rollback_state_root_migration,
                write_state_root_migration_plan,
            )
            from .v2 import state_root_hygiene

            effects = sum(
                bool(value)
                for value in (
                    args.write_plan,
                    args.apply_plan,
                    args.rollback_receipt,
                )
            )
            if effects > 1:
                raise StateError("use only one of --write-plan, --apply-plan or --rollback-receipt")
            artifact_root = _state_root_path(args)
            if args.write_plan:
                if not args.entry or not args.destination_root:
                    raise StateError("--write-plan requires --entry and --destination-root")
                value = write_state_root_migration_plan(
                    artifact_root,
                    args.entry,
                    Path(args.destination_root),
                    Path(args.write_plan),
                    reference_root=root,
                )
            elif args.apply_plan:
                if args.entry or args.destination_root:
                    raise StateError("--entry and --destination-root cannot accompany --apply-plan")
                value = apply_state_root_migration_plan(Path(args.apply_plan))
                value["post_hygiene"] = state_root_hygiene(artifact_root, _state_path(args))
            elif args.rollback_receipt:
                if args.entry or args.destination_root:
                    raise StateError(
                        "--entry and --destination-root cannot accompany --rollback-receipt"
                    )
                value = rollback_state_root_migration(Path(args.rollback_receipt))
                value["post_hygiene"] = state_root_hygiene(artifact_root, _state_path(args))
            else:
                if args.entry or args.destination_root:
                    raise StateError("--entry and --destination-root require --write-plan")
                value = managed_state_root_inventory(artifact_root)
                value["state_root_hygiene"] = state_root_hygiene(artifact_root, _state_path(args))
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

            value = registry_truth_diagnostics(root, probe_baselines=not args.no_baseline_probe)
            emit(value, args.json)
            return 1 if args.strict and not value["healthy"] else 0
        if args.command == "registry-registration-preflight":
            from .registry_registration_preflight import (
                repository_registration_preflight,
                write_receipt,
            )

            value = repository_registration_preflight(
                root,
                repository=args.repo_slug,
                task_json_path=args.task_json,
                checked_base_sha=args.checked_base_sha,
                base_ref=args.base_ref,
                pr_number=args.pr_number,
                head_sha=args.head_sha,
            )
            if args.receipt_out:
                write_receipt(args.receipt_out, value)
            emit(value, args.json)
            return 0 if value["decision"] == "allow" else 2
        registry = Registry.load(root)

        if args.command == "doctor" and args.inventory == "broad-bureau-leases":
            from .lease_migration import broad_bureau_lease_inventory

            value = broad_bureau_lease_inventory(registry)
            emit(value, args.json)
            return 0
        if args.command == "migrate-leases":
            from .lease_migration import (
                apply_lease_migration_plan,
                lease_migration_plan,
                write_lease_migration_plan,
            )

            if args.apply_plan:
                if args.write_plan or args.after_task_id or args.batch_size != 5:
                    raise StateError("--apply-plan cannot be combined with planning options")
                value = apply_lease_migration_plan(registry, args.apply_plan)
            elif args.write_plan:
                value = write_lease_migration_plan(
                    registry,
                    args.write_plan,
                    batch_size=args.batch_size,
                    after_task_id=args.after_task_id,
                )
            else:
                value = lease_migration_plan(
                    registry,
                    batch_size=args.batch_size,
                    after_task_id=args.after_task_id,
                )
            emit(value, args.json)
            return 0

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
            from .lease_contract import registry_bureau_lease_findings

            broad_scope_findings = registry_bureau_lease_findings(registry)
            value = {
                "valid": not broad_scope_findings,
                **registry.summary(),
                "state": read_only_state_integrity(args),
                "adapters": adapters(args).status(),
                "broad_bureau_scope_findings": broad_scope_findings,
            }
            emit(value, args.json)
            return 1 if broad_scope_findings else 0
        if args.command == "github-observe":
            from .github_observer import filter_observation_by_task, observe_pull_requests
            from .github_repository import (
                RepositoryIdentifierError,
                resolve_github_repository,
            )

            try:
                selection = resolve_github_repository(
                    registry,
                    repo_slug=args.repo_slug,
                    repo_resource=args.repo_resource,
                    legacy_repo=args.legacy_repo,
                )
            except RepositoryIdentifierError as exc:
                emit(exc.payload(), args.json)
                return 2
            value = observe_pull_requests(
                root,
                repository=selection.repository,
                registry=registry,
                state_db=state_path,
                state_root=state_root,
            )
            value["repository_input"] = selection.metadata()
            value["notes"] = list(dict.fromkeys([*value.get("notes", []), *selection.notes()]))
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
        if coordination_binding is not None:
            rechecked_identity = bureau_runtime_identity(root, state_path=state_path)
            rechecked_identity["registry_selection"] = registry_selection
            rechecked_identity["command_effect_scope"] = effect_scope
            blocked, rechecked_binding = _canonical_coordination_state_binding(
                args, root, rechecked_identity
            )
            if blocked is not None:
                emit(blocked, args.json)
                return 2
            if rechecked_binding != coordination_binding:
                emit(
                    _coordination_state_block(
                        status="coordination-state-binding-changed",
                        reason_codes=["coordination-state-binding-changed-before-open"],
                        required_action="inspect the state path and retry from a stable root",
                        runtime_identity=rechecked_identity,
                        state_root=state_root,
                        state_db=state_path,
                    ),
                    args.json,
                )
                return 2
            assert rechecked_binding is not None
            rechecked_identity["coordination_state_binding"] = rechecked_binding
            _CLI_RUNTIME_IDENTITY = rechecked_identity
        store = (
            ReadOnlyStateStore(state_path, state_root)
            if effect_scope == _COMMAND_EFFECT_READ_ONLY
            and registry_selection == "canonical-runtime-default"
            else StateStore(state_path, state_root)
        )
        adapter_registry = adapters(args)
        dispatcher = Dispatcher(
            registry,
            store,
            adapter_registry,
            enforce_runtime_gate=True,
            runtime_identity=_CLI_RUNTIME_IDENTITY,
        )
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
        elif args.command in {"lifecycle-reconcile", "lifecycle-reconcile-apply"}:
            from .v2 import reconcile_initiative_lifecycle

            apply_lifecycle = args.command == "lifecycle-reconcile-apply"
            value = reconcile_initiative_lifecycle(
                registry, store, apply=apply_lifecycle, task_id=args.task_id
            )
        elif args.command == "close-ready":
            value = close_ready_initiatives(registry, store)
        elif args.command == "frontier":
            value = dispatcher.frontier(set(args.capability), resource=args.resource)
        elif args.command == "explain-next":
            value = dispatcher.explain_next(set(args.capability), resource=args.resource)
        elif args.command == "what-now":
            value = dispatcher.what_now(
                set(args.capability),
                resource=args.resource,
                limit=args.limit,
                compact=args.compact,
            )
        elif args.command == "repo-balls":
            value = dispatcher.repo_balls(set(args.capability))
        elif args.command == "repo-scan":
            from .repo_scan import DEFAULT_DISCOVERY_REGISTRY, scan_repository_registry

            value = scan_repository_registry(
                registry,
                discovery_registry_path=(args.discovery_registry or DEFAULT_DISCOVERY_REGISTRY),
                resource_id=args.repo,
            )
        elif args.command == "queue-reconcile":
            from .queue_reconcile import (
                QUEUE_RECONCILE_ACTION_FILTER_SCHEMA_VERSION,
                apply_queue_reconcile_plan,
                queue_reconcile_report,
                write_queue_reconcile_plan,
            )

            if args.write_plan and args.apply_plan:
                raise StateError("use either --write-plan or --apply-plan, not both")
            action_filter = None
            if args.allow_action_class or args.allow_finding_code:
                action_filter = {
                    "schema_version": QUEUE_RECONCILE_ACTION_FILTER_SCHEMA_VERSION,
                    "selection": "allow",
                    "allowed_action_classes": args.allow_action_class or [],
                    "allowed_finding_codes": args.allow_finding_code or [],
                }
            if args.apply_plan and action_filter is not None:
                raise StateError(
                    "action filter options cannot accompany --apply-plan; "
                    "apply uses the reviewed plan filter"
                )
            if args.write_plan:
                value = write_queue_reconcile_plan(
                    registry,
                    store,
                    args.write_plan,
                    resource=args.resource,
                    action_filter=action_filter,
                )
            elif args.apply_plan:
                value = apply_queue_reconcile_plan(
                    registry, store, args.apply_plan, resource=args.resource
                )
            else:
                value = queue_reconcile_report(
                    registry,
                    store,
                    resource=args.resource,
                    action_filter=action_filter,
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
        elif args.command == "operator-candidate-assess":
            value = candidate_assess(
                registry,
                store,
                candidate_id=args.candidate_id,
                event_id=args.event_id,
                idempotency_key=args.idempotency_key,
                initiative=args.initiative,
                task_id=args.task_id,
            )
        elif args.command == "operator-task-propose":
            task_json = read_json_object_file(args.task_json, field="task")
            value = task_propose(
                registry,
                store,
                task_json=task_json,
                publishing_task_id=args.publishing_task_id,
                path=args.write_plan,
                candidate_id=args.candidate_id,
                event_id=args.event_id,
                unresolved_fields=args.unresolved_field,
                placeholder_justification=args.placeholder_justification,
            )
        elif args.command == "operator-task-review":
            value = review_task_proposal(
                plan_path=args.plan,
                reviewer=args.reviewer,
                expected_proposal_sha256=args.proposal_sha256,
            )
        elif args.command == "operator-task-publish":
            if args.apply:
                if not args.lease_binding or not args.workspace_root or not args.receipt:
                    raise StateError(
                        "--apply requires --lease-binding, --workspace-root and --receipt"
                    )
                lease_binding = read_json_object_file(args.lease_binding, field="lease-binding")
                value = publish_task_proposal(
                    registry,
                    store,
                    plan_path=args.plan,
                    lease_binding=lease_binding,
                    workspace_root=args.workspace_root,
                    receipt_path=args.receipt,
                    resource_db=args.resource_db or DEFAULT_GRABOWSKI_RESOURCE_DB,
                )
            else:
                if args.lease_binding or args.resource_db or args.workspace_root or args.receipt:
                    raise StateError("publication effect arguments require --apply")
                value = publication_preview(registry, store, plan_path=args.plan)
        elif args.command == "claim-intent":
            value = dispatcher.claim_intent(
                args.worker,
                tuple(sorted(set(args.capability))),
                args.kind,
                task_id=args.task_id,
                resource=args.resource,
                base_dir=Path(args.base_dir).expanduser() if args.base_dir else None,
                approved=args.approve,
                break_glass=args.break_glass,
                approval_source=args.approval_source,
                idempotency_key=args.idempotency_key,
            )
        elif args.command == "claim-intent-readback":
            value = coordinated_claim_intent_readback(
                store, args.idempotency_key
            )
        elif args.command == "claim-commit":
            intent = read_json_object_file(args.intent, field="intent")
            lease_binding = (
                read_json_object_file(args.lease_binding, field="lease_binding")
                if args.lease_binding
                else None
            )
            if args.workspace:
                value = dispatcher.checkout_claim_intent(
                    intent,
                    lease_binding,
                    resource_db=DEFAULT_GRABOWSKI_RESOURCE_DB,
                )
            else:
                value = dispatcher.commit_claim_intent(
                    intent,
                    lease_binding,
                    resource_db=DEFAULT_GRABOWSKI_RESOURCE_DB,
                )
        elif args.command == "claim-coordination-status":
            value = coordinated_claim_status(
                store,
                args.run_id,
                resource_db=DEFAULT_GRABOWSKI_RESOURCE_DB,
            )
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
            acceptance_closeout = reconcile_state_evidence(registry, store)
            runtime_reconcile = dispatcher.reconcile(args.stale_after)
            value = {**runtime_reconcile, "acceptance_closeout": acceptance_closeout}
        elif args.command == "acceptance-authenticate":
            value = record_manual_acceptance_authentication(
                store,
                args.run_id,
                args.criterion_id,
                expected_evidence_sha256=args.expected_evidence_sha256,
                reviewer=args.reviewer,
            )
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
    except OperatorIntakeError as exc:
        emit(exc.payload(), args.json)
        return 2
    except RunStateConflict as exc:
        emit(exc.payload(), args.json)
        return 2
    except ApprovalRequired as exc:
        if args.json:
            emit(exc.payload(), True)
        else:
            print(f"bureau: {exc}", file=sys.stderr)
        return 2
    except StateError as exc:
        if getattr(args, "command", "").startswith("operator-"):
            emit(
                OperatorIntakeError(
                    "operator-intake-invalid",
                    str(exc),
                ).payload(),
                bool(args.json or args.json_envelope),
            )
            return 2
        _emit_cli_error(args, exc, code="state-error")
        return 2
    except BureauError as exc:
        _emit_cli_error(args, exc, code="bureau-error")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
