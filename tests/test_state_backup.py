from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from bureau import cli as bureau_cli
from bureau import state_backup
from bureau.adapters import AdapterRegistry, Observation
from bureau.core import Dispatcher, Registry, StateStore
from bureau.state_backup import (
    BackupError,
    create_backup,
    replicate_bundle,
    restore_test,
    verify_bundle,
)
from bureau.v2 import _complete_run_after_typed_evaluation as complete_run


def _resource_db(path: Path) -> Path:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.executemany(
            "INSERT INTO metadata(key,value) VALUES(?,?)",
            [("schema_version", "3"), ("resource_lease_contract_version", "1")],
        )
        connection.execute(
            "CREATE TABLE leases("
            "resource_key TEXT PRIMARY KEY, owner_id TEXT NOT NULL, "
            "expires_at_unix INTEGER NOT NULL)"
        )
        now = int(time.time())
        connection.executemany(
            "INSERT INTO leases(resource_key,owner_id,expires_at_unix) VALUES(?,?,?)",
            [("path:/live", "live", now + 3600), ("path:/expired", "old", now - 1)],
        )
    return path


def _state(tmp_path: Path) -> tuple[Path, StateStore]:
    root = tmp_path / "state"
    store = StateStore(root / "bureau.sqlite3", root)
    return root, store


def test_backup_restore_roundtrip_preserves_projection_and_does_not_reactivate_leases(
    tmp_path: Path,
):
    state_root, _ = _state(tmp_path)
    resource_db = _resource_db(tmp_path / "resources.sqlite3")

    backup = create_backup(
        state_root=state_root, backup_root=tmp_path / "backups", bundle_id="roundtrip"
    )
    restored = restore_test(
        bundle_root=Path(backup["bundle_path"]),
        restore_root=tmp_path / "restore",
        grabowski_resource_db=resource_db,
    )

    assert backup["status"] == "created"
    assert restored["status"] == "verified"
    assert restored["projection"] == backup["projection"]
    assert restored["post_restore_reconcile"]["required"] is False
    assert restored["post_restore_reconcile"]["leases_reactivated"] is False
    assert restored["post_restore_reconcile"]["external_state"]["active_lease_count"] == 1


def test_backup_materializes_claim_envelope_and_signed_receipt(registry_factory, tmp_path: Path):
    registry = Registry.load(registry_factory(1))
    state_root, store = _state(tmp_path)
    dispatcher = Dispatcher(registry, store)
    run = dispatcher.claim_next("backup-test", ("repository",))["run"]
    complete_run(registry, store, run["run_id"], {"proof": {"result": "passed"}})

    result = create_backup(
        state_root=state_root, backup_root=tmp_path / "backups", bundle_id="receipt"
    )
    verified = verify_bundle(Path(result["bundle_path"]))

    assert result["evidence"]["envelope_count"] == 1
    assert result["evidence"]["receipt_count"] == 1
    assert verified["manifest"]["evidence"] == result["evidence"]


def test_restore_marks_nonterminal_runs_for_reconcile_without_mutating_them(
    registry_factory, tmp_path: Path
):
    registry = Registry.load(registry_factory(1))
    state_root, store = _state(tmp_path)
    dispatcher = Dispatcher(registry, store)
    run = dispatcher.claim_next("backup-test", ("repository",))["run"]
    resource_db = _resource_db(tmp_path / "resources.sqlite3")

    backup = create_backup(
        state_root=state_root, backup_root=tmp_path / "backups", bundle_id="active"
    )
    restored = restore_test(
        bundle_root=Path(backup["bundle_path"]),
        restore_root=tmp_path / "restore",
        grabowski_resource_db=resource_db,
    )

    assert len(restored["nonterminal_runs"]) == 1
    restored_run = restored["nonterminal_runs"][0]
    assert restored_run["run_id"] == run["run_id"]
    assert restored_run["task_id"] == run["task_id"]
    assert restored_run["state"] == "assigned"
    assert restored_run["external_bound"] is False
    assert restored["post_restore_reconcile"]["required"] is True
    assert restored["post_restore_reconcile"]["status"] == "ready"
    assert restored["post_restore_reconcile"]["reconcile_ready"] is True
    assert restored["post_restore_reconcile"]["leases_reactivated"] is False
    assert restored["post_restore_reconcile"]["external_run_observations"] == [
        {
            "run_id": run["run_id"],
            "status": "unbound",
            "live_observed": True,
            "reason": "run has no external executor binding",
        }
    ]


