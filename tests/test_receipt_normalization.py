from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from bureau.core import BureauError, Registry, StateStore, sha256_json
from bureau.receipt_normalization import receipt_normalize

TASK_ID = "GRABOWSKI-OPERATOR-SURFACE-V1-T139"
SOURCE_RUN_ID = "BUR-RUN-20260804T212041Z-4cb4c3cf2e"
OLD_TASK_SHA256 = "ede934fb51ea09bd184f061a2d50a153802996a8af8c1df26663b85091604249"
PLAN_SHA256 = sha256_json({})
RECEIPT_SHA256 = "fd1d74140c55129794e8fcb6f8d2dcf22db0f481a103a745a6396899a4198125"
SOURCE_UPDATED_AT = "2026-08-04T21:36:02.129225Z"


def add_drift_task(registry_root: Path, *, state: str = "ready") -> str:
    task = {
        "schema_version": 1,
        "id": TASK_ID,
        "initiative": "BUR-TEST-001",
        "title": "Drift task",
        "state": state,
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
                    "closeout": {
                        "source_run_id": SOURCE_RUN_ID,
                        "source_run_state": "succeeded",
                        "source_run_updated_at": SOURCE_UPDATED_AT,
                        "source_run_task_sha256": OLD_TASK_SHA256,
                    },
                    "verification": {
                        "task_sha256": (
                            "7a0c53aa13ecc8242707d418c41b6f0eaf0facc9be1f91b37a83f881ffc42311"
                        ),
                        "plan_sha256": PLAN_SHA256,
                    },
                }
            }
        },
    }
    path = registry_root / f"registry/tasks/{TASK_ID}.json"
    path.write_text(json.dumps(task), encoding="utf-8")
    return TASK_ID


def insert_test_data(connection: sqlite3.Connection, task_id: str) -> None:
    connection.execute(
        """
        INSERT INTO workers(worker_id, kind, capabilities_json, heartbeat_at)
        VALUES ('worker', 'test', '[]', 'now')
        """
    )
    connection.execute(
        """
        INSERT INTO task_status(
            task_id, task_sha256, plan_sha256, state, receipt_sha256, updated_at
        )
        VALUES (?, ?, ?, 'verified', ?, 'status-updated-at')
        """,
        (task_id, OLD_TASK_SHA256, PLAN_SHA256, RECEIPT_SHA256),
    )
    connection.execute(
        """
        INSERT INTO runs(
            run_id, task_id, worker_id, state, created_at, updated_at, attempt,
            task_sha256, plan_sha256, envelope_json, envelope_sha256, heartbeat_at
        )
        VALUES (?, ?, 'worker', 'succeeded', 'created', ?, 1, ?, ?, '{}',
                'envelope-sha', 'heartbeat')
        """,
        (SOURCE_RUN_ID, task_id, SOURCE_UPDATED_AT, OLD_TASK_SHA256, PLAN_SHA256),
    )
    connection.execute(
        """
        INSERT INTO receipts(run_id, receipt_json, receipt_sha256, created_at)
        VALUES (?, '{}', ?, 'created')
        """,
        (SOURCE_RUN_ID, RECEIPT_SHA256),
    )


def setup_case(registry_factory, tmp_path: Path):
    registry_root = registry_factory()
    task_id = add_drift_task(registry_root)
    registry = Registry.load(registry_root)
    state_path = tmp_path / "state.sqlite3"
    store = StateStore(state_path)
    with store.connect() as connection:
        insert_test_data(connection, task_id)
    return registry, store, state_path, task_id


