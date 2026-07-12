from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path
from typing import Any

LEASE_CONTRACT_SCHEMA_VERSION = 2
BUREAU_REPOSITORY_ROOT = Path("/home/alex/repos/bureau")
BROAD_BUREAU_REPOSITORY_KEY = f"repo:{BUREAU_REPOSITORY_ROOT}"
BUREAU_MERGE_GATE_KEY = f"path:{BUREAU_REPOSITORY_ROOT}/.bureau-scopes/merge-main"
BUREAU_WORKTREE_ADMIN_KEY = (
    f"path:{BUREAU_REPOSITORY_ROOT}/.bureau-scopes/worktree-admin"
)
MAX_EMERGENCY_REPO_LEASE_TTL_SECONDS = 300

_SUBJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,239}$")
_GIT_HEAD_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_TERMINAL_TASK_STATES = {"verified", "cancelled", "superseded"}

_REPO_LEASE_SCOPE = {
    "deprecated_key": BROAD_BUREAU_REPOSITORY_KEY,
    "normal_work": "forbidden",
    "merge_serialization": BUREAU_MERGE_GATE_KEY,
    "worktree_administration": BUREAU_WORKTREE_ADMIN_KEY,
    "emergency_recovery": {
        "allowed": True,
        "maximum_ttl_seconds": MAX_EMERGENCY_REPO_LEASE_TTL_SECONDS,
        "requirements": [
            "explicit_recovery_justification",
            "bounded_expected_head_or_state",
            "immediate_release_after_effect",
        ],
    },
    "must_not_block": [
        "operational_state_read",
        "operational_state_append",
        "status_projection_read",
        "independent_registry_task_work",
        "independent_registry_initiative_work",
    ],
}

_READ_COMMANDS: dict[str, dict[str, Any]] = {
    "lease-contract": {
        "availability_class": "checkout_independent_read",
        "git_repository_lease_required": False,
        "registry_catalog_required": False,
        "state_store_required": False,
        "effect": "none",
        "conflict_scope": "none",
    },
    "live-register": {
        "availability_class": "always_on_operational_append",
        "git_repository_lease_required": False,
        "registry_catalog_required": "strict_mode_only",
        "state_store_required": True,
        "effect": "append_only_state_store_event",
        "conflict_scope": "sqlite_immediate_transaction",
        "fallback": {
            "mode": "deferred_catalog_validation",
            "cli": "--catalog-validation deferred",
            "nonclaims": ["repo_exists", "task_exists", "registry_binding_valid"],
        },
    },
    "live-list": {
        "availability_class": "checkout_independent_operational_read",
        "git_repository_lease_required": False,
        "registry_catalog_required": False,
        "state_store_required": True,
        "effect": "state_store_read",
        "conflict_scope": "none",
    },
    "live-export": {
        "availability_class": "checkout_independent_operational_read",
        "git_repository_lease_required": False,
        "registry_catalog_required": False,
        "state_store_required": True,
        "effect": "state_store_read",
        "conflict_scope": "none",
    },
    "live-retention": {
        "availability_class": "checkout_independent_operational_read",
        "git_repository_lease_required": False,
        "registry_catalog_required": False,
        "state_store_required": True,
        "effect": "state_store_read",
        "conflict_scope": "none",
    },
    "status-projection": {
        "availability_class": "registry_backed_operational_read",
        "git_repository_lease_required": False,
        "registry_catalog_required": True,
        "state_store_required": True,
        "effect": "derived_read_only_projection",
        "conflict_scope": "none",
    },
    "what-now": {
        "availability_class": "registry_backed_operational_read",
        "git_repository_lease_required": False,
        "registry_catalog_required": True,
        "state_store_required": True,
        "effect": "derived_read_only_projection",
        "conflict_scope": "none",
    },
    "repo-balls": {
        "availability_class": "registry_backed_operational_read",
        "git_repository_lease_required": False,
        "registry_catalog_required": True,
        "state_store_required": True,
        "effect": "derived_read_only_projection",
        "conflict_scope": "none",
    },
    "live-conflicts": {
        "availability_class": "registry_backed_operational_read",
        "git_repository_lease_required": False,
        "registry_catalog_required": True,
        "state_store_required": True,
        "effect": "derived_read_only_projection",
        "conflict_scope": "none",
    },
}

