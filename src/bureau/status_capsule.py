"""Independent read-only Bureau status capsule.

The writer reads Git, the Registry and one consistent SQLite backup without
mutating any of them. It atomically publishes a bounded JSON snapshot. The
reader needs only that JSON file; it never opens Git, the Registry, SQLite,
GitHub or Grabowski.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import tarfile
import tempfile
from collections import Counter
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import legacy

if TYPE_CHECKING:
    from .v2 import Registry

CAPSULE_SCHEMA_VERSION = 1
DEFAULT_FRESHNESS_SECONDS = 900
DEFAULT_MAX_RUNS = 25
MAX_ACTIVE_ITEMS = 100
MAX_CAPSULE_BYTES = 1_000_000
MAX_REPO_FINDINGS = 20
ACTIVE_RUN_STATES = frozenset({"assigned", "running", "verifying"})
DOES_NOT_ESTABLISH = (
    "registry_mutation",
    "state_store_mutation",
    "shell_authority",
    "task_verification",
    "queue_mutation",
    "claim_authority",
    "dispatch_authority",
    "merge_authority",
    "deployment_authority",
    "reviewer_identity",
    "snapshot_authenticity_or_signature",
    "remote_origin_freshness",
)


class CapsuleError(RuntimeError):
    """Raised when no new trustworthy capsule can be published."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise CapsuleError("capsule timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CapsuleError("capsule timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise CapsuleError("capsule timestamp lacks timezone")
    return parsed.astimezone(timezone.utc)


def default_capsule_path() -> Path:
    configured = os.environ.get("BUREAU_STATUS_CAPSULE")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local/state/bureau-readonly/status-capsule.json"


def failure_path(path: Path) -> Path:
    return path.with_name(path.name + ".last-failure.json")


def _canonical_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _pretty_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _content_sha256(value: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _seal(value: dict[str, Any]) -> dict[str, Any]:
    if "content_sha256" in value:
        raise CapsuleError("content_sha256 must not be pre-populated")
    return {**value, "content_sha256": _content_sha256(value)}


def _verify_sealed(value: Any, *, kind: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CapsuleError(f"{kind} document is not an object")
    if value.get("schema_version") != CAPSULE_SCHEMA_VERSION:
        raise CapsuleError(f"unsupported {kind} schema version")
    if value.get("kind") != kind:
        raise CapsuleError(f"unexpected {kind} document kind")
    expected = value.get("content_sha256")
    if not isinstance(expected, str) or len(expected) != 64:
        raise CapsuleError(f"{kind} content hash is missing")
    body = {key: item for key, item in value.items() if key != "content_sha256"}
    actual = _content_sha256(body)
    if actual != expected:
        raise CapsuleError(f"{kind} content hash mismatch")
    return value


def _read_sealed(path: Path, *, kind: str) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CapsuleError(f"{kind} file is missing") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise CapsuleError(f"{kind} file is unreadable") from exc
    return _verify_sealed(raw, kind=kind)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    requested = path.expanduser()
    parent = requested.parent
    if parent.exists():
        if not parent.is_dir():
            raise CapsuleError("capsule output parent is not a directory")
    else:
        parent.mkdir(parents=True, mode=0o700)
    if requested.is_symlink():
        raise CapsuleError("capsule output must not be a symbolic link")
    resolved_parent = parent.resolve(strict=True)
    target = resolved_parent / requested.name
    if target.is_symlink():
        raise CapsuleError("capsule output must not be a symbolic link")
    legacy.atomic_write(target, _pretty_bytes(value).decode("utf-8"))


def _collector_identity() -> dict[str, Any]:
    from importlib.metadata import PackageNotFoundError, version

    module = Path(__file__).resolve()
    try:
        package_version = version("heimgewebe-bureau")
    except PackageNotFoundError:
        package_version = "uninstalled-source-tree"
    return {
        "distribution": "heimgewebe-bureau",
        "package_version": package_version,
        "module": "bureau.status_capsule",
        "module_sha256": hashlib.sha256(module.read_bytes()).hexdigest(),
        "identity_scope": "running-module-bytes",
    }


def _git_environment() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", str(Path.home())),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
    }


def _git_command(root: Path, *arguments: str) -> list[str]:
    return [
        "git",
        "--no-pager",
        "--no-optional-locks",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.pager=cat",
        "-c",
        "diff.external=",
        "-c",
        "interactive.diffFilter=",
        "-C",
        str(root),
        *arguments,
    ]


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        _git_command(root, *arguments),
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
        env=_git_environment(),
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "git command failed"
        raise CapsuleError(detail)
    return completed.stdout.strip()


def _registry_digest(root: Path) -> tuple[str, int]:
    registry_root = root / "registry"
    files = sorted(registry_root.rglob("*.json"))
    if not files:
        raise CapsuleError("registry contains no JSON documents")
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
        digest.update(b"\n")
    return digest.hexdigest(), len(files)


def _registry_identity(root: Path) -> dict[str, Any]:
    resolved = root.expanduser().resolve()
    if not resolved.is_dir():
        raise CapsuleError("registry root is unavailable")
    head = _git(resolved, "rev-parse", "HEAD")
    status_output = _git(
        resolved,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    dirty_paths = [line for line in status_output.splitlines() if line]
    if dirty_paths:
        raise CapsuleError("registry root is dirty; refusing to publish a canonical capsule")
    tree = _git(resolved, "rev-parse", "HEAD:registry")
    registry_sha256, file_count = _registry_digest(resolved)
    return {
        "source": "clean-worktree-head",
        "source_scope": "local-head-without-fetch",
        "observed_ref": "HEAD",
        "observed_ref_head": head,
        "remote_freshness": "not-observed",
        "root": str(resolved),
        "git_head": head,
        "registry_tree": tree,
        "registry_sha256": registry_sha256,
        "registry_json_files": file_count,
        "dirty": False,
        "canonical_repo": None,
    }


def _safe_extract(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, "r") as handle:
        destination_root = destination.resolve()
        for member in handle.getmembers():
            target = (destination / member.name).resolve()
            if destination_root not in (target, *target.parents):
                raise CapsuleError("registry archive contains an unsafe path")
            if member.issym() or member.islnk():
                raise CapsuleError("registry archive contains an unsupported link")
            if not member.isfile() and not member.isdir():
                raise CapsuleError("registry archive contains an unsupported entry")
        handle.extractall(destination)


def _archive_canonical_registry(repo: Path, destination: Path) -> dict[str, Any]:
    canonical = repo.expanduser().resolve()
    head = _git(canonical, "rev-parse", "--verify", "origin/main^{commit}")
    tree = _git(canonical, "rev-parse", f"{head}:registry")
    archive = destination / "registry.tar"
    completed = subprocess.run(
        _git_command(
            canonical,
            "archive",
            "--format=tar",
            f"--output={archive}",
            head,
            "registry",
            "schemas",
        ),
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
        env=_git_environment(),
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "git archive failed"
        raise CapsuleError(detail)
    snapshot_root = destination / "registry-snapshot"
    snapshot_root.mkdir(mode=0o700)
    _safe_extract(archive, snapshot_root)
    registry_sha256, file_count = _registry_digest(snapshot_root)
    return {
        "snapshot_root": snapshot_root,
        "identity": {
            "source": "local-origin-main-archive",
            "source_scope": "local-origin-main-without-fetch",
            "observed_ref": "origin/main",
            "observed_ref_head": head,
            "remote_freshness": "not-observed",
            "root": str(canonical),
            "git_head": head,
            "registry_tree": tree,
            "registry_sha256": registry_sha256,
            "registry_json_files": file_count,
            "dirty": False,
            "canonical_repo": str(canonical),
        },
    }


def _state_db_path(state_db: Path | None, state_root: Path | None) -> Path:
    resolved_root = state_root.expanduser().resolve() if state_root is not None else None
    if state_db is not None:
        resolved = state_db.expanduser().resolve()
        if resolved_root is not None and resolved.parent != resolved_root:
            raise CapsuleError("state database must be directly inside state_root")
        return resolved
    if resolved_root is not None:
        return resolved_root / "bureau.sqlite3"
    configured = os.environ.get("BUREAU_STATE_DIR")
    root = Path(configured).expanduser() if configured else Path.home() / ".local/state/bureau"
    return (root / "bureau.sqlite3").resolve()


def _backup_state_database(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise CapsuleError("Bureau state database is unavailable")
    source_connection: sqlite3.Connection | None = None
    destination_connection: sqlite3.Connection | None = None
    try:
        source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=10)
        source_connection.execute("PRAGMA query_only=ON")
        destination_connection = sqlite3.connect(destination)
        source_connection.backup(destination_connection)
    except sqlite3.Error as exc:
        raise CapsuleError(f"state snapshot failed: {type(exc).__name__}: {exc}") from exc
    finally:
        if destination_connection is not None:
            destination_connection.close()
        if source_connection is not None:
            source_connection.close()


def _bounded_text(value: Any, *, limit: int = 2000) -> Any:
    if not isinstance(value, str) or len(value) <= limit:
        return value
    return value[:limit] + "…<truncated>"


def _bounded_json(value: Any, *, depth: int = 0) -> Any:
    if depth > 6:
        return "<depth-truncated>"
    if isinstance(value, str):
        return _bounded_text(value)
    if isinstance(value, list):
        return [_bounded_json(item, depth=depth + 1) for item in value[:MAX_REPO_FINDINGS]]
    if isinstance(value, dict):
        return {
            str(key): _bounded_json(item, depth=depth + 1)
            for key, item in list(value.items())[:50]
        }
    return value


def _freshness_threshold(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 60:
        raise CapsuleError("snapshot freshness threshold is invalid")
    return value


def _bounded_list(values: list[Any], *, limit: int = MAX_ACTIVE_ITEMS) -> dict[str, Any]:
    return {
        "count": len(values),
        "truncated": len(values) > limit,
        "items": values[:limit],
    }


def _public_run(row: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "run_id",
        "task_id",
        "worker_id",
        "attempt",
        "state",
        "task_sha256",
        "plan_sha256",
        "external_system",
        "external_id",
        "external_state",
        "external_observed_at",
        "workspace_path",
        "workspace_branch",
        "error",
        "created_at",
        "updated_at",
        "heartbeat_at",
    )
    return {key: _bounded_text(row.get(key)) for key in allowed}


def _read_state(snapshot: Path, source: Path, *, max_runs: int) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign = [dict(row) for row in connection.execute("PRAGMA foreign_key_check")]
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        required = {
            "runs",
            "reservations",
            "task_status",
            "receipts",
            "workspaces",
            "events",
        }
        missing = sorted(required - tables)
        if missing:
            raise CapsuleError("state snapshot lacks tables: " + ", ".join(missing))
        run_rows = [dict(row) for row in connection.execute("SELECT * FROM runs")]
        active_rows = [row for row in run_rows if row.get("state") in ACTIVE_RUN_STATES]
        recent_rows = sorted(
            run_rows,
            key=lambda row: str(row.get("created_at") or ""),
            reverse=True,
        )[:max_runs]
        leases = [
            dict(row)
            for row in connection.execute(
                """
                SELECT r.run_id,r.resource_id,r.mode,r.amount,r.created_at
                FROM reservations r
                JOIN runs u ON u.run_id=r.run_id
                WHERE u.state IN ('assigned','running','verifying')
                ORDER BY r.resource_id,r.run_id
                """
            )
        ]
        task_status = [dict(row) for row in connection.execute("SELECT * FROM task_status")]
        receipts = [dict(row) for row in connection.execute("SELECT * FROM receipts")]
        workspaces = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM workspaces WHERE state<>'removed' ORDER BY updated_at DESC"
            )
        ]
        events = connection.execute(
            "SELECT COUNT(*) AS count,MAX(event_id) AS max_event_id FROM events"
        ).fetchone()
        connection.execute("ROLLBACK")
    finally:
        connection.close()
    stat = source.stat()
    return {
        "available": True,
        "source_path": str(source),
        "source_size_bytes": stat.st_size,
        "source_mtime_ns": stat.st_mtime_ns,
        "integrity": integrity,
        "foreign_key_errors": foreign,
        "schema_version": version,
        "tables": sorted(tables),
        "runs": {
            "total": len(run_rows),
            "states": dict(sorted(Counter(str(row.get("state")) for row in run_rows).items())),
            "active_count": len(active_rows),
            "active_truncated": len(active_rows) > MAX_ACTIVE_ITEMS,
            "active": [_public_run(row) for row in active_rows[:MAX_ACTIVE_ITEMS]],
            "recent_limit": max_runs,
            "recent_truncated": len(run_rows) > len(recent_rows),
            "recent": [_public_run(row) for row in recent_rows],
        },
        "leases": {
            "active_count": len(leases),
            "truncated": len(leases) > MAX_ACTIVE_ITEMS,
            "items": leases[:MAX_ACTIVE_ITEMS],
        },
        "task_status_rows": task_status,
        "receipt_rows": receipts,
        "workspaces": workspaces,
        "events": {
            "count": int(events["count"]),
            "max_event_id": events["max_event_id"],
        },
    }


def _compact_repo_balls(value: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for repo_id, ball in sorted(value.items()):
        task_ids = ball.get("task_ids") if isinstance(ball.get("task_ids"), list) else []
        current = ball.get("current_ball")
        current_ball = None
        if isinstance(current, dict):
            current_ball = {
                key: _bounded_text(current.get(key))
                for key in (
                    "kind",
                    "task_id",
                    "title",
                    "queue_lane",
                    "run_id",
                    "state",
                )
                if current.get(key) is not None
            }
        active_runs = ball.get("active_runs")
        if not isinstance(active_runs, list):
            active_runs = []
        findings = ball.get("findings")
        if not isinstance(findings, list):
            findings = []
        result[repo_id] = {
            "resource": ball.get("resource"),
            "status": ball.get("status"),
            "current_ball": current_ball,
            "active_runs": _bounded_list(
                [_bounded_json(item) for item in active_runs],
                limit=MAX_ACTIVE_ITEMS,
            ),
            "findings": _bounded_list(
                [_bounded_json(item) for item in findings],
                limit=MAX_REPO_FINDINGS,
            ),
            "task_count": len(task_ids),
        }
    return result


def _mirror_doctor_markers(
    snapshot: Path,
    source_root: Path,
    destination_root: Path,
) -> None:
    connection = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        active_run_ids = [
            str(row["run_id"])
            for row in connection.execute(
                "SELECT run_id FROM runs WHERE state IN ('assigned','running','verifying')"
            )
        ]
        receipt_run_ids = [
            str(row["run_id"])
            for row in connection.execute("SELECT run_id FROM receipts")
        ]
    finally:
        connection.close()
    for directory, run_ids in (
        ("envelopes", active_run_ids),
        ("receipts", receipt_run_ids),
    ):
        destination = destination_root / directory
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        for run_id in run_ids:
            source = source_root / directory / f"{run_id}.json"
            if source.exists():
                marker = destination / f"{run_id}.json"
                marker.touch(mode=0o600)


def _compact_truth(value: dict[str, Any]) -> dict[str, Any]:
    errors = value.get("errors") if isinstance(value.get("errors"), list) else []
    warnings = value.get("warnings") if isinstance(value.get("warnings"), list) else []
    return {
        "healthy": value.get("healthy") is True,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": [_bounded_json(item) for item in errors[:20]],
        "warnings": [_bounded_json(item) for item in warnings[:20]],
        "truncated": len(errors) > 20 or len(warnings) > 20,
    }


def _canonical_doctor(
    registry: Registry,
    snapshot: Path,
    source_state_root: Path,
    snapshot_state_root: Path,
    source_schema_version: int,
) -> dict[str, Any]:
    from .v2 import Dispatcher, StateStore, state_root_hygiene

    store = StateStore(snapshot, snapshot_state_root)
    _mirror_doctor_markers(snapshot, source_state_root, snapshot_state_root)
    report = Dispatcher(
        registry,
        store,
        open_pr_reservations_provider=lambda _registry: [],
    ).doctor()
    source_hygiene = state_root_hygiene(
        source_state_root,
        source_state_root / "bureau.sqlite3",
    )
    doctor_schema_version = int(report.get("database", {}).get("schema_version", -1))
    migration_applied_to_copy = doctor_schema_version != source_schema_version
    healthy = (
        report.get("healthy") is True
        and source_hygiene.get("healthy") is True
        and not migration_applied_to_copy
    )
    return {
        "healthy": healthy,
        "read_only": True,
        "source": "canonical-dispatcher-doctor-on-state-copy",
        "database": report.get("database"),
        "source_schema_version": source_schema_version,
        "migration_applied_to_copy": migration_applied_to_copy,
        "state_root_hygiene": _bounded_json(source_hygiene),
        "missing_envelopes": _bounded_list(report.get("missing_envelopes", [])),
        "missing_receipts": _bounded_list(report.get("missing_receipts", [])),
        "stale_tasks": _bounded_list(report.get("stale_tasks", [])),
        "workspace_findings": _bounded_list(report.get("workspace_findings", [])),
        "queue_findings": _bounded_list(report.get("queue_findings", [])),
        "lifecycle": _bounded_json(report.get("lifecycle", [])),
        "runtime_truth": _bounded_json(report.get("runtime_truth")),
        "registry_truth": _compact_truth(report.get("registry_truth", {})),
        "does_not_repair": True,
    }


def _previous_snapshot(path: Path) -> dict[str, Any] | None:
    try:
        value = _read_sealed(path, kind="bureau-status-capsule")
    except CapsuleError:
        return None
    return {
        "created_at": value.get("created_at"),
        "registry_head": value.get("registry", {}).get("git_head"),
        "content_sha256": value.get("content_sha256"),
    }


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _assert_output_boundary(
    output: Path | None,
    *,
    source_roots: list[Path],
    state_path: Path,
) -> None:
    if output is None:
        return
    resolved = output.expanduser().resolve()
    forbidden = [root.expanduser().resolve() for root in source_roots]
    forbidden.append(state_path.parent.resolve())
    if any(_is_relative_to(resolved, root) for root in forbidden):
        raise CapsuleError("capsule output must be outside registry and state source roots")


def build_capsule(
    root: Path,
    *,
    state_db: Path | None = None,
    state_root: Path | None = None,
    canonical_repo: Path | None = None,
    output: Path | None = None,
    freshness_seconds: int = DEFAULT_FRESHNESS_SECONDS,
    max_runs: int = DEFAULT_MAX_RUNS,
    now: datetime | None = None,
) -> dict[str, Any]:
    from .registry_truth import registry_truth_diagnostics
    from .status_projection import status_projection
    from .v2 import Registry

    if freshness_seconds < 60:
        raise CapsuleError("freshness threshold must be at least 60 seconds")
    if max_runs < 1 or max_runs > 100:
        raise CapsuleError("max-runs must be between 1 and 100")
    created = (now or _utc_now()).astimezone(timezone.utc)
    resolved_state = _state_db_path(state_db, state_root)
    resolved_state_root = (
        state_root.expanduser().resolve() if state_root is not None else resolved_state.parent
    )
    source_roots = [root, resolved_state_root]
    if canonical_repo is not None:
        source_roots.append(canonical_repo)
    _assert_output_boundary(output, source_roots=source_roots, state_path=resolved_state)
    previous = _previous_snapshot(output) if output is not None else None
    with tempfile.TemporaryDirectory(prefix="bureau-status-capsule-") as temp_dir:
        temporary = Path(temp_dir)
        if canonical_repo is not None:
            archived = _archive_canonical_registry(canonical_repo, temporary)
            resolved_root = archived["snapshot_root"]
            registry_identity = archived["identity"]
        else:
            resolved_root = root.expanduser().resolve()
            registry_identity = _registry_identity(resolved_root)
        registry = Registry.load(resolved_root)
        snapshot_state_root = temporary / "state"
        snapshot_state_root.mkdir(mode=0o700)
        snapshot_path = snapshot_state_root / "bureau.sqlite3"
        _backup_state_database(resolved_state, snapshot_path)
        state = _read_state(snapshot_path, resolved_state, max_runs=max_runs)
        projection = status_projection(
            resolved_root,
            registry=registry,
            state_db=snapshot_path,
            github=None,
            now=_timestamp(created),
        )
        truth = registry_truth_diagnostics(resolved_root, probe_baselines=False)
        doctor = _canonical_doctor(
            registry,
            snapshot_path,
            resolved_state_root,
            snapshot_state_root,
            int(state["schema_version"]),
        )
    repo_balls = _compact_repo_balls(projection.get("repository_balls", {}))
    repo_summary = Counter(str(item.get("status")) for item in repo_balls.values())
    body = {
        "schema_version": CAPSULE_SCHEMA_VERSION,
        "kind": "bureau-status-capsule",
        "read_only": True,
        "created_at": _timestamp(created),
        "freshness": {
            "threshold_seconds": freshness_seconds,
            "status_at_creation": "fresh",
            "age_seconds_at_creation": 0,
        },
        "registry": registry_identity,
        "collector": _collector_identity(),
        "observation_scope": {
            "registry": "local-origin-main-without-fetch",
            "state_store": "consistent-read-only-sqlite-backup",
            "github": "not-observed",
            "grabowski": "not-required",
            "network": "not-used",
        },
        "state_store": {
            key: value
            for key, value in state.items()
            if key not in {
                "task_status_rows",
                "receipt_rows",
                "workspaces",
                "runs",
                "leases",
            }
        },
        "runs": state["runs"],
        "leases": state["leases"],
        "repo_balls": {
            "summary": {
                "repositories": len(repo_balls),
                "status_counts": dict(sorted(repo_summary.items())),
            },
            "items": repo_balls,
        },
        "doctor": doctor,
        "registry_truth": {
            **_compact_truth(truth),
            "baseline_probe": "skipped-for-bounded-read-only-capsule",
        },
        "last_successful_snapshot": previous,
        "does_not_establish": list(DOES_NOT_ESTABLISH),
    }
    sealed = _seal(body)
    size = len(_pretty_bytes(sealed))
    if size > MAX_CAPSULE_BYTES:
        raise CapsuleError(
            f"capsule exceeds bounded size limit: {size}>{MAX_CAPSULE_BYTES}"
        )
    return sealed


def _failure_document(
    path: Path,
    error: Exception,
    *,
    attempted_at: datetime,
) -> dict[str, Any]:
    body = {
        "schema_version": CAPSULE_SCHEMA_VERSION,
        "kind": "bureau-status-capsule-failure",
        "read_only": True,
        "attempted_at": _timestamp(attempted_at),
        "target": str(path.expanduser()),
        "error_type": type(error).__name__,
        "error": _bounded_text(str(error)),
        "last_successful_snapshot": _previous_snapshot(path),
        "does_not_establish": list(DOES_NOT_ESTABLISH),
    }
    return _seal(body)


def write_capsule(
    root: Path,
    *,
    output: Path | None = None,
    state_db: Path | None = None,
    state_root: Path | None = None,
    canonical_repo: Path | None = None,
    freshness_seconds: int = DEFAULT_FRESHNESS_SECONDS,
    max_runs: int = DEFAULT_MAX_RUNS,
    now: datetime | None = None,
) -> dict[str, Any]:
    path = (output or default_capsule_path()).expanduser()
    resolved_state = _state_db_path(state_db, state_root)
    resolved_state_root = (
        state_root.expanduser().resolve() if state_root is not None else resolved_state.parent
    )
    source_roots = [root, resolved_state_root]
    if canonical_repo is not None:
        source_roots.append(canonical_repo)
    _assert_output_boundary(path, source_roots=source_roots, state_path=resolved_state)
    attempted = (now or _utc_now()).astimezone(timezone.utc)
    try:
        capsule = build_capsule(
            root,
            state_db=state_db,
            state_root=state_root,
            canonical_repo=canonical_repo,
            output=path,
            freshness_seconds=freshness_seconds,
            max_runs=max_runs,
            now=attempted,
        )
        _atomic_json(path, capsule)
    except Exception as exc:
        failure = _failure_document(path, exc, attempted_at=attempted)
        try:
            _atomic_json(failure_path(path), failure)
        except Exception as failure_error:
            raise CapsuleError(
                f"{exc}; refresh failure evidence could not be written: {failure_error}"
            ) from exc
        raise CapsuleError(str(exc)) from exc
    with suppress(FileNotFoundError):
        failure_path(path).unlink()
    return {
        "schema_version": CAPSULE_SCHEMA_VERSION,
        "command": "write",
        "written": True,
        "path": str(path),
        "created_at": capsule["created_at"],
        "content_sha256": capsule["content_sha256"],
        "freshness_threshold_seconds": freshness_seconds,
        "doctor_healthy": capsule["doctor"]["healthy"],
        "registry_truth_healthy": capsule["registry_truth"]["healthy"],
        "does_not_establish": list(DOES_NOT_ESTABLISH),
    }


def _read_failure(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    sidecar = failure_path(path)
    if not sidecar.exists():
        return None, None
    try:
        return _read_sealed(sidecar, kind="bureau-status-capsule-failure"), None
    except CapsuleError as exc:
        return None, str(exc)


def read_capsule(path: Path | None = None, *, now: datetime | None = None) -> dict[str, Any]:
    capsule_path = (path or default_capsule_path()).expanduser()
    observed = (now or _utc_now()).astimezone(timezone.utc)
    failure, failure_error = _read_failure(capsule_path)
    try:
        capsule = _read_sealed(capsule_path, kind="bureau-status-capsule")
        created = _parse_timestamp(capsule.get("created_at"))
        threshold = _freshness_threshold(
            capsule.get("freshness", {}).get("threshold_seconds")
        )
    except CapsuleError as exc:
        return {
            "schema_version": CAPSULE_SCHEMA_VERSION,
            "command": "read",
            "read_only": True,
            "status": "unavailable",
            "path": str(capsule_path),
            "observed_at": _timestamp(observed),
            "age_seconds": None,
            "freshness_threshold_seconds": None,
            "reasons": [str(exc)],
            "last_successful_snapshot": None,
            "last_refresh_failure": failure
            if failure is not None
            else ({"status": "unreadable", "error": failure_error} if failure_error else None),
            "snapshot": None,
            "does_not_establish": list(DOES_NOT_ESTABLISH),
        }
    signed_age = (observed - created).total_seconds()
    age = max(0.0, signed_age)
    reasons: list[str] = []
    if signed_age < -5:
        reasons.append("snapshot timestamp is in the future")
    if failure_error is not None:
        reasons.append("refresh failure evidence is unreadable")
    if age > threshold:
        reasons.append("snapshot age exceeds freshness threshold")
    if failure is not None:
        try:
            failed_at = _parse_timestamp(failure.get("attempted_at"))
        except CapsuleError:
            failed_at = None
            reasons.append("refresh failure timestamp is invalid")
        if failed_at is not None and failed_at > created:
            reasons.append("refresh failed after the last successful snapshot")
    status = "fresh" if not reasons else "stale"
    return {
        "schema_version": CAPSULE_SCHEMA_VERSION,
        "command": "read",
        "read_only": True,
        "status": status,
        "path": str(capsule_path),
        "observed_at": _timestamp(observed),
        "age_seconds": age,
        "freshness_threshold_seconds": threshold,
        "reasons": reasons,
        "last_successful_snapshot": {
            "created_at": capsule["created_at"],
            "registry_head": capsule.get("registry", {}).get("git_head"),
            "content_sha256": capsule["content_sha256"],
        },
        "last_refresh_failure": failure
        if failure is not None
        else ({"status": "unreadable", "error": failure_error} if failure_error else None),
        "snapshot": capsule,
        "does_not_establish": list(DOES_NOT_ESTABLISH),
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="bureau-status-capsule")
    sub = result.add_subparsers(dest="command", required=True)
    write = sub.add_parser("write")
    write.add_argument("--root")
    write.add_argument("--state-db")
    write.add_argument("--state-root")
    write.add_argument("--canonical-repo")
    write.add_argument("--output")
    write.add_argument("--freshness-seconds", type=int, default=DEFAULT_FRESHNESS_SECONDS)
    write.add_argument("--max-runs", type=int, default=DEFAULT_MAX_RUNS)
    read = sub.add_parser("read")
    read.add_argument("--path")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "write":
        if not args.root and not args.canonical_repo:
            print(
                json.dumps(
                    {
                        "schema_version": CAPSULE_SCHEMA_VERSION,
                        "command": "write",
                        "written": False,
                        "status": "unavailable",
                        "error": "write requires --root or --canonical-repo",
                        "does_not_establish": list(DOES_NOT_ESTABLISH),
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 2
        try:
            result = write_capsule(
                Path(args.root or args.canonical_repo or "."),
                output=Path(args.output).expanduser() if args.output else None,
                state_db=Path(args.state_db).expanduser() if args.state_db else None,
                state_root=Path(args.state_root).expanduser() if args.state_root else None,
                canonical_repo=(
                    Path(args.canonical_repo).expanduser() if args.canonical_repo else None
                ),
                freshness_seconds=args.freshness_seconds,
                max_runs=args.max_runs,
            )
        except CapsuleError as exc:
            print(
                json.dumps(
                    {
                        "schema_version": CAPSULE_SCHEMA_VERSION,
                        "command": "write",
                        "written": False,
                        "status": "unavailable",
                        "error": str(exc),
                        "does_not_establish": list(DOES_NOT_ESTABLISH),
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    result = read_capsule(Path(args.path).expanduser() if args.path else None)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "fresh" else 1 if result["status"] == "stale" else 2


if __name__ == "__main__":
    raise SystemExit(main())
