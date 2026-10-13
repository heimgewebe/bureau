"""T044 pre-effect gates: an executable Registry document is not a claim grant."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from bureau.acceptance import t044_claim_admission_reasons, validate_acceptance_contract
from bureau.core import Dispatcher, Registry, StateStore
from bureau.legacy import Resource


def t044_task() -> dict:
    root = Path(__file__).resolve().parents[1]
    return json.loads(
        (root / "registry/tasks/WELTGEWEBE-OS-V1-T044.json").read_text(encoding="utf-8")
    )


def test_t044_unselected_green_remains_readable_but_not_claimable() -> None:
    task = t044_task()
    validate_acceptance_contract(task)
    reasons = t044_claim_admission_reasons(task, runtime_reader_registered=False)
    assert any("authorized Green target" in reason for reason in reasons)


def test_t044_frozen_green_still_requires_primary_runtime_reader() -> None:
    task = copy.deepcopy(t044_task())
    for criterion in task["acceptance"]:
        if criterion["verifier"] == "runtime_proof_bundle":
            criterion["verifier_config"].update(
                expected_runtime_revision="a" * 40,
                expected_target_identity="authorized-green",
            )
    validate_acceptance_contract(task)
    assert any(
        "trusted primary runtime proof reader not registered" in reason
        for reason in t044_claim_admission_reasons(task, runtime_reader_registered=False)
    )
    # Reader registration cannot bypass separate resource ownership.
    assert any(
        "runtime resource registry unavailable" in reason
        for reason in t044_claim_admission_reasons(task, runtime_reader_registered=True)
    )


def test_dispatcher_blocks_t044_before_claim_for_missing_green(tmp_path: Path) -> None:
    registry = Registry.load(Path(__file__).resolve().parents[1])
    store = StateStore(tmp_path / "bureau.sqlite3", state_root=tmp_path)
    dispatcher = Dispatcher(
        registry, store, open_pr_reservations_provider=lambda _registry: []
    )
    task = dispatcher.registry.tasks["WELTGEWEBE-OS-V1-T044"]
    reasons = dispatcher.reasons(task, set(task.capabilities), [], [], {})
    assert any("T044 claim blocked: authorized Green target" in reason for reason in reasons)
    assert store.list_runs() == []


def test_t044_frozen_target_and_reader_still_block_without_exclusive_runtime_claim() -> None:
    task = copy.deepcopy(t044_task())
    for criterion in task["acceptance"]:
        if criterion["verifier"] == "runtime_proof_bundle":
            criterion["verifier_config"]["expected_runtime_revision"] = "a" * 40
            criterion["verifier_config"]["expected_target_identity"] = "runtime.green-test"
    validate_acceptance_contract(task)
    reasons = t044_claim_admission_reasons(task, runtime_reader_registered=True)
    assert any("runtime resource" in reason or "exclusive" in reason for reason in reasons)

def _resource(
    resource_id: str, grabowski_key: str, *,
    parent: str = "root",
    metadata: dict | None = None,
) -> Resource:
    return Resource(
        id=resource_id,
        type="service",
        parent=parent,
        capacity=None,
        path=None,
        github_slug=None,
        grabowski_key=grabowski_key,
        metadata=metadata,
    )


def test_t044_exclusive_resource_admission_requires_exact_registered_green_and_blue() -> None:
    task = copy.deepcopy(t044_task())
    release = "a" * 40
    green_identity = "kubernetes-prod-green-unit-test"
    for criterion in task["acceptance"]:
        if criterion["verifier"] == "runtime_proof_bundle":
            criterion["verifier_config"]["expected_runtime_revision"] = release
            criterion["verifier_config"]["expected_target_identity"] = green_identity
    resources = {
        "runtime.commonserver": _resource("runtime.commonserver", "host:commonserver"),
        "runtime.green-test": _resource(
            "runtime.green-test", "host:isolated-green-test",
            metadata={
                "t044_role": "green",
                "target_identity": green_identity,
                "expected_runtime_revision": release,
            },
        ),
    }
    validate_acceptance_contract(task)
    assert any(
        "exclusive" in reason
        for reason in t044_claim_admission_reasons(
            task, runtime_reader_registered=True, registered_resources=resources
        )
    )
    task["claims"].extend([
        {"resource": "runtime.commonserver", "mode": "exclusive"},
        {"resource": "runtime.green-test", "mode": "exclusive"},
    ])
    assert t044_claim_admission_reasons(
        task, runtime_reader_registered=True, registered_resources=resources
    ) == []
    # Every independent prerequisite is required, even when the two claims exist.
    assert t044_claim_admission_reasons(
        task, runtime_reader_registered=False, registered_resources=resources
    )

    wrong_revision = dict(resources)
    wrong_revision["runtime.green-test"] = _resource(
        "runtime.green-test", "host:isolated-green-test",
        metadata={
            "t044_role": "green",
            "target_identity": green_identity,
            "expected_runtime_revision": "b" * 40,
        },
    )
    assert any(
        "exactly one registered runtime resource" in reason
        for reason in t044_claim_admission_reasons(
            task, runtime_reader_registered=True, registered_resources=wrong_revision
        )
    )
    duplicate_green = dict(resources)
    duplicate_green["runtime.other-green"] = _resource(
        "runtime.other-green", "host:another-green-test",
        metadata={
            "t044_role": "green",
            "target_identity": green_identity,
            "expected_runtime_revision": release,
        },
    )
    assert t044_claim_admission_reasons(
        task, runtime_reader_registered=True, registered_resources=duplicate_green
    )


def test_t044_green_runtime_cannot_be_production_blue_alias_or_child() -> None:
    task = copy.deepcopy(t044_task())
    for criterion in task["acceptance"]:
        if criterion["verifier"] == "runtime_proof_bundle":
            criterion["verifier_config"]["expected_runtime_revision"] = "a" * 40
            criterion["verifier_config"]["expected_target_identity"] = "green-alias"
    task["claims"].extend([
        {"resource": "runtime.commonserver", "mode": "exclusive"},
        {"resource": "runtime.wg-prod-1", "mode": "exclusive"},
    ])
    resources = {
        "runtime.commonserver": _resource("runtime.commonserver", "host:commonserver"),
        "runtime.wg-prod-1": _resource(
            "runtime.wg-prod-1", "host:commonserver",
            parent="runtime.commonserver",
            metadata={
                "t044_role": "green",
                "target_identity": "green-alias",
                "expected_runtime_revision": "a" * 40,
            },
        ),
    }
    assert any(
        "alias or overlapping" in reason
        for reason in t044_claim_admission_reasons(
            task, runtime_reader_registered=True, registered_resources=resources
        )
    )
