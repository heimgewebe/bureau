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

DOCTOR_SCHEMA_VERSION = 1
DOCTOR_KIND = "bureau_control_plane_doctor"
DASHBOARD_KIND = "bureau_control_plane_dashboard"

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
    control_plane: Mapping[str, Any], plan: Mapping[str, Any]
) -> dict[str, Any]:
    """Return the deliberately small dashboard consumer surface."""
    return {
        "schema_version": 1,
        "kind": DASHBOARD_KIND,
        "generated_at": control_plane.get("generated_at"),
        "healthy": control_plane.get("healthy"),
        "metrics": control_plane.get("metrics", {}),
        "organs": control_plane.get("organs", {}),
        "attention": [
            {
                "code": item.get("code"),
                "impact": item.get("impact"),
                "dry_run_sha256": item.get("dry_run_sha256"),
            }
            for item in plan.get("proposals", [])
            if isinstance(item, Mapping)
        ],
        "source": "bureau-control-plane-doctor",
        "read_only": True,
        "does_not_establish": list(DOCTOR_DOES_NOT_ESTABLISH),
    }


def doctor_projection(
    root: Path,
    *,
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
        state_root=state_root,
        github=github,
        github_max_age_seconds=github_max_age_seconds,
        now=generated_at,
    )
    parsed_now = _parse_time(generated_at) or _utc_now()
    backup = observe_backup(backup_root, now=parsed_now)
    restore = observe_restore(restore_receipt_root, now=parsed_now)
    control_plane = control_plane_summary(base, backup=backup, restore=restore)
    plan = repair_plan(control_plane)
    dashboard = dashboard_projection(control_plane, plan)
    result = {
        "schema_version": DOCTOR_SCHEMA_VERSION,
        "kind": DOCTOR_KIND,
        "generated_at": generated_at,
        "read_only": True,
        "effect": "none",
        "healthy": bool(control_plane.get("healthy")),
        "control_plane": control_plane,
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
    github = _load_json(args.github_observations) if args.github_observations else None
    value = doctor_projection(
        args.root.expanduser().resolve(),
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
