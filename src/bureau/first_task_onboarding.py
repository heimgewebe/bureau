from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

FIRST_TASK_ONBOARDING_SCHEMA_VERSION = 1
FIRST_TASK_ONBOARDING_KIND = "bureau_first_task_onboarding_authority"
_REQUIRED_REPOSITORY_CLAIM = {
    "mode": "write",
    "isolation": "worktree",
}


class FirstTaskOnboardingError(ValueError):
    """Fail-closed error while deriving first-TaskSpec onboarding authority."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _require_nonempty_string(value: Any, *, code: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FirstTaskOnboardingError(code, f"{label} must be a non-empty string")
    return value


def validate_first_task_onboarding(
    *,
    target_resource_id: str,
    target_resource_type: str,
    proposed_task_id: str,
    proposed_claims: Sequence[Mapping[str, Any]],
    target_task_exists: bool,
    conflicting_task_ids: Sequence[str],
) -> dict[str, Any]:
    """Derive the narrow authority needed to create one repository's first TaskSpec.

    This validator deliberately does not acquire a lease, approve a proposal, or
    mutate StateStore.  It only proves the part that normal task publication
    cannot prove for the first TaskSpec: the target is a registered repository,
    the proposed task does not already exist, no authoritative TaskSpec already
    claims the repository, and the proposal itself asks for the ordinary
    write/worktree repository claim.

    The caller must separately bind this result to the exact candidate,
    Registry snapshot, proposal digest, reviewed-plan approval, and live
    StateStore-path lease before any publication effect is allowed.
    """

    resource_id = _require_nonempty_string(
        target_resource_id,
        code="first-task-onboarding-resource-invalid",
        label="target_resource_id",
    )
    resource_type = _require_nonempty_string(
        target_resource_type,
        code="first-task-onboarding-resource-invalid",
        label="target_resource_type",
    )
    if resource_type != "git-repository":
        raise FirstTaskOnboardingError(
            "first-task-onboarding-resource-invalid",
            "first-task onboarding is limited to Registry git-repository resources",
        )

    task_id = _require_nonempty_string(
        proposed_task_id,
        code="first-task-onboarding-task-invalid",
        label="proposed_task_id",
    )
    if not isinstance(target_task_exists, bool):
        raise FirstTaskOnboardingError(
            "first-task-onboarding-state-invalid",
            "target_task_exists must be an authoritative boolean observation",
        )
    if target_task_exists:
        raise FirstTaskOnboardingError(
            "first-task-onboarding-create-only",
            f"TaskSpec {task_id} already exists; onboarding authority is create-only",
        )

    if not isinstance(proposed_claims, Sequence) or isinstance(
        proposed_claims, (str, bytes, bytearray)
    ):
        raise FirstTaskOnboardingError(
            "first-task-onboarding-claim-invalid",
            "proposed_claims must be a sequence of claim objects",
        )

    matching_claims: list[dict[str, Any]] = []
    for claim in proposed_claims:
        if not isinstance(claim, Mapping):
            raise FirstTaskOnboardingError(
                "first-task-onboarding-claim-invalid",
                "every proposed claim must be an object",
            )
        if claim.get("resource") != resource_id:
            continue
        matching_claims.append(dict(claim))

    if len(matching_claims) != 1:
        raise FirstTaskOnboardingError(
            "first-task-onboarding-claim-invalid",
            "the proposed TaskSpec must contain exactly one claim for the target repository",
        )

    target_claim = matching_claims[0]
    claim_matches = all(
        target_claim.get(key) == expected
        for key, expected in _REQUIRED_REPOSITORY_CLAIM.items()
    )
    if not claim_matches:
        raise FirstTaskOnboardingError(
            "first-task-onboarding-claim-invalid",
            "the target repository claim must use mode=write and isolation=worktree",
        )

    if not isinstance(conflicting_task_ids, Sequence) or isinstance(
        conflicting_task_ids, (str, bytes, bytearray)
    ):
        raise FirstTaskOnboardingError(
            "first-task-onboarding-state-invalid",
            "conflicting_task_ids must be an authoritative sequence",
        )
    conflicts: list[str] = []
    for conflict in conflicting_task_ids:
        conflicts.append(
            _require_nonempty_string(
                conflict,
                code="first-task-onboarding-state-invalid",
                label="conflicting task id",
            )
        )
    conflicts = sorted(set(conflicts))
    if conflicts:
        raise FirstTaskOnboardingError(
            "first-task-onboarding-repository-not-first",
            "repository already overlaps authoritative TaskSpec claims: " + ", ".join(conflicts),
        )

    return {
        "schema_version": FIRST_TASK_ONBOARDING_SCHEMA_VERSION,
        "kind": FIRST_TASK_ONBOARDING_KIND,
        "target_resource_id": resource_id,
        "target_resource_type": resource_type,
        "proposed_task_id": task_id,
        "lease_task_id": task_id,
        "create_only": True,
        "required_claim": {
            "resource": resource_id,
            **_REQUIRED_REPOSITORY_CLAIM,
        },
        "conflicting_task_ids": [],
        "does_not_establish": [
            "reviewed-plan approval",
            "candidate publication approval",
            "Registry mutation authority",
            "queue mutation authority",
            "TaskSpec revision authority",
            "authority for a second task in the repository",
            "StateStore mutation without an exact live lease",
        ],
    }
