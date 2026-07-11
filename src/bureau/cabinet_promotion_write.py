from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .cabinet_graph import CabinetGraphError


def _expect_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CabinetGraphError(f"{label} must be an object")
    return value


def _expect_false(value: Any, label: str) -> None:
    if value is not False:
        raise CabinetGraphError(f"{label} must be false")


def _promotion_task(proposal: dict[str, Any]) -> dict[str, Any]:
    promotion = _expect_object(proposal, "Cabinet frontier promotion")
    if promotion.get("schemaVersion") != 1:
        raise CabinetGraphError("Cabinet frontier promotion schemaVersion must be 1")
    if promotion.get("kind") != "cabinet_frontier_promotion":
        raise CabinetGraphError("promotion kind must be cabinet_frontier_promotion")
    if promotion.get("mode") != "proposal_only":
        raise CabinetGraphError("Cabinet frontier promotion must stay proposal_only")
    _expect_false(promotion.get("dispatchAllowed"), "Cabinet frontier promotion dispatchAllowed")
    _expect_false(
        promotion.get("queueMutationAllowed"),
        "Cabinet frontier promotion queueMutationAllowed",
    )
    _expect_false(
        promotion.get("taskCreationAllowed"),
        "Cabinet frontier promotion taskCreationAllowed",
    )

    task = _expect_object(promotion.get("task"), "Cabinet promotion task")
    metadata = _expect_object(task.get("metadata"), "Cabinet promotion task metadata")
    _expect_false(metadata.get("dispatch_allowed"), "Cabinet promotion task dispatch_allowed")
    _expect_false(
        metadata.get("queue_mutation_allowed"),
        "Cabinet promotion task queue_mutation_allowed",
    )
    _expect_false(
        metadata.get("task_creation_allowed"),
        "Cabinet promotion task task_creation_allowed",
    )
    return task


def write_promotion_task(proposal: dict[str, Any], path: str | Path) -> dict[str, Any]:
    """Write one Cabinet promotion task proposal to a JSON file only.

    This is deliberately not a Registry import and not dispatch. Existing files
    are refused so the operation remains explicit and reviewable.
    """
    task = _promotion_task(proposal)
    task_path = Path(path)
    if not str(task_path).strip():
        raise CabinetGraphError("promotion task write requires a non-empty path")
    if not task_path.parent.exists():
        raise CabinetGraphError(f"promotion task write parent missing: {task_path.parent}")

    rendered = _render_task(task)
    try:
        with task_path.open("x", encoding="utf-8") as handle:
            handle.write(rendered)
    except FileExistsError as exc:
        raise CabinetGraphError(f"promotion task file already exists: {task_path}") from exc
    except OSError as exc:
        raise CabinetGraphError(
            f"promotion task file cannot be written: {task_path}: {exc.__class__.__name__}"
        ) from exc

    return {
        "schemaVersion": 1,
        "kind": "cabinet_promotion_task_write",
        "mode": "file_only",
        "path": str(task_path),
        "bytes": len(rendered.encode("utf-8")),
        "dispatchAllowed": False,
        "queueMutationAllowed": False,
        "taskCreationAllowed": False,
        "registryMutationAllowed": False,
    }



def _expect_non_empty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CabinetGraphError(f"{label} must be a non-empty string")
    return value.strip()


def _expect_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise CabinetGraphError(f"{label} must be a list")
    return value


