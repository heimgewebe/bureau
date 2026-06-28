from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from bureau import cli as bureau_cli
from bureau.adapters import AdapterRegistry, Observation
from bureau.core import (
    Dispatcher,
    Registry,
    StateError,
    StateStore,
    ValidationError,
    cleanup_workspace,
    close_ready_initiatives,
    complete_run,
    fail_run,
    lifecycle_diagnostics,
    verification_stamp,
    workspace_status,
)
from bureau.v2 import plan_sha256, task_revision_sha256


class FakeAdapter:
    system = "grabowski-task"

    def __init__(self, state: str = "running"):
        self.state = state
        self.dispatched: list[dict] = []

    def dispatch(self, request: dict) -> str:
        self.dispatched.append(request)
        return "external-1"

    def recover(self, request_id: str) -> str | None:
        return "external-1" if self.dispatched else None

    def observe(self, external_id: str) -> Observation:
        return Observation(self.state, {"external_id": external_id, "state": self.state})

    def cancel(self, external_id: str) -> dict:
        return {"task_id": external_id, "state": "cancelled"}

    def resume(self, external_id: str) -> dict:
        return {"task_id": external_id, "state": "running"}


def setup(root: Path, tmp_path: Path, monkeypatch, adapters: AdapterRegistry | None = None):
    state = tmp_path / "state"
    monkeypatch.setenv("BUREAU_STATE_DIR", str(state))
    registry = Registry.load(root)
    store = StateStore(state / "bureau.sqlite3")
    return registry, store, Dispatcher(registry, store, adapters)


def claim_and_complete(root: Path, tmp_path: Path, monkeypatch):
    registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    run = dispatcher.claim_next("worker", ("repository",))["run"]
    receipt = complete_run(
        registry,
        store,
        run["run_id"],
        {"proof": {"result": "passed"}},
    )
    return registry, store, run, receipt


def test_schema_contract_rejects_missing_goal(registry_factory):
    root = registry_factory(1)
    initiative = root / "registry/initiatives/main.json"
    value = json.loads(initiative.read_text())
    del value["goal"]
    initiative.write_text(json.dumps(value))
    with pytest.raises(ValidationError, match="goal"):
        Registry.load(root)


def test_schema_contract_rejects_unknown_task_property(registry_factory):
    root = registry_factory(1)
    task = next((root / "registry/tasks").glob("*.json"))
    value = json.loads(task.read_text())
    value["surprise"] = True
    task.write_text(json.dumps(value))
    with pytest.raises(ValidationError, match="surprise"):
        Registry.load(root)


def test_state_root_controls_database_and_sidecars(registry_factory, tmp_path, monkeypatch):
    root = registry_factory(1)
    state_root = tmp_path / "isolated-state"
    registry = Registry.load(root)
    store = StateStore(state_root / "custom.sqlite3")
    dispatcher = Dispatcher(registry, store)
    run = dispatcher.claim_next("worker", ("repository",))["run"]
    assert store.path == state_root / "custom.sqlite3"
    assert store.envelope_path(run["run_id"]).parent == state_root / "envelopes"
    assert store.envelope_path(run["run_id"]).is_file()


