from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from bureau.core import (
    Claim,
    ConflictError,
    Dispatcher,
    NoEligibleTask,
    Registry,
    StateError,
    StateStore,
    ValidationError,
    complete_run,
    create_workspace,
    grabowski_handoff,
)


def setup(root: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BUREAU_STATE_DIR", str(tmp_path / "state"))
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state.sqlite3")
    return registry, store, Dispatcher(registry, store)


def test_registry_loads(registry_factory):
    registry = Registry.load(registry_factory())
    assert len(registry.tasks) == 3
    assert registry.ordered_tasks()[0].id.endswith("T001")


def test_unknown_resource_fails(registry_factory):
    root = registry_factory(1)
    path = next((root / "registry/tasks").glob("*.json"))
    data = json.loads(path.read_text())
    data["claims"][0]["resource"] = "missing"
    path.write_text(json.dumps(data))
    with pytest.raises(ValidationError, match="unknown resource"):
        Registry.load(root)


def test_readers_parallel(registry_factory, tmp_path, monkeypatch):
    _, _, dispatcher = setup(registry_factory(2, "read"), tmp_path, monkeypatch)
    first = dispatcher.claim_next("a", ("repository",))
    second = dispatcher.claim_next("b", ("repository",))
    assert first["run"]["task_id"] != second["run"]["task_id"]


def test_same_worker_is_idempotent(registry_factory, tmp_path, monkeypatch):
    _, _, dispatcher = setup(registry_factory(2), tmp_path, monkeypatch)
    first = dispatcher.claim_next("a", ("repository",))
    second = dispatcher.claim_next("a", ("repository",))
    assert second["status"] == "existing-assignment"
    assert second["run"]["run_id"] == first["run"]["run_id"]


def test_conflicting_writers_serialize(registry_factory, tmp_path, monkeypatch):
    root = registry_factory(2, "write")
    paths = sorted((root / "registry/tasks").glob("*.json"))
    second = json.loads(paths[1].read_text())
    second["claims"][0]["resource"] = "repo.alpha"
    paths[1].write_text(json.dumps(second))
    _, _, dispatcher = setup(root, tmp_path, monkeypatch)
    dispatcher.claim_next("a", ("repository",))
    with pytest.raises(NoEligibleTask):
        dispatcher.claim_next("b", ("repository",))


def test_claim_expansion_conflict(registry_factory, tmp_path, monkeypatch):
    _, _, dispatcher = setup(registry_factory(2, "read"), tmp_path, monkeypatch)
    dispatcher.claim_next("a", ("repository",))
    second = dispatcher.claim_next("b", ("repository",))
    with pytest.raises(ConflictError):
        dispatcher.expand_claim(second["run"]["run_id"], Claim("repo.alpha", "write"), "scope")


def test_completion_requires_evidence(registry_factory, tmp_path, monkeypatch):
    registry, store, dispatcher = setup(registry_factory(1), tmp_path, monkeypatch)
    run = dispatcher.claim_next("a", ("repository",))["run"]
    with pytest.raises(StateError, match="missing evidence"):
        complete_run(registry, store, run["run_id"], {})
    result = complete_run(registry, store, run["run_id"], {"proof": {"result": "passed"}})
    assert result["receipt"]["task_id"].endswith("T001")


def test_reconcile_orphans_unbound_run(registry_factory, tmp_path, monkeypatch):
    _, store, dispatcher = setup(registry_factory(1), tmp_path, monkeypatch)
    run = dispatcher.claim_next("a", ("repository",))["run"]
    with store.immediate() as connection:
        connection.execute("UPDATE runs SET heartbeat_at='2000-01-01T00:00:00Z'")
    assert dispatcher.reconcile(1)["orphaned"] == [run["run_id"]]
    assert dispatcher.claim_next("b", ("repository",))["run"]["task_id"].endswith("T001")


def test_grabowski_handoff_has_idempotency(registry_factory, tmp_path, monkeypatch):
    registry, store, dispatcher = setup(registry_factory(1), tmp_path, monkeypatch)
    run = dispatcher.claim_next("a", ("repository",))["run"]
    handoff = grabowski_handoff(registry, store, run["run_id"])
    assert handoff["origin_ref"] == f"bureau:{run['run_id']}"
    assert handoff["request_id"].endswith(":dispatch-1")


def test_concurrent_claim_stress(registry_factory, tmp_path, monkeypatch):
    root = registry_factory(20, "read", 30)
    monkeypatch.setenv("BUREAU_STATE_DIR", str(tmp_path / "state"))
    registry = Registry.load(root)
    database = tmp_path / "state.sqlite3"
    StateStore(database)

    def claim(index: int) -> str:
        dispatcher = Dispatcher(registry, StateStore(database))
        return dispatcher.claim_next(f"worker-{index}", ("repository",))["run"]["task_id"]

    with ThreadPoolExecutor(max_workers=20) as pool:
        claimed = list(pool.map(claim, range(20)))
    assert len(set(claimed)) == 20


def test_workspace_isolated(registry_factory, tmp_path, monkeypatch):
    root = registry_factory(1)
    subprocess.run(["git", "-C", str(root), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", "initial"], check=True, capture_output=True
    )
    registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    run = dispatcher.claim_next("a", ("repository",))["run"]
    result = create_workspace(registry, store, run["run_id"], tmp_path / "worktrees")
    assert Path(result["workspace_path"]).is_dir()
    assert result["workspace_branch"].startswith("bureau/")