def _task_sha256(task: dict[str, Any]) -> str:
    rendered = json.dumps(task, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _render_task(task: dict[str, Any]) -> str:
    return json.dumps(task, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def validate_promotion_task(task: dict[str, Any]) -> dict[str, Any]:
    """Validate one file-only Cabinet promotion task proposal.

    This validates the dry task artifact written by CAB-ECO-007. It does not
    import the task into the Bureau registry and does not dispatch work.
    """
    task = _expect_object(task, "Cabinet promotion task")
    if task.get("schema_version") != 1:
        raise CabinetGraphError("Cabinet promotion task schema_version must be 1")
    task_id = _expect_non_empty_string(task.get("id"), "Cabinet promotion task id")
    initiative = _expect_non_empty_string(
        task.get("initiative"), "Cabinet promotion task initiative"
    )
    _expect_non_empty_string(task.get("title"), "Cabinet promotion task title")
    if task.get("state") != "planned":
        raise CabinetGraphError("Cabinet promotion task state must be planned")

    execution = _expect_object(task.get("execution"), "Cabinet promotion task execution")
    if execution.get("mode") != "manual":
        raise CabinetGraphError("Cabinet promotion task execution mode must be manual")
    if execution.get("policy") != "review-before-effect":
        raise CabinetGraphError(
            "Cabinet promotion task execution policy must be review-before-effect"
        )

    capabilities = _expect_list(
        task.get("required_capabilities"), "Cabinet promotion task capabilities"
    )
    if "repository" not in capabilities or "review" not in capabilities:
        raise CabinetGraphError(
            "Cabinet promotion task capabilities must include repository and review"
        )

    claims = _expect_list(task.get("claims"), "Cabinet promotion task claims")
    if not claims:
        raise CabinetGraphError("Cabinet promotion task claims must not be empty")
    for index, raw_claim in enumerate(claims):
        claim = _expect_object(raw_claim, f"Cabinet promotion task claim {index}")
        if claim.get("mode") != "read":
            raise CabinetGraphError("Cabinet promotion task claims must stay read-only")
        if claim.get("isolation") != "none":
            raise CabinetGraphError("Cabinet promotion task claims must keep isolation none")

    acceptance = _expect_list(
        task.get("acceptance"), "Cabinet promotion task acceptance"
    )
    acceptance_ids = {
        item.get("id")
        for item in acceptance
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    if "target-proof" not in acceptance_ids:
        raise CabinetGraphError("Cabinet promotion task must include target-proof acceptance")
    if "no-auto-dispatch" not in acceptance_ids:
        raise CabinetGraphError("Cabinet promotion task must include no-auto-dispatch acceptance")

    metadata = _expect_object(task.get("metadata"), "Cabinet promotion task metadata")
    if metadata.get("source") != "cabinet_frontier_export":
        raise CabinetGraphError("Cabinet promotion task source must be cabinet_frontier_export")
    _expect_non_empty_string(
        metadata.get("source_candidate_id"), "Cabinet promotion task source_candidate_id"
    )
    source_candidate = _expect_object(
        metadata.get("source_candidate"), "Cabinet promotion task source_candidate"
    )
    _expect_false(source_candidate.get("dispatchAllowed"), "source candidate dispatchAllowed")
    _expect_false(metadata.get("dispatch_allowed"), "task metadata dispatch_allowed")
    _expect_false(
        metadata.get("queue_mutation_allowed"), "task metadata queue_mutation_allowed"
    )
    _expect_false(
        metadata.get("task_creation_allowed"), "task metadata task_creation_allowed"
    )

    return {
        "schemaVersion": 1,
        "kind": "cabinet_promotion_task_validation",
        "mode": "file_only",
        "valid": True,
        "taskId": task_id,
        "initiative": initiative,
        "dispatchAllowed": False,
        "queueMutationAllowed": False,
        "taskCreationAllowed": False,
        "registryMutationAllowed": False,
    }


def load_promotion_task(path: str | Path) -> dict[str, Any]:
    task_path = Path(path)
    try:
        raw = task_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise CabinetGraphError(f"promotion task file missing: {task_path}") from exc
    except OSError as exc:
        raise CabinetGraphError(
            f"promotion task file cannot be read: {task_path}: {exc.__class__.__name__}"
        ) from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CabinetGraphError(f"promotion task file is invalid JSON: {exc.msg}") from exc
    return _expect_object(value, "Cabinet promotion task file")


def validate_promotion_task_file(path: str | Path) -> dict[str, Any]:
    task_path = Path(path)
    receipt = validate_promotion_task(load_promotion_task(task_path))
    return {**receipt, "path": str(task_path)}



def preview_promotion_task_import(
    task: dict[str, Any],
    *,
    registry: Any | None = None,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Preview importing one Cabinet promotion task into a Bureau registry.

    The preview is deliberately read-only. It validates the same dry task artifact
    as CAB-ECO-008, then optionally checks the active Bureau registry context:
    JSON Schema compatibility, task-id availability and initiative existence.
    """
    receipt = validate_promotion_task(task)
    task_id = receipt["taskId"]
    initiative = receipt["initiative"]
    schema_validated = False
    initiative_known: bool | None = None

    if registry is not None:
        try:
            registry.schemas.validate(
                "task",
                task,
                Path(path) if path is not None else Path("<cabinet-promotion-task>"),
            )
        except Exception as exc:
            raise CabinetGraphError(
                f"promotion task does not satisfy Bureau task schema: {exc}"
            ) from exc
        schema_validated = True

        if task_id in getattr(registry, "tasks", {}):
            raise CabinetGraphError(f"promotion task already exists in registry: {task_id}")
        initiative_known = initiative in getattr(registry, "initiatives", {})
        if not initiative_known:
            raise CabinetGraphError(
                f"promotion task initiative missing from registry: {initiative}"
            )

    return {
        "schemaVersion": 1,
        "kind": "cabinet_promotion_task_import_preview",
        "mode": "dry_run",
        "valid": True,
        "importReady": True,
        "taskId": task_id,
        "initiative": initiative,
        "path": str(path) if path is not None else None,
        "checks": {
            "taskValidation": True,
            "taskSchema": schema_validated,
            "taskIdAvailable": True,
            "initiativeKnown": initiative_known,
        },
        "dispatchAllowed": False,
        "queueMutationAllowed": False,
        "taskCreationAllowed": False,
        "registryMutationAllowed": False,
    }


def preview_promotion_task_import_file(
    path: str | Path,
    *,
    registry: Any | None = None,
) -> dict[str, Any]:
    task_path = Path(path)
    return preview_promotion_task_import(
        load_promotion_task(task_path), registry=registry, path=task_path
    )



def import_reviewed_promotion_task(
    task: dict[str, Any],
    *,
    registry: Any,
    reviewer: str,
    apply: bool,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Review-gated import for one Cabinet promotion task.

    Without ``apply`` this is only a dry run. With ``apply`` it creates exactly
    one task file in ``registry/tasks`` using exclusive create; it never touches
    the queue and never dispatches work.
    """
    reviewer_id = _expect_non_empty_string(reviewer, "promotion task reviewer")
    preview = preview_promotion_task_import(task, registry=registry, path=path)
    task_id = preview["taskId"]
    target_dir = Path(registry.root) / "registry" / "tasks"
    target_path = target_dir / f"{task_id}.json"
    if target_path.exists() or target_path.is_symlink():
        raise CabinetGraphError(f"promotion task already exists in registry: {task_id}")

    base = {
        "schemaVersion": 1,
        "kind": "cabinet_promotion_task_reviewed_import",
        "taskId": task_id,
        "initiative": preview["initiative"],
        "sourcePath": str(path) if path is not None else None,
        "targetPath": str(target_path),
        "reviewedBy": reviewer_id,
        "checks": preview["checks"],
        "dispatchAllowed": False,
        "queueMutationAllowed": False,
        "dispatchPerformed": False,
        "queueMutationPerformed": False,
    }
    if not apply:
        return {
            **base,
            "mode": "dry_run",
            "importReady": True,
            "registryMutationAllowed": False,
            "registryMutationPerformed": False,
            "taskCreationPerformed": False,
        }

    if not target_dir.is_dir():
        raise CabinetGraphError(f"promotion task registry directory missing: {target_dir}")

    imported_task = json.loads(json.dumps(task))
    metadata = _expect_object(
        imported_task.setdefault("metadata", {}),
        "Cabinet promotion task metadata",
    )
    metadata["reviewed_import"] = {
        "source": "systemkatalog-import-reviewed",
        "reviewer": reviewer_id,
        "source_task_file": str(path) if path is not None else None,
        "source_task_sha256": _task_sha256(task),
        "dispatch_performed": False,
        "queue_mutation_performed": False,
    }
    try:
        registry.schemas.validate("task", imported_task, target_path)
    except Exception as exc:
        raise CabinetGraphError(
            f"reviewed promotion task does not satisfy Bureau task schema: {exc}"
        ) from exc

    rendered = _render_task(imported_task)
    try:
        with target_path.open("x", encoding="utf-8") as handle:
            handle.write(rendered)
    except FileExistsError as exc:
        raise CabinetGraphError(f"promotion task already exists in registry: {task_id}") from exc
    except OSError as exc:
        raise CabinetGraphError(
            f"promotion task cannot be imported: {target_path}: {exc.__class__.__name__}"
        ) from exc

    return {
        **base,
        "mode": "apply",
        "importReady": True,
        "bytes": len(rendered.encode("utf-8")),
        "registryMutationAllowed": True,
        "registryMutationPerformed": True,
        "taskCreationPerformed": True,
    }


def import_reviewed_promotion_task_file(
    path: str | Path,
    *,
    registry: Any,
    reviewer: str,
    apply: bool,
) -> dict[str, Any]:
    task_path = Path(path)
    return import_reviewed_promotion_task(
        load_promotion_task(task_path),
        registry=registry,
        reviewer=reviewer,
        apply=apply,
        path=task_path,
    )
