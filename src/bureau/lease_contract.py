from __future__ import annotations

from copy import deepcopy
from typing import Any

LEASE_CONTRACT_SCHEMA_VERSION = 1

_REPO_LEASE_SCOPE = {
    "protects": [
        "bureau_source_code_mutation",
        "schema_and_migration_mutation",
        "reviewed_git_registry_mutation",
        "branch_push_merge_and_deployment",
    ],
    "must_not_block": [
        "operational_state_read",
        "operational_state_append",
        "status_projection_read",
    ],
}

_COMMANDS: dict[str, dict[str, Any]] = {
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


def bureau_lease_contract(command: str | None = None) -> dict[str, Any]:
    """Return the bounded, machine-readable Bureau lease boundary."""
    if command is not None and command not in _COMMANDS:
        covered = ", ".join(sorted(_COMMANDS))
        raise ValueError(f"lease contract has no entry for {command}; covered commands: {covered}")
    commands = {command: _COMMANDS[command]} if command is not None else _COMMANDS
    return {
        "schema_version": LEASE_CONTRACT_SCHEMA_VERSION,
        "kind": "bureau_lease_contract",
        "coverage": "bounded_fail_closed",
        "repo_lease_scope": deepcopy(_REPO_LEASE_SCOPE),
        "commands": deepcopy(commands),
        "invariant": (
            "A Bureau repository lease protects Git-backed Bureau development and reviewed "
            "registry mutation; it must not reserve or disable the operational state store."
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
