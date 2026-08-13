from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import legacy, state_events, task_specs
from .adapters import AdapterRegistry
from .runtime_refresh import DEFAULT_GRABOWSKI_RESOURCE_DB

SCHEMA_VERSION = 1
BUNDLE_KIND = "bureau_state_backup_bundle"
MANIFEST_NAME = "manifest.json"
DATABASE_NAME = "bureau.sqlite3"
ACTIVE_RUN_STATES = frozenset({"assigned", "running", "verifying"})


class BackupError(legacy.BureauError):
    """A bounded Bureau backup or restore contract failed."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return (legacy.canonical_json(value) + "\n").encode("utf-8")


def _ensure_regular_file(path: Path, label: str) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink() or not expanded.is_file():
        raise BackupError(f"{label} must be a regular file: {expanded}")
    return expanded.resolve()


def _ensure_private_file(path: Path, label: str) -> Path:
    resolved = _ensure_regular_file(path, label)
    details = resolved.stat()
    if details.st_uid != os.geteuid():
        raise BackupError(f"{label} owner is invalid")
    if stat.S_IMODE(details.st_mode) & 0o077:
        raise BackupError(f"{label} must not be group- or world-accessible")
    return resolved


def _ensure_private_directory(path: Path, label: str, *, create: bool) -> Path:
    expanded = path.expanduser()
    if expanded.exists():
        if expanded.is_symlink() or not expanded.is_dir():
            raise BackupError(f"{label} must be a real directory: {expanded}")
    elif create:
        expanded.mkdir(parents=True, mode=0o700)
    else:
        raise BackupError(f"{label} does not exist: {expanded}")
    resolved = expanded.resolve()
    if create:
        os.chmod(resolved, 0o700)
    return resolved


def _assert_child(path: Path, root: Path, label: str) -> Path:
    resolved = path.expanduser().resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise BackupError(f"{label} escaped its root: {resolved}") from exc
    return resolved


def _open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _integrity(connection: sqlite3.Connection) -> dict[str, Any]:
    integrity_rows = [tuple(row) for row in connection.execute("PRAGMA integrity_check").fetchall()]
    if integrity_rows != [("ok",)]:
        raise BackupError(f"SQLite integrity_check failed: {integrity_rows!r}")
    foreign_rows = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check").fetchall()]
    if foreign_rows:
        raise BackupError(f"SQLite foreign_key_check failed: {foreign_rows!r}")
    return {
        "integrity_check": "ok",
        "foreign_key_error_count": 0,
        "schema_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
    }


def _create_online_backup(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise BackupError(f"backup database already exists: {destination}")
    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    source_connection: sqlite3.Connection | None = None
    destination_connection: sqlite3.Connection | None = None
    try:
        source_connection = _open_read_only(source)
        destination_connection = sqlite3.connect(destination)
        source_connection.backup(destination_connection)
        destination_connection.commit()
        _integrity(destination_connection)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        if destination_connection is not None:
            destination_connection.close()
        if source_connection is not None:
            source_connection.close()
    os.chmod(destination, 0o600)


def _parse_json_object(raw: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise BackupError(f"{label} contains invalid JSON") from exc
    if not isinstance(value, dict):
        raise BackupError(f"{label} JSON root must be an object")
    return value


def _projection_contract(connection: sqlite3.Connection) -> dict[str, Any]:
    rows = [
        dict(row)
        for row in connection.execute(
            "SELECT event_id,run_id,event_type,event_schema_version,payload_json "
            "FROM events ORDER BY event_id"
        )
    ]
    base_rows, task_spec_rows = task_specs.split_event_rows(rows)
    current = state_events.current_projection(connection)
    replayed = state_events.replay(base_rows)
    current_root = state_events.projection_root(current)
    if replayed["root_sha256"] != current_root:
        raise BackupError("event replay root does not match the backed-up projection")
    task_spec_replay = task_specs.verify_replay(connection, task_spec_rows)
    authoritative = {
        "schema_version": 1,
        "operational": replayed["projection"],
        "task_specs": task_spec_replay["projection"],
    }
    authoritative_root = legacy.sha256_json(authoritative)
    return {
        "event_count": len(rows),
        "last_event_id": max((int(row["event_id"]) for row in rows), default=0),
        "operational_root_sha256": replayed["root_sha256"],
        "task_spec_root_sha256": task_spec_replay["root_sha256"],
        "authoritative_root_sha256": authoritative_root,
    }


def _materialize_evidence(connection: sqlite3.Connection, bundle_root: Path) -> dict[str, Any]:
    envelope_root = bundle_root / "envelopes"
    receipt_root = bundle_root / "receipts"
    envelope_root.mkdir(mode=0o700)
    receipt_root.mkdir(mode=0o700)

    envelope_index: list[dict[str, str]] = []
    for row in connection.execute(
        "SELECT run_id,envelope_json,envelope_sha256 FROM runs ORDER BY run_id"
    ):
        run_id = str(row["run_id"])
        value = _parse_json_object(row["envelope_json"], f"run {run_id} envelope")
        observed = legacy.sha256_json(value)
        expected = str(row["envelope_sha256"])
        if observed != expected:
            raise BackupError(f"run {run_id} envelope digest mismatch")
        path = _assert_child(envelope_root / f"{run_id}.json", envelope_root, "envelope")
        if path.exists() or path.is_symlink():
            raise BackupError(f"duplicate envelope path: {path.name}")
        content = _canonical_bytes(value)
        path.write_bytes(content)
        os.chmod(path, 0o600)
        envelope_index.append({"run_id": run_id, "sha256": observed})

    receipt_index: list[dict[str, str]] = []
    for row in connection.execute(
        "SELECT run_id,receipt_json,receipt_sha256 FROM receipts ORDER BY run_id"
    ):
        run_id = str(row["run_id"])
        value = _parse_json_object(row["receipt_json"], f"run {run_id} receipt")
        unsigned = {key: item for key, item in value.items() if key != "receipt_sha256"}
        observed = legacy.sha256_json(unsigned)
        expected = str(row["receipt_sha256"])
        if value.get("receipt_sha256") != observed or observed != expected:
            raise BackupError(f"run {run_id} receipt digest mismatch")
        path = _assert_child(receipt_root / f"{run_id}.json", receipt_root, "receipt")
        if path.exists() or path.is_symlink():
            raise BackupError(f"duplicate receipt path: {path.name}")
        path.write_bytes(_canonical_bytes(value))
        os.chmod(path, 0o600)
        receipt_index.append({"run_id": run_id, "sha256": observed})

    return {
        "envelope_count": len(envelope_index),
        "envelope_root_sha256": legacy.sha256_json(envelope_index),
        "receipt_count": len(receipt_index),
        "receipt_root_sha256": legacy.sha256_json(receipt_index),
    }


def _active_runs(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT run_id,task_id,state,heartbeat_at,updated_at,dispatch_request_id,"
        "external_system,external_id,external_state,external_observed_at FROM runs "
        "WHERE state IN ('assigned','running','verifying') ORDER BY run_id"
    ).fetchall()
    return [
        {
            "run_id": str(row["run_id"]),
            "task_id": str(row["task_id"]),
            "state": str(row["state"]),
            "heartbeat_at": str(row["heartbeat_at"]),
            "updated_at": str(row["updated_at"]),
            "dispatch_request_id": row["dispatch_request_id"],
            "external_system": row["external_system"],
            "external_id": row["external_id"],
            "backed_up_external_state": row["external_state"],
            "backed_up_external_observed_at": row["external_observed_at"],
        }
        for row in rows
    ]


def _public_active_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "run_id": str(run["run_id"]),
            "task_id": str(run["task_id"]),
            "state": str(run["state"]),
            "heartbeat_at": str(run["heartbeat_at"]),
            "updated_at": str(run["updated_at"]),
            "external_system": run.get("external_system"),
            "external_bound": bool(run.get("external_system") and run.get("external_id")),
        }
        for run in runs
    ]


def _write_create_only_json(path: Path, value: Any) -> str:
    content = _canonical_bytes(value)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    return _sha256_bytes(content)


def _bundle_payload(bundle_root: Path, database_path: Path) -> dict[str, Any]:
    connection = _open_read_only(database_path)
    try:
        connection.execute("BEGIN")
        db_health = _integrity(connection)
        projection = _projection_contract(connection)
        evidence = _materialize_evidence(connection, bundle_root)
        active_runs = _active_runs(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "database": {
            "path": DATABASE_NAME,
            "sha256": _sha256_file(database_path),
            "size_bytes": database_path.stat().st_size,
            **db_health,
        },
        "projection": projection,
        "evidence": evidence,
        "nonterminal_runs": _public_active_runs(active_runs),
    }


def create_backup(
    *,
    state_root: Path,
    backup_root: Path,
    bundle_id: str | None = None,
) -> dict[str, Any]:
    source_root = _ensure_private_directory(state_root, "Bureau state root", create=False)
    source_db = _ensure_regular_file(source_root / DATABASE_NAME, "Bureau state database")
    destination_root = _ensure_private_directory(backup_root, "Bureau backup root", create=True)
    selected_id = bundle_id or (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "-" + uuid.uuid4().hex[:12]
    )
    if not selected_id or "/" in selected_id or "\x00" in selected_id:
        raise BackupError("bundle id is invalid")
    final = _assert_child(destination_root / selected_id, destination_root, "bundle")
    if final.exists() or final.is_symlink():
        raise BackupError(f"backup bundle already exists: {selected_id}")

    staging = Path(tempfile.mkdtemp(prefix=f".{selected_id}.", dir=destination_root)).resolve()
    os.chmod(staging, 0o700)
    try:
        database_path = staging / DATABASE_NAME
        _create_online_backup(source_db, database_path)
        payload = _bundle_payload(staging, database_path)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "kind": BUNDLE_KIND,
            "bundle_id": selected_id,
            "created_at": _utc_now(),
            **payload,
            "does_not_establish": [
                "external_backup_replication",
                "future_source_state",
                "lease_reactivation",
                "external_state_success",
            ],
        }
        manifest_sha256 = _write_create_only_json(staging / MANIFEST_NAME, manifest)
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "kind": "bureau_state_backup_receipt",
            "bundle_id": selected_id,
            "manifest_sha256": manifest_sha256,
            "database_sha256": manifest["database"]["sha256"],
            "authoritative_root_sha256": manifest["projection"]["authoritative_root_sha256"],
            "envelope_root_sha256": manifest["evidence"]["envelope_root_sha256"],
            "receipt_root_sha256": manifest["evidence"]["receipt_root_sha256"],
            "created_at": manifest["created_at"],
        }
        receipt_sha256 = _write_create_only_json(staging / "receipt.json", receipt)
        os.rename(staging, final)
        directory = os.open(destination_root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "bureau_state_backup_result",
            "status": "created",
            "bundle_id": selected_id,
            "bundle_path": str(final),
            "manifest_sha256": manifest_sha256,
            "receipt_sha256": receipt_sha256,
            "database_sha256": manifest["database"]["sha256"],
            "projection": manifest["projection"],
            "evidence": manifest["evidence"],
            "nonterminal_run_count": len(manifest["nonterminal_runs"]),
            "replication": {"status": "not-attempted"},
        }
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _load_manifest(bundle: Path) -> tuple[dict[str, Any], str]:
    manifest_path = _ensure_regular_file(bundle / MANIFEST_NAME, "backup manifest")
    manifest_bytes = manifest_path.read_bytes()
    manifest_sha256 = _sha256_bytes(manifest_bytes)
    try:
        manifest = json.loads(manifest_bytes)
    except json.JSONDecodeError as exc:
        raise BackupError("backup manifest JSON is invalid") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != SCHEMA_VERSION
        or manifest.get("kind") != BUNDLE_KIND
    ):
        raise BackupError("backup manifest contract is invalid")
    if manifest_bytes != _canonical_bytes(manifest):
        raise BackupError("backup manifest is not canonical JSON")
    return manifest, manifest_sha256


def _verify_materialized_directory(
    root: Path, expected_count: int, expected_root: str, label: str
) -> None:
    if root.is_symlink() or not root.is_dir():
        raise BackupError(f"{label} directory is invalid")
    index: list[dict[str, str]] = []
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if path.is_symlink() or not path.is_file() or path.suffix != ".json":
            raise BackupError(f"{label} directory contains an invalid entry")
        value = _parse_json_object(path.read_text(encoding="utf-8"), f"{label} {path.name}")
        if label == "receipt":
            unsigned = {key: item for key, item in value.items() if key != "receipt_sha256"}
            digest = legacy.sha256_json(unsigned)
            if value.get("receipt_sha256") != digest:
                raise BackupError(f"{label} {path.name} digest mismatch")
        else:
            digest = legacy.sha256_json(value)
        run_id = path.stem
        index.append({"run_id": run_id, "sha256": digest})
    if len(index) != expected_count or legacy.sha256_json(index) != expected_root:
        raise BackupError(f"{label} materialized evidence root mismatch")


def verify_bundle(bundle_root: Path) -> dict[str, Any]:
    bundle = _ensure_private_directory(bundle_root, "backup bundle", create=False)
    manifest, manifest_sha256 = _load_manifest(bundle)
    database_path = _ensure_regular_file(bundle / DATABASE_NAME, "backup database")
    database = manifest.get("database")
    projection = manifest.get("projection")
    evidence = manifest.get("evidence")
    if not all(isinstance(item, dict) for item in (database, projection, evidence)):
        raise BackupError("backup manifest sections are invalid")
    if _sha256_file(database_path) != database.get("sha256"):
        raise BackupError("backup database digest mismatch")

    connection = _open_read_only(database_path)
    try:
        connection.execute("BEGIN")
        db_health = _integrity(connection)
        observed_projection = _projection_contract(connection)
        active_runs = _active_runs(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    if db_health["schema_version"] != database.get("schema_version"):
        raise BackupError("backup database schema version mismatch")
    if observed_projection != projection:
        raise BackupError("backup projection roots changed")
    if _public_active_runs(active_runs) != manifest.get("nonterminal_runs"):
        raise BackupError("backup nonterminal run projection changed")
    _verify_materialized_directory(
        bundle / "envelopes",
        int(evidence.get("envelope_count", -1)),
        str(evidence.get("envelope_root_sha256", "")),
        "envelope",
    )
    _verify_materialized_directory(
        bundle / "receipts",
        int(evidence.get("receipt_count", -1)),
        str(evidence.get("receipt_root_sha256", "")),
        "receipt",
    )
    return {
        "manifest": manifest,
        "manifest_sha256": manifest_sha256,
        "database_path": database_path,
        "active_runs": active_runs,
    }


def _fresh_grabowski_readback(resource_db: Path) -> dict[str, Any]:
    path = _ensure_regular_file(resource_db, "Grabowski resource database")
    connection = _open_read_only(path)
    try:
        connection.execute("BEGIN")
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not {"metadata", "leases"}.issubset(tables):
            raise BackupError("Grabowski resource database lacks lease contract tables")
        metadata = {
            str(row["key"]): str(row["value"])
            for row in connection.execute("SELECT key,value FROM metadata ORDER BY key")
        }
        now_unix = int(datetime.now(timezone.utc).timestamp())
        lease_count = int(
            connection.execute(
                "SELECT count(*) FROM leases WHERE expires_at_unix > ?", (now_unix,)
            ).fetchone()[0]
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "observed_at": _utc_now(),
        "schema_version": metadata.get("schema_version"),
        "resource_lease_contract_version": metadata.get("resource_lease_contract_version"),
        "active_lease_count": lease_count,
        "source": "fresh-read-only-grabowski-resource-db",
    }


def _fresh_external_run_observations(
    runs: list[dict[str, Any]], adapter_registry: AdapterRegistry | None
) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    for run in runs:
        run_id = str(run["run_id"])
        system = run.get("external_system")
        if not system:
            observations.append(
                {
                    "run_id": run_id,
                    "status": "unbound",
                    "live_observed": True,
                    "reason": "run has no external executor binding",
                }
            )
            continue
        if adapter_registry is None:
            observations.append(
                {
                    "run_id": run_id,
                    "system": str(system),
                    "status": "blocked",
                    "live_observed": False,
                    "reason": "external adapter registry unavailable",
                }
            )
            continue
        adapter = adapter_registry.get(str(system))
        if adapter is None:
            observations.append(
                {
                    "run_id": run_id,
                    "system": str(system),
                    "status": "blocked",
                    "live_observed": False,
                    "reason": adapter_registry.unavailable_reason(str(system))
                    or "external adapter unavailable",
                }
            )
            continue
        external_id = run.get("external_id")
        recovered = False
        if not external_id:
            request_id = run.get("dispatch_request_id")
            if not isinstance(request_id, str) or not request_id:
                observations.append(
                    {
                        "run_id": run_id,
                        "system": str(system),
                        "status": "blocked",
                        "live_observed": False,
                        "reason": "external binding and dispatch request are both missing",
                    }
                )
                continue
            try:
                external_id = adapter.recover(request_id)
            except Exception as exc:
                observations.append(
                    {
                        "run_id": run_id,
                        "system": str(system),
                        "status": "blocked",
                        "live_observed": False,
                        "reason": f"external binding recovery failed: {type(exc).__name__}",
                    }
                )
                continue
            if not external_id:
                observations.append(
                    {
                        "run_id": run_id,
                        "system": str(system),
                        "status": "blocked",
                        "live_observed": False,
                        "reason": "external binding could not be recovered",
                    }
                )
                continue
            recovered = True
        try:
            observation = adapter.observe(str(external_id))
        except Exception as exc:
            observations.append(
                {
                    "run_id": run_id,
                    "system": str(system),
                    "status": "blocked",
                    "live_observed": False,
                    "reason": f"external observation failed: {type(exc).__name__}",
                }
            )
            continue
        if observation.state not in {
            "running",
            "succeeded",
            "failed",
            "cancelled",
            "interrupted",
            "missing",
        }:
            observations.append(
                {
                    "run_id": run_id,
                    "system": str(system),
                    "status": "blocked",
                    "live_observed": True,
                    "observed_at": _utc_now(),
                    "reason": "external state is unknown",
                    "external_id_sha256": _sha256_bytes(str(external_id).encode()),
                }
            )
            continue
        observations.append(
            {
                "run_id": run_id,
                "system": str(system),
                "status": "observed",
                "live_observed": True,
                "observed_at": _utc_now(),
                "state": observation.state,
                "external_id_sha256": _sha256_bytes(str(external_id).encode()),
                "detail_sha256": legacy.sha256_json(observation.detail),
                "binding_recovered": recovered,
            }
        )
    return observations


def restore_test(
    *,
    bundle_root: Path,
    restore_root: Path,
    grabowski_resource_db: Path = DEFAULT_GRABOWSKI_RESOURCE_DB,
    adapter_registry: AdapterRegistry | None = None,
) -> dict[str, Any]:
    bundle = _ensure_private_directory(bundle_root, "backup bundle", create=False)
    verification = verify_bundle(bundle)
    raw_restore = restore_root.expanduser()
    if raw_restore.exists():
        if raw_restore.is_symlink() or not raw_restore.is_dir():
            raise BackupError("restore root must be a real directory")
        if any(raw_restore.iterdir()):
            raise BackupError("restore root must be empty")
        restore = raw_restore.resolve()
        os.chmod(restore, 0o700)
    else:
        raw_restore.mkdir(parents=True, mode=0o700)
        restore = raw_restore.resolve()
    restored_db = restore / DATABASE_NAME
    _create_online_backup(verification["database_path"], restored_db)
    shutil.copytree(bundle / "envelopes", restore / "envelopes", symlinks=False)
    shutil.copytree(bundle / "receipts", restore / "receipts", symlinks=False)
    os.chmod(restore / "envelopes", 0o700)
    os.chmod(restore / "receipts", 0o700)
    for evidence_file in (restore / "envelopes").iterdir():
        os.chmod(evidence_file, 0o600)
    for evidence_file in (restore / "receipts").iterdir():
        os.chmod(evidence_file, 0o600)

    restored_connection = _open_read_only(restored_db)
    try:
        restored_connection.execute("BEGIN")
        restored_health = _integrity(restored_connection)
        restored_projection = _projection_contract(restored_connection)
        restored_active_runs = _active_runs(restored_connection)
        restored_connection.commit()
    except Exception:
        restored_connection.rollback()
        raise
    finally:
        restored_connection.close()

    manifest = verification["manifest"]
    if restored_projection != manifest["projection"]:
        raise BackupError("restored projection roots do not match the source bundle")
    public_active_runs = _public_active_runs(restored_active_runs)
    if public_active_runs != manifest.get("nonterminal_runs"):
        raise BackupError("restored nonterminal run projection changed")
    external = _fresh_grabowski_readback(grabowski_resource_db)
    run_observations = _fresh_external_run_observations(
        restored_active_runs, adapter_registry
    )
    reconcile_ready = all(
        item["status"] in {"unbound", "observed"} for item in run_observations
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "bureau_state_restore_test_result",
        "status": "verified",
        "bundle_id": manifest["bundle_id"],
        "manifest_sha256": verification["manifest_sha256"],
        "restore_root": str(restore),
        "database": restored_health,
        "projection": restored_projection,
        "nonterminal_runs": public_active_runs,
        "post_restore_reconcile": {
            "required": bool(restored_active_runs),
            "run_count": len(restored_active_runs),
            "status": "ready" if reconcile_ready else "blocked",
            "reconcile_ready": reconcile_ready,
            "leases_reactivated": False,
            "external_state": external,
            "external_run_observations": run_observations,
            "rule": "fresh external state must be reconciled before any nonterminal run resumes",
        },
    }


def _run(
    argv: list[str],
    *,
    env: dict[str, str],
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            argv,
            env=env,
            input=input_bytes,
            capture_output=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackupError(f"restic command failed to execute: {type(exc).__name__}") from exc


def replicate_bundle(
    *,
    bundle_root: Path,
    repository: str,
    password_file: Path,
    restic_bin: Path = Path("/usr/bin/restic"),
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    bundle = _ensure_private_directory(bundle_root, "backup bundle", create=False)
    verification = verify_bundle(bundle)
    password = _ensure_private_file(password_file, "Restic password file")
    executable = _ensure_regular_file(restic_bin, "Restic executable")
    if not os.access(executable, os.X_OK):
        raise BackupError("Restic executable is not executable")
    if not repository.strip():
        raise BackupError("Restic repository is required")
    run_tag = "bureau-state-" + uuid.uuid4().hex
    env = dict(os.environ)
    if extra_env:
        env.update({str(key): str(value) for key, value in extra_env.items()})
    env["RESTIC_REPOSITORY"] = repository
    env["RESTIC_PASSWORD_FILE"] = str(password)
    manifest_path = bundle / MANIFEST_NAME

    backup = _run(
        [
            str(executable),
            "--no-cache",
            "backup",
            str(bundle),
            "--host",
            "heim-pc",
            "--tag",
            "bureau-state",
            "--tag",
            run_tag,
        ],
        env=env,
    )
    if backup.returncode != 0:
        raise BackupError("Restic backup failed")

    snapshots = _run(
        [
            str(executable),
            "--no-cache",
            "snapshots",
            "--json",
            "--host",
            "heim-pc",
            "--tag",
            run_tag,
        ],
        env=env,
    )
    if snapshots.returncode != 0:
        raise BackupError("Restic snapshot readback failed")
    try:
        rows = json.loads(snapshots.stdout)
    except json.JSONDecodeError as exc:
        raise BackupError("Restic snapshot readback is invalid JSON") from exc
    if not isinstance(rows, list) or len(rows) != 1:
        raise BackupError("Restic run tag did not resolve to exactly one snapshot")
    snapshot_id = rows[0].get("id")
    if (
        not isinstance(snapshot_id, str)
        or len(snapshot_id) != 64
        or any(character not in "0123456789abcdef" for character in snapshot_id)
    ):
        raise BackupError("Restic snapshot id is invalid")

    relative_manifest = manifest_path.as_posix()
    dump = _run(
        [str(executable), "--no-cache", "dump", snapshot_id, relative_manifest],
        env=env,
    )
    if dump.returncode != 0:
        raise BackupError("Restic exact-snapshot manifest restore failed")
    if _sha256_bytes(dump.stdout) != verification["manifest_sha256"]:
        raise BackupError("Restic exact-snapshot manifest digest mismatch")
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "bureau_state_restic_replication_result",
        "status": "verified",
        "bundle_id": verification["manifest"]["bundle_id"],
        "snapshot_id": snapshot_id,
        "manifest_sha256": verification["manifest_sha256"],
        "run_tag": run_tag,
        "retention_changed": False,
        "forbidden_operations": ["forget", "prune"],
    }


def latest_bundle(backup_root: Path) -> Path:
    root = _ensure_private_directory(backup_root, "Bureau backup root", create=False)
    candidates = sorted(
        (
            path
            for path in root.iterdir()
            if path.is_dir() and not path.is_symlink() and (path / MANIFEST_NAME).is_file()
        ),
        key=lambda path: path.name,
    )
    if not candidates:
        raise BackupError("no Bureau backup bundle is available")
    return candidates[-1].resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bureau-state-backup")
    subparsers = parser.add_subparsers(dest="command", required=True)
    default_state = Path(os.environ.get("BUREAU_STATE_DIR", "~/.local/state/bureau")).expanduser()
    default_backup = Path(
        os.environ.get("BUREAU_BACKUP_DIR", "~/.local/state/bureau-backup")
    ).expanduser()

    backup = subparsers.add_parser("backup")
    backup.add_argument("--state-root", type=Path, default=default_state)
    backup.add_argument("--backup-root", type=Path, default=default_backup)
    backup.add_argument("--bundle-id")
    backup.add_argument("--repository")
    backup.add_argument("--password-file", type=Path)
    backup.add_argument("--restic-bin", type=Path, default=Path("/usr/bin/restic"))
    backup.add_argument("--require-replication", action="store_true")

    restore = subparsers.add_parser("restore-test")
    restore.add_argument("--backup-root", type=Path, default=default_backup)
    restore.add_argument("--bundle", type=Path)
    restore.add_argument("--restore-root", type=Path)
    restore.add_argument(
        "--grabowski-resource-db", type=Path, default=DEFAULT_GRABOWSKI_RESOURCE_DB
    )

    replicate = subparsers.add_parser("replicate")
    replicate.add_argument("--bundle", type=Path, required=True)
    replicate.add_argument("--repository", required=True)
    replicate.add_argument("--password-file", type=Path, required=True)
    replicate.add_argument("--restic-bin", type=Path, default=Path("/usr/bin/restic"))
    return parser


def execute(
    argv: list[str] | None = None,
    *,
    adapter_registry: AdapterRegistry | None = None,
) -> dict[str, Any]:
    args = build_parser().parse_args(argv)
    if args.command == "backup":
        result = create_backup(
            state_root=args.state_root,
            backup_root=args.backup_root,
            bundle_id=args.bundle_id,
        )
        repository = args.repository or os.environ.get("BUREAU_RESTIC_REPOSITORY")
        password_value = args.password_file or os.environ.get("BUREAU_RESTIC_PASSWORD_FILE")
        if repository and password_value:
            try:
                result["replication"] = replicate_bundle(
                    bundle_root=Path(result["bundle_path"]),
                    repository=str(repository),
                    password_file=Path(password_value),
                    restic_bin=args.restic_bin,
                )
            except BackupError as exc:
                message = (
                    f"local backup {result['bundle_id']} created but Restic "
                    f"replication failed: {exc}"
                )
                raise BackupError(message) from exc
        elif repository or password_value:
            raise BackupError(
                f"local backup {result['bundle_id']} created but Restic replication "
                "config is incomplete"
            )
        elif args.require_replication:
            raise BackupError(
                f"local backup {result['bundle_id']} created but Restic replication "
                "is required and not configured"
            )
        else:
            result["replication"] = {
                "status": "not-configured",
                "does_not_establish": ["encrypted_offsite_copy"],
            }
        return result
    if args.command == "restore-test":
        bundle = args.bundle or latest_bundle(args.backup_root)
        created_temp = args.restore_root is None
        restore_root = args.restore_root or Path(tempfile.mkdtemp(prefix="bureau-restore-test-"))
        if created_temp:
            shutil.rmtree(restore_root)
        try:
            return restore_test(
                bundle_root=bundle,
                restore_root=restore_root,
                grabowski_resource_db=args.grabowski_resource_db,
                adapter_registry=adapter_registry,
            )
        finally:
            if created_temp:
                shutil.rmtree(restore_root, ignore_errors=True)
    return replicate_bundle(
        bundle_root=args.bundle,
        repository=args.repository,
        password_file=args.password_file,
        restic_bin=args.restic_bin,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        result = execute(argv)
    except BackupError as exc:
        print(
            legacy.canonical_json(
                {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "bureau_state_backup_error",
                    "status": "blocked",
                    "error": str(exc),
                }
            )
        )
        return 2
    print(legacy.canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
