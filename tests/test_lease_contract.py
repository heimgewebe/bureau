from __future__ import annotations

import json

from bureau import cli as bureau_cli
from bureau.lease_contract import bureau_lease_contract


def test_live_register_contract_excludes_git_repository_lease() -> None:
    contract = bureau_lease_contract("live-register")

    operation = contract["commands"]["live-register"]
    assert operation["availability_class"] == "always_on_operational_append"
    assert operation["git_repository_lease_required"] is False
    assert operation["conflict_scope"] == "sqlite_immediate_transaction"
    assert operation["fallback"]["mode"] == "deferred_catalog_validation"
    assert "operational_state_append" in contract["repo_lease_scope"]["must_not_block"]


def test_lease_contract_cli_does_not_load_registry(monkeypatch, tmp_path, capsys) -> None:
    def fail_registry_load(_root):
        raise AssertionError("lease-contract must not load the Git registry")

    monkeypatch.setattr(bureau_cli.Registry, "load", fail_registry_load)

    rc = bureau_cli.main(
        [
            "--root",
            str(tmp_path / "missing-checkout"),
            "--json",
            "lease-contract",
            "--operation",
            "live-register",
        ]
    )

    assert rc == 0
    result = json.loads(capsys.readouterr().out)
    assert result["commands"]["live-register"]["git_repository_lease_required"] is False


def test_registry_task_scopes_are_object_specific() -> None:
    first = bureau_lease_contract("registry-task-write", subject="BUR-ONE-T001")
    second = bureau_lease_contract("registry-task-write", subject="BUR-TWO-T001")

    first_operation = first["commands"]["registry-task-write"]
    second_operation = second["commands"]["registry-task-write"]
    assert first_operation["git_repository_lease_required"] is False
    assert first_operation["required_resource_keys"] != second_operation["required_resource_keys"]
    assert first_operation["required_resource_keys"] == [
        "path:/home/alex/repos/bureau/registry/tasks/BUR-ONE-T001.json"
    ]
    assert first_operation["forbidden_resource_keys"] == ["repo:/home/alex/repos/bureau"]


def test_registry_initiative_scope_rejects_path_traversal() -> None:
    import pytest

    with pytest.raises(ValueError, match="invalid initiative_id"):
        bureau_lease_contract("registry-initiative-write", subject="../escape")


def test_broad_bureau_repo_lease_is_blocked_for_normal_work() -> None:
    from bureau.lease_contract import diagnose_bureau_resource_keys

    report = diagnose_bureau_resource_keys(["repo:/home/alex/repos/bureau"])

    assert report["healthy"] is False
    assert [item["code"] for item in report["findings"]] == ["broad-bureau-repo-lease-forbidden"]


def test_bounded_emergency_repo_lease_requires_justification() -> None:
    from bureau.lease_contract import diagnose_bureau_resource_keys

    denied = diagnose_bureau_resource_keys(
        ["repo:/home/alex/repos/bureau"],
        phase="emergency-recovery",
        ttl_seconds=300,
    )
    allowed = diagnose_bureau_resource_keys(
        ["repo:/home/alex/repos/bureau"],
        phase="emergency-recovery",
        ttl_seconds=300,
        justification="recover corrupt shared Git metadata",
        expected_head="a" * 40,
    )

    assert denied["healthy"] is False
    assert allowed["healthy"] is True
    assert allowed["findings"][0]["code"] == "bounded-emergency-bureau-repo-lease"


def test_merge_uses_short_gate_without_global_repo_lease() -> None:
    from bureau.lease_contract import BUREAU_MERGE_GATE_KEY, diagnose_bureau_resource_keys

    missing = diagnose_bureau_resource_keys([], phase="merge", ttl_seconds=120)
    too_long = diagnose_bureau_resource_keys(
        [BUREAU_MERGE_GATE_KEY], phase="merge", ttl_seconds=3600
    )
    valid = diagnose_bureau_resource_keys([BUREAU_MERGE_GATE_KEY], phase="merge", ttl_seconds=120)

    assert missing["healthy"] is False
    assert too_long["healthy"] is False
    assert valid["healthy"] is True


def test_lease_contract_cli_emits_registry_task_scope(tmp_path, capsys) -> None:
    rc = bureau_cli.main(
        [
            "--root",
            str(tmp_path / "missing-checkout"),
            "--json",
            "lease-contract",
            "--operation",
            "registry-task-write",
            "--subject",
            "BUR-TEST-T001",
        ]
    )

    assert rc == 0
    result = json.loads(capsys.readouterr().out)
    operation = result["commands"]["registry-task-write"]
    assert operation["required_resource_keys"] == [
        "path:/home/alex/repos/bureau/registry/tasks/BUR-TEST-T001.json"
    ]


