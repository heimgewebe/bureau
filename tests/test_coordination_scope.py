from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from bureau.coordination_scope import changed_paths_sha256, coordination_scope_sha256
from bureau.core import (
    Dispatcher,
    NoEligibleTask,
    Registry,
    StateError,
    StateStore,
    ValidationError,
)
from bureau.v2 import grabowski_handoff

NONCLAIMS = [
    "failure_domain_health",
    "complete_runtime_conflict_state",
    "git_commit_reachability",
    "changed_paths_match_git_diff",
    "merge_or_dispatch_authority",
]
BASE_COMMIT = "0" * 40
SOURCE_COMMIT = "1" * 40


def add_resilience_resource(
    root: Path,
    resource_id: str,
    resource_type: str,
    *,
    capacity: int = 1,
    criticality: str = "essential",
) -> None:
    path = root / "registry/resources" / f"{resource_id}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": resource_id,
                "type": resource_type,
                "parent": "root",
                "capacity": capacity,
                "criticality": criticality,
            }
        ),
        encoding="utf-8",
    )


def scope_for(
    *,
    failure_domains: list[dict] | None = None,
    recovery_paths: list[dict] | None = None,
) -> dict:
    scope = {
        "schema_version": 1,
        "base_commit": BASE_COMMIT,
        "source_commit": SOURCE_COMMIT,
        "changed_paths": ["src/example.py"],
        "changed_paths_sha256": changed_paths_sha256(["src/example.py"]),
        "failure_domains": failure_domains or [],
        "recovery_paths": recovery_paths or [],
        "does_not_establish": list(NONCLAIMS),
    }
    scope["scope_sha256"] = coordination_scope_sha256(scope)
    return scope


def add_scope_claim(
    task_path: Path,
    *,
    section: str,
    resource: str,
    mode: str = "capacity",
    amount: int = 1,
) -> dict:
    task = json.loads(task_path.read_text(encoding="utf-8"))
    item = {"resource": resource, "mode": mode, "amount": amount}
    task["claims"].append(
        {"resource": resource, "mode": mode, "amount": amount, "isolation": "none"}
    )
    task["coordination_scope"] = scope_for(
        failure_domains=[item] if section == "failure_domains" else [],
        recovery_paths=[item] if section == "recovery_paths" else [],
    )
    task_path.write_text(json.dumps(task), encoding="utf-8")
    return task


def setup(root: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BUREAU_STATE_DIR", str(tmp_path / "state"))
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state.sqlite3")
    return registry, store, Dispatcher(registry, store)


def test_failure_domain_resource_requires_capacity_and_criticality(registry_factory) -> None:
    root = registry_factory(1)
    path = root / "registry/resources/failure-domain.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "failure-domain.test",
                "type": "failure-domain",
                "parent": "root",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="required property"):
        Registry.load(root)


def test_resilience_claim_without_scope_is_rejected(registry_factory) -> None:
    root = registry_factory(1)
    add_resilience_resource(root, "failure-domain.shared", "failure-domain")
    task_path = next((root / "registry/tasks").glob("*.json"))
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task["claims"].append(
        {
            "resource": "failure-domain.shared",
            "mode": "capacity",
            "amount": 1,
            "isolation": "none",
        }
    )
    task_path.write_text(json.dumps(task), encoding="utf-8")

    with pytest.raises(ValidationError, match="without coordination_scope"):
        Registry.load(root)


def test_identical_base_and_source_commit_is_rejected(registry_factory) -> None:
    root = registry_factory(1)
    add_resilience_resource(root, "failure-domain.shared", "failure-domain")
    task_path = next((root / "registry/tasks").glob("*.json"))
    task = add_scope_claim(task_path, section="failure_domains", resource="failure-domain.shared")
    task["coordination_scope"]["source_commit"] = BASE_COMMIT
    task["coordination_scope"]["scope_sha256"] = coordination_scope_sha256(
        task["coordination_scope"]
    )
    task_path.write_text(json.dumps(task), encoding="utf-8")

    with pytest.raises(ValidationError, match="identical base and source commits"):
        Registry.load(root)


