from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

CONTRACT_VERSION = 2
SCHEMA_VERSION = 2
STAGES = {"scanner", "curator", "frontier", "operator", "verifier", "watchdog"}
TERMINAL_RESULTS = {"completed", "partial", "blocked", "idle", "failed"}
TRANSIENT_UNIT_PREFIXES = (
    "grabowski-task-",
    "grabowski-job-",
    "grabowski-browser-worker-",
    "grabowski-gui-worker-",
)
DEFAULT_ATTENTION_HORIZON_SECONDS = 3 * 60 * 60


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def cycle_id(at: datetime | None = None) -> str:
    selected = at or datetime.now(timezone.utc)
    return selected.astimezone(ZoneInfo("Europe/Berlin")).strftime("%Y-%m-%dT%H")


def parse_utc(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must contain a timezone")
    return parsed.astimezone(timezone.utc)


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def stage_state_root(stage: str, state_root: Path | None = None) -> Path:
    if stage not in STAGES:
        raise ValueError(f"unsupported cycle stage: {stage}")
    base = state_root or Path.home() / ".local/state"
    if stage == "scanner":
        return base / "bureau-halfhour-operator"
    if stage == "frontier":
        return base / "bureau-agent-frontier"
    return base / f"bureau-{stage}"


def begin_receipt(
    stage: str,
    trigger: str,
    *,
    state_root: Path | None = None,
    selected_cycle_id: str | None = None,
) -> dict[str, Any]:
    started_at = utc_now()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{stage}-{stamp}-{uuid.uuid4().hex[:12]}"
    root = stage_state_root(stage, state_root)
    receipt_path = root / "runs" / f"{stamp}-{run_id}.json"
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "cycle_id": selected_cycle_id or cycle_id(),
        "stage": stage,
        "run_id": run_id,
        "trigger": trigger,
        "started_at": started_at,
        "finished_at": None,
        "lifecycle_state": "running",
        "result": None,
        "degraded": False,
        "evidence": [],
        "next_action": "finish this cycle stage and publish a terminal receipt",
        "receipt_path": str(receipt_path),
    }
    atomic_json(receipt_path, receipt)
    atomic_json(root / "latest.json", receipt)
    return receipt


def validate_receipt(
    value: Any,
    *,
    expected_stage: str | None = None,
    expected_cycle_id: str | None = None,
    require_terminal: bool = True,
) -> list[str]:
    if not isinstance(value, dict):
        return ["receipt is not an object"]
    errors: list[str] = []
    required = (
        "schema_version",
        "contract_version",
        "cycle_id",
        "stage",
        "run_id",
        "trigger",
        "started_at",
        "lifecycle_state",
        "degraded",
        "evidence",
        "next_action",
    )
    for key in required:
        if key not in value:
            errors.append(f"missing field: {key}")
    if value.get("contract_version") != CONTRACT_VERSION:
        errors.append(
            f"contract_version must be {CONTRACT_VERSION}, got {value.get('contract_version')!r}"
        )
    stage = value.get("stage")
    if stage not in STAGES:
        errors.append(f"unsupported stage: {stage!r}")
    if expected_stage is not None and stage != expected_stage:
        errors.append(f"stage mismatch: expected {expected_stage}, got {stage!r}")
    selected_cycle = value.get("cycle_id")
    if not isinstance(selected_cycle, str) or len(selected_cycle) != 13:
        errors.append("cycle_id must use YYYY-MM-DDTHH")
    if expected_cycle_id is not None and selected_cycle != expected_cycle_id:
        errors.append(f"cycle_id mismatch: expected {expected_cycle_id}, got {selected_cycle!r}")
    try:
        started = parse_utc(str(value.get("started_at")))
    except (TypeError, ValueError):
        errors.append("started_at is not a timezone-aware ISO timestamp")
        started = None
    finished_value = value.get("finished_at")
    lifecycle_state = value.get("lifecycle_state")
    result = value.get("result")
    if require_terminal:
        if lifecycle_state != "terminal":
            errors.append("lifecycle_state is not terminal")
        if result not in TERMINAL_RESULTS:
            errors.append(f"result is not terminal: {result!r}")
        try:
            finished = parse_utc(str(finished_value))
        except (TypeError, ValueError):
            errors.append("finished_at is not a timezone-aware ISO timestamp")
        else:
            if started is not None and finished < started:
                errors.append("finished_at precedes started_at")
    elif lifecycle_state not in {"running", "terminal"}:
        errors.append(f"unsupported lifecycle_state: {lifecycle_state!r}")
    if not isinstance(value.get("degraded"), bool):
        errors.append("degraded must be boolean")
    if not isinstance(value.get("evidence"), list):
        errors.append("evidence must be an array")
    if not isinstance(value.get("next_action"), str):
        errors.append("next_action must be a string")
    if stage == "scanner":
        for key in ("scanner_run_id", "source_revisions", "promotion_allowed"):
            if key not in value:
                errors.append(f"scanner receipt missing field: {key}")
    return errors


