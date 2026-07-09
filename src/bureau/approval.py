"""Explicit approval semantics for Bureau effect boundaries.

The module is deliberately small and deterministic. It does not grant authority;
it classifies effectful actions and verifies that a caller supplied an explicit,
source-bound approval record before the action is allowed to continue.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import legacy

APPROVAL_SCHEMA_VERSION = 1

READ_ONLY_ACTIONS = frozenset({"read_only_observation", "dry_run", "proposal_preview"})

APPROVAL_RULES: dict[str, dict[str, Any]] = {
    "repository_mutation": {
        "required_level": "operator",
        "reason": (
            "repository writes, branch operations, commits, pushes or merges "
            "change source state"
        ),
    },
    "source_import": {
        "required_level": "reviewed_receipt",
        "reason": "source imports convert external evidence into Bureau registry material",
    },
    "agent_dispatch": {
        "required_level": "operator",
        "reason": (
            "agent dispatch creates external work that may mutate repositories "
            "or runtime state"
        ),
    },
    "task_creation_from_external_evidence": {
        "required_level": "operator",
        "reason": (
            "external evidence may propose tasks, but Bureau task creation "
            "requires explicit approval"
        ),
    },
    "queue_mutation": {
        "required_level": "reviewed_plan",
        "reason": "queue order controls what agents may pick next",
    },
    "runtime_mutation": {
        "required_level": "break_glass",
        "reason": "runtime mutation may restart, deploy or alter live services",
    },
}

LEVEL_ORDER = {
    "none": 0,
    "operator": 10,
    "reviewed_plan": 20,
    "reviewed_receipt": 20,
    "break_glass": 30,
}


@dataclass(frozen=True)
class ApprovalEvidence:
    """One explicit approval input for an effectful Bureau action."""

    source: str
    level: str
    approved: bool
    reviewer: str | None = None
    reference: str | None = None
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": APPROVAL_SCHEMA_VERSION,
            "source": self.source,
            "level": self.level,
            "approved": self.approved,
        }
        if self.reviewer:
            result["reviewer"] = self.reviewer
        if self.reference:
            result["reference"] = self.reference
        if self.note:
            result["note"] = self.note
        return result


def explicit_operator_approval(
    *,
    source: str,
    approved: bool,
    reviewer: str | None = None,
    reference: str | None = None,
    note: str | None = None,
) -> ApprovalEvidence:
    """Return a normalized operator approval record from a visible command flag."""
    return ApprovalEvidence(
        source=source,
        level="operator",
        approved=bool(approved),
        reviewer=reviewer,
        reference=reference,
        note=note,
    )


def reviewed_plan_approval(
    *, reviewer: str, reference: str, approved: bool = True
) -> ApprovalEvidence:
    return ApprovalEvidence(
        source="reviewed_plan",
        level="reviewed_plan",
        approved=approved,
        reviewer=reviewer,
        reference=reference,
    )


def reviewed_receipt_approval(
    *, reviewer: str, reference: str, approved: bool = True
) -> ApprovalEvidence:
    return ApprovalEvidence(
        source="reviewed_receipt",
        level="reviewed_receipt",
        approved=approved,
        reviewer=reviewer,
        reference=reference,
    )


def approval_decision(
    action_class: str,
    approval: ApprovalEvidence | None,
) -> dict[str, Any]:
    """Evaluate approval for one action class without mutating anything."""
    if action_class in READ_ONLY_ACTIONS:
        return {
            "schema_version": APPROVAL_SCHEMA_VERSION,
            "action_class": action_class,
            "required": False,
            "required_level": "none",
            "allowed": True,
            "reason": "read-only or dry-run action",
            "evidence": approval.as_dict() if approval else None,
        }
    rule = APPROVAL_RULES.get(action_class)
    if rule is None:
        return {
            "schema_version": APPROVAL_SCHEMA_VERSION,
            "action_class": action_class,
            "required": True,
            "required_level": "unknown",
            "allowed": False,
            "reason": "unknown action class fails closed",
            "evidence": approval.as_dict() if approval else None,
        }
    required_level = str(rule["required_level"])
    evidence = approval.as_dict() if approval else None
    level_ok = False
    if approval is not None:
        level_ok = LEVEL_ORDER.get(approval.level, -1) >= LEVEL_ORDER[required_level]
    allowed = bool(approval is not None and approval.approved and level_ok)
    reason = "approved" if allowed else str(rule["reason"])
    if approval is None:
        reason = f"explicit approval missing: {reason}"
    elif not approval.approved:
        reason = f"approval record is not approved: {reason}"
    elif not level_ok:
        reason = f"approval level {approval.level} is below required {required_level}"
    return {
        "schema_version": APPROVAL_SCHEMA_VERSION,
        "action_class": action_class,
        "required": True,
        "required_level": required_level,
        "allowed": allowed,
        "reason": reason,
        "evidence": evidence,
    }


def require_approval(
    action_class: str,
    approval: ApprovalEvidence | None,
) -> dict[str, Any]:
    """Return an approval decision or raise StateError before any effect."""
    decision = approval_decision(action_class, approval)
    if not decision["allowed"]:
        raise legacy.StateError(
            f"approval required for {action_class}: {decision['reason']}"
        )
    return decision


def task_approval_contract(task: dict[str, Any]) -> dict[str, Any]:
    """Summarize the declared approval contract of a task.

    The declaration is advisory unless a caller routes an effectful action
    through require_approval. Missing declarations fail closed for effectful task
    policies and pass for read-only/manual proposal tasks.
    """
    execution = task.get("execution") if isinstance(task.get("execution"), dict) else {}
    declared = execution.get("approval") if isinstance(execution.get("approval"), dict) else {}
    action_class = declared.get("action_class")
    if not action_class:
        mode = str(execution.get("mode", ""))
        policy = str(execution.get("policy", ""))
        claims = task.get("claims") if isinstance(task.get("claims"), list) else []
        has_write_claim = any(
            isinstance(claim, dict) and claim.get("mode") in {"write", "exclusive"}
            for claim in claims
        )
        if has_write_claim:
            action_class = "repository_mutation"
        elif mode == "grabowski-task":
            action_class = "agent_dispatch"
        elif policy in {"manual", "review-before-effect", "prohibited"}:
            action_class = "proposal_preview"
        else:
            action_class = "read_only_observation"
    return {
        "schema_version": APPROVAL_SCHEMA_VERSION,
        "task_id": task.get("id"),
        "action_class": action_class,
        "decision": approval_decision(str(action_class), None),
        "declared": declared,
    }
