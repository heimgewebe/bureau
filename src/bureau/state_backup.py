from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import legacy, state_events, task_specs

SCHEMA_VERSION = 1
TERMINAL_RUN_STATES = {"succeeded", "failed", "cancelled", "orphaned"}
DEFAULT_STATE_ROOT = Path.home() / ".local/state/bureau"
DEFAULT_BACKUP_ROOT = Path.home() / "artifacts/merges/bureau-state-backups"
DEFAULT_RESTORE_RECEIPT_ROOT = DEFAULT_BACKUP_ROOT / "restore-tests"
DEFAULT_RUNTIME_MANIFEST = Path.home() / ".local/share/bureau/deployment-manifest.json"
DEFAULT_RUNTIME_PREFIX = Path.home() / ".local/share/bureau"
DEFAULT_REGISTRY_SNAPSHOT_ROOT = DEFAULT_RUNTIME_PREFIX / "registry-snapshots"
DEFAULT_CLOSURE_ROOT = Path.home() / ".local/state/bureau-closure"
LOCAL_RETENTION_POLICY = {
    "schema_version": 1,
    "kind": "bureau_local_retention_policy",
    "policy_id": "bureau-local-retention-v1",
    "stores": {
        "state_backups": {
            "recent_seconds": 2 * 24 * 60 * 60,
            "recent_max_count": 192,
            "restore_receipt_max_age_seconds": 36 * 60 * 60,
            "tiers": [
                {"seconds": 24 * 60 * 60, "count": 30},
                {"seconds": 7 * 24 * 60 * 60, "count": 26},
                {"seconds": 30 * 24 * 60 * 60, "count": 12},
            ],
        },
        "registry_snapshots": {
            "recent_seconds": 14 * 24 * 60 * 60,
            "recent_max_count": 64,
            "rollback_manifest_depth": 8,
            "tiers": [
                {"seconds": 7 * 24 * 60 * 60, "count": 26},
                {"seconds": 30 * 24 * 60 * 60, "count": 12},
            ],
        },
        "review_receipts": {
            "recent_seconds": 14 * 24 * 60 * 60,
            "recent_max_count": 336,
            "tiers": [
                {"seconds": 24 * 60 * 60, "count": 90},
                {"seconds": 7 * 24 * 60 * 60, "count": 26},
                {"seconds": 30 * 24 * 60 * 60, "count": 12},
            ],
        },
    },
}


class StateBackupError(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _utc_now()).isoformat().replace("+00:00", "Z")


def _timestamp(value: datetime | None = None) -> str:
    return (value or _utc_now()).strftime("%Y%m%dT%H%M%S.%fZ")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def _require_regular(path: Path, *, label: str) -> Path:
    raw = path.expanduser()
    if raw.is_symlink() or not raw.is_file():
        raise StateBackupError(f"{label} must be a regular file: {raw}")
    return raw.resolve()


def _load_json_file(path: Path, *, label: str) -> tuple[dict[str, Any], bytes]:
    regular = _require_regular(path, label=label)
    data = regular.read_bytes()
    try:
        value = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise StateBackupError(f"{label} is invalid JSON: {regular}") from exc
    if not isinstance(value, dict):
        raise StateBackupError(f"{label} must contain a JSON object: {regular}")
    return value, data


