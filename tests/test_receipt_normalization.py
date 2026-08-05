import json
import sqlite3
from pathlib import Path
from typing import Any
import pytest
from bureau.core import StateStore, BureauError, Registry
from bureau.receipt_normalization import receipt_normalize

def add_drift_task(registry_root: Path):
    task_id = "GRABOWSKI-OPERATOR-SURFACE-V1-T139"
    task = {
        "schema_version": 1,
        "id": task_id,
        "initiative": "BUR-TEST-001",
        "title": "Drift task",
        "state": "ready",
        "depends_on": [],
        "required_capabilities": ["repository"],
        "priority": {"lane": "now", "rank": 0},
        "execution": {
            "mode": "interactive-agent",
            "policy": "autonomous",
            "working_repository": str(registry_root),
        },
        "claims": [],
        "acceptance": [{"id": "proof", "assertion": "proof exists"}],
        "metadata": {
            "partial_delivery": {
                "supersedes_invalid_closeout": {
                    "source_run_id": "BUR-RUN-20260804T212041Z-4cb4c3cf2e",
                    "task_sha256": "ede934fb51ea09bd184f061a2d50a153802996a8af8c1df26663b85091604249",
                    "plan_sha256": "c867c77d1608db974e679be15a885302e7c155d9f1f523e8853d2b3c4b1d665b"
                }
            }
        }
    }
    (registry_root / f"registry/tasks/{task_id}.json").write_text(json.dumps(task))
    return task_id

def insert_test_data(conn, task_id, task_sha="ede934fb51ea09bd184f061a2d50a153802996a8af8c1df26663b85091604249"):
    conn.execute(
        "INSERT INTO workers (worker_id, kind, capabilities_json, heartbeat_at) VALUES ('worker', 'test', '[]', 'now')"
    )
    conn.execute(
        "INSERT INTO task_status (task_id, task_sha256, plan_sha256, state, receipt_sha256, updated_at) VALUES (?, ?, 'c867c77d1608db974e679be15a885302e7c155d9f1f523e8853d2b3c4b1d665b', 'done', 'fd1d74140c55129794e8fcb6f8d2dcf22db0f481a103a745a6396899a4198125', 'now')",
        (task_id, task_sha)
    )
    conn.execute(
        "INSERT INTO runs (run_id, task_id, worker_id, state, created_at, updated_at, attempt, task_sha256, plan_sha256, envelope_json, envelope_sha256, heartbeat_at) VALUES ('BUR-RUN-20260804T212041Z-4cb4c3cf2e', ?, 'worker', 'done', 'now', 'now', 1, ?, 'c867c77d1608db974e679be15a885302e7c155d9f1f523e8853d2b3c4b1d665b', '{}', 'envelope_sha', 'now')",
        (task_id, task_sha)
    )
    conn.execute(
        "INSERT INTO receipts (run_id, receipt_json, receipt_sha256, created_at) VALUES ('BUR-RUN-20260804T212041Z-4cb4c3cf2e', '{}', 'fd1d74140c55129794e8fcb6f8d2dcf22db0f481a103a745a6396899a4198125', 'now')"
    )

def test_dry_run_lists_normalizable(registry_factory, tmp_path: Path):
    registry_root = registry_factory()
    task_id = add_drift_task(registry_root)
    registry = Registry.load(registry_root)
    
    state_path = tmp_path / "state.sqlite3"
    store = StateStore(state_path)
    with store.connect() as conn:
        insert_test_data(conn, task_id)
        
    result = receipt_normalize(registry, store, dry_run=True, runtime_identity={})
    assert result["status"] == "ok"
    assert len(result["normalizable"]) == 1
    assert result["normalizable"][0]["task_id"] == task_id
    
    with store.connect() as conn:
        row = conn.execute("SELECT * FROM task_status WHERE task_id = ?", (task_id,)).fetchone()
        assert row["state"] == "done"
        
def test_apply_creates_backup_and_updates_db_and_events(registry_factory, tmp_path: Path):
    registry_root = registry_factory()
    task_id = add_drift_task(registry_root)
    registry = Registry.load(registry_root)
    
    state_path = tmp_path / "state.sqlite3"
    store = StateStore(state_path)
    with store.connect() as conn:
        insert_test_data(conn, task_id)
        
    result = receipt_normalize(registry, store, dry_run=False, runtime_identity={})
    assert result["status"] == "ok"
    assert result["applied"] == [task_id]
    assert result["backup"] is not None
    backup_path = Path(result["backup"])
    assert backup_path.exists()
    assert backup_path.name.startswith("bureau.sqlite3.pre-normalization.")
    
    with store.connect() as conn:
        row = conn.execute("SELECT * FROM task_status WHERE task_id = ?", (task_id,)).fetchone()
        assert row["state"] == "ready"
        assert row["task_sha256"] == registry.tasks[task_id].sha256
        assert row["plan_sha256"] == registry.tasks[task_id].raw.get("plan_sha256", "")
        assert row["receipt_sha256"] == "fd1d74140c55129794e8fcb6f8d2dcf22db0f481a103a745a6396899a4198125"
        
        events = [dict(r) for r in conn.execute("SELECT * FROM events")]
        assert len(events) == 1
        event = events[0]
        assert event["event_type"] == "receipt_normalization"

def test_ignores_wrong_bindings(registry_factory, tmp_path: Path):
    registry_root = registry_factory()
    task_id = add_drift_task(registry_root)
    registry = Registry.load(registry_root)
    
    state_path = tmp_path / "state.sqlite3"
    store = StateStore(state_path)
    with store.connect() as conn:
        insert_test_data(conn, task_id, task_sha="wrong")
        
    result = receipt_normalize(registry, store, dry_run=True, runtime_identity={})
    assert len(result.get("normalizable", [])) == 0

def test_idempotent_apply(registry_factory, tmp_path: Path):
    registry_root = registry_factory()
    task_id = add_drift_task(registry_root)
    registry = Registry.load(registry_root)
    
    state_path = tmp_path / "state.sqlite3"
    store = StateStore(state_path)
    with store.connect() as conn:
        insert_test_data(conn, task_id)
        
    receipt_normalize(registry, store, dry_run=False, runtime_identity={})
    result = receipt_normalize(registry, store, dry_run=False, runtime_identity={})
    assert result.get("applied", []) == []

def test_apply_fails_if_backup_exists(registry_factory, tmp_path: Path, monkeypatch):
    registry_root = registry_factory()
    task_id = add_drift_task(registry_root)
    registry = Registry.load(registry_root)
    
    state_path = tmp_path / "state.sqlite3"
    store = StateStore(state_path)
    with store.connect() as conn:
        insert_test_data(conn, task_id)
        
    class MockDatetime:
        @classmethod
        def now(cls, tz):
            import datetime
            return datetime.datetime(2026, 8, 5, 9, 0, 0, tzinfo=datetime.timezone.utc)
            
    monkeypatch.setattr("bureau.receipt_normalization.datetime", MockDatetime)
    
    result = receipt_normalize(registry, store, dry_run=False, runtime_identity={})
    assert result["status"] == "ok"
    
    with store.connect() as conn:
        conn.execute("UPDATE task_status SET task_sha256 = 'ede934fb51ea09bd184f061a2d50a153802996a8af8c1df26663b85091604249', plan_sha256 = 'c867c77d1608db974e679be15a885302e7c155d9f1f523e8853d2b3c4b1d665b', state = 'done'")
        
    with pytest.raises(BureauError, match="Backup file already exists"):
        receipt_normalize(registry, store, dry_run=False, runtime_identity={})
