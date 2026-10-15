"""Regression tests for T044's intentionally blocked Bureau effect boundaries.

All effects operate on temporary StateStore instances; production is untouched.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

import pytest

from bureau import legacy, v2
from bureau.core import Dispatcher, Registry, StateError, StateStore
from bureau.v2 import plan_sha256

T044 = "WELTGEWEBE-OS-V1-T044"


def _assigned_t044(tmp_path: Path):
    registry = Registry.load(Path(__file__).resolve().parents[1])
    task = registry.tasks[T044]
    store = StateStore(tmp_path / "state.sqlite3", state_root=tmp_path)
    worker = "legacy-t044-effect-test"
    run_id = "BUR-RUN-20261014T180000Z-1122334455"
    envelope = {
        "run_id": run_id,
        "task_id": T044,
        "worker_id": worker,
        "task_sha256": task.sha256,
        "plan_sha256": plan_sha256(registry, task.initiative),
        "task": task.raw,
        "claims": [claim.as_dict() for claim in task.claims],
    }
    now = legacy.utc_now()
    store.register_worker(worker, "interactive-agent", tuple(task.capabilities))
    with store.immediate() as db:
        db.execute(
            "INSERT INTO runs (run_id,task_id,worker_id,attempt,state,task_sha256,"
            "plan_sha256,envelope_json,envelope_sha256,created_at,updated_at,heartbeat_at)"
            " VALUES (?,?,?,1,'assigned',?,?,?,?,?,?,?)",
            (
                run_id, T044, worker, task.sha256, envelope["plan_sha256"],
                legacy.canonical_json(envelope), legacy.sha256_json(envelope),
                now, now, now,
            ),
        )
        for claim in task.claims:
            db.execute(
                "INSERT INTO reservations (run_id,resource_id,mode,amount,created_at)"
                " VALUES (?,?,?,?,?)",
                (run_id, claim.resource, claim.mode, claim.amount, now),
            )
    dispatcher = Dispatcher(
        registry, store, open_pr_reservations_provider=lambda _registry: [],
    )
    return registry, store, dispatcher, task, worker, run_id, envelope


def _reserve_count(store: StateStore) -> int:
    with store.connect() as db:
        return db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0]


def test_direct_legacy_workspace_refuses_t044_before_git_or_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, store, _dispatcher, _task, _worker, run_id, _envelope = _assigned_t044(
        tmp_path
    )
    base_dir = tmp_path / "must-not-exist"

    def no_git(*_args, **_kwargs):
        raise AssertionError("unfenced legacy Git effect attempted")

    monkeypatch.setattr(legacy.subprocess, "run", no_git)
    before = _reserve_count(store)
    with pytest.raises(StateError, match="T044 legacy workspace blocked"):
        legacy.create_workspace(registry, store, run_id, base_dir=base_dir)
    assert not base_dir.exists()
    assert store.run(run_id)["workspace_path"] is None
    assert _reserve_count(store) == before


def test_direct_legacy_workspace_refuses_historical_t044(
    tmp_path: Path,
) -> None:
    registry, store, _dispatcher, _task, _worker, run_id, _envelope = _assigned_t044(
        tmp_path
    )
    # A historical worktree recorded in the DB must not bypass the fence.
    with store.immediate() as db:
        db.execute(
            "UPDATE runs SET workspace_path=? WHERE run_id=?",
            (str(tmp_path / "historical-worktree"), run_id),
        )
    with pytest.raises(StateError, match="T044 legacy workspace blocked"):
        legacy.create_workspace(registry, store, run_id)
    assert _reserve_count(store) > 0


def test_direct_legacy_handoff_refuses_t044_executable_payload(
    tmp_path: Path,
) -> None:
    registry, store, _dispatcher, _task, _worker, run_id, _envelope = _assigned_t044(
        tmp_path
    )
    with pytest.raises(StateError, match="T044 legacy handoff blocked"):
        legacy.grabowski_handoff(registry, store, run_id)
    assert store.run(run_id)["state"] == "assigned"
    assert _reserve_count(store) > 0


def test_uncoordinated_checkout_error_names_real_t044_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _registry, store, dispatcher, task, worker, run_id, envelope = _assigned_t044(
        tmp_path
    )
    monkeypatch.setattr(dispatcher, "_runtime_execution_truth", lambda: {})
    monkeypatch.setattr(dispatcher, "_runtime_execution_stop", lambda **_kw: None)
    monkeypatch.setattr(dispatcher, "reconcile", lambda: {})
    monkeypatch.setattr(
        dispatcher, "claim_next",
        lambda *_a, **_kw: {
            "status": "existing-assignment",
            "run": store.run(run_id),
            "envelope": envelope,
        },
    )
    monkeypatch.setattr(
        dispatcher, "_validate_t044_existing_assignment", lambda *_a, **_kw: None
    )
    with pytest.raises(StateError) as captured:
        dispatcher.checkout_next(worker, tuple(task.capabilities))
    message = str(captured.value)
    assert "T044 uncoordinated checkout blocked" in message
    assert "no new" in message.lower()
    assert "exact-run" in message
    assert "claim-commit --workspace" not in message
    assert store.run(run_id)["state"] == "assigned"
    assert _reserve_count(store) == len(task.claims)


def _lease_database(tmp_path: Path, intent: dict) -> tuple[dict, Path]:
    path = tmp_path / "runtime-leases.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.execute("INSERT INTO metadata VALUES ('schema_version','1')")
    db.execute("INSERT INTO metadata VALUES ('resource_lease_contract_version','1')")
    db.execute(
        "CREATE TABLE leases (resource_key TEXT PRIMARY KEY, owner_id TEXT NOT NULL, "
        "purpose TEXT NOT NULL, acquired_at_unix INTEGER NOT NULL, "
        "updated_at_unix INTEGER NOT NULL, expires_at_unix INTEGER NOT NULL, "
        "metadata_sha256 TEXT NOT NULL, metadata_json TEXT NOT NULL, "
        "reclaimed_from_owner TEXT)"
    )
    now = int(time.time())
    meta = json.dumps(
        {"task_id": intent["task_id"], "run_id": intent["run_id"],
         "claim_intent_sha256": intent["intent_sha256"]},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    digest = hashlib.sha256(meta.encode()).hexdigest()
    for key in intent["required_resource_keys"]:
        db.execute(
            "INSERT INTO leases VALUES (?,?,?,?,?,?,?,?,NULL)",
            (key, intent["lease_owner_id"], "isolated regression", now - 30,
             now - 30, now + 3600, digest, meta),
        )
    db.commit()
    db.close()
    path.chmod(0o600)
    return {"owner_id": intent["lease_owner_id"], "task_id": T044}, path


def test_direct_commit_rejects_new_t044_before_run_and_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = Registry.load(Path(__file__).resolve().parents[1])
    task = registry.tasks[T044]
    store = StateStore(tmp_path / "state.sqlite3", state_root=tmp_path)
    dispatcher = Dispatcher(
        registry, store, open_pr_reservations_provider=lambda _registry: [],
    )
    # Issue a real signed/hashed intent as a programmatic caller. Forcing the
    # preceding selection to pass only isolates the direct commit boundary.
    monkeypatch.setattr(dispatcher, "_runtime_execution_truth", lambda: {})
    monkeypatch.setattr(dispatcher, "_runtime_execution_stop", lambda **_kw: None)
    monkeypatch.setattr(dispatcher, "_ordered_tasks", lambda: [task])
    monkeypatch.setattr(dispatcher, "reasons", lambda *_a, **_kw: [])
    # Exercise the public non-workspace claim-commit path on real StateStore.
    # T044's declared working_repository names a local host and is absent
    # on GitHub runners; workspace planning is irrelevant to this path.
    monkeypatch.setattr(v2, "_planned_workspace", lambda *_a, **_kw: None)
    intent = dispatcher.claim_intent(
        "t044-programmatic-worker", tuple(task.capabilities),
        task_id=T044, approved=True,
    )["intent"]
    assert store.claim_intent_issuance(intent["run_id"]) is not None
    assert store.list_runs() == []
    assert _reserve_count(store) == 0
    lease_binding, lease_db = _lease_database(tmp_path, intent)
    with pytest.raises(StateError, match="T044 coordinated claim commit blocked"):
        dispatcher.commit_claim_intent(
            intent, lease_binding if intent["required_resource_keys"] else None,
            resource_db=lease_db,
        )
    assert store.list_runs() == []
    assert _reserve_count(store) == 0

def test_direct_legacy_dispatcher_refuses_t044_before_run_or_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy compatibility selection must not persist a T044 assignment."""
    registry = legacy.Registry.load(Path(__file__).resolve().parents[1])
    task = registry.tasks[T044]
    store = StateStore(tmp_path / "claim-state.sqlite3", state_root=tmp_path)
    dispatcher = legacy.Dispatcher(registry, store)
    # Force T044 to be the sole otherwise-eligible candidate. Exercise the
    # original real claim transaction, not a mocked StateStore.
    monkeypatch.setattr(registry, "ordered_tasks", lambda: [task])
    monkeypatch.setattr(dispatcher, "reasons", lambda *_a: [])
    monkeypatch.setattr(legacy, "default_state_dir", lambda: tmp_path / "envelopes")
    with pytest.raises(legacy.NoEligibleTask, match="T044 legacy claim-next blocked"):
        dispatcher.claim_next("legacy-t044-direct-worker", tuple(task.capabilities))
    assert store.list_runs() == []
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0
    assert not list((tmp_path / "envelopes").glob("*.json"))


