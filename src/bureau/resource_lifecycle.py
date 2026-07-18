from __future__ import annotations

from copy import deepcopy
from typing import Any

RESOURCE_LIFECYCLE_SCHEMA_VERSION = 1

_COMMON_NONCLAIMS = [
    "current_resource_state",
    "cleanup_authority_without_live_revalidation",
    "permission_to_delete_historical_evidence",
    "permission_to_release_foreign_ownership",
    "safe_retry_from_age_or_process_absence_alone",
]

_RESOURCE_CLASSES: dict[str, dict[str, Any]] = {
    "task-run": {
        "authority": "bureau",
        "operational_owner": "bureau-run-bound-executor",
        "terminal_evidence": {
            "authority_kind": "current-attempt-revision-bound-receipt",
            "accepted_states": ["succeeded", "failed", "cancelled", "orphaned"],
            "forbidden_inferences": ["process_absence", "chat_response_end", "task_age"],
        },
        "retention": {
            "operational_claim": "until-terminal-current-attempt",
            "historical_evidence": "permanent",
        },
        "cleanup": {
            "trigger": "current attempt is terminal and receipt integrity passes",
            "action": "remove from active projection; retain run, envelope and receipt",
            "idempotency": "repeat returns no-change without rewriting terminal evidence",
        },
        "orphan_detection": {
            "observation": "bound executor is authoritatively unobservable after reconciliation",
            "action": "mark orphaned only through Bureau reconciliation and preserve the finding",
        },
        "migration_owners": ["bureau"],
    },
    "coordination-claim": {
        "authority": "bureau",
        "operational_owner": "bureau-run",
        "terminal_evidence": {
            "authority_kind": "bound-run-terminal-receipt",
            "accepted_states": ["released"],
            "forbidden_inferences": ["ttl_expiry", "worker_absence", "queue_change"],
        },
        "retention": {
            "operational_claim": "until-bound-run-terminal",
            "historical_evidence": "receipt-bound-audit",
        },
        "cleanup": {
            "trigger": "bound run is terminal and claim identity is unchanged",
            "action": "release exact claim rows atomically",
            "idempotency": "already-absent is no-change",
        },
        "orphan_detection": {
            "observation": "claim references no live or terminally reconcilable run",
            "action": "emit finding; never release without owner or terminal evidence",
        },
        "migration_owners": ["bureau"],
    },
    "execution-lease": {
        "authority": "grabowski",
        "operational_owner": "grabowski-task-or-workspace",
        "terminal_evidence": {
            "authority_kind": "current-attempt-lifecycle-receipt",
            "accepted_states": ["released"],
            "forbidden_inferences": ["ttl_expiry", "process_absence", "task_title", "caller_hash"],
        },
        "retention": {
            "operational_claim": "ttl-maintained-until-terminal",
            "historical_evidence": "audit-chain-and-receipt",
        },
        "cleanup": {
            "trigger": "terminal receipt predates unchanged exact lease snapshot",
            "action": "release only owner-bound unchanged leases in one transaction",
            "idempotency": "already-absent is no-change",
        },
        "orphan_detection": {
            "observation": "lease survives terminal owner attempt",
            "action": "receipt-bound obsolete-lease reconciliation",
        },
        "migration_owners": ["grabowski", "bureau"],
    },
    "git-worktree": {
        "authority": "git-and-owning-repository",
        "operational_owner": "bureau-or-grabowski-workspace",
        "terminal_evidence": {
            "authority_kind": "workspace-close-receipt-plus-live-git-state",
            "accepted_states": ["removed", "preserved", "quarantined"],
            "forbidden_inferences": [
                "directory_age",
                "branch_merged_name",
                "process_absence_alone",
            ],
        },
        "retention": {
            "operational_claim": "until-workspace-terminal-decision",
            "historical_evidence": "commit-or-recovery-ref-and-close-receipt",
        },
        "cleanup": {
            "trigger": (
                "reviewed plan, clean merged or explicitly abandoned state, "
                "no process and no foreign lease"
            ),
            "action": "remove linked worktree only; preserve branch and recovery reference",
            "idempotency": "missing reviewed candidate is no-change or explicit conflict",
        },
        "orphan_detection": {
            "observation": "registered worktree has no reconcilable workspace owner",
            "action": "inventory as unknown; archive-first review before cleanup",
        },
        "migration_owners": ["bureau", "grabowski"],
    },
    "worker": {
        "authority": "grabowski",
        "operational_owner": "grabowski-browser-gui-or-agent-workspace",
        "terminal_evidence": {
            "authority_kind": "worker-status-or-workspace-close-receipt",
            "accepted_states": ["stopped", "complete", "abandoned-failed-roles"],
            "forbidden_inferences": [
                "tmux_pane_absence",
                "display_absence",
                "browser_pid_absence_alone",
            ],
        },
        "retention": {
            "operational_claim": "until-worker-terminal-readback",
            "historical_evidence": "bounded-worker-receipt",
        },
        "cleanup": {
            "trigger": (
                "worker is terminal and exact display, profile, port and path "
                "ownership is unchanged"
            ),
            "action": "stop process group and release exact worker resources",
            "idempotency": "terminal reread is no-change",
        },
        "orphan_detection": {
            "observation": "worker record and live process disagree",
            "action": "observe and reconcile; do not infer successful work",
        },
        "migration_owners": ["grabowski"],
    },
    "profile": {
        "authority": "profile-owning-runtime",
        "operational_owner": "isolated-browser-or-gui-worker",
        "terminal_evidence": {
            "authority_kind": "owning-worker-terminal-receipt",
            "accepted_states": ["quarantined", "removed", "retained"],
            "forbidden_inferences": ["directory_age", "browser_closed_once"],
        },
        "retention": {
            "operational_claim": "until-worker-terminal",
            "historical_evidence": "metadata-only-no-secret-bytes",
        },
        "cleanup": {
            "trigger": (
                "owner worker terminal, no process reference, exact profile lease releasable"
            ),
            "action": "quarantine before irreversible removal; never export secret-bearing bytes",
            "idempotency": "same quarantine receipt is stable",
        },
        "orphan_detection": {
            "observation": "profile exists without live or terminal owner metadata",
            "action": "sensitive-boundary review; no generic file cleanup",
        },
        "migration_owners": ["grabowski"],
    },
    "cache": {
        "authority": "source-system-not-cache",
        "operational_owner": "cache-producing-component",
        "terminal_evidence": {
            "authority_kind": "source-binding-change-or-declared-ttl",
            "accepted_states": ["expired", "invalidated", "superseded"],
            "forbidden_inferences": ["cache_hit_as_source_truth"],
        },
        "retention": {
            "operational_claim": "bounded-ttl",
            "historical_evidence": "source-ref-and-cache-policy-only",
        },
        "cleanup": {
            "trigger": "ttl, source revision change or explicit invalidation",
            "action": "discard derived bytes; preserve source identity and policy evidence",
            "idempotency": "missing cache entry is no-change",
        },
        "orphan_detection": {
            "observation": "cache has no resolvable source binding or policy",
            "action": "invalidate fail-closed; never promote cached value to truth",
        },
        "migration_owners": ["infra", "repoground", "semantah"],
    },
    "durable-outbox": {
        "authority": "producing-system-until-acknowledged-delivery",
        "operational_owner": "outbox-producer",
        "terminal_evidence": {
            "authority_kind": "origin-and-payload-bound-idempotent-acknowledgement",
            "accepted_states": ["acknowledged", "compacted"],
            "forbidden_inferences": ["downstream_health", "file_age", "retry_count"],
        },
        "retention": {
            "operational_claim": "until-valid-acknowledgement",
            "historical_evidence": "digest-and-delivery-receipt",
        },
        "cleanup": {
            "trigger": "acknowledgement matches origin, payload digest and destination identity",
            "action": "compact acknowledged entries without deleting unacknowledged siblings",
            "idempotency": "duplicate acknowledgement does not duplicate effect",
        },
        "orphan_detection": {
            "observation": "entry lacks resolvable producer identity or payload integrity",
            "action": "quarantine and surface attention; never silently drop",
        },
        "migration_owners": ["grabowski", "chronik"],
    },
    "generated-bundle": {
        "authority": "bound-source-revision",
        "operational_owner": "bundle-producer",
        "terminal_evidence": {
            "authority_kind": "newer-valid-manifest-or-explicit-retention-decision",
            "accepted_states": ["current", "superseded", "quarantined"],
            "forbidden_inferences": ["newer_mtime", "consumer_cache_hit"],
        },
        "retention": {
            "operational_claim": "current-plus-bounded-generations",
            "historical_evidence": "manifest-digest-and-source-revision",
        },
        "cleanup": {
            "trigger": (
                "newer valid generation exists and no active consumer or review "
                "binds the old generation"
            ),
            "action": "remove derived payload; retain manifest and source binding",
            "idempotency": "already-superseded generation is no-change",
        },
        "orphan_detection": {
            "observation": "bundle lacks valid source revision, manifest or producer",
            "action": "mark unusable and quarantine; never serve as current context",
        },
        "migration_owners": ["repoground", "grabowski"],
    },
    "feature-flag": {
        "authority": "owning-product-or-runtime",
        "operational_owner": "flag-owning-component",
        "terminal_evidence": {
            "authority_kind": "declared-expiry-plus-code-and-runtime-readback",
            "accepted_states": ["retired", "promoted", "rolled-back"],
            "forbidden_inferences": ["calendar_expiry_alone", "unused_telemetry_alone"],
        },
        "retention": {
            "operational_claim": "until-reviewed-terminal-decision",
            "historical_evidence": "decision-and-release-receipt",
        },
        "cleanup": {
            "trigger": "terminal decision, observation window and rollback evidence pass",
            "action": "remove dead branch and configuration in one reviewed change",
            "idempotency": "retired flag is absent from code, config and runtime projection",
        },
        "orphan_detection": {
            "observation": "flag has no owner, expiry, decision criterion or current projection",
            "action": "block new dependencies and register explicit review",
        },
        "migration_owners": ["owning-repository"],
    },
    "compatibility-layer": {
        "authority": "owning-interface-contract",
        "operational_owner": "compatibility-provider",
        "terminal_evidence": {
            "authority_kind": "consumer-migration-plus-observation-window-and-rollback-proof",
            "accepted_states": ["retired", "extended-with-review"],
            "forbidden_inferences": ["deprecation_date_alone", "no_recent_logs_alone"],
        },
        "retention": {
            "operational_claim": "until-consumer-migration-proven",
            "historical_evidence": "migration-and-removal-receipts",
        },
        "cleanup": {
            "trigger": (
                "all bounded consumers migrated, rollback path verified and "
                "observation window complete"
            ),
            "action": "remove compatibility path and update contracts",
            "idempotency": "removed path cannot remain advertised by discovery surfaces",
        },
        "orphan_detection": {
            "observation": "layer has no named consumers, owner or retirement decision",
            "action": "classify as legacy debt; do not remove without consumer audit",
        },
        "migration_owners": ["owning-repository", "consumer-repositories"],
    },
    "deployment-staging": {
        "authority": "deployment-transaction-owner",
        "operational_owner": "deploying-runtime-or-grabowski-operation",
        "terminal_evidence": {
            "authority_kind": "deployment-or-rollback-transaction-receipt",
            "accepted_states": ["published", "rolled-back", "abandoned-with-recovery-ref"],
            "forbidden_inferences": ["service_healthy_once", "temporary_directory_age"],
        },
        "retention": {
            "operational_claim": "until-deployment-transaction-terminal",
            "historical_evidence": "manifest-and-effect-receipt",
        },
        "cleanup": {
            "trigger": (
                "deployment transaction terminal, active pointer verified and "
                "rollback material retained"
            ),
            "action": "remove unreferenced staging bytes and release deployment gate",
            "idempotency": "active or rollback-referenced release is never removed",
        },
        "orphan_detection": {
            "observation": "staging state has no terminal transaction or active pointer relation",
            "action": "recovery inspection before any cleanup or new deployment",
        },
        "migration_owners": ["grabowski", "owning-runtime"],
    },
}


