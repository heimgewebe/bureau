from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

EVENT_SCHEMA_VERSION = 1
PROJECTION_SCHEMA_VERSION = 1
PROJECTION_EVENT_TYPE = "state-projection-v1"
PROJECTION_REPAIR_EVENT_TYPE = "state-projection-repair-v1"
PROJECTION_REPAIR_KIND = "bureau.state_projection_repair_checkpoint"
PROJECTION_REPAIR_CANDIDATE_KIND = "bureau.state_projection_repair_candidate"
MANUAL_ACCEPTANCE_AUTHENTICATION_EVENT_TYPE = (
    "manual-acceptance-source-authenticated"
)
MANUAL_ACCEPTANCE_AUTHENTICATION_KIND = (
    "bureau.acceptance_source_authentication"
)

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
        MANUAL_ACCEPTANCE_AUTHENTICATION_EVENT_TYPE,
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
EVENT_TYPES = OPERATIONAL_EVENT_TYPES | {PROJECTION_EVENT_TYPE, PROJECTION_REPAIR_EVENT_TYPE}
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


def projection_diff(
    replayed: Mapping[str, Any], current: Mapping[str, Any]
) -> dict[str, dict[str, dict[str, Any]]]:
    validate_projection_value(replayed)
    validate_projection_value(current)
    diff: dict[str, dict[str, dict[str, Any]]] = {key: {} for key in PROJECTION_KEYS}
    for key in PROJECTION_KEYS:
        replayed_values = replayed[key]
        current_values = current[key]
        for entity_id in sorted(set(replayed_values) | set(current_values)):
            replayed_value = replayed_values.get(entity_id)
            current_value = current_values.get(entity_id)
            if replayed_value != current_value:
                diff[key][entity_id] = {
                    "replayed": replayed_value,
                    "current": current_value,
                }
    return diff


def _projection_diff_has_changes(diff: Mapping[str, Any]) -> bool:
    return any(bool(diff.get(key)) for key in PROJECTION_KEYS)


def _event_stream_row_sha256(
    *,
    event_id: int,
    run_id: Any,
    event_type: str,
    event_schema_version: int,
    payload_json: str,
) -> str:
    return sha256_json(
        {
            "event_id": event_id,
            "run_id": run_id,
            "event_type": event_type,
            "event_schema_version": event_schema_version,
            "payload_json": payload_json,
        }
    )


def _validate_projection_diff(diff: Any) -> None:
    if not isinstance(diff, Mapping) or set(diff) != set(PROJECTION_KEYS):
        raise StateEventError("projection repair diff fields are not exact")
    for key in PROJECTION_KEYS:
        values = diff[key]
        if not isinstance(values, Mapping):
            raise StateEventError(f"projection repair diff {key} must be an object")
        for entity_id, value in values.items():
            if not isinstance(entity_id, str) or not entity_id:
                raise StateEventError("projection repair diff entity id is invalid")
            if not isinstance(value, Mapping) or set(value) != {"replayed", "current"}:
                raise StateEventError("projection repair diff entry fields are not exact")


def _apply_projection_repair_diff(
    projection: Mapping[str, Any], diff: Mapping[str, Any]
) -> dict[str, Any]:
    validate_projection_value(projection)
    _validate_projection_diff(diff)
    repaired = json.loads(json.dumps(projection))
    for key in PROJECTION_KEYS:
        for entity_id, entry in diff[key].items():
            observed = repaired[key].get(entity_id)
            if observed != entry["replayed"]:
                raise StateEventError(
                    "projection repair checkpoint replayed value differs"
                )
            current = entry["current"]
            if current is None:
                repaired[key].pop(entity_id, None)
            else:
                repaired[key][entity_id] = json.loads(json.dumps(current))
    validate_projection_value(repaired)
    return repaired


