from __future__ import annotations

import fcntl, hashlib, json, os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .cycle_contract import CONTRACT_VERSION, SCHEMA_VERSION, atomic_json, cycle_id, load_json, utc_now, validate_receipt

STATE = Path(os.environ.get("BUREAU_CURATOR_STATE_ROOT", Path.home() / ".local/state/bureau-curator")).expanduser()
SCANNER_STATE = Path(os.environ.get("BUREAU_SCANNER_STATE_ROOT", Path.home() / ".local/state/bureau-halfhour-operator")).expanduser()
RUNS = STATE / "runs"
LOCKS = STATE / "locks"
SEEN = STATE / "seen.json"
LOCK = LOCKS / "curator.lock"

def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def write(receipt: dict[str, Any]) -> None:
    atomic_json(Path(receipt["receipt_path"]), receipt)
    atomic_json(STATE / "latest.json", receipt)

def base_receipt(stamp: str, run_id: str, started_at: str, result: str, degraded: bool) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "cycle_id": cycle_id(),
        "stage": "curator",
        "run_id": run_id,
        "trigger": "chatgpt-quarter-to",
        "started_at": started_at,
        "finished_at": utc_now(),
        "lifecycle_state": "terminal",
        "result": result,
        "degraded": degraded,
        "input_receipts": [],
        "inspected_scanner_run_id": None,
        "scanner_gates": {},
        "candidate_counts": {"raw": 0, "inspected": 0, "confirmed": 0, "duplicate": 0, "obsolete": 0, "rejected": 0, "informational": 0},
        "confirmed_count": 0,
        "duplicate_count": 0,
        "obsolete_count": 0,
        "rejected_noise_count": 0,
        "decisions": [],
        "promoted_planned_task_ids": [],
        "promoted_ready_task_ids": [],
        "queue_changes": [],
        "backpressure": {"cycle_id": cycle_id(), "updated_at": utc_now(), "degraded": degraded, "critical_findings": [], "allow_next_dispatch": not degraded},
        "evidence": [],
        "next_action": "No candidates in scanner handoff; keep cycle read-only.",
        "receipt_path": str(RUNS / f"{stamp}-{run_id}.json"),
    }

def main() -> int:
    for directory in (STATE, RUNS, LOCKS):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    started_at = utc_now()
    run_id = f"curator-{stamp}"
    lock_handle = LOCK.open("a+")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        receipt = base_receipt(stamp, run_id, started_at, "blocked", False)
        receipt["evidence"] = [{"kind": "lock", "value": "curator-already-running"}]
        receipt["next_action"] = "allow the existing curator invocation to finish"
        write(receipt)
        print(json.dumps({"status": "blocked", "cycle_id": receipt["cycle_id"], "report": receipt["receipt_path"]}))
        return 0
    scanner_latest = SCANNER_STATE / "latest.json"
    scanner = load_json(scanner_latest, None)
    receipt = base_receipt(stamp, run_id, started_at, "idle", False)
    receipt["input_receipts"] = [str(scanner_latest)]
    if isinstance(scanner, dict) and scanner_latest.exists():
        receipt["evidence"].append({"kind": "scanner_latest", "path": str(scanner_latest), "sha256": sha256_file(scanner_latest)})
        path_value = scanner.get("receipt_path")
        if isinstance(path_value, str) and path_value:
            scanner_path = Path(path_value).expanduser()
            if scanner_path.exists() and scanner_path != scanner_latest:
                receipt["input_receipts"].append(str(scanner_path))
                receipt["evidence"].append({"kind": "scanner_receipt_path", "path": str(scanner_path), "sha256": sha256_file(scanner_path)})
        receipt["inspected_scanner_run_id"] = scanner.get("run_id")
        candidates = scanner.get("new_candidates") if isinstance(scanner.get("new_candidates"), list) else []
        receipt["candidate_counts"]["raw"] = len(candidates)
        receipt["candidate_counts"]["inspected"] = len(candidates)
        errors = validate_receipt(scanner, expected_stage="scanner", expected_cycle_id=receipt["cycle_id"])
        receipt["scanner_gates"] = {"valid_contract": not errors, "promotion_allowed": bool(scanner.get("promotion_allowed"))}
        if errors or scanner.get("degraded") or scanner.get("result") in {"partial", "failed"}:
            receipt["result"] = "partial"
            receipt["degraded"] = True
            receipt["backpressure"]["degraded"] = True
            receipt["backpressure"]["allow_next_dispatch"] = False
            if errors:
                receipt["evidence"].append({"kind": "scanner_contract_errors", "value": errors})
            receipt["next_action"] = "Scanner handoff is degraded or invalid; do not promote raw candidates."
        elif candidates:
            receipt["next_action"] = "Raw candidates detected; keep read-only until an explicit promotion policy exists."
    else:
        receipt["result"] = "partial"
        receipt["degraded"] = True
        receipt["backpressure"]["degraded"] = True
        receipt["backpressure"]["allow_next_dispatch"] = False
        receipt["evidence"].append({"kind": "scanner_missing", "path": str(scanner_latest)})
        receipt["next_action"] = "No scanner handoff; repair scanner before promotion."
    seen = load_json(SEEN, {"seen_scanner_run_ids": []})
    ids = list(seen.get("seen_scanner_run_ids", [])) if isinstance(seen, dict) else []
    if receipt["inspected_scanner_run_id"] and receipt["inspected_scanner_run_id"] not in ids:
        ids.append(receipt["inspected_scanner_run_id"])
    atomic_json(SEEN, {"schema_version": SCHEMA_VERSION, "contract_version": CONTRACT_VERSION, "updated_at": utc_now(), "seen_scanner_run_ids": ids[-200:]})
    receipt["evidence"].append({"kind": "curator_seen_after", "path": str(SEEN), "sha256": sha256_file(SEEN)})
    receipt["finished_at"] = utc_now()
    errors = validate_receipt(receipt, expected_stage="curator", expected_cycle_id=receipt["cycle_id"])
    if errors:
        raise RuntimeError("curator receipt contract failed: " + "; ".join(errors))
    write(receipt)
    print(json.dumps({"status": "ok", "cycle_id": receipt["cycle_id"], "result": receipt["result"], "degraded": receipt["degraded"], "report": receipt["receipt_path"]}))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
