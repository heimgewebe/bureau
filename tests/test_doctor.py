from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from bureau import legacy
from bureau.doctor import doctor_projection
from bureau.state_backup import create_backup
from bureau.v2 import StateStore

NOW = "2026-07-07T12:00:00Z"
TASK_1 = "BUR-TEST-001-T001"


def make_state(root: Path) -> Path:
    state_root = root / "state"
    state_root.mkdir(exist_ok=True)
    StateStore(state_root / "bureau.sqlite3", state_root)
    return state_root


def add_running_work(state_root: Path) -> None:
    with sqlite3.connect(state_root / "bureau.sqlite3") as connection:
        connection.execute(
            "INSERT OR IGNORE INTO workers VALUES(?,?,?,?)",
            ("worker-doctor", "interactive-agent", "[]", NOW),
        )
        connection.execute(
            """
            INSERT INTO runs(
                run_id,task_id,worker_id,attempt,state,task_sha256,plan_sha256,
                envelope_json,envelope_sha256,workspace_path,workspace_branch,
                created_at,updated_at,heartbeat_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "BUR-RUN-DOCTOR",
                TASK_1,
                "worker-doctor",
                1,
                "running",
                "task-sha",
                "plan-sha",
                "{}",
                "envelope-sha",
                str(state_root / "workspace"),
                "task/doctor",
                NOW,
                NOW,
                NOW,
            ),
        )
        connection.execute(
            "INSERT INTO reservations VALUES(?,?,?,?,?)",
            ("BUR-RUN-DOCTOR", "repo.alpha", "write", 1, NOW),
        )
        connection.execute(
            "INSERT INTO workspaces VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "BUR-RUN-DOCTOR",
                str(state_root.parent),
                str(state_root / "workspace"),
                "task/doctor",
                "a" * 40,
                "active",
                NOW,
                NOW,
                None,
            ),
        )


def write_restore_receipt(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "kind": "bureau_state_restore_test_receipt",
        "status": "verified",
        "tested_at": NOW,
        "manifest_sha256": "a" * 64,
        "authoritative_root_sha256": "b" * 64,
    }
    payload["receipt_sha256"] = legacy.sha256_json(payload)
    (root / "latest.json").write_text(json.dumps(payload), encoding="utf-8")


def test_doctor_is_read_only_and_repair_plan_has_no_effect(
    registry_factory, tmp_path: Path
) -> None:
    root = registry_factory()
    state_root = make_state(root)
    database = state_root / "bureau.sqlite3"
    before = database.read_bytes()

    result = doctor_projection(
        root,
        state_root=state_root,
        backup_root=tmp_path / "no-backups",
        restore_receipt_root=tmp_path / "no-restores",
        now=NOW,
    )

    assert result["read_only"] is True
    assert result["effect"] == "none"
    assert result["healthy"] is False
    assert database.read_bytes() == before
    assert result["repair_plan"]["apply_contract_required"] is True
    assert result["repair_plan"]["proposal_count"] >= 2
    for proposal in result["repair_plan"]["proposals"]:
        assert proposal["effect"] == "none"
        assert len(proposal["dry_run_sha256"]) == 64
        assert proposal["required_authority"]
        assert proposal["apply_contract"]
        assert proposal["readback"]


def test_doctor_projects_flow_backup_restore_and_authority(
    registry_factory, tmp_path: Path
) -> None:
    root = registry_factory(2, mode="write")
    state_root = make_state(root)
    backup_root = tmp_path / "backups"
    create_backup(state_root=state_root, backup_root=backup_root)
    add_running_work(state_root)
    restore_root = tmp_path / "restore-receipts"
    write_restore_receipt(restore_root)

    result = doctor_projection(
        root,
        state_root=state_root,
        backup_root=backup_root,
        restore_receipt_root=restore_root,
        now=NOW,
    )
    control = result["control_plane"]
    metrics = control["metrics"]

    assert metrics["ready"]["value"] == 2
    assert metrics["claimable"]["value"] == 1
    assert metrics["in_flight"]["value"] == 1
    assert metrics["claims"]["value"] == 1
    assert metrics["workspaces"]["value"] == 1
    assert metrics["backup_age_seconds"]["value"] is not None
    assert metrics["restore_status"]["value"] == "verified"
    assert control["organs"]["backup"]["status"] == "verified"
    assert control["organs"]["restore"]["status"] == "verified"
    assert control["authority"]["operational"] == "Bureau StateStore"
    for organ in control["organs"].values():
        assert {"source", "freshness", "bounds", "authority", "status"} <= set(organ)


def test_dashboard_is_bounded_doctor_consumer(registry_factory, tmp_path: Path) -> None:
    root = registry_factory()
    state_root = make_state(root)
    result = doctor_projection(
        root,
        state_root=state_root,
        backup_root=tmp_path / "no-backups",
        restore_receipt_root=tmp_path / "no-restores",
        now=NOW,
    )
    dashboard = result["dashboard"]

    assert dashboard["kind"] == "bureau_control_plane_dashboard"
    assert dashboard["source"] == "bureau-control-plane-doctor"
    assert dashboard["read_only"] is True
    assert "tasks" not in dashboard
    assert "repository_balls" not in dashboard
    assert "next_actions" not in dashboard
    assert set(dashboard) == {
        "schema_version",
        "kind",
        "generated_at",
        "healthy",
        "metrics",
        "organs",
        "attention",
        "source",
        "read_only",
        "does_not_establish",
    }
    assert all("dry_run_sha256" in item for item in dashboard["attention"])
