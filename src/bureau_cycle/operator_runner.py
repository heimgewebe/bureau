from __future__ import annotations

import fcntl
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .cycle_contract import (
    CONTRACT_VERSION,
    SCHEMA_VERSION,
    atomic_json,
    begin_receipt,
    classify_task_attention,
    cycle_id,
    load_json,
    utc_now,
    validate_receipt,
)

STATE = Path.home() / ".local/state/bureau-operator"
CURATOR_STATE = Path.home() / ".local/state/bureau-curator"
HEALTH_PATH = Path.home() / ".local/state/bureau-cycle/health.json"
TASK_DB = Path.home() / ".local/state/grabowski/tasks.sqlite3"
RUNS = STATE / "runs"
LOCKS = STATE / "locks"
LOCK = LOCKS / "operator.lock"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def terminal_receipt(stamp: str, run_id: str, started_at: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "cycle_id": cycle_id(),
        "stage": "operator",
        "run_id": run_id,
        "trigger": "chatgpt-hourly",
        "started_at": started_at,
        "finished_at": utc_now(),
        "lifecycle_state": "terminal",
        "result": "idle",
        "degraded": False,
        "input_receipts": [],
        "selected_task_id": None,
        "selected_finding_fingerprint": None,
        "actions": [],
        "active_run_count_before": 0,
        "ready_count_before": 0,
        "queue_changes": [],
        "circuit_breaker": {"active": False, "critical": False, "allow_next_dispatch": True, "reason": None},
        "evidence": [],
        "created_followup_task_ids": [],
        "next_action": "local control operator kept the cycle fresh; no canonical ready task selected",
        "receipt_path": str(RUNS / f"{stamp}-{run_id}.json"),
    }


def publish(receipt: dict[str, Any]) -> None:
    receipt["finished_at"] = utc_now()
    errors = validate_receipt(receipt, expected_stage="operator", expected_cycle_id=receipt["cycle_id"])
    if errors:
        raise RuntimeError("operator receipt contract failed: " + "; ".join(errors))
    atomic_json(Path(receipt["receipt_path"]), receipt)
    atomic_json(STATE / "latest.json", receipt)


def main() -> int:
    for directory in (STATE, RUNS, LOCKS):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    started_at = utc_now()
    run_id = f"operator-{stamp}"
    lock_handle = LOCK.open("a+")
    receipt = terminal_receipt(stamp, run_id, started_at)
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        receipt["result"] = "blocked"
        receipt["actions"].append({"kind": "lease", "decision": "operator_already_running"})
        receipt["next_action"] = "allow the existing operator invocation to finish"
        publish(receipt)
        print(json.dumps({"status": "blocked", "cycle_id": receipt["cycle_id"], "report": receipt["receipt_path"]}))
        return 0

    receipt["actions"].append({"kind": "lease", "decision": "acquired_local_operator_lock"})

    health = load_json(HEALTH_PATH, None)
    if isinstance(health, dict) and HEALTH_PATH.exists():
        receipt["input_receipts"].append({"path": str(HEALTH_PATH), "sha256": sha256_file(HEALTH_PATH)})
        receipt["evidence"].append({"kind": "health", "value": {k: health.get(k) for k in ("cycle_id", "critical", "critical_findings", "degraded", "allow_next_dispatch", "updated_at")}})
        if health.get("critical") or health.get("allow_next_dispatch") is False:
            receipt["result"] = "blocked"
            receipt["degraded"] = True
            receipt["circuit_breaker"] = {"active": True, "critical": bool(health.get("critical")), "allow_next_dispatch": False, "reason": "health gate blocks dispatch"}
            receipt["next_action"] = "repair current critical health gate before work dispatch"
    else:
        receipt["degraded"] = True
        receipt["evidence"].append({"kind": "health_missing", "path": str(HEALTH_PATH)})
        receipt["next_action"] = "verifier should publish fresh health"

    curator_latest = CURATOR_STATE / "latest.json"
    curator = load_json(curator_latest, None)
    if isinstance(curator, dict) and curator_latest.exists():
        receipt["input_receipts"].append({"path": str(curator_latest), "sha256": sha256_file(curator_latest)})
        errors = validate_receipt(curator, expected_stage="curator", require_terminal=True)
        receipt["evidence"].append({
            "kind": "curator_latest",
            "cycle_id": curator.get("cycle_id"),
            "contract_version": curator.get("contract_version"),
            "schema_version": curator.get("schema_version"),
            "result": curator.get("result"),
            "degraded": curator.get("degraded"),
            "valid": not errors,
        })
        if errors or curator.get("degraded"):
            receipt["degraded"] = True
            receipt["actions"].append({"kind": "curator_handoff", "decision": "read_only_invalid_or_degraded", "errors": errors[:20]})
        else:
            receipt["actions"].append({"kind": "curator_handoff", "decision": "read_only_valid"})
    else:
        receipt["degraded"] = True
        receipt["evidence"].append({"kind": "curator_missing", "path": str(curator_latest)})

    try:
        attention = classify_task_attention(TASK_DB, horizon_seconds=10800, limit=5)
    except Exception as exc:  # defensive receipt, not crash
        attention = {"available": False, "error": str(exc)[:500]}
        receipt["degraded"] = True
    receipt["evidence"].append({"kind": "grabowski_attention", "counts": attention.get("counts", {}), "current_attention_count": attention.get("current_attention_count")})
    current_attention = int(attention.get("current_attention_count") or 0)
    if current_attention:
        receipt["actions"].append({"kind": "attention", "decision": "current_attention_observed_read_only", "count": current_attention})
    else:
        receipt["actions"].append({"kind": "attention", "decision": "no_current_attention"})

    if receipt["result"] == "idle":
        receipt["actions"].append({"kind": "reconciliation", "decision": "no_fachliche_work", "reason": "local control operator is non-mutating"})
    receipt["actions"].append({"kind": "lease", "decision": "released_local_operator_lock"})
    publish(receipt)
    print(json.dumps({"status": "ok", "cycle_id": receipt["cycle_id"], "result": receipt["result"], "degraded": receipt["degraded"], "report": receipt["receipt_path"]}))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
