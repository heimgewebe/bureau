"""Gated, one-shot refresh of the immutable Bureau runtime.

The observer is read-only with respect to Git, Registry truth and the deployed
runtime.  The apply path requires an explicit hash-bound intent plus an externally
supplied owner/task binding verified against live Grabowski leases.  It never retries
an attempt whose durable
start record already exists.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_REPOSITORY = "heimgewebe/bureau"
DEFAULT_REMOTE_URL = "git@github.com:heimgewebe/bureau.git"
DEFAULT_REQUIRED_CHECKS = ("validate (3.10)", "validate (3.12)")
DEFAULT_SLO_SECONDS = 5400
DEFAULT_INTENT_TTL_SECONDS = 900
DEFAULT_MIN_LEASE_REMAINING_SECONDS = 600
SUPPORTED_GRABOWSKI_RESOURCE_DB_SCHEMAS = frozenset({"1", "2"})
DEFAULT_GRABOWSKI_RESOURCE_DB = Path("~/.local/state/grabowski/resources.sqlite3").expanduser()
MAX_JSON_BYTES = 256 * 1024


class RuntimeRefreshError(RuntimeError):
    """A typed fail-closed runtime refresh error."""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise RuntimeRefreshError("invalid-timestamp", f"invalid timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise RuntimeRefreshError("naive-timestamp", f"timestamp has no timezone: {value!r}")
    return parsed.astimezone(timezone.utc)


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def payload_digest(value: dict[str, Any], field: str) -> str:
    payload = dict(value)
    payload.pop(field, None)
    return sha256_bytes(canonical_bytes(payload))


def bind_digest(value: dict[str, Any], field: str) -> dict[str, Any]:
    bound = dict(value)
    bound[field] = payload_digest(bound, field)
    return bound


def verify_digest(value: dict[str, Any], field: str) -> None:
    observed = value.get(field)
    expected = payload_digest(value, field)
    if observed != expected:
        raise RuntimeRefreshError(
            "digest-mismatch",
            f"{field} does not match payload",
            details={"expected": expected, "observed": observed},
        )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.parent / f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def create_only(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(path.parent)
    except Exception:
        if path.exists() and path.stat().st_size == 0:
            path.unlink()
        raise


def read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeRefreshError("invalid-json-path", f"not a regular file: {path}")
    if path.stat().st_size > MAX_JSON_BYTES:
        raise RuntimeRefreshError("json-too-large", f"JSON file exceeds limit: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeRefreshError("invalid-json", f"cannot read JSON: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeRefreshError("invalid-json-object", f"JSON root is not an object: {path}")
    return value


def _run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    timeout: float = 60,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise
    except OSError as exc:
        raise RuntimeRefreshError(
            "command-unavailable",
            f"failed to start {argv[0]}",
            details={"argv": argv, "error": str(exc)},
        ) from exc


def _require_command(result: subprocess.CompletedProcess[str], argv: list[str]) -> str:
    if result.returncode:
        raise RuntimeRefreshError(
            "command-failed",
            f"command failed: {argv[0]}",
            details={
                "argv": argv,
                "returncode": result.returncode,
                "stdout": result.stdout[-4000:],
                "stderr": result.stderr[-4000:],
            },
        )
    return result.stdout.strip()


def gh_json(arguments: list[str], *, timeout: float = 60) -> Any:
    argv = ["gh", *arguments]
    output = _require_command(_run(argv, timeout=timeout), argv)
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeRefreshError(
            "github-json-invalid",
            "GitHub command returned invalid JSON",
            details={"argv": argv, "output": output[-4000:]},
        ) from exc


def load_manifest(path: Path) -> tuple[dict[str, Any], str]:
    value = read_json(path)
    if value.get("kind") != "bureau_runtime_deployment":
        raise RuntimeRefreshError(
            "manifest-kind-invalid", "Bureau deployment manifest kind is invalid"
        )
    source_commit = value.get("source_commit")
    if not isinstance(source_commit, str) or len(source_commit) != 40:
        raise RuntimeRefreshError("manifest-source-invalid", "manifest source_commit is invalid")
    return value, sha256_bytes(path.read_bytes())


def _check_state(item: dict[str, Any]) -> str:
    raw = item.get("conclusion") or item.get("state") or item.get("status") or ""
    value = str(raw).upper()
    if value == "SUCCESS":
        return "success"
    if value in {"QUEUED", "PENDING", "IN_PROGRESS", "WAITING", "EXPECTED", "REQUESTED"}:
        return "pending"
    return "failure"


def summarize_required_checks(
    rollup: Any, required_checks: Iterable[str]
) -> dict[str, dict[str, Any]]:
    entries = rollup if isinstance(rollup, list) else []
    by_name: dict[str, list[str]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("context")
        if not isinstance(name, str):
            continue
        by_name.setdefault(name, []).append(_check_state(entry))
    result: dict[str, dict[str, Any]] = {}
    for name in required_checks:
        states = by_name.get(name, [])
        if not states:
            state = "missing"
        elif "failure" in states:
            state = "failure"
        elif "pending" in states:
            state = "pending"
        else:
            state = "success"
        result[name] = {"state": state, "observed_states": states}
    return result


def _target_payload(observation: dict[str, Any]) -> dict[str, Any]:
    return {
        "repository": observation.get("repository"),
        "main_commit": observation.get("main_commit"),
        "pull_request": observation.get("pull_request"),
        "merged_at": observation.get("merged_at"),
        "required_checks": observation.get("required_checks"),
        "check_summary": observation.get("check_summary"),
        "deployed_source_commit": observation.get("deployed_source_commit"),
        "deployed_manifest_sha256": observation.get("deployed_manifest_sha256"),
        "lag_commits": observation.get("lag_commits"),
    }


def observe_runtime_refresh(
    *,
    repository: str,
    manifest_path: Path,
    required_checks: tuple[str, ...] = DEFAULT_REQUIRED_CHECKS,
    slo_seconds: int = DEFAULT_SLO_SECONDS,
    now: datetime | None = None,
    github: Callable[[list[str]], Any] = gh_json,
) -> dict[str, Any]:
    observed_at = now or utc_now()
    reasons: list[str] = []
    manifest, manifest_sha = load_manifest(manifest_path)
    deployed = manifest["source_commit"]

    first_main = github(["api", f"repos/{repository}/commits/main"])
    if not isinstance(first_main, dict) or not isinstance(first_main.get("sha"), str):
        raise RuntimeRefreshError("github-main-invalid", "GitHub main commit response is invalid")
    main_commit = first_main["sha"]
    pull_request: dict[str, Any] | None = None
    merged_at: str | None = None
    check_summary: dict[str, dict[str, Any]] = {}
    lag_commits: int | None = 0 if deployed == main_commit else None

    if deployed != main_commit:
        associated = github(
            [
                "api",
                "-H",
                "Accept: application/vnd.github+json",
                f"repos/{repository}/commits/{main_commit}/pulls",
            ]
        )
        candidates = []
        for item in associated if isinstance(associated, list) else []:
            if not isinstance(item, dict):
                continue
            base = item.get("base") if isinstance(item.get("base"), dict) else {}
            if (
                item.get("merge_commit_sha") == main_commit
                and item.get("merged_at")
                and base.get("ref") == "main"
            ):
                candidates.append(item)
        if len(candidates) != 1:
            reasons.append("merged-main-pr-ambiguous")
        else:
            number = candidates[0].get("number")
            detail = github(
                [
                    "pr",
                    "view",
                    str(number),
                    "--repo",
                    repository,
                    "--json",
                    "number,state,isDraft,mergedAt,mergeCommit,headRefOid,baseRefName,statusCheckRollup,url",
                ]
            )
            merge_commit = detail.get("mergeCommit") if isinstance(detail, dict) else None
            merge_oid = merge_commit.get("oid") if isinstance(merge_commit, dict) else None
            if (
                not isinstance(detail, dict)
                or detail.get("state") != "MERGED"
                or detail.get("isDraft") is True
                or detail.get("baseRefName") != "main"
                or merge_oid != main_commit
                or not isinstance(detail.get("headRefOid"), str)
                or not detail.get("mergedAt")
            ):
                reasons.append("merged-main-pr-invalid")
            else:
                check_summary = summarize_required_checks(
                    detail.get("statusCheckRollup"), required_checks
                )
                bad_checks = [
                    name for name, item in check_summary.items() if item["state"] != "success"
                ]
                if bad_checks:
                    reasons.append("required-ci-not-green")
                pull_request = {
                    "number": detail["number"],
                    "url": detail.get("url"),
                    "head_commit": detail["headRefOid"],
                    "merge_commit": merge_oid,
                }
                merged_at = detail["mergedAt"]

        compare = github(["api", f"repos/{repository}/compare/{deployed}...{main_commit}"])
        if isinstance(compare, dict) and isinstance(compare.get("ahead_by"), int):
            lag_commits = compare["ahead_by"]
        else:
            reasons.append("commit-lag-unavailable")

        second_main = github(["api", f"repos/{repository}/commits/main"])
        if not isinstance(second_main, dict) or second_main.get("sha") != main_commit:
            reasons.append("main-changed-during-observation")

    age_seconds: int | None = None
    if merged_at:
        age_seconds = max(0, int((observed_at - parse_time(merged_at)).total_seconds()))

    if deployed == main_commit:
        status = "already_current"
    elif reasons:
        status = "blocked"
    elif age_seconds is not None and age_seconds > slo_seconds:
        status = "alert"
    else:
        status = "candidate"

    observation: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "bureau_runtime_refresh_observation",
        "repository": repository,
        "main_commit": main_commit,
        "pull_request": pull_request,
        "merged_at": merged_at,
        "required_checks": list(required_checks),
        "check_summary": check_summary,
        "deployed_source_commit": deployed,
        "deployed_manifest_sha256": manifest_sha,
        "lag_commits": lag_commits,
        "age_seconds": age_seconds,
        "slo_seconds": slo_seconds,
        "status": status,
        "reason_codes": reasons,
        "observed_at": isoformat(observed_at),
        "does_not_establish": [
            "deployment_authority",
            "external_lease_liveness",
            "future_main_stability",
            "runtime_semantic_correctness",
        ],
    }
    observation["target_sha256"] = sha256_bytes(canonical_bytes(_target_payload(observation)))
    return bind_digest(observation, "observation_sha256")


def persist_observation(state_root: Path, observation: dict[str, Any]) -> Path:
    verify_digest(observation, "observation_sha256")
    stamp = observation["observed_at"].replace(":", "").replace("-", "")
    path = (
        state_root
        / "observations"
        / f"{stamp}-{observation['main_commit'][:12]}-{observation['observation_sha256'][:12]}.json"
    )
    create_only(path, canonical_bytes(observation))
    atomic_write(state_root / "latest-observation.json", canonical_bytes(observation))
    return path


def required_resource_keys(
    *, state_root: Path, prefix: Path, bin_dir: Path, workspace: Path
) -> list[str]:
    return sorted(
        {
            f"path:{bin_dir / 'bureau'}",
            f"path:{bin_dir / 'bureau-runtime-refresh'}",
            f"path:{prefix}",
            f"path:{state_root}",
            f"path:{workspace}",
        }
    )


def prepare_intent(
    *,
    candidate: dict[str, Any],
    state_root: Path,
    prefix: Path,
    bin_dir: Path,
    remote_url: str,
    authorized_by: str,
    authorization: str,
    ttl_seconds: int = DEFAULT_INTENT_TTL_SECONDS,
    now: datetime | None = None,
) -> tuple[dict[str, Any], Path]:
    verify_digest(candidate, "observation_sha256")
    if candidate.get("status") not in {"candidate", "alert"}:
        raise RuntimeRefreshError(
            "candidate-not-deployable", f"candidate status is {candidate.get('status')!r}"
        )
    if not authorized_by.strip() or len(authorization.strip()) < 8:
        raise RuntimeRefreshError("authorization-missing", "explicit authorization is required")
    if ttl_seconds <= 0 or ttl_seconds > 3600:
        raise RuntimeRefreshError(
            "intent-ttl-invalid", "intent TTL must be between 1 and 3600 seconds"
        )
    current = now or utc_now()
    workspace = state_root / "workspaces" / candidate["main_commit"]
    intent: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "bureau_runtime_refresh_intent",
        "repository": candidate["repository"],
        "remote_url": remote_url,
        "main_commit": candidate["main_commit"],
        "pull_request": candidate["pull_request"],
        "merged_at": candidate["merged_at"],
        "required_checks": candidate["required_checks"],
        "target_sha256": candidate["target_sha256"],
        "observation_sha256": candidate["observation_sha256"],
        "expected_deployed_source_commit": candidate["deployed_source_commit"],
        "expected_manifest_sha256": candidate["deployed_manifest_sha256"],
        "state_root": str(state_root),
        "prefix": str(prefix),
        "bin_dir": str(bin_dir),
        "workspace": str(workspace),
        "required_resource_keys": required_resource_keys(
            state_root=state_root, prefix=prefix, bin_dir=bin_dir, workspace=workspace
        ),
        "authorized_by": authorized_by.strip(),
        "authorization": authorization.strip(),
        "created_at": isoformat(current),
        "expires_at": isoformat(current + timedelta(seconds=ttl_seconds)),
        "nonce": uuid.uuid4().hex,
        "does_not_establish": [
            "external_lease_liveness",
            "merge_authority",
            "automatic_retry_authority",
        ],
    }
    intent = bind_digest(intent, "intent_sha256")
    path = state_root / "intents" / f"{intent['intent_sha256']}.json"
    try:
        create_only(path, canonical_bytes(intent))
    except FileExistsError as exc:
        existing = read_json(path)
        if existing != intent:
            raise RuntimeRefreshError(
                "intent-collision", "intent digest path contains other content"
            ) from exc
    return intent, path


def _validate_binding_identity(binding: dict[str, Any]) -> tuple[str, str]:
    owner = binding.get("owner_id")
    task_id = binding.get("task_id")
    if not isinstance(owner, str) or not owner.strip():
        raise RuntimeRefreshError("lease-owner-missing", "Grabowski lease owner is required")
    if not isinstance(task_id, str) or not task_id.strip():
        raise RuntimeRefreshError("lease-task-missing", "Grabowski task id is required")
    return owner.strip(), task_id.strip()


def _validate_resource_database_path(resource_db: Path) -> Path:
    raw = resource_db.expanduser()
    if raw.is_symlink():
        raise RuntimeRefreshError(
            "lease-database-symlink",
            "Grabowski resource database may not be a symlink",
        )
    current = raw.parent
    while current != current.parent:
        if current.is_symlink():
            raise RuntimeRefreshError(
                "lease-database-parent-symlink",
                "Grabowski resource state path may not contain a symlink",
                details={"path": str(current)},
            )
        current = current.parent
    path = raw.resolve(strict=False)
    if not path.is_file():
        raise RuntimeRefreshError(
            "lease-database-invalid", f"Grabowski resource database is invalid: {path}"
        )
    stat_result = path.stat()
    if stat_result.st_uid != os.getuid():
        raise RuntimeRefreshError(
            "lease-database-owner-invalid", "Grabowski resource database owner differs"
        )
    if stat_result.st_mode & 0o077:
        raise RuntimeRefreshError(
            "lease-database-mode-invalid", "Grabowski resource database is not private"
        )
    return path


def validate_live_lease_binding(
    intent: dict[str, Any],
    binding: dict[str, Any],
    *,
    resource_db: Path = DEFAULT_GRABOWSKI_RESOURCE_DB,
    now: datetime | None = None,
    min_remaining_seconds: int = DEFAULT_MIN_LEASE_REMAINING_SECONDS,
    required_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    owner, task_id = _validate_binding_identity(binding)
    if not isinstance(min_remaining_seconds, int) or min_remaining_seconds < 30:
        raise RuntimeRefreshError(
            "lease-minimum-invalid", "minimum lease lifetime must be at least 30 seconds"
        )
    if required_metadata is not None:
        if not isinstance(required_metadata, dict):
            raise RuntimeRefreshError(
                "lease-required-metadata-invalid",
                "required lease metadata must be an object",
            )
        try:
            required_metadata_bytes = json.dumps(
                required_metadata,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise RuntimeRefreshError(
                "lease-required-metadata-invalid",
                "required lease metadata is not canonical JSON",
            ) from exc
        if len(required_metadata_bytes) > 16_384:
            raise RuntimeRefreshError(
                "lease-required-metadata-invalid",
                "required lease metadata is too large",
            )
    else:
        required_metadata_bytes = None
    required = intent.get("required_resource_keys")
    if (
        not isinstance(required, list)
        or not required
        or not all(isinstance(item, str) for item in required)
    ):
        raise RuntimeRefreshError(
            "intent-lease-resources-invalid", "intent resource keys are invalid"
        )
    keys = sorted(set(required))
    if keys != required:
        raise RuntimeRefreshError(
            "intent-lease-resources-invalid", "intent resource keys are not canonical"
        )
    path = _validate_resource_database_path(resource_db)
    current_unix = int((now or utc_now()).timestamp())
    threshold = current_unix + min_remaining_seconds
    uri = f"file:{path}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        schema = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()
        observed_schema = schema["value"] if schema is not None else None
        if observed_schema not in SUPPORTED_GRABOWSKI_RESOURCE_DB_SCHEMAS:
            raise RuntimeRefreshError(
                "lease-database-schema-unsupported",
                "Grabowski resource database schema is unsupported",
                details={
                    "observed": observed_schema,
                    "supported": sorted(SUPPORTED_GRABOWSKI_RESOURCE_DB_SCHEMAS),
                },
            )
        placeholders = ",".join("?" for _ in keys)
        rows = connection.execute(
            f"SELECT resource_key, owner_id, acquired_at_unix, updated_at_unix, "
            f"expires_at_unix, metadata_sha256, metadata_json FROM leases "
            f"WHERE resource_key IN ({placeholders}) ORDER BY resource_key",
            keys,
        ).fetchall()
        connection.commit()
    except RuntimeRefreshError:
        raise
    except sqlite3.Error as exc:
        raise RuntimeRefreshError(
            "lease-database-read-failed",
            "Grabowski resource database could not be read",
            details={"error": str(exc)},
        ) from exc
    finally:
        if "connection" in locals():
            connection.close()
    raw_snapshots = [dict(row) for row in rows]
    observed_keys = [item["resource_key"] for item in raw_snapshots]
    if observed_keys != keys:
        raise RuntimeRefreshError(
            "lease-resources-missing",
            "required live Grabowski leases are missing",
            details={"missing": sorted(set(keys) - set(observed_keys))},
        )
    snapshots: list[dict[str, Any]] = []
    for item in raw_snapshots:
        if item["owner_id"] != owner:
            raise RuntimeRefreshError(
                "lease-owner-mismatch",
                "a required Grabowski lease has another owner",
                details={
                    "resource_key": item["resource_key"],
                    "expected_owner": owner,
                    "observed_owner": item["owner_id"],
                },
            )
        acquired = item["acquired_at_unix"]
        updated = item["updated_at_unix"]
        expires = item["expires_at_unix"]
        if not all(isinstance(value, int) for value in (acquired, updated, expires)):
            raise RuntimeRefreshError(
                "lease-time-invalid", "a required Grabowski lease has invalid times"
            )
        if not acquired <= updated < expires or expires <= threshold:
            raise RuntimeRefreshError(
                "lease-expired",
                "a required Grabowski lease is expired or too short for deployment",
                details={
                    "resource_key": item["resource_key"],
                    "expires_at_unix": expires,
                    "required_after_unix": threshold,
                },
            )
        digest = item["metadata_sha256"]
        metadata_json = item.get("metadata_json")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
            or not isinstance(metadata_json, str)
        ):
            raise RuntimeRefreshError(
                "lease-metadata-invalid",
                "a required Grabowski lease has invalid metadata evidence",
            )
        try:
            metadata = json.loads(metadata_json)
        except json.JSONDecodeError as exc:
            raise RuntimeRefreshError(
                "lease-metadata-invalid",
                "a required Grabowski lease has malformed metadata JSON",
            ) from exc
        if not isinstance(metadata, dict):
            raise RuntimeRefreshError(
                "lease-metadata-invalid",
                "a required Grabowski lease metadata value is not an object",
            )
        canonical_metadata = json.dumps(
            metadata,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        if hashlib.sha256(canonical_metadata).hexdigest() != digest:
            raise RuntimeRefreshError(
                "lease-metadata-digest-mismatch",
                "a required Grabowski lease metadata hash does not match its JSON",
                details={"resource_key": item["resource_key"]},
            )
        if required_metadata is not None:
            mismatched = {
                key: {"expected": value, "observed": metadata.get(key)}
                for key, value in required_metadata.items()
                if metadata.get(key) != value
            }
            if mismatched:
                raise RuntimeRefreshError(
                    "lease-metadata-binding-mismatch",
                    "a required Grabowski lease is not bound to the requested effect",
                    details={
                        "resource_key": item["resource_key"],
                        "mismatched": mismatched,
                    },
                )
        snapshots.append({key: value for key, value in item.items() if key != "metadata_json"})
    normalized = {
        "owner_id": owner,
        "task_id": task_id,
        "resource_db": str(path),
        "resource_db_schema_version": observed_schema,
        "resource_keys": keys,
        "min_expires_at_unix": min(item["expires_at_unix"] for item in snapshots),
        "lease_snapshots": snapshots,
        "observed_at_unix": current_unix,
        "minimum_remaining_seconds": min_remaining_seconds,
        "required_metadata_sha256": (
            hashlib.sha256(required_metadata_bytes).hexdigest()
            if required_metadata_bytes is not None
            else None
        ),
    }
    normalized["lease_binding_sha256"] = sha256_bytes(canonical_bytes(normalized))
    return normalized


def _git(source: Path, *arguments: str) -> str:
    argv = ["git", "-C", str(source), *arguments]
    return _require_command(_run(argv, timeout=120), argv)


def validate_source_checkout(source: Path, expected_commit: str, remote_url: str) -> dict[str, Any]:
    if source.is_symlink() or not source.is_dir():
        raise RuntimeRefreshError("source-invalid", f"source checkout is invalid: {source}")
    head = _git(source, "rev-parse", "HEAD")
    origin_main = _git(source, "rev-parse", "origin/main")
    status = _git(source, "status", "--porcelain=v1", "--untracked-files=normal")
    branch = _git(source, "branch", "--show-current")
    observed_remote = _git(source, "remote", "get-url", "origin")
    if head != expected_commit or origin_main != expected_commit:
        raise RuntimeRefreshError(
            "source-head-drift",
            "source HEAD or origin/main differs from intent",
            details={"head": head, "origin_main": origin_main, "expected": expected_commit},
        )
    if status:
        raise RuntimeRefreshError("source-dirty", "isolated source checkout is dirty")
    if branch:
        raise RuntimeRefreshError("source-not-detached", "isolated source checkout is not detached")
    if observed_remote != remote_url:
        raise RuntimeRefreshError(
            "source-remote-mismatch",
            "isolated source remote differs from intent",
            details={"expected": remote_url, "observed": observed_remote},
        )
    return {
        "root": str(source),
        "head": head,
        "origin_main": origin_main,
        "dirty": False,
        "detached": True,
        "remote_url": observed_remote,
    }


def prepare_source_checkout(
    *, remote_url: str, workspace: Path, expected_commit: str, workspaces_root: Path
) -> dict[str, Any]:
    resolved_root = workspaces_root.expanduser().resolve()
    resolved_workspace = workspace.expanduser().resolve()
    if not resolved_workspace.is_relative_to(resolved_root):
        raise RuntimeRefreshError(
            "workspace-outside-root", "workspace escaped the runtime-refresh root"
        )
    if os.path.lexists(resolved_workspace):
        raise RuntimeRefreshError("workspace-exists", "runtime-refresh workspace already exists")
    resolved_workspace.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    argv = [
        "git",
        "clone",
        "--no-checkout",
        "--single-branch",
        "--branch",
        "main",
        "--origin",
        "origin",
        "--",
        remote_url,
        str(resolved_workspace),
    ]
    _require_command(_run(argv, timeout=300), argv)
    origin_main = _git(resolved_workspace, "rev-parse", "origin/main")
    if origin_main != expected_commit:
        raise RuntimeRefreshError(
            "origin-main-drift",
            "origin/main changed after intent creation",
            details={"expected": expected_commit, "observed": origin_main},
        )
    argv = ["git", "-C", str(resolved_workspace), "checkout", "--detach", expected_commit]
    _require_command(_run(argv, timeout=120), argv)
    return validate_source_checkout(resolved_workspace, expected_commit, remote_url)


def run_installer(
    *, source: Path, prefix: Path, bin_dir: Path, timeout: float = 300
) -> dict[str, Any]:
    argv = [
        sys.executable,
        str(source / "ops/install-bureau-runtime.py"),
        "--source",
        str(source),
        "--prefix",
        str(prefix),
        "--bin-dir",
        str(bin_dir),
        "--replace-existing",
    ]
    result = _run(argv, cwd=source, timeout=timeout)
    if result.returncode:
        raise RuntimeRefreshError(
            "installer-returned-nonzero",
            "Bureau installer returned non-zero after effect start",
            details={
                "returncode": result.returncode,
                "stdout": result.stdout[-4000:],
                "stderr": result.stderr[-4000:],
            },
        )
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise RuntimeRefreshError(
            "installer-receipt-invalid", "Bureau installer did not return a valid receipt"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeRefreshError("installer-receipt-invalid", "installer receipt is not an object")
    return payload


def _extract_result(value: dict[str, Any]) -> dict[str, Any]:
    result = value.get("result")
    return result if isinstance(result, dict) else value


def _extract_runtime_identity(value: dict[str, Any]) -> dict[str, Any]:
    identity = value.get("runtime_identity")
    if not isinstance(identity, dict):
        raise RuntimeRefreshError(
            "readback-runtime-identity-invalid",
            "bureau runtime-identity response has no runtime_identity object",
        )
    return identity


def run_json_command(argv: list[str], *, timeout: float = 120) -> dict[str, Any]:
    output = _require_command(_run(argv, timeout=timeout), argv)
    try:
        value = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeRefreshError("readback-json-invalid", f"invalid JSON from {argv[0]}") from exc
    if not isinstance(value, dict):
        raise RuntimeRefreshError("readback-json-invalid", f"JSON from {argv[0]} is not an object")
    return value


def readback_install(
    *, expected_commit: str, prefix: Path, bin_dir: Path, install_receipt: dict[str, Any]
) -> dict[str, Any]:
    manifest_path = prefix / "deployment-manifest.json"
    manifest, manifest_sha = load_manifest(manifest_path)
    if manifest["source_commit"] != expected_commit:
        raise RuntimeRefreshError("readback-source-mismatch", "deployed source differs from intent")
    if install_receipt.get("manifest_sha256") != manifest_sha:
        raise RuntimeRefreshError(
            "readback-manifest-mismatch", "installer receipt manifest hash mismatch"
        )
    bureau_launcher = bin_dir / "bureau"
    refresh_launcher = bin_dir / "bureau-runtime-refresh"
    for path, receipt_field in (
        (bureau_launcher, "launcher_sha256"),
        (refresh_launcher, "runtime_refresh_launcher_sha256"),
    ):
        if path.is_symlink() or not path.is_file():
            raise RuntimeRefreshError("readback-launcher-invalid", f"launcher is invalid: {path}")
        digest = sha256_bytes(path.read_bytes())
        if install_receipt.get(receipt_field) != digest:
            raise RuntimeRefreshError(
                "readback-launcher-mismatch",
                f"launcher hash mismatch: {path}",
                details={"expected": install_receipt.get(receipt_field), "observed": digest},
            )
    check = _extract_result(run_json_command([str(bureau_launcher), "--json", "check"]))
    identity = _extract_runtime_identity(
        run_json_command([str(bureau_launcher), "--json", "runtime-identity"])
    )
    if check.get("valid") is not True:
        raise RuntimeRefreshError(
            "readback-check-failed", "installed bureau --json check is not valid"
        )
    manifest_identity = (
        identity.get("manifest") if isinstance(identity.get("manifest"), dict) else {}
    )
    registry_identity = (
        manifest_identity.get("canonical_registry")
        if isinstance(manifest_identity.get("canonical_registry"), dict)
        else {}
    )
    if manifest_identity.get("valid") is not True or registry_identity.get("valid") is not True:
        raise RuntimeRefreshError(
            "readback-identity-invalid", "installed runtime identity is invalid"
        )
    if manifest_identity.get("source_commit") != expected_commit:
        raise RuntimeRefreshError(
            "readback-identity-source-mismatch", "runtime identity source differs"
        )
    if manifest_identity.get("observed_package_tree_sha256") != manifest.get("package_tree_sha256"):
        raise RuntimeRefreshError("readback-package-mismatch", "package tree readback mismatch")
    if registry_identity.get("observed_tree_sha256") != manifest.get(
        "canonical_registry_tree_sha256"
    ):
        raise RuntimeRefreshError(
            "readback-snapshot-mismatch", "Registry snapshot readback mismatch"
        )
    return {
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha,
        "source_commit": expected_commit,
        "release_id": manifest.get("release_id"),
        "package_tree_sha256": manifest.get("package_tree_sha256"),
        "registry_snapshot_tree_sha256": manifest.get("canonical_registry_tree_sha256"),
        "bureau_launcher_sha256": sha256_bytes(bureau_launcher.read_bytes()),
        "runtime_refresh_launcher_sha256": sha256_bytes(refresh_launcher.read_bytes()),
        "check_valid": True,
        "runtime_identity_valid": True,
        "rollback": install_receipt.get("rollback"),
    }


def _write_attempt_result(path: Path, value: dict[str, Any]) -> dict[str, Any]:
    result = bind_digest(value, "result_sha256")
    create_only(path, canonical_bytes(result))
    return result


def apply_runtime_refresh(
    *,
    intent_path: Path,
    lease_binding: dict[str, Any],
    manifest_path: Path,
    state_root: Path,
    resource_db: Path = DEFAULT_GRABOWSKI_RESOURCE_DB,
    now: datetime | None = None,
    observer: Callable[..., dict[str, Any]] = observe_runtime_refresh,
    source_preparer: Callable[..., dict[str, Any]] = prepare_source_checkout,
    installer: Callable[..., dict[str, Any]] = run_installer,
    readback: Callable[..., dict[str, Any]] = readback_install,
) -> dict[str, Any]:
    current = now or utc_now()
    intent = read_json(intent_path)
    verify_digest(intent, "intent_sha256")
    if intent.get("kind") != "bureau_runtime_refresh_intent":
        raise RuntimeRefreshError("intent-kind-invalid", "runtime-refresh intent kind is invalid")
    if parse_time(intent["expires_at"]) <= current:
        raise RuntimeRefreshError("intent-expired", "runtime-refresh intent has expired")
    resolved_state_root = state_root.expanduser().resolve()
    if Path(intent["state_root"]).expanduser().resolve() != resolved_state_root:
        raise RuntimeRefreshError("intent-state-root-mismatch", "intent state root differs")
    binding = validate_live_lease_binding(
        intent, lease_binding, resource_db=resource_db, now=current
    )

    attempt_dir = resolved_state_root / "attempts" / intent["target_sha256"]
    started_path = attempt_dir / "started.json"
    result_path = attempt_dir / "result.json"
    if result_path.exists():
        existing = read_json(result_path)
        verify_digest(existing, "result_sha256")
        return {**existing, "reused": True}
    if started_path.exists():
        started = read_json(started_path)
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "bureau_runtime_refresh_result",
            "status": "unclear_existing_attempt",
            "intent_sha256": intent["intent_sha256"],
            "started": started,
            "reused": True,
            "does_not_establish": ["safe_retry", "deployment_outcome"],
        }

    required_checks = tuple(intent["required_checks"])
    live = observer(
        repository=intent["repository"],
        manifest_path=manifest_path,
        required_checks=required_checks,
        now=current,
    )
    verify_digest(live, "observation_sha256")
    if live.get("main_commit") != intent["main_commit"]:
        raise RuntimeRefreshError("main-drift", "GitHub main changed after intent creation")
    if live.get("status") == "already_current":
        started = bind_digest(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": "bureau_runtime_refresh_attempt_start",
                "intent_sha256": intent["intent_sha256"],
                "lease_binding": binding,
                "started_at": isoformat(current),
                "effect_started": False,
            },
            "start_sha256",
        )
        create_only(started_path, canonical_bytes(started))
        return _write_attempt_result(
            result_path,
            {
                "schema_version": SCHEMA_VERSION,
                "kind": "bureau_runtime_refresh_result",
                "status": "already_current",
                "intent_sha256": intent["intent_sha256"],
                "main_commit": intent["main_commit"],
                "finished_at": isoformat(current),
                "effect_started": False,
                "lease_binding": binding,
            },
        )
    if live.get("status") not in {"candidate", "alert"}:
        raise RuntimeRefreshError(
            "live-candidate-blocked",
            "live observation is not deployable",
            details={"status": live.get("status"), "reason_codes": live.get("reason_codes")},
        )
    if live.get("target_sha256") != intent.get("target_sha256"):
        raise RuntimeRefreshError(
            "target-drift",
            "live deployment target differs from explicit intent",
            details={"intent": intent.get("target_sha256"), "live": live.get("target_sha256")},
        )
    current_manifest, current_manifest_sha = load_manifest(manifest_path)
    if (
        current_manifest.get("source_commit") != intent["expected_deployed_source_commit"]
        or current_manifest_sha != intent["expected_manifest_sha256"]
    ):
        raise RuntimeRefreshError(
            "deployed-boundary-drift", "deployed runtime changed after intent"
        )

    started = bind_digest(
        {
            "schema_version": SCHEMA_VERSION,
            "kind": "bureau_runtime_refresh_attempt_start",
            "intent_sha256": intent["intent_sha256"],
            "target_sha256": intent["target_sha256"],
            "main_commit": intent["main_commit"],
            "lease_binding": binding,
            "started_at": isoformat(current),
            "effect_started": False,
        },
        "start_sha256",
    )
    create_only(started_path, canonical_bytes(started))
    effect_started = False
    workspace = Path(intent["workspace"])
    prefix = Path(intent["prefix"])
    bin_dir = Path(intent["bin_dir"])
    try:
        source_identity = source_preparer(
            remote_url=intent["remote_url"],
            workspace=workspace,
            expected_commit=intent["main_commit"],
            workspaces_root=resolved_state_root / "workspaces",
        )
        effect_started = True
        install_receipt = installer(source=workspace, prefix=prefix, bin_dir=bin_dir)
        evidence = readback(
            expected_commit=intent["main_commit"],
            prefix=prefix,
            bin_dir=bin_dir,
            install_receipt=install_receipt,
        )
        result = _write_attempt_result(
            result_path,
            {
                "schema_version": SCHEMA_VERSION,
                "kind": "bureau_runtime_refresh_result",
                "status": "deployed",
                "intent_sha256": intent["intent_sha256"],
                "target_sha256": intent["target_sha256"],
                "main_commit": intent["main_commit"],
                "source_identity": source_identity,
                "install_receipt": install_receipt,
                "readback": evidence,
                "lease_binding": binding,
                "finished_at": isoformat(utc_now()),
                "effect_started": True,
                "does_not_establish": ["future_runtime_health", "future_main_stability"],
            },
        )
        shutil.rmtree(workspace)
        return result
    except subprocess.TimeoutExpired as exc:
        error = {
            "code": "effect-timeout",
            "message": str(exc),
            "details": {"cmd": exc.cmd, "timeout": exc.timeout},
        }
        status = "unclear" if effect_started else "failed"
    except RuntimeRefreshError as exc:
        error = exc.as_dict()
        status = "unclear" if effect_started else "failed"
    except Exception as exc:  # fail closed; effect outcome may be ambiguous
        error = {"code": "unexpected-error", "message": repr(exc), "details": {}}
        status = "unclear" if effect_started else "failed"
    return _write_attempt_result(
        result_path,
        {
            "schema_version": SCHEMA_VERSION,
            "kind": "bureau_runtime_refresh_result",
            "status": status,
            "intent_sha256": intent["intent_sha256"],
            "target_sha256": intent["target_sha256"],
            "main_commit": intent["main_commit"],
            "error": error,
            "lease_binding": binding,
            "finished_at": isoformat(utc_now()),
            "effect_started": effect_started,
            "workspace_preserved": os.path.lexists(workspace),
            "does_not_establish": ["safe_retry", "deployment_outcome"]
            if status == "unclear"
            else ["future_success"],
        },
    )


def status_report(state_root: Path, manifest_path: Path) -> dict[str, Any]:
    manifest, manifest_sha = load_manifest(manifest_path)
    latest_path = state_root / "latest-observation.json"
    latest = read_json(latest_path) if latest_path.exists() else None
    attempts = []
    attempts_root = state_root / "attempts"
    if attempts_root.exists():
        for directory in sorted(attempts_root.iterdir()):
            if not directory.is_dir() or directory.is_symlink():
                continue
            result_path = directory / "result.json"
            started_path = directory / "started.json"
            if result_path.exists():
                value = read_json(result_path)
                attempts.append(
                    {
                        "target_sha256": directory.name,
                        "intent_sha256": value.get("intent_sha256"),
                        "status": value.get("status"),
                        "result_sha256": value.get("result_sha256"),
                    }
                )
            elif started_path.exists():
                attempts.append(
                    {"intent_sha256": directory.name, "status": "unclear_existing_attempt"}
                )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "bureau_runtime_refresh_status",
        "deployed_source_commit": manifest["source_commit"],
        "deployed_manifest_sha256": manifest_sha,
        "latest_observation": latest,
        "attempts": attempts[-20:],
        "does_not_establish": ["future_runtime_health", "deployment_authority"],
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(prog="bureau-runtime-refresh")
    value.add_argument(
        "--state-root",
        default=os.environ.get(
            "BUREAU_RUNTIME_REFRESH_STATE_ROOT",
            "~/.local/state/bureau/runtime-refresh",
        ),
        type=Path,
    )
    value.add_argument(
        "--manifest",
        default=os.environ.get(
            "BUREAU_RUNTIME_MANIFEST",
            "~/.local/share/bureau/deployment-manifest.json",
        ),
        type=Path,
    )
    sub = value.add_subparsers(dest="command", required=True)

    observe = sub.add_parser("observe")
    observe.add_argument("--repository", default=DEFAULT_REPOSITORY)
    observe.add_argument("--required-check", action="append", default=[])
    observe.add_argument("--slo-seconds", type=int, default=DEFAULT_SLO_SECONDS)

    intent = sub.add_parser("prepare-intent")
    intent.add_argument("--candidate", required=True, type=Path)
    intent.add_argument("--prefix", default="~/.local/share/bureau", type=Path)
    intent.add_argument("--bin-dir", default="~/.local/bin", type=Path)
    intent.add_argument("--remote-url", default=DEFAULT_REMOTE_URL)
    intent.add_argument("--authorized-by", required=True)
    intent.add_argument("--authorization", required=True)
    intent.add_argument("--ttl-seconds", type=int, default=DEFAULT_INTENT_TTL_SECONDS)

    apply = sub.add_parser("apply")
    apply.add_argument("--intent", required=True, type=Path)
    apply.add_argument("--lease-owner", required=True)
    apply.add_argument("--lease-task-id", required=True)

    sub.add_parser("status")
    return value


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve()


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    state_root = _resolved(args.state_root)
    manifest_path = _resolved(args.manifest)
    try:
        if args.command == "observe":
            checks = tuple(args.required_check) or DEFAULT_REQUIRED_CHECKS
            observation = observe_runtime_refresh(
                repository=args.repository,
                manifest_path=manifest_path,
                required_checks=checks,
                slo_seconds=args.slo_seconds,
            )
            path = persist_observation(state_root, observation)
            output = {**observation, "observation_path": str(path)}
            print(json.dumps(output, sort_keys=True))
            return 0
        if args.command == "prepare-intent":
            candidate = read_json(_resolved(args.candidate))
            intent, path = prepare_intent(
                candidate=candidate,
                state_root=state_root,
                prefix=_resolved(args.prefix),
                bin_dir=_resolved(args.bin_dir),
                remote_url=args.remote_url,
                authorized_by=args.authorized_by,
                authorization=args.authorization,
                ttl_seconds=args.ttl_seconds,
            )
            print(json.dumps({**intent, "intent_path": str(path)}, sort_keys=True))
            return 0
        if args.command == "apply":
            result = apply_runtime_refresh(
                intent_path=_resolved(args.intent),
                lease_binding={
                    "owner_id": args.lease_owner,
                    "task_id": args.lease_task_id,
                },
                manifest_path=manifest_path,
                state_root=state_root,
            )
            print(json.dumps(result, sort_keys=True))
            return 0 if result.get("status") in {"deployed", "already_current"} else 2
        if args.command == "status":
            print(json.dumps(status_report(state_root, manifest_path), sort_keys=True))
            return 0
    except RuntimeRefreshError as exc:
        print(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "bureau_runtime_refresh_error",
                    "status": "blocked",
                    "error": exc.as_dict(),
                },
                sort_keys=True,
            )
        )
        return 2
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