def _readonly_connection(path: Path) -> sqlite3.Connection:
    regular = _require_regular(path, label="state database")
    connection = sqlite3.connect(f"file:{regular}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def _database_integrity(connection: sqlite3.Connection) -> dict[str, Any]:
    integrity_rows = [row[0] for row in connection.execute("PRAGMA integrity_check")]
    foreign_key_rows = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
    if integrity_rows != ["ok"]:
        raise StateBackupError(f"SQLite integrity_check failed: {integrity_rows!r}")
    if foreign_key_rows:
        raise StateBackupError(f"SQLite foreign_key_check failed: {foreign_key_rows!r}")
    return {"integrity": "ok", "foreign_key_errors": []}


def _projection_evidence(connection: sqlite3.Connection) -> dict[str, Any]:
    rows = [
        dict(row)
        for row in connection.execute(
            "SELECT event_id,run_id,event_type,event_schema_version,payload_json "
            "FROM events ORDER BY event_id"
        )
    ]
    base_rows, task_spec_rows = task_specs.split_event_rows(rows)
    try:
        current = state_events.current_projection(connection)
        replayed = state_events.replay(base_rows)
        current_root = state_events.projection_root(current)
        task_spec_replay = task_specs.verify_replay(connection, task_spec_rows)
    except (state_events.StateEventError, task_specs.TaskSpecError) as exc:
        raise StateBackupError(str(exc)) from exc
    if replayed["root_sha256"] != current_root:
        raise StateBackupError("replayed operational projection differs from current StateStore")
    authoritative_projection = {
        "schema_version": 1,
        "operational": replayed["projection"],
        "task_specs": task_spec_replay["projection"],
    }
    authoritative_root = legacy.sha256_json(authoritative_projection)
    current_authoritative = {
        "schema_version": 1,
        "operational": current,
        "task_specs": task_spec_replay["projection"],
    }
    current_authoritative_root = legacy.sha256_json(current_authoritative)
    if authoritative_root != current_authoritative_root:
        raise StateBackupError("replayed authoritative projection differs from current StateStore")
    return {
        "event_count": len(rows),
        "operational_root_sha256": replayed["root_sha256"],
        "task_spec_root_sha256": task_spec_replay["root_sha256"],
        "authoritative_root_sha256": authoritative_root,
        "current_authoritative_root_sha256": current_authoritative_root,
        "matches_current": True,
    }


def _online_backup(source: Path, destination: Path) -> dict[str, Any]:
    source = _require_regular(source, label="source state database")
    if destination.exists() or destination.is_symlink():
        raise StateBackupError(f"backup destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    source_connection = _readonly_connection(source)
    destination_connection = sqlite3.connect(destination, timeout=30)
    try:
        source_connection.backup(destination_connection)
        destination_connection.commit()
        destination_connection.row_factory = sqlite3.Row
        destination_connection.execute("PRAGMA foreign_keys=ON")
        integrity = _database_integrity(destination_connection)
    except Exception:
        destination_connection.close()
        source_connection.close()
        destination.unlink(missing_ok=True)
        raise
    destination_connection.close()
    source_connection.close()
    os.chmod(destination, 0o600)
    return {**integrity, "sha256": _sha256_file(destination), "bytes": destination.stat().st_size}


def _logical_json_digest(kind: str, payload: dict[str, Any]) -> str:
    if kind == "receipt":
        payload = {key: value for key, value in payload.items() if key != "receipt_sha256"}
    return legacy.sha256_json(payload)


def _copy_bound_json(
    *,
    kind: str,
    rows: Iterable[sqlite3.Row],
    source_dir: Path,
    destination_dir: Path,
) -> list[dict[str, Any]]:
    destination_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    entries: list[dict[str, Any]] = []
    json_column = f"{kind}_json"
    sha_column = f"{kind}_sha256"
    for row in rows:
        run_id = row["run_id"]
        try:
            stored = json.loads(row[json_column])
        except (TypeError, json.JSONDecodeError) as exc:
            raise StateBackupError(f"stored {kind} JSON is invalid for {run_id}") from exc
        if not isinstance(stored, dict):
            raise StateBackupError(f"stored {kind} must be an object for {run_id}")
        expected = row[sha_column]
        if _logical_json_digest(kind, stored) != expected:
            raise StateBackupError(f"stored {kind} digest mismatch for {run_id}")
        source_path = source_dir / f"{run_id}.json"
        materialized, raw = _load_json_file(source_path, label=f"{kind}:{run_id}")
        if _logical_json_digest(kind, materialized) != expected:
            raise StateBackupError(f"materialized {kind} digest mismatch for {run_id}")
        destination_path = destination_dir / source_path.name
        destination_path.write_bytes(raw)
        os.chmod(destination_path, 0o600)
        if destination_path.read_bytes() != raw:
            raise StateBackupError(f"copied {kind} readback mismatch for {run_id}")
        entries.append(
            {
                "run_id": run_id,
                "logical_sha256": expected,
                "file_sha256": _sha256_bytes(raw),
                "bytes": len(raw),
            }
        )
    return entries


def _bound_files_from_snapshot(
    connection: sqlite3.Connection,
    *,
    source_state_root: Path,
    destination_root: Path,
) -> dict[str, Any]:
    envelope_rows = list(
        connection.execute("SELECT run_id,envelope_json,envelope_sha256 FROM runs ORDER BY run_id")
    )
    receipt_rows = list(
        connection.execute(
            "SELECT run_id,receipt_json,receipt_sha256 FROM receipts ORDER BY run_id"
        )
    )
    envelopes = _copy_bound_json(
        kind="envelope",
        rows=envelope_rows,
        source_dir=source_state_root / "envelopes",
        destination_dir=destination_root / "envelopes",
    )
    receipts = _copy_bound_json(
        kind="receipt",
        rows=receipt_rows,
        source_dir=source_state_root / "receipts",
        destination_dir=destination_root / "receipts",
    )
    return {
        "envelopes": envelopes,
        "receipts": receipts,
        "envelope_count": len(envelopes),
        "receipt_count": len(receipts),
        "envelope_root_sha256": _sha256_bytes(_canonical_bytes(envelopes)),
        "receipt_root_sha256": _sha256_bytes(_canonical_bytes(receipts)),
    }


def _manifest_without_digest(manifest: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in manifest.items() if key != "manifest_sha256"}


def _write_json_private(path: Path, payload: dict[str, Any]) -> None:
    if path.exists() or path.is_symlink():
        raise StateBackupError(f"refusing to overwrite existing evidence: {path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    path.write_text(data, encoding="utf-8")
    os.chmod(path, 0o600)


def create_backup(
    *,
    state_root: Path = DEFAULT_STATE_ROOT,
    backup_root: Path = DEFAULT_BACKUP_ROOT,
    now: datetime | None = None,
) -> dict[str, Any]:
    state_root = state_root.expanduser().resolve()
    source_db = _require_regular(state_root / "bureau.sqlite3", label="source state database")
    backup_root = backup_root.expanduser().resolve()
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backup_root, 0o700)
    created_at = now or _utc_now()
    temporary = backup_root / f".{_timestamp(created_at)}-{uuid.uuid4().hex}.tmp"
    temporary.mkdir(mode=0o700)
    try:
        database_path = temporary / "bureau.sqlite3"
        database = _online_backup(source_db, database_path)
        connection = _readonly_connection(database_path)
        try:
            projection = _projection_evidence(connection)
            bound_files = _bound_files_from_snapshot(
                connection,
                source_state_root=state_root,
                destination_root=temporary,
            )
        finally:
            connection.close()
        bundle_id = f"{_timestamp(created_at)}-{projection['authoritative_root_sha256'][:12]}"
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "kind": "bureau_state_backup_manifest",
            "bundle_id": bundle_id,
            "created_at": _iso(created_at),
            "database": database,
            "projection": projection,
            "bound_files": bound_files,
            "offsite": {
                "staging_contract": "existing-restic-source",
                "staging_root": str(backup_root),
                "existing_backup_source": str(Path.home() / "artifacts/merges"),
                "existing_backup_command": str(Path.home() / ".local/bin/heim-pc-restic-backup")
                + " run",
                "encryption": "restic",
                "does_not_establish": ["offsite_snapshot_completed"],
            },
            "does_not_include": [
                "Grabowski leases",
                "live external process state",
                "GitHub state",
            ],
        }
        manifest["manifest_sha256"] = _sha256_bytes(_canonical_bytes(manifest))
        _write_json_private(temporary / "manifest.json", manifest)
        final = backup_root / bundle_id
        if final.exists() or final.is_symlink():
            raise StateBackupError(f"backup bundle already exists: {final}")
        temporary.rename(final)
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "bureau_state_backup_result",
            "status": "created",
            "bundle": str(final),
            "bundle_id": bundle_id,
            "manifest_sha256": manifest["manifest_sha256"],
            "authoritative_root_sha256": projection["authoritative_root_sha256"],
            "event_count": projection["event_count"],
            "envelope_count": bound_files["envelope_count"],
            "receipt_count": bound_files["receipt_count"],
            "offsite_staging": str(backup_root),
        }
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _verify_entry_set(
    *,
    kind: str,
    entries: Any,
    root: Path,
    expected_by_run: dict[str, str],
) -> str:
    if not isinstance(entries, list):
        raise StateBackupError(f"manifest {kind} entries must be a list")
    observed_ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("run_id"), str):
            raise StateBackupError(f"manifest {kind} entry is invalid")
        run_id = entry["run_id"]
        if run_id in observed_ids:
            raise StateBackupError(f"duplicate {kind} entry for {run_id}")
        observed_ids.add(run_id)
        if expected_by_run.get(run_id) != entry.get("logical_sha256"):
            raise StateBackupError(f"manifest {kind} logical binding mismatch for {run_id}")
        payload, raw = _load_json_file(root / f"{run_id}.json", label=f"backup {kind}:{run_id}")
        if _logical_json_digest(kind, payload) != entry.get("logical_sha256"):
            raise StateBackupError(f"backup {kind} logical digest mismatch for {run_id}")
        if _sha256_bytes(raw) != entry.get("file_sha256"):
            raise StateBackupError(f"backup {kind} file digest mismatch for {run_id}")
    if observed_ids != set(expected_by_run):
        missing = sorted(set(expected_by_run) - observed_ids)
        extra = sorted(observed_ids - set(expected_by_run))
        raise StateBackupError(
            f"manifest {kind} coverage mismatch: missing={missing} extra={extra}"
        )
    return _sha256_bytes(_canonical_bytes(entries))


def _verify_bound_files(
    connection: sqlite3.Connection,
    *,
    root: Path,
    bound_files: dict[str, Any],
) -> dict[str, str]:
    envelope_expected = {
        row["run_id"]: row["envelope_sha256"]
        for row in connection.execute("SELECT run_id,envelope_sha256 FROM runs")
    }
    receipt_expected = {
        row["run_id"]: row["receipt_sha256"]
        for row in connection.execute("SELECT run_id,receipt_sha256 FROM receipts")
    }
    envelope_root = _verify_entry_set(
        kind="envelope",
        entries=bound_files.get("envelopes"),
        root=root / "envelopes",
        expected_by_run=envelope_expected,
    )
    receipt_root = _verify_entry_set(
        kind="receipt",
        entries=bound_files.get("receipts"),
        root=root / "receipts",
        expected_by_run=receipt_expected,
    )
    if envelope_root != bound_files.get("envelope_root_sha256"):
        raise StateBackupError("backup envelope root mismatch")
    if receipt_root != bound_files.get("receipt_root_sha256"):
        raise StateBackupError("backup receipt root mismatch")
    return {
        "envelope_root_sha256": envelope_root,
        "receipt_root_sha256": receipt_root,
    }