def validate_receipt_path(
    path: Path,
    *,
    expected_stage: str | None = None,
    expected_cycle_id: str | None = None,
    require_terminal: bool = True,
) -> dict[str, Any]:
    value = load_json(path, None)
    errors = validate_receipt(
        value,
        expected_stage=expected_stage,
        expected_cycle_id=expected_cycle_id,
        require_terminal=require_terminal,
    )
    return {
        "path": str(path),
        "valid": not errors,
        "errors": errors,
        "receipt": value if isinstance(value, dict) else None,
    }


def _bounded_task(task: sqlite3.Row, age_seconds: int) -> dict[str, Any]:
    return {
        "task_id": task["task_id"],
        "unit": task["unit"],
        "state": task["state"],
        "age_seconds": age_seconds,
        "updated_at_unix": task["updated_at_unix"],
    }


def classify_task_attention(
    task_db: Path,
    *,
    now_unix: int | None = None,
    horizon_seconds: int = DEFAULT_ATTENTION_HORIZON_SECONDS,
    limit: int = 20,
) -> dict[str, Any]:
    selected_now = int(time.time()) if now_unix is None else now_unix
    if horizon_seconds < 60:
        raise ValueError("attention horizon must be at least 60 seconds")
    if limit < 1 or limit > 200:
        raise ValueError("limit must be between 1 and 200")
    if not task_db.is_file():
        return {
            "task_db": str(task_db),
            "available": False,
            "current_attention_count": 0,
            "error": "task database does not exist",
        }
    connection = sqlite3.connect(f"file:{task_db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT task_id, unit, state, runtime_seconds, created_at_unix, updated_at_unix
            FROM tasks
            ORDER BY updated_at_unix DESC, task_id
            """
        ).fetchall()
    finally:
        connection.close()

    groups: dict[str, list[dict[str, Any]]] = {
        "stale_running": [],
        "current_outcome_unknown": [],
        "recent_failed": [],
        "legacy_outcome_unavailable": [],
        "historical_failed": [],
        "terminal_history": [],
        "healthy_running": [],
    }
    counts = {key: 0 for key in groups}
    for row in rows:
        age = max(0, selected_now - int(row["updated_at_unix"]))
        state = str(row["state"])
        if state == "running":
            runtime_deadline = int(row["created_at_unix"]) + int(row["runtime_seconds"]) + 300
            group = "stale_running" if selected_now > runtime_deadline else "healthy_running"
        elif state == "interrupted":
            group = (
                "current_outcome_unknown"
                if age <= horizon_seconds
                else "legacy_outcome_unavailable"
            )
        elif state == "failed":
            group = "recent_failed" if age <= horizon_seconds else "historical_failed"
        else:
            group = "terminal_history"
        counts[group] += 1
        if len(groups[group]) < limit:
            groups[group].append(_bounded_task(row, age))

    current_attention_count = (
        counts["stale_running"] + counts["current_outcome_unknown"] + counts["recent_failed"]
    )
    return {
        "task_db": str(task_db),
        "available": True,
        "generated_at": utc_now(),
        "attention_horizon_seconds": horizon_seconds,
        "task_count": len(rows),
        "current_attention_count": current_attention_count,
        "counts": counts,
        "items": groups,
        "interpretation": {
            "legacy_outcome_unavailable": (
                "historical records without a trustworthy terminal outcome; diagnostic history, "
                "not current attention by age alone"
            ),
            "current_outcome_unknown": (
                "recent interrupted records whose effect may still require verification"
            ),
        },
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="bureau-cycle")
    sub = result.add_subparsers(dest="command", required=True)
    cycle = sub.add_parser("cycle-id")
    cycle.add_argument("--at")
    begin = sub.add_parser("begin")
    begin.add_argument("--stage", required=True, choices=sorted(STAGES))
    begin.add_argument("--trigger", required=True)
    begin.add_argument("--state-root")
    begin.add_argument("--cycle-id")
    validate = sub.add_parser("validate")
    validate.add_argument("receipt")
    validate.add_argument("--stage", choices=sorted(STAGES))
    validate.add_argument("--cycle-id")
    validate.add_argument("--allow-running", action="store_true")
    attention = sub.add_parser("attention")
    attention.add_argument(
        "--task-db",
        default=str(Path.home() / ".local/state/grabowski/tasks.sqlite3"),
    )
    attention.add_argument(
        "--horizon-seconds",
        type=int,
        default=DEFAULT_ATTENTION_HORIZON_SECONDS,
    )
    attention.add_argument("--limit", type=int, default=20)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "cycle-id":
        selected = parse_utc(args.at) if args.at else None
        value: Any = {"cycle_id": cycle_id(selected)}
    elif args.command == "begin":
        value = begin_receipt(
            args.stage,
            args.trigger,
            state_root=Path(args.state_root).expanduser() if args.state_root else None,
            selected_cycle_id=args.cycle_id,
        )
    elif args.command == "validate":
        value = validate_receipt_path(
            Path(args.receipt).expanduser(),
            expected_stage=args.stage,
            expected_cycle_id=args.cycle_id,
            require_terminal=not args.allow_running,
        )
    elif args.command == "attention":
        value = classify_task_attention(
            Path(args.task_db).expanduser(),
            horizon_seconds=args.horizon_seconds,
            limit=args.limit,
        )
    else:
        raise AssertionError(args.command)
    print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))
    return 0 if not isinstance(value, dict) or value.get("valid", True) else 2


if __name__ == "__main__":
    raise SystemExit(main())
