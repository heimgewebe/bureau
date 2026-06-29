from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from bureau import discovery, discovery_runner
from bureau.cycle_contract import (
    CONTRACT_VERSION,
    begin_receipt,
    classify_task_attention,
    validate_receipt,
)


def create_task_db(path: Path, now: int) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY,
            unit TEXT NOT NULL,
            state TEXT NOT NULL,
            runtime_seconds INTEGER NOT NULL,
            created_at_unix INTEGER NOT NULL,
            updated_at_unix INTEGER NOT NULL
        )
        """
    )
    rows = [
        ("legacy", "grabowski-task-legacy.service", "interrupted", 60, now - 50_000, now - 40_000),
        ("recent", "grabowski-task-recent.service", "interrupted", 60, now - 2_000, now - 1_000),
        ("failed-new", "grabowski-task-failed-new.service", "failed", 60, now - 2_000, now - 900),
        (
            "failed-old",
            "grabowski-task-failed-old.service",
            "failed",
            60,
            now - 50_000,
            now - 40_000,
        ),
        ("running-ok", "grabowski-task-running-ok.service", "running", 10_000, now - 100, now - 10),
        (
            "running-stale",
            "grabowski-task-running-stale.service",
            "running",
            60,
            now - 1_000,
            now - 900,
        ),
        ("done", "grabowski-task-done.service", "completed", 60, now - 1_000, now - 800),
    ]
    connection.executemany("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?)", rows)
    connection.commit()
    connection.close()


def test_attention_separates_legacy_unknown_from_current_attention(tmp_path: Path) -> None:
    now = 1_800_000_000
    task_db = tmp_path / "tasks.sqlite3"
    create_task_db(task_db, now)

    report = classify_task_attention(
        task_db,
        now_unix=now,
        horizon_seconds=10_800,
        limit=10,
    )

    assert report["counts"]["legacy_outcome_unavailable"] == 1
    assert report["counts"]["current_outcome_unknown"] == 1
    assert report["counts"]["recent_failed"] == 1
    assert report["counts"]["stale_running"] == 1
    assert report["current_attention_count"] == 3
    assert report["counts"]["historical_failed"] == 1
    assert report["counts"]["healthy_running"] == 1
    assert report["counts"]["terminal_history"] == 1


def test_begin_receipt_is_atomic_running_handoff(tmp_path: Path) -> None:
    receipt = begin_receipt(
        "operator",
        "test-trigger",
        state_root=tmp_path,
        selected_cycle_id="2026-06-29T04",
    )

    assert receipt["contract_version"] == CONTRACT_VERSION
    assert receipt["lifecycle_state"] == "running"
    assert receipt["result"] is None
    receipt_path = Path(receipt["receipt_path"])
    assert receipt_path.is_file()
    assert (tmp_path / "bureau-operator/latest.json").is_file()
    assert (
        validate_receipt(
            receipt,
            expected_stage="operator",
            expected_cycle_id="2026-06-29T04",
            require_terminal=False,
        )
        == []
    )


def test_terminal_receipt_contract_rejects_running_or_wrong_cycle() -> None:
    receipt = {
        "schema_version": 2,
        "contract_version": CONTRACT_VERSION,
        "cycle_id": "2026-06-29T04",
        "stage": "verifier",
        "run_id": "verifier-test",
        "trigger": "test",
        "started_at": "2026-06-29T02:15:00Z",
        "finished_at": None,
        "lifecycle_state": "running",
        "result": None,
        "degraded": False,
        "evidence": [],
        "next_action": "finish",
    }

    errors = validate_receipt(
        receipt,
        expected_stage="verifier",
        expected_cycle_id="2026-06-29T05",
    )

    assert any("cycle_id mismatch" in error for error in errors)
    assert "lifecycle_state is not terminal" in errors
    assert any("result is not terminal" in error for error in errors)


def test_scanner_receipt_requires_scanner_handoff_fields() -> None:
    receipt = {
        "schema_version": 2,
        "contract_version": CONTRACT_VERSION,
        "cycle_id": "2026-06-29T04",
        "stage": "scanner",
        "run_id": "scanner-test",
        "trigger": "test",
        "started_at": "2026-06-29T02:30:00Z",
        "finished_at": "2026-06-29T02:30:01Z",
        "lifecycle_state": "terminal",
        "result": "idle",
        "degraded": False,
        "evidence": [],
        "next_action": "none",
    }

    errors = validate_receipt(receipt, expected_stage="scanner")

    assert "scanner receipt missing field: scanner_run_id" in errors
    assert "scanner receipt missing field: source_revisions" in errors
    assert "scanner receipt missing field: promotion_allowed" in errors


def configure_scanner_paths(tmp_path: Path, monkeypatch) -> None:
    state = tmp_path / "state"
    monkeypatch.setattr(discovery, "STATE", state)
    monkeypatch.setattr(discovery, "REGISTRY", state / "source-registry.json")
    monkeypatch.setattr(discovery, "SOURCE_STATE", state / "source-state.json")
    monkeypatch.setattr(discovery, "RUNS", state / "runs")
    monkeypatch.setattr(discovery, "INBOX", state / "inbox")
    monkeypatch.setattr(discovery, "LOCK", state / "scanner.lock")


def test_corrupt_source_state_fails_closed(tmp_path: Path, monkeypatch) -> None:
    configure_scanner_paths(tmp_path, monkeypatch)
    discovery.STATE.mkdir(parents=True)
    discovery.REGISTRY.write_text(
        json.dumps({"repositories": [], "vault_root": str(tmp_path / "vault")}),
        encoding="utf-8",
    )
    discovery.SOURCE_STATE.write_text("{not-json", encoding="utf-8")

    try:
        discovery.main()
    except RuntimeError as exc:
        assert "invalid source state" in str(exc)
    else:
        raise AssertionError("corrupt source state must fail closed")

    latest = json.loads((discovery.STATE / "latest.json").read_text(encoding="utf-8"))
    assert latest["lifecycle_state"] == "running"
    assert latest["promotion_allowed"] is False


def test_failed_receipt_closes_current_running_scanner_receipt(tmp_path: Path, monkeypatch) -> None:
    configure_scanner_paths(tmp_path, monkeypatch)
    discovery.RUNS.mkdir(parents=True)
    discovery.INBOX.mkdir(parents=True)
    selected_cycle = discovery.cycle_id()
    running_path = discovery.RUNS / "20260629T030000Z.json"
    running = {
        "schema_version": 2,
        "contract_version": CONTRACT_VERSION,
        "cycle_id": selected_cycle,
        "stage": "scanner",
        "run_id": "scanner-running",
        "scanner_run_id": "scanner-running",
        "trigger": "test",
        "started_at": "2026-06-29T03:00:00Z",
        "finished_at": None,
        "lifecycle_state": "running",
        "result": None,
        "degraded": False,
        "promotion_allowed": False,
        "source_revisions": [],
        "evidence": [],
        "next_action": "finish",
        "receipt_path": str(running_path),
    }
    running_path.write_text(json.dumps(running), encoding="utf-8")

    result_path = discovery_runner.failed_receipt(RuntimeError("broken state"))

    assert result_path == running_path
    terminal = json.loads(running_path.read_text(encoding="utf-8"))
    assert terminal["run_id"] == "scanner-running"
    assert terminal["started_at"] == "2026-06-29T03:00:00Z"
    assert terminal["lifecycle_state"] == "terminal"
    assert terminal["result"] == "failed"
    assert terminal["promotion_allowed"] is False
    assert validate_receipt(terminal, expected_stage="scanner") == []
