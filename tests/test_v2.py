from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

from bureau import cli as bureau_cli
from bureau import closure_observer, legacy, registry_snapshot
from bureau import v2 as bureau_v2
from bureau.adapters import AdapterRegistry, Observation
from bureau.bound_activity import (
    BOUND_ACTIVITY_KIND,
    BOUND_ACTIVITY_OUTCOME,
    BOUND_ACTIVITY_SOURCE,
    BOUND_ACTIVITY_UNBOUND_EVIDENCE_SOURCE,
    run_heartbeat_projection,
)
from bureau.core import (
    Dispatcher,
    NoEligibleTask,
    Registry,
    StateError,
    StateStore,
    ValidationError,
    cleanup_workspace,
    close_ready_initiatives,
    fail_run,
    lifecycle_diagnostics,
    verification_stamp,
    workspace_status,
)
from bureau.v2 import (
    _complete_run_after_typed_evaluation as complete_run,
)
from bureau.v2 import (
    coordinated_claim_status,
    plan_sha256,
    runtime_drift_check,
    task_revision_sha256,
)


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



def remove_from_queue(root: Path, task_id: str) -> None:
    queue_path = root / "registry/queue.json"
    queue = json.loads(queue_path.read_text())
    for lane in queue["lanes"].values():
        while task_id in lane:
            lane.remove(task_id)
    queue_path.write_text(json.dumps(queue))

def mark_registry_task_superseded(
    root: Path, task_id: str, successor_task_id: object
) -> None:
    task_path = root / f"registry/tasks/{task_id}.json"
    task = json.loads(task_path.read_text())
    task["state"] = "superseded"
    metadata = dict(task.get("metadata", {}))
    metadata["superseded_by"] = successor_task_id
    task["metadata"] = metadata
    task_path.write_text(json.dumps(task))
    remove_from_queue(root, task_id)


def setup(root: Path, tmp_path: Path, monkeypatch, adapters: AdapterRegistry | None = None):
    state = tmp_path / "state"
    monkeypatch.setenv("BUREAU_STATE_DIR", str(state))
    registry = Registry.load(root)
    store = StateStore(state / "bureau.sqlite3")
    return registry, store, Dispatcher(registry, store, adapters)


class TracingStateStore(StateStore):
    def __init__(self, path: Path):
        self.statements: list[str] = []
        super().__init__(path)

    def connect(self) -> sqlite3.Connection:
        connection = super().connect()
        connection.set_trace_callback(self.statements.append)
        return connection


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


def test_legacy_untyped_task_is_visible_but_not_claimable(
    registry_factory, tmp_path, monkeypatch
) -> None:
    root = registry_factory(1)
    task_path = root / "registry/tasks/BUR-TEST-001-T001.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task["acceptance"] = [{"id": "legacy", "assertion": "legacy prose"}]
    task_path.write_text(json.dumps(task), encoding="utf-8")
    registry, store, _dispatcher = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    dispatcher = Dispatcher(registry, store)

    item = dispatcher.frontier({"repository"})[0]

    assert registry.tasks[task["id"]].acceptance[0]["assertion"] == "legacy prose"
    assert item["eligible"] is False
    assert item["claim_reasons"] == [
        f"invalid acceptance contract: task {task['id']} criterion legacy at "
        "$.acceptance[0].evidence_type: evidence_type is required and must be exactly "
        "'object'; missing=evidence_type; "
        "repair the TaskSpec acceptance contract before claim"
    ]
    with pytest.raises(NoEligibleTask):
        dispatcher.claim_next("worker", ("repository",))


def test_coordinated_claim_intent_rejects_legacy_untyped_task_before_issuance(
    registry_factory, tmp_path, monkeypatch
) -> None:
    root = registry_factory(1)
    task_path = root / "registry/tasks/BUR-TEST-001-T001.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task["acceptance"] = [{"id": "legacy", "assertion": "legacy prose"}]
    task_path.write_text(json.dumps(task), encoding="utf-8")
    registry, store, _dispatcher = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    dispatcher = Dispatcher(registry, store)
    idempotency_key = "legacy-untyped-preselection"

    with pytest.raises(NoEligibleTask, match="invalid acceptance contract"):
        dispatcher.claim_intent(
            "intent-worker",
            ("repository",),
            task_id=task["id"],
            approved=True,
            approval_source="test",
            idempotency_key=idempotency_key,
        )

    assert store.list_runs() == []
    assert store.claim_intent_issuance_by_idempotency_key(idempotency_key) is None


def test_terminal_legacy_untyped_task_remains_readable_without_claim_reason(
    registry_factory, tmp_path, monkeypatch
) -> None:
    root = registry_factory(1)
    task_path = root / "registry/tasks/BUR-TEST-001-T001.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task["state"] = "cancelled"
    task["acceptance"] = [{"id": "legacy", "assertion": "legacy prose"}]
    task_path.write_text(json.dumps(task), encoding="utf-8")
    queue_path = root / "registry/queue.json"
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    queue["lanes"]["now"] = []
    queue_path.write_text(json.dumps(queue), encoding="utf-8")
    registry, _store, dispatcher = setup(root, tmp_path, monkeypatch)

    item = dispatcher.frontier({"repository"})[0]

    assert registry.tasks[task["id"]].state == "cancelled"
    assert item["eligible"] is False
    assert "invalid acceptance contract" not in " ".join(item["claim_reasons"])