def _verify_backup_impl(bundle: Path) -> dict[str, Any]:
    bundle = bundle.expanduser().resolve()
    if bundle.is_symlink() or not bundle.is_dir():
        raise StateBackupError(f"backup bundle must be a real directory: {bundle}")
    manifest, _raw = _load_json_file(bundle / "manifest.json", label="backup manifest")
    if (
        manifest.get("kind") != "bureau_state_backup_manifest"
        or manifest.get("schema_version") != SCHEMA_VERSION
    ):
        raise StateBackupError("unsupported backup manifest")
    expected_manifest = manifest.get("manifest_sha256")
    observed_manifest = _sha256_bytes(_canonical_bytes(_manifest_without_digest(manifest)))
    if expected_manifest != observed_manifest:
        raise StateBackupError("backup manifest digest mismatch")
    database_path = _require_regular(bundle / "bureau.sqlite3", label="backup state database")
    database = manifest.get("database")
    if not isinstance(database, dict) or _sha256_file(database_path) != database.get("sha256"):
        raise StateBackupError("backup database digest mismatch")
    connection = _readonly_connection(database_path)
    try:
        _database_integrity(connection)
        projection = _projection_evidence(connection)
        expected_projection = manifest.get("projection")
        if not isinstance(expected_projection, dict):
            raise StateBackupError("backup projection evidence is missing")
        for key in (
            "event_count",
            "operational_root_sha256",
            "task_spec_root_sha256",
            "authoritative_root_sha256",
            "current_authoritative_root_sha256",
        ):
            if projection.get(key) != expected_projection.get(key):
                raise StateBackupError(f"backup projection mismatch for {key}")
        bound_files = manifest.get("bound_files")
        if not isinstance(bound_files, dict):
            raise StateBackupError("backup bound_files evidence is missing")
        bound_roots = _verify_bound_files(
            connection,
            root=bundle,
            bound_files=bound_files,
        )
        active_runs = [
            {"run_id": row["run_id"], "recorded_state": row["state"]}
            for row in connection.execute("SELECT run_id,state FROM runs ORDER BY run_id")
            if row["state"] not in TERMINAL_RUN_STATES
        ]
    finally:
        connection.close()
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "bureau_state_backup_verification",
        "status": "verified",
        "bundle": str(bundle),
        "bundle_id": manifest.get("bundle_id"),
        "manifest_sha256": expected_manifest,
        "authoritative_root_sha256": projection["authoritative_root_sha256"],
        "event_count": projection["event_count"],
        **bound_roots,
        "nonterminal_runs": active_runs,
    }


def verify_backup(bundle: Path) -> dict[str, Any]:
    """Verify one untrusted backup bundle through a single fail-closed boundary."""
    try:
        return _verify_backup_impl(bundle)
    except StateBackupError:
        raise
    except Exception as exc:
        raise StateBackupError("backup verification failed") from exc


def latest_bundle(backup_root: Path = DEFAULT_BACKUP_ROOT) -> Path:
    backup_root = backup_root.expanduser().resolve()
    if backup_root.is_symlink() or not backup_root.is_dir():
        raise StateBackupError(f"backup root does not exist: {backup_root}")
    candidates = sorted(
        path
        for path in backup_root.iterdir()
        if path.is_dir() and not path.is_symlink() and not path.name.startswith(".")
    )
    for candidate in reversed(candidates):
        try:
            verify_backup(candidate)
        except StateBackupError:
            continue
        return candidate
    raise StateBackupError("no verified Bureau state backup exists")


def _runtime_registry_root(
    manifest_path: Path = DEFAULT_RUNTIME_MANIFEST,
) -> Path:
    manifest, _raw = _load_json_file(manifest_path, label="Bureau runtime manifest")
    value = manifest.get("canonical_registry_root")
    if not isinstance(value, str) or not value:
        raise StateBackupError("Bureau runtime manifest lacks canonical_registry_root")
    raw = Path(value).expanduser()
    if raw.is_symlink() or not raw.is_dir():
        raise StateBackupError(f"canonical runtime registry is unavailable: {raw}")
    return raw.resolve()


def _default_adapter_registry():
    from .adapters import AdapterRegistry
    from .cli import default_grabowski_source

    registry = AdapterRegistry()
    candidate = default_grabowski_source()
    if candidate is None:
        return registry
    try:
        from .grabowski_adapter import GrabowskiTaskAdapter

        registry.add(GrabowskiTaskAdapter(candidate))
    except Exception as exc:
        registry.mark_unavailable("grabowski-task", exc)
        registry.mark_unavailable("grabowski-job", exc)
    return registry


def _post_restore_reconcile(
    restored_root: Path,
    *,
    registry_root: Path | None = None,
    adapters: Any | None = None,
) -> dict[str, Any]:
    from .core import Dispatcher, Registry, StateStore

    resolved_registry = (
        registry_root.expanduser().resolve()
        if registry_root is not None
        else _runtime_registry_root()
    )
    if resolved_registry.is_symlink() or not resolved_registry.is_dir():
        raise StateBackupError(f"restore reconcile registry is unavailable: {resolved_registry}")
    restored_store = StateStore(
        restored_root / "bureau.sqlite3",
        state_root=restored_root,
    )
    adapter_registry = adapters if adapters is not None else _default_adapter_registry()
    try:
        dispatcher = Dispatcher(
            Registry.load(resolved_registry),
            restored_store,
            adapters=adapter_registry,
        )
        reconcile = dispatcher.reconcile(stale_after=0)
        unobserved = reconcile.get("unobserved", [])
        if unobserved:
            raise StateBackupError(
                "post-restore external reconciliation is unobserved: "
                + legacy.canonical_json(unobserved)
            )
        post_projection = restored_store.replay_projection()
        with restored_store.connect() as connection:
            remaining = [
                {
                    "run_id": row["run_id"],
                    "recorded_state": row["state"],
                    "external_system": row["external_system"],
                    "external_id": row["external_id"],
                }
                for row in connection.execute(
                    "SELECT run_id,state,external_system,external_id FROM runs ORDER BY run_id"
                )
                if row["state"] not in TERMINAL_RUN_STATES
            ]
            reservation_count = int(
                connection.execute("SELECT COUNT(*) FROM reservations").fetchone()[0]
            )
    except StateBackupError:
        raise
    except Exception as exc:
        raise StateBackupError(f"post-restore reconciliation failed: {exc}") from exc
    return {
        "status": "reconciled",
        "mode": "fresh-external-readback",
        "default": "fail-closed",
        "lease_reactivation": False,
        "result": reconcile,
        "remaining_nonterminal_runs": remaining,
        "remaining_reservation_count": reservation_count,
        "post_reconcile_authoritative_root_sha256": post_projection["authoritative_root_sha256"],
    }


def restore_test(
    *,
    bundle: Path | None = None,
    backup_root: Path = DEFAULT_BACKUP_ROOT,
    scratch_root: Path | None = None,
    receipt_path: Path | None = None,
    registry_root: Path | None = None,
    adapters: Any | None = None,
) -> dict[str, Any]:
    selected = bundle.expanduser().resolve() if bundle is not None else latest_bundle(backup_root)
    source_verification = verify_backup(selected)
    manifest, _raw = _load_json_file(selected / "manifest.json", label="backup manifest")
    bound_files = manifest.get("bound_files")
    if not isinstance(bound_files, dict):
        raise StateBackupError("backup bound_files evidence is missing")
    parent = scratch_root.expanduser().resolve() if scratch_root is not None else None
    if parent is not None:
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = Path(tempfile.mkdtemp(prefix="bureau-state-restore-test-", dir=parent))
    restored_root = temporary / "state"
    restored_root.mkdir(mode=0o700)
    try:
        shutil.copy2(selected / "bureau.sqlite3", restored_root / "bureau.sqlite3")
        for dirname in ("envelopes", "receipts"):
            source_dir = selected / dirname
            destination_dir = restored_root / dirname
            destination_dir.mkdir(mode=0o700)
            for source in sorted(source_dir.glob("*.json")):
                if source.is_symlink() or not source.is_file():
                    raise StateBackupError(f"restore source is not regular: {source}")
                shutil.copy2(source, destination_dir / source.name)
        restored_db = _readonly_connection(restored_root / "bureau.sqlite3")
        try:
            _database_integrity(restored_db)
            restored_projection = _projection_evidence(restored_db)
            restored_bound_roots = _verify_bound_files(
                restored_db,
                root=restored_root,
                bound_files=bound_files,
            )
            nonterminal = [
                {"run_id": row["run_id"], "recorded_state": row["state"]}
                for row in restored_db.execute("SELECT run_id,state FROM runs ORDER BY run_id")
                if row["state"] not in TERMINAL_RUN_STATES
            ]
        finally:
            restored_db.close()
        if (
            restored_projection["authoritative_root_sha256"]
            != source_verification["authoritative_root_sha256"]
        ):
            raise StateBackupError("restored authoritative root differs from backup")
        for key in ("envelope_root_sha256", "receipt_root_sha256"):
            if restored_bound_roots[key] != source_verification[key]:
                raise StateBackupError(f"restored {key} differs from backup")
        post_reconcile = _post_restore_reconcile(
            restored_root,
            registry_root=registry_root,
            adapters=adapters,
        )
        result = {
            "schema_version": SCHEMA_VERSION,
            "kind": "bureau_state_restore_test_receipt",
            "status": "verified",
            "tested_at": _iso(),
            "bundle": str(selected),
            "manifest_sha256": source_verification["manifest_sha256"],
            "authoritative_root_sha256": restored_projection["authoritative_root_sha256"],
            "event_count": restored_projection["event_count"],
            **restored_bound_roots,
            "empty_target_created": True,
            "external_leases_restored": False,
            "external_state_reused": False,
            "nonterminal_runs_before_reconcile": nonterminal,
            "post_restore_reconcile": post_reconcile,
        }
        result["receipt_sha256"] = _sha256_bytes(_canonical_bytes(result))
        if receipt_path is not None:
            receipt_path = receipt_path.expanduser().resolve()
            receipt_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary_receipt = receipt_path.with_name(
                f".{receipt_path.name}.{uuid.uuid4().hex}.tmp"
            )
            _write_json_private(temporary_receipt, result)
            os.replace(temporary_receipt, receipt_path)
            os.chmod(receipt_path, 0o600)
        return result
    finally:
        shutil.rmtree(temporary, ignore_errors=True)