def test_direct_legacy_dispatcher_still_claims_non_t044(
    registry_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A targeted T044 fence must preserve ordinary legacy compatibility."""
    root = registry_factory(1, mode="write")
    registry = legacy.Registry.load(root)
    store = StateStore(
        tmp_path / "ordinary" / "ordinary-state.sqlite3",
        state_root=tmp_path / "ordinary",
    )
    dispatcher = legacy.Dispatcher(registry, store)
    monkeypatch.setattr(legacy, "default_state_dir", lambda: tmp_path / "ordinary")
    result = dispatcher.claim_next("legacy-ordinary-worker", ("repository",))
    assert result["status"] == "claimed"
    assert result["run"]["task_id"] != T044
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 2


def test_t044_existing_coordinated_commit_never_reasserts_legacy_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing T044 run and reservations must remain read-only, not claimed."""
    registry = Registry.load(Path(__file__).resolve().parents[1])
    task = registry.tasks[T044]
    store = StateStore(tmp_path / "historical.sqlite3", state_root=tmp_path)
    dispatcher = Dispatcher(
        registry, store, open_pr_reservations_provider=lambda _registry: [],
    )
    monkeypatch.setattr(dispatcher, "_runtime_execution_truth", lambda: {})
    monkeypatch.setattr(dispatcher, "_runtime_execution_stop", lambda **_kw: None)
    monkeypatch.setattr(dispatcher, "_ordered_tasks", lambda: [task])
    monkeypatch.setattr(dispatcher, "reasons", lambda *_a, **_kw: [])
    # The historical commit-only route does not require a local Git worktree.
    monkeypatch.setattr(v2, "_planned_workspace", lambda *_a, **_kw: None)
    intent = dispatcher.claim_intent(
        "t044-existing-coordinated-worker", tuple(task.capabilities),
        task_id=T044, approved=True,
    )["intent"]
    assert store.claim_intent_issuance(intent["run_id"]) is not None
    now = legacy.utc_now()
    envelope = {
        "run_id": intent["run_id"],
        "task_id": T044,
        "worker_id": intent["worker_id"],
        "task_sha256": task.sha256,
        "plan_sha256": intent["plan_sha256"],
        "task": task.raw,
        "claims": [claim.as_dict() for claim in task.claims],
        "claim_intent": intent,
    }
    store.register_worker(
        intent["worker_id"], "interactive-agent", tuple(task.capabilities),
    )
    with store.immediate() as db:
        db.execute(
            "INSERT INTO runs (run_id,task_id,worker_id,attempt,state,task_sha256,"
            "plan_sha256,envelope_json,envelope_sha256,created_at,updated_at,heartbeat_at)"
            " VALUES (?,?,?,1,'assigned',?,?,?,?,?,?,?)",
            (
                intent["run_id"], T044, intent["worker_id"], task.sha256,
                intent["plan_sha256"], legacy.canonical_json(envelope),
                legacy.sha256_json(envelope), now, now, now,
            ),
        )
        for claim in task.claims:
            db.execute(
                "INSERT INTO reservations (run_id,resource_id,mode,amount,created_at)"
                " VALUES (?,?,?,?,?)",
                (
                    intent["run_id"], claim.resource, claim.mode,
                    claim.amount, now,
                ),
            )
    lease_binding, lease_db = _lease_database(tmp_path, intent)
    initial = store.run(intent["run_id"])
    assert initial["state"] == "assigned"
    assert _reserve_count(store) == len(task.claims)
    with pytest.raises(StateError, match="T044 coordinated claim commit blocked"):
        dispatcher.commit_claim_intent(
            intent, lease_binding if intent["required_resource_keys"] else None,
            resource_db=lease_db,
        )
    after = store.run(intent["run_id"])
    assert after["state"] == "assigned"
    assert after["envelope_sha256"] == initial["envelope_sha256"]
    assert after["reservations"] == initial["reservations"]
    assert _reserve_count(store) == len(task.claims)
