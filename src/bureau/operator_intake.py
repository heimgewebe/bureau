from __future__ import annotations

import contextlib
import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from . import legacy
from .approval import (
    approval_decision,
    require_approval,
    reviewed_plan_approval,
    task_approval_contract,
)
from .core import Registry, StateError, StateStore
from .lease_contract import (
    BUREAU_REGISTRY_PUBLICATION_GATE_KEY,
    BUREAU_REPOSITORY_ROOT,
)
from .live_register import (
    ACTIVE_LIVE_STATUSES,
    candidate_records,
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
from .worktree_hygiene import _process_references

OPERATOR_INTAKE_SCHEMA_VERSION = 1
MAX_SIMILARITY_RESULTS = 5
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
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
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


class TaskPublisher(Protocol):
    def publish(
        self,
        *,
        registry: Registry,
        plan: dict[str, Any],
        workspace_root: Path,
        assert_plan_unchanged: Callable[[], None],
        phase_changed: Callable[[str], None],
    ) -> dict[str, Any]: ...


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


def _request_sha256(value: dict[str, Any]) -> str:
    return legacy.sha256_json(value)


def _candidate_id_for_key(key: str) -> str:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return f"candidate-{digest[:24]}"


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
            "created_at": observed["created_at"],
            "request_sha256": request_sha256,
            "record": observed["record"],
            "does_not_establish": observed["record"].get("does_not_establish", []),
        }
    return None


