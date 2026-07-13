"""Strict inventory and reviewed migration for Bureau state-root artifacts."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import sys
from contextlib import ExitStack, contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import legacy
from .approval import require_approval, reviewed_plan_approval

INVENTORY_SCHEMA_VERSION = 1
MIGRATION_PLAN_SCHEMA_VERSION = 2
MIGRATION_RECEIPT_SCHEMA_VERSION = 2
MIGRATION_PLAN_COMMAND = "state-root-artifacts-migration-plan"
MIGRATION_APPLY_COMMAND = "state-root-artifacts-migration-apply"
MIGRATION_ROLLBACK_COMMAND = "state-root-artifacts-migration-rollback"

_COMPLETION_BUNDLE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_COMPLETION_MAX_COUNT = 128
_COMPLETION_DIFF_MAX_BYTES = 16 * 1024 * 1024
_COMPLETION_REVIEW_MAX_BYTES = 256 * 1024
_COMPLETION_REVIEW_AXES = {
    "correctness",
    "integration",
    "regression_risk",
    "security",
    "tests",
}
_REVIEWED_PLAN_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,191}\.json")
_REVIEWED_PLAN_MAX_COUNT = 256
_REVIEWED_PLAN_MAX_BYTES = 512 * 1024
_MIGRATION_ENTRY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}")
_MIGRATION_MAX_ENTRIES = 256
_MIGRATION_MAX_FILES_PER_ENTRY = 4096
_MIGRATION_MAX_TOTAL_BYTES_PER_ENTRY = 512 * 1024 * 1024
_REFERENCE_SCAN_MAX_FILES = 20000
_REFERENCE_SCAN_MAX_BYTES = 128 * 1024 * 1024


def _utc_from_ns(value: int) -> str:
    return (
        datetime.fromtimestamp(value / 1_000_000_000, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _descriptor_relative_support_error() -> str | None:
    required = (os.open, os.mkdir, os.rename, os.rmdir, os.stat)
    if not sys.platform.startswith("linux"):
        return "Linux is required"
    if any(function not in os.supports_dir_fd for function in required):
        return "required dir_fd operations are unavailable"
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        return "O_DIRECTORY or O_NOFOLLOW is unavailable"
    if not Path("/proc/self/fd").is_dir():
        return "/proc/self/fd is unavailable"
    return None


def _require_descriptor_relative_mutation() -> None:
    reason = _descriptor_relative_support_error()
    if reason is not None:
        raise legacy.StateError("descriptor-relative state-root mutation is unsupported: " + reason)


def _absolute_lexical_path(path: Path) -> Path:
    expanded = path.expanduser()
    return Path(os.path.abspath(os.fspath(expanded)))


def _directory_open_flags() -> int:
    return os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW


@contextmanager
def _open_directory_no_follow(path: Path):
    _require_descriptor_relative_mutation()
    target = _absolute_lexical_path(path)
    descriptor = os.open("/", _directory_open_flags())
    try:
        for component in target.parts[1:]:
            try:
                child = os.open(
                    component,
                    _directory_open_flags(),
                    dir_fd=descriptor,
                )
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise legacy.StateError(
                        f"directory path contains a symlink or non-directory: {target}"
                    ) from exc
                raise legacy.StateError(f"cannot open directory anchor {target}: {exc}") from exc
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        with suppress(OSError):
            os.close(descriptor)


def _directory_anchor(path: Path, descriptor: int) -> dict[str, Any]:
    target = _absolute_lexical_path(path)
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode):
        raise legacy.StateError(f"directory anchor is not a directory: {target}")
    return {
        "path": str(target),
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
        "mode": oct(stat.S_IMODE(info.st_mode)),
    }


def _validate_anchor_shape(
    anchor: object,
    *,
    expected_path: Path,
    role: str,
) -> dict[str, Any]:
    target = _absolute_lexical_path(expected_path)
    if not isinstance(anchor, dict):
        raise legacy.StateError(f"{role} directory anchor is missing")
    if anchor.get("path") != str(target):
        raise legacy.StateError(f"{role} directory anchor path mismatch")
    device = anchor.get("device")
    inode = anchor.get("inode")
    mode = anchor.get("mode")
    if (
        not isinstance(device, int)
        or device < 0
        or not isinstance(inode, int)
        or inode <= 0
        or not isinstance(mode, str)
    ):
        raise legacy.StateError(f"{role} directory anchor is invalid")
    return anchor


def _assert_descriptor_matches_anchor(
    descriptor: int,
    anchor: dict[str, Any],
    *,
    role: str,
) -> None:
    info = os.fstat(descriptor)
    if (
        int(info.st_dev) != anchor["device"]
        or int(info.st_ino) != anchor["inode"]
        or oct(stat.S_IMODE(info.st_mode)) != anchor["mode"]
    ):
        raise legacy.StateError(f"{role} directory descriptor identity mismatch")


@contextmanager
def _open_bound_directory(
    anchor: object,
    *,
    expected_path: Path,
    role: str,
):
    validated = _validate_anchor_shape(
        anchor,
        expected_path=expected_path,
        role=role,
    )
    with _open_directory_no_follow(expected_path) as descriptor:
        _assert_descriptor_matches_anchor(descriptor, validated, role=role)
        yield descriptor


def _assert_bound_directory_path(
    descriptor: int,
    anchor: dict[str, Any],
    *,
    role: str,
) -> None:
    _assert_descriptor_matches_anchor(descriptor, anchor, role=role)
    with _open_directory_no_follow(Path(anchor["path"])) as current:
        _assert_descriptor_matches_anchor(current, anchor, role=role)


def _descriptor_path(descriptor: int, name: str | None = None) -> Path:
    path = Path("/proc/self/fd") / str(descriptor)
    return path if name is None else path / name


def _lstat_at(descriptor: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise legacy.StateError(f"cannot inspect descriptor-relative entry {name}: {exc}") from exc


def _rename_at(
    source_descriptor: int,
    source_name: str,
    destination_descriptor: int,
    destination_name: str,
) -> None:
    _require_descriptor_relative_mutation()
    os.rename(
        source_name,
        destination_name,
        src_dir_fd=source_descriptor,
        dst_dir_fd=destination_descriptor,
    )


def _anchor_bundle(plan: dict[str, Any]) -> dict[str, Any]:
    anchors = plan.get("directory_anchors")
    if not isinstance(anchors, dict):
        raise legacy.StateError("migration directory anchors are missing")
    return anchors


def _destination_layout(destination: Path) -> tuple[Path, list[str], dict[str, Any]]:
    _require_descriptor_relative_mutation()
    target = _absolute_lexical_path(destination)
    if target == Path("/"):
        raise legacy.StateError("migration destination cannot be the filesystem root")
    descriptor = os.open("/", _directory_open_flags())
    current_path = Path("/")
    try:
        for index, component in enumerate(target.parts[1:]):
            try:
                child = os.open(
                    component,
                    _directory_open_flags(),
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                missing = list(target.parts[1 + index :])
                if not missing:
                    raise legacy.StateError("migration destination layout is invalid") from None
                return current_path, missing, _directory_anchor(current_path, descriptor)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise legacy.StateError(
                        f"migration destination path contains a symlink or non-directory: {target}"
                    ) from exc
                raise legacy.StateError(
                    f"cannot inspect migration destination layout {target}: {exc}"
                ) from exc
            os.close(descriptor)
            descriptor = child
            current_path = current_path / component
        raise legacy.StateError("migration destination must not exist")
    finally:
        with suppress(OSError):
            os.close(descriptor)


def _entry_name_from_bound_path(path_value: object, root: Path, *, role: str) -> str:
    path = _absolute_lexical_path(Path(str(path_value or "")))
    if path.parent != root or _MIGRATION_ENTRY_RE.fullmatch(path.name) is None:
        raise legacy.StateError(f"{role} path escaped its bound root")
    return path.name


def _regular_file_metadata(path: Path, *, maximum_bytes: int) -> dict[str, Any]:
    try:
        info = path.lstat()
    except OSError as exc:
        raise legacy.StateError(f"cannot inspect {path}: {type(exc).__name__}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise legacy.StateError(f"symlink artifact is forbidden: {path}")
    if not stat.S_ISREG(info.st_mode):
        raise legacy.StateError(f"regular file required: {path}")
    if info.st_size > maximum_bytes:
        raise legacy.StateError(f"artifact exceeds {maximum_bytes} bytes: {path} ({info.st_size})")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise legacy.StateError(f"cannot hash {path}: {type(exc).__name__}: {exc}") from exc
    return {
        "type": "file",
        "size_bytes": info.st_size,
        "mode": oct(stat.S_IMODE(info.st_mode)),
        "mtime_ns": info.st_mtime_ns,
        "source_mtime": _utc_from_ns(info.st_mtime_ns),
        "sha256": digest.hexdigest(),
    }


def _json_file(path: Path, *, maximum_bytes: int) -> tuple[dict[str, Any], dict[str, Any]]:
    metadata = _regular_file_metadata(path, maximum_bytes=maximum_bytes)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise legacy.StateError(f"invalid JSON artifact {path}: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise legacy.StateError(f"JSON artifact must be an object: {path}")
    return value, metadata


def _completion_bundle_inventory(bundle: Path, observed_at: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": bundle.name,
        "type": "directory",
        "content_class": "completion-evidence-bundle",
        "retention_class": "audit-until-superseded",
        "authority": "review-evidence-only",
        "observed_at": observed_at,
        "valid": False,
        "reasons": [],
        "children": [],
        "does_not_establish": [
            "merge_state",
            "deployment_state",
            "task_completion_without_registry_binding",
        ],
    }
    try:
        bundle_info = bundle.lstat()
        if stat.S_ISLNK(bundle_info.st_mode) or not stat.S_ISDIR(bundle_info.st_mode):
            raise legacy.StateError("bundle is not a real directory")
        if _COMPLETION_BUNDLE_RE.fullmatch(bundle.name) is None:
            raise legacy.StateError("bundle name is unsupported")
        children = sorted(bundle.iterdir(), key=lambda item: item.name)
        if {child.name for child in children} != {"pr.diff", "self-review.json"}:
            raise legacy.StateError("bundle must contain exactly pr.diff and self-review.json")
        diff_meta = _regular_file_metadata(
            bundle / "pr.diff", maximum_bytes=_COMPLETION_DIFF_MAX_BYTES
        )
        review, review_meta = _json_file(
            bundle / "self-review.json", maximum_bytes=_COMPLETION_REVIEW_MAX_BYTES
        )
        result["children"] = [
            {"name": "pr.diff", **diff_meta},
            {"name": "self-review.json", **review_meta},
        ]
        claimed_review_sha256 = review.get("review_sha256")
        unsigned_review = {key: value for key, value in review.items() if key != "review_sha256"}
        axes = review.get("axes")
        checks = [
            (review.get("schema_version") == 1, "unsupported review schema"),
            (review.get("kind") == "bureau_pr_self_review", "unsupported review kind"),
            (review.get("conclusion") == "PASS", "review conclusion is not PASS"),
            (
                isinstance(review.get("repository"), str) and bool(review["repository"].strip()),
                "repository identity is missing",
            ),
            (
                isinstance(review.get("pull_request"), int) and review["pull_request"] > 0,
                "pull request identity is invalid",
            ),
            (
                re.fullmatch(r"[0-9a-f]{40}", str(review.get("reviewed_head", ""))) is not None,
                "reviewed head is invalid",
            ),
            (
                re.fullmatch(r"[0-9a-f]{40}", str(review.get("base_head", ""))) is not None,
                "base head is invalid",
            ),
            (
                review.get("github_diff_bytes") == diff_meta["size_bytes"],
                "diff byte count mismatch",
            ),
            (
                review.get("github_diff_sha256") == diff_meta["sha256"],
                "diff digest mismatch",
            ),
            (
                re.fullmatch(r"[0-9a-f]{64}", str(claimed_review_sha256 or "")) is not None
                and legacy.sha256_json(unsigned_review) == claimed_review_sha256,
                "review digest mismatch",
            ),
            (
                isinstance(axes, dict) and set(axes) == _COMPLETION_REVIEW_AXES,
                "review axes are incomplete",
            ),
        ]
        for passed, reason in checks:
            if not passed:
                result["reasons"].append(reason)
        if isinstance(axes, dict):
            for axis_name in sorted(_COMPLETION_REVIEW_AXES):
                axis = axes.get(axis_name)
                if not isinstance(axis, dict) or axis.get("result") != "PASS":
                    result["reasons"].append(f"review axis is not PASS: {axis_name}")
                    continue
                evidence = axis.get("evidence")
                if (
                    not isinstance(evidence, list)
                    or not evidence
                    or len(evidence) > 64
                    or any(
                        not isinstance(item, str) or not item.strip() or len(item) > 1000
                        for item in evidence
                    )
                ):
                    result["reasons"].append(f"review axis evidence is invalid: {axis_name}")
        result["producer"] = {
            "kind": "bureau-pr-self-review",
            "repository": review.get("repository"),
            "pull_request": review.get("pull_request"),
            "reviewed_head": review.get("reviewed_head"),
            "base_head": review.get("base_head"),
            "reviewed_at_unix": review.get("reviewed_at_unix"),
        }
        result["content_sha256"] = legacy.sha256_json(
            {
                "bundle": bundle.name,
                "diff_sha256": diff_meta["sha256"],
                "review_sha256": review_meta["sha256"],
                "declared_review_sha256": claimed_review_sha256,
            }
        )
        result["valid"] = not result["reasons"]
    except (OSError, legacy.StateError) as exc:
        result["reasons"].append(str(exc))
    return result


def completion_evidence_inventory(entry: Path) -> dict[str, Any]:
    observed_at = legacy.utc_now()
    result: dict[str, Any] = {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "name": entry.name,
        "path": str(entry),
        "content_class": "completion-evidence-directory",
        "observed_at": observed_at,
        "valid": False,
        "children": [],
        "reasons": [],
    }
    try:
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise legacy.StateError("completion evidence root is not a real directory")
        bundles = sorted(entry.iterdir(), key=lambda item: item.name)
        if not bundles or len(bundles) > _COMPLETION_MAX_COUNT:
            raise legacy.StateError("completion evidence bundle count is invalid")
        result["children"] = [
            _completion_bundle_inventory(bundle, observed_at) for bundle in bundles
        ]
        result["reasons"] = [
            f"{item['name']}: {reason}" for item in result["children"] for reason in item["reasons"]
        ]
        result["valid"] = not result["reasons"]
    except (OSError, legacy.StateError) as exc:
        result["reasons"].append(str(exc))
    return result


def completion_evidence_directory_valid(entry: Path) -> bool:
    return bool(completion_evidence_inventory(entry)["valid"])


def _reviewed_plan_hash_matches(payload: dict[str, Any]) -> tuple[bool, str]:
    claimed = payload.get("plan_sha256")
    if re.fullmatch(r"[0-9a-f]{64}", str(claimed or "")) is None:
        return False, "plan digest is invalid"
    unsigned = {key: value for key, value in payload.items() if key != "plan_sha256"}
    if legacy.sha256_json(unsigned) == claimed:
        return True, "current-reviewed-plan"
    generated = json.loads(json.dumps(unsigned))
    generated["review"] = {"required": True, "status": "pending"}
    if legacy.sha256_json(generated) == claimed:
        return True, "generated-pending-plan-with-reviewed-overlay"
    return False, "plan digest mismatch"


def _reviewed_plan_inventory(path: Path, observed_at: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": path.name,
        "type": "file",
        "content_class": "reviewed-live-promotion-plan",
        "retention_class": "until-applied-or-superseded",
        "authority": "proposal-only",
        "observed_at": observed_at,
        "valid": False,
        "reasons": [],
        "does_not_establish": [
            "registry_task_truth",
            "queue_truth",
            "claim_authority",
            "dispatch_authority",
            "merge_readiness",
        ],
    }
    try:
        if _REVIEWED_PLAN_NAME_RE.fullmatch(path.name) is None:
            raise legacy.StateError("plan filename is unsupported")
        payload, metadata = _json_file(path, maximum_bytes=_REVIEWED_PLAN_MAX_BYTES)
        result.update(metadata)
        review = payload.get("review")
        task = payload.get("task_json")
        source_event = payload.get("source_event")
        nonclaims = payload.get("does_not_establish")
        digest_valid, digest_mode = _reviewed_plan_hash_matches(payload)
        checks = [
            (payload.get("schema_version") == 2, "unsupported plan schema"),
            (payload.get("command") == "live-promote-plan", "unsupported plan command"),
            (
                isinstance(payload.get("event_id"), int) and payload["event_id"] > 0,
                "event id is invalid",
            ),
            (
                isinstance(payload.get("initiative"), str)
                and legacy.ID_RE.fullmatch(payload["initiative"]) is not None,
                "initiative id is invalid",
            ),
            (
                isinstance(payload.get("task_id"), str)
                and legacy.ID_RE.fullmatch(payload["task_id"]) is not None,
                "task id is invalid",
            ),
            (
                isinstance(review, dict)
                and review.get("required") is True
                and review.get("status") == "reviewed"
                and isinstance(review.get("reviewer"), str)
                and bool(review["reviewer"].strip()),
                "review binding is incomplete",
            ),
            (
                isinstance(task, dict)
                and task.get("id") == payload.get("task_id")
                and task.get("initiative") == payload.get("initiative"),
                "task projection binding mismatch",
            ),
            (
                isinstance(source_event, dict)
                and source_event.get("event_id") == payload.get("event_id")
                and isinstance(source_event.get("record"), dict)
                and source_event["record"].get("kind") == "candidate_task",
                "source event binding mismatch",
            ),
            (
                isinstance(nonclaims, list)
                and bool(nonclaims)
                and all(isinstance(item, str) and item.strip() for item in nonclaims),
                "non-authority semantics are missing",
            ),
            (digest_valid, digest_mode),
        ]
        for passed, reason in checks:
            if not passed:
                result["reasons"].append(reason)
        result["producer"] = {
            "kind": "bureau-live-register",
            "event_id": payload.get("event_id"),
            "candidate_id": (
                source_event.get("record", {}).get("candidate_id")
                if isinstance(source_event, dict)
                else None
            ),
            "reviewer": review.get("reviewer") if isinstance(review, dict) else None,
            "task_id": payload.get("task_id"),
            "initiative": payload.get("initiative"),
        }
        result["digest_mode"] = digest_mode
        result["valid"] = not result["reasons"]
    except (OSError, legacy.StateError) as exc:
        result["reasons"].append(str(exc))
    return result


def reviewed_plan_inventory(entry: Path) -> dict[str, Any]:
    observed_at = legacy.utc_now()
    result: dict[str, Any] = {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "name": entry.name,
        "path": str(entry),
        "content_class": "reviewed-plan-directory",
        "observed_at": observed_at,
        "valid": False,
        "children": [],
        "reasons": [],
    }
    try:
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise legacy.StateError("reviewed plan root is not a real directory")
        plans = sorted(entry.iterdir(), key=lambda item: item.name)
        if not plans or len(plans) > _REVIEWED_PLAN_MAX_COUNT:
            raise legacy.StateError("reviewed plan count is invalid")
        result["children"] = [_reviewed_plan_inventory(path, observed_at) for path in plans]
        result["reasons"] = [
            f"{item['name']}: {reason}" for item in result["children"] for reason in item["reasons"]
        ]
        result["valid"] = not result["reasons"]
    except (OSError, legacy.StateError) as exc:
        result["reasons"].append(str(exc))
    return result


def reviewed_plan_directory_valid(entry: Path) -> bool:
    return bool(reviewed_plan_inventory(entry)["valid"])


def managed_state_root_inventory(state_root: Path) -> dict[str, Any]:
    root = state_root.expanduser().resolve(strict=False)
    observed_at = legacy.utc_now()
    entries = []
    for name, inspector in (
        ("evidence", completion_evidence_inventory),
        ("plans", reviewed_plan_inventory),
    ):
        path = root / name
        if not path.exists() and not path.is_symlink():
            entries.append(
                {
                    "name": name,
                    "path": str(path),
                    "content_class": "absent",
                    "observed_at": observed_at,
                    "valid": True,
                    "children": [],
                    "reasons": [],
                }
            )
        else:
            entries.append(inspector(path))
    return {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "kind": "bureau_state_root_artifact_inventory",
        "state_root": str(root),
        "observed_at": observed_at,
        "entries": entries,
        "healthy": all(item["valid"] for item in entries),
        "does_not_establish": [
            "task_completion",
            "merge_readiness",
            "content_authority_beyond_declared_schema",
            "permission_to_delete_or_move",
            "absence_of_unmanaged_state_root_entries",
        ],
    }


def _entry_identity(path: Path) -> dict[str, Any]:
    try:
        root_info = path.lstat()
    except OSError as exc:
        raise legacy.StateError(f"cannot inspect migration entry {path}: {exc}") from exc
    if stat.S_ISLNK(root_info.st_mode):
        raise legacy.StateError(f"symlink migration entry is forbidden: {path}")
    if stat.S_ISREG(root_info.st_mode):
        metadata = _regular_file_metadata(path, maximum_bytes=_MIGRATION_MAX_TOTAL_BYTES_PER_ENTRY)
        return {
            "name": path.name,
            "type": "file",
            "device": int(root_info.st_dev),
            "inode": int(root_info.st_ino),
            **metadata,
            "entry_sha256": legacy.sha256_json(
                {
                    "name": path.name,
                    "type": "file",
                    "device": int(root_info.st_dev),
                    "inode": int(root_info.st_ino),
                    "mode": metadata["mode"],
                    "size_bytes": metadata["size_bytes"],
                    "sha256": metadata["sha256"],
                    "mtime_ns": metadata["mtime_ns"],
                }
            ),
            "file_count": 1,
            "total_bytes": metadata["size_bytes"],
        }
    if not stat.S_ISDIR(root_info.st_mode):
        raise legacy.StateError(f"unsupported migration entry type: {path}")
    records: list[dict[str, Any]] = []
    file_count = 0
    total_bytes = 0
    for directory, dirnames, filenames in os.walk(path, followlinks=False):
        directory_path = Path(directory)
        directory_info = directory_path.lstat()
        if stat.S_ISLNK(directory_info.st_mode):
            raise legacy.StateError(f"symlink directory is forbidden: {directory_path}")
        relative_directory = directory_path.relative_to(path)
        records.append(
            {
                "path": ("." if not relative_directory.parts else relative_directory.as_posix()),
                "type": "directory",
                "mode": oct(stat.S_IMODE(directory_info.st_mode)),
                "mtime_ns": directory_info.st_mtime_ns,
            }
        )
        for name in sorted(dirnames):
            candidate = directory_path / name
            if candidate.is_symlink():
                raise legacy.StateError(f"symlink child is forbidden: {candidate}")
        for name in sorted(filenames):
            candidate = directory_path / name
            metadata = _regular_file_metadata(
                candidate,
                maximum_bytes=_MIGRATION_MAX_TOTAL_BYTES_PER_ENTRY,
            )
            file_count += 1
            total_bytes += metadata["size_bytes"]
            if file_count > _MIGRATION_MAX_FILES_PER_ENTRY:
                raise legacy.StateError(f"migration entry has too many files: {path}")
            if total_bytes > _MIGRATION_MAX_TOTAL_BYTES_PER_ENTRY:
                raise legacy.StateError(f"migration entry is too large: {path}")
            records.append(
                {
                    "path": candidate.relative_to(path).as_posix(),
                    "type": "file",
                    "mode": metadata["mode"],
                    "mtime_ns": metadata["mtime_ns"],
                    "size_bytes": metadata["size_bytes"],
                    "sha256": metadata["sha256"],
                }
            )
    identity_payload = {
        "name": path.name,
        "type": "directory",
        "device": int(root_info.st_dev),
        "inode": int(root_info.st_ino),
        "records": records,
    }
    return {
        "name": path.name,
        "type": "directory",
        "device": int(root_info.st_dev),
        "inode": int(root_info.st_ino),
        "mode": oct(stat.S_IMODE(root_info.st_mode)),
        "mtime_ns": root_info.st_mtime_ns,
        "source_mtime": _utc_from_ns(root_info.st_mtime_ns),
        "file_count": file_count,
        "total_bytes": total_bytes,
        "tree_sha256": legacy.sha256_json(records),
        "entry_sha256": legacy.sha256_json(identity_payload),
    }


def _reference_hits(reference_root: Path, source: Path) -> list[dict[str, Any]]:
    root = _absolute_lexical_path(reference_root)
    try:
        root_info = root.stat()
    except OSError as exc:
        raise legacy.StateError(f"reference root is unavailable: {root}: {exc}") from exc
    if not stat.S_ISDIR(root_info.st_mode):
        raise legacy.StateError(f"reference root is not a directory: {root}")
    needles = {str(source), source.name}
    hits: list[dict[str, Any]] = []
    inspected_files = 0
    inspected_bytes = 0
    for base_name in ("registry", "docs"):
        base = root / base_name
        if not base.is_dir():
            continue
        for candidate in sorted(base.rglob("*")):
            if candidate.is_symlink() or not candidate.is_file():
                continue
            inspected_files += 1
            if inspected_files > _REFERENCE_SCAN_MAX_FILES:
                raise legacy.StateError("reference scan file limit exceeded")
            try:
                size = candidate.stat().st_size
            except OSError as exc:
                raise legacy.StateError(f"reference scan failed: {candidate}: {exc}") from exc
            inspected_bytes += size
            if inspected_bytes > _REFERENCE_SCAN_MAX_BYTES:
                raise legacy.StateError("reference scan byte limit exceeded")
            if size > 2 * 1024 * 1024:
                continue
            try:
                text = candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            matched = sorted(needle for needle in needles if needle in text)
            if matched:
                hits.append({"path": str(candidate), "matched": matched})
                if len(hits) >= 100:
                    return hits
    return hits


def _inode_key(info: os.stat_result) -> tuple[int, int, int]:
    return int(info.st_dev), int(info.st_ino), stat.S_IFMT(info.st_mode)


def _entry_inode_keys(path: Path) -> set[tuple[int, int, int]]:
    try:
        root_info = path.lstat()
    except OSError as exc:
        raise legacy.StateError(f"cannot inspect process-reference root {path}: {exc}") from exc
    if stat.S_ISLNK(root_info.st_mode):
        raise legacy.StateError(f"symlink process-reference root is forbidden: {path}")
    keys = {_inode_key(root_info)}
    if not stat.S_ISDIR(root_info.st_mode):
        return keys
    count = 0
    for directory, dirnames, filenames in os.walk(path, followlinks=False):
        directory_path = Path(directory)
        for name in sorted(dirnames + filenames):
            candidate = directory_path / name
            try:
                info = candidate.lstat()
            except OSError as exc:
                raise legacy.StateError(
                    f"cannot inspect process-reference entry {candidate}: {exc}"
                ) from exc
            if stat.S_ISLNK(info.st_mode):
                raise legacy.StateError(
                    f"symlink process-reference entry is forbidden: {candidate}"
                )
            count += 1
            if count > _MIGRATION_MAX_FILES_PER_ENTRY * 2:
                raise legacy.StateError("process-reference inode scan limit exceeded")
            keys.add(_inode_key(info))
    return keys


def _process_target_description(path: Path) -> str:
    try:
        return os.readlink(path)
    except OSError:
        return str(path)


def _process_references(path: Path) -> list[dict[str, Any]]:
    target_keys = _entry_inode_keys(path)
    references: list[dict[str, Any]] = []
    proc = Path("/proc")
    for process in proc.iterdir():
        if not process.name.isdigit():
            continue
        pid = int(process.name)
        cwd_link = process / "cwd"
        try:
            cwd_info = cwd_link.stat()
        except OSError:
            cwd_info = None
        if cwd_info is not None and _inode_key(cwd_info) in target_keys:
            references.append(
                {
                    "pid": pid,
                    "kind": "cwd",
                    "path": _process_target_description(cwd_link),
                }
            )
        fd_root = process / "fd"
        try:
            descriptors = list(fd_root.iterdir())
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                descriptor_info = descriptor.stat()
            except OSError:
                continue
            if _inode_key(descriptor_info) in target_keys:
                references.append(
                    {
                        "pid": pid,
                        "kind": "fd",
                        "fd": descriptor.name,
                        "path": _process_target_description(descriptor),
                    }
                )
        if len(references) >= 100:
            break
    return references


def _migration_review_payload_sha256(plan: dict[str, Any]) -> str:
    payload = {
        key: value for key, value in plan.items() if key not in {"review", "review_payload_sha256"}
    }
    return legacy.sha256_json(payload)


def state_root_migration_plan(
    state_root: Path,
    entry_names: list[str],
    destination_root: Path,
    *,
    reference_root: Path,
) -> dict[str, Any]:
    _require_descriptor_relative_mutation()
    root = _absolute_lexical_path(state_root)
    destination = _absolute_lexical_path(destination_root)
    reference = _absolute_lexical_path(reference_root)
    if (
        destination == root
        or _path_is_within(destination, root)
        or _path_is_within(root, destination)
    ):
        raise legacy.StateError("migration destination must not overlap the state root")
    names = sorted(set(entry_names))
    if not names or len(names) > _MIGRATION_MAX_ENTRIES:
        raise legacy.StateError("migration entry count is invalid")

    destination_base, destination_components, destination_base_anchor = _destination_layout(
        destination
    )
    with ExitStack() as stack:
        root_descriptor = stack.enter_context(_open_directory_no_follow(root))
        root_parent_descriptor = stack.enter_context(_open_directory_no_follow(root.parent))
        reference_descriptor = stack.enter_context(_open_directory_no_follow(reference))
        reference_parent_descriptor = stack.enter_context(
            _open_directory_no_follow(reference.parent)
        )
        entries: list[dict[str, Any]] = []
        for name in names:
            if _MIGRATION_ENTRY_RE.fullmatch(name) is None:
                raise legacy.StateError(f"invalid top-level migration entry name: {name}")
            source = root / name
            if _lstat_at(root_descriptor, name) is None:
                raise legacy.StateError(f"migration source is missing: {source}")
            anchored_source = _descriptor_path(root_descriptor, name)
            identity = _entry_identity(anchored_source)
            references = _reference_hits(
                _descriptor_path(reference_descriptor),
                source,
            )
            processes = _process_references(anchored_source)
            if references:
                raise legacy.StateError(
                    f"migration source is referenced by repository evidence: {source}"
                )
            if processes:
                raise legacy.StateError(
                    f"migration source is referenced by an active process: {source}"
                )
            entries.append(
                {
                    **identity,
                    "source": str(source),
                    "destination": str(destination / name),
                    "reference_hits": references,
                    "process_references": processes,
                }
            )
        entries_sha256 = legacy.sha256_json(entries)
        directory_anchors = {
            "state_root": _directory_anchor(root, root_descriptor),
            "state_root_parent": _directory_anchor(root.parent, root_parent_descriptor),
            "destination_base": destination_base_anchor,
            "reference_root": _directory_anchor(reference, reference_descriptor),
            "reference_root_parent": _directory_anchor(
                reference.parent, reference_parent_descriptor
            ),
        }

    plan = {
        "schema_version": MIGRATION_PLAN_SCHEMA_VERSION,
        "command": MIGRATION_PLAN_COMMAND,
        "created_at": legacy.utc_now(),
        "state_root": str(root),
        "destination_root": str(destination),
        "reference_root": str(reference),
        "directory_anchors": directory_anchors,
        "destination_layout": {
            "base_path": str(destination_base),
            "missing_components": destination_components,
        },
        "platform_contract": {
            "mutation_mode": "linux-descriptor-relative-v1",
            "no_follow": True,
            "silent_fallback": False,
        },
        "entries": entries,
        "entries_sha256": entries_sha256,
        "review": {
            "required": True,
            "status": "pending",
            "instructions": (
                "Review every operational field, source/destination, directory anchor "
                "and digest. To apply, set status to reviewed, add reviewer and "
                "reviewed_at, and copy review_payload_sha256, entries_sha256 and "
                "destination_root into this review object."
            ),
        },
        "execution_preconditions": [
            "All bound directory device/inode identities remain current before each effect.",
            "Every destination component remains absent until descriptor-relative creation.",
            "Every source identity and the reviewed plan file remain unchanged.",
            "No source has a repository reference or active process reference.",
            "Effects use no-follow directory descriptors and same-filesystem renameat semantics.",
            "Apply compensates moves from this run and removes its empty created "
            "directories on failure.",
        ],
        "does_not_establish": [
            "artifact_obsolescence",
            "permission_to_delete",
            "content_authority",
            "future_reference_or_process_absence",
            "cleanup_authority_without_review",
            "protection_against_final_entry_replacement_between_last_stat_and_renameat",
            "crash_or_power_loss_atomicity_without_a_durable_recovery_journal",
        ],
    }
    plan["review_payload_sha256"] = _migration_review_payload_sha256(plan)
    return plan


def _write_create_only(path: Path, content: str) -> None:
    target = path.expanduser().resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        with suppress(OSError):
            target.unlink()
        raise


def write_state_root_migration_plan(
    state_root: Path,
    entry_names: list[str],
    destination_root: Path,
    path: Path,
    *,
    reference_root: Path,
) -> dict[str, Any]:
    plan = state_root_migration_plan(
        state_root,
        entry_names,
        destination_root,
        reference_root=reference_root,
    )
    target = path.expanduser().resolve(strict=False)
    if _path_is_within(target, state_root.expanduser().resolve(strict=True)):
        raise legacy.StateError("migration plan must be stored outside the active state root")
    _write_create_only(target, legacy.canonical_json(plan) + "\n")
    return {**plan, "path": str(target)}


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise legacy.StateError(f"cannot read plan or receipt {path}: {exc}") from exc


def _load_reviewed_migration_plan(path: Path) -> tuple[Path, dict[str, Any], str]:
    target = path.expanduser().resolve(strict=True)
    plan_sha256 = _file_sha256(target)
    plan = legacy.read_json(target)
    if (
        plan.get("schema_version") != MIGRATION_PLAN_SCHEMA_VERSION
        or plan.get("command") != MIGRATION_PLAN_COMMAND
    ):
        raise legacy.StateError("state-root migration plan has unsupported schema or command")
    review = plan.get("review")
    if not isinstance(review, dict) or review.get("status") != "reviewed":
        raise legacy.StateError("state-root migration plan is not reviewed")
    if not review.get("reviewer") or not review.get("reviewed_at"):
        raise legacy.StateError("reviewed migration plan requires reviewer and reviewed_at")
    claimed_payload_sha256 = plan.get("review_payload_sha256")
    if (
        re.fullmatch(r"[0-9a-f]{64}", str(claimed_payload_sha256 or "")) is None
        or _migration_review_payload_sha256(plan) != claimed_payload_sha256
    ):
        raise legacy.StateError("migration plan payload digest mismatch")
    if review.get("review_payload_sha256") != claimed_payload_sha256:
        raise legacy.StateError("review is not bound to migration plan payload")
    if review.get("entries_sha256") != plan.get("entries_sha256"):
        raise legacy.StateError("review is not bound to migration entry states")
    if review.get("destination_root") != plan.get("destination_root"):
        raise legacy.StateError("review is not bound to migration destination")
    entries = plan.get("entries")
    if (
        not isinstance(entries, list)
        or not entries
        or legacy.sha256_json(entries) != plan.get("entries_sha256")
    ):
        raise legacy.StateError("migration entry binding is invalid")
    plan["approval"] = require_approval(
        "state_root_migration",
        reviewed_plan_approval(
            reviewer=str(review["reviewer"]),
            reference=str(target),
            approved=True,
            scope="state_root_migration",
        ),
        expected_reference=str(target),
    )
    return target, plan, plan_sha256


def _receipt_path(plan_path: Path) -> Path:
    return plan_path.with_name(f"{plan_path.name}.receipt.json")


def _validate_migration_paths(
    plan: dict[str, Any],
) -> tuple[Path, Path, Path, Path, list[str], dict[str, Any]]:
    _require_descriptor_relative_mutation()
    root = _absolute_lexical_path(Path(str(plan.get("state_root", ""))))
    destination = _absolute_lexical_path(Path(str(plan.get("destination_root", ""))))
    reference = _absolute_lexical_path(Path(str(plan.get("reference_root", ""))))
    if (
        destination == root
        or _path_is_within(destination, root)
        or _path_is_within(root, destination)
    ):
        raise legacy.StateError("migration destination overlaps the state root")
    platform_contract = plan.get("platform_contract")
    if platform_contract != {
        "mutation_mode": "linux-descriptor-relative-v1",
        "no_follow": True,
        "silent_fallback": False,
    }:
        raise legacy.StateError("migration platform contract is invalid")
    layout = plan.get("destination_layout")
    if not isinstance(layout, dict):
        raise legacy.StateError("migration destination layout is missing")
    destination_base = _absolute_lexical_path(Path(str(layout.get("base_path", ""))))
    components = layout.get("missing_components")
    if (
        not isinstance(components, list)
        or not components
        or any(
            not isinstance(component, str) or _MIGRATION_ENTRY_RE.fullmatch(component) is None
            for component in components
        )
        or destination_base.joinpath(*components) != destination
    ):
        raise legacy.StateError("migration destination layout is invalid")
    anchors = _anchor_bundle(plan)
    _validate_anchor_shape(anchors.get("state_root"), expected_path=root, role="state-root")
    _validate_anchor_shape(
        anchors.get("state_root_parent"),
        expected_path=root.parent,
        role="state-root-parent",
    )
    _validate_anchor_shape(
        anchors.get("destination_base"),
        expected_path=destination_base,
        role="destination-base",
    )
    _validate_anchor_shape(
        anchors.get("reference_root"),
        expected_path=reference,
        role="reference-root",
    )
    _validate_anchor_shape(
        anchors.get("reference_root_parent"),
        expected_path=reference.parent,
        role="reference-root-parent",
    )
    return root, destination, reference, destination_base, components, anchors


def _open_plan_directories(
    stack: ExitStack,
    *,
    root: Path,
    reference: Path,
    destination_base: Path,
    anchors: dict[str, Any],
) -> dict[str, int]:
    return {
        "state_root": stack.enter_context(
            _open_bound_directory(anchors["state_root"], expected_path=root, role="state-root")
        ),
        "state_root_parent": stack.enter_context(
            _open_bound_directory(
                anchors["state_root_parent"],
                expected_path=root.parent,
                role="state-root-parent",
            )
        ),
        "reference_root": stack.enter_context(
            _open_bound_directory(
                anchors["reference_root"],
                expected_path=reference,
                role="reference-root",
            )
        ),
        "reference_root_parent": stack.enter_context(
            _open_bound_directory(
                anchors["reference_root_parent"],
                expected_path=reference.parent,
                role="reference-root-parent",
            )
        ),
        "destination_base": stack.enter_context(
            _open_bound_directory(
                anchors["destination_base"],
                expected_path=destination_base,
                role="destination-base",
            )
        ),
    }


def _assert_plan_directory_paths_current(
    descriptors: dict[str, int],
    anchors: dict[str, Any],
) -> None:
    for key, role in (
        ("state_root_parent", "state-root-parent"),
        ("state_root", "state-root"),
        ("reference_root_parent", "reference-root-parent"),
        ("reference_root", "reference-root"),
        ("destination_base", "destination-base"),
    ):
        _assert_bound_directory_path(descriptors[key], anchors[key], role=role)


def _create_destination_chain(
    stack: ExitStack,
    *,
    base_descriptor: int,
    base_path: Path,
    components: list[str],
) -> tuple[int, list[dict[str, Any]]]:
    current_descriptor = base_descriptor
    current_path = base_path
    created: list[dict[str, Any]] = []
    try:
        for component in components:
            if _lstat_at(current_descriptor, component) is not None:
                raise legacy.StateError(
                    "migration destination collision: component appeared since review: "
                    f"{current_path / component}"
                )
            try:
                os.mkdir(component, mode=0o700, dir_fd=current_descriptor)
            except OSError as exc:
                raise legacy.StateError(
                    f"cannot create descriptor-relative migration destination "
                    f"{current_path / component}: {exc}"
                ) from exc
            try:
                child = os.open(
                    component,
                    _directory_open_flags(),
                    dir_fd=current_descriptor,
                )
            except OSError as exc:
                with suppress(OSError):
                    os.rmdir(component, dir_fd=current_descriptor)
                raise legacy.StateError(
                    f"cannot open created migration destination {current_path / component}: {exc}"
                ) from exc
            stack.callback(os.close, child)
            current_path = current_path / component
            record = {
                "path": str(current_path),
                "name": component,
                "parent_descriptor": current_descriptor,
                "descriptor": child,
                "anchor": _directory_anchor(current_path, child),
            }
            created.append(record)
            current_descriptor = child
    except Exception as exc:
        if created:
            _cleanup_created_destination_chain(created, cause=exc)
        raise
    return current_descriptor, created


def _assert_created_destination_paths_current(
    created: list[dict[str, Any]],
) -> None:
    for index, record in enumerate(created):
        _assert_bound_directory_path(
            record["descriptor"],
            record["anchor"],
            role=f"destination-component-{index}",
        )


def _cleanup_created_destination_chain(
    created: list[dict[str, Any]],
    *,
    cause: Exception,
) -> None:
    failures: list[str] = []
    for record in reversed(created):
        try:
            current = _lstat_at(record["parent_descriptor"], record["name"])
            expected = record["anchor"]
            if (
                current is None
                or stat.S_ISLNK(current.st_mode)
                or int(current.st_dev) != expected["device"]
                or int(current.st_ino) != expected["inode"]
            ):
                raise legacy.StateError(
                    "created destination path no longer names its bound directory: "
                    f"{record['path']}"
                )
            os.rmdir(record["name"], dir_fd=record["parent_descriptor"])
        except (OSError, legacy.StateError) as exc:
            failures.append(f"{record['path']}: {type(exc).__name__}: {exc}")
    if failures:
        raise legacy.StateError(
            "migration failed and created destination cleanup was incomplete: "
            + str(cause)
            + "; "
            + "; ".join(failures)
        ) from cause


def _receipt_directory_anchors(
    receipt: dict[str, Any],
    *,
    root: Path,
    destination: Path,
    reference: Path,
    destination_base: Path,
) -> dict[str, Any]:
    anchors = receipt.get("directory_anchors")
    if not isinstance(anchors, dict):
        raise legacy.StateError("migration receipt directory anchors are missing")
    for key, path, role in (
        ("state_root", root, "state-root"),
        ("state_root_parent", root.parent, "state-root-parent"),
        ("destination_base", destination_base, "destination-base"),
        ("destination_root", destination, "destination-root"),
        ("reference_root", reference, "reference-root"),
        ("reference_root_parent", reference.parent, "reference-root-parent"),
    ):
        _validate_anchor_shape(anchors.get(key), expected_path=path, role=role)
    return anchors


def _validated_apply_receipt(
    receipt: dict[str, Any],
    *,
    reviewed_plan_sha256: str,
    plan: dict[str, Any],
) -> dict[str, Any]:
    claimed_receipt_sha256 = receipt.get("receipt_sha256")
    unsigned_receipt = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    entries = receipt.get("entries")
    receipt_anchors = receipt.get("directory_anchors")
    plan_anchors = plan.get("directory_anchors")
    if (
        receipt.get("schema_version") != MIGRATION_RECEIPT_SCHEMA_VERSION
        or receipt.get("command") != MIGRATION_APPLY_COMMAND
        or receipt.get("reviewed_plan_sha256") != reviewed_plan_sha256
        or receipt.get("entries_sha256") != plan.get("entries_sha256")
        or receipt.get("state_root") != plan.get("state_root")
        or receipt.get("destination_root") != plan.get("destination_root")
        or receipt.get("reference_root") != plan.get("reference_root")
        or receipt.get("destination_layout") != plan.get("destination_layout")
        or receipt.get("platform_contract") != plan.get("platform_contract")
        or not isinstance(receipt_anchors, dict)
        or not isinstance(plan_anchors, dict)
        or any(receipt_anchors.get(key) != value for key, value in plan_anchors.items())
        or not isinstance(entries, list)
        or len(entries) != len(plan.get("entries", []))
        or re.fullmatch(r"[0-9a-f]{64}", str(claimed_receipt_sha256 or "")) is None
        or legacy.sha256_json(unsigned_receipt) != claimed_receipt_sha256
    ):
        raise legacy.StateError("existing migration receipt does not match the plan")
    return receipt


def _verify_applied_receipt_state(
    receipt: dict[str, Any],
    *,
    state_root: Path,
    destination_root: Path,
    state_descriptor: int,
    destination_descriptor: int,
) -> None:
    entries = receipt.get("entries")
    if not isinstance(entries, list):
        raise legacy.StateError("migration receipt entries are invalid")
    for item in entries:
        if not isinstance(item, dict):
            raise legacy.StateError("migration receipt entry is invalid")
        source_name = _entry_name_from_bound_path(
            item.get("source"), state_root, role="receipt source"
        )
        destination_name = _entry_name_from_bound_path(
            item.get("destination"), destination_root, role="receipt destination"
        )
        expected = str(item.get("entry_sha256", ""))
        if _lstat_at(state_descriptor, source_name) is not None:
            raise legacy.StateError(
                f"idempotent migration source unexpectedly exists: {state_root / source_name}"
            )
        destination_info = _lstat_at(destination_descriptor, destination_name)
        if destination_info is None or stat.S_ISLNK(destination_info.st_mode):
            raise legacy.StateError(
                f"idempotent migration destination is missing or symlinked: "
                f"{destination_root / destination_name}"
            )
        if (
            _entry_identity(_descriptor_path(destination_descriptor, destination_name))[
                "entry_sha256"
            ]
            != expected
        ):
            raise legacy.StateError(
                f"idempotent migration destination state mismatch: "
                f"{destination_root / destination_name}"
            )


def _require_same_filesystem(source_info: os.stat_result, destination_descriptor: int) -> None:
    if source_info.st_dev != os.fstat(destination_descriptor).st_dev:
        raise legacy.StateError("migration requires same-filesystem atomic rename")


def _rollback_moved_entries(
    moved: list[dict[str, Any]],
    *,
    cause: Exception,
    source_descriptor: int,
    destination_descriptor: int,
) -> None:
    rollback: list[dict[str, Any]] = []
    for item in reversed(moved):
        source_name = Path(item["source"]).name
        destination_name = Path(item["destination"]).name
        restored = False
        error = None
        try:
            source_info = _lstat_at(source_descriptor, source_name)
            destination_info = _lstat_at(destination_descriptor, destination_name)
            effect_device = item.get("effect_device")
            effect_inode = item.get("effect_inode")
            if (
                source_info is None
                and destination_info is not None
                and not stat.S_ISLNK(destination_info.st_mode)
                and isinstance(effect_device, int)
                and isinstance(effect_inode, int)
                and int(destination_info.st_dev) == effect_device
                and int(destination_info.st_ino) == effect_inode
            ):
                _rename_at(
                    destination_descriptor,
                    destination_name,
                    source_descriptor,
                    source_name,
                )
                restored = True
        except Exception as rollback_exc:  # pragma: no cover
            error = f"{type(rollback_exc).__name__}: {rollback_exc}"
        rollback.append(
            {
                "source": item["destination"],
                "destination": item["source"],
                "entry_sha256": item["entry_sha256"],
                "restored": restored,
                "error": error,
            }
        )
    if rollback and not all(item["restored"] for item in rollback):
        raise legacy.StateError(
            f"state-root migration failed and rollback was incomplete: {cause}; "
            + legacy.canonical_json(rollback)
        ) from cause


def _recover_failed_effect(
    *,
    moved: list[dict[str, Any]],
    cause: Exception,
    source_descriptor: int,
    destination_descriptor: int,
    created: list[dict[str, Any]] | None = None,
) -> None:
    failures: list[str] = []
    try:
        _rollback_moved_entries(
            moved,
            cause=cause,
            source_descriptor=source_descriptor,
            destination_descriptor=destination_descriptor,
        )
    except Exception as exc:
        failures.append(f"compensation: {type(exc).__name__}: {exc}")
    if created:
        try:
            _cleanup_created_destination_chain(created, cause=cause)
        except Exception as exc:
            failures.append(f"destination cleanup: {type(exc).__name__}: {exc}")
    if failures:
        raise legacy.StateError(
            f"state-root mutation failed with incomplete recovery: {cause}; " + "; ".join(failures)
        ) from cause


def _assert_receipt_paths_current(
    *,
    descriptors: dict[str, int],
    anchors: dict[str, Any],
    destination_descriptor: int,
) -> None:
    _assert_plan_directory_paths_current(descriptors, anchors)
    _assert_bound_directory_path(
        destination_descriptor,
        anchors["destination_root"],
        role="destination-root",
    )


def apply_state_root_migration_plan(path: Path) -> dict[str, Any]:
    plan_path, plan, reviewed_plan_sha256 = _load_reviewed_migration_plan(path)
    (
        root,
        destination_root,
        reference_root,
        destination_base,
        destination_components,
        anchors,
    ) = _validate_migration_paths(plan)
    receipt_path = _receipt_path(plan_path)

    if receipt_path.exists():
        receipt = _validated_apply_receipt(
            legacy.read_json(receipt_path),
            reviewed_plan_sha256=reviewed_plan_sha256,
            plan=plan,
        )
        receipt_anchors = _receipt_directory_anchors(
            receipt,
            root=root,
            destination=destination_root,
            reference=reference_root,
            destination_base=destination_base,
        )
        with ExitStack() as stack:
            descriptors = _open_plan_directories(
                stack,
                root=root,
                reference=reference_root,
                destination_base=destination_base,
                anchors=receipt_anchors,
            )
            destination_descriptor = stack.enter_context(
                _open_bound_directory(
                    receipt_anchors["destination_root"],
                    expected_path=destination_root,
                    role="destination-root",
                )
            )
            _assert_receipt_paths_current(
                descriptors=descriptors,
                anchors=receipt_anchors,
                destination_descriptor=destination_descriptor,
            )
            _verify_applied_receipt_state(
                receipt,
                state_root=root,
                destination_root=destination_root,
                state_descriptor=descriptors["state_root"],
                destination_descriptor=destination_descriptor,
            )
        return {**receipt, "receipt_path": str(receipt_path), "idempotent_rerun": True}

    with ExitStack() as stack:
        descriptors = _open_plan_directories(
            stack,
            root=root,
            reference=reference_root,
            destination_base=destination_base,
            anchors=anchors,
        )
        _assert_plan_directory_paths_current(descriptors, anchors)
        destination_descriptor, created = _create_destination_chain(
            stack,
            base_descriptor=descriptors["destination_base"],
            base_path=destination_base,
            components=destination_components,
        )
        destination_root_anchor = created[-1]["anchor"]
        _assert_plan_directory_paths_current(descriptors, anchors)
        _assert_created_destination_paths_current(created)
        unexpected = sorted(os.listdir(destination_descriptor))
        if unexpected:
            raise legacy.StateError(
                "new migration destination unexpectedly contains entries: " + ", ".join(unexpected)
            )

        moved_this_run: list[dict[str, Any]] = []
        applied: list[dict[str, Any]] = []
        try:
            planned_entries = plan["entries"]
            prepared: list[dict[str, Any]] = []
            for expected in planned_entries:
                if not isinstance(expected, dict):
                    raise legacy.StateError("migration plan entry is invalid")
                source_name = _entry_name_from_bound_path(
                    expected.get("source"), root, role="migration source"
                )
                destination_name = _entry_name_from_bound_path(
                    expected.get("destination"),
                    destination_root,
                    role="migration destination",
                )
                if source_name != destination_name or source_name != expected.get("name"):
                    raise legacy.StateError("migration entry name binding is invalid")
                source_info = _lstat_at(descriptors["state_root"], source_name)
                if source_info is None or stat.S_ISLNK(source_info.st_mode):
                    raise legacy.StateError(
                        f"migration source is missing or symlinked: {root / source_name}"
                    )
                if _lstat_at(destination_descriptor, destination_name) is not None:
                    raise legacy.StateError(
                        f"migration destination collision: {destination_root / destination_name}"
                    )
                current = _entry_identity(_descriptor_path(descriptors["state_root"], source_name))
                if current["entry_sha256"] != expected["entry_sha256"]:
                    raise legacy.StateError(
                        f"migration source changed since review: {root / source_name}"
                    )
                references = _reference_hits(
                    _descriptor_path(descriptors["reference_root"]),
                    root / source_name,
                )
                processes = _process_references(
                    _descriptor_path(descriptors["state_root"], source_name)
                )
                if references or processes:
                    raise legacy.StateError(
                        f"migration source gained references: {root / source_name}"
                    )
                _require_same_filesystem(source_info, destination_descriptor)
                prepared.append(
                    {
                        "source": str(root / source_name),
                        "destination": str(destination_root / destination_name),
                        "source_name": source_name,
                        "destination_name": destination_name,
                        "entry_sha256": expected["entry_sha256"],
                    }
                )

            for item in prepared:
                if _file_sha256(plan_path) != reviewed_plan_sha256:
                    raise legacy.StateError("reviewed migration plan changed before effect")
                _assert_plan_directory_paths_current(descriptors, anchors)
                _assert_created_destination_paths_current(created)
                source_info = _lstat_at(descriptors["state_root"], item["source_name"])
                if source_info is None or stat.S_ISLNK(source_info.st_mode):
                    raise legacy.StateError(
                        f"migration source changed before effect: {item['source']}"
                    )
                if _lstat_at(destination_descriptor, item["destination_name"]) is not None:
                    raise legacy.StateError(
                        f"migration destination gained a collision: {item['destination']}"
                    )
                if (
                    _entry_identity(
                        _descriptor_path(descriptors["state_root"], item["source_name"])
                    )["entry_sha256"]
                    != item["entry_sha256"]
                ):
                    raise legacy.StateError(
                        f"migration source changed before effect: {item['source']}"
                    )
                references = _reference_hits(
                    _descriptor_path(descriptors["reference_root"]),
                    Path(item["source"]),
                )
                processes = _process_references(
                    _descriptor_path(descriptors["state_root"], item["source_name"])
                )
                if references or processes:
                    raise legacy.StateError(f"migration source gained references: {item['source']}")
                _require_same_filesystem(source_info, destination_descriptor)
                _rename_at(
                    descriptors["state_root"],
                    item["source_name"],
                    destination_descriptor,
                    item["destination_name"],
                )
                effect_info = _lstat_at(destination_descriptor, item["destination_name"])
                if effect_info is None or stat.S_ISLNK(effect_info.st_mode):
                    raise legacy.StateError(
                        f"migration effect disappeared or became symlinked: {item['destination']}"
                    )
                moved = {
                    "source": item["source"],
                    "destination": item["destination"],
                    "entry_sha256": item["entry_sha256"],
                    "effect_device": int(effect_info.st_dev),
                    "effect_inode": int(effect_info.st_ino),
                    "status": "moved",
                }
                moved_this_run.append(moved)
                applied.append(
                    {
                        key: value
                        for key, value in moved.items()
                        if key not in {"effect_device", "effect_inode"}
                    }
                )
                _assert_plan_directory_paths_current(descriptors, anchors)
                _assert_created_destination_paths_current(created)
                if (
                    _entry_identity(
                        _descriptor_path(destination_descriptor, item["destination_name"])
                    )["entry_sha256"]
                    != item["entry_sha256"]
                ):
                    raise legacy.StateError(
                        f"migration destination identity mismatch after effect: "
                        f"{item['destination']}"
                    )
            if _file_sha256(plan_path) != reviewed_plan_sha256:
                raise legacy.StateError("reviewed migration plan changed during effect")
            _assert_plan_directory_paths_current(descriptors, anchors)
            _assert_created_destination_paths_current(created)
            receipt_anchors = {
                **anchors,
                "destination_root": destination_root_anchor,
            }
            receipt = {
                "schema_version": MIGRATION_RECEIPT_SCHEMA_VERSION,
                "command": MIGRATION_APPLY_COMMAND,
                "applied_at": legacy.utc_now(),
                "reviewed_plan_path": str(plan_path),
                "reviewed_plan_sha256": reviewed_plan_sha256,
                "state_root": str(root),
                "destination_root": str(destination_root),
                "reference_root": str(reference_root),
                "destination_layout": plan["destination_layout"],
                "platform_contract": plan["platform_contract"],
                "directory_anchors": receipt_anchors,
                "entries_sha256": plan["entries_sha256"],
                "entries": applied,
                "rollback": [
                    {
                        "source": item["destination"],
                        "destination": item["source"],
                        "entry_sha256": item["entry_sha256"],
                    }
                    for item in reversed(applied)
                ],
                "approval": plan.get("approval"),
                "does_not_establish": [
                    "permission_to_delete_migrated_artifacts",
                    "artifact_obsolescence",
                    "future_state_root_health",
                    "task_completion",
                    "protection_against_final_entry_replacement_between_last_stat_and_renameat",
                    "crash_or_power_loss_atomicity_without_a_durable_recovery_journal",
                ],
            }
            receipt["receipt_sha256"] = legacy.sha256_json(receipt)
            _write_create_only(receipt_path, legacy.canonical_json(receipt) + "\n")
        except Exception as exc:
            _recover_failed_effect(
                moved=moved_this_run,
                cause=exc,
                source_descriptor=descriptors["state_root"],
                destination_descriptor=destination_descriptor,
                created=created,
            )
            raise
    return {**receipt, "receipt_path": str(receipt_path), "idempotent_rerun": False}


def rollback_state_root_migration(receipt_path: Path) -> dict[str, Any]:
    _require_descriptor_relative_mutation()
    target = receipt_path.expanduser().resolve(strict=True)
    receipt = legacy.read_json(target)
    claimed = receipt.get("receipt_sha256")
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if (
        receipt.get("schema_version") != MIGRATION_RECEIPT_SCHEMA_VERSION
        or receipt.get("command") != MIGRATION_APPLY_COMMAND
        or re.fullmatch(r"[0-9a-f]{64}", str(claimed or "")) is None
        or legacy.sha256_json(unsigned) != claimed
    ):
        raise legacy.StateError("migration receipt integrity is invalid or unsupported")
    actions = receipt.get("rollback")
    if not isinstance(actions, list) or not actions:
        raise legacy.StateError("migration receipt has no rollback actions")

    root = _absolute_lexical_path(Path(str(receipt.get("state_root", ""))))
    destination_root = _absolute_lexical_path(Path(str(receipt.get("destination_root", ""))))
    reference_root = _absolute_lexical_path(Path(str(receipt.get("reference_root", ""))))
    layout = receipt.get("destination_layout")
    if not isinstance(layout, dict):
        raise legacy.StateError("migration receipt destination layout is invalid")
    destination_base = _absolute_lexical_path(Path(str(layout.get("base_path", ""))))
    anchors = _receipt_directory_anchors(
        receipt,
        root=root,
        destination=destination_root,
        reference=reference_root,
        destination_base=destination_base,
    )

    with ExitStack() as stack:
        descriptors = _open_plan_directories(
            stack,
            root=root,
            reference=reference_root,
            destination_base=destination_base,
            anchors=anchors,
        )
        destination_descriptor = stack.enter_context(
            _open_bound_directory(
                anchors["destination_root"],
                expected_path=destination_root,
                role="destination-root",
            )
        )
        _assert_receipt_paths_current(
            descriptors=descriptors,
            anchors=anchors,
            destination_descriptor=destination_descriptor,
        )

        planned: list[dict[str, Any]] = []
        source_paths: set[str] = set()
        destination_paths: set[str] = set()
        for action in actions:
            if not isinstance(action, dict):
                raise legacy.StateError("migration rollback action is invalid")
            source_name = _entry_name_from_bound_path(
                action.get("source"), destination_root, role="rollback source"
            )
            destination_name = _entry_name_from_bound_path(
                action.get("destination"), root, role="rollback destination"
            )
            expected = str(action.get("entry_sha256", ""))
            source_key = str(destination_root / source_name)
            destination_key = str(root / destination_name)
            if (
                source_key in source_paths
                or destination_key in destination_paths
                or source_name != destination_name
                or re.fullmatch(r"[0-9a-f]{64}", expected) is None
            ):
                raise legacy.StateError("migration rollback action binding is invalid")
            source_paths.add(source_key)
            destination_paths.add(destination_key)

            source_info = _lstat_at(destination_descriptor, source_name)
            destination_info = _lstat_at(descriptors["state_root"], destination_name)
            if destination_info is not None:
                if (
                    source_info is None
                    and not stat.S_ISLNK(destination_info.st_mode)
                    and _entry_identity(
                        _descriptor_path(descriptors["state_root"], destination_name)
                    )["entry_sha256"]
                    == expected
                ):
                    planned.append(
                        {
                            "source": source_key,
                            "destination": destination_key,
                            "source_name": source_name,
                            "destination_name": destination_name,
                            "entry_sha256": expected,
                            "status": "already-restored",
                        }
                    )
                    continue
                raise legacy.StateError(f"rollback destination collision: {destination_key}")
            if source_info is None or stat.S_ISLNK(source_info.st_mode):
                raise legacy.StateError(f"rollback source is missing or symlinked: {source_key}")
            if (
                _entry_identity(_descriptor_path(destination_descriptor, source_name))[
                    "entry_sha256"
                ]
                != expected
            ):
                raise legacy.StateError(f"rollback source identity mismatch: {source_key}")
            references = _reference_hits(
                _descriptor_path(descriptors["reference_root"]),
                Path(source_key),
            )
            processes = _process_references(_descriptor_path(destination_descriptor, source_name))
            if references or processes:
                raise legacy.StateError(f"rollback source gained references: {source_key}")
            _require_same_filesystem(source_info, descriptors["state_root"])
            planned.append(
                {
                    "source": source_key,
                    "destination": destination_key,
                    "source_name": source_name,
                    "destination_name": destination_name,
                    "entry_sha256": expected,
                    "status": "ready",
                }
            )

        restored: list[dict[str, Any]] = []
        moved_this_run: list[dict[str, Any]] = []
        try:
            for item in planned:
                if item["status"] == "already-restored":
                    restored.append(
                        {
                            "source": item["source"],
                            "destination": item["destination"],
                            "entry_sha256": item["entry_sha256"],
                            "status": "already-restored",
                        }
                    )
                    continue
                _assert_receipt_paths_current(
                    descriptors=descriptors,
                    anchors=anchors,
                    destination_descriptor=destination_descriptor,
                )
                source_info = _lstat_at(destination_descriptor, item["source_name"])
                if source_info is None or stat.S_ISLNK(source_info.st_mode):
                    raise legacy.StateError(
                        f"rollback source changed before effect: {item['source']}"
                    )
                if _lstat_at(descriptors["state_root"], item["destination_name"]) is not None:
                    raise legacy.StateError(
                        f"rollback destination gained a collision: {item['destination']}"
                    )
                if (
                    _entry_identity(_descriptor_path(destination_descriptor, item["source_name"]))[
                        "entry_sha256"
                    ]
                    != item["entry_sha256"]
                ):
                    raise legacy.StateError(
                        f"rollback source changed before effect: {item['source']}"
                    )
                _require_same_filesystem(source_info, descriptors["state_root"])
                _rename_at(
                    destination_descriptor,
                    item["source_name"],
                    descriptors["state_root"],
                    item["destination_name"],
                )
                effect_info = _lstat_at(descriptors["state_root"], item["destination_name"])
                if effect_info is None or stat.S_ISLNK(effect_info.st_mode):
                    raise legacy.StateError(
                        f"rollback effect disappeared or became symlinked: {item['destination']}"
                    )
                moved = {
                    "source": item["source"],
                    "destination": item["destination"],
                    "entry_sha256": item["entry_sha256"],
                    "effect_device": int(effect_info.st_dev),
                    "effect_inode": int(effect_info.st_ino),
                    "status": "restored",
                }
                moved_this_run.append(moved)
                restored.append(
                    {
                        key: value
                        for key, value in moved.items()
                        if key not in {"effect_device", "effect_inode"}
                    }
                )
                _assert_receipt_paths_current(
                    descriptors=descriptors,
                    anchors=anchors,
                    destination_descriptor=destination_descriptor,
                )
                if (
                    _entry_identity(
                        _descriptor_path(descriptors["state_root"], item["destination_name"])
                    )["entry_sha256"]
                    != item["entry_sha256"]
                ):
                    raise legacy.StateError(
                        f"rollback destination identity mismatch after effect: "
                        f"{item['destination']}"
                    )
        except Exception as exc:
            _recover_failed_effect(
                moved=moved_this_run,
                cause=exc,
                source_descriptor=destination_descriptor,
                destination_descriptor=descriptors["state_root"],
            )
            raise

    return {
        "schema_version": 2,
        "command": MIGRATION_ROLLBACK_COMMAND,
        "rolled_back_at": legacy.utc_now(),
        "receipt_path": str(target),
        "receipt_sha256": claimed,
        "entries": restored,
        "does_not_establish": [
            "permission_to_delete_receipt_or_quarantine",
            "future_state_root_health",
            "protection_against_final_entry_replacement_between_last_stat_and_renameat",
            "crash_or_power_loss_atomicity_without_a_durable_recovery_journal",
        ],
    }
