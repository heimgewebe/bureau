from __future__ import annotations

import json

import pytest
from test_operator_intake import (
    _committed_registry,
    _downgrade_publication_evidence_to_v2,
    _lease_binding,
    _lease_db,
    _proposal,
)

from bureau import operator_intake as operator_intake_module
from bureau import task_specs as task_specs_module
from bureau.core import StateStore
from bureau.operator_intake import OperatorIntakeError, publication_preview, publish_task_proposal


@pytest.mark.parametrize(
    "root_field",
    ["task_spec_root_sha256", "authoritative_root_sha256"],
)
def test_schema_v2_replay_rejects_tampered_projection_root_before_v3_upgrade(
    registry_factory, tmp_path, root_field
):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    plan = json.loads(plan_path.read_text())
    preview = publication_preview(registry, store, plan_path=plan_path)
    receipt = tmp_path / "schema-v2-tampered-root.json"
    first = publish_task_proposal(
        registry,
        store,
        plan_path=plan_path,
        lease_binding=_lease_binding(),
        resource_db=_lease_db(preview, tmp_path),
        workspace_root=tmp_path / "workspaces",
        receipt_path=receipt,
    )
    legacy_evidence = _downgrade_publication_evidence_to_v2(
        store, plan["proposal_sha256"]
    )
    assert legacy_evidence["schema_version"] == 2

    tampered = json.loads(receipt.read_text())
    observed_root = tampered["publication"][root_field]
    tampered["publication"][root_field] = (
        "0" * 64 if observed_root != "0" * 64 else "1" * 64
    )
    unsigned = {
        key: value for key, value in tampered.items() if key != "receipt_sha256"
    }
    tampered["receipt_sha256"] = operator_intake_module.legacy.sha256_json(unsigned)
    receipt.write_text(json.dumps(tampered, indent=2) + "\n")

    with pytest.raises(OperatorIntakeError) as caught:
        publish_task_proposal(
            registry,
            store,
            plan_path=plan_path,
            lease_binding={"owner_id": "must-not-be-read", "task_id": "wrong"},
            resource_db=tmp_path / "must-not-be-read.sqlite3",
            workspace_root=tmp_path / "unused",
            receipt_path=receipt,
        )

    assert caught.value.code == "receipt-conflict"
    assert caught.value.effect_started is True
    assert f"publication.{root_field}" in caught.value.details["mismatched"]
    assert store.task_spec(first["task_id"])["revision"] == 1
    with store.connect() as connection:
        mutation = task_specs_module.get_mutation_receipt(
            connection, f"operator-intake:{plan['proposal_sha256']}"
        )
    assert mutation is not None
    assert mutation["activation_evidence"] == legacy_evidence


def test_schema_v2_replay_uses_publication_event_roots_after_later_state_change(
    registry_factory, tmp_path
):
    _, registry = _committed_registry(registry_factory)
    store = StateStore(tmp_path / "state.sqlite3")
    plan_path = _proposal(registry, store, tmp_path)
    plan = json.loads(plan_path.read_text())
    preview = publication_preview(registry, store, plan_path=plan_path)
    receipt = tmp_path / "schema-v2-historical-root.json"
    first = publish_task_proposal(
        registry,
        store,
        plan_path=plan_path,
        lease_binding=_lease_binding(),
        resource_db=_lease_db(preview, tmp_path),
        workspace_root=tmp_path / "workspaces",
        receipt_path=receipt,
    )
    _downgrade_publication_evidence_to_v2(store, plan["proposal_sha256"])

    store.set_initiative_state("BUR-TEST-001", "active")
    current_projection = store.replay_projection()
    assert (
        current_projection["authoritative_root_sha256"]
        != first["publication"]["authoritative_root_sha256"]
    )

    replay = publish_task_proposal(
        registry,
        store,
        plan_path=plan_path,
        lease_binding={"owner_id": "must-not-be-read", "task_id": "wrong"},
        resource_db=tmp_path / "must-not-be-read.sqlite3",
        workspace_root=tmp_path / "unused",
        receipt_path=receipt,
    )

    assert replay["idempotent_replay"] is True
    assert replay["receipt_sha256"] == first["receipt_sha256"]
    with store.connect() as connection:
        mutation = task_specs_module.get_mutation_receipt(
            connection, f"operator-intake:{plan['proposal_sha256']}"
        )
    assert mutation is not None
    assert mutation["activation_evidence"]["schema_version"] == 3
