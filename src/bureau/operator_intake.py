from __future__ import annotations

import contextlib
import copy
import ctypes
import errno
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from . import legacy, task_specs
from .acceptance import AcceptanceContractError
from .approval import (
    approval_decision,
    require_approval,
    reviewed_plan_approval,
    task_approval_contract,
)
from .core import Registry, StateError, StateStore
from .live_register import (
    ACTIVE_LIVE_STATUSES,
    CANDIDATE_EVENT_SCHEMA_VERSION,
    candidate_authority_nonclaims,
    candidate_content_fingerprint,
    candidate_event_id,
    candidate_id_for_content,
    candidate_records,
    candidate_source_fingerprint,
    current_candidate_record,
    current_candidate_records,
    live_register_record,
)
from .runtime_identity import bureau_runtime_identity
from .runtime_refresh import (
    DEFAULT_GRABOWSKI_RESOURCE_DB,
    RuntimeRefreshError,
    validate_live_lease_binding,
)
from .schema_validation import DocumentSchemaError
from .v2 import _task_from_authoritative_spec

OPERATOR_INTAKE_SCHEMA_VERSION = 1
MAX_SIMILARITY_RESULTS = 5
MAX_SOURCE_RELATIONSHIPS = 20
_SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_GITHUB_SLUG_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_GENERIC_ACCEPTANCE_IDS = {"source-event-bound", "reviewed-before-effect"}
PUBLICATION_PHASES = (
    "before_workspace",
    "local_workspace",
    "committed_locally",
    "push_attempted",
    "push_confirmed",
    "pr_attempted",
    "pr_confirmed",
)
_REMOTE_EFFECT_PHASES = {
    "push_attempted",
    "push_confirmed",
    "pr_attempted",
    "pr_confirmed",
}
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_RENAME_EXCHANGE = 2
MAX_PROPOSAL_BYTES = 4 * 1024 * 1024
_CANDIDATE_RECORD_REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "idempotency_key",
        "title",
        "source_kind",
        "desired_outcome",
        "repo",
        "source_locator",
        "source_sha256",
        "observed_at",
        "task_id",
        "candidate_id",
        "supersedes_event_id",
        "note",
        "catalog_validation",
    }
)
_CANDIDATE_RECORD_REQUEST_REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "idempotency_key",
        "title",
        "source_kind",
        "desired_outcome",
    }
)
_CANDIDATE_CLOSE_REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "operation",
        "idempotency_key",
        "candidate_id",
        "expected_event_id",
        "outcome",
        "evidence",
        "note",
    }
)
_CANDIDATE_CLOSE_REQUEST_REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "operation",
        "idempotency_key",
        "candidate_id",
        "expected_event_id",
        "outcome",
        "evidence",
    }
)
_CANDIDATE_CLOSE_EVIDENCE_SOURCES = frozenset(
    {"receipt", "test", "git", "github", "runtime", "bureau", "workspace", "job", "user"}
)
MAX_CANDIDATE_CLOSE_EVIDENCE = 16


def candidate_record_request_contract() -> dict[str, Any]:
    """Return the canonical read-only transport contract for candidate recording."""
    allowed_fields = sorted(_CANDIDATE_RECORD_REQUEST_FIELDS)
    required_fields = sorted(_CANDIDATE_RECORD_REQUEST_REQUIRED_FIELDS)
    optional_fields = sorted(
        _CANDIDATE_RECORD_REQUEST_FIELDS - _CANDIDATE_RECORD_REQUEST_REQUIRED_FIELDS
    )
    return {
        "schema_version": 1,
        "kind": "bureau_candidate_record_request_contract",
        "request_schema_version": OPERATOR_INTAKE_SCHEMA_VERSION,
        "allowed_fields": allowed_fields,
        "required_fields": required_fields,
        "optional_fields": optional_fields,
        "defaults": {"catalog_validation": "strict"},
        "operations": {
            "record": {
                "operation": "omitted-or-record",
                "allowed_fields": allowed_fields,
                "required_fields": required_fields,
            },
            "close": {
                "operation": "close",
                "allowed_fields": sorted(_CANDIDATE_CLOSE_REQUEST_FIELDS),
                "required_fields": sorted(_CANDIDATE_CLOSE_REQUEST_REQUIRED_FIELDS),
                "outcome": "completed",
                "evidence_sources": sorted(_CANDIDATE_CLOSE_EVIDENCE_SOURCES),
                "max_evidence": MAX_CANDIDATE_CLOSE_EVIDENCE,
            },
        },
        "does_not_establish": [
            "candidate_validity",
            "catalog_binding_validity",
            "candidate_recording",
            "claim_authority",
            "dispatch_authority",
        ],
    }


def _github_repository_slug(remote: str) -> str:
    value = remote.strip()
    prefixes = (
        "git@github.com:",
        "ssh://git@github.com/",
        "https://github.com/",
    )
    prefix = next((item for item in prefixes if value.startswith(item)), None)
    if prefix is None:
        raise OperatorIntakeError(
            "github-remote-invalid", "origin remote is not a GitHub repository"
        )
    slug = value.removeprefix(prefix).removesuffix("/").removesuffix(".git")
    parts = slug.split("/")
    if (
        len(parts) != 2
        or any(part in {"", ".", ".."} for part in parts)
        or any(_GITHUB_SLUG_COMPONENT_RE.fullmatch(part) is None for part in parts)
    ):
        raise OperatorIntakeError(
            "github-remote-invalid", "origin remote is not a GitHub repository"
        )
    return "/".join(parts)


def _canonical_runtime_repository(root: Path) -> str | None:
    try:
        identity = bureau_runtime_identity(root)
        compatibility = identity.get("compatibility", {})
        manifest = identity.get("manifest", {})
        canonical = manifest.get("canonical_registry", {})
        registry = identity.get("registry", {})
        external = identity.get("external_remote", {})
        canonical_root = Path(str(canonical.get("root", ""))).expanduser().resolve()
        registry_root = Path(str(registry.get("root", ""))).expanduser().resolve()
        resolved_root = root.expanduser().resolve()
    except (OSError, TypeError, ValueError):
        return None
    expected_repository = external.get("expected_repository")
    if (
        compatibility.get("status") != "canonical-read-only"
        or manifest.get("valid") is not True
        or canonical.get("valid") is not True
        or registry.get("role") != "canonical-runtime-snapshot"
        or registry.get("bureau_project") is not True
        or canonical_root != resolved_root
        or registry_root != resolved_root
        or canonical.get("source_commit") != registry.get("head")
        or not isinstance(expected_repository, str)
        or not expected_repository
    ):
        return None
    try:
        return _github_repository_slug(f"https://github.com/{expected_repository}")
    except OperatorIntakeError:
        return None


def _github_repository_for_preview(root: Path) -> str | None:
    try:
        process = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        process = None
    if process is not None and process.returncode == 0:
        try:
            return _github_repository_slug(process.stdout)
        except OperatorIntakeError:
            return None
    return _canonical_runtime_repository(root)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(errno.ENOTDIR, "path is not a directory", str(path))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_regular_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(errno.EINVAL, "path is not a regular file", str(path))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    """Persist an inactive owned tree before publishing its directory entry."""
    directories: list[Path] = []
    for raw_directory, names, files in os.walk(root, topdown=True, followlinks=False):
        directory = Path(raw_directory)
        directories.append(directory)
        names[:] = [name for name in names if not (directory / name).is_symlink()]
        for name in files:
            path = directory / name
            try:
                mode = path.lstat().st_mode
            except OSError:
                raise
            if stat.S_ISREG(mode):
                _fsync_regular_file(path)
    for directory in reversed(directories):
        _fsync_directory(directory)


def _rename_noreplace(
    source: Path | str,
    target: Path | str,
    *,
    source_dir_fd: int = _AT_FDCWD,
    target_dir_fd: int = _AT_FDCWD,
) -> None:
    """Atomically publish a path without replacing any existing directory entry."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "renameat2(RENAME_NOREPLACE) is unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        source_dir_fd,
        os.fsencode(source),
        target_dir_fd,
        os.fsencode(target),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), str(target))


def _rename_exchange(
    source: Path | str,
    target: Path | str,
    *,
    source_dir_fd: int = _AT_FDCWD,
    target_dir_fd: int = _AT_FDCWD,
) -> None:
    """Atomically exchange two existing directory entries."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "renameat2(RENAME_EXCHANGE) is unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        source_dir_fd,
        os.fsencode(source),
        target_dir_fd,
        os.fsencode(target),
        _RENAME_EXCHANGE,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), str(target))


def _regular_file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns)


def _directory_path_matches_descriptor(path: Path, descriptor: int) -> bool:
    """Return whether the current directory path still names the opened directory."""
    try:
        current = path.stat()
        opened = os.fstat(descriptor)
    except OSError:
        return False
    return (
        stat.S_ISDIR(current.st_mode)
        and stat.S_ISDIR(opened.st_mode)
        and current.st_dev == opened.st_dev
        and current.st_ino == opened.st_ino
    )


def _read_bounded_regular_file(
    path: Path,
    *,
    field: str,
) -> tuple[bytes, tuple[int, int, int, int, int]]:
    """Read exact no-follow bytes and stable identity from one bounded regular file."""
    try:
        path_before = path.lstat()
    except OSError as exc:
        raise OperatorIntakeError(
            f"{field}-read-failed",
            f"cannot inspect {field} file {path}: {exc}",
            retryable=isinstance(exc, (BlockingIOError, InterruptedError)),
            details={"path": str(path)},
        ) from exc
    if not stat.S_ISREG(path_before.st_mode):
        raise OperatorIntakeError(
            f"{field}-type-invalid",
            f"{field} must be a no-follow regular file",
            details={"path": str(path), "mode": stat.filemode(path_before.st_mode)},
        )
    if path_before.st_size > MAX_PROPOSAL_BYTES:
        raise OperatorIntakeError(
            f"{field}-too-large",
            f"{field} exceeds the bounded {MAX_PROPOSAL_BYTES}-byte limit",
            details={"path": str(path), "size": path_before.st_size},
        )
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        opened_before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_before.st_mode)
            or opened_before.st_dev != path_before.st_dev
            or opened_before.st_ino != path_before.st_ino
        ):
            raise OperatorIntakeError(
                f"{field}-changed-during-read",
                f"{field} changed before descriptor binding",
                retryable=True,
                details={"path": str(path)},
            )
        chunks: list[bytes] = []
        remaining = MAX_PROPOSAL_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        opened_after = os.fstat(descriptor)
        path_after = path.lstat()
    except OperatorIntakeError:
        raise
    except OSError as exc:
        raise OperatorIntakeError(
            f"{field}-read-failed",
            f"cannot read {field} file {path}: {exc}",
            retryable=isinstance(exc, (BlockingIOError, InterruptedError)),
            details={"path": str(path)},
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(raw) > MAX_PROPOSAL_BYTES:
        raise OperatorIntakeError(
            f"{field}-too-large",
            f"{field} exceeds the bounded {MAX_PROPOSAL_BYTES}-byte limit",
            details={"path": str(path), "size": len(raw)},
        )
    identity = _regular_file_identity(opened_after)
    if (
        _regular_file_identity(path_before) != identity
        or _regular_file_identity(opened_before) != identity
        or _regular_file_identity(path_after) != identity
        or len(raw) != opened_after.st_size
    ):
        raise OperatorIntakeError(
            f"{field}-changed-during-read",
            f"{field} changed while its exact bytes were read",
            retryable=True,
            details={"path": str(path)},
        )
    return raw, identity


def _before_proposal_review_exchange(path: Path) -> None:
    """Fault-injection seam immediately before the review CAS exchange."""


def _after_proposal_review_exchange(path: Path) -> None:
    """Fault-injection seam immediately after the review CAS exchange."""


def _open_directory_beneath(root: Path, relative: Path) -> int:
    """Open a descendant directory one no-follow component at a time."""
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise OSError(errno.EINVAL, "directory path is not a safe relative descendant")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(root, flags)
    try:
        for part in relative.parts:
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _clear_directory_fd(descriptor: int) -> None:
    """Remove one owned directory tree through stable directory descriptors."""
    with os.scandir(descriptor) as entries:
        names = [entry.name for entry in entries]
    for name in names:
        observed = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISDIR(observed.st_mode):
            child = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
            try:
                opened = os.fstat(child)
                if opened.st_dev != observed.st_dev or opened.st_ino != observed.st_ino:
                    raise OSError(
                        errno.ESTALE,
                        "directory entry changed before descriptor binding",
                        name,
                    )
                _clear_directory_fd(child)
                current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if current.st_dev != opened.st_dev or current.st_ino != opened.st_ino:
                    raise OSError(
                        errno.ESTALE,
                        "directory entry changed during descriptor-bound removal",
                        name,
                    )
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=descriptor)
        else:
            os.unlink(name, dir_fd=descriptor)
    os.fsync(descriptor)


def _remove_directory_tree_at(
    parent_descriptor: int, name: str, *, expected: os.stat_result
) -> None:
    """Remove only the exact directory inode observed by the caller."""
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_descriptor,
    )
    try:
        opened = os.fstat(descriptor)
        if opened.st_dev != expected.st_dev or opened.st_ino != expected.st_ino:
            raise OSError(
                errno.ESTALE,
                "reserved staging directory identity changed before removal",
                name,
            )
        _clear_directory_fd(descriptor)
        current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if current.st_dev != opened.st_dev or current.st_ino != opened.st_ino:
            raise OSError(
                errno.ESTALE,
                "reserved staging directory identity changed during removal",
                name,
            )
    finally:
        os.close(descriptor)
    os.rmdir(name, dir_fd=parent_descriptor)
    os.fsync(parent_descriptor)


