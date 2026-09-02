from __future__ import annotations

from bureau.task_identity import (
    assess_task_reference,
    canonical_task_reference_contract,
    local_task_ordinal,
    task_namespace,
)


def test_exact_canonical_task_id_resolves_even_when_local_ordinal_is_reused() -> None:
    known = [
        "GRABOWSKI-OPERATOR-SURFACE-V1-T191",
        "REPOGROUND-UNUSED-AUTHORITY-CLOSEOUT-V1-T191",
    ]

    result = assess_task_reference("GRABOWSKI-OPERATOR-SURFACE-V1-T191", known)

    assert result["status"] == "resolved"
    assert result["reason"] == "exact_canonical_task_id"
    assert result["canonical_task_id"] == "GRABOWSKI-OPERATOR-SURFACE-V1-T191"


def test_bare_local_ordinal_fails_closed_and_names_all_canonical_candidates() -> None:
    known = [
        "REPOGROUND-UNUSED-AUTHORITY-CLOSEOUT-V1-T191",
        "GRABOWSKI-OPERATOR-SURFACE-V1-T191",
        "OTHER-V1-T007",
    ]

    result = assess_task_reference("T191", known)

    assert result == {
        "status": "rejected",
        "reason": "bare_local_ordinal_not_global_identity",
        "reference": "T191",
        "local_ordinal": "T191",
        "candidate_task_ids": [
            "GRABOWSKI-OPERATOR-SURFACE-V1-T191",
            "REPOGROUND-UNUSED-AUTHORITY-CLOSEOUT-V1-T191",
        ],
        "canonical_reference_required": True,
    }


def test_bare_local_ordinal_is_rejected_even_when_currently_unique() -> None:
    result = assess_task_reference("t191", ["GRABOWSKI-OPERATOR-SURFACE-V1-T191"])

    assert result["status"] == "rejected"
    assert result["local_ordinal"] == "T191"
    assert result["candidate_task_ids"] == ["GRABOWSKI-OPERATOR-SURFACE-V1-T191"]


def test_bare_local_ordinal_is_rejected_even_if_malformed_registry_contains_it() -> None:
    result = assess_task_reference("T191", ["T191"])

    assert result["status"] == "rejected"
    assert result["reason"] == "bare_local_ordinal_not_global_identity"


def test_explicit_namespace_plus_local_ordinal_resolves_exactly_one_task() -> None:
    known = [
        "GRABOWSKI-OPERATOR-SURFACE-V1-T191",
        "REPOGROUND-UNUSED-AUTHORITY-CLOSEOUT-V1-T191",
    ]

    result = assess_task_reference(
        "T191",
        known,
        namespace="REPOGROUND-UNUSED-AUTHORITY-CLOSEOUT-V1",
    )

    assert result["status"] == "resolved"
    assert result["reason"] == "explicit_namespace_local_ordinal"
    assert result["canonical_task_id"] == "REPOGROUND-UNUSED-AUTHORITY-CLOSEOUT-V1-T191"


def test_wrong_explicit_namespace_fails_closed_and_keeps_diagnostic_candidates() -> None:
    known = [
        "GRABOWSKI-OPERATOR-SURFACE-V1-T191",
        "REPOGROUND-UNUSED-AUTHORITY-CLOSEOUT-V1-T191",
    ]

    result = assess_task_reference("T191", known, namespace="NOT-A-REAL-LANE")

    assert result["status"] == "rejected"
    assert result["reason"] == "namespace_local_ordinal_not_found"
    assert result["candidate_task_ids"] == known


def test_only_a_trailing_local_ordinal_is_interpreted_as_the_task_ordinal() -> None:
    assert local_task_ordinal(
        "BUREAU-CONTROL-PLANE-V3-FB-RUNTIME-REFRESH-SUCCESSOR-T191-EROFS-RECOVERY-20260830"
    ) is None
    assert local_task_ordinal("GRABOWSKI-OPERATOR-SURFACE-V1-T191") == "T191"
    assert local_task_ordinal("T191") == "T191"
    assert task_namespace("GRABOWSKI-OPERATOR-SURFACE-V1-T191") == (
        "GRABOWSKI-OPERATOR-SURFACE-V1"
    )
    assert task_namespace("T191") is None


def test_reference_contract_marks_local_ordinal_namespace_local() -> None:
    contract = canonical_task_reference_contract(
        "GRABOWSKI-OPERATOR-SURFACE-V1-T191",
        [
            "GRABOWSKI-OPERATOR-SURFACE-V1-T191",
            "REPOGROUND-UNUSED-AUTHORITY-CLOSEOUT-V1-T191",
        ],
    )

    assert contract["canonical_reference_required"] is True
    assert contract["bare_local_reference_allowed"] is False
    assert contract["namespace"] == "GRABOWSKI-OPERATOR-SURFACE-V1"
    assert contract["local_ordinal_scope"] == "namespace_local"
    assert contract["same_local_ordinal_task_ids"] == [
        "REPOGROUND-UNUSED-AUTHORITY-CLOSEOUT-V1-T191"
    ]
