from __future__ import annotations

import difflib
import hashlib
import json
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

from . import legacy
from .approval import require_approval, reviewed_plan_approval
from .core import Registry
from .lease_contract import (
    BROAD_BUREAU_REPOSITORY_KEY,
    registry_bureau_lease_findings,
)
from .v2 import plan_sha256

MIGRATION_ID = "BUREAU-TRUTH-MODEL-V2-T013"
CATALOG_RELATIVE_PATH = Path("registry/lease-migrations") / f"{MIGRATION_ID}.json"
SOURCE_CLAIM_RESOURCE = "repo.bureau"
TERMINAL_STATES = {"verified", "cancelled", "superseded"}
MAX_BATCH_SIZE = 5
DOES_NOT_ESTABLISH = [
    "queue mutation",
    "task readiness",
    "task claim or dispatch authority",
    "merge or deployment authority",
    "verification authority",
    "safe mutation of terminal historical tasks",
]


class LeaseMigrationError(legacy.StateError):
    """Lease migration planning or apply failed closed."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _file_sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _git_value(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "git command failed"
        raise LeaseMigrationError(detail)
    return result.stdout.rstrip("\n")


def _git_head(root: Path) -> str:
    return _git_value(root, "rev-parse", "HEAD")


def _dirty_paths(root: Path) -> list[str]:
    output = _git_value(root, "status", "--porcelain", "--untracked-files=normal")
    if not output:
        return []
    return sorted(line[3:] for line in output.splitlines() if len(line) >= 4)


def _require_clean_worktree(root: Path) -> None:
    dirty = _dirty_paths(root)
    if dirty:
        raise LeaseMigrationError(
            "lease migration planning requires a clean worktree: " + ", ".join(dirty)
        )


def _catalog_path(registry: Registry) -> Path:
    return registry.root / CATALOG_RELATIVE_PATH


def _task_path(registry: Registry, task_id: str) -> Path:
    expected = registry.root / "registry" / "tasks" / f"{task_id}.json"
    if expected.exists():
        raw = legacy.read_json(expected)
        if raw.get("id") == task_id:
            return expected
    matches: list[Path] = []
    for candidate in sorted((registry.root / "registry" / "tasks").glob("*.json")):
        try:
            raw = legacy.read_json(candidate)
        except legacy.ValidationError:
            continue
        if raw.get("id") == task_id:
            matches.append(candidate)
    if len(matches) != 1:
        raise LeaseMigrationError(
            f"task file resolution for {task_id} returned {len(matches)} matches"
        )
    return matches[0]


def _initiative_path(registry: Registry, initiative_id: str) -> Path:
    expected = registry.root / "registry" / "initiatives" / f"{initiative_id}.json"
    if expected.exists():
        raw = legacy.read_json(expected)
        if raw.get("id") == initiative_id:
            return expected
    matches: list[Path] = []
    for candidate in sorted((registry.root / "registry" / "initiatives").glob("*.json")):
        try:
            raw = legacy.read_json(candidate)
        except legacy.ValidationError:
            continue
        if raw.get("id") == initiative_id:
            matches.append(candidate)
    if len(matches) != 1:
        raise LeaseMigrationError(
            f"initiative file resolution for {initiative_id} returned {len(matches)} matches"
        )
    return matches[0]


def _canonical_repository_root(registry: Registry) -> Path:
    resource = registry.resources.get(SOURCE_CLAIM_RESOURCE)
    if resource is None or not resource.path:
        raise LeaseMigrationError("repo.bureau resource lacks a canonical path")
    return Path(resource.path)


def _canonical_task_resource_key(registry: Registry, task_id: str) -> str:
    return f"path:{_canonical_repository_root(registry)}/registry/tasks/{task_id}.json"


def _canonical_initiative_resource_key(registry: Registry, initiative_id: str) -> str:
    return (
        f"path:{_canonical_repository_root(registry)}/registry/initiatives/"
        f"{initiative_id}.json"
    )


def _queue_sha256(registry: Registry) -> str:
    return _file_sha256(registry.root / "registry" / "queue.json")


def _load_catalog(registry: Registry) -> tuple[dict[str, Any], str]:
    path = _catalog_path(registry)
    if not path.exists():
        raise LeaseMigrationError(f"lease migration catalog is missing: {path}")
    raw = legacy.read_json(path)
    if raw.get("schema_version") != 1 or raw.get("migration_id") != MIGRATION_ID:
        raise LeaseMigrationError("lease migration catalog has unsupported schema or id")
    if raw.get("source_broad_resource_key") != BROAD_BUREAU_REPOSITORY_KEY:
        raise LeaseMigrationError("lease migration catalog broad resource key mismatch")
    if raw.get("source_claim_resource") != SOURCE_CLAIM_RESOURCE:
        raise LeaseMigrationError("lease migration catalog claim resource mismatch")
    if not isinstance(raw.get("entries"), dict):
        raise LeaseMigrationError("lease migration catalog entries must be an object")
    return raw, _file_sha256(path)


def _entry_refusals(
    registry: Registry,
    task: legacy.Task,
    entry: Any,
    *,
    finding_sources: list[str],
) -> list[dict[str, Any]]:
    refusals: list[dict[str, Any]] = []
    if task.state in TERMINAL_STATES:
        refusals.append(
            {
                "code": "terminal-task-refused",
                "detail": f"task state is {task.state}",
            }
        )
    if not isinstance(entry, dict):
        return [
            {
                "code": "missing-semantic-catalog-entry",
                "detail": "no reviewed semantic mapping exists",
            }
        ]
    rationale = entry.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        refusals.append(
            {"code": "missing-semantic-rationale", "detail": "rationale is required"}
        )
    effects = entry.get("effect_boundaries")
    if not isinstance(effects, list) or not effects or not all(
        isinstance(item, str) and item.strip() for item in effects
    ):
        refusals.append(
            {
                "code": "missing-effect-boundary",
                "detail": "one or more explicit effect boundaries are required",
            }
        )
    claims = entry.get("replacement_claim_resources")
    if not isinstance(claims, list) or not all(isinstance(item, str) for item in claims):
        refusals.append(
            {
                "code": "invalid-claim-replacements",
                "detail": "replacement_claim_resources must be a string list",
            }
        )
        claims = []
    if "claims" in finding_sources and not claims:
        refusals.append(
            {
                "code": "missing-claim-replacement",
                "detail": "a broad repo.bureau claim requires semantic replacements",
            }
        )
    for resource_id in claims:
        if resource_id == SOURCE_CLAIM_RESOURCE:
            refusals.append(
                {
                    "code": "broad-claim-replacement-refused",
                    "detail": resource_id,
                }
            )
        elif resource_id not in registry.resources:
            refusals.append(
                {
                    "code": "unknown-replacement-claim-resource",
                    "detail": resource_id,
                }
            )
    resources = entry.get("replacement_grabowski_resources")
    if not isinstance(resources, list) or not all(
        isinstance(item, str) and item for item in resources
    ):
        refusals.append(
            {
                "code": "invalid-grabowski-replacements",
                "detail": "replacement_grabowski_resources must be a string list",
            }
        )
        resources = []
    for resource_key in resources:
        if resource_key == BROAD_BUREAU_REPOSITORY_KEY:
            refusals.append(
                {
                    "code": "broad-grabowski-replacement-refused",
                    "detail": resource_key,
                }
            )
        elif ":" not in resource_key:
            refusals.append(
                {
                    "code": "unsupported-grabowski-resource-key",
                    "detail": resource_key,
                }
            )
    if not isinstance(entry.get("include_initiative_resource"), bool):
        refusals.append(
            {
                "code": "invalid-initiative-resource-flag",
                "detail": "include_initiative_resource must be boolean",
            }
        )
    return sorted(refusals, key=lambda item: (item["code"], item["detail"]))


def broad_bureau_lease_inventory(registry: Registry) -> dict[str, Any]:
    catalog, catalog_sha256 = _load_catalog(registry)
    findings = registry_bureau_lease_findings(registry)
    entries: list[dict[str, Any]] = []
    for finding in findings:
        task = registry.tasks[finding["task_id"]]
        task_path = _task_path(registry, task.id)
        initiative_path = _initiative_path(registry, task.initiative)
        catalog_entry = catalog["entries"].get(task.id)
        refusals = _entry_refusals(
            registry,
            task,
            catalog_entry,
            finding_sources=list(finding["sources"]),
        )
        semantic: dict[str, Any] | None = None
        if isinstance(catalog_entry, dict):
            semantic = {
                "catalog_entry_sha256": legacy.sha256_json(catalog_entry),
                "rationale": catalog_entry.get("rationale"),
                "effect_boundaries": catalog_entry.get("effect_boundaries"),
                "replacement_claim_resources": catalog_entry.get(
                    "replacement_claim_resources"
                ),
                "replacement_grabowski_resources": catalog_entry.get(
                    "replacement_grabowski_resources"
                ),
                "include_initiative_resource": catalog_entry.get(
                    "include_initiative_resource"
                ),
            }
        entries.append(
            {
                "task_id": task.id,
                "title": task.title,
                "initiative_id": task.initiative,
                "state": task.state,
                "lane": finding["lane"],
                "finding_sources": list(finding["sources"]),
                "claim_resources": list(finding["claim_resources"]),
                "current_grabowski_resources": list(
                    task.execution.get("grabowski_resources", [])
                ),
                "current_task_sha256": task.sha256,
                "initiative_plan_sha256": plan_sha256(registry, task.initiative),
                "task_path": str(task_path.relative_to(registry.root)),
                "initiative_path": str(initiative_path.relative_to(registry.root)),
                "replacement_task_resource_key": _canonical_task_resource_key(
                    registry, task.id
                ),
                "semantic_input": semantic,
                "unresolved_semantic_inputs": refusals,
                "actionable": not refusals,
            }
        )
    inventory = {
        "schema_version": 1,
        "kind": "broad-bureau-lease-inventory",
        "migration_id": MIGRATION_ID,
        "registry": {
            "root": str(registry.root),
            "base_commit": _git_head(registry.root),
            "queue_sha256": _queue_sha256(registry),
        },
        "catalog": {
            "path": str(CATALOG_RELATIVE_PATH),
            "sha256": catalog_sha256,
        },
        "count": len(entries),
        "actionable_count": sum(1 for item in entries if item["actionable"]),
        "refused_count": sum(1 for item in entries if not item["actionable"]),
        "entries": sorted(entries, key=lambda item: item["task_id"]),
        "does_not_establish": DOES_NOT_ESTABLISH,
    }
    inventory["inventory_sha256"] = legacy.sha256_json(inventory)
    return inventory


def _dedupe_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in values if isinstance(item, str) and item))


def _dedupe_claims(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        key = legacy.canonical_json(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _migrate_task_raw(
    registry: Registry,
    task: legacy.Task,
    catalog_entry: dict[str, Any],
) -> dict[str, Any]:
    raw = deepcopy(task.raw)
    if raw.get("state") in TERMINAL_STATES:
        raise LeaseMigrationError(f"terminal task cannot be migrated: {task.id}")
    execution = deepcopy(raw.get("execution", {}))
    current_resources = list(execution.get("grabowski_resources", []))
    retained_resources = [
        item for item in current_resources if item != BROAD_BUREAU_REPOSITORY_KEY
    ]
    replacements = list(catalog_entry["replacement_grabowski_resources"])
    replacements.append(_canonical_task_resource_key(registry, task.id))
    if catalog_entry["include_initiative_resource"]:
        replacements.append(
            _canonical_initiative_resource_key(registry, task.initiative)
        )
    execution["grabowski_resources"] = _dedupe_strings(
        [*retained_resources, *replacements]
    )
    raw["execution"] = execution

    replacement_claim_resources = list(catalog_entry["replacement_claim_resources"])
    claims: list[dict[str, Any]] = []
    replaced_claim_count = 0
    for claim in raw.get("claims", []):
        if claim.get("resource") != SOURCE_CLAIM_RESOURCE:
            claims.append(deepcopy(claim))
            continue
        replaced_claim_count += 1
        for resource_id in replacement_claim_resources:
            replacement = deepcopy(claim)
            replacement["resource"] = resource_id
            claims.append(replacement)
    if replaced_claim_count and not replacement_claim_resources:
        raise LeaseMigrationError(f"task {task.id} lacks replacement claims")
    raw["claims"] = _dedupe_claims(claims)

    metadata = deepcopy(raw.get("metadata", {}))
    metadata["lease_scope_migration"] = {
        "migration_id": MIGRATION_ID,
        "source_broad_resource_key": BROAD_BUREAU_REPOSITORY_KEY,
        "source_claim_resource": SOURCE_CLAIM_RESOURCE,
        "rationale": catalog_entry["rationale"],
        "effect_boundaries": list(catalog_entry["effect_boundaries"]),
        "catalog_entry_sha256": legacy.sha256_json(catalog_entry),
        "does_not_establish": DOES_NOT_ESTABLISH,
    }
    raw["metadata"] = metadata
    if BROAD_BUREAU_REPOSITORY_KEY in execution["grabowski_resources"]:
        raise LeaseMigrationError(f"task {task.id} still has broad execution resource")
    if any(claim.get("resource") == SOURCE_CLAIM_RESOURCE for claim in raw["claims"]):
        raise LeaseMigrationError(f"task {task.id} still has broad claim")
    return raw


def _render_task(raw: dict[str, Any]) -> str:
    return json.dumps(raw, ensure_ascii=False, indent=2) + "\n"


def _unified_diff(relative_path: str, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{relative_path}",
            tofile=f"b/{relative_path}",
        )
    )


def _plan_unsigned(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in plan.items()
        if key not in {"plan_sha256", "review"}
    }


def _validate_batch_size(batch_size: int) -> None:
    if batch_size < 1 or batch_size > MAX_BATCH_SIZE:
        raise LeaseMigrationError(
            f"batch size must be between 1 and {MAX_BATCH_SIZE}"
        )


def lease_migration_plan(
    registry: Registry,
    *,
    batch_size: int = MAX_BATCH_SIZE,
    after_task_id: str | None = None,
) -> dict[str, Any]:
    _validate_batch_size(batch_size)
    _require_clean_worktree(registry.root)
    inventory = broad_bureau_lease_inventory(registry)
    refusals = [
        {
            "task_id": item["task_id"],
            "codes": [entry["code"] for entry in item["unresolved_semantic_inputs"]],
            "details": item["unresolved_semantic_inputs"],
        }
        for item in inventory["entries"]
        if not item["actionable"]
    ]
    actionable = [
        item
        for item in inventory["entries"]
        if item["actionable"]
        and (after_task_id is None or item["task_id"] > after_task_id)
    ]
    selected = [] if refusals else actionable[:batch_size]
    catalog, catalog_sha256 = _load_catalog(registry)
    proposals: list[dict[str, Any]] = []
    combined_diff_parts: list[str] = []
    for inventory_entry in selected:
        task = registry.tasks[inventory_entry["task_id"]]
        task_path = _task_path(registry, task.id)
        relative_path = str(task_path.relative_to(registry.root))
        before_text = task_path.read_text(encoding="utf-8")
        after_raw = _migrate_task_raw(registry, task, catalog["entries"][task.id])
        after_text = _render_task(after_raw)
        diff_text = _unified_diff(relative_path, before_text, after_text)
        if not diff_text:
            raise LeaseMigrationError(f"task {task.id} migration produced no diff")
        combined_diff_parts.append(diff_text)
        proposals.append(
            {
                "task_id": task.id,
                "initiative_id": task.initiative,
                "task_path": relative_path,
                "task_resource_key": inventory_entry[
                    "replacement_task_resource_key"
                ],
                "finding_sources": inventory_entry["finding_sources"],
                "source_task_sha256": task.sha256,
                "initiative_plan_sha256": plan_sha256(registry, task.initiative),
                "catalog_entry_sha256": inventory_entry["semantic_input"][
                    "catalog_entry_sha256"
                ],
                "rationale": inventory_entry["semantic_input"]["rationale"],
                "effect_boundaries": inventory_entry["semantic_input"][
                    "effect_boundaries"
                ],
                "state_before": task.state,
                "state_after": after_raw["state"],
                "priority_before": deepcopy(task.raw.get("priority")),
                "priority_after": deepcopy(after_raw.get("priority")),
                "proposed_task_sha256": legacy.sha256_json(after_raw),
                "proposed_file_diff": diff_text,
                "proposed_file_diff_sha256": _sha256_text(diff_text),
            }
        )
    combined_diff = "".join(combined_diff_parts)
    plan: dict[str, Any] = {
        "schema_version": 1,
        "command": "migrate-leases-plan",
        "migration_id": MIGRATION_ID,
        "registry": {
            "root": str(registry.root),
            "base_commit": inventory["registry"]["base_commit"],
            "queue_sha256": inventory["registry"]["queue_sha256"],
        },
        "catalog_sha256": catalog_sha256,
        "inventory_sha256": inventory["inventory_sha256"],
        "batch_size": batch_size,
        "after_task_id": after_task_id,
        "task_ids": [item["task_id"] for item in proposals],
        "remaining_actionable_count": max(0, len(actionable) - len(proposals)),
        "proposals": proposals,
        "refusals": refusals,
        "applicable": not refusals,
        "blocked_by_refusals": bool(refusals),
        "combined_diff": combined_diff,
        "combined_diff_sha256": _sha256_text(combined_diff),
        "review": {
            "required": True,
            "status": "pending",
            "instructions": (
                "Review every task rationale, effect boundary and complete diff. "
                "To apply, set status to reviewed, copy plan_sha256 into "
                "review.approved_plan_sha256 and add reviewer plus reviewed_at."
            ),
        },
        "does_not_establish": DOES_NOT_ESTABLISH,
    }
    plan["plan_sha256"] = legacy.sha256_json(_plan_unsigned(plan))
    return plan


def write_lease_migration_plan(
    registry: Registry,
    path: str | Path,
    *,
    batch_size: int = MAX_BATCH_SIZE,
    after_task_id: str | None = None,
) -> dict[str, Any]:
    plan = lease_migration_plan(
        registry,
        batch_size=batch_size,
        after_task_id=after_task_id,
    )
    target = Path(path).expanduser()
    legacy.atomic_write(target, legacy.canonical_json(plan) + "\n")
    return {**plan, "path": str(target)}


def _load_reviewed_plan(path: str | Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    target = Path(path).expanduser()
    plan = legacy.read_json(target)
    if plan.get("schema_version") != 1 or plan.get("command") != "migrate-leases-plan":
        raise LeaseMigrationError("lease migration plan has unsupported schema or command")
    if plan.get("migration_id") != MIGRATION_ID:
        raise LeaseMigrationError("lease migration plan id mismatch")
    claimed_sha = plan.get("plan_sha256")
    if not isinstance(claimed_sha, str) or legacy.sha256_json(_plan_unsigned(plan)) != claimed_sha:
        raise LeaseMigrationError("lease migration plan SHA-256 mismatch")
    review = plan.get("review")
    if not isinstance(review, dict) or review.get("status") != "reviewed":
        raise LeaseMigrationError("lease migration plan is not reviewed")
    reviewer = review.get("reviewer")
    reviewed_at = review.get("reviewed_at")
    approved_plan_sha256 = review.get("approved_plan_sha256")
    if not isinstance(reviewer, str) or not reviewer.strip() or not isinstance(
        reviewed_at, str
    ) or not reviewed_at.strip():
        raise LeaseMigrationError(
            "reviewed lease migration plan requires reviewer and reviewed_at"
        )
    if approved_plan_sha256 != claimed_sha:
        raise LeaseMigrationError(
            "reviewed lease migration plan is not bound to plan_sha256"
        )
    approval = require_approval(
        "registry_mutation",
        reviewed_plan_approval(
            reviewer=reviewer,
            reference=str(target),
            approved=True,
            task_id=MIGRATION_ID,
            scope="registry_mutation",
        ),
        expected_reference=str(target),
        task_id=MIGRATION_ID,
    )
    return target, plan, approval


def _require_plan_root(registry: Registry, plan: dict[str, Any]) -> None:
    bound = plan.get("registry")
    if not isinstance(bound, dict):
        raise LeaseMigrationError("lease migration plan lacks Registry binding")
    planned_root = bound.get("root")
    if not isinstance(planned_root, str) or Path(planned_root).resolve() != registry.root.resolve():
        raise LeaseMigrationError("lease migration plan Registry root mismatch")
    if bound.get("base_commit") != _git_head(registry.root):
        raise LeaseMigrationError("Registry base commit changed since plan generation")
    if bound.get("queue_sha256") != _queue_sha256(registry):
        raise LeaseMigrationError("Registry queue changed since plan generation")


def apply_lease_migration_plan(
    registry: Registry,
    path: str | Path,
) -> dict[str, Any]:
    plan_path, plan, approval = _load_reviewed_plan(path)
    _require_clean_worktree(registry.root)
    _require_plan_root(registry, plan)
    catalog_path = _catalog_path(registry)
    if _file_sha256(catalog_path) != plan.get("catalog_sha256"):
        raise LeaseMigrationError("lease migration catalog changed since plan generation")
    recomputed = lease_migration_plan(
        registry,
        batch_size=int(plan.get("batch_size", 0)),
        after_task_id=plan.get("after_task_id"),
    )
    if recomputed["plan_sha256"] != plan.get("plan_sha256"):
        raise LeaseMigrationError("lease migration plan no longer matches current Registry")
    if plan.get("refusals"):
        raise LeaseMigrationError(
            "lease migration plan has unresolved semantic refusals"
        )
    if not plan.get("proposals"):
        return {
            "schema_version": 1,
            "command": "migrate-leases-apply",
            "applied": False,
            "no_op": True,
            "plan_path": str(plan_path),
            "plan_sha256": plan["plan_sha256"],
            "approval": approval,
            "tasks": [],
            "does_not_establish": DOES_NOT_ESTABLISH,
        }

    before_texts: dict[Path, str] = {}
    expected_paths: list[str] = []
    task_states: dict[str, str] = {}
    task_priorities: dict[str, Any] = {}
    try:
        for proposal in plan["proposals"]:
            task_id = proposal["task_id"]
            task = registry.tasks.get(task_id)
            if task is None:
                raise LeaseMigrationError(f"planned task disappeared: {task_id}")
            if task.sha256 != proposal["source_task_sha256"]:
                raise LeaseMigrationError(f"task changed since plan review: {task_id}")
            if plan_sha256(registry, task.initiative) != proposal[
                "initiative_plan_sha256"
            ]:
                raise LeaseMigrationError(
                    f"initiative plan changed since review: {task.initiative}"
                )
            target = registry.root / proposal["task_path"]
            before = target.read_text(encoding="utf-8")
            after_raw = _migrate_task_raw(
                registry,
                task,
                _load_catalog(registry)[0]["entries"][task_id],
            )
            after = _render_task(after_raw)
            diff_text = _unified_diff(proposal["task_path"], before, after)
            if _sha256_text(diff_text) != proposal["proposed_file_diff_sha256"]:
                raise LeaseMigrationError(f"proposed diff changed for {task_id}")
            if legacy.sha256_json(after_raw) != proposal["proposed_task_sha256"]:
                raise LeaseMigrationError(f"proposed task hash changed for {task_id}")
            before_texts[target] = before
            expected_paths.append(proposal["task_path"])
            task_states[task_id] = task.state
            task_priorities[task_id] = deepcopy(task.raw.get("priority"))
            legacy.atomic_write(target, after)

        registry_after = Registry.load(registry.root)
        remaining_findings = {
            item["task_id"]
            for item in registry_bureau_lease_findings(registry_after)
            if item["task_id"] in task_states
        }
        if remaining_findings:
            raise LeaseMigrationError(
                "migrated tasks still have broad lease findings: "
                + ", ".join(sorted(remaining_findings))
            )
        for task_id, state in task_states.items():
            task_after = registry_after.tasks[task_id]
            if task_after.state != state:
                raise LeaseMigrationError(f"task state changed during migration: {task_id}")
            if task_after.raw.get("priority") != task_priorities[task_id]:
                raise LeaseMigrationError(
                    f"task priority changed during migration: {task_id}"
                )
        if _queue_sha256(registry_after) != plan["registry"]["queue_sha256"]:
            raise LeaseMigrationError("queue changed during lease migration")
        changed_paths = sorted(_dirty_paths(registry.root))
        if changed_paths != sorted(expected_paths):
            raise LeaseMigrationError(
                "lease migration changed unexpected paths: " + ", ".join(changed_paths)
            )
    except Exception:
        for target, before in before_texts.items():
            legacy.atomic_write(target, before)
        raise

    return {
        "schema_version": 1,
        "command": "migrate-leases-apply",
        "applied": True,
        "no_op": False,
        "plan_path": str(plan_path),
        "plan_sha256": plan["plan_sha256"],
        "combined_diff_sha256": plan["combined_diff_sha256"],
        "approval": approval,
        "tasks": [
            {
                "task_id": proposal["task_id"],
                "task_path": proposal["task_path"],
                "source_task_sha256": proposal["source_task_sha256"],
                "proposed_task_sha256": proposal["proposed_task_sha256"],
                "proposed_file_diff_sha256": proposal[
                    "proposed_file_diff_sha256"
                ],
                "rationale": proposal["rationale"],
            }
            for proposal in plan["proposals"]
        ],
        "post_gates": {
            "registry_load": True,
            "migrated_findings_remaining": 0,
            "queue_unchanged": True,
            "states_unchanged": True,
            "priorities_unchanged": True,
            "changed_paths_exact": True,
        },
        "does_not_establish": DOES_NOT_ESTABLISH,
    }