class OperatorIntakeError(StateError):
    """Typed operator-intake failure with explicit retry and readback semantics."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        effect_started: bool = False,
        ambiguity: bool = False,
        required_readback: Sequence[str] = (),
        details: dict[str, Any] | None = None,
        publication_phase: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.effect_started = effect_started
        self.ambiguity = ambiguity
        self.required_readback = tuple(required_readback)
        self.details = details or {}
        self.publication_phase = publication_phase

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": OPERATOR_INTAKE_SCHEMA_VERSION,
            "kind": "bureau_operator_intake_failure",
            "status": "failed",
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
            "effect_started": self.effect_started,
            "ambiguity": self.ambiguity,
            "required_readback": list(self.required_readback),
            "publication_phase": self.publication_phase,
            "details": self.details,
            "does_not_establish": ["safe_retry", "effect_absence"],
        }


def read_json_object_file(
    path: str | Path,
    *,
    field: str,
) -> dict[str, Any]:
    """Read one operator transport object with stable machine failure semantics."""
    target = Path(path).expanduser()
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise OperatorIntakeError(
            f"{field}-read-failed",
            f"cannot read {field} file {target}: {exc}",
            retryable=isinstance(exc, (BlockingIOError, InterruptedError)),
            details={"path": str(target)},
        ) from exc
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise OperatorIntakeError(
            f"{field}-json-invalid",
            f"cannot parse {field} JSON from {target}: {exc}",
            details={"path": str(target)},
        ) from exc
    if not isinstance(value, dict):
        raise OperatorIntakeError(
            f"{field}-object-required",
            f"{field} JSON must be an object",
            details={"path": str(target)},
        )
    return value


def _checked_text(
    value: Any,
    *,
    field: str,
    maximum: int,
    required: bool = True,
) -> str | None:
    if value is None:
        if required:
            raise OperatorIntakeError("missing-field", f"{field} is required")
        return None
    if not isinstance(value, str):
        raise OperatorIntakeError("invalid-field", f"{field} must be a string")
    normalized = value.strip()
    if not normalized:
        if required:
            raise OperatorIntakeError("empty-field", f"{field} must not be empty")
        return None
    if len(normalized) > maximum:
        raise OperatorIntakeError("field-too-long", f"{field} must be at most {maximum} characters")
    return normalized


def _checked_source_sha(value: Any) -> str | None:
    normalized = _checked_text(value, field="source_sha256", maximum=64, required=False)
    if normalized is not None and not _SOURCE_SHA_RE.fullmatch(normalized):
        raise OperatorIntakeError(
            "source-digest-invalid", "source_sha256 must be a lowercase SHA-256 digest"
        )
    return normalized


def _checked_supersedes_event_id(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise OperatorIntakeError(
            "supersedes-event-id-invalid",
            "supersedes_event_id must be a positive integer",
        )
    return value


def _request_sha256(value: dict[str, Any]) -> str:
    return legacy.sha256_json(value)


def _operator_context(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("operator_intake")
    return value if isinstance(value, dict) else {}


def _candidate_identity(event: dict[str, Any]) -> str:
    value = event["record"].get("candidate_id")
    return str(value or f"candidate-event-{event['event_id']}")


def _candidate_idempotency_result(
    store: StateStore, *, key: str, request_sha256: str
) -> dict[str, Any] | None:
    """Return the current idempotent replay or fail on a conflicting key."""
    for event in reversed(candidate_records(store)):
        context = _operator_context(event["record"])
        if context.get("idempotency_key") != key:
            continue
        if context.get("request_sha256") != request_sha256:
            raise OperatorIntakeError(
                "idempotency-conflict",
                "idempotency_key already identifies different candidate input",
                details={
                    "candidate_id": _candidate_identity(event),
                    "event_id": event["event_id"],
                    "existing_request_sha256": context.get("request_sha256"),
                    "requested_sha256": request_sha256,
                },
            )
        identity = _candidate_identity(event)
        candidate_event = event["record"].get("candidate_event")
        candidate_event = candidate_event if isinstance(candidate_event, dict) else {}
        try:
            observed = current_candidate_record(store, candidate_id=identity)
        except StateError:
            observed = event
        return {
            "schema_version": OPERATOR_INTAKE_SCHEMA_VERSION,
            "kind": "bureau_candidate_record_result",
            "status": "existing",
            "effect_started": False,
            "retryable": False,
            "ambiguity": False,
            "required_readback": [],
            "idempotent_replay": True,
            "candidate_id": identity,
            "event_id": observed["event_id"],
            "source_event_id": event["event_id"],
            "candidate_event_id": candidate_event.get("event_id"),
            "source_fingerprint": candidate_event.get("source_fingerprint"),
            "content_fingerprint": candidate_event.get("content_fingerprint"),
            "created_at": observed["created_at"],
            "request_sha256": request_sha256,
            "record": observed["record"],
            "does_not_establish": observed["record"].get("does_not_establish", []),
        }
    return None


def _candidate_by_idempotency_key(store: StateStore, *, idempotency_key: str) -> dict[str, Any]:
    key = _checked_text(idempotency_key, field="idempotency_key", maximum=200, required=True)
    assert key is not None
    if not _IDEMPOTENCY_RE.fullmatch(key):
        raise OperatorIntakeError(
            "idempotency-key-invalid",
            "idempotency_key contains unsupported characters",
        )
    for event in reversed(candidate_records(store)):
        if _operator_context(event["record"]).get("idempotency_key") != key:
            continue
        identity = _candidate_identity(event)
        try:
            return current_candidate_record(store, candidate_id=identity)
        except StateError:
            return event
    raise OperatorIntakeError(
        "idempotency-key-unknown",
        "idempotency_key does not identify a candidate",
    )


def candidate_record_request(
    registry: Registry | None,
    store: StateStore,
    request: dict[str, Any],
) -> dict[str, Any]:
    """Validate the versioned JSON transport request before domain dispatch."""
    contract = candidate_record_request_contract()
    expected_schema_version = contract["request_schema_version"]
    received_schema_version = request.get("schema_version")
    if received_schema_version != expected_schema_version:
        raise OperatorIntakeError(
            "request-schema-unsupported",
            f"candidate request schema_version must be {expected_schema_version}",
            details={
                "expected_schema_version": expected_schema_version,
                "received_schema_version": received_schema_version,
            },
        )
    operation = request.get("operation")
    if operation is not None and not isinstance(operation, str):
        raise OperatorIntakeError(
            "request-operation-invalid",
            "candidate request operation must be text",
        )
    if operation == "close":
        close_contract = contract["operations"]["close"]
        unknown = sorted(set(request) - set(close_contract["allowed_fields"]))
        if unknown:
            raise OperatorIntakeError(
                "request-fields-unknown",
                "candidate close request contains unknown fields",
                details={
                    "unknown_fields": unknown,
                    "allowed_fields": close_contract["allowed_fields"],
                },
            )
        missing = sorted(set(close_contract["required_fields"]) - set(request))
        if missing:
            raise OperatorIntakeError(
                "request-fields-missing",
                "candidate close request is missing required fields",
                details={"missing_fields": missing},
            )
        payload = {
            key: value
            for key, value in request.items()
            if key not in {"schema_version", "operation"}
        }
        return candidate_close(registry, store, **payload)
    if operation not in {None, "record"}:
        raise OperatorIntakeError(
            "request-operation-unsupported",
            "candidate request operation must be record or close",
            details={"received_operation": operation},
        )
    legacy_request = {key: value for key, value in request.items() if key != "operation"}
    unknown = sorted(set(legacy_request) - set(contract["allowed_fields"]))
    if unknown:
        raise OperatorIntakeError(
            "request-fields-unknown",
            "candidate request contains unknown fields",
            details={
                "unknown_fields": unknown,
                "allowed_fields": contract["allowed_fields"],
            },
        )
    missing = sorted(set(contract["required_fields"]) - set(legacy_request))
    if missing:
        raise OperatorIntakeError(
            "request-fields-missing",
            "candidate request is missing required fields",
            details={"missing_fields": missing},
        )
    payload = {key: value for key, value in legacy_request.items() if key != "schema_version"}
    return candidate_record(registry, store, **payload)


def _normalize_candidate_close_evidence(value: Any) -> list[dict[str, str]]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > MAX_CANDIDATE_CLOSE_EVIDENCE
    ):
        raise OperatorIntakeError(
            "candidate-close-evidence-invalid",
            f"evidence must contain 1..{MAX_CANDIDATE_CLOSE_EVIDENCE} entries",
        )
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != {"source", "reference", "sha256"}:
            raise OperatorIntakeError(
                "candidate-close-evidence-invalid",
                f"evidence[{index}] must contain exactly source, reference and sha256",
            )
        source = _checked_text(
            item.get("source"),
            field=f"evidence[{index}].source",
            maximum=32,
        )
        assert source is not None
        if source not in _CANDIDATE_CLOSE_EVIDENCE_SOURCES:
            raise OperatorIntakeError(
                "candidate-close-evidence-source-unsupported",
                f"evidence[{index}].source is unsupported",
                details={"allowed_sources": sorted(_CANDIDATE_CLOSE_EVIDENCE_SOURCES)},
            )
        reference = _checked_text(
            item.get("reference"),
            field=f"evidence[{index}].reference",
            maximum=2048,
        )
        assert reference is not None
        digest = _checked_text(
            item.get("sha256"),
            field=f"evidence[{index}].sha256",
            maximum=64,
        )
        assert digest is not None
        if _SOURCE_SHA_RE.fullmatch(digest) is None:
            raise OperatorIntakeError(
                "candidate-close-evidence-digest-invalid",
                f"evidence[{index}].sha256 must be a lowercase SHA-256 digest",
            )
        entry = {"source": source, "reference": reference, "sha256": digest}
        fingerprint = legacy.canonical_json(entry)
        if fingerprint in seen:
            raise OperatorIntakeError(
                "candidate-close-evidence-duplicate",
                f"evidence[{index}] duplicates an earlier entry",
            )
        seen.add(fingerprint)
        normalized.append(entry)
    return sorted(normalized, key=legacy.canonical_json)


def _candidate_close_replay(
    event: dict[str, Any],
    *,
    candidate_id: str,
    idempotency_key: str,
    request_sha256: str,
) -> dict[str, Any] | None:
    context = _operator_context(event["record"])
    closeout = context.get("candidate_closeout")
    if not isinstance(closeout, dict):
        return None
    if (
        closeout.get("idempotency_key") != idempotency_key
        or closeout.get("request_sha256") != request_sha256
        or event["record"].get("status") != "closed"
    ):
        return None
    return {
        "schema_version": OPERATOR_INTAKE_SCHEMA_VERSION,
        "kind": "bureau_candidate_close_result",
        "status": "closed",
        "effect_started": False,
        "retryable": False,
        "ambiguity": False,
        "required_readback": [],
        "idempotent_replay": True,
        "candidate_id": candidate_id,
        "event_id": event["event_id"],
        "request_sha256": request_sha256,
        "closeout_sha256": closeout.get("closeout_sha256"),
        "record": event["record"],
        "does_not_establish": list(closeout.get("does_not_establish", [])),
    }


def candidate_close(
    registry: Registry | None,
    store: StateStore,
    *,
    idempotency_key: str,
    candidate_id: str,
    expected_event_id: int,
    outcome: str,
    evidence: Any,
    note: str | None = None,
) -> dict[str, Any]:
    """Terminally close exactly one current candidate with SHA-bound evidence."""
    key = _checked_text(idempotency_key, field="idempotency_key", maximum=200)
    assert key is not None
    if not _IDEMPOTENCY_RE.fullmatch(key):
        raise OperatorIntakeError(
            "idempotency-key-invalid",
            "idempotency_key contains unsupported characters",
        )
    identity = _checked_text(candidate_id, field="candidate_id", maximum=120)
    assert identity is not None
    checked_event_id = _checked_supersedes_event_id(expected_event_id)
    assert checked_event_id is not None
    if outcome != "completed":
        raise OperatorIntakeError(
            "candidate-close-outcome-invalid",
            "candidate close outcome must be completed",
        )
    normalized_evidence = _normalize_candidate_close_evidence(evidence)
    checked_note = _checked_text(note, field="note", maximum=2000, required=False)
    normalized_request = {
        "schema_version": OPERATOR_INTAKE_SCHEMA_VERSION,
        "operation": "close",
        "idempotency_key": key,
        "candidate_id": identity,
        "expected_event_id": checked_event_id,
        "outcome": "completed",
        "evidence": normalized_evidence,
        "note": checked_note,
    }
    request_sha = _request_sha256(normalized_request)
    try:
        current = current_candidate_record(store, candidate_id=identity)
    except StateError as exc:
        raise OperatorIntakeError(
            "candidate-close-target-invalid",
            str(exc),
        ) from exc
    replay = _candidate_close_replay(
        current,
        candidate_id=identity,
        idempotency_key=key,
        request_sha256=request_sha,
    )
    if replay is not None:
        return replay
    current_status = current["record"].get("status")
    if current_status not in ACTIVE_LIVE_STATUSES:
        raise OperatorIntakeError(
            "candidate-close-not-active",
            "only a current active or observed candidate can be closed",
            details={"candidate_status": current_status, "event_id": current["event_id"]},
        )
    if int(current["event_id"]) != checked_event_id:
        raise OperatorIntakeError(
            "candidate-close-stale",
            "expected_event_id does not match the current candidate event",
            details={
                "expected_event_id": checked_event_id,
                "current_event_id": current["event_id"],
            },
            required_readback=("candidate_by_candidate_id",),
        )

    record = current["record"]
    catalog = record.get("catalog_validation")
    catalog_mode = (
        str(catalog.get("mode"))
        if isinstance(catalog, dict) and catalog.get("mode") in {"strict", "deferred"}
        else "strict"
    )
    bound_registry = registry
    if catalog_mode == "strict":
        if registry is None:
            raise OperatorIntakeError(
                "candidate-close-registry-required",
                "strict candidate close requires a Registry snapshot",
            )
        bound_registry, _ = _canonical_read_registry_snapshot(registry)
        bound_registry = _authoritative_candidate_catalog(
            bound_registry, store, record.get("task_id")
        )
    else:
        bound_registry = None

    evidence_sha = legacy.sha256_json(normalized_evidence)
    closeout_material = {
        "schema_version": 1,
        "kind": "bureau_candidate_closeout",
        "candidate_id": identity,
        "predecessor_event_id": checked_event_id,
        "idempotency_key": key,
        "request_sha256": request_sha,
        "outcome": "completed",
        "evidence": normalized_evidence,
        "evidence_sha256": evidence_sha,
        "closed_at": legacy.utc_now(),
        "does_not_establish": [
            "registry_task_truth",
            "queue_truth",
            *candidate_authority_nonclaims(),
            "system_convergence",
        ],
    }
    closeout_sha = legacy.sha256_json(closeout_material)
    closeout = {**closeout_material, "closeout_sha256": closeout_sha}
    context = dict(_operator_context(record))
    context["candidate_closeout"] = closeout
    try:
        closed = live_register_record(
            bound_registry,
            store,
            kind="candidate_task",
            title=str(record.get("title") or identity),
            source=str(record.get("source") or "operator-intake"),
            repo=record.get("repo"),
            task_id=record.get("task_id"),
            candidate_id=identity,
            supersedes_event_id=checked_event_id,
            status="closed",
            promotion_required=False,
            note=checked_note or "Completed with evidence-bound candidate closeout.",
            catalog_validation=catalog_mode,
            operator_context=context,
        )
    except StateError as exc:
        try:
            latest = current_candidate_record(store, candidate_id=identity)
        except StateError:
            latest = None
        if latest is not None:
            replay = _candidate_close_replay(
                latest,
                candidate_id=identity,
                idempotency_key=key,
                request_sha256=request_sha,
            )
            if replay is not None:
                return replay
        raise OperatorIntakeError(
            "candidate-close-conflict",
            str(exc),
            required_readback=("candidate_by_candidate_id",),
        ) from exc
    return {
        "schema_version": OPERATOR_INTAKE_SCHEMA_VERSION,
        "kind": "bureau_candidate_close_result",
        "status": "closed",
        "effect_started": True,
        "retryable": False,
        "ambiguity": False,
        "required_readback": [],
        "idempotent_replay": False,
        "candidate_id": identity,
        "event_id": closed["event_id"],
        "request_sha256": request_sha,
        "closeout_sha256": closeout_sha,
        "record": closed["record"],
        "does_not_establish": closeout["does_not_establish"],
    }


def _authoritative_candidate_catalog(
    registry: Registry,
    store: StateStore,
    task_id: Any,
) -> Registry:
    """Expose one authoritative StateStore-only task to strict Live Register validation."""
    if not isinstance(task_id, str):
        return registry
    normalized_task_id = task_id.strip()
    if not normalized_task_id or normalized_task_id in registry.tasks:
        return registry
    authoritative = store.task_spec(normalized_task_id)
    if authoritative is None:
        return registry
    spec = authoritative["spec"]
    digest = str(authoritative["spec_sha256"])
    if task_specs.task_spec_digest(spec) != digest:
        raise StateError(f"authoritative TaskSpec digest drift for {normalized_task_id}")
    task = _task_from_authoritative_spec(spec, digest)
    if task.id != normalized_task_id:
        raise StateError(f"authoritative TaskSpec id drift for {normalized_task_id}")
    catalog = copy.copy(registry)
    catalog.tasks = dict(registry.tasks)
    catalog.tasks[normalized_task_id] = task
    return catalog


def candidate_record(
    registry: Registry | None,
    store: StateStore,
    *,
    idempotency_key: str,
    title: str,
    source_kind: str,
    desired_outcome: str,
    repo: str | None = None,
    source_locator: str | None = None,
    source_sha256: str | None = None,
    observed_at: str | None = None,
    task_id: str | None = None,
    candidate_id: str | None = None,
    supersedes_event_id: int | None = None,
    note: str | None = None,
    catalog_validation: str = "strict",
) -> dict[str, Any]:
    """Record one source-bound candidate idempotently in the existing Live Register."""
    key = _checked_text(idempotency_key, field="idempotency_key", maximum=200, required=True)
    assert key is not None
    if not _IDEMPOTENCY_RE.fullmatch(key):
        raise OperatorIntakeError(
            "idempotency-key-invalid",
            "idempotency_key contains unsupported characters",
        )
    checked_title = _checked_text(title, field="title", maximum=240)
    checked_kind = _checked_text(source_kind, field="source_kind", maximum=80)
    checked_outcome = _checked_text(desired_outcome, field="desired_outcome", maximum=4000)
    checked_locator = _checked_text(
        source_locator, field="source_locator", maximum=2000, required=False
    )
    checked_sha = _checked_source_sha(source_sha256)
    checked_observed = _checked_text(observed_at, field="observed_at", maximum=80, required=False)
    checked_supersedes_event_id = _checked_supersedes_event_id(supersedes_event_id)
    checked_note = _checked_text(note, field="note", maximum=2000, required=False)
    request = {
        "schema_version": OPERATOR_INTAKE_SCHEMA_VERSION,
        "idempotency_key": key,
        "title": checked_title,
        "source_kind": checked_kind,
        "desired_outcome": checked_outcome,
        "repo": repo,
        "source_locator": checked_locator,
        "source_sha256": checked_sha,
        "observed_at": checked_observed,
        "task_id": task_id,
        "candidate_id": candidate_id,
        "note": checked_note,
        "catalog_validation": catalog_validation,
    }
    if checked_supersedes_event_id is not None:
        request["supersedes_event_id"] = checked_supersedes_event_id
    request_sha = _request_sha256(request)
    replayed = _candidate_idempotency_result(store, key=key, request_sha256=request_sha)
    if replayed is not None:
        return replayed

    predecessor = None
    if checked_supersedes_event_id is not None:
        predecessor = next(
            (
                item
                for item in reversed(candidate_records(store))
                if int(item["event_id"]) == checked_supersedes_event_id
            ),
            None,
        )

    bound_registry = registry
    if catalog_validation == "strict" and registry is not None:
        bound_registry, _ = _canonical_read_registry_snapshot(registry)
        validation_task_id = task_id
        if validation_task_id is None and predecessor is not None:
            validation_task_id = predecessor["record"].get("task_id")
        bound_registry = _authoritative_candidate_catalog(
            bound_registry, store, validation_task_id
        )
    elif catalog_validation == "deferred":
        bound_registry = None

    generated_observed_at = checked_observed or legacy.utc_now()
    content_fingerprint = candidate_content_fingerprint(
        title=str(checked_title),
        desired_outcome=str(checked_outcome),
        repo=repo,
        task_id=task_id,
    )
    source_fingerprint = candidate_source_fingerprint(
        source_kind=str(checked_kind),
        source_locator=checked_locator,
        source_sha256=checked_sha,
    )
    equivalent_candidate_id: str | None = None
    if candidate_id is None and checked_supersedes_event_id is None:
        for existing in reversed(current_candidate_records(store)):
            existing_record = existing["record"]
            existing_context = _operator_context(existing_record)
            existing_contract = existing_record.get("candidate_event")
            existing_fingerprint = (
                existing_contract.get("content_fingerprint")
                if isinstance(existing_contract, dict)
                else candidate_content_fingerprint(
                    title=str(existing_record.get("title") or ""),
                    desired_outcome=str(existing_context.get("desired_outcome") or ""),
                    repo=existing_record.get("repo"),
                    task_id=existing_record.get("task_id"),
                )
            )
            if existing_fingerprint == content_fingerprint:
                equivalent_candidate_id = _candidate_identity(existing)
                break
    if checked_supersedes_event_id is not None:
        selected_candidate_id = (
            candidate_id
            or (_candidate_identity(predecessor) if predecessor is not None else None)
            or candidate_id_for_content(content_fingerprint)
        )
    else:
        selected_candidate_id = (
            candidate_id or equivalent_candidate_id or candidate_id_for_content(content_fingerprint)
        )
    stable_candidate_event_id = candidate_event_id(
        idempotency_key=key,
        request_sha256=request_sha,
    )
    source_observation = {
        "kind": checked_kind,
        "locator": checked_locator,
        "sha256": checked_sha,
        "fingerprint": source_fingerprint,
        "observed_at": generated_observed_at,
        "freshness": "digest-bound" if checked_sha else "fingerprint-bound",
        "does_not_establish": [] if checked_sha else ["source_content_identity"],
    }
    candidate_event = {
        "schema_version": CANDIDATE_EVENT_SCHEMA_VERSION,
        "kind": "bureau_candidate_event",
        "candidate_id": selected_candidate_id,
        "event_id": stable_candidate_event_id,
        "idempotency_key": key,
        "source_fingerprint": source_fingerprint,
        "content_fingerprint": content_fingerprint,
        "does_not_establish": candidate_authority_nonclaims(),
    }
    context = {
        "schema_version": OPERATOR_INTAKE_SCHEMA_VERSION,
        "idempotency_key": key,
        "request_sha256": request_sha,
        "candidate_event_id": stable_candidate_event_id,
        "source_fingerprint": source_fingerprint,
        "content_fingerprint": content_fingerprint,
        "source": source_observation,
        "source_observations": [source_observation],
        "desired_outcome": checked_outcome,
        "does_not_establish": [
            "registry_task_truth",
            "queue_truth",
            "task_readiness",
            *candidate_authority_nonclaims(),
        ],
    }
    try:
        recorded = live_register_record(
            bound_registry,
            store,
            kind="candidate_task",
            title=str(checked_title),
            source="operator-intake",
            repo=repo,
            task_id=task_id,
            candidate_id=selected_candidate_id,
            supersedes_event_id=checked_supersedes_event_id,
            status=None if checked_supersedes_event_id is not None else "observed",
            promotion_required=(None if checked_supersedes_event_id is not None else True),
            note=checked_note or str(checked_outcome),
            catalog_validation=catalog_validation,
            operator_context=context,
            candidate_event=candidate_event,
            deduplicate_candidate=(candidate_id is None and checked_supersedes_event_id is None),
        )
    except OperatorIntakeError:
        raise
    except StateError as exc:
        replayed = _candidate_idempotency_result(store, key=key, request_sha256=request_sha)
        if replayed is not None:
            return replayed
        raise OperatorIntakeError(
            "candidate-record-invalid",
            str(exc),
            details={"catalog_validation": catalog_validation},
        ) from exc
    if recorded.get("idempotent_replay") is True:
        replayed = _candidate_idempotency_result(
            store,
            key=key,
            request_sha256=request_sha,
        )
        if replayed is not None:
            return replayed
    recorded_candidate_id = _candidate_identity(recorded)
    return {
        "schema_version": OPERATOR_INTAKE_SCHEMA_VERSION,
        "kind": "bureau_candidate_record_result",
        "status": "recorded",
        "effect_started": True,
        "retryable": False,
        "ambiguity": False,
        "required_readback": [],
        "idempotent_replay": False,
        "candidate_id": recorded_candidate_id,
        "event_id": recorded["event_id"],
        "source_event_id": recorded["event_id"],
        "candidate_event_id": stable_candidate_event_id,
        "source_fingerprint": source_fingerprint,
        "content_fingerprint": content_fingerprint,
        "created_at": recorded["created_at"],
        "request_sha256": request_sha,
        "record": recorded["record"],
        "does_not_establish": recorded["nonclaims"],
    }


def _tokens(value: str) -> set[str]:
    return set(_TOKEN_RE.findall(value.casefold()))


def _similarity(left: str, right: str) -> float:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _candidate_text(event: dict[str, Any]) -> str:
    record = event["record"]
    context = _operator_context(record)
    return " ".join(
        str(value)
        for value in (
            record.get("title"),
            record.get("note"),
            context.get("desired_outcome"),
        )
        if value
    )


def _task_text(task: Any) -> str:
    raw = task.raw
    return " ".join(str(value) for value in (task.title, raw.get("goal")) if value)


def _candidate_assess(
    registry: Registry,
    store: StateStore,
    *,
    candidate_id: str | None = None,
    event_id: int | None = None,
    idempotency_key: str | None = None,
    initiative: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Assess one current candidate without changing Registry or Live Register truth."""
    selector_count = sum(value is not None for value in (candidate_id, event_id, idempotency_key))
    if selector_count != 1:
        raise OperatorIntakeError(
            "candidate-selector-invalid",
            "use exactly one of candidate_id, event_id or idempotency_key",
        )
    event = (
        _candidate_by_idempotency_key(store, idempotency_key=idempotency_key)
        if idempotency_key is not None
        else current_candidate_record(store, candidate_id=candidate_id, event_id=event_id)
    )
    record = event["record"]
    identity = _candidate_identity(event)
    context = _operator_context(record)
    selected_initiative = initiative
    if selected_initiative is not None and selected_initiative not in registry.initiatives:
        raise OperatorIntakeError("initiative-unknown", f"unknown initiative {selected_initiative}")
    exact: list[dict[str, Any]] = []
    source_relationships: list[dict[str, Any]] = []
    source = context.get("source") if isinstance(context.get("source"), dict) else {}
    source_sha = source.get("sha256")
    candidate_event = record.get("candidate_event")
    candidate_event = candidate_event if isinstance(candidate_event, dict) else {}
    requested_task_id = task_id or record.get("task_id")
    for existing in registry.tasks.values():
        metadata = existing.raw.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        binding = metadata.get("operator_intake")
        binding = binding if isinstance(binding, dict) else {}
        if binding.get("candidate_id") == identity:
            exact.append(
                {
                    "kind": "task-candidate-binding",
                    "task_id": existing.id,
                    "reason": "same candidate_id",
                }
            )
        existing_source = binding.get("source")
        if (
            source_sha
            and isinstance(existing_source, dict)
            and existing_source.get("sha256") == source_sha
        ):
            source_relationships.append(
                {
                    "kind": "task-source-digest",
                    "task_id": existing.id,
                    "reason": "same source_sha256",
                    "identity_equivalent": binding.get("candidate_id") == identity,
                }
            )
    if requested_task_id and requested_task_id in registry.tasks:
        exact.append(
            {
                "kind": "task-id",
                "task_id": requested_task_id,
                "reason": "task_id exists",
            }
        )
    for other in current_candidate_records(store):
        if (
            int(other["event_id"]) == int(event["event_id"])
            or _candidate_identity(other) == identity
        ):
            continue
        other_record = other["record"]
        other_context = _operator_context(other_record)
        if (
            other_record.get("status") not in {"closed", "promoted", "dropped"}
            and record.get("task_id")
            and other_record.get("task_id") == record.get("task_id")
        ):
            exact.append(
                {
                    "kind": "candidate-task-id",
                    "candidate_id": _candidate_identity(other),
                    "event_id": other["event_id"],
                    "task_id": record.get("task_id"),
                    "reason": "same explicit task_id",
                }
            )
        other_source = other_context.get("source")
        if (
            source_sha
            and isinstance(other_source, dict)
            and other_source.get("sha256") == source_sha
        ):
            source_relationships.append(
                {
                    "kind": "candidate-source-digest",
                    "candidate_id": _candidate_identity(other),
                    "event_id": other["event_id"],
                    "reason": "same source_sha256",
                    "identity_equivalent": False,
                    "same_repository": other_record.get("repo") == record.get("repo"),
                    "same_desired_outcome": other_context.get("desired_outcome")
                    == context.get("desired_outcome"),
                    "same_explicit_task_id": bool(
                        record.get("task_id")
                        and other_record.get("task_id") == record.get("task_id")
                    ),
                }
            )
    deduped_exact = sorted(
        {legacy.canonical_json(item): item for item in exact}.values(),
        key=legacy.canonical_json,
    )
    deduped_source_relationships = sorted(
        {legacy.canonical_json(item): item for item in source_relationships}.values(),
        key=legacy.canonical_json,
    )
    candidate_text = _candidate_text(event)
    similar: list[dict[str, Any]] = []
    for existing in registry.tasks.values():
        score = _similarity(candidate_text, _task_text(existing))
        if score >= 0.2:
            similar.append(
                {
                    "kind": "task",
                    "id": existing.id,
                    "title": existing.title,
                    "score": round(score, 6),
                }
            )
    for other in current_candidate_records(store):
        if (
            int(other["event_id"]) == int(event["event_id"])
            or _candidate_identity(other) == identity
        ):
            continue
        score = _similarity(candidate_text, _candidate_text(other))
        if score >= 0.2:
            similar.append(
                {
                    "kind": "candidate",
                    "id": _candidate_identity(other),
                    "event_id": other["event_id"],
                    "title": other["record"].get("title"),
                    "score": round(score, 6),
                }
            )
    similar.sort(key=lambda item: (-float(item["score"]), item["kind"], item["id"]))
    missing: list[str] = []
    if not record.get("repo"):
        missing.append("repo")
    if not context.get("desired_outcome"):
        missing.append("desired_outcome")
    if not source.get("kind"):
        missing.append("source.kind")
    if not source.get("locator") and not source.get("sha256"):
        missing.append("source.locator_or_sha256")
    catalog = record.get("catalog_validation")
    deferred = isinstance(catalog, dict) and catalog.get("status") == "deferred"
    status = record.get("status")
    if status in {"closed", "dropped"}:
        decision = "drop"
    elif deduped_exact:
        decision = "merge"
    elif deferred:
        decision = "defer"
    elif missing:
        decision = "refine"
    else:
        decision = "promote"
    repo = record.get("repo")
    claims = [{"resource": repo, "mode": "write", "isolation": "worktree"}] if repo else []
    return {
        "schema_version": OPERATOR_INTAKE_SCHEMA_VERSION,
        "kind": "bureau_candidate_assessment",
        "status": "assessed",
        "candidate_id": identity,
        "event_id": event["event_id"],
        "candidate_event_id": candidate_event.get("event_id"),
        "source_fingerprint": candidate_event.get("source_fingerprint"),
        "content_fingerprint": candidate_event.get("content_fingerprint"),
        "candidate_status": status,
        "decision": decision,
        "source_freshness": {
            "status": source.get("freshness", "unknown"),
            "observed_at": source.get("observed_at"),
            "sha256": source_sha,
            "catalog_validation": catalog,
        },
        "target": {
            "initiative": selected_initiative,
            "task_id": requested_task_id,
            "claims": claims,
            "risk": "medium" if claims else "unknown",
            "implementation_approval": (
                task_approval_contract(
                    {
                        "id": requested_task_id,
                        "execution": {
                            "mode": "interactive-agent",
                            "policy": "review-before-effect",
                        },
                        "claims": claims,
                    }
                )
                if requested_task_id
                else None
            ),
            "publication_approval": approval_decision("registry_mutation", None),
        },
        "exact_duplicates": deduped_exact,
        "source_relationships": deduped_source_relationships[:MAX_SOURCE_RELATIONSHIPS],
        "source_relationships_summary": {
            "total_count": len(deduped_source_relationships),
            "returned_count": min(len(deduped_source_relationships), MAX_SOURCE_RELATIONSHIPS),
            "truncated": len(deduped_source_relationships) > MAX_SOURCE_RELATIONSHIPS,
        },
        "similarity_suggestions": similar[:MAX_SIMILARITY_RESULTS],
        "missing_fields": missing,
        "advisory_only": True,
        "does_not_establish": [
            "automatic_merge",
            "automatic_close",
            "automatic_suppression",
            "task_readiness",
            "registry_mutation",
            *candidate_authority_nonclaims(),
        ],
    }


