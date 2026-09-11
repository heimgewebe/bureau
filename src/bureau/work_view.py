"""Read-only resource-centred Bureau work projection.

A work view is navigation, not a new truth layer.  It groups existing Bureau
TaskSpecs and candidate records around one canonical Registry resource while
preserving the authority and state reported by the existing status projection.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from . import legacy
from .live_register import current_candidate_records
from .read_only_state import ReadOnlyStateStore
from .status_projection import status_projection
from .v2 import TERMINAL_TASK_STATES, Registry, _read_only_state_rows, _runtime_state_db_path

WORK_VIEW_SCHEMA_VERSION = 1
RELATION_DEPTH = 1

WORK_VIEW_DOES_NOT_ESTABLISH = [
    "new_task_truth",
    "initiative_membership",
    "queue_mutation",
    "task_state_mutation",
    "candidate_promotion",
    "claim_authority",
    "dispatch_authority",
    "merge_readiness",
    "runtime_correctness",
]


def _normalized_path(value: str | None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return os.path.normpath(value.strip())


def _basis_key(value: dict[str, Any]) -> str:
    return legacy.canonical_json(value)


def _operational_registry(
    root: Path,
    *,
    state_db: Path | None,
    state_root: Path | None,
) -> tuple[Registry, Path, dict[str, Any]]:
    """Resolve the same read-only TaskSpec authority used by status_projection."""
    source_registry = Registry.load(root)
    state_path = _runtime_state_db_path(state_db, state_root)
    state = _read_only_state_rows(state_path, registry=source_registry)
    authority = state.get("task_authority")
    operational = state.get("operational_registry")
    if (
        state.get("available")
        and isinstance(authority, dict)
        and authority.get("kind") == "bureau-state-store-task-specs"
        and isinstance(operational, Registry)
    ):
        return operational, state_path, state
    return source_registry, state_path, state


def _direct_bases(task: legacy.Task, resource: legacy.Resource) -> list[dict[str, Any]]:
    bases: list[dict[str, Any]] = []
    for claim in task.claims:
        if claim.resource == resource.id:
            bases.append(
                {
                    "kind": "claim",
                    "resource": resource.id,
                    "mode": claim.mode,
                    "isolation": claim.isolation,
                }
            )

    working_repository = _normalized_path(task.execution.get("working_repository"))
    resource_path = _normalized_path(resource.path)
    if resource_path is not None and working_repository == resource_path:
        bases.append(
            {
                "kind": "working_repository",
                "path": resource.path,
            }
        )

    grabowski_resources = task.execution.get("grabowski_resources", [])
    if isinstance(grabowski_resources, list) and resource.grabowski_key:
        prefix = f"{resource.grabowski_key}:"
        for key in sorted({str(value) for value in grabowski_resources}):
            if key == resource.grabowski_key or key.startswith(prefix):
                bases.append(
                    {
                        "kind": "grabowski_resource",
                        "resource_key": key,
                        "canonical_repository_key": resource.grabowski_key,
                    }
                )

    return sorted(bases, key=_basis_key)


def _task_relations(tasks: dict[str, legacy.Task]) -> list[dict[str, str]]:
    """Return validated, explicit one-edge task relations only."""
    relations: dict[tuple[str, str, str], dict[str, str]] = {}
    task_ids = set(tasks)
    for task in tasks.values():
        for dependency in task.depends_on:
            if dependency not in task_ids:
                continue
            relation = {
                "kind": "depends_on",
                "source_task_id": task.id,
                "target_task_id": dependency,
            }
            relations[(relation["kind"], task.id, dependency)] = relation
        metadata = task.raw.get("metadata")
        parent = metadata.get("parent_task") if isinstance(metadata, dict) else None
        if isinstance(parent, str) and parent in task_ids:
            relation = {
                "kind": "parent_task",
                "source_task_id": task.id,
                "target_task_id": parent,
            }
            relations[(relation["kind"], task.id, parent)] = relation
    return [relations[key] for key in sorted(relations)]


def _relation_bases(
    task_id: str,
    direct_task_ids: set[str],
    relations: list[dict[str, str]],
) -> list[dict[str, Any]]:
    bases: list[dict[str, Any]] = []
    for relation in relations:
        source = relation["source_task_id"]
        target = relation["target_task_id"]
        source_matches = task_id == source and target in direct_task_ids
        target_matches = task_id == target and source in direct_task_ids
        if source_matches or target_matches:
            bases.append({**relation, "distance": 1})
    return sorted(bases, key=_basis_key)


def _claims_any(task: legacy.Task, resource_ids: set[str]) -> list[str]:
    return sorted({claim.resource for claim in task.claims if claim.resource in resource_ids})


def _public_task(
    task: legacy.Task,
    status: dict[str, Any],
    *,
    membership: str,
    basis: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "task_id": task.id,
        "title": status.get("title", task.title),
        "initiative": task.initiative,
        "membership": membership,
        "basis": basis,
        "task_spec_state": status.get("task_spec_state", task.state),
        "effective_state": status.get("effective_state", task.state),
        "queue_lane": status.get("queue_lane"),
        "active_run": status.get("active_run"),
        "blocked_reasons": status.get("blocked_reasons", []),
        "stale_reasons": status.get("stale_reasons", []),
        "unknowns": status.get("unknowns", []),
    }


def _migration_gaps(
    tasks: dict[str, legacy.Task],
    status_by_task: dict[str, dict[str, Any]],
    *,
    visible_task_ids: set[str],
    legacy_resource_ids: set[str],
) -> list[dict[str, Any]]:
    """Report unresolved legacy bindings without promoting them to view membership."""
    gaps: list[dict[str, Any]] = []
    for task_id in sorted(set(tasks) - visible_task_ids):
        task = tasks[task_id]
        status = status_by_task.get(task_id, {})
        task_spec_state = str(status.get("task_spec_state", task.state))
        if task_spec_state in TERMINAL_TASK_STATES:
            continue
        claimed_legacy = _claims_any(task, legacy_resource_ids)
        if not claimed_legacy:
            continue
        gaps.append(
            {
                "task_id": task.id,
                "title": status.get("title", task.title),
                "initiative": task.initiative,
                "task_spec_state": task_spec_state,
                "effective_state": status.get("effective_state", task.state),
                "diagnostic": "declared_legacy_resource_without_explicit_target_relation",
                "membership": None,
                "basis": [
                    {
                        "kind": "declared_legacy_resource",
                        "resource": legacy_id,
                        "diagnostic_only": True,
                    }
                    for legacy_id in claimed_legacy
                ],
                "does_not_establish": [
                    "target_resource_membership",
                    "automatic_task_migration",
                    "automatic_resource_supersession",
                ],
            }
        )
    return gaps


def _candidate_identity(item: dict[str, Any]) -> str:
    record = item.get("record")
    if isinstance(record, dict):
        candidate_id = record.get("candidate_id")
        if isinstance(candidate_id, str) and candidate_id:
            return candidate_id
    return f"candidate-event-{item.get('event_id')}"


def _public_candidate(
    item: dict[str, Any],
    *,
    membership: str,
    basis: list[dict[str, Any]],
) -> dict[str, Any]:
    record = item.get("record") if isinstance(item.get("record"), dict) else {}
    return {
        "candidate_id": _candidate_identity(item),
        "event_id": item.get("event_id"),
        "title": record.get("title"),
        "status": record.get("status"),
        "repo": record.get("repo"),
        "task_id": record.get("task_id"),
        "promotion_required": record.get("promotion_required"),
        "membership": membership,
        "basis": basis,
        "authority": "candidate_only",
        "does_not_establish": [
            "registry_task_truth",
            "claim_authority",
            "dispatch_authority",
            "merge_authority",
            "deployment_authority",
        ],
    }


def _candidate_sections(
    records: Iterable[dict[str, Any]],
    *,
    resource_id: str,
    direct_task_ids: set[str],
    related_task_ids: set[str],
    legacy_task_ids: set[str],
    legacy_resource_ids: set[str],
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {"direct": [], "related": [], "legacy": []}
    seen: set[str] = set()
    ordered_records = sorted(
        records,
        key=lambda value: (int(value.get("event_id") or 0), _candidate_identity(value)),
    )
    for item in ordered_records:
        identity = _candidate_identity(item)
        if identity in seen:
            continue
        seen.add(identity)
        record = item.get("record") if isinstance(item.get("record"), dict) else {}
        repo = record.get("repo")
        task_id = record.get("task_id")
        membership: str | None = None
        bases: list[dict[str, Any]] = []
        if repo == resource_id:
            membership = "direct"
            bases.append({"kind": "candidate_repo", "resource": resource_id})
        elif isinstance(task_id, str) and task_id in direct_task_ids:
            membership = "related"
            bases.append({"kind": "candidate_task", "task_id": task_id, "distance": 1})
        elif (
            repo in legacy_resource_ids
            and isinstance(task_id, str)
            and task_id in legacy_task_ids
        ):
            membership = "legacy"
            bases.append(
                {
                    "kind": "candidate_legacy_repo",
                    "resource": repo,
                    "task_id": task_id,
                }
            )
        elif isinstance(task_id, str) and task_id in related_task_ids:
            membership = "related"
            bases.append({"kind": "candidate_task", "task_id": task_id, "distance": 1})
        elif isinstance(task_id, str) and task_id in legacy_task_ids:
            membership = "legacy"
            bases.append({"kind": "candidate_task", "task_id": task_id, "distance": 1})
        if membership is not None:
            result[membership].append(
                _public_candidate(item, membership=membership, basis=sorted(bases, key=_basis_key))
            )
    for values in result.values():
        values.sort(key=lambda value: value["candidate_id"])
    return result


def resource_work_view(
    root: Path,
    resource_id: str,
    *,
    state_db: Path | None = None,
    state_root: Path | None = None,
    github: dict[str, Any] | None = None,
    legacy_resource_ids: Iterable[str] = (),
    candidate_records_override: Iterable[dict[str, Any]] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Build one deterministic, read-only work view around a Registry resource."""
    root = Path(root).expanduser().resolve()
    registry, state_path, state = _operational_registry(
        root, state_db=state_db, state_root=state_root
    )
    resource = registry.resources.get(resource_id)
    if resource is None:
        raise legacy.ValidationError(f"unknown work-view resource {resource_id}")

    status = status_projection(
        root,
        state_db=state_db,
        state_root=state_root,
        github=github,
        now=now,
    )
    status_by_task = {str(item["task_id"]): item for item in status.get("tasks", [])}
    tasks = registry.tasks

    direct_bases = {task.id: _direct_bases(task, resource) for task in tasks.values()}
    direct_bases = {task_id: bases for task_id, bases in direct_bases.items() if bases}
    direct_task_ids = set(direct_bases)

    relations = _task_relations(tasks)
    related_bases: dict[str, list[dict[str, Any]]] = {}
    for task_id in sorted(set(tasks) - direct_task_ids):
        bases = _relation_bases(task_id, direct_task_ids, relations)
        if bases:
            related_bases[task_id] = bases

    legacy_ids = {str(value) for value in legacy_resource_ids}
    legacy_bases: dict[str, list[dict[str, Any]]] = {}
    for task_id in sorted(related_bases):
        claimed_legacy = _claims_any(tasks[task_id], legacy_ids)
        if not claimed_legacy:
            continue
        legacy_bases[task_id] = [
            *related_bases[task_id],
            *({"kind": "legacy_resource", "resource": legacy_id} for legacy_id in claimed_legacy),
        ]
        legacy_bases[task_id] = sorted(legacy_bases[task_id], key=_basis_key)
    for task_id in legacy_bases:
        related_bases.pop(task_id, None)

    related_task_ids = set(related_bases)
    legacy_task_ids = set(legacy_bases)

    sections = {
        "direct": [
            _public_task(
                tasks[task_id],
                status_by_task.get(task_id, {}),
                membership="direct",
                basis=direct_bases[task_id],
            )
            for task_id in sorted(direct_task_ids)
        ],
        "related": [
            _public_task(
                tasks[task_id],
                status_by_task.get(task_id, {}),
                membership="related",
                basis=related_bases[task_id],
            )
            for task_id in sorted(related_task_ids)
        ],
        "legacy": [
            _public_task(
                tasks[task_id],
                status_by_task.get(task_id, {}),
                membership="legacy",
                basis=legacy_bases[task_id],
            )
            for task_id in sorted(legacy_task_ids)
        ],
    }

    candidate_source: dict[str, Any]
    if candidate_records_override is not None:
        candidate_records = list(candidate_records_override)
        candidate_source = {
            "available": True,
            "kind": "caller_supplied_snapshot",
            "read_only": True,
        }
    elif state.get("available"):
        store = ReadOnlyStateStore(state_path, state_path.parent)
        candidate_records = current_candidate_records(store)
        candidate_source = {
            "available": True,
            "kind": "bureau_state_store_events",
            "path": str(state_path),
            "read_only": True,
        }
    else:
        candidate_records = []
        candidate_source = {
            "available": False,
            "kind": "unavailable",
            "reason": state.get("error"),
            "read_only": True,
        }

    candidates = _candidate_sections(
        candidate_records,
        resource_id=resource_id,
        direct_task_ids=direct_task_ids,
        related_task_ids=related_task_ids,
        legacy_task_ids=legacy_task_ids,
        legacy_resource_ids=legacy_ids,
    )

    initiative_groups: dict[str, dict[str, Any]] = {}
    for membership, task_ids in (
        ("direct", direct_task_ids),
        ("related", related_task_ids),
        ("legacy", legacy_task_ids),
    ):
        for task_id in sorted(task_ids):
            initiative_id = tasks[task_id].initiative
            initiative = registry.initiatives.get(initiative_id)
            group = initiative_groups.setdefault(
                initiative_id,
                {
                    "initiative": initiative_id,
                    "title": initiative.title if initiative is not None else None,
                    "state": initiative.state if initiative is not None else None,
                    "task_ids": {"direct": [], "related": [], "legacy": []},
                },
            )
            group["task_ids"][membership].append(task_id)

    initiatives = [initiative_groups[key] for key in sorted(initiative_groups)]
    visible_task_ids = direct_task_ids | related_task_ids | legacy_task_ids
    migration_gaps = _migration_gaps(
        tasks,
        status_by_task,
        visible_task_ids=visible_task_ids,
        legacy_resource_ids=legacy_ids,
    )
    out_count = len(tasks) - len(visible_task_ids)

    counts = {
        "tasks": {
            "direct": len(sections["direct"]),
            "related": len(sections["related"]),
            "legacy": len(sections["legacy"]),
            "out": out_count,
        },
        "migration_gaps": len(migration_gaps),
        "candidates": {key: len(value) for key, value in candidates.items()},
        "initiatives": len(initiatives),
    }

    return {
        "schema_version": WORK_VIEW_SCHEMA_VERSION,
        "kind": "bureau_resource_work_view",
        "read_only": True,
        "resource": {
            "id": resource.id,
            "type": resource.type,
            "path": resource.path,
            "github_slug": resource.github_slug,
            "grabowski_key": resource.grabowski_key,
        },
        "relation_depth": RELATION_DEPTH,
        "membership_policy": {
            "direct": [
                "exact task claim on resource id",
                "exact execution.working_repository path",
                "canonical Grabowski repository key or a key anchored below it",
            ],
            "related": "one explicit depends_on or parent_task edge from a direct task",
            "legacy": "related task plus an exact claim on a caller-declared legacy resource",
            "migration_gap": (
                "nonterminal non-member task with an exact claim on a caller-declared legacy "
                "resource; diagnostic only"
            ),
            "title_or_id_heuristics": False,
            "transitive_expansion": False,
        },
        "task_authority": status.get("task_authority"),
        "candidate_source": candidate_source,
        "sections": sections,
        "candidates": candidates,
        "initiatives": initiatives,
        "diagnostics": {"migration_gaps": migration_gaps},
        "counts": counts,
        "status_projection_healthy": status.get("healthy"),
        "status_projection_findings": status.get("findings", []),
        "does_not_establish": list(WORK_VIEW_DOES_NOT_ESTABLISH),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Project Bureau work around one canonical Registry resource without mutation."
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--resource", required=True)
    parser.add_argument("--state-db", type=Path)
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--legacy-resource", action="append", default=[])
    parser.add_argument("--now")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    view = resource_work_view(
        args.root,
        args.resource,
        state_db=args.state_db,
        state_root=args.state_root,
        legacy_resource_ids=args.legacy_resource,
        now=args.now,
    )
    print(json.dumps(view, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