def validate_projection_repair_candidate(candidate: Mapping[str, Any]) -> None:
    expected_fields = {
        "schema_version",
        "kind",
        "previous_repair_event_id",
        "previous_repair_candidate_sha256",
        "repair_through_event_id",
        "segment_event_stream_sha256",
        "first_mismatch_event_id",
        "last_mismatch_event_id",
        "mismatch_count",
        "mismatch_event_ids",
        "mismatch_evidence_sha256",
        "first_mismatch_stored_root_sha256",
        "first_mismatch_observed_root_sha256",
        "historical_replayed_root_sha256",
        "current_root_sha256",
        "diff",
        "diff_sha256",
    }
    if set(candidate) != expected_fields:
        raise StateEventError("projection repair candidate fields are not exact")
    if candidate.get("schema_version") != EVENT_SCHEMA_VERSION:
        raise StateEventError("projection repair candidate schema version is unsupported")
    if candidate.get("kind") != PROJECTION_REPAIR_CANDIDATE_KIND:
        raise StateEventError("projection repair candidate kind is invalid")
    previous_event = candidate.get("previous_repair_event_id")
    previous_sha = candidate.get("previous_repair_candidate_sha256")
    if (previous_event is None) != (previous_sha is None):
        raise StateEventError("projection repair candidate previous anchor is incomplete")
    if previous_event is not None:
        if (
            isinstance(previous_event, bool)
            or not isinstance(previous_event, int)
            or previous_event < 0
        ):
            raise StateEventError("projection repair candidate previous event id is invalid")
        if not _valid_digest(previous_sha):
            raise StateEventError("projection repair candidate previous digest is invalid")
    repair_through = candidate.get("repair_through_event_id")
    if (
        isinstance(repair_through, bool)
        or not isinstance(repair_through, int)
        or repair_through < 0
    ):
        raise StateEventError("projection repair candidate repair horizon is invalid")
    if previous_event is not None and repair_through <= previous_event:
        raise StateEventError("projection repair candidate repair horizon is not after anchor")
    if not _valid_digest(candidate.get("segment_event_stream_sha256")):
        raise StateEventError("projection repair candidate segment digest is invalid")
    mismatch_ids = candidate.get("mismatch_event_ids")
    if not isinstance(mismatch_ids, list) or not mismatch_ids:
        raise StateEventError("projection repair candidate mismatch ids are invalid")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in mismatch_ids
    ):
        raise StateEventError("projection repair candidate mismatch id is invalid")
    if mismatch_ids != sorted(set(mismatch_ids)):
        raise StateEventError(
            "projection repair candidate mismatch ids are not strictly increasing"
        )
    if candidate.get("mismatch_count") != len(mismatch_ids):
        raise StateEventError("projection repair candidate mismatch count is invalid")
    if candidate.get("first_mismatch_event_id") != mismatch_ids[0]:
        raise StateEventError("projection repair candidate first mismatch id is invalid")
    if candidate.get("last_mismatch_event_id") != mismatch_ids[-1]:
        raise StateEventError("projection repair candidate last mismatch id is invalid")
    if mismatch_ids[-1] > repair_through:
        raise StateEventError("projection repair candidate mismatch exceeds repair horizon")
    for field in (
        "mismatch_evidence_sha256",
        "first_mismatch_stored_root_sha256",
        "first_mismatch_observed_root_sha256",
        "historical_replayed_root_sha256",
        "current_root_sha256",
        "diff_sha256",
    ):
        if not _valid_digest(candidate.get(field)):
            raise StateEventError(f"projection repair candidate {field} is invalid")
    _validate_projection_diff(candidate.get("diff"))
    if not _projection_diff_has_changes(candidate["diff"]):
        raise StateEventError("projection repair candidate has no StateStore drift")
    if sha256_json(candidate["diff"]) != candidate["diff_sha256"]:
        raise StateEventError("projection repair candidate diff digest mismatches")


