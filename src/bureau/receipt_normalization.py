from __future__ import annotations

import json
import sqlite3
import shutil
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import Registry, StateStore, BureauError

def receipt_normalize(
    registry: Registry,
    state_store: StateStore,
    *,
    dry_run: bool,
    runtime_identity: dict[str, Any]
) -> dict[str, Any]:
    connection = state_store.connect()
    try:
        task_status_rows = [dict(row) for row in connection.execute("SELECT * FROM task_status")]
        
        normalizable = []
        for row in task_status_rows:
            task_id = row["task_id"]
            if task_id not in registry.tasks:
                continue
            
            task = registry.tasks[task_id]
            if task.state in ("done", "dropped"):
                continue
            
            supersedes = task.raw.get("metadata", {}).get("partial_delivery", {}).get("supersedes_invalid_closeout", {})
            if not supersedes:
                continue
            
            source_run_id = supersedes.get("source_run_id")
            old_task_sha256 = supersedes.get("task_sha256")
            old_plan_sha256 = supersedes.get("plan_sha256")
            
            if row["task_sha256"] != old_task_sha256 or row["plan_sha256"] != old_plan_sha256:
                continue
                
            receipt_row = connection.execute(
                "SELECT receipt_sha256 FROM receipts WHERE run_id = ?", (source_run_id,)
            ).fetchone()
            if not receipt_row or receipt_row["receipt_sha256"] != row["receipt_sha256"]:
                continue
                
            normalizable.append({
                "task_id": task_id,
                "current_task_sha256": task.sha256,
                "current_plan_sha256": task.raw.get("plan_sha256", ""),
                "current_state": task.state,
                "old_task_sha256": old_task_sha256,
                "old_plan_sha256": old_plan_sha256,
                "receipt_sha256": row["receipt_sha256"],
                "source_run_id": source_run_id
            })
            
        if dry_run:
            return {
                "schema_version": 1,
                "status": "ok",
                "normalizable": normalizable
            }
            
        if not normalizable:
            return {
                "schema_version": 1,
                "status": "ok",
                "normalizable": []
            }
            
        # apply
        state_path = state_store.path
        if state_path:
            backup_path = state_path.with_name(f"bureau.sqlite3.pre-normalization.{datetime.now(timezone.utc).strftime('%Y%md%H%M%S')}")
            if backup_path.exists():
                raise BureauError("Backup file already exists")
            shutil.copy2(state_path, backup_path)
            
            # verify backup
            try:
                b_conn = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
                b_conn.execute("PRAGMA integrity_check")
                b_conn.close()
            except sqlite3.Error as e:
                raise BureauError(f"Backup integrity check failed: {e}")
        else:
            backup_path = None
            
        now = datetime.now(timezone.utc).isoformat()
        
        applied = []
        connection.execute("BEGIN IMMEDIATE")
        for norm in normalizable:
            connection.execute(
                "UPDATE task_status SET task_sha256 = ?, plan_sha256 = ?, state = ?, updated_at = ? WHERE task_id = ?",
                (norm["current_task_sha256"], norm["current_plan_sha256"], norm["current_state"], now, norm["task_id"])
            )
            
            event_payload = {
                "kind": "receipt_normalization",
                "task_id": norm["task_id"],
                "before": {
                    "task_sha256": norm["old_task_sha256"],
                    "plan_sha256": norm["old_plan_sha256"],
                },
                "after": {
                    "task_sha256": norm["current_task_sha256"],
                    "plan_sha256": norm["current_plan_sha256"],
                    "state": norm["current_state"]
                },
                "receipt_sha256": norm["receipt_sha256"],
                "source_run_id": norm["source_run_id"],
                "runtime_identity": runtime_identity,
                "backup": str(backup_path) if backup_path else None
            }
            connection.execute(
                "INSERT INTO events (event_type, payload_json, created_at) VALUES (?, ?, ?)",
                ("receipt_normalization", json.dumps(event_payload), now)
            )
            applied.append(norm["task_id"])
            
        connection.commit()
        return {
            "schema_version": 1,
            "status": "ok",
            "applied": applied,
            "backup": str(backup_path) if backup_path else None
        }
    finally:
        connection.close()

