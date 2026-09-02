from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import legacy, task_specs
from .core import (
    Registry,
    StateStore,
    task_revision_sha256,
)
from .core import (
    plan_sha256 as initiative_plan_sha256,
)

PLAN_SCHEMA_VERSION = 1
PLAN_KIND = "bureau.repository_identity_rebind_plan"
RESULT_SCHEMA_VERSION = 1
RESULT_KIND = "bureau.repository_identity_rebind_result"
DEFAULT_MAX_PLAN_AGE_SECONDS = 900
_PLAN_FIELDS = {
    "schema_version",
    "kind",
    "generated_at",
    "old_resource",
    "new_resource",
    "resource_binding_sha256",
    "task_spec_root_sha256_before",
    "items",
    "excluded_active_tasks",
    "summary",
    "plan_sha256",
}
_ITEM_FIELDS = {
    "task_id",
    "effective_state",
    "expected_revision",
    "expected_spec_sha256",
    "resulting_spec_sha256",
    "idempotency_key",
    "changed_paths",
    "acceptance_sha256",
    "acceptance_diagnostics_sha256",
}
_EXCLUDED_FIELDS = {
    "task_id",
    "run_id",
    "run_state",
    "expected_revision",
    "expected_spec_sha256",
}
_RESOURCE_FIELDS = {
    "id",
    "type",
    "parent",
    "capacity",
    "path",
    "github_slug",
    "grabowski_key",
    "criticality",
}
_SUMMARY_FIELDS = {"migration_items", "excluded_active_tasks", "changed_paths"}
_ACTIVE_RUN_STATES = frozenset(task_specs.REPOSITORY_IDENTITY_REBIND_ACTIVE_RUN_STATES)
_TERMINAL_TASK_STATES = task_specs.REPOSITORY_IDENTITY_REBIND_TERMINAL_STATES


def _resource_descriptor(registry: Registry, resource_id: str) -> dict[str, Any]:
    resource = registry.resources.get(resource_id)
    if resource is None:
        raise legacy.StateError(f"unknown repository resource: {resource_id}")
    if resource.type != "git-repository":
        raise legacy.StateError(f"resource {resource_id} is not a git-repository")
    if not resource.path or not resource.path.startswith("/"):
        raise legacy.StateError(f"resource {resource_id} has no absolute repository path")
    if resource.grabowski_key != f"repo:{resource.path}":
        raise legacy.StateError(
            f"resource {resource_id} Grabowski key does not match its repository path"
        )
    if not resource.github_slug or "/" not in resource.github_slug:
        raise legacy.StateError(f"resource {resource_id} has no canonical GitHub slug")
    return {
        "id": resource.id,
        "type": resource.type,
        "parent": resource.parent,
        "capacity": resource.capacity,
        "path": resource.path,
        "github_slug": resource.github_slug,
        "grabowski_key": resource.grabowski_key,
        "criticality": resource.criticality,
    }


def _resource_pair(
    registry: Registry, old_resource_id: str, new_resource_id: str
) -> tuple[dict[str, Any], dict[str, Any], str]:
    old = _resource_descriptor(registry, old_resource_id)
    new = _resource_descriptor(registry, new_resource_id)
    if old["id"] == new["id"] or old["path"] == new["path"]:
        raise legacy.StateError("repository identity rebind resources must be distinct")
    binding_sha256 = legacy.sha256_json({"old_resource": old, "new_resource": new})
    return old, new, binding_sha256