def candidate_assess(
    registry: Registry,
    store: StateStore,
    *,
    candidate_id: str | None = None,
    event_id: int | None = None,
    idempotency_key: str | None = None,
    initiative: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Assess one candidate against a clean, HEAD-bound Registry snapshot."""
    bound_registry, _ = _canonical_read_registry_snapshot(registry)
    return _candidate_assess(
        bound_registry,
        store,
        candidate_id=candidate_id,
        event_id=event_id,
        idempotency_key=idempotency_key,
        initiative=initiative,
        task_id=task_id,
    )


def _git_value(root: Path, *arguments: str) -> str:
    env = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    process = subprocess.run(
        ["git", "-C", str(root), *arguments],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
        env=env,
    )
    if process.returncode != 0:
        detail = (process.stderr or process.stdout).strip()[:2000]
        raise OperatorIntakeError(
            "registry-git-read-failed",
            f"git {' '.join(arguments)} failed: {detail}",
            retryable=True,
        )
    return process.stdout.strip()


def _registry_status(root: Path) -> list[str]:
    status = _git_value(
        root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        "registry",
        "schemas",
    )
    return [line for line in status.splitlines() if line]


def _raise_dirty_registry(entries: list[str]) -> None:
    raise OperatorIntakeError(
        "registry-working-tree-dirty",
        "Registry sources differ from HEAD",
        retryable=True,
        details={
            "entries": entries[:20],
            "entry_count": len(entries),
            "truncated": len(entries) > 20,
        },
    )


def _runtime_snapshot_binding(root: Path) -> dict[str, str] | None:
    try:
        identity = bureau_runtime_identity(root)
        compatibility = identity.get("compatibility", {})
        manifest = identity.get("manifest", {})
        canonical = manifest.get("canonical_registry", {})
        canonical_root = Path(str(canonical.get("root", ""))).expanduser().resolve()
        source_commit = str(canonical.get("source_commit", ""))
        tree_sha256 = str(canonical.get("tree_sha256", ""))
        inventory_sha256 = str(canonical.get("inventory_sha256", ""))
    except (OSError, TypeError, ValueError):
        return None
    if (
        compatibility.get("status") != "canonical-read-only"
        or manifest.get("valid") is not True
        or canonical.get("valid") is not True
        or canonical_root != root
        or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", source_commit) is None
        or _SOURCE_SHA_RE.fullmatch(tree_sha256) is None
        or _SOURCE_SHA_RE.fullmatch(inventory_sha256) is None
    ):
        return None
    return {
        "commit": source_commit,
        "registry_tree": tree_sha256,
        "inventory_sha256": inventory_sha256,
    }


def _canonical_read_registry_snapshot(
    registry: Registry,
) -> tuple[Registry, dict[str, str]]:
    try:
        return _canonical_registry_snapshot(registry)
    except OperatorIntakeError as git_error:
        if git_error.code != "registry-git-read-failed":
            raise
        root = registry.root.expanduser().resolve()
        before = _runtime_snapshot_binding(root)
        if before is None:
            raise
        try:
            bound_registry = Registry.load(root)
        except Exception as exc:
            raise OperatorIntakeError(
                "registry-reload-failed",
                f"cannot reload canonical Registry snapshot: {str(exc)[:2000]}",
                retryable=True,
            ) from exc
        after = _runtime_snapshot_binding(root)
        if after != before:
            raise OperatorIntakeError(
                "registry-snapshot-drift",
                "immutable Registry snapshot changed while it was loaded",
                retryable=True,
                details={"before": before, "after": after},
            ) from git_error
        return bound_registry, {
            "commit": before["commit"],
            "registry_tree": before["registry_tree"],
        }


def _canonical_registry_snapshot(registry: Registry) -> tuple[Registry, dict[str, str]]:
    root = registry.root.expanduser().resolve()
    before = {
        "commit": _git_value(root, "rev-parse", "HEAD"),
        "registry_tree": _git_value(root, "rev-parse", "HEAD:registry"),
    }
    dirty = _registry_status(root)
    if dirty:
        _raise_dirty_registry(dirty)
    try:
        bound_registry = Registry.load(root)
    except Exception as exc:
        raise OperatorIntakeError(
            "registry-reload-failed",
            f"cannot reload canonical Registry snapshot: {str(exc)[:2000]}",
            retryable=True,
        ) from exc
    after = {
        "commit": _git_value(root, "rev-parse", "HEAD"),
        "registry_tree": _git_value(root, "rev-parse", "HEAD:registry"),
    }
    dirty = _registry_status(root)
    if dirty:
        _raise_dirty_registry(dirty)
    if after != before:
        raise OperatorIntakeError(
            "registry-snapshot-drift",
            "Registry HEAD changed while the canonical snapshot was loaded",
            retryable=True,
            details={"before": before, "after": after},
        )
    return bound_registry, before


def _render_task(task_json: dict[str, Any]) -> bytes:
    return (json.dumps(task_json, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _task_change_sha256(path: str, content: bytes, *, before_sha256: str | None = None) -> str:
    return legacy.sha256_json(
        {
            "path": path,
            "before_sha256": before_sha256,
            "after_sha256": hashlib.sha256(content).hexdigest(),
        }
    )


def _write_create_only(path: Path, content: bytes) -> None:
    target = path.expanduser().resolve()
    try:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise OperatorIntakeError(
            "target-exists", f"refusing to overwrite existing file {target}"
        ) from exc
    except OSError as exc:
        raise OperatorIntakeError(
            "target-create-failed",
            f"cannot create output file {target}: {exc}",
            retryable=isinstance(exc, (BlockingIOError, InterruptedError)),
            details={"path": str(target)},
        ) from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        target.unlink(missing_ok=True)
        raise OperatorIntakeError(
            "target-write-failed",
            f"cannot durably write output file {target}: {exc}",
            retryable=isinstance(exc, (BlockingIOError, InterruptedError)),
            details={"path": str(target)},
        ) from exc


def _task_schema_identity(task_json: dict[str, Any]) -> tuple[str, str]:
    raw_task_id = task_json.get("id")
    task_id = raw_task_id if isinstance(raw_task_id, str) and raw_task_id else "<missing-task-id>"
    return task_id, f"operator-intake-task:{task_id}"


def _validate_task_structure(registry: Registry, task_json: dict[str, Any]) -> None:
    task_id, source = _task_schema_identity(task_json)
    try:
        registry.schemas.validate("task", task_json, source)
    except DocumentSchemaError as exc:
        raise OperatorIntakeError(
            "task-schema-invalid",
            f"task {task_id} JSON does not satisfy the task schema: {exc}",
            details={
                "task_id": task_id,
                "source": source,
                "schema_errors": str(exc).splitlines(),
            },
        ) from exc


def _validate_task_schema(registry: Registry, task_json: dict[str, Any]) -> None:
    task_id, source = _task_schema_identity(task_json)
    _validate_task_structure(registry, task_json)
    try:
        registry.schemas.validate_task_write(task_json, source)
    except AcceptanceContractError as exc:
        diagnostics = [dict(item) for item in exc.diagnostics]
        raise OperatorIntakeError(
            "task-acceptance-contract-invalid",
            f"task JSON does not have an executable typed acceptance contract: {exc}",
            details={
                "task_id": task_id,
                "acceptance_contract_errors": diagnostics,
            },
        ) from exc
    except DocumentSchemaError as exc:
        # The structural pass above already normalizes schema failures into the
        # operator-intake error contract. Keep this defensive branch for callers
        # whose SchemaSet implementation changes between the two checks.
        raise OperatorIntakeError(
            "task-schema-invalid",
            f"task {task_id} JSON does not satisfy the task schema: {exc}",
            details={
                "task_id": task_id,
                "source": source,
                "schema_errors": str(exc).splitlines(),
            },
        ) from exc


def _validate_task_semantics(
    registry: Registry,
    store: StateStore,
    task_json: dict[str, Any],
    *,
    allow_existing_task_id: bool = False,
) -> None:
    _validate_task_schema(registry, task_json)
    from .lease_contract import assess_task_broad_bureau_scope

    scope_assessment = assess_task_broad_bureau_scope(task_json, registry.resources)
    if scope_assessment["broad_scope_requested"] and not scope_assessment["allowed"]:
        raise OperatorIntakeError(
            "broad-bureau-task-scope-forbidden",
            "nonterminal task proposals must use exact Bureau resources or an explicit "
            "reviewed repository-wide exception",
            details={"scope_assessment": scope_assessment},
        )
    task_id = str(task_json.get("id", ""))
    initiative = str(task_json.get("initiative", ""))
    current_task_spec = store.task_spec(task_id)
    if current_task_spec is not None and not allow_existing_task_id:
        raise OperatorIntakeError("task-exists", f"task {task_id} already exists")
    if initiative not in registry.initiatives:
        raise OperatorIntakeError("initiative-unknown", f"unknown initiative {initiative}")
    for dependency in task_json.get("depends_on", []):
        if store.task_spec(str(dependency)) is None:
            raise OperatorIntakeError(
                "dependency-unknown", f"task dependency {dependency} is unknown"
            )
    for claim in task_json.get("claims", []):
        resource = claim.get("resource") if isinstance(claim, dict) else None
        if resource not in registry.resources:
            raise OperatorIntakeError(
                "claim-resource-unknown", f"claim resource {resource} is unknown"
            )
    if not task_json.get("claims"):
        raise OperatorIntakeError("claims-missing", "task proposal requires explicit claims")
    if not task_json.get("required_capabilities"):
        raise OperatorIntakeError(
            "capabilities-missing", "task proposal requires explicit capabilities"
        )
    if not task_json.get("acceptance"):
        raise OperatorIntakeError(
            "acceptance-missing", "task proposal requires explicit acceptance criteria"
        )


def _task_projection_file_sha256(registry: Registry, task_id: str) -> str | None:
    target = registry.root / "registry" / "tasks" / f"{task_id}.json"
    if not os.path.lexists(target):
        return None
    payload, _ = _read_bounded_regular_file(target, field="task projection")
    return hashlib.sha256(payload).hexdigest()


_TASK_REVISION_TEXT_CONTINUITY_MIN = 0.5
_TASK_REVISION_SUBJECT_OVERLAP_MIN = 2
_TASK_REVISION_TOKEN_RE = re.compile(r"@?\w+(?:(?:[-./]|::)\w+)*(?:[+#]+)?")
# Process verbs, qualifiers, and function words are weak identity evidence. Keep
# domain nouns/acronyms available so short subjects such as API/SSH still count.
_TASK_REVISION_GENERIC_TOKENS = frozenset(
    {
        "a", "aber", "add", "adopt", "after", "align", "als", "an", "and", "apply",
        "archive", "assess", "audit", "auf", "aus", "automatisieren", "autorisieren",
        "bauen", "before", "beheben", "bei", "bestehend", "bestehende",
        "bestehenden", "bind", "binden", "bounded", "build", "change", "check",
        "classify", "clean", "close", "complete", "configure", "consolidate",
        "converge", "create", "current", "das", "decide", "define", "definieren",
        "delete", "deliver", "dem", "den", "der", "des", "diagnose", "die",
        "disable", "document", "dokumentieren", "ein", "eine", "einem", "einen",
        "einer", "einfuehren", "einführen", "enable", "enforce", "ensure",
        "entfernen", "erstellen", "establish", "evaluate", "exact", "exakt",
        "existing", "expose", "extend", "filter", "final", "finale", "finalen",
        "fix", "for", "fresh", "frisch", "frische", "frischen", "from", "fuer",
        "für", "gegen", "genau", "generalize", "generate", "haerten", "harden",
        "härten", "im", "implement", "implementieren", "improve", "in", "inspect",
        "into", "introduce", "make", "measure", "merge", "migrate", "migrieren",
        "mit", "move", "nach", "neu", "neue", "neuen", "neuer", "neues", "new",
        "next", "normalize", "oder", "of", "ohne", "old", "on", "one", "optimieren",
        "optimize", "or", "perform", "prepare", "prevent", "prove", "pruefen",
        "prüfen", "publish", "reconcile", "reduce", "refactor", "registrieren",
        "remaining", "remove", "repair", "reparieren", "replace", "resolve",
        "restore", "return", "review", "run", "same", "schliessen", "schließen",
        "scout", "separate", "separately", "set", "single", "switch", "test",
        "testen", "the", "to", "tune", "umsetzen", "und", "unify", "update", "use",
        "validate", "validieren", "verbleibende", "verifizieren", "verify", "von",
        "vor", "vorbereiten", "wiederherstellen", "with", "without", "zu", "zum",
        "zur",
    }
)


def _task_revision_claims(task_json: dict[str, Any]) -> list[dict[str, Any]]:
    claims = task_json.get("claims")
    if not isinstance(claims, list):
        return []
    return [claim for claim in claims if isinstance(claim, dict)]


def _task_revision_resource_scope(task_json: dict[str, Any]) -> set[str]:
    return {
        str(claim["resource"])
        for claim in _task_revision_claims(task_json)
        if isinstance(claim.get("resource"), str) and claim.get("resource")
    }


def _task_revision_write_scope(task_json: dict[str, Any]) -> set[str]:
    return {
        str(claim["resource"])
        for claim in _task_revision_claims(task_json)
        if claim.get("mode") in {"write", "exclusive"}
        and isinstance(claim.get("resource"), str)
        and claim.get("resource")
    }


def _task_revision_acceptance_items(
    task_json: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    acceptance = task_json.get("acceptance")
    if not isinstance(acceptance, list):
        return {}
    return {
        str(item["id"]): item
        for item in acceptance
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and item.get("id")
    }


_TASK_REVISION_INFLECTION_EXCEPTIONS = frozenset({"news", "series", "species"})


def _task_revision_plural_base(token: str) -> str | None:
    # Inflection equivalence is deliberately pairwise. Normalize only the trailing
    # subject token and retain conservative ambiguity exclusions so identifiers such
    # as `Canva` and `canvas` do not collapse through a generic trailing-s rule.
    if (
        not token.isalpha()
        or len(token) <= 4
        or token in _TASK_REVISION_INFLECTION_EXCEPTIONS
    ):
        return None
    if len(token) > 5 and token.endswith("ies"):
        return f"{token[:-3]}y"
    if token.endswith(("sses", "shes", "ches", "xes", "zes")):
        return token[:-2]
    if token.endswith("s") and not token.endswith(("ss", "us", "is", "as", "os")):
        return token[:-1]
    return None


def _task_revision_plural_equivalent(before_token: str, after_token: str) -> bool:
    if before_token == after_token:
        return False
    return (
        _task_revision_plural_base(before_token) == after_token
        or _task_revision_plural_base(after_token) == before_token
    )


def _task_revision_tokens(value: Any) -> list[str]:
    text = str(value or "").strip().casefold()
    return _TASK_REVISION_TOKEN_RE.findall(text)


def _task_revision_strong_leading_identity(value: Any) -> str | None:
    raw_tokens = _TASK_REVISION_TOKEN_RE.findall(str(value or "").strip())
    if len(raw_tokens) <= 1:
        return None
    token = raw_tokens[0]
    # A leading repository/package/path-like identifier is subject evidence even
    # though the generic prose model normally treats position zero as an action
    # slot. Keep short all-caps domain acronyms (API/SSH/MCP/CI) for the same
    # reason, but never promote known process words merely because they are cased.
    if any(char.isdigit() or char in "./:@+#" for char in token):
        return token.casefold()
    # A single hyphen is common in process terms such as `pre-check`; reserve
    # hyphen-only promotion for multi-segment technical compounds.
    if token.count("-") >= 2:
        return token.casefold()
    letters = "".join(char for char in token if char.isalpha())
    if (
        2 <= len(letters) <= 4
        and letters.isupper()
        and token.casefold() not in _TASK_REVISION_GENERIC_TOKENS
    ):
        return token.casefold()
    return None


def _task_revision_has_strong_leading_identity(value: Any) -> bool:
    return _task_revision_strong_leading_identity(value) is not None


def _task_revision_subject_sequence(value: Any) -> list[str]:
    tokens = _task_revision_tokens(value)
    if len(tokens) > 1 and _task_revision_strong_leading_identity(value) is None:
        tokens = tokens[1:]
    return [token for token in tokens if token not in _TASK_REVISION_GENERIC_TOKENS]


def _task_revision_trailing_inflection_is_continuous(before: Any, after: Any) -> bool:
    before_raw = _task_revision_tokens(before)
    after_raw = _task_revision_tokens(after)
    if not before_raw or not after_raw:
        return False
    leading_compatible = before_raw[0] == after_raw[0] or (
        before_raw[0] in _TASK_REVISION_GENERIC_TOKENS
        and after_raw[0] in _TASK_REVISION_GENERIC_TOKENS
    )
    if not leading_compatible:
        return False
    before_subject = _task_revision_subject_sequence(before)
    after_subject = _task_revision_subject_sequence(after)
    return (
        len(before_subject) == len(after_subject)
        and len(before_subject) >= 1
        and before_subject[:-1] == after_subject[:-1]
        and _task_revision_plural_equivalent(before_subject[-1], after_subject[-1])
    )


def _task_revision_shared_subject_suffix(
    before: Any, after: Any
) -> tuple[int, list[str], list[str]]:
    before_subject = _task_revision_subject_sequence(before)
    after_subject = _task_revision_subject_sequence(after)
    shared = 0
    for before_token, after_token in zip(
        reversed(before_subject), reversed(after_subject), strict=False
    ):
        if before_token != after_token:
            break
        shared += 1
    return shared, before_subject, after_subject


def _task_revision_text_evidence(
    before: Any,
    after: Any,
    *,
    ignore_leading_action: bool = True,
) -> tuple[float, int, int, bool]:
    before_text = str(before or "").strip().casefold()
    after_text = str(after or "").strip().casefold()
    exact = bool(before_text) and before_text == after_text
    before_raw_tokens = _task_revision_tokens(before_text)
    after_raw_tokens = _task_revision_tokens(after_text)
    # Task prose often starts with an action verb. The action-stripped view prevents
    # unseen verbs such as "Upgrade" from becoming identity evidence merely because
    # a static weak-token set did not enumerate them. A second full-token view is
    # required when resources change so noun phrases do not lose their real subject.
    if ignore_leading_action:
        if (
            len(before_raw_tokens) > 1
            and _task_revision_strong_leading_identity(before) is None
        ):
            before_raw_tokens = before_raw_tokens[1:]
        if (
            len(after_raw_tokens) > 1
            and _task_revision_strong_leading_identity(after) is None
        ):
            after_raw_tokens = after_raw_tokens[1:]
    before_tokens = {
        token for token in before_raw_tokens if token not in _TASK_REVISION_GENERIC_TOKENS
    }
    after_tokens = {
        token for token in after_raw_tokens if token not in _TASK_REVISION_GENERIC_TOKENS
    }
    shorter = min(len(before_tokens), len(after_tokens))
    if shorter == 0:
        return 0.0, 0, 0, exact
    overlap = len(before_tokens & after_tokens)
    return overlap / shorter, overlap, shorter, exact


def _task_revision_acceptance_item_is_continuous(
    before: dict[str, Any], after: dict[str, Any]
) -> bool:
    if before == after:
        return True
    # Acceptance criteria are a strong identity escape hatch for large task prose
    # rewrites. Keep the verifier side of the complete typed criterion contract
    # exact; a retained id cannot mask verifier/config drift. Assertion wording may
    # evolve only when it independently survives the hardened text-identity rule
    # with substantial subject evidence.
    for field in ("evidence_type", "verifier", "verifier_config"):
        if before.get(field) != after.get(field):
            return False
    continuous, evidence = _task_revision_text_is_continuous(
        before.get("assertion"),
        after.get("assertion"),
        resource_continuity=True,
    )
    return continuous and evidence["subject_token_overlap"] >= 4


def _task_revision_text_is_continuous(
    before: Any, after: Any, *, resource_continuity: bool
) -> tuple[bool, dict[str, Any]]:
    continuity, subject_overlap, shorter_subject_count, exact = (
        _task_revision_text_evidence(before, after)
    )
    full_continuity, full_overlap, full_shorter_count, _ = (
        _task_revision_text_evidence(
            before,
            after,
            ignore_leading_action=False,
        )
    )
    before_strong_leading_identity = _task_revision_strong_leading_identity(before)
    after_strong_leading_identity = _task_revision_strong_leading_identity(after)
    retained_strong_leading_identity = (
        before_strong_leading_identity is not None
        and before_strong_leading_identity == after_strong_leading_identity
    )
    shared_subject_suffix_count, before_subject, after_subject = (
        _task_revision_shared_subject_suffix(before, after)
    )
    trailing_inflection_continuity = (
        resource_continuity
        and _task_revision_trailing_inflection_is_continuous(before, after)
    )
    shorter_subject_sequence_count = min(len(before_subject), len(after_subject))
    all_overlap_is_shared_suffix = shared_subject_suffix_count == subject_overlap
    # Position zero is deliberately action-agnostic: an unseen process verb must
    # not become identity evidence just because a static weak-token list omitted
    # it. If every surviving overlap is merely a trailing suffix and another
    # subject token changed, require independent goal/acceptance evidence instead.
    # Explicit identifiers/acronyms are the narrow exception because the tokenizer
    # already treats them as atomic subject identities.
    shared_suffix_only_collision = (
        not retained_strong_leading_identity
        and shared_subject_suffix_count >= _TASK_REVISION_SUBJECT_OVERLAP_MIN
        and shared_subject_suffix_count < shorter_subject_sequence_count
        and all_overlap_is_shared_suffix
    )
    if exact:
        # Exact generic prose is not a subject anchor. Across a resource change, use
        # the full-token view because the first word may be a noun rather than an
        # action verb.
        continuous = full_shorter_count > 0 and (
            resource_continuity
            or full_shorter_count >= _TASK_REVISION_SUBJECT_OVERLAP_MIN
        )
    elif continuity < _TASK_REVISION_TEXT_CONTINUITY_MIN:
        continuous = trailing_inflection_continuity
    elif resource_continuity:
        continuous = trailing_inflection_continuity or (
            (
                subject_overlap >= _TASK_REVISION_SUBJECT_OVERLAP_MIN
                or (subject_overlap == 1 and shorter_subject_count == 1)
            )
            and full_continuity >= continuity
            and not shared_suffix_only_collision
        )
    else:
        # A resource change must survive both interpretations of the first word:
        # possible action verb and possible noun/adjective subject. This prevents
        # `Backup retention policy` -> `Dashboard retention policy` from looking
        # identical merely because the leading token was stripped.
        continuous = (
            continuity == 1.0
            and subject_overlap >= _TASK_REVISION_SUBJECT_OVERLAP_MIN
            and full_continuity == 1.0
            and full_overlap >= _TASK_REVISION_SUBJECT_OVERLAP_MIN
        )
    return continuous, {
        "continuity": round(continuity, 6),
        "subject_token_overlap": subject_overlap,
        "shorter_subject_token_count": shorter_subject_count,
        "full_token_continuity": round(full_continuity, 6),
        "full_subject_token_overlap": full_overlap,
        "full_shorter_subject_token_count": full_shorter_count,
        "exact_nonempty_text": exact,
        "resource_continuity": resource_continuity,
        "before_strong_leading_identity": before_strong_leading_identity is not None,
        "after_strong_leading_identity": after_strong_leading_identity is not None,
        "retained_strong_leading_identity": retained_strong_leading_identity,
        "before_subject_sequence": before_subject,
        "after_subject_sequence": after_subject,
        "shared_subject_suffix_count": shared_subject_suffix_count,
        "all_overlap_is_shared_suffix": all_overlap_is_shared_suffix,
        "shared_suffix_only_collision": shared_suffix_only_collision,
        "trailing_inflection_continuity": trailing_inflection_continuity,
        "requires_complete_shorter_subject": not resource_continuity,
        "minimum_subject_token_overlap": _TASK_REVISION_SUBJECT_OVERLAP_MIN,
    }


def _validate_task_revision_identity_continuity(
    before: dict[str, Any], after: dict[str, Any]
) -> None:
    if before.get("initiative") != after.get("initiative"):
        raise OperatorIntakeError(
            "task-revision-initiative-mismatch",
            "TaskSpec revision cannot move an existing task id to another initiative",
            details={
                "task_id": after.get("id"),
                "before_initiative": before.get("initiative"),
                "after_initiative": after.get("initiative"),
            },
        )
    before_scope = _task_revision_write_scope(before)
    after_scope = _task_revision_write_scope(after)
    write_scope_overlap = before_scope & after_scope
    resource_overlap = _task_revision_resource_scope(before) & _task_revision_resource_scope(
        after
    )
    # Read-only revisions do not gain mutation authority. Any later transition to
    # write scope is validated against revision 1 by the binding layer, so read-only
    # drift cannot launder an unrelated writable identity.
    if not after_scope:
        return
    title_continuous, title_evidence = _task_revision_text_is_continuous(
        before.get("title"),
        after.get("title"),
        resource_continuity=bool(write_scope_overlap),
    )
    goal_continuous, goal_evidence = _task_revision_text_is_continuous(
        before.get("goal"),
        after.get("goal"),
        resource_continuity=bool(write_scope_overlap),
    )
    before_acceptance_items = _task_revision_acceptance_items(before)
    after_acceptance_items = _task_revision_acceptance_items(after)
    before_acceptance = set(before_acceptance_items)
    after_acceptance = set(after_acceptance_items)
    acceptance_overlap = before_acceptance & after_acceptance
    continuous_acceptance_ids = {
        item_id
        for item_id in acceptance_overlap
        if _task_revision_acceptance_item_is_continuous(
            before_acceptance_items[item_id], after_acceptance_items[item_id]
        )
    }
    # Only byte-for-byte equivalent typed criteria may independently anchor a
    # large prose rewrite. Semantically similar assertion text can still support
    # already-continuous task prose, but cannot by itself preserve task identity.
    exact_acceptance_ids = {
        item_id
        for item_id in acceptance_overlap
        if before_acceptance_items[item_id] == after_acceptance_items[item_id]
    }
    acceptance_continuity = (
        bool(write_scope_overlap)
        and len(exact_acceptance_ids) >= _TASK_REVISION_SUBJECT_OVERLAP_MIN
        and len(exact_acceptance_ids)
        == min(len(before_acceptance), len(after_acceptance))
    )
    if acceptance_continuity:
        return
    continuous_evidence = [
        evidence
        for continuous, evidence in (
            (title_continuous, title_evidence),
            (goal_continuous, goal_evidence),
        )
        if continuous
    ]
    if any(evidence["trailing_inflection_continuity"] for evidence in continuous_evidence):
        return
    if any(
        max(
            int(evidence["shorter_subject_token_count"]),
            int(evidence["full_shorter_subject_token_count"]),
        )
        >= _TASK_REVISION_SUBJECT_OVERLAP_MIN
        for evidence in continuous_evidence
    ):
        return
    # A single subject token is intentionally a weak continuity signal. It is
    # accepted only when at least one typed acceptance criterion contract also
    # remains continuous, so an unseen one-word process label such as "Upgrade"
    # cannot carry identity by
    # itself while compact real subjects such as "contracts" remain revisable.
    if continuous_evidence and continuous_acceptance_ids:
        return
    raise OperatorIntakeError(
        "task-revision-identity-discontinuity",
        "TaskSpec revision changes both task title and goal without identity "
        "continuity; create a new task id instead",
        details={
            "task_id": after.get("id"),
            "before_write_scope": sorted(before_scope),
            "after_write_scope": sorted(after_scope),
            "write_scope_overlap": sorted(write_scope_overlap),
            "resource_overlap": sorted(resource_overlap),
            "acceptance_overlap": sorted(acceptance_overlap),
            "continuous_acceptance_ids": sorted(continuous_acceptance_ids),
            "exact_acceptance_ids": sorted(exact_acceptance_ids),
            "acceptance_continuity": acceptance_continuity,
            "title_evidence": title_evidence,
            "goal_evidence": goal_evidence,
            "minimum_text_continuity": _TASK_REVISION_TEXT_CONTINUITY_MIN,
            "minimum_subject_token_overlap": _TASK_REVISION_SUBJECT_OVERLAP_MIN,
        },
    )


def _task_revision_identity_baseline(
    store: StateStore, current: dict[str, Any]
) -> dict[str, Any]:
    task_id = str(current["task_id"])
    current_revision = int(current["revision"])
    if current_revision == 1:
        return current
    from . import task_specs as task_specs_module

    try:
        with store.connect() as connection:
            # The permanent task id is born with revision 1. Every later write-capable
            # revision must remain recognizable against that stable origin rather than
            # inheriting identity from a chain of individually plausible edits.
            return task_specs_module.get_revision(connection, task_id, 1)
    except (sqlite3.Error, task_specs_module.TaskSpecError) as exc:
        raise OperatorIntakeError(
            "task-revision-identity-baseline-read-failed",
            "cannot reconstruct the initial TaskSpec identity baseline",
            retryable=True,
            details={"task_id": task_id, "current_revision": current_revision},
        ) from exc


def _task_spec_proposal_binding(
    registry: Registry,
    store: StateStore,
    *,
    task_json: dict[str, Any],
    event: dict[str, Any],
) -> dict[str, Any]:
    task_id = str(task_json.get("id", ""))
    proposed_sha256 = legacy.sha256_json(task_json)
    current = store.task_spec(task_id)
    projected_file_sha256 = _task_projection_file_sha256(registry, task_id)
    if current is None:
        return {
            "operation": "register",
            "expected_revision": None,
            "expected_spec_sha256": None,
            "expected_task_file_sha256": projected_file_sha256,
            "proposed_spec_sha256": proposed_sha256,
        }
    explicit_task_id = event["record"].get("task_id")
    if explicit_task_id != task_id:
        raise OperatorIntakeError(
            "candidate-task-identity-mismatch",
            "TaskSpec revision candidate must explicitly bind the exact existing task_id",
            details={"candidate_task_id": explicit_task_id, "task_id": task_id},
        )
    identity_baseline = (
        _task_revision_identity_baseline(store, current)
        if _task_revision_write_scope(task_json)
        else current
    )
    _validate_task_revision_identity_continuity(identity_baseline["spec"], task_json)
    return {
        "operation": "revise",
        "expected_revision": int(current["revision"]),
        "expected_spec_sha256": str(current["spec_sha256"]),
        "expected_task_file_sha256": projected_file_sha256,
        "proposed_spec_sha256": proposed_sha256,
    }


def _validate_task_spec_proposal_binding(
    registry: Registry,
    store: StateStore,
    *,
    plan: dict[str, Any],
    task_json: dict[str, Any],
    event: dict[str, Any],
) -> dict[str, Any]:
    binding = plan.get("task_spec")
    expected_fields = {
        "operation",
        "expected_revision",
        "expected_spec_sha256",
        "expected_task_file_sha256",
        "proposed_spec_sha256",
    }
    if not isinstance(binding, dict) or set(binding) != expected_fields:
        raise OperatorIntakeError(
            "task-spec-binding-invalid",
            "proposal TaskSpec revision binding fields are not exact",
        )
    task_id = str(task_json.get("id", ""))
    proposed_sha256 = legacy.sha256_json(task_json)
    if binding.get("proposed_spec_sha256") != proposed_sha256:
        raise OperatorIntakeError(
            "task-spec-proposed-digest-drift",
            "proposal TaskSpec digest does not match task_json",
        )
    operation = binding.get("operation")
    current = store.task_spec(task_id)
    expected_revision = binding.get("expected_revision")
    expected_spec_sha256 = binding.get("expected_spec_sha256")
    expected_file_sha256 = binding.get("expected_task_file_sha256")
    if operation == "register":
        if expected_revision is not None or expected_spec_sha256 is not None:
            raise OperatorIntakeError(
                "task-spec-binding-invalid",
                "new TaskSpec registration must bind a null StateStore preimage",
            )
        if expected_file_sha256 is not None and (
            not isinstance(expected_file_sha256, str)
            or _SOURCE_SHA_RE.fullmatch(expected_file_sha256) is None
        ):
            raise OperatorIntakeError(
                "task-spec-binding-invalid",
                "compatibility task-file trace digest is malformed",
            )
        replay_state = (
            current is not None
            and int(current["revision"]) == 1
            and current["spec_sha256"] == proposed_sha256
        )
        if current is not None and not replay_state:
            raise OperatorIntakeError(
                "task-spec-baseline-drift",
                "TaskSpec appeared after proposal review",
                retryable=True,
                details={"task_id": task_id, "current_revision": current["revision"]},
            )
    elif operation == "revise":
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 1
            or not isinstance(expected_spec_sha256, str)
            or _SOURCE_SHA_RE.fullmatch(expected_spec_sha256) is None
            or (
                expected_file_sha256 is not None
                and (
                    not isinstance(expected_file_sha256, str)
                    or _SOURCE_SHA_RE.fullmatch(expected_file_sha256) is None
                )
            )
        ):
            raise OperatorIntakeError(
                "task-spec-binding-invalid",
                "TaskSpec revision baseline is malformed",
            )
        baseline_state = (
            current is not None
            and int(current["revision"]) == expected_revision
            and current["spec_sha256"] == expected_spec_sha256
        )
        replay_state = (
            current is not None
            and int(current["revision"]) == expected_revision + 1
            and current["spec_sha256"] == proposed_sha256
        )
        if not baseline_state and not replay_state:
            raise OperatorIntakeError(
                "task-spec-baseline-drift",
                "authoritative TaskSpec revision changed after proposal review",
                retryable=True,
                details={
                    "task_id": task_id,
                    "expected_revision": expected_revision,
                    "current_revision": None if current is None else current["revision"],
                },
            )
        if baseline_state:
            assert current is not None
            identity_baseline = (
                _task_revision_identity_baseline(store, current)
                if _task_revision_write_scope(task_json)
                else current
            )
            _validate_task_revision_identity_continuity(
                identity_baseline["spec"], task_json
            )
        if event["record"].get("task_id") != task_id:
            raise OperatorIntakeError(
                "candidate-task-identity-mismatch",
                "reviewed TaskSpec revision candidate no longer binds the exact task_id",
            )
    else:
        raise OperatorIntakeError(
            "task-spec-binding-invalid",
            "proposal TaskSpec operation must be register or revise",
        )
    return binding


def _validate_register_replay_mutation(
    store: StateStore,
    *,
    task_id: str,
    proposal_sha256: str,
    proposed_spec_sha256: str,
) -> None:
    idempotency_key = f"operator-intake:{proposal_sha256}"
    try:
        with store.connect() as connection:
            mutation = connection.execute(
                "SELECT task_id,expected_revision,requested_sha256,resulting_revision "
                "FROM task_spec_mutations WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise OperatorIntakeError(
            "task-spec-register-replay-mutation-read-failed",
            "cannot verify the TaskSpec mutation for register publication replay",
            retryable=True,
            details={"task_id": task_id},
        ) from exc
    if (
        mutation is None
        or mutation["task_id"] != task_id
        or mutation["expected_revision"] is not None
        or mutation["requested_sha256"] != proposed_spec_sha256
        or type(mutation["resulting_revision"]) is not int
        or mutation["resulting_revision"] != 1
    ):
        raise OperatorIntakeError(
            "task-spec-register-replay-mutation-mismatch",
            "existing register TaskSpec is not bound to this reviewed proposal mutation",
            details={"task_id": task_id, "mutation_present": mutation is not None},
        )


def _inject_candidate_binding(task_json: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(legacy.canonical_json(task_json))
    metadata = result.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        raise OperatorIntakeError("metadata-invalid", "task metadata must be an object")
    context = _operator_context(event["record"])
    candidate_event = event["record"].get("candidate_event")
    candidate_event = candidate_event if isinstance(candidate_event, dict) else {}
    metadata["operator_intake"] = {
        "schema_version": OPERATOR_INTAKE_SCHEMA_VERSION,
        "candidate_id": _candidate_identity(event),
        "event_id": event["event_id"],
        "event_created_at": event["created_at"],
        "request_sha256": context.get("request_sha256"),
        "candidate_event_id": candidate_event.get("event_id"),
        "source_fingerprint": candidate_event.get("source_fingerprint"),
        "content_fingerprint": candidate_event.get("content_fingerprint"),
        "source": context.get("source"),
        "source_observations": context.get("source_observations", []),
        "does_not_establish": [
            "queue_truth",
            "task_readiness",
            *candidate_authority_nonclaims(),
        ],
    }
    return result


def task_propose(
    registry: Registry,
    store: StateStore,
    *,
    task_json: dict[str, Any],
    publishing_task_id: str,
    path: str | Path,
    candidate_id: str | None = None,
    event_id: int | None = None,
    unresolved_fields: Sequence[str] = (),
    placeholder_justification: str | None = None,
) -> dict[str, Any]:
    """Write one source-, Registry- and candidate-bound task proposal."""
    registry, identity = _canonical_registry_snapshot(registry)
    event = current_candidate_record(store, candidate_id=candidate_id, event_id=event_id)
    if event["record"].get("status") not in ACTIVE_LIVE_STATUSES:
        raise OperatorIntakeError(
            "candidate-not-open", "only a current open candidate can be proposed"
        )
    publishing_task = store.task_spec(publishing_task_id)
    if publishing_task is None:
        raise OperatorIntakeError(
            "publishing-task-unknown",
            f"publishing task {publishing_task_id} is not in the authoritative StateStore",
        )
    bound_task = _inject_candidate_binding(task_json, event)
    # Structural shape must be safe before we inspect acceptance data, but the
    # specific generic-placeholder policy intentionally has precedence over the
    # newer executable-acceptance contract diagnostics.
    _validate_task_structure(registry, bound_task)
    generic_ids = {
        criterion.get("id")
        for criterion in bound_task.get("acceptance", [])
        if isinstance(criterion, dict)
    } & _GENERIC_ACCEPTANCE_IDS
    if generic_ids and not _checked_text(
        placeholder_justification,
        field="placeholder_justification",
        maximum=2000,
        required=False,
    ):
        raise OperatorIntakeError(
            "generic-placeholder-rejected",
            "generic promotion acceptance requires explicit justification",
            details={"acceptance_ids": sorted(generic_ids)},
        )
    _validate_task_schema(registry, bound_task)
    task_spec_binding = _task_spec_proposal_binding(
        registry, store, task_json=bound_task, event=event
    )
    _validate_task_semantics(
        registry,
        store,
        bound_task,
        allow_existing_task_id=task_spec_binding["operation"] == "revise",
    )
    assessment = _candidate_assess(
        registry,
        store,
        event_id=int(event["event_id"]),
        initiative=str(bound_task["initiative"]),
        task_id=str(bound_task["id"]),
    )
    blocking_duplicates = list(assessment["exact_duplicates"])
    if task_spec_binding["operation"] == "revise":
        blocking_duplicates = [
            item
            for item in blocking_duplicates
            if not (item.get("kind") == "task-id" and item.get("task_id") == bound_task["id"])
        ]
    if blocking_duplicates:
        raise OperatorIntakeError(
            "exact-duplicate",
            "candidate assessment found an exact duplicate",
            details={"findings": blocking_duplicates},
        )
    task_id = str(bound_task["id"])
    target_path = f"registry/tasks/{task_id}.json"
    content = _render_task(bound_task)
    unresolved = sorted(
        {value.strip() for value in unresolved_fields if isinstance(value, str) and value.strip()}
    )
    proposal: dict[str, Any] = {
        "schema_version": OPERATOR_INTAKE_SCHEMA_VERSION,
        "kind": "bureau_operator_task_proposal",
        "command": "operator-task-propose",
        "candidate": {
            "candidate_id": _candidate_identity(event),
            "event_id": event["event_id"],
            "event_created_at": event["created_at"],
            "event_sha256": legacy.sha256_json(event),
        },
        "registry": identity,
        "publishing_task_id": publishing_task_id,
        "publishing_task_sha256": publishing_task["spec_sha256"],
        "task_id": task_id,
        "target_path": target_path,
        "task_json": bound_task,
        "task_json_sha256": legacy.sha256_json(bound_task),
        "task_file_sha256": hashlib.sha256(content).hexdigest(),
        "task_spec": task_spec_binding,
        "proposed_diff_sha256": _task_change_sha256(
            target_path,
            content,
            before_sha256=task_spec_binding["expected_task_file_sha256"],
        ),
        "assessment": assessment,
        "unresolved_fields": unresolved,
        "placeholder_justification": placeholder_justification,
        "publication": {
            "action_class": "registry_mutation",
            "publication_mode": "state_store",
            "required_level": "reviewed_plan",
            "queue_mutated": False,
        },
        "review": {
            "required": True,
            "status": "pending",
            "required_fields": [
                "reviewer",
                "reviewed_at",
                "reviewed_proposal_sha256",
            ],
        },
        "does_not_establish": [
            "git_registry_mutation",
            "queue_mutation",
            "task_readiness",
            "claim_or_dispatch_authority",
            "merge_or_deployment_authority",
            *candidate_authority_nonclaims(),
        ],
    }
    unsigned = {
        key: value for key, value in proposal.items() if key not in {"proposal_sha256", "review"}
    }
    proposal["proposal_sha256"] = legacy.sha256_json(unsigned)
    rendered = (json.dumps(proposal, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    target = Path(path).expanduser()
    _write_create_only(target, rendered)
    return {
        "schema_version": OPERATOR_INTAKE_SCHEMA_VERSION,
        "kind": "bureau_task_proposal_result",
        "status": "written",
        "effect_started": True,
        "retryable": False,
        "ambiguity": False,
        "required_readback": [],
        "path": str(target),
        "proposal_sha256": proposal["proposal_sha256"],
        "plan_file_sha256": hashlib.sha256(rendered).hexdigest(),
        "proposal": proposal,
    }


def _proposal_unsigned(plan: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in plan.items() if key not in {"proposal_sha256", "review"}}


def review_task_proposal(
    *,
    plan_path: str | Path,
    reviewer: str,
    expected_proposal_sha256: str,
) -> dict[str, Any]:
    """Bind one operator review to exact proposal bytes through an atomic local CAS."""
    path = Path(plan_path).expanduser().absolute()
    checked_reviewer = _checked_text(reviewer, field="reviewer", maximum=200)
    assert checked_reviewer is not None
    checked_expected = _checked_text(
        expected_proposal_sha256,
        field="expected_proposal_sha256",
        maximum=64,
    )
    assert checked_expected is not None
    if _SOURCE_SHA_RE.fullmatch(checked_expected) is None:
        raise OperatorIntakeError(
            "proposal-digest-invalid",
            "expected_proposal_sha256 must be a lowercase SHA-256 digest",
        )
    plan_bytes, plan_identity = _read_bounded_regular_file(path, field="proposal")
    try:
        plan = json.loads(plan_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise OperatorIntakeError(
            "proposal-json-invalid",
            f"cannot parse task proposal: {exc}",
            details={"path": str(path)},
        ) from exc
    if not isinstance(plan, dict) or plan.get("kind") != "bureau_operator_task_proposal":
        raise OperatorIntakeError("proposal-kind-invalid", "unsupported operator task proposal")
    proposal_sha256 = legacy.sha256_json(_proposal_unsigned(plan))
    if plan.get("proposal_sha256") != proposal_sha256:
        raise OperatorIntakeError(
            "proposal-integrity-invalid",
            "task proposal hash does not match its content",
        )
    if proposal_sha256 != checked_expected:
        raise OperatorIntakeError(
            "proposal-review-reference-mismatch",
            "expected proposal digest does not match the exact plan bytes",
            details={"expected": checked_expected, "observed": proposal_sha256},
        )
    unresolved = plan.get("unresolved_fields")
    if not isinstance(unresolved, list):
        raise OperatorIntakeError(
            "proposal-unresolved-invalid",
            "proposal unresolved_fields must be a list",
        )
    if unresolved:
        raise OperatorIntakeError(
            "proposal-unresolved",
            "proposal cannot be reviewed while unresolved fields remain",
            details={"unresolved_fields": unresolved},
        )
    task_id = _checked_text(plan.get("task_id"), field="task_id", maximum=240)
    assert task_id is not None
    review = plan.get("review")
    if not isinstance(review, dict):
        raise OperatorIntakeError("review-invalid", "proposal review must be an object")
    if review.get("status") == "reviewed":
        same_review = (
            review.get("reviewer") == checked_reviewer
            and review.get("reviewed_proposal_sha256") == proposal_sha256
        )
        if not same_review:
            raise OperatorIntakeError(
                "review-conflict",
                "proposal is already bound to a different review",
                details={"path": str(path), "review": review},
            )
        approval = require_approval(
            "registry_mutation",
            reviewed_plan_approval(
                reviewer=checked_reviewer,
                reference=proposal_sha256,
                task_id=task_id,
                scope="registry_mutation",
            ),
            expected_reference=proposal_sha256,
            task_id=task_id,
        )
        plan_file_sha256 = hashlib.sha256(plan_bytes).hexdigest()
        return {
            "schema_version": OPERATOR_INTAKE_SCHEMA_VERSION,
            "kind": "bureau_task_review_result",
            "status": "existing",
            "effect_started": False,
            "retryable": False,
            "ambiguity": False,
            "required_readback": [],
            "idempotent_replay": True,
            "path": str(path),
            "proposal_sha256": proposal_sha256,
            "plan_file_sha256_before": plan_file_sha256,
            "plan_file_sha256_after": plan_file_sha256,
            "review": review,
            "approval": approval,
            "does_not_establish": [
                "registry_mutation",
                "queue_mutation",
                "publication_effect",
                *candidate_authority_nonclaims(),
            ],
        }
    if review.get("required") is not True or review.get("status") != "pending":
        raise OperatorIntakeError(
            "review-state-invalid",
            "proposal review must be required and pending before review",
            details={"review": review},
        )
    selected_reviewed_at = legacy.utc_now()
    plan["review"] = {
        "required": True,
        "status": "reviewed",
        "reviewer": checked_reviewer,
        "reviewed_at": selected_reviewed_at,
        "reviewed_proposal_sha256": proposal_sha256,
    }
    approval = require_approval(
        "registry_mutation",
        reviewed_plan_approval(
            reviewer=checked_reviewer,
            reference=proposal_sha256,
            task_id=task_id,
            scope="registry_mutation",
        ),
        expected_reference=proposal_sha256,
        task_id=task_id,
    )
    reviewed_bytes = (json.dumps(plan, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    plan_sha256_before = hashlib.sha256(plan_bytes).hexdigest()
    plan_sha256_after = hashlib.sha256(reviewed_bytes).hexdigest()
    parent_descriptor = -1
    temporary_path: Path | None = None
    exchanged = False
    try:
        parent_descriptor = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        if not _directory_path_matches_descriptor(path.parent, parent_descriptor):
            raise OperatorIntakeError(
                "proposal-review-parent-changed",
                "proposal parent changed before temporary-file binding",
                retryable=True,
                details={"path": str(path), "effect_started": False},
            )
        descriptor, raw_temporary = tempfile.mkstemp(
            prefix=f".{path.name}.review-",
            dir=f"/proc/self/fd/{parent_descriptor}",
        )
        temporary_path = Path(raw_temporary)
        try:
            os.fchmod(descriptor, 0o600)
            offset = 0
            while offset < len(reviewed_bytes):
                written = os.write(descriptor, reviewed_bytes[offset:])
                if written <= 0:
                    raise OSError(errno.EIO, "proposal review write stalled")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _before_proposal_review_exchange(path)
        if not _directory_path_matches_descriptor(path.parent, parent_descriptor):
            raise OperatorIntakeError(
                "proposal-review-parent-changed",
                "proposal parent changed before the atomic review exchange",
                retryable=True,
                details={"path": str(path), "effect_started": False},
            )
        _rename_exchange(
            temporary_path.name,
            path.name,
            source_dir_fd=parent_descriptor,
            target_dir_fd=parent_descriptor,
        )
        exchanged = True
        os.fsync(parent_descriptor)
        displaced_bytes, displaced_identity = _read_bounded_regular_file(
            temporary_path,
            field="proposal-displaced",
        )
        if displaced_bytes != plan_bytes or displaced_identity[:4] != plan_identity[:4]:
            _rename_exchange(
                temporary_path.name,
                path.name,
                source_dir_fd=parent_descriptor,
                target_dir_fd=parent_descriptor,
            )
            exchanged = False
            os.fsync(parent_descriptor)
            os.unlink(temporary_path.name, dir_fd=parent_descriptor)
            temporary_path = None
            os.fsync(parent_descriptor)
            raise OperatorIntakeError(
                "proposal-review-conflict",
                "proposal changed before the atomic review exchange; foreign bytes were restored",
                retryable=True,
                details={"path": str(path), "rollback_complete": True},
            )
        _after_proposal_review_exchange(path)
        if not _directory_path_matches_descriptor(path.parent, parent_descriptor):
            raise OperatorIntakeError(
                "proposal-review-parent-ambiguous",
                "proposal parent changed after the atomic review exchange",
                retryable=False,
                effect_started=True,
                ambiguity=True,
                required_readback=[
                    f"directory identity for {path.parent}",
                    f"exact proposal bytes at {path}",
                ],
                details={
                    "path": str(path),
                    "displaced_path": str(temporary_path),
                },
            )
        bound_target_path = Path(f"/proc/self/fd/{parent_descriptor}") / path.name
        observed_bytes, _ = _read_bounded_regular_file(
            bound_target_path,
            field="proposal-reviewed",
        )
        if observed_bytes != reviewed_bytes:
            raise OperatorIntakeError(
                "proposal-review-readback-ambiguous",
                "review exchange completed but exact reviewed bytes were not readable",
                retryable=False,
                effect_started=True,
                ambiguity=True,
                required_readback=[f"exact proposal bytes at {path}"],
                details={
                    "path": str(path),
                    "expected_plan_file_sha256": plan_sha256_after,
                    "observed_plan_file_sha256": hashlib.sha256(observed_bytes).hexdigest(),
                    "displaced_path": str(temporary_path),
                },
            )
        if not _directory_path_matches_descriptor(path.parent, parent_descriptor):
            raise OperatorIntakeError(
                "proposal-review-parent-ambiguous",
                "proposal parent changed before review completion",
                retryable=False,
                effect_started=True,
                ambiguity=True,
                required_readback=[
                    f"directory identity for {path.parent}",
                    f"exact proposal bytes at {path}",
                ],
                details={
                    "path": str(path),
                    "displaced_path": str(temporary_path),
                },
            )
        os.unlink(temporary_path.name, dir_fd=parent_descriptor)
        temporary_path = None
        os.fsync(parent_descriptor)
    except OperatorIntakeError as exc:
        if temporary_path is not None and not exchanged:
            with contextlib.suppress(OSError):
                os.unlink(temporary_path.name, dir_fd=parent_descriptor)
                os.fsync(parent_descriptor)
        if exchanged and not (exc.effect_started and exc.ambiguity):
            raise OperatorIntakeError(
                "proposal-review-effect-ambiguous",
                "review exchange started before a typed verification failure",
                retryable=False,
                effect_started=True,
                ambiguity=True,
                required_readback=[f"exact proposal bytes at {path}"],
                details={
                    "path": str(path),
                    "cause_code": exc.code,
                    "displaced_path": str(temporary_path) if temporary_path is not None else None,
                },
            ) from exc
        raise
    except OSError as exc:
        raise OperatorIntakeError(
            "proposal-review-write-ambiguous" if exchanged else "proposal-review-write-failed",
            f"cannot atomically bind proposal review: {exc}",
            retryable=not exchanged,
            effect_started=exchanged,
            ambiguity=exchanged,
            required_readback=[f"exact proposal bytes at {path}"] if exchanged else [],
            details={
                "path": str(path),
                "error_type": type(exc).__name__,
                "displaced_path": str(temporary_path) if temporary_path is not None else None,
            },
        ) from exc
    except Exception as exc:
        if not exchanged:
            raise
        raise OperatorIntakeError(
            "proposal-review-effect-ambiguous",
            "review exchange started before an unexpected verification failure",
            retryable=False,
            effect_started=True,
            ambiguity=True,
            required_readback=[f"exact proposal bytes at {path}"],
            details={
                "path": str(path),
                "error_type": type(exc).__name__,
                "displaced_path": str(temporary_path) if temporary_path is not None else None,
            },
        ) from exc
    finally:
        if temporary_path is not None and not exchanged and parent_descriptor >= 0:
            with contextlib.suppress(OSError):
                os.unlink(temporary_path.name, dir_fd=parent_descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)
    return {
        "schema_version": OPERATOR_INTAKE_SCHEMA_VERSION,
        "kind": "bureau_task_review_result",
        "status": "reviewed",
        "effect_started": True,
        "retryable": False,
        "ambiguity": False,
        "required_readback": [],
        "idempotent_replay": False,
        "path": str(path),
        "proposal_sha256": proposal_sha256,
        "plan_file_sha256_before": plan_sha256_before,
        "plan_file_sha256_after": plan_sha256_after,
        "review": plan["review"],
        "approval": approval,
        "does_not_establish": [
            "registry_mutation",
            "queue_mutation",
            "publication_effect",
            *candidate_authority_nonclaims(),
        ],
    }


def _validated_proposal(
    registry: Registry,
    store: StateStore,
    *,
    plan_path: Path,
) -> tuple[dict[str, Any], bytes, dict[str, Any]]:
    plan_bytes, _ = _read_bounded_regular_file(plan_path, field="proposal")
    try:
        plan = json.loads(plan_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise OperatorIntakeError(
            "proposal-json-invalid", f"cannot parse task proposal: {exc}"
        ) from exc
    if not isinstance(plan, dict) or plan.get("kind") != "bureau_operator_task_proposal":
        raise OperatorIntakeError("proposal-kind-invalid", "unsupported operator task proposal")
    expected_proposal_sha = legacy.sha256_json(_proposal_unsigned(plan))
    if plan.get("proposal_sha256") != expected_proposal_sha:
        raise OperatorIntakeError(
            "proposal-integrity-invalid", "task proposal hash does not match its content"
        )
    review = plan.get("review")
    if not isinstance(review, dict):
        raise OperatorIntakeError("review-invalid", "proposal review must be an object")
    if review.get("status") != "reviewed":
        raise OperatorIntakeError("review-missing", "proposal review.status must be reviewed")
    reviewer = _checked_text(review.get("reviewer"), field="reviewer", maximum=200)
    _checked_text(review.get("reviewed_at"), field="reviewed_at", maximum=80)
    if review.get("reviewed_proposal_sha256") != expected_proposal_sha:
        raise OperatorIntakeError(
            "review-binding-invalid",
            "reviewed_proposal_sha256 does not match proposal_sha256",
        )
    approval = reviewed_plan_approval(
        reviewer=str(reviewer),
        reference=expected_proposal_sha,
        task_id=str(plan.get("task_id")),
        scope="registry_mutation",
    )
    approval_result = require_approval(
        "registry_mutation",
        approval,
        expected_reference=expected_proposal_sha,
        task_id=str(plan.get("task_id")),
    )
    registry, _identity = _canonical_registry_snapshot(registry)
    publishing_task_id = str(plan.get("publishing_task_id", ""))
    publishing_task = store.task_spec(publishing_task_id)
    if publishing_task is None:
        raise OperatorIntakeError(
            "publishing-task-unknown",
            f"publishing task {publishing_task_id} is not in the authoritative StateStore",
        )
    planned_publishing_sha = plan.get("publishing_task_sha256")
    legacy_projection = registry.tasks.get(publishing_task_id)
    accepted_publishing_digests = {str(publishing_task["spec_sha256"])}
    if legacy_projection is not None:
        accepted_publishing_digests.add(legacy_projection.sha256)
    if planned_publishing_sha not in accepted_publishing_digests:
        raise OperatorIntakeError(
            "publishing-task-drift",
            "publishing task binding does not match the authoritative StateStore revision",
        )
    candidate = plan.get("candidate")
    if not isinstance(candidate, dict):
        raise OperatorIntakeError("candidate-binding-invalid", "proposal candidate is invalid")
    current = current_candidate_record(store, candidate_id=str(candidate.get("candidate_id")))
    if int(current["event_id"]) != int(candidate.get("event_id", -1)):
        raise OperatorIntakeError(
            "candidate-drift",
            "candidate was superseded after proposal creation",
            retryable=True,
        )
    task_json = plan.get("task_json")
    if not isinstance(task_json, dict):
        raise OperatorIntakeError("task-json-invalid", "proposal task_json is missing")
    _validate_task_schema(registry, task_json)
    task_spec_binding = _validate_task_spec_proposal_binding(
        registry, store, plan=plan, task_json=task_json, event=current
    )
    current_task_spec = store.task_spec(str(task_json.get("id", "")))
    allow_existing_task_id = task_spec_binding["operation"] == "revise"
    if task_spec_binding["operation"] == "register" and current_task_spec is not None:
        _validate_register_replay_mutation(
            store,
            task_id=str(task_json.get("id", "")),
            proposal_sha256=str(plan.get("proposal_sha256", "")),
            proposed_spec_sha256=task_spec_binding["proposed_spec_sha256"],
        )
        allow_existing_task_id = True
    _validate_task_semantics(
        registry,
        store,
        task_json,
        allow_existing_task_id=allow_existing_task_id,
    )
    content = _render_task(task_json)
    target_path = str(plan.get("target_path"))
    expected_path = f"registry/tasks/{task_json.get('id')}.json"
    if target_path != expected_path:
        raise OperatorIntakeError("target-path-invalid", f"target path must be {expected_path}")
    if plan.get("task_json_sha256") != legacy.sha256_json(task_json):
        raise OperatorIntakeError("task-json-drift", "task_json_sha256 does not match task_json")
    if plan.get("task_file_sha256") != hashlib.sha256(content).hexdigest():
        raise OperatorIntakeError(
            "task-file-drift", "task_file_sha256 does not match rendered task JSON"
        )
    if plan.get("proposed_diff_sha256") != _task_change_sha256(
        target_path,
        content,
        before_sha256=task_spec_binding["expected_task_file_sha256"],
    ):
        raise OperatorIntakeError(
            "proposal-diff-drift", "proposed_diff_sha256 does not match task file change"
        )
    unresolved = plan.get("unresolved_fields")
    if not isinstance(unresolved, list) or unresolved:
        raise OperatorIntakeError(
            "proposal-unresolved",
            "reviewed proposal still contains unresolved fields",
            details={"unresolved_fields": unresolved},
        )
    return plan, plan_bytes, approval_result




def publication_preview(
    registry: Registry,
    store: StateStore,
    *,
    plan_path: str | Path,
    github_repository: str | None = None,
    open_pr_runner: Callable[[list[str]], str] | None = None,
) -> dict[str, Any]:
    """Preview the StateStore-only reviewed TaskSpec publication contract."""
    del github_repository, open_pr_runner
    path = Path(plan_path).expanduser().absolute()
    plan, plan_bytes, approval_result = _validated_proposal(registry, store, plan_path=path)
    state_root = store.state_root.expanduser().resolve()
    task_id = str(plan["task_id"])
    return {
        "schema_version": OPERATOR_INTAKE_SCHEMA_VERSION,
        "kind": "bureau_task_publication_preview",
        "status": "ready",
        "effect_started": False,
        "retryable": False,
        "ambiguity": False,
        "required_readback": [],
        "publication_mode": "state_store",
        "coordination_state_root": str(state_root),
        "plan_path": str(path),
        "plan_file_sha256": hashlib.sha256(plan_bytes).hexdigest(),
        "proposal_sha256": plan["proposal_sha256"],
        "publishing_task_sha256": plan["publishing_task_sha256"],
        "task_id": task_id,
        "target_path": plan["target_path"],
        "branch": None,
        "required_resource_keys": [f"path:{state_root}"],
        "open_pr_identity_revalidation": {
            "status": "not_required",
            "reason": "state_store_is_operational_task_authority",
            "semantic_similarity_consulted": False,
            "does_not_establish": ["github_open_pr_absence", "git_publication_authority"],
        },
        "approval": approval_result,
        "does_not_establish": [
            "lease_ownership",
            "git_task_projection",
            "branch_creation",
            "pull_request_creation",
            "queue_mutation",
            "task_readiness",
            "merge_readiness",
            *candidate_authority_nonclaims(),
        ],
    }



def _release_unchanged_publication_leases(binding: dict[str, Any]) -> dict[str, Any]:
    """Release only the exact lease rows observed before a proven local outcome."""
    path = Path(str(binding["resource_db"]))
    snapshots = binding.get("lease_snapshots")
    if not isinstance(snapshots, list) or not snapshots:
        raise OperatorIntakeError(
            "lease-release-evidence-invalid",
            "publication lease release requires exact observed lease snapshots",
        )
    try:
        connection = sqlite3.connect(path, timeout=5, isolation_level=None)
    except sqlite3.Error as exc:
        raise OperatorIntakeError(
            "lease-release-failed",
            f"cannot open publication lease database for exact release: {exc}",
        ) from exc
    try:
        connection.execute("BEGIN IMMEDIATE")
        for snapshot in snapshots:
            row = connection.execute(
                "SELECT owner_id, acquired_at_unix, updated_at_unix, expires_at_unix, "
                "metadata_sha256 FROM leases WHERE resource_key=?",
                (snapshot["resource_key"],),
            ).fetchone()
            expected = (
                snapshot["owner_id"],
                snapshot["acquired_at_unix"],
                snapshot["updated_at_unix"],
                snapshot["expires_at_unix"],
                snapshot["metadata_sha256"],
            )
            if row != expected:
                raise OperatorIntakeError(
                    "lease-release-binding-changed",
                    "publication leases changed after validation; none were released",
                    details={"resource_key": snapshot["resource_key"]},
                )
        for snapshot in snapshots:
            cursor = connection.execute(
                "DELETE FROM leases WHERE resource_key=? AND owner_id=? "
                "AND acquired_at_unix=? AND updated_at_unix=? AND expires_at_unix=? "
                "AND metadata_sha256=?",
                (
                    snapshot["resource_key"],
                    snapshot["owner_id"],
                    snapshot["acquired_at_unix"],
                    snapshot["updated_at_unix"],
                    snapshot["expires_at_unix"],
                    snapshot["metadata_sha256"],
                ),
            )
            if cursor.rowcount != 1:
                raise OperatorIntakeError(
                    "lease-release-binding-changed",
                    "an exact publication lease disappeared during release",
                    details={"resource_key": snapshot["resource_key"]},
                )
        connection.commit()
    except OperatorIntakeError:
        with contextlib.suppress(sqlite3.Error):
            connection.rollback()
        raise
    except sqlite3.Error as exc:
        with contextlib.suppress(sqlite3.Error):
            connection.rollback()
        raise OperatorIntakeError(
            "lease-release-failed",
            f"cannot atomically release exact publication leases: {exc}",
        ) from exc
    finally:
        connection.close()
    return {
        "released": True,
        "owner_id": binding["owner_id"],
        "resource_keys": [item["resource_key"] for item in snapshots],
        "lease_binding_sha256": binding["lease_binding_sha256"],
    }






def publish_task_proposal(
    registry: Registry,
    store: StateStore,
    *,
    plan_path: str | Path,
    lease_binding: dict[str, Any],
    workspace_root: str | Path,
    receipt_path: str | Path,
    resource_db: str | Path = DEFAULT_GRABOWSKI_RESOURCE_DB,
) -> dict[str, Any]:
    """Publish one reviewed TaskSpec to the authoritative StateStore only."""
    del workspace_root
    path = Path(plan_path).expanduser().absolute()
    receipt = Path(receipt_path).expanduser().absolute()
    plan_bytes, _ = _read_bounded_regular_file(path, field="proposal")
    plan_file_sha = hashlib.sha256(plan_bytes).hexdigest()
    try:
        plan = json.loads(plan_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise OperatorIntakeError(
            "proposal-json-invalid", f"cannot parse task proposal: {exc}"
        ) from exc
    if not isinstance(plan, dict):
        raise OperatorIntakeError(
            "proposal-object-required", "task proposal JSON must be an object"
        )

    state_root = store.state_root.expanduser().resolve()
    if os.path.lexists(receipt):
        receipt_bytes, _ = _read_bounded_regular_file(receipt, field="receipt")
        try:
            existing = json.loads(receipt_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise OperatorIntakeError(
                "receipt-invalid", f"cannot parse publication receipt: {exc}"
            ) from exc
        if not isinstance(existing, dict):
            raise OperatorIntakeError(
                "receipt-invalid", "publication receipt JSON must be an object"
            )
        unsigned_receipt = {
            key: value for key, value in existing.items() if key != "receipt_sha256"
        }
        publication = existing.get("publication")
        receipt_valid = (
            existing.get("kind") == "bureau_task_publication_receipt"
            and existing.get("status") == "published"
            and existing.get("publication_mode") == "state_store"
            and existing.get("coordination_state_root") == str(state_root)
            and existing.get("receipt_sha256") == legacy.sha256_json(unsigned_receipt)
            and isinstance(publication, dict)
            and publication.get("mode") == "state_store"
            and publication.get("readback_complete") is True
        )
        if not receipt_valid:
            raise OperatorIntakeError(
                "receipt-integrity-invalid",
                "existing publication receipt is not a valid completed StateStore receipt",
            )
        if (
            existing.get("proposal_sha256") == plan.get("proposal_sha256")
            and existing.get("plan_file_sha256") == plan_file_sha
        ):
            return {**existing, "idempotent_replay": True, "receipt_path": str(receipt)}
        raise OperatorIntakeError(
            "receipt-conflict", "existing publication receipt belongs to a different plan"
        )

    preview = publication_preview(registry, store, plan_path=path)
    if lease_binding.get("task_id") != plan["publishing_task_id"]:
        raise OperatorIntakeError(
            "lease-task-mismatch",
            "lease binding task_id must match the registered publishing task",
            details={
                "expected": plan["publishing_task_id"],
                "observed": lease_binding.get("task_id"),
            },
        )
    try:
        normalized_leases = validate_live_lease_binding(
            {"required_resource_keys": preview["required_resource_keys"]},
            lease_binding,
            resource_db=Path(resource_db),
            min_remaining_seconds=60,
            required_metadata={
                "task_id": plan["publishing_task_id"],
                "operation": "state-task-publication",
                "proposal_sha256": plan["proposal_sha256"],
            },
        )
    except RuntimeRefreshError as exc:
        raise OperatorIntakeError(
            exc.code,
            str(exc),
            retryable=exc.code
            in {
                "lease-database-read-failed",
                "lease-expired",
                "lease-resources-missing",
            },
            details=exc.details,
        ) from exc

    current_plan_bytes, _ = _read_bounded_regular_file(path, field="plan")
    if hashlib.sha256(current_plan_bytes).hexdigest() != plan_file_sha:
        raise OperatorIntakeError(
            "plan-file-drift",
            "reviewed plan bytes changed before StateStore publication effect",
            retryable=True,
        )

    task_spec_binding = plan["task_spec"]
    expected_revision = task_spec_binding["expected_revision"]
    try:
        task_spec_revision = store.put_task_spec(
            plan["task_json"],
            idempotency_key=f"operator-intake:{plan['proposal_sha256']}",
            expected_revision=expected_revision,
            source="operator-intake-reviewed-proposal",
        )
    except StateError as exc:
        raise OperatorIntakeError(
            "task-spec-state-mutation-failed",
            f"cannot register reviewed TaskSpec in StateStore: {exc}",
            retryable=False,
            effect_started=False,
            details={"task_id": plan["task_id"]},
        ) from exc
    if task_spec_revision["spec_sha256"] != plan["task_json_sha256"]:
        raise OperatorIntakeError(
            "task-spec-digest-divergence",
            "StateStore TaskSpec digest differs from reviewed proposal digest",
            effect_started=True,
            required_readback=["StateStore TaskSpec revision"],
            details={
                "task_id": plan["task_id"],
                "state_store_sha256": task_spec_revision["spec_sha256"],
                "proposal_sha256": plan["task_json_sha256"],
            },
        )
    current = store.task_spec(plan["task_id"])
    if (
        current is None
        or current.get("revision") != task_spec_revision.get("revision")
        or current.get("spec_sha256") != task_spec_revision.get("spec_sha256")
        or current.get("spec") != plan["task_json"]
    ):
        raise OperatorIntakeError(
            "task-spec-state-readback-ambiguous",
            "StateStore post-publication readback differs from the reviewed TaskSpec revision",
            effect_started=True,
            ambiguity=True,
            retryable=False,
            required_readback=[f"StateStore TaskSpec {plan['task_id']}"],
        )
    try:
        projection = store.replay_projection()
    except Exception as exc:
        raise OperatorIntakeError(
            "task-spec-projection-postcommit-failed",
            "StateStore TaskSpec publication committed but projection replay failed",
            retryable=False,
            effect_started=bool(task_spec_revision.get("changed", True)),
            ambiguity=True,
            required_readback=[
                f"StateStore TaskSpec {plan['task_id']}",
                "StateStore projection replay",
                "publication lease rows",
            ],
            details={
                "task_id": plan["task_id"],
                "task_spec_revision": task_spec_revision,
                "cause_type": type(exc).__name__,
            },
            publication_phase="committed_locally",
        ) from exc
    try:
        lease_release = _release_unchanged_publication_leases(normalized_leases)
    except OperatorIntakeError as exc:
        raise OperatorIntakeError(
            "lease-release-failed",
            f"StateStore publication succeeded but exact lease release failed: {exc}",
            effect_started=True,
            required_readback=["publication lease rows"],
            details={"task_spec_revision": task_spec_revision, "cause_code": exc.code},
            publication_phase="committed_locally",
        ) from exc

    publication = {
        "mode": "state_store",
        "readback_complete": True,
        "coordination_state_root": str(state_root),
        "task_id": plan["task_id"],
        "revision": task_spec_revision["revision"],
        "spec_sha256": task_spec_revision["spec_sha256"],
        "task_spec_root_sha256": projection["task_specs"]["root_sha256"],
        "authoritative_root_sha256": projection["authoritative_root_sha256"],
    }
    value: dict[str, Any] = {
        "schema_version": OPERATOR_INTAKE_SCHEMA_VERSION,
        "kind": "bureau_task_publication_receipt",
        "status": "published",
        "effect_started": bool(task_spec_revision.get("changed", True)),
        "retryable": False,
        "ambiguity": False,
        "required_readback": [],
        "publication_phase": "committed_locally",
        "publication_mode": "state_store",
        "coordination_state_root": str(state_root),
        "proposal_sha256": preview["proposal_sha256"],
        "plan_file_sha256": plan_file_sha,
        "task_id": preview["task_id"],
        "target_path": preview["target_path"],
        "branch": None,
        "registry": plan["registry"],
        "publishing_task_id": plan["publishing_task_id"],
        "publishing_task_sha256": plan["publishing_task_sha256"],
        "lease_binding": normalized_leases,
        "lease_release": lease_release,
        "task_spec_revision": task_spec_revision,
        "legacy_task_spec_import": {
            "status": "retired",
            "imported": 0,
            "reason": "StateStore is the sole operational TaskSpec authority",
        },
        "publication": publication,
        "created_at": legacy.utc_now(),
        "queue_mutated": False,
        "does_not_establish": [
            "git_task_projection",
            "task_readiness",
            "claim_or_dispatch_authority",
            "merge_or_deployment_authority",
            "task_verification",
        ],
    }
    unsigned = {key: item for key, item in value.items() if key != "receipt_sha256"}
    value["receipt_sha256"] = legacy.sha256_json(unsigned)
    receipt_bytes = (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        _write_create_only(receipt, receipt_bytes)
    except (OSError, OperatorIntakeError) as exc:
        raise OperatorIntakeError(
            "receipt-write-unclear",
            f"StateStore publication succeeded but receipt write failed: {exc}",
            retryable=False,
            effect_started=True,
            ambiguity=True,
            required_readback=[
                f"StateStore TaskSpec {plan['task_id']}",
                f"publication receipt at {receipt}",
            ],
            details={
                "proposal_sha256": preview["proposal_sha256"],
                "publication": publication,
                "ambiguity_scope": "receipt",
            },
            publication_phase="committed_locally",
        ) from exc
    return {**value, "idempotent_replay": False, "receipt_path": str(receipt)}

def _read_task_promotion_publication_receipt(path: str | Path) -> dict[str, Any]:
    receipt_path = Path(path).expanduser().absolute()
    raw, _ = _read_bounded_regular_file(receipt_path, field="publication_receipt")
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise OperatorIntakeError(
            "promotion-publication-receipt-invalid",
            f"cannot parse publication receipt: {exc}",
            details={"path": str(receipt_path)},
        ) from exc
    if not isinstance(value, dict):
        raise OperatorIntakeError(
            "promotion-publication-receipt-invalid",
            "publication receipt JSON must be an object",
            details={"path": str(receipt_path)},
        )
    unsigned = {key: item for key, item in value.items() if key != "receipt_sha256"}
    if (
        value.get("kind") != "bureau_task_publication_receipt"
        or value.get("status") != "published"
        or value.get("receipt_sha256") != legacy.sha256_json(unsigned)
    ):
        raise OperatorIntakeError(
            "promotion-publication-receipt-integrity-invalid",
            "publication receipt identity or digest is invalid",
            details={"path": str(receipt_path)},
        )
    publication = value.get("publication")
    revision = value.get("task_spec_revision")
    task_id = value.get("task_id")
    proposal_sha256 = value.get("proposal_sha256")
    target_path = value.get("target_path")
    if (
        not isinstance(task_id, str)
        or not task_id
        or not isinstance(proposal_sha256, str)
        or not isinstance(target_path, str)
        or target_path != f"registry/tasks/{task_id}.json"
        or not isinstance(publication, dict)
        or publication.get("readback_complete") is not True
        or not isinstance(revision, dict)
        or revision.get("task_id") != task_id
        or not isinstance(revision.get("revision"), int)
        or revision["revision"] < 1
        or not isinstance(revision.get("spec_sha256"), str)
        or not isinstance(revision.get("spec"), dict)
    ):
        raise OperatorIntakeError(
            "promotion-publication-receipt-shape-invalid",
            "publication receipt is missing exact task or revision bindings",
        )
    mode = value.get("publication_mode", "git_pr")
    if mode == "state_store":
        state_root = value.get("coordination_state_root")
        if (
            not isinstance(state_root, str)
            or not state_root
            or not Path(state_root).is_absolute()
            or publication.get("mode") != "state_store"
            or publication.get("coordination_state_root") != state_root
            or publication.get("task_id") != task_id
            or publication.get("revision") != revision["revision"]
            or publication.get("spec_sha256") != revision["spec_sha256"]
        ):
            raise OperatorIntakeError(
                "promotion-publication-receipt-shape-invalid",
                "StateStore publication receipt lacks exact authority bindings",
            )
    elif mode == "git_pr":
        pull_request = publication.get("pull_request")
        if (
            not isinstance(publication.get("repository"), str)
            or not isinstance(publication.get("branch"), str)
            or not isinstance(publication.get("head"), str)
            or not isinstance(publication.get("target_file_sha256"), str)
            or not isinstance(pull_request, dict)
            or not isinstance(pull_request.get("number"), int)
            or pull_request["number"] < 1
        ):
            raise OperatorIntakeError(
                "promotion-publication-receipt-shape-invalid",
                "legacy Git publication receipt lacks exact PR bindings",
            )
    else:
        raise OperatorIntakeError(
            "promotion-publication-mode-invalid",
            f"unsupported publication mode: {mode}",
        )
    if revision["spec"].get("id") != task_id or revision["spec"].get("state") != "planned":
        raise OperatorIntakeError(
            "promotion-publication-spec-invalid",
            "publication receipt must bind the exact planned TaskSpec",
        )
    try:
        receipt_spec_sha256 = task_specs.task_spec_digest(revision["spec"])
    except task_specs.TaskSpecError as exc:
        raise OperatorIntakeError(
            "promotion-publication-spec-invalid",
            f"publication receipt TaskSpec is invalid: {exc}",
        ) from exc
    if receipt_spec_sha256 != revision["spec_sha256"]:
        raise OperatorIntakeError(
            "promotion-publication-spec-digest-mismatch",
            "publication receipt TaskSpec bytes do not match its bound digest",
        )
    return value

def _promotion_pull_request_readback(repository: str, number: int) -> dict[str, Any]:
    binary = os.environ.get("BUREAU_GH_BIN", "gh")
    command = [
        binary,
        "pr",
        "view",
        str(number),
        "--repo",
        repository,
        "--json",
        "number,state,mergedAt,mergeCommit,headRefOid,headRefName,baseRefName,url",
    ]
    try:
        process = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OperatorIntakeError(
            "promotion-pr-readback-unavailable",
            f"cannot read merged pull request: {type(exc).__name__}: {exc}",
            retryable=True,
            required_readback=["exact merged pull request identity"],
        ) from exc
    if process.returncode != 0:
        diagnostic = process.stderr.strip() or process.stdout.strip() or "no diagnostic"
        raise OperatorIntakeError(
            "promotion-pr-readback-unavailable",
            f"gh pr view failed: {diagnostic}",
            retryable=True,
            required_readback=["exact merged pull request identity"],
        )
    try:
        value = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise OperatorIntakeError(
            "promotion-pr-readback-invalid",
            f"gh pr view returned invalid JSON: {exc}",
            required_readback=["exact merged pull request identity"],
        ) from exc
    if not isinstance(value, dict):
        raise OperatorIntakeError(
            "promotion-pr-readback-invalid",
            "gh pr view returned a non-object payload",
            required_readback=["exact merged pull request identity"],
        )
    return value


def _promotion_merge_identity(
    publication_receipt: dict[str, Any],
) -> dict[str, Any]:
    publication = publication_receipt["publication"]
    pull_request = publication["pull_request"]
    repository = publication["repository"]
    number = pull_request["number"]
    readback = _promotion_pull_request_readback(repository, number)
    merge_commit = readback.get("mergeCommit")
    merge_commit_oid = merge_commit.get("oid") if isinstance(merge_commit, dict) else None
    if (
        readback.get("number") != number
        or readback.get("state") != "MERGED"
        or not isinstance(readback.get("mergedAt"), str)
        or not readback["mergedAt"]
        or readback.get("headRefOid") != publication["head"]
        or readback.get("headRefName") != publication["branch"]
        or readback.get("baseRefName") != "main"
        or not isinstance(merge_commit_oid, str)
        or not merge_commit_oid
    ):
        raise OperatorIntakeError(
            "promotion-pr-identity-mismatch",
            "pull request is not an exact merged readback of the published task branch",
            details={
                "expected": {
                    "number": number,
                    "head": publication["head"],
                    "branch": publication["branch"],
                    "base": "main",
                },
                "observed": readback,
            },
            required_readback=["exact merged pull request identity"],
        )
    return {
        "repository": repository,
        "number": number,
        "head": publication["head"],
        "branch": publication["branch"],
        "base": "main",
        "merged_at": readback["mergedAt"],
        "merge_commit": merge_commit_oid,
        "url": readback.get("url"),
    }


def _task_promotion_binding(
    registry: Registry,
    store: StateStore,
    publication_receipt: dict[str, Any],
) -> dict[str, Any]:
    task_id = publication_receipt["task_id"]
    revision = publication_receipt["task_spec_revision"]
    mode = publication_receipt.get("publication_mode", "git_pr")
    target_path = publication_receipt["target_path"]
    target_sha256: str | None = None

    try:
        with store.connect() as connection:
            state_projection = task_specs.current_projection(connection)
    except (task_specs.TaskSpecError, sqlite3.Error) as exc:
        raise OperatorIntakeError(
            "promotion-state-projection-invalid",
            f"cannot read authoritative TaskSpec projection: {exc}",
        ) from exc
    state_tasks = state_projection["tasks"]
    state_target = state_tasks.get(task_id)
    state_target_spec = state_target.get("spec", {}) if state_target is not None else {}
    state_target_metadata = state_target_spec.get("metadata")
    state_parent_task_id = (
        state_target_metadata.get("parent_task")
        if isinstance(state_target_metadata, dict)
        else None
    )
    state_child_task_ids = sorted(
        candidate_id
        for candidate_id, candidate in state_tasks.items()
        if candidate_id != task_id
        and isinstance(candidate.get("spec", {}).get("metadata"), dict)
        and candidate["spec"]["metadata"].get("parent_task") == task_id
    )

    if mode == "state_store":
        expected_root = str(store.state_root.expanduser().resolve())
        if publication_receipt.get("coordination_state_root") != expected_root:
            raise OperatorIntakeError(
                "promotion-state-root-identity-mismatch",
                "current StateStore root does not match the publication receipt",
                details={
                    "expected": publication_receipt.get("coordination_state_root"),
                    "observed": expected_root,
                },
            )
        parent_task_id = state_parent_task_id
        child_task_ids = state_child_task_ids
        publication_identity = {
            "mode": "state_store",
            "coordination_state_root": expected_root,
            "task_id": task_id,
            "revision": revision["revision"],
            "spec_sha256": revision["spec_sha256"],
        }
    else:
        publication = publication_receipt["publication"]
        repository = _github_repository_for_preview(registry.root)
        if repository is None or repository != publication["repository"]:
            raise OperatorIntakeError(
                "promotion-repository-identity-mismatch",
                "current Registry origin does not match the publication receipt repository",
                details={"expected": publication["repository"], "observed": repository},
            )
        task = registry.tasks.get(task_id)
        if task is None:
            raise OperatorIntakeError(
                "promotion-task-missing-in-registry",
                f"task {task_id} is missing from the current Registry",
            )
        target_file = registry.root / target_path
        target_bytes, _ = _read_bounded_regular_file(
            target_file, field="promotion_task_file"
        )
        target_sha256 = hashlib.sha256(target_bytes).hexdigest()
        if target_sha256 != publication["target_file_sha256"]:
            raise OperatorIntakeError(
                "promotion-task-file-drift",
                "current Registry task-file bytes differ from the publication receipt",
                details={
                    "expected": publication["target_file_sha256"],
                    "observed": target_sha256,
                },
            )
        try:
            registry_spec_sha256 = task_specs.task_spec_digest(task.raw)
        except task_specs.TaskSpecError as exc:
            raise OperatorIntakeError(
                "promotion-registry-task-invalid",
                f"current Registry TaskSpec is invalid: {exc}",
            ) from exc
        if registry_spec_sha256 != revision["spec_sha256"] or task.raw != revision["spec"]:
            raise OperatorIntakeError(
                "promotion-registry-spec-drift",
                "current Registry TaskSpec differs from the exact published planned spec",
            )
        family = registry.parent_child_projection(task_id)
        parent_task_id = family.parent_task_id or state_parent_task_id
        child_task_ids = sorted(set(family.child_task_ids) | set(state_child_task_ids))
        merge = _promotion_merge_identity(publication_receipt)
        publication_identity = {
            **merge,
            "target_path": target_path,
            "target_file_sha256": target_sha256,
        }

    if revision["spec"].get("depends_on") or parent_task_id or child_task_ids:
        raise OperatorIntakeError(
            "promotion-standalone-task-required",
            "direct readiness is allowed only for tasks without dependencies, parent or children",
            details={
                "depends_on": list(revision["spec"].get("depends_on") or []),
                "parent_task_id": parent_task_id,
                "child_task_ids": child_task_ids,
                "state_store_parent_task_id": state_parent_task_id,
                "state_store_child_task_ids": state_child_task_ids,
            },
        )
    ready_spec = json.loads(json.dumps(revision["spec"]))
    ready_spec["state"] = "ready"
    try:
        ready_sha256 = task_specs.task_spec_digest(ready_spec)
    except task_specs.TaskSpecError as exc:
        raise OperatorIntakeError(
            "promotion-ready-spec-invalid",
            f"planned-to-ready TaskSpec is invalid: {exc}",
        ) from exc
    return {
        "task_id": task_id,
        "proposal_sha256": publication_receipt["proposal_sha256"],
        "publication_receipt_sha256": publication_receipt["receipt_sha256"],
        "publication": publication_identity,
        "target_path": target_path,
        "target_file_sha256": target_sha256,
        "before": {
            "revision": revision["revision"],
            "spec_sha256": revision["spec_sha256"],
            "state": "planned",
        },
        "after": {
            "revision": revision["revision"] + 1,
            "spec_sha256": ready_sha256,
            "state": "ready",
        },
        "planned_spec": revision["spec"],
        "ready_spec": ready_spec,
    }

def _task_promotion_preflight(
    registry: Registry,
    store: StateStore,
    publication_receipt: dict[str, Any],
) -> dict[str, Any]:
    binding = _task_promotion_binding(registry, store, publication_receipt)
    current = store.task_spec(binding["task_id"])
    before = binding["before"]
    if (
        current is None
        or current.get("revision") != before["revision"]
        or current.get("spec_sha256") != before["spec_sha256"]
        or current.get("spec") != binding["planned_spec"]
        or current.get("spec", {}).get("state") != "planned"
    ):
        raise OperatorIntakeError(
            "promotion-state-preimage-mismatch",
            "authoritative StateStore no longer matches the exact published planned revision",
            details={
                "expected_revision": before["revision"],
                "expected_spec_sha256": before["spec_sha256"],
                "observed_revision": current.get("revision") if current else None,
                "observed_spec_sha256": current.get("spec_sha256") if current else None,
                "observed_state": current.get("spec", {}).get("state") if current else None,
            },
        )
    return binding


def _read_task_promotion_receipt(path: Path) -> dict[str, Any]:
    raw, _ = _read_bounded_regular_file(path, field="promotion_receipt")
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise OperatorIntakeError(
            "promotion-receipt-invalid",
            f"cannot parse promotion receipt: {exc}",
        ) from exc
    if not isinstance(value, dict):
        raise OperatorIntakeError(
            "promotion-receipt-invalid",
            "promotion receipt JSON must be an object",
        )
    unsigned = {key: item for key, item in value.items() if key != "receipt_sha256"}
    if (
        value.get("kind") != "bureau_task_readiness_promotion_receipt"
        or value.get("status") != "promoted"
        or value.get("receipt_sha256") != legacy.sha256_json(unsigned)
    ):
        raise OperatorIntakeError(
            "promotion-receipt-integrity-invalid",
            "promotion receipt identity or digest is invalid",
        )
    return value


def _task_promotion_replay(
    registry: Registry,
    store: StateStore,
    publication_receipt: dict[str, Any],
    promotion_receipt_path: Path,
) -> dict[str, Any]:
    binding = _task_promotion_binding(registry, store, publication_receipt)
    receipt = _read_task_promotion_receipt(promotion_receipt_path)
    expected_publication = binding["publication"]
    if (
        receipt.get("task_id") != binding["task_id"]
        or receipt.get("proposal_sha256") != binding["proposal_sha256"]
        or receipt.get("publication_receipt_sha256") != binding["publication_receipt_sha256"]
        or receipt.get("publication") != expected_publication
        or receipt.get("before") != binding["before"]
        or receipt.get("after") != binding["after"]
        or receipt.get("queue_mutated") is not False
    ):
        raise OperatorIntakeError(
            "promotion-receipt-binding-mismatch",
            "promotion receipt does not bind the exact current publication "
            "and readiness transition",
        )
    current = store.task_spec(binding["task_id"])
    after = binding["after"]
    if (
        current is None
        or current.get("revision") != after["revision"]
        or current.get("spec_sha256") != after["spec_sha256"]
        or current.get("spec") != binding["ready_spec"]
        or current.get("spec", {}).get("state") != "ready"
    ):
        raise OperatorIntakeError(
            "promotion-readback-mismatch",
            "StateStore no longer matches the exact receipt-bound promoted revision",
            details={
                "expected_revision": after["revision"],
                "expected_spec_sha256": after["spec_sha256"],
                "observed_revision": current.get("revision") if current else None,
                "observed_spec_sha256": current.get("spec_sha256") if current else None,
                "observed_state": current.get("spec", {}).get("state") if current else None,
            },
        )
    return {
        **receipt,
        "idempotent_replay": True,
        "readback_verified": True,
        "receipt_path": str(promotion_receipt_path),
    }


def promote_task_ready(
    registry: Registry,
    store: StateStore,
    *,
    publication_receipt_path: str | Path,
    promotion_receipt_path: str | Path | None = None,
    mode: str = "preview",
) -> dict[str, Any]:
    """Preview, apply or read back one exact standalone post-merge readiness promotion."""
    if mode not in {"preview", "apply", "readback"}:
        raise OperatorIntakeError("promotion-mode-invalid", f"unsupported promotion mode: {mode}")
    publication_receipt = _read_task_promotion_publication_receipt(publication_receipt_path)
    receipt_path = (
        Path(promotion_receipt_path).expanduser().absolute()
        if promotion_receipt_path is not None
        else None
    )
    if mode == "preview":
        if receipt_path is not None:
            raise OperatorIntakeError(
                "promotion-preview-receipt-forbidden",
                "preview does not accept a promotion receipt path",
            )
        binding = _task_promotion_preflight(registry, store, publication_receipt)
        return {
            "schema_version": OPERATOR_INTAKE_SCHEMA_VERSION,
            "kind": "bureau_task_readiness_promotion_preview",
            "status": "ready",
            "task_id": binding["task_id"],
            "proposal_sha256": binding["proposal_sha256"],
            "publication_receipt_sha256": binding["publication_receipt_sha256"],
            "publication": binding["publication"],
            "before": binding["before"],
            "after": binding["after"],
            "read_only": True,
            "effect_started": False,
            "does_not_establish": ["promotion_effect", "queue_membership", "claimability"],
        }
    if receipt_path is None:
        raise OperatorIntakeError(
            "promotion-receipt-required",
            f"{mode} requires a promotion receipt path",
        )
    if os.path.lexists(receipt_path):
        return _task_promotion_replay(registry, store, publication_receipt, receipt_path)
    if mode == "readback":
        raise OperatorIntakeError(
            "promotion-receipt-missing",
            "readback requires an existing promotion receipt",
            details={"path": str(receipt_path)},
        )

    binding = _task_promotion_preflight(registry, store, publication_receipt)
    try:
        written = store.put_task_spec(
            binding["ready_spec"],
            idempotency_key=f"operator-intake-ready:{binding['publication_receipt_sha256']}",
            expected_revision=binding["before"]["revision"],
            source="operator-intake-postmerge-readiness",
        )
    except StateError as exc:
        raise OperatorIntakeError(
            "promotion-state-cas-conflict",
            f"planned-to-ready CAS was rejected: {exc}",
            required_readback=[f"StateStore TaskSpec {binding['task_id']}"],
        ) from exc
    after = binding["after"]
    if (
        written.get("revision") != after["revision"]
        or written.get("parent_revision") != binding["before"]["revision"]
        or written.get("spec_sha256") != after["spec_sha256"]
        or written.get("spec") != binding["ready_spec"]
        or written.get("changed") is not True
        or written.get("idempotent_replay") is not False
    ):
        raise OperatorIntakeError(
            "promotion-state-write-ambiguous",
            "StateStore write returned an unexpected promoted revision",
            effect_started=True,
            ambiguity=True,
            retryable=False,
            required_readback=[f"StateStore TaskSpec {binding['task_id']}"],
            details={"write_result": written},
        )
    current = store.task_spec(binding["task_id"])
    if (
        current is None
        or current.get("revision") != after["revision"]
        or current.get("spec_sha256") != after["spec_sha256"]
        or current.get("spec") != binding["ready_spec"]
    ):
        raise OperatorIntakeError(
            "promotion-state-readback-ambiguous",
            "StateStore post-CAS readback does not match the promoted revision",
            effect_started=True,
            ambiguity=True,
            retryable=False,
            required_readback=[f"StateStore TaskSpec {binding['task_id']}"],
        )
    publication_binding = binding["publication"]
    value = {
        "schema_version": OPERATOR_INTAKE_SCHEMA_VERSION,
        "kind": "bureau_task_readiness_promotion_receipt",
        "status": "promoted",
        "task_id": binding["task_id"],
        "proposal_sha256": binding["proposal_sha256"],
        "publication_receipt_sha256": binding["publication_receipt_sha256"],
        "publication": publication_binding,
        "before": binding["before"],
        "after": binding["after"],
        "queue_mutated": False,
        "effect_started": True,
        "created_at": legacy.utc_now(),
        "does_not_establish": ["queue_membership", "claim", "dispatch", "task_completion"],
    }
    unsigned = {key: item for key, item in value.items() if key != "receipt_sha256"}
    value["receipt_sha256"] = legacy.sha256_json(unsigned)
    encoded = (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    try:
        _write_create_only(receipt_path, encoded)
    except (OSError, OperatorIntakeError) as exc:
        raise OperatorIntakeError(
            "promotion-receipt-write-ambiguous",
            f"readiness promotion succeeded but receipt publication failed: {exc}",
            effect_started=True,
            ambiguity=True,
            retryable=False,
            required_readback=[
                f"StateStore TaskSpec {binding['task_id']}",
                f"promotion receipt at {receipt_path}",
            ],
        ) from exc
    return {
        **value,
        "idempotent_replay": False,
        "readback_verified": True,
        "receipt_path": str(receipt_path),
    }