def resource_lifecycle_contract(resource_kind: str | None = None) -> dict[str, Any]:
    if resource_kind is not None and resource_kind not in _RESOURCE_CLASSES:
        known = ", ".join(sorted(_RESOURCE_CLASSES))
        raise ValueError(f"unknown resource lifecycle kind {resource_kind}; known: {known}")
    resources = (
        {resource_kind: deepcopy(_RESOURCE_CLASSES[resource_kind])}
        if resource_kind is not None
        else deepcopy(_RESOURCE_CLASSES)
    )
    return {
        "schema_version": RESOURCE_LIFECYCLE_SCHEMA_VERSION,
        "kind": "bureau_resource_lifecycle_contract",
        "coverage": "cross-owner-evidence-preserving",
        "invariant": (
            "operational ownership ends only through authoritative terminal evidence; "
            "immutable evidence survives cleanup"
        ),
        "resource_classes": resources,
        "migration_sequence": [
            "inventory-existing-owner-contracts",
            "add-read-only-conformance-report",
            "fix-stale-active-projections",
            "bind-exact-cleanup-to-terminal-evidence",
            "measure-terminal-projection-lag-and-orphan-growth",
            "enable-bounded-automatic-cleanup-only-after-negative-controls",
        ],
        "does_not_establish": list(_COMMON_NONCLAIMS),
    }
