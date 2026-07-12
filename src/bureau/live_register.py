from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any

from . import legacy
from .core import Registry, StateError, StateStore

LIVE_REGISTER_EVENT_TYPE = "live-register"
LIVE_REGISTER_SCHEMA_VERSION = 2
LIVE_REGISTER_KINDS = {"thread_focus", "candidate_task", "focus_override"}
LIVE_REGISTER_STATUSES = {"active", "paused", "closed", "observed", "promoted", "dropped"}
ACTIVE_LIVE_STATUSES = {"active", "observed"}
_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}$")
_CANDIDATE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")

LIVE_REGISTER_RETENTION_POLICY = {
    "schema_version": 1,
    "policy": "live-register-retention-v1",
    "classes": {
        "thread_focus": {
            "default_retention_days": 30,
            "chronik_export": "optional-redacted-summary",
        },
        "candidate_task": {
            "default_retention_days": 180,
            "chronik_export": "optional-redacted-summary",
        },
        "focus_override": {
            "default_retention_days": 14,
            "chronik_export": "optional-redacted-summary",
        },
    },
    "nonclaims": [
        "automatic_deletion_authority",
        "unredacted_chronik_export",
        "general_chat_memory",
    ],
}


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


def _validate_worker_id(worker_id: str | None) -> str | None:
    if worker_id is None:
        return None
    normalized = worker_id.strip()
    if not _THREAD_ID_RE.fullmatch(normalized):
        raise StateError("worker_id uses the same syntax as thread_id")
    return normalized


def _validate_repo(registry: Registry | None, repo: str | None) -> str | None:
    if repo is None:
        return None
    normalized = repo.strip()
    if len(normalized) > 200:
        raise StateError("live register repo must be at most 200 characters")
    if not normalized.startswith("repo."):
        raise StateError("live register repo must be a repo.* resource")
    if registry is not None and registry.resources.get(normalized) is None:
        raise StateError(f"unknown live register repo resource {normalized}")
    return normalized


def _validate_task(registry: Registry | None, task_id: str | None) -> str | None:
    if task_id is None:
        return None
    normalized = task_id.strip()
    if not normalized:
        raise StateError("live register task must not be empty")
    if len(normalized) > 240:
        raise StateError("live register task must be at most 240 characters")
    if registry is not None and normalized not in registry.tasks:
        raise StateError(f"unknown live register task {normalized}")
    return normalized


def _validate_candidate_id(candidate_id: str | None) -> str | None:
    if candidate_id is None:
        return None
    normalized = candidate_id.strip()
    if not _CANDIDATE_ID_RE.fullmatch(normalized):
        raise StateError(
            "candidate_id must start with an alphanumeric character and contain only "
            "alphanumeric, dot, underscore, colon or dash characters"
        )
    return normalized


def _generated_candidate_id() -> str:
    return f"candidate-{uuid.uuid4().hex}"


def _legacy_candidate_id(event_id: int) -> str:
    return f"candidate-event-{event_id}"


def _candidate_identity(item: dict[str, Any]) -> str:
    payload = item["record"]
    candidate_id = payload.get("candidate_id")
    return str(candidate_id) if candidate_id else _legacy_candidate_id(int(item["event_id"]))


