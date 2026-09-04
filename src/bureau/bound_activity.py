from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .adapters import AdapterRegistry
from .core import (
    ACTIVE_STATES,
    Registry,
    StateError,
    StateStore,
    plan_sha256,
    sha256_json,
    utc_now,
)
from .v2 import _authoritative_task_registry_from_connection

BOUND_ACTIVITY_KIND = "bureau.bound_activity_heartbeat"
BOUND_ACTIVITY_SOURCE = "bound-activity"
BOUND_ACTIVITY_OUTCOME = "succeeded"
BOUND_ACTIVITY_ADAPTER_EVIDENCE_SOURCE = "adapter.observe"
BOUND_ACTIVITY_UNBOUND_EVIDENCE_SOURCE = "exact-run-binding"
_EVENT_TYPE = "run-heartbeat"
_DIGEST_FIELDS = ("task_sha256", "plan_sha256", "envelope_sha256")
_EXTERNAL_FIELDS = (
    "external_system",
    "external_id",
    "external_state",
    "external_observed_at",
)
_ACTIVITY_FIELDS = {
    "activity_id",
    "run_id",
    "task_id",
    "worker_id",
    *_DIGEST_FIELDS,
    "external_binding",
}
_PAYLOAD_FIELDS = {"kind", "source", "outcome", "activity", "evidence", "heartbeat_at"}
_BOUND_EVIDENCE_FIELDS = {
    "source",
    "external_system",
    "external_id",
    "observed_state",
    "observed_at",
}
_UNBOUND_EVIDENCE_FIELDS = {"source", "binding_status"}
_SUCCESSFUL_EXTERNAL_STATES = {"running", "succeeded"}