def validate_projection_repair_event_payload(
    payload: Mapping[str, Any], run_id: str | None
) -> None:
    if run_id is not None:
        raise StateEventError("projection repair event must not be run-bound")
    expected_fields = {
        "schema_version",
        "kind",
        "candidate",
        "candidate_sha256",
        "authority",
    }
    if set(payload) != expected_fields:
        raise StateEventError("projection repair event fields are not exact")
    if payload.get("schema_version") != EVENT_SCHEMA_VERSION:
        raise StateEventError("projection repair event schema version is unsupported")
    if payload.get("kind") != PROJECTION_REPAIR_KIND:
        raise StateEventError("projection repair event kind is invalid")
    candidate = payload.get("candidate")
    if not isinstance(candidate, Mapping):
        raise StateEventError("projection repair event candidate is invalid")
    validate_projection_repair_candidate(candidate)
    candidate_sha256 = payload.get("candidate_sha256")
    if not _valid_digest(candidate_sha256) or sha256_json(candidate) != candidate_sha256:
        raise StateEventError("projection repair event candidate digest mismatches")
    authority = payload.get("authority")
    if not isinstance(authority, Mapping) or set(authority) != {
        "schema_version",
        "kind",
        "reviewer",
        "reference",
        "reason",
    }:
        raise StateEventError("projection repair authority fields are not exact")
    if authority.get("schema_version") != EVENT_SCHEMA_VERSION:
        raise StateEventError("projection repair authority schema version is unsupported")
    if authority.get("kind") != "operator":
        raise StateEventError("projection repair authority kind is invalid")
    for field in ("reviewer", "reference", "reason"):
        value = authority.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > 1000:
            raise StateEventError(f"projection repair authority {field} is invalid")


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


def validate_manual_acceptance_authentication_payload(
    payload: Mapping[str, Any], run_id: str | None
) -> None:
    expected_fields = {
        "schema_version",
        "kind",
        "run_id",
        "task_id",
        "task_sha256",
        "plan_sha256",
        "envelope_sha256",
        "criterion_id",
        "verifier",
        "observation_scope",
        "evidence_sha256",
        "authority",
        "reviewer",
        "observer",
    }
    if set(payload) != expected_fields:
        raise StateEventError(
            "manual acceptance authentication event fields are not exact"
        )
    if payload.get("schema_version") != EVENT_SCHEMA_VERSION:
        raise StateEventError(
            "manual acceptance authentication schema version is unsupported"
        )
    if payload.get("kind") != MANUAL_ACCEPTANCE_AUTHENTICATION_KIND:
        raise StateEventError("manual acceptance authentication kind is invalid")
    if run_id is None or payload.get("run_id") != run_id:
        raise StateEventError("manual acceptance authentication run binding mismatches")
    for field in ("task_id", "criterion_id", "observation_scope", "reviewer", "observer"):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise StateEventError(
                f"manual acceptance authentication {field} is invalid"
            )
    if len(payload["reviewer"]) > 200:
        raise StateEventError(
            "manual acceptance authentication reviewer is too long"
        )
    if payload.get("reviewer") != payload.get("observer"):
        raise StateEventError(
            "manual acceptance authentication reviewer and observer mismatch"
        )
    if payload.get("verifier") != "manual_observation":
        raise StateEventError("manual acceptance authentication verifier is invalid")
    if payload.get("authority") != "manual":
        raise StateEventError("manual acceptance authentication authority is invalid")
    for field in (
        "task_sha256",
        "plan_sha256",
        "envelope_sha256",
        "evidence_sha256",
    ):
        if not _valid_digest(payload.get(field)):
            raise StateEventError(
                f"manual acceptance authentication {field} is invalid"
            )


def validate_event(event_type: str, payload: Mapping[str, Any], run_id: str | None) -> None:
    if event_type not in EVENT_TYPES:
        raise StateEventError(f"unknown Bureau state event type: {event_type}")
    if not isinstance(payload, Mapping):
        raise StateEventError("Bureau state event payload must be an object")
    if run_id is not None and (not isinstance(run_id, str) or not run_id):
        raise StateEventError("Bureau state event run_id is invalid")
    if event_type == PROJECTION_EVENT_TYPE:
        validate_projection_event_payload(payload)
    elif event_type == PROJECTION_REPAIR_EVENT_TYPE:
        validate_projection_repair_event_payload(payload, run_id)
    elif event_type == MANUAL_ACCEPTANCE_AUTHENTICATION_EVENT_TYPE:
        validate_manual_acceptance_authentication_payload(payload, run_id)


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