def _candidate_projection(records: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = [
        item for item in records if item["record"].get("kind") == "candidate_task"
    ]
    superseded_event_ids = {
        int(item["record"]["supersedes_event_id"])
        for item in candidates
        if isinstance(item["record"].get("supersedes_event_id"), int)
    }
    latest_by_identity: dict[str, dict[str, Any]] = {}
    for item in sorted(candidates, key=lambda value: int(value["event_id"])):
        if int(item["event_id"]) in superseded_event_ids:
            continue
        latest_by_identity[_candidate_identity(item)] = item
    latest = sorted(latest_by_identity.values(), key=lambda value: int(value["event_id"]))
    return {
        "history_count": len(candidates),
        "superseded_event_count": len(superseded_event_ids),
        "latest": latest,
    }


def _live_nonclaims() -> list[str]:
    return [
        "registry_task_truth",
        "queue_truth",
        "claim_authority",
        "dispatch_authority",
        "merge_readiness",
    ]


def live_register_record(
    registry: Registry | None,
    store: StateStore,
    *,
    kind: str,
    title: str,
    source: str = "operator",
    thread_id: str | None = None,
    worker_id: str | None = None,
    repo: str | None = None,
    task_id: str | None = None,
    candidate_id: str | None = None,
    supersedes_event_id: int | None = None,
    status: str | None = None,
    promotion_required: bool | None = None,
    note: str | None = None,
    catalog_validation: str = "strict",
) -> dict[str, Any]:
    """Append one gitless operational Bureau live-register event."""
    if catalog_validation not in {"strict", "deferred"}:
        raise StateError("catalog_validation must be strict or deferred")
    if catalog_validation == "strict" and registry is None:
        raise StateError("strict catalog validation requires a loaded Bureau registry")
    validation_registry = registry if catalog_validation == "strict" else None
    checked_kind = _validate_kind(kind)
    checked_status = (
        _validate_status(checked_kind, status) if status is not None else None
    )
    checked_title = _optional_text(title, field="title", max_length=240)
    assert checked_title is not None
    checked_source = _optional_text(source, field="source", max_length=80) or "operator"
    checked_thread_id = _validate_thread_id(
        thread_id, required=(checked_kind == "thread_focus")
    )
    checked_worker_id = _validate_worker_id(worker_id)
    checked_repo = _validate_repo(validation_registry, repo)
    checked_task_id = _validate_task(validation_registry, task_id)
    checked_candidate_id = _validate_candidate_id(candidate_id)
    checked_note = _optional_text(note, field="note", max_length=2000)
    if checked_kind in {"thread_focus", "focus_override"} and checked_repo is None:
        raise StateError(f"repo is required for {checked_kind}")
    if supersedes_event_id is not None and supersedes_event_id < 1:
        raise StateError("supersedes_event_id must be a positive integer")
    if checked_kind != "candidate_task" and (
        checked_candidate_id is not None or supersedes_event_id is not None
    ):
        raise StateError(
            "candidate_id and supersedes_event_id are only valid for candidate_task"
        )

    with store.immediate() as connection:
        if checked_kind == "candidate_task":
            rows = connection.execute(
                """
                SELECT event_id, payload_json, created_at
                FROM events
                WHERE event_type=?
                ORDER BY event_id ASC
                """,
                (LIVE_REGISTER_EVENT_TYPE,),
            ).fetchall()
            existing = [_decode_live_row(row) for row in rows]
            candidates = [
                item
                for item in existing
                if item["record"].get("kind") == "candidate_task"
            ]
            if supersedes_event_id is not None:
                previous = next(
                    (
                        item
                        for item in candidates
                        if int(item["event_id"]) == supersedes_event_id
                    ),
                    None,
                )
                if previous is None:
                    raise StateError(
                        "supersedes_event_id must reference a candidate_task event: "
                        f"{supersedes_event_id}"
                    )
                if any(
                    item["record"].get("supersedes_event_id") == supersedes_event_id
                    for item in candidates
                ):
                    raise StateError(
                        f"candidate event {supersedes_event_id} is already superseded"
                    )
                inherited_id = _candidate_identity(previous)
                if checked_candidate_id is not None and checked_candidate_id != inherited_id:
                    raise StateError(
                        "candidate_id must match the superseded candidate identity"
                    )
                previous_repo = previous["record"].get("repo")
                if checked_repo is not None and checked_repo != previous_repo:
                    raise StateError("candidate repo cannot change across supersession")
                checked_repo = checked_repo or previous_repo
                if checked_task_id is None:
                    checked_task_id = previous["record"].get("task_id")
                if promotion_required is None:
                    promotion_required = bool(
                        previous["record"].get("promotion_required", False)
                    )
                if checked_status is None:
                    previous_status = previous["record"].get("status")
                    if not isinstance(previous_status, str) or not previous_status.strip():
                        raise StateError(
                            f"candidate event {supersedes_event_id} is missing required status"
                        )
                    checked_status = _validate_status(checked_kind, previous_status)
                checked_candidate_id = inherited_id
            elif checked_candidate_id is not None and any(
                _candidate_identity(item) == checked_candidate_id for item in candidates
            ):
                raise StateError(
                    "an existing candidate_id requires supersedes_event_id pointing to "
                    "its current event"
                )
            checked_candidate_id = checked_candidate_id or _generated_candidate_id()

        checked_status = checked_status or _validate_status(checked_kind, None)
        payload: dict[str, Any] = {
            "schema_version": LIVE_REGISTER_SCHEMA_VERSION,
            "kind": checked_kind,
            "title": checked_title,
            "source": checked_source,
            "status": checked_status,
            "promotion_required": bool(promotion_required),
            "does_not_establish": _live_nonclaims(),
            "catalog_validation": {
                "mode": catalog_validation,
                "status": "validated" if catalog_validation == "strict" else "deferred",
                "does_not_establish": (
                    []
                    if catalog_validation == "strict"
                    else ["repo_exists", "task_exists", "registry_binding_valid"]
                ),
            },
        }
        optional = {
            "thread_id": checked_thread_id,
            "worker_id": checked_worker_id,
            "repo": checked_repo,
            "task_id": checked_task_id,
            "candidate_id": checked_candidate_id,
            "supersedes_event_id": supersedes_event_id,
            "note": checked_note,
        }
        payload.update({key: value for key, value in optional.items() if value is not None})
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


def _load_live_history(
    store: StateStore, *, limit: int = 50
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if limit < 1 or limit > 500:
        raise StateError("limit must be between 1 and 500")
    with store.connect() as connection:
        total_records = int(
            connection.execute(
                "SELECT COUNT(*) FROM events WHERE event_type=?",
                (LIVE_REGISTER_EVENT_TYPE,),
            ).fetchone()[0]
        )
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
    records = list(reversed([_decode_live_row(row) for row in rows]))
    return records, {
        "history_loaded_records": len(records),
        "history_total_records": total_records,
        "history_truncated": total_records > len(records),
        "oldest_loaded_event_id": int(records[0]["event_id"]) if records else None,
    }


def _load_live_records(store: StateStore, *, limit: int = 50) -> list[dict[str, Any]]:
    records, _metadata = _load_live_history(store, limit=limit)
    return records


def _load_live_projection_records(
    store: StateStore,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load the complete event basis used to derive current live state.

    History limits are presentation controls only. Operational projections must
    never silently derive current state from a truncated history window.
    """
    with store.connect() as connection:
        rows = connection.execute(
            """
            SELECT event_id, payload_json, created_at
            FROM events
            WHERE event_type=?
            ORDER BY event_id ASC
            """,
            (LIVE_REGISTER_EVENT_TYPE,),
        ).fetchall()
    records = [_decode_live_row(row) for row in rows]
    return records, {
        "coverage_complete": True,
        "projection_source": "complete_event_scan",
        "projection_records": len(records),
    }


def _load_live_projection_snapshot(
    store: StateStore, *, limit: int
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
    dict[str, Any],
]:
    if limit < 1 or limit > 500:
        raise StateError("limit must be between 1 and 500")
    projection_records, projection_metadata = _load_live_projection_records(store)
    history_records = projection_records[-limit:]
    history_metadata = {
        "history_loaded_records": len(history_records),
        "history_total_records": projection_metadata["projection_records"],
        "history_truncated": len(projection_records) > len(history_records),
        "oldest_loaded_event_id": (
            int(history_records[0]["event_id"]) if history_records else None
        ),
    }
    return (
        history_records,
        projection_records,
        history_metadata,
        projection_metadata,
    )


def _load_live_record(store: StateStore, event_id: int) -> dict[str, Any]:
    with store.connect() as connection:
        row = connection.execute(
            """
            SELECT event_id, payload_json, created_at
            FROM events
            WHERE event_type=? AND event_id=?
            """,
            (LIVE_REGISTER_EVENT_TYPE, event_id),
        ).fetchone()
    if row is None:
        raise StateError(f"unknown live register event {event_id}")
    return _decode_live_row(row)


def _load_all_candidate_records(store: StateStore) -> list[dict[str, Any]]:
    with store.connect() as connection:
        rows = connection.execute(
            """
            SELECT event_id, payload_json, created_at
            FROM events
            WHERE event_type=?
            ORDER BY event_id ASC
            """,
            (LIVE_REGISTER_EVENT_TYPE,),
        ).fetchall()
    return [
        item
        for item in (_decode_live_row(row) for row in rows)
        if item["record"].get("kind") == "candidate_task"
    ]


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
        if item["record"].get("status") in ACTIVE_LIVE_STATUSES
    ]


def _filter_records(
    records: list[dict[str, Any]],
    *,
    kind: str | None = None,
    repo: str | None = None,
    thread_id: str | None = None,
) -> list[dict[str, Any]]:
    if kind is not None:
        records = [item for item in records if item["record"].get("kind") == kind]
    if repo is not None:
        records = [item for item in records if item["record"].get("repo") == repo]
    if thread_id is not None:
        records = [item for item in records if item["record"].get("thread_id") == thread_id]
    return records


def _summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    active_thread_focus = _active_latest(
        [item for item in records if item["record"].get("kind") == "thread_focus"],
        ("thread_id",),
    )
    active_focus_overrides = _active_latest(
        [item for item in records if item["record"].get("kind") == "focus_override"],
        ("repo",),
    )
    candidate_projection = _candidate_projection(records)
    open_candidates = [
        item
        for item in candidate_projection["latest"]
        if item["record"].get("status") in ACTIVE_LIVE_STATUSES
    ]
    promotion_required = [
        item for item in open_candidates if item["record"].get("promotion_required") is True
    ]
    return {
        "records": len(records),
        "active_thread_focus_count": len(active_thread_focus),
        "active_focus_override_count": len(active_focus_overrides),
        "open_candidate_count": len(open_candidates),
        "candidate_history_count": candidate_projection["history_count"],
        "superseded_candidate_event_count": candidate_projection[
            "superseded_event_count"
        ],
        "promotion_required_count": len(promotion_required),
        "active_thread_focus": active_thread_focus,
        "active_focus_overrides": active_focus_overrides,
        "open_candidates": open_candidates,
        "latest_candidates": candidate_projection["latest"],
        "promotion_required": promotion_required,
    }


def _complete_projection_summary(
    history_records: list[dict[str, Any]],
    projection_records: list[dict[str, Any]],
    *,
    history_metadata: dict[str, Any],
    projection_metadata: dict[str, Any],
) -> dict[str, Any]:
    summary = _summary(projection_records)
    summary.update(
        {
            "records": len(history_records),
            "history_loaded_records": history_metadata["history_loaded_records"],
            "history_total_records": history_metadata["history_total_records"],
            "history_truncated": history_metadata["history_truncated"],
            "oldest_loaded_event_id": history_metadata["oldest_loaded_event_id"],
            "coverage_complete": bool(projection_metadata["coverage_complete"]),
            "projection_source": projection_metadata["projection_source"],
            "projection_records": projection_metadata["projection_records"],
            "projection_matching_records": len(projection_records),
        }
    )
    return summary


def _coverage_fields(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "coverage_complete": summary["coverage_complete"],
        "history_truncated": summary["history_truncated"],
        "oldest_loaded_event_id": summary["oldest_loaded_event_id"],
        "projection_source": summary["projection_source"],
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
    (
        history_records,
        projection_records,
        history_metadata,
        projection_metadata,
    ) = _load_live_projection_snapshot(store, limit=limit)
    history_records = _filter_records(
        history_records, kind=kind, repo=repo, thread_id=thread_id
    )
    projection_records = _filter_records(
        projection_records, kind=kind, repo=repo, thread_id=thread_id
    )
    summary = _complete_projection_summary(
        history_records,
        projection_records,
        history_metadata=history_metadata,
        projection_metadata=projection_metadata,
    )
    return {
        "schema_version": LIVE_REGISTER_SCHEMA_VERSION,
        "command": "live-list",
        "filters": {"kind": kind, "repo": repo, "thread_id": thread_id, "limit": limit},
        "summary": summary,
        "records": history_records,
        **_coverage_fields(summary),
        "nonclaims": _live_nonclaims(),
    }


def live_register_context(
    store: StateStore, *, repo: str | None = None, limit: int = 50
) -> dict[str, Any]:
    (
        history_records,
        projection_records,
        history_metadata,
        projection_metadata,
    ) = _load_live_projection_snapshot(store, limit=limit)
    history_records = _filter_records(history_records, repo=repo)
    projection_records = _filter_records(projection_records, repo=repo)
    summary = _complete_projection_summary(
        history_records,
        projection_records,
        history_metadata=history_metadata,
        projection_metadata=projection_metadata,
    )
    return {
        "schema_version": LIVE_REGISTER_SCHEMA_VERSION,
        "source": "bureau_state_store_events",
        "repo": repo,
        "summary": summary,
        **_coverage_fields(summary),
        "does_not_establish": _live_nonclaims(),
    }


def live_register_repo_context(
    store: StateStore, repo: str, *, limit: int = 100
) -> dict[str, Any]:
    context = live_register_context(store, repo=repo, limit=limit)
    summary = context["summary"]
    return {
        "source": context["source"],
        "repo": repo,
        "active_thread_focus": summary["active_thread_focus"],
        "active_focus_overrides": summary["active_focus_overrides"],
        "open_candidates": summary["open_candidates"],
        "promotion_required": summary["promotion_required"],
        "counts": {
            "active_thread_focus": summary["active_thread_focus_count"],
            "active_focus_overrides": summary["active_focus_override_count"],
            "open_candidates": summary["open_candidate_count"],
            "promotion_required": summary["promotion_required_count"],
        },
        **_coverage_fields(summary),
        "does_not_establish": context["does_not_establish"],
    }


def _run_repo_resources(registry: Registry, run: dict[str, Any]) -> list[str]:
    task = registry.tasks.get(str(run["task_id"]))
    if task is None:
        return []
    return sorted(
        claim.resource for claim in task.claims if claim.resource.startswith("repo.")
    )


def _repo_has_active_run(registry: Registry, run: dict[str, Any], repo: str) -> bool:
    return any(
        legacy.overlaps(resource, repo, registry.resources)
        for resource in _run_repo_resources(registry, run)
    )


def live_register_conflict_report(
    registry: Registry,
    store: StateStore,
    *,
    repo_ball_report: dict[str, Any] | None = None,
    repo: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    context = live_register_context(store, repo=repo, limit=limit)
    with store.connect() as connection:
        active_runs = [dict(row) for row in store.active_runs(connection)]
    active_focus = context["summary"]["active_thread_focus"]
    findings: list[dict[str, Any]] = []
    if not context["coverage_complete"]:
        findings.append(
            {
                "severity": "blocker",
                "code": "live-register-projection-incomplete",
                "repo": repo,
                "projection_source": context["projection_source"],
                "message": (
                    "Live-register conflict coverage is incomplete; repository "
                    "conflict decisions must fail closed."
                ),
            }
        )
    for focus in active_focus:
        payload = focus["record"]
        focus_repo = payload.get("repo")
        if focus_repo is None:
            continue
        overlapping_runs = [
            run for run in active_runs if _repo_has_active_run(registry, run, str(focus_repo))
        ]
        if overlapping_runs:
            findings.append(
                {
                    "severity": "info",
                    "code": "live-focus-overlaps-active-run",
                    "repo": focus_repo,
                    "event_id": focus["event_id"],
                    "thread_id": payload.get("thread_id"),
                    "worker_id": payload.get("worker_id"),
                    "run_ids": sorted(run["run_id"] for run in overlapping_runs),
                    "task_ids": sorted(run["task_id"] for run in overlapping_runs),
                }
            )
        worker_id = payload.get("worker_id")
        if worker_id:
            worker_runs = [run for run in active_runs if run["worker_id"] == worker_id]
            for run in worker_runs:
                if payload.get("task_id") and payload.get("task_id") == run["task_id"]:
                    continue
                findings.append(
                    {
                        "severity": "blocker",
                        "code": "live-worker-has-different-active-run",
                        "repo": focus_repo,
                        "event_id": focus["event_id"],
                        "thread_id": payload.get("thread_id"),
                        "worker_id": worker_id,
                        "run_id": run["run_id"],
                        "run_task_id": run["task_id"],
                        "focus_task_id": payload.get("task_id"),
                    }
                )
    if repo_ball_report is not None:
        for repo_id, ball in repo_ball_report.get("repo_balls", {}).items():
            if repo is not None and repo_id != repo:
                continue
            blockers = []
            for lane in ball.get("lanes", {}).values():
                for item in lane:
                    blockers.extend(
                        reason for reason in item.get("reasons", []) if "open PR" in reason
                    )
            if blockers:
                findings.append(
                    {
                        "severity": "blocker",
                        "code": "repo-write-open-pr-blocker-visible",
                        "repo": repo_id,
                        "reasons": sorted(set(blockers)),
                    }
                )
    return {
        "schema_version": LIVE_REGISTER_SCHEMA_VERSION,
        "command": "live-conflicts",
        "repo": repo,
        "summary": {
            "findings": len(findings),
            "blockers": sum(1 for item in findings if item["severity"] == "blocker"),
            "active_runs": len(active_runs),
            "live_records": context["summary"]["records"],
            "history_loaded_records": context["summary"]["history_loaded_records"],
            "projection_records": context["summary"]["projection_matching_records"],
            "projection_total_records": context["summary"]["projection_records"],
            "coverage_complete": context["coverage_complete"],
        },
        "findings": findings,
        "live_register": context,
        **_coverage_fields(context["summary"]),
        "does_not_establish": _live_nonclaims(),
    }


def _slug_title(value: str) -> str:
    slug = re.sub(r"[^A-Z0-9]+", "-", value.upper()).strip("-")
    return slug or "LIVE-CANDIDATE"


def _suggested_task_json(
    registry: Registry,
    event: dict[str, Any],
    *,
    task_id: str,
    initiative: str,
) -> dict[str, Any]:
    payload = event["record"]
    repo = payload.get("repo")
    claims = []
    if repo:
        claims.append({"resource": repo, "mode": "write", "isolation": "worktree"})
    task = {
        "schema_version": 1,
        "id": task_id,
        "initiative": initiative,
        "title": payload["title"],
        "state": "planned",
        "goal": payload.get("note") or payload["title"],
        "priority": {"lane": "later", "rank": 900},
        "execution": {"mode": "interactive-agent", "policy": "review-before-effect"},
        "claims": claims,
        "required_capabilities": ["repository", "shell", "grabowski"],
        "depends_on": [],
        "acceptance": [
            {
                "id": "source-event-bound",
                "assertion": (
                    "Task metadata preserves the originating live-register event id "
                    "and source."
                ),
            },
            {
                "id": "reviewed-before-effect",
                "assertion": (
                    "Any repository effect remains review-before-effect and does not "
                    "follow from the live event alone."
                ),
            },
        ],
        "metadata": {
            "source": "bureau_live_register",
            "live_register_event_id": event["event_id"],
            "live_register_candidate_id": payload.get("candidate_id")
            or _legacy_candidate_id(int(event["event_id"])),
            "live_register_created_at": event["created_at"],
            "live_register_source": payload.get("source"),
            "promotion_required_from_event": payload.get("promotion_required", False),
            "does_not_establish": _live_nonclaims(),
        },
    }
    if not claims:
        task["claims"] = [{"resource": "repo.bureau", "mode": "read", "isolation": "none"}]
    repo_resource = registry.resources.get(str(repo)) if repo else None
    if repo_resource is not None and repo_resource.path:
        task["execution"]["working_repository"] = repo_resource.path
    return task


def write_live_promote_plan(
    registry: Registry,
    store: StateStore,
    *,
    event_id: int,
    initiative: str,
    task_id: str | None,
    path: str,
) -> dict[str, Any]:
    if initiative not in registry.initiatives:
        raise StateError(f"unknown initiative {initiative}")
    event = _load_live_record(store, event_id)
    payload = event["record"]
    if payload.get("kind") != "candidate_task":
        raise StateError("only candidate_task live-register events can be promoted")
    projection = _candidate_projection(_load_all_candidate_records(store))
    if not any(int(item["event_id"]) == event_id for item in projection["latest"]):
        raise StateError("cannot promote a superseded candidate_task event")
    if payload.get("status") not in ACTIVE_LIVE_STATUSES:
        raise StateError("only an open candidate_task event can be promoted")
    candidate_task_id = task_id or f"{initiative}-{_slug_title(payload['title'])}"
    if not legacy.ID_RE.fullmatch(candidate_task_id):
        raise StateError("task_id must match Bureau task id syntax")
    if candidate_task_id in registry.tasks:
        raise StateError(f"task {candidate_task_id} already exists")
    task_json = _suggested_task_json(
        registry, event, task_id=candidate_task_id, initiative=initiative
    )
    plan = {
        "schema_version": LIVE_REGISTER_SCHEMA_VERSION,
        "command": "live-promote-plan",
        "event_id": event_id,
        "initiative": initiative,
        "task_id": candidate_task_id,
        "source_event": event,
        "task_json": task_json,
        "review": {"required": True, "status": "pending"},
        "does_not_establish": [
            "queue_mutation",
            "task_verification",
            "claim_authority",
            "dispatch_authority",
            "merge_readiness",
        ],
    }
    unsigned_plan = {key: value for key, value in plan.items() if key != "plan_sha256"}
    plan["plan_sha256"] = legacy.sha256_json(unsigned_plan)
    rendered = json.dumps(plan, indent=2, ensure_ascii=False) + "\n"
    legacy.atomic_write(Path(path).expanduser(), rendered)
    return {"status": "written", "path": path, "plan": plan}


def apply_live_promote_plan(registry: Registry, *, path: str) -> dict[str, Any]:
    plan_path = Path(path).expanduser()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    review = plan.get("review", {})
    if review.get("status") != "reviewed" or not review.get("reviewer"):
        raise StateError("live promotion plan requires review.status=reviewed and reviewer")
    task_json = plan.get("task_json")
    if not isinstance(task_json, dict):
        raise StateError("promotion plan is missing task_json")
    task_id = str(task_json.get("id"))
    if task_id in registry.tasks:
        raise StateError(f"task {task_id} already exists")
    target = registry.root / "registry" / "tasks" / f"{task_id}.json"
    if target.exists():
        raise StateError(f"target task file already exists: {target}")
    legacy.atomic_write(target, json.dumps(task_json, indent=2, ensure_ascii=False) + "\n")
    return {
        "status": "applied",
        "task_id": task_id,
        "task_file": str(target),
        "queue_mutated": False,
        "does_not_establish": ["queue_truth", "task_verification", "merge_readiness"],
    }


def live_register_export(
    store: StateStore,
    *,
    repo: str | None = None,
    limit: int = 100,
    export_format: str = "chronik",
) -> dict[str, Any]:
    if export_format != "chronik":
        raise StateError("only chronik export format is supported")
    records = _filter_records(_load_live_records(store, limit=limit), repo=repo)
    exported = []
    for item in records:
        payload = item["record"]
        redacted = {
            "event_type": "bureau.live_register.observed",
            "source_event_id": item["event_id"],
            "source_created_at": item["created_at"],
            "kind": payload.get("kind"),
            "status": payload.get("status"),
            "repo": payload.get("repo"),
            "task_id": payload.get("task_id"),
            "thread_id": payload.get("thread_id"),
            "worker_id": payload.get("worker_id"),
            "title": payload.get("title"),
            "promotion_required": payload.get("promotion_required", False),
            "candidate_id": payload.get("candidate_id"),
            "supersedes_event_id": payload.get("supersedes_event_id"),
            "payload_digest": hashlib.sha256(
                legacy.canonical_json(payload).encode("utf-8")
            ).hexdigest(),
        }
        exported.append(redacted)
    return {
        "schema_version": LIVE_REGISTER_SCHEMA_VERSION,
        "command": "live-export",
        "format": export_format,
        "repo": repo,
        "records": exported,
        "retention_policy": LIVE_REGISTER_RETENTION_POLICY,
        "does_not_establish": [
            "chronik_import",
            "unredacted_export",
            "queue_truth",
            "registry_task_truth",
        ],
    }


def live_retention_report(store: StateStore, *, limit: int = 500) -> dict[str, Any]:
    records = _load_live_records(store, limit=limit)
    by_kind: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for item in records:
        payload = item["record"]
        kind = str(payload.get("kind", "unknown"))
        status = str(payload.get("status", "unknown"))
        by_kind[kind] = by_kind.get(kind, 0) + 1
        by_status[status] = by_status.get(status, 0) + 1
    return {
        "schema_version": LIVE_REGISTER_SCHEMA_VERSION,
        "command": "live-retention",
        "summary": {
            "records_sampled": len(records),
            "by_kind": dict(sorted(by_kind.items())),
            "by_status": dict(sorted(by_status.items())),
        },
        "policy": LIVE_REGISTER_RETENTION_POLICY,
        "delete_authority": False,
    }