def test_database_migrates_old_columns(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE workers(
            worker_id TEXT PRIMARY KEY,kind TEXT,capabilities_json TEXT,heartbeat_at TEXT
        );
        CREATE TABLE runs(
            run_id TEXT PRIMARY KEY,task_id TEXT,worker_id TEXT,attempt INTEGER,state TEXT,
            task_sha256 TEXT,envelope_json TEXT,envelope_sha256 TEXT,external_system TEXT,
            external_id TEXT,workspace_path TEXT,workspace_branch TEXT,error TEXT,
            created_at TEXT,updated_at TEXT,heartbeat_at TEXT
        );
        CREATE TABLE task_status(
            task_id TEXT PRIMARY KEY,state TEXT,receipt_sha256 TEXT,updated_at TEXT
        );
        """
    )
    connection.commit()
    connection.close()
    store = StateStore(database)
    with store.connect() as migrated:
        run_columns = {row["name"] for row in migrated.execute("PRAGMA table_info(runs)")}
        status_columns = {row["name"] for row in migrated.execute("PRAGMA table_info(task_status)")}
        version = migrated.execute("PRAGMA user_version").fetchone()[0]
    assert {"plan_sha256", "dispatch_request_id", "external_state"} <= run_columns
    assert {"task_sha256", "plan_sha256"} <= status_columns
    assert version == 3


def test_task_revision_makes_operational_receipt_stale(registry_factory, tmp_path, monkeypatch):
    root = registry_factory(1)
    _, store, _, _ = claim_and_complete(root, tmp_path, monkeypatch)
    task_path = next((root / "registry/tasks").glob("*.json"))
    task = json.loads(task_path.read_text())
    task["title"] = "Changed after verification"
    task["state"] = "ready"
    task_path.write_text(json.dumps(task))
    changed = Registry.load(root)
    with store.connect() as connection:
        overlays = store.overlays(connection, changed)
    assert overlays[task["id"]] == "stale"


def test_plan_revision_makes_operational_receipt_stale(registry_factory, tmp_path, monkeypatch):
    root = registry_factory(1)
    initiative_path = root / "registry/initiatives/main.json"
    initiative = json.loads(initiative_path.read_text())
    initiative["current_plan"] = {
        "repository": "test",
        "path": "plan.md",
        "commit": "1" * 40,
        "document_sha256": "2" * 64,
    }
    initiative_path.write_text(json.dumps(initiative))
    _, store, _, _ = claim_and_complete(root, tmp_path, monkeypatch)
    initiative["current_plan"]["commit"] = "3" * 40
    initiative_path.write_text(json.dumps(initiative))
    changed = Registry.load(root)
    with store.connect() as connection:
        overlays = store.overlays(connection, changed)
    task_id = next(iter(changed.tasks))
    assert overlays[task_id] == "stale"


def test_completion_is_idempotent(registry_factory, tmp_path, monkeypatch):
    root = registry_factory(1)
    registry, store, run, first = claim_and_complete(root, tmp_path, monkeypatch)
    second = complete_run(registry, store, run["run_id"], {})
    assert second["idempotent"] is True
    assert second["receipt"]["receipt_sha256"] == first["receipt"]["receipt_sha256"]


def test_completion_rejects_task_drift_after_claim(registry_factory, tmp_path, monkeypatch):
    root = registry_factory(1)
    _registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    run = dispatcher.claim_next("worker", ("repository",))["run"]
    task_path = next((root / "registry/tasks").glob("*.json"))
    task = json.loads(task_path.read_text())
    task["title"] = "Drift"
    task_path.write_text(json.dumps(task))
    changed = Registry.load(root)
    with pytest.raises(StateError, match="baseline is stale"):
        complete_run(changed, store, run["run_id"], {"proof": True})


def stale_bound_run(root: Path, tmp_path: Path, monkeypatch, adapter: FakeAdapter):
    _registry, store, dispatcher = setup(
        root,
        tmp_path,
        monkeypatch,
        AdapterRegistry([adapter]),
    )
    run = dispatcher.claim_next("worker", ("repository",))["run"]
    store.bind(run["run_id"], adapter.system, "external-1")
    with store.immediate() as connection:
        connection.execute(
            "UPDATE runs SET heartbeat_at='2000-01-01T00:00:00Z' WHERE run_id=?",
            (run["run_id"],),
        )
    return store, dispatcher, run


def test_reconcile_moves_successful_external_run_to_verifying(
    registry_factory, tmp_path, monkeypatch
):
    store, dispatcher, run = stale_bound_run(
        registry_factory(1), tmp_path, monkeypatch, FakeAdapter("succeeded")
    )
    result = dispatcher.reconcile(1)
    assert result["verifying"] == [run["run_id"]]
    assert store.run(run["run_id"])["state"] == "verifying"


def test_reconcile_releases_failed_external_run(registry_factory, tmp_path, monkeypatch):
    store, dispatcher, run = stale_bound_run(
        registry_factory(1), tmp_path, monkeypatch, FakeAdapter("failed")
    )
    result = dispatcher.reconcile(1)
    assert result["terminal"] == [run["run_id"]]
    refreshed = store.run(run["run_id"])
    assert refreshed["state"] == "failed"
    assert refreshed["reservations"] == []


def test_reconcile_reports_missing_adapter(registry_factory, tmp_path, monkeypatch):
    _registry, store, dispatcher = setup(registry_factory(1), tmp_path, monkeypatch)
    run = dispatcher.claim_next("worker", ("repository",))["run"]
    store.bind(run["run_id"], "missing", "external-1")
    with store.immediate() as connection:
        connection.execute("UPDATE runs SET heartbeat_at='2000-01-01T00:00:00Z'")
    result = dispatcher.reconcile(1)
    assert result["unobserved"][0]["run_id"] == run["run_id"]
    assert store.run(run["run_id"])["state"] == "running"


def test_claim_next_reconciles_orphan_automatically(registry_factory, tmp_path, monkeypatch):
    _registry, store, dispatcher = setup(registry_factory(1), tmp_path, monkeypatch)
    first = dispatcher.claim_next("first", ("repository",))["run"]
    with store.immediate() as connection:
        connection.execute("UPDATE runs SET heartbeat_at='2000-01-01T00:00:00Z'")
    second = dispatcher.claim_next("second", ("repository",))["run"]
    assert second["task_id"] == first["task_id"]
    assert store.run(first["run_id"])["state"] == "orphaned"


def test_checkout_uses_repository_head_when_plan_targets_another_repo(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    subprocess.run(["git", "-C", str(root), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", "base"], check=True, capture_output=True
    )
    initiative_path = root / "registry/initiatives/main.json"
    initiative = json.loads(initiative_path.read_text())
    initiative["current_plan"] = {
        "repository": "another-repo",
        "path": "plan.md",
        "commit": "f" * 40,
        "document_sha256": "e" * 64,
    }
    initiative_path.write_text(json.dumps(initiative))
    _registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    result = dispatcher.checkout_next(
        "worker",
        ("repository",),
        base_dir=tmp_path / "worktrees",
    )
    status = workspace_status(store, result["run"]["run_id"])
    head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    assert status["baseline_commit"] == head


def test_dirty_workspace_is_preserved(registry_factory, tmp_path, monkeypatch):
    root = registry_factory(1)
    subprocess.run(["git", "-C", str(root), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", "base"], check=True, capture_output=True
    )
    _registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    checkout = dispatcher.checkout_next("worker", ("repository",), base_dir=tmp_path / "worktrees")
    run_id = checkout["run"]["run_id"]
    workspace = Path(checkout["run"]["workspace_path"])
    (workspace / "dirty.txt").write_text("dirty")
    fail_run(store, run_id, "test")
    cleaned = cleanup_workspace(store, run_id)
    assert cleaned["state"] == "preserved"
    assert workspace.is_dir()


def test_lifecycle_diagnoses_completion_ready(registry_factory, tmp_path, monkeypatch):
    root = registry_factory(1)
    task_path = next((root / "registry/tasks").glob("*.json"))
    preliminary = Registry.load(root)
    task = json.loads(task_path.read_text())
    task["state"] = "verified"
    task["metadata"] = {
        "verification": {
            "task_sha256": task_revision_sha256(task),
            "plan_sha256": plan_sha256(preliminary, task["initiative"]),
        }
    }
    task_path.write_text(json.dumps(task))
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    lifecycle = lifecycle_diagnostics(registry, store)[0]
    assert lifecycle["recommended_state"] == "completion-ready"


def test_doctor_reports_stale_task(registry_factory, tmp_path, monkeypatch):
    root = registry_factory(1)
    _, store, _, _ = claim_and_complete(root, tmp_path, monkeypatch)
    task_path = next((root / "registry/tasks").glob("*.json"))
    task = json.loads(task_path.read_text())
    task["title"] = "Changed"
    task["state"] = "ready"
    task_path.write_text(json.dumps(task))
    registry = Registry.load(root)
    doctor = Dispatcher(registry, store).doctor()
    assert doctor["healthy"] is False
    assert doctor["stale_tasks"] == [task["id"]]


def test_verified_task_requires_revision_stamp(registry_factory):
    root = registry_factory(1)
    task_path = next((root / "registry/tasks").glob("*.json"))
    task = json.loads(task_path.read_text())
    task["state"] = "verified"
    task_path.write_text(json.dumps(task))
    with pytest.raises(ValidationError, match="task verification"):
        Registry.load(root)


def test_revision_hash_ignores_lifecycle_state():
    base = {
        "schema_version": 1,
        "id": "BUR-TEST-001-T001",
        "initiative": "BUR-TEST-001",
        "title": "Task",
        "state": "ready",
        "execution": {"mode": "interactive-agent", "policy": "autonomous"},
        "claims": [],
        "acceptance": [{"id": "proof", "assertion": "done"}],
    }
    verified = json.loads(json.dumps(base))
    verified["state"] = "verified"
    verified["metadata"] = {"verification": {"task_sha256": "x", "plan_sha256": "y"}}
    assert task_revision_sha256(base) == task_revision_sha256(verified)


def test_verification_stamp_uses_current_operational_receipt(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    registry, store, run, receipt = claim_and_complete(root, tmp_path, monkeypatch)
    stamp = verification_stamp(registry, store, run["task_id"])
    assert stamp["task_sha256"] == run["task_sha256"]
    assert stamp["receipt_sha256"] == receipt["receipt"]["receipt_sha256"]


def test_heartbeat_refreshes_owned_active_run(registry_factory, tmp_path, monkeypatch):
    root = registry_factory(1)
    _, store, dispatcher = setup(root, tmp_path, monkeypatch)
    run = dispatcher.claim_next("worker-a", ("repository",))["run"]
    with store.immediate() as connection:
        connection.execute(
            "UPDATE runs SET heartbeat_at='2000-01-01T00:00:00Z' WHERE run_id=?",
            (run["run_id"],),
        )
        connection.execute(
            "UPDATE workers SET heartbeat_at='2000-01-01T00:00:00Z' WHERE worker_id='worker-a'"
        )
    refreshed = store.heartbeat(run["run_id"], "worker-a")
    assert refreshed["heartbeat_at"] != "2000-01-01T00:00:00Z"
    with pytest.raises(StateError, match="does not own"):
        store.heartbeat(run["run_id"], "worker-b")


class RecoveringAdapter(FakeAdapter):
    def __init__(self, state: str = "running", recover_id: str | None = "external-recovered"):
        super().__init__(state)
        self.recover_id = recover_id

    def dispatch(self, request: dict) -> str:
        self.dispatched.append(request)
        raise RuntimeError("lost response after external start")

    def recover(self, request_id: str) -> str | None:
        return self.recover_id


def test_state_root_rejects_database_outside_root(tmp_path):
    with pytest.raises(StateError, match="inside state_root"):
        StateStore(tmp_path / "other/state.sqlite3", state_root=tmp_path / "state")


def test_future_database_schema_is_rejected(tmp_path):
    database = tmp_path / "state" / "bureau.sqlite3"
    database.parent.mkdir()
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA user_version=99")
    connection.close()
    with pytest.raises(RuntimeError, match="unsupported Bureau state schema"):
        StateStore(database)


def test_dispatch_response_loss_recovers_binding(registry_factory, tmp_path, monkeypatch):
    root = registry_factory(1)
    task_path = next((root / "registry/tasks").glob("*.json"))
    task = json.loads(task_path.read_text())
    task["execution"].update(mode="grabowski-task", argv=["/usr/bin/true"])
    task["claims"][0]["isolation"] = "none"
    task_path.write_text(json.dumps(task))
    adapter = RecoveringAdapter()
    _registry, _store, dispatcher = setup(
        root,
        tmp_path,
        monkeypatch,
        AdapterRegistry([adapter]),
    )
    result = dispatcher.checkout_next(
        "worker",
        ("repository",),
        base_dir=tmp_path / "worktrees",
        dispatch=True,
    )
    assert result["dispatch"]["external_id"] == "external-recovered"
    assert result["run"]["external_state"] == "running"


def test_uncertain_dispatch_is_recovered_by_reconcile(registry_factory, tmp_path, monkeypatch):
    root = registry_factory(1)
    task_path = next((root / "registry/tasks").glob("*.json"))
    task = json.loads(task_path.read_text())
    task["execution"].update(mode="grabowski-task", argv=["/usr/bin/true"])
    task["claims"][0]["isolation"] = "none"
    task_path.write_text(json.dumps(task))
    adapter = RecoveringAdapter(recover_id=None)
    _registry, store, dispatcher = setup(
        root,
        tmp_path,
        monkeypatch,
        AdapterRegistry([adapter]),
    )
    with pytest.raises(StateError, match="dispatch is uncertain"):
        dispatcher.checkout_next(
            "worker",
            ("repository",),
            base_dir=tmp_path / "worktrees",
            dispatch=True,
        )
    run = store.list_runs()[0]
    assert run["external_state"] == "dispatch-uncertain"
    adapter.recover_id = "external-later"
    result = dispatcher.reconcile()
    assert result["recovered"] == [run["run_id"]]
    assert store.run(run["run_id"])["external_id"] == "external-later"


def test_checkout_existing_binding_does_not_redispatch(registry_factory, tmp_path, monkeypatch):
    root = registry_factory(1)
    task_path = next((root / "registry/tasks").glob("*.json"))
    task = json.loads(task_path.read_text())
    task["execution"].update(mode="grabowski-task", argv=["/usr/bin/true"])
    task["claims"][0]["isolation"] = "none"
    task_path.write_text(json.dumps(task))
    adapter = FakeAdapter()
    _registry, _store, dispatcher = setup(
        root,
        tmp_path,
        monkeypatch,
        AdapterRegistry([adapter]),
    )
    first = dispatcher.checkout_next(
        "worker",
        ("repository",),
        base_dir=tmp_path / "worktrees",
        dispatch=True,
    )
    second = dispatcher.checkout_next(
        "worker",
        ("repository",),
        base_dir=tmp_path / "worktrees",
        dispatch=True,
    )
    assert first["run"]["run_id"] == second["run"]["run_id"]
    assert second["dispatch"]["status"] == "existing"
    assert len(adapter.dispatched) == 1


def test_reconcile_observes_bound_run_without_waiting_for_stale_timeout(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    adapter = FakeAdapter("succeeded")
    _registry, store, dispatcher = setup(
        root,
        tmp_path,
        monkeypatch,
        AdapterRegistry([adapter]),
    )
    run = dispatcher.claim_next("worker", ("repository",))["run"]
    store.prepare_dispatch(run["run_id"], adapter.system)
    store.bind(run["run_id"], adapter.system, "external-1")
    result = dispatcher.reconcile(stale_after=999999)
    assert result["verifying"] == [run["run_id"]]


def test_idempotent_receipt_reports_when_registry_revision_is_stale(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    _registry, store, run, _ = claim_and_complete(root, tmp_path, monkeypatch)
    task_path = next((root / "registry/tasks").glob("*.json"))
    task = json.loads(task_path.read_text())
    task["title"] = "Changed after verification"
    task_path.write_text(json.dumps(task))
    changed = Registry.load(root)
    repeated = complete_run(changed, store, run["run_id"], {"proof": {"result": "passed"}})
    assert repeated["idempotent"] is True
    assert repeated["current"] is False


def test_close_ready_updates_initiative_atomically(registry_factory, tmp_path, monkeypatch):
    root = registry_factory(1)
    task_path = next((root / "registry/tasks").glob("*.json"))
    initial = Registry.load(root)
    task = json.loads(task_path.read_text())
    task["state"] = "verified"
    task["metadata"] = {
        "verification": {
            "task_sha256": task_revision_sha256(task),
            "plan_sha256": plan_sha256(initial, task["initiative"]),
        }
    }
    task_path.write_text(json.dumps(task))
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    changed = close_ready_initiatives(registry, store)
    assert changed[0]["initiative_id"] == "BUR-TEST-001"
    initiative = json.loads((root / "registry/initiatives/main.json").read_text())
    assert initiative["state"] == "completed"
    assert initiative["commitment"] == "completed"
    assert initiative["metadata"]["lifecycle"]["completed_at"].endswith("Z")
    Registry.load(root)


def test_doctor_repairs_receipt_sidecar(registry_factory, tmp_path, monkeypatch):
    root = registry_factory(1)
    _registry, store, run, _ = claim_and_complete(root, tmp_path, monkeypatch)
    store.receipt_path(run["run_id"]).unlink()
    dispatcher = Dispatcher(_registry, store)
    before = dispatcher.doctor()
    assert before["healthy"] is False
    assert before["missing_receipts"] == [run["run_id"]]
    repaired = dispatcher.doctor(repair=True)
    assert repaired["missing_receipts"] == []
    assert store.receipt_path(run["run_id"]).is_file()


def test_doctor_reports_lifecycle_mismatch(registry_factory, tmp_path, monkeypatch):
    root = registry_factory(1)
    task_path = next((root / "registry/tasks").glob("*.json"))
    initial = Registry.load(root)
    task = json.loads(task_path.read_text())
    task["state"] = "verified"
    task["metadata"] = {
        "verification": {
            "task_sha256": task_revision_sha256(task),
            "plan_sha256": plan_sha256(initial, task["initiative"]),
        }
    }
    task_path.write_text(json.dumps(task))
    _registry, _store, dispatcher = setup(root, tmp_path, monkeypatch)
    doctor = dispatcher.doctor()
    assert doctor["healthy"] is False
    assert doctor["lifecycle"][0]["recommended_state"] == "completion-ready"


def test_read_command_survives_unavailable_grabowski_adapter(
    registry_factory, tmp_path, monkeypatch, capsys
):
    root = registry_factory(1)

    class BrokenAdapter:
        def __init__(self, source_root):
            raise ModuleNotFoundError("No module named 'mcp'")

    monkeypatch.setattr("bureau.grabowski_adapter.GrabowskiTaskAdapter", BrokenAdapter)
    result = bureau_cli.main(
        [
            "--root",
            str(root),
            "--state-db",
            str(tmp_path / "state/bureau.sqlite3"),
            "--grabowski-source",
            str(tmp_path / "grabowski-src"),
            "--json",
            "status",
        ]
    )
    output = json.loads(capsys.readouterr().out)
    assert result == 0
    assert output["adapters"]["grabowski-task"] == {
        "available": False,
        "detail": "No module named 'mcp'",
        "error_type": "ModuleNotFoundError",
    }


def test_unavailable_adapter_reason_remains_explicit():
    adapters = AdapterRegistry()
    adapters.mark_unavailable("grabowski-task", ModuleNotFoundError("missing runtime dependency"))
    assert adapters.get("grabowski-task") is None
    assert adapters.unavailable_reason("grabowski-task") == (
        "ModuleNotFoundError: missing runtime dependency"
    )


def test_default_grabowski_source_prefers_hash_bound_release(tmp_path, monkeypatch):
    release = tmp_path / "release"
    site_packages = release / ".venv/lib/python3.10/site-packages"
    site_packages.mkdir(parents=True)
    tasks_module = site_packages / "grabowski_tasks.py"
    tasks_module.write_text("# deployed module\n")
    manifest = tmp_path / "deployment-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "immutable_release_path": str(release),
                "module_paths": {"grabowski_tasks": str(tasks_module)},
            }
        )
    )
    monkeypatch.setenv("BUREAU_GRABOWSKI_MANIFEST", str(manifest))
    assert bureau_cli.default_grabowski_source() == site_packages.resolve()


def test_default_grabowski_source_rejects_module_outside_release(tmp_path, monkeypatch):
    release = tmp_path / "release"
    release.mkdir()
    tasks_module = tmp_path / "outside/grabowski_tasks.py"
    tasks_module.parent.mkdir()
    tasks_module.write_text("# unbound module\n")
    manifest = tmp_path / "deployment-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "immutable_release_path": str(release),
                "module_paths": {"grabowski_tasks": str(tasks_module)},
            }
        )
    )
    monkeypatch.setenv("BUREAU_GRABOWSKI_MANIFEST", str(manifest))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "empty-home"))
    assert bureau_cli.default_grabowski_source() is None
