from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

from . import legacy, state_events

TASK_SPEC_SCHEMA_VERSION = 1
TASK_SPEC_EVENT_TYPE = "task-spec-revision-set"
TASK_SPEC_EVENT_SCHEMA_VERSION = state_events.EVENT_SCHEMA_VERSION
TASK_SPEC_PROJECTION_SCHEMA_VERSION = 1


class TaskSpecError(ValueError):
    """Fail-closed TaskSpec revision or replay error."""


def _canonical_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(spec, Mapping):
        raise TaskSpecError("TaskSpec must be an object")
    material = json.loads(legacy.canonical_json(dict(spec)))
    task_id = material.get("id")
    if not isinstance(task_id, str) or not task_id:
        raise TaskSpecError("TaskSpec id must be a non-empty string")
    return material


def task_spec_digest(spec: Mapping[str, Any]) -> str:
    return legacy.sha256_json(_canonical_spec(spec))


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS task_spec_revisions(
            task_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            parent_revision INTEGER,
            spec_sha256 TEXT NOT NULL,
            spec_json TEXT NOT NULL,
            source TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(task_id, revision)
        );
        CREATE TABLE IF NOT EXISTS task_specs(
            task_id TEXT PRIMARY KEY,
            current_revision INTEGER NOT NULL,
            spec_sha256 TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(task_id, current_revision)
                REFERENCES task_spec_revisions(task_id, revision)
        );
        CREATE TABLE IF NOT EXISTS task_spec_mutations(
            idempotency_key TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            expected_revision INTEGER,
            requested_sha256 TEXT NOT NULL,
            resulting_revision INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(task_id, resulting_revision)
                REFERENCES task_spec_revisions(task_id, revision)
        );
        CREATE INDEX IF NOT EXISTS task_spec_revision_digest
            ON task_spec_revisions(spec_sha256);
        """
    )


def validate_schema(connection: sqlite3.Connection) -> None:
    required = {
        "task_spec_revisions": {
            "task_id", "revision", "parent_revision", "spec_sha256", "spec_json",
            "source", "created_at",
        },
        "task_specs": {"task_id", "current_revision", "spec_sha256", "updated_at"},
        "task_spec_mutations": {
            "idempotency_key", "task_id", "expected_revision", "requested_sha256",
            "resulting_revision", "created_at",
        },
    }
    missing: dict[str, list[str]] = {}
    for table, columns in required.items():
        observed = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        absent = sorted(columns - observed)
        if absent:
            missing[table] = absent
    if missing:
        raise TaskSpecError(f"TaskSpec schema drift: {missing}")


def _revision_row(connection: sqlite3.Connection, task_id: str, revision: int) -> sqlite3.Row:
    row = connection.execute(
        "SELECT task_id,revision,parent_revision,spec_sha256,spec_json,source,created_at "
        "FROM task_spec_revisions WHERE task_id=? AND revision=?",
        (task_id, revision),
    ).fetchone()
    if row is None:
        raise TaskSpecError(f"unknown TaskSpec revision: {task_id}@{revision}")
    return row


def _validated_row(row: Mapping[str, Any]) -> dict[str, Any]:
    try:
        spec = json.loads(str(row["spec_json"]))
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise TaskSpecError("TaskSpec revision JSON is invalid") from exc
    canonical = _canonical_spec(spec)
    task_id = str(row["task_id"])
    if canonical["id"] != task_id:
        raise TaskSpecError("TaskSpec revision id does not match row task_id")
    digest = task_spec_digest(canonical)
    if digest != row["spec_sha256"]:
        raise TaskSpecError("TaskSpec revision digest mismatch")
    revision = int(row["revision"])
    parent = row["parent_revision"]
    parent_revision = None if parent is None else int(parent)
    if revision < 1 or parent_revision != (None if revision == 1 else revision - 1):
        raise TaskSpecError("TaskSpec revision lineage is invalid")
    return {
        "task_id": task_id,
        "revision": revision,
        "parent_revision": parent_revision,
        "spec_sha256": digest,
        "spec": canonical,
        "source": str(row["source"]),
        "created_at": str(row["created_at"]),
    }


def get_current(connection: sqlite3.Connection, task_id: str) -> dict[str, Any] | None:
    validate_schema(connection)
    pointer = connection.execute(
        "SELECT task_id,current_revision,spec_sha256,updated_at FROM task_specs WHERE task_id=?",
        (task_id,),
    ).fetchone()
    if pointer is None:
        return None
    revision = _validated_row(_revision_row(connection, task_id, int(pointer["current_revision"])))
    if revision["spec_sha256"] != pointer["spec_sha256"]:
        raise TaskSpecError("TaskSpec current pointer digest mismatch")
    return {**revision, "updated_at": str(pointer["updated_at"])}


def current_projection(connection: sqlite3.Connection) -> dict[str, Any]:
    validate_schema(connection)
    tasks: dict[str, dict[str, Any]] = {}
    for pointer in connection.execute(
        "SELECT task_id,current_revision,spec_sha256,updated_at FROM task_specs ORDER BY task_id"
    ):
        item = get_current(connection, str(pointer["task_id"]))
        assert item is not None
        tasks[item["task_id"]] = {
            "revision": item["revision"],
            "spec_sha256": item["spec_sha256"],
            "spec": item["spec"],
        }
    return {"schema_version": TASK_SPEC_PROJECTION_SCHEMA_VERSION, "tasks": tasks}


def projection_root(projection: Mapping[str, Any]) -> str:
    if not isinstance(projection, Mapping) or set(projection) != {"schema_version", "tasks"}:
        raise TaskSpecError("TaskSpec projection fields are not exact")
    if projection.get("schema_version") != TASK_SPEC_PROJECTION_SCHEMA_VERSION:
        raise TaskSpecError("TaskSpec projection schema version is unsupported")
    if not isinstance(projection.get("tasks"), Mapping):
        raise TaskSpecError("TaskSpec projection tasks must be an object")
    return legacy.sha256_json(dict(projection))


def _event_payload(
    *,
    task_id: str,
    revision: int,
    parent_revision: int | None,
    digest: str,
    spec: Mapping[str, Any],
    source: str,
    idempotency_key: str,
) -> dict[str, Any]:
    return {
        "schema_version": TASK_SPEC_SCHEMA_VERSION,
        "task_id": task_id,
        "revision": revision,
        "parent_revision": parent_revision,
        "spec_sha256": digest,
        "spec": _canonical_spec(spec),
        "source": source,
        "idempotency_key": idempotency_key,
    }


def validate_event_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "schema_version", "task_id", "revision", "parent_revision", "spec_sha256",
        "spec", "source", "idempotency_key",
    }
    if not isinstance(payload, Mapping) or set(payload) != expected:
        raise TaskSpecError("TaskSpec event fields are not exact")
    if payload.get("schema_version") != TASK_SPEC_SCHEMA_VERSION:
        raise TaskSpecError("TaskSpec event payload schema version is unsupported")
    task_id = payload.get("task_id")
    revision = payload.get("revision")
    parent = payload.get("parent_revision")
    source = payload.get("source")
    key = payload.get("idempotency_key")
    if not isinstance(task_id, str) or not task_id:
        raise TaskSpecError("TaskSpec event task_id is invalid")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise TaskSpecError("TaskSpec event revision is invalid")
    if parent != (None if revision == 1 else revision - 1):
        raise TaskSpecError("TaskSpec event parent revision is invalid")
    if not isinstance(source, str) or not source or not isinstance(key, str) or not key:
        raise TaskSpecError("TaskSpec event source/idempotency binding is invalid")
    spec = _canonical_spec(payload.get("spec"))
    if spec["id"] != task_id:
        raise TaskSpecError("TaskSpec event id does not match task_id")
    digest = task_spec_digest(spec)
    if payload.get("spec_sha256") != digest:
        raise TaskSpecError("TaskSpec event digest mismatch")
    return _event_payload(
        task_id=task_id,
        revision=revision,
        parent_revision=parent,
        digest=digest,
        spec=spec,
        source=source,
        idempotency_key=key,
    )


def _insert_event(connection: sqlite3.Connection, payload: Mapping[str, Any]) -> None:
    checked = validate_event_payload(payload)
    connection.execute(
        "INSERT INTO events(run_id,event_type,event_schema_version,payload_json,created_at) "
        "VALUES(NULL,?,?,?,?)",
        (
            TASK_SPEC_EVENT_TYPE,
            TASK_SPEC_EVENT_SCHEMA_VERSION,
            legacy.canonical_json(checked),
            legacy.utc_now(),
        ),
    )


def put(
    connection: sqlite3.Connection,
    spec: Mapping[str, Any],
    *,
    idempotency_key: str,
    expected_revision: int | None,
    source: str,
) -> dict[str, Any]:
    """Apply one TaskSpec CAS mutation inside the caller's IMMEDIATE transaction."""
    validate_schema(connection)
    canonical = _canonical_spec(spec)
    task_id = canonical["id"]
    digest = task_spec_digest(canonical)
    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise TaskSpecError("TaskSpec idempotency key must be non-empty")
    if not isinstance(source, str) or not source:
        raise TaskSpecError("TaskSpec mutation source must be non-empty")
    if expected_revision is not None and (
        not isinstance(expected_revision, int)
        or isinstance(expected_revision, bool)
        or expected_revision < 1
    ):
        raise TaskSpecError("expected TaskSpec revision must be a positive integer or null")

    replay = connection.execute(
        "SELECT task_id,expected_revision,requested_sha256,resulting_revision "
        "FROM task_spec_mutations WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    if replay is not None:
        if (
            replay["task_id"] != task_id
            or replay["expected_revision"] != expected_revision
            or replay["requested_sha256"] != digest
        ):
            raise TaskSpecError("TaskSpec idempotency key is bound to another mutation")
        current = get_current(connection, task_id)
        if current is None or current["revision"] < int(replay["resulting_revision"]):
            raise TaskSpecError("TaskSpec idempotency receipt references an unknown revision")
        result = _validated_row(
            _revision_row(connection, task_id, int(replay["resulting_revision"]))
        )
        if result["spec_sha256"] != digest:
            raise TaskSpecError("TaskSpec idempotency revision digest mismatch")
        return {**result, "idempotent_replay": True, "changed": False}

    current = get_current(connection, task_id)
    current_revision = None if current is None else int(current["revision"])
    if current_revision != expected_revision:
        raise TaskSpecError(
            f"stale TaskSpec revision baseline for {task_id}: "
            f"expected {expected_revision!r}, current {current_revision!r}"
        )
    if current is not None and current["spec_sha256"] == digest:
        connection.execute(
            "INSERT INTO task_spec_mutations("
            "idempotency_key,task_id,expected_revision,requested_sha256,resulting_revision,created_at"
            ") VALUES(?,?,?,?,?,?)",
            (
                idempotency_key,
                task_id,
                expected_revision,
                digest,
                current_revision,
                legacy.utc_now(),
            ),
        )
        return {**current, "idempotent_replay": False, "changed": False}

    revision = 1 if current_revision is None else current_revision + 1
    parent = current_revision
    now = legacy.utc_now()
    connection.execute(
        "INSERT INTO task_spec_revisions("
        "task_id,revision,parent_revision,spec_sha256,spec_json,source,created_at"
        ") VALUES(?,?,?,?,?,?,?)",
        (task_id, revision, parent, digest, legacy.canonical_json(canonical), source, now),
    )
    connection.execute(
        "INSERT INTO task_specs(task_id,current_revision,spec_sha256,updated_at) VALUES(?,?,?,?) "
        "ON CONFLICT(task_id) DO UPDATE SET "
        "current_revision=excluded.current_revision,spec_sha256=excluded.spec_sha256,updated_at=excluded.updated_at",
        (task_id, revision, digest, now),
    )
    connection.execute(
        "INSERT INTO task_spec_mutations("
        "idempotency_key,task_id,expected_revision,requested_sha256,resulting_revision,created_at"
        ") VALUES(?,?,?,?,?,?)",
        (idempotency_key, task_id, expected_revision, digest, revision, now),
    )
    _insert_event(
        connection,
        _event_payload(
            task_id=task_id,
            revision=revision,
            parent_revision=parent,
            digest=digest,
            spec=canonical,
            source=source,
            idempotency_key=idempotency_key,
        ),
    )
    return {
        "task_id": task_id,
        "revision": revision,
        "parent_revision": parent,
        "spec_sha256": digest,
        "spec": canonical,
        "source": source,
        "created_at": now,
        "idempotent_replay": False,
        "changed": True,
    }


def import_registry(connection: sqlite3.Connection, registry: Any) -> dict[str, Any]:
    """Idempotently import exact Git TaskSpecs without changing their semantics."""
    imported = 0
    unchanged = 0
    for task_id in sorted(registry.tasks):
        task = registry.tasks[task_id]
        current = get_current(connection, task_id)
        expected = None if current is None else int(current["revision"])
        digest = task_spec_digest(task.raw)
        if current is not None:
            if current["spec_sha256"] != digest:
                raise TaskSpecError(
                    f"legacy TaskSpec divergence for {task_id}: "
                    "StateStore differs from Git projection"
                )
            unchanged += 1
            continue
        result = put(
            connection,
            task.raw,
            idempotency_key=f"legacy-import:{task_id}:{digest}",
            expected_revision=expected,
            source="legacy-git-import",
        )
        if result["changed"]:
            imported += 1
        else:
            unchanged += 1
    return {"imported": imported, "unchanged": unchanged, "total": len(registry.tasks)}


def seed_missing_registry(
    connection: sqlite3.Connection, registry: Any
) -> dict[str, Any]:
    """Import only missing Git TaskSpecs while preserving authoritative StateStore revisions.

    This is the transition helper for registering one reviewed TaskSpec. Existing
    StateStore specs are validated by ``get_current`` but are never overwritten or
    rejected merely because the Git projection is older. Use ``import_registry``
    when an explicit strict Git/StateStore equality check is intended.
    """
    imported = 0
    unchanged = 0
    divergent_preserved: list[str] = []
    for task_id in sorted(registry.tasks):
        task = registry.tasks[task_id]
        current = get_current(connection, task_id)
        digest = task_spec_digest(task.raw)
        if current is not None:
            if current["spec_sha256"] == digest:
                unchanged += 1
            else:
                divergent_preserved.append(task_id)
            continue
        result = put(
            connection,
            task.raw,
            idempotency_key=f"legacy-seed:{task_id}:{digest}",
            expected_revision=None,
            source="legacy-git-seed",
        )
        if result["changed"]:
            imported += 1
        else:
            unchanged += 1
    sample_limit = 20
    return {
        "imported": imported,
        "unchanged": unchanged,
        "divergent_preserved_count": len(divergent_preserved),
        "divergent_preserved_sample": divergent_preserved[:sample_limit],
        "divergent_preserved_truncated": len(divergent_preserved) > sample_limit,
        "total": len(registry.tasks),
        "mode": "seed-missing-preserve-state-store",
    }


def split_event_rows(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    base: list[Mapping[str, Any]] = []
    task_spec_rows: list[Mapping[str, Any]] = []
    for row in rows:
        if row.get("event_type") == TASK_SPEC_EVENT_TYPE:
            task_spec_rows.append(row)
        else:
            base.append(row)
    return base, task_spec_rows


def replay(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    tasks: dict[str, dict[str, Any]] = {}
    event_count = 0
    last_event_id = -1
    idempotency: set[str] = set()
    for row in rows:
        try:
            event_id = int(row["event_id"])
            version = int(row["event_schema_version"])
            event_type = str(row["event_type"])
            payload = json.loads(str(row["payload_json"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TaskSpecError("TaskSpec event row is malformed") from exc
        if event_id <= last_event_id:
            raise TaskSpecError("TaskSpec event ordering is not strictly increasing")
        last_event_id = event_id
        if event_type != TASK_SPEC_EVENT_TYPE:
            raise TaskSpecError("unexpected event in TaskSpec replay")
        if version != TASK_SPEC_EVENT_SCHEMA_VERSION:
            raise TaskSpecError(f"unsupported TaskSpec event schema version: {version}")
        checked = validate_event_payload(payload)
        key = checked["idempotency_key"]
        if key in idempotency:
            raise TaskSpecError("duplicate TaskSpec idempotency key in event journal")
        idempotency.add(key)
        task_id = checked["task_id"]
        current = tasks.get(task_id)
        expected_parent = None if current is None else current["revision"]
        if checked["parent_revision"] != expected_parent:
            raise TaskSpecError("TaskSpec replay revision lineage mismatch")
        tasks[task_id] = {
            "revision": checked["revision"],
            "spec_sha256": checked["spec_sha256"],
            "spec": checked["spec"],
        }
        event_count += 1
    projection = {"schema_version": TASK_SPEC_PROJECTION_SCHEMA_VERSION, "tasks": tasks}
    return {
        "schema_version": TASK_SPEC_PROJECTION_SCHEMA_VERSION,
        "projection": projection,
        "root_sha256": projection_root(projection),
        "event_count": event_count,
        "last_event_id": last_event_id,
    }


def verify_replay(
    connection: sqlite3.Connection, rows: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    current = current_projection(connection)
    replayed = replay(rows)
    current_root = projection_root(current)
    if replayed["root_sha256"] != current_root:
        raise TaskSpecError(
            "replayed TaskSpec projection does not match current StateStore projection"
        )
    return {**replayed, "current_root_sha256": current_root, "matches_current": True}
