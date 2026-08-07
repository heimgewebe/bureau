from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

EVENT_SCHEMA_VERSION = 1
PROJECTION_SCHEMA_VERSION = 1
PROJECTION_EVENT_TYPE = "state-projection-v1"

# Version-1 writers are deliberately closed over this vocabulary. Historical
# rows migrated from schema 3 remain event_schema_version=0 and are replayed
# only through the migration baseline.
OPERATIONAL_EVENT_TYPES = frozenset(
    {
        "coordinated-claim-intent-issued",
        "dispatch-prepared",
        "dispatch-recovered",
        "dispatch-uncertain",
        "external-bound",
        "external-succeeded",
        "external-terminal",
        "initiative-state-set",
        "run-claimed",
        "run-claimed-coordinated",
        "run-completed",
        "run-failed",
        "run-heartbeat",
        "run-orphaned",
        "workspace-created",
        "workspace-preserved",
        "workspace-removed",
    }
)
EVENT_TYPES = OPERATIONAL_EVENT_TYPES | {PROJECTION_EVENT_TYPE}
PROJECTION_KEYS = ("tasks", "initiatives", "claims", "runs", "acceptances")

_RUN_FIELDS = (
    "run_id",
    "task_id",
    "worker_id",
    "attempt",
    "state",
    "task_sha256",
    "plan_sha256",
    "envelope_sha256",
    "dispatch_request_id",
    "external_system",
    "external_id",
    "external_state",
    "workspace_path",
    "workspace_branch",
    "error",
)
_TASK_FIELDS = (
    "task_id",
    "task_sha256",
    "plan_sha256",
    "state",
    "receipt_sha256",
)
_INITIATIVE_FIELDS = ("initiative_id", "state")
_ACCEPTANCE_FIELDS = ("run_id", "receipt_sha256")
_CLAIM_FIELDS = ("resource_id", "mode", "amount")
_REQUIRED_TABLE_COLUMNS = {
    "runs": set(_RUN_FIELDS),
    "reservations": {"run_id", *_CLAIM_FIELDS},
    "task_status": set(_TASK_FIELDS),
    "receipts": set(_ACCEPTANCE_FIELDS),
    "initiative_status": set(_INITIATIVE_FIELDS),
    "events": {
        "event_id",
        "run_id",
        "event_type",
        "event_schema_version",
        "payload_json",
        "created_at",
    },
}


class StateEventError(ValueError):
    pass


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")}


def validate_projection_schema(connection: sqlite3.Connection) -> None:
    missing: dict[str, list[str]] = {}
    for table, required in _REQUIRED_TABLE_COLUMNS.items():
        observed = _table_columns(connection, table)
        absent = sorted(required - observed)
        if absent:
            missing[table] = absent
    if missing:
        raise StateEventError(f"state projection schema drift: {missing}")


def _row_projection(row: sqlite3.Row | None, fields: tuple[str, ...]) -> dict[str, Any] | None:
    if row is None:
        return None
    return {field: row[field] for field in fields}


def current_projection(connection: sqlite3.Connection) -> dict[str, Any]:
    validate_projection_schema(connection)
    tasks = {
        str(row["task_id"]): _row_projection(row, _TASK_FIELDS)
        for row in connection.execute(
            f"SELECT {','.join(_TASK_FIELDS)} FROM task_status ORDER BY task_id"
        )
    }
    initiatives = {
        str(row["initiative_id"]): _row_projection(row, _INITIATIVE_FIELDS)
        for row in connection.execute(
            f"SELECT {','.join(_INITIATIVE_FIELDS)} FROM initiative_status ORDER BY initiative_id"
        )
    }
    runs = {
        str(row["run_id"]): _row_projection(row, _RUN_FIELDS)
        for row in connection.execute(
            f"SELECT {','.join(_RUN_FIELDS)} FROM runs ORDER BY run_id"
        )
    }
    claims: dict[str, list[dict[str, Any]]] = {run_id: [] for run_id in runs}
    for row in connection.execute(
        "SELECT run_id,resource_id,mode,amount FROM reservations "
        "ORDER BY run_id,resource_id"
    ):
        claims.setdefault(str(row["run_id"]), []).append(
            {field: row[field] for field in _CLAIM_FIELDS}
        )
    acceptances = {
        str(row["run_id"]): _row_projection(row, _ACCEPTANCE_FIELDS)
        for row in connection.execute("SELECT run_id,receipt_sha256 FROM receipts ORDER BY run_id")
    }
    return {
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "tasks": tasks,
        "initiatives": initiatives,
        "claims": claims,
        "runs": runs,
        "acceptances": acceptances,
    }


def projection_root(projection: Mapping[str, Any]) -> str:
    validate_projection_value(projection)
    return sha256_json(projection)


def validate_projection_value(projection: Mapping[str, Any]) -> None:
    if not isinstance(projection, Mapping):
        raise StateEventError("projection must be an object")
    expected = {"schema_version", *PROJECTION_KEYS}
    if set(projection) != expected:
        raise StateEventError("projection fields are not exact")
    if projection.get("schema_version") != PROJECTION_SCHEMA_VERSION:
        raise StateEventError("projection schema version is unsupported")
    for key in PROJECTION_KEYS:
        if not isinstance(projection.get(key), Mapping):
            raise StateEventError(f"projection {key} must be an object")


def baseline_payload(connection: sqlite3.Connection, *, trigger: str) -> dict[str, Any]:
    projection = current_projection(connection)
    return {
        "schema_version": EVENT_SCHEMA_VERSION,
        "mode": "baseline",
        "trigger": trigger,
        "projection": projection,
        "root_sha256": projection_root(projection),
    }