_MUTATION_OPERATIONS: dict[str, dict[str, Any]] = {
    "registry-task-write": {
        "subject_kind": "task_id",
        "path_template": "registry/tasks/{subject}.json",
        "component_resource": "component.bureau.registry",
        "effect": "reviewed_git_registry_mutation",
        "conflict_scope": "single_task_file_compare_and_swap",
    },
    "registry-initiative-write": {
        "subject_kind": "initiative_id",
        "path_template": "registry/initiatives/{subject}.json",
        "component_resource": "component.bureau.registry",
        "effect": "reviewed_git_registry_mutation",
        "conflict_scope": "single_initiative_file_compare_and_swap",
    },
    "registry-resource-write": {
        "subject_kind": "resource_id",
        "path_template": "registry/resources/{subject}.json",
        "component_resource": "component.bureau.registry",
        "effect": "reviewed_git_registry_mutation",
        "conflict_scope": "single_resource_file_compare_and_swap",
    },
    "registry-queue-write": {
        "path": "registry/queue.json",
        "component_resource": "component.bureau.queue",
        "effect": "reviewed_queue_mutation",
        "conflict_scope": "queue_file_compare_and_swap",
    },
    "bureau-core-write": {
        "path": ".bureau-scopes/core-code",
        "component_resource": "component.bureau.core",
        "effect": "bureau_source_code_mutation",
        "conflict_scope": "core_component",
    },
    "bureau-schema-write": {
        "path": ".bureau-scopes/schema",
        "component_resource": "component.bureau.schema",
        "effect": "schema_and_migration_mutation",
        "conflict_scope": "schema_component",
    },
    "worktree-admin": {
        "path": ".bureau-scopes/worktree-admin",
        "component_resource": "component.bureau.worktree-admin",
        "effect": "short_linked_worktree_and_ref_administration",
        "conflict_scope": "worktree_admin_gate",
        "maximum_ttl_seconds": MAX_EMERGENCY_REPO_LEASE_TTL_SECONDS,
    },
    "merge-main": {
        "path": ".bureau-scopes/merge-main",
        "component_resource": "component.bureau.merge-gate",
        "effect": "short_main_merge_serialization",
        "conflict_scope": "merge_gate",
        "maximum_ttl_seconds": MAX_EMERGENCY_REPO_LEASE_TTL_SECONDS,
    },
    "runtime-deploy": {
        "resource_key": "service:bureau-status-capsule",
        "component_resource": "component.bureau.runtime",
        "effect": "runtime_deployment",
        "conflict_scope": "runtime_service",
    },
}


def _validated_subject(subject: str | None, *, kind: str) -> str:
    if subject is None:
        raise ValueError(f"{kind} is required for this lease operation")
    normalized = subject.strip()
    if not _SUBJECT_RE.fullmatch(normalized):
        raise ValueError(f"invalid {kind} for Bureau lease scope")
    return normalized


def _mutation_contract(operation: str, subject: str | None) -> dict[str, Any]:
    template = deepcopy(_MUTATION_OPERATIONS[operation])
    subject_kind = template.pop("subject_kind", None)
    if subject_kind is not None:
        checked_subject = _validated_subject(subject, kind=subject_kind)
        relative_path = str(template.pop("path_template")).format(subject=checked_subject)
        template["subject"] = checked_subject
        template["subject_kind"] = subject_kind
    else:
        if subject is not None:
            raise ValueError(f"{operation} does not accept a subject")
        relative_path = template.pop("path", None)
    resource_key = template.pop("resource_key", None)
    if resource_key is None:
        assert relative_path is not None
        resource_key = f"path:{BUREAU_REPOSITORY_ROOT / relative_path}"
    return {
        "availability_class": "parallel_reviewed_work",
        "git_repository_lease_required": False,
        "required_resource_keys": [resource_key],
        "forbidden_resource_keys": [BROAD_BUREAU_REPOSITORY_KEY],
        "merge_gate_resource_key": BUREAU_MERGE_GATE_KEY,
        "worktree_admin_resource_key": BUREAU_WORKTREE_ADMIN_KEY,
        "branch_rule": "unique_branch_without_global_repo_lease",
        **template,
    }