def _required_text(name: str, value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise StateError(f"bound activity {name} must be a non-empty trimmed string")
    return value


def _required_digest(name: str, value: str) -> str:
    value = _required_text(name, value)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise StateError(f"bound activity {name} must be a lowercase SHA-256 digest")
    return value


def _external_snapshot(
    *,
    external_unbound: bool,
    external_system: str | None,
    external_id: str | None,
    external_state: str | None,
    external_observed_at: str | None,
) -> dict[str, Any]:
    values = {
        "external_system": external_system,
        "external_id": external_id,
        "external_state": external_state,
        "external_observed_at": external_observed_at,
    }
    provided = {name for name, value in values.items() if value is not None}
    if external_unbound:
        if provided:
            raise StateError(
                "bound activity --external-unbound cannot include external binding fields"
            )
        return {"status": "explicitly-unbound"}
    if provided != set(_EXTERNAL_FIELDS):
        raise StateError(
            "bound activity requires a complete external binding snapshot or --external-unbound"
        )
    return {
        "status": "bound",
        **{
            name: _required_text(name, value) for name, value in values.items() if value is not None
        },
    }


def _activity_request(
    *,
    activity_id: str,
    run_id: str,
    task_id: str,
    worker_id: str,
    task_sha256: str,
    plan_sha256: str,
    envelope_sha256: str,
    external_snapshot: dict[str, Any],
) -> dict[str, Any]:
    activity_id = _required_text("activity_id", activity_id)
    if len(activity_id) > 200:
        raise StateError("bound activity activity_id is too long")
    return {
        "activity_id": activity_id,
        "run_id": _required_text("run_id", run_id),
        "task_id": _required_text("task_id", task_id),
        "worker_id": _required_text("worker_id", worker_id),
        "task_sha256": _required_digest("task_sha256", task_sha256),
        "plan_sha256": _required_digest("plan_sha256", plan_sha256),
        "envelope_sha256": _required_digest("envelope_sha256", envelope_sha256),
        "external_binding": external_snapshot,
    }


def _fresh_activity_evidence(
    activity: dict[str, Any],
    adapter_registry: AdapterRegistry | None,
) -> dict[str, Any]:
    external = activity["external_binding"]
    if external["status"] == "explicitly-unbound":
        return {
            "source": BOUND_ACTIVITY_UNBOUND_EVIDENCE_SOURCE,
            "binding_status": "explicitly-unbound",
        }

    external_system = external["external_system"]
    external_id = external["external_id"]
    if adapter_registry is None:
        raise StateError("bound activity external adapter registry is unavailable; rebind required")
    adapter = adapter_registry.get(external_system)
    if adapter is None:
        reason = adapter_registry.unavailable_reason(external_system)
        detail = f": {reason}" if reason else ""
        raise StateError(
            f"bound activity external adapter {external_system!r} is unavailable{detail}; "
            "rebind required"
        )
    try:
        observation = adapter.observe(external_id)
    except Exception as exc:
        raise StateError(
            f"bound activity fresh external observation failed: {exc}; rebind required"
        ) from exc
    observed_state = getattr(observation, "state", None)
    if observed_state not in _SUCCESSFUL_EXTERNAL_STATES:
        rendered_state = observed_state if isinstance(observed_state, str) else "unknown"
        raise StateError(
            f"bound activity fresh external state {rendered_state!r} is not active; rebind required"
        )
    observed_at = utc_now()
    if observed_at == external["external_observed_at"]:
        raise StateError(
            "bound activity fresh external observation timestamp did not advance; "
            "rebind required"
        )
    return {
        "source": BOUND_ACTIVITY_ADAPTER_EVIDENCE_SOURCE,
        "external_system": external_system,
        "external_id": external_id,
        "observed_state": observed_state,
        "observed_at": observed_at,
    }


def _validated_event_payload(
    payload: dict[str, Any],
    *,
    event_schema_version: int,
    event_run_id: str,
    event_activity_id: str,
) -> dict[str, Any]:
    if event_schema_version != 1:
        raise StateError("bound activity readback event schema version is unsupported")
    if set(payload) != _PAYLOAD_FIELDS:
        raise StateError("bound activity readback payload fields are not exact")
    if payload.get("kind") != BOUND_ACTIVITY_KIND:
        raise StateError("bound activity readback payload kind is invalid")
    if payload.get("source") != BOUND_ACTIVITY_SOURCE:
        raise StateError("bound activity readback payload source is invalid")
    if payload.get("outcome") != BOUND_ACTIVITY_OUTCOME:
        raise StateError("bound activity readback payload outcome is invalid")
    heartbeat_at = payload.get("heartbeat_at")
    _required_text("readback heartbeat_at", heartbeat_at)

    activity = payload.get("activity")
    if not isinstance(activity, dict) or set(activity) != _ACTIVITY_FIELDS:
        raise StateError("bound activity readback activity fields are not exact")
    external = activity.get("external_binding")
    if not isinstance(external, dict):
        raise StateError("bound activity readback external binding must be an object")
    if external.get("status") == "explicitly-unbound":
        if set(external) != {"status"}:
            raise StateError("bound activity readback unbound snapshot fields are not exact")
        external_snapshot = _external_snapshot(
            external_unbound=True,
            external_system=None,
            external_id=None,
            external_state=None,
            external_observed_at=None,
        )
    elif external.get("status") == "bound":
        if set(external) != {"status", *_EXTERNAL_FIELDS}:
            raise StateError("bound activity readback external snapshot fields are not exact")
        external_snapshot = _external_snapshot(
            external_unbound=False,
            **{field: external.get(field) for field in _EXTERNAL_FIELDS},
        )
    else:
        raise StateError("bound activity readback external snapshot status is invalid")

    validated_activity = _activity_request(
        activity_id=activity.get("activity_id"),
        run_id=activity.get("run_id"),
        task_id=activity.get("task_id"),
        worker_id=activity.get("worker_id"),
        task_sha256=activity.get("task_sha256"),
        plan_sha256=activity.get("plan_sha256"),
        envelope_sha256=activity.get("envelope_sha256"),
        external_snapshot=external_snapshot,
    )
    if activity != validated_activity:
        raise StateError("bound activity readback activity binding is malformed")
    if validated_activity["run_id"] != event_run_id:
        raise StateError("bound activity readback event run binding is malformed")
    if validated_activity["activity_id"] != event_activity_id:
        raise StateError("bound activity readback event activity binding is malformed")

    evidence = payload.get("evidence")
    if not isinstance(evidence, dict):
        raise StateError("bound activity readback evidence must be an object")
    if external_snapshot["status"] == "explicitly-unbound":
        if set(evidence) != _UNBOUND_EVIDENCE_FIELDS:
            raise StateError("bound activity readback unbound evidence fields are not exact")
        if (
            evidence.get("source") != BOUND_ACTIVITY_UNBOUND_EVIDENCE_SOURCE
            or evidence.get("binding_status") != "explicitly-unbound"
        ):
            raise StateError("bound activity readback unbound evidence is invalid")
    else:
        if set(evidence) != _BOUND_EVIDENCE_FIELDS:
            raise StateError("bound activity readback external evidence fields are not exact")
        if evidence.get("source") != BOUND_ACTIVITY_ADAPTER_EVIDENCE_SOURCE:
            raise StateError("bound activity readback external evidence source is invalid")
        if evidence.get("external_system") != external_snapshot["external_system"]:
            raise StateError("bound activity readback external evidence system is malformed")
        if evidence.get("external_id") != external_snapshot["external_id"]:
            raise StateError("bound activity readback external evidence id is malformed")
        if evidence.get("observed_state") not in _SUCCESSFUL_EXTERNAL_STATES:
            raise StateError("bound activity readback external evidence state is invalid")
        _required_text("readback evidence observed_at", evidence.get("observed_at"))
        if evidence.get("observed_at") == external_snapshot["external_observed_at"]:
            raise StateError("bound activity readback external evidence timestamp is stale")
    return payload


def _indexed_activity(
    connection: Any,
    activity_id: str,
    *,
    context: str,
) -> tuple[int, str, dict[str, Any]] | None:
    if "activity_id" not in {
        row["name"] for row in connection.execute("PRAGMA table_info(events)")
    }:
        raise StateError(f"bound activity {context} index is unavailable")
    rows = connection.execute(
        "SELECT event_id,run_id,event_type,event_schema_version,activity_id,payload_json "
        "FROM events WHERE activity_id=? ORDER BY event_id",
        (activity_id,),
    ).fetchall()
    if len(rows) > 1:
        raise StateError(f"bound activity {context} evidence is ambiguous")
    if not rows:
        return None
    row = rows[0]
    if row["event_type"] != _EVENT_TYPE:
        raise StateError(f"bound activity {context} event type is invalid")
    try:
        payload = json.loads(row["payload_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise StateError(f"bound activity {context} audit trail contains invalid JSON") from exc
    if not isinstance(payload, dict):
        raise StateError(f"bound activity {context} audit payload must be an object")
    event_run_id = row["run_id"]
    if not isinstance(event_run_id, str) or not event_run_id:
        raise StateError(f"bound activity {context} event run binding is malformed")
    validated = _validated_event_payload(
        payload,
        event_schema_version=row["event_schema_version"],
        event_run_id=event_run_id,
        event_activity_id=row["activity_id"],
    )
    return int(row["event_id"]), event_run_id, validated


def bound_activity_status(
    store: StateStore,
    run_id: str,
    activity_id: str,
) -> dict[str, Any]:
    """Read exact durable bound-activity evidence without changing StateStore state."""

    run_id = _required_text("readback run_id", run_id)
    activity_id = _required_text("readback activity_id", activity_id)
    if len(activity_id) > 200:
        raise StateError("bound activity activity_id is too long")

    with store.connect() as connection:
        indexed = _indexed_activity(connection, activity_id, context="readback")
    if indexed is not None and indexed[1] != run_id:
        raise StateError("bound activity readback run binding mismatch; rebind required")
    payload = indexed[2] if indexed is not None else None
    return {
        "schema_version": 1,
        "kind": "bureau_bound_activity_status",
        "status": "recorded" if payload is not None else "missing",
        "run_id": run_id,
        "activity_id": activity_id,
        "bound_activity": payload,
    }


def _binding_mismatches(row: Any, activity: dict[str, Any]) -> list[str]:
    mismatches = [
        field
        for field in ("run_id", "task_id", "worker_id", *_DIGEST_FIELDS)
        if row[field] != activity[field]
    ]
    external = activity["external_binding"]
    if external["status"] == "explicitly-unbound":
        mismatches.extend(field for field in _EXTERNAL_FIELDS if row[field] is not None)
    else:
        mismatches.extend(field for field in _EXTERNAL_FIELDS if row[field] != external[field])
    return mismatches


def _require_current_run_revision(
    connection: Any,
    registry_root: Path,
    row: Any,
) -> None:
    fresh_source_registry = Registry.load(registry_root)
    fresh_registry, _, _ = _authoritative_task_registry_from_connection(
        fresh_source_registry, connection
    )
    task = fresh_registry.tasks.get(row["task_id"])
    if task is None or task.sha256 != row["task_sha256"]:
        raise StateError("bound activity current TaskSpec differs from run")
    if plan_sha256(fresh_registry, task.initiative) != row["plan_sha256"]:
        raise StateError("bound activity current plan differs from run")

    try:
        envelope = json.loads(row["envelope_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise StateError("bound activity run envelope is invalid") from exc
    if not isinstance(envelope, dict):
        raise StateError("bound activity run envelope must be an object")
    if sha256_json(envelope) != row["envelope_sha256"]:
        raise StateError("bound activity run envelope digest mismatch")
    fresh_source_registry.schemas.validate(
        "execution-envelope", envelope, f"run:{row['run_id']}:bound-activity"
    )
    for field in ("run_id", "task_id", "worker_id", "task_sha256", "plan_sha256"):
        if envelope.get(field) != row[field]:
            raise StateError(f"bound activity run envelope {field} mismatch")


def _existing_activity(
    connection: Any, activity_id: str
) -> tuple[int, str, dict[str, Any]] | None:
    return _indexed_activity(connection, activity_id, context="replay")


def _require_bound_activity_run(
    connection: Any,
    registry_root: Path,
    activity: dict[str, Any],
) -> Any:
    run_id = activity["run_id"]
    row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None or row["state"] not in ACTIVE_STATES:
        raise StateError(f"run {run_id} is not active; bound activity requires rebind")
    mismatches = _binding_mismatches(row, activity)
    if mismatches:
        raise StateError(
            "bound activity binding drift requires rebind: " + ", ".join(mismatches)
        )
    _require_current_run_revision(connection, registry_root, row)
    return row


def _exact_activity_replay(
    store: StateStore,
    connection: Any,
    row: Any,
    activity: dict[str, Any],
) -> dict[str, Any] | None:
    existing = _existing_activity(connection, activity["activity_id"])
    if existing is None:
        return None
    _event_id, event_run_id, payload = existing
    if event_run_id != activity["run_id"] or payload.get("activity") != activity:
        raise StateError("bound activity activity_id was already used for another binding")
    heartbeat_at = payload.get("heartbeat_at")
    if not isinstance(heartbeat_at, str) or not heartbeat_at:
        raise StateError("bound activity replay audit is incomplete")
    return _run_result(
        store,
        connection,
        row,
        payload=payload,
        heartbeat_at=heartbeat_at,
        replayed=True,
    )


def _normal_heartbeat_projection(
    run_id: str,
    heartbeat_at: str,
    *,
    event_id: int | None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "bureau_run_heartbeat_projection",
        "run_id": run_id,
        "status": "normal",
        "canonical_source": "runs.heartbeat_at",
        "heartbeat_at": heartbeat_at,
        "activity_id": None,
        "binding_source": None,
        "evidence_source": "legacy-or-normal-heartbeat",
        "event_id": event_id,
        "rebind_required": False,
        "fail_closed": False,
    }


def _failed_heartbeat_projection(
    run_id: str,
    heartbeat_at: str,
    *,
    event_id: int,
    activity_id: str | None,
    detail: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "bureau_run_heartbeat_projection",
        "run_id": run_id,
        "status": "rebind-required",
        "canonical_source": "fail-closed",
        "heartbeat_at": heartbeat_at,
        "activity_id": activity_id,
        "binding_source": "events.activity_id",
        "evidence_source": "invalid-bound-activity-evidence",
        "event_id": event_id,
        "rebind_required": True,
        "fail_closed": True,
        "reason": detail,
        "detail": detail,
    }


def _heartbeat_projection_from_row(connection: Any, run_row: Any) -> dict[str, Any]:
    run_id = run_row["run_id"]
    heartbeat_at = run_row["heartbeat_at"]
    event_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(events)")
    }
    has_activity_id = "activity_id" in event_columns
    activity_column = "activity_id" if has_activity_id else "NULL AS activity_id"
    schema_column = (
        "event_schema_version"
        if "event_schema_version" in event_columns
        else "NULL AS event_schema_version"
    )
    event = connection.execute(
        "SELECT event_id,run_id,event_type,"
        f"{schema_column},{activity_column},payload_json FROM events "
        "WHERE run_id=? AND event_type=? ORDER BY event_id DESC LIMIT 1",
        (run_id, _EVENT_TYPE),
    ).fetchone()
    if event is None:
        return _normal_heartbeat_projection(run_id, heartbeat_at, event_id=None)
    event_id = int(event["event_id"])
    activity_id = event["activity_id"]
    if activity_id is None:
        try:
            candidate = json.loads(event["payload_json"])
        except (TypeError, json.JSONDecodeError):
            candidate = None
        if isinstance(candidate, dict) and candidate.get("kind") == BOUND_ACTIVITY_KIND:
            return _failed_heartbeat_projection(
                run_id,
                heartbeat_at,
                event_id=event_id,
                activity_id=None,
                detail="bound activity event is missing its indexed activity_id",
            )
        return _normal_heartbeat_projection(run_id, heartbeat_at, event_id=event_id)

    try:
        payload = _validated_event_payload(
            json.loads(event["payload_json"]),
            event_schema_version=event["event_schema_version"],
            event_run_id=event["run_id"],
            event_activity_id=activity_id,
        )
        mismatches = _binding_mismatches(run_row, payload["activity"])
        if mismatches:
            raise StateError(
                "bound activity binding drift requires rebind: " + ", ".join(mismatches)
            )
        if payload["heartbeat_at"] != heartbeat_at:
            raise StateError("bound activity heartbeat no longer matches runs.heartbeat_at")
    except (TypeError, json.JSONDecodeError, StateError) as exc:
        return _failed_heartbeat_projection(
            run_id,
            heartbeat_at,
            event_id=event_id,
            activity_id=activity_id,
            detail=str(exc),
        )
    evidence = payload["evidence"]
    return {
        "schema_version": 1,
        "kind": "bureau_run_heartbeat_projection",
        "run_id": run_id,
        "status": "valid-bound-activity",
        "canonical_source": BOUND_ACTIVITY_SOURCE,
        "heartbeat_at": heartbeat_at,
        "activity_id": activity_id,
        "binding_source": "events.activity_id+exact-run-binding",
        "evidence_source": evidence["source"],
        "event_id": event_id,
        "rebind_required": False,
        "fail_closed": False,
        "observed_state": evidence.get("observed_state"),
        "observed_at": evidence.get("observed_at"),
    }


def run_heartbeat_projection(store: StateStore, run_id: str) -> dict[str, Any]:
    """Classify the canonical heartbeat source for one run without mutation."""

    run_id = _required_text("projection run_id", run_id)
    with store.connect() as connection:
        row = connection.execute(
            "SELECT * FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise StateError(f"unknown run {run_id}")
        return _heartbeat_projection_from_row(connection, row)


def run_heartbeat_projections(
    store: StateStore,
    run_ids: list[str] | tuple[str, ...] | set[str],
) -> list[dict[str, Any]]:
    """Project heartbeat sources for known run ids in deterministic order."""

    requested = sorted({_required_text("projection run_id", item) for item in run_ids})
    if not requested:
        return []
    with store.connect() as connection:
        placeholders = ",".join("?" for _ in requested)
        rows = {
            row["run_id"]: row
            for row in connection.execute(
                f"SELECT * FROM runs WHERE run_id IN ({placeholders})",
                requested,
            )
        }
        missing = [run_id for run_id in requested if run_id not in rows]
        if missing:
            raise StateError("unknown run " + ", ".join(missing))
        return [_heartbeat_projection_from_row(connection, rows[run_id]) for run_id in requested]


def _run_result(
    store: StateStore,
    connection: Any,
    row: Any,
    *,
    payload: dict[str, Any],
    heartbeat_at: str,
    replayed: bool,
) -> dict[str, Any]:
    result = store.public_run(row)
    result["reservations"] = [
        dict(item)
        for item in connection.execute(
            "SELECT resource_id,mode,amount,created_at FROM reservations WHERE run_id=?",
            (row["run_id"],),
        )
    ]
    result["bound_activity"] = {
        "activity_id": payload["activity"]["activity_id"],
        "status": "replayed" if replayed else "recorded",
        "heartbeat_at": heartbeat_at,
        "liveness_truth": "runs.heartbeat_at",
        "event_type": _EVENT_TYPE,
        "payload": payload,
    }
    return result


def bound_activity_heartbeat(
    store: StateStore,
    registry_root: Path,
    run_id: str,
    *,
    activity_id: str,
    task_id: str,
    worker_id: str,
    task_sha256: str,
    plan_sha256: str,
    envelope_sha256: str,
    external_unbound: bool = False,
    external_system: str | None = None,
    external_id: str | None = None,
    external_state: str | None = None,
    external_observed_at: str | None = None,
    adapter_registry: AdapterRegistry | None = None,
) -> dict[str, Any]:
    """Record exact run-bound activity as the canonical run heartbeat.

    Current TaskSpec/plan, stored-envelope validation and exact replay are
    checked before any external observation. After observation, every
    predicate is checked again under ``BEGIN IMMEDIATE`` before the heartbeat
    update and audit event. No lease or process observation is accepted as a
    substitute for the complete run binding.
    """

    external_snapshot = _external_snapshot(
        external_unbound=external_unbound,
        external_system=external_system,
        external_id=external_id,
        external_state=external_state,
        external_observed_at=external_observed_at,
    )
    activity = _activity_request(
        activity_id=activity_id,
        run_id=run_id,
        task_id=task_id,
        worker_id=worker_id,
        task_sha256=task_sha256,
        plan_sha256=plan_sha256,
        envelope_sha256=envelope_sha256,
        external_snapshot=external_snapshot,
    )
    active_placeholders = ",".join("?" for _ in ACTIVE_STATES)

    with store.connect() as connection:
        row = _require_bound_activity_run(connection, registry_root, activity)
        replay = _exact_activity_replay(store, connection, row, activity)
        if replay is not None:
            return replay

    evidence = _fresh_activity_evidence(activity, adapter_registry)

    with store.immediate() as connection:
        row = _require_bound_activity_run(connection, registry_root, activity)
        replay = _exact_activity_replay(store, connection, row, activity)
        if replay is not None:
            return replay

        now = utc_now()
        parameters: list[Any] = [
            now,
            now,
            run_id,
            *ACTIVE_STATES,
            task_id,
            worker_id,
            task_sha256,
            plan_sha256,
            envelope_sha256,
        ]
        predicates = [
            "run_id=?",
            f"state IN ({active_placeholders})",
            "task_id=?",
            "worker_id=?",
            "task_sha256=?",
            "plan_sha256=?",
            "envelope_sha256=?",
        ]
        if external_snapshot["status"] == "explicitly-unbound":
            predicates.extend(f"{field} IS NULL" for field in _EXTERNAL_FIELDS)
        else:
            predicates.extend(f"{field}=?" for field in _EXTERNAL_FIELDS)
            parameters.extend(external_snapshot[field] for field in _EXTERNAL_FIELDS)
        updated = connection.execute(
            "UPDATE runs SET heartbeat_at=?,updated_at=? WHERE " + " AND ".join(predicates),
            parameters,
        )
        if updated.rowcount != 1:
            raise StateError("bound activity binding changed during update; rebind required")
        worker_updated = connection.execute(
            "UPDATE workers SET heartbeat_at=? WHERE worker_id=?",
            (now, worker_id),
        )
        if worker_updated.rowcount != 1:
            raise StateError("bound activity worker binding disappeared; rebind required")
        payload = {
            "kind": BOUND_ACTIVITY_KIND,
            "source": BOUND_ACTIVITY_SOURCE,
            "outcome": BOUND_ACTIVITY_OUTCOME,
            "activity": activity,
            "evidence": evidence,
            "heartbeat_at": now,
        }
        store.event(connection, _EVENT_TYPE, payload, run_id, activity_id=activity_id)
        updated_row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        assert updated_row is not None
        return _run_result(
            store,
            connection,
            updated_row,
            payload=payload,
            heartbeat_at=now,
            replayed=False,
        )