def test_all_mutation_operations_avoid_global_repo_lease() -> None:
    cases = {
        "registry-task-write": "BUR-TEST-T001",
        "registry-initiative-write": "BUR-TEST",
        "registry-resource-write": "component.example",
        "registry-publication": None,
        "registry-queue-write": None,
        "bureau-core-write": None,
        "bureau-schema-write": None,
        "worktree-admin": None,
        "merge-main": None,
        "runtime-deploy": None,
    }

    for operation, subject in cases.items():
        contract = bureau_lease_contract(operation, subject=subject)
        value = contract["commands"][operation]
        assert value["git_repository_lease_required"] is False
        assert value["forbidden_resource_keys"] == ["repo:/home/alex/repos/bureau"]
        assert value["required_resource_keys"]


def test_registry_lease_findings_detect_claim_derived_global_key() -> None:
    from types import SimpleNamespace

    from bureau.lease_contract import registry_bureau_lease_findings

    registry = SimpleNamespace(
        queue={"now": [], "next": [], "later": []},
        resources={"repo.bureau": SimpleNamespace(grabowski_key="repo:/home/alex/repos/bureau")},
        tasks={
            "TASK-1": SimpleNamespace(
                id="TASK-1",
                state="planned",
                execution={},
                claims=[SimpleNamespace(resource="repo.bureau")],
            )
        },
    )

    findings = registry_bureau_lease_findings(registry)

    assert findings[0]["sources"] == ["claims"]
    assert findings[0]["claim_resources"] == ["repo.bureau"]


def test_worktree_admin_uses_short_gate() -> None:
    from bureau.lease_contract import (
        BUREAU_WORKTREE_ADMIN_KEY,
        diagnose_bureau_resource_keys,
    )

    missing = diagnose_bureau_resource_keys([], phase="worktree-admin", ttl_seconds=120)
    too_long = diagnose_bureau_resource_keys(
        [BUREAU_WORKTREE_ADMIN_KEY], phase="worktree-admin", ttl_seconds=3600
    )
    valid = diagnose_bureau_resource_keys(
        [BUREAU_WORKTREE_ADMIN_KEY], phase="worktree-admin", ttl_seconds=120
    )

    assert missing["healthy"] is False
    assert too_long["healthy"] is False
    assert valid["healthy"] is True


def test_emergency_repo_lease_rejects_missing_expected_boundary() -> None:
    from bureau.lease_contract import diagnose_bureau_resource_keys

    report = diagnose_bureau_resource_keys(
        ["repo:/home/alex/repos/bureau"],
        phase="emergency-recovery",
        ttl_seconds=300,
        justification="recover shared metadata",
    )

    assert report["healthy"] is False
    assert report["expected_boundary_present"] is False


def test_operator_intake_read_and_plan_contracts_are_explicit() -> None:
    expected = {
        "operator-candidate-record": {
            "availability_class": "always_on_operational_append",
            "effect": "append_only_state_store_candidate_event",
            "state_store_required": True,
        },
        "operator-candidate-assess": {
            "availability_class": "registry_backed_operational_read",
            "effect": "derived_read_only_candidate_assessment",
            "state_store_required": True,
        },
        "operator-task-propose": {
            "availability_class": "registry_backed_create_only_plan",
            "effect": "create_only_external_review_plan",
            "state_store_required": True,
        },
        "operator-task-publish-preview": {
            "availability_class": "registry_backed_operational_read",
            "effect": "validated_read_only_publication_preview",
            "state_store_required": True,
        },
    }
    for operation, fields in expected.items():
        contract = bureau_lease_contract(operation)
        observed = contract["commands"][operation]
        assert observed["git_repository_lease_required"] is False
        for key, value in fields.items():
            assert observed[key] == value


def test_registry_publication_contract_uses_short_dedicated_gate() -> None:
    from bureau.lease_contract import BUREAU_REGISTRY_PUBLICATION_GATE_KEY

    contract = bureau_lease_contract("registry-publication")
    operation = contract["commands"]["registry-publication"]

    assert operation["required_resource_keys"] == [BUREAU_REGISTRY_PUBLICATION_GATE_KEY]
    assert operation["maximum_ttl_seconds"] == 300
    assert operation["effect"] == ("reviewed_task_file_branch_and_pull_request_publication")
    assert operation["forbidden_resource_keys"] == ["repo:/home/alex/repos/bureau"]
