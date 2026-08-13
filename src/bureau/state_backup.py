from __future__ import annotations

import argparse
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
DEFAULT_RESTORE_RECEIPT_ROOT = Path.home() / ".local/state/bureau-backup-restore-tests"
DEFAULT_RUNTIME_MANIFEST = Path.home() / ".local/share/bureau/deployment-manifest.json"


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
    except json.JSONDecodeError as exc:
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


def verify_backup(bundle: Path) -> dict[str, Any]:
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "backup":
            result = create_backup(state_root=args.state_root, backup_root=args.backup_root)
        elif args.command == "verify":
            result = verify_backup(args.bundle)
        else:
            result = restore_test(
                bundle=args.bundle,
                backup_root=args.backup_root,
                scratch_root=args.scratch_root,
                receipt_path=args.receipt,
                registry_root=args.registry_root,
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
