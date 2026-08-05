from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import BureauError, Registry, StateStore, plan_sha256

_SUPPORTED_TASK_ID = "GRABOWSKI-OPERATOR-SURFACE-V1-T139"
_TERMINAL_TASK_STATES = frozenset({"verified", "cancelled", "superseded"})
_EXPECTED_STALE_STATUS = "verified"


def _backup_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def _backup_path(state_path: Path) -> Path:
    return state_path.with_name(
        f"{state_path.name}.pre-normalization.{_backup_timestamp()}"
    )


def _create_backup(connection: sqlite3.Connection, backup_path: Path) -> None:
    try:
        descriptor = os.open(
            backup_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
    except FileExistsError as exc:
        raise BureauError(f"Backup file already exists: {backup_path}") from exc
    else:
        os.close(descriptor)

    try:
        with sqlite3.connect(backup_path) as backup:
            connection.backup(backup)
            result = backup.execute("PRAGMA integrity_check").fetchall()
            if result != [("ok",)]:
                raise BureauError(
                    f"Backup integrity check failed for {backup_path}: {result!r}"
                )
    except Exception:
        backup_path.unlink(missing_ok=True)
        raise


def _normalization_candidate(
    registry: Registry,
    connection: sqlite3.Connection,
) -> dict[str, Any] | None:
    task = registry.tasks.get(_SUPPORTED_TASK_ID)
    if task is None or task.state in _TERMINAL_TASK_STATES:
        return None

    supersedes = (
        task.raw.get("metadata", {})
        .get("partial_delivery", {})
        .get("supersedes_invalid_closeout")
    )
    if not isinstance(supersedes, dict):
        return None
    closeout = supersedes.get("closeout")
    verification = supersedes.get("verification")
    if not isinstance(closeout, dict) or not isinstance(verification, dict):
        return None

    source_run_id = closeout.get("source_run_id")
    old_task_sha256 = closeout.get("source_run_task_sha256")
    old_plan_sha256 = verification.get("plan_sha256")
    source_run_state = closeout.get("source_run_state")
    source_run_updated_at = closeout.get("source_run_updated_at")
    required_strings = (
        source_run_id,
        old_task_sha256,
        old_plan_sha256,
        source_run_state,
        source_run_updated_at,
    )
    if not all(isinstance(value, str) and value for value in required_strings):
        return None

    current_plan_sha256 = plan_sha256(registry, task.initiative)
    if current_plan_sha256 != old_plan_sha256:
        return None

    status_row = connection.execute(
        """
        SELECT task_id, task_sha256, plan_sha256, state, receipt_sha256, updated_at
        FROM task_status
        WHERE task_id = ?
        """,
        (_SUPPORTED_TASK_ID,),
    ).fetchone()
    if status_row is None:
        return None
    receipt_sha256 = status_row["receipt_sha256"]
    if (
        status_row["task_sha256"] != old_task_sha256
        or status_row["plan_sha256"] != old_plan_sha256
        or status_row["state"] != _EXPECTED_STALE_STATUS
        or not isinstance(receipt_sha256, str)
        or not receipt_sha256
    ):
        return None

    run_row = connection.execute(
        """
        SELECT task_id, state, task_sha256, plan_sha256, updated_at
        FROM runs
        WHERE run_id = ?
        """,
        (source_run_id,),
    ).fetchone()
    if run_row is None:
        return None
    if (
        run_row["task_id"] != _SUPPORTED_TASK_ID
        or run_row["state"] != source_run_state
        or run_row["task_sha256"] != old_task_sha256
        or run_row["plan_sha256"] != old_plan_sha256
        or run_row["updated_at"] != source_run_updated_at
    ):
        return None

    receipt_row = connection.execute(
        "SELECT receipt_sha256 FROM receipts WHERE run_id = ?",
        (source_run_id,),
    ).fetchone()
    if receipt_row is None or receipt_row["receipt_sha256"] != receipt_sha256:
        return None

    return {
        "task_id": _SUPPORTED_TASK_ID,
        "source_run_id": source_run_id,
        "receipt_sha256": receipt_sha256,
        "before": {
            "task_sha256": old_task_sha256,
            "plan_sha256": old_plan_sha256,
            "state": status_row["state"],
            "updated_at": status_row["updated_at"],
        },
        "after": {
            "task_sha256": task.sha256,
            "plan_sha256": current_plan_sha256,
            "state": task.state,
        },
    }


def _public_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": candidate["task_id"],
        "source_run_id": candidate["source_run_id"],
        "receipt_sha256": candidate["receipt_sha256"],
        "before": dict(candidate["before"]),
        "after": dict(candidate["after"]),
    }


def receipt_normalize(
    registry: Registry,
    state_store: StateStore,
    *,
    dry_run: bool,
    runtime_identity: dict[str, Any],
) -> dict[str, Any]:
    connection = state_store.connect()
    backup_path: Path | None = None
    try:
        candidate = _normalization_candidate(registry, connection)
        if dry_run:
            return {
                "schema_version": 1,
                "status": "ok",
                "task_id": _SUPPORTED_TASK_ID,
                "normalizable": (
                    [_public_candidate(candidate)] if candidate is not None else []
                ),
            }
        if candidate is None:
            return {
                "schema_version": 1,
                "status": "ok",
                "task_id": _SUPPORTED_TASK_ID,
                "applied": [],
                "backup": None,
            }

        backup_path = _backup_path(state_store.path)
        _create_backup(connection, backup_path)

        connection.execute("BEGIN IMMEDIATE")
        locked_candidate = _normalization_candidate(registry, connection)
        if locked_candidate != candidate:
            connection.rollback()
            backup_path.unlink(missing_ok=True)
            raise BureauError(
                "Coordination state changed during receipt normalization preflight"
            )

        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        before = candidate["before"]
        after = candidate["after"]
        cursor = connection.execute(
            """
            UPDATE task_status
            SET task_sha256 = ?, plan_sha256 = ?, state = ?, updated_at = ?
            WHERE task_id = ?
              AND task_sha256 = ?
              AND plan_sha256 = ?
              AND state = ?
              AND receipt_sha256 = ?
              AND updated_at = ?
            """,
            (
                after["task_sha256"],
                after["plan_sha256"],
                after["state"],
                now,
                candidate["task_id"],
                before["task_sha256"],
                before["plan_sha256"],
                before["state"],
                candidate["receipt_sha256"],
                before["updated_at"],
            ),
        )
        if cursor.rowcount != 1:
            raise BureauError("Receipt normalization compare-and-swap did not match")

        event_payload = {
            "kind": "receipt_normalization",
            "task_id": candidate["task_id"],
            "source_run_id": candidate["source_run_id"],
            "receipt_sha256": candidate["receipt_sha256"],
            "before": before,
            "after": after,
            "runtime_identity": runtime_identity,
            "backup": str(backup_path),
        }
        connection.execute(
            "INSERT INTO events (event_type, payload_json, created_at) VALUES (?, ?, ?)",
            (
                "receipt_normalization",
                json.dumps(event_payload, sort_keys=True, separators=(",", ":")),
                now,
            ),
        )
        connection.commit()
        return {
            "schema_version": 1,
            "status": "ok",
            "task_id": _SUPPORTED_TASK_ID,
            "applied": [candidate["task_id"]],
            "backup": str(backup_path),
        }
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