def _replay_internal(
    rows: Iterable[Mapping[str, Any]], *, allow_unrepaired_mismatch: bool
) -> dict[str, Any]:
    row_list = list(rows)
    repair_positions: set[int] = set()
    for index, row in enumerate(row_list):
        try:
            if (
                int(row["event_schema_version"]) == EVENT_SCHEMA_VERSION
                and str(row["event_type"]) == PROJECTION_REPAIR_EVENT_TYPE
            ):
                repair_positions.add(index)
        except (KeyError, TypeError, ValueError):
            pass

    projection = _empty_projection()
    baseline_seen = False
    projection_event_count = 0
    repair_checkpoint_count = 0
    last_event_id = -1
    last_repair_event_id: int | None = None
    last_repair_candidate_sha256: str | None = None
    segment_row_sha256s: list[str] = []
    segment_last_event_id: int | None = None
    pending_mismatches: list[dict[str, Any]] = []

    for index, row in enumerate(row_list):
        try:
            version = int(row["event_schema_version"])
            event_id = int(row["event_id"])
            event_type = str(row["event_type"])
            payload_json = row["payload_json"]
            if not isinstance(payload_json, str):
                raise TypeError("payload_json must be a string")
            payload = json.loads(payload_json)
            run_id = row.get("run_id")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StateEventError("state event row is malformed") from exc
        if event_id <= last_event_id:
            raise StateEventError("state event ordering is not strictly increasing")
        last_event_id = event_id

        if version == 0:
            segment_row_sha256s.append(
                _event_stream_row_sha256(
                    event_id=event_id,
                    run_id=run_id,
                    event_type=event_type,
                    event_schema_version=version,
                    payload_json=payload_json,
                )
            )
            segment_last_event_id = event_id
            continue
        if version != EVENT_SCHEMA_VERSION:
            raise StateEventError(f"unsupported state event schema version: {version}")
        validate_event(event_type, payload, run_id)

        if event_type == PROJECTION_REPAIR_EVENT_TYPE:
            if not baseline_seen:
                raise StateEventError("projection repair checkpoint precedes migration baseline")
            if not pending_mismatches:
                raise StateEventError("projection repair checkpoint has no preceding mismatch")
            candidate = payload["candidate"]
            if candidate["previous_repair_event_id"] != last_repair_event_id:
                raise StateEventError("projection repair checkpoint previous event mismatches")
            if (
                candidate["previous_repair_candidate_sha256"]
                != last_repair_candidate_sha256
            ):
                raise StateEventError("projection repair checkpoint previous digest mismatches")
            if segment_last_event_id is None:
                raise StateEventError("projection repair checkpoint segment is empty")
            if candidate["repair_through_event_id"] != segment_last_event_id:
                raise StateEventError("projection repair checkpoint horizon mismatches")
            if (
                candidate["segment_event_stream_sha256"]
                != sha256_json(segment_row_sha256s)
            ):
                raise StateEventError("projection repair checkpoint segment digest mismatches")
            observed_historical_root = projection_root(projection)
            if (
                candidate["historical_replayed_root_sha256"]
                != observed_historical_root
            ):
                raise StateEventError("projection repair checkpoint historical root mismatches")
            mismatch_ids = [item["event_id"] for item in pending_mismatches]
            if candidate["mismatch_event_ids"] != mismatch_ids:
                raise StateEventError("projection repair checkpoint mismatch ids differ")
            if candidate["mismatch_count"] != len(pending_mismatches):
                raise StateEventError("projection repair checkpoint mismatch count differs")
            if (
                candidate["mismatch_evidence_sha256"]
                != sha256_json(pending_mismatches)
            ):
                raise StateEventError("projection repair checkpoint mismatch evidence differs")
            first = pending_mismatches[0]
            if (
                candidate["first_mismatch_stored_root_sha256"]
                != first["stored_root_sha256"]
                or candidate["first_mismatch_observed_root_sha256"]
                != first["observed_root_sha256"]
            ):
                raise StateEventError("projection repair checkpoint first mismatch differs")
            projection = _apply_projection_repair_diff(
                projection, candidate["diff"]
            )
            if projection_root(projection) != candidate["current_root_sha256"]:
                raise StateEventError(
                    "projection repair checkpoint target root mismatches"
                )
            last_repair_event_id = event_id
            last_repair_candidate_sha256 = payload["candidate_sha256"]
            segment_row_sha256s = []
            segment_last_event_id = None
            pending_mismatches = []
            repair_checkpoint_count += 1
            continue

        segment_row_sha256s.append(
            _event_stream_row_sha256(
                event_id=event_id,
                run_id=run_id,
                event_type=event_type,
                event_schema_version=version,
                payload_json=payload_json,
            )
        )
        segment_last_event_id = event_id
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
            pending_mismatches.append(
                {
                    "event_id": event_id,
                    "stored_root_sha256": payload["root_sha256"],
                    "observed_root_sha256": observed_root,
                }
            )
            if not allow_unrepaired_mismatch and not any(
                repair_index > index for repair_index in repair_positions
            ):
                raise StateEventError("projection replay root digest mismatch")

    if not baseline_seen:
        raise StateEventError("projection migration baseline is missing")
    if pending_mismatches and not allow_unrepaired_mismatch:
        raise StateEventError("projection replay root digest mismatch")
    return {
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "projection": projection,
        "root_sha256": projection_root(projection),
        "projection_event_count": projection_event_count,
        "repair_checkpoint_count": repair_checkpoint_count,
        "last_event_id": last_event_id,
        "_pending_mismatches": pending_mismatches,
        "_last_repair_event_id": last_repair_event_id,
        "_last_repair_candidate_sha256": last_repair_candidate_sha256,
        "_segment_row_sha256s": segment_row_sha256s,
        "_segment_last_event_id": segment_last_event_id,
    }


