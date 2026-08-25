"""Gated, one-shot refresh of the immutable Bureau runtime.

The observer is read-only with respect to Git, Registry truth and the deployed
runtime.  The apply path requires an explicit hash-bound intent plus an externally
supplied owner/task binding verified against live Grabowski leases.  It never retries
an attempt whose durable
start record already exists.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import approval, legacy, registry_snapshot

SCHEMA_VERSION = 1
DEFAULT_REPOSITORY = "heimgewebe/bureau"
DEFAULT_REMOTE_URL = "git@github.com:heimgewebe/bureau.git"
DEFAULT_REQUIRED_CHECKS = ("validate (3.10)", "validate (3.12)")
DEFAULT_AUTHORITY_ADOPTION_REQUIRED_CHECKS = (
    *DEFAULT_REQUIRED_CHECKS,
    "registry-registration-preflight/freshness",
)
DEFAULT_SLO_SECONDS = 5400
DEFAULT_INTENT_TTL_SECONDS = 900
DEFAULT_MIN_LEASE_REMAINING_SECONDS = 600
DEFAULT_MIN_RUNTIME_APPROVAL_REMAINING_SECONDS = DEFAULT_MIN_LEASE_REMAINING_SECONDS
DEFAULT_MIN_LEGACY_CUTOVER_REMAINING_SECONDS = 30
DEFAULT_RUNTIME_REFRESH_STATE_ROOT = Path(
    os.environ.get(
        "BUREAU_RUNTIME_REFRESH_STATE_ROOT",
        "~/.local/state/bureau/runtime-refresh",
    )
).expanduser()
DEFAULT_BUREAU_STATE_ROOT = legacy.default_state_dir()
DEFAULT_BUREAU_STATE_DB = DEFAULT_BUREAU_STATE_ROOT / "bureau.sqlite3"
GRABOWSKI_RESOURCE_LEASE_CONTRACT_METADATA_KEY = "resource_lease_contract_version"
SUPPORTED_GRABOWSKI_RESOURCE_LEASE_CONTRACTS = frozenset({"1"})
DEFAULT_GRABOWSKI_RESOURCE_DB = Path("~/.local/state/grabowski/resources.sqlite3").expanduser()
MAX_JSON_BYTES = 256 * 1024
MAX_RUNTIME_AUTHORITY_HISTORY_INTENTS = 4096
MAX_RUNTIME_BACKUP_ARTIFACT_DIRS = 4096
MAX_HISTORICAL_RUNTIME_ARTIFACT_BYTES = 4 * 1024 * 1024
MAX_GITHUB_FIRST_PARENT_LAG_COMMITS = 100
GITHUB_PREFLIGHT_RETRY_DELAYS_SECONDS = (0.5, 1.5)
RUNTIME_LAUNCHER_MANAGED_MARKER = "# managed-by: heimgewebe-bureau-runtime-v1"
RUNTIME_MANIFEST_PAYLOAD_DIGEST_FIELD = "manifest_payload_sha256"
RUNTIME_LAUNCHER_ENTRYPOINTS = (
    ("bureau", "bureau.cli"),
    ("bureau-runtime-refresh", "bureau.runtime_refresh"),
    ("bureau-status-capsule", "bureau.status_capsule"),
)
RUNTIME_SCHEDULER_NAMES = (
    "bureau-halfhour-operator",
    "bureau-curator",
    "bureau-operator-control",
    "bureau-verifier-control",
    "bureau-closure-planner",
    "bureau-task-supply",
)
REQUIRED_RUNTIME_TIMER = "bureau-task-supply"
DEFAULT_USER_SYSTEMD_UNIT_ROOT = Path("~/.config/systemd/user").expanduser()
DEFAULT_RUNTIME_LIBEXEC_ROOT = Path("~/.local/libexec").expanduser()
SYSTEMD_TIMER_PROPERTIES = (
    "LoadState",
    "UnitFileState",
    "ActiveState",
    "SubState",
    "FragmentPath",
)
SYSTEMD_SERVICE_PROPERTIES = (
    "LoadState",
    "UnitFileState",
    "ActiveState",
    "SubState",
    "FragmentPath",
    "Result",
)
SCHEDULER_RECONVERGEABLE_READBACK_CODES = frozenset(
    {
        "scheduler-readback-fragment-replaceable",
        "scheduler-readback-fragment-mismatch",
        "scheduler-readback-load-state-invalid",
        "scheduler-required-timer-invalid",
        "scheduler-timer-intent-drift",
    }
)
RUNTIME_AUTHORITY_SCHEMA_VERSION = 1
RUNTIME_AUTHORITY_MODE = "single-use-target-bound"
RUNTIME_AUTHORITY_TARGET_BINDING = "candidate.target_sha256"
RUNTIME_AUTHORITY_ALLOWED_STATES = ("ready", "active")
RUNTIME_AUTHORITY_BINDING_KIND = "bureau_runtime_refresh_authority_target_binding"
RUNTIME_AUTHORITY_CONSUMPTION_KIND = "bureau_runtime_refresh_authority_consumption"
RUNTIME_AUTHORITY_CLOSEOUT_KIND = "bureau_runtime_refresh_no_run_closeout"


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


def stable_launcher_bytes(
    manifest_path: Path,
    entrypoint: str = "bureau.cli",
) -> bytes:
    """Render the deployment-independent Bureau bootstrap launcher.

    The launcher validates the manifest canonical bytes and payload digest
    before it consumes any runtime paths, then retains the existing
    module/package-tree verification.  The raw manifest digest is
    still exported to the runtime identity layer, but it is no longer embedded
    in launcher bytes and therefore does not force a launcher rewrite for every
    ordinary deployment.
    """
    return f"""#!/usr/bin/env python3
{RUNTIME_LAUNCHER_MANAGED_MARKER}
import hashlib
import importlib
import json
import os
import sys
from pathlib import Path

manifest_path = Path({str(manifest_path)!r})
manifest_digest_field = {RUNTIME_MANIFEST_PAYLOAD_DIGEST_FIELD!r}
if manifest_path.is_symlink() or not manifest_path.is_file():
    raise SystemExit("bureau runtime manifest is not a regular file")
