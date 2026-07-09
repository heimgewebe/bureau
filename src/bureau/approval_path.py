"""Approval path classification and fail-closed gate for Bureau effects.

This module is deliberately pure: it reads a task-like mapping and optional
approval evidence, then returns an auditable decision. It does not mutate the
registry, dispatch agents, write files, or inspect live runtime state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final


class ApprovalPathError(ValueError):
    """Raised when an approval path request is malformed."""


READ_ONLY_EFFECTS: Final[frozenset[str]] = frozenset(
    {
        "read_observation",
        "planning_proposal",
    }
)

KNOWN_EFFECT_CLASSES: Final[frozenset[str]] = frozenset(
    {
        *READ_ONLY_EFFECTS,
        "registry_mutation",
        "repository_mutation",
        "source_import",
        "agent_dispatch",
        "task_creation_from_external_evidence",
        "runtime_mutation",
        "privileged_mutation",
        "prohibited",
    }
)

APPROVAL_LEVEL_RANK: Final[dict[str, int]] = {
    "none": 0,
    "reviewed_plan": 1,
    "operator": 2,
    "privileged_operator": 3,
}

APPROVAL_LEVEL_ALIASES: Final[dict[str, str]] = {
    "none": "none",
    "no_approval": "none",
    "reviewed-plan": "reviewed_plan",
    "reviewed_plan": "reviewed_plan",
    "review_before_effect": "reviewed_plan",
    "review-before-effect": "reviewed_plan",
    "explicit_operator": "operator",
    "operator": "operator",
    "human": "operator",
    "privileged_operator": "privileged_operator",
    "privileged-operator": "privileged_operator",
    "captain": "privileged_operator",
}

EFFECT_REQUIRED_APPROVAL: Final[dict[str, str]] = {
    "read_observation": "none",
    "planning_proposal": "none",
    "registry_mutation": "reviewed_plan",
    "task_creation_from_external_evidence": "reviewed_plan",
    "source_import": "operator",
    "repository_mutation": "operator",
    "agent_dispatch": "operator",
    "runtime_mutation": "operator",
    "privileged_mutation": "privileged_operator",
}

PROHIBITED_EFFECT_CLASSES: Final[frozenset[str]] = frozenset({"prohibited"})

DOES_NOT_ESTABLISH: Final[list[str]] = [
    "task correctness",
    "runtime correctness",
    "review completeness",
    "merge readiness",
    "operator intent beyond the approved scope",
]


@dataclass(frozen=True)
class ApprovalClass:
    effect_class: str
    required_approval: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {
            "effect_class": self.effect_class,
            "required_approval": self.required_approval,
            "reason": self.reason,
        }


def normalize_approval_level(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    key = value.strip().lower().replace(" ", "_")
    return APPROVAL_LEVEL_ALIASES.get(key)


def _max_approval_level(levels: list[str]) -> str:
    if not levels:
        return "none"
    return max(levels, key=lambda item: APPROVAL_LEVEL_RANK[item])


def _has_truthy_marker(mapping: dict[str, Any], keys: tuple[str, ...]) -> bool:
    return any(mapping.get(key) is True for key in keys)


def _metadata(task: dict[str, Any]) -> dict[str, Any]:
    metadata = task.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _execution(task: dict[str, Any]) -> dict[str, Any]:
    execution = task.get("execution")
    return execution if isinstance(execution, dict) else {}


def _claims(task: dict[str, Any]) -> list[dict[str, Any]]:
    claims = task.get("claims")
    if not isinstance(claims, list):
        return []
    return [claim for claim in claims if isinstance(claim, dict)]


def classify_task_effects(
    task: dict[str, Any], *, requested_effects: list[str] | tuple[str, ...] | None = None
) -> list[ApprovalClass]:
    """Infer effect classes for a task and explicit requested effects.

    The classifier errs on the side of requiring stronger approval when a task
    declares write/exclusive claims, external import markers, dispatch mode, or
    privileged metadata. Unknown requested effects are rejected by the caller
    instead of silently becoming read-only.
    """

    classes: dict[str, ApprovalClass] = {}

    def add(effect_class: str, reason: str) -> None:
        required = EFFECT_REQUIRED_APPROVAL.get(effect_class, "none")
        classes.setdefault(effect_class, ApprovalClass(effect_class, required, reason))

    execution = _execution(task)
    metadata = _metadata(task)
    policy = execution.get("policy")
    mode = execution.get("mode")

    if policy == "prohibited":
        add("prohibited", "execution policy is prohibited")
    if mode == "manual" and policy == "prohibited":
        add("prohibited", "manual/prohibited task cannot be approved by this gate")
    if mode == "grabowski-task":
        add("agent_dispatch", "grabowski-task mode can start an external worker")

    if _has_truthy_marker(
        metadata,
        (
            "source_import",
            "cabinet_import",
            "external_source_import",
            "reviewed_import",
        ),
    ):
        add("source_import", "metadata marks this task as source/import work")
    if _has_truthy_marker(
        metadata,
        (
            "external_evidence_task_creation",
            "task_creation_from_external_evidence",
            "creates_task_from_external_evidence",
        ),
    ):
        add(
            "task_creation_from_external_evidence",
            "metadata marks task creation from external evidence",
        )
    if _has_truthy_marker(metadata, ("runtime_mutation", "service_restart", "deploy")):
        add("runtime_mutation", "metadata marks runtime mutation")
    if _has_truthy_marker(metadata, ("privileged_mutation", "sudo", "root")):
        add("privileged_mutation", "metadata marks privileged mutation")

    operation = execution.get("operation")
    if isinstance(operation, str):
        lowered = operation.lower()
        if any(token in lowered for token in ("import", "sync", "fetch")):
            add("source_import", f"execution operation looks like source import: {operation}")
        if any(token in lowered for token in ("deploy", "restart", "systemd")):
            add("runtime_mutation", f"execution operation looks like runtime mutation: {operation}")

    for claim in _claims(task):
        mode_value = claim.get("mode")
        resource = claim.get("resource")
        if mode_value not in {"write", "exclusive", "capacity"} or not isinstance(resource, str):
            continue
        if resource == "repo.bureau" or resource.startswith("registry"):
            add("registry_mutation", f"claim {resource}:{mode_value} can mutate Bureau registry")
        elif resource.startswith("repo.") or resource == "repo":
            add("repository_mutation", f"claim {resource}:{mode_value} can mutate a repository")
        else:
            add("registry_mutation", f"claim {resource}:{mode_value} is a non-read claim")

    if requested_effects:
        for effect in requested_effects:
            if effect not in KNOWN_EFFECT_CLASSES:
                raise ApprovalPathError(f"unknown effect class: {effect}")
            if effect == "prohibited":
                add(effect, "requested effect is prohibited")
            else:
                add(effect, "requested effect")

    if not classes:
        add(
            "read_observation",
            "no write, dispatch, import, runtime, or privileged effect inferred",
        )
    return sorted(
        classes.values(),
        key=lambda item: (APPROVAL_LEVEL_RANK.get(item.required_approval, 99), item.effect_class),
    )


def _approval_scope_contains(approval: dict[str, Any], effect_classes: set[str]) -> bool:
    scope = approval.get("scope")
    if scope == "task":
        return True
    if isinstance(scope, str):
        return scope in effect_classes
    if isinstance(scope, list):
        values = {item for item in scope if isinstance(item, str)}
        return bool(values & effect_classes) or "task" in values
    return False


def evaluate_approval_path(
    task: dict[str, Any],
    *,
    requested_effects: list[str] | tuple[str, ...] | None = None,
    approval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a deterministic approval decision for a task/effect request."""

    classes = classify_task_effects(task, requested_effects=requested_effects)
    effect_classes = {item.effect_class for item in classes}
    required_level = _max_approval_level(
        [
            item.required_approval
            for item in classes
            if item.effect_class not in PROHIBITED_EFFECT_CLASSES
        ]
    )
    reasons: list[str] = []
    blockers: list[str] = []

    if effect_classes & PROHIBITED_EFFECT_CLASSES:
        blockers.append("prohibited effect class cannot be approved")

    approval_level = "none"
    approval_present = isinstance(approval, dict)
    if required_level != "none" and approval is None:
        embedded = _execution(task).get("approval")
        approval = embedded if isinstance(embedded, dict) else None
        approval_present = isinstance(approval, dict)

    if approval_present and approval is not None:
        level = normalize_approval_level(approval.get("level"))
        if level is None:
            blockers.append("approval level is missing or unknown")
        else:
            approval_level = level
        if approval.get("approved") is not True:
            blockers.append("approval approved=true is required")
        if approval.get("decision") not in {"approve", "approved", None}:
            blockers.append("approval decision is not approve")
        task_id = task.get("id")
        if isinstance(task_id, str) and approval.get("task_id") not in {task_id, None}:
            blockers.append("approval task_id does not match task")
        if not _approval_scope_contains(approval, effect_classes):
            blockers.append("approval scope does not cover requested effect classes")
        reviewer = approval.get("reviewer")
        if not isinstance(reviewer, str) or not reviewer.strip():
            blockers.append("approval reviewer is required")
    elif required_level != "none":
        blockers.append(f"approval evidence required at level {required_level}")

    if APPROVAL_LEVEL_RANK[approval_level] < APPROVAL_LEVEL_RANK[required_level]:
        blockers.append(
            f"approval level {approval_level} is below required level {required_level}"
        )

    if required_level == "none" and not blockers:
        reasons.append("read-only or planning effect needs no explicit approval")
    elif not blockers:
        reasons.append(f"approval satisfies required level {required_level}")

    status = "allowed" if not blockers else "blocked"
    return {
        "schema_version": 1,
        "status": status,
        "task_id": task.get("id"),
        "effect_classes": [item.as_dict() for item in classes],
        "required_approval": required_level,
        "approval_present": approval_present,
        "approval_level": approval_level,
        "blockers": blockers,
        "reasons": reasons,
        "operator_relay_compatible": True,
        "does_not_establish": DOES_NOT_ESTABLISH,
    }
