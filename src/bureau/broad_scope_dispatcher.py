from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from . import legacy, runtime_refresh
from .approval import require_approval, reviewed_plan_approval
from .lease_contract import (
    BROAD_SCOPE_REVIEW_RECEIPT_VERIFIER_AVAILABLE,
    assess_task_broad_bureau_scope,
)
from .v2 import Dispatcher as _BaseDispatcher
from .v2 import (
    _validate_coordinated_claim_intent,
    coordinated_claim_intent_sha256,
    task_revision_sha256,
)

_MAX_REVIEWED_PLAN_BYTES = 4 * 1024 * 1024
_BROAD_SCOPE_REVIEW_REASON = (
    "broad Bureau scope requires authority-bound reviewed plan evidence"
)
_BROAD_SCOPE_REVIEWED_TASK: ContextVar[str | None] = ContextVar(
    "bureau_broad_scope_reviewed_task",
    default=None,
)
_TERMINAL_TASK_STATES = {"verified", "cancelled", "superseded"}


def _read_stable_regular_file(path: Path) -> tuple[bytes, str]:
    """Read one bounded reviewed-plan artifact through a pinned descriptor."""
    target = path.expanduser().absolute()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags)
    except OSError as exc:
        raise legacy.StateError(
            f"cannot open broad-scope reviewed plan: {exc}"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise legacy.StateError(
                "broad-scope reviewed plan is not a regular file"
            )
        if before.st_size > _MAX_REVIEWED_PLAN_BYTES:
            raise legacy.StateError(
                "broad-scope reviewed plan exceeds the size limit"
            )

        def read_once() -> bytes:
            os.lseek(descriptor, 0, os.SEEK_SET)
            chunks: list[bytes] = []
            remaining = _MAX_REVIEWED_PLAN_BYTES + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            if len(payload) > _MAX_REVIEWED_PLAN_BYTES:
                raise legacy.StateError(
                    "broad-scope reviewed plan exceeds the size limit"
                )
            return payload

        first = read_once()
        second = read_once()
        after = os.fstat(descriptor)
        if first != second:
            raise legacy.StateError(
                "broad-scope reviewed plan changed while it was read"
            )
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if before_identity != after_identity:
            raise legacy.StateError(
                "broad-scope reviewed plan identity changed while it was read"
            )
        try:
            current = os.stat(target, follow_symlinks=False)
        except OSError as exc:
            raise legacy.StateError(
                f"cannot re-read broad-scope reviewed plan identity: {exc}"
            ) from exc
        current_identity = (current.st_dev, current.st_ino, current.st_size)
        descriptor_identity = (after.st_dev, after.st_ino, after.st_size)
        if current_identity != descriptor_identity:
            raise legacy.StateError(
                "broad-scope reviewed plan path changed while it was read"
            )
        return first, str(target)
    finally:
        os.close(descriptor)


def _required_text(value: Any, *, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise legacy.StateError(
            f"broad-scope reviewed plan has invalid {field}"
        )
    return value.strip()


def _reviewed_plan_evidence(task: legacy.Task) -> dict[str, Any]:
    if not BROAD_SCOPE_REVIEW_RECEIPT_VERIFIER_AVAILABLE:
        raise legacy.StateError(
            "broad Bureau scope review authority is unavailable; "
            "self-declared reviewed-plan evidence is forbidden"
        )
    execution = task.execution if isinstance(task.execution, dict) else {}
    exception = execution.get("broad_bureau_scope_exception")
    if not isinstance(exception, dict):
        raise legacy.StateError(
            "broad Bureau scope exception contract is missing"
        )
    raw_path = exception.get("reviewed_plan_path")
    if (
        not isinstance(raw_path, str)
        or not raw_path.strip()
        or len(raw_path) > 4096
    ):
        raise legacy.StateError(
            "broad Bureau scope requires execution."
            "broad_bureau_scope_exception.reviewed_plan_path"
        )

    payload, normalized_path = _read_stable_regular_file(Path(raw_path))
    try:
        plan = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise legacy.StateError(
            f"broad-scope reviewed plan is invalid JSON: {exc}"
        ) from exc
    if (
        not isinstance(plan, dict)
        or plan.get("kind") != "bureau_operator_task_proposal"
    ):
        raise legacy.StateError(
            "broad-scope reviewed plan has an unsupported kind"
        )
    if plan.get("command") != "operator-task-propose":
        raise legacy.StateError(
            "broad-scope reviewed plan has an unsupported command"
        )

    unsigned = {
        key: value
        for key, value in plan.items()
        if key not in {"proposal_sha256", "review"}
    }
    proposal_sha256 = legacy.sha256_json(unsigned)
    if plan.get("proposal_sha256") != proposal_sha256:
        raise legacy.StateError(
            "broad-scope reviewed plan proposal digest mismatch"
        )

    review = plan.get("review")
    if not isinstance(review, dict) or review.get("status") != "reviewed":
        raise legacy.StateError("broad-scope reviewed plan is not reviewed")
    reviewer = _required_text(
        review.get("reviewer"),
        field="reviewer",
        maximum=200,
    )
    reviewed_at = _required_text(
        review.get("reviewed_at"),
        field="reviewed_at",
        maximum=80,
    )
    if review.get("reviewed_proposal_sha256") != proposal_sha256:
        raise legacy.StateError(
            "broad-scope reviewed plan review binding mismatch"
        )

    if plan.get("task_id") != task.id:
        raise legacy.StateError(
            "broad-scope reviewed plan task id mismatch"
        )
    task_json = plan.get("task_json")
    if not isinstance(task_json, dict) or task_json != task.raw:
        raise legacy.StateError(
            "broad-scope reviewed plan task payload mismatch"
        )
    if plan.get("task_json_sha256") != task.sha256:
        raise legacy.StateError(
            "broad-scope reviewed plan task digest mismatch"
        )
    if task_revision_sha256(task_json) != task.sha256:
        raise legacy.StateError(
            "broad-scope reviewed plan current task digest mismatch"
        )
    if plan.get("target_path") != f"registry/tasks/{task.id}.json":
        raise legacy.StateError(
            "broad-scope reviewed plan target path mismatch"
        )
    unresolved = plan.get("unresolved_fields")
    if not isinstance(unresolved, list) or unresolved:
        raise legacy.StateError(
            "broad-scope reviewed plan contains unresolved fields"
        )
    publication = plan.get("publication")
    if (
        not isinstance(publication, dict)
        or publication.get("action_class") != "registry_mutation"
    ):
        raise legacy.StateError(
            "broad-scope reviewed plan publication contract is invalid"
        )
    if publication.get("required_level") != "reviewed_plan":
        raise legacy.StateError(
            "broad-scope reviewed plan required level is invalid"
        )

    approval = require_approval(
        "registry_mutation",
        reviewed_plan_approval(
            reviewer=reviewer,
            reference=proposal_sha256,
            task_id=task.id,
            scope="registry_mutation",
        ),
        expected_reference=proposal_sha256,
        task_id=task.id,
    )
    return {
        "schema_version": 1,
        "kind": "bureau_broad_scope_review_evidence",
        "reviewed_plan_path": normalized_path,
        "plan_file_sha256": hashlib.sha256(payload).hexdigest(),
        "proposal_sha256": proposal_sha256,
        "task_id": task.id,
        "task_sha256": task.sha256,
        "reviewer": reviewer,
        "reviewed_at": reviewed_at,
        "approval": approval,
        "does_not_establish": [
            "ordinary repository mutation approval",
            "Grabowski lease acquisition",
            "claim commitment before live revalidation",
        ],
    }


def _broad_scope_assessment(
    task: legacy.Task,
    resources: dict[str, Any],
) -> dict[str, Any]:
    return assess_task_broad_bureau_scope(task, resources)


class Dispatcher(_BaseDispatcher):
    """Public Dispatcher with evidence-bound broad Bureau scope claims."""

    def reasons(
        self,
        task: legacy.Task,
        capabilities: set[str],
        runs: list[sqlite3.Row],
        reservations: list[legacy.Reservation],
        overlays: dict[str, str],
        *,
        projection_resource: str | None = None,
    ) -> list[str]:
        reasons = super().reasons(
            task,
            capabilities,
            runs,
            reservations,
            overlays,
            projection_resource=projection_resource,
        )
        assessment = _broad_scope_assessment(
            task,
            self.registry.resources,
        )
        if (
            assessment["broad_scope_requested"]
            and task.state not in _TERMINAL_TASK_STATES
            and _BROAD_SCOPE_REVIEWED_TASK.get() != task.id
            and _BROAD_SCOPE_REVIEW_REASON not in reasons
        ):
            reasons.append(_BROAD_SCOPE_REVIEW_REASON)
        return reasons

    def claim_intent(
        self,
        worker_id: str,
        capabilities: tuple[str, ...],
        kind: str = "interactive-agent",
        *,
        task_id: str | None = None,
        resource: str | None = None,
        base_dir: Path | None = None,
        approved: bool = False,
        break_glass: bool = False,
        approval_source: str = "coordinated claim intent",
    ) -> dict[str, Any]:
        evidence: dict[str, Any] | None = None
        token = None
        if task_id is not None:
            task = self.registry.tasks.get(task_id)
            if task is None:
                raise legacy.StateError(
                    f"unknown coordinated claim task {task_id}"
                )
            assessment = _broad_scope_assessment(
                task,
                self.registry.resources,
            )
            if (
                assessment["broad_scope_requested"]
                and not assessment["allowed"]
            ):
                if assessment["exception_status"] == "review-authority-unavailable":
                    raise legacy.StateError(
                        "broad Bureau scope review authority is unavailable; "
                        "self-declared reviewed-plan evidence is forbidden"
                    )
                raise legacy.StateError(
                    "broad Bureau scope exception contract is invalid"
                )
            if assessment["broad_scope_requested"]:
                evidence = _reviewed_plan_evidence(task)
                token = _BROAD_SCOPE_REVIEWED_TASK.set(task.id)
        try:
            result = super().claim_intent(
                worker_id,
                capabilities,
                kind,
                task_id=task_id,
                resource=resource,
                base_dir=base_dir,
                approved=approved,
                break_glass=break_glass,
                approval_source=approval_source,
            )
        finally:
            if token is not None:
                _BROAD_SCOPE_REVIEWED_TASK.reset(token)
        if evidence is None or result.get("status") != "claim-intent":
            return result
        intent = result["intent"]
        operator_approval = intent.get("operator_approval")
        if not isinstance(operator_approval, dict):
            raise legacy.StateError(
                "coordinated claim operator approval is invalid"
            )
        operator_approval["broad_scope_review"] = evidence
        intent["intent_sha256"] = coordinated_claim_intent_sha256(intent)
        ready_supply = result["ready_supply"]
        ready_supply["broad_scope_review_bound"] = True
        ready_supply["broad_scope_review"] = {
            "proposal_sha256": evidence["proposal_sha256"],
            "task_sha256": evidence["task_sha256"],
            "reviewer": evidence["reviewer"],
        }
        return result

    def commit_claim_intent(
        self,
        intent: dict[str, Any],
        lease_binding: dict[str, Any] | None,
        *,
        resource_db: Path = runtime_refresh.DEFAULT_GRABOWSKI_RESOURCE_DB,
    ) -> dict[str, Any]:
        _validate_coordinated_claim_intent(intent)
        with self.store.connect() as connection:
            existing = connection.execute(
                "SELECT 1 FROM runs WHERE run_id=?",
                (intent["run_id"],),
            ).fetchone()
        if existing is not None:
            return super().commit_claim_intent(
                intent,
                lease_binding,
                resource_db=resource_db,
            )

        task = self.registry.tasks.get(intent["task_id"])
        if task is None:
            raise legacy.StateError(
                "coordinated claim task no longer exists"
            )
        assessment = _broad_scope_assessment(
            task,
            self.registry.resources,
        )
        operator_approval = intent.get("operator_approval")
        supplied = (
            operator_approval.get("broad_scope_review")
            if isinstance(operator_approval, dict)
            else None
        )
        token = None
        if (
            assessment["broad_scope_requested"]
            and not assessment["allowed"]
        ):
            if assessment["exception_status"] == "review-authority-unavailable":
                raise legacy.StateError(
                    "broad Bureau scope review authority is unavailable; "
                    "self-declared reviewed-plan evidence is forbidden"
                )
            raise legacy.StateError(
                "broad Bureau scope exception contract is invalid"
            )
        if assessment["broad_scope_requested"]:
            expected = _reviewed_plan_evidence(task)
            if supplied != expected:
                raise legacy.StateError(
                    "broad Bureau scope requires matching digest-bound "
                    "reviewed plan evidence"
                )
            token = _BROAD_SCOPE_REVIEWED_TASK.set(task.id)
        elif supplied is not None:
            raise legacy.StateError(
                "coordinated claim has unexpected broad-scope review evidence"
            )
        try:
            return super().commit_claim_intent(
                intent,
                lease_binding,
                resource_db=resource_db,
            )
        finally:
            if token is not None:
                _BROAD_SCOPE_REVIEWED_TASK.reset(token)
