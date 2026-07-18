from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from bureau import cli as bureau_cli
from bureau.lease_contract import bureau_lease_contract
from bureau.resource_lifecycle import resource_lifecycle_contract
from bureau.v2 import TERMINAL_STATES

EXPECTED_RESOURCE_KINDS = {
    "task-run",
    "coordination-claim",
    "execution-lease",
    "git-worktree",
    "worker",
    "profile",
    "cache",
    "durable-outbox",
    "generated-bundle",
    "feature-flag",
    "compatibility-layer",
    "deployment-staging",
}


def test_resource_lifecycle_contract_matches_published_schema() -> None:
    root = Path(__file__).resolve().parents[1]
    schema = json.loads(
        (root / "schemas/resource-lifecycle.v1.schema.json").read_text(encoding="utf-8")
    )
    contract = resource_lifecycle_contract()

    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(contract)
    assert set(contract["resource_classes"]) == EXPECTED_RESOURCE_KINDS


def test_task_run_terminal_states_follow_operational_bureau_contract() -> None:
    contract = resource_lifecycle_contract("task-run")
    states = contract["resource_classes"]["task-run"]["terminal_evidence"]["accepted_states"]

    assert set(states) == TERMINAL_STATES


def test_expiry_or_process_absence_never_establishes_terminality() -> None:
    contract = resource_lifecycle_contract()
    resources = contract["resource_classes"]

    assert (
        "ttl_expiry" in resources["coordination-claim"]["terminal_evidence"]["forbidden_inferences"]
    )
    assert "ttl_expiry" in resources["execution-lease"]["terminal_evidence"]["forbidden_inferences"]
    assert (
        "process_absence_alone"
        in resources["git-worktree"]["terminal_evidence"]["forbidden_inferences"]
    )
    assert "safe_retry_from_age_or_process_absence_alone" in contract["does_not_establish"]


def test_cleanup_preserves_authoritative_history_and_foreign_ownership() -> None:
    contract = resource_lifecycle_contract()

    assert "permission_to_delete_historical_evidence" in contract["does_not_establish"]
    assert "permission_to_release_foreign_ownership" in contract["does_not_establish"]
    for resource in contract["resource_classes"].values():
        assert resource["retention"]["historical_evidence"]
        assert resource["cleanup"]["idempotency"]
        assert resource["migration_owners"]


def test_resource_lifecycle_contract_returns_a_defensive_copy() -> None:
    first = resource_lifecycle_contract("cache")
    first["resource_classes"]["cache"]["authority"] = "mutated"

    second = resource_lifecycle_contract("cache")
    assert second["resource_classes"]["cache"]["authority"] == "source-system-not-cache"


def test_unknown_resource_kind_fails_closed() -> None:
    with pytest.raises(ValueError, match="unknown resource lifecycle kind"):
        resource_lifecycle_contract("unknown")


def test_resource_lifecycle_cli_is_read_only_and_checkout_independent(
    monkeypatch, tmp_path, capsys
) -> None:
    def fail_registry_load(_root):
        raise AssertionError("resource-lifecycle-contract must not load the Git registry")

    def fail_mutation_gate(_identity):
        raise AssertionError("read-only lifecycle contract must not enter mutation gate")

    monkeypatch.setattr(bureau_cli.Registry, "load", fail_registry_load)
    monkeypatch.setattr(bureau_cli, "require_mutation_compatible", fail_mutation_gate)

    rc = bureau_cli.main(
        [
            "--root",
            str(tmp_path / "missing-checkout"),
            "--json",
            "resource-lifecycle-contract",
            "--kind",
            "durable-outbox",
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    result = payload.get("result", payload)
    assert list(result["resource_classes"]) == ["durable-outbox"]


def test_lease_contract_declares_checkout_independent_read() -> None:
    contract = bureau_lease_contract("resource-lifecycle-contract")
    operation = contract["commands"]["resource-lifecycle-contract"]

    assert operation == {
        "availability_class": "checkout_independent_read",
        "git_repository_lease_required": False,
        "registry_catalog_required": False,
        "state_store_required": False,
        "effect": "none",
        "conflict_scope": "none",
    }


def test_resource_lifecycle_command_is_classified_read_only() -> None:
    args = bureau_cli.parser().parse_args(["resource-lifecycle-contract"])
    assert bureau_cli._command_mutates(args) is False
