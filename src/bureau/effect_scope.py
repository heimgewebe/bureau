"""Fail-closed command effect and canonical coordination-state bindings."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

READ_ONLY = "read_only"
COORDINATION_STATE_MUTATION = "coordination_state_mutation"
REGISTRY_MUTATION = "registry_mutation"
_COORDINATION_STATE_MUTATION_COMMANDS = frozenset(
    {
        "acceptance-authenticate",
        "bind",
        "claim-commit",
        "claim-intent",
        "complete",
        "doctor",
        "fail",
        "heartbeat",
        "projection-repair",
        "lifecycle-reconcile-apply",
        "receipt-normalize",
        "workspace-cleanup",
    }
)


def classify_command_effect_scope(command: str, *, mutates: bool) -> str:
    """Classify effects while keeping unknown mutations on the strict Registry path."""
    if not mutates:
        return READ_ONLY
    if command in _COORDINATION_STATE_MUTATION_COMMANDS:
        return COORDINATION_STATE_MUTATION
    return REGISTRY_MUTATION


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _paths_overlap(left: Path, right: Path) -> bool:
    return _path_is_within(left, right) or _path_is_within(right, left)


def _first_symlink_component(path: Path) -> Path | None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            if current.is_symlink():
                return current
        except OSError:
            return current
    return None


def _nearest_existing_ancestor(path: Path) -> Path | None:
    candidate = path
    while True:
        try:
            candidate.lstat()
            return candidate
        except FileNotFoundError:
            parent = candidate.parent
            if parent == candidate:
                return None
            candidate = parent
        except OSError:
            return None


def _coordination_state_sidecar_reasons(resolved_root: Path) -> list[str]:
    reasons: list[str] = []
    for name in ("envelopes", "receipts"):
        sidecar = resolved_root / name
        if _first_symlink_component(sidecar) is not None:
            reasons.append(f"coordination-state-{name}-symlink-component")
            continue
        try:
            resolved_sidecar = sidecar.resolve(strict=False)
        except (OSError, RuntimeError):
            reasons.append(f"coordination-state-{name}-resolve-failed")
            continue
        if (
            resolved_sidecar.parent != resolved_root
            or not _path_is_within(resolved_sidecar, resolved_root)
        ):
            reasons.append(f"coordination-state-{name}-outside-root")
            continue
        try:
            sidecar_stat = sidecar.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            reasons.append(f"coordination-state-{name}-stat-failed")
            continue
        if not stat.S_ISDIR(sidecar_stat.st_mode):
            reasons.append(f"coordination-state-{name}-not-directory")
        elif sidecar_stat.st_uid != os.geteuid():
            reasons.append(f"coordination-state-{name}-owner-mismatch")
    return reasons


def coordination_state_block(
    *,
    status: str,
    reason_codes: list[str],
    required_action: str,
    runtime_identity: dict[str, Any],
    state_root: Path | None = None,
    state_db: Path | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": status,
        "effect_scope": COORDINATION_STATE_MUTATION,
        "reason_codes": sorted(set(reason_codes)),
        "required_action": required_action,
        "state_root": str(state_root) if state_root is not None else None,
        "state_db": str(state_db) if state_db is not None else None,
        "runtime_identity": runtime_identity,
        "does_not_establish": [
            "coordination_mutation_authority",
            "state_path_safety",
            "safe_retry",
        ],
    }


def canonical_coordination_state_binding(
    *,
    state_root_value: Any,
    state_db_value: Any,
    registry_root: Path,
    runtime_identity: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Bind state-only claim effects to an exact immutable Registry release."""
    manifest = runtime_identity.get("manifest", {})
    canonical = manifest.get("canonical_registry", {})
    registry = runtime_identity.get("registry", {})
    compatibility = runtime_identity.get("compatibility", {})
    resolved_registry = registry_root.expanduser().resolve()
    identity_reasons: list[str] = []
    if manifest.get("valid") is not True:
        identity_reasons.append("runtime-manifest-invalid")
    if canonical.get("valid") is not True:
        identity_reasons.append("canonical-registry-invalid")
    if registry.get("role") != "canonical-runtime-snapshot":
        identity_reasons.append("registry-not-canonical-runtime-snapshot")
    if compatibility.get("status") != "canonical-read-only":
        identity_reasons.append("runtime-not-canonical-read-only")
    if canonical.get("root") != str(resolved_registry):
        identity_reasons.append("canonical-registry-root-mismatch")
    source_commit = canonical.get("source_commit")
    if not source_commit or source_commit != manifest.get("source_commit"):
        identity_reasons.append("canonical-release-commit-mismatch")
    if source_commit != registry.get("head"):
        identity_reasons.append("canonical-registry-commit-mismatch")
    if identity_reasons:
        return (
            coordination_state_block(
                status="coordination-runtime-identity-blocked",
                reason_codes=identity_reasons,
                required_action=(
                    "refresh the immutable Bureau release and canonical Registry snapshot "
                    "before retrying the coordinated claim"
                ),
                runtime_identity=runtime_identity,
            ),
            None,
        )

    if not state_root_value and not state_db_value:
        return (
            coordination_state_block(
                status="explicit-coordination-state-root-required",
                reason_codes=["coordination-state-path-not-explicit"],
                required_action=(
                    "rerun with an absolute --state-root or --state-db outside the "
                    "canonical Registry snapshot"
                ),
                runtime_identity=runtime_identity,
            ),
            None,
        )

    raw_root = (
        Path(state_root_value).expanduser()
        if state_root_value
        else Path(state_db_value).expanduser().parent
    )
    raw_db = (
        Path(state_db_value).expanduser()
        if state_db_value
        else raw_root / "bureau.sqlite3"
    )
    if not raw_root.is_absolute() or not raw_db.is_absolute():
        return (
            coordination_state_block(
                status="coordination-state-path-invalid",
                reason_codes=["coordination-state-path-not-absolute"],
                required_action="use absolute coordination state paths",
                runtime_identity=runtime_identity,
                state_root=raw_root,
                state_db=raw_db,
            ),
            None,
        )

    root_symlink = _first_symlink_component(raw_root)
    db_symlink = _first_symlink_component(raw_db)
    if root_symlink is not None or db_symlink is not None:
        return (
            coordination_state_block(
                status="coordination-state-path-invalid",
                reason_codes=["coordination-state-symlink-component"],
                required_action="use a coordination state path without symlink components",
                runtime_identity=runtime_identity,
                state_root=raw_root,
                state_db=raw_db,
            ),
            None,
        )

    resolved_root = raw_root.resolve(strict=False)
    resolved_db = raw_db.resolve(strict=False)
    reasons: list[str] = []
    if resolved_root == Path(resolved_root.anchor):
        reasons.append("coordination-state-root-is-filesystem-root")
    if resolved_db.parent != resolved_root:
        reasons.append("coordination-state-db-outside-root")
    if _paths_overlap(resolved_root, resolved_registry):
        reasons.append("coordination-state-root-overlaps-registry")
    if _path_is_within(resolved_db, resolved_registry):
        reasons.append("coordination-state-db-inside-registry")
    if resolved_root.exists() and not resolved_root.is_dir():
        reasons.append("coordination-state-root-not-directory")
    if resolved_db.exists() and not resolved_db.is_file():
        reasons.append("coordination-state-db-not-file")
    reasons.extend(_coordination_state_sidecar_reasons(resolved_root))
    existing_ancestor = _nearest_existing_ancestor(resolved_root)
    try:
        if existing_ancestor is None:
            reasons.append("coordination-state-ancestor-unavailable")
        elif not existing_ancestor.is_dir():
            reasons.append("coordination-state-ancestor-not-directory")
        elif existing_ancestor.stat().st_uid != os.geteuid():
            reasons.append("coordination-state-ancestor-owner-mismatch")
        if resolved_root.exists() and resolved_root.stat().st_uid != os.geteuid():
            reasons.append("coordination-state-root-owner-mismatch")
        if resolved_db.exists():
            db_stat = resolved_db.stat()
            if db_stat.st_uid != os.geteuid():
                reasons.append("coordination-state-db-owner-mismatch")
            if db_stat.st_nlink > 1:
                reasons.append("coordination-state-db-hardlink-ambiguous")
    except OSError:
        reasons.append("coordination-state-path-stat-failed")
    if reasons:
        return (
            coordination_state_block(
                status="coordination-state-path-invalid",
                reason_codes=reasons,
                required_action=(
                    "choose an owner-controlled state root outside and non-overlapping with "
                    "the canonical Registry snapshot"
                ),
                runtime_identity=runtime_identity,
                state_root=resolved_root,
                state_db=resolved_db,
            ),
            None,
        )

    binding = {
        "schema_version": 1,
        "kind": "bureau_coordination_state_binding",
        "effect_scope": COORDINATION_STATE_MUTATION,
        "registry_root": str(resolved_registry),
        "registry_source_commit": source_commit,
        "registry_tree_sha256": canonical.get("tree_sha256"),
        "runtime_release_id": manifest.get("release_id"),
        "state_root": str(resolved_root),
        "state_db": str(resolved_db),
        "state_db_existed_at_binding": resolved_db.is_file(),
        "does_not_establish": [
            "future_path_identity",
            "filesystem_race_absence",
            "claim_commit_success",
        ],
    }
    return None, binding