def bureau_lease_contract(
    command: str | None = None,
    *,
    subject: str | None = None,
) -> dict[str, Any]:
    """Return the bounded, machine-readable Bureau lease boundary."""
    covered = {*_READ_COMMANDS, *_MUTATION_OPERATIONS}
    if command is not None and command not in covered:
        names = ", ".join(sorted(covered))
        raise ValueError(f"lease contract has no entry for {command}; covered commands: {names}")
    if command is None and subject is not None:
        raise ValueError("--subject requires --operation")
    if command is None:
        commands = deepcopy(_READ_COMMANDS)
        mutation_operations = {
            name: {
                **deepcopy(spec),
                "git_repository_lease_required": False,
                "forbidden_resource_keys": [BROAD_BUREAU_REPOSITORY_KEY],
                "merge_gate_resource_key": BUREAU_MERGE_GATE_KEY,
            }
            for name, spec in _MUTATION_OPERATIONS.items()
        }
    elif command in _READ_COMMANDS:
        if subject is not None:
            raise ValueError(f"{command} does not accept a subject")
        commands = {command: deepcopy(_READ_COMMANDS[command])}
        mutation_operations = {}
    else:
        commands = {command: _mutation_contract(command, subject)}
        mutation_operations = {}
    return {
        "schema_version": LEASE_CONTRACT_SCHEMA_VERSION,
        "kind": "bureau_lease_contract",
        "coverage": "bounded_fail_closed",
        "repo_lease_scope": deepcopy(_REPO_LEASE_SCOPE),
        "commands": commands,
        "mutation_operation_templates": mutation_operations,
        "invariant": (
            "Bureau intake and independent Registry work remain available. Normal work uses "
            "object, file, component and short effect-gate leases; the global Bureau repository "
            "lease is reserved only for bounded emergency recovery."
        ),
        "does_not_establish": [
            "permission_for_unlisted_commands",
            "registry_task_truth_from_live_register",
            "queue_truth_from_live_register",
            "claim_authority",
            "dispatch_authority",
            "merge_authority",
        ],
    }