def test_duplicate_identical_resilience_claim_is_rejected(registry_factory) -> None:
    root = registry_factory(1)
    add_resilience_resource(root, "failure-domain.shared", "failure-domain", capacity=2)
    task_path = next((root / "registry/tasks").glob("*.json"))
    task = add_scope_claim(task_path, section="failure_domains", resource="failure-domain.shared")
    duplicate = deepcopy(task["claims"][-1])
    duplicate.pop("amount")
    task["claims"].append(duplicate)
    task_path.write_text(json.dumps(task), encoding="utf-8")

    with pytest.raises(ValidationError, match="repeats an identical resilience claim"):
        Registry.load(root)


def test_scope_digest_and_claim_binding_are_fail_closed(registry_factory) -> None:
    root = registry_factory(1)
    add_resilience_resource(root, "failure-domain.shared", "failure-domain")
    task_path = next((root / "registry/tasks").glob("*.json"))
    task = add_scope_claim(task_path, section="failure_domains", resource="failure-domain.shared")
    task["coordination_scope"]["changed_paths_sha256"] = "3" * 64
    task_path.write_text(json.dumps(task), encoding="utf-8")

    with pytest.raises(ValidationError, match="stale or invalid scope_sha256"):
        Registry.load(root)

    task["coordination_scope"]["failure_domains"][0]["amount"] = 2
    task["coordination_scope"]["scope_sha256"] = coordination_scope_sha256(
        task["coordination_scope"]
    )
    task_path.write_text(json.dumps(task), encoding="utf-8")
    with pytest.raises(ValidationError, match="does not exactly match resilience claims"):
        Registry.load(root)


def test_changed_paths_are_visible_sorted_and_hash_bound(registry_factory) -> None:
    root = registry_factory(1)
    add_resilience_resource(root, "failure-domain.shared", "failure-domain")
    task_path = next((root / "registry/tasks").glob("*.json"))
    task = add_scope_claim(task_path, section="failure_domains", resource="failure-domain.shared")
    task["coordination_scope"]["changed_paths"] = ["z.py", "a.py"]
    task["coordination_scope"]["changed_paths_sha256"] = changed_paths_sha256(
        task["coordination_scope"]["changed_paths"]
    )
    task["coordination_scope"]["scope_sha256"] = coordination_scope_sha256(
        task["coordination_scope"]
    )
    task_path.write_text(json.dumps(task), encoding="utf-8")

    with pytest.raises(ValidationError, match="sorted and unique"):
        Registry.load(root)

    task["coordination_scope"]["changed_paths"] = ["a.py", "z.py"]
    task["coordination_scope"]["scope_sha256"] = coordination_scope_sha256(
        task["coordination_scope"]
    )
    task_path.write_text(json.dumps(task), encoding="utf-8")
    with pytest.raises(ValidationError, match="stale changed_paths_sha256"):
        Registry.load(root)


def test_shared_failure_domain_serializes_disjoint_file_work(
    registry_factory, tmp_path, monkeypatch
) -> None:
    root = registry_factory(2, "read")
    add_resilience_resource(root, "failure-domain.shared", "failure-domain", capacity=1)
    for task_path in sorted((root / "registry/tasks").glob("*.json")):
        add_scope_claim(task_path, section="failure_domains", resource="failure-domain.shared")

    _, _, dispatcher = setup(root, tmp_path, monkeypatch)
    dispatcher.claim_next("worker-a", ("repository",))
    with pytest.raises(NoEligibleTask):
        dispatcher.claim_next("worker-b", ("repository",))


def test_exclusive_domain_claim_blocks_other_capacity(
    registry_factory, tmp_path, monkeypatch
) -> None:
    root = registry_factory(2, "read")
    add_resilience_resource(root, "failure-domain.shared", "failure-domain", capacity=4)
    task_paths = sorted((root / "registry/tasks").glob("*.json"))
    add_scope_claim(
        task_paths[0],
        section="failure_domains",
        resource="failure-domain.shared",
        mode="exclusive",
    )
    add_scope_claim(task_paths[1], section="failure_domains", resource="failure-domain.shared")

    _, _, dispatcher = setup(root, tmp_path, monkeypatch)
    dispatcher.claim_next("worker-a", ("repository",))
    with pytest.raises(NoEligibleTask):
        dispatcher.claim_next("worker-b", ("repository",))


