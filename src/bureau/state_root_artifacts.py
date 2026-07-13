"""Strict inventory and reviewed migration for Bureau state-root artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import legacy
from .approval import require_approval, reviewed_plan_approval

INVENTORY_SCHEMA_VERSION = 1
MIGRATION_PLAN_SCHEMA_VERSION = 1
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


def _regular_file_metadata(path: Path, *, maximum_bytes: int) -> dict[str, Any]:
    try:
        info = path.lstat()
    except OSError as exc:
        raise legacy.StateError(
            f"cannot inspect {path}: {type(exc).__name__}: {exc}"
        ) from exc
    if stat.S_ISLNK(info.st_mode):
        raise legacy.StateError(f"symlink artifact is forbidden: {path}")
    if not stat.S_ISREG(info.st_mode):
        raise legacy.StateError(f"regular file required: {path}")
    if info.st_size > maximum_bytes:
        raise legacy.StateError(
            f"artifact exceeds {maximum_bytes} bytes: {path} ({info.st_size})"
        )
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise legacy.StateError(
            f"cannot hash {path}: {type(exc).__name__}: {exc}"
        ) from exc
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
        raise legacy.StateError(
            f"invalid JSON artifact {path}: {type(exc).__name__}"
        ) from exc
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
            raise legacy.StateError(
                "bundle must contain exactly pr.diff and self-review.json"
            )
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
        unsigned_review = {
            key: value for key, value in review.items() if key != "review_sha256"
        }
        axes = review.get("axes")
        checks = [
            (review.get("schema_version") == 1, "unsupported review schema"),
            (review.get("kind") == "bureau_pr_self_review", "unsupported review kind"),
            (review.get("conclusion") == "PASS", "review conclusion is not PASS"),
            (
                isinstance(review.get("repository"), str)
                and bool(review["repository"].strip()),
                "repository identity is missing",
            ),
            (
                isinstance(review.get("pull_request"), int)
                and review["pull_request"] > 0,
                "pull request identity is invalid",
            ),
            (
                re.fullmatch(r"[0-9a-f]{40}", str(review.get("reviewed_head", "")))
                is not None,
                "reviewed head is invalid",
            ),
            (
                re.fullmatch(r"[0-9a-f]{40}", str(review.get("base_head", "")))
                is not None,
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
                re.fullmatch(r"[0-9a-f]{64}", str(claimed_review_sha256 or ""))
                is not None
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
                        not isinstance(item, str)
                        or not item.strip()
                        or len(item) > 1000
                        for item in evidence
                    )
                ):
                    result["reasons"].append(
                        f"review axis evidence is invalid: {axis_name}"
                    )
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
            f"{item['name']}: {reason}"
            for item in result["children"]
            for reason in item["reasons"]
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
        result["children"] = [
            _reviewed_plan_inventory(path, observed_at) for path in plans
        ]
        result["reasons"] = [
            f"{item['name']}: {reason}"
            for item in result["children"]
            for reason in item["reasons"]
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
        metadata = _regular_file_metadata(
            path, maximum_bytes=_MIGRATION_MAX_TOTAL_BYTES_PER_ENTRY
        )
        return {
            "name": path.name,
            "type": "file",
            **metadata,
            "entry_sha256": legacy.sha256_json(
                {
                    "name": path.name,
                    "type": "file",
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
                "path": (
                    "." if not relative_directory.parts else relative_directory.as_posix()
                ),
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
        "records": records,
    }
    return {
        "name": path.name,
        "type": "directory",
        "mode": oct(stat.S_IMODE(root_info.st_mode)),
        "mtime_ns": root_info.st_mtime_ns,
        "source_mtime": _utc_from_ns(root_info.st_mtime_ns),
        "file_count": file_count,
        "total_bytes": total_bytes,
        "tree_sha256": legacy.sha256_json(records),
        "entry_sha256": legacy.sha256_json(identity_payload),
    }


def _reference_hits(reference_root: Path, source: Path) -> list[dict[str, Any]]:
    root = reference_root.expanduser().resolve(strict=True)
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
                raise legacy.StateError(
                    f"reference scan failed: {candidate}: {exc}"
                ) from exc
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


def _process_references(path: Path) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    proc = Path("/proc")
    for process in proc.iterdir():
        if not process.name.isdigit():
            continue
        pid = int(process.name)
        try:
            cwd = (process / "cwd").resolve(strict=True)
        except (OSError, RuntimeError):
            cwd = None
        if cwd is not None and (cwd == path or _path_is_within(cwd, path)):
            references.append({"pid": pid, "kind": "cwd", "path": str(cwd)})
        fd_root = process / "fd"
        try:
            descriptors = list(fd_root.iterdir())
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                resolved = descriptor.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if resolved == path or _path_is_within(resolved, path):
                references.append(
                    {
                        "pid": pid,
                        "kind": "fd",
                        "fd": descriptor.name,
                        "path": str(resolved),
                    }
                )
        if len(references) >= 100:
            break
    return references


def state_root_migration_plan(
    state_root: Path,
    entry_names: list[str],
    destination_root: Path,
    *,
    reference_root: Path,
) -> dict[str, Any]:
    root = state_root.expanduser().resolve(strict=True)
    destination = destination_root.expanduser().resolve(strict=False)
    if destination.exists() or destination.is_symlink():
        raise legacy.StateError("migration destination must not exist")
    if (
        destination == root
        or _path_is_within(destination, root)
        or _path_is_within(root, destination)
    ):
        raise legacy.StateError("migration destination must not overlap the state root")
    names = sorted(set(entry_names))
    if not names or len(names) > _MIGRATION_MAX_ENTRIES:
        raise legacy.StateError("migration entry count is invalid")
    entries: list[dict[str, Any]] = []
    for name in names:
        if _MIGRATION_ENTRY_RE.fullmatch(name) is None:
            raise legacy.StateError(f"invalid top-level migration entry name: {name}")
        source = root / name
        if not source.exists() and not source.is_symlink():
            raise legacy.StateError(f"migration source is missing: {source}")
        identity = _entry_identity(source)
        references = _reference_hits(reference_root, source)
        processes = _process_references(source)
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
    return {
        "schema_version": MIGRATION_PLAN_SCHEMA_VERSION,
        "command": MIGRATION_PLAN_COMMAND,
        "created_at": legacy.utc_now(),
        "state_root": str(root),
        "destination_root": str(destination),
        "reference_root": str(reference_root.expanduser().resolve(strict=True)),
        "entries": entries,
        "entries_sha256": entries_sha256,
        "review": {
            "required": True,
            "status": "pending",
            "instructions": (
                "Review every source/destination and digest. To apply, set status to "
                "reviewed, add reviewer and reviewed_at, and copy entries_sha256 plus "
                "destination_root into this review object."
            ),
        },
        "execution_preconditions": [
            "Destination root remains absent before the first effect.",
            "Every source identity and the reviewed plan file remain unchanged.",
            "No source has a repository reference or active process reference.",
            "Apply uses same-filesystem atomic rename and rolls back this run on failure.",
        ],
        "does_not_establish": [
            "artifact_obsolescence",
            "permission_to_delete",
            "content_authority",
            "future_reference_or_process_absence",
            "cleanup_authority_without_review",
        ],
    }


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
        raise legacy.StateError(
            "state-root migration plan has unsupported schema or command"
        )
    review = plan.get("review")
    if not isinstance(review, dict) or review.get("status") != "reviewed":
        raise legacy.StateError("state-root migration plan is not reviewed")
    if not review.get("reviewer") or not review.get("reviewed_at"):
        raise legacy.StateError(
            "reviewed migration plan requires reviewer and reviewed_at"
        )
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


def _validate_migration_paths(plan: dict[str, Any]) -> tuple[Path, Path]:
    root = Path(str(plan.get("state_root", ""))).expanduser().resolve(strict=True)
    destination = Path(str(plan.get("destination_root", ""))).expanduser().resolve(
        strict=False
    )
    if (
        destination == root
        or _path_is_within(destination, root)
        or _path_is_within(root, destination)
    ):
        raise legacy.StateError("migration destination overlaps the state root")
    return root, destination


def _validated_apply_receipt(
    receipt: dict[str, Any],
    *,
    reviewed_plan_sha256: str,
    plan: dict[str, Any],
) -> dict[str, Any]:
    claimed_receipt_sha256 = receipt.get("receipt_sha256")
    unsigned_receipt = {
        key: value for key, value in receipt.items() if key != "receipt_sha256"
    }
    entries = receipt.get("entries")
    if (
        receipt.get("command") != MIGRATION_APPLY_COMMAND
        or receipt.get("reviewed_plan_sha256") != reviewed_plan_sha256
        or receipt.get("entries_sha256") != plan.get("entries_sha256")
        or receipt.get("state_root") != plan.get("state_root")
        or receipt.get("destination_root") != plan.get("destination_root")
        or not isinstance(entries, list)
        or len(entries) != len(plan.get("entries", []))
        or re.fullmatch(r"[0-9a-f]{64}", str(claimed_receipt_sha256 or ""))
        is None
        or legacy.sha256_json(unsigned_receipt) != claimed_receipt_sha256
    ):
        raise legacy.StateError("existing migration receipt does not match the plan")
    return receipt


def _verify_applied_receipt_state(receipt: dict[str, Any]) -> None:
    entries = receipt.get("entries")
    if not isinstance(entries, list):
        raise legacy.StateError("migration receipt entries are invalid")
    for item in entries:
        if not isinstance(item, dict):
            raise legacy.StateError("migration receipt entry is invalid")
        source = Path(str(item.get("source", "")))
        destination = Path(str(item.get("destination", "")))
        expected = str(item.get("entry_sha256", ""))
        if source.exists() or source.is_symlink():
            raise legacy.StateError(
                f"idempotent migration source unexpectedly exists: {source}"
            )
        if not destination.exists() or destination.is_symlink():
            raise legacy.StateError(
                f"idempotent migration destination is missing or symlinked: {destination}"
            )
        if _entry_identity(destination)["entry_sha256"] != expected:
            raise legacy.StateError(
                f"idempotent migration destination state mismatch: {destination}"
            )


def _rollback_moved_entries(
    moved: list[dict[str, Any]],
    *,
    cause: Exception,
) -> None:
    rollback: list[dict[str, Any]] = []
    for item in reversed(moved):
        source = Path(item["source"])
        destination = Path(item["destination"])
        restored = False
        error = None
        try:
            if (
                not source.exists()
                and destination.exists()
                and not destination.is_symlink()
                and _entry_identity(destination)["entry_sha256"]
                == item["entry_sha256"]
            ):
                destination.rename(source)
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


def apply_state_root_migration_plan(path: Path) -> dict[str, Any]:
    plan_path, plan, reviewed_plan_sha256 = _load_reviewed_migration_plan(path)
    root, destination_root = _validate_migration_paths(plan)
    receipt_path = _receipt_path(plan_path)
    if receipt_path.exists():
        receipt = _validated_apply_receipt(
            legacy.read_json(receipt_path),
            reviewed_plan_sha256=reviewed_plan_sha256,
            plan=plan,
        )
        _verify_applied_receipt_state(receipt)
        return {**receipt, "receipt_path": str(receipt_path), "idempotent_rerun": True}
    if destination_root.exists() and not destination_root.is_dir():
        raise legacy.StateError("migration destination is not a directory")
    if destination_root.is_symlink():
        raise legacy.StateError("migration destination symlink is forbidden")
    if not destination_root.exists():
        destination_root.mkdir(parents=True, mode=0o700)
    planned_entries = plan["entries"]
    destination_names = {Path(item["destination"]).name for item in planned_entries}
    unexpected = sorted(
        item.name for item in destination_root.iterdir() if item.name not in destination_names
    )
    if unexpected:
        raise legacy.StateError(
            "migration destination contains unexpected entries: " + ", ".join(unexpected)
        )
    moved_this_run: list[dict[str, Any]] = []
    applied: list[dict[str, Any]] = []
    try:
        for expected in planned_entries:
            if _file_sha256(plan_path) != reviewed_plan_sha256:
                raise legacy.StateError("reviewed migration plan changed before effect")
            source = Path(expected["source"])
            destination = Path(expected["destination"])
            if source.parent.resolve(strict=True) != root:
                raise legacy.StateError("migration source escaped the state root")
            if destination.parent.resolve(strict=True) != destination_root:
                raise legacy.StateError("migration destination escaped its root")
            source_exists = source.exists() or source.is_symlink()
            destination_exists = destination.exists() or destination.is_symlink()
            if source_exists and destination_exists:
                raise legacy.StateError(f"migration collision: {source} and {destination}")
            if not source_exists and not destination_exists:
                raise legacy.StateError(
                    f"migration source and destination are both missing: {source}"
                )
            if destination_exists:
                current = _entry_identity(destination)
                if current["entry_sha256"] != expected["entry_sha256"]:
                    raise legacy.StateError(
                        f"existing destination identity mismatch: {destination}"
                    )
                applied.append(
                    {
                        "source": str(source),
                        "destination": str(destination),
                        "entry_sha256": expected["entry_sha256"],
                        "status": "already-applied",
                    }
                )
                continue
            current = _entry_identity(source)
            if current["entry_sha256"] != expected["entry_sha256"]:
                raise legacy.StateError(f"migration source changed since review: {source}")
            references = _reference_hits(Path(plan["reference_root"]), source)
            processes = _process_references(source)
            if references or processes:
                raise legacy.StateError(f"migration source gained references: {source}")
            if source.stat().st_dev != destination_root.stat().st_dev:
                raise legacy.StateError("migration requires same-filesystem atomic rename")
            source.rename(destination)
            moved = {
                "source": str(source),
                "destination": str(destination),
                "entry_sha256": expected["entry_sha256"],
                "status": "moved",
            }
            moved_this_run.append(moved)
            applied.append(moved)
        if _file_sha256(plan_path) != reviewed_plan_sha256:
            raise legacy.StateError("reviewed migration plan changed during effect")
        receipt = {
            "schema_version": 1,
            "command": MIGRATION_APPLY_COMMAND,
            "applied_at": legacy.utc_now(),
            "reviewed_plan_path": str(plan_path),
            "reviewed_plan_sha256": reviewed_plan_sha256,
            "state_root": str(root),
            "destination_root": str(destination_root),
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
            ],
        }
        receipt["receipt_sha256"] = legacy.sha256_json(receipt)
        _write_create_only(receipt_path, legacy.canonical_json(receipt) + "\n")
    except Exception as exc:
        _rollback_moved_entries(moved_this_run, cause=exc)
        raise
    return {**receipt, "receipt_path": str(receipt_path), "idempotent_rerun": False}

def rollback_state_root_migration(receipt_path: Path) -> dict[str, Any]:
    target = receipt_path.expanduser().resolve(strict=True)
    receipt = legacy.read_json(target)
    claimed = receipt.get("receipt_sha256")
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if (
        receipt.get("command") != MIGRATION_APPLY_COMMAND
        or re.fullmatch(r"[0-9a-f]{64}", str(claimed or "")) is None
        or legacy.sha256_json(unsigned) != claimed
    ):
        raise legacy.StateError("migration receipt integrity is invalid")
    actions = receipt.get("rollback")
    if not isinstance(actions, list) or not actions:
        raise legacy.StateError("migration receipt has no rollback actions")
    restored: list[dict[str, Any]] = []
    for action in actions:
        source = Path(str(action.get("source", "")))
        destination = Path(str(action.get("destination", "")))
        expected = str(action.get("entry_sha256", ""))
        if destination.exists() or destination.is_symlink():
            if not source.exists() and _entry_identity(destination)["entry_sha256"] == expected:
                restored.append(
                    {
                        "source": str(source),
                        "destination": str(destination),
                        "entry_sha256": expected,
                        "status": "already-restored",
                    }
                )
                continue
            raise legacy.StateError(f"rollback destination collision: {destination}")
        if not source.exists() or source.is_symlink():
            raise legacy.StateError(f"rollback source is missing or symlinked: {source}")
        current = _entry_identity(source)
        if current["entry_sha256"] != expected:
            raise legacy.StateError(f"rollback source identity mismatch: {source}")
        source.rename(destination)
        restored.append(
            {
                "source": str(source),
                "destination": str(destination),
                "entry_sha256": expected,
                "status": "restored",
            }
        )
    return {
        "schema_version": 1,
        "command": MIGRATION_ROLLBACK_COMMAND,
        "rolled_back_at": legacy.utc_now(),
        "receipt_path": str(target),
        "receipt_sha256": claimed,
        "entries": restored,
        "does_not_establish": [
            "permission_to_delete_receipt_or_quarantine",
            "future_state_root_health",
        ],
    }
