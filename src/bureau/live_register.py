from __future__ import annotations

import json
import re
from typing import Any

from . import legacy
from .core import Registry, StateError, StateStore

LIVE_REGISTER_EVENT_TYPE = "live-register"
LIVE_REGISTER_SCHEMA_VERSION = 1
LIVE_REGISTER_KINDS = {"thread_focus", "candidate_task", "focus_override"}
LIVE_REGISTER_STATUSES = {"active", "paused", "closed", "observed", "promoted", "dropped"}
_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}$")


def _optional_text(value: str | None, *, field: str, max_length: int) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        raise StateError(f"{field} must not be empty")
    if len(normalized) > max_length:
        raise StateError(f"{field} must be at most {max_length} characters")
    return normalized


def _validate_kind(kind: str) -> str:
    if kind not in LIVE_REGISTER_KINDS:
        allowed = ", ".join(sorted(LIVE_REGISTER_KINDS))
        raise StateError(f"live register kind must be one of: {allowed}")
    return kind


def _validate_status(kind: str, status: str | None) -> str:
    if status is None:
        return "observed" if kind == "candidate_task" else "active"
    if status not in LIVE_REGISTER_STATUSES:
        allowed = ", ".join(sorted(LIVE_REGISTER_STATUSES))
        raise StateError(f"live register status must be one of: {allowed}")
    return status


def _validate_thread_id(thread_id: str | None, *, required: bool) -> str | None:
    if thread_id is None:
        if required:
            raise StateError("thread_id is required for thread_focus")
        return None
    normalized = thread_id.strip()
    if not _THREAD_ID_RE.fullmatch(normalized):
        raise StateError(
            "thread_id must start with an alphanumeric character and contain only "
            "alphanumeric, dot, underscore, colon, at, slash or dash characters"
        )
    return normalized


def _validate_repo(registry: Registry, repo: str | None) -> str | None:
    if repo is None:
        return None
    normalized = repo.strip()
    resource = registry.resources.get(normalized)
    if resource is None:
        raise StateError(f"unknown live register repo resource {normalized}")
    if not normalized.startswith("repo."):
        raise StateError("live register repo must be a repo.* resource")
    return normalized


def _validate_task(registry: Registry, task_id: str | None) -> str | None:
    if task_id is None:
        return None
    normalized = task_id.strip()
    if normalized not in registry.tasks:
        raise StateError(f"unknown live register task {normalized}")
    return normalized


def live_register_record(
    registry: Registry,
    store: StateStore,
    *,
    kind: str,
    title: str,
    source: str = "operator",
    thread_id: str | None = None,
    repo: str | None = None,
    task_id: str | None = None,
    status: str | None = None,
    promotion_required: bool = False,
    note: str | None = None,
) -> dict[str, Any]:
    """Append one gitless operational Bureau live-register event.

    The live register is intentionally a state-store/eventlog surface. It records current operator
    focus and candidate work without mutating registry/queue.json or task files.
    """
    checked_kind = _validate_kind(kind)
    checked_status = _validate_status(checked_kind, status)
    checked_title = _optional_text(title, field="title", max_length=240)
    assert checked_title is not None
    checked_source = _optional_text(source, field="source", max_length=80) or "operator"
    checked_thread_id = _validate_thread_id(
        thread_id, required=(checked_kind == "thread_focus")
    )
    checked_repo = _validate_repo(registry, repo)
    checked_task_id = _validate_task(registry, task_id)
    checked_note = _optional_text(note, field="note", max_length=2000)
    if checked_kind in {"thread_focus", "focus_override"} and checked_repo is None:
        raise StateError(f"repo is required for {checked_kind}")
    payload: dict[str, Any] = {
        "schema_version": LIVE_REGISTER_SCHEMA_VERSION,
        "kind": checked_kind,
        "title": checked_title,
        "source": checked_source,
        "status": checked_status,
        "promotion_required": bool(promotion_required),
        "does_not_establish": [
            "registry_task_truth",
            "queue_truth",
            "claim_authority",
            "dispatch_authority",
            "merge_readiness",
        ],
    }
    optional = {
        "thread_id": checked_thread_id,
        "repo": checked_repo,
        "task_id": checked_task_id,
        "note": checked_note,
    }
    payload.update({key: value for key, value in optional.items() if value is not None})
    with store.immediate() as connection:
        created_at = legacy.utc_now()
        cursor = connection.execute(
            "INSERT INTO events(run_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
            (None, LIVE_REGISTER_EVENT_TYPE, legacy.canonical_json(payload), created_at),
        )
        event_id = int(cursor.lastrowid)
    return {
        "schema_version": LIVE_REGISTER_SCHEMA_VERSION,
        "command": "live-register",
        "event_id": event_id,
        "created_at": created_at,
        "record": payload,
        "nonclaims": payload["does_not_establish"],
    }


