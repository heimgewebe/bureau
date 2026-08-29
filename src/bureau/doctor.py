"""Bounded read-only Bureau Control Plane doctor.

The v3 doctor is a consumer, not an authority.  It composes the existing
status projection with independently verified backup/restore observations and
renders inert repair proposals.  No function in this module applies a repair.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import legacy
from .github_observer import observe_pull_requests
from .state_backup import (
    DEFAULT_BACKUP_ROOT,
    DEFAULT_RESTORE_RECEIPT_ROOT,
    StateBackupError,
    latest_bundle,
    verify_backup,
)
from .status_projection import (
    DEFAULT_GITHUB_MAX_AGE_SECONDS,
    control_plane_summary,
    status_projection,
)
from .v2 import TERMINAL_TASK_STATES

DOCTOR_SCHEMA_VERSION = 1
DOCTOR_KIND = "bureau_control_plane_doctor"
DASHBOARD_KIND = "bureau_control_plane_dashboard"
TRUTH_DRIFT_KIND = "bureau_truth_drift_projection"
TRUTH_DRIFT_FINDING_KIND = "bureau_truth_drift_finding"
CLOSEOUT_PLAN_KIND = "bureau_truth_drift_closeout_plan"
REVIEWED_CLOSEOUT_PLAN_KIND = "bureau_truth_drift_reviewed_closeout_plan"
TRUTH_DRIFT_CODES = (
    "merged-implementation-open-task",
    "verified-task-still-queued",
    "closed-pr-without-decision",
    "runtime-proof-without-closeout",
    "queue-state-lane-mismatch",
)

DOCTOR_DOES_NOT_ESTABLISH = (
    "repair_authority",
    "repair_effect",
    "claim_authority",
    "dispatch_authority",
    "task_completion",
    "merge_readiness",
    "deployment_authority",
    "future_runtime_health",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_seconds(value: Any, now: datetime) -> int | None:
    parsed = _parse_time(value)
    if parsed is None:
        return None
    return max(0, int((now - parsed).total_seconds()))


def observe_backup(
    backup_root: Path = DEFAULT_BACKUP_ROOT,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read and verify the newest coherent Bureau backup without creating one."""
    current = now or _utc_now()
    try:
        bundle = latest_bundle(backup_root)
        verification = verify_backup(bundle)
        manifest_path = bundle / "manifest.json"
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        created_at = raw.get("created_at") if isinstance(raw, dict) else None
        return {
            "observed": True,
            "status": "verified",
            "source": "state-backup-manifest",
            "freshness": {
                "observed_at": _iso(current),
                "created_at": created_at,
                "age_seconds": _age_seconds(created_at, current),
            },
            "binding": {
                "bundle_id": verification.get("bundle_id"),
                "manifest_sha256": verification.get("manifest_sha256"),
                "authoritative_root_sha256": verification.get("authoritative_root_sha256"),
            },
            "authority": "verified-backup-bundle",
            "bounds": "latest verified bundle only",
        }
    except (StateBackupError, OSError, json.JSONDecodeError) as exc:
        return {
            "observed": True,
            "status": "unavailable",
            "source": "state-backup-manifest",
            "freshness": {"observed_at": _iso(current)},
            "error": str(exc),
            "authority": "verified-backup-bundle",
            "bounds": "latest verified bundle only",
        }