def _run_initiative_id(connection: sqlite3.Connection, run_id: str) -> str | None:
    row = connection.execute(
        "SELECT envelope_json FROM runs WHERE run_id=?", (run_id,)
    ).fetchone()
    if row is None:
        return None
    try:
        envelope = json.loads(row["envelope_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise StateEventError("run envelope JSON is invalid") from exc
    task = envelope.get("task") if isinstance(envelope, dict) else None
    initiative = task.get("initiative") if isinstance(task, dict) else None
    return initiative if isinstance(initiative, str) and initiative else None


def delta_payload(
    connection: sqlite3.Connection,
    *,
    trigger: str,
    run_id: str | None = None,
    initiative_id: str | None = None,
) -> dict[str, Any] | None:
    projection = current_projection(connection)
    changes: dict[str, dict[str, Any]] = {key: {} for key in PROJECTION_KEYS}
    if run_id is not None:
        run = projection["runs"].get(run_id)
        if run is None:
            return None
        changes["runs"][run_id] = run
        changes["claims"][run_id] = projection["claims"].get(run_id, [])
        task_id = run["task_id"]
        changes["tasks"][task_id] = projection["tasks"].get(task_id)
        changes["acceptances"][run_id] = projection["acceptances"].get(run_id)
        bound_initiative = _run_initiative_id(connection, run_id)
        if bound_initiative is not None:
            changes["initiatives"][bound_initiative] = projection["initiatives"].get(
                bound_initiative
            )
    if initiative_id is not None:
        changes["initiatives"][initiative_id] = projection["initiatives"].get(initiative_id)
    if not any(changes[key] for key in PROJECTION_KEYS):
        raise StateEventError("projection delta has no bounded entity")
    return {
        "schema_version": EVENT_SCHEMA_VERSION,
        "mode": "delta",
        "trigger": trigger,
        "changes": changes,
        "root_sha256": projection_root(projection),
    }


def _valid_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def validate_projection_event_payload(payload: Mapping[str, Any]) -> None:
    if payload.get("schema_version") != EVENT_SCHEMA_VERSION:
        raise StateEventError("projection event schema version is unsupported")
    mode = payload.get("mode")
    if mode not in {"baseline", "delta"}:
        raise StateEventError("projection event mode is invalid")
    if not isinstance(payload.get("trigger"), str) or not payload["trigger"]:
        raise StateEventError("projection event trigger is invalid")
    if not _valid_digest(payload.get("root_sha256")):
        raise StateEventError("projection event root digest is invalid")
    if mode == "baseline":
        if set(payload) != {"schema_version", "mode", "trigger", "projection", "root_sha256"}:
            raise StateEventError("baseline projection event fields are not exact")
        validate_projection_value(payload["projection"])
        if projection_root(payload["projection"]) != payload["root_sha256"]:
            raise StateEventError("baseline projection root digest mismatches")
        return
    if set(payload) != {"schema_version", "mode", "trigger", "changes", "root_sha256"}:
        raise StateEventError("delta projection event fields are not exact")
    changes = payload.get("changes")
    if not isinstance(changes, Mapping) or set(changes) != set(PROJECTION_KEYS):
        raise StateEventError("projection delta fields are not exact")
    for key in PROJECTION_KEYS:
        if not isinstance(changes[key], Mapping):
            raise StateEventError(f"projection delta {key} must be an object")


def validate_event(event_type: str, payload: Mapping[str, Any], run_id: str | None) -> None:
    if event_type not in EVENT_TYPES:
        raise StateEventError(f"unknown Bureau state event type: {event_type}")
    if not isinstance(payload, Mapping):
        raise StateEventError("Bureau state event payload must be an object")
    if run_id is not None and (not isinstance(run_id, str) or not run_id):
        raise StateEventError("Bureau state event run_id is invalid")
    if event_type == PROJECTION_EVENT_TYPE:
        validate_projection_event_payload(payload)


def _empty_projection() -> dict[str, Any]:
    return {
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "tasks": {},
        "initiatives": {},
        "claims": {},
        "runs": {},
        "acceptances": {},
    }


def _apply_delta(projection: dict[str, Any], changes: Mapping[str, Any]) -> None:
    for key in PROJECTION_KEYS:
        for entity_id, value in changes[key].items():
            if value is None:
                projection[key].pop(entity_id, None)
            else:
                projection[key][entity_id] = value


def replay(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    projection = _empty_projection()
    baseline_seen = False
    projection_event_count = 0
    last_event_id = -1
    for row in rows:
        try:
            version = int(row["event_schema_version"])
            event_id = int(row["event_id"])
            event_type = str(row["event_type"])
            payload = json.loads(row["payload_json"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StateEventError("state event row is malformed") from exc
        if event_id <= last_event_id:
            raise StateEventError("state event ordering is not strictly increasing")
        last_event_id = event_id
        if version == 0:
            continue
        if version != EVENT_SCHEMA_VERSION:
            raise StateEventError(f"unsupported state event schema version: {version}")
        validate_event(event_type, payload, row.get("run_id"))
        if event_type != PROJECTION_EVENT_TYPE:
            continue
        projection_event_count += 1
        if payload["mode"] == "baseline":
            if baseline_seen:
                raise StateEventError("multiple projection baselines are not allowed")
            projection = json.loads(json.dumps(payload["projection"]))
            baseline_seen = True
        else:
            if not baseline_seen:
                raise StateEventError("projection delta precedes migration baseline")
            _apply_delta(projection, payload["changes"])
        observed_root = projection_root(projection)
        if observed_root != payload["root_sha256"]:
            raise StateEventError("projection replay root digest mismatch")
    if not baseline_seen:
        raise StateEventError("projection migration baseline is missing")
    return {
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "projection": projection,
        "root_sha256": projection_root(projection),
        "projection_event_count": projection_event_count,
        "last_event_id": last_event_id,
    }
