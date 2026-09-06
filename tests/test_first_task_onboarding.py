from __future__ import annotations

import pytest

from bureau.first_task_onboarding import (
    FirstTaskOnboardingError,
    validate_first_task_onboarding,
)


def _validate(**overrides):
    arguments = {
        "target_resource_id": "repo.commonthing",
        "target_resource_type": "git-repository",
        "proposed_task_id": "COMMONTHING-MAP-VISIBLE-CARDINALITY-V1-T001",
        "proposed_claims": [
            {
                "resource": "repo.commonthing",
                "mode": "write",
                "isolation": "worktree",
            }
        ],
        "target_task_exists": False,
        "conflicting_task_ids": [],
    }
    arguments.update(overrides)
    return validate_first_task_onboarding(**arguments)


def test_first_task_onboarding_binds_create_only_repository_authority() -> None:
    authority = _validate()

    assert authority == {
        "schema_version": 1,
        "kind": "bureau_first_task_onboarding_authority",
        "target_resource_id": "repo.commonthing",
        "target_resource_type": "git-repository",
        "proposed_task_id": "COMMONTHING-MAP-VISIBLE-CARDINALITY-V1-T001",
        "lease_task_id": "COMMONTHING-MAP-VISIBLE-CARDINALITY-V1-T001",
        "create_only": True,
        "required_claim": {
            "resource": "repo.commonthing",
            "mode": "write",
            "isolation": "worktree",
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
            "Registry or StateStore freshness beyond this validation call",
            "reusable publication authority after an intervening state change",
        ],
    }


def test_first_task_onboarding_authority_is_not_reusable_freshness_evidence() -> None:
    authority = _validate()

    assert (
        "Registry or StateStore freshness beyond this validation call"
        in authority["does_not_establish"]
    )
    assert (
        "reusable publication authority after an intervening state change"
        in authority["does_not_establish"]
    )


@pytest.mark.parametrize("resource_type", ["path", "service", "repository", ""])
def test_first_task_onboarding_rejects_non_git_repository(resource_type: str) -> None:
    with pytest.raises(FirstTaskOnboardingError) as exc_info:
        _validate(target_resource_type=resource_type)

    assert exc_info.value.code == "first-task-onboarding-resource-invalid"


def test_first_task_onboarding_is_strictly_create_only() -> None:
    with pytest.raises(FirstTaskOnboardingError) as exc_info:
        _validate(target_task_exists=True)

    assert exc_info.value.code == "first-task-onboarding-create-only"


@pytest.mark.parametrize(
    "claim",
    [
        {"resource": "repo.commonthing", "mode": "read", "isolation": "worktree"},
        {"resource": "repo.commonthing", "mode": "write", "isolation": "shared"},
        {"resource": "repo.other", "mode": "write", "isolation": "worktree"},
    ],
)
def test_first_task_onboarding_requires_exact_write_worktree_claim(claim: dict) -> None:
    with pytest.raises(FirstTaskOnboardingError) as exc_info:
        _validate(proposed_claims=[claim])

    assert exc_info.value.code == "first-task-onboarding-claim-invalid"


def test_first_task_onboarding_rejects_duplicate_target_claim() -> None:
    claim = {"resource": "repo.commonthing", "mode": "write", "isolation": "worktree"}
    with pytest.raises(FirstTaskOnboardingError) as exc_info:
        _validate(proposed_claims=[claim, dict(claim)])

    assert exc_info.value.code == "first-task-onboarding-claim-invalid"


def test_first_task_onboarding_rejects_existing_repository_task_claim() -> None:
    with pytest.raises(FirstTaskOnboardingError) as exc_info:
        _validate(conflicting_task_ids=["COMMONTHING-OLDER-V1-T001"])

    assert exc_info.value.code == "first-task-onboarding-repository-not-first"
    assert "COMMONTHING-OLDER-V1-T001" in str(exc_info.value)


def test_first_task_onboarding_fails_closed_on_untrusted_state_shapes() -> None:
    with pytest.raises(FirstTaskOnboardingError) as task_exists_error:
        _validate(target_task_exists=1)
    assert task_exists_error.value.code == "first-task-onboarding-state-invalid"

    with pytest.raises(FirstTaskOnboardingError) as conflicts_error:
        _validate(conflicting_task_ids="COMMONTHING-OLDER-V1-T001")
    assert conflicts_error.value.code == "first-task-onboarding-state-invalid"