def _decode_live_row(row: Any) -> dict[str, Any]:
    payload = json.loads(row["payload_json"])
    return {
        "event_id": row["event_id"],
        "created_at": row["created_at"],
        "record": payload,
    }


def _active_latest(
    records: list[dict[str, Any]], key_fields: tuple[str, ...]
) -> list[dict[str, Any]]:
    latest: dict[tuple[str, ...], dict[str, Any]] = {}
    for item in sorted(records, key=lambda value: int(value["event_id"])):
        payload = item["record"]
        try:
            key = tuple(str(payload[field]) for field in key_fields)
        except KeyError:
            continue
        latest[key] = item
    return [
        item
        for item in sorted(latest.values(), key=lambda value: int(value["event_id"]))
        if item["record"].get("status") in {"active", "observed"}
    ]


def _summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    active_thread_focus = _active_latest(
        [item for item in records if item["record"].get("kind") == "thread_focus"],
        ("thread_id",),
    )
    active_focus_overrides = _active_latest(
        [item for item in records if item["record"].get("kind") == "focus_override"],
        ("repo",),
    )
    open_candidates = [
        item
        for item in records
        if item["record"].get("kind") == "candidate_task"
        and item["record"].get("status") in {"active", "observed"}
    ]
    promotion_required = [
        item for item in open_candidates if item["record"].get("promotion_required") is True
    ]
    return {
        "records": len(records),
        "active_thread_focus_count": len(active_thread_focus),
        "active_focus_override_count": len(active_focus_overrides),
        "open_candidate_count": len(open_candidates),
        "promotion_required_count": len(promotion_required),
        "active_thread_focus": active_thread_focus,
        "active_focus_overrides": active_focus_overrides,
        "promotion_required": promotion_required,
    }


def live_register_list(
    store: StateStore,
    *,
    kind: str | None = None,
    repo: str | None = None,
    thread_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    if kind is not None:
        _validate_kind(kind)
    if limit < 1 or limit > 500:
        raise StateError("limit must be between 1 and 500")
    with store.connect() as connection:
        rows = connection.execute(
            """
            SELECT event_id, payload_json, created_at
            FROM events
            WHERE event_type=?
            ORDER BY event_id DESC
            LIMIT ?
            """,
            (LIVE_REGISTER_EVENT_TYPE, limit),
        ).fetchall()
    records = [_decode_live_row(row) for row in rows]
    records = list(reversed(records))
    if kind is not None:
        records = [item for item in records if item["record"].get("kind") == kind]
    if repo is not None:
        records = [item for item in records if item["record"].get("repo") == repo]
    if thread_id is not None:
        records = [item for item in records if item["record"].get("thread_id") == thread_id]
    return {
        "schema_version": LIVE_REGISTER_SCHEMA_VERSION,
        "command": "live-list",
        "filters": {"kind": kind, "repo": repo, "thread_id": thread_id, "limit": limit},
        "summary": _summary(records),
        "records": records,
        "nonclaims": [
            "registry_task_truth",
            "queue_truth",
            "claim_authority",
            "dispatch_authority",
            "merge_readiness",
        ],
    }