def candidate_record_request(
    registry: Registry | None,
    store: StateStore,
    request: dict[str, Any],
) -> dict[str, Any]:
    """Validate the versioned JSON transport request before domain dispatch."""
    allowed = {
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
        "note",
        "catalog_validation",
    }
    if request.get("schema_version") != OPERATOR_INTAKE_SCHEMA_VERSION:
        raise OperatorIntakeError(
            "request-schema-unsupported",
            f"candidate request schema_version must be {OPERATOR_INTAKE_SCHEMA_VERSION}",
        )
    unknown = sorted(set(request) - allowed)
    if unknown:
        raise OperatorIntakeError(
            "request-fields-unknown",
            "candidate request contains unknown fields",
            details={"unknown_fields": unknown},
        )
    payload = {key: value for key, value in request.items() if key != "schema_version"}
    return candidate_record(registry, store, **payload)


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
    request_sha = _request_sha256(request)
    replayed = _candidate_idempotency_result(store, key=key, request_sha256=request_sha)
    if replayed is not None:
        return replayed

    bound_registry = registry
    if registry is not None:
        bound_registry, _ = _canonical_read_registry_snapshot(registry)

    generated_observed_at = checked_observed or legacy.utc_now()
    selected_candidate_id = candidate_id or _candidate_id_for_key(key)
    context = {
        "schema_version": OPERATOR_INTAKE_SCHEMA_VERSION,
        "idempotency_key": key,
        "request_sha256": request_sha,
        "source": {
            "kind": checked_kind,
            "locator": checked_locator,
            "sha256": checked_sha,
            "observed_at": generated_observed_at,
            "freshness": "digest-bound" if checked_sha else "unknown",
            "does_not_establish": [] if checked_sha else ["source_content_identity"],
        },
        "desired_outcome": checked_outcome,
        "does_not_establish": [
            "registry_task_truth",
            "queue_truth",
            "task_readiness",
            "claim_or_dispatch_authority",
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
            status="observed",
            promotion_required=True,
            note=checked_note or str(checked_outcome),
            catalog_validation=catalog_validation,
            operator_context=context,
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
    return {
        "schema_version": OPERATOR_INTAKE_SCHEMA_VERSION,
        "kind": "bureau_candidate_record_result",
        "status": "recorded",
        "effect_started": True,
        "retryable": False,
        "ambiguity": False,
        "required_readback": [],
        "idempotent_replay": False,
        "candidate_id": selected_candidate_id,
        "event_id": recorded["event_id"],
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
    initiative: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Assess one current candidate without changing Registry or Live Register truth."""
    event = current_candidate_record(store, candidate_id=candidate_id, event_id=event_id)
    record = event["record"]
    identity = _candidate_identity(event)
    context = _operator_context(record)
    selected_initiative = initiative
    if selected_initiative is not None and selected_initiative not in registry.initiatives:
        raise OperatorIntakeError("initiative-unknown", f"unknown initiative {selected_initiative}")
    exact: list[dict[str, Any]] = []
    source = context.get("source") if isinstance(context.get("source"), dict) else {}
    source_sha = source.get("sha256")
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
            exact.append(
                {
                    "kind": "task-source-digest",
                    "task_id": existing.id,
                    "reason": "same source_sha256",
                }
            )
    if task_id and task_id in registry.tasks:
        exact.append({"kind": "task-id", "task_id": task_id, "reason": "task_id exists"})
    for other in current_candidate_records(store):
        if (
            int(other["event_id"]) == int(event["event_id"])
            or _candidate_identity(other) == identity
        ):
            continue
        other_context = _operator_context(other["record"])
        other_source = other_context.get("source")
        if (
            source_sha
            and isinstance(other_source, dict)
            and other_source.get("sha256") == source_sha
        ):
            exact.append(
                {
                    "kind": "candidate-source-digest",
                    "candidate_id": _candidate_identity(other),
                    "event_id": other["event_id"],
                    "reason": "same source_sha256",
                }
            )
    deduped_exact = list({legacy.canonical_json(item): item for item in exact}.values())
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
            "task_id": task_id,
            "claims": claims,
            "risk": "medium" if claims else "unknown",
            "implementation_approval": (
                task_approval_contract(
                    {
                        "id": task_id,
                        "execution": {
                            "mode": "interactive-agent",
                            "policy": "review-before-effect",
                        },
                        "claims": claims,
                    }
                )
                if task_id
                else None
            ),
            "publication_approval": approval_decision("registry_mutation", None),
        },
        "exact_duplicates": deduped_exact,
        "similarity_suggestions": similar[:MAX_SIMILARITY_RESULTS],
        "missing_fields": missing,
        "advisory_only": True,
        "does_not_establish": [
            "automatic_merge",
            "automatic_close",
            "automatic_suppression",
            "task_readiness",
            "registry_mutation",
        ],
    }


def candidate_assess(
    registry: Registry,
    store: StateStore,
    *,
    candidate_id: str | None = None,
    event_id: int | None = None,
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


def _task_change_sha256(path: str, content: bytes) -> str:
    return legacy.sha256_json(
        {
            "path": path,
            "before": None,
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


def _validate_task_semantics(registry: Registry, task_json: dict[str, Any]) -> None:
    try:
        registry.schemas.validate("task", task_json, "operator-intake-task")
    except Exception as exc:
        raise OperatorIntakeError(
            "task-schema-invalid", f"task JSON does not satisfy the task schema: {exc}"
        ) from exc
    task_id = str(task_json.get("id", ""))
    initiative = str(task_json.get("initiative", ""))
    if task_id in registry.tasks:
        raise OperatorIntakeError("task-exists", f"task {task_id} already exists")
    if initiative not in registry.initiatives:
        raise OperatorIntakeError("initiative-unknown", f"unknown initiative {initiative}")
    for dependency in task_json.get("depends_on", []):
        if dependency not in registry.tasks:
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


def _inject_candidate_binding(task_json: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(legacy.canonical_json(task_json))
    metadata = result.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        raise OperatorIntakeError("metadata-invalid", "task metadata must be an object")
    context = _operator_context(event["record"])
    metadata["operator_intake"] = {
        "schema_version": OPERATOR_INTAKE_SCHEMA_VERSION,
        "candidate_id": _candidate_identity(event),
        "event_id": event["event_id"],
        "event_created_at": event["created_at"],
        "request_sha256": context.get("request_sha256"),
        "source": context.get("source"),
        "does_not_establish": [
            "queue_truth",
            "task_readiness",
            "claim_or_dispatch_authority",
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
    if publishing_task_id not in registry.tasks:
        raise OperatorIntakeError(
            "publishing-task-unknown",
            f"publishing task {publishing_task_id} is not in the Registry",
        )
    bound_task = _inject_candidate_binding(task_json, event)
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
    _validate_task_semantics(registry, bound_task)
    assessment = _candidate_assess(
        registry,
        store,
        event_id=int(event["event_id"]),
        initiative=str(bound_task["initiative"]),
        task_id=str(bound_task["id"]),
    )
    if assessment["exact_duplicates"]:
        raise OperatorIntakeError(
            "exact-duplicate",
            "candidate assessment found an exact duplicate",
            details={"findings": assessment["exact_duplicates"]},
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
        "publishing_task_sha256": registry.tasks[publishing_task_id].sha256,
        "task_id": task_id,
        "target_path": target_path,
        "task_json": bound_task,
        "task_json_sha256": legacy.sha256_json(bound_task),
        "task_file_sha256": hashlib.sha256(content).hexdigest(),
        "proposed_diff_sha256": _task_change_sha256(target_path, content),
        "assessment": assessment,
        "unresolved_fields": unresolved,
        "placeholder_justification": placeholder_justification,
        "publication": {
            "action_class": "registry_mutation",
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
            "registry_mutation",
            "queue_mutation",
            "task_readiness",
            "claim_or_dispatch_authority",
            "merge_or_deployment_authority",
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


def _validated_proposal(
    registry: Registry,
    store: StateStore,
    *,
    plan_path: Path,
) -> tuple[dict[str, Any], bytes, dict[str, Any]]:
    try:
        plan_bytes = plan_path.read_bytes()
    except OSError as exc:
        raise OperatorIntakeError(
            "proposal-read-failed",
            f"cannot read task proposal {plan_path}: {exc}",
            retryable=isinstance(exc, (BlockingIOError, InterruptedError)),
            details={"path": str(plan_path)},
        ) from exc
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
    registry, identity = _canonical_registry_snapshot(registry)
    publishing_task_id = str(plan.get("publishing_task_id", ""))
    publishing_task = registry.tasks.get(publishing_task_id)
    if publishing_task is None:
        raise OperatorIntakeError(
            "publishing-task-unknown",
            f"publishing task {publishing_task_id} is not in the Registry",
        )
    if plan.get("publishing_task_sha256") != publishing_task.sha256:
        raise OperatorIntakeError(
            "publishing-task-drift",
            "publishing task revision does not match the reviewed proposal",
        )
    if plan.get("registry") != identity:
        raise OperatorIntakeError(
            "registry-drift",
            "Registry commit or tree changed after proposal creation",
            retryable=True,
            details={"planned": plan.get("registry"), "current": identity},
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
    _validate_task_semantics(registry, task_json)
    content = _render_task(task_json)
    target_path = str(plan.get("target_path"))
    expected_path = f"registry/tasks/{task_json.get('id')}.json"
    if target_path != expected_path:
        raise OperatorIntakeError("target-path-invalid", f"target path must be {expected_path}")
    if os.path.lexists(registry.root / target_path):
        raise OperatorIntakeError(
            "target-exists", f"target task file already exists: {target_path}"
        )
    if plan.get("task_json_sha256") != legacy.sha256_json(task_json):
        raise OperatorIntakeError("task-json-drift", "task_json_sha256 does not match task_json")
    if plan.get("task_file_sha256") != hashlib.sha256(content).hexdigest():
        raise OperatorIntakeError(
            "task-file-drift", "task_file_sha256 does not match rendered task JSON"
        )
    if plan.get("proposed_diff_sha256") != _task_change_sha256(target_path, content):
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
) -> dict[str, Any]:
    path = Path(plan_path).expanduser().resolve()
    plan, plan_bytes, approval_result = _validated_proposal(registry, store, plan_path=path)
    task_id = str(plan["task_id"])
    required_keys = sorted(
        [
            f"path:{BUREAU_REPOSITORY_ROOT / plan['target_path']}",
            BUREAU_REGISTRY_PUBLICATION_GATE_KEY,
        ]
    )
    branch = _publication_branch(task_id, str(plan["proposal_sha256"]))
    return {
        "schema_version": OPERATOR_INTAKE_SCHEMA_VERSION,
        "kind": "bureau_task_publication_preview",
        "status": "ready",
        "effect_started": False,
        "retryable": False,
        "ambiguity": False,
        "required_readback": [],
        "plan_path": str(path),
        "plan_file_sha256": hashlib.sha256(plan_bytes).hexdigest(),
        "proposal_sha256": plan["proposal_sha256"],
        "publishing_task_sha256": plan["publishing_task_sha256"],
        "task_id": task_id,
        "target_path": plan["target_path"],
        "branch": branch,
        "required_resource_keys": required_keys,
        "approval": approval_result,
        "does_not_establish": [
            "lease_ownership",
            "branch_creation",
            "pull_request_creation",
            "queue_mutation",
            "merge_readiness",
        ],
    }


def _publication_branch(task_id: str, proposal_sha256: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", task_id.casefold()).strip("-")
    branch = f"operator/register-{slug}-{proposal_sha256[:10]}"
    if not _BRANCH_RE.fullmatch(branch):
        raise OperatorIntakeError("branch-invalid", "derived publication branch is invalid")
    return branch


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


def _attach_publication_phase(error: OperatorIntakeError, phase: str) -> OperatorIntakeError:
    """Keep the original typed failure while adding the last proven phase."""
    if (
        error.publication_phase not in PUBLICATION_PHASES
        or PUBLICATION_PHASES.index(error.publication_phase) < PUBLICATION_PHASES.index(phase)
    ):
        error.publication_phase = phase
    return error


def _release_leases_after_safe_failure(
    error: OperatorIntakeError,
    *,
    phase: str,
    binding: dict[str, Any],
) -> None:
    """Release exact leases only for an independently proven pre-remote failure."""
    recorded_phase = error.publication_phase or phase
    if recorded_phase in _REMOTE_EFFECT_PHASES or error.effect_started or error.ambiguity:
        return
    try:
        error.details["lease_release"] = _release_unchanged_publication_leases(binding)
    except Exception as release_exc:
        error.details["lease_release"] = {
            "released": False,
            "error": str(release_exc)[:2000],
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
    publisher: TaskPublisher | None = None,
) -> dict[str, Any]:
    """Publish one reviewed task proposal without queue, merge or deployment effects."""
    path = Path(plan_path).expanduser().resolve()
    receipt = Path(receipt_path).expanduser().resolve()
    try:
        plan_bytes = path.read_bytes()
    except OSError as exc:
        raise OperatorIntakeError(
            "proposal-read-failed",
            f"cannot read task proposal {path}: {exc}",
            retryable=isinstance(exc, (BlockingIOError, InterruptedError)),
            details={"path": str(path)},
        ) from exc
    plan_file_sha = hashlib.sha256(plan_bytes).hexdigest()
    try:
        plan_for_replay = json.loads(plan_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise OperatorIntakeError(
            "proposal-json-invalid", f"cannot parse task proposal: {exc}"
        ) from exc
    if not isinstance(plan_for_replay, dict):
        raise OperatorIntakeError(
            "proposal-object-required", "task proposal JSON must be an object"
        )
    if receipt.exists():
        try:
            existing = json.loads(receipt.read_bytes())
        except OSError as exc:
            raise OperatorIntakeError(
                "receipt-read-failed",
                f"cannot read publication receipt {receipt}: {exc}",
                retryable=isinstance(exc, (BlockingIOError, InterruptedError)),
                details={"path": str(receipt)},
            ) from exc
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
        receipt_valid = (
            existing.get("kind") == "bureau_task_publication_receipt"
            and existing.get("status") == "published"
            and existing.get("receipt_sha256") == legacy.sha256_json(unsigned_receipt)
            and existing.get("publication", {}).get("readback_complete") is True
        )
        if not receipt_valid:
            raise OperatorIntakeError(
                "receipt-integrity-invalid",
                "existing publication receipt is not a valid completed receipt",
            )
        if (
            existing.get("proposal_sha256") == plan_for_replay.get("proposal_sha256")
            and existing.get("plan_file_sha256") == plan_file_sha
        ):
            return {**existing, "idempotent_replay": True, "receipt_path": str(receipt)}
        raise OperatorIntakeError(
            "receipt-conflict", "existing publication receipt belongs to a different plan"
        )
    preview = publication_preview(registry, store, plan_path=path)
    plan = plan_for_replay
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
                "operation": "registry-publication",
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
    gate_snapshot = next(
        item
        for item in normalized_leases["lease_snapshots"]
        if item["resource_key"] == BUREAU_REGISTRY_PUBLICATION_GATE_KEY
    )
    if gate_snapshot["expires_at_unix"] - gate_snapshot["acquired_at_unix"] > 300:
        error = OperatorIntakeError(
            "publication-gate-ttl-invalid",
            "registry publication gate lease must be bounded to at most 300 seconds",
            publication_phase="before_workspace",
        )
        _release_leases_after_safe_failure(
            error, phase="before_workspace", binding=normalized_leases
        )
        raise error

    def assert_plan_unchanged() -> None:
        try:
            current_bytes = path.read_bytes()
        except OSError as exc:
            raise OperatorIntakeError(
                "plan-read-failed",
                f"cannot reread reviewed plan {path}: {exc}",
                retryable=isinstance(exc, (BlockingIOError, InterruptedError)),
                details={"path": str(path)},
            ) from exc
        observed = hashlib.sha256(current_bytes).hexdigest()
        if observed != plan_file_sha:
            raise OperatorIntakeError(
                "plan-file-drift",
                "reviewed plan bytes changed before publication effect",
                retryable=True,
            )

    phase = "before_workspace"

    def phase_changed(value: str) -> None:
        nonlocal phase
        if value not in PUBLICATION_PHASES:
            raise OperatorIntakeError(
                "publication-phase-invalid",
                f"publisher reported unsupported publication phase {value!r}",
                publication_phase=phase,
            )
        if PUBLICATION_PHASES.index(value) < PUBLICATION_PHASES.index(phase):
            raise OperatorIntakeError(
                "publication-phase-regression",
                "publisher publication phase regressed",
                publication_phase=phase,
                details={"reported": value},
            )
        phase = value

    selected_publisher = publisher or SubprocessTaskPublisher()
    try:
        assert_plan_unchanged()
        published = selected_publisher.publish(
            registry=registry,
            plan=plan,
            workspace_root=Path(workspace_root).expanduser().absolute(),
            assert_plan_unchanged=assert_plan_unchanged,
            phase_changed=phase_changed,
        )
        assert_plan_unchanged()
        if phase != "pr_confirmed":
            raise OperatorIntakeError(
                "publication-readback-incomplete",
                "publisher returned success without a confirmed pull-request phase",
                effect_started=phase in _REMOTE_EFFECT_PHASES,
                ambiguity=phase in {"push_attempted", "pr_attempted"},
                required_readback=["remote branch head", "open pull request for exact branch"],
                publication_phase=phase,
            )
    except OperatorIntakeError as exc:
        error = _attach_publication_phase(exc, phase)
        recorded_phase = error.publication_phase or phase
        if recorded_phase in _REMOTE_EFFECT_PHASES:
            error.effect_started = True
            if recorded_phase in {"push_attempted", "pr_attempted"}:
                error.ambiguity = True
                if not error.required_readback:
                    error.required_readback = (
                        "remote branch head",
                        "open pull request for exact branch",
                        "target task file at remote head",
                    )
        _release_leases_after_safe_failure(
            error, phase=recorded_phase, binding=normalized_leases
        )
        raise
    except Exception as exc:
        remote_effect_possible = phase in _REMOTE_EFFECT_PHASES
        error = OperatorIntakeError(
            "publication-unclear" if remote_effect_possible else "local-publication-failed",
            (
                f"publisher failed with unclear remote outcome: {exc}"
                if remote_effect_possible
                else f"publisher failed before any remote effect: {exc}"
            ),
            retryable=False,
            effect_started=remote_effect_possible,
            ambiguity=remote_effect_possible,
            required_readback=(
                [
                    "remote branch head",
                    "open pull request for exact branch",
                    "target task file at remote head",
                ]
                if remote_effect_possible
                else []
            ),
            publication_phase=phase,
        )
        _release_leases_after_safe_failure(error, phase=phase, binding=normalized_leases)
        raise error from exc
    try:
        lease_release = _release_unchanged_publication_leases(normalized_leases)
    except OperatorIntakeError as exc:
        raise OperatorIntakeError(
            "lease-release-failed",
            f"publication succeeded but exact lease release failed: {exc}",
            effect_started=True,
            required_readback=["publication lease rows"],
            details={"publication": published, "cause_code": exc.code},
            publication_phase=phase,
        ) from exc
    value: dict[str, Any] = {
        "schema_version": OPERATOR_INTAKE_SCHEMA_VERSION,
        "kind": "bureau_task_publication_receipt",
        "status": "published",
        "effect_started": True,
        "retryable": False,
        "ambiguity": False,
        "required_readback": [],
        "publication_phase": phase,
        "proposal_sha256": preview["proposal_sha256"],
        "plan_file_sha256": plan_file_sha,
        "task_id": preview["task_id"],
        "target_path": preview["target_path"],
        "branch": preview["branch"],
        "registry": plan["registry"],
        "publishing_task_id": plan["publishing_task_id"],
        "publishing_task_sha256": plan["publishing_task_sha256"],
        "lease_binding": normalized_leases,
        "lease_release": lease_release,
        "publication": published,
        "created_at": legacy.utc_now(),
        "queue_mutated": False,
        "does_not_establish": [
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
            f"publication succeeded but receipt write failed: {exc}",
            retryable=False,
            effect_started=True,
            ambiguity=True,
            required_readback=[f"publication receipt at {receipt}"],
            details={
                "proposal_sha256": preview["proposal_sha256"],
                "branch": preview["branch"],
                "publication": published,
                "publication_confirmed": True,
                "ambiguity_scope": "receipt",
            },
            publication_phase=phase,
        ) from exc
    return {**value, "idempotent_replay": False, "receipt_path": str(receipt)}


class SubprocessTaskPublisher:
    """Narrow Git/GitHub transport for one already reviewed task-file publication."""

    _MARKER_NAME = "bureau-operator-publication.json"
    _MARKER_TEMP_SUFFIX = ".tmp"
    _STAGING_SUFFIX = ".bureau-staging"
    _RESERVATION_SUFFIX = ".bureau-reservation.json"
    _TARGET_TEMP_SUFFIX = ".bureau-publication-tmp"

    @staticmethod
    def _command_environment() -> dict[str, str]:
        return {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }

    def _run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout: int = 60,
    ) -> str:
        env = self._command_environment()
        phase = getattr(self, "_publication_phase", "before_workspace")
        effect_command = list(arguments[:2]) == ["git", "push"] or list(
            arguments[:3]
        ) == ["gh", "pr", "create"]
        try:
            process = subprocess.run(
                list(arguments),
                cwd=cwd,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            remote_possible = effect_command or phase in _REMOTE_EFFECT_PHASES
            raise OperatorIntakeError(
                "publication-unclear" if remote_possible else "publication-command-failed",
                f"{' '.join(arguments)} could not complete: {exc}",
                effect_started=remote_possible,
                ambiguity=effect_command or phase in {"push_attempted", "pr_attempted"},
                required_readback=(
                    ["remote branch head", "open pull request"] if remote_possible else []
                ),
                publication_phase=phase,
            ) from exc
        if process.returncode != 0:
            detail = "\n".join(
                part for part in (process.stdout.strip(), process.stderr.strip()) if part
            )[:4000]
            remote_possible = effect_command or phase in _REMOTE_EFFECT_PHASES
            raise OperatorIntakeError(
                "publication-unclear" if effect_command else "publication-command-failed",
                f"{' '.join(arguments)} failed: {detail}",
                effect_started=remote_possible,
                ambiguity=effect_command or phase in {"push_attempted", "pr_attempted"},
                required_readback=(
                    ["remote branch head", "open pull request"]
                    if remote_possible
                    else []
                ),
                publication_phase=phase,
            )
        return process.stdout.strip()

    def _git_blob_sha256(self, workspace: Path, object_name: str) -> str:
        """Hash exact Git object bytes without consulting the worktree."""
        phase = getattr(self, "_publication_phase", "before_workspace")
        try:
            process = subprocess.run(
                ["git", "cat-file", "blob", object_name],
                cwd=workspace,
                capture_output=True,
                check=False,
                timeout=60,
                env=self._command_environment(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OperatorIntakeError(
                "publication-git-object-read-failed",
                f"cannot read exact Git blob {object_name!r}: {exc}",
                publication_phase=phase,
            ) from exc
        if process.returncode != 0:
            detail = process.stderr.decode("utf-8", errors="replace").strip()[:4000]
            raise OperatorIntakeError(
                "publication-git-object-read-failed",
                f"cannot read exact Git blob {object_name!r}: {detail}",
                publication_phase=phase,
            )
        return hashlib.sha256(process.stdout).hexdigest()

    def _set_phase(
        self,
        phase: str,
        phase_changed: Callable[[str], None],
        *,
        workspace: Path | None = None,
        marker: dict[str, Any] | None = None,
    ) -> None:
        self._publication_phase = phase
        if workspace is not None and marker is not None:
            marker["phase"] = phase
            self._write_workspace_marker(workspace, marker)
        phase_changed(phase)

    @staticmethod
    def _json_bytes(value: dict[str, Any]) -> bytes:
        return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )

    def _write_workspace_marker(self, workspace: Path, marker: dict[str, Any]) -> None:
        marker_path = workspace / ".git" / self._MARKER_NAME
        temporary = marker_path.with_name(marker_path.name + self._MARKER_TEMP_SUFFIX)
        marker_bytes = self._json_bytes(marker)
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except OSError as exc:
            raise OperatorIntakeError(
                "workspace-marker-write-failed",
                f"cannot create publication workspace marker temporary: {exc}",
                publication_phase=getattr(self, "_publication_phase", None),
            ) from exc
        try:
            self._after_marker_temp_created(temporary)
            offset = 0
            while offset < len(marker_bytes):
                written = os.write(descriptor, marker_bytes[offset:])
                if written <= 0:
                    raise OSError(errno.EIO, "publication marker temporary write stalled")
                offset += written
            self._after_marker_temp_written(temporary)
            os.fsync(descriptor)
            self._after_marker_temp_fsync(temporary)
        finally:
            os.close(descriptor)
        self._before_marker_replace(temporary, marker_path)
        try:
            os.replace(temporary, marker_path)
            _fsync_directory(marker_path.parent)
        except OSError as exc:
            raise OperatorIntakeError(
                "workspace-marker-write-failed",
                f"cannot atomically publish workspace marker: {exc}",
                publication_phase=getattr(self, "_publication_phase", None),
            ) from exc

    def _before_clone(self, staging: Path) -> None:
        """Fault-injection seam after reservation and before staging creation."""

    def _after_clone_destination_created(self, staging: Path) -> None:
        """Fault-injection seam after durable staging creation and before clone."""

    def _after_workspace_rename_fsync(self, workspace: Path) -> None:
        """Fault-injection seam after durable final rename and before reservation removal."""

    def _after_marker_temp_created(self, temporary: Path) -> None:
        """Fault-injection seam after exclusive marker temporary creation."""

    def _after_marker_temp_written(self, temporary: Path) -> None:
        """Fault-injection seam after complete marker write and before fsync."""

    def _after_marker_temp_fsync(self, temporary: Path) -> None:
        """Fault-injection seam after marker fsync and before replace."""

    def _before_marker_replace(self, temporary: Path, marker: Path) -> None:
        """Fault-injection seam immediately before atomic marker replacement."""

    def _before_git_add(self, target: Path) -> None:
        """Fault-injection seam after materialization and before index staging."""

    def _after_git_add(self, target: Path) -> None:
        """Fault-injection seam after index staging and before byte verification."""

    def _before_git_commit(self, target: Path) -> None:
        """Fault-injection seam after index verification and before tree capture."""

    def _before_publication_ref_update(self, workspace: Path, target: Path, commit: str) -> None:
        """Fault-injection seam after immutable commit verification."""

    def _before_markerless_staging_remove(self, staging: Path) -> None:
        """Fault-injection seam after inode observation and before removal."""

    def _before_workspace_rename(self, staging: Path, workspace: Path) -> None:
        """Fault-injection seam after durable validation and before publication."""

    def _after_target_temp_created(self, temporary: Path) -> None:
        """Fault-injection seam after exclusive creation and before the first write."""

    def _after_target_temp_fsync(self, temporary: Path) -> None:
        """Fault-injection seam after durable reviewed bytes and before rename."""

    def _after_target_rename(self, target: Path) -> None:
        """Fault-injection seam after rename and before the parent fsync."""

    @staticmethod
    def _validate_local_tree(registry: Registry, plan: dict[str, Any]) -> None:
        """Validate the exact post-publication Registry before creating its workspace."""
        try:
            with tempfile.TemporaryDirectory(prefix="bureau-publication-validate-") as raw:
                sandbox = Path(raw)
                shutil.copytree(registry.root / "registry", sandbox / "registry")
                shutil.copytree(registry.root / "schemas", sandbox / "schemas")
                target = sandbox / str(plan["target_path"])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(_render_task(plan["task_json"]))
                Registry.load(sandbox)
        except OperatorIntakeError:
            raise
        except Exception as exc:
            raise OperatorIntakeError(
                "local-registry-validation-failed",
                f"proposed Registry tree is invalid before workspace creation: {exc}",
                publication_phase="before_workspace",
            ) from exc

    @classmethod
    def _marker_identity(cls, plan: dict[str, Any], branch: str) -> dict[str, Any]:
        target = Path(str(plan["target_path"]))
        target_temporary = target.parent / (
            f".{plan['proposal_sha256']}{cls._TARGET_TEMP_SUFFIX}"
        )
        return {
            "schema_version": 1,
            "kind": "bureau_operator_publication_workspace",
            "proposal_sha256": plan["proposal_sha256"],
            "base_commit": plan["registry"]["commit"],
            "branch": branch,
            "target_path": plan["target_path"],
            "target_file_sha256": plan["task_file_sha256"],
            "target_temporary": target_temporary.as_posix(),
            "publishing_task_id": plan["publishing_task_id"],
            "publishing_task_sha256": plan["publishing_task_sha256"],
        }

    @classmethod
    def _reservation_path(cls, workspace: Path) -> Path:
        return workspace.with_name(f".{workspace.name}{cls._RESERVATION_SUFFIX}")

    @classmethod
    def _reservation_identity(
        cls,
        plan: dict[str, Any],
        branch: str,
        remote: str,
        staging: Path,
        workspace: Path,
    ) -> dict[str, Any]:
        identity = {
            "schema_version": 1,
            "kind": "bureau_operator_publication_staging_reservation",
            "proposal_sha256": plan["proposal_sha256"],
            "base_commit": plan["registry"]["commit"],
            "branch": branch,
            "remote": remote,
            "staging_path": str(staging),
            "final_path": str(workspace),
        }
        return {**identity, "reservation_sha256": legacy.sha256_json(identity)}

    @staticmethod
    def _read_regular_file_no_follow(
        path: Path,
        *,
        code: str,
        phase: str,
        missing_ok: bool = False,
    ) -> bytes | None:
        try:
            path_stat = path.lstat()
        except FileNotFoundError:
            if missing_ok:
                return None
            raise OperatorIntakeError(
                code,
                f"required publication artifact is missing: {path}",
                publication_phase=phase,
            ) from None
        except OSError as exc:
            raise OperatorIntakeError(
                code,
                f"cannot inspect publication artifact {path}: {exc}",
                publication_phase=phase,
            ) from exc
        if not stat.S_ISREG(path_stat.st_mode):
            raise OperatorIntakeError(
                code,
                f"publication artifact is not a no-follow regular file: {path}",
                details={"mode": stat.filemode(path_stat.st_mode)},
                publication_phase=phase,
            )
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            raise OperatorIntakeError(
                code,
                f"cannot safely open publication artifact {path}: {exc}",
                publication_phase=phase,
            ) from exc
        try:
            opened_stat = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened_stat.st_mode)
                or opened_stat.st_dev != path_stat.st_dev
                or opened_stat.st_ino != path_stat.st_ino
            ):
                raise OperatorIntakeError(
                    code,
                    f"publication artifact changed during inspection: {path}",
                    publication_phase=phase,
                )
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                return handle.read()
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _load_exact_reservation(
        self, reservation: Path, *, expected: dict[str, Any]
    ) -> dict[str, Any]:
        raw = self._read_regular_file_no_follow(
            reservation,
            code="workspace-reservation-invalid",
            phase="before_workspace",
        )
        assert raw is not None
        try:
            observed = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise OperatorIntakeError(
                "workspace-reservation-invalid",
                "publication staging reservation is not valid JSON",
                details={"reservation": str(reservation)},
                publication_phase="before_workspace",
            ) from exc
        unsigned = (
            {key: value for key, value in observed.items() if key != "reservation_sha256"}
            if isinstance(observed, dict)
            else {}
        )
        if (
            observed != expected
            or raw != self._json_bytes(expected)
            or observed.get("reservation_sha256") != legacy.sha256_json(unsigned)
        ):
            raise OperatorIntakeError(
                "workspace-reservation-mismatch",
                "publication staging reservation is foreign, malformed or identity-invalid",
                details={"reservation": str(reservation)},
                publication_phase="before_workspace",
            )
        return observed

    def _create_or_load_reservation(
        self, reservation: Path, *, expected: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        if os.path.lexists(reservation):
            return self._load_exact_reservation(reservation, expected=expected), True
        parent = reservation.parent
        try:
            parent_descriptor = os.open(
                parent,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
        except OSError as exc:
            raise OperatorIntakeError(
                "workspace-reservation-write-failed",
                f"cannot open reservation parent without following links: {exc}",
                publication_phase="before_workspace",
            ) from exc
        temporary_path: Path | None = None
        descriptor = -1
        try:
            descriptor, raw_temporary = tempfile.mkstemp(
                prefix=reservation.name + ".tmp-", dir=parent
            )
            temporary_path = Path(raw_temporary)
            os.fchmod(descriptor, 0o600)
            payload = self._json_bytes(expected)
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise OSError(errno.EIO, "publication reservation write stalled")
                offset += written
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            try:
                _rename_noreplace(
                    temporary_path.name,
                    reservation.name,
                    source_dir_fd=parent_descriptor,
                    target_dir_fd=parent_descriptor,
                )
            except FileExistsError:
                return self._load_exact_reservation(reservation, expected=expected), True
            os.fsync(parent_descriptor)
            temporary_path = None
            return expected, False
        except OperatorIntakeError:
            raise
        except OSError as exc:
            raise OperatorIntakeError(
                "workspace-reservation-write-failed",
                f"cannot atomically persist publication staging reservation: {exc}",
                publication_phase="before_workspace",
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_path is not None:
                with contextlib.suppress(OSError):
                    temporary_path.unlink()
            os.close(parent_descriptor)

    def _remove_exact_reservation(
        self, reservation: Path, *, expected: dict[str, Any]
    ) -> None:
        self._load_exact_reservation(reservation, expected=expected)
        try:
            parent_descriptor = os.open(
                reservation.parent,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                os.unlink(reservation.name, dir_fd=parent_descriptor)
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)
        except OSError as exc:
            raise OperatorIntakeError(
                "workspace-reservation-remove-failed",
                f"cannot durably remove exact publication reservation: {exc}",
                publication_phase="local_workspace",
            ) from exc

    @classmethod
    def _valid_marker_temporary(
        cls,
        raw: bytes,
        *,
        identity: dict[str, Any],
        phases: Sequence[str],
    ) -> bool:
        return any(raw == candidate[: len(raw)] for candidate in (
            cls._json_bytes({**identity, "phase": phase}) for phase in phases
        ))

    def _remove_marker_temporary(self, temporary: Path, *, git_directory: Path) -> None:
        try:
            descriptor = os.open(
                git_directory,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                os.unlink(temporary.name, dir_fd=descriptor)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise OperatorIntakeError(
                "workspace-marker-temp-reconcile-failed",
                f"cannot reconcile exact publication marker temporary: {exc}",
                publication_phase=getattr(self, "_publication_phase", "before_workspace"),
            ) from exc

    def _load_exact_workspace_marker(
        self,
        workspace: Path,
        *,
        plan: dict[str, Any],
        branch: str,
        phase_changed: Callable[[str], None],
        initial_reservation: bool = False,
        missing_ok: bool = False,
    ) -> dict[str, Any] | None:
        """Restore persisted phase evidence before any transport-dependent check."""
        git_directory = workspace / ".git"
        marker_path = git_directory / self._MARKER_NAME
        temporary = marker_path.with_name(marker_path.name + self._MARKER_TEMP_SUFFIX)
        try:
            if not stat.S_ISDIR(git_directory.lstat().st_mode):
                raise OSError(errno.EINVAL, "workspace Git metadata is not a real directory")
        except FileNotFoundError:
            if missing_ok and not os.path.lexists(temporary):
                return None
            raise OperatorIntakeError(
                "workspace-identity-ambiguous",
                "existing publication workspace has no real Git metadata directory",
                details={"workspace": str(workspace)},
                publication_phase="before_workspace",
            ) from None
        except OSError as exc:
            raise OperatorIntakeError(
                "workspace-identity-ambiguous",
                "existing publication workspace has no readable exact identity marker",
                details={"workspace": str(workspace)},
                publication_phase="before_workspace",
            ) from exc
        marker_raw = self._read_regular_file_no_follow(
            marker_path,
            code="workspace-identity-ambiguous",
            phase="before_workspace",
            missing_ok=True,
        )
        temporary_raw = self._read_regular_file_no_follow(
            temporary,
            code="workspace-marker-temp-invalid",
            phase="before_workspace",
            missing_ok=True,
        )
        expected = self._marker_identity(plan, branch)
        if marker_raw is None:
            if temporary_raw is None:
                if missing_ok:
                    return None
                raise OperatorIntakeError(
                    "workspace-identity-ambiguous",
                    "existing publication workspace has no exact identity marker",
                    details={"workspace": str(workspace)},
                    publication_phase="before_workspace",
                )
            if not initial_reservation or not self._valid_marker_temporary(
                temporary_raw, identity=expected, phases=("local_workspace",)
            ):
                raise OperatorIntakeError(
                    "workspace-marker-temp-mismatch",
                    "markerless workspace has a foreign or unauthorized marker temporary",
                    details={"temporary": str(temporary)},
                    publication_phase="before_workspace",
                )
            self._remove_marker_temporary(temporary, git_directory=git_directory)
            marker = {**expected, "phase": "local_workspace"}
            self._publication_phase = "local_workspace"
            self._write_workspace_marker(workspace, marker)
            phase_changed("local_workspace")
            return marker
        try:
            marker = json.loads(marker_raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise OperatorIntakeError(
                "workspace-identity-ambiguous",
                "existing publication workspace marker is not valid JSON",
                details={"workspace": str(workspace)},
                publication_phase="before_workspace",
            ) from exc
        if not isinstance(marker, dict):
            raise OperatorIntakeError(
                "workspace-identity-ambiguous",
                "existing publication workspace identity marker is not an object",
                details={"workspace": str(workspace)},
                publication_phase="before_workspace",
            )
        mismatched = {
            key: {"expected": value, "observed": marker.get(key)}
            for key, value in expected.items()
            if marker.get(key) != value
        }
        marker_phase = marker.get("phase")
        if marker_phase not in PUBLICATION_PHASES or marker_phase == "before_workspace":
            mismatched["phase"] = {
                "expected": list(PUBLICATION_PHASES[1:]),
                "observed": marker_phase,
            }
        if mismatched:
            raise OperatorIntakeError(
                "workspace-identity-mismatch",
                "existing publication workspace belongs to different or ambiguous work",
                details={"workspace": str(workspace), "mismatched": mismatched},
                publication_phase="before_workspace",
            )
        exact_marker = {**expected, "phase": marker_phase}
        if marker_raw != self._json_bytes(exact_marker):
            raise OperatorIntakeError(
                "workspace-identity-mismatch",
                "existing publication workspace marker bytes are not canonical and exact",
                details={"workspace": str(workspace)},
                publication_phase="before_workspace",
            )
        if temporary_raw is not None:
            phase_index = PUBLICATION_PHASES.index(str(marker_phase))
            if not self._valid_marker_temporary(
                temporary_raw,
                identity=expected,
                phases=PUBLICATION_PHASES[phase_index:],
            ):
                raise OperatorIntakeError(
                    "workspace-marker-temp-mismatch",
                    "publication workspace marker temporary contains foreign state",
                    details={"temporary": str(temporary)},
                    publication_phase=str(marker_phase),
                )
            self._remove_marker_temporary(temporary, git_directory=git_directory)
        self._set_phase(str(marker_phase), phase_changed)
        return marker

    def _reconcile_staged_workspace(
        self,
        staging: Path,
        *,
        marker: dict[str, Any],
        plan: dict[str, Any],
        branch: str,
        remote: str,
    ) -> None:
        """Accept only an exact, clean, fully initialized pre-publication staging tree."""
        if marker.get("phase") != "local_workspace":
            raise OperatorIntakeError(
                "workspace-staging-phase-invalid",
                "staged publication workspace is not in the exact local workspace phase",
                details={"staging": str(staging), "phase": marker.get("phase")},
                publication_phase=str(marker.get("phase", "before_workspace")),
            )
        try:
            references = _process_references(staging)
        except StateError as exc:
            raise OperatorIntakeError(
                "workspace-process-check-failed",
                str(exc),
                publication_phase="local_workspace",
            ) from exc
        if references:
            raise OperatorIntakeError(
                "workspace-active",
                "staged publication workspace is referenced by active processes",
                details={"staging": str(staging), "processes": references},
                publication_phase="local_workspace",
            )
        observed_origin = self._run(["git", "remote", "get-url", "origin"], cwd=staging)
        observed_branch = self._run(["git", "branch", "--show-current"], cwd=staging)
        head = self._run(["git", "rev-parse", "HEAD"], cwd=staging)
        status = self._run(
            ["git", "status", "--porcelain", "--untracked-files=all"], cwd=staging
        ).splitlines()
        if (
            observed_origin != remote
            or observed_branch != branch
            or head != str(plan["registry"]["commit"])
            or status
        ):
            raise OperatorIntakeError(
                "workspace-staging-state-mismatch",
                "staged publication workspace is dirty, foreign or incomplete",
                details={
                    "expected_origin": remote,
                    "observed_origin": observed_origin,
                    "expected_branch": branch,
                    "observed_branch": observed_branch,
                    "expected_head": plan["registry"]["commit"],
                    "observed_head": head,
                    "status": status,
                },
                publication_phase="local_workspace",
            )
        try:
            Registry.load(staging)
        except Exception as exc:
            raise OperatorIntakeError(
                "workspace-staging-validation-failed",
                f"staged publication workspace Registry is invalid: {exc}",
                publication_phase="local_workspace",
            ) from exc

    def _remove_reserved_markerless_staging(
        self,
        staging: Path,
        *,
        reservation: Path,
        expected_reservation: dict[str, Any],
    ) -> None:
        """Remove only an inactive markerless clone path with an exact reservation."""
        self._load_exact_reservation(reservation, expected=expected_reservation)
        if not self._checked_workspace_directory(
            staging, code="workspace-staging-path-invalid"
        ):
            return
        if not self._checked_workspace_directory(
            staging.parent, code="workspace-root-invalid"
        ):
            raise OperatorIntakeError(
                "workspace-root-invalid",
                "publication staging parent is missing",
                publication_phase="before_workspace",
            )
        try:
            references = _process_references(staging)
        except StateError as exc:
            raise OperatorIntakeError(
                "workspace-process-check-failed",
                str(exc),
                publication_phase="before_workspace",
            ) from exc
        if references:
            raise OperatorIntakeError(
                "workspace-active",
                "markerless reserved staging path is referenced by active processes",
                details={"staging": str(staging), "processes": references},
                publication_phase="before_workspace",
            )
        try:
            expected = staging.lstat()
            if not stat.S_ISDIR(expected.st_mode):
                raise OSError(
                    errno.ENOTDIR,
                    "reserved staging path is not a real directory",
                    str(staging),
                )
            self._before_markerless_staging_remove(staging)
            parent_descriptor = os.open(
                staging.parent,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                _remove_directory_tree_at(parent_descriptor, staging.name, expected=expected)
            finally:
                os.close(parent_descriptor)
        except OSError as exc:
            raise OperatorIntakeError(
                "workspace-staging-identity-changed"
                if exc.errno == errno.ESTALE
                else "workspace-staging-recreate-failed",
                f"cannot remove exact inactive markerless staging path: {exc}",
                details={"staging": str(staging)},
                publication_phase="before_workspace",
            ) from exc

    @staticmethod
    def _checked_workspace_directory(path: Path, *, code: str) -> bool:
        if not os.path.lexists(path):
            return False
        try:
            path_stat = path.lstat()
        except OSError as exc:
            raise OperatorIntakeError(
                code, f"cannot inspect publication path {path}: {exc}"
            ) from exc
        if not stat.S_ISDIR(path_stat.st_mode):
            raise OperatorIntakeError(
                code,
                f"publication path is not a real directory: {path}",
                publication_phase="before_workspace",
            )
        return True

    def _target_temporary_path(
        self,
        target: Path,
        *,
        proposal_sha256: str,
    ) -> Path:
        return target.parent / f".{proposal_sha256}{self._TARGET_TEMP_SUFFIX}"

    def _materialize_target_file(
        self,
        workspace: Path,
        target: Path,
        *,
        reviewed_bytes: bytes,
        expected_sha256: str,
        proposal_sha256: str,
    ) -> None:
        """Create one reviewed target through a durable same-directory temporary."""
        try:
            relative_target = target.relative_to(workspace)
            parent_descriptor = _open_directory_beneath(
                workspace, relative_target.parent
            )
        except OSError as exc:
            raise OperatorIntakeError(
                "workspace-target-parent-invalid",
                f"cannot open publication target parent without symlinks: {exc}",
                publication_phase="local_workspace",
            ) from exc
        except ValueError as exc:
            raise OperatorIntakeError(
                "workspace-target-parent-invalid",
                "publication target must remain beneath its exact workspace",
                details={"target": str(target), "workspace": str(workspace)},
                publication_phase="local_workspace",
            ) from exc
        temporary = self._target_temporary_path(
            target,
            proposal_sha256=proposal_sha256,
        )
        try:
            target_hash = self._workspace_file_sha256(
                workspace, relative_target, phase="local_workspace"
            )
            if target_hash is not None:
                try:
                    os.stat(
                        temporary.name,
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    raise OperatorIntakeError(
                        "workspace-target-temp-ambiguous",
                        "target and publication temporary both exist; neither was changed",
                        details={"target": str(target), "temporary": str(temporary)},
                        publication_phase="local_workspace",
                    )
                if target_hash != expected_sha256:
                    raise OperatorIntakeError(
                        "workspace-target-hash-mismatch",
                        "existing publication target does not match the reviewed proposal",
                        details={"expected": expected_sha256, "observed": target_hash},
                        publication_phase="local_workspace",
                    )
                return
            try:
                temporary_stat = os.stat(
                    temporary.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                temporary_stat = None
            if temporary_stat is not None:
                if not stat.S_ISREG(temporary_stat.st_mode):
                    raise OperatorIntakeError(
                        "workspace-target-temp-type-invalid",
                        "publication target temporary is not an exact owned regular file",
                        details={"temporary": str(temporary)},
                        publication_phase="local_workspace",
                    )
                try:
                    os.unlink(temporary.name, dir_fd=parent_descriptor)
                    os.fsync(parent_descriptor)
                except OSError as exc:
                    raise OperatorIntakeError(
                        "workspace-target-temp-reconcile-failed",
                        f"cannot reconcile exact owned publication temporary: {exc}",
                        publication_phase="local_workspace",
                    ) from exc
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                descriptor = os.open(
                    temporary.name, flags, 0o600, dir_fd=parent_descriptor
                )
            except OSError as exc:
                raise OperatorIntakeError(
                    "workspace-target-temp-create-failed",
                    f"cannot exclusively create publication target temporary: {exc}",
                    publication_phase="local_workspace",
                ) from exc
            try:
                self._after_target_temp_created(temporary)
                offset = 0
                while offset < len(reviewed_bytes):
                    written = os.write(descriptor, reviewed_bytes[offset:])
                    if written <= 0:
                        raise OSError(
                            errno.EIO, "publication target temporary write stalled"
                        )
                    offset += written
                os.fsync(descriptor)
                self._after_target_temp_fsync(temporary)
            finally:
                os.close(descriptor)
            observed_temporary_hash = self._workspace_file_sha256(
                workspace,
                temporary.relative_to(workspace),
                phase="local_workspace",
            )
            if observed_temporary_hash != expected_sha256:
                raise OperatorIntakeError(
                    "workspace-target-temp-hash-mismatch",
                    "publication target temporary does not contain the exact reviewed bytes",
                    details={
                        "expected": expected_sha256,
                        "observed": observed_temporary_hash,
                    },
                    publication_phase="local_workspace",
                )
            try:
                _rename_noreplace(
                    temporary.name,
                    target.name,
                    source_dir_fd=parent_descriptor,
                    target_dir_fd=parent_descriptor,
                )
                self._after_target_rename(target)
                os.fsync(parent_descriptor)
            except OSError as exc:
                raise OperatorIntakeError(
                    "workspace-target-rename-failed",
                    f"cannot atomically publish the reviewed target file: {exc}",
                    publication_phase="local_workspace",
                ) from exc
        finally:
            os.close(parent_descriptor)

    @staticmethod
    def _workspace_file_sha256(
        workspace: Path, relative: Path, *, phase: str
    ) -> str | None:
        """Hash a workspace file without following any symbolic-link component."""
        if relative.is_absolute() or relative.name in {"", ".", ".."}:
            raise OperatorIntakeError(
                "workspace-target-path-invalid",
                "publication workspace file path is not a safe relative path",
                details={"path": str(relative)},
                publication_phase=phase,
            )
        try:
            parent_descriptor = _open_directory_beneath(workspace, relative.parent)
        except OSError as exc:
            raise OperatorIntakeError(
                "workspace-target-parent-invalid",
                f"cannot open publication workspace parent without symlinks: {exc}",
                details={"path": str(relative.parent)},
                publication_phase=phase,
            ) from exc
        try:
            target_stat = os.stat(
                relative.name, dir_fd=parent_descriptor, follow_symlinks=False
            )
        except FileNotFoundError:
            os.close(parent_descriptor)
            return None
        except OSError as exc:
            os.close(parent_descriptor)
            raise OperatorIntakeError(
                "workspace-target-inspection-failed",
                f"cannot inspect publication workspace target {relative}: {exc}",
                publication_phase=phase,
            ) from exc
        if not stat.S_ISREG(target_stat.st_mode):
            os.close(parent_descriptor)
            raise OperatorIntakeError(
                "workspace-target-type-invalid",
                "publication workspace target must be a regular file and never a symlink",
                details={"target": str(relative), "mode": stat.filemode(target_stat.st_mode)},
                publication_phase=phase,
            )
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(relative.name, flags, dir_fd=parent_descriptor)
        except OSError as exc:
            os.close(parent_descriptor)
            raise OperatorIntakeError(
                "workspace-target-inspection-failed",
                f"cannot safely open publication workspace target {relative}: {exc}",
                publication_phase=phase,
            ) from exc
        try:
            opened_stat = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened_stat.st_mode)
                or opened_stat.st_dev != target_stat.st_dev
                or opened_stat.st_ino != target_stat.st_ino
            ):
                raise OperatorIntakeError(
                    "workspace-target-type-invalid",
                    "publication workspace target changed during no-follow inspection",
                    details={"target": str(relative)},
                    publication_phase=phase,
                )
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                return hashlib.sha256(handle.read()).hexdigest()
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(parent_descriptor)

    def _reconcile_exact_workspace(
        self,
        workspace: Path,
        *,
        marker: dict[str, Any],
        plan: dict[str, Any],
        branch: str,
        remote: str,
        phase_changed: Callable[[str], None],
    ) -> tuple[dict[str, Any], str | None, str]:
        marker_phase = str(marker["phase"])
        try:
            references = _process_references(workspace)
        except StateError as exc:
            raise OperatorIntakeError(
                "workspace-process-check-failed",
                str(exc),
                publication_phase=str(marker_phase),
            ) from exc
        if references:
            raise OperatorIntakeError(
                "workspace-active",
                "existing publication workspace is referenced by active processes",
                details={"workspace": str(workspace), "processes": references},
                publication_phase=str(marker_phase),
            )
        observed_origin = self._run(["git", "remote", "get-url", "origin"], cwd=workspace)
        observed_branch = self._run(["git", "branch", "--show-current"], cwd=workspace)
        head = self._run(["git", "rev-parse", "HEAD"], cwd=workspace)
        status = self._run(
            ["git", "status", "--porcelain", "--untracked-files=all"], cwd=workspace
        ).splitlines()
        if observed_origin != remote or observed_branch != branch:
            raise OperatorIntakeError(
                "workspace-git-binding-mismatch",
                "existing publication workspace has a foreign origin or branch",
                details={
                    "expected_origin": remote,
                    "observed_origin": observed_origin,
                    "expected_branch": branch,
                    "observed_branch": observed_branch,
                },
                publication_phase=str(marker_phase),
            )
        target_path = str(plan["target_path"])
        target = workspace / target_path
        target_hash = self._workspace_file_sha256(
            workspace, Path(target_path), phase=marker_phase
        )
        expected_hash = str(plan["task_file_sha256"])
        temporary = self._target_temporary_path(
            target,
            proposal_sha256=str(plan["proposal_sha256"]),
        )
        temporary_hash = self._workspace_file_sha256(
            workspace, temporary.relative_to(workspace), phase=marker_phase
        )
        temporary_path = temporary.relative_to(workspace).as_posix()
        base_commit = str(plan["registry"]["commit"])
        if head == base_commit:
            allowed = [
                [],
                [f"?? {target_path}"],
                [f"A  {target_path}"],
                [f"?? {temporary_path}"],
            ]
            target_and_temporary = target_hash is not None and temporary_hash is not None
            invalid_temporary_state = temporary_hash is not None and target_hash is not None
            if (
                status not in allowed
                or target_hash not in {None, expected_hash}
                or target_and_temporary
                or invalid_temporary_state
            ):
                raise OperatorIntakeError(
                    "workspace-local-state-mismatch",
                    "existing pre-commit workspace is dirty, foreign or ambiguous",
                    details={
                        "status": status,
                        "target_file_sha256": target_hash,
                        "target_temporary_sha256": temporary_hash,
                    },
                    publication_phase=str(marker_phase),
                )
            if PUBLICATION_PHASES.index(str(marker_phase)) >= PUBLICATION_PHASES.index(
                "committed_locally"
            ):
                raise OperatorIntakeError(
                    "workspace-phase-mismatch",
                    "workspace marker claims a commit or remote effect that Git does not contain",
                    publication_phase=str(marker_phase),
                )
            return marker, None, ""
        count = self._run(["git", "rev-list", "--count", f"{base_commit}..HEAD"], cwd=workspace)
        parent = self._run(["git", "rev-parse", "HEAD^"], cwd=workspace)
        changed = self._run(
            ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
            cwd=workspace,
        ).splitlines()
        if count != "1" or parent != base_commit or status or changed != [target_path]:
            raise OperatorIntakeError(
                "workspace-commit-mismatch",
                "existing workspace commit is not the exact single target-file commit",
                details={
                    "commit_count": count,
                    "parent": parent,
                    "status": status,
                    "changed": changed,
                },
                publication_phase=str(marker_phase),
            )
        if target_hash != expected_hash:
            raise OperatorIntakeError(
                "workspace-target-hash-mismatch",
                "existing workspace target file does not match the reviewed proposal",
                details={"expected": expected_hash, "observed": target_hash},
                publication_phase=str(marker_phase),
            )
        committed_target_hash = self._git_blob_sha256(workspace, f"HEAD:{target_path}")
        if committed_target_hash != expected_hash:
            raise OperatorIntakeError(
                "workspace-commit-target-hash-mismatch",
                "existing workspace commit does not contain the exact reviewed target bytes",
                details={
                    "expected": expected_hash,
                    "observed": committed_target_hash,
                    "head": head,
                },
                publication_phase=str(marker_phase),
            )
        diff = self._run(["git", "show", "--format=", "--binary", "HEAD"], cwd=workspace)
        if PUBLICATION_PHASES.index(str(marker_phase)) < PUBLICATION_PHASES.index(
            "committed_locally"
        ):
            self._set_phase(
                "committed_locally", phase_changed, workspace=workspace, marker=marker
            )
        return marker, head, diff

    def _pull_request_readback(
        self,
        *,
        repository: str,
        branch: str,
        head: str,
        cwd: Path,
    ) -> dict[str, Any] | None:
        raw = self._run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repository,
                "--head",
                branch,
                "--state",
                "all",
                "--json",
                "number,url,state,headRefOid,headRefName,baseRefName",
            ],
            cwd=cwd,
        )
        try:
            values = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise OperatorIntakeError(
                "publication-readback-invalid",
                "GitHub pull-request readback was not valid JSON",
                effect_started=True,
                ambiguity=True,
                required_readback=["pull request metadata", "remote branch head"],
                publication_phase=getattr(self, "_publication_phase", None),
            ) from exc
        if not isinstance(values, list) or len(values) > 1:
            raise OperatorIntakeError(
                "publication-readback-mismatch",
                "GitHub pull-request readback is ambiguous for the exact branch",
                effect_started=True,
                ambiguity=True,
                required_readback=["pull request metadata", "remote branch head"],
                details={"readback": values},
                publication_phase=getattr(self, "_publication_phase", None),
            )
        if not values:
            return None
        readback = values[0]
        if (
            not isinstance(readback, dict)
            or readback.get("headRefOid") != head
            or readback.get("headRefName") != branch
            or readback.get("baseRefName") != "main"
            or readback.get("state") != "OPEN"
        ):
            raise OperatorIntakeError(
                "publication-readback-mismatch",
                "GitHub pull-request readback does not match the exact publication",
                effect_started=True,
                ambiguity=True,
                required_readback=["pull request metadata", "remote branch head"],
                details={"readback": readback, "expected_head": head},
                publication_phase=getattr(self, "_publication_phase", None),
            )
        return readback

    @staticmethod
    def _github_slug(remote: str) -> str:
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

    def publish(
        self,
        *,
        registry: Registry,
        plan: dict[str, Any],
        workspace_root: Path,
        assert_plan_unchanged: Callable[[], None],
        phase_changed: Callable[[str], None],
    ) -> dict[str, Any]:
        self._publication_phase = "before_workspace"
        base_commit = str(plan["registry"]["commit"])
        task_id = str(plan["task_id"])
        target_path = str(plan["target_path"])
        branch = _publication_branch(task_id, str(plan["proposal_sha256"]))
        workspace = workspace_root / str(plan["proposal_sha256"])[:20]
        staging = workspace.with_name(f".{workspace.name}{self._STAGING_SUFFIX}")
        reservation = self._reservation_path(workspace)
        root_exists = self._checked_workspace_directory(
            workspace_root, code="workspace-root-invalid"
        )
        if not root_exists and not self._checked_workspace_directory(
            workspace_root.parent, code="workspace-root-parent-invalid"
        ):
            raise OperatorIntakeError(
                "workspace-root-parent-invalid",
                f"publication workspace parent does not exist: {workspace_root.parent}",
                publication_phase="before_workspace",
            )
        final_exists = self._checked_workspace_directory(
            workspace, code="workspace-path-invalid"
        )
        staging_exists = self._checked_workspace_directory(
            staging, code="workspace-staging-path-invalid"
        )
        if final_exists and staging_exists:
            raise OperatorIntakeError(
                "workspace-publication-ambiguous",
                "both final and staged publication workspaces exist; neither was changed",
                details={"workspace": str(workspace), "staging": str(staging)},
                publication_phase="before_workspace",
            )
        reservation_exists = os.path.lexists(reservation)
        workspace_reconciled = final_exists or staging_exists or reservation_exists
        marker: dict[str, Any] | None = None
        head: str | None = None
        diff = ""
        if final_exists:
            marker = self._load_exact_workspace_marker(
                workspace,
                plan=plan,
                branch=branch,
                phase_changed=phase_changed,
            )
        self._validate_local_tree(registry, plan)
        assert_plan_unchanged()
        remote = self._run(["git", "-C", str(registry.root), "remote", "get-url", "origin"])
        repository = self._github_slug(remote)
        expected_reservation = self._reservation_identity(
            plan, branch, remote, staging, workspace
        )
        if final_exists:
            assert marker is not None
            if reservation_exists:
                self._load_exact_reservation(
                    reservation, expected=expected_reservation
                )
            marker, head, diff = self._reconcile_exact_workspace(
                workspace,
                marker=marker,
                plan=plan,
                branch=branch,
                remote=remote,
                phase_changed=phase_changed,
            )
            if reservation_exists:
                self._remove_exact_reservation(
                    reservation, expected=expected_reservation
                )
                reservation_exists = False
        elif staging_exists:
            self._load_exact_reservation(reservation, expected=expected_reservation)
            marker = self._load_exact_workspace_marker(
                staging,
                plan=plan,
                branch=branch,
                phase_changed=phase_changed,
                initial_reservation=True,
                missing_ok=True,
            )
            if marker is None:
                self._remove_reserved_markerless_staging(
                    staging,
                    reservation=reservation,
                    expected_reservation=expected_reservation,
                )
                staging_exists = False
            else:
                self._reconcile_staged_workspace(
                    staging,
                    marker=marker,
                    plan=plan,
                    branch=branch,
                    remote=remote,
                )
        remote_main_output = self._run(["git", "ls-remote", remote, "refs/heads/main"]).split()
        if not remote_main_output:
            raise OperatorIntakeError(
                "remote-main-missing", "remote main ref is missing", retryable=True
            )
        remote_main = remote_main_output[0]
        if remote_main != base_commit:
            raise OperatorIntakeError(
                "remote-main-drift",
                "remote main changed after proposal creation",
                retryable=True,
                details={"planned": base_commit, "observed": remote_main},
            )
        if not final_exists and not staging_exists:
            if not root_exists:
                try:
                    workspace_root.mkdir(mode=0o700)
                    _fsync_directory(workspace_root.parent)
                except OSError as exc:
                    raise OperatorIntakeError(
                        "workspace-root-create-failed",
                        f"cannot create no-follow publication workspace root: {exc}",
                        publication_phase="before_workspace",
                    ) from exc
                root_exists = True
            if not self._checked_workspace_directory(
                workspace_root, code="workspace-root-invalid"
            ):
                raise OperatorIntakeError(
                    "workspace-root-invalid",
                    f"publication workspace root could not be created: {workspace_root}",
                    publication_phase="before_workspace",
                )
            _fsync_directory(workspace_root)
            _, reservation_reused = self._create_or_load_reservation(
                reservation, expected=expected_reservation
            )
            workspace_reconciled = workspace_reconciled or reservation_reused
            self._before_clone(staging)
            try:
                root_descriptor = os.open(
                    workspace_root,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                )
                try:
                    os.mkdir(staging.name, mode=0o700, dir_fd=root_descriptor)
                    os.fsync(root_descriptor)
                finally:
                    os.close(root_descriptor)
            except OSError as exc:
                raise OperatorIntakeError(
                    "workspace-staging-create-failed",
                    f"cannot exclusively create reserved staging destination: {exc}",
                    publication_phase="before_workspace",
                ) from exc
            self._after_clone_destination_created(staging)
            self._run(
                [
                    "git",
                    "clone",
                    "--no-hardlinks",
                    "--no-local",
                    "--no-checkout",
                    str(registry.root),
                    str(staging),
                ]
            )
            self._run(["git", "remote", "set-url", "origin", remote], cwd=staging)
            self._run(["git", "checkout", "--detach", base_commit], cwd=staging)
            self._run(["git", "checkout", "-b", branch], cwd=staging)
            marker = self._marker_identity(plan, branch)
            self._set_phase(
                "local_workspace", phase_changed, workspace=staging, marker=marker
            )
            self._reconcile_staged_workspace(
                staging,
                marker=marker,
                plan=plan,
                branch=branch,
                remote=remote,
            )
        if not final_exists:
            assert marker is not None
            try:
                _fsync_tree(staging)
                _fsync_directory(workspace_root)
                self._before_workspace_rename(staging, workspace)
                _rename_noreplace(staging, workspace)
                _fsync_directory(workspace_root)
                self._after_workspace_rename_fsync(workspace)
            except OSError as exc:
                raise OperatorIntakeError(
                    "workspace-atomic-publication-failed",
                    f"cannot atomically publish staged workspace: {exc}",
                    details={"workspace": str(workspace), "staging": str(staging)},
                    publication_phase="local_workspace",
                ) from exc
            self._remove_exact_reservation(
                reservation, expected=expected_reservation
            )
            reservation_exists = False
            head = None
            diff = ""
        task_file = workspace / target_path
        if head is None:
            reviewed_bytes = _render_task(plan["task_json"])
            self._materialize_target_file(
                workspace,
                task_file,
                reviewed_bytes=reviewed_bytes,
                expected_sha256=str(plan["task_file_sha256"]),
                proposal_sha256=str(plan["proposal_sha256"]),
            )
            try:
                Registry.load(workspace)
            except Exception as exc:
                raise OperatorIntakeError(
                    "local-registry-validation-failed",
                    f"publication workspace Registry validation failed: {exc}",
                    publication_phase="local_workspace",
                ) from exc
            changed = self._run(
                ["git", "status", "--porcelain", "--untracked-files=all"], cwd=workspace
            ).splitlines()
            if changed not in ([f"?? {target_path}"], [f"A  {target_path}"]):
                raise OperatorIntakeError(
                    "publication-scope-drift",
                    "publication workspace changed outside the target task file",
                    details={"status": changed},
                    publication_phase="local_workspace",
                )
            self._before_git_add(task_file)
            self._run(["git", "add", "--", target_path], cwd=workspace)
            self._after_git_add(task_file)
            staged = self._run(
                ["git", "diff", "--cached", "--name-only"], cwd=workspace
            ).splitlines()
            if staged != [target_path]:
                raise OperatorIntakeError(
                    "publication-scope-drift",
                    "staged publication diff is not exactly the target task file",
                    details={"paths": staged},
                    publication_phase="local_workspace",
                )
            expected_target_hash = str(plan["task_file_sha256"])
            staged_target_hash = self._git_blob_sha256(workspace, f":{target_path}")
            if staged_target_hash != expected_target_hash:
                raise OperatorIntakeError(
                    "publication-index-target-hash-mismatch",
                    "staged publication target bytes do not match the reviewed proposal",
                    details={
                        "expected": expected_target_hash,
                        "observed": staged_target_hash,
                    },
                    publication_phase="local_workspace",
                )
            diff = self._run(["git", "diff", "--cached", "--binary"], cwd=workspace)
            assert_plan_unchanged()
            self._before_git_commit(task_file)
            final_worktree_hash = self._workspace_file_sha256(
                workspace, Path(target_path), phase="local_workspace"
            )
            final_index_hash = self._git_blob_sha256(workspace, f":{target_path}")
            if (
                final_worktree_hash != expected_target_hash
                or final_index_hash != expected_target_hash
            ):
                raise OperatorIntakeError(
                    "publication-precommit-target-hash-mismatch",
                    "publication target changed after staging; commit was not attempted",
                    details={
                        "expected": expected_target_hash,
                        "worktree": final_worktree_hash,
                        "index": final_index_hash,
                    },
                    publication_phase="local_workspace",
                )
            tree = self._run(["git", "write-tree"], cwd=workspace)
            tree_paths = self._run(
                [
                    "git",
                    "diff-tree",
                    "--no-commit-id",
                    "--name-only",
                    "-r",
                    base_commit,
                    tree,
                ],
                cwd=workspace,
            ).splitlines()
            tree_target_hash = self._git_blob_sha256(workspace, f"{tree}:{target_path}")
            if tree_paths != [target_path] or tree_target_hash != expected_target_hash:
                raise OperatorIntakeError(
                    "publication-tree-mismatch",
                    "immutable publication tree is outside scope or has unreviewed bytes",
                    details={
                        "paths": tree_paths,
                        "expected": expected_target_hash,
                        "observed": tree_target_hash,
                    },
                    publication_phase="local_workspace",
                )
            base_date = self._run(
                ["git", "show", "-s", "--format=%aI", base_commit], cwd=workspace
            )
            candidate_head = self._run(
                [
                    "/usr/bin/env",
                    f"GIT_AUTHOR_DATE={base_date}",
                    f"GIT_COMMITTER_DATE={base_date}",
                    "git",
                    "-c",
                    "user.name=Bureau Operator",
                    "-c",
                    "user.email=bureau-operator@localhost",
                    "commit-tree",
                    tree,
                    "-p",
                    base_commit,
                    "-m",
                    f"Register Bureau task {task_id}",
                ],
                cwd=workspace,
            )
            candidate_tree = self._run(
                ["git", "rev-parse", f"{candidate_head}^{{tree}}"], cwd=workspace
            )
            candidate_parent = self._run(
                ["git", "rev-parse", f"{candidate_head}^"], cwd=workspace
            )
            committed_target_hash = self._git_blob_sha256(
                workspace, f"{candidate_head}:{target_path}"
            )
            if (
                candidate_tree != tree
                or candidate_parent != base_commit
                or committed_target_hash != expected_target_hash
            ):
                raise OperatorIntakeError(
                    "publication-commit-target-hash-mismatch",
                    "local publication commit object is not the exact reviewed tree",
                    details={
                        "expected_tree": tree,
                        "observed_tree": candidate_tree,
                        "expected_parent": base_commit,
                        "observed_parent": candidate_parent,
                        "expected": expected_target_hash,
                        "observed": committed_target_hash,
                        "head": candidate_head,
                    },
                    publication_phase="local_workspace",
                )
            self._before_publication_ref_update(workspace, task_file, candidate_head)
            self._run(
                ["git", "update-ref", f"refs/heads/{branch}", candidate_head, base_commit],
                cwd=workspace,
            )
            head = self._run(["git", "rev-parse", "HEAD"], cwd=workspace)
            if head != candidate_head:
                raise OperatorIntakeError(
                    "publication-ref-update-mismatch",
                    "publication branch did not advance to the verified commit object",
                    details={"expected": candidate_head, "observed": head},
                    publication_phase="local_workspace",
                )
            post_commit_status = self._run(
                ["git", "status", "--porcelain", "--untracked-files=all"], cwd=workspace
            ).splitlines()
            if post_commit_status:
                raise OperatorIntakeError(
                    "publication-postcommit-workspace-drift",
                    "workspace or index changed after immutable tree capture; push was blocked",
                    details={"status": post_commit_status, "head": head},
                    publication_phase="local_workspace",
                )
            self._set_phase(
                "committed_locally", phase_changed, workspace=workspace, marker=marker
            )
        remote_main_output = self._run(["git", "ls-remote", remote, "refs/heads/main"]).split()
        if not remote_main_output:
            raise OperatorIntakeError(
                "remote-main-missing", "remote main ref is missing", retryable=True
            )
        remote_main = remote_main_output[0]
        if remote_main != base_commit:
            raise OperatorIntakeError(
                "remote-main-drift",
                "remote main changed immediately before publication push",
                retryable=True,
                publication_phase=getattr(self, "_publication_phase", None),
            )
        assert_plan_unchanged()
        remote_branch_output = self._run(
            ["git", "ls-remote", remote, f"refs/heads/{branch}"]
        ).split()
        if remote_branch_output:
            if remote_branch_output[0] != head:
                raise OperatorIntakeError(
                    "remote-branch-mismatch",
                    "remote publication branch does not match the exact local commit",
                    effect_started=True,
                    required_readback=["remote branch head", "target task file at remote head"],
                    details={"expected": head, "observed": remote_branch_output[0]},
                    publication_phase=getattr(self, "_publication_phase", None),
                )
            remote_target_hash = self._git_blob_sha256(
                workspace, f"{remote_branch_output[0]}:{target_path}"
            )
            if remote_target_hash != str(plan["task_file_sha256"]):
                raise OperatorIntakeError(
                    "remote-target-hash-mismatch",
                    "remote publication head does not contain the exact reviewed target bytes",
                    effect_started=True,
                    required_readback=["target task file at remote head"],
                    details={
                        "expected": plan["task_file_sha256"],
                        "observed": remote_target_hash,
                        "head": remote_branch_output[0],
                    },
                    publication_phase=getattr(self, "_publication_phase", None),
                )
            if PUBLICATION_PHASES.index(str(marker["phase"])) < PUBLICATION_PHASES.index(
                "push_confirmed"
            ):
                self._set_phase(
                    "push_confirmed", phase_changed, workspace=workspace, marker=marker
                )
        else:
            if PUBLICATION_PHASES.index(str(marker["phase"])) >= PUBLICATION_PHASES.index(
                "push_confirmed"
            ):
                raise OperatorIntakeError(
                    "remote-branch-missing-after-confirmation",
                    "workspace records a confirmed push but the exact remote branch is absent",
                    effect_started=True,
                    required_readback=["remote branch head"],
                    publication_phase=str(marker["phase"]),
                )
            self._set_phase(
                "push_attempted", phase_changed, workspace=workspace, marker=marker
            )
            self._run(["git", "push", "origin", f"HEAD:refs/heads/{branch}"], cwd=workspace)
            confirmed = self._run(
                ["git", "ls-remote", remote, f"refs/heads/{branch}"]
            ).split()
            if not confirmed or confirmed[0] != head:
                raise OperatorIntakeError(
                    "publication-readback-mismatch",
                    "remote branch readback does not match the pushed commit",
                    effect_started=True,
                    ambiguity=True,
                    required_readback=["remote branch head", "target task file at remote head"],
                    publication_phase="push_attempted",
                )
            remote_target_hash = self._git_blob_sha256(
                workspace, f"{confirmed[0]}:{target_path}"
            )
            if remote_target_hash != str(plan["task_file_sha256"]):
                raise OperatorIntakeError(
                    "remote-target-hash-mismatch",
                    "pushed remote head does not contain the exact reviewed target bytes",
                    effect_started=True,
                    ambiguity=True,
                    required_readback=["target task file at remote head"],
                    details={
                        "expected": plan["task_file_sha256"],
                        "observed": remote_target_hash,
                        "head": confirmed[0],
                    },
                    publication_phase="push_attempted",
                )
            self._set_phase(
                "push_confirmed", phase_changed, workspace=workspace, marker=marker
            )
        body = (
            f"Bureau-Task: {plan['publishing_task_id']}\n\n"
            f"Register reviewed candidate task `{task_id}`.\n\n"
            f"- proposal: `{plan['proposal_sha256']}`\n"
            f"- candidate: `{plan['candidate']['candidate_id']}`\n"
            f"- source event: `{plan['candidate']['event_id']}`\n"
            f"- target: `{target_path}`\n\n"
            "This PR does not queue, claim, dispatch, merge, deploy or verify the task.\n"
        )
        readback = self._pull_request_readback(
            repository=repository, branch=branch, head=head, cwd=workspace
        )
        if readback is None:
            if marker["phase"] == "pr_confirmed":
                raise OperatorIntakeError(
                    "publication-readback-mismatch",
                    "workspace records a confirmed pull request but none exists",
                    effect_started=True,
                    ambiguity=True,
                    required_readback=["pull request metadata", "remote branch head"],
                    publication_phase="pr_confirmed",
                )
            self._set_phase(
                "pr_attempted", phase_changed, workspace=workspace, marker=marker
            )
            url = self._run(
                [
                    "gh",
                    "pr",
                    "create",
                    "--repo",
                    repository,
                    "--base",
                    "main",
                    "--head",
                    branch,
                    "--title",
                    f"Register Bureau task {task_id}",
                    "--body",
                    body,
                ],
                cwd=workspace,
            )
            readback_raw = self._run(
                [
                    "gh",
                    "pr",
                    "view",
                    url,
                    "--repo",
                    repository,
                    "--json",
                    "number,url,state,headRefOid,headRefName,baseRefName",
                ],
                cwd=workspace,
            )
            try:
                readback = json.loads(readback_raw)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise OperatorIntakeError(
                    "publication-readback-invalid",
                    "created pull-request readback was not valid JSON",
                    effect_started=True,
                    ambiguity=True,
                    required_readback=["pull request metadata", "remote branch head"],
                    publication_phase="pr_attempted",
                ) from exc
            if (
                not isinstance(readback, dict)
                or readback.get("headRefOid") != head
                or readback.get("headRefName") != branch
                or readback.get("baseRefName") != "main"
                or readback.get("state") != "OPEN"
            ):
                raise OperatorIntakeError(
                    "publication-readback-mismatch",
                    "GitHub pull-request readback does not match the published branch",
                    effect_started=True,
                    ambiguity=True,
                    required_readback=["pull request metadata", "remote branch head"],
                    details={"readback": readback, "expected_head": head},
                    publication_phase="pr_attempted",
                )
        self._set_phase("pr_confirmed", phase_changed, workspace=workspace, marker=marker)
        target_file_sha256 = self._workspace_file_sha256(
            workspace, Path(target_path), phase="pr_confirmed"
        )
        if target_file_sha256 != str(plan["task_file_sha256"]):
            raise OperatorIntakeError(
                "workspace-target-hash-mismatch",
                "confirmed publication workspace target does not match reviewed bytes",
                effect_started=True,
                required_readback=["target task file at remote head"],
                details={
                    "expected": plan["task_file_sha256"],
                    "observed": target_file_sha256,
                },
                publication_phase="pr_confirmed",
            )
        return {
            "repository": repository,
            "workspace": str(workspace),
            "branch": branch,
            "head": head,
            "pull_request": readback,
            "url": readback.get("url"),
            "git_diff_sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
            "target_file_sha256": target_file_sha256,
            "readback_complete": True,
            "publication_phase": "pr_confirmed",
            "workspace_reconciled": workspace_reconciled,
            "does_not_establish": [
                "queue_truth",
                "task_readiness",
                "claim_or_dispatch_authority",
                "merge_or_deployment_authority",
                "task_verification",
            ],
        }
