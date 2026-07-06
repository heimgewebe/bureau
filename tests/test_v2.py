from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from bureau import cli as bureau_cli
from bureau import v2 as bureau_v2
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
from bureau.v2 import plan_sha256, runtime_drift_check, task_revision_sha256


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




def git_output(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def init_clean_origin_main(root: Path) -> str:
    git_output(root, "init", "-b", "main")
    git_output(root, "config", "user.email", "bureau-test@example.invalid")
    git_output(root, "config", "user.name", "Bureau Test")
    git_output(root, "add", ".")
    git_output(root, "commit", "-m", "initial")
    head = git_output(root, "rev-parse", "HEAD")
    git_output(root, "update-ref", "refs/remotes/origin/main", head)
    return head

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



def test_grabowski_task_without_resource_keys_fails_registry_validation(registry_factory):
    root = registry_factory(1)
    task_path = next((root / "registry/tasks").glob("*.json"))
    task = json.loads(task_path.read_text())
    task["execution"].update(mode="grabowski-task", argv=["/usr/bin/true"])
    task_path.write_text(json.dumps(task))

    with pytest.raises(ValidationError) as excinfo:
        Registry.load(root)

    assert "requires at least one Grabowski resource key" in str(excinfo.value)


def test_grabowski_task_handoff_uses_execution_resource_keys(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    task_path = next((root / "registry/tasks").glob("*.json"))
    task = json.loads(task_path.read_text())
    task["execution"].update(
        mode="grabowski-task",
        argv=["/usr/bin/true"],
        grabowski_resources=["repo:/tmp/test-repo"],
    )
    task["claims"][0]["isolation"] = "none"
    task_path.write_text(json.dumps(task))
    _registry, _store, dispatcher = setup(root, tmp_path, monkeypatch)

    result = dispatcher.checkout_next("worker", ("repository",), dispatch=False)

    assert result["handoff"]["resource_keys"] == ["repo:/tmp/test-repo"]


def test_dispatch_response_loss_recovers_binding(registry_factory, tmp_path, monkeypatch):
    root = registry_factory(1)
    task_path = next((root / "registry/tasks").glob("*.json"))
    task = json.loads(task_path.read_text())
    task["execution"].update(
        mode="grabowski-task",
        argv=["/usr/bin/true"],
        grabowski_resources=["repo:/tmp/test-repo"],
    )
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
    task["execution"].update(
        mode="grabowski-task",
        argv=["/usr/bin/true"],
        grabowski_resources=["repo:/tmp/test-repo"],
    )
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
    task["execution"].update(
        mode="grabowski-task",
        argv=["/usr/bin/true"],
        grabowski_resources=["repo:/tmp/test-repo"],
    )
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



def test_adapter_registry_resolves_external_system_alias():
    class AliasAdapter(FakeAdapter):
        aliases = ("grabowski-job",)

    adapter = AliasAdapter()
    registry = AdapterRegistry([adapter])

    assert registry.get("grabowski-task") is adapter
    assert registry.get("grabowski-job") is adapter
    assert registry.status()["grabowski-job"] == {"available": True}


def test_adapter_registry_marks_aliases_unavailable_with_canonical_system():
    class AliasAdapter(FakeAdapter):
        aliases = ("grabowski-job",)

    adapter = AliasAdapter()
    registry = AdapterRegistry([adapter])
    registry.mark_unavailable("grabowski-task", RuntimeError("runtime offline"))

    assert registry.get("grabowski-task") is None
    assert registry.get("grabowski-job") is None
    assert registry.status()["grabowski-task"] == {
        "available": False,
        "detail": "runtime offline",
        "error_type": "RuntimeError",
    }
    assert registry.status()["grabowski-job"] == {
        "available": False,
        "detail": "runtime offline",
        "error_type": "RuntimeError",
    }


def test_adapter_registry_marks_canonical_unavailable_with_alias_system():
    class AliasAdapter(FakeAdapter):
        aliases = ("grabowski-job",)

    adapter = AliasAdapter()
    registry = AdapterRegistry([adapter])
    registry.mark_unavailable("grabowski-job", RuntimeError("runtime offline"))

    assert registry.get("grabowski-task") is None
    assert registry.get("grabowski-job") is None




def test_adapter_registry_rejects_alias_conflicts():
    class FirstAdapter(FakeAdapter):
        system = "first-system"
        aliases = ("shared-alias",)

    class SecondAdapter(FakeAdapter):
        system = "second-system"
        aliases = ("shared-alias",)

    registry = AdapterRegistry([FirstAdapter()])

    with pytest.raises(ValueError, match="shared-alias"):
        registry.add(SecondAdapter())


def test_adapter_registry_remembers_alias_group_after_unavailable():
    class AliasAdapter(FakeAdapter):
        aliases = ("grabowski-job",)

    registry = AdapterRegistry([AliasAdapter()])
    registry.mark_unavailable("grabowski-task", RuntimeError("runtime offline"))
    registry.mark_unavailable("grabowski-job", RuntimeError("still offline"))

    assert registry.get("grabowski-task") is None
    assert registry.get("grabowski-job") is None
    assert registry.status()["grabowski-task"]["detail"] == "still offline"
    assert registry.status()["grabowski-job"]["detail"] == "still offline"

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


def valid_agent_brief(path: Path) -> Path:
    brief = {
        "goal": "Implement the bounded change exactly as scoped.",
        "context_summary": "Grabowski has already identified the target and invariants.",
        "target_files_or_search_scope": ["src/example.py"],
        "acceptance_criteria": ["tests pass"],
        "non_goals": ["do not broaden scope"],
        "allowed_changes": ["minimal code and tests"],
        "forbidden_changes": ["no deployment", "no unrelated rewrites"],
        "validation_commands": ["pytest"],
        "expected_handoff_format": "summary, changed files, validation results, unresolved risks",
    }
    path.write_text(json.dumps(brief), encoding="utf-8")
    return path


def test_external_agent_checkout_requires_valid_grabowski_brief(
    registry_factory, tmp_path, monkeypatch
):
    monkeypatch.setenv("BUREAU_WORKER_ROUTING_CONFIG", str(tmp_path / "routing.json"))
    (tmp_path / "routing.json").write_text(
        json.dumps({"policy": {"agent_brief_required": True}}), encoding="utf-8"
    )
    root = registry_factory(1)
    task_path = next((root / "registry/tasks").glob("*.json"))
    task = json.loads(task_path.read_text())
    task["execution"]["worker_profile"] = "codex-efficient"
    task["claims"][0]["isolation"] = "none"
    task_path.write_text(json.dumps(task))
    _registry, _store, dispatcher = setup(root, tmp_path, monkeypatch)
    with pytest.raises(StateError, match="requires a Grabowski agent brief"):
        dispatcher.checkout_next("codex-worker", ("repository",), base_dir=tmp_path / "worktrees")


def test_external_agent_checkout_accepts_valid_grabowski_brief(
    registry_factory, tmp_path, monkeypatch
):
    monkeypatch.setenv("BUREAU_WORKER_ROUTING_CONFIG", str(tmp_path / "routing.json"))
    (tmp_path / "routing.json").write_text(
        json.dumps({"policy": {"agent_brief_required": True}}), encoding="utf-8"
    )
    root = registry_factory(1)
    brief_path = valid_agent_brief(tmp_path / "brief.json")
    task_path = next((root / "registry/tasks").glob("*.json"))
    task = json.loads(task_path.read_text())
    task["execution"]["worker_profile"] = "codex-efficient"
    task["execution"]["agent_brief_path"] = str(brief_path)
    task["claims"][0]["isolation"] = "none"
    task_path.write_text(json.dumps(task))
    _registry, _store, dispatcher = setup(root, tmp_path, monkeypatch)
    result = dispatcher.checkout_next(
        "codex-worker", ("repository",), base_dir=tmp_path / "worktrees"
    )
    assert result["agent_brief"]["status"] == "valid"
    assert result["handoff"]["agent_brief_path"] == str(brief_path)
    assert result["handoff"]["worker_profile"] == "codex-efficient"




def test_runtime_drift_check_reports_clean_checkout_without_mutation(
    registry_factory, tmp_path
):
    root = registry_factory(1)
    head = init_clean_origin_main(root)
    state = StateStore(tmp_path / "bureau.sqlite3")

    report = runtime_drift_check(root, state_db=state.path)

    assert report["command"] == "runtime-drift-check"
    assert report["read_only"] is True
    assert report["status"] == "ok"
    assert report["checkout"]["branch"] == "main"
    assert report["checkout"]["head"] == head
    assert report["checkout"]["origin_main"] == head
    assert report["checkout"]["head_equals_origin_main"] is True
    assert report["checkout"]["dirty"] is False
    assert report["runtime"]["state_integrity"] == "ok"
    assert report["receipts"]["stale_tasks"] == []
    assert report["receipts"]["active_run_drift"] == []
    assert {item["code"] for item in report["findings"]} == {
        "checkout-clean",
        "receipt-drift-clear",
    }


def test_runtime_drift_check_reports_dirty_checkout_and_receipt_drift(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    init_clean_origin_main(root)
    registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    run = dispatcher.claim_next("worker", ("repository",))["run"]
    complete_run(registry, store, run["run_id"], {"proof": {"result": "passed"}})
    task_path = next((root / "registry/tasks").glob("*.json"))
    task = json.loads(task_path.read_text())
    task["title"] = "Changed after receipt"
    task_path.write_text(json.dumps(task))

    report = runtime_drift_check(root, state_db=store.path)
    codes = {item["code"] for item in report["findings"]}

    assert report["status"] == "blocked"
    assert report["checkout"]["dirty"] is True
    assert any("registry/tasks" in path for path in report["checkout"]["dirty_paths"])
    assert report["receipts"]["stale_tasks"][0]["task_id"] == task["id"]
    assert {"checkout-dirty", "receipt-drift"} <= codes
    assert {item["severity"] for item in report["findings"]} >= {"warning", "blocker"}


def test_runtime_drift_check_reports_untracked_files_when_git_config_hides_them(
    registry_factory, tmp_path
):
    root = registry_factory(1)
    init_clean_origin_main(root)
    subprocess.run(
        ["git", "-C", str(root), "config", "status.showUntrackedFiles", "no"],
        check=True,
    )
    (root / "hidden-untracked.txt").write_text("not tracked\n")
    state = StateStore(tmp_path / "bureau.sqlite3")

    report = runtime_drift_check(root, state_db=state.path)

    assert report["status"] == "warning"
    assert report["checkout"]["dirty"] is True
    assert "?? hidden-untracked.txt" in report["checkout"]["dirty_paths"]
    assert "checkout-dirty" in {item["code"] for item in report["findings"]}


def test_runtime_drift_check_cli_emits_read_only_report(
    registry_factory, tmp_path, capsys
):
    root = registry_factory(1)
    init_clean_origin_main(root)
    state = StateStore(tmp_path / "bureau.sqlite3")

    result = bureau_cli.main(
        [
            "--root",
            str(root),
            "--state-db",
            str(state.path),
            "--json",
            "runtime-drift-check",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert result == 0
    assert output["command"] == "runtime-drift-check"
    assert output["read_only"] is True
    assert output["checkout"]["dirty"] is False

def test_explain_next_reports_runtime_truth_for_lifecycle_reopen(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    initiative_path = root / "registry/initiatives/main.json"
    initiative = json.loads(initiative_path.read_text())
    initiative["state"] = "completed"
    initiative["commitment"] = "completed"
    initiative_path.write_text(json.dumps(initiative))
    task_path = next((root / "registry/tasks").glob("*.json"))
    task = json.loads(task_path.read_text())
    task["state"] = "planned"
    task["execution"]["policy"] = "review-before-effect"
    task_path.write_text(json.dumps(task))

    _registry, _store, dispatcher = setup(root, tmp_path, monkeypatch)
    explained = dispatcher.explain_next({"repository"})

    assert explained["selected"] is None
    assert explained["runtime_truth"]["next_task_available"] is False
    assert explained["runtime_truth"]["lifecycle_mismatch"] is True
    assert explained["runtime_truth"]["health_blocks_normal_claim"] is True
    assert explained["runtime_truth"]["repair_task_required"] is True
    assert explained["runtime_truth"]["repair_recommendations"] == [
        {
            "initiative_id": "BUR-TEST-001",
            "declared_state": "completed",
            "recommended_state": "reopen-required",
            "open_task_count": 1,
            "open_tasks": [task["id"]],
        }
    ]


def test_doctor_reports_runtime_truth_for_lifecycle_mismatch(
    registry_factory, tmp_path, monkeypatch
):
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
    assert doctor["runtime_truth"]["capability_context"] == "not-evaluated"
    assert doctor["runtime_truth"]["lifecycle_mismatch"] is True
    assert doctor["runtime_truth"]["repair_task_required"] is True


def test_no_eligible_cli_paths_expose_runtime_truth(registry_factory, tmp_path, capsys):
    root = registry_factory(1)
    initiative_path = root / "registry/initiatives/main.json"
    initiative = json.loads(initiative_path.read_text())
    initiative["state"] = "completed"
    initiative["commitment"] = "completed"
    initiative_path.write_text(json.dumps(initiative))
    task_path = next((root / "registry/tasks").glob("*.json"))
    task = json.loads(task_path.read_text())
    task["state"] = "planned"
    task["execution"]["policy"] = "review-before-effect"
    task_path.write_text(json.dumps(task))

    for command in ("claim-next", "checkout-next"):
        result = bureau_cli.main(
            [
                "--root",
                str(root),
                "--state-db",
                str(tmp_path / f"{command}.sqlite3"),
                "--json",
                command,
                "--worker",
                command,
                "--capability",
                "repository",
            ]
        )
        output = json.loads(capsys.readouterr().out)
        truth = output["explain_next"]["runtime_truth"]
        assert result == 1
        assert output["status"] == "no-eligible-task"
        assert output["explain_next"]["selected"] is None
        assert truth["repair_task_required"] is True
        assert truth["repair_recommendations"][0]["open_tasks"] == [task["id"]]


def test_explain_next_exposes_read_only_lifecycle_repair_candidate(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    initiative_path = root / "registry/initiatives/main.json"
    initiative = json.loads(initiative_path.read_text())
    initiative["state"] = "completed"
    initiative["commitment"] = "completed"
    initiative_path.write_text(json.dumps(initiative))
    task_path = next((root / "registry/tasks").glob("*.json"))
    task = json.loads(task_path.read_text())
    task["state"] = "planned"
    task["execution"]["policy"] = "review-before-effect"
    task_path.write_text(json.dumps(task))

    _registry, _store, dispatcher = setup(root, tmp_path, monkeypatch)
    explained = dispatcher.explain_next({"repository"})
    candidates = explained["runtime_truth"]["repair_task_candidates"]

    assert explained["runtime_truth"]["repair_task_required"] is True
    assert explained["runtime_truth"]["repair_task_candidate_count"] == 1
    assert candidates == [
        {
            "kind": "bureau_lifecycle_repair_candidate",
            "id": "lifecycle-repair:BUR-TEST-001",
            "initiative_id": "BUR-TEST-001",
            "title": "Repair lifecycle mismatch for BUR-TEST-001",
            "reason": (
                "Initiative state conflicts with open task states; reconcile "
                "initiative lifecycle before claiming normal work."
            ),
            "declared_state": "completed",
            "recommended_state": "reopen-required",
            "open_task_count": 1,
            "open_tasks": [task["id"]],
            "dispatch_allowed": False,
            "queue_mutation_allowed": False,
            "task_creation_allowed": False,
            "suggested_action": "reconcile_initiative_lifecycle",
        }
    ]


def test_git_read_disables_optional_locks(monkeypatch, tmp_path):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(bureau_v2.subprocess, "run", fake_run)

    result = bureau_v2._git_read(tmp_path, ["status", "--porcelain=v1"])

    assert calls[0][0][:2] == ["git", "--no-optional-locks"]
    assert result["stdout"] == "ok"


def test_runtime_drift_check_blocks_when_git_status_fails(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    init_clean_origin_main(root)
    state = StateStore(tmp_path / "bureau.sqlite3")
    original_git_read = bureau_v2._git_read

    def fake_git_read(repo: Path, arguments: list[str]) -> dict[str, object]:
        if arguments == ["status", "--porcelain=v1", "--untracked-files=all"]:
            return {"returncode": 128, "stdout": "", "stderr": "fatal: bad index"}
        return original_git_read(repo, arguments)

    monkeypatch.setattr(bureau_v2, "_git_read", fake_git_read)

    report = runtime_drift_check(root, state_db=state.path)
    codes = {item["code"] for item in report["findings"]}

    assert report["status"] == "blocked"
    assert report["checkout"]["dirty"] is None
    assert "checkout-status-unreadable" in codes
    assert "checkout-clean" not in codes


def test_runtime_drift_check_blocks_incomplete_state_db(registry_factory, tmp_path):
    root = registry_factory(1)
    init_clean_origin_main(root)
    state_path = tmp_path / "incomplete.sqlite3"
    connection = sqlite3.connect(state_path)
    connection.execute("PRAGMA user_version=3")
    connection.execute("CREATE TABLE task_status(task_id TEXT)")
    connection.commit()
    connection.close()

    report = runtime_drift_check(root, state_db=state_path)
    codes = {item["code"] for item in report["findings"]}

    assert report["status"] == "blocked"
    assert report["runtime"]["state_available"] is False
    assert report["runtime"]["state_schema_version"] == 3
    assert "state-db-unavailable" in codes

def test_doctor_reports_known_state_root_entries(registry_factory, tmp_path, monkeypatch):
    root = registry_factory(1)
    registry, store, _ = setup(root, tmp_path, monkeypatch)

    report = Dispatcher(registry, store).doctor()["state_root_hygiene"]

    assert report["healthy"] is True
    assert report["unknown_entries"] == []
    known = {entry["name"]: entry["class"] for entry in report["known_entries"]}
    assert known["bureau.sqlite3"] == "sqlite-database"
    assert known["envelopes"] == "envelope-directory"
    assert known["receipts"] == "receipt-directory"


def test_doctor_uses_configured_state_database_name(registry_factory, tmp_path):
    root = registry_factory(1)
    state_root = tmp_path / "custom-state"
    registry = Registry.load(root)
    store = StateStore(state_root / "custom.sqlite3")
    (state_root / "custom.sqlite3-wal").write_text("", encoding="utf-8")
    (state_root / "custom.sqlite3-shm").write_text("", encoding="utf-8")

    report = Dispatcher(registry, store).doctor()["state_root_hygiene"]

    assert report["healthy"] is True
    assert report["unknown_entries"] == []
    known = {entry["name"]: entry["class"] for entry in report["known_entries"]}
    assert known["custom.sqlite3"] == "sqlite-database"
    assert known["custom.sqlite3-wal"] == "sqlite-sidecar"
    assert known["custom.sqlite3-shm"] == "sqlite-sidecar"


def test_doctor_reports_unknown_state_root_file_without_deleting(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    foreign = store.state_root / "foreign-prompt.txt"
    foreign.write_text("not bureau state", encoding="utf-8")

    doctor = Dispatcher(registry, store).doctor(repair=True)

    assert doctor["healthy"] is False
    assert doctor["state_root_hygiene"]["healthy"] is False
    assert doctor["state_root_hygiene"]["unknown_entries"] == [
        {"name": "foreign-prompt.txt", "type": "file", "class": "unknown"}
    ]
    assert foreign.read_text(encoding="utf-8") == "not bureau state"


def test_doctor_reports_unknown_state_root_directory(registry_factory, tmp_path, monkeypatch):
    root = registry_factory(1)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    foreign = store.state_root / "manual-maintenance"
    foreign.mkdir()

    report = Dispatcher(registry, store).doctor()["state_root_hygiene"]

    assert report["healthy"] is False
    assert report["unknown_entries"] == [
        {"name": "manual-maintenance", "type": "directory", "class": "unknown"}
    ]
