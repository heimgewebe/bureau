"""Public-safe, read-only Bureau StateStore snapshot export.

This module has no import, apply, queue, claim, or StateStore write path.  Its
only private inputs are an owner-bound immutable runtime manifest, the
manifest-bound Registry, and ``ReadOnlyStateStore``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import legacy, registry_snapshot
from .read_only_state import ReadOnlyStateStore
from .v2 import Dispatcher, Registry, authoritative_task_registry

SCHEMA_VERSION = 1
KIND = "bureau_public_state_snapshot"
REPOSITORY = "heimgewebe/bureau"
DEFAULT_STATE_ROOT = Path.home() / ".local/state/bureau"
DEFAULT_RUNTIME_MANIFEST = Path.home() / ".local/share/bureau/deployment-manifest.json"
PUBLIC_SNAPSHOT_PATH = Path("registry/public-state.json")
PUBLIC_BRANCH = "automation/bureau-state-snapshot"
MAX_SNAPSHOT_BYTES = 1024 * 1024

TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "generated_at",
        "repository",
        "release",
        "counts",
        "frontier",
        "event_checkpoint",
        "roots",
        "redaction",
        "snapshot_sha256",
    }
)
TASK_STATES = (
    "inbox",
    "planned",
    "ready",
    "blocked",
    "verified",
    "cancelled",
    "superseded",
)
FRONTIER_STATES = (*TASK_STATES, "stale")
REDACTION_CONTRACT = {
    "schema_version": 1,
    "profile": "public-safe-v1",
    "forbidden_categories": [
        "local_paths",
        "secrets_and_credentials",
        "prompt_material",
        "raw_logs_and_traces",
        "personally_identifying_information",
    ],
    "scanner": "recursive-key-and-value-marker-scan-v1",
    "fail_closed": True,
}

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_ABSOLUTE_PATH_RE = re.compile(
    r"(?:^|\s)(?:~[/\\]|/(?:home|users|tmp|var|etc|srv|opt|run)/|[A-Za-z]:[\\/]|file://)",
    re.IGNORECASE,
)
_SECRET_VALUE_RE = re.compile(
    r"(?:"
    r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}|"
    r"\b(?:github_pat_|gh[pousr]_|sk-)[A-Za-z0-9_-]{8,}|"
    r"\bAKIA[0-9A-Z]{16}\b|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\b(?:password|passwd|token|secret|api[_ -]?key)\s*[:=]"
    r")",
    re.IGNORECASE,
)
_PROMPT_VALUE_RE = re.compile(
    r"(?:\b(?:system|developer|user|assistant)\s+prompt\s*[:=]|"
    r"<\|(?:system|user|assistant)\|>|\[INST\]|###\s*instruction)",
    re.IGNORECASE,
)
_RAW_LOG_VALUE_RE = re.compile(
    r"(?:Traceback \(most recent call last\)|\b(?:stdout|stderr|raw[_ -]?log)\s*[:=])",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<![0-9a-f])\+?[0-9][0-9 .()/-]{8,}[0-9](?![0-9a-f])", re.IGNORECASE)

_FORBIDDEN_KEY_MARKERS = {
    "local_paths": {
        "path",
        "filepath",
        "root",
        "directory",
        "cwd",
        "home",
        "workspace",
        "worktree",
    },
    "secrets_and_credentials": {
        "secret",
        "password",
        "passwd",
        "token",
        "credential",
        "credentials",
        "authorization",
        "api_key",
        "private_key",
        "client_secret",
        "cookie",
    },
    "prompt_material": {
        "prompt",
        "system_prompt",
        "developer_prompt",
        "user_prompt",
        "instructions",
    },
    "raw_logs_and_traces": {
        "log",
        "logs",
        "raw_log",
        "stdout",
        "stderr",
        "traceback",
        "stacktrace",
    },
    "personally_identifying_information": {
        "pii",
        "email",
        "phone",
        "telephone",
        "address",
        "full_name",
        "first_name",
        "last_name",
        "birth_date",
        "ip_address",
        "username",
        "user_name",
    },
}


class StateSnapshotError(ValueError):
    """Raised when a private input or public snapshot fails closed."""


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise StateSnapshotError("value is not canonical JSON") from exc


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _require_owner_bound_file(path: Path, *, label: str) -> Path:
    raw = path.expanduser()
    if raw.is_symlink() or not raw.is_file():
        raise StateSnapshotError(f"{label} must be a regular non-symlink file")
    resolved = raw.resolve()
    status = resolved.stat()
    if status.st_uid != os.geteuid():
        raise StateSnapshotError(f"{label} is not owned by the local exporter uid")
    if status.st_mode & 0o022:
        raise StateSnapshotError(f"{label} is group/world writable")
    return resolved


def _load_runtime_release(manifest_path: Path) -> tuple[dict[str, Any], Path]:
    path = _require_owner_bound_file(manifest_path, label="runtime manifest")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateSnapshotError("runtime manifest is invalid JSON") from exc
    if not isinstance(manifest, dict):
        raise StateSnapshotError("runtime manifest must be an object")
    if manifest.get("schema_version") != 1 or manifest.get("kind") != "bureau_runtime_deployment":
        raise StateSnapshotError("unsupported runtime manifest")
    commit = manifest.get("source_commit")
    if not isinstance(commit, str) or _COMMIT_RE.fullmatch(commit) is None:
        raise StateSnapshotError("runtime source commit is invalid")
    identity = registry_snapshot.canonical_registry_identity(manifest)
    if identity.get("available") is not True or identity.get("valid") is not True:
        raise StateSnapshotError("runtime canonical Registry identity is invalid")
    root_value = identity.get("root")
    if not isinstance(root_value, str) or not root_value:
        raise StateSnapshotError("runtime canonical Registry root is unavailable")
    root = Path(root_value).expanduser()
    if root.is_symlink() or not root.is_dir():
        raise StateSnapshotError("runtime canonical Registry root is unavailable")
    return {"commit": commit, "schema_version": 1}, root.resolve()


def _parse_time(value: Any) -> str:
    if not isinstance(value, str):
        raise StateSnapshotError("snapshot timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StateSnapshotError("snapshot timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise StateSnapshotError("snapshot timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _latest_time(connection: sqlite3.Connection) -> str:
    values = [
        connection.execute("SELECT MAX(created_at) FROM events").fetchone()[0],
        connection.execute("SELECT MAX(updated_at) FROM task_specs").fetchone()[0],
    ]
    parsed = [_parse_time(value) for value in values if isinstance(value, str) and value]
    if not parsed:
        return "1970-01-01T00:00:00Z"
    return max(parsed, key=lambda value: datetime.fromisoformat(value.replace("Z", "+00:00")))


def _merkle_root(records: Sequence[Mapping[str, Any]]) -> str:
    serialized = sorted(canonical_bytes(dict(record)) for record in records)
    if not serialized:
        return hashlib.sha256(b"bureau-public-merkle-empty-v1").hexdigest()
    level = [
        hashlib.sha256(b"bureau-public-merkle-leaf-v1\0" + item).digest() for item in serialized
    ]
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [
            hashlib.sha256(
                b"bureau-public-merkle-node-v1\0" + level[index] + level[index + 1]
            ).digest()
            for index in range(0, len(level), 2)
        ]
    return level[0].hex()


def _task_counts(registry: Registry) -> dict[str, Any]:
    observed = Counter(task.state for task in registry.tasks.values())
    unknown = sorted(set(observed) - set(TASK_STATES))
    if unknown:
        raise StateSnapshotError(f"unsupported authoritative task states: {unknown}")
    by_state = {state: observed[state] for state in TASK_STATES}
    return {"tasks_total": sum(by_state.values()), "tasks_by_state": by_state}


def _frontier_counts(registry: Registry, store: ReadOnlyStateStore) -> dict[str, Any]:
    capabilities = {
        capability for task in registry.tasks.values() for capability in task.capabilities
    }
    dispatcher = Dispatcher(
        registry,
        store,
        open_pr_reservations_provider=lambda _registry: [],
    )
    items = dispatcher.frontier(capabilities)
    observed = Counter(str(item.get("effective_state")) for item in items)
    unknown = sorted(set(observed) - set(FRONTIER_STATES))
    if unknown:
        raise StateSnapshotError(f"unsupported frontier states: {unknown}")
    eligible = sum(item.get("eligible") is True for item in items)
    by_state = {state: observed[state] for state in FRONTIER_STATES}
    return {
        "tasks_total": len(items),
        "eligible": eligible,
        "blocked": len(items) - eligible,
        "tasks_by_effective_state": by_state,
    }


def _public_roots(
    registry: Registry,
    revisions: Mapping[str, Mapping[str, Any]],
    connection: sqlite3.Connection,
) -> dict[str, Any]:
    task_identities: list[dict[str, Any]] = []
    for task_id, task in sorted(registry.tasks.items()):
        revision = revisions.get(task_id)
        if not isinstance(revision, Mapping):
            raise StateSnapshotError("authoritative task revision identity is unavailable")
        digest = revision.get("spec_sha256")
        number = revision.get("revision")
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise StateSnapshotError("authoritative task digest is invalid")
        if number is not None and (
            not isinstance(number, int) or isinstance(number, bool) or number < 1
        ):
            raise StateSnapshotError("authoritative task revision is invalid")
        task_identities.append(
            {
                "id": task_id,
                "revision": 0 if number is None else number,
                "schema_version": task.raw.get("schema_version"),
                "sha256": digest,
            }
        )

    receipt_identities: list[dict[str, str]] = []
    for row in connection.execute("SELECT receipt_sha256 FROM receipts ORDER BY receipt_sha256"):
        digest = str(row["receipt_sha256"])
        if _SHA256_RE.fullmatch(digest) is None:
            raise StateSnapshotError("receipt identity digest is invalid")
        receipt_identities.append({"sha256": digest})

    return {
        "public_task_identities": {
            "count": len(task_identities),
            "merkle_sha256": _merkle_root(task_identities),
        },
        "public_receipt_identities": {
            "count": len(receipt_identities),
            "merkle_sha256": _merkle_root(receipt_identities),
        },
    }


def _key_marker(key: str) -> str | None:
    snake = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key).casefold()
    tokens = {part for part in re.split(r"[^a-z0-9]+", snake) if part}
    compact = "_".join(part for part in re.split(r"[^a-z0-9]+", snake) if part)
    for category, markers in _FORBIDDEN_KEY_MARKERS.items():
        if compact in markers or tokens.intersection(markers):
            return category
    return None


def _value_marker(value: str) -> str | None:
    checks = (
        ("local_paths", _ABSOLUTE_PATH_RE),
        ("secrets_and_credentials", _SECRET_VALUE_RE),
        ("prompt_material", _PROMPT_VALUE_RE),
        ("raw_logs_and_traces", _RAW_LOG_VALUE_RE),
        ("personally_identifying_information", _EMAIL_RE),
        ("personally_identifying_information", _PHONE_RE),
    )
    for category, pattern in checks:
        if pattern.search(value):
            return category
    return None


def assert_public_safe(value: Any) -> None:
    """Recursively reject forbidden key and value markers.

    The fixed forbidden-category declarations are schema constants rather than
    exported private data. Their exact list is checked separately and is the
    sole value-scan exemption.
    """

    def visit(item: Any, location: tuple[str, ...]) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise StateSnapshotError("public snapshot keys must be strings")
                marker = _key_marker(key)
                if marker is not None:
                    raise StateSnapshotError(f"public-safe redaction blocked {marker} key marker")
                visit(child, (*location, key))
            return
        if isinstance(item, (list, tuple)):
            for index, child in enumerate(item):
                visit(child, (*location, str(index)))
            return
        if isinstance(item, str):
            if location[:2] == ("redaction", "forbidden_categories"):
                return
            if location == ("generated_at",):
                return
            if _SHA256_RE.fullmatch(item) is not None or _COMMIT_RE.fullmatch(item) is not None:
                return
            marker = _value_marker(item)
            if marker is not None:
                raise StateSnapshotError(f"public-safe redaction blocked {marker} value marker")
            return
        if item is None or isinstance(item, (bool, int)):
            return
        raise StateSnapshotError("public snapshot contains a non-public JSON value")

    visit(value, ())


def _require_keys(
    value: Any, expected: set[str] | frozenset[str], *, label: str
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(expected):
        raise StateSnapshotError(f"{label} fields are invalid")
    return value


def _require_count(value: Any, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise StateSnapshotError(f"{label} count is invalid")
    return value


def _require_sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise StateSnapshotError(f"{label} digest is invalid")
    return value


def validate_public_snapshot(snapshot: Any) -> dict[str, Any]:
    top = _require_keys(snapshot, TOP_LEVEL_FIELDS, label="snapshot")
    if top["schema_version"] != SCHEMA_VERSION or top["kind"] != KIND:
        raise StateSnapshotError("snapshot schema or kind is unsupported")
    if top["repository"] != REPOSITORY:
        raise StateSnapshotError("snapshot repository identity is invalid")
    _parse_time(top["generated_at"])

    release = _require_keys(top["release"], {"commit", "schema_version"}, label="release")
    if (
        release["schema_version"] != 1
        or not isinstance(release["commit"], str)
        or _COMMIT_RE.fullmatch(release["commit"]) is None
    ):
        raise StateSnapshotError("snapshot release identity is invalid")

    counts = _require_keys(top["counts"], {"tasks_total", "tasks_by_state"}, label="counts")
    _require_count(counts["tasks_total"], label="task total")
    states = _require_keys(counts["tasks_by_state"], set(TASK_STATES), label="task states")
    for state, count in states.items():
        _require_count(count, label=f"task state {state}")
    if sum(states.values()) != counts["tasks_total"]:
        raise StateSnapshotError("task state counts do not sum to total")

    frontier = _require_keys(
        top["frontier"],
        {"tasks_total", "eligible", "blocked", "tasks_by_effective_state"},
        label="frontier",
    )
    for field in ("tasks_total", "eligible", "blocked"):
        _require_count(frontier[field], label=f"frontier {field}")
    frontier_states = _require_keys(
        frontier["tasks_by_effective_state"],
        set(FRONTIER_STATES),
        label="frontier states",
    )
    for state, count in frontier_states.items():
        _require_count(count, label=f"frontier state {state}")
    if sum(frontier_states.values()) != frontier["tasks_total"]:
        raise StateSnapshotError("frontier state counts do not sum to total")
    if frontier["eligible"] + frontier["blocked"] != frontier["tasks_total"]:
        raise StateSnapshotError("frontier eligibility counts do not sum to total")

    checkpoint = _require_keys(top["event_checkpoint"], {"id"}, label="event checkpoint")
    _require_count(checkpoint["id"], label="event checkpoint")

    roots = _require_keys(
        top["roots"],
        {"public_task_identities", "public_receipt_identities"},
        label="roots",
    )
    for name in ("public_task_identities", "public_receipt_identities"):
        identity = _require_keys(roots[name], {"count", "merkle_sha256"}, label=name)
        _require_count(identity["count"], label=name)
        _require_sha256(identity["merkle_sha256"], label=name)
    if roots["public_task_identities"]["count"] != counts["tasks_total"]:
        raise StateSnapshotError("public task identity count does not match task total")

    redaction = _require_keys(
        top["redaction"],
        {"schema_version", "profile", "forbidden_categories", "scanner", "fail_closed"},
        label="redaction",
    )
    if dict(redaction) != REDACTION_CONTRACT:
        raise StateSnapshotError("redaction contract is invalid")

    assert_public_safe(top)
    _require_sha256(top["snapshot_sha256"], label="snapshot")
    unsigned = {key: value for key, value in top.items() if key != "snapshot_sha256"}
    if top["snapshot_sha256"] != sha256_json(unsigned):
        raise StateSnapshotError("snapshot digest mismatch")
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "bureau_public_state_snapshot_validation",
        "status": "valid",
        "snapshot_sha256": top["snapshot_sha256"],
    }


def build_public_snapshot(
    *,
    state_root: Path = DEFAULT_STATE_ROOT,
    runtime_manifest: Path = DEFAULT_RUNTIME_MANIFEST,
) -> dict[str, Any]:
    state_root = state_root.expanduser().resolve()
    state_db = _require_owner_bound_file(state_root / "bureau.sqlite3", label="StateStore database")
    release, registry_root = _load_runtime_release(runtime_manifest)
    store = ReadOnlyStateStore(state_db, state_root)
    replay = store.replay_projection()
    if replay.get("matches_current") is not True:
        raise StateSnapshotError("StateStore replay does not match current projection")
    source_registry = Registry.load(registry_root)
    registry, _authority, revisions = authoritative_task_registry(source_registry, store)
    with store.connect() as connection:
        checkpoint_row = connection.execute(
            "SELECT COALESCE(MAX(event_id),0) AS checkpoint FROM events"
        ).fetchone()
        if checkpoint_row is None:
            raise StateSnapshotError("StateStore event checkpoint is unavailable")
        generated_at = _latest_time(connection)
        roots = _public_roots(registry, revisions, connection)
    unsigned = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "generated_at": generated_at,
        "repository": REPOSITORY,
        "release": release,
        "counts": _task_counts(registry),
        "frontier": _frontier_counts(registry, store),
        "event_checkpoint": {"id": int(checkpoint_row["checkpoint"])},
        "roots": roots,
        "redaction": json.loads(canonical_bytes(REDACTION_CONTRACT)),
    }
    assert_public_safe(unsigned)
    snapshot = {**unsigned, "snapshot_sha256": sha256_json(unsigned)}
    validate_public_snapshot(snapshot)
    return snapshot


def decode_public_snapshot(raw: bytes) -> dict[str, Any]:
    if not isinstance(raw, bytes) or len(raw) > MAX_SNAPSHOT_BYTES:
        raise StateSnapshotError("public snapshot size is invalid")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateSnapshotError("public snapshot is invalid JSON") from exc
    validate_public_snapshot(payload)
    return dict(payload)


def read_public_snapshot(path: Path) -> dict[str, Any]:
    raw = path.expanduser()
    if raw.is_symlink() or not raw.is_file():
        raise StateSnapshotError("public snapshot must be a regular non-symlink file")
    return decode_public_snapshot(raw.read_bytes())


def write_public_snapshot(path: Path, snapshot: Mapping[str, Any]) -> dict[str, Any]:
    validate_public_snapshot(snapshot)
    target = path.expanduser()
    if target.is_symlink():
        raise StateSnapshotError("refusing to replace a symlink snapshot path")
    parent = target.parent
    if parent.is_symlink() or not parent.is_dir():
        raise StateSnapshotError("snapshot parent must be an existing real directory")
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        json.dump(snapshot, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    observed = read_public_snapshot(target)
    if observed["snapshot_sha256"] != snapshot["snapshot_sha256"]:
        raise StateSnapshotError("snapshot write readback mismatch")
    return {"status": "written", "snapshot_sha256": snapshot["snapshot_sha256"]}


def export_public_snapshot(
    *,
    output: Path,
    state_root: Path = DEFAULT_STATE_ROOT,
    runtime_manifest: Path = DEFAULT_RUNTIME_MANIFEST,
) -> dict[str, Any]:
    snapshot = build_public_snapshot(state_root=state_root, runtime_manifest=runtime_manifest)
    written = write_public_snapshot(output, snapshot)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "bureau_public_state_snapshot_export",
        **written,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export or validate a public-safe Bureau StateStore snapshot."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--output", required=True)
    export.add_argument("--state-root", default=str(DEFAULT_STATE_ROOT))
    export.add_argument("--runtime-manifest", default=str(DEFAULT_RUNTIME_MANIFEST))
    validate = commands.add_parser("validate")
    validate.add_argument("snapshot")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "export":
            result = export_public_snapshot(
                output=Path(arguments.output),
                state_root=Path(arguments.state_root),
                runtime_manifest=Path(arguments.runtime_manifest),
            )
        else:
            result = validate_public_snapshot(read_public_snapshot(Path(arguments.snapshot)))
    except (OSError, sqlite3.Error, legacy.StateError, StateSnapshotError) as exc:
        raise SystemExit(f"state snapshot failed: {exc}") from exc
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
