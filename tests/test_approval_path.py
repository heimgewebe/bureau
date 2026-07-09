from __future__ import annotations

from bureau.approval_path import (
    ApprovalPathError,
    classify_task_effects,
    evaluate_approval_path,
)


def task(**overrides):
    base = {
        "schema_version": 1,
        "id": "BUR-TEST-T001",
        "initiative": "BUR-TEST",
        "title": "Test task",
        "state": "ready",
        "execution": {"mode": "interactive-agent", "policy": "autonomous"},
        "claims": [{"resource": "repo.bureau", "mode": "read"}],
        "acceptance": [{"id": "ok", "assertion": "ok"}],
    }
    base.update(overrides)
    return base


def approval(level="operator", **overrides):
    base = {
        "schema_version": 1,
        "task_id": "BUR-TEST-T001",
        "approved": True,
        "decision": "approve",
        "level": level,
        "reviewer": "operator",
        "scope": "task",
    }
    base.update(overrides)
    return base


def test_read_observation_needs_no_approval() -> None:
    result = evaluate_approval_path(task())

    assert result["status"] == "allowed"
    assert result["required_approval"] == "none"
    assert result["approval_present"] is False
    assert result["operator_relay_compatible"] is True


def test_repository_mutation_fails_closed_without_operator_approval() -> None:
    result = evaluate_approval_path(
        task(claims=[{"resource": "repo.bureau", "mode": "write"}])
    )

    assert result["status"] == "blocked"
    assert result["required_approval"] == "reviewed_plan"
    assert "approval evidence required at level reviewed_plan" in result["blockers"]


def test_non_bureau_repository_write_requires_operator_approval() -> None:
    result = evaluate_approval_path(
        task(claims=[{"resource": "repo.commonworld", "mode": "write"}]),
        approval=approval(level="reviewed_plan"),
    )

    assert result["status"] == "blocked"
    assert result["required_approval"] == "operator"
    assert "approval level reviewed_plan is below required level operator" in result["blockers"]


def test_operator_approval_allows_repository_mutation() -> None:
    result = evaluate_approval_path(
        task(claims=[{"resource": "repo.commonworld", "mode": "write"}]),
        approval=approval(level="operator"),
    )

    assert result["status"] == "allowed"
    assert result["required_approval"] == "operator"


def test_approval_must_be_bound_to_task_id() -> None:
    result = evaluate_approval_path(
        task(claims=[{"resource": "repo.commonworld", "mode": "write"}]),
        approval=approval(level="operator", task_id=None),
    )

    assert result["status"] == "blocked"
    assert "approval task_id is required" in result["blockers"]


def test_approval_task_id_must_match_task() -> None:
    result = evaluate_approval_path(
        task(claims=[{"resource": "repo.commonworld", "mode": "write"}]),
        approval=approval(level="operator", task_id="BUR-OTHER-T001"),
    )

    assert result["status"] == "blocked"
    assert "approval task_id does not match task" in result["blockers"]


def test_agent_dispatch_requires_operator_scope() -> None:
    result = evaluate_approval_path(
        task(execution={"mode": "grabowski-task", "policy": "autonomous"}),
        approval=approval(level="operator", scope=["repository_mutation"]),
    )

    assert result["status"] == "blocked"
    assert "approval scope does not cover requested effect classes" in result["blockers"]



def test_scope_must_cover_all_effect_classes() -> None:
    result = evaluate_approval_path(
        task(
            claims=[{"resource": "repo.commonworld", "mode": "write"}],
            metadata={"source_import": True},
        ),
        approval=approval(level="operator", scope=["repository_mutation"]),
    )

    assert result["status"] == "blocked"
    assert "approval scope does not cover requested effect classes" in result["blockers"]


def test_scope_list_allows_when_all_effect_classes_are_covered() -> None:
    result = evaluate_approval_path(
        task(
            claims=[{"resource": "repo.commonworld", "mode": "write"}],
            metadata={"source_import": True},
        ),
        approval=approval(
            level="operator", scope=["repository_mutation", "source_import"]
        ),
    )

    assert result["status"] == "allowed"
    assert result["required_approval"] == "operator"


def test_source_import_requires_operator_approval() -> None:
    result = evaluate_approval_path(
        task(metadata={"source_import": True}),
        approval=approval(level="reviewed_plan"),
    )

    assert result["status"] == "blocked"
    assert result["required_approval"] == "operator"


def test_external_evidence_task_creation_needs_reviewed_plan() -> None:
    result = evaluate_approval_path(
        task(metadata={"task_creation_from_external_evidence": True}),
        approval=approval(level="reviewed_plan"),
    )

    assert result["status"] == "allowed"
    assert result["required_approval"] == "reviewed_plan"


def test_privileged_mutation_requires_privileged_operator() -> None:
    result = evaluate_approval_path(
        task(metadata={"privileged_mutation": True}),
        approval=approval(level="operator"),
    )

    assert result["status"] == "blocked"
    assert result["required_approval"] == "privileged_operator"


def test_prohibited_policy_cannot_be_approved() -> None:
    result = evaluate_approval_path(
        task(execution={"mode": "manual", "policy": "prohibited"}),
        approval=approval(level="privileged_operator"),
    )

    assert result["status"] == "blocked"
    assert "prohibited effect class cannot be approved" in result["blockers"]


def test_unknown_requested_effect_is_rejected() -> None:
    try:
        classify_task_effects(task(), requested_effects=["mystery"])
    except ApprovalPathError as exc:
        assert "unknown effect class" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("unknown effect should fail closed")
