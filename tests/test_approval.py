from __future__ import annotations

import pytest

from bureau import approval
from bureau.core import StateError


def test_unknown_effect_class_fails_closed() -> None:
    decision = approval.approval_decision("mystery_effect", None)
    assert decision["allowed"] is False
    assert decision["reason"] == "unknown action class fails closed"
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