def test_domain_capacity_preserves_bounded_parallelism(
    registry_factory, tmp_path, monkeypatch
) -> None:
    root = registry_factory(2, "read")
    add_resilience_resource(root, "failure-domain.shared", "failure-domain", capacity=2)
    for task_path in sorted((root / "registry/tasks").glob("*.json")):
        add_scope_claim(task_path, section="failure_domains", resource="failure-domain.shared")

    _, _, dispatcher = setup(root, tmp_path, monkeypatch)
    first = dispatcher.claim_next("worker-a", ("repository",))
    second = dispatcher.claim_next("worker-b", ("repository",))
    assert first["run"]["task_id"] != second["run"]["task_id"]


def test_shared_recovery_path_serializes_disjoint_file_work(
    registry_factory, tmp_path, monkeypatch
) -> None:
    root = registry_factory(2, "read")
    add_resilience_resource(root, "recovery-path.primary", "recovery-path", capacity=1)
    for task_path in sorted((root / "registry/tasks").glob("*.json")):
        add_scope_claim(task_path, section="recovery_paths", resource="recovery-path.primary")

    _, _, dispatcher = setup(root, tmp_path, monkeypatch)
    dispatcher.claim_next("worker-a", ("repository",))
    with pytest.raises(NoEligibleTask):
        dispatcher.claim_next("worker-b", ("repository",))


def test_independent_domains_remain_parallel(registry_factory, tmp_path, monkeypatch) -> None:
    root = registry_factory(2, "read")
    add_resilience_resource(root, "failure-domain.alpha", "failure-domain")
    add_resilience_resource(root, "failure-domain.beta", "failure-domain")
    task_paths = sorted((root / "registry/tasks").glob("*.json"))
    add_scope_claim(task_paths[0], section="failure_domains", resource="failure-domain.alpha")
    add_scope_claim(task_paths[1], section="failure_domains", resource="failure-domain.beta")

    _, _, dispatcher = setup(root, tmp_path, monkeypatch)
    first = dispatcher.claim_next("worker-a", ("repository",))
    second = dispatcher.claim_next("worker-b", ("repository",))
    assert first["run"]["task_id"] != second["run"]["task_id"]


def test_envelope_and_handoff_preserve_hash_bound_scope(
    registry_factory, tmp_path, monkeypatch
) -> None:
    root = registry_factory(1, "read")
    add_resilience_resource(root, "failure-domain.shared", "failure-domain")
    task_path = next((root / "registry/tasks").glob("*.json"))
    task = add_scope_claim(task_path, section="failure_domains", resource="failure-domain.shared")

    _registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    claimed = dispatcher.claim_next("worker", ("repository",))
    run_id = claimed["run"]["run_id"]
    frozen_scope = deepcopy(task["coordination_scope"])

    current = json.loads(task_path.read_text(encoding="utf-8"))
    current["coordination_scope"]["source_commit"] = "4" * 40
    current["coordination_scope"]["scope_sha256"] = coordination_scope_sha256(
        current["coordination_scope"]
    )
    task_path.write_text(json.dumps(current), encoding="utf-8")
    reloaded_registry = Registry.load(root)
    handoff = grabowski_handoff(reloaded_registry, store, run_id)

    assert claimed["envelope"]["coordination_scope"] == frozen_scope
    assert handoff["coordination_scope"] == frozen_scope
    assert handoff["coordination_scope_sha256"] == frozen_scope["scope_sha256"]
    assert handoff["coordination_scope"] != current["coordination_scope"]


def test_handoff_rejects_corrupt_claim_bound_envelope(
    registry_factory, tmp_path, monkeypatch
) -> None:
    root = registry_factory(1, "read")
    add_resilience_resource(root, "failure-domain.shared", "failure-domain")
    task_path = next((root / "registry/tasks").glob("*.json"))
    add_scope_claim(task_path, section="failure_domains", resource="failure-domain.shared")

    registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    claimed = dispatcher.claim_next("worker", ("repository",))
    run_id = claimed["run"]["run_id"]
    corrupt = deepcopy(claimed["envelope"])
    corrupt["coordination_scope"]["source_commit"] = "4" * 40
    with store.immediate() as connection:
        connection.execute(
            "UPDATE runs SET envelope_json=? WHERE run_id=?",
            (json.dumps(corrupt), run_id),
        )

    with pytest.raises(StateError, match="envelope integrity mismatch"):
        grabowski_handoff(registry, store, run_id)