def _retention_material(value: dict[str, Any], digest_field: str) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != digest_field}


def _retention_digest(value: dict[str, Any], digest_field: str) -> str:
    return _sha256_bytes(_canonical_bytes(_retention_material(value, digest_field)))


def _validate_retention_policy(policy: dict[str, Any]) -> dict[str, Any]:
    if (
        not isinstance(policy, dict)
        or policy.get("schema_version") != 1
        or policy.get("kind") != "bureau_local_retention_policy"
        or not isinstance(policy.get("policy_id"), str)
    ):
        raise StateBackupError("local retention policy contract is invalid")
    stores = policy.get("stores")
    if not isinstance(stores, dict) or set(stores) != {
        "state_backups",
        "registry_snapshots",
        "review_receipts",
    }:
        raise StateBackupError("local retention policy store set is invalid")
    for store_name, store_policy in stores.items():
        if not isinstance(store_policy, dict):
            raise StateBackupError(f"retention policy is invalid for {store_name}")
        recent_seconds = store_policy.get("recent_seconds")
        recent_max_count = store_policy.get("recent_max_count")
        if (
            isinstance(recent_seconds, bool)
            or not isinstance(recent_seconds, int)
            or recent_seconds < 0
            or isinstance(recent_max_count, bool)
            or not isinstance(recent_max_count, int)
            or recent_max_count < 1
        ):
            raise StateBackupError(f"retention recent window is invalid for {store_name}")
        tiers = store_policy.get("tiers")
        if not isinstance(tiers, list):
            raise StateBackupError(f"retention tiers are invalid for {store_name}")
        previous_seconds = 0
        for tier in tiers:
            if not isinstance(tier, dict) or set(tier) != {"seconds", "count"}:
                raise StateBackupError(f"retention tier shape is invalid for {store_name}")
            seconds = tier["seconds"]
            count = tier["count"]
            if (
                isinstance(seconds, bool)
                or not isinstance(seconds, int)
                or seconds <= previous_seconds
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count < 1
            ):
                raise StateBackupError(f"retention tier value is invalid for {store_name}")
            previous_seconds = seconds
        if store_name == "state_backups":
            max_age = store_policy.get("restore_receipt_max_age_seconds")
            if (
                isinstance(max_age, bool)
                or not isinstance(max_age, int)
                or max_age < 60 * 60
                or max_age > 7 * 24 * 60 * 60
            ):
                raise StateBackupError("backup restore receipt max age is invalid")
        if store_name == "registry_snapshots":
            depth = store_policy.get("rollback_manifest_depth")
            if isinstance(depth, bool) or not isinstance(depth, int) or not 1 <= depth <= 64:
                raise StateBackupError("registry rollback manifest depth is invalid")
    return json.loads(json.dumps(policy))


