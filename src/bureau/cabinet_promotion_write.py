from __future__ import annotations

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

    rendered = json.dumps(task, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
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
