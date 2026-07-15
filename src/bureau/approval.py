"""Explicit approval semantics for Bureau effect boundaries.

The module is deliberately small and deterministic. It does not grant authority;
it classifies effectful actions and verifies that a caller supplied an explicit,
source-bound approval record before the action is allowed to continue.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from . import legacy

APPROVAL_SCHEMA_VERSION = 1

READ_ONLY_ACTIONS = frozenset({"read_only_observation", "dry_run", "proposal_preview"})

APPROVAL_RULES: dict[str, dict[str, Any]] = {
    "repository_mutation": {
        "required_level": "operator",
        "allowed_levels": frozenset({"operator", "break_glass"}),
        "reason": (
            "repository writes, branch operations, commits, pushes or merges "
            "change source state"
        ),
    },
    "source_import": {
        "required_level": "reviewed_receipt",
        "allowed_levels": frozenset({"reviewed_receipt", "break_glass"}),
        "reason": "source imports convert external evidence into Bureau registry material",
    },
    "agent_dispatch": {
        "required_level": "operator",
        "allowed_levels": frozenset({"operator", "break_glass"}),
        "reason": (
            "agent dispatch creates external work that may mutate repositories "
            "or runtime state"
        ),
    },
    "task_creation_from_external_evidence": {
        "required_level": "operator",
        "allowed_levels": frozenset({"operator", "break_glass"}),
        "reason": (
            "external evidence may propose tasks, but Bureau task creation "
            "requires explicit approval"
        ),
    },
    "queue_mutation": {
        "required_level": "reviewed_plan",
        "allowed_levels": frozenset({"reviewed_plan", "break_glass"}),
        "reason": "queue order controls what agents may pick next",
    },
    "registry_mutation": {
        "required_level": "reviewed_plan",
        "allowed_levels": frozenset({"reviewed_plan", "break_glass"}),
        "reason": (
            "reviewed Registry task-file rewrites require a digest-bound reviewed plan"
        ),
    },
    "worktree_cleanup": {
        "required_level": "reviewed_plan",
        "allowed_levels": frozenset({"reviewed_plan", "break_glass"}),
        "reason": (
            "worktree removal changes repository administration state and may "
            "discard the last checked-out copy of a commit"
        ),
    },
    "state_root_migration": {
        "required_level": "reviewed_plan",
        "allowed_levels": frozenset({"reviewed_plan", "break_glass"}),
        "reason": (
            "state-root migration changes active operational evidence paths and "
            "therefore requires a digest-bound reviewed plan"
        ),
    },
    "runtime_mutation": {
        "required_level": "break_glass",
        "allowed_levels": frozenset({"break_glass"}),
        "reason": "runtime mutation may restart, deploy or alter live services",
    },
}


def _scope_tuple(scope: str | Iterable[str] | None) -> tuple[str, ...]:
    if scope is None:
        return ()
    if isinstance(scope, str):
        return (scope,)
    return tuple(item for item in scope if isinstance(item, str) and item)


@dataclass(frozen=True)
class ApprovalEvidence:
    """One explicit approval input for an effectful Bureau action."""

    source: str
    level: str
    approved: bool
    reviewer: str | None = None
    reference: str | None = None
    task_id: str | None = None
    scope: tuple[str, ...] = ()
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
        if self.task_id:
            result["task_id"] = self.task_id
        scope = _scope_tuple(self.scope)
        if scope:
            result["scope"] = list(scope)
        if self.note:
            result["note"] = self.note
        return result


def explicit_operator_approval(
    *,
    source: str,
    approved: bool,
    reviewer: str | None = None,
    reference: str | None = None,
    task_id: str | None = None,
    scope: str | Iterable[str] | None = None,
    note: str | None = None,
) -> ApprovalEvidence:
    """Return a normalized operator approval record from a visible command flag."""
    return ApprovalEvidence(
        source=source,
        level="operator",
        approved=bool(approved),
        reviewer=reviewer,
        reference=reference,
        task_id=task_id,
        scope=_scope_tuple(scope),
        note=note,
    )


def reviewed_plan_approval(
    *,
    reviewer: str,
    reference: str,
    approved: bool = True,
    task_id: str | None = None,
    scope: str | Iterable[str] | None = None,
) -> ApprovalEvidence:
    return ApprovalEvidence(
        source="reviewed_plan",
        level="reviewed_plan",
        approved=approved,
        reviewer=reviewer,
        reference=reference,
        task_id=task_id,
        scope=_scope_tuple(scope),
    )


def reviewed_receipt_approval(
    *,
    reviewer: str,
    reference: str,
    approved: bool = True,
    task_id: str | None = None,
    scope: str | Iterable[str] | None = None,
) -> ApprovalEvidence:
    return ApprovalEvidence(
        source="reviewed_receipt",
        level="reviewed_receipt",
        approved=approved,
        reviewer=reviewer,
        reference=reference,
        task_id=task_id,
        scope=_scope_tuple(scope),
    )


def _scope_covers(approval: ApprovalEvidence, action_classes: set[str]) -> bool:
    scope = set(_scope_tuple(approval.scope))
    return not scope or "task" in scope or action_classes <= scope


def _required_level(action_classes: list[str]) -> str:
    if not action_classes:
        return "none"
    levels = {
        str(APPROVAL_RULES[action]["required_level"])
        for action in action_classes
        if action in APPROVAL_RULES
    }
    if not levels:
        return "unknown"
    if levels == {"break_glass"}:
        return "break_glass"
    if len(action_classes) == 1:
        return next(iter(levels))
    if "break_glass" in levels:
        return "break_glass"
    return "multi_effect"


def approval_decision(
    action_class: str,
    approval: ApprovalEvidence | None,
    *,
    expected_reference: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Evaluate approval for one action class without mutating anything."""
    return approval_decision_for_effects(
        [action_class],
        approval,
        expected_reference=expected_reference,
        task_id=task_id,
    )


