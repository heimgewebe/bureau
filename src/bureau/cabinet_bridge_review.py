from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .cabinet_bridge import CabinetBridgeError


EFFECT_FLAGS = ("dispatchAllowed", "queueMutationAllowed", "taskCreationAllowed")
METADATA_EFFECT_FLAGS = ("dispatch_allowed", "queue_mutation_allowed", "task_creation_allowed")


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CabinetBridgeError(f"{label} must be an object")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise CabinetBridgeError(f"{label} must be a list")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CabinetBridgeError(f"{label} must be a non-empty string")
    return value.strip()


def _load_preview(path: str | Path) -> dict[str, Any]:
    preview_path = Path(path).expanduser()
    try:
        preview = json.loads(preview_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CabinetBridgeError(f"preview missing: {preview_path}") from exc
    except json.JSONDecodeError as exc:
        raise CabinetBridgeError(f"preview invalid JSON: {exc.msg}") from exc
    return _object(preview, "preview")


def _require_false(container: dict[str, Any], fields: tuple[str, ...], label: str) -> None:
    for field in fields:
        if container.get(field) is not False:
            raise CabinetBridgeError(f"{label} must keep {field} false")


def review_preview(path: str | Path) -> dict[str, Any]:
    preview = _load_preview(path)
    if preview.get("schemaVersion") != 1:
        raise CabinetBridgeError("preview schemaVersion must be 1")
    if preview.get("kind") != "cabinet_bridge_promotion_preview":
        raise CabinetBridgeError("preview kind must be cabinet_bridge_promotion_preview")
    if preview.get("mode") != "proposal_only":
        raise CabinetBridgeError("preview mode must be proposal_only")
    _require_false(preview, EFFECT_FLAGS, "preview")

    task = _object(preview.get("task"), "preview task")
    if task.get("schema_version") != 1:
        raise CabinetBridgeError("preview task schema_version must be 1")
    task_id = _text(task.get("id"), "preview task id")
    execution = _object(task.get("execution"), "preview task execution")
    if execution.get("mode") != "manual":
        raise CabinetBridgeError("preview task execution mode must be manual")
    if execution.get("policy") != "review-before-effect":
        raise CabinetBridgeError("preview task policy must be review-before-effect")

    capabilities = {str(item) for item in _list(task.get("required_capabilities"), "capabilities")}
    if "review" not in capabilities:
        raise CabinetBridgeError("preview task must require review capability")
    for claim in _list(task.get("claims"), "preview task claims"):
        claim_obj = _object(claim, "preview task claim")
        if claim_obj.get("mode") != "read":
            raise CabinetBridgeError("preview task claims must stay read-only")

    acceptance_ids = {
        str(_object(item, "acceptance item").get("id"))
        for item in _list(task.get("acceptance"), "acceptance")
    }
    required_acceptance = {"target-proof", "no-auto-effect"}
    if not required_acceptance.issubset(acceptance_ids):
        missing = sorted(required_acceptance - acceptance_ids)
        raise CabinetBridgeError("preview task acceptance missing: " + ",".join(missing))

    metadata = _object(task.get("metadata"), "preview task metadata")
    _require_false(metadata, METADATA_EFFECT_FLAGS, "preview metadata")
    if metadata.get("source") != "cabinet_bridge_probe":
        raise CabinetBridgeError("preview metadata source must be cabinet_bridge_probe")
    source_candidate_id = _text(metadata.get("source_candidate_id"), "source_candidate_id")
    candidate = _object(metadata.get("source_candidate"), "source candidate")
    if candidate.get("id") != source_candidate_id:
        raise CabinetBridgeError("source candidate id mismatch")
    if candidate.get("decision") != "admissible":
        raise CabinetBridgeError("source candidate must be admissible")
    if candidate.get("reasons") not in ([], None):
        raise CabinetBridgeError("source candidate must not have blocking reasons")

    return {
        "schemaVersion": 1,
        "kind": "cabinet_bridge_preview_review_gate",
        "status": "requires_human_review",
        "reviewRequired": True,
        "importAllowed": False,
        "dispatchAllowed": False,
        "queueMutationAllowed": False,
        "taskCreationAllowed": False,
        "taskId": task_id,
        "sourceCandidateId": source_candidate_id,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="bureau-cabinet-bridge-review")
    result.add_argument("--preview", required=True)
    result.add_argument("--json", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        value = review_preview(args.preview)
    except CabinetBridgeError as exc:
        print(f"bureau-cabinet-bridge-review: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(value, indent=2 if args.json else None, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
