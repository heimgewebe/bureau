from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from . import legacy

RESOURCE_TYPE_BY_SECTION = {
    "failure_domains": "failure-domain",
    "recovery_paths": "recovery-path",
}
RESILIENCE_RESOURCE_TYPES = frozenset(RESOURCE_TYPE_BY_SECTION.values())
REQUIRED_NONCLAIMS = frozenset(
    {
        "failure_domain_health",
        "complete_runtime_conflict_state",
        "git_commit_reachability",
        "changed_paths_match_git_diff",
        "merge_or_dispatch_authority",
    }
)


def changed_paths_sha256(paths: list[str]) -> str:
    return legacy.sha256_json(paths)


def coordination_scope_sha256(scope: dict[str, Any]) -> str:
    payload = deepcopy(scope)
    payload.pop("scope_sha256", None)
    return legacy.sha256_json(payload)


def coordination_scope_for_task(task: legacy.Task) -> dict[str, Any] | None:
    value = task.raw.get("coordination_scope")
    return deepcopy(value) if isinstance(value, dict) else None


def _scope_claim_key(item: dict[str, Any]) -> tuple[str, str, int]:
    return (
        str(item.get("resource", "")),
        str(item.get("mode", "")),
        int(item.get("amount", 1)),
    )


def _task_claim_key(claim: legacy.Claim) -> tuple[str, str, int]:
    return (claim.resource, claim.mode, claim.amount)


def validate_coordination_scope(
    task: legacy.Task,
    resources: dict[str, legacy.Resource],
) -> list[str]:
    errors: list[str] = []
    typed_claims = [
        claim
        for claim in task.claims
        if claim.resource in resources
        and resources[claim.resource].type in RESILIENCE_RESOURCE_TYPES
    ]
    typed_claim_keys = [_task_claim_key(claim) for claim in typed_claims]
    if len(typed_claim_keys) != len(set(typed_claim_keys)):
        errors.append(f"task {task.id} repeats an identical resilience claim")
    scope = task.raw.get("coordination_scope")
    if scope is None:
        if typed_claims:
            errors.append(
                f"task {task.id} uses resilience resource claims without coordination_scope"
            )
        return errors
    if not isinstance(scope, dict):
        return [f"task {task.id} coordination_scope must be an object"]

    if scope.get("base_commit") == scope.get("source_commit"):
        errors.append(f"task {task.id} coordination_scope has identical base and source commits")

    paths = scope.get("changed_paths")
    if not isinstance(paths, list) or not paths:
        errors.append(f"task {task.id} coordination_scope has no changed paths")
    else:
        if paths != sorted(set(paths)):
            errors.append(
                f"task {task.id} coordination_scope changed paths must be sorted and unique"
            )
        for path in paths:
            if (
                not isinstance(path, str)
                or not path
                or path.startswith("/")
                or ".." in Path(path).parts
            ):
                errors.append(
                    f"task {task.id} coordination_scope has invalid changed path {path!r}"
                )
        if scope.get("changed_paths_sha256") != changed_paths_sha256(paths):
            errors.append(f"task {task.id} coordination_scope has stale changed_paths_sha256")

    claimed_digest = scope.get("scope_sha256")
    if claimed_digest != coordination_scope_sha256(scope):
        errors.append(f"task {task.id} coordination_scope has stale or invalid scope_sha256")

    nonclaims = scope.get("does_not_establish")
    if not isinstance(nonclaims, list) or not REQUIRED_NONCLAIMS.issubset(nonclaims):
        errors.append(f"task {task.id} coordination_scope misses required nonclaims")

    scoped_items: list[dict[str, Any]] = []
    seen_resources: set[str] = set()
    for section, expected_type in RESOURCE_TYPE_BY_SECTION.items():
        values = scope.get(section, [])
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            scoped_items.append(item)
            resource_id = str(item.get("resource", ""))
            if resource_id in seen_resources:
                errors.append(
                    f"task {task.id} coordination_scope repeats resilience resource {resource_id}"
                )
            seen_resources.add(resource_id)
            resource = resources.get(resource_id)
            if resource is None:
                errors.append(
                    f"task {task.id} coordination_scope references unknown resource {resource_id}"
                )
                continue
            if resource.type != expected_type:
                errors.append(
                    f"task {task.id} coordination_scope section {section} requires "
                    f"resource type {expected_type}: {resource_id}"
                )
            if resource.capacity is None:
                errors.append(f"task {task.id} resilience resource {resource_id} has no capacity")
            if resource.criticality is None:
                errors.append(
                    f"task {task.id} resilience resource {resource_id} has no criticality"
                )
            mode = item.get("mode")
            amount = item.get("amount", 1)
            if mode == "exclusive" and amount != 1:
                errors.append(
                    f"task {task.id} exclusive resilience claim {resource_id} must use amount 1"
                )

    if not scoped_items:
        errors.append(f"task {task.id} coordination_scope has no resilience resources")

    scoped_claim_keys = {_scope_claim_key(item) for item in scoped_items}
    unique_typed_claim_keys = set(typed_claim_keys)
    if scoped_claim_keys != unique_typed_claim_keys:
        errors.append(f"task {task.id} coordination_scope does not exactly match resilience claims")
    for claim in typed_claims:
        if claim.isolation != "none":
            errors.append(
                f"task {task.id} resilience claim {claim.resource} must use isolation none"
            )
    return errors