def _parse_retention_time(value: Any, *, label: str) -> int:
    if not isinstance(value, str) or not value:
        raise StateBackupError(f"{label} timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StateBackupError(f"{label} timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise StateBackupError(f"{label} timestamp lacks timezone")
    return int(parsed.timestamp())


def _retention_root(path: Path, *, label: str) -> Path:
    raw = path.expanduser()
    if raw.is_symlink() or not raw.is_dir():
        raise StateBackupError(f"{label} must be a real directory: {raw}")
    return raw.resolve()


def _retention_reference(
    raw: Any,
    *,
    root: Path,
    label: str,
) -> Path:
    if not isinstance(raw, str) or not raw:
        raise StateBackupError(f"{label} reference is invalid")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise StateBackupError(f"{label} reference must be absolute")
    resolved = path.resolve(strict=False)
    if resolved.parent != root and not resolved.is_relative_to(root):
        raise StateBackupError(f"{label} reference escapes its managed root")
    return resolved


def _filesystem_preimage(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise StateBackupError(f"retention target is a symlink: {path}")
    try:
        path.lstat()
    except FileNotFoundError as exc:
        raise StateBackupError(f"retention target disappeared: {path}") from exc

    records: list[dict[str, Any]] = []

    def observe(candidate: Path, relative: str) -> int:
        metadata = candidate.lstat()
        if candidate.is_symlink():
            raise StateBackupError(f"retention tree contains symlink: {candidate}")
        records.append(
            {
                "path": relative,
                "device": int(metadata.st_dev),
                "inode": int(metadata.st_ino),
                "mode": int(metadata.st_mode),
                "size": int(metadata.st_size),
                "mtime_ns": int(metadata.st_mtime_ns),
                "blocks": int(metadata.st_blocks),
            }
        )
        return int(metadata.st_blocks) * 512

    total = observe(path, ".")
    if path.is_file():
        return {
            "allocated_bytes": total,
            "preimage_sha256": _sha256_bytes(_canonical_bytes(records)),
        }
    if not path.is_dir():
        raise StateBackupError(f"retention target has unsupported type: {path}")
    for parent, directories, files in os.walk(path, topdown=True, followlinks=False):
        directories.sort()
        files.sort()
        parent_path = Path(parent)
        for name in [*directories, *files]:
            child = parent_path / name
            total += observe(child, child.relative_to(path).as_posix())
    return {
        "allocated_bytes": total,
        "preimage_sha256": _sha256_bytes(_canonical_bytes(records)),
    }


def _keep_reasons(
    entries: list[dict[str, Any]],
    store_policy: dict[str, Any],
    *,
    now_unix: int,
    explicit: dict[str, set[str]],
) -> dict[str, set[str]]:
    kept: dict[str, set[str]] = {path: set(reasons) for path, reasons in explicit.items()}
    ordered = sorted(
        entries,
        key=lambda item: (int(item["created_at_unix"]), str(item["path"])),
        reverse=True,
    )
    if ordered:
        kept.setdefault(str(ordered[0]["path"]), set()).add("newest")
    recent = [
        item
        for item in ordered
        if 0 <= now_unix - int(item["created_at_unix"]) <= store_policy["recent_seconds"]
    ]
    for item in recent[: store_policy["recent_max_count"]]:
        kept.setdefault(str(item["path"]), set()).add("recent-window")
    for tier in store_policy["tiers"]:
        seconds = int(tier["seconds"])
        count = int(tier["count"])
        current_bucket = now_unix // seconds
        selected: set[int] = set()
        for item in ordered:
            created = int(item["created_at_unix"])
            if created > now_unix:
                continue
            bucket = created // seconds
            distance = current_bucket - bucket
            if distance < 0 or distance >= count or bucket in selected:
                continue
            selected.add(bucket)
            kept.setdefault(str(item["path"]), set()).add(
                f"tier:{seconds}:{distance}"
            )
    return kept


def _candidate_id(store: str, path: str, identity: dict[str, Any]) -> str:
    return _sha256_bytes(
        _canonical_bytes({"store": store, "path": path, "identity": identity})
    )


def _state_backup_entries(
    backup_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    entries: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for path in sorted(backup_root.iterdir()):
        if path.name == "restore-tests":
            continue
        if path.name.startswith("."):
            if path.name.startswith(".retention-delete-"):
                errors.append({"path": str(path), "reason": "unfinished-retention-delete"})
            continue
        if path.is_symlink() or not path.is_dir():
            errors.append({"path": str(path), "reason": "unexpected-backup-root-entry"})
            continue
        try:
            manifest, _raw = _load_json_file(path / "manifest.json", label="backup manifest")
            if (
                manifest.get("schema_version") != SCHEMA_VERSION
                or manifest.get("kind") != "bureau_state_backup_manifest"
            ):
                raise StateBackupError("unsupported backup manifest")
            manifest_sha = manifest.get("manifest_sha256")
            if manifest_sha != _retention_digest(manifest, "manifest_sha256"):
                raise StateBackupError("backup manifest digest mismatch")
            created = _parse_retention_time(
                manifest.get("created_at"), label="backup manifest created_at"
            )
            identity = {
                "bundle_id": manifest.get("bundle_id"),
                "manifest_sha256": manifest_sha,
                "created_at": manifest.get("created_at"),
            }
            if not isinstance(identity["bundle_id"], str) or not isinstance(
                manifest_sha, str
            ):
                raise StateBackupError("backup manifest identity is invalid")
            entries.append(
                {
                    "path": str(path.resolve()),
                    "created_at_unix": created,
                    "identity": identity,
                }
            )
        except StateBackupError as exc:
            errors.append({"path": str(path), "reason": str(exc)})
    return entries, errors


def _backup_explicit_references(
    backup_root: Path,
) -> tuple[dict[str, set[str]], list[dict[str, Any]]]:
    references: dict[str, set[str]] = {}
    errors: list[dict[str, Any]] = []
    receipt = backup_root / "restore-tests/latest.json"
    if receipt.exists() or receipt.is_symlink():
        try:
            payload, _raw = _load_json_file(receipt, label="latest restore test receipt")
            if payload.get("status") != "verified":
                raise StateBackupError("latest restore test receipt is not verified")
            bundle = _retention_reference(
                payload.get("bundle"), root=backup_root, label="restore test bundle"
            )
            references.setdefault(str(bundle), set()).add("latest-verified-restore-test")
        except StateBackupError as exc:
            errors.append({"path": str(receipt), "reason": str(exc)})
    return references, errors


def _registry_snapshot_entries(
    root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    entries: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for path in sorted(root.iterdir()):
        if path.name.startswith("."):
            if path.name.startswith(".retention-delete-"):
                errors.append({"path": str(path), "reason": "unfinished-retention-delete"})
            continue
        if path.is_symlink() or not path.is_dir():
            errors.append({"path": str(path), "reason": "unexpected-registry-root-entry"})
            continue
        try:
            inventory, raw = _load_json_file(
                path / ".bureau-runtime-snapshot.json",
                label="registry snapshot inventory",
            )
            if (
                inventory.get("schema_version") != 1
                or inventory.get("kind") != "bureau_registry_snapshot"
                or not isinstance(inventory.get("source_commit"), str)
                or not isinstance(inventory.get("tree_sha256"), str)
            ):
                raise StateBackupError("registry snapshot inventory contract is invalid")
            entries.append(
                {
                    "path": str(path.resolve()),
                    "created_at_unix": int(path.lstat().st_mtime),
                    "identity": {
                        "source_commit": inventory["source_commit"],
                        "tree_sha256": inventory["tree_sha256"],
                        "inventory_sha256": _sha256_bytes(raw),
                    },
                }
            )
        except StateBackupError as exc:
            errors.append({"path": str(path), "reason": str(exc)})
    return entries, errors


def _registry_manifest_reference(
    manifest: dict[str, Any],
    *,
    snapshot_root: Path,
    label: str,
) -> Path:
    return _retention_reference(
        manifest.get("canonical_registry_root"),
        root=snapshot_root,
        label=label,
    )


def _registry_explicit_references(
    runtime_prefix: Path,
    snapshot_root: Path,
    *,
    rollback_depth: int,
) -> tuple[dict[str, set[str]], list[dict[str, Any]]]:
    references: dict[str, set[str]] = {}
    errors: list[dict[str, Any]] = []
    current_path = runtime_prefix / "deployment-manifest.json"
    try:
        current, _raw = _load_json_file(current_path, label="runtime deployment manifest")
        current_registry = _registry_manifest_reference(
            current, snapshot_root=snapshot_root, label="current runtime registry"
        )
        references.setdefault(str(current_registry), set()).add("current-runtime")
        manifest = current
        seen_manifests: set[str] = set()
        for depth in range(rollback_depth):
            rollback = manifest.get("rollback")
            if not isinstance(rollback, dict) or not rollback.get("manifest"):
                break
            manifest_path = Path(str(rollback["manifest"])).expanduser().resolve(strict=False)
            backup_root = (runtime_prefix / "backups").resolve(strict=False)
            if not manifest_path.is_relative_to(backup_root):
                raise StateBackupError("runtime rollback manifest escapes backup root")
            marker = str(manifest_path)
            if marker in seen_manifests:
                raise StateBackupError("runtime rollback manifest chain contains a cycle")
            seen_manifests.add(marker)
            manifest, _raw = _load_json_file(
                manifest_path, label=f"runtime rollback manifest depth {depth + 1}"
            )
            registry = _registry_manifest_reference(
                manifest,
                snapshot_root=snapshot_root,
                label=f"rollback runtime registry depth {depth + 1}",
            )
            references.setdefault(str(registry), set()).add(
                f"rollback-manifest-depth:{depth + 1}"
            )
    except StateBackupError as exc:
        errors.append({"path": str(current_path), "reason": str(exc)})
    return references, errors


def _review_receipt_entries(
    review_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    entries: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for path in sorted(review_root.iterdir()):
        if path.name.startswith("."):
            continue
        if path.is_symlink() or not path.is_file():
            errors.append({"path": str(path), "reason": "unexpected-review-root-entry"})
            continue
        try:
            payload, raw = _load_json_file(path, label="closure review receipt")
            reviewed_at = _parse_retention_time(
                payload.get("reviewed_at"), label="review receipt reviewed_at"
            )
            run_id = payload.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                raise StateBackupError("review receipt lacks run_id")
            entries.append(
                {
                    "path": str(path.resolve()),
                    "created_at_unix": reviewed_at,
                    "identity": {
                        "run_id": run_id,
                        "file_sha256": _sha256_bytes(raw),
                    },
                }
            )
        except StateBackupError as exc:
            errors.append({"path": str(path), "reason": str(exc)})
    return entries, errors


def _review_explicit_references(
    closure_root: Path,
    review_root: Path,
) -> tuple[dict[str, set[str]], list[dict[str, Any]]]:
    references: dict[str, set[str]] = {}
    errors: list[dict[str, Any]] = []

    def protect(value: Any, reason: str) -> None:
        if value is None:
            return
        path = _retention_reference(value, root=review_root, label=reason)
        references.setdefault(str(path), set()).add(reason)

    for name in ("review-latest.json", "lanes.json"):
        path = closure_root / name
        try:
            payload, _raw = _load_json_file(path, label=f"closure {name}")
            if name == "review-latest.json":
                protect(payload.get("receipt_path"), "review-latest")
            else:
                protect(payload.get("latest_review_receipt"), "lanes-latest-review")
                lanes = payload.get("lanes")
                if not isinstance(lanes, list):
                    raise StateBackupError("closure lanes array is invalid")
                for lane in lanes:
                    if not isinstance(lane, dict):
                        continue
                    evidence = lane.get("review_evidence")
                    if isinstance(evidence, dict):
                        protect(evidence.get("receipt_path"), "lane-review-binding")
        except StateBackupError as exc:
            errors.append({"path": str(path), "reason": str(exc)})
    return references, errors


def _build_store_retention(
    store: str,
    entries: list[dict[str, Any]],
    *,
    policy: dict[str, Any],
    now_unix: int,
    explicit: dict[str, set[str]],
    errors: list[dict[str, Any]],
) -> dict[str, Any]:
    entry_paths = {str(item["path"]) for item in entries}
    for path, reasons in explicit.items():
        if path not in entry_paths:
            errors.append(
                {
                    "path": path,
                    "reason": "protected-reference-target-missing",
                    "reference_reasons": sorted(reasons),
                }
            )
    keep = _keep_reasons(entries, policy, now_unix=now_unix, explicit=explicit)
    candidates: list[dict[str, Any]] = []
    retained: list[dict[str, Any]] = []
    for item in sorted(entries, key=lambda row: str(row["path"])):
        path = str(item["path"])
        reasons = sorted(keep.get(path, set()))
        identity = dict(item["identity"])
        record = {
            "path": path,
            "created_at_unix": int(item["created_at_unix"]),
            "identity": identity,
        }
        if reasons:
            retained.append({**record, "reasons": reasons})
            continue
        preimage = _filesystem_preimage(Path(path))
        identity["filesystem_preimage_sha256"] = preimage["preimage_sha256"]
        record["identity"] = identity
        candidates.append(
            {
                **record,
                "allocated_bytes": preimage["allocated_bytes"],
                "candidate_id": _candidate_id(store, path, identity),
                "reason": "outside-retention-and-unreferenced",
            }
        )
    return {
        "candidate_count": len(candidates),
        "candidate_allocated_bytes": sum(
            int(item["allocated_bytes"]) for item in candidates
        ),
        "retained_count": len(retained),
        "candidates": candidates,
        "retained": retained,
        "errors": errors,
    }


def build_local_retention_plan(
    *,
    backup_root: Path = DEFAULT_BACKUP_ROOT,
    runtime_prefix: Path = DEFAULT_RUNTIME_PREFIX,
    closure_root: Path = DEFAULT_CLOSURE_ROOT,
    policy: dict[str, Any] | None = None,
    now_unix: int | None = None,
) -> dict[str, Any]:
    selected_policy = _validate_retention_policy(policy or LOCAL_RETENTION_POLICY)
    generated = int(_utc_now().timestamp()) if now_unix is None else int(now_unix)
    backup_root = _retention_root(backup_root, label="backup root")
    runtime_prefix = _retention_root(runtime_prefix, label="runtime prefix")
    snapshot_root = _retention_root(
        runtime_prefix / "registry-snapshots", label="registry snapshot root"
    )
    closure_root = _retention_root(closure_root, label="closure root")
    review_root = _retention_root(
        closure_root / "review-receipts", label="review receipt root"
    )

    backup_entries, backup_errors = _state_backup_entries(backup_root)
    backup_refs, backup_ref_errors = _backup_explicit_references(backup_root)
    registry_entries, registry_errors = _registry_snapshot_entries(snapshot_root)
    registry_refs, registry_ref_errors = _registry_explicit_references(
        runtime_prefix,
        snapshot_root,
        rollback_depth=selected_policy["stores"]["registry_snapshots"][
            "rollback_manifest_depth"
        ],
    )
    review_entries, review_errors = _review_receipt_entries(review_root)
    review_refs, review_ref_errors = _review_explicit_references(
        closure_root, review_root
    )

    stores = {
        "state_backups": _build_store_retention(
            "state_backups",
            backup_entries,
            policy=selected_policy["stores"]["state_backups"],
            now_unix=generated,
            explicit=backup_refs,
            errors=[*backup_errors, *backup_ref_errors],
        ),
        "registry_snapshots": _build_store_retention(
            "registry_snapshots",
            registry_entries,
            policy=selected_policy["stores"]["registry_snapshots"],
            now_unix=generated,
            explicit=registry_refs,
            errors=[*registry_errors, *registry_ref_errors],
        ),
        "review_receipts": _build_store_retention(
            "review_receipts",
            review_entries,
            policy=selected_policy["stores"]["review_receipts"],
            now_unix=generated,
            explicit=review_refs,
            errors=[*review_errors, *review_ref_errors],
        ),
    }
    errors = [
        {"store": store, **item}
        for store, result in stores.items()
        for item in result["errors"]
    ]
    plan = {
        "schema_version": 1,
        "kind": "bureau_local_retention_plan",
        "generated_at_unix": generated,
        "policy": selected_policy,
        "policy_sha256": _sha256_bytes(_canonical_bytes(selected_policy)),
        "roots": {
            "backup_root": str(backup_root),
            "runtime_prefix": str(runtime_prefix),
            "closure_root": str(closure_root),
        },
        "stores": stores,
        "summary": {
            "candidate_count": sum(
                int(result["candidate_count"]) for result in stores.values()
            ),
            "candidate_allocated_bytes": sum(
                int(result["candidate_allocated_bytes"]) for result in stores.values()
            ),
            "retained_count": sum(
                int(result["retained_count"]) for result in stores.values()
            ),
            "error_count": len(errors),
        },
        "errors": errors,
        "safe_to_apply": not errors,
        "automatic_cleanup_authorized": False,
        "does_not_establish": [
            "effect_authorization",
            "offsite_backup_completion",
            "permission_to_delete_runtime_backups",
            "permission_to_delete_unlisted_paths",
        ],
    }
    plan["plan_sha256"] = _retention_digest(plan, "plan_sha256")
    return plan


def _load_retention_plan(path: Path) -> dict[str, Any]:
    plan, _raw = _load_json_file(path, label="local retention plan")
    if (
        plan.get("schema_version") != 1
        or plan.get("kind") != "bureau_local_retention_plan"
        or plan.get("plan_sha256") != _retention_digest(plan, "plan_sha256")
    ):
        raise StateBackupError("local retention plan integrity is invalid")
    _validate_retention_policy(plan.get("policy"))
    return plan


def _registry_snapshot_tree_sha256(path: Path, identity: dict[str, Any]) -> str:
    inventory, raw = _load_json_file(
        path / ".bureau-runtime-snapshot.json", label="registry snapshot inventory"
    )
    if _sha256_bytes(raw) != identity.get("inventory_sha256"):
        raise StateBackupError("registry snapshot inventory changed after plan")
    if (
        inventory.get("source_commit") != identity.get("source_commit")
        or inventory.get("tree_sha256") != identity.get("tree_sha256")
    ):
        raise StateBackupError("registry snapshot identity changed after plan")
    paths = inventory.get("paths")
    if not isinstance(paths, list) or not paths:
        raise StateBackupError("registry snapshot inventory paths are invalid")
    digest = hashlib.sha256()
    for item in paths:
        if (
            not isinstance(item, str)
            or not item
            or Path(item).is_absolute()
            or ".." in Path(item).parts
        ):
            raise StateBackupError("registry snapshot inventory path is unsafe")
        relative = Path(item)
        source = path / relative
        regular = _require_regular(source, label="registry snapshot tracked file")
        if regular != source.resolve():
            raise StateBackupError("registry snapshot tracked path escaped root")
        encoded = relative.as_posix().encode()
        content = source.read_bytes()
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    observed = digest.hexdigest()
    if observed != identity.get("tree_sha256"):
        raise StateBackupError("registry snapshot tree changed after plan")
    return observed


def _candidate_identity_still_matches(store: str, candidate: dict[str, Any]) -> None:
    path = Path(str(candidate["path"]))
    identity = candidate.get("identity")
    if not isinstance(identity, dict):
        raise StateBackupError("retention candidate identity is invalid")
    preimage = _filesystem_preimage(path)
    if preimage["preimage_sha256"] != identity.get("filesystem_preimage_sha256"):
        raise StateBackupError("retention candidate filesystem preimage changed after plan")
    if store == "review_receipts":
        regular = _require_regular(path, label="review receipt candidate")
        if _sha256_file(regular) != identity.get("file_sha256"):
            raise StateBackupError("review receipt changed after plan")
        return
    if path.is_symlink() or not path.is_dir():
        raise StateBackupError(f"retention directory candidate is unavailable: {path}")
    if store == "state_backups":
        manifest, _raw = _load_json_file(path / "manifest.json", label="backup manifest")
        if (
            manifest.get("bundle_id") != identity.get("bundle_id")
            or manifest.get("manifest_sha256") != identity.get("manifest_sha256")
            or manifest.get("manifest_sha256")
            != _retention_digest(manifest, "manifest_sha256")
        ):
            raise StateBackupError("backup candidate changed after plan")
        return
    if store == "registry_snapshots":
        _registry_snapshot_tree_sha256(path, identity)
        return
    raise StateBackupError(f"unsupported retention store: {store}")


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_directory_candidate(path: Path, *, candidate_id: str) -> None:
    parent = path.parent
    quarantine = parent / f".retention-delete-{candidate_id}"
    if quarantine.exists() or quarantine.is_symlink():
        raise StateBackupError(f"retention quarantine already exists: {quarantine}")
    os.replace(path, quarantine)
    _fsync_directory(parent)
    try:
        for walk_root, directories, files in os.walk(
            quarantine, topdown=False, followlinks=False
        ):
            root = Path(walk_root)
            for name in files:
                child = root / name
                if child.is_symlink():
                    raise StateBackupError(
                        f"retention candidate gained symlink during delete: {child}"
                    )
                os.chmod(child, 0o600)
            for name in directories:
                child = root / name
                if child.is_symlink():
                    raise StateBackupError(
                        f"retention candidate gained symlink during delete: {child}"
                    )
                os.chmod(child, 0o700)
        os.chmod(quarantine, 0o700)
        shutil.rmtree(quarantine)
        _fsync_directory(parent)
    except Exception:
        if quarantine.exists() and not path.exists():
            with contextlib.suppress(Exception):
                os.replace(quarantine, path)
                _fsync_directory(parent)
        raise


def _atomic_replace_json(path: Path, payload: dict[str, Any]) -> None:
    path = path.expanduser().resolve(strict=False)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    data = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    temporary.write_text(data, encoding="utf-8")
    os.chmod(temporary, 0o600)
    with temporary.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)



def _validated_historical_restore_evidence(
    *,
    backup_root: Path,
    retained_paths: set[str],
    newest_path: Path,
    max_age_seconds: int,
    now_unix: int,
) -> dict[str, Any]:
    receipt_path = backup_root / "restore-tests" / "latest.json"
    receipt, _raw = _load_json_file(receipt_path, label="latest restore test receipt")
    if (
        receipt.get("schema_version") != 1
        or receipt.get("kind") != "bureau_state_restore_test_receipt"
        or receipt.get("status") != "verified"
        or receipt.get("receipt_sha256")
        != _retention_digest(receipt, "receipt_sha256")
    ):
        raise StateBackupError("historical restore receipt integrity is invalid")
    tested_at_unix = _parse_retention_time(
        receipt.get("tested_at"), label="historical restore tested_at"
    )
    age_seconds = now_unix - tested_at_unix
    if age_seconds < 0 or age_seconds > max_age_seconds:
        raise StateBackupError("historical restore receipt is stale")
    historical_path = _retention_reference(
        receipt.get("bundle"),
        root=backup_root,
        label="historical restore bundle",
    )
    if historical_path == newest_path:
        raise StateBackupError("historical restore point must differ from newest retained backup")
    if str(historical_path) not in retained_paths:
        raise StateBackupError("historical restore point is no longer retained")
    verification = verify_backup(historical_path)
    for field in (
        "manifest_sha256",
        "authoritative_root_sha256",
        "event_count",
        "envelope_root_sha256",
        "receipt_root_sha256",
    ):
        if verification.get(field) != receipt.get(field):
            raise StateBackupError(
                f"historical restore receipt differs from verified backup for {field}"
            )
    if (
        receipt.get("empty_target_created") is not True
        or receipt.get("external_leases_restored") is not False
        or receipt.get("external_state_reused") is not False
    ):
        raise StateBackupError("historical restore isolation evidence is invalid")
    reconcile = receipt.get("post_restore_reconcile")
    if (
        not isinstance(reconcile, dict)
        or reconcile.get("status") != "reconciled"
        or reconcile.get("mode") != "fresh-external-readback"
        or reconcile.get("default") != "fail-closed"
        or reconcile.get("lease_reactivation") is not False
    ):
        raise StateBackupError("historical restore reconciliation evidence is invalid")
    return {
        "status": "verified",
        "bundle": str(historical_path),
        "tested_at": receipt["tested_at"],
        "tested_at_unix": tested_at_unix,
        "age_seconds": age_seconds,
        "receipt_sha256": receipt["receipt_sha256"],
        "manifest_sha256": receipt["manifest_sha256"],
        "authoritative_root_sha256": receipt["authoritative_root_sha256"],
    }


def _retention_recovery_gate(
    plan: dict[str, Any],
    *,
    now_unix: int,
) -> dict[str, Any]:
    state_store = plan.get("stores", {}).get("state_backups")
    if not isinstance(state_store, dict):
        raise StateBackupError("retention plan lacks StateStore backup projection")
    candidates = state_store.get("candidates")
    retained = state_store.get("retained")
    if not isinstance(candidates, list) or not isinstance(retained, list):
        raise StateBackupError("retention StateStore projection is invalid")
    if not candidates:
        return {
            "status": "not-required",
            "reason": "no-state-backup-candidates",
        }
    if len(retained) < 2:
        raise StateBackupError("backup pruning requires at least two retained recovery points")
    roots = plan.get("roots")
    policy = plan.get("policy")
    if not isinstance(roots, dict) or not isinstance(policy, dict):
        raise StateBackupError("retention recovery contract is incomplete")
    backup_root = _retention_root(
        Path(str(roots.get("backup_root"))), label="backup root"
    )
    state_policy = policy.get("stores", {}).get("state_backups")
    if not isinstance(state_policy, dict):
        raise StateBackupError("backup retention policy is unavailable")
    max_age = state_policy.get("restore_receipt_max_age_seconds")
    if isinstance(max_age, bool) or not isinstance(max_age, int):
        raise StateBackupError("backup restore receipt max age is invalid")
    newest = max(
        retained,
        key=lambda item: (int(item["created_at_unix"]), str(item["path"])),
    )
    newest_path = _retention_reference(
        newest.get("path"),
        root=backup_root,
        label="newest retained backup",
    )
    retained_paths = {str(Path(str(item["path"])).resolve(strict=False)) for item in retained}
    historical = _validated_historical_restore_evidence(
        backup_root=backup_root,
        retained_paths=retained_paths,
        newest_path=newest_path,
        max_age_seconds=max_age,
        now_unix=now_unix,
    )
    registry_root = _runtime_registry_root()
    newest_restore = restore_test(
        bundle=newest_path,
        backup_root=backup_root,
        receipt_path=None,
        registry_root=registry_root,
    )
    if newest_restore.get("status") != "verified":
        raise StateBackupError("newest retained backup restore did not verify")
    return {
        "status": "verified",
        "historical": historical,
        "newest": {
            "bundle": str(newest_path),
            "tested_at": newest_restore.get("tested_at"),
            "manifest_sha256": newest_restore.get("manifest_sha256"),
            "authoritative_root_sha256": newest_restore.get(
                "authoritative_root_sha256"
            ),
            "receipt_sha256": newest_restore.get("receipt_sha256"),
        },
        "registry_root": str(registry_root),
        "distinct_recovery_points": historical["bundle"] != str(newest_path),
    }


def apply_local_retention_plan(
    plan: dict[str, Any],
    *,
    expected_plan_sha256: str,
    confirmation: str,
    receipt_path: Path,
    now_unix: int | None = None,
) -> dict[str, Any]:
    if (
        plan.get("schema_version") != 1
        or plan.get("kind") != "bureau_local_retention_plan"
        or plan.get("plan_sha256") != expected_plan_sha256
        or expected_plan_sha256 != _retention_digest(plan, "plan_sha256")
    ):
        raise StateBackupError("local retention plan hash mismatch")
    if plan.get("safe_to_apply") is not True:
        raise StateBackupError("local retention plan is blocked")
    if confirmation != f"APPLY:{expected_plan_sha256}":
        raise StateBackupError("local retention confirmation mismatch")
    roots = plan.get("roots")
    if not isinstance(roots, dict):
        raise StateBackupError("local retention plan roots are invalid")
    policy = _validate_retention_policy(plan.get("policy"))
    if receipt_path.is_symlink():
        raise StateBackupError("retention apply receipt must not be a symlink")
    receipt_path = receipt_path.expanduser().resolve(strict=False)
    if receipt_path.exists():
        existing, _raw = _load_json_file(receipt_path, label="retention apply receipt")
        if existing.get("receipt_sha256") != _retention_digest(
            existing, "receipt_sha256"
        ):
            raise StateBackupError("retention apply receipt integrity mismatch")
        if (
            existing.get("plan_sha256") == expected_plan_sha256
            and existing.get("state") == "complete"
        ):
            return {**existing, "replayed": True}
        raise StateBackupError("existing retention receipt is not a completed replay")
    fresh = build_local_retention_plan(
        backup_root=Path(str(roots.get("backup_root"))),
        runtime_prefix=Path(str(roots.get("runtime_prefix"))),
        closure_root=Path(str(roots.get("closure_root"))),
        policy=policy,
        now_unix=now_unix,
    )
    if fresh.get("safe_to_apply") is not True:
        raise StateBackupError("fresh local retention readback is blocked")
    fresh_candidates = {
        (store, str(candidate["candidate_id"])): candidate
        for store, result in fresh["stores"].items()
        for candidate in result["candidates"]
    }
    selected: list[tuple[str, dict[str, Any]]] = []
    for store, result in plan["stores"].items():
        for candidate in result["candidates"]:
            key = (store, str(candidate.get("candidate_id")))
            current = fresh_candidates.get(key)
            if current is None or current.get("identity") != candidate.get("identity"):
                raise StateBackupError(
                    f"retention candidate is no longer eligible: {candidate.get('path')}"
                )
            selected.append((store, candidate))
    recovery_gate = _retention_recovery_gate(
        fresh,
        now_unix=int(fresh["generated_at_unix"]),
    )
    receipt = {
        "schema_version": 1,
        "kind": "bureau_local_retention_apply_receipt",
        "state": "intent",
        "plan_sha256": expected_plan_sha256,
        "policy_sha256": plan.get("policy_sha256"),
        "fresh_plan_sha256": fresh.get("plan_sha256"),
        "started_at": _iso(),
        "candidate_count": len(selected),
        "recovery_gate": recovery_gate,
        "results": [],
        "current_candidate": None,
        "error": None,
    }
    receipt["receipt_sha256"] = _retention_digest(receipt, "receipt_sha256")
    _atomic_replace_json(receipt_path, receipt)
    try:
        for store, candidate in selected:
            _candidate_identity_still_matches(store, candidate)
            path = Path(str(candidate["path"]))
            receipt["state"] = "applying"
            receipt["current_candidate"] = {
                "store": store,
                "candidate_id": candidate["candidate_id"],
                "path": str(path),
            }
            receipt["receipt_sha256"] = _retention_digest(receipt, "receipt_sha256")
            _atomic_replace_json(receipt_path, receipt)
            if store == "review_receipts":
                path.unlink()
                _fsync_directory(path.parent)
            else:
                _remove_directory_candidate(
                    path, candidate_id=str(candidate["candidate_id"])
                )
            receipt["results"].append(
                {
                    "store": store,
                    "candidate_id": candidate["candidate_id"],
                    "path": str(path),
                    "allocated_bytes": candidate["allocated_bytes"],
                    "removed": not path.exists(),
                }
            )
            receipt["current_candidate"] = None
            receipt["receipt_sha256"] = _retention_digest(receipt, "receipt_sha256")
            _atomic_replace_json(receipt_path, receipt)
        receipt["state"] = "complete"
        receipt["completed_at"] = _iso()
        receipt["freed_allocated_bytes_claim"] = sum(
            int(item["allocated_bytes"]) for item in receipt["results"]
        )
        receipt["current_candidate"] = None
        receipt["receipt_sha256"] = _retention_digest(receipt, "receipt_sha256")
        _atomic_replace_json(receipt_path, receipt)
        return {**receipt, "replayed": False}
    except Exception as exc:
        receipt["state"] = "partial"
        receipt["error"] = {
            "class": type(exc).__name__,
            "message": str(exc)[:1000],
        }
        receipt["receipt_sha256"] = _retention_digest(receipt, "receipt_sha256")
        _atomic_replace_json(receipt_path, receipt)
        raise



def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bureau coherent StateStore backup and restore proof"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup = subparsers.add_parser("backup")
    backup.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    backup.add_argument("--backup-root", type=Path, default=DEFAULT_BACKUP_ROOT)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--bundle", type=Path, required=True)

    restore = subparsers.add_parser("restore-test")
    restore.add_argument("--bundle", type=Path)
    restore.add_argument("--backup-root", type=Path, default=DEFAULT_BACKUP_ROOT)
    restore.add_argument("--scratch-root", type=Path)
    restore.add_argument("--registry-root", type=Path)
    restore.add_argument(
        "--receipt", type=Path, default=DEFAULT_RESTORE_RECEIPT_ROOT / "latest.json"
    )

    retention_plan = subparsers.add_parser("retention-plan")
    retention_plan.add_argument("--backup-root", type=Path, default=DEFAULT_BACKUP_ROOT)
    retention_plan.add_argument("--runtime-prefix", type=Path, default=DEFAULT_RUNTIME_PREFIX)
    retention_plan.add_argument("--closure-root", type=Path, default=DEFAULT_CLOSURE_ROOT)
    retention_plan.add_argument("--output", type=Path)

    retention_apply = subparsers.add_parser("retention-apply")
    retention_apply.add_argument("--plan", type=Path, required=True)
    retention_apply.add_argument("--expected-plan-sha256", required=True)
    retention_apply.add_argument("--confirmation", required=True)
    retention_apply.add_argument("--receipt", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "backup":
            result = create_backup(state_root=args.state_root, backup_root=args.backup_root)
        elif args.command == "verify":
            result = verify_backup(args.bundle)
        elif args.command == "restore-test":
            result = restore_test(
                bundle=args.bundle,
                backup_root=args.backup_root,
                scratch_root=args.scratch_root,
                receipt_path=args.receipt,
                registry_root=args.registry_root,
            )
        elif args.command == "retention-plan":
            result = build_local_retention_plan(
                backup_root=args.backup_root,
                runtime_prefix=args.runtime_prefix,
                closure_root=args.closure_root,
            )
            if args.output is not None:
                _write_json_private(args.output.expanduser().resolve(strict=False), result)
        else:
            plan = _load_retention_plan(args.plan)
            result = apply_local_retention_plan(
                plan,
                expected_plan_sha256=args.expected_plan_sha256,
                confirmation=args.confirmation,
                receipt_path=args.receipt,
            )
    except StateBackupError as exc:
        print(
            json.dumps(
                {"schema_version": SCHEMA_VERSION, "status": "error", "error": str(exc)},
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())