def diagnose_bureau_resource_keys(
    resource_keys: list[str] | tuple[str, ...],
    *,
    phase: str = "work",
    ttl_seconds: int | None = None,
    justification: str | None = None,
    expected_head: str | None = None,
    expected_state: str | None = None,
) -> dict[str, Any]:
    """Diagnose an intended Bureau resource set without acquiring any lease."""
    if phase not in {"work", "worktree-admin", "merge", "emergency-recovery"}:
        raise ValueError(
            "phase must be work, worktree-admin, merge or emergency-recovery"
        )
    keys = sorted(set(resource_keys))
    normalized_expected_head = expected_head.strip() if expected_head else None
    normalized_expected_state = expected_state.strip() if expected_state else None
    expected_boundary_present = bool(
        normalized_expected_state
        or (normalized_expected_head and _GIT_HEAD_RE.fullmatch(normalized_expected_head))
    )
    findings: list[dict[str, Any]] = []
    if BROAD_BUREAU_REPOSITORY_KEY in keys:
        if (
            phase == "emergency-recovery"
            and ttl_seconds is not None
            and 0 < ttl_seconds <= MAX_EMERGENCY_REPO_LEASE_TTL_SECONDS
            and justification is not None
            and bool(justification.strip())
            and expected_boundary_present
        ):
            findings.append(
                {
                    "severity": "warning",
                    "code": "bounded-emergency-bureau-repo-lease",
                    "resource_key": BROAD_BUREAU_REPOSITORY_KEY,
                    "message": "global Bureau repo lease accepted only for bounded recovery",
                }
            )
        else:
            findings.append(
                {
                    "severity": "blocker",
                    "code": "broad-bureau-repo-lease-forbidden",
                    "resource_key": BROAD_BUREAU_REPOSITORY_KEY,
                    "message": (
                        "normal Bureau work must use object, path or component scopes; "
                        "bounded emergency recovery requires phase=emergency-recovery and "
                        f"ttl_seconds<={MAX_EMERGENCY_REPO_LEASE_TTL_SECONDS} "
                        "an explicit justification and an expected head or state"
                    ),
                }
            )
    if phase == "worktree-admin":
        if BUREAU_WORKTREE_ADMIN_KEY not in keys:
            findings.append(
                {
                    "severity": "blocker",
                    "code": "bureau-worktree-admin-gate-missing",
                    "resource_key": BUREAU_WORKTREE_ADMIN_KEY,
                    "message": "Bureau linked-worktree administration requires its short gate",
                }
            )
        if (
            ttl_seconds is None
            or ttl_seconds <= 0
            or ttl_seconds > MAX_EMERGENCY_REPO_LEASE_TTL_SECONDS
        ):
            findings.append(
                {
                    "severity": "blocker",
                    "code": "bureau-worktree-admin-ttl-invalid",
                    "resource_key": BUREAU_WORKTREE_ADMIN_KEY,
                    "message": (
                        "Bureau worktree-admin gate requires an explicit TTL between 1 and "
                        f"{MAX_EMERGENCY_REPO_LEASE_TTL_SECONDS} seconds"
                    ),
                }
            )
    if phase == "merge":
        if BUREAU_MERGE_GATE_KEY not in keys:
            findings.append(
                {
                    "severity": "blocker",
                    "code": "bureau-merge-gate-missing",
                    "resource_key": BUREAU_MERGE_GATE_KEY,
                    "message": "Bureau main merge requires the short merge-gate resource",
                }
            )
        if (
            ttl_seconds is None
            or ttl_seconds <= 0
            or ttl_seconds > MAX_EMERGENCY_REPO_LEASE_TTL_SECONDS
        ):
            findings.append(
                {
                    "severity": "blocker",
                    "code": "bureau-merge-gate-ttl-invalid",
                    "resource_key": BUREAU_MERGE_GATE_KEY,
                    "message": (
                        "Bureau merge gate requires an explicit TTL between 1 and "
                        f"{MAX_EMERGENCY_REPO_LEASE_TTL_SECONDS} seconds"
                    ),
                }
            )
    blockers = [item for item in findings if item["severity"] == "blocker"]
    return {
        "schema_version": LEASE_CONTRACT_SCHEMA_VERSION,
        "kind": "bureau_lease_diagnostics",
        "phase": phase,
        "ttl_seconds": ttl_seconds,
        "justification_present": bool(justification and justification.strip()),
        "expected_head": normalized_expected_head,
        "expected_state": normalized_expected_state,
        "expected_boundary_present": expected_boundary_present,
        "resource_keys": keys,
        "healthy": not blockers,
        "findings": findings,
        "required_merge_gate": BUREAU_MERGE_GATE_KEY,
        "required_worktree_admin_gate": BUREAU_WORKTREE_ADMIN_KEY,
        "global_repo_lease": BROAD_BUREAU_REPOSITORY_KEY,
    }


def registry_bureau_lease_findings(registry: Any) -> list[dict[str, Any]]:
    """Report nonterminal tasks that still request the deprecated global Bureau repo lease."""
    lanes = {
        task_id: lane
        for lane, task_ids in registry.queue.items()
        for task_id in task_ids
    }
    findings: list[dict[str, Any]] = []
    for task in registry.tasks.values():
        if task.state in _TERMINAL_TASK_STATES:
            continue
        explicit_keys = set(task.execution.get("grabowski_resources", []))
        claim_resources = []
        for claim in task.claims:
            resource = registry.resources.get(claim.resource)
            if resource is not None and resource.grabowski_key == BROAD_BUREAU_REPOSITORY_KEY:
                claim_resources.append(claim.resource)
        sources = []
        if BROAD_BUREAU_REPOSITORY_KEY in explicit_keys:
            sources.append("execution.grabowski_resources")
        if claim_resources:
            sources.append("claims")
        if not sources:
            continue
        lane = lanes.get(task.id)
        severity = "blocker" if task.state == "ready" or lane == "now" else "warning"
        findings.append(
            {
                "severity": severity,
                "code": "task-uses-broad-bureau-repo-lease",
                "task_id": task.id,
                "task_state": task.state,
                "lane": lane,
                "resource_key": BROAD_BUREAU_REPOSITORY_KEY,
                "sources": sources,
                "claim_resources": sorted(claim_resources),
                "replacement_task_resource_key": (
                    f"path:{BUREAU_REPOSITORY_ROOT}/registry/tasks/{task.id}.json"
                ),
                "message": (
                    "replace the global Bureau repository lease before this task becomes ready"
                ),
            }
        )
    return sorted(findings, key=lambda item: (item["severity"], item["task_id"]))
