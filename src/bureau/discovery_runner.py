from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import discovery
from .cycle_contract import (
    CONTRACT_VERSION,
    SCHEMA_VERSION,
    atomic_json,
    cycle_id,
    load_json,
    utc_now,
    validate_receipt,
)


def failed_receipt(exc: Exception) -> Path:
    for directory in (discovery.STATE, discovery.RUNS, discovery.INBOX):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    selected_cycle = cycle_id()
    run_id = f"scanner-{stamp}"
    report_path = discovery.RUNS / f"{stamp}-failed.json"
    started_at = utc_now()
    for candidate in sorted(discovery.RUNS.glob("*.json"), reverse=True):
        prior = load_json(candidate, None)
        if not isinstance(prior, dict):
            continue
        if (
            prior.get("stage") == "scanner"
            and prior.get("cycle_id") == selected_cycle
            and prior.get("lifecycle_state") == "running"
        ):
            report_path = candidate
            run_id = str(prior.get("run_id") or run_id)
            started_at = str(prior.get("started_at") or started_at)
            break
    now = utc_now()
    report = {
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "cycle_id": selected_cycle,
        "stage": "scanner",
        "run_id": run_id,
        "scanner_run_id": run_id,
        "trigger": "local-half-hour",
        "schedule_role": "deterministic-discovery-scanner",
        "started_at": started_at,
        "finished_at": now,
        "lifecycle_state": "terminal",
        "result": "failed",
        "degraded": True,
        "baseline": not discovery.SOURCE_STATE.exists(),
        "promotion_allowed": False,
        "source_revisions": [],
        "changed_documents": [],
        "new_candidates": [],
        "resolved_candidate_fingerprints": [],
        "scanner_errors": [{"error": str(exc)[:2000]}],
        "overflow_candidate_count": 0,
        "metrics": {
            "enabled_repository_count": 0,
            "source_revision_count": 0,
            "documents_considered": 0,
            "documents_changed": 0,
            "candidate_count": 0,
            "new_candidate_count": 0,
            "resolved_candidate_count": 0,
            "scanner_error_count": 1,
        },
        "receipt_path": str(report_path),
        "evidence": [],
        "next_action": "repair scanner configuration before candidate promotion",
    }
    errors = validate_receipt(report, expected_stage="scanner", expected_cycle_id=selected_cycle)
    if errors:
        raise RuntimeError("fallback receipt contract failed: " + "; ".join(errors))
    atomic_json(report_path, report)
    atomic_json(discovery.STATE / "latest.json", report)
    atomic_json(discovery.INBOX / f"{selected_cycle}-{stamp}-failed.json", report)
    return report_path


def main() -> int:
    try:
        return discovery.main()
    except Exception as exc:
        path = failed_receipt(exc)
        print(
            json.dumps({"status": "failed", "error": str(exc)[:2000], "report": str(path)}),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