def projection_repair_candidate(
    rows: Iterable[Mapping[str, Any]], current: Mapping[str, Any]
) -> dict[str, Any]:
    validate_projection_value(current)
    replayed = _replay_internal(rows, allow_unrepaired_mismatch=True)
    mismatches = replayed["_pending_mismatches"]
    if not mismatches:
        raise StateEventError("state projection has no unrepaired root mismatch")
    diff = projection_diff(replayed["projection"], current)
    if not _projection_diff_has_changes(diff):
        raise StateEventError(
            "state projection mismatch has no repairable StateStore drift"
        )
    repair_through_event_id = replayed["_segment_last_event_id"]
    if repair_through_event_id is None:
        raise StateEventError("state projection repair segment is empty")
    first = mismatches[0]
    candidate = {
        "schema_version": EVENT_SCHEMA_VERSION,
        "kind": PROJECTION_REPAIR_CANDIDATE_KIND,
        "previous_repair_event_id": replayed["_last_repair_event_id"],
        "previous_repair_candidate_sha256": replayed[
            "_last_repair_candidate_sha256"
        ],
        "repair_through_event_id": repair_through_event_id,
        "segment_event_stream_sha256": sha256_json(
            replayed["_segment_row_sha256s"]
        ),
        "first_mismatch_event_id": first["event_id"],
        "last_mismatch_event_id": mismatches[-1]["event_id"],
        "mismatch_count": len(mismatches),
        "mismatch_event_ids": [item["event_id"] for item in mismatches],
        "mismatch_evidence_sha256": sha256_json(mismatches),
        "first_mismatch_stored_root_sha256": first["stored_root_sha256"],
        "first_mismatch_observed_root_sha256": first["observed_root_sha256"],
        "historical_replayed_root_sha256": replayed["root_sha256"],
        "current_root_sha256": projection_root(current),
        "diff": diff,
        "diff_sha256": sha256_json(diff),
    }
    validate_projection_repair_candidate(candidate)
    return {
        "schema_version": EVENT_SCHEMA_VERSION,
        "kind": "bureau.state_projection_repair_assessment",
        "candidate": candidate,
        "candidate_sha256": sha256_json(candidate),
        "mismatch_count": len(mismatches),
        "first_mismatch_event_id": first["event_id"],
        "last_mismatch_event_id": mismatches[-1]["event_id"],
        "repairable": True,
        "does_not_establish": [
            "repair_authority",
            "checkpoint_append",
            "future_projection_integrity",
        ],
    }


def replay(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    replayed = _replay_internal(rows, allow_unrepaired_mismatch=False)
    return {
        key: value
        for key, value in replayed.items()
        if not key.startswith("_")
    }