def test_dry_run_is_exact_and_read_only(registry_factory, tmp_path: Path):
    registry, store, _, task_id = setup_case(registry_factory, tmp_path)

    result = receipt_normalize(
        registry,
        store,
        dry_run=True,
        runtime_identity={"release": "test"},
    )

    assert result["status"] == "ok"
    assert [item["task_id"] for item in result["normalizable"]] == [task_id]
    assert result["normalizable"][0]["before"]["state"] == "verified"
    assert result["normalizable"][0]["after"]["state"] == "ready"
    with store.connect() as connection:
        row = connection.execute(
            "SELECT * FROM task_status WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    assert row["state"] == "verified"
    assert row["task_sha256"] == OLD_TASK_SHA256


def test_apply_uses_online_backup_cas_and_event(registry_factory, tmp_path: Path):
    registry, store, _, task_id = setup_case(registry_factory, tmp_path)

    result = receipt_normalize(
        registry,
        store,
        dry_run=False,
        runtime_identity={"release": "test"},
    )

    assert result["applied"] == [task_id]
    backup_path = Path(result["backup"])
    assert backup_path.exists()
    with sqlite3.connect(backup_path) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        stale = backup.execute(
            "SELECT task_sha256, state FROM task_status WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    assert stale == (OLD_TASK_SHA256, "verified")

    with store.connect() as connection:
        row = connection.execute(
            "SELECT * FROM task_status WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        event = connection.execute(
            "SELECT * FROM events WHERE event_type = 'receipt_normalization'"
        ).fetchone()
    assert row["task_sha256"] == registry.tasks[task_id].sha256
    assert row["plan_sha256"] == PLAN_SHA256
    assert row["state"] == "ready"
    assert row["receipt_sha256"] == RECEIPT_SHA256
    payload = json.loads(event["payload_json"])
    assert payload["before"]["task_sha256"] == OLD_TASK_SHA256
    assert payload["after"]["task_sha256"] == registry.tasks[task_id].sha256
    assert payload["backup"] == str(backup_path)


def test_source_run_mismatch_is_not_normalizable(registry_factory, tmp_path: Path):
    registry, store, _, _ = setup_case(registry_factory, tmp_path)
    with store.connect() as connection:
        connection.execute(
            "UPDATE runs SET updated_at = 'different' WHERE run_id = ?",
            (SOURCE_RUN_ID,),
        )

    result = receipt_normalize(
        registry,
        store,
        dry_run=True,
        runtime_identity={},
    )

    assert result["normalizable"] == []


def test_apply_is_idempotent(registry_factory, tmp_path: Path):
    registry, store, _, _ = setup_case(registry_factory, tmp_path)
    receipt_normalize(registry, store, dry_run=False, runtime_identity={})

    result = receipt_normalize(
        registry,
        store,
        dry_run=False,
        runtime_identity={},
    )

    assert result["applied"] == []
    assert result["backup"] is None


def test_apply_rejects_backup_collision(
    registry_factory,
    tmp_path: Path,
    monkeypatch,
):
    registry, store, state_path, _ = setup_case(registry_factory, tmp_path)
    timestamp = "20260805T090000.000000Z"
    monkeypatch.setattr(
        "bureau.receipt_normalization._backup_timestamp",
        lambda: timestamp,
    )
    collision = state_path.with_name(
        f"{state_path.name}.pre-normalization.{timestamp}"
    )
    collision.write_text("do not overwrite", encoding="utf-8")

    with pytest.raises(BureauError, match="Backup file already exists"):
        receipt_normalize(registry, store, dry_run=False, runtime_identity={})

    assert collision.read_text(encoding="utf-8") == "do not overwrite"
    with store.connect() as connection:
        row = connection.execute(
            "SELECT task_sha256, state FROM task_status WHERE task_id = ?",
            (TASK_ID,),
        ).fetchone()
    assert tuple(row) == (OLD_TASK_SHA256, "verified")


def test_apply_fails_closed_on_precommit_race(
    registry_factory,
    tmp_path: Path,
    monkeypatch,
):
    registry, store, _, _ = setup_case(registry_factory, tmp_path)
    from bureau import receipt_normalization as module

    original = module._create_backup

    def create_backup_then_race(connection, backup_path):
        original(connection, backup_path)
        with store.connect() as competing:
            competing.execute(
                "UPDATE task_status SET updated_at = 'raced' WHERE task_id = ?",
                (TASK_ID,),
            )

    monkeypatch.setattr(module, "_create_backup", create_backup_then_race)

    with pytest.raises(BureauError, match="state changed"):
        receipt_normalize(registry, store, dry_run=False, runtime_identity={})

    with store.connect() as connection:
        row = connection.execute(
            "SELECT task_sha256, state, updated_at FROM task_status WHERE task_id = ?",
            (TASK_ID,),
        ).fetchone()
    assert tuple(row) == (OLD_TASK_SHA256, "verified", "raced")