def test_verify_rejects_database_tamper(tmp_path: Path):
    state_root, _ = _state(tmp_path)
    backup = create_backup(
        state_root=state_root, backup_root=tmp_path / "backups", bundle_id="tamper"
    )
    database = Path(backup["bundle_path"]) / "bureau.sqlite3"
    database.write_bytes(database.read_bytes() + b"tamper")

    with pytest.raises(BackupError, match="database digest mismatch"):
        verify_bundle(Path(backup["bundle_path"]))


def test_restore_requires_empty_root(tmp_path: Path):
    state_root, _ = _state(tmp_path)
    backup = create_backup(
        state_root=state_root, backup_root=tmp_path / "backups", bundle_id="occupied"
    )
    restore_root = tmp_path / "restore"
    restore_root.mkdir()
    (restore_root / "foreign.txt").write_text("keep")

    with pytest.raises(BackupError, match="restore root must be empty"):
        restore_test(
            bundle_root=Path(backup["bundle_path"]),
            restore_root=restore_root,
            grabowski_resource_db=_resource_db(tmp_path / "resources.sqlite3"),
        )


def test_restic_replication_is_exact_snapshot_bound_and_never_prunes(monkeypatch, tmp_path: Path):
    state_root, _ = _state(tmp_path)
    backup = create_backup(
        state_root=state_root, backup_root=tmp_path / "backups", bundle_id="restic"
    )
    bundle = Path(backup["bundle_path"])
    restic = tmp_path / "restic"
    restic.write_text("stub")
    restic.chmod(0o700)
    password = tmp_path / "restic.pass"
    password.write_text("not-a-real-secret")
    password.chmod(0o600)
    calls: list[list[str]] = []

    def fake_run(argv: list[str], *, env: dict[str, str], input_bytes: bytes | None = None):
        calls.append(list(argv))
        if argv[2] == "backup":
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if argv[2] == "snapshots":
            return subprocess.CompletedProcess(
                argv, 0, json.dumps([{"id": "a" * 64}]).encode(), b""
            )
        if argv[2] == "dump":
            return subprocess.CompletedProcess(
                argv, 0, (bundle / "manifest.json").read_bytes(), b""
            )
        raise AssertionError(argv)

    monkeypatch.setattr(state_backup, "_run", fake_run)
    result = replicate_bundle(
        bundle_root=bundle,
        repository="rest:http://127.0.0.1:1/bureau-test",
        password_file=password,
        restic_bin=restic,
    )

    assert result["status"] == "verified"
    assert result["retention_changed"] is False
    assert result["forbidden_operations"] == ["forget", "prune"]
    assert [call[2] for call in calls] == ["backup", "snapshots", "dump"]
    assert all(call[1] == "--no-cache" for call in calls)


def test_cli_parser_preserves_state_backup_arguments() -> None:
    args = bureau_cli._parse_arguments(
        [
            "--json",
            "state-backup",
            "backup",
            "--state-root",
            "/tmp/state",
            "--backup-root",
            "/tmp/backup",
        ]
    )
    assert args.command == "state-backup"
    assert args.backup_args == [
        "backup",
        "--state-root",
        "/tmp/state",
        "--backup-root",
        "/tmp/backup",
    ]


def test_backup_require_replication_fails_visibly_when_private_config_is_missing(
    monkeypatch, tmp_path: Path
) -> None:
    state_root, _ = _state(tmp_path)
    monkeypatch.delenv("BUREAU_RESTIC_REPOSITORY", raising=False)
    monkeypatch.delenv("BUREAU_RESTIC_PASSWORD_FILE", raising=False)

    with pytest.raises(BackupError, match="replication is required and not configured"):
        state_backup.execute(
            [
                "backup",
                "--state-root",
                str(state_root),
                "--backup-root",
                str(tmp_path / "backups"),
                "--bundle-id",
                "required",
                "--require-replication",
            ]
        )

    assert (tmp_path / "backups" / "required" / "manifest.json").is_file()


