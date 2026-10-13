"""T044 pre-effect gates: an executable Registry document is not a claim grant."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from bureau import legacy, v2
from bureau.acceptance import t044_claim_admission_reasons, validate_acceptance_contract
from bureau.core import Dispatcher, Registry, StateStore
from bureau.legacy import Claim, Resource


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
    dispatcher = Dispatcher(registry, store, open_pr_reservations_provider=lambda _registry: [])
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
    resource_id: str,
    grabowski_key: str,
    *,
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
            "runtime.green-test",
            "host:isolated-green-test",
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
    task["claims"].extend(
        [
            {"resource": "runtime.commonserver", "mode": "exclusive"},
            {"resource": "runtime.green-test", "mode": "exclusive"},
        ]
    )
    assert (
        t044_claim_admission_reasons(
            task, runtime_reader_registered=True, registered_resources=resources
        )
        == []
    )
    # Every independent prerequisite is required, even when the two claims exist.
    assert t044_claim_admission_reasons(
        task, runtime_reader_registered=False, registered_resources=resources
    )

    wrong_revision = dict(resources)
    wrong_revision["runtime.green-test"] = _resource(
        "runtime.green-test",
        "host:isolated-green-test",
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
        "runtime.other-green",
        "host:another-green-test",
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
    task["claims"].extend(
        [
            {"resource": "runtime.commonserver", "mode": "exclusive"},
            {"resource": "runtime.wg-prod-1", "mode": "exclusive"},
        ]
    )
    resources = {
        "runtime.commonserver": _resource("runtime.commonserver", "host:commonserver"),
        "runtime.wg-prod-1": _resource(
            "runtime.wg-prod-1",
            "host:commonserver",
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


@pytest.mark.parametrize(
    ("historical_snapshot", "expected_reason"),
    [
        (True, "T044 existing assignment snapshot drift"),
        (False, "T044 existing assignment missing exact runtime reservations"),
    ],
)
def test_t044_existing_assignment_rechecks_exact_frozen_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    historical_snapshot: bool,
    expected_reason: str,
) -> None:
    registry = Registry.load(Path(__file__).resolve().parents[1])
    store = StateStore(tmp_path / "bureau.sqlite3", state_root=tmp_path)
    task_id = "WELTGEWEBE-OS-V1-T044"
    original = registry.tasks[task_id]
    frozen = copy.deepcopy(original.raw)
    revision = "a" * 40
    green_identity = "kubernetes-green-replay-test"
    for criterion in frozen["acceptance"]:
        if criterion["verifier"] == "runtime_proof_bundle":
            criterion["verifier_config"].update(
                expected_runtime_revision=revision,
                expected_target_identity=green_identity,
            )
    frozen["claims"].extend(
        [
            {"resource": "runtime.commonserver", "mode": "exclusive"},
            {"resource": "runtime.green-test", "mode": "exclusive"},
        ]
    )
    registry.resources["runtime.green-test"] = _resource(
        "runtime.green-test",
        "host:green-replay-test",
        metadata={
            "t044_role": "green",
            "target_identity": green_identity,
            "expected_runtime_revision": revision,
        },
    )
    frozen_task = replace(
        original,
        raw=frozen,
        sha256=legacy.sha256_json(frozen),
        claims=tuple(Claim.from_raw(item) for item in frozen["claims"]),
        acceptance=tuple(frozen["acceptance"]),
    )
    registry.tasks[task_id] = frozen_task
    assert (
        t044_claim_admission_reasons(
            frozen, runtime_reader_registered=True, registered_resources=registry.resources
        )
        == []
    )

    dispatcher = Dispatcher(
        registry,
        store,
        runtime_proof_reader=lambda _request: {},
        open_pr_reservations_provider=lambda _registry: [],
    )
    # This fixture models a future frozen TaskSpec not present on disk yet.
    # Preserve the real registry-load boundary, but return that synthetic
    # registered snapshot for the exact source path under test.
    real_registry_load = Registry.load
    source_root = Path(__file__).resolve().parents[1]

    def load_test_registry(cls: type[Registry], root: Path) -> Registry:
        if Path(root).resolve() == source_root:
            return registry
        return real_registry_load(root)

    monkeypatch.setattr(Registry, "load", classmethod(load_test_registry))
    worker = "t044-existing-assignment-worker"
    run_id = "BUR-RUN-20261013T000000Z-1234567890"
    source = original if historical_snapshot else frozen_task
    run_plan_sha = v2.plan_sha256(registry, frozen_task.initiative)
    envelope = {
        "run_id": run_id,
        "task_id": task_id,
        "worker_id": worker,
        "task_sha256": source.sha256,
        "plan_sha256": run_plan_sha,
        "task": source.raw,
        "claims": [claim.as_dict() for claim in source.claims],
    }
    now = legacy.utc_now()
    store.register_worker(worker, "interactive-agent", tuple(frozen_task.capabilities))
    with store.immediate() as connection:
        connection.execute(
            """
            INSERT INTO runs(
                run_id,task_id,worker_id,attempt,state,task_sha256,plan_sha256,
                envelope_json,envelope_sha256,created_at,updated_at,heartbeat_at
            ) VALUES(?,?,?,1,'assigned',?,?,?,?,?,?,?)
            """,
            (
                run_id,
                task_id,
                worker,
                source.sha256,
                run_plan_sha,
                legacy.canonical_json(envelope),
                legacy.sha256_json(envelope),
                now,
                now,
                now,
            ),
        )
        # Historical reservations, or a current envelope whose Green lease
        # disappeared, are both unsafe before checkout/handoff.
        for claim in original.claims:
            connection.execute(
                "INSERT INTO reservations(run_id,resource_id,mode,amount,created_at) "
                "VALUES(?,?,?,?,?)",
                (run_id, claim.resource, claim.mode, claim.amount, now),
            )
    monkeypatch.setattr(dispatcher, "reconcile", lambda *a, **kw: {})
    monkeypatch.setattr(dispatcher, "_runtime_execution_truth", lambda: {})
    monkeypatch.setattr(dispatcher, "_runtime_execution_stop", lambda **kw: None)
    monkeypatch.setattr(v2, "_requires_agent_brief", lambda *a, **kw: False)
    monkeypatch.setattr(
        v2,
        "grabowski_handoff",
        lambda *a, **kw: {"unexpected_unsafe_handoff": True},
    )
    with pytest.raises(legacy.StateError, match=expected_reason):
        dispatcher.checkout_next(worker, tuple(frozen_task.capabilities))
