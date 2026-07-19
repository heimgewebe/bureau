from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any

SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,160}\Z")
TERMINAL_TASK_STATES = {"verified", "cancelled", "superseded"}
DISPOSITIONS = {"removed", "archived", "recovery", "still-required"}
REQUIRED_BOUNDARIES = {
    "source_truth",
    "effect_authority",
    "recovery_completion",
}
MAX_PREDECESSORS = 100


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def generation_closeout_sha256(
    transition: dict[str, Any], closeout: dict[str, Any]
) -> str:
    material = {
        "generation_transition": transition,
        "generation_closeout": {
            key: value
            for key, value in closeout.items()
            if key != "closeout_sha256"
        },
    }
    return _canonical_sha256(material)


def _mapping(value: Any, label: str, errors: list[str]) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return None
    return value


def _identifier(value: Any, label: str, errors: list[str]) -> str | None:
    if not isinstance(value, str) or ID_RE.fullmatch(value) is None:
        errors.append(f"{label} must match {ID_RE.pattern}")
        return None
    return value


def _nonempty(value: Any, label: str, errors: list[str]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{label} must be non-empty text")
        return None
    return value.strip()


def _timestamp(value: Any, label: str, errors: list[str]) -> str | None:
    observed = _nonempty(value, label, errors)
    if observed is None:
        return None
    try:
        parsed = datetime.fromisoformat(observed.replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"{label} must be an ISO-8601 date-time")
        return None
    if parsed.tzinfo is None:
        errors.append(f"{label} must include a timezone")
        return None
    return observed


def _validate_boundaries(value: Any, label: str, errors: list[str]) -> None:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        errors.append(f"{label} must contain text")
        return
    missing = sorted(REQUIRED_BOUNDARIES - set(value))
    if missing:
        errors.append(f"{label} misses boundaries: {missing}")


def _validate_transition(
    transition: dict[str, Any], errors: list[str]
) -> tuple[str | None, dict[str, dict[str, Any]]]:
    if transition.get("schema_version") != 1:
        errors.append("generation_transition.schema_version must be 1")
    transition_id = _identifier(
        transition.get("transition_id"),
        "generation_transition.transition_id",
        errors,
    )
    _nonempty(
        transition.get("successor_ref"),
        "generation_transition.successor_ref",
        errors,
    )
    _validate_boundaries(
        transition.get("does_not_establish"),
        "generation_transition.does_not_establish",
        errors,
    )
    predecessors = transition.get("predecessors")
    if not isinstance(predecessors, list) or not 1 <= len(predecessors) <= MAX_PREDECESSORS:
        errors.append(
            "generation_transition.predecessors must contain between 1 and "
            f"{MAX_PREDECESSORS} entries"
        )
        return transition_id, {}
    result: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(predecessors):
        item = _mapping(raw, f"generation_transition.predecessors[{index}]", errors)
        if item is None:
            continue
        surface_id = _identifier(
            item.get("surface_id"),
            f"generation_transition.predecessors[{index}].surface_id",
            errors,
        )
        _nonempty(
            item.get("surface_kind"),
            f"generation_transition.predecessors[{index}].surface_kind",
            errors,
        )
        _nonempty(
            item.get("source_ref"),
            f"generation_transition.predecessors[{index}].source_ref",
            errors,
        )
        if surface_id is None:
            continue
        if surface_id in result:
            errors.append(f"generation_transition duplicate surface_id {surface_id}")
        else:
            result[surface_id] = item
    return transition_id, result


def _validate_classification(
    raw: Any,
    index: int,
    errors: list[str],
) -> tuple[str | None, dict[str, Any] | None]:
    label = f"generation_closeout.classifications[{index}]"
    item = _mapping(raw, label, errors)
    if item is None:
        return None, None
    surface_id = _identifier(item.get("surface_id"), f"{label}.surface_id", errors)
    disposition = item.get("disposition")
    if disposition not in DISPOSITIONS:
        errors.append(f"{label}.disposition must be one of {sorted(DISPOSITIONS)}")
    _nonempty(item.get("source_ref"), f"{label}.source_ref", errors)
    digest = item.get("evidence_sha256")
    if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
        errors.append(f"{label}.evidence_sha256 must be a lowercase sha256")
    _timestamp(item.get("observed_at"), f"{label}.observed_at", errors)
    if disposition == "archived":
        _nonempty(item.get("archive_ref"), f"{label}.archive_ref", errors)
    if disposition == "recovery":
        _nonempty(item.get("recovery_ref"), f"{label}.recovery_ref", errors)
    if disposition == "still-required":
        _nonempty(item.get("reason"), f"{label}.reason", errors)
    return surface_id, item


def validate_generation_closeout(task: dict[str, Any]) -> list[str]:
    task_id = str(task.get("id", "<unknown-task>"))
    metadata = task.get("metadata", {})
    if not isinstance(metadata, dict):
        return [f"task {task_id} metadata must be an object"]
    transition_raw = metadata.get("generation_transition")
    closeout_raw = metadata.get("generation_closeout")
    if transition_raw is None and closeout_raw is None:
        return []

    errors: list[str] = []
    transition = _mapping(transition_raw, "generation_transition", errors)
    closeout = (
        _mapping(closeout_raw, "generation_closeout", errors)
        if closeout_raw is not None
        else None
    )
    if transition is None:
        return [f"task {task_id}: {error}" for error in errors]
    transition_id, predecessors = _validate_transition(transition, errors)

    terminal = task.get("state") in TERMINAL_TASK_STATES
    if closeout is None:
        if terminal:
            errors.append(
                "terminal generation transition requires generation_closeout"
            )
        return [f"task {task_id}: {error}" for error in errors]

    if closeout.get("schema_version") != 1:
        errors.append("generation_closeout.schema_version must be 1")
    closeout_transition_id = _identifier(
        closeout.get("transition_id"),
        "generation_closeout.transition_id",
        errors,
    )
    if (
        transition_id is not None
        and closeout_transition_id is not None
        and transition_id != closeout_transition_id
    ):
        errors.append("generation_closeout.transition_id does not match transition")

    classifications = closeout.get("classifications")
    classified: dict[str, dict[str, Any]] = {}
    if not isinstance(classifications, list) or not 1 <= len(classifications) <= MAX_PREDECESSORS:
        errors.append(
            "generation_closeout.classifications must contain between 1 and "
            f"{MAX_PREDECESSORS} entries"
        )
    else:
        for index, raw in enumerate(classifications):
            surface_id, item = _validate_classification(raw, index, errors)
            if surface_id is None or item is None:
                continue
            if surface_id in classified:
                errors.append(f"generation_closeout duplicate surface_id {surface_id}")
            else:
                classified[surface_id] = item

    missing = sorted(set(predecessors) - set(classified))
    extra = sorted(set(classified) - set(predecessors))
    if missing:
        errors.append(f"generation_closeout missing predecessors: {missing}")
    if extra:
        errors.append(f"generation_closeout has undeclared predecessors: {extra}")

    _validate_boundaries(
        closeout.get("does_not_establish"),
        "generation_closeout.does_not_establish",
        errors,
    )

    observed_hash = closeout.get("closeout_sha256")
    if not isinstance(observed_hash, str) or SHA256_RE.fullmatch(observed_hash) is None:
        errors.append("generation_closeout.closeout_sha256 must be a lowercase sha256")
    elif observed_hash != generation_closeout_sha256(transition, closeout):
        errors.append("generation_closeout.closeout_sha256 does not match content")

    return [f"task {task_id}: {error}" for error in errors]