def observe_restore(
    receipt_root: Path = DEFAULT_RESTORE_RECEIPT_ROOT,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read the latest hash-bound restore-test receipt without running a restore."""
    current = now or _utc_now()
    path = receipt_root.expanduser().resolve() / "latest.json"
    if path.is_symlink() or not path.is_file():
        return {
            "observed": True,
            "status": "missing",
            "source": "restore-test-receipt",
            "freshness": {"observed_at": _iso(current)},
            "authority": "hash-bound-restore-test-receipt",
            "bounds": "latest receipt only",
        }
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "observed": True,
            "status": "invalid",
            "source": "restore-test-receipt",
            "freshness": {"observed_at": _iso(current)},
            "error": str(exc),
            "authority": "hash-bound-restore-test-receipt",
            "bounds": "latest receipt only",
        }
    if not isinstance(raw, dict):
        return {
            "observed": True,
            "status": "invalid",
            "source": "restore-test-receipt",
            "freshness": {"observed_at": _iso(current)},
            "error": "receipt is not an object",
            "authority": "hash-bound-restore-test-receipt",
            "bounds": "latest receipt only",
        }
    expected = raw.get("receipt_sha256")
    unsigned = {key: value for key, value in raw.items() if key != "receipt_sha256"}
    valid_digest = isinstance(expected, str) and expected == legacy.sha256_json(unsigned)
    tested_at = raw.get("tested_at")
    valid_contract = (
        raw.get("kind") == "bureau_state_restore_test_receipt"
        and raw.get("status") == "verified"
        and valid_digest
    )
    return {
        "observed": True,
        "status": "verified" if valid_contract else "invalid",
        "source": "restore-test-receipt",
        "freshness": {
            "observed_at": _iso(current),
            "tested_at": tested_at,
            "age_seconds": _age_seconds(tested_at, current),
        },
        "binding": {
            "receipt_sha256": expected if valid_digest else None,
            "manifest_sha256": raw.get("manifest_sha256") if valid_contract else None,
            "authoritative_root_sha256": raw.get("authoritative_root_sha256")
            if valid_contract
            else None,
        },
        "authority": "hash-bound-restore-test-receipt",
        "bounds": "latest receipt only",
    }


def _proposal(
    *,
    code: str,
    finding: str,
    impact: str,
    proposed_action: str,
    required_authority: str,
    apply_contract: str,
    readback: str,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "code": code,
        "finding": finding,
        "impact": impact,
        "proposed_action": proposed_action,
        "required_authority": required_authority,
        "apply_contract": apply_contract,
        "readback": readback,
        "effect": "none",
    }
    value["dry_run_sha256"] = legacy.sha256_json(value)
    return value


def _truth_drift_finding(
    *,
    code: str,
    task: Mapping[str, Any],
    evidence: Mapping[str, Any],
    finding: str,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": 1,
        "kind": TRUTH_DRIFT_FINDING_KIND,
        "code": code,
        "task_id": task.get("task_id"),
        "effective_state": task.get("effective_state"),
        "queue_lane": task.get("queue_lane"),
        "compatibility_queue_lane": task.get("compatibility_queue_lane"),
        "finding": finding,
        "evidence": dict(evidence),
        "acceptance_recheck_required": True,
        "effect": "none",
        "does_not_establish": [
            "task_completion",
            "queue_mutation_authority",
            "merge_authority",
            "runtime_correctness",
        ],
    }
    value["finding_sha256"] = legacy.sha256_json(value)
    return value


def _latest_github_evidence(task: Mapping[str, Any]) -> Mapping[str, Any] | None:
    candidates: list[Mapping[str, Any]] = []
    current = task.get("github")
    if isinstance(current, Mapping):
        candidates.append(current)
    candidates.extend(
        item for item in task.get("github_history", []) if isinstance(item, Mapping)
    )
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            str(item.get("updated_at") or ""),
            int(item.get("number") or 0),
        ),
    )


def _latest_github_lifecycle_evidence(
    task: Mapping[str, Any], state: str
) -> Mapping[str, Any] | None:
    candidates: list[Mapping[str, Any]] = []
    current = task.get("github")
    if isinstance(current, Mapping):
        candidates.append(current)
    candidates.extend(
        item for item in task.get("github_history", []) if isinstance(item, Mapping)
    )
    matching = [
        item for item in candidates if str(item.get("state") or "").upper() == state
    ]
    if not matching:
        return None
    return max(
        matching,
        key=lambda item: (
            str(item.get("updated_at") or ""),
            int(item.get("number") or 0),
        ),
    )


def truth_drift_projection(projection: Mapping[str, Any]) -> dict[str, Any]:
    """Classify registry/implementation drift without granting repair authority."""
    findings: list[dict[str, Any]] = []
    for task in projection.get("tasks", []):
        if not isinstance(task, Mapping):
            continue
        state = str(task.get("effective_state") or "")
        terminal = state in TERMINAL_TASK_STATES
        current_github = task.get("github")
        current_github = current_github if isinstance(current_github, Mapping) else {}
        current_github_state = str(current_github.get("state") or "").upper()
        current_open_present = current_github_state == "OPEN" or (
            str(current_github.get("binding") or "") == "ambiguous"
            and str(current_github.get("ambiguous_reason") or "")
            == "multiple-open-prs-for-task"
        )
        latest_github = _latest_github_evidence(task)
        merged_github = _latest_github_lifecycle_evidence(task, "MERGED")
        closed_github = _latest_github_lifecycle_evidence(task, "CLOSED")
        queue_lane = task.get("queue_lane")
        compatibility_lane = task.get("compatibility_queue_lane")
        receipts = [item for item in task.get("receipts", []) if isinstance(item, Mapping)]

        if merged_github is not None and not terminal:
            findings.append(
                _truth_drift_finding(
                    code="merged-implementation-open-task",
                    task=task,
                    finding=(
                        "GitHub reports a merged implementation while Bureau task truth "
                        "remains nonterminal."
                    ),
                    evidence={
                        "github_state": "MERGED",
                        "pull_request": merged_github.get("number"),
                        "head_sha": merged_github.get("head_sha"),
                        "merge_is_not_completion": True,
                    },
                )
            )

        if state == "verified" and compatibility_lane in {"now", "next", "later"}:
            findings.append(
                _truth_drift_finding(
                    code="verified-task-still-queued",
                    task=task,
                    finding=(
                        "The authoritative task is verified but the compatibility queue "
                        "still contains it."
                    ),
                    evidence={
                        "authoritative_state": state,
                        "compatibility_queue_lane": compatibility_lane,
                        "queue_authoritative": False,
                    },
                )
            )

        if (
            closed_github is not None
            and not terminal
            and not current_open_present
            and isinstance(latest_github, Mapping)
            and str(latest_github.get("state") or "").upper() == "CLOSED"
        ):
            findings.append(
                _truth_drift_finding(
                    code="closed-pr-without-decision",
                    task=task,
                    finding=(
                        "A bound pull request is closed while Bureau has no terminal "
                        "lifecycle decision for the task."
                    ),
                    evidence={
                        "github_state": "CLOSED",
                        "pull_request": closed_github.get("number"),
                        "review_decision": closed_github.get("review_decision"),
                    },
                )
            )

        if receipts and not terminal:
            findings.append(
                _truth_drift_finding(
                    code="runtime-proof-without-closeout",
                    task=task,
                    finding=(
                        "Bound run evidence exists while Bureau task truth remains "
                        "nonterminal; the receipt is a recheck trigger, not proof of "
                        "runtime correctness or task completion."
                    ),
                    evidence={
                        "receipt_sha256s": sorted(
                            str(item.get("receipt_sha256"))
                            for item in receipts
                            if item.get("receipt_sha256")
                        ),
                        "receipt_scope": "run-evidence-only",
                        "runtime_readback_required": True,
                    },
                )
            )

        if (
            not terminal
            and compatibility_lane in {"now", "next", "later"}
            and queue_lane != compatibility_lane
        ):
            findings.append(
                _truth_drift_finding(
                    code="queue-state-lane-mismatch",
                    task=task,
                    finding=(
                        "The compatibility queue lane differs from authoritative task "
                        "priority/state projection."
                    ),
                    evidence={
                        "authoritative_queue_lane": queue_lane,
                        "compatibility_queue_lane": compatibility_lane,
                        "queue_authoritative": False,
                    },
                )
            )

    findings.sort(key=lambda item: (str(item.get("task_id")), str(item.get("code"))))
    by_code = {code: 0 for code in TRUTH_DRIFT_CODES}
    for finding in findings:
        code = str(finding.get("code"))
        if code in by_code:
            by_code[code] += 1
    payload: dict[str, Any] = {
        "schema_version": 1,
        "kind": TRUTH_DRIFT_KIND,
        "read_only": True,
        "effect": "none",
        "finding_count": len(findings),
        "counts": by_code,
        "findings": findings,
        "does_not_establish": [
            "task_completion",
            "repair_authority",
            "queue_mutation_authority",
            "merge_authority",
            "deployment_authority",
        ],
    }
    payload["projection_sha256"] = legacy.sha256_json(payload)
    return payload


def control_plane_with_truth_drift(
    control_plane: Mapping[str, Any], truth_drift: Mapping[str, Any]
) -> dict[str, Any]:
    """Attach bounded truth-model health without granting repair authority."""
    finding_count = truth_drift.get("finding_count")
    count = finding_count if isinstance(finding_count, int) and finding_count >= 0 else 0
    value = dict(control_plane)
    metrics_value = control_plane.get("metrics")
    metrics = dict(metrics_value) if isinstance(metrics_value, Mapping) else {}
    metrics["truth_drift"] = {
        "value": count,
        "definition": (
            "deterministic registry/implementation lifecycle drift findings that require "
            "review and fresh acceptance rechecks"
        ),
        "source": "bureau truth-drift projection",
    }
    organs_value = control_plane.get("organs")
    organs = dict(organs_value) if isinstance(organs_value, Mapping) else {}
    organs["truth_drift"] = {
        "source": "bureau-truth-drift-projection",
        "freshness": {"observed_at": control_plane.get("generated_at")},
        "bounds": "five deterministic T003 drift classes; no automatic repair",
        "authority": "derived diagnostic only; reviewed typed operations remain authoritative",
        "status": "attention" if count else "ok",
    }
    value["metrics"] = metrics
    value["organs"] = organs
    value["healthy"] = bool(control_plane.get("healthy")) and count == 0
    return value


def _closeout_rechecks(code: str) -> list[str]:
    rechecks = [
        "current-task-revision",
        "current-acceptance-contract",
        "current-state-store-task-state",
    ]
    if code in {"merged-implementation-open-task", "closed-pr-without-decision"}:
        rechecks.extend(["fresh-github-pr-state", "current-tests-when-applicable"])
    if code == "runtime-proof-without-closeout":
        rechecks.extend(
            ["receipt-integrity", "fresh-runtime-readback", "current-tests-when-applicable"]
        )
    if code in {"verified-task-still-queued", "queue-state-lane-mismatch"}:
        rechecks.append("recomputed-authoritative-queue-projection")
    return rechecks


def _closeout_action(code: str) -> str:
    return {
        "merged-implementation-open-task": "recheck-acceptance-before-closeout",
        "verified-task-still-queued": "reconcile-compatibility-queue",
        "closed-pr-without-decision": "record-reviewed-lifecycle-decision",
        "runtime-proof-without-closeout": "recheck-runtime-and-acceptance-before-closeout",
        "queue-state-lane-mismatch": "reconcile-compatibility-queue",
    }[code]


def closeout_plan_projection(drift: Mapping[str, Any]) -> dict[str, Any]:
    """Render review-required, hash-bound plans for deterministic drift findings."""
    plans: list[dict[str, Any]] = []
    for finding in drift.get("findings", []):
        if not isinstance(finding, Mapping):
            continue
        code = str(finding.get("code"))
        if code not in TRUTH_DRIFT_CODES:
            continue
        plan: dict[str, Any] = {
            "schema_version": 1,
            "kind": CLOSEOUT_PLAN_KIND,
            "finding_code": code,
            "finding_sha256": finding.get("finding_sha256"),
            "task_id": finding.get("task_id"),
            "proposed_action": _closeout_action(code),
            "required_rechecks": _closeout_rechecks(code),
            "review_contract": {
                "required": True,
                "status": "pending",
                "binding": "exact-plan-sha256",
            },
            "apply_contract": (
                "after review and all fresh rechecks, select the existing typed "
                "lifecycle/queue reconcile operation; this plan itself grants no authority"
            ),
            "rollback_or_refusal": {
                "revision_drift": "refuse",
                "failed_acceptance_or_test": "refuse",
                "unknown_external_truth": "refuse",
                "ambiguous_effect": "read back authoritative state; never blind-retry",
            },
            "effect": "none",
            "does_not_establish": [
                "review_completed",
                "task_completion",
                "queue_mutation_authority",
                "claim_authority",
                "dispatch_authority",
                "merge_authority",
                "deployment_authority",
            ],
        }
        plan["plan_sha256"] = legacy.sha256_json(plan)
        plans.append(plan)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "kind": "bureau_truth_drift_closeout_plan_set",
        "read_only": True,
        "effect": "none",
        "plan_count": len(plans),
        "plans": plans,
        "review_required": True,
    }
    payload["plan_set_sha256"] = legacy.sha256_json(payload)
    return payload


def review_closeout_plan(
    plan: Mapping[str, Any], *, expected_plan_sha256: str, reviewer: str
) -> dict[str, Any]:
    """Review one exact inert plan without applying or authorizing its effects."""
    if plan.get("kind") != CLOSEOUT_PLAN_KIND:
        raise ValueError("closeout plan kind is invalid")
    if not isinstance(expected_plan_sha256, str) or len(expected_plan_sha256) != 64:
        raise ValueError("expected plan SHA-256 is invalid")
    unsigned = {key: value for key, value in plan.items() if key != "plan_sha256"}
    observed = legacy.sha256_json(unsigned)
    if plan.get("plan_sha256") != observed or observed != expected_plan_sha256:
        raise ValueError("closeout plan SHA-256 mismatch")
    reviewer_value = reviewer.strip() if isinstance(reviewer, str) else ""
    if not reviewer_value or len(reviewer_value) > 200:
        raise ValueError("reviewer must contain 1..200 characters")
    value: dict[str, Any] = {
        "schema_version": 1,
        "kind": REVIEWED_CLOSEOUT_PLAN_KIND,
        "plan": dict(plan),
        "review": {
            "reviewer": reviewer_value,
            "decision": "approved-for-fresh-recheck-only",
            "plan_sha256": expected_plan_sha256,
        },
        "effect": "none",
        "does_not_establish": [
            "task_completion",
            "queue_mutation_authority",
            "claim_authority",
            "dispatch_authority",
            "merge_authority",
            "deployment_authority",
            "fresh_recheck_success",
        ],
    }
    value["review_sha256"] = legacy.sha256_json(value)
    return value


def repair_plan(control_plane: Mapping[str, Any]) -> dict[str, Any]:
    """Render inert, individually hash-bound repair suggestions."""
    metrics = control_plane.get("metrics")
    metrics = metrics if isinstance(metrics, Mapping) else {}
    organs = control_plane.get("organs")
    organs = organs if isinstance(organs, Mapping) else {}
    proposals: list[dict[str, Any]] = []

    state = organs.get("state_store")
    if isinstance(state, Mapping) and state.get("status") != "ok":
        proposals.append(
            _proposal(
                code="state-store-unhealthy",
                finding="StateStore is unavailable or reports integrity trouble.",
                impact="Operational task, run, acceptance and closeout truth is not reliable.",
                proposed_action="diagnose-or-restore-state-store",
                required_authority="state-recovery",
                apply_contract="explicit state restore/recovery operation",
                readback="bureau check plus authoritative projection replay",
            )
        )

    github = organs.get("github_bridge")
    if isinstance(github, Mapping) and github.get("status") != "fresh":
        proposals.append(
            _proposal(
                code="github-observation-refresh",
                finding="GitHub bridge is unobserved, unhealthy or stale.",
                impact=(
                    "PR/CI facts remain unknown; local operation may continue "
                    "where GitHub is non-authoritative."
                ),
                proposed_action="refresh-github-observation",
                required_authority="read-only-external-observation",
                apply_contract="bureau github-observe",
                readback="doctor/status projection bound to the new observation",
            )
        )

    backup = organs.get("backup")
    if isinstance(backup, Mapping) and backup.get("status") != "verified":
        proposals.append(
            _proposal(
                code="backup-unavailable",
                finding="No currently verified backup bundle was observed.",
                impact="Rollback confidence is reduced until a coherent backup exists.",
                proposed_action="create-state-backup",
                required_authority="backup-artifact-write",
                apply_contract="bureau state-backup backup",
                readback="bureau state-backup verify on the emitted bundle",
            )
        )

    restore = organs.get("restore")
    if isinstance(restore, Mapping) and restore.get("status") != "verified":
        proposals.append(
            _proposal(
                code="restore-proof-unavailable",
                finding="No valid latest restore-test receipt was observed.",
                impact="Backup existence is not equivalent to proven restoreability.",
                proposed_action="run-restore-test",
                required_authority="restore-test-artifact-write",
                apply_contract="bureau state-restore-test",
                readback="hash-bound latest restore-test receipt",
            )
        )

    drift = metrics.get("drift")
    drift_value = drift.get("value") if isinstance(drift, Mapping) else None
    if isinstance(drift_value, int) and drift_value > 0:
        proposals.append(
            _proposal(
                code="projection-drift-present",
                finding=(
                    f"{drift_value} drift/blocker signal(s) are visible in the "
                    "bounded projection."
                ),
                impact="Automation must not infer convergence from a partially inconsistent view.",
                proposed_action="inspect-drift-and-render-specific-plan",
                required_authority="read-only-diagnosis",
                apply_contract="separate typed reconcile/apply contract selected from the finding",
                readback="fresh doctor projection after the selected operation",
            )
        )

    closeout = metrics.get("closeout_pending")
    closeout_value = closeout.get("value") if isinstance(closeout, Mapping) else None
    if isinstance(closeout_value, int) and closeout_value > 0:
        proposals.append(
            _proposal(
                code="closeout-pending",
                finding=(
                    f"{closeout_value} nonterminal task(s) have run receipt(s) and "
                    "require authoritative closeout review."
                ),
                impact="Technical execution may be finished while task truth remains nonterminal.",
                proposed_action="reconcile-closeout",
                required_authority="coordination-state-mutation",
                apply_contract="typed acceptance/closure reconcile",
                readback="terminal StateStore task revision plus bound receipt",
            )
        )

    payload = {
        "schema_version": 1,
        "kind": "bureau_control_plane_repair_plan",
        "read_only": True,
        "effect": "none",
        "proposal_count": len(proposals),
        "proposals": proposals,
        "apply_contract_required": True,
    }
    payload["repair_plan_sha256"] = legacy.sha256_json(payload)
    return payload


def dashboard_projection(
    control_plane: Mapping[str, Any],
    plan: Mapping[str, Any],
    *,
    truth_drift: Mapping[str, Any] | None = None,
    closeout_plans: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the deliberately small dashboard consumer surface."""
    attention = [
        {
            "code": item.get("code"),
            "impact": item.get("impact"),
            "dry_run_sha256": item.get("dry_run_sha256"),
        }
        for item in plan.get("proposals", [])
        if isinstance(item, Mapping)
    ]
    drift = truth_drift if isinstance(truth_drift, Mapping) else {}
    drift_count = drift.get("finding_count")
    if isinstance(drift_count, int) and drift_count > 0:
        plans = closeout_plans if isinstance(closeout_plans, Mapping) else {}
        evidence_sha256 = plans.get("plan_set_sha256") or drift.get("projection_sha256")
        attention.append(
            {
                "code": "truth-drift-present",
                "impact": (
                    f"{drift_count} truth-model drift finding(s) require reviewed "
                    "closeout/recheck; no detector output grants apply authority."
                ),
                "dry_run_sha256": evidence_sha256,
            }
        )
    return {
        "schema_version": 1,
        "kind": DASHBOARD_KIND,
        "generated_at": control_plane.get("generated_at"),
        "healthy": control_plane.get("healthy"),
        "metrics": control_plane.get("metrics", {}),
        "organs": control_plane.get("organs", {}),
        "attention": attention,
        "source": "bureau-control-plane-doctor",
        "read_only": True,
        "does_not_establish": list(DOCTOR_DOES_NOT_ESTABLISH),
    }


def doctor_projection(
    root: Path,
    *,
    registry: legacy.Registry | None = None,
    state_root: Path | None = None,
    github: dict[str, Any] | None = None,
    github_max_age_seconds: float = DEFAULT_GITHUB_MAX_AGE_SECONDS,
    backup_root: Path = DEFAULT_BACKUP_ROOT,
    restore_receipt_root: Path = DEFAULT_RESTORE_RECEIPT_ROOT,
    now: str | None = None,
) -> dict[str, Any]:
    generated_at = now or _iso(_utc_now())
    base = status_projection(
        root,
        registry=registry,
        state_root=state_root,
        github=github,
        github_max_age_seconds=github_max_age_seconds,
        now=generated_at,
    )
    parsed_now = _parse_time(generated_at) or _utc_now()
    backup = observe_backup(backup_root, now=parsed_now)
    restore = observe_restore(restore_receipt_root, now=parsed_now)
    base_control_plane = control_plane_summary(base, backup=backup, restore=restore)
    truth_drift = truth_drift_projection(base)
    control_plane = control_plane_with_truth_drift(base_control_plane, truth_drift)
    closeout_plans = closeout_plan_projection(truth_drift)
    plan = repair_plan(control_plane)
    dashboard = dashboard_projection(
        control_plane,
        plan,
        truth_drift=truth_drift,
        closeout_plans=closeout_plans,
    )
    result = {
        "schema_version": DOCTOR_SCHEMA_VERSION,
        "kind": DOCTOR_KIND,
        "generated_at": generated_at,
        "read_only": True,
        "effect": "none",
        "healthy": bool(control_plane.get("healthy")),
        "control_plane": control_plane,
        "truth_drift": truth_drift,
        "closeout_plans": closeout_plans,
        "repair_plan": plan,
        "dashboard": dashboard,
        "does_not_establish": list(DOCTOR_DOES_NOT_ESTABLISH),
    }
    result["doctor_sha256"] = legacy.sha256_json(result)
    return result


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON input must contain an object")
    return value


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(prog="python -m bureau.doctor")
    value.add_argument("--root", type=Path, required=True)
    value.add_argument("--state-root", type=Path)
    value.add_argument("--github-observations", type=Path)
    value.add_argument("--github-max-age", type=float, default=DEFAULT_GITHUB_MAX_AGE_SECONDS)
    value.add_argument("--backup-root", type=Path, default=DEFAULT_BACKUP_ROOT)
    value.add_argument("--restore-receipt-root", type=Path, default=DEFAULT_RESTORE_RECEIPT_ROOT)
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = args.root.expanduser().resolve()
    registry = legacy.Registry.load(root)
    github = (
        _load_json(args.github_observations)
        if args.github_observations
        else observe_pull_requests(
            root, registry=registry, state_root=args.state_root
        )
    )
    value = doctor_projection(
        root,
        registry=registry,
        state_root=args.state_root,
        github=github,
        github_max_age_seconds=args.github_max_age,
        backup_root=args.backup_root,
        restore_receipt_root=args.restore_receipt_root,
    )
    print(json.dumps(value, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
