from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .closure import run_closure_cycle
from .cycle_contract import (
    CONTRACT_VERSION,
    SCHEMA_VERSION,
    atomic_json,
    cycle_id,
    utc_now,
    validate_receipt,
)

STATE = Path.home() / ".local/state/bureau-closure"
RUNS = STATE / "runs"


def main() -> int:
    RUNS.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    selected_cycle = cycle_id()
    run_id = f"closure-{stamp}"
    started_at = utc_now()
    result = "idle"
    degraded = False
    evidence: list[dict[str, object]] = []
    try:
        plan = run_closure_cycle(state_root=STATE)
        result = "completed" if plan.get("selected_lane_count", 0) else "idle"
        evidence.append(
            {
                "kind": "closure_plan",
                "path": str(STATE / "plan.json"),
                "selected_lane_count": plan.get("selected_lane_count", 0),
                "manual_intent_count": plan.get("manual_intent_count", 0),
            }
        )
    except Exception as exc:  # receipt first, crash never silent
        degraded = True
        result = "failed"
        evidence.append({"kind": "closure_error", "error": str(exc)[:2000]})
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "cycle_id": selected_cycle,
        "stage": "watchdog",
        "run_id": run_id,
        "trigger": "local-closure-planner",
        "started_at": started_at,
        "finished_at": utc_now(),
        "lifecycle_state": "terminal",
        "result": result,
        "degraded": degraded,
        "evidence": evidence,
        "next_action": "use closure plan for ChatGPT/Grabowski delegation; keep merge gates hard",
        "receipt_path": str(RUNS / f"{stamp}-{run_id}.json"),
    }
    errors = validate_receipt(receipt, expected_stage="watchdog", expected_cycle_id=selected_cycle)
    if errors:
        raise RuntimeError("closure receipt contract failed: " + "; ".join(errors))
    atomic_json(Path(receipt["receipt_path"]), receipt)
    atomic_json(STATE / "latest.json", receipt)
    print(json.dumps({"status": result, "degraded": degraded, "report": receipt["receipt_path"]}))
    return 0 if not degraded else 1


if __name__ == "__main__":
    raise SystemExit(main())
