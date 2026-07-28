from __future__ import annotations

from bureau.approval import (
    approval_decision,
    explicit_operator_approval,
    reviewed_plan_approval,
)
from bureau.lease_contract import assess_task_broad_bureau_scope


def test_broad_scope_gate_and_effect_approval_share_one_canonical_contract() -> None:
    task_id = "BUREAU-TEST-V1-T003"
    proposal_sha256 = "a" * 64
    task = {
        "id": task_id,
        "state": "planned",
        "execution": {
            "approval": {
                "action_class": "repository_mutation",
                "required_level": "operator",
            },
            "grabowski_resources": ["repo:/home/alex/repos/bureau"],
            "broad_bureau_scope_exception": {
                "justification": "One atomic schema migration covers all tracked Bureau files.",
                "effect_boundaries": ["all tracked Bureau files"],
                "approval": {
                    "action_class": "registry_mutation",
                    "required_level": "reviewed_plan",
                },
            },
        },
        "claims": [],
    }

    assessment = assess_task_broad_bureau_scope(task)

    assert assessment["allowed"] is True
    action_class = assessment["exception_approval"]["action_class"]
    assert action_class == "registry_mutation"
    reviewed = approval_decision(
        action_class,
        reviewed_plan_approval(
            reviewer="self-review",
            reference=proposal_sha256,
            task_id=task_id,
            scope=action_class,
        ),
        expected_reference=proposal_sha256,
        task_id=task_id,
    )
    operator_only = approval_decision(
        action_class,
        explicit_operator_approval(
            source="visible-command",
            approved=True,
            reviewer="operator",
            reference=proposal_sha256,
            task_id=task_id,
            scope=action_class,
        ),
        expected_reference=proposal_sha256,
        task_id=task_id,
    )

    assert reviewed["allowed"] is True
    assert reviewed["required_level"] == "reviewed_plan"
    assert operator_only["allowed"] is False
