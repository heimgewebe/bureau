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
