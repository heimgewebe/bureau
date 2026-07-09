from __future__ import annotations

import pytest

from bureau import approval
from bureau.core import StateError


def test_unknown_effect_class_fails_closed() -> None:
    decision = approval.approval_decision("mystery_effect", None)
    assert decision["allowed"] is False
    assert decision["reason"].startswith("unknown action class fails closed")
    with pytest.raises(StateError, match="approval required"):
        approval.require_approval("mystery_effect", None)


def test_unsafe_repository_mutation_requires_explicit_operator_approval() -> None:
    blocked = approval.approval_decision("repository_mutation", None)
    assert blocked["allowed"] is False
    assert blocked["required_level"] == "operator"
    assert "explicit approval missing" in blocked["reason"]

    allowed = approval.require_approval(
        "repository_mutation",
        approval.explicit_operator_approval(source="cli --approve", approved=True),
    )
    assert allowed["allowed"] is True
    assert allowed["evidence"]["source"] == "cli --approve"


def test_expected_reference_binds_approval_to_source() -> None:
    with pytest.raises(StateError, match="approval reference"):
        approval.require_approval(
            "repository_mutation",
            approval.explicit_operator_approval(
                source="cli --approve", approved=True, reference="other-run"
            ),
            expected_reference="run-1",
        )

    allowed = approval.require_approval(
        "repository_mutation",
        approval.explicit_operator_approval(
            source="cli --approve", approved=True, reference="run-1"
        ),
        expected_reference="run-1",
    )
    assert allowed["allowed"] is True


def test_task_id_binding_blocks_unbound_or_wrong_task_approval() -> None:
    with pytest.raises(StateError, match="approval task_id"):
        approval.require_approval(
            "repository_mutation",
            approval.explicit_operator_approval(source="cli --approve", approved=True),
            task_id="BUR-TEST-T001",
        )

    with pytest.raises(StateError, match="approval task_id"):
        approval.require_approval(
            "repository_mutation",
            approval.explicit_operator_approval(
                source="cli --approve", approved=True, task_id="BUR-OTHER-T001"
            ),
            task_id="BUR-TEST-T001",
        )

    allowed = approval.require_approval(
        "repository_mutation",
        approval.explicit_operator_approval(
            source="cli --approve", approved=True, task_id="BUR-TEST-T001"
        ),
        task_id="BUR-TEST-T001",
    )
    assert allowed["allowed"] is True
    assert allowed["evidence"]["task_id"] == "BUR-TEST-T001"


def test_multi_effect_approval_scope_must_cover_all_action_classes() -> None:
    partial = approval.approval_decision_for_effects(
        ["repository_mutation", "source_import"],
        approval.ApprovalEvidence(
            source="break-glass procedure",
            level="break_glass",
            approved=True,
            scope=("repository_mutation",),
        ),
    )
    assert partial["allowed"] is False
    assert "scope does not cover all action classes" in partial["reason"]

    allowed = approval.require_approval_for_effects(
        ["repository_mutation", "source_import"],
        approval.ApprovalEvidence(
            source="break-glass procedure",
            level="break_glass",
            approved=True,
            scope=("repository_mutation", "source_import"),
        ),
    )
    assert allowed["allowed"] is True
    assert allowed["evidence"]["scope"] == ["repository_mutation", "source_import"]


def test_mixed_read_only_and_effectful_actions_ignore_read_only_for_gate() -> None:
    decision = approval.require_approval_for_effects(
        ["read_only_observation", "repository_mutation"],
        approval.explicit_operator_approval(
            source="cli --approve",
            approved=True,
            scope="repository_mutation",
        ),
    )

    assert decision["allowed"] is True
    assert decision["action_classes"] == [
        "read_only_observation",
        "repository_mutation",
    ]
    assert decision["required_level"] == "operator"


def test_multi_effect_runtime_reports_break_glass_required_level() -> None:
    decision = approval.approval_decision_for_effects(
        ["repository_mutation", "runtime_mutation"],
        approval.explicit_operator_approval(
            source="cli --approve",
            approved=True,
            scope=["repository_mutation", "runtime_mutation"],
        ),
    )

    assert decision["allowed"] is False
    assert decision["required_level"] == "break_glass"
    assert "not accepted for required break_glass, operator" in decision["reason"]


def test_reviewed_plan_does_not_satisfy_source_import() -> None:
    with pytest.raises(StateError, match="not accepted for required reviewed_receipt"):
        approval.require_approval(
            "source_import",
            approval.reviewed_plan_approval(reviewer="reviewer", reference="plan.json"),
        )


def test_reviewed_receipt_does_not_satisfy_queue_mutation() -> None:
    with pytest.raises(StateError, match="not accepted for required reviewed_plan"):
        approval.require_approval(
            "queue_mutation",
            approval.reviewed_receipt_approval(
                reviewer="reviewer", reference="receipt.json"
            ),
        )


def test_break_glass_satisfies_explicitly_allowed_lower_gates() -> None:
    evidence = approval.ApprovalEvidence(
        source="break-glass procedure", level="break_glass", approved=True
    )
    assert approval.require_approval("source_import", evidence)["allowed"] is True
    assert approval.require_approval("queue_mutation", evidence)["allowed"] is True


def test_runtime_mutation_rejects_lower_approval_level() -> None:
    with pytest.raises(StateError, match="not accepted for required break_glass"):
        approval.require_approval(
            "runtime_mutation",
            approval.explicit_operator_approval(source="cli --approve", approved=True),
        )


def test_read_only_action_does_not_need_approval() -> None:
    decision = approval.approval_decision("dry_run", None)
    assert decision["allowed"] is True
    assert decision["required"] is False


def test_task_approval_contract_infers_write_claim_as_repository_mutation() -> None:
    task = {
        "id": "BUR-TEST-001-T001",
        "execution": {"mode": "interactive-agent", "policy": "autonomous"},
        "claims": [{"resource": "repo.alpha", "mode": "write"}],
    }
    contract = approval.task_approval_contract(task)
    assert contract["action_class"] == "repository_mutation"
    assert contract["decision"]["allowed"] is False