def approval_decision_for_effects(
    action_classes: Iterable[str],
    approval: ApprovalEvidence | None,
    *,
    expected_reference: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Evaluate one approval against one or more action classes."""
    actions = list(dict.fromkeys(action_classes))
    evidence = approval.as_dict() if approval else None
    if not actions:
        return {
            "schema_version": APPROVAL_SCHEMA_VERSION,
            "action_class": "none",
            "action_classes": [],
            "required": False,
            "required_level": "none",
            "allowed": True,
            "reason": "no effectful action",
            "expected_reference": expected_reference,
            "task_id": task_id,
            "evidence": evidence,
        }
    if all(action in READ_ONLY_ACTIONS for action in actions):
        return {
            "schema_version": APPROVAL_SCHEMA_VERSION,
            "action_class": actions[0],
            "action_classes": actions,
            "required": False,
            "required_level": "none",
            "allowed": True,
            "reason": "read-only or dry-run action",
            "expected_reference": expected_reference,
            "task_id": task_id,
            "evidence": evidence,
        }

    effectful_actions = [
        action for action in actions if action not in READ_ONLY_ACTIONS
    ]
    rules = {action: APPROVAL_RULES.get(action) for action in effectful_actions}
    unknown = [action for action, rule in rules.items() if rule is None]
    if unknown:
        return {
            "schema_version": APPROVAL_SCHEMA_VERSION,
            "action_class": actions[0],
            "action_classes": actions,
            "required": True,
            "required_level": "unknown",
            "allowed": False,
            "reason": f"unknown action class fails closed: {', '.join(unknown)}",
            "expected_reference": expected_reference,
            "task_id": task_id,
            "evidence": evidence,
        }

    required_levels = {str(rule["required_level"]) for rule in rules.values() if rule}
    required_level = _required_level(effectful_actions)
    allowed_levels = set.intersection(
        *(
            set(rule.get("allowed_levels", {rule["required_level"]}))
            for rule in rules.values()
            if rule
        )
    )
    action_set = set(effectful_actions)
    level_ok = bool(approval is not None and approval.level in allowed_levels)
    reference_ok = True
    reference_reason = ""
    task_ok = True
    task_reason = ""
    scope_ok = True
    if approval is not None:
        if expected_reference is not None:
            reference_ok = approval.reference == expected_reference
            actual = approval.reference or "<missing>"
            reference_reason = (
                f"approval reference {actual} does not match expected {expected_reference}"
            )
        if task_id is not None:
            task_ok = approval.task_id == task_id
            actual = approval.task_id or "<missing>"
            task_reason = f"approval task_id {actual} does not match expected {task_id}"
        scope_ok = _scope_covers(approval, action_set)

    allowed = bool(
        approval is not None
        and approval.approved
        and level_ok
        and reference_ok
        and task_ok
        and scope_ok
    )
    reason = "approved"
    if not allowed:
        reason = "; ".join(
            reason
            for reason in (
                "explicit approval missing" if approval is None else "",
                "approval record is not approved"
                if approval is not None and not approval.approved
                else "",
                (
                    f"approval level {approval.level} is not accepted for required "
                    f"{', '.join(sorted(required_levels))}"
                )
                if approval is not None and not level_ok
                else "",
                reference_reason if approval is not None and not reference_ok else "",
                task_reason if approval is not None and not task_ok else "",
                "approval scope does not cover all action classes"
                if approval is not None and not scope_ok
                else "",
            )
            if reason
        )
    result = {
        "schema_version": APPROVAL_SCHEMA_VERSION,
        "action_class": actions[0],
        "action_classes": actions,
        "required": True,
        "required_level": required_level,
        "allowed": allowed,
        "reason": reason,
        "expected_reference": expected_reference,
        "expected_task_id": task_id,
        "evidence": evidence,
    }
    return result


def require_approval(
    action_class: str,
    approval: ApprovalEvidence | None,
    *,
    expected_reference: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Return an approval decision or raise StateError before any effect."""
    decision = approval_decision(
        action_class,
        approval,
        expected_reference=expected_reference,
        task_id=task_id,
    )
    if not decision["allowed"]:
        raise legacy.StateError(
            f"approval required for {action_class}: {decision['reason']}"
        )
    return decision


def require_approval_for_effects(
    action_classes: Iterable[str],
    approval: ApprovalEvidence | None,
    *,
    expected_reference: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Return a multi-effect approval decision or raise before any effect."""
    actions = list(action_classes)
    decision = approval_decision_for_effects(
        actions,
        approval,
        expected_reference=expected_reference,
        task_id=task_id,
    )
    if not decision["allowed"]:
        raise legacy.StateError(
            f"approval required for {', '.join(actions)}: {decision['reason']}"
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