def test_restic_password_file_must_be_private(tmp_path: Path) -> None:
    state_root, _ = _state(tmp_path)
    backup = create_backup(
        state_root=state_root, backup_root=tmp_path / "backups", bundle_id="private"
    )
    restic = tmp_path / "restic"
    restic.write_text("stub")
    restic.chmod(0o700)
    password = tmp_path / "restic.pass"
    password.write_text("test")
    password.chmod(0o644)

    with pytest.raises(BackupError, match="group- or world-accessible"):
        replicate_bundle(
            bundle_root=Path(backup["bundle_path"]),
            repository="rest:http://127.0.0.1:1/test",
            password_file=password,
            restic_bin=restic,
        )


def test_systemd_units_use_manifest_bound_launcher_and_fail_closed_replication() -> None:
    root = Path(__file__).parents[1] / "ops" / "systemd"
    backup = (root / "bureau-state-backup.service").read_text(encoding="utf-8")
    restore = (root / "bureau-state-restore-test.service").read_text(encoding="utf-8")
    assert "ExecStart=%h/.local/bin/bureau --json state-backup backup" in backup
    assert "--require-replication" in backup
    assert "EnvironmentFile=-%h/.config/bureau/state-backup.env" in backup
    assert "ReadOnlyPaths=%h/.local/state/bureau" in backup
    assert "ReadWritePaths=%h/.local/state/bureau-backup" in backup
    assert "ExecStart=%h/.local/bin/bureau --json state-backup restore-test" in restore
    assert "ReadWritePaths" not in restore
    for name in ("bureau-state-backup.timer", "bureau-state-restore-test.timer"):
        text = (root / name).read_text(encoding="utf-8")
        assert "Persistent=true" in text
        assert f"Unit={Path(name).stem}.service" in text


class _ObservedAdapter:
    system = "grabowski-task"
    aliases: tuple[str, ...] = ()

    def recover(self, request_id: str) -> str | None:
        return None

    def observe(self, external_id: str) -> Observation:
        return Observation(
            "running",
            {"state": "running", "external_id": external_id, "private": "detail"},
        )


def test_restore_freshly_observes_bound_external_run_without_returning_raw_id(
    registry_factory, tmp_path: Path
) -> None:
    registry = Registry.load(registry_factory(1))
    state_root, store = _state(tmp_path)
    dispatcher = Dispatcher(registry, store)
    run = dispatcher.claim_next("backup-test", ("repository",))["run"]
    bound = store.bind(run["run_id"], "grabowski-task", "external-secret-1")
    backup = create_backup(
        state_root=state_root, backup_root=tmp_path / "backups", bundle_id="external"
    )

    restored = restore_test(
        bundle_root=Path(backup["bundle_path"]),
        restore_root=tmp_path / "restore",
        grabowski_resource_db=_resource_db(tmp_path / "resources.sqlite3"),
        adapter_registry=AdapterRegistry([_ObservedAdapter()]),
    )

    assert restored["post_restore_reconcile"]["status"] == "ready"
    observation = restored["post_restore_reconcile"]["external_run_observations"][0]
    assert observation["status"] == "observed"
    assert observation["state"] == "running"
    assert observation["binding_recovered"] is False
    assert len(observation["external_id_sha256"]) == 64
    assert restored["nonterminal_runs"][0]["external_bound"] is True
    assert restored["nonterminal_runs"][0]["state"] == bound["state"]
    assert "external-secret-1" not in json.dumps(restored, sort_keys=True)


def test_restore_blocks_external_bound_run_when_live_adapter_is_unavailable(
    registry_factory, tmp_path: Path
) -> None:
    registry = Registry.load(registry_factory(1))
    state_root, store = _state(tmp_path)
    dispatcher = Dispatcher(registry, store)
    run = dispatcher.claim_next("backup-test", ("repository",))["run"]
    store.bind(run["run_id"], "grabowski-task", "external-secret-2")
    backup = create_backup(
        state_root=state_root, backup_root=tmp_path / "backups", bundle_id="blocked"
    )

    restored = restore_test(
        bundle_root=Path(backup["bundle_path"]),
        restore_root=tmp_path / "restore",
        grabowski_resource_db=_resource_db(tmp_path / "resources.sqlite3"),
        adapter_registry=AdapterRegistry(),
    )

    reconcile = restored["post_restore_reconcile"]
    assert reconcile["status"] == "blocked"
    assert reconcile["reconcile_ready"] is False
    assert reconcile["leases_reactivated"] is False
    assert reconcile["external_run_observations"][0]["live_observed"] is False
    assert "external-secret-2" not in json.dumps(restored, sort_keys=True)