@pytest.mark.parametrize(
    ("verifier", "verifier_config"),
    [
        ("manual_observation", {"observation_scope": "manual:test"}),
        (
            "required_ci_green",
            {
                "repository": "heimgewebe/test",
                "pull_request": 7,
                "head_sha": "a" * 40,
                "base_ref": "main",
                "required_checks": ["validate"],
            },
        ),
    ],
)
def test_public_complete_run_cannot_bypass_typed_acceptance(
    registry_factory,
    tmp_path,
    monkeypatch,
    verifier: str,
    verifier_config: dict[str, object],
) -> None:
    root = registry_factory(1)
    task_path = root / "registry/tasks/BUR-TEST-001-T001.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task["acceptance"] = [
        {
            "id": "proof",
            "assertion": "typed evidence must be verified",
            "evidence_type": "object",
            "verifier": verifier,
            "verifier_config": verifier_config,
        }
    ]
    task_path.write_text(json.dumps(task), encoding="utf-8")
    registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    run = dispatcher.claim_next("worker", ("repository",))["run"]

    assert bureau_cli.complete_run is bureau_v2.complete_run
    with pytest.raises(bureau_v2.RunStateConflict) as caught:
        bureau_cli.complete_run(
            registry,
            store,
            run["run_id"],
            {
                "proof": {"arbitrary": "object"},
                "_typed_acceptance": {
                    "state": "passed",
                    "automatic_terminalization": True,
                },
            },
        )

    assert caught.value.code == "typed-acceptance-required"
    assert caught.value.details == {
        "observed_state": "assigned",
        "required_command": "bureau reconcile",
        "required_authority": (
            "closure_observer PASSED authenticated typed evaluation"
        ),
    }
    assert "canonical `bureau reconcile` closure path" in str(caught.value)
    assert store.run(run["run_id"])["state"] == "assigned"
    assert store.receipt(run["run_id"]) is None


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
        CREATE TABLE events(
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT,event_type TEXT,
            event_schema_version INTEGER NOT NULL DEFAULT 0,payload_json TEXT,created_at TEXT
        );
        INSERT INTO events(
            event_id,run_id,event_type,event_schema_version,payload_json,created_at
        ) VALUES(
            0,
            NULL,
            'state-projection-v1',
            1,
            '{"mode":"baseline","projection":{"acceptances":{},"claims":{},"initiatives":{},"runs":{},"schema_version":1,"tasks":{}},"root_sha256":"f0c4d491a9e31211b25251bffcadf6037c7d537df6aa7f6120674b2f5bb9adaa","schema_version":1,"trigger":"schema-v4-migration"}',
            '2026-08-01T00:00:00Z'
        );
        INSERT INTO events(run_id,event_type,payload_json,created_at)
        VALUES(NULL,'run-heartbeat','{}','2026-08-01T00:00:01Z');
        INSERT INTO events(run_id,event_type,payload_json,created_at)
        VALUES(
            'BUR-RUN-MIGRATION',
            'run-heartbeat',
            '{"kind":"bureau.bound_activity_heartbeat","activity":{"activity_id":"migrated-activity"}}',
            '2026-08-01T00:00:02Z'
        );
        PRAGMA user_version=5;
        """
    )
    event_history_before = connection.execute(
        "SELECT event_id,run_id,event_type,event_schema_version,payload_json,created_at "
        "FROM events ORDER BY event_id"
    ).fetchall()
    connection.commit()
    connection.close()
    store = StateStore(database)
    with store.connect() as migrated:
        run_columns = {row["name"] for row in migrated.execute("PRAGMA table_info(runs)")}
        status_columns = {row["name"] for row in migrated.execute("PRAGMA table_info(task_status)")}
        event_columns = {row["name"] for row in migrated.execute("PRAGMA table_info(events)")}
        legacy_event_version = migrated.execute(
            "SELECT event_schema_version FROM events WHERE event_type='run-heartbeat'"
        ).fetchone()[0]
        baseline_count = migrated.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='state-projection-v1' "
            "AND event_schema_version=1"
        ).fetchone()[0]
        migrated_activity_id = migrated.execute(
            "SELECT activity_id FROM events WHERE run_id='BUR-RUN-MIGRATION'"
        ).fetchone()[0]
        event_history_after = migrated.execute(
            "SELECT event_id,run_id,event_type,event_schema_version,payload_json,created_at "
            "FROM events ORDER BY event_id"
        ).fetchall()
        indexes = {
            row["name"] for row in migrated.execute("PRAGMA index_list(events)")
        }
        version = migrated.execute("PRAGMA user_version").fetchone()[0]
    assert {"plan_sha256", "dispatch_request_id", "external_state"} <= run_columns
    assert {"task_sha256", "plan_sha256"} <= status_columns
    assert {"event_schema_version", "activity_id"} <= event_columns
    assert legacy_event_version == 0
    assert baseline_count == 1
    assert migrated_activity_id is None
    assert [tuple(row) for row in event_history_after] == event_history_before
    assert {"unique_bound_activity_id", "events_by_run_activity"} <= indexes
    assert version == 6
    assert store.replay_projection()["matches_current"] is True


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
    second = bureau_v2.complete_run(registry, store, run["run_id"], {})
    assert second["idempotent"] is True
    assert second["receipt"]["receipt_sha256"] == first["receipt"]["receipt_sha256"]


def test_claim_and_completion_projection_replays_current_state(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    run = dispatcher.claim_next("worker", ("repository",))["run"]

    claimed = store.replay_projection()
    assert claimed["matches_current"] is True
    assert claimed["projection"]["runs"][run["run_id"]]["state"] == "assigned"
    assert claimed["projection"]["claims"][run["run_id"]]
    assert run["task_id"] not in claimed["projection"]["tasks"]

    result = complete_run(
        registry,
        store,
        run["run_id"],
        {"proof": {"result": "passed"}},
    )
    completed = store.replay_projection()
    assert completed["matches_current"] is True
    assert completed["projection"]["runs"][run["run_id"]]["state"] == "succeeded"
    assert completed["projection"]["claims"][run["run_id"]] == []
    assert completed["projection"]["tasks"][run["task_id"]]["state"] == "verified"
    assert (
        completed["projection"]["acceptances"][run["run_id"]]["receipt_sha256"]
        == result["receipt"]["receipt_sha256"]
    )


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
        complete_run(changed, store, run["run_id"], {"proof": {"result": "passed"}})


def test_completion_uses_state_store_task_spec_authority_over_stale_git_projection(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    task_id = next(iter(registry.tasks))
    current = store.task_spec(task_id)
    assert current is not None
    revised = json.loads(json.dumps(current["spec"]))
    revised["title"] = "StateStore authoritative task contract"
    updated = store.put_task_spec(
        revised,
        idempotency_key="close-state-store-authority-before-claim",
        expected_revision=current["revision"],
        source="test",
    )

    dispatcher = Dispatcher(registry, store)
    run = dispatcher.claim_next("worker", ("repository",))["run"]
    assert run["task_sha256"] == task_revision_sha256(revised)
    assert updated["spec_sha256"] != bureau_v2.task_specs.task_spec_digest(
        registry.tasks[task_id].raw
    )

    result = complete_run(
        registry,
        store,
        run["run_id"],
        {"proof": {"result": "passed"}},
    )

    assert result["idempotent"] is False
    assert result["current"] is True
    assert store.run(run["run_id"])["state"] == "succeeded"


def test_completion_rejects_state_store_task_spec_drift_after_claim(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    task_id = next(iter(registry.tasks))
    dispatcher = Dispatcher(registry, store)
    run = dispatcher.claim_next("worker", ("repository",))["run"]

    current = store.task_spec(task_id)
    assert current is not None
    drifted = json.loads(json.dumps(current["spec"]))
    drifted["state"] = "planned"
    assert task_revision_sha256(drifted) == run["task_sha256"]
    store.put_task_spec(
        drifted,
        idempotency_key="close-state-store-drift-after-claim",
        expected_revision=current["revision"],
        source="test",
    )

    with pytest.raises(StateError, match="baseline is stale"):
        complete_run(
            registry,
            store,
            run["run_id"],
            {"proof": {"result": "passed"}},
        )

    assert store.run(run["run_id"])["state"] == "assigned"


def test_completion_still_rejects_plan_drift_with_state_store_task_authority(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    dispatcher = Dispatcher(registry, store)
    run = dispatcher.claim_next("worker", ("repository",))["run"]

    initiative_path = root / "registry/initiatives/main.json"
    initiative = json.loads(initiative_path.read_text())
    initiative["current_plan"] = {
        "repository": "test",
        "path": "plan.md",
        "commit": "3" * 40,
        "document_sha256": "4" * 64,
    }
    initiative_path.write_text(json.dumps(initiative))
    changed = Registry.load(root)

    with pytest.raises(StateError, match="baseline is stale"):
        complete_run(
            changed,
            store,
            run["run_id"],
            {"proof": {"result": "passed"}},
        )

    assert store.run(run["run_id"])["state"] == "assigned"


def test_completion_keeps_legacy_git_bootstrap_without_task_specs(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    task_id = next(iter(registry.tasks))
    assert store.task_spec(task_id) is None
    run = dispatcher.claim_next("worker", ("repository",))["run"]

    result = complete_run(
        registry,
        store,
        run["run_id"],
        {"proof": {"result": "passed"}},
    )

    assert result["current"] is True
    assert store.run(run["run_id"])["state"] == "succeeded"


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




@pytest.mark.parametrize("force", [False, True])
def test_missing_workspace_cleanup_reconciles_once(
    registry_factory, tmp_path, monkeypatch, force
):
    root = registry_factory(1)
    init_clean_origin_main(root)
    _registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    checkout = dispatcher.checkout_next(
        "worker", ("repository",), base_dir=tmp_path / "worktrees"
    )
    run_id = checkout["run"]["run_id"]
    workspace = Path(checkout["run"]["workspace_path"])
    fail_run(store, run_id, "test")
    subprocess.run(
        ["git", "-C", str(root), "worktree", "remove", "--force", str(workspace)],
        check=True,
        capture_output=True,
    )

    first = cleanup_workspace(store, run_id, force=force)
    assert first["state"] == "removed"
    assert first["exists"] is False
    assert first["cleanup"] == "already-missing"
    first_updated_at = first["updated_at"]

    second = cleanup_workspace(store, run_id, force=force)
    assert second["state"] == "removed"
    assert second["exists"] is False
    assert second["cleanup"] == "already-missing"
    assert second["updated_at"] == first_updated_at

    with store.connect() as connection:
        event_count = connection.execute(
            "SELECT COUNT(*) FROM events WHERE run_id=? AND event_type='workspace-removed'",
            (run_id,),
        ).fetchone()[0]
    assert event_count == 1


def test_normal_workspace_cleanup_returns_removed_projection_and_repeat_is_noop(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    init_clean_origin_main(root)
    _registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    checkout = dispatcher.checkout_next(
        "worker", ("repository",), base_dir=tmp_path / "worktrees"
    )
    run_id = checkout["run"]["run_id"]
    workspace = Path(checkout["run"]["workspace_path"])
    fail_run(store, run_id, "test")

    first = cleanup_workspace(store, run_id)
    assert first["state"] == "removed"
    assert first["exists"] is False
    assert first["cleanup"] == "removed"
    assert not workspace.exists()
    first_updated_at = first["updated_at"]

    second = cleanup_workspace(store, run_id)
    assert second["state"] == "removed"
    assert second["exists"] is False
    assert second["cleanup"] == "already-missing"
    assert second["updated_at"] == first_updated_at

    with store.connect() as connection:
        event_count = connection.execute(
            "SELECT COUNT(*) FROM events WHERE run_id=? AND event_type='workspace-removed'",
            (run_id,),
        ).fetchone()[0]
    assert event_count == 1


def make_squash_cleanup_fixture(registry_factory, tmp_path, monkeypatch):
    root = registry_factory(1)
    init_clean_origin_main(root)
    git_output(root, "remote", "add", "origin", "https://github.com/heimgewebe/bureau-test.git")
    _registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    checkout = dispatcher.checkout_next(
        "worker", ("repository",), base_dir=tmp_path / "worktrees"
    )
    run_id = checkout["run"]["run_id"]
    workspace = Path(checkout["run"]["workspace_path"])

    (workspace / "squash-effect.txt").write_text("same effect\n")
    git_output(workspace, "add", "squash-effect.txt")
    git_output(workspace, "commit", "-m", "feature head")
    head_sha = git_output(workspace, "rev-parse", "HEAD")

    (root / "squash-effect.txt").write_text("same effect\n")
    git_output(root, "add", "squash-effect.txt")
    git_output(root, "commit", "-m", "squash merge")
    merge_commit_sha = git_output(root, "rev-parse", "HEAD")
    git_output(root, "update-ref", "refs/remotes/origin/main", merge_commit_sha)
    fail_run(store, run_id, "test")

    status = workspace_status(store, run_id)
    assert status["dirty"] is False
    assert status["merged"] is False
    assert status["head"] == head_sha
    return root, store, run_id, workspace, head_sha, merge_commit_sha


def test_squash_workspace_cleanup_accepts_exact_typed_merge_proof(
    registry_factory, tmp_path, monkeypatch
):
    root, store, run_id, workspace, head_sha, merge_commit_sha = make_squash_cleanup_fixture(
        registry_factory, tmp_path, monkeypatch
    )
    proof = {
        "schema_version": 1,
        "status": "verified",
        "repository": "heimgewebe/bureau-test",
        "pull_request": 17,
        "head_sha": head_sha,
        "merge_commit_sha": merge_commit_sha,
        "merged_at": "2026-08-28T10:00:00Z",
        "base_ref": "main",
        "main_sha": merge_commit_sha,
        "comparison_status": "identical",
        "proof_sha256": "f" * 64,
    }

    def observe(repo, observed_head):
        assert repo == root
        assert observed_head == head_sha
        return proof

    monkeypatch.setattr(bureau_v2, "_workspace_squash_merge_proof", observe)
    cleaned = cleanup_workspace(store, run_id)
    assert cleaned["state"] == "removed"
    assert cleaned["cleanup"] == "removed"
    assert cleaned["merge_proof"] == proof
    assert not workspace.exists()

    repeated = cleanup_workspace(store, run_id)
    assert repeated["state"] == "removed"
    assert repeated["cleanup"] == "already-missing"


def test_squash_workspace_cleanup_preserves_without_merge_proof(
    registry_factory, tmp_path, monkeypatch
):
    _root, store, run_id, workspace, _head_sha, _merge_commit_sha = make_squash_cleanup_fixture(
        registry_factory, tmp_path, monkeypatch
    )
    monkeypatch.setattr(bureau_v2, "_workspace_squash_merge_proof", lambda _repo, _head: None)
    cleaned = cleanup_workspace(store, run_id)
    assert cleaned["state"] == "preserved"
    assert cleaned["detail"] == "branch not merged"
    assert workspace.is_dir()


def test_squash_workspace_cleanup_preserves_on_contradictory_merge_evidence(
    registry_factory, tmp_path, monkeypatch
):
    _root, store, run_id, workspace, _head_sha, _merge_commit_sha = make_squash_cleanup_fixture(
        registry_factory, tmp_path, monkeypatch
    )

    def reject(_repo, _head):
        raise bureau_v2.OpenPullRequestObservationError("merge/main readback contradicted")

    monkeypatch.setattr(bureau_v2, "_workspace_squash_merge_proof", reject)
    cleaned = cleanup_workspace(store, run_id)
    assert cleaned["state"] == "preserved"
    assert cleaned["detail"] == "branch not merged"
    assert cleaned["merge_proof"]["status"] == "unavailable"
    assert "contradicted" in cleaned["merge_proof"]["diagnostic"]
    assert workspace.is_dir()


def test_squash_workspace_cleanup_preserves_if_head_changes_during_merge_proof(
    registry_factory, tmp_path, monkeypatch
):
    _root, store, run_id, workspace, head_sha, merge_commit_sha = make_squash_cleanup_fixture(
        registry_factory, tmp_path, monkeypatch
    )
    proof = {
        "schema_version": 1,
        "status": "verified",
        "repository": "heimgewebe/bureau-test",
        "pull_request": 17,
        "head_sha": head_sha,
        "merge_commit_sha": merge_commit_sha,
        "merged_at": "2026-08-28T10:00:00Z",
        "base_ref": "main",
        "main_sha": merge_commit_sha,
        "comparison_status": "identical",
        "proof_sha256": "f" * 64,
    }

    def observe(_repo, observed_head):
        assert observed_head == head_sha
        (workspace / "late-clean-change.txt").write_text("new revision\n")
        git_output(workspace, "add", "late-clean-change.txt")
        git_output(workspace, "commit", "-m", "late clean change")
        return proof

    monkeypatch.setattr(bureau_v2, "_workspace_squash_merge_proof", observe)
    cleaned = cleanup_workspace(store, run_id)
    assert cleaned["state"] == "preserved"
    assert cleaned["detail"] == "workspace changed during merge proof"
    assert cleaned["head"] != head_sha
    assert workspace.is_dir()


def test_squash_workspace_cleanup_preserves_if_workspace_turns_dirty_during_merge_proof(
    registry_factory, tmp_path, monkeypatch
):
    _root, store, run_id, workspace, _head_sha, _merge_commit_sha = make_squash_cleanup_fixture(
        registry_factory, tmp_path, monkeypatch
    )

    def observe(_repo, _observed_head):
        (workspace / "late-dirty-change.txt").write_text("uncommitted\n")
        return {
            "schema_version": 1,
            "status": "verified",
            "repository": "heimgewebe/bureau-test",
            "pull_request": 17,
            "head_sha": _observed_head,
            "merge_commit_sha": "b" * 40,
            "merged_at": "2026-08-28T10:00:00Z",
            "base_ref": "main",
            "main_sha": "b" * 40,
            "comparison_status": "identical",
            "proof_sha256": "f" * 64,
        }

    monkeypatch.setattr(bureau_v2, "_workspace_squash_merge_proof", observe)
    cleaned = cleanup_workspace(store, run_id)
    assert cleaned["state"] == "preserved"
    assert cleaned["detail"] == "workspace changed during merge proof"
    assert cleaned["dirty"] is True
    assert workspace.is_dir()


def test_workspace_squash_merge_proof_binds_exact_pr_head_and_current_main(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    git_output(root, "init", "-b", "main")
    git_output(root, "remote", "add", "origin", "https://github.com/heimgewebe/example.git")
    head_sha = "a" * 40
    merge_commit_sha = "b" * 40
    main_sha = "c" * 40
    calls = []

    monkeypatch.setattr(
        bureau_v2.github_repository,
        "resolve_canonical_repository_identity",
        lambda slug: bureau_v2.github_repository.CanonicalRepositoryIdentity(slug, slug),
    )

    def api(endpoint, *, diagnostic):
        calls.append((endpoint, diagnostic))
        if endpoint.startswith(f"repos/heimgewebe/example/commits/{head_sha}/pulls"):
            return [
                {
                    "number": 17,
                    "state": "closed",
                    "merged_at": "2026-08-28T10:00:00Z",
                    "merge_commit_sha": merge_commit_sha,
                    "head": {"sha": head_sha},
                    "base": {
                        "ref": "main",
                        "repo": {"full_name": "heimgewebe/example"},
                    },
                }
            ]
        if endpoint == "repos/heimgewebe/example/pulls/17":
            return {
                "number": 17,
                "state": "closed",
                "merged": True,
                "merged_at": "2026-08-28T10:00:00Z",
                "merge_commit_sha": merge_commit_sha,
                "head": {"sha": head_sha},
                "base": {
                    "ref": "main",
                    "repo": {"full_name": "heimgewebe/example"},
                },
            }
        if endpoint == "repos/heimgewebe/example/branches/main":
            return {"name": "main", "commit": {"sha": main_sha}}
        if endpoint == f"repos/heimgewebe/example/compare/{merge_commit_sha}...{main_sha}":
            return {
                "status": "ahead",
                "behind_by": 0,
                "base_commit": {"sha": merge_commit_sha},
                "merge_base_commit": {"sha": merge_commit_sha},
            }
        raise AssertionError(endpoint)

    monkeypatch.setattr(bureau_v2, "_workspace_github_api_json", api)
    proof = bureau_v2._workspace_squash_merge_proof(root, head_sha)
    assert proof["repository"] == "heimgewebe/example"
    assert proof["pull_request"] == 17
    assert proof["head_sha"] == head_sha
    assert proof["merge_commit_sha"] == merge_commit_sha
    assert proof["main_sha"] == main_sha
    assert len(proof["proof_sha256"]) == 64
    assert len(calls) == 5


def test_workspace_squash_merge_proof_rejects_stale_pr_head(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    git_output(root, "init", "-b", "main")
    git_output(root, "remote", "add", "origin", "https://github.com/heimgewebe/example.git")
    head_sha = "a" * 40

    monkeypatch.setattr(
        bureau_v2.github_repository,
        "resolve_canonical_repository_identity",
        lambda slug: bureau_v2.github_repository.CanonicalRepositoryIdentity(slug, slug),
    )

    def api(endpoint, *, diagnostic):
        assert endpoint.startswith(f"repos/heimgewebe/example/commits/{head_sha}/pulls")
        return [
            {
                "number": 17,
                "state": "closed",
                "merged_at": "2026-08-28T10:00:00Z",
                "merge_commit_sha": "b" * 40,
                "head": {"sha": "d" * 40},
                "base": {
                    "ref": "main",
                    "repo": {"full_name": "heimgewebe/example"},
                },
            }
        ]

    monkeypatch.setattr(bureau_v2, "_workspace_github_api_json", api)
    assert bureau_v2._workspace_squash_merge_proof(root, head_sha) is None


def test_workspace_squash_merge_proof_uses_canonical_repository_identity(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    git_output(root, "init", "-b", "main")
    git_output(root, "remote", "add", "origin", "https://github.com/HEIMGEWEBE/old-name.git")
    head_sha = "a" * 40
    merge_commit_sha = "b" * 40
    main_sha = "c" * 40
    calls = []

    def canonicalize(slug):
        assert slug == "HEIMGEWEBE/old-name"
        return bureau_v2.github_repository.CanonicalRepositoryIdentity(
            supplied_slug=slug,
            canonical_slug="heimgewebe/new-name",
        )

    monkeypatch.setattr(
        bureau_v2.github_repository,
        "resolve_canonical_repository_identity",
        canonicalize,
    )

    def api(endpoint, *, diagnostic):
        calls.append(endpoint)
        if endpoint.startswith(f"repos/heimgewebe/new-name/commits/{head_sha}/pulls"):
            return [{
                "number": 17,
                "state": "closed",
                "merged_at": "2026-08-28T10:00:00Z",
                "merge_commit_sha": merge_commit_sha,
                "head": {"sha": head_sha},
                "base": {"ref": "main", "repo": {"full_name": "HeimGewebe/New-Name"}},
            }]
        if endpoint == "repos/heimgewebe/new-name/pulls/17":
            return {
                "number": 17,
                "state": "closed",
                "merged": True,
                "merged_at": "2026-08-28T10:00:00Z",
                "merge_commit_sha": merge_commit_sha,
                "head": {"sha": head_sha},
                "base": {"ref": "main", "repo": {"full_name": "HEIMGEWEBE/NEW-NAME"}},
            }
        if endpoint == "repos/heimgewebe/new-name/branches/main":
            return {"name": "main", "commit": {"sha": main_sha}}
        if endpoint == f"repos/heimgewebe/new-name/compare/{merge_commit_sha}...{main_sha}":
            return {
                "status": "ahead",
                "behind_by": 0,
                "base_commit": {"sha": merge_commit_sha},
                "merge_base_commit": {"sha": merge_commit_sha},
            }
        raise AssertionError(endpoint)

    monkeypatch.setattr(bureau_v2, "_workspace_github_api_json", api)
    proof = bureau_v2._workspace_squash_merge_proof(root, head_sha)
    assert proof["repository"] == "heimgewebe/new-name"
    assert proof["head_sha"] == head_sha
    assert len(calls) == 5


def test_workspace_cleanup_still_requires_terminal_run(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    init_clean_origin_main(root)
    _registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    checkout = dispatcher.checkout_next(
        "worker", ("repository",), base_dir=tmp_path / "worktrees"
    )
    run_id = checkout["run"]["run_id"]
    workspace = Path(checkout["run"]["workspace_path"])

    with pytest.raises(StateError, match="workspace cleanup requires a terminal run"):
        cleanup_workspace(store, run_id)
    assert workspace.is_dir()


def test_queue_json_is_compatibility_only_for_unqueued_ready_task(registry_factory, tmp_path):
    root = registry_factory(2, mode="write", max_active=2)
    queue_path = root / "registry/queue.json"
    queue = json.loads(queue_path.read_text())
    queue["lanes"]["now"] = []
    queue_path.write_text(json.dumps(queue))

    second_path = root / "registry/tasks/BUR-TEST-001-T002.json"
    second = json.loads(second_path.read_text())
    second["state"] = "ready"
    second["priority"] = {"lane": "now", "rank": 0}
    second_path.write_text(json.dumps(second))

    first_path = root / "registry/tasks/BUR-TEST-001-T001.json"
    first = json.loads(first_path.read_text())
    first["state"] = "planned"
    first_path.write_text(json.dumps(first))

    subprocess.run(["git", "-C", str(root), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", "base"],
        check=True,
        capture_output=True,
    )

    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    dispatcher = Dispatcher(registry, store)

    frontier = {item["task_id"]: item for item in dispatcher.frontier({"repository"})}
    assert frontier["BUR-TEST-001-T002"]["eligible"] is True
    assert frontier["BUR-TEST-001-T002"]["queue_lane"] is None
    assert (
        "task is not queued in registry/queue.json"
        not in frontier["BUR-TEST-001-T002"]["reasons"]
    )

    intent = dispatcher.claim_intent(
        "intent-worker",
        ("repository",),
        task_id="BUR-TEST-001-T002",
        approved=True,
        approval_source="test",
    )
    assert intent["status"] == "claim-intent"
    assert intent["intent"]["task_id"] == "BUR-TEST-001-T002"

    claimed = dispatcher.claim_next("worker", ("repository",))
    assert claimed["run"]["task_id"] == "BUR-TEST-001-T002"


def test_stale_git_queue_order_does_not_preempt_task_priority(registry_factory, tmp_path):
    root = registry_factory(2, mode="write", max_active=2)
    queue_path = root / "registry/queue.json"
    queue = json.loads(queue_path.read_text())
    queue["lanes"]["now"] = ["BUR-TEST-001-T001"]
    queue_path.write_text(json.dumps(queue))

    first_path = root / "registry/tasks/BUR-TEST-001-T001.json"
    first = json.loads(first_path.read_text())
    first["state"] = "ready"
    first["priority"] = {"lane": "now", "rank": 50}
    first_path.write_text(json.dumps(first))

    second_path = root / "registry/tasks/BUR-TEST-001-T002.json"
    second = json.loads(second_path.read_text())
    second["state"] = "ready"
    second["priority"] = {"lane": "now", "rank": 1}
    second_path.write_text(json.dumps(second))

    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    dispatcher = Dispatcher(registry, store)

    # The legacy Registry view may still render the checked-in queue first;
    # dispatch must instead follow the task priority used by the dynamic frontier.
    assert registry.ordered_tasks()[0].id == "BUR-TEST-001-T001"
    claimed = dispatcher.claim_next("worker", ("repository",))
    assert claimed["run"]["task_id"] == "BUR-TEST-001-T002"


def test_queued_tasks_sort_before_unqueued_priority_tasks(registry_factory):
    root = registry_factory(2, mode="write", max_active=2)
    queue_path = root / "registry/queue.json"
    queue = json.loads(queue_path.read_text())
    queue["lanes"]["now"] = ["BUR-TEST-001-T001"]
    queue_path.write_text(json.dumps(queue))

    second_path = root / "registry/tasks/BUR-TEST-001-T002.json"
    second = json.loads(second_path.read_text())
    second["priority"] = {"lane": "now", "rank": 0}
    second_path.write_text(json.dumps(second))

    registry = Registry.load(root)
    ordered_ids = [task.id for task in registry.ordered_tasks()]
    assert ordered_ids.index("BUR-TEST-001-T001") < ordered_ids.index("BUR-TEST-001-T002")


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
    remove_from_queue(root, task["id"])
    task_path.write_text(json.dumps(task))
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    lifecycle = lifecycle_diagnostics(registry, store)[0]
    assert lifecycle["recommended_state"] == "completion-ready"


def test_lifecycle_reconcile_plans_and_applies_only_safe_active_waiting_transition(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    task_path = next((root / "registry/tasks").glob("*.json"))
    task = json.loads(task_path.read_text())
    task["state"] = "blocked"
    remove_from_queue(root, task["id"])
    task_path.write_text(json.dumps(task))
    initiative_path = root / "registry/initiatives/main.json"
    initiative_before = initiative_path.read_bytes()
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)

    preview = bureau_v2.reconcile_initiative_lifecycle(registry, store)
    assert preview["candidate_count"] == 1
    assert preview["changed_count"] == 0
    assert preview["candidates"][0]["from_state"] == "active"
    assert preview["candidates"][0]["to_state"] == "waiting"

    applied = bureau_v2.reconcile_initiative_lifecycle(registry, store, apply=True)
    assert "scope" not in applied
    assert "task_selector" not in applied
    assert applied["changed_count"] == 1
    assert applied["registry_mutated"] is False
    assert initiative_path.read_bytes() == initiative_before
    with store.connect() as connection:
        row = connection.execute(
            "SELECT state FROM initiative_status WHERE initiative_id=?",
            ("BUR-TEST-001",),
        ).fetchone()
    assert row is not None
    assert row["state"] == "waiting"
    lifecycle = lifecycle_diagnostics(registry, store)[0]
    assert lifecycle["declared_state"] == "waiting"
    assert lifecycle["registry_state"] == "active"
    assert lifecycle["consistent"] is True

    dispatcher = Dispatcher(registry, store)
    dispatcher_lifecycle = dispatcher.explain_next({"repository"})["lifecycle"][0]
    assert dispatcher_lifecycle["declared_state"] == "waiting"
    assert dispatcher_lifecycle["registry_state"] == "active"


def test_lifecycle_reconcile_refuses_stale_initiative_task_inputs(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    task_path = next((root / "registry/tasks").glob("*.json"))
    task = json.loads(task_path.read_text())
    task["state"] = "blocked"
    remove_from_queue(root, task["id"])
    task_path.write_text(json.dumps(task))
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    overlays = iter([{task["id"]: "blocked"}, {task["id"]: "ready"}])
    monkeypatch.setattr(store, "overlays", lambda connection, selected: next(overlays))

    with pytest.raises(StateError, match="lifecycle inputs changed during reconcile"):
        bureau_v2.reconcile_initiative_lifecycle(registry, store, apply=True)

    with store.connect() as connection:
        row = connection.execute(
            "SELECT state FROM initiative_status WHERE initiative_id=?",
            ("BUR-TEST-001",),
        ).fetchone()
    assert row is None


def test_lifecycle_reconcile_promotes_planned_task_after_verified_dependency(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(2)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    task_files_before = {
        path.name: path.read_bytes()
        for path in (root / "registry/tasks").glob("*.json")
    }
    dependency_id, task_id = sorted(registry.tasks)

    dependency = store.task_spec(dependency_id)
    assert dependency is not None
    dependency_spec = dict(dependency["spec"])
    dependency_spec["state"] = "verified"
    store.put_task_spec(
        dependency_spec,
        idempotency_key="lifecycle-dependency-verified",
        expected_revision=dependency["revision"],
        source="test",
    )

    current = store.task_spec(task_id)
    assert current is not None
    task_spec = dict(current["spec"])
    task_spec["state"] = "planned"
    task_spec["depends_on"] = [dependency_id]
    planned = store.put_task_spec(
        task_spec,
        idempotency_key="lifecycle-dependent-planned",
        expected_revision=current["revision"],
        source="test",
    )

    without_evidence = bureau_v2.reconcile_initiative_lifecycle(registry, store)
    assert without_evidence["task_candidate_count"] == 0

    operational, _, _ = bureau_v2.authoritative_task_registry(registry, store)
    current_dependency = store.task_spec(dependency_id)
    assert current_dependency is not None
    dependency_spec = json.loads(json.dumps(current_dependency["spec"]))
    dependency_task = operational.tasks[dependency_id]
    dependency_spec.setdefault("metadata", {})["verification"] = {
        "task_sha256": dependency_task.sha256,
        "plan_sha256": plan_sha256(operational, dependency_task.initiative),
    }
    store.put_task_spec(
        dependency_spec,
        idempotency_key="lifecycle-dependency-verification-evidence",
        expected_revision=current_dependency["revision"],
        source="test",
    )

    preview = bureau_v2.reconcile_initiative_lifecycle(registry, store)
    assert preview["task_candidate_count"] == 1
    candidate = preview["task_candidates"][0]
    assert candidate["task_id"] == task_id
    assert candidate["from_state"] == "planned"
    assert candidate["to_state"] == "ready"
    assert candidate["revision"] == planned["revision"]
    assert candidate["dependency_states"] == {dependency_id: "verified"}

    applied = bureau_v2.reconcile_initiative_lifecycle(registry, store, apply=True)
    assert "scope" not in applied
    assert "task_selector" not in applied
    assert applied["changed_task_count"] == 1
    assert applied["total_changed_count"] == 1
    after = store.task_spec(task_id)
    assert after is not None
    assert after["revision"] == planned["revision"] + 1
    assert after["spec"]["state"] == "ready"
    assert {
        path.name: path.read_bytes()
        for path in (root / "registry/tasks").glob("*.json")
    } == task_files_before


def test_lifecycle_reconcile_supersedes_stale_ready_taskspec_from_explicit_registry(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(2, mode="write", max_active=2)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    task_id, successor_task_id = sorted(registry.tasks)
    before = store.task_spec(task_id)
    assert before is not None

    mark_registry_task_superseded(root, task_id, successor_task_id)
    registry_bytes = (root / f"registry/tasks/{task_id}.json").read_bytes()
    source_registry = Registry.load(root)

    preview = bureau_v2.reconcile_initiative_lifecycle(
        source_registry, store, task_id=task_id
    )
    assert preview["task_candidate_count"] == 1
    candidate = preview["task_candidates"][0]
    assert candidate == {
        "task_id": task_id,
        "from_state": "ready",
        "to_state": "superseded",
        "revision": before["revision"],
        "spec_sha256": before["spec_sha256"],
        "successor_task_id": successor_task_id,
        "registry_task_sha256": source_registry.tasks[task_id].sha256,
        "successor_task_sha256": source_registry.tasks[successor_task_id].sha256,
        "active_run_ids": [],
        "gate": "explicit-registry-supersession-no-active-run-cas",
    }

    applied = bureau_v2.reconcile_initiative_lifecycle(
        source_registry, store, apply=True, task_id=task_id
    )
    assert applied["changed_task_count"] == 1
    assert applied["changed_tasks"][0]["task_id"] == task_id
    after = store.task_spec(task_id)
    assert after is not None
    assert after["revision"] == before["revision"] + 1
    assert after["spec"]["state"] == "superseded"
    assert after["spec"]["metadata"]["superseded_by"] == successor_task_id
    assert (root / f"registry/tasks/{task_id}.json").read_bytes() == registry_bytes

    dispatcher = Dispatcher(Registry.load(root), store)
    frontier = {item["task_id"]: item for item in dispatcher.frontier({"repository"})}
    assert frontier[task_id]["effective_state"] == "superseded"
    assert frontier[task_id]["eligible"] is False
    assert "state is superseded" in frontier[task_id]["reasons"]
    claimed = dispatcher.claim_next("worker", ("repository",))["run"]
    assert claimed["task_id"] == successor_task_id


@pytest.mark.parametrize(
    ("superseded_by", "message"),
    [
        (None, "metadata.superseded_by must name exactly one successor"),
        (["BUR-TEST-001-T002"], "metadata.superseded_by must name exactly one successor"),
        ("BUR-TEST-001-T999", "successor BUR-TEST-001-T999 is missing"),
    ],
)
def test_lifecycle_reconcile_supersession_requires_one_existing_successor(
    registry_factory, tmp_path, monkeypatch, superseded_by, message
):
    root = registry_factory(2)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    task_id = sorted(registry.tasks)[0]
    before = store.task_spec(task_id)
    assert before is not None
    mark_registry_task_superseded(root, task_id, superseded_by)

    with pytest.raises(StateError, match=message):
        bureau_v2.reconcile_initiative_lifecycle(
            Registry.load(root), store, task_id=task_id
        )
    assert store.task_spec(task_id) == before


def test_lifecycle_reconcile_supersession_rejects_active_run(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(2, mode="write", max_active=2)
    registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    task_id, successor_task_id = sorted(registry.tasks)
    before = store.task_spec(task_id)
    assert before is not None
    run = dispatcher.claim_next("active-worker", ("repository",))["run"]
    assert run["task_id"] == task_id
    mark_registry_task_superseded(root, task_id, successor_task_id)

    with pytest.raises(StateError, match="active run exists"):
        bureau_v2.reconcile_initiative_lifecycle(
            Registry.load(root), store, task_id=task_id
        )
    assert store.task_spec(task_id) == before


def test_lifecycle_reconcile_supersession_rechecks_active_run_inside_apply(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(2)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    task_id, successor_task_id = sorted(registry.tasks)
    before = store.task_spec(task_id)
    assert before is not None
    mark_registry_task_superseded(root, task_id, successor_task_id)

    observations = iter(
        [
            [],
            [{"task_id": task_id, "run_id": "BUR-RUN-RACE"}],
        ]
    )
    monkeypatch.setattr(store, "active_runs", lambda connection: next(observations))

    with pytest.raises(
        StateError,
        match="task lifecycle gates changed during lifecycle reconcile: active run exists",
    ):
        bureau_v2.reconcile_initiative_lifecycle(
            Registry.load(root), store, apply=True, task_id=task_id
        )
    assert store.task_spec(task_id) == before


def test_lifecycle_reconcile_supersession_rechecks_successor_digest_inside_apply(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(2)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    task_id, successor_task_id = sorted(registry.tasks)
    before = store.task_spec(task_id)
    assert before is not None
    mark_registry_task_superseded(root, task_id, successor_task_id)
    selected_registry = Registry.load(root)

    original_load = bureau_v2.Registry.load
    loads = 0

    def drifting_load(path):
        nonlocal loads
        loads += 1
        if loads == 2:
            successor_path = root / f"registry/tasks/{successor_task_id}.json"
            successor = json.loads(successor_path.read_text())
            successor["title"] += " drift"
            successor_path.write_text(json.dumps(successor))
        return original_load(path)

    monkeypatch.setattr(bureau_v2.Registry, "load", staticmethod(drifting_load))

    with pytest.raises(
        StateError, match="task lifecycle gates changed during lifecycle reconcile"
    ):
        bureau_v2.reconcile_initiative_lifecycle(
            selected_registry, store, apply=True, task_id=task_id
        )
    assert store.task_spec(task_id) == before


def test_lifecycle_reconcile_supersession_leaves_already_terminal_taskspec_unchanged(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(2)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    task_id, successor_task_id = sorted(registry.tasks)
    current = store.task_spec(task_id)
    assert current is not None
    terminal_spec = json.loads(json.dumps(current["spec"]))
    terminal_spec["state"] = "superseded"
    store.put_task_spec(
        terminal_spec,
        idempotency_key="already-superseded",
        expected_revision=current["revision"],
        source="test",
    )
    terminal = store.task_spec(task_id)
    assert terminal is not None
    mark_registry_task_superseded(root, task_id, successor_task_id)

    with pytest.raises(StateError, match="StateStore state is superseded, expected ready"):
        bureau_v2.reconcile_initiative_lifecycle(
            Registry.load(root), store, task_id=task_id
        )
    assert store.task_spec(task_id) == terminal


def test_lifecycle_reconcile_promotes_parent_after_terminal_child_gate(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(2)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    parent_id, child_id = sorted(registry.tasks)

    parent = store.task_spec(parent_id)
    assert parent is not None
    parent_spec = dict(parent["spec"])
    parent_spec["state"] = "planned"
    store.put_task_spec(
        parent_spec,
        idempotency_key="lifecycle-parent-planned",
        expected_revision=parent["revision"],
        source="test",
    )

    child = store.task_spec(child_id)
    assert child is not None
    child_spec = dict(child["spec"])
    child_spec["state"] = "verified"
    child_metadata = dict(child_spec.get("metadata", {}))
    child_metadata["parent_task"] = parent_id
    child_spec["metadata"] = child_metadata
    store.put_task_spec(
        child_spec,
        idempotency_key="lifecycle-child-verified",
        expected_revision=child["revision"],
        source="test",
    )

    without_evidence = bureau_v2.reconcile_initiative_lifecycle(registry, store)
    assert without_evidence["task_candidate_count"] == 0

    operational, _, _ = bureau_v2.authoritative_task_registry(registry, store)
    current_child = store.task_spec(child_id)
    assert current_child is not None
    child_spec = json.loads(json.dumps(current_child["spec"]))
    child_task = operational.tasks[child_id]
    child_spec.setdefault("metadata", {})["verification"] = {
        "task_sha256": child_task.sha256,
        "plan_sha256": plan_sha256(operational, child_task.initiative),
    }
    store.put_task_spec(
        child_spec,
        idempotency_key="lifecycle-child-verification-evidence",
        expected_revision=current_child["revision"],
        source="test",
    )

    preview = bureau_v2.reconcile_initiative_lifecycle(registry, store)
    assert preview["task_candidate_count"] == 1
    candidate = preview["task_candidates"][0]
    assert candidate["task_id"] == parent_id
    assert candidate["dependency_states"] == {}
    assert candidate["child_task_states"] == {child_id: "verified"}


def test_lifecycle_reconcile_cli_accepts_task_selector():
    for command in ("lifecycle-reconcile", "lifecycle-reconcile-apply"):
        args = bureau_cli.parser().parse_args(
            [command, "--task-id", "BUR-TEST-001-T001"]
        )
        assert args.task_id == "BUR-TEST-001-T001"


def test_lifecycle_reconcile_cli_accepts_legacy_verified_disposition_selector():
    preview = bureau_cli.parser().parse_args(
        [
            "lifecycle-reconcile",
            "--legacy-verified-task-id",
            "BUR-TEST-001-T001",
            "--expected-task-sha256",
            "a" * 64,
            "--reason",
            "legacy evidence cannot be authenticated under the current contract",
            "--evidence-ref",
            "receipt:legacy",
        ]
    )
    assert preview.legacy_verified_task_id == "BUR-TEST-001-T001"
    assert preview.task_id is None
    applied = bureau_cli.parser().parse_args(
        [
            "lifecycle-reconcile-apply",
            "--legacy-verified-task-id",
            "BUR-TEST-001-T001",
            "--expected-task-sha256",
            "a" * 64,
            "--reason",
            "legacy evidence cannot be authenticated under the current contract",
            "--evidence-ref",
            "receipt:legacy",
            "--expected-preview-sha256",
            "b" * 64,
        ]
    )
    assert applied.expected_preview_sha256 == "b" * 64


def test_legacy_verified_disposition_is_append_only_idempotent_and_diagnostic_only(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    task_id = next(iter(registry.tasks))
    current = store.task_spec(task_id)
    assert current is not None
    task_spec = json.loads(json.dumps(current["spec"]))
    task_spec["state"] = "verified"
    store.put_task_spec(
        task_spec,
        idempotency_key="legacy-disposition-verified",
        expected_revision=current["revision"],
        source="test",
    )
    operational, _, _ = bureau_v2.authoritative_task_registry(registry, store)
    task = operational.tasks[task_id]

    preview = bureau_v2.legacy_verified_disposition(
        registry,
        store,
        task_id=task.id,
        expected_task_sha256=task.sha256,
        reason="legacy evidence cannot be authenticated under the current contract",
        evidence_ref="receipt:legacy-task",
    )
    assert preview["status"] == "ready-to-apply"
    applied = bureau_v2.legacy_verified_disposition(
        registry,
        store,
        task_id=task.id,
        expected_task_sha256=task.sha256,
        reason="legacy evidence cannot be authenticated under the current contract",
        evidence_ref="receipt:legacy-task",
        apply=True,
        expected_preview_sha256=preview["preview_sha256"],
    )
    assert applied["status"] == "applied"
    assert applied["effect_started"] is True

    replay_preview = bureau_v2.legacy_verified_disposition(
        registry,
        store,
        task_id=task.id,
        expected_task_sha256=task.sha256,
        reason="legacy evidence cannot be authenticated under the current contract",
        evidence_ref="receipt:legacy-task",
    )
    assert replay_preview["idempotent"] is True
    replay = bureau_v2.legacy_verified_disposition(
        registry,
        store,
        task_id=task.id,
        expected_task_sha256=task.sha256,
        reason="legacy evidence cannot be authenticated under the current contract",
        evidence_ref="receipt:legacy-task",
        apply=True,
        expected_preview_sha256=replay_preview["preview_sha256"],
    )
    assert replay["status"] == "already-dispositioned"
    assert replay["effect_started"] is False

    with pytest.raises(StateError, match="conflicts with existing current binding"):
        bureau_v2.legacy_verified_disposition(
            registry,
            store,
            task_id=task.id,
            expected_task_sha256=task.sha256,
            reason="different reason",
            evidence_ref="receipt:legacy-task",
        )

    lifecycle = lifecycle_diagnostics(registry, store)[0]
    assert lifecycle["legacy_dispositioned_verified_task_ids"] == [task.id]
    assert lifecycle["unverified_verified_task_ids"] == []
    assert lifecycle["recommended_state"] == "active"
    assert lifecycle["consistent"] is True
    with pytest.raises(StateError, match="has no current verification"):
        verification_stamp(operational, store, task.id)
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type=?",
            (bureau_v2.state_events.LEGACY_VERIFIED_DISPOSITION_EVENT_TYPE,),
        ).fetchone()[0] == 1


def test_legacy_verified_disposition_goes_stale_and_real_stamp_wins(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    task_id = next(iter(registry.tasks))
    initial = store.task_spec(task_id)
    assert initial is not None
    initial_spec = json.loads(json.dumps(initial["spec"]))
    initial_spec["state"] = "verified"
    store.put_task_spec(
        initial_spec,
        idempotency_key="legacy-disposition-stale-verified",
        expected_revision=initial["revision"],
        source="test",
    )
    operational, _, _ = bureau_v2.authoritative_task_registry(registry, store)
    task = operational.tasks[task_id]
    preview = bureau_v2.legacy_verified_disposition(
        registry,
        store,
        task_id=task.id,
        expected_task_sha256=task.sha256,
        reason="legacy evidence only",
        evidence_ref="receipt:old",
    )
    bureau_v2.legacy_verified_disposition(
        registry,
        store,
        task_id=task.id,
        expected_task_sha256=task.sha256,
        reason="legacy evidence only",
        evidence_ref="receipt:old",
        apply=True,
        expected_preview_sha256=preview["preview_sha256"],
    )

    current = store.task_spec(task.id)
    assert current is not None
    revised = json.loads(json.dumps(current["spec"]))
    revised["title"] += " revised"
    store.put_task_spec(
        revised,
        idempotency_key="legacy-disposition-hash-drift",
        expected_revision=current["revision"],
        source="test",
    )
    drifted_operational, _, _ = bureau_v2.authoritative_task_registry(registry, store)
    drifted_task = drifted_operational.tasks[task.id]
    assert drifted_task.sha256 != task.sha256
    lifecycle = lifecycle_diagnostics(registry, store)[0]
    assert lifecycle["legacy_dispositioned_verified_task_ids"] == []
    assert lifecycle["unverified_verified_task_ids"] == [task.id]
    assert lifecycle["legacy_disposition_historical_statuses"] == {
        task.id: ["stale_task_sha256"]
    }

    current = store.task_spec(task.id)
    assert current is not None
    stamped = json.loads(json.dumps(current["spec"]))
    stamped.setdefault("metadata", {})["verification"] = {
        "task_sha256": drifted_task.sha256,
        "plan_sha256": plan_sha256(drifted_operational, drifted_task.initiative),
        "receipt_sha256": "c" * 64,
    }
    store.put_task_spec(
        stamped,
        idempotency_key="legacy-disposition-real-stamp",
        expected_revision=current["revision"],
        source="test",
    )
    stamped_operational, _, _ = bureau_v2.authoritative_task_registry(registry, store)
    stamp = verification_stamp(stamped_operational, store, task.id)
    assert stamp["receipt_sha256"] == "c" * 64
    lifecycle = lifecycle_diagnostics(registry, store)[0]
    assert lifecycle["legacy_dispositioned_verified_task_ids"] == []
    assert lifecycle["unverified_verified_task_ids"] == []
    assert lifecycle["recommended_state"] == "completion-ready"
    assert lifecycle["legacy_disposition_historical_statuses"] == {
        task.id: ["stale_task_sha256"]
    }
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type=?",
            (bureau_v2.state_events.LEGACY_VERIFIED_DISPOSITION_EVENT_TYPE,),
        ).fetchone()[0] == 1


def test_legacy_verified_disposition_does_not_satisfy_dependency_readiness(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(2)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    dependency_id, child_id = sorted(registry.tasks)

    dependency = store.task_spec(dependency_id)
    child = store.task_spec(child_id)
    assert dependency is not None and child is not None
    dependency_spec = json.loads(json.dumps(dependency["spec"]))
    dependency_spec["state"] = "verified"
    store.put_task_spec(
        dependency_spec,
        idempotency_key="legacy-dependency-verified",
        expected_revision=dependency["revision"],
        source="test",
    )
    child_spec = json.loads(json.dumps(child["spec"]))
    child_spec["state"] = "planned"
    child_spec["depends_on"] = [dependency_id]
    store.put_task_spec(
        child_spec,
        idempotency_key="legacy-child-planned",
        expected_revision=child["revision"],
        source="test",
    )
    operational, _, _ = bureau_v2.authoritative_task_registry(registry, store)
    dependency_task = operational.tasks[dependency_id]
    preview = bureau_v2.legacy_verified_disposition(
        registry,
        store,
        task_id=dependency_id,
        expected_task_sha256=dependency_task.sha256,
        reason="legacy dependency evidence only",
        evidence_ref="receipt:legacy-dependency",
    )
    bureau_v2.legacy_verified_disposition(
        registry,
        store,
        task_id=dependency_id,
        expected_task_sha256=dependency_task.sha256,
        reason="legacy dependency evidence only",
        evidence_ref="receipt:legacy-dependency",
        apply=True,
        expected_preview_sha256=preview["preview_sha256"],
    )

    with pytest.raises(
        StateError, match="has no deterministic lifecycle reconcile candidate"
    ):
        bureau_v2.reconcile_initiative_lifecycle(
            registry, store, task_id=child_id
        )
    lifecycle = lifecycle_diagnostics(registry, store)[0]
    assert dependency_id in lifecycle["legacy_dispositioned_verified_task_ids"]


def test_lifecycle_reconcile_cli_accepts_initiative_selector_and_rejects_combination():
    for command in ("lifecycle-reconcile", "lifecycle-reconcile-apply"):
        args = bureau_cli.parser().parse_args(
            [command, "--initiative-id", "BUR-TEST-001"]
        )
        assert args.initiative_id == "BUR-TEST-001"
        assert args.task_id is None
        with pytest.raises(SystemExit):
            bureau_cli.parser().parse_args(
                [
                    command,
                    "--task-id",
                    "BUR-TEST-001-T001",
                    "--initiative-id",
                    "BUR-TEST-001",
                ]
            )


def test_lifecycle_reconcile_initiative_selector_limits_preview_and_apply(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(3)
    task_paths = {
        json.loads(path.read_text())["id"]: path
        for path in (root / "registry/tasks").glob("*.json")
    }
    blocked_id, dependency_id, planned_id = sorted(task_paths)
    blocked = json.loads(task_paths[blocked_id].read_text())
    blocked["state"] = "blocked"
    task_paths[blocked_id].write_text(json.dumps(blocked))
    remove_from_queue(root, blocked_id)

    secondary_initiative = json.loads(
        (root / "registry/initiatives/main.json").read_text()
    )
    secondary_initiative["id"] = "BUR-TEST-002"
    secondary_initiative["title"] = "Unrelated lifecycle task candidates"
    (root / "registry/initiatives/secondary.json").write_text(
        json.dumps(secondary_initiative)
    )
    for unrelated_id in (dependency_id, planned_id):
        unrelated = json.loads(task_paths[unrelated_id].read_text())
        unrelated["initiative"] = "BUR-TEST-002"
        task_paths[unrelated_id].write_text(json.dumps(unrelated))

    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)

    dependency = store.task_spec(dependency_id)
    assert dependency is not None
    dependency_spec = json.loads(json.dumps(dependency["spec"]))
    dependency_spec["state"] = "verified"
    store.put_task_spec(
        dependency_spec,
        idempotency_key="initiative-scope-dependency-verified",
        expected_revision=dependency["revision"],
        source="test",
    )

    planned = store.task_spec(planned_id)
    assert planned is not None
    planned_spec = json.loads(json.dumps(planned["spec"]))
    planned_spec["state"] = "planned"
    planned_spec["depends_on"] = [dependency_id]
    store.put_task_spec(
        planned_spec,
        idempotency_key="initiative-scope-dependent-planned",
        expected_revision=planned["revision"],
        source="test",
    )

    operational, _, _ = bureau_v2.authoritative_task_registry(registry, store)
    current_dependency = store.task_spec(dependency_id)
    assert current_dependency is not None
    dependency_spec = json.loads(json.dumps(current_dependency["spec"]))
    dependency_task = operational.tasks[dependency_id]
    dependency_spec.setdefault("metadata", {})["verification"] = {
        "task_sha256": dependency_task.sha256,
        "plan_sha256": plan_sha256(operational, dependency_task.initiative),
    }
    store.put_task_spec(
        dependency_spec,
        idempotency_key="initiative-scope-dependency-evidence",
        expected_revision=current_dependency["revision"],
        source="test",
    )

    broad = bureau_v2.reconcile_initiative_lifecycle(registry, store)
    assert broad["task_candidate_count"] == 1
    assert broad["task_candidates"][0]["task_id"] == planned_id
    assert broad["candidate_count"] == 1

    scoped = bureau_v2.reconcile_initiative_lifecycle(
        registry, store, initiative_id="BUR-TEST-001"
    )
    assert scoped["scope"] == "initiative"
    assert scoped["initiative_selector"] == "BUR-TEST-001"
    assert scoped["task_candidate_count"] == 0
    assert scoped["task_candidates"] == []
    assert scoped["candidate_count"] == 1
    assert scoped["candidates"][0]["initiative_id"] == "BUR-TEST-001"
    assert scoped["candidates"][0]["from_state"] == "active"
    assert scoped["candidates"][0]["to_state"] == "waiting"

    task_specs_before = {
        task_id: store.task_spec(task_id) for task_id in sorted(registry.tasks)
    }
    applied = bureau_v2.reconcile_initiative_lifecycle(
        registry, store, apply=True, initiative_id="BUR-TEST-001"
    )
    assert applied["scope"] == "initiative"
    assert applied["initiative_selector"] == "BUR-TEST-001"
    assert applied["changed_task_count"] == 0
    assert applied["changed_tasks"] == []
    assert applied["changed_count"] == 1
    assert applied["changed"][0]["initiative_id"] == "BUR-TEST-001"
    assert applied["total_changed_count"] == 1
    assert {
        task_id: store.task_spec(task_id) for task_id in sorted(registry.tasks)
    } == task_specs_before
    with store.connect() as connection:
        row = connection.execute(
            "SELECT state FROM initiative_status WHERE initiative_id=?",
            ("BUR-TEST-001",),
        ).fetchone()
    assert row is not None
    assert row["state"] == "waiting"


def test_lifecycle_reconcile_initiative_selector_filters_excluded_recommendations(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)

    diagnostics = [
        {
            "initiative_id": "BUR-TEST-001",
            "declared_state": "waiting",
            "recommended_state": "active",
            "task_states": {},
            "unverified_verified_task_ids": [],
            "consistent": False,
        },
        {
            "initiative_id": "BUR-OTHER",
            "declared_state": "completed",
            "recommended_state": "reopen-required",
            "task_states": {},
            "unverified_verified_task_ids": [],
            "consistent": False,
        },
    ]
    monkeypatch.setattr(
        bureau_v2,
        "_lifecycle_diagnostics_from_overlays",
        lambda *args, **kwargs: diagnostics,
    )

    broad = bureau_v2.reconcile_initiative_lifecycle(registry, store)
    scoped = bureau_v2.reconcile_initiative_lifecycle(
        registry, store, initiative_id="BUR-TEST-001"
    )

    assert broad["excluded_recommendations"] == ["reopen-required"]
    assert scoped["candidate_count"] == 1
    assert scoped["candidates"][0]["initiative_id"] == "BUR-TEST-001"
    assert scoped["excluded_recommendations"] == []


def test_lifecycle_reconcile_initiative_selector_rejects_unknown_and_combined_scope(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    task_id = next(iter(registry.tasks))
    before = store.task_spec(task_id)

    with pytest.raises(StateError, match="unknown initiative BUR-TEST-999"):
        bureau_v2.reconcile_initiative_lifecycle(
            registry, store, initiative_id="BUR-TEST-999"
        )
    with pytest.raises(StateError, match="task_id and initiative_id are mutually exclusive"):
        bureau_v2.reconcile_initiative_lifecycle(
            registry,
            store,
            task_id=task_id,
            initiative_id="BUR-TEST-001",
        )

    assert store.task_spec(task_id) == before


def test_lifecycle_reconcile_initiative_selector_rechecks_candidate_inside_apply(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    task_path = next((root / "registry/tasks").glob("*.json"))
    task = json.loads(task_path.read_text())
    task["state"] = "blocked"
    remove_from_queue(root, task["id"])
    task_path.write_text(json.dumps(task))
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    overlays = iter([{task["id"]: "blocked"}, {task["id"]: "ready"}])
    monkeypatch.setattr(store, "overlays", lambda connection, selected: next(overlays))

    with pytest.raises(StateError, match="initiative lifecycle inputs changed during reconcile"):
        bureau_v2.reconcile_initiative_lifecycle(
            registry, store, apply=True, initiative_id="BUR-TEST-001"
        )

    with store.connect() as connection:
        row = connection.execute(
            "SELECT state FROM initiative_status WHERE initiative_id=?",
            ("BUR-TEST-001",),
        ).fetchone()
    assert row is None


def test_lifecycle_reconcile_task_selector_limits_preview_and_apply(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(3)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    queue_path = root / "registry/queue.json"
    queue_before = queue_path.read_bytes()
    task_files_before = {
        path.name: path.read_bytes()
        for path in (root / "registry/tasks").glob("*.json")
    }
    dependency_id, selected_id, other_id = sorted(registry.tasks)

    dependency = store.task_spec(dependency_id)
    assert dependency is not None
    dependency_spec = json.loads(json.dumps(dependency["spec"]))
    dependency_spec["state"] = "verified"
    store.put_task_spec(
        dependency_spec,
        idempotency_key="scoped-lifecycle-dependency-verified",
        expected_revision=dependency["revision"],
        source="test",
    )

    for candidate_id in (selected_id, other_id):
        current = store.task_spec(candidate_id)
        assert current is not None
        spec = json.loads(json.dumps(current["spec"]))
        spec["state"] = "planned"
        spec["depends_on"] = [dependency_id]
        store.put_task_spec(
            spec,
            idempotency_key=f"scoped-lifecycle-{candidate_id}-planned",
            expected_revision=current["revision"],
            source="test",
        )

    operational, _, _ = bureau_v2.authoritative_task_registry(registry, store)
    current_dependency = store.task_spec(dependency_id)
    assert current_dependency is not None
    dependency_spec = json.loads(json.dumps(current_dependency["spec"]))
    dependency_task = operational.tasks[dependency_id]
    dependency_spec.setdefault("metadata", {})["verification"] = {
        "task_sha256": dependency_task.sha256,
        "plan_sha256": plan_sha256(operational, dependency_task.initiative),
    }
    store.put_task_spec(
        dependency_spec,
        idempotency_key="scoped-lifecycle-dependency-evidence",
        expected_revision=current_dependency["revision"],
        source="test",
    )

    broad = bureau_v2.reconcile_initiative_lifecycle(registry, store)
    assert "scope" not in broad
    assert "task_selector" not in broad
    assert broad["task_candidate_count"] == 2
    assert {candidate["task_id"] for candidate in broad["task_candidates"]} == {
        selected_id,
        other_id,
    }

    scoped = bureau_v2.reconcile_initiative_lifecycle(
        registry, store, task_id=selected_id
    )
    assert scoped["scope"] == "task"
    assert scoped["task_selector"] == selected_id
    assert scoped["task_candidate_count"] == 1
    assert scoped["task_candidates"][0]["task_id"] == selected_id
    assert scoped["candidate_count"] == 0

    other_before = store.task_spec(other_id)
    assert other_before is not None
    applied = bureau_v2.reconcile_initiative_lifecycle(
        registry, store, apply=True, task_id=selected_id
    )
    assert applied["changed_task_count"] == 1
    assert applied["changed_tasks"][0]["task_id"] == selected_id
    assert applied["changed_count"] == 0
    assert applied["total_changed_count"] == 1
    selected_after = store.task_spec(selected_id)
    other_after = store.task_spec(other_id)
    assert selected_after is not None
    assert other_after is not None
    assert selected_after["spec"]["state"] == "ready"
    assert other_after["spec"]["state"] == "planned"
    assert other_after["revision"] == other_before["revision"]
    assert queue_path.read_bytes() == queue_before
    assert {
        path.name: path.read_bytes()
        for path in (root / "registry/tasks").glob("*.json")
    } == task_files_before
    assert store.list_runs() == []
    with store.connect() as connection:
        row = connection.execute(
            "SELECT state FROM initiative_status WHERE initiative_id=?",
            ("BUR-TEST-001",),
        ).fetchone()
    assert row is None


def test_lifecycle_reconcile_task_selector_rejects_unknown_and_non_candidate(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    task_id = next(iter(registry.tasks))
    before = store.task_spec(task_id)
    assert before is not None

    with pytest.raises(StateError, match="unknown task BUR-TEST-001-T999"):
        bureau_v2.reconcile_initiative_lifecycle(
            registry, store, task_id="BUR-TEST-001-T999"
        )
    with pytest.raises(
        StateError, match=f"task {task_id} has no deterministic lifecycle reconcile candidate"
    ):
        bureau_v2.reconcile_initiative_lifecycle(registry, store, task_id=task_id)

    assert store.task_spec(task_id) == before
    assert store.list_runs() == []
    with store.connect() as connection:
        row = connection.execute(
            "SELECT state FROM initiative_status WHERE initiative_id=?",
            ("BUR-TEST-001",),
        ).fetchone()
    assert row is None


def test_lifecycle_reconcile_task_selector_rejects_candidate_drift(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    task_id = next(iter(registry.tasks))
    current = store.task_spec(task_id)
    assert current is not None
    candidate = {
        "task_id": task_id,
        "from_state": current["spec"]["state"],
        "to_state": "planned",
        "revision": current["revision"],
        "spec_sha256": current["spec_sha256"],
    }
    drifted = {**candidate, "revision": candidate["revision"] + 1}
    generated = iter([[candidate], [drifted]])
    monkeypatch.setattr(
        bureau_v2,
        "_structural_task_reconcile_candidates",
        lambda *args, **kwargs: next(generated),
    )

    with pytest.raises(
        StateError, match="task lifecycle gates changed during lifecycle reconcile"
    ):
        bureau_v2.reconcile_initiative_lifecycle(
            registry, store, apply=True, task_id=task_id
        )

    after = store.task_spec(task_id)
    assert after == current


def test_lifecycle_reconcile_leaves_ungated_planned_task_open(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    task_id = next(iter(registry.tasks))
    current = store.task_spec(task_id)
    assert current is not None
    task_spec = dict(current["spec"])
    task_spec["state"] = "planned"
    task_spec["depends_on"] = []
    store.put_task_spec(
        task_spec,
        idempotency_key="lifecycle-ungated-planned",
        expected_revision=current["revision"],
        source="test",
    )

    preview = bureau_v2.reconcile_initiative_lifecycle(registry, store)
    assert preview["task_candidate_count"] == 0
    assert preview["changed_task_count"] == 0



def test_lifecycle_reconcile_legacy_git_preview_fails_closed_on_apply(
    registry_factory, tmp_path, monkeypatch
):
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
    remove_from_queue(root, task["id"])
    task_path.write_text(json.dumps(task))
    initiative_path = root / "registry/initiatives/main.json"
    initiative_before = initiative_path.read_bytes()
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    _, task_authority, _ = bureau_v2.authoritative_task_registry(registry, store)
    assert task_authority["kind"] == "legacy-git-bootstrap"

    preview = bureau_v2.reconcile_initiative_lifecycle(registry, store)
    assert preview["candidate_count"] == 1
    assert preview["candidates"][0]["from_state"] == "active"
    assert preview["candidates"][0]["to_state"] == "completion-ready"
    assert "completion-ready" not in preview["excluded_recommendations"]

    with pytest.raises(
        StateError, match="lifecycle reconciliation requires StateStore TaskSpec authority"
    ):
        bureau_v2.reconcile_initiative_lifecycle(registry, store, apply=True)
    assert initiative_path.read_bytes() == initiative_before
    with store.connect() as connection:
        row = connection.execute(
            "SELECT state FROM initiative_status WHERE initiative_id=?",
            ("BUR-TEST-001",),
        ).fetchone()
    assert row is None


def test_lifecycle_reconcile_rejects_legacy_git_verified_to_planned_drift(
    registry_factory, tmp_path, monkeypatch
):
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
    remove_from_queue(root, task["id"])
    task_path.write_text(json.dumps(task))
    registry, store, _ = setup(root, tmp_path, monkeypatch)

    preview = bureau_v2.reconcile_initiative_lifecycle(registry, store)
    assert preview["candidates"][0]["to_state"] == "completion-ready"

    original_immediate = store.immediate
    injected = False

    @contextmanager
    def reopen_task_before_reconcile_transaction():
        nonlocal injected
        if not injected:
            injected = True
            reopened = json.loads(task_path.read_text())
            reopened["state"] = "planned"
            reopened.get("metadata", {}).pop("verification", None)
            task_path.write_text(json.dumps(reopened))
        with original_immediate() as connection:
            yield connection

    monkeypatch.setattr(store, "immediate", reopen_task_before_reconcile_transaction)
    with pytest.raises(
        StateError, match="lifecycle reconciliation requires StateStore TaskSpec authority"
    ):
        bureau_v2.reconcile_initiative_lifecycle(registry, store, apply=True)

    with store.connect() as connection:
        row = connection.execute(
            "SELECT state FROM initiative_status WHERE initiative_id=?",
            ("BUR-TEST-001",),
        ).fetchone()
    assert row is None




def test_lifecycle_reconcile_requires_current_evidence_before_completion_ready(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    task_id = next(iter(registry.tasks))
    current = store.task_spec(task_id)
    assert current is not None
    task_spec = json.loads(json.dumps(current["spec"]))
    task_spec["state"] = "verified"
    metadata = dict(task_spec.get("metadata", {}))
    metadata.pop("verification", None)
    task_spec["metadata"] = metadata
    store.put_task_spec(
        task_spec,
        idempotency_key="lifecycle-unbound-verified-no-completion",
        expected_revision=current["revision"],
        source="test",
    )

    preview = bureau_v2.reconcile_initiative_lifecycle(registry, store)
    assert preview["candidate_count"] == 0
    lifecycle = lifecycle_diagnostics(registry, store)[0]
    assert lifecycle["recommended_state"] == "active"
    assert lifecycle["unverified_verified_task_ids"] == [task_id]


def test_close_ready_rejects_unverified_state_store_task(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    task_id = next(iter(registry.tasks))
    current = store.task_spec(task_id)
    assert current is not None
    task_spec = json.loads(json.dumps(current["spec"]))
    task_spec["state"] = "verified"
    store.put_task_spec(
        task_spec,
        idempotency_key="close-unverified-task",
        expected_revision=current["revision"],
        source="test",
    )
    store.set_initiative_state("BUR-TEST-001", "completion-ready")
    initiative_path = root / "registry/initiatives/main.json"
    initiative_before = initiative_path.read_bytes()

    assert close_ready_initiatives(registry, store) == []
    assert initiative_path.read_bytes() == initiative_before
    with store.connect() as connection:
        row = connection.execute(
            "SELECT state FROM initiative_status WHERE initiative_id=?",
            ("BUR-TEST-001",),
        ).fetchone()
    assert row is not None
    assert row["state"] == "completion-ready"





def test_close_ready_revalidates_tasks_inside_completion_transaction(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    task_id = next(iter(registry.tasks))

    current = store.task_spec(task_id)
    assert current is not None
    task_spec = json.loads(json.dumps(current["spec"]))
    task_spec["state"] = "verified"
    store.put_task_spec(
        task_spec,
        idempotency_key="close-race-verified-state",
        expected_revision=current["revision"],
        source="test",
    )

    operational, _, _ = bureau_v2.authoritative_task_registry(registry, store)
    current = store.task_spec(task_id)
    assert current is not None
    task_spec = json.loads(json.dumps(current["spec"]))
    task = operational.tasks[task_id]
    task_spec.setdefault("metadata", {})["verification"] = {
        "task_sha256": task.sha256,
        "plan_sha256": plan_sha256(operational, task.initiative),
    }
    store.put_task_spec(
        task_spec,
        idempotency_key="close-race-verification-evidence",
        expected_revision=current["revision"],
        source="test",
    )

    reconciled = bureau_v2.reconcile_initiative_lifecycle(registry, store, apply=True)
    assert reconciled["changed"][0]["to_state"] == "completion-ready"
    initiative_path = root / "registry/initiatives/main.json"
    initiative_before = initiative_path.read_bytes()

    original_immediate = store.immediate
    injected = False

    @contextmanager
    def mutate_task_before_close_transaction():
        nonlocal injected
        if not injected:
            injected = True
            with original_immediate() as race_connection:
                current_spec = bureau_v2.task_specs.get_current(race_connection, task_id)
                assert current_spec is not None
                mutated = json.loads(json.dumps(current_spec["spec"]))
                mutated["state"] = "planned"
                metadata = dict(mutated.get("metadata", {}))
                metadata.pop("verification", None)
                mutated["metadata"] = metadata
                bureau_v2.task_specs.put(
                    race_connection,
                    mutated,
                    idempotency_key="close-race-task-reopened",
                    expected_revision=int(current_spec["revision"]),
                    source="test",
                )
        with original_immediate() as connection:
            yield connection

    monkeypatch.setattr(store, "immediate", mutate_task_before_close_transaction)
    with pytest.raises(StateError, match="TaskSpec projection changed during explicit closure"):
        close_ready_initiatives(registry, store)

    assert initiative_path.read_bytes() == initiative_before
    with store.connect() as connection:
        row = connection.execute(
            "SELECT state FROM initiative_status WHERE initiative_id=?",
            ("BUR-TEST-001",),
        ).fetchone()
    assert row is not None
    assert row["state"] == "completion-ready"


def test_close_ready_rejects_legacy_git_verified_to_planned_drift(
    registry_factory, tmp_path, monkeypatch
):
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
    remove_from_queue(root, task["id"])
    task_path.write_text(json.dumps(task))
    registry, store, _ = setup(root, tmp_path, monkeypatch)

    store.set_initiative_state("BUR-TEST-001", "completion-ready")
    initiative_path = root / "registry/initiatives/main.json"
    initiative_before = initiative_path.read_bytes()

    original_immediate = store.immediate
    injected = False

    @contextmanager
    def reopen_git_task_before_close_transaction():
        nonlocal injected
        if not injected:
            injected = True
            reopened = json.loads(task_path.read_text())
            reopened["state"] = "planned"
            reopened.get("metadata", {}).pop("verification", None)
            task_path.write_text(json.dumps(reopened))
        with original_immediate() as connection:
            yield connection

    monkeypatch.setattr(store, "immediate", reopen_git_task_before_close_transaction)
    with pytest.raises(StateError, match="closure requires StateStore TaskSpec authority"):
        close_ready_initiatives(registry, store)

    assert initiative_path.read_bytes() == initiative_before
    with store.connect() as connection:
        row = connection.execute(
            "SELECT state FROM initiative_status WHERE initiative_id=?",
            ("BUR-TEST-001",),
        ).fetchone()
    assert row is not None
    assert row["state"] == "completion-ready"


def test_completed_lifecycle_accepts_mixed_terminal_task_states(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(3)
    preliminary = Registry.load(root)
    initiative_path = root / "registry/initiatives/main.json"
    initiative = json.loads(initiative_path.read_text())
    initiative["state"] = "completed"
    initiative["commitment"] = "completed"
    initiative_path.write_text(json.dumps(initiative))

    terminal_states = ("verified", "cancelled", "superseded")
    for task_path, state in zip(
        sorted((root / "registry/tasks").glob("*.json")), terminal_states, strict=True
    ):
        task = json.loads(task_path.read_text())
        task["state"] = state
        if state == "verified":
            task["metadata"] = {
                "verification": {
                    "task_sha256": task_revision_sha256(task),
                    "plan_sha256": plan_sha256(preliminary, task["initiative"]),
                }
            }
        remove_from_queue(root, task["id"])
        task_path.write_text(json.dumps(task))

    registry, store, _ = setup(root, tmp_path, monkeypatch)
    lifecycle = lifecycle_diagnostics(registry, store)[0]

    assert lifecycle["recommended_state"] == "completed"
    assert lifecycle["consistent"] is True


def test_read_only_lifecycle_accepts_mixed_terminal_task_states(registry_factory):
    root = registry_factory(3)
    initiative_path = root / "registry/initiatives/main.json"
    initiative = json.loads(initiative_path.read_text())
    initiative["state"] = "completed"
    initiative["commitment"] = "completed"
    initiative_path.write_text(json.dumps(initiative))
    registry = Registry.load(root)
    task_ids = sorted(registry.tasks)
    overlays = dict(
        zip(task_ids, ("verified", "cancelled", "superseded"), strict=True)
    )

    lifecycle = bureau_v2._read_only_lifecycle(registry, overlays)[0]

    assert lifecycle["recommended_state"] == "completed"
    assert lifecycle["consistent"] is True


def test_completed_lifecycle_still_reopens_for_open_task(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(2)
    initiative_path = root / "registry/initiatives/main.json"
    initiative = json.loads(initiative_path.read_text())
    initiative["state"] = "completed"
    initiative["commitment"] = "completed"
    initiative_path.write_text(json.dumps(initiative))
    task_paths = sorted((root / "registry/tasks").glob("*.json"))
    first = json.loads(task_paths[0].read_text())
    first["state"] = "superseded"
    remove_from_queue(root, first["id"])
    task_paths[0].write_text(json.dumps(first))
    second = json.loads(task_paths[1].read_text())
    second["state"] = "planned"
    remove_from_queue(root, second["id"])
    task_paths[1].write_text(json.dumps(second))

    registry, store, _ = setup(root, tmp_path, monkeypatch)
    lifecycle = lifecycle_diagnostics(registry, store)[0]

    assert lifecycle["recommended_state"] == "reopen-required"
    assert lifecycle["consistent"] is False


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
    remove_from_queue(root, task["id"])
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


def test_cli_heartbeat_allows_only_queue_projection_release_drift(
    registry_factory, tmp_path, monkeypatch, capsys
) -> None:
    from bureau import runtime_identity as runtime_identity_module

    root = registry_factory(1)
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'bureau-fixture'\nversion = '0'\n", encoding="utf-8"
    )
    (root / "src/bureau").mkdir(parents=True)
    deployed_commit = init_clean_origin_main(root)
    git_output(root, "remote", "add", "origin", "git@github.com:heimgewebe/bureau.git")
    _registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    run = dispatcher.claim_next("worker-a", ("repository",))["run"]
    with store.immediate() as connection:
        connection.execute(
            "UPDATE runs SET heartbeat_at='2000-01-01T00:00:00Z' WHERE run_id=?",
            (run["run_id"],),
        )

    module = tmp_path / "release/src/bureau/runtime_identity.py"
    module.parent.mkdir(parents=True)
    module.write_text("# immutable release\n", encoding="utf-8")
    monkeypatch.setattr(
        runtime_identity_module,
        "_manifest_identity",
        lambda _module: {
            "available": True,
            "valid": True,
            "source_commit": deployed_commit,
            "canonical_registry": {},
        },
    )

    queue_path = root / "registry/queue.json"
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    queue_path.write_text(
        json.dumps(queue, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    git_output(root, "add", "registry/queue.json")
    git_output(root, "commit", "-m", "advance legacy queue projection")
    queue_head = git_output(root, "rev-parse", "HEAD")
    git_output(root, "update-ref", "refs/remotes/origin/main", queue_head)
    queue_identity = runtime_identity_module.bureau_runtime_identity(
        root, state_path=store.path, module_path=module
    )
    assert queue_identity["compatibility"]["mutation_allowed"] is True
    assert queue_identity["claim_root"]["status"] == "local-preflight-clear"
    monkeypatch.setattr(
        bureau_cli,
        "bureau_runtime_identity",
        lambda *args, **kwargs: json.loads(json.dumps(queue_identity)),
    )

    result = bureau_cli.main(
        [
            "--root",
            str(root),
            "--state-root",
            str(store.state_root),
            "--json",
            "heartbeat",
            "--worker",
            "worker-a",
            run["run_id"],
        ]
    )
    capsys.readouterr()
    assert result == 0
    assert store.run(run["run_id"])["heartbeat_at"] != "2000-01-01T00:00:00Z"

    with store.immediate() as connection:
        connection.execute(
            "UPDATE runs SET heartbeat_at='2000-01-01T00:00:00Z' WHERE run_id=?",
            (run["run_id"],),
        )
    task_path = root / "registry/tasks/BUR-TEST-001-T001.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task["title"] = "authoritative drift"
    task_path.write_text(json.dumps(task), encoding="utf-8")
    git_output(root, "add", "registry/tasks/BUR-TEST-001-T001.json")
    git_output(root, "commit", "-m", "advance authoritative task spec")
    task_head = git_output(root, "rev-parse", "HEAD")
    git_output(root, "update-ref", "refs/remotes/origin/main", task_head)
    blocked_identity = runtime_identity_module.bureau_runtime_identity(
        root, state_path=store.path, module_path=module
    )
    assert blocked_identity["compatibility"]["mutation_allowed"] is False
    monkeypatch.setattr(
        bureau_cli,
        "bureau_runtime_identity",
        lambda *args, **kwargs: json.loads(json.dumps(blocked_identity)),
    )

    blocked = bureau_cli.main(
        [
            "--root",
            str(root),
            "--state-root",
            str(store.state_root),
            "--json",
            "heartbeat",
            "--worker",
            "worker-a",
            run["run_id"],
        ]
    )
    blocked_output = json.loads(capsys.readouterr().out)
    blocked_result = blocked_output.get("result", blocked_output)
    assert blocked == 2
    assert blocked_result["status"] == "stale-runtime-blocked"
    assert blocked_result["reason_codes"] == ["release-registry-identity-mismatch"]
    assert store.run(run["run_id"])["heartbeat_at"] == "2000-01-01T00:00:00Z"


def test_heartbeat_reduces_successful_full_run_reads_by_half(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    state = tmp_path / "state"
    monkeypatch.setenv("BUREAU_STATE_DIR", str(state))
    registry = Registry.load(root)
    store = TracingStateStore(state / "bureau.sqlite3")
    dispatcher = Dispatcher(registry, store)
    run = dispatcher.claim_next("worker-a", ("repository",))["run"]

    def legacy_heartbeat() -> dict:
        now = bureau_v2.legacy.utc_now()
        with store.immediate() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id=?", (run["run_id"],)
            ).fetchone()
            assert row is not None
            connection.execute(
                "UPDATE runs SET heartbeat_at=?,updated_at=? WHERE run_id=?",
                (now, now, run["run_id"]),
            )
            connection.execute(
                "UPDATE workers SET heartbeat_at=? WHERE worker_id=?",
                (now, row["worker_id"]),
            )
            store.event(connection, "run-heartbeat", {}, run["run_id"])
        return store.run(run["run_id"])

    store.statements.clear()
    legacy_heartbeat()
    legacy_full_reads = sum(
        statement.lstrip().upper().startswith("SELECT * FROM RUNS")
        for statement in store.statements
    )

    store.statements.clear()
    refreshed = store.heartbeat(run["run_id"], "worker-a")
    cas_full_reads = sum(
        statement.lstrip().upper().startswith("SELECT * FROM RUNS")
        for statement in store.statements
    )

    assert refreshed["worker_id"] == "worker-a"
    assert legacy_full_reads == 2
    assert cas_full_reads == 1
    assert not any(
        "SELECT STATE,WORKER_ID FROM RUNS" in statement.upper()
        for statement in store.statements
    )


def test_heartbeat_without_expected_worker_preserves_existing_owner_semantics(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    _, store, dispatcher = setup(root, tmp_path, monkeypatch)
    run = dispatcher.claim_next("worker-a", ("repository",))["run"]
    with store.immediate() as connection:
        connection.execute(
            "UPDATE workers SET heartbeat_at='2000-01-01T00:00:00Z' "
            "WHERE worker_id='worker-a'"
        )

    refreshed = store.heartbeat(run["run_id"])

    with store.connect() as connection:
        worker = connection.execute(
            "SELECT heartbeat_at FROM workers WHERE worker_id='worker-a'"
        ).fetchone()
    assert refreshed["worker_id"] == "worker-a"
    assert worker["heartbeat_at"] != "2000-01-01T00:00:00Z"


def test_heartbeat_rejections_have_zero_effects(registry_factory, tmp_path, monkeypatch):
    root = registry_factory(1)
    _, store, dispatcher = setup(root, tmp_path, monkeypatch)
    run = dispatcher.claim_next("worker-a", ("repository",))["run"]

    def effects() -> tuple[str, str, int]:
        with store.connect() as connection:
            run_row = connection.execute(
                "SELECT heartbeat_at FROM runs WHERE run_id=?", (run["run_id"],)
            ).fetchone()
            worker_row = connection.execute(
                "SELECT heartbeat_at FROM workers WHERE worker_id='worker-a'"
            ).fetchone()
            event_count = connection.execute(
                "SELECT COUNT(*) FROM events "
                "WHERE run_id=? AND event_type='run-heartbeat'",
                (run["run_id"],),
            ).fetchone()[0]
        return run_row["heartbeat_at"], worker_row["heartbeat_at"], event_count

    before_wrong_owner = effects()
    with pytest.raises(StateError, match="does not own"):
        store.heartbeat(run["run_id"], "worker-b")
    assert effects() == before_wrong_owner

    with store.immediate() as connection:
        connection.execute(
            "UPDATE runs SET state='completed' WHERE run_id=?", (run["run_id"],)
        )
    before_terminal = effects()
    with pytest.raises(StateError, match="not active"):
        store.heartbeat(run["run_id"], "worker-a")
    assert effects() == before_terminal

    before_unknown = effects()
    with pytest.raises(StateError, match="not active"):
        store.heartbeat("BUR-RUN-UNKNOWN", "worker-a")
    assert effects() == before_unknown


def test_competing_heartbeat_owners_produce_one_winner(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    _, store, dispatcher = setup(root, tmp_path, monkeypatch)
    run = dispatcher.claim_next("worker-a", ("repository",))["run"]
    barrier = threading.Barrier(2)

    def heartbeat(worker_id: str) -> str:
        barrier.wait()
        try:
            store.heartbeat(run["run_id"], worker_id)
        except StateError as exc:
            return str(exc)
        return "success"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = sorted(
            future.result()
            for future in (
                executor.submit(heartbeat, "worker-a"),
                executor.submit(heartbeat, "worker-b"),
            )
        )

    with store.connect() as connection:
        event_count = connection.execute(
            "SELECT COUNT(*) FROM events "
            "WHERE run_id=? AND event_type='run-heartbeat'",
            (run["run_id"],),
        ).fetchone()[0]
    assert outcomes == ["success", "worker does not own this run"]
    assert event_count == 1


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


def test_checkout_next_records_repository_mutation_approval(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    init_clean_origin_main(root)
    _registry, _store, dispatcher = setup(root, tmp_path, monkeypatch)

    result = dispatcher.checkout_next(
        "worker", ("repository",), base_dir=tmp_path / "worktrees"
    )

    approval = result["approval"]["repository_mutation"]
    assert approval["action_class"] == "repository_mutation"
    assert approval["allowed"] is True
    assert approval["evidence"]["source"] == "checkout-next workspace"
    assert approval["evidence"]["reference"] == result["run"]["run_id"]


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
    repeated = bureau_v2.complete_run(
        changed, store, run["run_id"], {"proof": {"result": "passed"}}
    )
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
    remove_from_queue(root, task["id"])
    task_path.write_text(json.dumps(task))
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    reconciled = bureau_v2.reconcile_initiative_lifecycle(registry, store, apply=True)
    assert reconciled["changed"][0]["to_state"] == "completion-ready"

    changed = close_ready_initiatives(registry, store)
    assert changed[0]["initiative_id"] == "BUR-TEST-001"
    initiative = json.loads((root / "registry/initiatives/main.json").read_text())
    assert initiative["state"] == "completed"
    assert initiative["commitment"] == "completed"
    assert initiative["metadata"]["lifecycle"]["completed_at"].endswith("Z")
    with store.connect() as connection:
        status = connection.execute(
            "SELECT state FROM initiative_status WHERE initiative_id=?",
            ("BUR-TEST-001",),
        ).fetchone()
    assert status is not None
    assert status["state"] == "completed"

    current = Registry.load(root)
    lifecycle = lifecycle_diagnostics(current, store)[0]
    assert lifecycle["declared_state"] == "completed"
    assert lifecycle["registry_state"] == "completed"
    assert lifecycle["consistent"] is True



def test_close_ready_preserves_completed_at_across_state_store_retry(
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
    remove_from_queue(root, task["id"])
    task_path.write_text(json.dumps(task))
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    reconciled = bureau_v2.reconcile_initiative_lifecycle(registry, store, apply=True)
    assert reconciled["changed"][0]["to_state"] == "completion-ready"

    original_set_initiative_state = store.set_initiative_state
    injected = False

    def fail_first_state_store_completion(
        initiative_id: str,
        state: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, object]:
        nonlocal injected
        if not injected and state == "completed":
            injected = True
            raise RuntimeError("injected state-store completion failure")
        return original_set_initiative_state(
            initiative_id, state, connection=connection
        )

    monkeypatch.setattr(store, "set_initiative_state", fail_first_state_store_completion)
    with pytest.raises(RuntimeError, match="injected state-store completion failure"):
        close_ready_initiatives(registry, store)

    initiative_path = root / "registry/initiatives/main.json"
    after_first = json.loads(initiative_path.read_text())
    first_completed_at = after_first["metadata"]["lifecycle"]["completed_at"]
    assert first_completed_at.endswith("Z")

    monkeypatch.setattr(store, "set_initiative_state", original_set_initiative_state)
    retried = close_ready_initiatives(Registry.load(root), store)
    assert retried[0]["initiative_id"] == "BUR-TEST-001"
    after_retry = json.loads(initiative_path.read_text())
    assert after_retry["metadata"]["lifecycle"]["completed_at"] == first_completed_at
    with store.connect() as connection:
        status = connection.execute(
            "SELECT state FROM initiative_status WHERE initiative_id=?",
            ("BUR-TEST-001",),
        ).fetchone()
    assert status is not None
    assert status["state"] == "completed"


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
    remove_from_queue(root, task["id"])
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


def _canonical_runtime_identity(root: Path, commit: str = "a" * 40) -> dict[str, object]:
    return {
        "registry_selection": "canonical-runtime-default",
        "module": {"source_kind": "immutable-release"},
        "compatibility": {
            "status": "canonical-read-only",
            "mutation_allowed": False,
        },
        "manifest": {
            "valid": True,
            "source_commit": commit,
            "canonical_registry": {
                "valid": True,
                "root": str(root),
                "source_commit": commit,
            },
        },
        "registry": {
            "role": "canonical-runtime-snapshot",
            "root": str(root),
            "dirty": False,
            "head": commit,
            "origin_main": commit,
            "head_equals_origin_main": True,
        },
    }


def test_runtime_drift_check_accepts_identity_bound_canonical_snapshot_without_git(
    registry_factory, tmp_path
):
    root = registry_factory(1)
    state = StateStore(tmp_path / "bureau.sqlite3")

    report = runtime_drift_check(
        root,
        state_db=state.path,
        runtime_identity=_canonical_runtime_identity(root),
    )
    codes = {item["code"] for item in report["findings"]}

    assert report["status"] == "ok"
    assert report["checkout"]["available"] is False
    assert report["checkout"]["git_required"] is False
    assert report["checkout"]["role"] == "canonical-runtime-snapshot"
    assert "canonical-snapshot-no-git" in codes
    assert "checkout-not-git" not in codes


def test_dispatcher_claim_uses_identity_bound_canonical_snapshot_without_git(
    registry_factory, tmp_path
):
    root = registry_factory(1)
    registry = Registry.load(root)
    store = StateStore(tmp_path / "canonical-claim.sqlite3")
    dispatcher = Dispatcher(
        registry,
        store,
        enforce_runtime_gate=True,
        runtime_identity=_canonical_runtime_identity(root),
    )

    result = dispatcher.claim_next(
        "canonical-runtime-worker",
        ("repository",),
        reconcile_first=False,
    )
    runtime_truth = result["runtime_truth"]
    codes = {item["code"] for item in runtime_truth["findings"]}

    assert result["status"] == "claimed"
    assert runtime_truth["status"] == "not-applicable"
    assert runtime_truth["runtime_status"] == "ok"
    assert runtime_truth["execution_blocked"] is False
    assert runtime_truth["drift_classification"] == "not-git"
    assert "canonical-snapshot-no-git" in codes
    assert "checkout-not-git" not in codes


def test_cli_passes_runtime_identity_to_dispatcher(
    registry_factory, tmp_path, monkeypatch, capsys
):
    root = registry_factory(1)
    captured: dict[str, object] = {}

    class CapturingDispatcher:
        def __init__(
            self,
            registry,
            store,
            adapters,
            *,
            enforce_runtime_gate,
            runtime_identity,
        ):
            captured["registry_root"] = registry.root
            captured["enforce_runtime_gate"] = enforce_runtime_gate
            captured["runtime_identity"] = runtime_identity

    monkeypatch.setattr(bureau_cli, "Dispatcher", CapturingDispatcher)

    result = bureau_cli.main(
        [
            "--root",
            str(root),
            "--state-db",
            str(tmp_path / "cli-runtime-identity.sqlite3"),
            "--json",
            "status",
        ]
    )
    capsys.readouterr()

    assert result == 0
    assert captured["registry_root"] == root.resolve()
    assert captured["enforce_runtime_gate"] is True
    assert captured["runtime_identity"] == bureau_cli._CLI_RUNTIME_IDENTITY


def test_runtime_drift_check_rejects_non_git_root_when_canonical_identity_is_malformed(
    registry_factory, tmp_path
):
    root = registry_factory(1)
    state = StateStore(tmp_path / "bureau.sqlite3")
    identity = _canonical_runtime_identity(root)
    identity["manifest"]["canonical_registry"]["source_commit"] = "b" * 40

    report = runtime_drift_check(root, state_db=state.path, runtime_identity=identity)
    codes = {item["code"] for item in report["findings"]}

    assert report["status"] == "blocked"
    assert "checkout-not-git" in codes
    assert "canonical-snapshot-no-git" not in codes


def test_runtime_drift_check_uses_authoritative_task_spec_over_git_projection(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    init_clean_origin_main(root)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    task_id = next(iter(registry.tasks))
    current = store.task_spec(task_id)
    assert current is not None
    authoritative = json.loads(json.dumps(current["spec"]))
    authoritative["title"] = "Newer StateStore task contract"
    store.put_task_spec(
        authoritative,
        idempotency_key="runtime-drift-authoritative-task-spec",
        expected_revision=current["revision"],
        source="test",
    )

    dispatcher = Dispatcher(registry, store)
    run = dispatcher.claim_next("worker", ("repository",))["run"]
    complete_run(registry, store, run["run_id"], {"proof": {"result": "passed"}})
    store.set_initiative_state(next(iter(registry.initiatives)), "completion-ready")

    report = runtime_drift_check(root, state_db=store.path)
    codes = {item["code"] for item in report["findings"]}

    assert report["status"] == "ok"
    assert report["receipts"]["stale_tasks"] == []
    assert "receipt-drift" not in codes
    assert "receipt-drift-clear" in codes


def test_runtime_drift_check_blocks_authoritative_task_spec_drift(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    init_clean_origin_main(root)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    task_id = next(iter(registry.tasks))
    dispatcher = Dispatcher(registry, store)
    run = dispatcher.claim_next("worker", ("repository",))["run"]
    complete_run(registry, store, run["run_id"], {"proof": {"result": "passed"}})

    current = store.task_spec(task_id)
    assert current is not None
    drifted = json.loads(json.dumps(current["spec"]))
    drifted["title"] = "Drifted authoritative StateStore task contract"
    store.put_task_spec(
        drifted,
        idempotency_key="runtime-drift-authoritative-task-spec-drift",
        expected_revision=current["revision"],
        source="test",
    )

    report = runtime_drift_check(root, state_db=store.path)
    stale = report["receipts"]["stale_tasks"]

    assert report["status"] == "blocked"
    assert {item["code"] for item in report["findings"]} >= {"receipt-drift"}
    assert stale[0]["task_id"] == task_id
    assert stale[0]["current_task_sha256"] == task_revision_sha256(drifted)
    assert stale[0]["stored_plan_sha256"] == stale[0]["current_plan_sha256"]


def _runtime_closeout_fixture(
    task_id: str,
    *,
    manifest_sha256: str = "f" * 64,
    authority_revision: int = 1,
    authority_spec_sha256: str = "a" * 64,
    acceptance_evidence: dict[str, object] | None = None,
) -> dict[str, object]:
    closeout = {
        "schema_version": 1,
        "kind": "bureau_runtime_refresh_no_run_closeout",
        "status": "verified",
        "task_id": task_id,
        "authority_revision": authority_revision,
        "authority_spec_sha256": authority_spec_sha256,
        "target_sha256": "b" * 64,
        "intent_sha256": "c" * 64,
        "runtime_result_sha256": "d" * 64,
        "source_commit": "e" * 40,
        "manifest_sha256": manifest_sha256,
        "readback_sha256": "1" * 64,
        "lease_binding_sha256": "2" * 64,
        "lease_release_sha256": "3" * 64,
        "closed_at": "2026-08-13T07:00:00Z",
        "does_not_establish": ["future runtime health"],
    }
    if acceptance_evidence is not None:
        closeout["acceptance_evidence"] = acceptance_evidence
    return closeout


def _seal_runtime_registry_snapshot(root: Path, source_commit: str) -> Path:
    prefix = root.parent / f"runtime-{root.name}"
    snapshot_root = prefix / "registry-snapshots" / f"{source_commit[:12]}-fixture"
    shutil.copytree(root, snapshot_root)
    paths = sorted(
        path.relative_to(snapshot_root)
        for path in (snapshot_root / "registry").rglob("*")
        if path.is_file() and not path.is_symlink()
    )
    tree_sha256 = registry_snapshot.snapshot_tree_sha256(snapshot_root, paths)
    assert tree_sha256 is not None
    inventory_path = snapshot_root / ".bureau-runtime-snapshot.json"
    inventory = {
        "schema_version": 1,
        "kind": "bureau_registry_snapshot",
        "source_commit": source_commit,
        "tree_sha256": tree_sha256,
        "paths": [path.as_posix() for path in paths],
    }
    inventory_path.write_text(
        json.dumps(inventory, sort_keys=True) + "\n", encoding="utf-8"
    )
    manifest = {
        "schema_version": 1,
        "kind": "bureau_runtime_deployment",
        "source_commit": source_commit,
        "canonical_registry_root": str(snapshot_root),
        "canonical_registry_inventory_path": str(inventory_path),
        "canonical_registry_inventory_sha256": hashlib.sha256(
            inventory_path.read_bytes()
        ).hexdigest(),
        "canonical_registry_tree_sha256": tree_sha256,
    }
    (prefix / "deployment-manifest.json").write_text(
        json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
    )
    return snapshot_root


def _runtime_registry_manifest_sha256(root: Path) -> str:
    return hashlib.sha256(
        (root.parent.parent / "deployment-manifest.json").read_bytes()
    ).hexdigest()


def _runtime_authority_receipts_fixture(
    task_id: str,
    *,
    authority_revision: int = 1,
    authority_spec_sha256: str = "a" * 64,
) -> dict[str, object]:
    return {
        "target_binding_receipt": {
            "schema_version": 1,
            "kind": "bureau_runtime_refresh_authority_target_binding",
            "task_id": task_id,
            "authority_revision": authority_revision,
            "authority_spec_sha256": authority_spec_sha256,
            "target_sha256": "b" * 64,
            "intent_sha256": "c" * 64,
            "bound_at": "2026-08-13T06:50:00Z",
        },
        "consumption": {
            "schema_version": 1,
            "kind": "bureau_runtime_refresh_authority_consumption",
            "status": "consumed",
            "task_id": task_id,
            "authority_revision": authority_revision,
            "authority_spec_sha256": authority_spec_sha256,
            "target_sha256": "b" * 64,
            "intent_sha256": "c" * 64,
            "result_sha256": "d" * 64,
            "consumed_at": "2026-08-13T06:55:00Z",
        },
    }


def _put_runtime_closeout_fixture_revision(
    store: StateStore,
    task_id: str,
    *,
    manifest_sha256: str,
    typed_acceptance: bool = False,
    available_evidence: list[str] | None = None,
    closeout_acceptance_assertion: str | None = None,
) -> dict[str, object]:
    current = store.task_spec(task_id)
    assert current is not None
    authority_spec = json.loads(json.dumps(current["spec"]))
    authority: dict[str, object] = {}
    if typed_acceptance:
        criterion_id = authority_spec["acceptance"][0]["id"]
        authority["no_run_closeout_acceptance"] = {
            "schema_version": 1,
            "kind": "bureau_runtime_refresh_no_run_acceptance_contract",
            "criteria": {
                criterion_id: {
                    "verifier": "runtime-refresh-no-run-evidence-v1",
                    "required_evidence": ["state-store-integrity"],
                }
            },
        }
    authority_spec.setdefault("metadata", {})["runtime_refresh_authority"] = authority
    staged = store.put_task_spec(
        authority_spec,
        idempotency_key=f"stage-runtime-authority:{task_id}:{current['revision']}",
        expected_revision=current["revision"],
        source="test",
    )
    authority_revision = staged["revision"]
    authority_spec_sha256 = staged["spec_sha256"]

    acceptance_evidence: dict[str, object] | None = None
    if typed_acceptance:
        staged_authority = staged["spec"]["metadata"]["runtime_refresh_authority"]
        contract = bureau_v2.runtime_refresh._validated_no_run_acceptance_contract(
            spec=staged["spec"], authority=staged_authority
        )
        evidence = {
            "schema_version": 1,
            "kind": "bureau_runtime_refresh_no_run_acceptance_evidence",
            "task_id": task_id,
            "task_spec_sha256": authority_spec_sha256,
            "contract_sha256": bureau_v2.runtime_refresh.sha256_bytes(
                bureau_v2.runtime_refresh.canonical_bytes(contract)
            ),
            "criterion_ids": sorted(contract["criteria"]),
            "available_evidence": sorted(
                ["state-store-integrity"]
                if available_evidence is None
                else available_evidence
            ),
            "runtime_result_sha256": "d" * 64,
            "readback_sha256": "1" * 64,
            "lease_release_sha256": "3" * 64,
            "effect_history_sha256": "4" * 64,
            "state_store_root_sha256": "5" * 64,
            "run_evidence_sha256": "6" * 64,
        }
        acceptance_evidence = bureau_v2.runtime_refresh.bind_digest(
            evidence, "evidence_sha256"
        )

    closed = json.loads(json.dumps(staged["spec"]))
    closed["state"] = "verified"
    if closeout_acceptance_assertion is not None:
        closed["acceptance"][0]["assertion"] = closeout_acceptance_assertion
    metadata = closed.setdefault("metadata", {})
    metadata.pop("verification", None)
    runtime_authority = dict(metadata["runtime_refresh_authority"])
    runtime_authority.update(
        _runtime_authority_receipts_fixture(
            task_id,
            authority_revision=authority_revision,
            authority_spec_sha256=authority_spec_sha256,
        )
    )
    metadata["runtime_refresh_authority"] = runtime_authority
    metadata["runtime_closeout"] = _runtime_closeout_fixture(
        task_id,
        manifest_sha256=manifest_sha256,
        authority_revision=authority_revision,
        authority_spec_sha256=authority_spec_sha256,
        acceptance_evidence=acceptance_evidence,
    )
    return store.put_runtime_refresh_no_run_closeout_task_spec(
        closed,
        idempotency_key=f"runtime-refresh-no-run-closeout:{task_id}:{'d' * 64}",
        expected_revision=authority_revision,
    )


def test_runtime_closeout_is_current_verification_only_for_matching_intact_snapshot(
    registry_factory, tmp_path, monkeypatch
):
    source_commit = "e" * 40
    root = _seal_runtime_registry_snapshot(registry_factory(1), source_commit)
    manifest_sha256 = _runtime_registry_manifest_sha256(root)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    task_id = next(iter(registry.tasks))
    _put_runtime_closeout_fixture_revision(
        store, task_id, manifest_sha256=manifest_sha256
    )

    operational = Dispatcher(registry, store).registry
    stamp = verification_stamp(operational, store, task_id)
    assert stamp["kind"] == "bureau_runtime_refresh_snapshot_verification"
    assert stamp["source_commit"] == source_commit
    assert stamp["task_sha256"] == operational.tasks[task_id].sha256
    assert stamp["plan_sha256"] == plan_sha256(operational, operational.tasks[task_id].initiative)
    lifecycle = lifecycle_diagnostics(registry, store)[0]
    assert lifecycle["unverified_verified_task_ids"] == []
    assert lifecycle["recommended_state"] == "completion-ready"

    initiative_path = root / "registry/initiatives/main.json"
    initiative = json.loads(initiative_path.read_text(encoding="utf-8"))
    initiative["current_plan"] = {
        "repository": "test",
        "path": "changed-plan.md",
        "commit": "7" * 40,
        "document_sha256": "8" * 64,
    }
    initiative_path.write_text(json.dumps(initiative), encoding="utf-8")

    drifted = lifecycle_diagnostics(registry, store)[0]
    assert drifted["unverified_verified_task_ids"] == [task_id]
    assert drifted["recommended_state"] == "active"


def test_runtime_closeout_typed_acceptance_binding_is_current_verification(
    registry_factory, tmp_path, monkeypatch
):
    source_commit = "e" * 40
    root = _seal_runtime_registry_snapshot(registry_factory(1), source_commit)
    manifest_sha256 = _runtime_registry_manifest_sha256(root)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    task_id = next(iter(registry.tasks))
    _put_runtime_closeout_fixture_revision(
        store,
        task_id,
        manifest_sha256=manifest_sha256,
        typed_acceptance=True,
    )

    operational = Dispatcher(registry, store).registry
    stamp = verification_stamp(operational, store, task_id)

    assert stamp["kind"] == "bureau_runtime_refresh_snapshot_verification"
    lifecycle = lifecycle_diagnostics(registry, store)[0]
    assert lifecycle["unverified_verified_task_ids"] == []
    assert lifecycle["recommended_state"] == "completion-ready"


def test_runtime_closeout_incomplete_acceptance_evidence_remains_unverified(
    registry_factory, tmp_path, monkeypatch
):
    source_commit = "e" * 40
    root = _seal_runtime_registry_snapshot(registry_factory(1), source_commit)
    manifest_sha256 = _runtime_registry_manifest_sha256(root)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    task_id = next(iter(registry.tasks))
    _put_runtime_closeout_fixture_revision(
        store,
        task_id,
        manifest_sha256=manifest_sha256,
        typed_acceptance=True,
        available_evidence=[],
    )

    operational = Dispatcher(registry, store).registry
    with pytest.raises(StateError, match="no current verification"):
        verification_stamp(operational, store, task_id)
    lifecycle = lifecycle_diagnostics(registry, store)[0]
    assert lifecycle["unverified_verified_task_ids"] == [task_id]
    assert lifecycle["recommended_state"] == "active"


def test_runtime_closeout_snapshot_identity_is_validated_once_per_lifecycle_scan(
    registry_factory, tmp_path, monkeypatch
):
    source_commit = "e" * 40
    root = _seal_runtime_registry_snapshot(registry_factory(2), source_commit)
    manifest_sha256 = _runtime_registry_manifest_sha256(root)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    for task_id in registry.tasks:
        _put_runtime_closeout_fixture_revision(
            store, task_id, manifest_sha256=manifest_sha256
        )

    original = bureau_v2._runtime_registry_snapshot_identity
    calls = 0

    def counted_snapshot_identity(candidate_registry):
        nonlocal calls
        calls += 1
        return original(candidate_registry)

    monkeypatch.setattr(
        bureau_v2, "_runtime_registry_snapshot_identity", counted_snapshot_identity
    )

    lifecycle = lifecycle_diagnostics(registry, store)[0]

    assert lifecycle["unverified_verified_task_ids"] == []
    assert calls == 1


def test_lifecycle_scan_skips_snapshot_validation_without_runtime_closeout(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(2)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)

    def unexpected_snapshot_identity(_registry):
        pytest.fail("runtime snapshot identity must stay lazy without a closeout candidate")

    monkeypatch.setattr(
        bureau_v2, "_runtime_registry_snapshot_identity", unexpected_snapshot_identity
    )

    lifecycle_diagnostics(registry, store)


def test_runtime_closeout_later_taskspec_revision_invalidates_verification_stamp(
    registry_factory, tmp_path, monkeypatch
):
    source_commit = "e" * 40
    root = _seal_runtime_registry_snapshot(registry_factory(1), source_commit)
    manifest_sha256 = _runtime_registry_manifest_sha256(root)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    task_id = next(iter(registry.tasks))
    _put_runtime_closeout_fixture_revision(
        store, task_id, manifest_sha256=manifest_sha256
    )

    operational = Dispatcher(registry, store).registry
    assert verification_stamp(operational, store, task_id)["kind"] == (
        "bureau_runtime_refresh_snapshot_verification"
    )

    verified = store.task_spec(task_id)
    assert verified is not None
    revised = json.loads(json.dumps(verified["spec"]))
    revised["acceptance"][0]["assertion"] = "A later acceptance contract must be proven anew."
    store.put_task_spec(
        revised,
        idempotency_key="runtime-closeout-later-acceptance-revision",
        expected_revision=verified["revision"],
        source="test",
    )

    revised_operational = Dispatcher(registry, store).registry
    with pytest.raises(StateError, match="no current verification"):
        verification_stamp(revised_operational, store, task_id)
    lifecycle = lifecycle_diagnostics(registry, store)[0]
    assert lifecycle["unverified_verified_task_ids"] == [task_id]
    assert lifecycle["recommended_state"] == "active"


def test_runtime_closeout_evidence_free_authority_revision_drift_remains_unverified(
    registry_factory, tmp_path, monkeypatch
):
    source_commit = "e" * 40
    root = _seal_runtime_registry_snapshot(registry_factory(1), source_commit)
    manifest_sha256 = _runtime_registry_manifest_sha256(root)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    task_id = next(iter(registry.tasks))
    _put_runtime_closeout_fixture_revision(
        store,
        task_id,
        manifest_sha256=manifest_sha256,
        closeout_acceptance_assertion=(
            "A closeout revision cannot silently replace the authority acceptance contract."
        ),
    )

    operational = Dispatcher(registry, store).registry
    with pytest.raises(StateError, match="no current verification"):
        verification_stamp(operational, store, task_id)
    lifecycle = lifecycle_diagnostics(registry, store)[0]
    assert lifecycle["unverified_verified_task_ids"] == [task_id]
    assert lifecycle["recommended_state"] == "active"


def test_runtime_closeout_manifest_digest_mismatch_remains_unverified(
    registry_factory, tmp_path, monkeypatch
):
    source_commit = "e" * 40
    root = _seal_runtime_registry_snapshot(registry_factory(1), source_commit)
    manifest_sha256 = _runtime_registry_manifest_sha256(root)
    assert manifest_sha256 != "f" * 64
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    task_id = next(iter(registry.tasks))
    _put_runtime_closeout_fixture_revision(
        store, task_id, manifest_sha256="f" * 64
    )

    lifecycle = lifecycle_diagnostics(registry, store)[0]
    assert lifecycle["unverified_verified_task_ids"] == [task_id]
    with pytest.raises(StateError, match="no current verification"):
        verification_stamp(Dispatcher(registry, store).registry, store, task_id)


def test_runtime_closeout_source_commit_mismatch_remains_unverified(
    registry_factory, tmp_path, monkeypatch
):
    root = _seal_runtime_registry_snapshot(registry_factory(1), "9" * 40)
    manifest_sha256 = _runtime_registry_manifest_sha256(root)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    task_id = next(iter(registry.tasks))
    _put_runtime_closeout_fixture_revision(
        store, task_id, manifest_sha256=manifest_sha256
    )

    lifecycle = lifecycle_diagnostics(registry, store)[0]
    assert lifecycle["unverified_verified_task_ids"] == [task_id]
    with pytest.raises(StateError, match="no current verification"):
        verification_stamp(Dispatcher(registry, store).registry, store, task_id)


def test_runtime_drift_check_accepts_validated_runtime_closeout_supersession(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    init_clean_origin_main(root)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    task_id = next(iter(registry.tasks))
    dispatcher = Dispatcher(registry, store)
    run = dispatcher.claim_next("worker", ("repository",))["run"]
    complete_run(registry, store, run["run_id"], {"proof": {"result": "passed"}})

    current = store.task_spec(task_id)
    assert current is not None
    closed = json.loads(json.dumps(current["spec"]))
    closed["state"] = "verified"
    metadata = closed.setdefault("metadata", {})
    metadata.pop("verification", None)
    metadata["runtime_refresh_authority"] = _runtime_authority_receipts_fixture(task_id)
    metadata["runtime_closeout"] = _runtime_closeout_fixture(task_id)
    store.put_task_spec(
        closed,
        idempotency_key="runtime-drift-terminal-runtime-closeout",
        expected_revision=current["revision"],
        source="runtime-refresh-no-run-closeout",
    )
    store.set_initiative_state(next(iter(registry.initiatives)), "completion-ready")

    report = runtime_drift_check(root, state_db=store.path)
    codes = {item["code"] for item in report["findings"]}

    assert report["status"] == "ok"
    assert report["receipts"]["stale_tasks"] == []
    superseded = report["receipts"]["runtime_closeout_receipt_tasks"]
    assert superseded[0]["task_id"] == task_id
    assert superseded[0]["reason"] == "validated-runtime-closeout"
    assert superseded[0]["runtime_result_sha256"] == "d" * 64
    assert "receipt-drift" not in codes
    assert "receipt-drift-superseded-by-runtime-closeout" in codes


def test_runtime_drift_check_rejects_runtime_closeout_for_different_task(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    init_clean_origin_main(root)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    task_id = next(iter(registry.tasks))
    dispatcher = Dispatcher(registry, store)
    run = dispatcher.claim_next("worker", ("repository",))["run"]
    complete_run(registry, store, run["run_id"], {"proof": {"result": "passed"}})

    current = store.task_spec(task_id)
    assert current is not None
    drifted = json.loads(json.dumps(current["spec"]))
    drifted["state"] = "verified"
    metadata = drifted.setdefault("metadata", {})
    metadata.pop("verification", None)
    metadata["runtime_refresh_authority"] = _runtime_authority_receipts_fixture(task_id)
    metadata["runtime_closeout"] = _runtime_closeout_fixture("different-task")
    store.put_task_spec(
        drifted,
        idempotency_key="runtime-drift-foreign-runtime-closeout",
        expected_revision=current["revision"],
        source="test",
    )
    store.set_initiative_state(next(iter(registry.initiatives)), "completion-ready")

    report = runtime_drift_check(root, state_db=store.path)

    assert report["status"] == "blocked"
    assert report["receipts"]["runtime_closeout_receipt_tasks"] == []
    assert report["receipts"]["stale_tasks"][0]["task_id"] == task_id
    assert "receipt-drift" in {item["code"] for item in report["findings"]}


def test_runtime_drift_check_blocks_plan_drift_with_task_spec_authority(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    init_clean_origin_main(root)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    dispatcher = Dispatcher(registry, store)
    run = dispatcher.claim_next("worker", ("repository",))["run"]
    complete_run(registry, store, run["run_id"], {"proof": {"result": "passed"}})

    initiative_path = root / "registry/initiatives/main.json"
    initiative = json.loads(initiative_path.read_text())
    initiative["current_plan"] = {
        "repository": "test",
        "path": "plan.md",
        "commit": "3" * 40,
        "document_sha256": "4" * 64,
    }
    initiative_path.write_text(json.dumps(initiative))
    git_output(root, "add", ".")
    git_output(root, "commit", "-m", "change initiative plan")
    git_output(
        root, "update-ref", "refs/remotes/origin/main", git_output(root, "rev-parse", "HEAD")
    )

    report = runtime_drift_check(root, state_db=store.path)
    stale = report["receipts"]["stale_tasks"]

    assert report["status"] == "blocked"
    assert {item["code"] for item in report["findings"]} >= {"receipt-drift"}
    assert stale[0]["stored_task_sha256"] == stale[0]["current_task_sha256"]
    assert stale[0]["stored_plan_sha256"] != stale[0]["current_plan_sha256"]


def test_runtime_drift_check_keeps_git_bootstrap_receipt_gate(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    init_clean_origin_main(root)
    registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    task_id = next(iter(registry.tasks))
    assert store.task_spec(task_id) is None
    run = dispatcher.claim_next("worker", ("repository",))["run"]
    complete_run(registry, store, run["run_id"], {"proof": {"result": "passed"}})

    task_path = next((root / "registry/tasks").glob("*.json"))
    task = json.loads(task_path.read_text())
    task["title"] = "Git bootstrap task contract drift"
    task_path.write_text(json.dumps(task))
    git_output(root, "add", ".")
    git_output(root, "commit", "-m", "change bootstrap task")
    git_output(
        root, "update-ref", "refs/remotes/origin/main", git_output(root, "rev-parse", "HEAD")
    )

    report = runtime_drift_check(root, state_db=store.path)

    assert report["status"] == "blocked"
    assert report["receipts"]["stale_tasks"][0]["task_id"] == task_id
    assert "receipt-drift" in {item["code"] for item in report["findings"]}


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


def test_runtime_drift_check_treats_stale_state_rows_as_superseded_when_registry_verifies(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    init_clean_origin_main(root)
    registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    run = dispatcher.claim_next("worker", ("repository",))["run"]
    complete_run(registry, store, run["run_id"], {"proof": {"result": "passed"}})

    task_path = next((root / "registry/tasks").glob("*.json"))
    task = json.loads(task_path.read_text())
    task["state"] = "verified"
    task["title"] = "Changed after receipt but registry verified"
    metadata = task.setdefault("metadata", {})
    metadata.pop("verification", None)
    metadata["verification"] = {
        "task_sha256": task_revision_sha256(task),
        "plan_sha256": plan_sha256(registry, task["initiative"]),
    }
    remove_from_queue(root, task["id"])
    task_path.write_text(json.dumps(task))
    initiative_path = root / "registry/initiatives/main.json"
    initiative = json.loads(initiative_path.read_text())
    initiative["state"] = "completion-ready"
    initiative_path.write_text(json.dumps(initiative))
    git_output(root, "add", ".")
    git_output(root, "commit", "-m", "verify task in registry")
    git_output(
        root, "update-ref", "refs/remotes/origin/main", git_output(root, "rev-parse", "HEAD")
    )

    report = runtime_drift_check(root, state_db=store.path)
    codes = {item["code"] for item in report["findings"]}

    assert report["status"] == "ok"
    assert report["receipts"]["stale_tasks"] == []
    assert report["receipts"]["superseded_receipt_tasks"][0]["task_id"] == task["id"]
    assert "receipt-drift" not in codes
    assert "receipt-drift-superseded-by-registry-verification" in codes


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
    remove_from_queue(root, task["id"])
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
    remove_from_queue(root, task["id"])
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
    remove_from_queue(root, task["id"])
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


def test_claim_next_cli_exposes_runtime_preflight_truth(
    registry_factory, tmp_path, capsys
):
    root = registry_factory(1)
    head = init_clean_origin_main(root)

    result = bureau_cli.main(
        [
            "--root",
            str(root),
            "--state-db",
            str(tmp_path / "claim.sqlite3"),
            "--json",
            "claim-next",
            "--worker",
            "runtime-truth-worker",
            "--capability",
            "repository",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    runtime_truth = output["runtime_truth"]
    envelope_truth = output["envelope"]["runtime_truth"]
    assert result == 0
    assert output["status"] == "claimed"
    assert runtime_truth["status"] == "clear"
    assert runtime_truth["execution_blocked"] is False
    assert runtime_truth["drift_classification"] == "clean"
    assert runtime_truth["checkout"]["branch"] == "main"
    assert runtime_truth["checkout"]["head"] == head
    assert runtime_truth["checkout"]["base"] == head
    assert runtime_truth["checkout"]["head_equals_base"] is True
    assert runtime_truth["checkout"]["dirty"] is False
    assert envelope_truth == runtime_truth


def test_claim_next_cli_fails_closed_on_dirty_runtime_checkout(
    registry_factory, tmp_path, capsys
):
    root = registry_factory(1)
    init_clean_origin_main(root)
    (root / "dirty-runtime.txt").write_text("dirty", encoding="utf-8")
    state_db = tmp_path / "dirty.sqlite3"

    result = bureau_cli.main(
        [
            "--root",
            str(root),
            "--state-db",
            str(state_db),
            "--json",
            "claim-next",
            "--worker",
            "dirty-worker",
            "--capability",
            "repository",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    runtime_truth = output["runtime_truth"]
    assert result == 2
    assert output["status"] == "runtime-drift-blocked"
    assert output["command"] == "claim-next"
    assert runtime_truth["status"] == "blocked"
    assert runtime_truth["execution_blocked"] is True
    assert runtime_truth["drift_classification"] == "blocked"
    assert "checkout-dirty" in runtime_truth["blocker_codes"]
    assert any("dirty-runtime.txt" in path for path in runtime_truth["checkout"]["dirty_paths"])

    store = StateStore(state_db)
    assert store.list_runs() == []


def test_checkout_next_cli_fails_closed_on_dirty_runtime_checkout(
    registry_factory, tmp_path, capsys
):
    root = registry_factory(1)
    init_clean_origin_main(root)
    (root / "dirty-runtime.txt").write_text("dirty", encoding="utf-8")

    result = bureau_cli.main(
        [
            "--root",
            str(root),
            "--state-db",
            str(tmp_path / "checkout-dirty.sqlite3"),
            "--json",
            "checkout-next",
            "--worker",
            "checkout-dirty-worker",
            "--capability",
            "repository",
            "--base-dir",
            str(tmp_path / "worktrees"),
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert result == 2
    assert output["status"] == "runtime-drift-blocked"
    assert output["command"] == "checkout-next"
    assert output["runtime_truth"]["execution_blocked"] is True
    assert "checkout-dirty" in output["runtime_truth"]["blocker_codes"]


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
    remove_from_queue(root, task["id"])
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


def test_explain_next_can_be_scoped_to_repository_resource(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(2, mode="write")
    _registry, _store, dispatcher = setup(root, tmp_path, monkeypatch)

    explained = dispatcher.explain_next({"repository"}, resource="repo.beta")

    assert explained["resource"] == "repo.beta"
    assert explained["selected"]["task_id"] == "BUR-TEST-001-T002"
    assert all(
        any(
            bureau_v2.legacy.overlaps(resource, "repo.beta", dispatcher.registry.resources)
            for resource in item["claim_resources"]
        )
        for item in explained["frontier"]
    )

def test_what_now_ranks_eligible_tasks_from_registry_truth(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(3, mode="write")
    _registry, _store, dispatcher = setup(root, tmp_path, monkeypatch)

    result = dispatcher.what_now({"repository"}, limit=2)

    assert result["selected"]["task_id"] == "BUR-TEST-001-T001"
    assert [item["task_id"] for item in result["ranked_eligible"]] == [
        "BUR-TEST-001-T001",
        "BUR-TEST-001-T002",
    ]
    assert result["selected"]["rank_key"] == {
        "lane_order": 0,
        "queue_index": 0,
        "task_rank": 0,
        "task_id": "BUR-TEST-001-T001",
    }
    assert result["selected"]["claims"][0]["resource"] == "repo.alpha"
    assert result["ranking_contract"]["does_not_use"] == [
        "raw chat memory",
        "informal plans outside live-register",
    ]
    assert result["live_register"]["summary"]["records"] == 0


def test_what_now_explains_blockers_when_no_task_is_eligible(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(2, mode="write")
    _registry, _store, dispatcher = setup(root, tmp_path, monkeypatch)

    result = dispatcher.what_now(set(), limit=3)

    assert result["selected"] is None
    assert result["ranked_eligible"] == []
    assert result["runtime_truth"]["next_task_available"] is False
    assert result["blocker_summary"]["total_blocked"] == 2
    assert result["blocked"][0]["task_id"] == "BUR-TEST-001-T001"
    assert result["blocked"][0]["missing_capabilities"] == ["repository"]
    assert "missing capabilities: repository" in result["blocked"][0]["reasons"]


def test_what_now_compact_projection_omits_high_volume_details(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(3, mode="write")
    _registry, _store, dispatcher = setup(root, tmp_path, monkeypatch)

    result = dispatcher.what_now({"repository"}, limit=2, compact=True)

    assert result["projection"] == "compact"
    assert result["selected"]["task_id"] == "BUR-TEST-001-T001"
    assert "claims" not in result["selected"]
    assert "approval_contract" not in result["selected"]
    assert result["lifecycle"]["total"] == 1
    assert "task_states" not in json.dumps(result["lifecycle"])
    assert "latest_candidates" not in result["live_register"]["summary"]
    assert len(result["blocked"]) <= 2
    assert result["blocker_summary"]["blocked_returned"] <= 2
    assert "repair_task_candidates" not in result["runtime_truth"]
    assert "repair_recommendations" not in result["runtime_truth"]
    assert "repair_task_candidate_count" in result["runtime_truth"]


def test_what_now_compact_projection_reports_blocker_truncation(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(3, mode="write")
    _registry, _store, dispatcher = setup(root, tmp_path, monkeypatch)

    result = dispatcher.what_now(set(), limit=1, compact=True)

    assert result["blocker_summary"]["total_blocked"] == 3
    assert result["blocker_summary"]["blocked_returned"] == 1
    assert result["blocker_summary"]["blocked_truncated"] is True


def test_what_now_treats_planned_review_before_effect_as_operator_eligible(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1, mode="write")
    task_path = next((root / "registry/tasks").glob("*.json"))
    task = json.loads(task_path.read_text())
    task["state"] = "planned"
    task["priority"] = {"lane": "next", "rank": 10}
    task["execution"]["policy"] = "review-before-effect"
    task_path.write_text(json.dumps(task))
    queue_path = root / "registry/queue.json"
    queue = json.loads(queue_path.read_text())
    queue["lanes"] = {"now": [], "next": [task["id"]], "later": []}
    queue_path.write_text(json.dumps(queue))
    _registry, _store, dispatcher = setup(root, tmp_path, monkeypatch)

    result = dispatcher.what_now({"repository"})

    assert result["selected"]["task_id"] == task["id"]
    assert result["selected"]["eligible"] is True
    assert result["selected"]["claim_eligible"] is False
    assert result["selected"]["soft_reasons"] == [
        "state is planned",
        "execution is interactive-agent/review-before-effect",
    ]
    assert result["selected"]["blocker_reasons"] == []


def test_what_now_cli_is_read_only_and_json_emits_ranked_answer(
    registry_factory, tmp_path, monkeypatch, capsys
):
    root = registry_factory(1, mode="write")
    state = tmp_path / "state"
    monkeypatch.setenv("BUREAU_STATE_DIR", str(state))

    result = bureau_cli.main(
        [
            "--root",
            str(root),
            "--json",
            "what-now",
            "--capability",
            "repository",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert result == 0
    assert output["selected"]["task_id"] == "BUR-TEST-001-T001"
    assert output["does_not_establish"] == [
        "claim authority",
        "workspace creation",
        "dispatch authority",
        "merge readiness",
    ]
    envelope_dir = state / "envelopes"
    assert not envelope_dir.exists() or list(envelope_dir.iterdir()) == []


def test_what_now_cli_compact_flag_selects_compact_projection(
    registry_factory, tmp_path, monkeypatch, capsys
):
    root = registry_factory(1, mode="write")
    monkeypatch.setenv("BUREAU_STATE_DIR", str(tmp_path / "state"))

    result = bureau_cli.main(
        [
            "--root",
            str(root),
            "--json",
            "what-now",
            "--capability",
            "repository",
            "--compact",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert result == 0
    assert output["projection"] == "compact"
    assert output["selected"]["task_id"] == "BUR-TEST-001-T001"


def test_what_now_includes_live_register_context(registry_factory, tmp_path, monkeypatch):
    root = registry_factory(1, mode="write")
    registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    from bureau.live_register import live_register_record

    live_register_record(
        registry,
        store,
        kind="candidate_task",
        repo="repo.alpha",
        title="Alpha candidate",
        promotion_required=True,
    )

    result = dispatcher.what_now({"repository"}, resource="repo.alpha")

    assert result["selected"]["task_id"] == "BUR-TEST-001-T001"
    assert result["live_register"]["summary"]["promotion_required_count"] == 1
    assert result["live_register"]["does_not_establish"] == [
        "registry_task_truth",
        "queue_truth",
        "claim_authority",
        "dispatch_authority",
        "merge_readiness",
    ]



def test_resource_scoped_claims_allow_one_ball_per_repository_with_scoped_workers(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(2, mode="write")
    _registry, _store, dispatcher = setup(root, tmp_path, monkeypatch)

    alpha = dispatcher.claim_next(
        "worker-alpha", ("repository",), resource="repo.alpha"
    )["run"]
    beta = dispatcher.claim_next(
        "worker-beta", ("repository",), resource="repo.beta"
    )["run"]
    alpha_again = dispatcher.claim_next(
        "worker-alpha", ("repository",), resource="repo.alpha"
    )

    assert alpha["task_id"] == "BUR-TEST-001-T001"
    assert beta["task_id"] == "BUR-TEST-001-T002"
    assert alpha_again["status"] == "existing-assignment"
    assert alpha_again["run"]["task_id"] == alpha["task_id"]
    with pytest.raises(StateError, match="resource-scoped worker id"):
        dispatcher.claim_next("worker-alpha", ("repository",), resource="repo.beta")


def test_repo_balls_projects_current_ball_per_repository(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(2, mode="write")
    _registry, _store, dispatcher = setup(root, tmp_path, monkeypatch)

    report = dispatcher.repo_balls({"repository"})

    assert report["read_only"] is True
    assert report["scope"] == "repository"
    assert report["repo_balls"]["repo.alpha"]["status"] == "ready"
    assert (
        report["repo_balls"]["repo.alpha"]["current_ball"]["task_id"]
        == "BUR-TEST-001-T001"
    )
    assert report["repo_balls"]["repo.beta"]["status"] == "ready"
    assert (
        report["repo_balls"]["repo.beta"]["current_ball"]["task_id"]
        == "BUR-TEST-001-T002"
    )




def test_repo_balls_overlays_live_focus_without_changing_current_ball(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(2, mode="write")
    registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    from bureau.live_register import live_register_record

    live_register_record(
        registry,
        store,
        kind="thread_focus",
        thread_id="chat-alpha",
        repo="repo.alpha",
        title="Alpha live focus",
    )

    report = dispatcher.repo_balls({"repository"})

    alpha = report["repo_balls"]["repo.alpha"]
    assert alpha["current_ball"]["task_id"] == "BUR-TEST-001-T001"
    assert alpha["live_register"]["counts"]["active_thread_focus"] == 1
    assert report["summary"]["live_register_repositories"] == 1


def test_live_conflicts_reports_thread_worker_run_conflict(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(2, mode="write")
    registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    from bureau.live_register import live_register_record

    run = dispatcher.claim_next("worker-alpha", ("repository",), resource="repo.alpha")["run"]
    live_register_record(
        registry,
        store,
        kind="thread_focus",
        thread_id="chat-alpha",
        worker_id="worker-alpha",
        repo="repo.alpha",
        title="Conflicting live focus",
    )

    report = dispatcher.live_conflicts({"repository"}, resource="repo.alpha")

    assert report["summary"]["findings"] >= 1
    assert any(
        item["code"] == "live-worker-has-different-active-run"
        and item["run_id"] == run["run_id"]
        for item in report["findings"]
    )



def test_repo_balls_reports_ambiguous_same_repo_active_runs(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(3, mode="read")
    _registry, _store, dispatcher = setup(root, tmp_path, monkeypatch)

    first = dispatcher.claim_next(
        "worker-alpha-1", ("repository",), resource="repo.alpha"
    )["run"]
    second = dispatcher.claim_next(
        "worker-alpha-2", ("repository",), resource="repo.alpha"
    )["run"]
    beta = dispatcher.claim_next(
        "worker-beta", ("repository",), resource="repo.beta"
    )["run"]

    report = dispatcher.repo_balls({"repository"})

    alpha = report["repo_balls"]["repo.alpha"]
    beta_ball = report["repo_balls"]["repo.beta"]
    assert alpha["status"] == "ambiguous"
    assert alpha["current_ball"]["kind"] == "ambiguous_active_runs"
    assert alpha["current_ball"]["run_ids"] == sorted(
        [first["run_id"], second["run_id"]]
    )
    assert alpha["findings"][0]["code"] == "multiple-active-balls-for-repository"
    assert alpha["findings"][0]["severity"] == "blocker"
    assert beta_ball["status"] == "active"
    assert beta_ball["current_ball"]["task_id"] == beta["task_id"]
    assert report["summary"]["ambiguous"] == 1
    assert report["summary"]["active"] == 1


def test_repo_balls_cli_emits_repository_projection(
    registry_factory, tmp_path, capsys
):
    root = registry_factory(2, mode="write")
    state = StateStore(tmp_path / "bureau.sqlite3")

    result = bureau_cli.main(
        [
            "--root",
            str(root),
            "--state-db",
            str(state.path),
            "--json",
            "repo-balls",
            "--capability",
            "repository",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert result == 0
    assert output["scope"] == "repository"
    assert (
        output["repo_balls"]["repo.alpha"]["current_ball"]["task_id"]
        == "BUR-TEST-001-T001"
    )


def test_doctor_warns_then_blocks_deprecated_global_bureau_lease(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    task_path = next((root / "registry/tasks").glob("*.json"))
    task = json.loads(task_path.read_text())
    task["execution"]["grabowski_resources"] = ["repo:/home/alex/repos/bureau"]
    task["state"] = "planned"
    task["priority"]["lane"] = "later"
    task_path.write_text(json.dumps(task))
    queue_path = root / "registry/queue.json"
    queue = json.loads(queue_path.read_text())
    task_id = task["id"]
    queue["lanes"]["now"] = []
    queue["lanes"]["later"] = [task_id]
    queue_path.write_text(json.dumps(queue))

    registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    warning_report = dispatcher.doctor()
    assert warning_report["lease_scope_blockers"] == []
    assert warning_report["lease_scope_findings"][0]["severity"] == "warning"

    task["state"] = "ready"
    task["priority"]["lane"] = "now"
    task_path.write_text(json.dumps(task))
    queue["lanes"]["later"] = []
    queue["lanes"]["now"] = [task_id]
    queue_path.write_text(json.dumps(queue))

    registry = Registry.load(root)
    blocked = Dispatcher(registry, store).doctor()
    assert blocked["healthy"] is False
    assert blocked["lease_scope_blockers"][0]["task_id"] == task_id


def prepare_coordinated_registry(root: Path) -> str:
    task_path = next((root / "registry/tasks").glob("*.json"))
    task = json.loads(task_path.read_text())
    task["execution"]["policy"] = "review-before-effect"
    task["execution"]["grabowski_resources"] = [f"path:{root / 'leased-component'}"]
    task_path.write_text(json.dumps(task))
    init_clean_origin_main(root)
    return task["id"]




def declare_runtime_mutation(root: Path, task_id: str) -> None:
    task_path = root / f"registry/tasks/{task_id}.json"
    task = json.loads(task_path.read_text())
    task["execution"]["approval"] = {
        "action_class": "runtime_mutation",
        "required_level": "break_glass",
        "note": "test-only live runtime effect",
    }
    task_path.write_text(json.dumps(task))

def coordinated_lease_database(
    root: Path,
    intent: dict,
    *,
    omit: set[str] | None = None,
    metadata: dict | None = None,
) -> tuple[dict, Path]:
    database = root / "resources.sqlite3"
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO metadata VALUES ('schema_version', '1')")
    connection.execute(
        "INSERT INTO metadata VALUES ('resource_lease_contract_version', '1')"
    )
    connection.execute(
        """
        CREATE TABLE leases (
            resource_key TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            purpose TEXT NOT NULL,
            acquired_at_unix INTEGER NOT NULL,
            updated_at_unix INTEGER NOT NULL,
            expires_at_unix INTEGER NOT NULL,
            metadata_sha256 TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            reclaimed_from_owner TEXT
        )
        """
    )
    now = int(time.time())
    bound_metadata = metadata or {
        "task_id": intent["task_id"],
        "run_id": intent["run_id"],
        "claim_intent_sha256": intent["intent_sha256"],
    }
    metadata_json = json.dumps(
        bound_metadata,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.sha256(metadata_json.encode()).hexdigest()
    for key in intent["required_resource_keys"]:
        if key in (omit or set()):
            continue
        connection.execute(
            "INSERT INTO leases VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                key,
                intent["lease_owner_id"],
                "coordinated test",
                now - 30,
                now - 30,
                now + 3600,
                digest,
                metadata_json,
            ),
        )
    connection.commit()
    connection.close()
    database.chmod(0o600)
    return {
        "owner_id": intent["lease_owner_id"],
        "task_id": intent["task_id"],
    }, database


def orphaned_coordinated_run(
    registry_factory, tmp_path, monkeypatch, *, materialize_workspace: bool = False
):
    root = registry_factory(1, mode="write")
    task_id = prepare_coordinated_registry(root)
    registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    intent = dispatcher.claim_intent(
        "operator",
        ("repository",),
        task_id=task_id,
        approved=True,
        approval_source="test orphan resume",
    )["intent"]
    binding, database = coordinated_lease_database(tmp_path / "lease-db", intent)
    claimed = dispatcher.commit_claim_intent(intent, binding, resource_db=database)
    run_id = claimed["run"]["run_id"]
    if materialize_workspace:
        bureau_v2.create_workspace(
            registry,
            store,
            run_id,
            Path(intent["workspace"]["workspace_path"]).parent,
        )
    with store.immediate() as connection:
        connection.execute(
            "UPDATE runs SET heartbeat_at='2000-01-01T00:00:00Z' WHERE run_id=?",
            (run_id,),
        )
    reconciled = dispatcher.reconcile(stale_after=1)
    assert reconciled["orphaned"] == [run_id]
    orphaned = store.run(run_id)
    assert orphaned["state"] == "orphaned"
    assert orphaned["error"] == bureau_v2.ORPHANED_STALE_WORKER_ERROR
    assert orphaned["reservations"] == []
    return store, dispatcher, intent, database, orphaned


def test_orphan_resume_preserves_run_identity_and_requires_live_original_lease(
    registry_factory, tmp_path, monkeypatch
):
    store, dispatcher, intent, database, orphaned = orphaned_coordinated_run(
        registry_factory, tmp_path, monkeypatch, materialize_workspace=True
    )

    result = dispatcher.resume_orphaned_run(
        orphaned["run_id"],
        worker_id=orphaned["worker_id"],
        expected_updated_at=orphaned["updated_at"],
        expected_task_sha256=orphaned["task_sha256"],
        expected_plan_sha256=orphaned["plan_sha256"],
        expected_envelope_sha256=orphaned["envelope_sha256"],
        resource_db=database,
    )

    resumed = result["run"]
    assert result["status"] == "resumed"
    assert resumed["state"] == "assigned"
    assert resumed["error"] is None
    assert resumed["run_id"] == orphaned["run_id"]
    assert resumed["worker_id"] == orphaned["worker_id"]
    assert resumed["attempt"] == orphaned["attempt"]
    assert resumed["envelope_sha256"] == orphaned["envelope_sha256"]
    assert resumed["task_sha256"] == orphaned["task_sha256"]
    assert resumed["plan_sha256"] == orphaned["plan_sha256"]
    assert sorted(item["resource_id"] for item in resumed["reservations"]) == sorted(
        claim["resource"] for claim in json.loads(
            Path(store.envelope_path(resumed["run_id"])).read_text()
        )["claims"]
    )
    status = coordinated_claim_status(store, resumed["run_id"], resource_db=database)
    assert status["status"] == "coordinated"
    assert status["blocking"] is False
    assert status["lease"]["status"] == "active-bound"
    workspace = bureau_v2.workspace_status(store, resumed["run_id"])
    assert workspace["state"] == "active"
    assert workspace["workspace_path"] == resumed["workspace_path"]
    assert workspace["branch"] == resumed["workspace_branch"]
    with store.connect() as connection:
        event_types = [
            row["event_type"]
            for row in connection.execute(
                "SELECT event_type FROM events WHERE run_id=? ORDER BY event_id",
                (resumed["run_id"],),
            )
        ]
    assert "run-orphan-resumed" in event_types
    assert intent["run_id"] == resumed["run_id"]


def test_orphan_resume_supersedes_prior_bound_heartbeat(
    registry_factory, tmp_path, monkeypatch
):
    store, dispatcher, _intent, database, orphaned = orphaned_coordinated_run(
        registry_factory, tmp_path, monkeypatch
    )
    activity_id = "orphan-resume-bound-heartbeat"
    payload = {
        "kind": BOUND_ACTIVITY_KIND,
        "source": BOUND_ACTIVITY_SOURCE,
        "outcome": BOUND_ACTIVITY_OUTCOME,
        "activity": {
            "activity_id": activity_id,
            "run_id": orphaned["run_id"],
            "task_id": orphaned["task_id"],
            "worker_id": orphaned["worker_id"],
            "task_sha256": orphaned["task_sha256"],
            "plan_sha256": orphaned["plan_sha256"],
            "envelope_sha256": orphaned["envelope_sha256"],
            "external_binding": {"status": "explicitly-unbound"},
        },
        "evidence": {
            "source": BOUND_ACTIVITY_UNBOUND_EVIDENCE_SOURCE,
            "binding_status": "explicitly-unbound",
        },
        "heartbeat_at": orphaned["heartbeat_at"],
    }
    with store.immediate() as connection:
        store.event(
            connection,
            "run-heartbeat",
            payload,
            orphaned["run_id"],
            activity_id=activity_id,
        )

    before = run_heartbeat_projection(store, orphaned["run_id"])
    assert before["status"] == "valid-bound-activity"

    result = dispatcher.resume_orphaned_run(
        orphaned["run_id"],
        worker_id=orphaned["worker_id"],
        expected_updated_at=orphaned["updated_at"],
        expected_task_sha256=orphaned["task_sha256"],
        expected_plan_sha256=orphaned["plan_sha256"],
        expected_envelope_sha256=orphaned["envelope_sha256"],
        resource_db=database,
    )

    projection = run_heartbeat_projection(store, orphaned["run_id"])
    assert result["status"] == "resumed"
    assert projection["status"] == "normal"
    assert projection["activity_id"] is None
    assert projection["heartbeat_at"] == result["run"]["heartbeat_at"]
    with store.connect() as connection:
        latest = connection.execute(
            "SELECT activity_id,payload_json FROM events "
            "WHERE run_id=? AND event_type='run-heartbeat' "
            "ORDER BY event_id DESC LIMIT 1",
            (orphaned["run_id"],),
        ).fetchone()
    assert latest["activity_id"] is None
    assert json.loads(latest["payload_json"])["source"] == "orphan-resume"


def test_orphan_resume_fails_closed_without_live_original_lease(
    registry_factory, tmp_path, monkeypatch
):
    store, dispatcher, _intent, database, orphaned = orphaned_coordinated_run(
        registry_factory, tmp_path, monkeypatch
    )
    connection = sqlite3.connect(database)
    connection.execute("DELETE FROM leases")
    connection.commit()
    connection.close()

    with pytest.raises(StateError, match="orphan resume lease validation failed"):
        dispatcher.resume_orphaned_run(
            orphaned["run_id"],
            worker_id=orphaned["worker_id"],
            expected_updated_at=orphaned["updated_at"],
            expected_task_sha256=orphaned["task_sha256"],
            expected_plan_sha256=orphaned["plan_sha256"],
            expected_envelope_sha256=orphaned["envelope_sha256"],
            resource_db=database,
        )
    unchanged = store.run(orphaned["run_id"])
    assert unchanged["state"] == "orphaned"
    assert unchanged["reservations"] == []


def test_orphan_resume_rejects_stale_run_snapshot_before_effect(
    registry_factory, tmp_path, monkeypatch
):
    store, dispatcher, _intent, database, orphaned = orphaned_coordinated_run(
        registry_factory, tmp_path, monkeypatch
    )
    with pytest.raises(StateError, match="orphan resume run snapshot drift"):
        dispatcher.resume_orphaned_run(
            orphaned["run_id"],
            worker_id=orphaned["worker_id"],
            expected_updated_at="1999-01-01T00:00:00Z",
            expected_task_sha256=orphaned["task_sha256"],
            expected_plan_sha256=orphaned["plan_sha256"],
            expected_envelope_sha256=orphaned["envelope_sha256"],
            resource_db=database,
        )
    unchanged = store.run(orphaned["run_id"])
    assert unchanged["state"] == "orphaned"
    assert unchanged["reservations"] == []


def test_orphan_resume_reloads_authoritative_task_spec_before_effect(
    registry_factory, tmp_path, monkeypatch
):
    store, dispatcher, _intent, database, orphaned = orphaned_coordinated_run(
        registry_factory, tmp_path, monkeypatch
    )
    store.import_registry_task_specs(dispatcher.source_registry)
    current = store.task_spec(orphaned["task_id"])
    assert current is not None
    revised = json.loads(json.dumps(current["spec"]))
    revised["priority"]["rank"] += 1
    store.put_task_spec(
        revised,
        idempotency_key="test-orphan-resume-task-revision",
        expected_revision=current["revision"],
        source="test orphan resume task revision",
    )

    with pytest.raises(StateError, match="orphan resume current TaskSpec differs from run"):
        dispatcher.resume_orphaned_run(
            orphaned["run_id"],
            worker_id=orphaned["worker_id"],
            expected_updated_at=orphaned["updated_at"],
            expected_task_sha256=orphaned["task_sha256"],
            expected_plan_sha256=orphaned["plan_sha256"],
            expected_envelope_sha256=orphaned["envelope_sha256"],
            resource_db=database,
        )
    unchanged = store.run(orphaned["run_id"])
    assert unchanged["state"] == "orphaned"
    assert unchanged["reservations"] == []


def test_orphan_resume_honors_runtime_drift_gate_before_effect(
    registry_factory, tmp_path, monkeypatch
):
    store, dispatcher, _intent, database, orphaned = orphaned_coordinated_run(
        registry_factory, tmp_path, monkeypatch
    )
    dispatcher.enforce_runtime_gate = True
    monkeypatch.setattr(
        dispatcher,
        "_runtime_execution_truth",
        lambda: {"execution_blocked": True, "status": "blocked-for-test"},
    )

    result = dispatcher.resume_orphaned_run(
        orphaned["run_id"],
        worker_id=orphaned["worker_id"],
        expected_updated_at=orphaned["updated_at"],
        expected_task_sha256=orphaned["task_sha256"],
        expected_plan_sha256=orphaned["plan_sha256"],
        expected_envelope_sha256=orphaned["envelope_sha256"],
        resource_db=database,
    )

    assert result["status"] == "runtime-drift-blocked"
    assert result["command"] == "orphan-resume"
    unchanged = store.run(orphaned["run_id"])
    assert unchanged["state"] == "orphaned"
    assert unchanged["reservations"] == []


def test_coordinated_claim_intent_records_issuance_and_requires_approval(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1, mode="write")
    task_id = prepare_coordinated_registry(root)
    _registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    with pytest.raises(NoEligibleTask, match="review-before-effect"):
        dispatcher.claim_intent(
            "operator",
            ("repository",),
            task_id=task_id,
            approved=False,
        )
    assert store.list_runs() == []
    result = dispatcher.claim_intent(
        "operator",
        ("repository",),
        task_id=task_id,
        base_dir=tmp_path / "worktrees",
        approved=True,
        approval_source="test explicit approval",
    )
    assert result["status"] == "claim-intent"
    assert result["ready_supply"]["lease_required"] is True
    assert result["intent"]["operator_approval"]["task_id"] == task_id
    handoff = result["claim_commit_handoff"]
    assert handoff["claim_intent_sha256"] == result["intent"]["intent_sha256"]
    assert handoff["run_id"] == result["intent"]["run_id"]
    assert handoff["task_id"] == task_id
    assert handoff["commit_operation"] == "claim-commit"
    assert handoff["lease_binding_required"] is True
    assert handoff["lease_owner_id"] == result["intent"]["lease_owner_id"]
    assert handoff["required_resource_keys"] == result["intent"]["required_resource_keys"]
    assert handoff["required_lease_metadata"] == {
        "task_id": task_id,
        "run_id": result["intent"]["run_id"],
        "claim_intent_sha256": result["intent"]["intent_sha256"],
    }
    assert handoff["minimum_remaining_seconds"] == 60
    assert store.list_runs() == []
    issuance = store.claim_intent_issuance(result["intent"]["run_id"])
    assert issuance["intent_sha256"] == result["intent"]["intent_sha256"]
    assert issuance["worker_id"] == "operator"
    assert issuance["reviewer"] == "operator"
    assert issuance["action_class"] == "repository_mutation"
    assert issuance["approval_level"] == "operator"
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM workers").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type=?",
            (bureau_v2.COORDINATED_CLAIM_ISSUED_EVENT,),
        ).fetchone()[0] == 1



def test_coordinated_claim_intent_idempotency_and_unknown_outcome_readback(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1, mode="write")
    task_id = prepare_coordinated_registry(root)
    _registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    monkeypatch.setattr(
        bureau_v2,
        "_coordinated_grabowski_resource_keys",
        lambda _resources, _task, _open_pr_scope: set(),
    )

    first = dispatcher.claim_intent(
        "operator",
        ("repository",),
        task_id=task_id,
        approved=True,
        approval_source="t005 idempotency",
        idempotency_key="t005:claim:one",
    )
    replay = dispatcher.claim_intent(
        "operator",
        ("repository",),
        task_id=task_id,
        approved=True,
        approval_source="t005 idempotency",
        idempotency_key="t005:claim:one",
    )

    assert first["idempotent_replay"] is False
    assert replay["idempotent_replay"] is True
    assert replay["intent"] == first["intent"]
    contract = first["mutation_contract"]
    assert contract["idempotency_key"] == "t005:claim:one"
    assert contract["expected_task_state"] == "ready"
    assert contract["attempt"] == 1
    assert first["claim_commit_handoff"]["mutation_contract"] == contract
    assert first["claim_commit_handoff"]["claim_intent_readback"] == {
        "operation": "claim-intent-readback",
        "idempotency_key": "t005:claim:one",
    }
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type=?",
            (bureau_v2.COORDINATED_CLAIM_ISSUED_EVENT,),
        ).fetchone()[0] == 1

    readback = bureau_v2.coordinated_claim_intent_readback(
        store, "t005:claim:one"
    )
    assert readback["status"] == "issued-uncommitted"
    assert readback["intent"] == first["intent"]
    assert readback["mutation_contract"] == contract
    assert readback["next_action"] == "claim-commit"
    assert store.list_runs() == []

    claimed = dispatcher.commit_claim_intent(first["intent"], None)
    assert claimed["status"] == "claimed"
    observed = bureau_v2.coordinated_claim_intent_readback(
        store, "t005:claim:one"
    )
    assert observed["status"] == "run-observed"
    assert observed["run"]["run_id"] == first["intent"]["run_id"]
    assert observed["next_action"] == "claim-coordination-status"


def test_coordinated_claim_idempotency_key_rejects_different_request(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1, mode="write")
    task_id = prepare_coordinated_registry(root)
    _registry, store, dispatcher = setup(root, tmp_path, monkeypatch)

    dispatcher.claim_intent(
        "operator-a",
        ("repository",),
        task_id=task_id,
        approved=True,
        approval_source="t005 key binding",
        idempotency_key="t005:shared-key",
    )
    with pytest.raises(StateError, match="idempotency key is bound to another mutation"):
        dispatcher.claim_intent(
            "operator-b",
            ("repository",),
            task_id=task_id,
            approved=True,
            approval_source="t005 key binding",
            idempotency_key="t005:shared-key",
        )
    assert store.list_runs() == []


def test_coordinated_claim_commit_rejects_task_spec_revision_cas_drift(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1, mode="write")
    task_id = prepare_coordinated_registry(root)
    registry, store, _dispatcher = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    dispatcher = Dispatcher(registry, store)
    fixed_runtime_truth = {
        "schema_version": 1,
        "execution_blocked": False,
        "status": "clear",
    }
    monkeypatch.setattr(dispatcher, "_runtime_execution_truth", lambda: fixed_runtime_truth)
    monkeypatch.setattr(
        bureau_v2,
        "_coordinated_grabowski_resource_keys",
        lambda _resources, _task, _open_pr_scope: set(),
    )

    result = dispatcher.claim_intent(
        "operator",
        ("repository",),
        task_id=task_id,
        approved=True,
        idempotency_key="t005:revision-cas",
    )
    contract = result["mutation_contract"]
    assert contract["expected_task_revision"] == 1
    current = store.task_spec(task_id)
    changed = json.loads(json.dumps(current["spec"]))
    changed["title"] = "concurrent TaskSpec revision"
    store.put_task_spec(
        changed,
        idempotency_key="t005:concurrent-spec-change",
        expected_revision=1,
        source="test concurrent mutation",
    )

    with pytest.raises(StateError, match="stale TaskSpec revision CAS"):
        dispatcher.commit_claim_intent(result["intent"], None)
    assert store.list_runs() == []
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM workers").fetchone()[0] == 0


def test_coordinated_claim_commit_rejects_prior_state_cas_drift(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1, mode="write")
    task_id = prepare_coordinated_registry(root)
    registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    fixed_runtime_truth = {
        "schema_version": 1,
        "execution_blocked": False,
        "status": "clear",
    }
    monkeypatch.setattr(dispatcher, "_runtime_execution_truth", lambda: fixed_runtime_truth)
    monkeypatch.setattr(
        bureau_v2,
        "_coordinated_grabowski_resource_keys",
        lambda _resources, _task, _open_pr_scope: set(),
    )

    result = dispatcher.claim_intent(
        "operator",
        ("repository",),
        task_id=task_id,
        approved=True,
        idempotency_key="t005:state-cas",
    )
    task = registry.tasks[task_id]
    with store.immediate() as connection:
        connection.execute(
            "INSERT INTO task_status("
            "task_id,task_sha256,plan_sha256,state,receipt_sha256,updated_at"
            ") VALUES(?,?,?,?,NULL,?)",
            (
                task_id,
                task.sha256,
                plan_sha256(registry, task.initiative),
                "planned",
                bureau_v2.legacy.utc_now(),
            ),
        )

    with pytest.raises(StateError, match="stale prior-state CAS"):
        dispatcher.commit_claim_intent(result["intent"], None)
    assert store.list_runs() == []


def test_coordinated_claim_parallel_same_task_cas_and_disjoint_tasks_continue(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(2, mode="read")
    for task_path in sorted((root / "registry/tasks").glob("*.json")):
        task = json.loads(task_path.read_text())
        task["execution"]["policy"] = "review-before-effect"
        task["execution"]["grabowski_resources"] = []
        task_path.write_text(json.dumps(task))
    init_clean_origin_main(root)
    registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    monkeypatch.setattr(
        bureau_v2,
        "_coordinated_grabowski_resource_keys",
        lambda _resources, _task, _open_pr_scope: set(),
    )
    first_id, second_id = sorted(registry.tasks)

    same_a = dispatcher.claim_intent(
        "worker-a",
        ("repository",),
        task_id=first_id,
        approved=True,
        idempotency_key="t005:same:a",
    )
    same_b = dispatcher.claim_intent(
        "worker-b",
        ("repository",),
        task_id=first_id,
        approved=True,
        idempotency_key="t005:same:b",
    )
    disjoint = dispatcher.claim_intent(
        "worker-c",
        ("repository",),
        task_id=second_id,
        approved=True,
        idempotency_key="t005:disjoint:c",
    )

    dispatcher.commit_claim_intent(same_a["intent"], None)
    with pytest.raises(StateError, match="stale attempt CAS"):
        dispatcher.commit_claim_intent(same_b["intent"], None)
    second_claim = dispatcher.commit_claim_intent(disjoint["intent"], None)

    active = [run for run in store.list_runs() if run["state"] == "assigned"]
    assert {run["task_id"] for run in active} == {first_id, second_id}
    assert second_claim["run"]["task_id"] == second_id

def test_coordinated_claim_supports_path_leased_worktree_without_broad_claim(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1, mode="write")
    task_path = next((root / "registry/tasks").glob("*.json"))
    task = json.loads(task_path.read_text())
    task["claims"] = []
    task["execution"]["policy"] = "review-before-effect"
    task["execution"]["workspace_isolation"] = "worktree"
    task["execution"]["grabowski_resources"] = [
        f"path:{root / 'leased-component'}"
    ]
    task_path.write_text(json.dumps(task))
    init_clean_origin_main(root)

    registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    intent_result = dispatcher.claim_intent(
        "operator",
        ("repository",),
        task_id=task["id"],
        base_dir=tmp_path / "worktrees",
        approved=True,
        approval_source="test exact path-leased worktree",
    )

    assert intent_result["ready_supply"]["workspace_planned"] is True
    assert intent_result["intent"]["required_resource_keys"] == [
        f"path:{root / 'leased-component'}"
    ]
    binding, database = coordinated_lease_database(
        tmp_path, intent_result["intent"]
    )
    claimed = dispatcher.commit_claim_intent(
        intent_result["intent"], binding, resource_db=database
    )
    workspace = bureau_v2.create_workspace(
        registry, store, claimed["run"]["run_id"], tmp_path / "worktrees"
    )
    assert Path(workspace["workspace_path"]).is_dir()
    assert workspace["workspace_branch"].startswith("bureau/")


def test_path_leased_worktree_rejects_broad_repository_resource(
    registry_factory
):
    root = registry_factory(1, mode="write")
    task_path = next((root / "registry/tasks").glob("*.json"))
    task = json.loads(task_path.read_text())
    task["claims"] = []
    task["execution"]["workspace_isolation"] = "worktree"
    task["execution"]["grabowski_resources"] = [f"repo:{root}"]
    task_path.write_text(json.dumps(task))

    with pytest.raises(
        ValidationError,
        match="path-leased worktree needs exact path resources without broad repo resources",
    ):
        Registry.load(root)


def test_coordinated_claim_intent_uses_origin_main_when_source_head_is_stale(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1, mode="write")
    task_id = prepare_coordinated_registry(root)
    stale_head = git_output(root, "rev-parse", "HEAD")
    (root / "fresh-origin-main.txt").write_text("fresh\n")
    git_output(root, "add", "fresh-origin-main.txt")
    git_output(root, "commit", "-m", "advance origin main")
    origin_main = git_output(root, "rev-parse", "HEAD")
    git_output(root, "update-ref", "refs/remotes/origin/main", origin_main)
    git_output(root, "checkout", "--detach", stale_head)
    (root / "dirty-local-only.txt").write_text("dirty\n")

    _registry, _store, dispatcher = setup(root, tmp_path, monkeypatch)
    result = dispatcher.claim_intent(
        "operator",
        ("repository",),
        task_id=task_id,
        base_dir=tmp_path / "worktrees",
        approved=True,
        approval_source="test clean origin main baseline",
    )

    workspace = result["intent"]["workspace"]
    assert workspace["source_head_at_intent"] == stale_head
    assert workspace["baseline_commit"] == origin_main


def test_coordinated_claim_binds_approval_to_selected_candidate(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(2, mode="write")
    first_path = root / "registry/tasks/BUR-TEST-001-T001.json"
    first = json.loads(first_path.read_text())
    first["execution"]["policy"] = "review-before-effect"
    first["required_capabilities"] = ["repository", "shell"]
    first_path.write_text(json.dumps(first))
    init_clean_origin_main(root)
    _registry, store, dispatcher = setup(root, tmp_path, monkeypatch)

    result = dispatcher.claim_intent(
        "operator",
        ("repository",),
        approved=True,
        approval_source="test selected candidate approval",
    )

    intent = result["intent"]
    assert intent["task_id"] == "BUR-TEST-001-T002"
    assert intent["operator_approval"]["task_id"] == "BUR-TEST-001-T002"
    assert intent["operator_approval"]["scope"] == ["repository_mutation"]
    issuance = store.claim_intent_issuance(intent["run_id"])
    assert issuance["task_id"] == "BUR-TEST-001-T002"
    assert issuance["approval_sha256"] == bureau_v2.legacy.sha256_json(
        intent["operator_approval"]
    )




def test_coordinated_runtime_claim_rejects_operator_approval(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1, mode="write")
    task_id = prepare_coordinated_registry(root)
    declare_runtime_mutation(root, task_id)
    _registry, store, dispatcher = setup(root, tmp_path, monkeypatch)

    with pytest.raises(StateError, match="approval required for runtime_mutation"):
        dispatcher.claim_intent(
            "operator",
            ("repository",),
            task_id=task_id,
            approved=True,
            approval_source="test operator approval",
        )

    assert store.list_runs() == []


def test_claim_intent_hard_blocker_precedes_break_glass_approval(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1, mode="write")
    task_id = prepare_coordinated_registry(root)
    declare_runtime_mutation(root, task_id)
    task_path = root / "registry" / "tasks" / f"{task_id}.json"
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
    remove_from_queue(root, task_id)
    _registry, store, dispatcher = setup(root, tmp_path, monkeypatch)

    with pytest.raises(NoEligibleTask, match="state is verified"):
        dispatcher.claim_intent(
            "operator",
            ("repository",),
            approved=True,
            approval_source="test operator approval",
        )

    assert store.list_runs() == []


def test_claim_intent_cli_json_envelopes_approval_rejection(
    registry_factory, tmp_path, capsys
):
    root = registry_factory(1, mode="write")
    task_id = prepare_coordinated_registry(root)
    declare_runtime_mutation(root, task_id)
    init_clean_origin_main(root)
    state_db = tmp_path / "claim-intent.sqlite3"

    result = bureau_cli.main(
        [
            "--root",
            str(root),
            "--state-db",
            str(state_db),
            "--json",
            "--json-envelope",
            "claim-intent",
            "--worker",
            "operator",
            "--capability",
            "repository",
            "--task-id",
            task_id,
            "--approve",
            "--approval-source",
            "test operator approval",
        ]
    )

    streams = capsys.readouterr()
    envelope = json.loads(streams.out)
    failure = envelope["result"]
    assert result == 2
    assert streams.err == ""
    assert envelope["runtime_identity"]["registry"]["root"] == str(root)
    assert failure["kind"] == "bureau_approval_required"
    assert failure["status"] == "approval-required"
    assert failure["code"] == "approval-required"
    assert failure["effect_started"] is False
    assert failure["ambiguity"] is False
    assert failure["retryable"] is False
    assert failure["required_readback"] == []
    assert failure["approval"]["action_classes"] == ["runtime_mutation"]
    assert failure["approval"]["required_level"] == "break_glass"
    assert failure["approval"]["evidence"]["level"] == "operator"
    assert StateStore(state_db).list_runs() == []


def test_coordinated_runtime_claim_binds_break_glass_through_commit(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1, mode="write")
    task_id = prepare_coordinated_registry(root)
    declare_runtime_mutation(root, task_id)
    registry, store, dispatcher = setup(root, tmp_path, monkeypatch)

    intent = dispatcher.claim_intent(
        "operator",
        ("repository",),
        task_id=task_id,
        break_glass=True,
        approval_source="test explicit break glass",
    )["intent"]

    approval = intent["operator_approval"]
    assert approval["level"] == "break_glass"
    assert approval["scope"] == ["runtime_mutation"]
    assert approval["reference"] == intent["run_id"]
    assert approval["task_id"] == task_id

    binding, database = coordinated_lease_database(tmp_path / "leases", intent)
    claimed = dispatcher.commit_claim_intent(intent, binding, resource_db=database)

    decision = claimed["envelope"]["operator_approval"]
    assert decision["action_class"] == "runtime_mutation"
    assert decision["required_level"] == "break_glass"
    assert decision["allowed"] is True
    assert decision["evidence"]["level"] == "break_glass"
    assert registry.tasks[task_id].state == "ready"
    assert store.run(intent["run_id"])["state"] == "assigned"


def test_coordinated_runtime_claim_rejects_rehashed_approval_downgrade(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1, mode="write")
    task_id = prepare_coordinated_registry(root)
    declare_runtime_mutation(root, task_id)
    _registry, store, dispatcher = setup(root, tmp_path, monkeypatch)

    intent = dispatcher.claim_intent(
        "operator",
        ("repository",),
        task_id=task_id,
        break_glass=True,
    )["intent"]
    intent["operator_approval"]["level"] = "operator"
    intent["intent_sha256"] = bureau_v2.coordinated_claim_intent_sha256(intent)
    binding, database = coordinated_lease_database(tmp_path / "leases", intent)

    with pytest.raises(StateError, match="intent differs from issued identity"):
        dispatcher.commit_claim_intent(intent, binding, resource_db=database)

    assert store.list_runs() == []

def test_coordinated_runtime_claim_rejects_rehashed_scope_widening(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1, mode="write")
    task_id = prepare_coordinated_registry(root)
    declare_runtime_mutation(root, task_id)
    _registry, store, dispatcher = setup(root, tmp_path, monkeypatch)

    intent = dispatcher.claim_intent(
        "operator",
        ("repository",),
        task_id=task_id,
        break_glass=True,
    )["intent"]
    intent["operator_approval"]["scope"] = []
    intent["intent_sha256"] = bureau_v2.coordinated_claim_intent_sha256(intent)
    binding, database = coordinated_lease_database(tmp_path / "leases", intent)

    with pytest.raises(StateError, match="intent differs from issued identity"):
        dispatcher.commit_claim_intent(intent, binding, resource_db=database)

    assert store.list_runs() == []


def test_coordinated_runtime_claim_rejects_rehashed_reviewer_drift(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1, mode="write")
    task_id = prepare_coordinated_registry(root)
    declare_runtime_mutation(root, task_id)
    _registry, store, dispatcher = setup(root, tmp_path, monkeypatch)

    intent = dispatcher.claim_intent(
        "operator",
        ("repository",),
        task_id=task_id,
        break_glass=True,
    )["intent"]
    intent["operator_approval"]["reviewer"] = "other-worker"
    intent["intent_sha256"] = bureau_v2.coordinated_claim_intent_sha256(intent)
    binding, database = coordinated_lease_database(tmp_path / "leases", intent)

    with pytest.raises(StateError, match="intent differs from issued identity"):
        dispatcher.commit_claim_intent(intent, binding, resource_db=database)

    assert store.list_runs() == []


def test_coordinated_runtime_claim_rejects_worker_and_reviewer_transfer(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1, mode="write")
    task_id = prepare_coordinated_registry(root)
    declare_runtime_mutation(root, task_id)
    _registry, store, dispatcher = setup(root, tmp_path, monkeypatch)

    intent = dispatcher.claim_intent(
        "operator",
        ("repository",),
        task_id=task_id,
        break_glass=True,
    )["intent"]
    intent["worker_id"] = "other-worker"
    intent["operator_approval"]["reviewer"] = "other-worker"
    intent["intent_sha256"] = bureau_v2.coordinated_claim_intent_sha256(intent)
    binding, database = coordinated_lease_database(tmp_path / "leases", intent)

    with pytest.raises(StateError, match="intent differs from issued identity"):
        dispatcher.commit_claim_intent(intent, binding, resource_db=database)

    assert store.list_runs() == []


def test_coordinated_claim_handoff_does_not_require_absent_lease(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1, mode="write")
    task_id = prepare_coordinated_registry(root)
    _registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    monkeypatch.setattr(
        bureau_v2,
        "_coordinated_grabowski_resource_keys",
        lambda _resources, _task, _open_pr_scope: set(),
    )

    result = dispatcher.claim_intent(
        "operator",
        ("repository",),
        task_id=task_id,
        approved=True,
    )

    assert result["ready_supply"]["lease_required"] is False
    assert result["intent"]["required_resource_keys"] == []
    handoff = result["claim_commit_handoff"]
    assert handoff["lease_binding_required"] is False
    assert handoff["required_resource_keys"] == []
    assert handoff["required_lease_metadata"] is None
    assert handoff["minimum_remaining_seconds"] is None
    assert store.list_runs() == []


def test_coordinated_claim_commit_binds_live_lease_and_terminal_release(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1, mode="write")
    task_id = prepare_coordinated_registry(root)
    registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    intent = dispatcher.claim_intent(
        "operator",
        ("repository",),
        task_id=task_id,
        base_dir=tmp_path / "worktrees",
        approved=True,
    )["intent"]
    binding, database = coordinated_lease_database(tmp_path / "leases", intent)
    claimed = dispatcher.commit_claim_intent(intent, binding, resource_db=database)
    assert claimed["status"] == "claimed"
    assert claimed["run"]["run_id"] == intent["run_id"]
    assert claimed["envelope"]["claim_intent"]["intent_sha256"] == intent["intent_sha256"]
    assert claimed["envelope"]["lease_binding"]["owner_id"] == intent["lease_owner_id"]
    assert claimed["envelope"]["lease_binding"]["resource_lease_contract_version"] == "1"
    historical_envelope = json.loads(json.dumps(claimed["envelope"]))
    historical_envelope["lease_binding"].pop("resource_lease_contract_version")
    historical_envelope["lease_binding"]["lease_binding_sha256"] = "0" * 64
    registry.schemas.validate(
        "execution-envelope", historical_envelope, "historical-v1-envelope"
    )
    active = coordinated_claim_status(store, intent["run_id"], resource_db=database)
    assert active["lease"]["status"] == "active-bound"
    terminal = fail_run(store, intent["run_id"], "test close")
    assert terminal["lease_release"]["required"] is True
    after = coordinated_claim_status(store, intent["run_id"], resource_db=database)
    assert after["lease"]["status"] == "terminal-release-pending"
    assert store.run(intent["run_id"])["reservations"] == []
    assert registry.tasks[task_id].state == "ready"


def test_coordinated_claim_missing_lease_never_creates_run(registry_factory, tmp_path, monkeypatch):
    root = registry_factory(1, mode="write")
    task_id = prepare_coordinated_registry(root)
    _registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    intent = dispatcher.claim_intent("operator", ("repository",), task_id=task_id, approved=True)[
        "intent"
    ]
    missing = {intent["required_resource_keys"][0]}
    binding, database = coordinated_lease_database(tmp_path / "leases", intent, omit=missing)
    with pytest.raises(StateError, match="lease-resources-missing"):
        dispatcher.commit_claim_intent(intent, binding, resource_db=database)
    assert store.list_runs() == []
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM workers").fetchone()[0] == 0


def test_coordinated_claim_rejects_task_drift_after_intent(registry_factory, tmp_path, monkeypatch):
    root = registry_factory(1, mode="write")
    task_id = prepare_coordinated_registry(root)
    _registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    intent = dispatcher.claim_intent("operator", ("repository",), task_id=task_id, approved=True)[
        "intent"
    ]
    binding, database = coordinated_lease_database(tmp_path / "leases", intent)
    task_path = root / f"registry/tasks/{task_id}.json"
    task = json.loads(task_path.read_text())
    task["title"] = "drifted after intent"
    task_path.write_text(json.dumps(task))
    changed = Registry.load(root)
    changed_dispatcher = Dispatcher(changed, store)
    with pytest.raises(StateError, match=r"runtime truth changed|task changed"):
        changed_dispatcher.commit_claim_intent(intent, binding, resource_db=database)
    assert store.list_runs() == []


def test_coordinated_workspace_failure_terminalizes_claim(registry_factory, tmp_path, monkeypatch):
    root = registry_factory(1, mode="write")
    task_id = prepare_coordinated_registry(root)
    _registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    intent = dispatcher.claim_intent(
        "operator",
        ("repository",),
        task_id=task_id,
        base_dir=tmp_path / "worktrees",
        approved=True,
    )["intent"]
    binding, database = coordinated_lease_database(tmp_path / "leases", intent)
    destination = Path(intent["workspace"]["workspace_path"])
    destination.mkdir(parents=True)
    with pytest.raises(StateError, match="not registered"):
        dispatcher.checkout_claim_intent(intent, binding, resource_db=database)
    run = store.run(intent["run_id"])
    assert run["state"] == "failed"
    assert run["reservations"] == []
    status = coordinated_claim_status(store, intent["run_id"], resource_db=database)
    assert status["release"]["required"] is True
    assert status["lease"]["status"] == "terminal-release-pending"


def test_coordinated_claim_intent_tamper_is_rejected(registry_factory, tmp_path, monkeypatch):
    root = registry_factory(1, mode="write")
    task_id = prepare_coordinated_registry(root)
    _registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    intent = dispatcher.claim_intent("operator", ("repository",), task_id=task_id, approved=True)[
        "intent"
    ]
    binding, database = coordinated_lease_database(tmp_path / "leases", intent)
    intent["worker_id"] = "tampered"
    with pytest.raises(StateError, match="intent digest mismatch"):
        dispatcher.commit_claim_intent(intent, binding, resource_db=database)
    assert store.list_runs() == []


def test_coordinated_claim_cli_contract_parses_exact_surfaces():
    intent_args = bureau_cli.parser().parse_args(
        [
            "claim-intent",
            "--worker",
            "operator",
            "--task-id",
            "TASK-1",
            "--capability",
            "repository",
            "--approve",
        ]
    )
    assert intent_args.command == "claim-intent"
    assert intent_args.approve is True
    assert intent_args.break_glass is False

    keyed_intent_args = bureau_cli.parser().parse_args(
        [
            "claim-intent",
            "--worker",
            "operator",
            "--idempotency-key",
            "request-1",
        ]
    )
    assert keyed_intent_args.idempotency_key == "request-1"
    readback_args = bureau_cli.parser().parse_args(
        ["claim-intent-readback", "--idempotency-key", "request-1"]
    )
    assert readback_args.command == "claim-intent-readback"
    assert readback_args.idempotency_key == "request-1"

    break_glass_args = bureau_cli.parser().parse_args(
        [
            "claim-intent",
            "--worker",
            "operator",
            "--task-id",
            "TASK-1",
            "--capability",
            "repository",
            "--break-glass",
        ]
    )
    assert break_glass_args.approve is False
    assert break_glass_args.break_glass is True

    with pytest.raises(SystemExit):
        bureau_cli.parser().parse_args(
            [
                "claim-intent",
                "--worker",
                "operator",
                "--approve",
                "--break-glass",
            ]
        )
    commit_args = bureau_cli.parser().parse_args(
        [
            "claim-commit",
            "--intent",
            "intent.json",
            "--lease-binding",
            "lease.json",
            "--workspace",
        ]
    )
    assert commit_args.command == "claim-commit"
    assert commit_args.workspace is True
    status_args = bureau_cli.parser().parse_args(
        ["claim-coordination-status", "BUR-RUN-20260724T000000Z-0000000000"]
    )
    assert status_args.command == "claim-coordination-status"

    with pytest.raises(SystemExit):
        bureau_cli.parser().parse_args(
            [
                "claim-commit",
                "--intent",
                "intent.json",
                "--resource-db",
                "/tmp/forged-resources.sqlite3",
            ]
        )
    with pytest.raises(SystemExit):
        bureau_cli.parser().parse_args(
            [
                "claim-coordination-status",
                "BUR-RUN-20260724T000000Z-0000000000",
                "--resource-db",
                "/tmp/forged-resources.sqlite3",
            ]
        )


def test_coordinated_claim_rejects_workspace_source_head_drift(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1, mode="write")
    task_id = prepare_coordinated_registry(root)
    _registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    fixed_runtime_truth = {
        "schema_version": 1,
        "execution_blocked": False,
        "status": "clear",
    }
    monkeypatch.setattr(dispatcher, "_runtime_execution_truth", lambda: fixed_runtime_truth)
    intent = dispatcher.claim_intent(
        "operator",
        ("repository",),
        task_id=task_id,
        base_dir=tmp_path / "worktrees",
        approved=True,
    )["intent"]
    binding, database = coordinated_lease_database(tmp_path / "leases", intent)
    (root / "head-drift.txt").write_text("drift", encoding="utf-8")
    git_output(root, "add", "head-drift.txt")
    git_output(root, "commit", "-m", "head drift")
    with pytest.raises(StateError, match="workspace changed after intent"):
        dispatcher.commit_claim_intent(intent, binding, resource_db=database)
    assert store.list_runs() == []
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM workers").fetchone()[0] == 0


def test_coordinated_claim_rejects_rehashed_workspace_baseline_tamper(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1, mode="write")
    task_id = prepare_coordinated_registry(root)
    _registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    intent = dispatcher.claim_intent(
        "operator",
        ("repository",),
        task_id=task_id,
        base_dir=tmp_path / "worktrees",
        approved=True,
    )["intent"]
    intent["workspace"]["baseline_commit"] = "0" * 40
    intent["intent_sha256"] = bureau_v2.coordinated_claim_intent_sha256(intent)
    binding, database = coordinated_lease_database(tmp_path / "leases", intent)
    with pytest.raises(StateError, match="intent differs from issued identity"):
        dispatcher.commit_claim_intent(intent, binding, resource_db=database)
    assert store.list_runs() == []


def test_coordinated_active_run_surfaces_live_lease_drift(registry_factory, tmp_path, monkeypatch):
    root = registry_factory(1, mode="write")
    task_id = prepare_coordinated_registry(root)
    _registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    intent = dispatcher.claim_intent("operator", ("repository",), task_id=task_id, approved=True)[
        "intent"
    ]
    binding, database = coordinated_lease_database(tmp_path / "leases", intent)
    dispatcher.commit_claim_intent(intent, binding, resource_db=database)
    connection = sqlite3.connect(database)
    connection.execute("DELETE FROM leases")
    connection.commit()
    connection.close()
    status = coordinated_claim_status(store, intent["run_id"], resource_db=database)
    assert status["blocking"] is True
    assert status["lease"]["status"] == "active-binding-drift"
    assert status["lease"]["error"]["code"] == "lease-resources-missing"


def test_coordinated_claim_retry_recovers_after_intent_expiry_and_reports_drift(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1, mode="write")
    task_id = prepare_coordinated_registry(root)
    _registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    intent = dispatcher.claim_intent("operator", ("repository",), task_id=task_id, approved=True)[
        "intent"
    ]
    binding, database = coordinated_lease_database(tmp_path / "leases", intent)
    first = dispatcher.commit_claim_intent(intent, binding, resource_db=database)
    assert first["status"] == "claimed"

    real_datetime = bureau_v2.datetime

    class FutureDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime.fromtimestamp(intent["expires_at_unix"] + 60, tz=tz)

    monkeypatch.setattr(bureau_v2, "datetime", FutureDateTime)
    retry = dispatcher.commit_claim_intent(intent, binding, resource_db=database)
    assert retry["status"] == "existing-assignment"
    assert retry["blocking"] is False
    assert retry["lease"]["status"] == "active-bound"
    assert len(store.list_runs()) == 1

    connection = sqlite3.connect(database)
    connection.execute("DELETE FROM leases")
    connection.commit()
    connection.close()
    drifted = dispatcher.commit_claim_intent(intent, binding, resource_db=database)
    assert drifted["status"] == "existing-assignment"
    assert drifted["blocking"] is True
    assert drifted["lease"]["status"] == "active-binding-drift"
    assert len(store.list_runs()) == 1


def test_claim_next_rechecks_initiative_state_inside_transaction(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    _registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    store.set_initiative_state("BUR-TEST-001", "completed")

    with pytest.raises(NoEligibleTask, match="initiative state is completed"):
        dispatcher.claim_next(
            "stale-initiative-worker",
            ("repository",),
            reconcile_first=False,
        )

    assert store.list_runs() == []


def test_claim_next_binds_envelope_to_fresh_initiative_plan(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    _registry, _store, dispatcher = setup(root, tmp_path, monkeypatch)
    initiative_path = root / "registry/initiatives/main.json"
    initiative = json.loads(initiative_path.read_text())
    initiative["current_plan"] = {
        "repository": root.name,
        "path": "docs/plan.md",
        "commit": "1" * 40,
        "document_sha256": "2" * 64,
    }
    initiative_path.write_text(json.dumps(initiative))

    claimed = dispatcher.claim_next(
        "fresh-plan-worker", ("repository",), reconcile_first=False
    )
    fresh_registry = Registry.load(root)

    assert claimed["envelope"]["plan"] == initiative["current_plan"]
    assert claimed["envelope"]["plan_sha256"] == plan_sha256(
        fresh_registry, "BUR-TEST-001"
    )
    assert claimed["envelope"]["baseline_commit"] == "1" * 40


def test_claim_intent_binds_plan_and_workspace_to_fresh_initiative(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1, mode="write")
    task_id = prepare_coordinated_registry(root)
    _registry, _store, dispatcher = setup(root, tmp_path, monkeypatch)
    fixed_runtime_truth = {
        "schema_version": 1,
        "execution_blocked": False,
        "status": "clear",
    }
    monkeypatch.setattr(dispatcher, "_runtime_execution_truth", lambda: fixed_runtime_truth)
    monkeypatch.setattr(
        bureau_v2,
        "_coordinated_grabowski_resource_keys",
        lambda _resources, _task, _open_pr_scope: set(),
    )
    head = git_output(root, "rev-parse", "HEAD")
    initiative_path = root / "registry/initiatives/main.json"
    initiative = json.loads(initiative_path.read_text())
    initiative["current_plan"] = {
        "repository": root.name,
        "path": "docs/plan.md",
        "commit": head,
        "document_sha256": "3" * 64,
    }
    initiative_path.write_text(json.dumps(initiative))

    issued = dispatcher.claim_intent(
        "fresh-plan-operator",
        ("repository",),
        task_id=task_id,
        approved=True,
        approval_source="fresh-plan-regression",
    )
    fresh_registry = Registry.load(root)

    assert issued["intent"]["plan_sha256"] == plan_sha256(
        fresh_registry, "BUR-TEST-001"
    )
    assert issued["intent"]["workspace"]["baseline_commit"] == head
    claimed = dispatcher.commit_claim_intent(issued["intent"], None)
    assert claimed["envelope"]["plan"] == initiative["current_plan"]
    assert claimed["envelope"]["plan_sha256"] == issued["intent"]["plan_sha256"]


def test_commit_claim_intent_rechecks_fresh_initiative_plan(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1, mode="write")
    task_id = prepare_coordinated_registry(root)
    head = git_output(root, "rev-parse", "HEAD")
    initiative_path = root / "registry/initiatives/main.json"
    initiative = json.loads(initiative_path.read_text())
    initiative["current_plan"] = {
        "repository": root.name,
        "path": "docs/plan.md",
        "commit": head,
        "document_sha256": "4" * 64,
    }
    initiative_path.write_text(json.dumps(initiative))
    _registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    fixed_runtime_truth = {
        "schema_version": 1,
        "execution_blocked": False,
        "status": "clear",
    }
    monkeypatch.setattr(dispatcher, "_runtime_execution_truth", lambda: fixed_runtime_truth)
    monkeypatch.setattr(
        bureau_v2,
        "_coordinated_grabowski_resource_keys",
        lambda _resources, _task, _open_pr_scope: set(),
    )
    intent = dispatcher.claim_intent(
        "fresh-plan-commit-operator",
        ("repository",),
        task_id=task_id,
        approved=True,
        approval_source="fresh-plan-commit-regression",
    )["intent"]
    initiative["current_plan"]["commit"] = "5" * 40
    initiative_path.write_text(json.dumps(initiative))

    with pytest.raises(StateError, match="plan changed after intent"):
        dispatcher.commit_claim_intent(intent, None)

    assert store.list_runs() == []


def test_commit_claim_intent_rechecks_initiative_state_inside_transaction(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1, mode="write")
    task_id = prepare_coordinated_registry(root)
    _registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    monkeypatch.setattr(
        bureau_v2,
        "_coordinated_grabowski_resource_keys",
        lambda _resources, _task, _open_pr_scope: set(),
    )

    issued = dispatcher.claim_intent(
        "stale-initiative-operator",
        ("repository",),
        task_id=task_id,
        approved=True,
        approval_source="initiative-state-race-regression",
        idempotency_key="initiative-state-race-regression",
    )
    store.set_initiative_state("BUR-TEST-001", "completed")

    with pytest.raises(StateError, match="initiative state is completed"):
        dispatcher.commit_claim_intent(issued["intent"], None)

    assert store.list_runs() == []


def _insert_runtime_closeout_lease(
    database: Path,
    *,
    state_root: Path,
    run_id: str,
    task_id: str,
) -> tuple[str, str]:
    resource_key = f"path:{(state_root.resolve() / 'runtime-closeout' / run_id)}"
    owner_id = f"bureau-runtime-closeout:{run_id}"
    metadata = {
        "task_id": task_id,
        "run_id": run_id,
        "operation": "runtime-closeout",
    }
    metadata_json = json.dumps(
        metadata,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.sha256(metadata_json.encode()).hexdigest()
    now = int(time.time())
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO leases VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
        (
            resource_key,
            owner_id,
            "runtime closeout test",
            now - 10,
            now - 10,
            now + 3600,
            digest,
            metadata_json,
        ),
    )
    connection.commit()
    connection.close()
    return resource_key, owner_id


def _prepare_runtime_closeout_case(
    registry_factory,
    tmp_path: Path,
    monkeypatch,
    *,
    with_closeout_lease: bool = True,
    authenticate_evidence: bool = True,
    with_runtime_refresh_authority: bool = False,
):
    from bureau import runtime_identity as runtime_identity_module

    root = registry_factory(1, mode="write")
    task_id = prepare_coordinated_registry(root)
    if with_runtime_refresh_authority:
        (root / "registry/resources/runtime.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "id": "component.bureau.runtime",
                    "type": "component",
                    "parent": "root",
                }
            ),
            encoding="utf-8",
        )
        task_path = root / "registry" / "tasks" / f"{task_id}.json"
        task_raw = json.loads(task_path.read_text(encoding="utf-8"))
        task_raw.setdefault("metadata", {})["runtime_refresh_authority"] = {
            "schema_version": 1,
            "mode": "single-use-target-bound",
            "single_use": True,
            "required_action_class": "runtime_mutation",
            "required_approval_level": "break_glass",
            "required_claim_resource": "component.bureau.runtime",
            "required_task_state": ["ready", "active"],
            "target_binding": "candidate.target_sha256",
            "forbid_foreign_task_substitution": True,
            "forbid_historical_target_reuse": True,
            "successor_task_required_after_terminal": True,
        }
        task_raw["execution"]["approval"] = {
            "action_class": "runtime_mutation",
            "required_level": "break_glass",
            "note": "test-only runtime refresh authority",
        }
        task_raw["claims"] = [
            {
                "resource": "component.bureau.runtime",
                "mode": "write",
                "isolation": "worktree",
            }
        ]
        task_path.write_text(json.dumps(task_raw), encoding="utf-8")
        git_output(
            root,
            "add",
            str(task_path.relative_to(root)),
            "registry/resources/runtime.json",
        )
        git_output(root, "commit", "-m", "add runtime refresh authority")
        git_output(
            root,
            "update-ref",
            "refs/remotes/origin/main",
            git_output(root, "rev-parse", "HEAD"),
        )
    deployed_commit = git_output(root, "rev-parse", "HEAD")
    _registry, store, dispatcher = setup(root, tmp_path, monkeypatch)
    intent = dispatcher.claim_intent(
        "operator",
        ("repository",),
        task_id=task_id,
        base_dir=tmp_path / "worktrees",
        approved=not with_runtime_refresh_authority,
        break_glass=with_runtime_refresh_authority,
    )["intent"]
    binding, database = coordinated_lease_database(
        tmp_path / "leases", intent
    )
    claimed = dispatcher.commit_claim_intent(
        intent, binding, resource_db=database
    )
    run_id = intent["run_id"]

    if with_closeout_lease:
        _insert_runtime_closeout_lease(
            database,
            state_root=store.state_root,
            run_id=run_id,
            task_id=task_id,
        )

    snapshot_root = tmp_path / "runtime-registry-snapshot"
    shutil.copytree(root, snapshot_root, ignore=shutil.ignore_patterns(".git"))

    lag_marker = root / "deploy-lag.txt"
    lag_marker.write_text("main advanced after deployed runtime\n", encoding="utf-8")
    git_output(root, "add", lag_marker.name)
    git_output(root, "commit", "-m", "advance main beyond deployed runtime")
    current_head = git_output(root, "rev-parse", "HEAD")
    git_output(
        root,
        "update-ref",
        "refs/remotes/origin/main",
        current_head,
    )
    assert current_head != deployed_commit
    assert git_output(root, "status", "--porcelain") == ""

    context = {
        "status": "ready",
        "source_commit": deployed_commit,
        "release_id": "test-deployed-release",
        "module_path": str(tmp_path / "release/src/bureau/runtime_identity.py"),
        "launcher_path": str(tmp_path / "bin/bureau"),
        "manifest_path": str(tmp_path / "deployment-manifest.json"),
        "canonical_registry_root": str(snapshot_root),
        "canonical_registry_tree_sha256": "f" * 64,
    }
    monkeypatch.setattr(
        runtime_identity_module,
        "deployed_runtime_closeout_context",
        lambda *, state_path=None: context,
    )

    def exact_identity(registry_root, *, state_path=None, module_path=None):
        exact = Path(registry_root).resolve() == snapshot_root.resolve()
        return {
            "manifest": {"valid": exact},
            "compatibility": {
                "status": "canonical-read-only" if exact else "stale",
                "mutation_allowed": False,
                "reason_codes": ["canonical-registry-read-only"] if exact else ["test-mismatch"],
            },
            "registry": {
                "role": "canonical-runtime-snapshot" if exact else None,
                "head": deployed_commit if exact else current_head,
                "dirty": False,
            },
        }

    monkeypatch.setattr(
        runtime_identity_module,
        "bureau_runtime_identity",
        exact_identity,
    )

    observation_scope = f"test:{task_id}:proof"
    evidence_item = {
        "schema_version": 1,
        "kind": closure_observer.EVIDENCE_KIND,
        "criterion_id": "proof",
        "evidence_type": "manual_observation",
        "source": {
            "authority": "manual",
            "reference": "operator:runtime-closeout-test",
        },
        "observed_at": legacy.utc_now(),
        "revision": {
            "task_sha256": claimed["run"]["task_sha256"],
            "plan_sha256": claimed["run"]["plan_sha256"],
            "observation_scope": observation_scope,
        },
        "facts": {
            "accepted": True,
            "observer": "evidence-producer",
            "observation": "runtime closeout behavior confirmed",
            "observation_scope": observation_scope,
        },
    }
    evidence_directory = store.state_root / closure_observer.EVIDENCE_DIRECTORY
    evidence_directory.mkdir(parents=True, exist_ok=True)
    evidence_path = evidence_directory / f"{run_id}.json"
    evidence_bundle = {
        "schema_version": 1,
        "kind": closure_observer.EVIDENCE_BUNDLE_KIND,
        "run_id": run_id,
        "task_id": task_id,
        "task_sha256": claimed["run"]["task_sha256"],
        "plan_sha256": claimed["run"]["plan_sha256"],
        "evidence": {"proof": evidence_item},
    }
    evidence_path.write_text(json.dumps(evidence_bundle), encoding="utf-8")
    if authenticate_evidence:
        closure_observer.record_manual_acceptance_authentication(
            store,
            run_id,
            "proof",
            expected_evidence_sha256=legacy.sha256_json(evidence_item),
            reviewer="independent-reviewer",
        )
    return {
        "root": root,
        "store": store,
        "intent": intent,
        "claimed": claimed,
        "database": database,
        "run_id": run_id,
        "task_id": task_id,
        "deployed_commit": deployed_commit,
        "current_head": current_head,
        "snapshot_root": snapshot_root,
        "evidence_path": evidence_path,
        "evidence_bundle": evidence_bundle,
        "runtime_identity_module": runtime_identity_module,
        "initial_state": claimed["run"]["state"],
    }


def _apply_runtime_refresh_authority_lifecycle(
    case, *, consume_source: str = "runtime-refresh-authority-consumption"
):
    case["store"].import_registry_task_specs(Registry.load(case["root"]))
    baseline = case["store"].task_spec(case["task_id"])
    assert baseline is not None
    baseline_spec = json.loads(json.dumps(baseline["spec"]))
    baseline_sha256 = baseline["spec_sha256"]
    baseline_revision = baseline["revision"]
    intent_sha256 = "1" * 64
    target_sha256 = "2" * 64
    result_sha256 = "3" * 64
    binding = {
        "schema_version": 1,
        "kind": "bureau_runtime_refresh_authority_target_binding",
        "task_id": case["task_id"],
        "authority_revision": baseline_revision,
        "authority_spec_sha256": baseline_sha256,
        "intent_sha256": intent_sha256,
        "target_sha256": target_sha256,
        "bound_at": "2026-08-19T14:57:00Z",
    }
    bound_spec = json.loads(json.dumps(baseline_spec))
    bound_spec["metadata"]["runtime_refresh_authority"]["target_binding_receipt"] = binding
    bound = case["store"].put_task_spec(
        bound_spec,
        idempotency_key=f"runtime-refresh-bind:{case['task_id']}:{intent_sha256}",
        expected_revision=baseline_revision,
        source="runtime-refresh-authority-target-binding",
    )
    consumption = {
        "schema_version": 1,
        "kind": "bureau_runtime_refresh_authority_consumption",
        "task_id": case["task_id"],
        "authority_revision": baseline_revision,
        "authority_spec_sha256": baseline_sha256,
        "intent_sha256": intent_sha256,
        "target_sha256": target_sha256,
        "result_sha256": result_sha256,
        "status": "consumed",
        "consumed_at": "2026-08-19T14:57:06Z",
    }
    consumed_spec = json.loads(json.dumps(bound["spec"]))
    consumed_spec["metadata"]["runtime_refresh_authority"]["consumption"] = consumption
    consumed = case["store"].put_task_spec(
        consumed_spec,
        idempotency_key=f"runtime-refresh-consume:{case['task_id']}:{result_sha256}",
        expected_revision=bound["revision"],
        source=consume_source,
    )
    return {
        "baseline": baseline,
        "bound": bound,
        "consumed": consumed,
        "intent_sha256": intent_sha256,
        "target_sha256": target_sha256,
        "result_sha256": result_sha256,
    }


def test_normal_complete_stays_fail_closed_during_deploy_lag(
    registry_factory, tmp_path, monkeypatch, capsys
):
    case = _prepare_runtime_closeout_case(
        registry_factory, tmp_path, monkeypatch
    )
    evidence_path = tmp_path / "normal-completion-evidence.json"
    evidence_path.write_text(
        json.dumps({"proof": {"result": "passed"}}),
        encoding="utf-8",
    )
    blocked_identity = {
        "registry": {
            "bureau_project": True,
            "dirty": False,
            "root": str(case["root"]),
        },
        "compatibility": {
            "status": "stale",
            "mutation_allowed": False,
            "reason_codes": ["release-registry-identity-mismatch"],
        },
    }
    monkeypatch.setattr(
        bureau_cli, "bureau_runtime_identity", lambda *args, **kwargs: blocked_identity
    )

    result = bureau_cli.main(
        [
            "--root",
            str(case["root"]),
            "--state-root",
            str(case["store"].state_root),
            "--json",
            "complete",
            case["run_id"],
            "--evidence",
            str(evidence_path),
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert result == 2
    assert output["result"]["status"] == "stale-runtime-blocked"
    assert output["result"]["reason_codes"] == [
        "release-registry-identity-mismatch"
    ]
    assert output["runtime_identity"]["compatibility"]["reason_codes"] == [
        "release-registry-identity-mismatch"
    ]
    assert case["store"].run(case["run_id"])["state"] == case["initial_state"]


def test_runtime_closeout_uses_typed_closure_and_releases_own_leases(
    registry_factory, tmp_path, monkeypatch
):
    case = _prepare_runtime_closeout_case(
        registry_factory, tmp_path, monkeypatch
    )
    preexisting_closeout_root = (
        case["store"].state_root / "runtime-closeout" / case["run_id"]
    )
    preexisting_closeout_root.mkdir(parents=True)

    result = bureau_v2.runtime_closeout(
        case["store"],
        case["run_id"],
        case["evidence_path"],
        resource_db=case["database"],
    )

    assert result["status"] == "succeeded"
    assert result["deployed_source_commit"] == case["deployed_commit"]
    assert result["exact_registry_head"] == case["deployed_commit"]
    assert result["exact_registry_dirty"] is False
    assert result["canonical_registry_root"] == str(case["snapshot_root"])
    assert result["authenticated_criterion_ids"] == ["proof"]
    assert result["authoritative_run_state"] == "succeeded"
    assert result["temporary_resources_removed"] is True
    assert case["store"].run(case["run_id"])["state"] == "succeeded"
    connection = sqlite3.connect(case["database"])
    assert connection.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == 0
    connection.close()
    assert not preexisting_closeout_root.exists()


def test_runtime_closeout_prefers_state_store_task_over_older_runtime_registry(
    registry_factory, tmp_path, monkeypatch
):
    case = _prepare_runtime_closeout_case(
        registry_factory, tmp_path, monkeypatch
    )
    case["store"].import_registry_task_specs(Registry.load(case["root"]))
    current = case["store"].task_spec(case["task_id"])
    assert current is not None
    assert (
        task_revision_sha256(current["spec"])
        == case["claimed"]["run"]["task_sha256"]
    )

    snapshot_task_path = (
        case["snapshot_root"] / "registry" / "tasks" / f"{case['task_id']}.json"
    )
    stale_snapshot_task = json.loads(snapshot_task_path.read_text())
    stale_snapshot_task["title"] = "older immutable runtime Registry projection"
    snapshot_task_path.write_text(json.dumps(stale_snapshot_task), encoding="utf-8")
    assert (
        task_revision_sha256(stale_snapshot_task)
        != case["claimed"]["run"]["task_sha256"]
    )

    result = bureau_v2.runtime_closeout(
        case["store"],
        case["run_id"],
        case["evidence_path"],
        resource_db=case["database"],
    )

    assert result["status"] == "succeeded"
    assert case["store"].run(case["run_id"])["state"] == "succeeded"


def test_runtime_closeout_accepts_canonical_runtime_refresh_authority_receipts(
    registry_factory, tmp_path, monkeypatch
):
    case = _prepare_runtime_closeout_case(
        registry_factory,
        tmp_path,
        monkeypatch,
        with_runtime_refresh_authority=True,
    )
    lifecycle = _apply_runtime_refresh_authority_lifecycle(case)
    assert lifecycle["consumed"]["revision"] == lifecycle["baseline"]["revision"] + 2
    assert (
        task_revision_sha256(lifecycle["consumed"]["spec"])
        != case["claimed"]["run"]["task_sha256"]
    )

    result = bureau_v2.runtime_closeout(
        case["store"],
        case["run_id"],
        case["evidence_path"],
        resource_db=case["database"],
    )

    assert result["status"] == "succeeded"
    assert case["store"].run(case["run_id"])["state"] == "succeeded"


def test_runtime_closeout_rejects_semantic_drift_after_runtime_refresh_receipts(
    registry_factory, tmp_path, monkeypatch
):
    case = _prepare_runtime_closeout_case(
        registry_factory,
        tmp_path,
        monkeypatch,
        with_runtime_refresh_authority=True,
    )
    lifecycle = _apply_runtime_refresh_authority_lifecycle(case)
    changed = json.loads(json.dumps(lifecycle["consumed"]["spec"]))
    changed["title"] = "concurrent semantic drift after runtime refresh"
    case["store"].put_task_spec(
        changed,
        idempotency_key=f"test-runtime-closeout-post-refresh-drift:{case['task_id']}",
        expected_revision=lifecycle["consumed"]["revision"],
        source="test semantic mutation after runtime refresh",
    )

    with pytest.raises(bureau_v2.RunStateConflict) as exc:
        bureau_v2.runtime_closeout(
            case["store"],
            case["run_id"],
            case["evidence_path"],
            resource_db=case["database"],
        )

    assert exc.value.code == "task-revision-changed"
    assert case["store"].run(case["run_id"])["state"] == case["initial_state"]


def test_runtime_closeout_rejects_forged_runtime_refresh_mutation_source(
    registry_factory, tmp_path, monkeypatch
):
    case = _prepare_runtime_closeout_case(
        registry_factory,
        tmp_path,
        monkeypatch,
        with_runtime_refresh_authority=True,
    )
    _apply_runtime_refresh_authority_lifecycle(
        case, consume_source="test-forged-runtime-refresh-consumption"
    )

    with pytest.raises(bureau_v2.RunStateConflict) as exc:
        bureau_v2.runtime_closeout(
            case["store"],
            case["run_id"],
            case["evidence_path"],
            resource_db=case["database"],
        )

    assert exc.value.code == "task-revision-changed"
    assert case["store"].run(case["run_id"])["state"] == case["initial_state"]


def test_runtime_closeout_rejects_true_state_store_task_drift_after_claim(
    registry_factory, tmp_path, monkeypatch
):
    case = _prepare_runtime_closeout_case(
        registry_factory, tmp_path, monkeypatch
    )
    case["store"].import_registry_task_specs(Registry.load(case["root"]))
    current = case["store"].task_spec(case["task_id"])
    assert current is not None
    changed = json.loads(json.dumps(current["spec"]))
    changed["title"] = "concurrent authoritative StateStore revision"
    case["store"].put_task_spec(
        changed,
        idempotency_key=f"test-runtime-closeout-drift:{case['task_id']}",
        expected_revision=current["revision"],
        source="test concurrent StateStore mutation",
    )

    with pytest.raises(bureau_v2.RunStateConflict) as exc:
        bureau_v2.runtime_closeout(
            case["store"],
            case["run_id"],
            case["evidence_path"],
            resource_db=case["database"],
        )

    assert exc.value.code == "task-revision-changed"
    assert exc.value.details["task_authority"] == "bureau-state-store-task-specs"
    assert exc.value.details["task_spec_revision"] == current["revision"] + 1
    assert case["store"].run(case["run_id"])["state"] == case["initial_state"]


def test_runtime_closeout_cli_routes_through_canonical_runtime_snapshot(
    registry_factory, tmp_path, monkeypatch, capsys
):
    case = _prepare_runtime_closeout_case(
        registry_factory, tmp_path, monkeypatch
    )
    canonical_identity = {
        "registry": {
            "bureau_project": True,
            "role": "canonical-runtime-snapshot",
            "root": str(case["snapshot_root"]),
            "head": case["deployed_commit"],
            "origin_main": case["deployed_commit"],
            "head_equals_origin_main": True,
            "dirty": False,
        },
        "manifest": {
            "valid": True,
            "source_commit": case["deployed_commit"],
            "canonical_registry": {
                "valid": True,
                "root": str(case["snapshot_root"]),
                "source_commit": case["deployed_commit"],
            },
        },
        "compatibility": {
            "status": "canonical-read-only",
            "mutation_allowed": False,
            "reason_codes": ["canonical-registry-read-only"],
        },
    }
    monkeypatch.setattr(
        bureau_cli,
        "resolve_registry_root",
        lambda _value: (case["snapshot_root"], "canonical-runtime-default"),
    )
    monkeypatch.setattr(
        bureau_cli,
        "bureau_runtime_identity",
        lambda *args, **kwargs: json.loads(json.dumps(canonical_identity)),
    )
    monkeypatch.setattr(
        bureau_cli,
        "runtime_closeout",
        lambda store, run_id, evidence_path: bureau_v2.runtime_closeout(
            store,
            run_id,
            evidence_path,
            resource_db=case["database"],
        ),
    )

    result = bureau_cli.main(
        [
            "--state-root",
            str(case["store"].state_root),
            "--json",
            "complete",
            case["run_id"],
            "--evidence",
            str(case["evidence_path"]),
            "--exact-runtime",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert result == 0
    assert output["result"]["status"] == "succeeded"
    assert output["result"]["canonical_registry_root"] == str(case["snapshot_root"])
    assert case["store"].run(case["run_id"])["state"] == "succeeded"


def test_runtime_closeout_requires_canonical_evidence_path(
    registry_factory, tmp_path, monkeypatch
):
    case = _prepare_runtime_closeout_case(
        registry_factory, tmp_path, monkeypatch
    )
    copied = tmp_path / "copied-evidence.json"
    copied.write_text(
        json.dumps(case["evidence_bundle"]),
        encoding="utf-8",
    )

    with pytest.raises(bureau_v2.RunStateConflict) as exc:
        bureau_v2.runtime_closeout(
            case["store"],
            case["run_id"],
            copied,
            resource_db=case["database"],
        )
    assert exc.value.code == "runtime-closeout-evidence-path-invalid"
    assert case["store"].run(case["run_id"])["state"] == case["initial_state"]


def test_runtime_closeout_rejects_symlink_canonical_evidence(
    registry_factory, tmp_path, monkeypatch
):
    case = _prepare_runtime_closeout_case(
        registry_factory, tmp_path, monkeypatch
    )
    target = case["evidence_path"].with_name("evidence-target.json")
    case["evidence_path"].rename(target)
    case["evidence_path"].symlink_to(target)

    with pytest.raises(bureau_v2.RunStateConflict) as exc:
        bureau_v2.runtime_closeout(
            case["store"],
            case["run_id"],
            case["evidence_path"],
            resource_db=case["database"],
        )
    assert exc.value.code == "runtime-closeout-evidence-invalid"
    assert case["store"].run(case["run_id"])["state"] == case["initial_state"]


def test_runtime_closeout_rejects_revision_drifted_evidence_bundle(
    registry_factory, tmp_path, monkeypatch
):
    case = _prepare_runtime_closeout_case(
        registry_factory, tmp_path, monkeypatch
    )
    bundle = dict(case["evidence_bundle"])
    bundle["task_sha256"] = "0" * 64
    case["evidence_path"].write_text(json.dumps(bundle), encoding="utf-8")

    with pytest.raises(bureau_v2.RunStateConflict) as exc:
        bureau_v2.runtime_closeout(
            case["store"],
            case["run_id"],
            case["evidence_path"],
            resource_db=case["database"],
        )
    assert exc.value.code == "runtime-closeout-evidence-binding-invalid"
    assert case["store"].run(case["run_id"])["state"] == case["initial_state"]


def test_runtime_closeout_requires_independent_evidence_authentication(
    registry_factory, tmp_path, monkeypatch
):
    case = _prepare_runtime_closeout_case(
        registry_factory,
        tmp_path,
        monkeypatch,
        authenticate_evidence=False,
    )

    with pytest.raises(bureau_v2.RunStateConflict) as exc:
        bureau_v2.runtime_closeout(
            case["store"],
            case["run_id"],
            case["evidence_path"],
            resource_db=case["database"],
        )
    assert exc.value.code == "runtime-closeout-evidence-authentication-unavailable"
    assert case["store"].run(case["run_id"])["state"] == case["initial_state"]
    assert not (
        case["store"].state_root / "runtime-closeout" / case["run_id"]
    ).exists()


def test_runtime_closeout_missing_temporary_lease_stops_before_state_mutation(
    registry_factory, tmp_path, monkeypatch
):
    case = _prepare_runtime_closeout_case(
        registry_factory,
        tmp_path,
        monkeypatch,
        with_closeout_lease=False,
    )

    with pytest.raises(
        bureau_v2.RunStateConflict,
        match="temporary closeout lease",
    ) as exc:
        bureau_v2.runtime_closeout(
            case["store"],
            case["run_id"],
            case["evidence_path"],
            resource_db=case["database"],
        )
    assert exc.value.code == "runtime-closeout-temporary-lease-invalid"
    assert case["store"].run(case["run_id"])["state"] == case["initial_state"]


def test_runtime_closeout_missing_pickup_lease_stops_before_state_mutation(
    registry_factory, tmp_path, monkeypatch
):
    case = _prepare_runtime_closeout_case(
        registry_factory, tmp_path, monkeypatch
    )
    missing_key = case["intent"]["required_resource_keys"][0]
    connection = sqlite3.connect(case["database"])
    connection.execute(
        "DELETE FROM leases WHERE resource_key=?",
        (missing_key,),
    )
    connection.commit()
    connection.close()

    with pytest.raises(bureau_v2.RunStateConflict) as exc:
        bureau_v2.runtime_closeout(
            case["store"],
            case["run_id"],
            case["evidence_path"],
            resource_db=case["database"],
        )
    assert exc.value.code == "runtime-closeout-pickup-lease-invalid"
    assert case["store"].run(case["run_id"])["state"] == case["initial_state"]


def test_runtime_closeout_dirty_canonical_identity_stops_before_state_mutation(
    registry_factory, tmp_path, monkeypatch
):
    case = _prepare_runtime_closeout_case(
        registry_factory, tmp_path, monkeypatch
    )
    runtime_identity_module = case["runtime_identity_module"]
    monkeypatch.setattr(
        runtime_identity_module,
        "bureau_runtime_identity",
        lambda *args, **kwargs: {
            "manifest": {"valid": True},
            "compatibility": {
                "status": "canonical-read-only",
                "mutation_allowed": False,
                "reason_codes": ["canonical-registry-read-only"],
            },
            "registry": {
                "role": "canonical-runtime-snapshot",
                "head": case["deployed_commit"],
                "dirty": True,
            },
        },
    )

    with pytest.raises(bureau_v2.RunStateConflict) as exc:
        bureau_v2.runtime_closeout(
            case["store"],
            case["run_id"],
            case["evidence_path"],
            resource_db=case["database"],
        )
    assert exc.value.code == "runtime-closeout-exact-identity-mismatch"
    assert case["store"].run(case["run_id"])["state"] == case["initial_state"]


def test_runtime_closeout_exact_identity_mismatch_stops_before_state_mutation(
    registry_factory, tmp_path, monkeypatch
):
    case = _prepare_runtime_closeout_case(
        registry_factory, tmp_path, monkeypatch
    )
    runtime_identity_module = case["runtime_identity_module"]
    monkeypatch.setattr(
        runtime_identity_module,
        "bureau_runtime_identity",
        lambda *args, **kwargs: {
            "manifest": {"valid": False},
            "compatibility": {
                "status": "stale",
                "mutation_allowed": False,
                "reason_codes": ["release-registry-identity-mismatch"],
            },
            "registry": {
                "head": case["deployed_commit"],
                "dirty": False,
            },
        },
    )

    with pytest.raises(bureau_v2.RunStateConflict) as exc:
        bureau_v2.runtime_closeout(
            case["store"],
            case["run_id"],
            case["evidence_path"],
            resource_db=case["database"],
        )
    assert exc.value.code == "runtime-closeout-exact-identity-mismatch"
    assert case["store"].run(case["run_id"])["state"] == case["initial_state"]


@pytest.mark.parametrize(
    ("field", "expected_code"),
    [
        ("task_sha256", "task-revision-changed"),
        ("plan_sha256", "plan-revision-changed"),
    ],
)
def test_runtime_closeout_revision_drift_stops_before_state_mutation(
    registry_factory, tmp_path, monkeypatch, field, expected_code
):
    case = _prepare_runtime_closeout_case(
        registry_factory, tmp_path, monkeypatch
    )
    original = bureau_v2._authoritative_close_revision

    def drifted_revision(registry, task_id, *, run_id=None):
        revision = original(registry, task_id, run_id=run_id)
        return bureau_v2.replace(
            revision,
            **{field: "0" * 64},
        )

    monkeypatch.setattr(
        bureau_v2,
        "_authoritative_close_revision",
        drifted_revision,
    )

    with pytest.raises(bureau_v2.RunStateConflict) as exc:
        bureau_v2.runtime_closeout(
            case["store"],
            case["run_id"],
            case["evidence_path"],
            resource_db=case["database"],
        )
    assert exc.value.code == expected_code
    assert case["store"].run(case["run_id"])["state"] == case["initial_state"]

def test_runtime_closeout_cli_contract_parses_exact_runtime_options():
    args = bureau_cli.parser().parse_args(
        [
            "complete",
            "BUR-RUN-TEST",
            "--evidence",
            "/tmp/evidence.json",
            "--exact-runtime",
        ]
    )
    assert args.command == "complete"
    assert args.exact_runtime is True

    with pytest.raises(SystemExit):
        bureau_cli.parser().parse_args(
            [
                "complete",
                "BUR-RUN-TEST",
                "--evidence",
                "/tmp/evidence.json",
                "--exact-runtime",
                "--resource-db",
                "/tmp/fake-resources.sqlite3",
            ]
        )
