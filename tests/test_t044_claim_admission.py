"""T044 pre-effect gates: an executable Registry document is not a claim grant."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from bureau import legacy, v2
from bureau.acceptance import (
    AcceptanceContractError,
    t044_claim_admission_reasons,
    validate_acceptance_contract,
)
from bureau.core import Dispatcher, Registry, StateStore
from bureau.legacy import Claim, Resource


def t044_task() -> dict:
    root = Path(__file__).resolve().parents[1]
    return json.loads(
        (root / "registry/tasks/WELTGEWEBE-OS-V1-T044.json").read_text(encoding="utf-8")
    )


@pytest.mark.parametrize(
    "tampering",
    [
        "swap-runtime-manual",
        "changed-criterion-id",
        "changed-runtime-scope",
        "removed-required-probe",
        "changed-manual-scope",
    ],
)
def test_t044_normative_eleven_criterion_contract_rejects_mutation(tampering: str) -> None:
    task = copy.deepcopy(t044_task())
    by_id = {criterion["id"]: criterion for criterion in task["acceptance"]}
    runtime = by_id["production-topology"]
    manual = by_id["staging-gitops-runtime"]
    if tampering == "swap-runtime-manual":
        runtime_config = runtime["verifier_config"]
        runtime["verifier"] = "manual_observation"
        runtime["verifier_config"] = {"observation_scope": runtime_config["observation_scope"]}
        manual["verifier"] = "runtime_proof_bundle"
        manual["verifier_config"] = {
            **runtime_config,
            "observation_scope": manual["verifier_config"]["observation_scope"],
        }
    elif tampering == "changed-criterion-id":
        runtime["id"] = "invented-production-topology"
    elif tampering == "changed-runtime-scope":
        runtime["verifier_config"]["observation_scope"] = "weltgewebe:t044:altered-topology"
    elif tampering == "removed-required-probe":
        runtime["verifier_config"]["required_probes"].pop()
    elif tampering == "changed-manual-scope":
        manual["verifier_config"]["observation_scope"] = "weltgewebe:t044:altered-staging"

    with pytest.raises(AcceptanceContractError, match="t044-proof-contract"):
        validate_acceptance_contract(task)


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


@pytest.mark.parametrize("terminal_state", sorted(v2.TERMINAL_TASK_STATES))
def test_t044_terminal_or_reverified_tasks_cannot_skip_early_claim_admission(
    tmp_path: Path, terminal_state: str
) -> None:
    """Terminal/closure bridges must not reserve Blue before T044 admission."""
    registry = Registry.load(Path(__file__).resolve().parents[1])
    store = StateStore(tmp_path / "bureau.sqlite3", state_root=tmp_path)
    dispatcher = Dispatcher(registry, store, open_pr_reservations_provider=lambda _registry: [])
    task = registry.tasks["WELTGEWEBE-OS-V1-T044"]

    reasons = dispatcher.reasons(
        task,
        set(task.capabilities),
        [],
        [],
        {task.id: terminal_state},
    )

    assert any("T044 claim blocked: authorized Green target" in reason for reason in reasons), (
        terminal_state,
        reasons,
    )
    assert store.list_runs() == []


def test_t044_standalone_handoff_rejects_historic_assignment_without_blue_green(
    tmp_path: Path,
) -> None:
    """Direct handoff must enforce the same frozen-run checks as checkout_next."""
    registry = Registry.load(Path(__file__).resolve().parents[1])
    store = StateStore(tmp_path / "bureau.sqlite3", state_root=tmp_path)
    task = registry.tasks["WELTGEWEBE-OS-V1-T044"]
    run_id = "BUR-RUN-20261013T000000Z-1234567891"
    worker_id = "historic-t044-worker"
    plan_digest = v2.plan_sha256(registry, task.initiative)
    envelope = {
        "run_id": run_id,
        "task_id": task.id,
        "worker_id": worker_id,
        "task_sha256": task.sha256,
        "plan_sha256": plan_digest,
        "task": task.raw,
        "claims": [claim.as_dict() for claim in task.claims],
    }
    now = legacy.utc_now()
    store.register_worker(worker_id, "interactive-agent", tuple(task.capabilities))
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
                task.id,
                worker_id,
                task.sha256,
                plan_digest,
                legacy.canonical_json(envelope),
                legacy.sha256_json(envelope),
                now,
                now,
                now,
            ),
        )
        # No historical Blue/Green reservation exists for this run.

    with pytest.raises(legacy.StateError, match="T044"):
        v2.grabowski_handoff(registry, store, run_id)


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
    _freeze_t044_runtime_identity(task, resources, "runtime.green-test")
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



def test_t044_reasons_use_fresh_registered_runtime_resources(tmp_path: Path) -> None:
    """Admission must use resources reloaded at the claim boundary, not cached resources."""
    registry = Registry.load(Path(__file__).resolve().parents[1])
    store = StateStore(tmp_path / "bureau.sqlite3", state_root=tmp_path)
    task_id = "WELTGEWEBE-OS-V1-T044"
    original = registry.tasks[task_id]
    frozen = copy.deepcopy(original.raw)
    release, green_identity = "a" * 40, "verified-green-test"
    for criterion in frozen["acceptance"]:
        if criterion["verifier"] == "runtime_proof_bundle":
            criterion["verifier_config"].update(
                expected_runtime_revision=release,
                expected_target_identity=green_identity,
            )
    frozen["claims"].extend([
        {"resource": "runtime.commonserver", "mode": "exclusive"},
        {"resource": "runtime.green-isolated", "mode": "exclusive"},
    ])
    fresh_resources = dict(registry.resources)
    fresh_resources["runtime.green-isolated"] = _resource(
        "runtime.green-isolated", "host:isolated-green",
        metadata={
            "t044_role": "green",
            "target_identity": green_identity,
            "expected_runtime_revision": release,
        },
    )
    _freeze_t044_runtime_identity(frozen, fresh_resources, "runtime.green-isolated")
    task = replace(
        original,
        raw=frozen,
        sha256=legacy.sha256_json(frozen),
        claims=tuple(Claim.from_raw(item) for item in frozen["claims"]),
        acceptance=tuple(frozen["acceptance"]),
    )
    registry.tasks[task_id] = task
    dispatcher = Dispatcher(
        registry, store, runtime_proof_reader=lambda _request: {},
        open_pr_reservations_provider=lambda _registry: [],
    )
    stale_reasons = dispatcher.reasons(task, set(task.capabilities), [], [], {})
    assert any("T044 claim blocked:" in value for value in stale_reasons)
    fresh_reasons = dispatcher.reasons(
        task, set(task.capabilities), [], [], {},
        registered_resources=fresh_resources,
    )
    assert not any("T044 claim blocked:" in value for value in fresh_reasons), fresh_reasons


@pytest.mark.parametrize("domain_type", ["host", "failure-domain"])
def test_t044_green_sibling_runtime_rejects_shared_physical_domain(
    domain_type: str,
) -> None:
    """Two logically distinct leases must not share a registered physical fault domain."""
    task = copy.deepcopy(t044_task())
    release = "a" * 40
    target = "shared-physical-domain-green"
    for criterion in task["acceptance"]:
        if criterion["verifier"] == "runtime_proof_bundle":
            criterion["verifier_config"].update(
                expected_runtime_revision=release,
                expected_target_identity=target,
            )
    task["claims"].extend(
        [
            {"resource": "runtime.commonserver", "mode": "exclusive"},
            {"resource": "runtime.green-sibling", "mode": "exclusive"},
        ]
    )
    shared_id = "host.shared-physical" if domain_type == "host" else "fault.shared-physical"
    resources = {
        shared_id: Resource(
            id=shared_id,
            type=domain_type,
            parent="root",
            capacity=1 if domain_type == "failure-domain" else None,
            criticality="essential" if domain_type == "failure-domain" else None,
            path=None,
            github_slug=None,
            grabowski_key="host:shared-physical",
            metadata={"physical": True},
        ),
        "runtime.commonserver": _resource(
            "runtime.commonserver", "service:blue-sibling",
            parent=shared_id,
        ),
        "runtime.green-sibling": _resource(
            "runtime.green-sibling", "service:green-sibling",
            parent=shared_id,
            metadata={
                "t044_role": "green",
                "target_identity": target,
                "expected_runtime_revision": release,
            },
        ),
    }
    _freeze_t044_runtime_identity(task, resources, "runtime.green-sibling")
    reasons = t044_claim_admission_reasons(
        task, runtime_reader_registered=True, registered_resources=resources
    )
    assert any("shared registered physical failure-domain" in r for r in reasons), reasons

    # A shared runtime parent removed from the registry is not proof of isolation.
    missing_parent = dict(resources)
    del missing_parent[shared_id]
    missing_reasons = t044_claim_admission_reasons(
        task, runtime_reader_registered=True, registered_resources=missing_parent,
    )
    assert any("runtime ancestor missing" in r for r in missing_reasons), missing_reasons

    # A shared physical parent cannot evade the gate by changing its type to service.
    disguised_parent = dict(resources)
    disguised_parent[shared_id] = replace(resources[shared_id], type="service")
    disguised_reasons = t044_claim_admission_reasons(
        task, runtime_reader_registered=True, registered_resources=disguised_parent,
    )
    assert any(
        "shared non-group runtime ancestor" in r for r in disguised_reasons
    ), disguised_reasons


@pytest.mark.parametrize("domain_type", ["host", "failure-domain"])
def test_t044_separate_host_ancestors_allow_common_organizational_root(domain_type: str) -> None:
    """A common generic registry root is not evidence of one physical host."""
    task = copy.deepcopy(t044_task())
    release = "a" * 40
    target = "separate-host-green"
    for criterion in task["acceptance"]:
        if criterion["verifier"] == "runtime_proof_bundle":
            criterion["verifier_config"].update(
                expected_runtime_revision=release,
                expected_target_identity=target,
            )
    task["claims"].extend(
        [
            {"resource": "runtime.commonserver", "mode": "exclusive"},
            {"resource": "runtime.green-independent", "mode": "exclusive"},
        ]
    )
    resources = {
        host_id: Resource(
            id=host_id,
            type=domain_type,
            parent="root",
            capacity=None,
            path=None,
            github_slug=None,
            grabowski_key=f"host:{host_id}",
            metadata={},
        )
        for host_id in ("host.blue-physical", "host.green-physical")
    }
    resources["runtime.commonserver"] = _resource(
        "runtime.commonserver", "service:blue", parent="host.blue-physical",
    )
    resources["runtime.green-independent"] = _resource(
        "runtime.green-independent", "service:green", parent="host.green-physical",
        metadata={
            "t044_role": "green",
            "target_identity": target,
            "expected_runtime_revision": release,
        },
    )
    _freeze_t044_runtime_identity(task, resources, "runtime.green-independent")
    assert t044_claim_admission_reasons(
        task, runtime_reader_registered=True, registered_resources=resources,
    ) == []

    # A single physical host can be registered twice under different ids.
    # Identical operator lease keys must never pass as independent ancestors.
    same_physical_host = dict(resources)
    same_physical_host["host.green-physical"] = replace(
        resources["host.green-physical"],
        grabowski_key=resources["host.blue-physical"].grabowski_key,
    )
    reasons = t044_claim_admission_reasons(
        task, runtime_reader_registered=True, registered_resources=same_physical_host,
    )
    assert any("shared registered physical failure-domain" in r for r in reasons), reasons

    # Operator keys remain the same physical identity even under renamed types.
    for disguised_type in ("service", "group", "Host"):
        disguised = dict(same_physical_host)
        disguised["host.green-physical"] = replace(
            same_physical_host["host.green-physical"], type=disguised_type,
        )
        disguised_reasons = t044_claim_admission_reasons(
            task, runtime_reader_registered=True, registered_resources=disguised,
        )
        assert any(
            "shared registered physical failure-domain" in r for r in disguised_reasons
        ), (disguised_type, disguised_reasons)

    # A Green ancestor may also alias Blue's own frozen runtime lease key.
    cross_level_alias = dict(resources)
    cross_level_alias["host.green-physical"] = replace(
        resources["host.green-physical"],
        grabowski_key=resources["runtime.commonserver"].grabowski_key,
    )
    cross_reasons = t044_claim_admission_reasons(
        task, runtime_reader_registered=True, registered_resources=cross_level_alias,
    )
    assert any(
        "shared registered physical failure-domain" in r for r in cross_reasons
    ), cross_reasons

    # A shared logical parent group is not automatically a shared physical host.
    logical_group_id = "group.common-organizational-parent"
    grouped = dict(resources)
    grouped[logical_group_id] = Resource(
        id=logical_group_id, type="group", parent="root", capacity=None,
        path=None, github_slug=None, grabowski_key="repo:organizational-group",
        metadata={},
    )
    for host_id in ("host.blue-physical", "host.green-physical"):
        grouped[host_id] = replace(resources[host_id], parent=logical_group_id)
    assert t044_claim_admission_reasons(
        task, runtime_reader_registered=True, registered_resources=grouped,
    ) == []


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
    _freeze_t044_runtime_identity(frozen, registry.resources, "runtime.green-test")
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

@pytest.mark.parametrize("missing_runtime", ["runtime.commonserver", "runtime.green-test"])
def test_t044_closeout_rejects_missing_exclusive_reservations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing_runtime: str
) -> None:
    """The last terminal CAS must re-read both live Blue/Green reservations."""
    registry = Registry.load(Path(__file__).resolve().parents[1])
    task_id = "WELTGEWEBE-OS-V1-T044"
    original = registry.tasks[task_id]
    frozen = copy.deepcopy(original.raw)
    release, identity = "a" * 40, "isolated-green-closeout-test"
    for criterion in frozen["acceptance"]:
        if criterion["verifier"] == "runtime_proof_bundle":
            criterion["verifier_config"].update(
                expected_runtime_revision=release,
                expected_target_identity=identity,
            )
    frozen["claims"].extend([
        {"resource": "runtime.commonserver", "mode": "exclusive"},
        {"resource": "runtime.green-test", "mode": "exclusive"},
    ])
    registry.resources["runtime.green-test"] = _resource(
        "runtime.green-test", "host:isolated-green-closeout-test",
        metadata={
            "t044_role": "green",
            "target_identity": identity,
            "expected_runtime_revision": release,
        },
    )
    _freeze_t044_runtime_identity(frozen, registry.resources, "runtime.green-test")
    frozen_task = replace(
        original,
        raw=frozen,
        sha256=legacy.sha256_json(frozen),
        claims=tuple(Claim.from_raw(item) for item in frozen["claims"]),
        acceptance=tuple(frozen["acceptance"]),
    )
    registry.tasks[task_id] = frozen_task
    assert t044_claim_admission_reasons(
        frozen, runtime_reader_registered=True, registered_resources=registry.resources
    ) == []
    monkeypatch.setattr(
        v2.Registry, "load", classmethod(lambda cls, _root: registry)
    )
    store = StateStore(tmp_path / "bureau.sqlite3", state_root=tmp_path)
    worker_id = "t044-closeout-test-worker"
    run_id = "BUR-RUN-20261013T000000Z-1234567892"
    plan = v2.plan_sha256(registry, frozen_task.initiative)
    envelope = {
        "run_id": run_id,
        "task_id": task_id,
        "worker_id": worker_id,
        "task_sha256": frozen_task.sha256,
        "plan_sha256": plan,
        "task": frozen,
        "claims": [claim.as_dict() for claim in frozen_task.claims],
    }
    now = legacy.utc_now()
    store.register_worker(worker_id, "interactive-agent", tuple(frozen_task.capabilities))
    with store.immediate() as connection:
        connection.execute(
            """
            INSERT INTO runs(
                run_id,task_id,worker_id,attempt,state,task_sha256,plan_sha256,
                envelope_json,envelope_sha256,created_at,updated_at,heartbeat_at
            ) VALUES(?,?,?,1,'assigned',?,?,?,?,?,?,?)
            """,
            (
                run_id, task_id, worker_id, frozen_task.sha256, plan,
                legacy.canonical_json(envelope), legacy.sha256_json(envelope),
                now, now, now,
            ),
        )
        for claim in frozen_task.claims:
            connection.execute(
                "INSERT INTO reservations(run_id,resource_id,mode,amount,created_at) "
                "VALUES(?,?,?,?,?)",
                (run_id, claim.resource, claim.mode, claim.amount, now),
            )
        row = connection.execute(
            "SELECT * FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        v2._require_t044_closeout_reservations(connection, registry, row, envelope)
        connection.execute(
            "DELETE FROM reservations WHERE run_id=? AND resource_id=?",
            (run_id, missing_runtime),
        )
        with pytest.raises(v2.RunStateConflict, match=r"T044.*reservations"):
            v2._require_t044_closeout_reservations(connection, registry, row, envelope)
    assert store.run(run_id)["state"] == "assigned"

def test_t044_existing_assignment_uses_authoritative_state_store_taskspec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git compatibility need not be republished, but the frozen Run must match StateStore."""
    registry = Registry.load(Path(__file__).resolve().parents[1])
    task_id = "WELTGEWEBE-OS-V1-T044"
    git_task = registry.tasks[task_id]
    frozen = copy.deepcopy(git_task.raw)
    frozen["depends_on"] = []
    frozen["state"] = "ready"
    revision = "a" * 40
    identity = "isolated-green-state-only"
    for criterion in frozen["acceptance"]:
        if criterion["verifier"] == "runtime_proof_bundle":
            criterion["verifier_config"].update(
                expected_runtime_revision=revision,
                expected_target_identity=identity,
            )
    frozen["claims"].extend(
        [
            {"resource": "runtime.commonserver", "mode": "exclusive"},
            {"resource": "runtime.green-state-only", "mode": "exclusive"},
        ]
    )
    registry.resources["runtime.green-state-only"] = _resource(
        "runtime.green-state-only",
        "host:isolated-green-state-only",
        metadata={
            "t044_role": "green",
            "target_identity": identity,
            "expected_runtime_revision": revision,
        },
    )
    _freeze_t044_runtime_identity(frozen, registry.resources, "runtime.green-state-only")
    assert t044_claim_admission_reasons(
        frozen, runtime_reader_registered=True, registered_resources=registry.resources
    ) == []

    store = StateStore(tmp_path / "bureau.sqlite3", state_root=tmp_path)
    first = store.put_task_spec(
        frozen,
        idempotency_key="t044-state-only-original",
        expected_revision=None,
        source="test",
    )
    # A real StateStore TaskSpec is authoritative while the original Git task stays old.
    assert registry.tasks[task_id].raw["acceptance"][0]["verifier_config"][
        "expected_runtime_revision"
    ] is None
    monkeypatch.setattr(
        v2.Registry, "load", classmethod(lambda cls, _root: registry)
    )
    dispatcher = Dispatcher(
        registry,
        store,
        runtime_proof_reader=lambda _request: {},
        open_pr_reservations_provider=lambda _registry: [],
    )
    assert dispatcher.task_authority["kind"] == "bureau-state-store-task-specs"
    task = dispatcher.registry.tasks[task_id]
    assert task.sha256 != git_task.sha256
    plan_digest = v2.plan_sha256(dispatcher.registry, task.initiative)
    run_id = "BUR-RUN-20261013T000000Z-1234567893"
    worker = "t044-state-only-worker"
    envelope = {
        "run_id": run_id,
        "task_id": task_id,
        "worker_id": worker,
        "task_sha256": task.sha256,
        "plan_sha256": plan_digest,
        "task": task.raw,
        "claims": [claim.as_dict() for claim in task.claims],
    }
    now = legacy.utc_now()
    store.register_worker(worker, "interactive-agent", tuple(task.capabilities))
    with store.immediate() as connection:
        connection.execute(
            """
            INSERT INTO runs(
                run_id,task_id,worker_id,attempt,state,task_sha256,plan_sha256,
                envelope_json,envelope_sha256,created_at,updated_at,heartbeat_at
            ) VALUES(?,?,?,1,'assigned',?,?,?,?,?,?,?)
            """,
            (
                run_id, task_id, worker, task.sha256, plan_digest,
                legacy.canonical_json(envelope), legacy.sha256_json(envelope),
                now, now, now,
            ),
        )
        for claim in task.claims:
            connection.execute(
                "INSERT INTO reservations(run_id,resource_id,mode,amount,created_at) "
                "VALUES(?,?,?,?,?)",
                (run_id, claim.resource, claim.mode, claim.amount, now),
            )
        row = connection.execute(
            "SELECT * FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        dispatcher._validate_t044_existing_assignment(connection, row, envelope)

    # A *new* StateStore revision, unlike the old Git projection, must now block.
    modified = copy.deepcopy(frozen)
    modified["title"] = "T044 newer StateStore revision"
    store.put_task_spec(
        modified,
        idempotency_key="t044-state-only-changed",
        expected_revision=first["revision"],
        source="test",
    )
    with store.connect() as connection:
        row = connection.execute(
            "SELECT * FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        with pytest.raises(legacy.StateError, match="snapshot drift"):
            dispatcher._validate_t044_existing_assignment(connection, row, envelope)

def _freeze_t044_runtime_identity(
    task: dict, resources: dict[str, Resource], green_id: str
) -> None:
    """Operator-owned TaskSpec identity; must match independently registered resources."""
    runtime = next(
        item["verifier_config"]
        for item in task["acceptance"]
        if item["verifier"] == "runtime_proof_bundle"
    )
    task.setdefault("metadata", {})["t044_runtime_identity"] = {
        "schema_version": 1,
        "blue_resource_id": "runtime.commonserver",
        "blue_grabowski_key": resources["runtime.commonserver"].grabowski_key,
        "green_resource_id": green_id,
        "green_grabowski_key": resources[green_id].grabowski_key,
        "runtime_revision": runtime["expected_runtime_revision"],
        "target_identity": runtime["expected_target_identity"],
    }


@pytest.mark.parametrize("drift", ["blue-key", "green-key"])
def test_t044_frozen_runtime_resource_keys_reject_retarget(drift: str) -> None:
    """Changing a Grabowski host alias after freezing a cutover must block admission."""
    task = copy.deepcopy(t044_task())
    revision, target = "a" * 40, "isolated-green-key-fixture"
    for criterion in task["acceptance"]:
        if criterion["verifier"] == "runtime_proof_bundle":
            criterion["verifier_config"].update(
                expected_runtime_revision=revision,
                expected_target_identity=target,
            )
    green_id = "runtime.green-key-fixture"
    task["claims"].extend(
        [
            {"resource": "runtime.commonserver", "mode": "exclusive"},
            {"resource": green_id, "mode": "exclusive"},
        ]
    )
    resources = {
        "runtime.commonserver": _resource("runtime.commonserver", "host:commonserver"),
        green_id: _resource(
            green_id,
            "host:isolated-green-key-fixture",
            metadata={
                "t044_role": "green",
                "target_identity": target,
                "expected_runtime_revision": revision,
            },
        ),
    }
    _freeze_t044_runtime_identity(task, resources, green_id)
    assert t044_claim_admission_reasons(
        task, runtime_reader_registered=True, registered_resources=resources
    ) == []
    changed = dict(resources)
    if drift == "blue-key":
        changed["runtime.commonserver"] = _resource(
            "runtime.commonserver", "host:retargeted-production-blue"
        )
    else:
        changed[green_id] = _resource(
            green_id, "host:retargeted-green",
            metadata=resources[green_id].metadata,
        )
    assert any(
        "frozen runtime resource identity" in reason
        for reason in t044_claim_admission_reasons(
            task, runtime_reader_registered=True, registered_resources=changed
        )
    )