manifest_bytes = manifest_path.read_bytes()
observed_manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
try:
    manifest = json.loads(manifest_bytes)
    if manifest["schema_version"] != 1 or manifest["kind"] != "bureau_runtime_deployment":
        raise ValueError("unsupported manifest contract")
    canonical_manifest_bytes = (
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\\n"
    ).encode()
    if manifest_bytes != canonical_manifest_bytes:
        raise ValueError("manifest digest mismatch: manifest bytes are not canonical")
    manifest_payload = dict(manifest)
    expected_manifest_payload_sha256 = manifest_payload.pop(manifest_digest_field)
    if (
        not isinstance(expected_manifest_payload_sha256, str)
        or len(expected_manifest_payload_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in expected_manifest_payload_sha256
        )
    ):
        raise ValueError("manifest payload digest is invalid")
    rendered_payload = (
        json.dumps(
            manifest_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\\n"
    ).encode()
    if hashlib.sha256(rendered_payload).hexdigest() != expected_manifest_payload_sha256:
        raise ValueError("manifest payload digest mismatch")
    release = Path(manifest["immutable_release_path"]).resolve()
    module = Path(manifest["module_path"]).resolve()
    expected_module_sha256 = manifest["module_sha256"]
    expected_tree_sha256 = manifest["package_tree_sha256"]
    canonical_registry_root = Path(manifest["canonical_registry_root"]).resolve()
    canonical_registry_inventory = Path(manifest["canonical_registry_inventory_path"]).resolve()
except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
    raise SystemExit(f"bureau runtime manifest invalid: {{exc}}")
if module.is_symlink() or not module.is_file():
    raise SystemExit("bureau runtime module is not a regular file")
if hashlib.sha256(module.read_bytes()).hexdigest() != expected_module_sha256:
    raise SystemExit("bureau runtime module digest mismatch")
try:
    module.relative_to(release)
except ValueError:
    raise SystemExit("bureau runtime module escaped immutable release")
pyproject = release / "pyproject.toml"
packages = [release / "src/bureau", release / "src/bureau_cycle"]
schemas = release / "schemas"
systemd = release / "ops/systemd"
schema_paths = sorted(schemas.glob("*.json")) if schemas.is_dir() else []
if (
    pyproject.is_symlink()
    or not pyproject.is_file()
    or schemas.is_symlink()
    or not schemas.is_dir()
    or not schema_paths
    or systemd.is_symlink()
    or not systemd.is_dir()
    or any(package.is_symlink() or not package.is_dir() for package in packages)
):
    raise SystemExit("bureau runtime package tree is incomplete")
digest = hashlib.sha256()
scheduler_names = {RUNTIME_SCHEDULER_NAMES!r}
paths = [
    pyproject,
    *schema_paths,
    *(path for package in packages for path in sorted(package.rglob("*.py"))),
    *(systemd / (name + ".service") for name in scheduler_names),
    *(systemd / (name + ".timer") for name in scheduler_names),
    *(systemd / "libexec" / name for name in scheduler_names),
]
for path in paths:
    if path.is_symlink() or not path.is_file():
        raise SystemExit("bureau runtime package tree contains a non-regular file")
    relative = path.relative_to(release).as_posix().encode()
    content = path.read_bytes()
    digest.update(len(relative).to_bytes(4, "big"))
    digest.update(relative)
    digest.update(b"x" if path.stat().st_mode & 0o111 else b"-")
    digest.update(len(content).to_bytes(8, "big"))
    digest.update(content)
if digest.hexdigest() != expected_tree_sha256:
    raise SystemExit("bureau runtime package tree digest mismatch")
if not canonical_registry_root.is_dir() or canonical_registry_root.is_symlink():
    raise SystemExit("bureau canonical Registry snapshot is invalid")
if not canonical_registry_inventory.is_file() or canonical_registry_inventory.is_symlink():
    raise SystemExit("bureau canonical Registry inventory is invalid")
sys.path.insert(0, str(release / "src"))
os.environ["BUREAU_RUNTIME_MANIFEST"] = str(manifest_path)
os.environ["BUREAU_RUNTIME_MANIFEST_SHA256"] = observed_manifest_sha256
os.environ["BUREAU_REGISTRY_ROOT"] = str(canonical_registry_root)
os.environ["BUREAU_REGISTRY_ROOT_MODE"] = "canonical-runtime-default"
os.environ.setdefault("BUREAU_JSON_ENVELOPE", "1")
module = importlib.import_module({entrypoint!r})
raise SystemExit(module.main())
""".encode()


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


def atomic_write(
    path: Path,
    data: bytes,
    mode: int = 0o600,
    *,
    staging_path: Path | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if staging_path is None:
        temporary = path.parent / f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    else:
        temporary = staging_path
        if temporary == path or temporary.parent != path.parent:
            raise RuntimeRefreshError(
                "atomic-staging-path-invalid",
                "atomic staging path must be a distinct sibling of the target",
                details={"path": str(path), "staging_path": str(temporary)},
            )
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
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


def _github_arguments_are_read_only(arguments: list[str]) -> bool:
    if len(arguments) >= 2 and arguments[:2] == ["pr", "view"]:
        return True
    if not arguments or arguments[0] != "api":
        return False

    explicit_method_seen = False
    index = 1
    while index < len(arguments):
        item = arguments[index]
        method: str | None = None
        if item in {"-X", "--method"}:
            if index + 1 >= len(arguments):
                return False
            method = arguments[index + 1].upper()
            index += 2
        elif item.startswith("--method="):
            method = item.split("=", 1)[1].upper()
            index += 1
        elif item.startswith("-X") and item != "-X":
            method = item[2:].upper()
            index += 1
        else:
            if (
                item in {"-f", "-F", "--field", "--raw-field", "--input"}
                or item.startswith(("-f", "-F", "--field=", "--raw-field=", "--input="))
            ):
                return False
            index += 1
            continue
        if explicit_method_seen or method != "GET":
            return False
        explicit_method_seen = True
    return True


def _github_http_503(error: RuntimeRefreshError) -> bool:
    if error.code != "command-failed":
        return False
    text = f"{error.details.get('stdout', '')}\n{error.details.get('stderr', '')}"
    return any(
        marker in text
        for marker in (
            "HTTP 503",
            "503 Service Unavailable",
            '"status": "503"',
            '"status":"503"',
        )
    )


def gh_preflight_json(
    arguments: list[str],
    *,
    timeout: float = 60,
    sleeper: Callable[[float], None] = time.sleep,
) -> Any:
    """Read GitHub with bounded 503 retry only for positively read-only command shapes."""
    if not _github_arguments_are_read_only(arguments):
        return gh_json(arguments, timeout=timeout)

    for attempt in range(len(GITHUB_PREFLIGHT_RETRY_DELAYS_SECONDS) + 1):
        try:
            return gh_json(arguments, timeout=timeout)
        except RuntimeRefreshError as exc:
            if (
                not _github_http_503(exc)
                or attempt >= len(GITHUB_PREFLIGHT_RETRY_DELAYS_SECONDS)
            ):
                raise
            sleeper(GITHUB_PREFLIGHT_RETRY_DELAYS_SECONDS[attempt])
    raise AssertionError("bounded GitHub preflight retry loop exhausted unexpectedly")


def _github_compare_not_found(error: RuntimeRefreshError) -> bool:
    if error.code != "command-failed":
        return False
    argv = error.details.get("argv")
    if not isinstance(argv, list) or not any(
        isinstance(item, str) and "/compare/" in item for item in argv
    ):
        return False
    text = f"{error.details.get('stdout', '')}\n{error.details.get('stderr', '')}"
    return "HTTP 404" in text or ("Not Found" in text and '"status":"404"' in text)


def _first_parent_lag_commits(
    *,
    repository: str,
    deployed: str,
    main_commit: str,
    github: Callable[[list[str]], Any],
) -> int | None:
    current = main_commit
    seen: set[str] = set()
    for lag in range(MAX_GITHUB_FIRST_PARENT_LAG_COMMITS + 1):
        if current == deployed:
            return lag
        if lag == MAX_GITHUB_FIRST_PARENT_LAG_COMMITS or current in seen:
            return None
        seen.add(current)
        detail = github(["api", f"repos/{repository}/commits/{current}"])
        if not isinstance(detail, dict) or detail.get("sha") != current:
            return None
        parents = detail.get("parents")
        if not isinstance(parents, list) or not parents or not isinstance(parents[0], dict):
            return None
        parent = parents[0].get("sha")
        if not isinstance(parent, str) or len(parent) != 40:
            return None
        current = parent
    return None


def load_manifest(path: Path) -> tuple[dict[str, Any], str]:
    value = read_json(path)
    if value.get("kind") != "bureau_runtime_deployment":
        raise RuntimeRefreshError(
            "manifest-kind-invalid", "Bureau deployment manifest kind is invalid"
        )
    source_commit = value.get("source_commit")
    if not isinstance(source_commit, str) or len(source_commit) != 40:
        raise RuntimeRefreshError("manifest-source-invalid", "manifest source_commit is invalid")
    manifest_payload_sha256 = value.get(RUNTIME_MANIFEST_PAYLOAD_DIGEST_FIELD)
    if manifest_payload_sha256 is not None:
        expected = payload_digest(value, RUNTIME_MANIFEST_PAYLOAD_DIGEST_FIELD)
        if (
            not isinstance(manifest_payload_sha256, str)
            or len(manifest_payload_sha256) != 64
            or any(character not in "0123456789abcdef" for character in manifest_payload_sha256)
            or manifest_payload_sha256 != expected
        ):
            raise RuntimeRefreshError(
                "manifest-payload-digest-mismatch",
                "Bureau deployment manifest payload digest is invalid",
                details={"expected": expected, "observed": manifest_payload_sha256},
            )
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
        "scheduler_target_state": observation.get("scheduler_target_state"),
    }


def observe_runtime_refresh(
    *,
    repository: str,
    manifest_path: Path,
    required_checks: tuple[str, ...] = DEFAULT_REQUIRED_CHECKS,
    slo_seconds: int = DEFAULT_SLO_SECONDS,
    now: datetime | None = None,
    github: Callable[[list[str]], Any] = gh_preflight_json,
    scheduler_reader: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    observed_at = now or utc_now()
    reasons: list[str] = []
    manifest, manifest_sha = load_manifest(manifest_path)
    deployed = manifest["source_commit"]
    registry_identity = registry_snapshot.canonical_registry_identity(manifest)
    if registry_identity.get("available") is True:
        registry_reasons = [
            str(item)
            for item in registry_identity.get("reasons", [])
            if isinstance(item, str) and item
        ]
        registry_source_commit = registry_identity.get("source_commit")
        identity_proven = (
            registry_identity.get("valid") is True and registry_source_commit == deployed
        )
        runtime_source_identity = {
            "schema_version": 1,
            "status": "proven" if identity_proven else "invalid",
            "deployed_source_commit": deployed,
            "registry_source_commit": registry_source_commit,
            "registry_reasons": registry_reasons,
        }
    else:
        runtime_source_identity = {
            "schema_version": 1,
            "status": "unavailable",
            "deployed_source_commit": deployed,
            "registry_source_commit": None,
            "registry_reasons": ["canonical-registry-not-configured"],
        }

    first_main = github(["api", f"repos/{repository}/commits/main"])
    if not isinstance(first_main, dict) or not isinstance(first_main.get("sha"), str):
        raise RuntimeRefreshError("github-main-invalid", "GitHub main commit response is invalid")
    main_commit = first_main["sha"]
    pull_request: dict[str, Any] | None = None
    merged_at: str | None = None
    check_summary: dict[str, dict[str, Any]] = {}
    lag_commits: int | None = 0 if deployed == main_commit else None
    source_ancestry: dict[str, Any] = {
        "schema_version": 1,
        "status": "proven" if deployed == main_commit else "unproven",
        "method": "same-commit" if deployed == main_commit else "pending",
        "deployed_source_commit": deployed,
        "main_commit": main_commit,
        "compare_status": "identical" if deployed == main_commit else None,
        "ahead_by": 0 if deployed == main_commit else None,
        "behind_by": 0 if deployed == main_commit else None,
        "merge_base_commit": deployed if deployed == main_commit else None,
    }

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

        try:
            compare = github(["api", f"repos/{repository}/compare/{deployed}...{main_commit}"])
        except RuntimeRefreshError as exc:
            if not _github_compare_not_found(exc):
                raise
            lag_commits = _first_parent_lag_commits(
                repository=repository,
                deployed=deployed,
                main_commit=main_commit,
                github=github,
            )
            if lag_commits is None:
                reasons.append("commit-lag-unavailable")
                source_ancestry = {
                    **source_ancestry,
                    "status": "unproven",
                    "method": "first-parent-walk",
                }
            else:
                source_ancestry = {
                    **source_ancestry,
                    "status": "proven",
                    "method": "first-parent-walk",
                    "ahead_by": lag_commits,
                    "behind_by": 0,
                    "merge_base_commit": deployed,
                }
        else:
            compare_status = compare.get("status") if isinstance(compare, dict) else None
            ahead_by = compare.get("ahead_by") if isinstance(compare, dict) else None
            behind_by = compare.get("behind_by") if isinstance(compare, dict) else None
            merge_base = compare.get("merge_base_commit") if isinstance(compare, dict) else None
            merge_base_commit = merge_base.get("sha") if isinstance(merge_base, dict) else None
            compare_shape_valid = (
                isinstance(ahead_by, int)
                and not isinstance(ahead_by, bool)
                and isinstance(behind_by, int)
                and not isinstance(behind_by, bool)
                and isinstance(compare_status, str)
                and isinstance(merge_base_commit, str)
            )
            if (
                compare_shape_valid
                and compare_status == "ahead"
                and ahead_by > 0
                and behind_by == 0
                and merge_base_commit == deployed
            ):
                lag_commits = ahead_by
                source_ancestry = {
                    **source_ancestry,
                    "status": "proven",
                    "method": "github-compare",
                    "compare_status": compare_status,
                    "ahead_by": ahead_by,
                    "behind_by": behind_by,
                    "merge_base_commit": merge_base_commit,
                }
            elif compare_shape_valid:
                lag_commits = None
                reasons.append("deployed-source-not-main-ancestor")
                source_ancestry = {
                    **source_ancestry,
                    "status": "rejected",
                    "method": "github-compare",
                    "compare_status": compare_status,
                    "ahead_by": ahead_by,
                    "behind_by": behind_by,
                    "merge_base_commit": merge_base_commit,
                }
            else:
                lag_commits = None
                reasons.append("commit-lag-unavailable")
                source_ancestry = {
                    **source_ancestry,
                    "status": "unproven",
                    "method": "github-compare",
                    "compare_status": compare_status,
                    "ahead_by": ahead_by,
                    "behind_by": behind_by,
                    "merge_base_commit": merge_base_commit,
                }

        second_main = github(["api", f"repos/{repository}/commits/main"])
        if not isinstance(second_main, dict) or second_main.get("sha") != main_commit:
            reasons.append("main-changed-during-observation")

    age_seconds: int | None = None
    if merged_at:
        age_seconds = max(0, int((observed_at - parse_time(merged_at)).total_seconds()))

    scheduler_actionable = False
    scheduler_blocked = False
    scheduler_readback_evidence: dict[str, Any] | None = None
    scheduler_target_state = "source-not-current"
    if deployed == main_commit:
        scheduler_target_state = "converged"
        if "scheduler" not in manifest:
            reasons.append("scheduler-receipt-missing")
            scheduler_target_state = "missing-receipt"
            scheduler_actionable = True
        else:
            scheduler_receipt = manifest.get("scheduler")
            if not isinstance(scheduler_receipt, dict):
                reasons.append("scheduler-receipt-invalid")
                scheduler_target_state = "blocked:scheduler-receipt-invalid"
                scheduler_blocked = True
            else:
                reader = scheduler_reader or readback_user_scheduler
                try:
                    scheduler_readback_evidence = reader(
                        scheduler_receipt, expected_commit=main_commit
                    )
                except subprocess.TimeoutExpired:
                    reason = "scheduler-readback-timeout"
                    reasons.append(reason)
                    scheduler_target_state = f"blocked:{reason}"
                    scheduler_blocked = True
                except OSError:
                    reason = "scheduler-readback-io-failed"
                    reasons.append(reason)
                    scheduler_target_state = f"blocked:{reason}"
                    scheduler_blocked = True
                except RuntimeRefreshError as exc:
                    reasons.append(exc.code)
                    if exc.code in SCHEDULER_RECONVERGEABLE_READBACK_CODES:
                        scheduler_target_state = f"drift:{exc.code}"
                        scheduler_actionable = True
                    else:
                        scheduler_target_state = f"blocked:{exc.code}"
                        scheduler_blocked = True

    if deployed == main_commit:
        if scheduler_blocked:
            status = "blocked"
        elif scheduler_actionable:
            status = "alert"
        else:
            status = "already_current"
    elif reasons:
        status = "blocked"
    elif age_seconds is not None and age_seconds > slo_seconds:
        status = "alert"
    else:
        status = "candidate"

    if status in {"candidate", "alert"}:
        recovery_action = {
            "action": "prepare-intent",
            "eligible": True,
            "requires_authorization": True,
        }
    elif status == "blocked":
        recovery_action = {
            "action": "resolve-reason-codes",
            "eligible": False,
            "reason_codes": list(reasons),
            "requires_authorization": False,
        }
    else:
        recovery_action = {
            "action": "none",
            "eligible": False,
            "requires_authorization": False,
        }

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
        "source_ancestry": source_ancestry,
        "runtime_source_identity": runtime_source_identity,
        "lag_commits": lag_commits,
        "scheduler_target_state": scheduler_target_state,
        "age_seconds": age_seconds,
        "slo_seconds": slo_seconds,
        "status": status,
        "reason_codes": reasons,
        "recovery_action": recovery_action,
        "observed_at": isoformat(observed_at),
        "does_not_establish": [
            "deployment_authority",
            "external_lease_liveness",
            "future_main_stability",
            "runtime_semantic_correctness",
        ],
    }
    if scheduler_readback_evidence is not None:
        observation["scheduler"] = scheduler_readback_evidence
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


def launcher_mutation_resource_keys(*, prefix: Path, bin_dir: Path) -> list[str]:
    manifest_path = prefix / "deployment-manifest.json"
    keys: set[str] = set()
    for name, entrypoint in RUNTIME_LAUNCHER_ENTRYPOINTS:
        path = bin_dir / name
        expected = stable_launcher_bytes(manifest_path, entrypoint)
        try:
            valid = (
                not path.is_symlink()
                and path.is_file()
                and bool(path.stat().st_mode & 0o111)
                and path.read_bytes() == expected
            )
        except OSError:
            valid = False
        if not valid:
            keys.add(f"path:{path}")
    return sorted(keys)


def default_runtime_user_unit_dir() -> Path:
    """Return the user manager's environment-selected runtime unit directory."""
    runtime_root = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_root is None:
        base = Path("/run/user") / str(os.getuid())
    else:
        base = Path(runtime_root)
        if not runtime_root or not base.is_absolute():
            raise RuntimeRefreshError(
                "runtime-dir-invalid",
                "XDG_RUNTIME_DIR must be a non-empty absolute path",
                details={"xdg_runtime_dir": runtime_root},
            )
    return (base / "systemd/user").resolve()


def validate_runtime_user_unit_dir(expected: Path) -> Path:
    """Fail closed when systemctl would use a runtime unit tree other than the intent."""
    expected_path = expected.expanduser().resolve()
    observed_path = default_runtime_user_unit_dir()
    if observed_path != expected_path:
        raise RuntimeRefreshError(
            "runtime-user-unit-dir-drift",
            "current user runtime systemd path differs from the runtime-refresh intent",
            details={"expected": str(expected_path), "observed": str(observed_path)},
        )
    return observed_path


def _scheduler_staging_path(path: Path) -> Path:
    """Return the deterministic sibling staging path covered by scheduler leases."""
    return path.parent / f".{path.name}.bureau-runtime-refresh-stage"


def scheduler_resource_keys(
    *,
    user_unit_dir: Path,
    libexec_dir: Path,
    runtime_user_unit_dir: Path | None = None,
) -> list[str]:
    """Return the exact filesystem and systemd-user authority needed by convergence."""
    staged_paths = {
        *(
            user_unit_dir / f"{name}.{kind}"
            for name in RUNTIME_SCHEDULER_NAMES
            for kind in ("service", "timer")
        ),
        *(libexec_dir / name for name in RUNTIME_SCHEDULER_NAMES),
    }
    leased_paths = set(staged_paths)
    if runtime_user_unit_dir is not None:
        leased_paths.update(
            unit_root / "timers.target.wants" / f"{name}.timer"
            for unit_root in (user_unit_dir, runtime_user_unit_dir)
            for name in RUNTIME_SCHEDULER_NAMES
        )
        staged_paths.add(
            user_unit_dir
            / "timers.target.wants"
            / f"{REQUIRED_RUNTIME_TIMER}.timer"
        )
    keys = {
        *(f"path:{path}" for path in leased_paths),
        *(f"path:{_scheduler_staging_path(path)}" for path in staged_paths),
        *(f"service:{name}.service" for name in RUNTIME_SCHEDULER_NAMES),
        *(f"service:{name}.timer" for name in RUNTIME_SCHEDULER_NAMES),
    }
    return sorted(keys)


def forbidden_scheduler_parent_resource_keys(
    *, user_unit_dir: Path, libexec_dir: Path, runtime_user_unit_dir: Path
) -> set[str]:
    """Return broad scheduler paths that must never substitute for exact leases."""
    return {
        f"path:{user_unit_dir}",
        f"path:{libexec_dir}",
        f"path:{runtime_user_unit_dir}",
        f"path:{user_unit_dir / 'timers.target.wants'}",
        f"path:{runtime_user_unit_dir / 'timers.target.wants'}",
    }


def validate_scheduler_runtime_layout(*, prefix: Path, bin_dir: Path) -> None:
    """Require the canonical runtime paths referenced by shipped scheduler artifacts."""
    expected_prefix = Path("~/.local/share/bureau").expanduser().resolve()
    expected_bin_dir = Path("~/.local/bin").expanduser().resolve()
    observed_prefix = prefix.expanduser().resolve()
    observed_bin_dir = bin_dir.expanduser().resolve()
    mismatches = [
        label
        for label, observed, expected in (
            ("prefix", observed_prefix, expected_prefix),
            ("bin_dir", observed_bin_dir, expected_bin_dir),
        )
        if observed != expected
    ]
    if mismatches:
        raise RuntimeRefreshError(
            "scheduler-runtime-layout-noncanonical",
            "scheduler convergence requires the current user's canonical runtime layout",
            details={
                "mismatches": mismatches,
                "prefix": str(observed_prefix),
                "expected_prefix": str(expected_prefix),
                "bin_dir": str(observed_bin_dir),
                "expected_bin_dir": str(expected_bin_dir),
            },
        )


def required_resource_keys(
    *,
    state_root: Path,
    prefix: Path,
    bin_dir: Path,
    workspace: Path,
    user_unit_dir: Path = DEFAULT_USER_SYSTEMD_UNIT_ROOT,
    libexec_dir: Path = DEFAULT_RUNTIME_LIBEXEC_ROOT,
    runtime_user_unit_dir: Path | None = None,
) -> list[str]:
    resolved_runtime_user_unit_dir = (
        default_runtime_user_unit_dir()
        if runtime_user_unit_dir is None
        else runtime_user_unit_dir.expanduser().resolve()
    )
    return sorted(
        {
            *launcher_mutation_resource_keys(prefix=prefix, bin_dir=bin_dir),
            *scheduler_resource_keys(
                user_unit_dir=user_unit_dir,
                libexec_dir=libexec_dir,
                runtime_user_unit_dir=resolved_runtime_user_unit_dir,
            ),
            f"path:{prefix}",
            f"path:{state_root}",
            f"path:{workspace}",
        }
    )


def _scheduler_artifacts(
    *, release: Path, user_unit_dir: Path, libexec_dir: Path
) -> list[dict[str, Any]]:
    release_root = release.expanduser().resolve()
    systemd_root = release_root / "ops/systemd"
    artifacts: list[dict[str, Any]] = []
    for name in RUNTIME_SCHEDULER_NAMES:
        for kind, source, live, mode in (
            (
                "service",
                systemd_root / f"{name}.service",
                user_unit_dir / f"{name}.service",
                0o644,
            ),
            (
                "timer",
                systemd_root / f"{name}.timer",
                user_unit_dir / f"{name}.timer",
                0o644,
            ),
            (
                "libexec",
                systemd_root / "libexec" / name,
                libexec_dir / name,
                0o755,
            ),
        ):
            if source.is_symlink() or not source.is_file():
                raise RuntimeRefreshError(
                    "scheduler-release-artifact-invalid",
                    "immutable scheduler artifact is not a regular file",
                    details={"path": str(source), "name": name, "kind": kind},
                )
            try:
                source.resolve(strict=True).relative_to(release_root)
            except (OSError, ValueError) as exc:
                raise RuntimeRefreshError(
                    "scheduler-release-artifact-invalid",
                    "immutable scheduler artifact escaped its release",
                    details={"path": str(source), "release": str(release_root)},
                ) from exc
            artifacts.append(
                {
                    "name": name,
                    "kind": kind,
                    "source_path": str(source),
                    "source_sha256": sha256_bytes(source.read_bytes()),
                    "live_path": str(live),
                    "mode": f"{mode:04o}",
                }
            )
    return artifacts


def _validate_scheduler_parent(
    path: Path, *, label: str, allow_absent: bool = False
) -> None:
    if not path.is_absolute():
        raise RuntimeRefreshError(
            "scheduler-parent-invalid",
            "scheduler parent must be absolute",
            details={"label": label, "path": str(path)},
        )
    try:
        observed = path.lstat()
    except FileNotFoundError:
        try:
            resolved = path.resolve(strict=False)
        except OSError as exc:
            raise RuntimeRefreshError(
                "scheduler-parent-invalid",
                "scheduler parent is unavailable",
                details={"label": label, "path": str(path)},
            ) from exc
        if resolved != path:
            raise RuntimeRefreshError(
                "scheduler-parent-invalid",
                "scheduler parent contains a symlink or non-canonical component",
                details={"label": label, "path": str(path), "resolved": str(resolved)},
            ) from None
        if allow_absent:
            return
        raise RuntimeRefreshError(
            "scheduler-parent-invalid",
            "scheduler parent must already exist",
            details={"label": label, "path": str(path)},
        ) from None
    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
        raise RuntimeRefreshError(
            "scheduler-parent-invalid",
            "scheduler parent must be a non-symlink directory",
            details={"label": label, "path": str(path)},
        )
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise RuntimeRefreshError(
            "scheduler-parent-invalid",
            "scheduler parent is unavailable",
            details={"label": label, "path": str(path)},
        ) from exc
    if resolved != path:
        raise RuntimeRefreshError(
            "scheduler-parent-invalid",
            "scheduler parent contains a symlink or non-canonical component",
            details={"label": label, "path": str(path), "resolved": str(resolved)},
        )


def _live_artifact_matches(item: dict[str, Any]) -> bool:
    path = Path(item["live_path"])
    try:
        return (
            not path.is_symlink()
            and path.is_file()
            and sha256_bytes(path.read_bytes()) == item["source_sha256"]
            and stat.S_IMODE(path.stat().st_mode) == int(item["mode"], 8)
        )
    except OSError:
        return False


def _parse_systemctl_show(
    output: str, *, unit: str, properties: tuple[str, ...]
) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in properties or key in values:
            raise RuntimeRefreshError(
                "scheduler-systemd-readback-invalid",
                "systemctl show returned ambiguous properties",
                details={"unit": unit, "line": line},
            )
        values[key] = value
    missing = sorted(set(properties) - values.keys())
    if missing:
        raise RuntimeRefreshError(
            "scheduler-systemd-readback-invalid",
            "systemctl show omitted required properties",
            details={"unit": unit, "missing": missing},
        )
    return values


def _scheduler_systemctl(
    arguments: list[str],
    *,
    command_runner: Callable[[list[str]], subprocess.CompletedProcess[str]],
) -> str:
    argv = ["systemctl", "--user", *arguments]
    result = command_runner(argv)
    if result.returncode:
        raise RuntimeRefreshError(
            "scheduler-systemctl-failed",
            "user systemd mutation or readback failed",
            details={
                "argv": argv,
                "returncode": result.returncode,
                "stdout": result.stdout[-4000:],
                "stderr": result.stderr[-4000:],
            },
        )
    return result.stdout.strip()


def _scheduler_unit_state(
    unit: str,
    *,
    kind: str,
    command_runner: Callable[[list[str]], subprocess.CompletedProcess[str]],
) -> dict[str, str]:
    properties = (
        SYSTEMD_TIMER_PROPERTIES if kind == "timer" else SYSTEMD_SERVICE_PROPERTIES
    )
    output = _scheduler_systemctl(
        [
            "show",
            unit,
            "--no-pager",
            "--property=" + ",".join(properties),
        ],
        command_runner=command_runner,
    )
    return _parse_systemctl_show(output, unit=unit, properties=properties)


def _timer_intent(state: dict[str, str], *, unit: str) -> str:
    unit_file_state = state["UnitFileState"]
    if state["LoadState"] == "not-found" and unit_file_state in {"", "disabled"}:
        return "absent"
    if unit_file_state in {"enabled", "enabled-runtime"}:
        return unit_file_state
    if unit_file_state == "disabled":
        return "disabled"
    raise RuntimeRefreshError(
        "scheduler-timer-intent-ambiguous",
        "managed timer has no unambiguous enabled or disabled intent",
        details={"unit": unit, "state": state},
    )


def _snapshot_live_artifact(item: dict[str, Any], backup_files: Path) -> dict[str, Any]:
    path = Path(item["live_path"])
    record: dict[str, Any] = {
        "name": item["name"],
        "kind": item["kind"],
        "path": str(path),
    }
    if path.is_symlink():
        record.update({"preimage_kind": "symlink", "target": os.readlink(path)})
        return record
    if not os.path.lexists(path):
        record["preimage_kind"] = "absent"
        return record
    if not path.is_file():
        raise RuntimeRefreshError(
            "scheduler-live-artifact-invalid",
            "live scheduler target is neither a regular file, symlink nor absent",
            details={"path": str(path)},
        )
    content = path.read_bytes()
    relative = f"{item['name']}.{item['kind']}"
    backup_path = backup_files / relative
    mode = stat.S_IMODE(path.stat().st_mode)
    atomic_write(backup_path, content, mode)
    record.update(
        {
            "preimage_kind": "file",
            "mode": f"{mode:04o}",
            "sha256": sha256_bytes(content),
            "backup_path": str(backup_path),
        }
    )
    return record


def _replace_with_symlink(path: Path, target: str) -> None:
    _validate_scheduler_parent(path.parent, label="scheduler symlink parent")
    temporary = _scheduler_staging_path(path)
    temporary_created = False
    try:
        temporary.symlink_to(target)
        temporary_created = True
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if temporary_created and os.path.lexists(temporary):
            temporary.unlink()


def _restore_artifact(record: dict[str, Any]) -> None:
    path = Path(record["path"])
    kind = record["preimage_kind"]
    if kind == "absent":
        if path.is_dir() and not path.is_symlink():
            raise RuntimeRefreshError(
                "scheduler-rollback-target-invalid",
                "scheduler rollback target became a directory",
                details={"path": str(path)},
            )
        if os.path.lexists(path):
            path.unlink()
            _fsync_directory(path.parent)
        return
    if kind == "symlink":
        _replace_with_symlink(path, record["target"])
        return
    if kind != "file":
        raise RuntimeRefreshError(
            "scheduler-rollback-preimage-invalid",
            "scheduler rollback preimage kind is invalid",
            details={"record": record},
        )
    backup_path = Path(record["backup_path"])
    if backup_path.is_symlink() or not backup_path.is_file():
        raise RuntimeRefreshError(
            "scheduler-rollback-backup-invalid",
            "scheduler rollback backup is unavailable",
            details={"path": str(backup_path)},
        )
    content = backup_path.read_bytes()
    if sha256_bytes(content) != record["sha256"]:
        raise RuntimeRefreshError(
            "scheduler-rollback-backup-invalid",
            "scheduler rollback backup digest differs",
            details={"path": str(backup_path)},
        )
    atomic_write(
        path,
        content,
        int(record["mode"], 8),
        staging_path=_scheduler_staging_path(path),
    )


def _run_scheduler_command(
    arguments: list[str],
    *,
    command_runner: Callable[[list[str]], subprocess.CompletedProcess[str]],
) -> None:
    _scheduler_systemctl(arguments, command_runner=command_runner)


def _timer_states(
    *, command_runner: Callable[[list[str]], subprocess.CompletedProcess[str]]
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    timers = {
        name: _scheduler_unit_state(
            f"{name}.timer", kind="timer", command_runner=command_runner
        )
        for name in RUNTIME_SCHEDULER_NAMES
    }
    services = {
        name: _scheduler_unit_state(
            f"{name}.service", kind="service", command_runner=command_runner
        )
        for name in RUNTIME_SCHEDULER_NAMES
    }
    return timers, services


def _restore_scheduler_files(preimage: dict[str, Any]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for record in preimage["artifacts"]:
        try:
            _restore_artifact(record)
        except Exception as exc:
            failures.append(
                {"operation": ["restore", record["path"]], "error": repr(exc)}
            )
    for record in preimage["artifacts"]:
        path = Path(record["path"])
        try:
            kind = record["preimage_kind"]
            if kind == "absent":
                matches = not os.path.lexists(path)
            elif kind == "symlink":
                matches = path.is_symlink() and os.readlink(path) == record["target"]
            else:
                matches = not path.is_symlink() and path.is_file()
                if matches:
                    content = path.read_bytes()
                    matches = (
                        sha256_bytes(content) == record["sha256"]
                        and stat.S_IMODE(path.stat().st_mode) == int(record["mode"], 8)
                    )
            if not matches:
                failures.append(
                    {"operation": ["verify-preimage", record["path"]]}
                )
        except Exception as exc:
            failures.append(
                {
                    "operation": ["verify-preimage", record["path"]],
                    "error": repr(exc),
                }
            )
    return failures


def _restore_scheduler_enablement_links(
    preimage: dict[str, Any], *, mutated_paths: set[str]
) -> list[dict[str, Any]]:
    """Restore only leased enablement links this transaction attempted to mutate."""
    failures: list[dict[str, Any]] = []
    records = preimage["enablement_links"]
    for record in records:
        if record["path"] not in mutated_paths:
            continue
        try:
            _restore_artifact(record)
        except Exception as exc:
            failures.append(
                {"operation": ["restore", record["path"]], "error": repr(exc)}
            )
    for record in records:
        path = Path(record["path"])
        try:
            kind = record["preimage_kind"]
            if kind == "absent":
                matches = not os.path.lexists(path)
            elif kind == "symlink":
                matches = path.is_symlink() and os.readlink(path) == record["target"]
            else:
                matches = not path.is_symlink() and path.is_file()
                if matches:
                    content = path.read_bytes()
                    matches = (
                        sha256_bytes(content) == record["sha256"]
                        and stat.S_IMODE(path.stat().st_mode) == int(record["mode"], 8)
                    )
            if not matches:
                failures.append(
                    {"operation": ["verify-preimage", record["path"]]}
                )
        except Exception as exc:
            failures.append(
                {
                    "operation": ["verify-preimage", record["path"]],
                    "error": repr(exc),
                }
            )
    return failures


def _validate_scheduler_candidate(
    *,
    manifest_path: Path,
    release: Path,
    user_unit_dir: Path,
    libexec_dir: Path,
    validation_runner: Callable[[list[str]], subprocess.CompletedProcess[str]],
    cycle_validator: Callable[..., dict[str, Any]] | None,
) -> dict[str, Any]:
    unit_paths = [
        str(user_unit_dir / f"{name}.{kind}")
        for name in RUNTIME_SCHEDULER_NAMES
        for kind in ("service", "timer")
    ]
    argv = ["systemd-analyze", "--user", "verify", *unit_paths]
    verified = validation_runner(argv)
    if verified.returncode:
        raise RuntimeRefreshError(
            "scheduler-candidate-systemd-verify-failed",
            "candidate scheduler units failed systemd-analyze --user verify",
            details={
                "argv": argv,
                "returncode": verified.returncode,
                "stdout": verified.stdout[-4000:],
                "stderr": verified.stderr[-4000:],
            },
        )

    mismatched_artifacts = [
        item["live_path"]
        for item in _scheduler_artifacts(
            release=release,
            user_unit_dir=user_unit_dir,
            libexec_dir=libexec_dir,
        )
        if not _live_artifact_matches(item)
    ]
    if mismatched_artifacts:
        raise RuntimeRefreshError(
            "scheduler-candidate-artifact-drift",
            "candidate scheduler artifacts changed before activation",
            details={"paths": mismatched_artifacts},
        )

    if cycle_validator is None:
        from .cycle_deployment import audit_cycle_deployment

        cycle_validator = audit_cycle_deployment
    module_paths = {
        "bureau.cycle_deployment": release / "src/bureau/cycle_deployment.py",
        "bureau_cycle": release / "src/bureau_cycle/__init__.py",
        "bureau.closure_runner": release / "src/bureau/closure_runner.py",
    }
    try:
        cycle = cycle_validator(
            manifest_path=manifest_path,
            canonical_root=release,
            unit_root=user_unit_dir,
            shim_root=libexec_dir,
            module_paths=module_paths,
        )
    except Exception as exc:
        raise RuntimeRefreshError(
            "scheduler-candidate-cycle-deployment-invalid",
            "candidate-bound read-only cycle-deployment validation failed",
            details={"error": repr(exc)},
        ) from exc
    if (
        not isinstance(cycle, dict)
        or cycle.get("status") != "ok"
        or cycle.get("activatable") is not False
        or cycle.get("read_only") is not True
        or cycle.get("self_heal") is not False
    ):
        raise RuntimeRefreshError(
            "scheduler-candidate-cycle-deployment-drift",
            "candidate-bound read-only cycle-deployment validation found drift",
            details={"audit": cycle},
        )
    return {
        "systemd_analyze": {
            "argv": argv,
            "returncode": verified.returncode,
        },
        "cycle_deployment": cycle,
        "contract_equivalent": "bureau --json cycle-deployment",
        "candidate_bound": True,
        "read_only": True,
    }


def _restore_scheduler_preimage(
    preimage: dict[str, Any],
    *,
    mutated_enablement_paths: set[str],
    command_runner: Callable[[list[str]], subprocess.CompletedProcess[str]],
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []

    def attempt(arguments: list[str]) -> None:
        try:
            _run_scheduler_command(arguments, command_runner=command_runner)
        except Exception as exc:  # every recovery failure must remain explicit
            failures.append({"operation": arguments, "error": repr(exc)})

    timer_units = [f"{name}.timer" for name in RUNTIME_SCHEDULER_NAMES]
    attempt(["stop", *timer_units])
    failures.extend(_restore_scheduler_files(preimage))
    failures.extend(
        _restore_scheduler_enablement_links(
            preimage, mutated_paths=mutated_enablement_paths
        )
    )
    attempt(["daemon-reload"])
    for name in RUNTIME_SCHEDULER_NAMES:
        state = preimage["timers"][name]
        intent = preimage["timer_intent"][name]
        unit = f"{name}.timer"
        if state["ActiveState"] == "active":
            attempt(["start", unit])
        elif state["ActiveState"] == "inactive" and intent != "absent":
            attempt(["stop", unit])
    for name in RUNTIME_SCHEDULER_NAMES:
        state = preimage["services"][name]
        unit = f"{name}.service"
        if state["LoadState"] == "loaded" and state["ActiveState"] == "inactive":
            attempt(["stop", unit])
        if state["LoadState"] == "loaded" and state["Result"] == "success":
            attempt(["reset-failed", unit])

    try:
        timers, services = _timer_states(command_runner=command_runner)
        for kind, expected_group, observed_group in (
            ("timer", preimage["timers"], timers),
            ("service", preimage["services"], services),
        ):
            for name in RUNTIME_SCHEDULER_NAMES:
                expected = expected_group[name]
                observed = observed_group[name]
                if observed != expected:
                    failures.append(
                        {
                            "operation": ["verify-preimage", f"{name}.{kind}"],
                            "expected": expected,
                            "observed": observed,
                        }
                    )
    except Exception as exc:
        failures.append({"operation": ["verify-preimage"], "error": repr(exc)})
    return failures


def _scheduler_readback(
    *,
    source_commit: str,
    release_id: str,
    release: Path,
    user_unit_dir: Path,
    libexec_dir: Path,
    prior_timers: dict[str, dict[str, str]],
    timer_intent: dict[str, str],
    command_runner: Callable[[list[str]], subprocess.CompletedProcess[str]],
) -> dict[str, Any]:
    artifacts = _scheduler_artifacts(
        release=release,
        user_unit_dir=user_unit_dir,
        libexec_dir=libexec_dir,
    )
    artifact_evidence: list[dict[str, Any]] = []
    for item in artifacts:
        path = Path(item["live_path"])
        if path.is_symlink():
            raise RuntimeRefreshError(
                "scheduler-readback-fragment-replaceable",
                "live scheduler artifact is a replaceable symlink",
                details={"path": str(path), "kind": "symlink"},
            )
        if not os.path.lexists(path):
            raise RuntimeRefreshError(
                "scheduler-readback-fragment-replaceable",
                "live scheduler artifact is absent",
                details={"path": str(path), "kind": "absent"},
            )
        if not path.is_file():
            raise RuntimeRefreshError(
                "scheduler-readback-fragment-unsafe",
                "live scheduler artifact has an unsafe non-file type",
                details={"path": str(path)},
            )
        observed_sha256 = sha256_bytes(path.read_bytes())
        expected_mode = int(item["mode"], 8)
        observed_mode = stat.S_IMODE(path.stat().st_mode)
        if observed_sha256 != item["source_sha256"] or observed_mode != expected_mode:
            raise RuntimeRefreshError(
                "scheduler-readback-fragment-mismatch",
                "live scheduler artifact differs from the immutable release",
                details={
                    "path": str(path),
                    "source_path": item["source_path"],
                    "expected_sha256": item["source_sha256"],
                    "observed_sha256": observed_sha256,
                    "expected_mode": item["mode"],
                    "observed_mode": f"{observed_mode:04o}",
                },
            )
        artifact_evidence.append(
            {**item, "live_sha256": observed_sha256, "matches_release": True}
        )

    timers, services = _timer_states(command_runner=command_runner)
    for name in RUNTIME_SCHEDULER_NAMES:
        timer = timers[name]
        service = services[name]
        expected_timer_path = str(user_unit_dir / f"{name}.timer")
        expected_service_path = str(user_unit_dir / f"{name}.service")
        if (
            timer["LoadState"] != "loaded"
            or timer["FragmentPath"] != expected_timer_path
            or service["LoadState"] != "loaded"
            or service["FragmentPath"] != expected_service_path
        ):
            raise RuntimeRefreshError(
                "scheduler-readback-load-state-invalid",
                "managed scheduler unit is not loaded from its converged fragment",
                details={"name": name, "timer": timer, "service": service},
            )
        if name == REQUIRED_RUNTIME_TIMER:
            if (
                timer["UnitFileState"] != "enabled"
                or timer["ActiveState"] != "active"
                or timer["SubState"] != "waiting"
            ):
                raise RuntimeRefreshError(
                    "scheduler-required-timer-invalid",
                    "required task-supply timer is not enabled and waiting",
                    details={"timer": timer, "service": service},
                )
            if service["ActiveState"] == "failed" or service["Result"] != "success":
                raise RuntimeRefreshError(
                    "scheduler-required-service-invalid",
                    "required task-supply service is failed or has a non-success result",
                    details={"timer": timer, "service": service},
                )
            continue
        prior = prior_timers[name]
        intent = timer_intent[name]
        expected_unit_file_state = "disabled" if intent == "absent" else intent
        if (
            timer["UnitFileState"] != expected_unit_file_state
            or timer["ActiveState"] != prior["ActiveState"]
            or timer["SubState"] != prior["SubState"]
        ):
            raise RuntimeRefreshError(
                "scheduler-timer-intent-drift",
                "managed timer intent or active state changed during convergence",
                details={
                    "name": name,
                    "expected_unit_file_state": expected_unit_file_state,
                    "prior": prior,
                    "observed": timer,
                },
            )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "bureau_runtime_scheduler_readback",
        "source_commit": source_commit,
        "release_id": release_id,
        "immutable_release_path": str(release),
        "user_unit_dir": str(user_unit_dir),
        "libexec_dir": str(libexec_dir),
        "required_timer": f"{REQUIRED_RUNTIME_TIMER}.timer",
        "artifacts": artifact_evidence,
        "prior_timer_intent": timer_intent,
        "pre_convergence_timers": prior_timers,
        "timers": timers,
        "services": services,
        "authoritative": True,
    }


def converge_user_scheduler(
    *,
    source_commit: str,
    release_id: str,
    release: Path,
    user_unit_dir: Path,
    libexec_dir: Path,
    runtime_user_unit_dir: Path,
    rollback_directory: Path,
    manifest_path: Path,
    command_runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
    validation_runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
    cycle_validator: Callable[..., dict[str, Any]] | None = None,
    before_validation: Callable[[], None] | None = None,
    after_activation: Callable[[dict[str, Any]], None] | None = None,
    rollback_effect: Callable[[], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Converge immutable scheduler bytes and systemd state as one recoverable effect."""
    runner = command_runner or (lambda argv: _run(argv, timeout=60))
    verifier = validation_runner or (lambda argv: _run(argv, timeout=60))
    _validate_scheduler_parent(user_unit_dir, label="user unit directory")
    _validate_scheduler_parent(libexec_dir, label="libexec directory")
    _validate_scheduler_parent(
        user_unit_dir / "timers.target.wants",
        label="persistent timer enablement directory",
    )
    _validate_scheduler_parent(
        runtime_user_unit_dir,
        label="runtime user unit directory",
        allow_absent=True,
    )
    artifacts = _scheduler_artifacts(
        release=release,
        user_unit_dir=user_unit_dir,
        libexec_dir=libexec_dir,
    )
    changed = [item for item in artifacts if not _live_artifact_matches(item)]
    timers, services = _timer_states(command_runner=runner)
    intents = {
        name: _timer_intent(timers[name], unit=f"{name}.timer")
        for name in RUNTIME_SCHEDULER_NAMES
    }
    rollback_root = rollback_directory / "scheduler"
    if rollback_root.exists():
        raise RuntimeRefreshError(
            "scheduler-rollback-generation-exists",
            "scheduler rollback generation already exists",
            details={"path": str(rollback_root)},
        )
    rollback_root.mkdir(parents=True, exist_ok=False, mode=0o700)
    backup_files = rollback_root / "files"
    backup_files.mkdir(mode=0o700)
    required_timer_unit = user_unit_dir / f"{REQUIRED_RUNTIME_TIMER}.timer"
    required_enablement_links = [
        {
            "name": REQUIRED_RUNTIME_TIMER,
            "kind": "persistent-wants-link",
            "live_path": str(
                user_unit_dir
                / "timers.target.wants"
                / f"{REQUIRED_RUNTIME_TIMER}.timer"
            ),
        },
        {
            "name": REQUIRED_RUNTIME_TIMER,
            "kind": "runtime-wants-link",
            "live_path": str(
                runtime_user_unit_dir
                / "timers.target.wants"
                / f"{REQUIRED_RUNTIME_TIMER}.timer"
            ),
        },
    ]
    preimage = {
        "schema_version": SCHEMA_VERSION,
        "kind": "bureau_runtime_scheduler_preimage",
        "source_commit": source_commit,
        "release_id": release_id,
        "artifacts": [_snapshot_live_artifact(item, backup_files) for item in artifacts],
        "timers": timers,
        "services": services,
        "timer_intent": intents,
        "enablement_links": [
            _snapshot_live_artifact(item, backup_files)
            for item in required_enablement_links
        ],
    }
    preimage_path = rollback_root / "preimage.json"
    atomic_write(preimage_path, canonical_bytes(preimage))

    systemd_mutated = False
    mutated_enablement_paths: set[str] = set()
    try:
        if before_validation is not None:
            before_validation()
        for item in changed:
            source = Path(item["source_path"])
            live_path = Path(item["live_path"])
            atomic_write(
                live_path,
                source.read_bytes(),
                int(item["mode"], 8),
                staging_path=_scheduler_staging_path(live_path),
            )
        validation = _validate_scheduler_candidate(
            manifest_path=manifest_path,
            release=release,
            user_unit_dir=user_unit_dir,
            libexec_dir=libexec_dir,
            validation_runner=verifier,
            cycle_validator=cycle_validator,
        )
        persistent_link = Path(required_enablement_links[0]["live_path"])
        link_matches = False
        try:
            link_matches = (
                persistent_link.is_symlink()
                and persistent_link.resolve(strict=False)
                == required_timer_unit.resolve(strict=False)
            )
        except (OSError, RuntimeError):
            link_matches = False
        if not link_matches:
            # Mark the leased exact path before replace/fsync so rollback also
            # covers an ambiguous failure after the namespace mutation.
            mutated_enablement_paths.add(str(persistent_link))
            _replace_with_symlink(
                persistent_link, f"../{REQUIRED_RUNTIME_TIMER}.timer"
            )
        systemd_mutated = True
        _run_scheduler_command(["daemon-reload"], command_runner=runner)
        _run_scheduler_command(
            ["start", f"{REQUIRED_RUNTIME_TIMER}.timer"],
            command_runner=runner,
        )
        readback = _scheduler_readback(
            source_commit=source_commit,
            release_id=release_id,
            release=release,
            user_unit_dir=user_unit_dir,
            libexec_dir=libexec_dir,
            prior_timers=timers,
            timer_intent=intents,
            command_runner=runner,
        )
        readback["candidate_validation"] = validation
        readback["changed_artifacts"] = [item["live_path"] for item in changed]
        readback["rollback_preimage_path"] = str(preimage_path)
        if after_activation is not None:
            after_activation(readback)
    except Exception as exc:
        rollback_failures = (
            _restore_scheduler_preimage(
                preimage,
                mutated_enablement_paths=mutated_enablement_paths,
                command_runner=runner,
            )
            if systemd_mutated
            else [
                *_restore_scheduler_files(preimage),
                *_restore_scheduler_enablement_links(
                    preimage, mutated_paths=mutated_enablement_paths
                ),
            ]
        )
        if rollback_effect is not None:
            try:
                rollback_failures.extend(rollback_effect())
            except Exception as rollback_exc:
                rollback_failures.append(
                    {"operation": ["restore-install"], "error": repr(rollback_exc)}
                )
        error = exc.as_dict() if isinstance(exc, RuntimeRefreshError) else {
            "code": "scheduler-convergence-unexpected",
            "message": repr(exc),
            "details": {},
        }
        if rollback_failures:
            raise RuntimeRefreshError(
                "scheduler-convergence-recovery-required",
                "scheduler convergence failed and exact preimage recovery is incomplete",
                details={
                    "cause": error,
                    "rollback_failures": rollback_failures,
                    "preimage_path": str(preimage_path),
                    "safe_to_retry": False,
                },
            ) from exc
        raise RuntimeRefreshError(
            "scheduler-convergence-rolled-back",
            "scheduler convergence failed; exact preimage was restored",
            details={
                "cause": error,
                "preimage_path": str(preimage_path),
                "safe_to_retry": False,
            },
        ) from exc
    return readback


def readback_user_scheduler(
    scheduler_receipt: dict[str, Any],
    *,
    expected_commit: str,
    command_runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    """Re-read one result-bound scheduler deployment from live user systemd."""
    if (
        scheduler_receipt.get("kind") != "bureau_runtime_scheduler_readback"
        or scheduler_receipt.get("source_commit") != expected_commit
        or scheduler_receipt.get("authoritative") is not True
    ):
        raise RuntimeRefreshError(
            "scheduler-receipt-invalid",
            "installer scheduler receipt is not bound to the deployed source",
        )
    runner = command_runner or (lambda argv: _run(argv, timeout=60))
    prior_timers = scheduler_receipt.get("pre_convergence_timers")
    timer_intent = scheduler_receipt.get("prior_timer_intent")
    if not isinstance(prior_timers, dict) or not isinstance(timer_intent, dict):
        raise RuntimeRefreshError(
            "scheduler-receipt-invalid", "installer scheduler receipt lacks timer preimage"
        )
    return _scheduler_readback(
        source_commit=expected_commit,
        release_id=str(scheduler_receipt.get("release_id", "")),
        release=Path(str(scheduler_receipt.get("immutable_release_path", ""))),
        user_unit_dir=Path(str(scheduler_receipt.get("user_unit_dir", ""))),
        libexec_dir=Path(str(scheduler_receipt.get("libexec_dir", ""))),
        prior_timers=prior_timers,
        timer_intent=timer_intent,
        command_runner=runner,
    )


def _scheduler_closeout_readbacks_match(
    persisted: dict[str, Any], current: dict[str, Any]
) -> bool:
    """Compare result-bound scheduler invariants while ignoring transient service state."""
    if current == persisted:
        return True
    stable_fields = (
        "schema_version",
        "kind",
        "source_commit",
        "release_id",
        "immutable_release_path",
        "user_unit_dir",
        "libexec_dir",
        "required_timer",
        "artifacts",
        "prior_timer_intent",
        "pre_convergence_timers",
        "timers",
        "authoritative",
    )
    if any(current.get(field) != persisted.get(field) for field in stable_fields):
        return False
    persisted_services = persisted.get("services")
    current_services = current.get("services")
    expected_names = set(RUNTIME_SCHEDULER_NAMES)
    if (
        not isinstance(persisted_services, dict)
        or not isinstance(current_services, dict)
        or set(persisted_services) != expected_names
        or set(current_services) != expected_names
    ):
        return False
    for name in RUNTIME_SCHEDULER_NAMES:
        persisted_state = persisted_services.get(name)
        current_state = current_services.get(name)
        if not isinstance(persisted_state, dict) or not isinstance(current_state, dict):
            return False
        fields = ("LoadState", "FragmentPath")
        if name == REQUIRED_RUNTIME_TIMER:
            fields += ("Result",)
        if any(current_state.get(field) != persisted_state.get(field) for field in fields):
            return False
    return True


def _install_closeout_readbacks_match(
    persisted: dict[str, Any],
    current: dict[str, Any],
    install_receipt: dict[str, Any],
) -> bool:
    """Authenticate immutable install evidence when historical readback omits scheduler state."""
    persisted_base = dict(persisted)
    current_base = dict(current)
    persisted_scheduler = persisted_base.pop("scheduler", None)
    current_scheduler = current_base.pop("scheduler", None)
    if current_base != persisted_base:
        return False
    if persisted_scheduler is None:
        return current_scheduler is None
    if not isinstance(persisted_scheduler, dict):
        return False
    installed_scheduler = install_receipt.get("scheduler")
    if not isinstance(installed_scheduler, dict) or not _scheduler_closeout_readbacks_match(
        persisted_scheduler, installed_scheduler
    ):
        return False
    # Historical install authentication intentionally cannot reproduce live systemd
    # state. The persisted scheduler evidence is already bound by the immutable
    # result and installer receipt. If a reader does provide scheduler state, compare
    # only the invariants that readback_user_scheduler itself validates as stable.
    if current_scheduler is None:
        return True
    if not isinstance(current_scheduler, dict):
        return False
    return _scheduler_closeout_readbacks_match(persisted_scheduler, current_scheduler)


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _default_read_only_authority_store() -> Any:
    # Imported lazily because v2 imports this module for state-root validation.
    from .read_only_state import ReadOnlyStateStore

    return ReadOnlyStateStore(DEFAULT_BUREAU_STATE_DB, DEFAULT_BUREAU_STATE_ROOT)


def _default_authority_store() -> Any:
    # Imported lazily because v2 imports this module for state-root validation.
    from .v2 import StateStore

    return StateStore(DEFAULT_BUREAU_STATE_DB, DEFAULT_BUREAU_STATE_ROOT)


def _authority_store_binding(store: Any) -> dict[str, str]:
    path = getattr(store, "path", None)
    root = getattr(store, "state_root", None)
    if not isinstance(path, Path) or not isinstance(root, Path):
        raise RuntimeRefreshError(
            "authority-state-store-invalid",
            "authoritative StateStore has no typed path binding",
        )
    resolved_path = path.expanduser().resolve()
    resolved_root = root.expanduser().resolve()
    if resolved_path.parent != resolved_root:
        raise RuntimeRefreshError(
            "authority-state-store-invalid",
            "authoritative StateStore database is outside its state root",
        )
    return {"state_db": str(resolved_path), "state_root": str(resolved_root)}


def _authority_store_from_intent(intent: dict[str, Any]) -> Any:
    binding = intent.get("authority_state_store")
    if not isinstance(binding, dict) or set(binding) != {"state_db", "state_root"}:
        raise RuntimeRefreshError(
            "authority-state-store-binding-invalid",
            "runtime-refresh intent has no exact authoritative StateStore binding",
        )
    state_db = Path(str(binding.get("state_db", ""))).expanduser().resolve()
    state_root = Path(str(binding.get("state_root", ""))).expanduser().resolve()
    if state_db.parent != state_root:
        raise RuntimeRefreshError(
            "authority-state-store-binding-invalid",
            "intent authoritative StateStore database is outside its state root",
        )
    # Imported lazily because v2 imports this module for state-root validation.
    from .v2 import StateStore

    return StateStore(state_db, state_root)


def _bound_authority_store(intent: dict[str, Any], store: Any | None) -> Any:
    resolved = store or _authority_store_from_intent(intent)
    observed = _authority_store_binding(resolved)
    if observed != intent.get("authority_state_store"):
        raise RuntimeRefreshError(
            "authority-state-store-binding-mismatch",
            "provided StateStore differs from the intent authority binding",
            details={"expected": intent.get("authority_state_store"), "observed": observed},
        )
    return resolved


def _closeout_authority_store(intent: dict[str, Any], store: Any | None) -> Any:
    if "authority_state_store" in intent:
        return _bound_authority_store(intent, store)
    # Historical typed intents predate the StateStore path field. The closeout
    # still resolves exactly one typed store; it does not infer task truth from
    # the installed Registry snapshot.
    resolved = store or _default_authority_store()
    _authority_store_binding(resolved)
    return resolved


def _read_authority_task(store: Any, task_id: str) -> dict[str, Any]:
    try:
        current = store.task_spec(task_id)
    except (legacy.StateError, OSError, sqlite3.Error) as exc:
        raise RuntimeRefreshError(
            "authority-state-store-invalid",
            "authoritative StateStore TaskSpec read failed",
            details={"task_id": task_id, "error": str(exc)},
        ) from exc
    if current is None:
        raise RuntimeRefreshError(
            "authority-task-missing",
            "approval_task_id is absent from the authoritative StateStore",
            details={"task_id": task_id},
        )
    return current


def _put_authority_task(
    store: Any,
    spec: dict[str, Any],
    *,
    idempotency_key: str,
    expected_revision: int,
    source: str,
) -> dict[str, Any]:
    try:
        return store.put_task_spec(
            spec,
            idempotency_key=idempotency_key,
            expected_revision=expected_revision,
            source=source,
        )
    except (legacy.StateError, OSError, sqlite3.Error) as exc:
        raise RuntimeRefreshError(
            "authority-task-cas-failed",
            "authoritative TaskSpec compare-and-swap failed closed",
            details={
                "task_id": spec.get("id"),
                "expected_revision": expected_revision,
                "error": str(exc),
            },
        ) from exc


def _validated_authority_target_binding(value: Any) -> dict[str, Any]:
    fields = {
        "schema_version",
        "kind",
        "task_id",
        "authority_revision",
        "authority_spec_sha256",
        "target_sha256",
        "intent_sha256",
        "bound_at",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise RuntimeRefreshError(
            "authority-target-binding-invalid",
            "runtime-refresh authority target binding is malformed",
        )
    if (
        value.get("schema_version") != RUNTIME_AUTHORITY_SCHEMA_VERSION
        or value.get("kind") != RUNTIME_AUTHORITY_BINDING_KIND
        or not isinstance(value.get("task_id"), str)
        or not isinstance(value.get("authority_revision"), int)
        or isinstance(value.get("authority_revision"), bool)
        or value["authority_revision"] < 1
        or not _is_sha256(value.get("authority_spec_sha256"))
        or not _is_sha256(value.get("target_sha256"))
        or not _is_sha256(value.get("intent_sha256"))
        or not isinstance(value.get("bound_at"), str)
    ):
        raise RuntimeRefreshError(
            "authority-target-binding-invalid",
            "runtime-refresh authority target binding values are invalid",
        )
    parse_time(value["bound_at"])
    return dict(value)


def _validated_authority_consumption(value: Any) -> dict[str, Any]:
    fields = {
        "schema_version",
        "kind",
        "status",
        "task_id",
        "authority_revision",
        "authority_spec_sha256",
        "target_sha256",
        "intent_sha256",
        "result_sha256",
        "consumed_at",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise RuntimeRefreshError(
            "authority-consumption-invalid",
            "runtime-refresh authority consumption is malformed",
        )
    if (
        value.get("schema_version") != RUNTIME_AUTHORITY_SCHEMA_VERSION
        or value.get("kind") != RUNTIME_AUTHORITY_CONSUMPTION_KIND
        or value.get("status") != "consumed"
        or not isinstance(value.get("task_id"), str)
        or not isinstance(value.get("authority_revision"), int)
        or isinstance(value.get("authority_revision"), bool)
        or value["authority_revision"] < 1
        or not _is_sha256(value.get("authority_spec_sha256"))
        or not _is_sha256(value.get("target_sha256"))
        or not _is_sha256(value.get("intent_sha256"))
        or not _is_sha256(value.get("result_sha256"))
        or not isinstance(value.get("consumed_at"), str)
    ):
        raise RuntimeRefreshError(
            "authority-consumption-invalid",
            "runtime-refresh authority consumption values are invalid",
        )
    parse_time(value["consumed_at"])
    return dict(value)


def _runtime_authority_metadata(spec: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    metadata = spec.get("metadata")
    authority = metadata.get("runtime_refresh_authority") if isinstance(metadata, dict) else None
    if not isinstance(metadata, dict) or not isinstance(authority, dict):
        raise RuntimeRefreshError(
            "authority-contract-missing",
            "TaskSpec has no runtime_refresh_authority contract",
        )
    return metadata, authority


def _validated_runtime_source_precondition(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    fields = {
        "schema_version",
        "policy",
        "identity_sources",
        "require_deployment_registry_identity_match",
        "registered_deployed_source_commit",
        "registered_manifest_sha256",
        "registered_registry_source_commit",
        "ancestry_verification",
        "require_target_freshness",
        "required_before",
        "fail_closed",
        "does_not_establish",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise RuntimeRefreshError(
            "authority-source-precondition-contract-invalid",
            "runtime authority source_precondition contract is malformed",
        )
    commit_fields = (
        value.get("registered_deployed_source_commit"),
        value.get("registered_registry_source_commit"),
    )
    if (
        value.get("schema_version") != 1
        or value.get("policy") != "registered-source-or-verified-target-ancestor"
        or value.get("identity_sources")
        != ["deployment-manifest.source_commit", "canonical-registry.source_commit"]
        or value.get("require_deployment_registry_identity_match") is not True
        or value.get("ancestry_verification") != "git-merge-base-is-ancestor"
        or value.get("require_target_freshness") is not True
        or value.get("required_before") != ["prepare-intent", "apply"]
        or value.get("fail_closed") is not True
        or not all(
            isinstance(item, str)
            and len(item) == 40
            and all(character in "0123456789abcdef" for character in item)
            for item in commit_fields
        )
        or commit_fields[0] != commit_fields[1]
        or not _is_sha256(value.get("registered_manifest_sha256"))
        or not isinstance(value.get("does_not_establish"), list)
        or not all(isinstance(item, str) and item for item in value["does_not_establish"])
    ):
        raise RuntimeRefreshError(
            "authority-source-precondition-contract-invalid",
            "runtime authority source_precondition values are invalid",
        )
    return dict(value)


def _validate_candidate_source_precondition(
    candidate: dict[str, Any], source_precondition: dict[str, Any] | None
) -> None:
    if source_precondition is None:
        return
    ancestry = candidate.get("source_ancestry")
    identity = candidate.get("runtime_source_identity")
    deployed = candidate.get("deployed_source_commit")
    main_commit = candidate.get("main_commit")
    if (
        not isinstance(ancestry, dict)
        or ancestry.get("schema_version") != 1
        or ancestry.get("status") != "proven"
        or ancestry.get("deployed_source_commit") != deployed
        or ancestry.get("main_commit") != main_commit
    ):
        raise RuntimeRefreshError(
            "source-ancestry-unproven",
            "runtime refresh source ancestry is not proven for the exact target",
        )
    method = ancestry.get("method")
    if method == "same-commit":
        ancestry_valid = deployed == main_commit
    elif method == "github-compare":
        ancestry_valid = (
            ancestry.get("compare_status") == "ahead"
            and isinstance(ancestry.get("ahead_by"), int)
            and not isinstance(ancestry.get("ahead_by"), bool)
            and ancestry["ahead_by"] > 0
            and ancestry.get("behind_by") == 0
            and ancestry.get("merge_base_commit") == deployed
        )
    elif method == "first-parent-walk":
        ancestry_valid = (
            isinstance(ancestry.get("ahead_by"), int)
            and not isinstance(ancestry.get("ahead_by"), bool)
            and ancestry["ahead_by"] > 0
            and ancestry.get("behind_by") == 0
            and ancestry.get("merge_base_commit") == deployed
        )
    else:
        ancestry_valid = False
    if not ancestry_valid:
        raise RuntimeRefreshError(
            "source-ancestry-unproven",
            "runtime refresh source ancestry evidence is malformed or divergent",
        )
    if (
        not isinstance(identity, dict)
        or identity.get("schema_version") != 1
        or identity.get("status") != "proven"
        or identity.get("deployed_source_commit") != deployed
        or identity.get("registry_source_commit") != deployed
        or identity.get("registry_reasons") != []
    ):
        raise RuntimeRefreshError(
            "runtime-source-identity-unproven",
            "deployment manifest and canonical Registry source identity do not match",
        )


def _validate_runtime_refresh_authority_contract(
    *,
    spec: dict[str, Any],
    approval_task_id: str,
    allow_planned: bool,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    if spec.get("id") != approval_task_id:
        raise RuntimeRefreshError(
            "authority-task-id-mismatch",
            "TaskSpec does not match approval_task_id",
            details={"expected": approval_task_id, "observed": spec.get("id")},
        )
    state = spec.get("state")
    if state in legacy.TERMINAL_TASK_STATES:
        raise RuntimeRefreshError(
            "authority-task-terminal",
            "terminal TaskSpec cannot authorize or bootstrap a runtime refresh",
            details={"task_id": approval_task_id, "state": state},
        )
    metadata, authority = _runtime_authority_metadata(spec)
    required_values = {
        "schema_version": RUNTIME_AUTHORITY_SCHEMA_VERSION,
        "mode": RUNTIME_AUTHORITY_MODE,
        "single_use": True,
        "required_action_class": "runtime_mutation",
        "required_approval_level": "break_glass",
        "required_claim_resource": "component.bureau.runtime",
        "target_binding": RUNTIME_AUTHORITY_TARGET_BINDING,
        "forbid_foreign_task_substitution": True,
        "forbid_historical_target_reuse": True,
        "successor_task_required_after_terminal": True,
    }
    mismatched = {
        key: {"expected": value, "observed": authority.get(key)}
        for key, value in required_values.items()
        if authority.get(key) != value
    }
    if mismatched:
        raise RuntimeRefreshError(
            "authority-contract-invalid",
            "TaskSpec runtime_refresh_authority contract is not single-use and target-bound",
            details={"mismatched": mismatched},
        )
    _validated_runtime_source_precondition(authority.get("source_precondition"))
    required_states = authority.get("required_task_state")
    if required_states != list(RUNTIME_AUTHORITY_ALLOWED_STATES):
        raise RuntimeRefreshError(
            "authority-state-contract-invalid",
            "runtime authority allowed-state contract is invalid",
            details={"observed": required_states},
        )
    allowed_states = set(required_states)
    if allow_planned:
        allowed_states.add("planned")
    if state not in allowed_states:
        raise RuntimeRefreshError(
            "authority-task-state-invalid",
            "TaskSpec is not in an allowed runtime-authority state",
            details={"task_id": approval_task_id, "state": state},
        )
    declared = spec.get("execution")
    declared = declared.get("approval") if isinstance(declared, dict) else None
    if not isinstance(declared, dict) or (
        declared.get("action_class") != "runtime_mutation"
        or declared.get("required_level") != "break_glass"
    ):
        raise RuntimeRefreshError(
            "authority-approval-contract-invalid",
            "TaskSpec approval contract is not runtime_mutation/break_glass",
        )
    claims = spec.get("claims")
    if not isinstance(claims, list) or not any(
        isinstance(claim, dict)
        and claim.get("resource") == "component.bureau.runtime"
        and claim.get("mode") == "write"
        for claim in claims
    ):
        raise RuntimeRefreshError(
            "authority-claim-contract-invalid",
            "TaskSpec does not claim the required Bureau runtime resource",
        )
    return metadata, authority, str(state)


def validate_authoritative_runtime_refresh_task(
    *,
    store: Any,
    approval_task_id: str,
    target_sha256: str,
    expected_revision: int | None = None,
    expected_spec_sha256: str | None = None,
    expected_intent_sha256: str | None = None,
    allow_bound_intent: bool = False,
    expected_consumption: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read and validate the current StateStore TaskSpec as runtime authority."""
    if not approval_task_id or not _is_sha256(target_sha256):
        raise RuntimeRefreshError(
            "authority-preflight-input-invalid",
            "runtime authority preflight requires an exact task and target digest",
        )
    current = _read_authority_task(store, approval_task_id)
    spec = current.get("spec")
    if not isinstance(spec, dict):
        raise RuntimeRefreshError(
            "authority-task-invalid", "authoritative TaskSpec payload is invalid"
        )
    if current.get("task_id") != approval_task_id or spec.get("id") != approval_task_id:
        raise RuntimeRefreshError(
            "authority-task-id-mismatch",
            "authoritative TaskSpec does not match approval_task_id",
            details={
                "expected": approval_task_id,
                "record_task_id": current.get("task_id"),
                "spec_task_id": spec.get("id"),
            },
        )
    revision = current.get("revision")
    digest = current.get("spec_sha256")
    if (
        not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 1
        or not _is_sha256(digest)
    ):
        raise RuntimeRefreshError(
            "authority-task-invalid", "authoritative TaskSpec revision binding is invalid"
        )
    if expected_revision is not None and (
        revision != expected_revision or digest != expected_spec_sha256
    ):
        raise RuntimeRefreshError(
            "authority-task-drift",
            "authoritative TaskSpec changed after the expected preflight",
            details={
                "task_id": approval_task_id,
                "expected_revision": expected_revision,
                "observed_revision": revision,
                "expected_spec_sha256": expected_spec_sha256,
                "observed_spec_sha256": digest,
            },
        )
    metadata, authority, state = _validate_runtime_refresh_authority_contract(
        spec=spec,
        approval_task_id=approval_task_id,
        allow_planned=False,
    )
    consumption_value = authority.get("consumption")
    consumption = (
        _validated_authority_consumption(consumption_value)
        if consumption_value is not None
        else None
    )
    if consumption is not None:
        comparable = dict(expected_consumption or {})
        if comparable:
            comparable["consumed_at"] = consumption["consumed_at"]
        if not comparable or consumption != comparable:
            raise RuntimeRefreshError(
                "authority-already-consumed",
                "single-use runtime authority is already consumed",
                details={"consumption": consumption},
            )
    if metadata.get("runtime_closeout") is not None:
        raise RuntimeRefreshError(
            "authority-already-consumed",
            "runtime authority already has closeout evidence",
        )
    binding_value = authority.get("target_binding_receipt")
    binding = (
        _validated_authority_target_binding(binding_value) if binding_value is not None else None
    )
    if binding is not None:
        if binding["task_id"] != approval_task_id or binding["target_sha256"] != target_sha256:
            raise RuntimeRefreshError(
                "authority-wrong-target",
                "single-use runtime authority is bound to another target",
                details={
                    "expected_target_sha256": target_sha256,
                    "bound_target_sha256": binding["target_sha256"],
                },
            )
        if not allow_bound_intent or binding["intent_sha256"] != expected_intent_sha256:
            raise RuntimeRefreshError(
                "authority-target-already-bound",
                "single-use runtime authority is already bound to an apply intent",
                details={
                    "target_sha256": binding["target_sha256"],
                    "intent_sha256": binding["intent_sha256"],
                },
            )
    return {
        "schema_version": RUNTIME_AUTHORITY_SCHEMA_VERSION,
        "task_id": approval_task_id,
        "revision": revision,
        "spec_sha256": digest,
        "state": state,
        "target_sha256": target_sha256,
        "target_binding_receipt": binding,
    }


def _validate_runtime_refresh_authority_adoption_spec(
    *,
    spec: dict[str, Any],
    approval_task_id: str,
) -> dict[str, Any]:
    metadata, authority, state = _validate_runtime_refresh_authority_contract(
        spec=spec,
        approval_task_id=approval_task_id,
        allow_planned=True,
    )
    publication = metadata.get("publication_path")
    if not isinstance(publication, dict) or (
        publication.get("kind") != "normal-protected-pull-request"
        or publication.get("state_store_transition")
        != "seed-missing-preserve-state-store"
    ):
        raise RuntimeRefreshError(
            "authority-adoption-publication-contract-invalid",
            "runtime authority is not marked for protected missing-only StateStore adoption",
        )
    if (
        authority.get("consumption") is not None
        or authority.get("target_binding_receipt") is not None
        or metadata.get("runtime_closeout") is not None
    ):
        raise RuntimeRefreshError(
            "authority-adoption-already-used",
            "runtime authority already contains target, consumption or closeout evidence",
        )
    return {
        "state": state,
        "publication_path": dict(publication),
    }


def _runtime_authority_task_path(approval_task_id: str) -> Path:
    if (
        not isinstance(approval_task_id, str)
        or not approval_task_id
        or "/" in approval_task_id
        or "\\" in approval_task_id
        or ".." in approval_task_id
    ):
        raise RuntimeRefreshError(
            "authority-adoption-task-id-invalid",
            "approval_task_id is unsafe for exact Registry lookup",
        )
    return Path("registry/tasks") / f"{approval_task_id}.json"


def _git_stdout(registry_root: Path, arguments: list[str]) -> str:
    argv = ["git", "-C", str(registry_root), *arguments]
    return _require_command(_run(argv), argv)


def verify_runtime_refresh_authority_publication(
    *,
    registry_root: Path,
    repository: str,
    approval_task_id: str,
    publication_pr: int,
    publication_merge_commit: str,
    expected_main_commit: str,
    expected_task_file_sha256: str,
    required_checks: tuple[str, ...] = DEFAULT_AUTHORITY_ADOPTION_REQUIRED_CHECKS,
    github: Callable[[list[str]], Any] = gh_preflight_json,
    registry: Any | None = None,
) -> tuple[Any, dict[str, Any]]:
    root = registry_root.expanduser().resolve()
    relative = _runtime_authority_task_path(approval_task_id)
    task_path = root / relative
    if root.is_symlink() or not root.is_dir():
        raise RuntimeRefreshError(
            "authority-adoption-registry-root-invalid",
            "Registry root is not a regular directory",
        )
    if task_path.is_symlink() or not task_path.is_file():
        raise RuntimeRefreshError(
            "authority-adoption-task-file-missing",
            "exact runtime authority TaskSpec file is absent",
            details={"path": str(relative)},
        )
    if not _is_sha256(expected_task_file_sha256):
        raise RuntimeRefreshError(
            "authority-adoption-file-digest-invalid",
            "expected TaskSpec file digest is invalid",
        )
    observed_file_sha256 = sha256_bytes(task_path.read_bytes())
    if observed_file_sha256 != expected_task_file_sha256:
        raise RuntimeRefreshError(
            "authority-adoption-file-digest-mismatch",
            "runtime authority TaskSpec file digest changed",
            details={
                "expected": expected_task_file_sha256,
                "observed": observed_file_sha256,
            },
        )
    try:
        raw_spec = json.loads(task_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeRefreshError(
            "authority-adoption-task-file-invalid",
            "runtime authority TaskSpec file is not valid JSON",
        ) from exc
    if not isinstance(raw_spec, dict):
        raise RuntimeRefreshError(
            "authority-adoption-task-file-invalid",
            "runtime authority TaskSpec payload is not an object",
        )
    adoption_contract = _validate_runtime_refresh_authority_adoption_spec(
        spec=raw_spec,
        approval_task_id=approval_task_id,
    )

    if (
        not isinstance(publication_pr, int)
        or isinstance(publication_pr, bool)
        or publication_pr < 1
        or not isinstance(publication_merge_commit, str)
        or len(publication_merge_commit) != 40
        or not isinstance(expected_main_commit, str)
        or len(expected_main_commit) != 40
    ):
        raise RuntimeRefreshError(
            "authority-adoption-publication-binding-invalid",
            "publication PR/main/merge binding is invalid",
        )

    observed_head = _git_stdout(root, ["rev-parse", "HEAD"])
    if observed_head != expected_main_commit:
        raise RuntimeRefreshError(
            "authority-adoption-checkout-head-mismatch",
            "Registry checkout is not bound to expected current main",
            details={"expected": expected_main_commit, "observed": observed_head},
        )
    status = _git_stdout(root, ["status", "--porcelain", "--untracked-files=no"])
    if status:
        raise RuntimeRefreshError(
            "authority-adoption-checkout-dirty",
            "Registry checkout has tracked local changes",
        )

    protection = github(["api", f"repos/{repository}/branches/main/protection"])
    protected_status = (
        protection.get("required_status_checks")
        if isinstance(protection, dict)
        else None
    )
    protected_contexts = (
        protected_status.get("contexts") if isinstance(protected_status, dict) else None
    )
    force_pushes = protection.get("allow_force_pushes") if isinstance(protection, dict) else None
    deletions = protection.get("allow_deletions") if isinstance(protection, dict) else None
    if (
        not isinstance(protected_status, dict)
        or protected_status.get("strict") is not True
        or not isinstance(protected_contexts, list)
        or any(
            not isinstance(context, str) or not context for context in protected_contexts
        )
        or not set(required_checks).issubset(set(protected_contexts))
        or not isinstance(force_pushes, dict)
        or force_pushes.get("enabled") is not False
        or not isinstance(deletions, dict)
        or deletions.get("enabled") is not False
    ):
        raise RuntimeRefreshError(
            "authority-adoption-main-protection-invalid",
            "GitHub main protection does not preserve the required publication gates",
            details={
                "required_checks": list(required_checks),
                "protected_contexts": protected_contexts,
            },
        )

    first_main = github(["api", f"repos/{repository}/commits/main"])
    if not isinstance(first_main, dict) or first_main.get("sha") != expected_main_commit:
        raise RuntimeRefreshError(
            "authority-adoption-main-drift",
            "GitHub main differs from the adoption checkout",
        )
    detail = github(
        [
            "pr",
            "view",
            str(publication_pr),
            "--repo",
            repository,
            "--json",
            "number,state,isDraft,mergedAt,mergeCommit,headRefOid,baseRefName,statusCheckRollup,url,files",
        ]
    )
    merge = detail.get("mergeCommit") if isinstance(detail, dict) else None
    merge_oid = merge.get("oid") if isinstance(merge, dict) else None
    files = detail.get("files") if isinstance(detail, dict) else None
    file_paths = (
        [
            item["path"]
            for item in files
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        ]
        if isinstance(files, list)
        else []
    )
    if (
        not isinstance(detail, dict)
        or detail.get("number") != publication_pr
        or detail.get("state") != "MERGED"
        or detail.get("isDraft") is True
        or detail.get("baseRefName") != "main"
        or merge_oid != publication_merge_commit
        or not isinstance(detail.get("headRefOid"), str)
        or not detail.get("mergedAt")
    ):
        raise RuntimeRefreshError(
            "authority-adoption-publication-pr-invalid",
            "publication PR is not the exact protected merged PR",
        )
    if file_paths != [relative.as_posix()]:
        raise RuntimeRefreshError(
            "authority-adoption-publication-scope-invalid",
            "publication PR changed files outside the exact authority TaskSpec",
            details={"observed_files": file_paths},
        )
    check_summary = summarize_required_checks(detail.get("statusCheckRollup"), required_checks)
    bad_checks = [name for name, item in check_summary.items() if item["state"] != "success"]
    if bad_checks:
        raise RuntimeRefreshError(
            "authority-adoption-required-ci-not-green",
            "publication PR required checks are not green",
            details={"checks": check_summary},
        )

    publication_parent = _git_stdout(root, ["rev-parse", f"{publication_merge_commit}^1"])
    prior_task_path = _git_stdout(
        root,
        ["ls-tree", "--name-only", publication_parent, "--", relative.as_posix()],
    )
    if prior_task_path:
        if prior_task_path != relative.as_posix():
            raise RuntimeRefreshError(
                "authority-adoption-prior-task-read-invalid",
                "prior TaskSpec path observation is ambiguous",
                details={"observed": prior_task_path},
            )
        raise RuntimeRefreshError(
            "authority-adoption-task-not-introduced",
            "publication PR did not introduce a new runtime authority TaskSpec",
        )

    ancestry = _run(
        [
            "git",
            "-C",
            str(root),
            "merge-base",
            "--is-ancestor",
            publication_merge_commit,
            expected_main_commit,
        ]
    )
    if ancestry.returncode != 0:
        raise RuntimeRefreshError(
            "authority-adoption-publication-not-ancestor",
            "publication merge is not an ancestor of current main",
        )
    merge_blob = _git_stdout(
        root, ["rev-parse", f"{publication_merge_commit}:{relative.as_posix()}"]
    )
    main_blob = _git_stdout(
        root, ["rev-parse", f"{expected_main_commit}:{relative.as_posix()}"]
    )
    if merge_blob != main_blob:
        raise RuntimeRefreshError(
            "authority-adoption-task-file-drift",
            "runtime authority TaskSpec changed after its publication merge",
        )
    second_main = github(["api", f"repos/{repository}/commits/main"])
    if not isinstance(second_main, dict) or second_main.get("sha") != expected_main_commit:
        raise RuntimeRefreshError(
            "authority-adoption-main-drift",
            "GitHub main changed during authority adoption preflight",
        )

    if registry is None:
        from .v2 import Registry
        try:
            registry = Registry.load(root)
        except (legacy.ValidationError, OSError) as exc:
            raise RuntimeRefreshError(
                "authority-adoption-registry-invalid",
                "Registry validation failed during authority adoption",
                details={"error": str(exc)},
            ) from exc
    task = getattr(registry, "tasks", {}).get(approval_task_id)
    task_raw = getattr(task, "raw", None)
    if task_raw != raw_spec:
        raise RuntimeRefreshError(
            "authority-adoption-registry-task-mismatch",
            "validated Registry TaskSpec differs from the exact task file",
        )
    return registry, {
        "repository": repository,
        "main_commit": expected_main_commit,
        "publication_pr": publication_pr,
        "publication_merge_commit": publication_merge_commit,
        "task_path": relative.as_posix(),
        "task_file_sha256": observed_file_sha256,
        "check_summary": check_summary,
        "task_state": adoption_contract["state"],
    }


def adopt_runtime_refresh_authority(
    *,
    registry_root: Path,
    repository: str,
    approval_task_id: str,
    publication_pr: int,
    publication_merge_commit: str,
    expected_main_commit: str,
    expected_task_file_sha256: str,
    required_checks: tuple[str, ...] = DEFAULT_AUTHORITY_ADOPTION_REQUIRED_CHECKS,
    authority_store: Any | None = None,
    github: Callable[[list[str]], Any] = gh_preflight_json,
    registry: Any | None = None,
) -> dict[str, Any]:
    store = authority_store or _default_authority_store()
    state_store_binding = _authority_store_binding(store)
    validated_registry, publication = verify_runtime_refresh_authority_publication(
        registry_root=registry_root,
        repository=repository,
        approval_task_id=approval_task_id,
        publication_pr=publication_pr,
        publication_merge_commit=publication_merge_commit,
        expected_main_commit=expected_main_commit,
        expected_task_file_sha256=expected_task_file_sha256,
        required_checks=required_checks,
        github=github,
        registry=registry,
    )
    try:
        result = store.seed_missing_registry_task_spec(validated_registry, approval_task_id)
    except (legacy.StateError, OSError, sqlite3.Error) as exc:
        raise RuntimeRefreshError(
            "authority-adoption-state-store-failed",
            "exact runtime authority StateStore adoption failed closed",
            details={"task_id": approval_task_id, "error": str(exc)},
        ) from exc
    current = _read_authority_task(store, approval_task_id)
    spec = current.get("spec")
    if not isinstance(spec, dict):
        raise RuntimeRefreshError(
            "authority-adoption-readback-invalid",
            "adopted runtime authority readback is invalid",
        )
    adoption = _validate_runtime_refresh_authority_adoption_spec(
        spec=spec,
        approval_task_id=approval_task_id,
    )
    if (
        current.get("revision") != result.get("revision")
        or current.get("spec_sha256") != result.get("spec_sha256")
    ):
        raise RuntimeRefreshError(
            "authority-adoption-readback-drift",
            "adopted runtime authority differs from the exact mutation receipt",
        )
    return {
        "schema_version": RUNTIME_AUTHORITY_SCHEMA_VERSION,
        "kind": "bureau_runtime_refresh_authority_adoption",
        "status": "adopted" if result.get("changed") else "already_present",
        "task_id": approval_task_id,
        "revision": current["revision"],
        "spec_sha256": current["spec_sha256"],
        "state": adoption["state"],
        "state_store": state_store_binding,
        "task_spec_root_sha256": result.get("task_spec_root_sha256"),
        "authoritative_root_sha256": result.get("authoritative_root_sha256"),
        "publication": publication,
        "does_not_establish": [
            "runtime mutation authority while task state is not ready or active",
            "queue mutation",
            "claim or dispatch authority",
            "adoption authority for any other Registry task",
        ],
    }


def _intent_authority_record(intent: dict[str, Any]) -> dict[str, Any]:
    value = intent.get("authority_task_spec")
    required = {
        "schema_version",
        "task_id",
        "revision",
        "spec_sha256",
        "state",
        "target_sha256",
        "target_binding_receipt",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value.get("schema_version") != RUNTIME_AUTHORITY_SCHEMA_VERSION
        or value.get("task_id") != intent.get("approval_task_id")
        or not isinstance(value.get("revision"), int)
        or isinstance(value.get("revision"), bool)
        or value["revision"] < 1
        or not _is_sha256(value.get("spec_sha256"))
        or value.get("target_sha256") != intent.get("target_sha256")
        or value.get("target_binding_receipt") is not None
    ):
        raise RuntimeRefreshError(
            "authority-intent-binding-invalid",
            "runtime-refresh intent authoritative TaskSpec binding is invalid",
        )
    return dict(value)


def bind_runtime_refresh_authority(
    *, store: Any, intent: dict[str, Any], now: datetime
) -> dict[str, Any]:
    expected = _intent_authority_record(intent)
    validate_authoritative_runtime_refresh_task(
        store=store,
        approval_task_id=expected["task_id"],
        target_sha256=expected["target_sha256"],
        expected_revision=expected["revision"],
        expected_spec_sha256=expected["spec_sha256"],
    )
    current = _read_authority_task(store, expected["task_id"])
    spec = json.loads(json.dumps(current["spec"]))
    metadata, authority = _runtime_authority_metadata(spec)
    binding = {
        "schema_version": RUNTIME_AUTHORITY_SCHEMA_VERSION,
        "kind": RUNTIME_AUTHORITY_BINDING_KIND,
        "task_id": expected["task_id"],
        "authority_revision": expected["revision"],
        "authority_spec_sha256": expected["spec_sha256"],
        "target_sha256": expected["target_sha256"],
        "intent_sha256": intent["intent_sha256"],
        "bound_at": isoformat(now),
    }
    authority["target_binding_receipt"] = binding
    metadata["runtime_refresh_authority"] = authority
    changed = _put_authority_task(
        store,
        spec,
        idempotency_key=f"runtime-refresh-bind:{expected['task_id']}:{intent['intent_sha256']}",
        expected_revision=expected["revision"],
        source="runtime-refresh-authority-target-binding",
    )
    readback = _read_authority_task(store, expected["task_id"])
    if readback.get("revision") != changed.get("revision") or readback.get(
        "spec_sha256"
    ) != changed.get("spec_sha256"):
        raise RuntimeRefreshError(
            "authority-target-binding-readback-failed",
            "target-bound TaskSpec readback differs from the CAS result",
        )
    _validated_authority_target_binding(
        readback["spec"]["metadata"]["runtime_refresh_authority"].get("target_binding_receipt")
    )
    return {
        "task_id": expected["task_id"],
        "revision": readback["revision"],
        "spec_sha256": readback["spec_sha256"],
        "authority_revision": expected["revision"],
        "authority_spec_sha256": expected["spec_sha256"],
        "target_binding_receipt": binding,
    }


def _validate_result_for_intent(result: dict[str, Any], intent: dict[str, Any]) -> dict[str, Any]:
    verify_digest(result, "result_sha256")
    if (
        result.get("schema_version") != SCHEMA_VERSION
        or result.get("kind") != "bureau_runtime_refresh_result"
        or result.get("intent_sha256") != intent.get("intent_sha256")
        or result.get("target_sha256") != intent.get("target_sha256")
        or result.get("main_commit") != intent.get("main_commit")
    ):
        raise RuntimeRefreshError(
            "result-intent-binding-invalid",
            "runtime-refresh result does not match the exact persisted intent",
        )
    return result


def consume_runtime_refresh_authority(
    *, store: Any, intent: dict[str, Any], result: dict[str, Any], now: datetime
) -> dict[str, Any]:
    """CAS-bind a successful immutable result to its single-use TaskSpec authority."""
    expected = _intent_authority_record(intent)
    _validate_result_for_intent(result, intent)
    if result.get("status") not in {"deployed", "already_current"}:
        raise RuntimeRefreshError(
            "authority-consumption-result-invalid",
            "only a successful terminal result can consume runtime authority",
            details={"status": result.get("status")},
        )
    expected_consumption = {
        "schema_version": RUNTIME_AUTHORITY_SCHEMA_VERSION,
        "kind": RUNTIME_AUTHORITY_CONSUMPTION_KIND,
        "status": "consumed",
        "task_id": expected["task_id"],
        "authority_revision": expected["revision"],
        "authority_spec_sha256": expected["spec_sha256"],
        "target_sha256": intent["target_sha256"],
        "intent_sha256": intent["intent_sha256"],
        "result_sha256": result["result_sha256"],
        "consumed_at": isoformat(now),
    }
    current = _read_authority_task(store, expected["task_id"])
    spec = current["spec"]
    _metadata, authority = _runtime_authority_metadata(spec)
    existing_value = authority.get("consumption")
    if existing_value is not None:
        existing = _validated_authority_consumption(existing_value)
        comparable = dict(expected_consumption)
        comparable["consumed_at"] = existing["consumed_at"]
        if existing != comparable:
            raise RuntimeRefreshError(
                "authority-consumption-mismatch",
                "single-use runtime authority was consumed by another result or target",
                details={"expected": comparable, "observed": existing},
            )
        return {
            "task_id": expected["task_id"],
            "revision": current["revision"],
            "spec_sha256": current["spec_sha256"],
            "consumption": existing,
            "idempotent_replay": True,
        }
    bound_authority = result.get("authority_task_spec")
    if not isinstance(bound_authority, dict):
        raise RuntimeRefreshError(
            "authority-consumption-binding-invalid",
            "successful result has no post-binding TaskSpec authority record",
        )
    bound_binding = _validated_authority_target_binding(
        bound_authority.get("target_binding_receipt")
    )
    if (
        bound_authority.get("task_id") != expected["task_id"]
        or not isinstance(bound_authority.get("revision"), int)
        or isinstance(bound_authority.get("revision"), bool)
        or bound_authority["revision"] < 1
        or not _is_sha256(bound_authority.get("spec_sha256"))
        or bound_authority.get("authority_revision") != expected["revision"]
        or bound_authority.get("authority_spec_sha256") != expected["spec_sha256"]
        or bound_binding["task_id"] != expected["task_id"]
        or bound_binding["target_sha256"] != intent["target_sha256"]
        or bound_binding["intent_sha256"] != intent["intent_sha256"]
    ):
        raise RuntimeRefreshError(
            "authority-consumption-binding-invalid",
            "successful result TaskSpec binding differs from the original authority intent",
        )
    if (
        current.get("revision") != bound_authority["revision"]
        or current.get("spec_sha256") != bound_authority["spec_sha256"]
    ):
        raise RuntimeRefreshError(
            "authority-task-drift-after-effect",
            "authoritative TaskSpec changed after target binding and before consumption",
            details={
                "expected_revision": bound_authority["revision"],
                "observed_revision": current.get("revision"),
                "expected_spec_sha256": bound_authority["spec_sha256"],
                "observed_spec_sha256": current.get("spec_sha256"),
            },
        )
    validate_authoritative_runtime_refresh_task(
        store=store,
        approval_task_id=expected["task_id"],
        target_sha256=intent["target_sha256"],
        expected_revision=bound_authority["revision"],
        expected_spec_sha256=bound_authority["spec_sha256"],
        expected_intent_sha256=intent["intent_sha256"],
        allow_bound_intent=True,
    )
    binding = _validated_authority_target_binding(authority.get("target_binding_receipt"))
    if (
        binding["task_id"] != expected["task_id"]
        or binding["authority_revision"] != expected["revision"]
        or binding["authority_spec_sha256"] != expected["spec_sha256"]
        or binding["target_sha256"] != intent["target_sha256"]
        or binding["intent_sha256"] != intent["intent_sha256"]
    ):
        raise RuntimeRefreshError(
            "authority-target-binding-mismatch",
            "TaskSpec target binding differs from the successful result",
        )
    mutated = json.loads(json.dumps(spec))
    mutated_authority = mutated["metadata"]["runtime_refresh_authority"]
    mutated_authority["consumption"] = expected_consumption
    changed = _put_authority_task(
        store,
        mutated,
        idempotency_key=(
            f"runtime-refresh-consume:{expected['task_id']}:{result['result_sha256']}"
        ),
        expected_revision=current["revision"],
        source="runtime-refresh-authority-consumption",
    )
    readback = _read_authority_task(store, expected["task_id"])
    observed = _validated_authority_consumption(
        readback["spec"]["metadata"]["runtime_refresh_authority"].get("consumption")
    )
    if (
        readback.get("revision") != changed.get("revision")
        or readback.get("spec_sha256") != changed.get("spec_sha256")
        or observed != expected_consumption
    ):
        raise RuntimeRefreshError(
            "authority-consumption-readback-failed",
            "consumed TaskSpec readback differs from the CAS result",
        )
    return {
        "task_id": expected["task_id"],
        "revision": readback["revision"],
        "spec_sha256": readback["spec_sha256"],
        "consumption": observed,
        "idempotent_replay": False,
    }


def prepare_intent(
    *,
    candidate: dict[str, Any],
    state_root: Path,
    prefix: Path,
    bin_dir: Path,
    user_unit_dir: Path = DEFAULT_USER_SYSTEMD_UNIT_ROOT,
    libexec_dir: Path = DEFAULT_RUNTIME_LIBEXEC_ROOT,
    runtime_user_unit_dir: Path | None = None,
    remote_url: str,
    authorized_by: str,
    authorization: str,
    break_glass: bool = False,
    approval_reference: str = "",
    approval_task_id: str = "",
    ttl_seconds: int = DEFAULT_INTENT_TTL_SECONDS,
    now: datetime | None = None,
    authority_store: Any | None = None,
    observer: Callable[..., dict[str, Any]] = observe_runtime_refresh,
) -> tuple[dict[str, Any], Path]:
    verify_digest(candidate, "observation_sha256")
    if candidate.get("status") not in {"candidate", "alert"}:
        raise RuntimeRefreshError(
            "candidate-not-deployable", f"candidate status is {candidate.get('status')!r}"
        )
    if not authorized_by.strip() or len(authorization.strip()) < 8:
        raise RuntimeRefreshError("authorization-missing", "explicit authorization is required")
    if not approval_reference.strip() or not approval_task_id.strip():
        raise RuntimeRefreshError(
            "runtime-approval-binding-missing",
            "runtime approval requires exact reference and task bindings",
        )
    evidence = approval.break_glass_approval(
        source=authorization.strip(),
        approved=break_glass,
        reviewer=authorized_by.strip(),
        reference=approval_reference.strip(),
        task_id=approval_task_id.strip(),
        scope=("runtime_mutation",),
        note="Bureau immutable runtime refresh",
    )
    try:
        runtime_approval = approval.require_approval(
            "runtime_mutation",
            evidence,
            expected_reference=candidate["target_sha256"],
            task_id=approval_task_id.strip(),
        )
    except legacy.StateError as exc:
        raise RuntimeRefreshError(
            "runtime-approval-required",
            str(exc),
        ) from exc
    if ttl_seconds <= 0 or ttl_seconds > 3600:
        raise RuntimeRefreshError(
            "intent-ttl-invalid", "intent TTL must be between 1 and 3600 seconds"
        )
    current = now or utc_now()
    resolved_user_unit_dir = user_unit_dir.expanduser().resolve()
    resolved_libexec_dir = libexec_dir.expanduser().resolve()
    resolved_runtime_user_unit_dir = (
        default_runtime_user_unit_dir()
        if runtime_user_unit_dir is None
        else runtime_user_unit_dir.expanduser().resolve()
    )
    store = authority_store or _default_read_only_authority_store()
    authority_task_spec = validate_authoritative_runtime_refresh_task(
        store=store,
        approval_task_id=approval_task_id.strip(),
        target_sha256=candidate["target_sha256"],
    )
    current_authority = _read_authority_task(store, approval_task_id.strip())
    _metadata, authority_contract = _runtime_authority_metadata(current_authority["spec"])
    source_precondition = _validated_runtime_source_precondition(
        authority_contract.get("source_precondition")
    )
    if source_precondition is not None:
        fresh_candidate = observer(
            repository=candidate["repository"],
            manifest_path=prefix.expanduser().resolve() / "deployment-manifest.json",
            required_checks=tuple(candidate["required_checks"]),
            now=current,
        )
        verify_digest(fresh_candidate, "observation_sha256")
        if fresh_candidate.get("status") not in {"candidate", "alert"}:
            raise RuntimeRefreshError(
                "source-precondition-fresh-candidate-blocked",
                "fresh runtime-refresh observation is not deployable before intent creation",
                details={
                    "status": fresh_candidate.get("status"),
                    "reason_codes": fresh_candidate.get("reason_codes"),
                },
            )
        _validate_candidate_source_precondition(fresh_candidate, source_precondition)
        if (
            fresh_candidate.get("main_commit") != candidate.get("main_commit")
            or fresh_candidate.get("target_sha256") != candidate.get("target_sha256")
        ):
            raise RuntimeRefreshError(
                "source-precondition-target-drift",
                "fresh source-precondition observation differs from the reviewed deployment target",
                details={
                    "candidate_main_commit": candidate.get("main_commit"),
                    "fresh_main_commit": fresh_candidate.get("main_commit"),
                    "candidate_target_sha256": candidate.get("target_sha256"),
                    "fresh_target_sha256": fresh_candidate.get("target_sha256"),
                },
            )
        candidate = fresh_candidate
    authority_state_store = _authority_store_binding(store)
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
        "user_unit_dir": str(resolved_user_unit_dir),
        "libexec_dir": str(resolved_libexec_dir),
        "runtime_user_unit_dir": str(resolved_runtime_user_unit_dir),
        "workspace": str(workspace),
        "required_resource_keys": required_resource_keys(
            state_root=state_root,
            prefix=prefix,
            bin_dir=bin_dir,
            workspace=workspace,
            user_unit_dir=resolved_user_unit_dir,
            libexec_dir=resolved_libexec_dir,
            runtime_user_unit_dir=resolved_runtime_user_unit_dir,
        ),
        "authorized_by": authorized_by.strip(),
        "authorization": authorization.strip(),
        "approval_task_id": approval_task_id.strip(),
        "runtime_approval": runtime_approval,
        "authority_state_store": authority_state_store,
        "authority_task_spec": authority_task_spec,
        "created_at": isoformat(current),
        "expires_at": isoformat(current + timedelta(seconds=ttl_seconds)),
        "nonce": uuid.uuid4().hex,
        "does_not_establish": [
            "external_lease_liveness",
            "merge_authority",
            "automatic_retry_authority",
        ],
    }
    if source_precondition is not None:
        intent["source_precondition"] = source_precondition
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


def validate_runtime_approval_intent(
    intent_path: Path,
    *,
    expected_source_commit: str | None = None,
    now: datetime | None = None,
    minimum_remaining_seconds: int = 0,
) -> dict[str, Any]:
    """Re-evaluate persisted, time-bounded break-glass evidence before effects."""
    current = now or utc_now()
    intent = read_json(intent_path)
    verify_digest(intent, "intent_sha256")
    if intent.get("kind") != "bureau_runtime_refresh_intent":
        raise RuntimeRefreshError("intent-kind-invalid", "runtime-refresh intent kind is invalid")
    expires_at = intent.get("expires_at")
    if not isinstance(expires_at, str):
        raise RuntimeRefreshError(
            "intent-expiry-missing",
            "runtime-refresh intent has no expiry binding",
        )
    try:
        expires = parse_time(expires_at)
    except (TypeError, ValueError) as exc:
        raise RuntimeRefreshError(
            "intent-expiry-invalid",
            "runtime-refresh intent expiry is invalid",
        ) from exc
    if expires <= current:
        raise RuntimeRefreshError("intent-expired", "runtime-refresh intent has expired")
    if (
        not isinstance(minimum_remaining_seconds, int)
        or isinstance(minimum_remaining_seconds, bool)
        or minimum_remaining_seconds < 0
    ):
        raise RuntimeRefreshError(
            "runtime-approval-minimum-invalid",
            "minimum runtime approval lifetime must be a non-negative integer",
        )
    remaining_seconds = int((expires - current).total_seconds())
    if remaining_seconds < minimum_remaining_seconds:
        raise RuntimeRefreshError(
            "runtime-approval-validity-too-short",
            "runtime approval expires too soon to start a deployment attempt",
            details={
                "remaining_seconds": remaining_seconds,
                "minimum_remaining_seconds": minimum_remaining_seconds,
            },
        )
    if expected_source_commit is not None and intent.get("main_commit") != expected_source_commit:
        raise RuntimeRefreshError(
            "approval-source-commit-mismatch",
            "runtime approval intent is bound to another source commit",
        )
    stored = intent.get("runtime_approval")
    raw = stored.get("evidence") if isinstance(stored, dict) else None
    if not isinstance(raw, dict):
        raise RuntimeRefreshError(
            "runtime-approval-missing",
            "runtime-refresh intent has no typed approval evidence",
        )
    scope = raw.get("scope", ())
    if not isinstance(scope, list) or not all(isinstance(item, str) for item in scope):
        raise RuntimeRefreshError("runtime-approval-invalid", "approval scope is invalid")
    required = {"source", "level", "approved", "reviewer", "reference", "task_id"}
    if not required <= raw.keys():
        raise RuntimeRefreshError("runtime-approval-invalid", "approval evidence is incomplete")
    evidence = approval.ApprovalEvidence(
        source=str(raw["source"]),
        level=str(raw["level"]),
        approved=raw["approved"] is True,
        reviewer=str(raw["reviewer"]),
        reference=str(raw["reference"]),
        task_id=str(raw["task_id"]),
        scope=tuple(scope),
        note=str(raw["note"]) if isinstance(raw.get("note"), str) else None,
    )
    try:
        decision = approval.require_approval(
            "runtime_mutation",
            evidence,
            expected_reference=str(intent.get("target_sha256", "")),
            task_id=str(intent.get("approval_task_id", "")),
        )
    except legacy.StateError as exc:
        raise RuntimeRefreshError("runtime-approval-required", str(exc)) from exc
    if decision != stored:
        raise RuntimeRefreshError(
            "runtime-approval-drift",
            "stored runtime approval decision differs from current policy",
        )
    return decision


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
        metadata_rows = connection.execute(
            "SELECT key, value FROM metadata WHERE key IN ('schema_version', ?) ORDER BY key",
            (GRABOWSKI_RESOURCE_LEASE_CONTRACT_METADATA_KEY,),
        ).fetchall()
        aggregate_rows = [row for row in metadata_rows if row["key"] == "schema_version"]
        contract_rows = [
            row
            for row in metadata_rows
            if row["key"] == GRABOWSKI_RESOURCE_LEASE_CONTRACT_METADATA_KEY
        ]
        if len(aggregate_rows) != 1:
            raise RuntimeRefreshError(
                (
                    "lease-database-schema-metadata-missing"
                    if not aggregate_rows
                    else "lease-database-schema-metadata-ambiguous"
                ),
                "Grabowski aggregate resource schema metadata is missing or ambiguous",
                details={
                    "observed_rows": len(aggregate_rows),
                    "recovery": (
                        "Keep the resource store unchanged and restore one valid "
                        "aggregate schema metadata row before retrying."
                    ),
                },
            )
        observed_schema = aggregate_rows[0]["value"]
        if (
            not isinstance(observed_schema, str)
            or not observed_schema
            or len(observed_schema.encode("utf-8")) > 32
            or not observed_schema.isdecimal()
        ):
            raise RuntimeRefreshError(
                "lease-database-schema-version-malformed",
                "Grabowski aggregate resource schema version is malformed",
                details={
                    "recovery": (
                        "Keep the resource store unchanged and restore valid aggregate "
                        "schema metadata before retrying."
                    ),
                },
            )
        if len(contract_rows) != 1:
            raise RuntimeRefreshError(
                (
                    "lease-contract-metadata-missing"
                    if not contract_rows
                    else "lease-contract-metadata-ambiguous"
                ),
                "Grabowski resource lease contract metadata is missing or ambiguous",
                details={
                    "metadata_key": GRABOWSKI_RESOURCE_LEASE_CONTRACT_METADATA_KEY,
                    "observed_rows": len(contract_rows),
                    "aggregate_schema": observed_schema,
                    "recovery": (
                        "Open the resource store once with a Grabowski runtime that "
                        "publishes the supported lease contract, then retry with new "
                        "exact leases."
                    ),
                },
            )
        observed_contract = contract_rows[0]["value"]
        if (
            not isinstance(observed_contract, str)
            or not observed_contract
            or len(observed_contract.encode("utf-8")) > 32
            or not observed_contract.isdecimal()
        ):
            raise RuntimeRefreshError(
                "lease-contract-version-malformed",
                "Grabowski resource lease contract version is malformed",
                details={
                    "metadata_key": GRABOWSKI_RESOURCE_LEASE_CONTRACT_METADATA_KEY,
                    "aggregate_schema": observed_schema,
                    "recovery": (
                        "Keep the resource store unchanged and restore verified "
                        "contract metadata or deploy a compatible Grabowski runtime."
                    ),
                },
            )
        if observed_contract not in SUPPORTED_GRABOWSKI_RESOURCE_LEASE_CONTRACTS:
            raise RuntimeRefreshError(
                "lease-contract-version-unsupported",
                "Grabowski resource lease contract version is unsupported",
                details={
                    "observed": observed_contract,
                    "supported": sorted(SUPPORTED_GRABOWSKI_RESOURCE_LEASE_CONTRACTS),
                    "aggregate_schema": observed_schema,
                    "recovery": (
                        "Keep the resource store and runtime unchanged; deploy a "
                        "Bureau runtime that explicitly supports this lease contract."
                    ),
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
        "resource_lease_contract_version": observed_contract,
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


def validate_legacy_runtime_refresh_bootstrap(
    *,
    state_root: Path,
    resource_db: Path,
    expected_source_commit: str,
    prefix: Path,
    bin_dir: Path,
    manifest_path: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Authorize only the one-time cutover from the pre-approval refresh runner.

    The previous immutable runner cannot pass the new approval-intent argument.
    Compatibility is therefore limited to one still-live legacy intent whose
    durable attempt start, installed preimage and exact Grabowski leases all agree.
    Typed intents are never accepted through this path.
    """
    current = now or utc_now()
    raw_root = state_root.expanduser()
    if raw_root.is_symlink():
        raise RuntimeRefreshError(
            "legacy-cutover-state-symlink",
            "legacy runtime-refresh state root may not be a symlink",
        )
    root = raw_root.resolve()
    resolved_prefix = _historical_nonsymlink_path(prefix, label="runtime prefix")
    resolved_bin_dir = _historical_nonsymlink_path(bin_dir, label="runtime bin directory")
    intents_root = root / "intents"
    if root.is_symlink() or not intents_root.is_dir() or intents_root.is_symlink():
        raise RuntimeRefreshError(
            "legacy-cutover-state-unavailable",
            "legacy runtime-refresh state is unavailable",
        )
    entries = sorted(intents_root.iterdir())
    if len(entries) > 1024:
        raise RuntimeRefreshError(
            "legacy-cutover-intent-set-too-large",
            "legacy runtime-refresh intent set exceeds the bounded cutover scan",
        )
    deployed_manifest, deployed_manifest_sha = load_manifest(manifest_path)
    matches: list[dict[str, Any]] = []
    for intent_path in entries:
        if intent_path.is_symlink() or not intent_path.is_file():
            continue
        try:
            intent = read_json(intent_path)
            verify_digest(intent, "intent_sha256")
        except RuntimeRefreshError:
            continue
        expires_at = intent.get("expires_at")
        if not isinstance(expires_at, str):
            continue
        try:
            expires = parse_time(expires_at)
        except RuntimeRefreshError:
            continue
        if intent_path.stem != intent.get("intent_sha256"):
            continue
        if "runtime_approval" in intent or "approval_task_id" in intent:
            continue
        if intent.get("kind") != "bureau_runtime_refresh_intent":
            continue
        if intent.get("main_commit") != expected_source_commit:
            continue
        if expires <= current + timedelta(seconds=DEFAULT_MIN_LEGACY_CUTOVER_REMAINING_SECONDS):
            continue
        if (
            not isinstance(intent.get("authorized_by"), str)
            or not intent["authorized_by"].strip()
            or not isinstance(intent.get("authorization"), str)
            or len(intent["authorization"].strip()) < 8
        ):
            continue
        raw_state_root = intent.get("state_root")
        raw_prefix = intent.get("prefix")
        raw_bin_dir = intent.get("bin_dir")
        raw_paths = (raw_state_root, raw_prefix, raw_bin_dir)
        if not all(isinstance(item, str) and item for item in raw_paths):
            continue
        if Path(raw_state_root).expanduser().resolve() != root:
            continue
        if Path(raw_prefix).expanduser().resolve() != resolved_prefix:
            continue
        if Path(raw_bin_dir).expanduser().resolve() != resolved_bin_dir:
            continue
        if (
            intent.get("expected_deployed_source_commit") != deployed_manifest.get("source_commit")
            or intent.get("expected_manifest_sha256") != deployed_manifest_sha
        ):
            continue
        target_sha = intent.get("target_sha256")
        if (
            not isinstance(target_sha, str)
            or len(target_sha) != 64
            or any(char not in "0123456789abcdef" for char in target_sha)
        ):
            continue
        attempt_root = root / "attempts" / target_sha
        started_path = attempt_root / "started.json"
        result_path = attempt_root / "result.json"
        if result_path.exists() or not started_path.is_file() or started_path.is_symlink():
            continue
        try:
            started = read_json(started_path)
            verify_digest(started, "start_sha256")
        except RuntimeRefreshError:
            continue
        if (
            started.get("intent_sha256") != intent.get("intent_sha256")
            or started.get("target_sha256") != target_sha
            or started.get("main_commit") != expected_source_commit
            or started.get("effect_started") is not False
        ):
            continue
        stored_binding = started.get("lease_binding")
        if not isinstance(stored_binding, dict):
            continue
        owner_id = stored_binding.get("owner_id")
        task_id = stored_binding.get("task_id")
        if (
            not isinstance(owner_id, str)
            or not owner_id.strip()
            or not isinstance(task_id, str)
            or not task_id.strip()
        ):
            continue
        try:
            live_binding = validate_live_lease_binding(
                intent,
                {"owner_id": owner_id, "task_id": task_id},
                resource_db=resource_db,
                now=current,
            )
        except RuntimeRefreshError:
            continue
        if (
            stored_binding.get("owner_id") != live_binding["owner_id"]
            or stored_binding.get("task_id") != live_binding["task_id"]
            or stored_binding.get("resource_keys") != live_binding["resource_keys"]
            or stored_binding.get("required_metadata_sha256")
            != live_binding["required_metadata_sha256"]
        ):
            continue
        stored_snapshots = stored_binding.get("lease_snapshots")
        live_snapshots = live_binding.get("lease_snapshots")
        if (
            not isinstance(stored_snapshots, list)
            or not isinstance(live_snapshots, list)
            or not all(isinstance(item, dict) for item in stored_snapshots)
            or not all(isinstance(item, dict) for item in live_snapshots)
        ):
            continue
        stored_by_key = {item.get("resource_key"): item for item in stored_snapshots}
        live_by_key = {item.get("resource_key"): item for item in live_snapshots}
        if set(stored_by_key) != set(live_by_key):
            continue
        same_lease_lineage = all(
            stored_by_key[key].get("owner_id") == live_by_key[key].get("owner_id")
            and stored_by_key[key].get("acquired_at_unix")
            == live_by_key[key].get("acquired_at_unix")
            and stored_by_key[key].get("metadata_sha256") == live_by_key[key].get("metadata_sha256")
            and isinstance(stored_by_key[key].get("expires_at_unix"), int)
            and isinstance(live_by_key[key].get("expires_at_unix"), int)
            and live_by_key[key]["expires_at_unix"] >= stored_by_key[key]["expires_at_unix"]
            for key in stored_by_key
        )
        if not same_lease_lineage:
            continue
        matches.append(
            {
                "intent": intent,
                "started": started,
                "live_binding": live_binding,
            }
        )
    if not matches:
        raise RuntimeRefreshError(
            "runtime-approval-missing",
            "no active legacy runtime-refresh cutover satisfies the operator gate",
        )
    if len(matches) != 1:
        raise RuntimeRefreshError(
            "legacy-cutover-ambiguous",
            "more than one active legacy runtime-refresh cutover matches the target",
            details={"match_count": len(matches)},
        )
    match = matches[0]
    intent = match["intent"]
    started = match["started"]
    live_binding = match["live_binding"]
    return {
        "schema_version": SCHEMA_VERSION,
        "action_class": "runtime_mutation",
        "action_classes": ["runtime_mutation"],
        "required": True,
        "required_level": "legacy_runtime_operator_gate",
        "allowed": True,
        "reason": "approved by an active legacy refresh attempt and exact live leases",
        "expected_reference": intent["target_sha256"],
        "expected_task_id": live_binding["task_id"],
        "evidence": {
            "source": "legacy-runtime-refresh-cutover",
            "level": "legacy_runtime_operator_gate",
            "approved": True,
            "reviewer": intent["authorized_by"],
            "reference": intent["target_sha256"],
            "task_id": live_binding["task_id"],
            "scope": ["runtime_mutation"],
            "note": "one-time compatibility path for the pre-approval immutable runner",
        },
        "legacy_cutover": {
            "intent_sha256": intent["intent_sha256"],
            "start_sha256": started["start_sha256"],
            "lease_binding_sha256": live_binding["lease_binding_sha256"],
            "expected_source_commit": expected_source_commit,
        },
    }


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
    *,
    source: Path,
    prefix: Path,
    bin_dir: Path,
    user_unit_dir: Path,
    libexec_dir: Path,
    runtime_user_unit_dir: Path,
    approval_intent: Path,
    allowed_launcher_paths: Iterable[Path] = (),
    timeout: float = 300,
) -> dict[str, Any]:
    validate_scheduler_runtime_layout(prefix=prefix, bin_dir=bin_dir)
    validate_runtime_user_unit_dir(runtime_user_unit_dir)
    argv = [
        sys.executable,
        str(source / "ops/install-bureau-runtime.py"),
        "--source",
        str(source),
        "--prefix",
        str(prefix),
        "--bin-dir",
        str(bin_dir),
        "--user-unit-dir",
        str(user_unit_dir),
        "--libexec-dir",
        str(libexec_dir),
        "--runtime-user-unit-dir",
        str(runtime_user_unit_dir),
        "--approval-intent",
        str(approval_intent),
        "--replace-existing",
        "--enforce-launcher-allowlist",
        "--converge-user-systemd",
    ]
    for path in sorted({str(item) for item in allowed_launcher_paths}):
        argv.extend(["--allowed-launcher-path", path])
    result = _run(argv, cwd=source, timeout=timeout)
    if result.returncode:
        try:
            structured_error = json.loads(result.stderr.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            structured_error = None
        if (
            isinstance(structured_error, dict)
            and structured_error.get("kind") == "bureau_runtime_install_error"
            and isinstance(structured_error.get("error"), dict)
        ):
            error = structured_error["error"]
            if isinstance(error.get("code"), str) and isinstance(error.get("message"), str):
                raise RuntimeRefreshError(
                    error["code"],
                    error["message"],
                    details=error.get("details")
                    if isinstance(error.get("details"), dict)
                    else {},
                )
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
    scheduler_receipt = install_receipt.get("scheduler")
    if (
        not isinstance(scheduler_receipt, dict)
        or manifest.get("scheduler") != scheduler_receipt
    ):
        raise RuntimeRefreshError(
            "scheduler-manifest-receipt-mismatch",
            "scheduler deployment is missing, malformed, or differs between manifest "
            "and installer receipt",
        )
    bureau_launcher = bin_dir / "bureau"
    refresh_launcher = bin_dir / "bureau-runtime-refresh"
    status_capsule_launcher = bin_dir / "bureau-status-capsule"
    for path, receipt_field in (
        (bureau_launcher, "launcher_sha256"),
        (refresh_launcher, "runtime_refresh_launcher_sha256"),
        (status_capsule_launcher, "status_capsule_launcher_sha256"),
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
    scheduler_readback = readback_user_scheduler(
        scheduler_receipt,
        expected_commit=expected_commit,
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
        "status_capsule_launcher_sha256": sha256_bytes(status_capsule_launcher.read_bytes()),
        "check_valid": True,
        "runtime_identity_valid": True,
        "scheduler": scheduler_readback,
        "rollback": install_receipt.get("rollback"),
    }


def _historical_nonsymlink_path(path: Path, *, label: str) -> Path:
    """Normalize one absolute historical path while rejecting symlink components."""
    raw = path.expanduser()
    normalized = Path(os.path.abspath(raw))
    current = normalized
    while True:
        if current.is_symlink():
            raise RuntimeRefreshError(
                "historical-runtime-path-symlink",
                f"historical runtime {label} path may not contain a symlink",
                details={"path": str(current)},
            )
        if current == current.parent:
            break
        current = current.parent
    return normalized


def _historical_artifact_sha256(path: Path, *, label: str) -> str | None:
    """Hash one bounded regular historical artifact without following symlinks."""
    try:
        if path.is_symlink() or not path.is_file():
            return None
        if path.stat().st_size > MAX_HISTORICAL_RUNTIME_ARTIFACT_BYTES:
            raise RuntimeRefreshError(
                "historical-runtime-artifact-too-large",
                f"historical runtime {label} exceeds the bounded read limit",
                details={"path": str(path), "limit": MAX_HISTORICAL_RUNTIME_ARTIFACT_BYTES},
            )
        return sha256_bytes(path.read_bytes())
    except RuntimeRefreshError:
        raise
    except OSError as exc:
        raise RuntimeRefreshError(
            "historical-runtime-artifact-unreadable",
            f"cannot read historical runtime {label}",
            details={"path": str(path), "error": str(exc)},
        ) from exc


def _validate_historical_install_receipt(
    *, prefix: Path, install_receipt: dict[str, Any]
) -> dict[str, Any]:
    if (
        install_receipt.get("schema_version") != SCHEMA_VERSION
        or install_receipt.get("kind") != "bureau_runtime_install_receipt"
    ):
        raise RuntimeRefreshError(
            "historical-install-receipt-invalid",
            "historical runtime result has no supported installer receipt",
        )
    receipt_path_value = install_receipt.get("receipt_path")
    release_id = install_receipt.get("release_id")
    manifest_sha256 = install_receipt.get("manifest_sha256")
    if (
        not isinstance(receipt_path_value, str)
        or not isinstance(release_id, str)
        or not release_id
        or not isinstance(manifest_sha256, str)
        or not _is_sha256(manifest_sha256)
    ):
        raise RuntimeRefreshError(
            "historical-install-receipt-invalid",
            "historical installer receipt identity is incomplete",
        )
    receipts_root = _historical_nonsymlink_path(prefix / "receipts", label="receipt root")
    receipt_path = _historical_nonsymlink_path(
        Path(receipt_path_value), label="install receipt"
    )
    try:
        receipt_path.relative_to(receipts_root)
    except ValueError as exc:
        raise RuntimeRefreshError(
            "historical-install-receipt-path-invalid",
            "historical installer receipt escaped the runtime receipt root",
            details={"path": str(receipt_path), "root": str(receipts_root)},
        ) from exc
    expected_name = f"{release_id}-{manifest_sha256[:12]}.json"
    if receipt_path.name != expected_name:
        raise RuntimeRefreshError(
            "historical-install-receipt-path-invalid",
            "historical installer receipt filename does not match its bound identity",
            details={"expected": expected_name, "observed": receipt_path.name},
        )
    persisted = read_json(receipt_path)
    expected = dict(install_receipt)
    expected.pop("receipt_path", None)
    if persisted != expected or receipt_path.read_bytes() != canonical_bytes(persisted):
        raise RuntimeRefreshError(
            "historical-install-receipt-mismatch",
            "persisted installer receipt differs from the result-bound receipt",
            details={"path": str(receipt_path)},
        )
    return persisted


def _historical_runtime_snapshot(
    *, prefix: Path, bin_dir: Path, install_receipt: dict[str, Any]
) -> tuple[Path, tuple[Path, Path, Path]]:
    expected_manifest_sha256 = install_receipt.get("manifest_sha256")
    launcher_contract = (
        ("bureau", "launcher_sha256"),
        ("bureau-runtime-refresh", "runtime_refresh_launcher_sha256"),
        ("bureau-status-capsule", "status_capsule_launcher_sha256"),
    )
    if not isinstance(expected_manifest_sha256, str) or not _is_sha256(expected_manifest_sha256):
        raise RuntimeRefreshError(
            "historical-install-receipt-invalid",
            "historical installer receipt manifest digest is invalid",
        )
    for _, field in launcher_contract:
        value = install_receipt.get(field)
        if not isinstance(value, str) or not _is_sha256(value):
            raise RuntimeRefreshError(
                "historical-install-receipt-invalid",
                f"historical installer receipt {field} is invalid",
            )

    manifest_matches = 0

    def matches(
        manifest_path: Path, launcher_paths: tuple[Path, Path, Path]
    ) -> bool:
        nonlocal manifest_matches
        if (
            _historical_artifact_sha256(manifest_path, label="manifest")
            != expected_manifest_sha256
        ):
            return False
        manifest_matches += 1
        return all(
            _historical_artifact_sha256(path, label=name) == install_receipt[field]
            for (name, field), path in zip(launcher_contract, launcher_paths, strict=True)
        )

    current_manifest = prefix / "deployment-manifest.json"
    current_launchers = tuple(bin_dir / name for name, _ in launcher_contract)
    if matches(current_manifest, current_launchers):
        return current_manifest, current_launchers

    backups_root = prefix / "backups"
    backup_directories: list[Path] = []
    if backups_root.exists():
        if backups_root.is_symlink() or not backups_root.is_dir():
            raise RuntimeRefreshError(
                "historical-runtime-backup-root-invalid",
                "historical runtime backup root is not a regular directory",
                details={"path": str(backups_root)},
            )
        try:
            backup_directories = sorted(
                path
                for path in backups_root.iterdir()
                if path.is_dir() and not path.is_symlink()
            )
        except OSError as exc:
            raise RuntimeRefreshError(
                "historical-runtime-backup-root-unreadable",
                "historical runtime backup root cannot be listed",
                details={"path": str(backups_root), "error": str(exc)},
            ) from exc
        if len(backup_directories) > MAX_RUNTIME_BACKUP_ARTIFACT_DIRS:
            raise RuntimeRefreshError(
                "historical-runtime-backup-scan-too-large",
                "historical runtime backup scan exceeds the bounded limit",
                details={
                    "count": len(backup_directories),
                    "limit": MAX_RUNTIME_BACKUP_ARTIFACT_DIRS,
                },
            )
    for directory in backup_directories:
        manifest_path = directory / "deployment-manifest.json"
        launcher_paths = tuple(directory / name for name, _ in launcher_contract)
        if matches(manifest_path, launcher_paths):
            return manifest_path, launcher_paths
    if manifest_matches:
        raise RuntimeRefreshError(
            "historical-runtime-launcher-evidence-unavailable",
            "historical manifest exists but matching launcher evidence is unavailable",
            details={"manifest_sha256": expected_manifest_sha256},
        )
    raise RuntimeRefreshError(
        "historical-runtime-manifest-evidence-unavailable",
        "result-bound historical deployment manifest is unavailable",
        details={"manifest_sha256": expected_manifest_sha256},
    )


def _historical_package_tree_constants(
    release: Path,
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """Parse package-tree constants from an immutable release without executing it."""
    module = release / "src/bureau/runtime_identity.py"
    try:
        if module.is_symlink() or not module.is_file():
            return None
        if module.stat().st_size > MAX_HISTORICAL_RUNTIME_ARTIFACT_BYTES:
            return None
        source = module.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(module))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return None

    values: dict[str, tuple[str, ...]] = {}
    required = {"MANAGED_PACKAGES", "SCHEDULER_NAMES"}
    for statement in tree.body:
        target: ast.expr | None = None
        value_node: ast.expr | None = None
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target = statement.targets[0]
            value_node = statement.value
        elif isinstance(statement, ast.AnnAssign):
            target = statement.target
            value_node = statement.value
        if not isinstance(target, ast.Name) or target.id not in required or value_node is None:
            continue
        if target.id in values:
            return None
        if not isinstance(value_node, ast.Tuple):
            return None
        value_items: list[str] = []
        for item in value_node.elts:
            if (
                not isinstance(item, ast.Constant)
                or not isinstance(item.value, str)
                or not item.value
            ):
                return None
            value_items.append(item.value)
        value = tuple(value_items)
        if not value or len(set(value)) != len(value):
            return None
        values[target.id] = value

    if set(values) != required:
        return None
    packages = values["MANAGED_PACKAGES"]
    scheduler_names = values["SCHEDULER_NAMES"]
    if "bureau" not in packages or not all(name.isidentifier() for name in packages):
        return None
    if not all(
        name not in {".", ".."}
        and len(name) <= 128
        and all(
            character.isascii() and (character.isalnum() or character in "-_.@")
            for character in name
        )
        for name in scheduler_names
    ):
        return None
    return packages, scheduler_names


def _historical_package_tree_sha256(release: Path) -> str | None:
    """Rebuild the legacy package-tree digest from release-bound data only."""
    constants = _historical_package_tree_constants(release)
    if constants is None:
        return None
    managed_packages, scheduler_names = constants
    pyproject = release / "pyproject.toml"
    packages = [release / "src" / name for name in managed_packages]
    schemas = release / "schemas"
    systemd = release / "ops/systemd"
    schema_paths = sorted(schemas.glob("*.json")) if schemas.is_dir() else []
    if (
        pyproject.is_symlink()
        or not pyproject.is_file()
        or schemas.is_symlink()
        or not schemas.is_dir()
        or not schema_paths
        or systemd.is_symlink()
        or not systemd.is_dir()
        or any(package.is_symlink() or not package.is_dir() for package in packages)
    ):
        return None
    paths = [
        pyproject,
        *schema_paths,
        *(path for package in packages for path in sorted(package.rglob("*.py"))),
        *(systemd / f"{name}.service" for name in scheduler_names),
        *(systemd / f"{name}.timer" for name in scheduler_names),
        *(systemd / "libexec" / name for name in scheduler_names),
    ]
    digest = hashlib.sha256()
    try:
        for path in paths:
            if path.is_symlink() or not path.is_file():
                return None
            relative = path.relative_to(release).as_posix().encode()
            content = path.read_bytes()
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            digest.update(b"x" if path.stat().st_mode & 0o111 else b"-")
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
    except (OSError, ValueError):
        return None
    return digest.hexdigest()


def readback_historical_install(
    *, expected_commit: str, prefix: Path, bin_dir: Path, install_receipt: dict[str, Any]
) -> dict[str, Any]:
    """Authenticate one result-bound historical install without consulting the current pointer."""
    resolved_prefix = prefix.expanduser().resolve()
    resolved_bin_dir = bin_dir.expanduser().resolve()
    persisted_receipt = _validate_historical_install_receipt(
        prefix=resolved_prefix, install_receipt=install_receipt
    )
    expected_manifest_path = _historical_nonsymlink_path(
        resolved_prefix / "deployment-manifest.json", label="manifest pointer"
    )
    expected_launcher_paths = {
        "launcher_path": _historical_nonsymlink_path(
            resolved_bin_dir / "bureau", label="bureau launcher pointer"
        ),
        "runtime_refresh_launcher_path": _historical_nonsymlink_path(
            resolved_bin_dir / "bureau-runtime-refresh",
            label="runtime refresh launcher pointer",
        ),
        "status_capsule_launcher_path": _historical_nonsymlink_path(
            resolved_bin_dir / "bureau-status-capsule",
            label="status capsule launcher pointer",
        ),
    }
    persisted_manifest_path = _historical_nonsymlink_path(
        Path(str(persisted_receipt.get("manifest_path", ""))),
        label="receipt manifest pointer",
    )
    if persisted_manifest_path != expected_manifest_path:
        raise RuntimeRefreshError(
            "historical-install-receipt-path-invalid",
            "historical installer receipt manifest path differs from the intent prefix",
        )
    for field, expected_path in expected_launcher_paths.items():
        observed_path = _historical_nonsymlink_path(
            Path(str(persisted_receipt.get(field, ""))), label=f"receipt {field}"
        )
        if observed_path != expected_path:
            raise RuntimeRefreshError(
                "historical-install-receipt-path-invalid",
                f"historical installer receipt {field} differs from the intent bin directory",
            )

    archived_manifest_path, archived_launchers = _historical_runtime_snapshot(
        prefix=resolved_prefix,
        bin_dir=resolved_bin_dir,
        install_receipt=install_receipt,
    )
    manifest, manifest_sha256 = load_manifest(archived_manifest_path)
    if manifest_sha256 != install_receipt["manifest_sha256"]:
        raise RuntimeRefreshError(
            "historical-runtime-manifest-mismatch",
            "historical deployment manifest digest differs from installer receipt",
        )
    if manifest.get("source_commit") != expected_commit:
        raise RuntimeRefreshError(
            "historical-runtime-source-mismatch",
            "historical deployment manifest source differs from the authority intent",
        )
    receipt_manifest_bindings = (
        ("release_id", "release_id"),
        ("package_tree_sha256", "package_tree_sha256"),
        ("canonical_registry_root", "canonical_registry_root"),
        ("canonical_registry_tree_sha256", "canonical_registry_tree_sha256"),
        ("launcher_path", "launcher_path"),
        ("runtime_refresh_launcher_path", "runtime_refresh_launcher_path"),
        ("status_capsule_launcher_path", "status_capsule_launcher_path"),
        ("installed_at", "installed_at"),
        ("runtime_approval", "runtime_approval"),
        ("rollback", "rollback"),
        ("scheduler", "scheduler"),
    )
    for manifest_field, receipt_field in receipt_manifest_bindings:
        if manifest.get(manifest_field) != persisted_receipt.get(receipt_field):
            raise RuntimeRefreshError(
                "historical-runtime-manifest-receipt-mismatch",
                "historical deployment manifest differs from installer receipt",
                details={"field": manifest_field},
            )

    release_id = manifest.get("release_id")
    if not isinstance(release_id, str) or not release_id:
        raise RuntimeRefreshError(
            "historical-runtime-release-invalid", "historical release id is invalid"
        )
    expected_release = _historical_nonsymlink_path(
        resolved_prefix / "releases" / release_id, label="expected immutable release"
    )
    release = _historical_nonsymlink_path(
        Path(str(manifest.get("immutable_release_path", ""))),
        label="immutable release",
    )
    if release != expected_release or not release.is_dir():
        raise RuntimeRefreshError(
            "historical-runtime-release-invalid",
            "historical immutable release path is invalid",
            details={"expected": str(expected_release), "observed": str(release)},
        )
    observed_package_sha256 = _historical_package_tree_sha256(release)
    if observed_package_sha256 != manifest.get("package_tree_sha256"):
        raise RuntimeRefreshError(
            "historical-runtime-package-mismatch",
            "historical immutable package tree digest mismatch",
            details={
                "expected": manifest.get("package_tree_sha256"),
                "observed": observed_package_sha256,
            },
        )
    module = _historical_nonsymlink_path(
        Path(str(manifest.get("module_path", ""))), label="runtime module"
    )
    try:
        module.relative_to(release)
    except ValueError as exc:
        raise RuntimeRefreshError(
            "historical-runtime-module-invalid",
            "historical runtime module escaped the immutable release",
        ) from exc
    observed_module_sha256 = _historical_artifact_sha256(module, label="runtime module")
    if observed_module_sha256 != manifest.get("module_sha256"):
        raise RuntimeRefreshError(
            "historical-runtime-module-mismatch",
            "historical runtime module digest mismatch",
        )
    _historical_nonsymlink_path(
        Path(str(manifest.get("canonical_registry_root", ""))),
        label="canonical Registry root",
    )
    _historical_nonsymlink_path(
        Path(str(manifest.get("canonical_registry_inventory_path", ""))),
        label="canonical Registry inventory",
    )
    registry_identity = registry_snapshot.canonical_registry_identity(manifest)
    if registry_identity.get("valid") is not True:
        raise RuntimeRefreshError(
            "historical-runtime-registry-snapshot-invalid",
            "historical canonical Registry snapshot failed immutable readback",
            details={"reasons": registry_identity.get("reasons", [])},
        )
    if registry_identity.get("source_commit") != expected_commit:
        raise RuntimeRefreshError(
            "historical-runtime-registry-source-mismatch",
            "historical Registry snapshot source differs from the authority intent",
        )

    launcher_fields = (
        "launcher_sha256",
        "runtime_refresh_launcher_sha256",
        "status_capsule_launcher_sha256",
    )
    launcher_sha256s = tuple(
        _historical_artifact_sha256(path, label=field)
        for path, field in zip(archived_launchers, launcher_fields, strict=True)
    )
    if launcher_sha256s != tuple(install_receipt[field] for field in launcher_fields):
        raise RuntimeRefreshError(
            "historical-runtime-launcher-mismatch",
            "historical launcher snapshot differs from installer receipt",
        )
    return {
        "manifest_path": str(expected_manifest_path),
        "manifest_sha256": manifest_sha256,
        "source_commit": expected_commit,
        "release_id": release_id,
        "package_tree_sha256": manifest.get("package_tree_sha256"),
        "registry_snapshot_tree_sha256": manifest.get("canonical_registry_tree_sha256"),
        "bureau_launcher_sha256": launcher_sha256s[0],
        "runtime_refresh_launcher_sha256": launcher_sha256s[1],
        "status_capsule_launcher_sha256": launcher_sha256s[2],
        "check_valid": True,
        "runtime_identity_valid": True,
        "rollback": install_receipt.get("rollback"),
    }


def _write_attempt_result(path: Path, value: dict[str, Any]) -> dict[str, Any]:
    result = bind_digest(value, "result_sha256")
    create_only(path, canonical_bytes(result))
    return result


def validate_released_lease_binding(
    *,
    intent: dict[str, Any],
    result: dict[str, Any],
    resource_db: Path = DEFAULT_GRABOWSKI_RESOURCE_DB,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Prove that no result-bound Grabowski lease remains in the live store."""
    binding = result.get("lease_binding")
    if not isinstance(binding, dict):
        raise RuntimeRefreshError(
            "authority-closeout-lease-binding-invalid",
            "deployed result has no canonical lease binding",
        )
    binding_digest = binding.get("lease_binding_sha256")
    material = dict(binding)
    material.pop("lease_binding_sha256", None)
    if not _is_sha256(binding_digest) or sha256_bytes(canonical_bytes(material)) != binding_digest:
        raise RuntimeRefreshError(
            "authority-closeout-lease-binding-invalid",
            "deployed result lease binding digest is invalid",
        )
    keys = binding.get("resource_keys")
    required = intent.get("required_resource_keys")
    if (
        not isinstance(keys, list)
        or keys != required
        or keys != sorted(set(keys))
        or not all(isinstance(item, str) for item in keys)
    ):
        raise RuntimeRefreshError(
            "authority-closeout-lease-binding-invalid",
            "result leases do not match the exact intent resources",
        )
    path = _validate_resource_database_path(resource_db)
    if binding.get("resource_db") != str(path):
        raise RuntimeRefreshError(
            "authority-closeout-lease-store-mismatch",
            "live Grabowski store differs from the result-bound lease store",
        )
    placeholders = ",".join("?" for _ in keys)
    try:
        connection = sqlite3.connect(
            f"file:{path}?mode=ro", uri=True, timeout=5, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        metadata_rows = connection.execute(
            "SELECT key,value FROM metadata WHERE key IN ('schema_version',?) ORDER BY key",
            (GRABOWSKI_RESOURCE_LEASE_CONTRACT_METADATA_KEY,),
        ).fetchall()
        rows = connection.execute(
            f"SELECT resource_key,owner_id FROM leases "
            f"WHERE resource_key IN ({placeholders}) ORDER BY resource_key",
            keys,
        ).fetchall()
        connection.commit()
    except sqlite3.Error as exc:
        raise RuntimeRefreshError(
            "authority-closeout-lease-read-failed",
            "Grabowski lease-release readback failed",
            details={"error": str(exc)},
        ) from exc
    finally:
        if "connection" in locals():
            connection.close()
    metadata = {str(row["key"]): str(row["value"]) for row in metadata_rows}
    if (
        len(metadata_rows) != 2
        or metadata.get(GRABOWSKI_RESOURCE_LEASE_CONTRACT_METADATA_KEY)
        not in SUPPORTED_GRABOWSKI_RESOURCE_LEASE_CONTRACTS
    ):
        raise RuntimeRefreshError(
            "authority-closeout-lease-contract-invalid",
            "Grabowski lease store does not expose the supported release-readback contract",
            details={"metadata": metadata},
        )
    if metadata.get("schema_version") != binding.get("resource_db_schema_version") or metadata.get(
        GRABOWSKI_RESOURCE_LEASE_CONTRACT_METADATA_KEY
    ) != binding.get("resource_lease_contract_version"):
        raise RuntimeRefreshError(
            "authority-closeout-lease-contract-drift",
            "Grabowski lease store contract drifted after the runtime result",
            details={
                "expected_schema": binding.get("resource_db_schema_version"),
                "observed_schema": metadata.get("schema_version"),
                "expected_contract": binding.get("resource_lease_contract_version"),
                "observed_contract": metadata.get(GRABOWSKI_RESOURCE_LEASE_CONTRACT_METADATA_KEY),
            },
        )
    if rows:
        raise RuntimeRefreshError(
            "authority-closeout-leases-not-released",
            "result-bound Grabowski leases are still present",
            details={"leases": [dict(row) for row in rows]},
        )
    evidence = {
        "schema_version": 1,
        "kind": "bureau_runtime_refresh_lease_release_readback",
        "lease_binding_sha256": binding_digest,
        "resource_keys": keys,
        "resource_db": str(path),
        "resource_db_schema_version": metadata.get("schema_version"),
        "resource_lease_contract_version": metadata.get(
            GRABOWSKI_RESOURCE_LEASE_CONTRACT_METADATA_KEY
        ),
        "observed_at": isoformat(now or utc_now()),
        "released": True,
    }
    evidence["lease_release_sha256"] = sha256_bytes(canonical_bytes(evidence))
    return evidence


def _validated_runtime_closeout(value: Any) -> dict[str, Any]:
    fields = {
        "schema_version",
        "kind",
        "status",
        "task_id",
        "authority_revision",
        "authority_spec_sha256",
        "target_sha256",
        "intent_sha256",
        "runtime_result_sha256",
        "source_commit",
        "manifest_sha256",
        "readback_sha256",
        "lease_binding_sha256",
        "lease_release_sha256",
        "closed_at",
        "does_not_establish",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise RuntimeRefreshError(
            "authority-closeout-record-invalid",
            "runtime authority closeout record is malformed",
        )
    digests = (
        "authority_spec_sha256",
        "target_sha256",
        "intent_sha256",
        "runtime_result_sha256",
        "manifest_sha256",
        "readback_sha256",
        "lease_binding_sha256",
        "lease_release_sha256",
    )
    if (
        value.get("schema_version") != RUNTIME_AUTHORITY_SCHEMA_VERSION
        or value.get("kind") != RUNTIME_AUTHORITY_CLOSEOUT_KIND
        or value.get("status") != "verified"
        or not isinstance(value.get("task_id"), str)
        or not isinstance(value.get("authority_revision"), int)
        or isinstance(value.get("authority_revision"), bool)
        or value["authority_revision"] < 1
        or any(not _is_sha256(value.get(field)) for field in digests)
        or not isinstance(value.get("source_commit"), str)
        or len(value["source_commit"]) != 40
        or not isinstance(value.get("closed_at"), str)
        or not isinstance(value.get("does_not_establish"), list)
        or not all(isinstance(item, str) for item in value["does_not_establish"])
    ):
        raise RuntimeRefreshError(
            "authority-closeout-record-invalid",
            "runtime authority closeout record values are invalid",
        )
    parse_time(value["closed_at"])
    return dict(value)


def _validate_state_store_health(store: Any) -> dict[str, Any]:
    try:
        integrity = store.integrity()
        replay = store.replay_projection()
    except (legacy.StateError, OSError, sqlite3.Error) as exc:
        raise RuntimeRefreshError(
            "authority-closeout-state-store-invalid",
            "authoritative StateStore integrity/readback failed",
            details={"error": str(exc)},
        ) from exc
    if (
        integrity.get("integrity") != "ok"
        or integrity.get("foreign_key_errors")
        or replay.get("matches_current") is not True
    ):
        raise RuntimeRefreshError(
            "authority-closeout-state-store-invalid",
            "authoritative StateStore integrity or event replay is invalid",
            details={"integrity": integrity, "replay": replay},
        )
    return {
        "integrity": integrity,
        "authoritative_root_sha256": replay.get("authoritative_root_sha256"),
    }


def _runtime_authority_effect_history(
    state_root: Path, approval_task_id: str
) -> list[dict[str, Any]]:
    """Return bounded, digest-verified effects attributed to one approval TaskSpec."""
    intents_root = state_root / "intents"
    if not intents_root.exists():
        return []
    if intents_root.is_symlink() or not intents_root.is_dir():
        raise RuntimeRefreshError(
            "authority-history-root-invalid",
            "runtime-refresh intent history root is not a regular directory",
        )
    try:
        entries = sorted(intents_root.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise RuntimeRefreshError(
            "authority-history-read-failed",
            "runtime-refresh intent history cannot be listed",
            details={"error": str(exc)},
        ) from exc
    if len(entries) > MAX_RUNTIME_AUTHORITY_HISTORY_INTENTS:
        raise RuntimeRefreshError(
            "authority-history-too-large",
            "runtime-refresh intent history exceeds the bounded closeout scan",
            details={
                "limit": MAX_RUNTIME_AUTHORITY_HISTORY_INTENTS,
                "observed": len(entries),
            },
        )
    effects: list[dict[str, Any]] = []
    for path in entries:
        if path.suffix != ".json":
            continue
        intent = read_json(path)
        verify_digest(intent, "intent_sha256")
        intent_sha256 = intent.get("intent_sha256")
        if path.name != f"{intent_sha256}.json":
            raise RuntimeRefreshError(
                "authority-history-intent-path-mismatch",
                "persisted runtime-refresh intent filename differs from its digest",
                details={"path": str(path)},
            )
        if intent.get("approval_task_id") != approval_task_id:
            continue
        target_sha256 = intent.get("target_sha256")
        if not _is_sha256(target_sha256):
            raise RuntimeRefreshError(
                "authority-history-intent-invalid",
                "runtime-refresh authority history contains an invalid target digest",
                details={"intent_sha256": intent_sha256},
            )
        result_path = state_root / "attempts" / target_sha256 / "result.json"
        if not result_path.exists():
            continue
        raw_result = read_json(result_path)
        # Multiple intents for one target intentionally share one attempt ledger.
        # Attribute the effect only to the intent that actually produced the result.
        if raw_result.get("intent_sha256") != intent_sha256:
            continue
        result = _validate_result_for_intent(raw_result, intent)
        if result.get("effect_started") is not True:
            continue
        effects.append(
            {
                "intent_sha256": intent_sha256,
                "target_sha256": target_sha256,
                "result_sha256": result["result_sha256"],
                "status": result.get("status"),
                "main_commit": intent.get("main_commit"),
            }
        )
    return effects


def _validated_terminal_authority_run(
    *, store: Any, approval_task_id: str, task_runs: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Allow only one fully authenticated succeeded run to coexist with authority closeout."""
    if not task_runs:
        return None
    run_ids = [run.get("run_id") for run in task_runs]
    if len(task_runs) != 1:
        raise RuntimeRefreshError(
            "authority-closeout-run-count-invalid",
            "historical runtime authority closeout requires zero or exactly one succeeded run",
            details={"run_ids": run_ids},
        )
    run_id = task_runs[0].get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise RuntimeRefreshError(
            "authority-closeout-run-invalid", "Bureau run identity is invalid"
        )
    try:
        run = store.run(run_id)
        receipt = store.receipt(run_id)
        envelope_path = store.envelope_path(run_id)
    except (legacy.StateError, OSError, sqlite3.Error, AttributeError) as exc:
        raise RuntimeRefreshError(
            "authority-closeout-run-read-failed",
            "Bureau terminal run evidence could not be read",
            details={"run_id": run_id, "error": str(exc)},
        ) from exc
    if run.get("task_id") != approval_task_id or run.get("state") != "succeeded":
        raise RuntimeRefreshError(
            "authority-closeout-run-not-succeeded",
            "only one task-bound succeeded run may coexist with historical authority closeout",
            details={"run_id": run_id, "state": run.get("state")},
        )
    reservations = run.get("reservations")
    if not isinstance(reservations, list) or reservations:
        raise RuntimeRefreshError(
            "authority-closeout-run-reservations-present",
            "succeeded authority run still has reservations",
            details={"run_id": run_id, "reservations": reservations},
        )
    if not isinstance(receipt, dict):
        raise RuntimeRefreshError(
            "authority-closeout-run-receipt-missing",
            "succeeded authority run has no canonical receipt",
            details={"run_id": run_id},
        )
    unsigned_receipt = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if receipt.get("receipt_sha256") != legacy.sha256_json(unsigned_receipt):
        raise RuntimeRefreshError(
            "authority-closeout-run-receipt-digest-mismatch",
            "succeeded authority run receipt digest is invalid",
            details={"run_id": run_id},
        )
    bindings = {
        "run_id": run_id,
        "task_id": approval_task_id,
        "task_sha256": run.get("task_sha256"),
        "plan_sha256": run.get("plan_sha256"),
        "envelope_sha256": run.get("envelope_sha256"),
    }
    if any(receipt.get(key) != value for key, value in bindings.items()):
        raise RuntimeRefreshError(
            "authority-closeout-run-receipt-binding-mismatch",
            "succeeded authority run receipt does not match the frozen run revision",
            details={"run_id": run_id},
        )
    if receipt.get("schema_version") != 1 or not isinstance(receipt.get("verified_at"), str):
        raise RuntimeRefreshError(
            "authority-closeout-run-receipt-invalid",
            "succeeded authority run receipt contract is invalid",
            details={"run_id": run_id},
        )
    try:
        parse_time(receipt["verified_at"])
    except RuntimeRefreshError as exc:
        raise RuntimeRefreshError(
            "authority-closeout-run-receipt-invalid",
            "succeeded authority run receipt verification timestamp is invalid",
            details={"run_id": run_id},
        ) from exc
    try:
        envelope = read_json(Path(envelope_path))
    except RuntimeRefreshError as exc:
        raise RuntimeRefreshError(
            "authority-closeout-run-envelope-invalid",
            "succeeded authority run envelope could not be authenticated",
            details={"run_id": run_id, "reason": exc.code},
        ) from exc
    if envelope.get("schema_version") != 1:
        raise RuntimeRefreshError(
            "authority-closeout-run-envelope-invalid",
            "succeeded authority run envelope contract is invalid",
            details={"run_id": run_id},
        )
    if legacy.sha256_json(envelope) != run.get("envelope_sha256"):
        raise RuntimeRefreshError(
            "authority-closeout-run-envelope-digest-mismatch",
            "succeeded authority run envelope digest is invalid",
            details={"run_id": run_id},
        )
    if any(
        envelope.get(key) != value
        for key, value in bindings.items()
        if key != "envelope_sha256"
    ):
        raise RuntimeRefreshError(
            "authority-closeout-run-envelope-binding-mismatch",
            "succeeded authority run envelope does not match the frozen run revision",
            details={"run_id": run_id},
        )
    task = envelope.get("task")
    acceptance = task.get("acceptance") if isinstance(task, dict) else None
    if not isinstance(acceptance, list) or not acceptance:
        raise RuntimeRefreshError(
            "authority-closeout-run-envelope-acceptance-invalid",
            "succeeded authority run envelope has no acceptance contract",
            details={"run_id": run_id},
        )
    criterion_ids = [item.get("id") for item in acceptance if isinstance(item, dict)]
    if (
        len(criterion_ids) != len(acceptance)
        or any(not isinstance(value, str) or not value for value in criterion_ids)
        or len(set(criterion_ids)) != len(criterion_ids)
    ):
        raise RuntimeRefreshError(
            "authority-closeout-run-envelope-acceptance-invalid",
            "succeeded authority run acceptance criterion identities are invalid",
            details={"run_id": run_id},
        )
    evidence = receipt.get("evidence")
    if not isinstance(evidence, dict) or set(evidence) != set(criterion_ids):
        raise RuntimeRefreshError(
            "authority-closeout-run-receipt-acceptance-incomplete",
            "succeeded authority run receipt does not cover the frozen acceptance contract",
            details={"run_id": run_id},
        )
    for criterion_id in criterion_ids:
        item = evidence.get(criterion_id)
        authentication = item.get("_source_authentication") if isinstance(item, dict) else None
        unsigned_evidence = (
            {key: value for key, value in item.items() if key != "_source_authentication"}
            if isinstance(item, dict)
            else {}
        )
        if (
            not isinstance(item, dict)
            or item.get("schema_version") != 1
            or item.get("kind") != "bureau.acceptance_evidence"
            or item.get("criterion_id") != criterion_id
            or not isinstance(authentication, dict)
            or authentication.get("schema_version") != 1
            or authentication.get("kind") != "bureau.acceptance_source_authentication"
            or authentication.get("criterion_id") != criterion_id
            or authentication.get("run_id") != run_id
            or authentication.get("task_id") != approval_task_id
            or authentication.get("task_sha256") != run.get("task_sha256")
            or authentication.get("plan_sha256") != run.get("plan_sha256")
            or authentication.get("envelope_sha256") != run.get("envelope_sha256")
            or authentication.get("evidence_sha256") != legacy.sha256_json(unsigned_evidence)
        ):
            raise RuntimeRefreshError(
                "authority-closeout-run-receipt-authentication-invalid",
                "succeeded authority run contains unauthenticated acceptance evidence",
                details={"run_id": run_id, "criterion_id": criterion_id},
            )
    return {
        "run_id": run_id,
        "receipt_sha256": receipt["receipt_sha256"],
        "task_sha256": run.get("task_sha256"),
        "plan_sha256": run.get("plan_sha256"),
        "envelope_sha256": run.get("envelope_sha256"),
    }


def closeout_runtime_refresh_authority(
    *,
    state_root: Path,
    approval_task_id: str,
    target_sha256: str,
    intent_sha256: str,
    result_sha256: str,
    resource_db: Path = DEFAULT_GRABOWSKI_RESOURCE_DB,
    now: datetime | None = None,
    authority_store: Any | None = None,
    readback: Callable[..., dict[str, Any]] = readback_historical_install,
    scheduler_readback: Callable[..., dict[str, Any]] = readback_user_scheduler,
) -> dict[str, Any]:
    """CAS-close a successful single-use authority that intentionally has no run."""
    current_time = now or utc_now()
    if not all(_is_sha256(value) for value in (target_sha256, intent_sha256, result_sha256)):
        raise RuntimeRefreshError(
            "authority-closeout-binding-invalid",
            "no-run closeout requires exact target, intent, and result digests",
        )
    resolved_state_root = state_root.expanduser().resolve()
    intent_path = resolved_state_root / "intents" / f"{intent_sha256}.json"
    started_path = resolved_state_root / "attempts" / target_sha256 / "started.json"
    effect_result_path = resolved_state_root / "attempts" / target_sha256 / "result.json"
    no_effect_result_path = (
        resolved_state_root / "no-effect-results" / f"{intent_sha256}.json"
    )
    result_path = (
        effect_result_path if effect_result_path.exists() else no_effect_result_path
    )
    intent = read_json(intent_path)
    verify_digest(intent, "intent_sha256")
    if (
        intent.get("intent_sha256") != intent_sha256
        or intent.get("approval_task_id") != approval_task_id
        or intent.get("target_sha256") != target_sha256
        or Path(str(intent.get("state_root", ""))).expanduser().resolve() != resolved_state_root
    ):
        raise RuntimeRefreshError(
            "authority-closeout-intent-mismatch",
            "persisted intent does not match the no-run closeout bindings",
        )
    created_at = intent.get("created_at")
    if not isinstance(created_at, str):
        raise RuntimeRefreshError(
            "authority-closeout-intent-invalid", "persisted intent creation time is invalid"
        )
    # Re-evaluate the typed evidence at the time it was issued. Closeout may
    # legitimately happen after the short deployment intent has expired.
    validate_runtime_approval_intent(intent_path, now=parse_time(created_at))
    result = _validate_result_for_intent(read_json(result_path), intent)
    if result.get("result_sha256") != result_sha256:
        raise RuntimeRefreshError(
            "authority-closeout-result-mismatch",
            "persisted result digest differs from the requested closeout",
        )
    deployed_success = (
        result.get("status") == "deployed" and result.get("effect_started") is True
    )
    no_effect_success = (
        result.get("status") == "already_current"
        and result.get("effect_started") is False
    )
    if not (deployed_success or no_effect_success):
        raise RuntimeRefreshError(
            "authority-closeout-result-not-successful",
            "no-run closeout requires deployed/effect-started or already-current/no-effect success",
            details={
                "status": result.get("status"),
                "effect_started": result.get("effect_started"),
            },
        )
    effect_history = _runtime_authority_effect_history(resolved_state_root, approval_task_id)
    requested_effect = {
        "intent_sha256": intent_sha256,
        "target_sha256": target_sha256,
        "result_sha256": result_sha256,
    }
    matching_effects = [
        item
        for item in effect_history
        if all(item.get(key) == value for key, value in requested_effect.items())
    ]
    conflicting_effects = [
        item
        for item in effect_history
        if not all(item.get(key) == value for key, value in requested_effect.items())
    ]
    if deployed_success:
        if len(matching_effects) != 1:
            raise RuntimeRefreshError(
                "authority-closeout-history-mismatch",
                "requested deployed result is not uniquely represented in authority history",
                details={"requested": requested_effect, "effects": effect_history},
            )
        if conflicting_effects:
            raise RuntimeRefreshError(
                "authority-closeout-historical-multi-use",
                (
                    "single-use runtime authority has other effect-started attempts and "
                    "cannot be verified by one no-run closeout"
                ),
                details={"requested": requested_effect, "conflicts": conflicting_effects},
            )
    elif effect_history:
        raise RuntimeRefreshError(
            "authority-closeout-no-effect-history-conflict",
            "already-current no-effect closeout cannot coexist with effect-started history",
            details={"effects": effect_history},
        )
    if deployed_success:
        started = read_json(started_path)
        verify_digest(started, "start_sha256")
        if (
            started.get("schema_version") != SCHEMA_VERSION
            or started.get("kind") != "bureau_runtime_refresh_attempt_start"
            or started.get("intent_sha256") != intent_sha256
            or started.get("target_sha256") != target_sha256
            or started.get("main_commit") != intent.get("main_commit")
            or started.get("lease_binding") != result.get("lease_binding")
        ):
            raise RuntimeRefreshError(
                "authority-closeout-start-mismatch",
                "attempt start receipt does not match the deployed result",
            )
    elif started_path.exists():
        raise RuntimeRefreshError(
            "authority-closeout-no-effect-start-conflict",
            "already-current no-effect result must not occupy the effect-attempt ledger",
        )
    if deployed_success:
        install_receipt = result.get("install_receipt")
        persisted_readback = result.get("readback")
        if not isinstance(install_receipt, dict) or not isinstance(persisted_readback, dict):
            raise RuntimeRefreshError(
                "authority-closeout-result-invalid",
                "deployed result lacks installer or immutable readback evidence",
            )
        current_readback = readback(
            expected_commit=intent["main_commit"],
            prefix=Path(intent["prefix"]).expanduser().resolve(),
            bin_dir=Path(intent["bin_dir"]).expanduser().resolve(),
            install_receipt=install_receipt,
        )
        closeout_manifest_sha256 = persisted_readback.get("manifest_sha256")
    else:
        persisted_readback = result.get("scheduler")
        closeout_manifest_sha256 = result.get("manifest_sha256")
        if (
            not isinstance(persisted_readback, dict)
            or not _is_sha256(closeout_manifest_sha256)
        ):
            raise RuntimeRefreshError(
                "authority-closeout-result-invalid",
                "already-current result lacks scheduler or manifest evidence",
            )
        current_readback = scheduler_readback(
            persisted_readback, expected_commit=intent["main_commit"]
        )
    readback_matches = (
        _install_closeout_readbacks_match(
            persisted_readback, current_readback, install_receipt
        )
        if deployed_success
        else _scheduler_closeout_readbacks_match(persisted_readback, current_readback)
    )
    if not readback_matches:
        raise RuntimeRefreshError(
            "authority-closeout-readback-drift",
            "current runtime readback differs from the successful result",
        )
    store = _closeout_authority_store(intent, authority_store)
    try:
        task_runs = [run for run in store.list_runs() if run.get("task_id") == approval_task_id]
    except (legacy.StateError, OSError, sqlite3.Error) as exc:
        raise RuntimeRefreshError(
            "authority-closeout-run-read-failed",
            "Bureau run readback failed closed",
            details={"error": str(exc)},
        ) from exc
    _validated_terminal_authority_run(
        store=store, approval_task_id=approval_task_id, task_runs=task_runs
    )
    _validate_state_store_health(store)
    release = validate_released_lease_binding(
        intent=intent,
        result=result,
        resource_db=resource_db,
        now=current_time,
    )
    current = _read_authority_task(store, approval_task_id)
    spec = current["spec"]
    metadata, authority = _runtime_authority_metadata(spec)
    existing_closeout_value = metadata.get("runtime_closeout")
    if existing_closeout_value is not None:
        existing_closeout = _validated_runtime_closeout(existing_closeout_value)
        expected_pairs = {
            "task_id": approval_task_id,
            "target_sha256": target_sha256,
            "intent_sha256": intent_sha256,
            "runtime_result_sha256": result_sha256,
            "lease_binding_sha256": result["lease_binding"]["lease_binding_sha256"],
        }
        if any(existing_closeout.get(key) != value for key, value in expected_pairs.items()):
            raise RuntimeRefreshError(
                "authority-closeout-replay-mismatch",
                "terminal TaskSpec closeout is bound to other evidence",
            )
        if spec.get("state") != "verified":
            raise RuntimeRefreshError(
                "authority-closeout-state-conflict",
                "closeout evidence exists on a non-verified TaskSpec",
            )
        return {
            "task_id": approval_task_id,
            "revision": current["revision"],
            "spec_sha256": current["spec_sha256"],
            "closeout": existing_closeout,
            "idempotent_replay": True,
        }
    if spec.get("state") in legacy.TERMINAL_TASK_STATES:
        raise RuntimeRefreshError(
            "authority-task-terminal",
            "terminal TaskSpec has no matching canonical runtime closeout",
            details={"state": spec.get("state")},
        )
    intent_authority = intent.get("authority_task_spec")
    if isinstance(intent_authority, dict):
        baseline = _intent_authority_record(intent)
    else:
        baseline = {
            "task_id": approval_task_id,
            "revision": current["revision"],
            "spec_sha256": current["spec_sha256"],
            "target_sha256": target_sha256,
        }
    expected_consumption = {
        "schema_version": RUNTIME_AUTHORITY_SCHEMA_VERSION,
        "kind": RUNTIME_AUTHORITY_CONSUMPTION_KIND,
        "status": "consumed",
        "task_id": approval_task_id,
        "authority_revision": baseline["revision"],
        "authority_spec_sha256": baseline["spec_sha256"],
        "target_sha256": target_sha256,
        "intent_sha256": intent_sha256,
        "result_sha256": result_sha256,
        "consumed_at": isoformat(current_time),
    }
    existing_consumption_value = authority.get("consumption")
    if existing_consumption_value is not None:
        existing_consumption = _validated_authority_consumption(existing_consumption_value)
        comparable = dict(expected_consumption)
        comparable["consumed_at"] = existing_consumption["consumed_at"]
        if existing_consumption != comparable:
            raise RuntimeRefreshError(
                "authority-consumption-mismatch",
                "TaskSpec consumption differs from no-run closeout evidence",
            )
        expected_consumption = existing_consumption
    else:
        authority["consumption"] = expected_consumption
    binding_value = authority.get("target_binding_receipt")
    if binding_value is not None:
        binding_record = _validated_authority_target_binding(binding_value)
        if (
            binding_record["task_id"] != approval_task_id
            or binding_record["target_sha256"] != target_sha256
            or binding_record["intent_sha256"] != intent_sha256
        ):
            raise RuntimeRefreshError(
                "authority-target-binding-mismatch",
                "TaskSpec target binding differs from no-run closeout evidence",
            )
    else:
        authority["target_binding_receipt"] = {
            "schema_version": RUNTIME_AUTHORITY_SCHEMA_VERSION,
            "kind": RUNTIME_AUTHORITY_BINDING_KIND,
            "task_id": approval_task_id,
            "authority_revision": baseline["revision"],
            "authority_spec_sha256": baseline["spec_sha256"],
            "target_sha256": target_sha256,
            "intent_sha256": intent_sha256,
            "bound_at": isoformat(current_time),
        }
    validate_authoritative_runtime_refresh_task(
        store=store,
        approval_task_id=approval_task_id,
        target_sha256=target_sha256,
        expected_intent_sha256=intent_sha256,
        allow_bound_intent=True,
        expected_consumption=expected_consumption,
    )
    closeout = {
        "schema_version": RUNTIME_AUTHORITY_SCHEMA_VERSION,
        "kind": RUNTIME_AUTHORITY_CLOSEOUT_KIND,
        "status": "verified",
        "task_id": approval_task_id,
        "authority_revision": baseline["revision"],
        "authority_spec_sha256": baseline["spec_sha256"],
        "target_sha256": target_sha256,
        "intent_sha256": intent_sha256,
        "runtime_result_sha256": result_sha256,
        "source_commit": intent["main_commit"],
        "manifest_sha256": closeout_manifest_sha256,
        "readback_sha256": sha256_bytes(canonical_bytes(current_readback)),
        "lease_binding_sha256": result["lease_binding"]["lease_binding_sha256"],
        "lease_release_sha256": release["lease_release_sha256"],
        "closed_at": isoformat(current_time),
        "does_not_establish": [
            "retroactive_claim_authority",
            "synthetic_run_authority",
            "runtime_authority_for_later_targets",
            "future_runtime_health",
        ],
    }
    mutated = json.loads(json.dumps(spec))
    mutated["state"] = "verified"
    mutated_metadata = mutated["metadata"]
    mutated_authority = mutated_metadata["runtime_refresh_authority"]
    mutated_authority["target_binding_receipt"] = authority["target_binding_receipt"]
    mutated_authority["consumption"] = expected_consumption
    mutated_metadata["runtime_closeout"] = closeout
    changed = _put_authority_task(
        store,
        mutated,
        idempotency_key=f"runtime-refresh-no-run-closeout:{approval_task_id}:{result_sha256}",
        expected_revision=current["revision"],
        source="runtime-refresh-no-run-closeout",
    )
    readback_task = _read_authority_task(store, approval_task_id)
    observed_closeout = _validated_runtime_closeout(
        readback_task["spec"]["metadata"].get("runtime_closeout")
    )
    if (
        readback_task.get("revision") != changed.get("revision")
        or readback_task.get("spec_sha256") != changed.get("spec_sha256")
        or readback_task["spec"].get("state") != "verified"
        or observed_closeout != closeout
    ):
        raise RuntimeRefreshError(
            "authority-closeout-readback-failed",
            "terminal TaskSpec readback differs from the closeout CAS result",
        )
    _validate_state_store_health(store)
    return {
        "task_id": approval_task_id,
        "revision": readback_task["revision"],
        "spec_sha256": readback_task["spec_sha256"],
        "closeout": observed_closeout,
        "idempotent_replay": False,
    }


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
    authority_store: Any | None = None,
) -> dict[str, Any]:
    current = now or utc_now()
    intent = read_json(intent_path)
    verify_digest(intent, "intent_sha256")
    if intent.get("kind") != "bureau_runtime_refresh_intent":
        raise RuntimeRefreshError("intent-kind-invalid", "runtime-refresh intent kind is invalid")
    validate_runtime_approval_intent(
        intent_path,
        now=current,
        minimum_remaining_seconds=DEFAULT_MIN_RUNTIME_APPROVAL_REMAINING_SECONDS,
    )
    resolved_state_root = state_root.expanduser().resolve()
    if Path(intent["state_root"]).expanduser().resolve() != resolved_state_root:
        raise RuntimeRefreshError("intent-state-root-mismatch", "intent state root differs")
    store = _bound_authority_store(intent, authority_store)
    expected_authority = _intent_authority_record(intent)
    attempt_dir = resolved_state_root / "attempts" / intent["target_sha256"]
    started_path = attempt_dir / "started.json"
    result_path = attempt_dir / "result.json"
    no_effect_result_path = (
        resolved_state_root
        / "no-effect-results"
        / f"{intent['intent_sha256']}.json"
    )
    if no_effect_result_path.exists():
        existing = _validate_result_for_intent(read_json(no_effect_result_path), intent)
        if (
            existing.get("status") != "already_current"
            or existing.get("effect_started") is not False
        ):
            raise RuntimeRefreshError(
                "no-effect-result-invalid",
                "intent-bound no-effect result is not an already-current success",
            )
        consume_runtime_refresh_authority(
            store=store, intent=intent, result=existing, now=current
        )
        return {**existing, "reused": True}
    if result_path.exists():
        existing = read_json(result_path)
        verify_digest(existing, "result_sha256")
        result_intent_sha256 = existing.get("intent_sha256")
        if result_intent_sha256 == intent.get("intent_sha256"):
            result_intent = intent
        elif _is_sha256(result_intent_sha256):
            result_intent = read_json(
                resolved_state_root / "intents" / f"{result_intent_sha256}.json"
            )
            verify_digest(result_intent, "intent_sha256")
            if (
                result_intent.get("approval_task_id") != intent.get("approval_task_id")
                or result_intent.get("target_sha256") != intent.get("target_sha256")
                or result_intent.get("main_commit") != intent.get("main_commit")
                or result_intent.get("authority_state_store") != intent.get("authority_state_store")
            ):
                raise RuntimeRefreshError(
                    "result-intent-binding-invalid",
                    "target result was produced by another authority binding",
                )
        else:
            raise RuntimeRefreshError(
                "result-intent-binding-invalid",
                "target result has no valid intent digest",
            )
        existing = _validate_result_for_intent(existing, result_intent)
        if existing.get("status") in {"deployed", "already_current"}:
            consume_runtime_refresh_authority(
                store=store, intent=result_intent, result=existing, now=current
            )
        else:
            validate_authoritative_runtime_refresh_task(
                store=store,
                approval_task_id=expected_authority["task_id"],
                target_sha256=expected_authority["target_sha256"],
                expected_intent_sha256=intent["intent_sha256"],
                allow_bound_intent=True,
            )
        return {**existing, "reused": True}
    if started_path.exists():
        validate_authoritative_runtime_refresh_task(
            store=store,
            approval_task_id=expected_authority["task_id"],
            target_sha256=expected_authority["target_sha256"],
            expected_intent_sha256=intent["intent_sha256"],
            allow_bound_intent=True,
        )
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

    validate_authoritative_runtime_refresh_task(
        store=store,
        approval_task_id=expected_authority["task_id"],
        target_sha256=expected_authority["target_sha256"],
        expected_revision=expected_authority["revision"],
        expected_spec_sha256=expected_authority["spec_sha256"],
    )
    current_authority = _read_authority_task(store, expected_authority["task_id"])
    _metadata, authority_contract = _runtime_authority_metadata(current_authority["spec"])
    source_precondition = _validated_runtime_source_precondition(
        authority_contract.get("source_precondition")
    )
    if intent.get("source_precondition") != source_precondition:
        raise RuntimeRefreshError(
            "source-precondition-intent-drift",
            "runtime-refresh intent source precondition differs from current authority",
        )
    prefix = Path(intent["prefix"]).expanduser().resolve()
    bin_dir = Path(intent["bin_dir"]).expanduser().resolve()
    user_unit_dir = Path(intent["user_unit_dir"]).expanduser().resolve()
    libexec_dir = Path(intent["libexec_dir"]).expanduser().resolve()
    runtime_user_unit_dir = Path(intent["runtime_user_unit_dir"]).expanduser().resolve()
    validate_runtime_user_unit_dir(runtime_user_unit_dir)
    intent_resource_keys = set(intent.get("required_resource_keys", []))
    live_launcher_keys = set(launcher_mutation_resource_keys(prefix=prefix, bin_dir=bin_dir))
    unleased_launcher_drift = sorted(live_launcher_keys - intent_resource_keys)
    if unleased_launcher_drift:
        raise RuntimeRefreshError(
            "launcher-drift-after-intent",
            "a launcher now requires mutation but is absent from the intent leases",
            details={"resource_keys": unleased_launcher_drift},
        )
    missing_scheduler_resources = sorted(
        set(
            scheduler_resource_keys(
                user_unit_dir=user_unit_dir,
                libexec_dir=libexec_dir,
                runtime_user_unit_dir=runtime_user_unit_dir,
            )
        )
        - intent_resource_keys
    )
    if missing_scheduler_resources:
        raise RuntimeRefreshError(
            "scheduler-resources-unleased",
            "scheduler convergence is absent from the exact runtime-refresh leases",
            details={"resource_keys": missing_scheduler_resources},
        )
    forbidden_scheduler_resources = sorted(
        forbidden_scheduler_parent_resource_keys(
            user_unit_dir=user_unit_dir,
            libexec_dir=libexec_dir,
            runtime_user_unit_dir=runtime_user_unit_dir,
        )
        & intent_resource_keys
    )
    if forbidden_scheduler_resources:
        raise RuntimeRefreshError(
            "scheduler-resources-overbroad",
            "scheduler convergence intent contains broad directory leases",
            details={"resource_keys": forbidden_scheduler_resources},
        )
    binding = validate_live_lease_binding(
        intent, lease_binding, resource_db=resource_db, now=current
    )

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
    _validate_candidate_source_precondition(live, source_precondition)
    if live.get("status") == "already_current":
        scheduler_evidence = live.get("scheduler")
        manifest_sha256 = live.get("deployed_manifest_sha256")
        if (
            not isinstance(scheduler_evidence, dict)
            or scheduler_evidence.get("kind") != "bureau_runtime_scheduler_readback"
            or scheduler_evidence.get("source_commit") != intent["main_commit"]
            or scheduler_evidence.get("authoritative") is not True
            or not _is_sha256(manifest_sha256)
        ):
            raise RuntimeRefreshError(
                "already-current-evidence-invalid",
                "already-current result lacks authoritative scheduler or manifest evidence",
            )
        bound_authority = bind_runtime_refresh_authority(store=store, intent=intent, now=current)
        result = _write_attempt_result(
            no_effect_result_path,
            {
                "schema_version": SCHEMA_VERSION,
                "kind": "bureau_runtime_refresh_result",
                "status": "already_current",
                "intent_sha256": intent["intent_sha256"],
                "target_sha256": intent["target_sha256"],
                "observed_target_sha256": live.get("target_sha256"),
                "main_commit": intent["main_commit"],
                "authority_task_spec": bound_authority,
                "scheduler": scheduler_evidence,
                "manifest_sha256": manifest_sha256,
                "finished_at": isoformat(current),
                "effect_started": False,
                "lease_binding": binding,
            },
        )
        consume_runtime_refresh_authority(store=store, intent=intent, result=result, now=current)
        return result
    if live.get("target_sha256") != intent.get("target_sha256"):
        raise RuntimeRefreshError(
            "target-drift",
            "live deployment target differs from explicit intent",
            details={"intent": intent.get("target_sha256"), "live": live.get("target_sha256")},
        )
    if live.get("status") not in {"candidate", "alert"}:
        raise RuntimeRefreshError(
            "live-candidate-blocked",
            "live observation is not deployable",
            details={"status": live.get("status"), "reason_codes": live.get("reason_codes")},
        )
    current_manifest, current_manifest_sha = load_manifest(manifest_path)
    if (
        current_manifest.get("source_commit") != intent["expected_deployed_source_commit"]
        or current_manifest_sha != intent["expected_manifest_sha256"]
    ):
        raise RuntimeRefreshError(
            "deployed-boundary-drift", "deployed runtime changed after intent"
        )

    # This CAS is the last authority operation before the attempt ledger and
    # runtime effects. It prevents two targets from racing on one TaskSpec.
    bound_authority = bind_runtime_refresh_authority(store=store, intent=intent, now=current)
    started = bind_digest(
        {
            "schema_version": SCHEMA_VERSION,
            "kind": "bureau_runtime_refresh_attempt_start",
            "intent_sha256": intent["intent_sha256"],
            "target_sha256": intent["target_sha256"],
            "main_commit": intent["main_commit"],
            "authority_task_spec": bound_authority,
            "lease_binding": binding,
            "started_at": isoformat(current),
            "effect_started": False,
        },
        "start_sha256",
    )
    create_only(started_path, canonical_bytes(started))
    effect_started = False
    workspace = Path(intent["workspace"])
    launcher_key_to_path = {
        f"path:{bin_dir / name}": bin_dir / name
        for name, _entrypoint in RUNTIME_LAUNCHER_ENTRYPOINTS
    }
    allowed_launcher_paths = [
        path for key, path in launcher_key_to_path.items() if key in intent_resource_keys
    ]
    try:
        source_identity = source_preparer(
            remote_url=intent["remote_url"],
            workspace=workspace,
            expected_commit=intent["main_commit"],
            workspaces_root=resolved_state_root / "workspaces",
        )
        effect_started = True
        install_receipt = installer(
            source=workspace,
            prefix=prefix,
            bin_dir=bin_dir,
            user_unit_dir=user_unit_dir,
            libexec_dir=libexec_dir,
            runtime_user_unit_dir=runtime_user_unit_dir,
            approval_intent=intent_path,
            allowed_launcher_paths=allowed_launcher_paths,
        )
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
                "authority_task_spec": bound_authority,
                "source_identity": source_identity,
                "install_receipt": install_receipt,
                "readback": evidence,
                "lease_binding": binding,
                "finished_at": isoformat(utc_now()),
                "effect_started": True,
                "does_not_establish": ["future_runtime_health", "future_main_stability"],
            },
        )
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
    else:
        consume_runtime_refresh_authority(store=store, intent=intent, result=result, now=current)
        shutil.rmtree(workspace)
        return result
    return _write_attempt_result(
        result_path,
        {
            "schema_version": SCHEMA_VERSION,
            "kind": "bureau_runtime_refresh_result",
            "status": status,
            "intent_sha256": intent["intent_sha256"],
            "target_sha256": intent["target_sha256"],
            "main_commit": intent["main_commit"],
            "authority_task_spec": bound_authority,
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
        default=DEFAULT_RUNTIME_REFRESH_STATE_ROOT,
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

    adopt = sub.add_parser("adopt-authority")
    adopt.add_argument("--registry-root", required=True, type=Path)
    adopt.add_argument("--repository", default=DEFAULT_REPOSITORY)
    adopt.add_argument("--approval-task-id", required=True)
    adopt.add_argument("--publication-pr", required=True, type=int)
    adopt.add_argument("--publication-merge-commit", required=True)
    adopt.add_argument("--expected-main-commit", required=True)
    adopt.add_argument("--task-file-sha256", required=True)
    adopt.add_argument("--required-check", action="append", default=[])

    intent = sub.add_parser("prepare-intent")
    intent.add_argument("--candidate", required=True, type=Path)
    intent.add_argument("--prefix", default="~/.local/share/bureau", type=Path)
    intent.add_argument("--bin-dir", default="~/.local/bin", type=Path)
    intent.add_argument("--user-unit-dir", default=DEFAULT_USER_SYSTEMD_UNIT_ROOT, type=Path)
    intent.add_argument("--libexec-dir", default=DEFAULT_RUNTIME_LIBEXEC_ROOT, type=Path)
    intent.add_argument("--runtime-user-unit-dir", type=Path)
    intent.add_argument("--remote-url", default=DEFAULT_REMOTE_URL)
    intent.add_argument("--authorized-by", required=True)
    intent.add_argument("--authorization", required=True)
    intent.add_argument("--break-glass", action="store_true")
    intent.add_argument("--approval-reference", required=True)
    intent.add_argument("--approval-task-id", required=True)
    intent.add_argument("--ttl-seconds", type=int, default=DEFAULT_INTENT_TTL_SECONDS)

    apply = sub.add_parser("apply")
    apply.add_argument("--intent", required=True, type=Path)
    apply.add_argument("--lease-owner", required=True)
    apply.add_argument("--lease-task-id", required=True)

    closeout = sub.add_parser("closeout-authority")
    closeout.add_argument("--approval-task-id", required=True)
    closeout.add_argument("--target-sha256", required=True)
    closeout.add_argument("--intent-sha256", required=True)
    closeout.add_argument("--result-sha256", required=True)

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
        if args.command == "adopt-authority":
            checks = (
                tuple(args.required_check)
                or DEFAULT_AUTHORITY_ADOPTION_REQUIRED_CHECKS
            )
            result = adopt_runtime_refresh_authority(
                registry_root=_resolved(args.registry_root),
                repository=args.repository,
                approval_task_id=args.approval_task_id,
                publication_pr=args.publication_pr,
                publication_merge_commit=args.publication_merge_commit,
                expected_main_commit=args.expected_main_commit,
                expected_task_file_sha256=args.task_file_sha256,
                required_checks=checks,
            )
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.command == "prepare-intent":
            candidate = read_json(_resolved(args.candidate))
            intent, path = prepare_intent(
                candidate=candidate,
                state_root=state_root,
                prefix=_resolved(args.prefix),
                bin_dir=_resolved(args.bin_dir),
                user_unit_dir=_resolved(args.user_unit_dir),
                libexec_dir=_resolved(args.libexec_dir),
                runtime_user_unit_dir=(
                    _resolved(args.runtime_user_unit_dir)
                    if args.runtime_user_unit_dir is not None
                    else default_runtime_user_unit_dir()
                ),
                remote_url=args.remote_url,
                authorized_by=args.authorized_by,
                authorization=args.authorization,
                break_glass=args.break_glass,
                approval_reference=args.approval_reference,
                approval_task_id=args.approval_task_id,
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
        if args.command == "closeout-authority":
            result = closeout_runtime_refresh_authority(
                state_root=state_root,
                approval_task_id=args.approval_task_id,
                target_sha256=args.target_sha256,
                intent_sha256=args.intent_sha256,
                result_sha256=args.result_sha256,
            )
            print(json.dumps(result, sort_keys=True))
            return 0
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