def _task_rows(connection, registry: Registry) -> list[dict[str, Any]]:
    status_columns = {
        str(row["name"]) for row in connection.execute("PRAGMA table_info(task_status)")
    }
    has_revision_bindings = {"task_sha256", "plan_sha256"} <= status_columns
    binding_fields = (
        ",st.task_sha256 AS overlay_task_sha256,"
        "st.plan_sha256 AS overlay_plan_sha256 "
        if has_revision_bindings
        else ",'' AS overlay_task_sha256,'' AS overlay_plan_sha256 "
    )
    rows = connection.execute(
        "SELECT s.task_id,s.current_revision,r.spec_sha256,r.spec_json,"
        "COALESCE(st.state,'') AS overlay_state"
        + binding_fields
        + "FROM task_specs s "
        "JOIN task_spec_revisions r ON r.task_id=s.task_id "
        "AND r.revision=s.current_revision "
        "LEFT JOIN task_status st ON st.task_id=s.task_id "
        "ORDER BY s.task_id"
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        try:
            spec = json.loads(str(row["spec_json"]))
        except json.JSONDecodeError as exc:
            raise legacy.StateError(
                f"TaskSpec JSON is invalid for {row['task_id']}"
            ) from exc
        if not isinstance(spec, dict):
            raise legacy.StateError(f"TaskSpec is not an object for {row['task_id']}")
        spec_state = str(spec.get("state") or "")
        overlay_state = str(row["overlay_state"] or "")
        if spec_state in _TERMINAL_TASK_STATES or not overlay_state:
            state = spec_state
        elif overlay_state not in _TERMINAL_TASK_STATES:
            state = overlay_state
        else:
            initiative_id = str(spec.get("initiative") or "")
            overlay_is_current = (
                has_revision_bindings
                and row["overlay_task_sha256"] == task_revision_sha256(spec)
                and row["overlay_plan_sha256"]
                == initiative_plan_sha256(registry, initiative_id)
            )
            state = overlay_state if overlay_is_current else "stale"
        result.append(
            {
                "task_id": str(row["task_id"]),
                "revision": int(row["current_revision"]),
                "spec_sha256": str(row["spec_sha256"]),
                "spec": spec,
                "effective_state": state,
            }
        )
    return result


def _active_runs(connection) -> dict[str, dict[str, str]]:
    placeholders = ",".join("?" for _ in _ACTIVE_RUN_STATES)
    rows = connection.execute(
        f"SELECT run_id,task_id,state FROM runs WHERE state IN ({placeholders}) "
        "ORDER BY task_id,run_id",
        tuple(sorted(_ACTIVE_RUN_STATES)),
    ).fetchall()
    result: dict[str, dict[str, str]] = {}
    for row in rows:
        task_id = str(row["task_id"])
        if task_id in result:
            raise legacy.StateError(f"multiple active runs exist for task {task_id}")
        result[task_id] = {
            "run_id": str(row["run_id"]),
            "state": str(row["state"]),
        }
    return result


def _has_old_binding(spec: Mapping[str, Any], *, old_resource_id: str, old_path: str) -> bool:
    claims = spec.get("claims")
    if isinstance(claims, list) and any(
        isinstance(claim, Mapping) and claim.get("resource") == old_resource_id
        for claim in claims
    ):
        return True
    execution = spec.get("execution")
    if not isinstance(execution, Mapping):
        return False
    if execution.get("working_repository") == old_path:
        return True
    resources = execution.get("grabowski_resources")
    return isinstance(resources, list) and any(
        isinstance(item, str) and old_path in item for item in resources
    )


def _candidates(
    connection,
    registry: Registry,
    *,
    old_resource_id: str,
    old_path: str,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in _task_rows(connection, registry):
        if item["effective_state"] in _TERMINAL_TASK_STATES:
            continue
        residue = sorted(
            set(
                task_specs._old_repository_binding_residue(
                    item["spec"],
                    old_resource_id=old_resource_id,
                    old_repository_path=old_path,
                )
            )
        )
        has_supported_binding = _has_old_binding(
            item["spec"], old_resource_id=old_resource_id, old_path=old_path
        )
        if residue and not has_supported_binding:
            raise legacy.StateError(
                "repository identity rebind found old technical bindings outside "
                f"approved rebind surfaces for task {item['task_id']}: "
                + ", ".join(residue)
            )
        if has_supported_binding:
            result[item["task_id"]] = item
    return result


def _plan_material(plan: Mapping[str, Any]) -> dict[str, Any]:
    material = dict(plan)
    material.pop("plan_sha256", None)
    return material


def plan_sha256(plan: Mapping[str, Any]) -> str:
    return legacy.sha256_json(_plan_material(plan))


def _require_sha256(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise legacy.StateError(f"repository identity rebind {label} is not a SHA-256")
    return value


def _require_positive_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise legacy.StateError(f"repository identity rebind {label} is invalid")
    return value


def _validate_plan_resource(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _RESOURCE_FIELDS:
        raise legacy.StateError(f"repository identity rebind plan {label} fields are not exact")
    material = json.loads(legacy.canonical_json(dict(value)))
    resource_id = material.get("id")
    path = material.get("path")
    github_slug = material.get("github_slug")
    if not isinstance(resource_id, str) or not resource_id.startswith("repo."):
        raise legacy.StateError(f"repository identity rebind plan {label} id is invalid")
    if material.get("type") != "git-repository":
        raise legacy.StateError(f"repository identity rebind plan {label} type is invalid")
    if not isinstance(path, str) or not path.startswith("/") or path == "/" or path.endswith("/"):
        raise legacy.StateError(f"repository identity rebind plan {label} path is invalid")
    if material.get("grabowski_key") != f"repo:{path}":
        raise legacy.StateError(
            f"repository identity rebind plan {label} Grabowski key is invalid"
        )
    if not isinstance(github_slug, str) or "/" not in github_slug:
        raise legacy.StateError(
            f"repository identity rebind plan {label} GitHub slug is invalid"
        )
    return material


def _validate_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, Mapping) or set(plan) != _PLAN_FIELDS:
        raise legacy.StateError("repository identity rebind plan fields are not exact")
    if plan.get("schema_version") != PLAN_SCHEMA_VERSION or plan.get("kind") != PLAN_KIND:
        raise legacy.StateError("repository identity rebind plan schema is unsupported")
    digest = _require_sha256(plan.get("plan_sha256"), "plan digest")
    if digest != plan_sha256(plan):
        raise legacy.StateError("repository identity rebind plan digest mismatch")
    if not isinstance(plan.get("generated_at"), str):
        raise legacy.StateError("repository identity rebind plan timestamp is invalid")
    _parse_time(str(plan["generated_at"]))
    old_resource = _validate_plan_resource(plan.get("old_resource"), "old_resource")
    new_resource = _validate_plan_resource(plan.get("new_resource"), "new_resource")
    if old_resource["id"] == new_resource["id"] or old_resource["path"] == new_resource["path"]:
        raise legacy.StateError("repository identity rebind plan resources are not distinct")
    binding = _require_sha256(plan.get("resource_binding_sha256"), "resource binding digest")
    expected_binding = legacy.sha256_json(
        {"old_resource": old_resource, "new_resource": new_resource}
    )
    if binding != expected_binding:
        raise legacy.StateError("repository identity rebind plan resource binding is inconsistent")
    _require_sha256(
        plan.get("task_spec_root_sha256_before"), "TaskSpec projection root"
    )

    items = plan.get("items")
    excluded = plan.get("excluded_active_tasks")
    summary = plan.get("summary")
    if not isinstance(items, list) or not items:
        raise legacy.StateError("repository identity rebind plan has no migration items")
    if not isinstance(excluded, list):
        raise legacy.StateError("repository identity rebind exclusions are invalid")
    if not isinstance(summary, Mapping) or set(summary) != _SUMMARY_FIELDS:
        raise legacy.StateError("repository identity rebind plan summary fields are not exact")
    for key in _SUMMARY_FIELDS:
        value = summary.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise legacy.StateError(
                f"repository identity rebind plan summary {key} is invalid"
            )

    for item in items:
        if not isinstance(item, Mapping) or set(item) != _ITEM_FIELDS:
            raise legacy.StateError("repository identity rebind plan item fields are not exact")
        if not isinstance(item.get("task_id"), str) or not item["task_id"]:
            raise legacy.StateError("repository identity rebind plan item task_id is invalid")
        if not isinstance(item.get("effective_state"), str) or not item["effective_state"]:
            raise legacy.StateError("repository identity rebind plan item state is invalid")
        _require_positive_int(item.get("expected_revision"), "item revision")
        _require_sha256(item.get("expected_spec_sha256"), "item expected digest")
        _require_sha256(item.get("resulting_spec_sha256"), "item resulting digest")
        _require_sha256(item.get("acceptance_sha256"), "item acceptance digest")
        _require_sha256(
            item.get("acceptance_diagnostics_sha256"),
            "item acceptance diagnostics digest",
        )
        key = item.get("idempotency_key")
        if (
            not isinstance(key, str)
            or not key.startswith(task_specs.REPOSITORY_IDENTITY_REBIND_IDEMPOTENCY_PREFIX)
        ):
            raise legacy.StateError(
                "repository identity rebind plan item idempotency key is invalid"
            )
        changed_paths = item.get("changed_paths")
        if (
            not isinstance(changed_paths, list)
            or not changed_paths
            or any(not isinstance(path, str) or not path.startswith("/") for path in changed_paths)
            or len(set(changed_paths)) != len(changed_paths)
        ):
            raise legacy.StateError(
                "repository identity rebind plan item changed_paths are invalid"
            )
        expected_key = task_specs.repository_identity_rebind_idempotency_key(
            task_id=str(item["task_id"]),
            expected_revision=int(item["expected_revision"]),
            expected_spec_sha256=str(item["expected_spec_sha256"]),
            resulting_spec_sha256=str(item["resulting_spec_sha256"]),
            old_resource_id=str(old_resource["id"]),
            new_resource_id=str(new_resource["id"]),
            old_repository_path=str(old_resource["path"]),
            new_repository_path=str(new_resource["path"]),
        )
        if key != expected_key:
            raise legacy.StateError(
                "repository identity rebind plan item idempotency binding is inconsistent"
            )

    for item in excluded:
        if not isinstance(item, Mapping) or set(item) != _EXCLUDED_FIELDS:
            raise legacy.StateError(
                "repository identity rebind excluded task fields are not exact"
            )
        if not isinstance(item.get("task_id"), str) or not item["task_id"]:
            raise legacy.StateError(
                "repository identity rebind excluded task id is invalid"
            )
        if not isinstance(item.get("run_id"), str) or not item["run_id"]:
            raise legacy.StateError(
                "repository identity rebind excluded run id is invalid"
            )
        if item.get("run_state") not in _ACTIVE_RUN_STATES:
            raise legacy.StateError(
                "repository identity rebind excluded run state is invalid"
            )
        _require_positive_int(item.get("expected_revision"), "excluded revision")
        _require_sha256(
            item.get("expected_spec_sha256"), "excluded expected digest"
        )

    task_ids = [str(item["task_id"]) for item in items]
    excluded_ids = [str(item["task_id"]) for item in excluded]
    if len(set(task_ids)) != len(task_ids) or len(set(excluded_ids)) != len(excluded_ids):
        raise legacy.StateError("repository identity rebind plan contains duplicate task ids")
    if set(task_ids) & set(excluded_ids):
        raise legacy.StateError("repository identity rebind migration/exclusion sets overlap")

    expected_summary = {
        "migration_items": len(items),
        "excluded_active_tasks": len(excluded),
        "changed_paths": sum(len(item["changed_paths"]) for item in items),
    }
    if dict(summary) != expected_summary:
        raise legacy.StateError("repository identity rebind plan summary is inconsistent")
    return json.loads(legacy.canonical_json(dict(plan)))


def _parse_time(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise legacy.StateError("repository identity rebind plan timestamp lacks timezone")
    return parsed.astimezone(timezone.utc)


def build_plan(
    registry: Registry,
    store: StateStore,
    *,
    old_resource_id: str,
    new_resource_id: str,
    excluded_active_task_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    old_resource, new_resource, binding_sha256 = _resource_pair(
        registry, old_resource_id, new_resource_id
    )
    excluded_requested = tuple(sorted(set(excluded_active_task_ids)))
    if len(excluded_requested) != len(excluded_active_task_ids):
        raise legacy.StateError("repository identity rebind exclusions contain duplicates")

    with store.connect() as connection:
        candidates = _candidates(
            connection,
            registry,
            old_resource_id=old_resource_id,
            old_path=str(old_resource["path"]),
        )
        active = _active_runs(connection)
        candidate_active = sorted(set(candidates) & set(active))
        if candidate_active != list(excluded_requested):
            raise legacy.StateError(
                "repository identity rebind active-task exclusions do not exactly match "
                "the live old-binding task set: expected "
                f"{candidate_active}, got {list(excluded_requested)}"
            )

        excluded: list[dict[str, Any]] = []
        items: list[dict[str, Any]] = []
        for task_id in sorted(candidates):
            current = candidates[task_id]
            if task_id in excluded_requested:
                try:
                    task_specs.preview_repository_identity_rebind(
                        current["spec"],
                        old_resource_id=old_resource_id,
                        new_resource_id=new_resource_id,
                        old_repository_path=str(old_resource["path"]),
                        new_repository_path=str(new_resource["path"]),
                    )
                except task_specs.TaskSpecError as exc:
                    raise legacy.StateError(str(exc)) from exc
                run = active[task_id]
                excluded.append(
                    {
                        "task_id": task_id,
                        "run_id": run["run_id"],
                        "run_state": run["state"],
                        "expected_revision": current["revision"],
                        "expected_spec_sha256": current["spec_sha256"],
                    }
                )
                continue
            try:
                preview = task_specs.preview_repository_identity_rebind(
                    current["spec"],
                    old_resource_id=old_resource_id,
                    new_resource_id=new_resource_id,
                    old_repository_path=str(old_resource["path"]),
                    new_repository_path=str(new_resource["path"]),
                )
            except task_specs.TaskSpecError as exc:
                raise legacy.StateError(str(exc)) from exc
            key = task_specs.repository_identity_rebind_idempotency_key(
                task_id=task_id,
                expected_revision=current["revision"],
                expected_spec_sha256=current["spec_sha256"],
                resulting_spec_sha256=preview["spec_sha256"],
                old_resource_id=old_resource_id,
                new_resource_id=new_resource_id,
                old_repository_path=str(old_resource["path"]),
                new_repository_path=str(new_resource["path"]),
            )
            items.append(
                {
                    "task_id": task_id,
                    "effective_state": current["effective_state"],
                    "expected_revision": current["revision"],
                    "expected_spec_sha256": current["spec_sha256"],
                    "resulting_spec_sha256": preview["spec_sha256"],
                    "idempotency_key": key,
                    "changed_paths": list(preview["changed_paths"]),
                    "acceptance_sha256": preview["acceptance_sha256"],
                    "acceptance_diagnostics_sha256": preview[
                        "acceptance_diagnostics_sha256"
                    ],
                }
            )
        before_projection = task_specs.current_projection(connection)
        before_root = task_specs.projection_root(before_projection)

    plan: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "kind": PLAN_KIND,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "old_resource": old_resource,
        "new_resource": new_resource,
        "resource_binding_sha256": binding_sha256,
        "task_spec_root_sha256_before": before_root,
        "items": items,
        "excluded_active_tasks": excluded,
        "summary": {
            "migration_items": len(items),
            "excluded_active_tasks": len(excluded),
            "changed_paths": sum(len(item["changed_paths"]) for item in items),
        },
    }
    plan["plan_sha256"] = plan_sha256(plan)
    return _validate_plan(plan)


def _require_resource_binding(registry: Registry, plan: Mapping[str, Any]) -> None:
    old_id = str(plan["old_resource"]["id"])
    new_id = str(plan["new_resource"]["id"])
    old, new, digest = _resource_pair(registry, old_id, new_id)
    if old != plan["old_resource"] or new != plan["new_resource"]:
        raise legacy.StateError("repository identity rebind resource binding changed")
    if digest != plan["resource_binding_sha256"]:
        raise legacy.StateError("repository identity rebind resource digest changed")


def _validate_freshness(plan: Mapping[str, Any], max_age_seconds: int) -> None:
    if max_age_seconds < 1 or max_age_seconds > 86400:
        raise legacy.StateError("repository identity rebind max plan age is invalid")
    generated = _parse_time(str(plan["generated_at"]))
    age = (datetime.now(timezone.utc) - generated).total_seconds()
    if age < -60 or age > max_age_seconds:
        raise legacy.StateError(
            f"repository identity rebind plan is stale: age_seconds={int(age)}"
        )


def _task_spec_event_replay(connection) -> dict[str, Any]:
    rows = [
        dict(row)
        for row in connection.execute(
            "SELECT event_id,run_id,event_type,event_schema_version,payload_json "
            "FROM events ORDER BY event_id"
        )
    ]
    _, task_rows = task_specs.split_event_rows(rows)
    return task_specs.verify_replay(connection, task_rows)


def apply_plan(
    registry: Registry,
    store: StateStore,
    plan: Mapping[str, Any],
    *,
    expected_plan_sha256: str,
    max_age_seconds: int = DEFAULT_MAX_PLAN_AGE_SECONDS,
) -> dict[str, Any]:
    checked = _validate_plan(plan)
    if expected_plan_sha256 != checked["plan_sha256"]:
        raise legacy.StateError("repository identity rebind expected plan digest mismatch")
    _validate_freshness(checked, max_age_seconds)
    _require_resource_binding(registry, checked)

    old_resource = checked["old_resource"]
    new_resource = checked["new_resource"]
    items = checked["items"]
    excluded = checked["excluded_active_tasks"]
    item_by_id = {str(item["task_id"]): item for item in items}
    excluded_by_id = {str(item["task_id"]): item for item in excluded}
    planned_all_ids = set(item_by_id) | set(excluded_by_id)

    with store.immediate() as connection:
        existing_receipts = {
            task_id: task_specs.get_mutation_receipt(
                connection, str(item["idempotency_key"])
            )
            for task_id, item in item_by_id.items()
        }
        existing_count = sum(receipt is not None for receipt in existing_receipts.values())
        if existing_count not in {0, len(items)}:
            raise legacy.StateError(
                "repository identity rebind found a partial mutation receipt set"
            )

        candidates = _candidates(
            connection,
            registry,
            old_resource_id=str(old_resource["id"]),
            old_path=str(old_resource["path"]),
        )
        active = _active_runs(connection)
        current_ids = set(candidates)
        for task_id, exclusion in excluded_by_id.items():
            run = active.get(task_id)
            if run is None:
                raise legacy.StateError(
                    f"repository identity rebind excluded task {task_id} is no longer active"
                )
            if (
                run["run_id"] != exclusion["run_id"]
                or run["state"] != exclusion["run_state"]
            ):
                raise legacy.StateError(
                    f"repository identity rebind excluded task {task_id} run binding changed"
                )
            current = candidates.get(task_id)
            if current is None or (
                current["revision"] != exclusion["expected_revision"]
                or current["spec_sha256"] != exclusion["expected_spec_sha256"]
            ):
                raise legacy.StateError(
                    f"repository identity rebind excluded task {task_id} revision changed"
                )
            try:
                task_specs.preview_repository_identity_rebind(
                    current["spec"],
                    old_resource_id=str(old_resource["id"]),
                    new_resource_id=str(new_resource["id"]),
                    old_repository_path=str(old_resource["path"]),
                    new_repository_path=str(new_resource["path"]),
                )
            except task_specs.TaskSpecError as exc:
                raise legacy.StateError(str(exc)) from exc
        if existing_count == len(items):
            if current_ids != set(excluded_by_id):
                raise legacy.StateError(
                    "repository identity rebind replay found unexpected old-binding tasks"
                )
            replay = _task_spec_event_replay(connection)
            return {
                "schema_version": RESULT_SCHEMA_VERSION,
                "kind": RESULT_KIND,
                "status": "already-applied",
                "plan_sha256": checked["plan_sha256"],
                "changed": False,
                "migration_items": len(items),
                "excluded_active_tasks": len(excluded),
                "task_spec_root_sha256": replay["root_sha256"],
                "task_spec_event_count": replay["event_count"],
            }

        if current_ids != planned_all_ids:
            raise legacy.StateError(
                "repository identity rebind live old-binding task set changed since plan"
            )
        active_migration = sorted(set(item_by_id) & set(active))
        if active_migration:
            raise legacy.StateError(
                "repository identity rebind migration tasks acquired active runs: "
                + ", ".join(active_migration)
            )

        before_root = task_specs.projection_root(task_specs.current_projection(connection))
        if before_root != checked["task_spec_root_sha256_before"]:
            raise legacy.StateError(
                "repository identity rebind TaskSpec projection root changed since plan"
            )
        results: list[dict[str, Any]] = []
        for task_id in sorted(item_by_id):
            item = item_by_id[task_id]
            current = candidates[task_id]
            if (
                current["revision"] != item["expected_revision"]
                or current["spec_sha256"] != item["expected_spec_sha256"]
            ):
                raise legacy.StateError(
                    f"repository identity rebind task {task_id} CAS baseline changed"
                )
            try:
                preview = task_specs.preview_repository_identity_rebind(
                    current["spec"],
                    old_resource_id=str(old_resource["id"]),
                    new_resource_id=str(new_resource["id"]),
                    old_repository_path=str(old_resource["path"]),
                    new_repository_path=str(new_resource["path"]),
                )
            except task_specs.TaskSpecError as exc:
                raise legacy.StateError(str(exc)) from exc
            if (
                preview["spec_sha256"] != item["resulting_spec_sha256"]
                or list(preview["changed_paths"]) != item["changed_paths"]
                or preview["acceptance_sha256"] != item["acceptance_sha256"]
                or preview["acceptance_diagnostics_sha256"]
                != item["acceptance_diagnostics_sha256"]
            ):
                raise legacy.StateError(
                    f"repository identity rebind preview drifted for task {task_id}"
                )
            try:
                result = task_specs._put_validated_material(
                    connection,
                    preview["spec"],
                    idempotency_key=str(item["idempotency_key"]),
                    expected_revision=int(item["expected_revision"]),
                    source="repository-identity-rebind",
                )
            except task_specs.TaskSpecError as exc:
                raise legacy.StateError(str(exc)) from exc
            results.append(
                {
                    "task_id": task_id,
                    "revision": result["revision"],
                    "spec_sha256": result["spec_sha256"],
                }
            )

        remaining = _candidates(
            connection,
            registry,
            old_resource_id=str(old_resource["id"]),
            old_path=str(old_resource["path"]),
        )
        if set(remaining) != set(excluded_by_id):
            raise legacy.StateError(
                "repository identity rebind left unexpected old-binding tasks after apply"
            )
        replay = _task_spec_event_replay(connection)
        after_root = replay["root_sha256"]

    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "kind": RESULT_KIND,
        "status": "applied",
        "plan_sha256": checked["plan_sha256"],
        "changed": True,
        "migration_items": len(results),
        "excluded_active_tasks": len(excluded),
        "task_spec_root_sha256_before": before_root,
        "task_spec_root_sha256": after_root,
        "task_spec_event_count": replay["event_count"],
        "results": results,
    }


def _state_store(path: str | None) -> StateStore:
    if path is None:
        resolved = legacy.default_state_dir() / "bureau.sqlite3"
    else:
        resolved = Path(path).expanduser().resolve()
    return StateStore(path=resolved, state_root=resolved.parent)


def _write_plan(path: Path, plan: Mapping[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")


def _read_plan(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise legacy.StateError(f"cannot read repository identity rebind plan: {exc}") from exc
    if not isinstance(value, dict):
        raise legacy.StateError("repository identity rebind plan must be an object")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bureau TaskSpec repository identity rebind")
    parser.add_argument("--registry-root", required=True)
    parser.add_argument("--state-path")
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan")
    plan.add_argument("--old-resource", required=True)
    plan.add_argument("--new-resource", required=True)
    plan.add_argument("--exclude-active-task", action="append", default=[])
    plan.add_argument("--output", required=True)

    apply = sub.add_parser("apply")
    apply.add_argument("--plan", required=True)
    apply.add_argument("--expected-plan-sha256", required=True)
    apply.add_argument(
        "--max-age-seconds", type=int, default=DEFAULT_MAX_PLAN_AGE_SECONDS
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        registry = Registry.load(Path(args.registry_root))
        store = _state_store(args.state_path)
        if args.command == "plan":
            plan = build_plan(
                registry,
                store,
                old_resource_id=args.old_resource,
                new_resource_id=args.new_resource,
                excluded_active_task_ids=tuple(args.exclude_active_task),
            )
            _write_plan(Path(args.output), plan)
            output = {
                "status": "planned",
                "plan_sha256": plan["plan_sha256"],
                "migration_items": plan["summary"]["migration_items"],
                "excluded_active_tasks": plan["summary"]["excluded_active_tasks"],
                "output": str(Path(args.output).expanduser().resolve()),
            }
        else:
            output = apply_plan(
                registry,
                store,
                _read_plan(Path(args.plan)),
                expected_plan_sha256=args.expected_plan_sha256,
                max_age_seconds=args.max_age_seconds,
            )
    except (legacy.BureauError, task_specs.TaskSpecError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
