from __future__ import annotations

import json
from pathlib import Path

import pytest

from bureau import state_backup
from bureau.adapters import AdapterRegistry, Observation
from bureau.core import Dispatcher, Registry, StateStore
from bureau.v2 import _complete_run_after_typed_evaluation as complete_run


class ObservingAdapter:
    system = "grabowski-task"
    aliases = ("grabowski-job",)

    def __init__(self, state: str = "running"):
        self.state = state
        self.observed: list[str] = []

    def dispatch(self, request):
        raise AssertionError("restore reconciliation must not dispatch")

    def recover(self, request_id):
        return None

    def observe(self, external_id):
        self.observed.append(external_id)
        return Observation(self.state, {"external_id": external_id, "state": self.state})

    def cancel(self, external_id):
        raise AssertionError("restore reconciliation must not cancel")

    def resume(self, external_id):
        raise AssertionError("restore reconciliation must not resume")


def _setup(registry_factory, tmp_path: Path):
    root = registry_factory(1)
    state_root = tmp_path / "state"
    registry = Registry.load(root)
    store = StateStore(state_root / "bureau.sqlite3")
    dispatcher = Dispatcher(registry, store)
    return root, state_root, registry, store, dispatcher


def _claim(dispatcher: Dispatcher) -> dict:
    return dispatcher.claim_next("worker-1", {"repository"})["run"]


def test_create_backup_binds_online_database_projection_envelope_and_receipt(
    registry_factory, tmp_path: Path
):
    _root, state_root, registry, store, dispatcher = _setup(registry_factory, tmp_path)
    run = _claim(dispatcher)
    complete_run(registry, store, run["run_id"], {"proof": {"result": "passed"}})

    expected_root = store.replay_projection()["authoritative_root_sha256"]
    result = state_backup.create_backup(
        state_root=state_root,
        backup_root=tmp_path / "artifacts/merges/bureau-state-backups",
    )

    assert result["status"] == "created"
    assert result["authoritative_root_sha256"] == expected_root
    assert result["envelope_count"] == 1
    assert result["receipt_count"] == 1
    verification = state_backup.verify_backup(Path(result["bundle"]))
    assert verification["status"] == "verified"
    assert verification["authoritative_root_sha256"] == expected_root
    assert len(verification["envelope_root_sha256"]) == 64
    assert len(verification["receipt_root_sha256"]) == 64


def test_create_backup_rejects_missing_bound_envelope(registry_factory, tmp_path: Path):
    _root, state_root, _registry, store, dispatcher = _setup(registry_factory, tmp_path)
    run = _claim(dispatcher)
    store.envelope_path(run["run_id"]).unlink()

    with pytest.raises(state_backup.StateBackupError, match="envelope"):
        state_backup.create_backup(
            state_root=state_root,
            backup_root=tmp_path / "artifacts/merges/bureau-state-backups",
        )


def test_verify_backup_rejects_tampered_bound_receipt(registry_factory, tmp_path: Path):
    _root, state_root, registry, store, dispatcher = _setup(registry_factory, tmp_path)
    run = _claim(dispatcher)
    complete_run(registry, store, run["run_id"], {"proof": {"result": "passed"}})
    result = state_backup.create_backup(
        state_root=state_root,
        backup_root=tmp_path / "artifacts/merges/bureau-state-backups",
    )
    bundle = Path(result["bundle"])
    receipt = bundle / "receipts" / f"{run['run_id']}.json"
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["evidence"] = {"tampered": True}
    receipt.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    with pytest.raises(state_backup.StateBackupError, match="receipt"):
        state_backup.verify_backup(bundle)


def test_restore_test_never_reactivates_leases_and_fail_closes_nonterminal_runs(
    registry_factory, tmp_path: Path
):
    root, state_root, _registry, _store, dispatcher = _setup(registry_factory, tmp_path)
    run = _claim(dispatcher)
    backup_root = tmp_path / "artifacts/merges/bureau-state-backups"
    result = state_backup.create_backup(state_root=state_root, backup_root=backup_root)
    receipt_path = tmp_path / "restore-receipts/latest.json"

    restored = state_backup.restore_test(
        bundle=Path(result["bundle"]),
        scratch_root=tmp_path / "scratch",
        receipt_path=receipt_path,
        registry_root=root,
        adapters=AdapterRegistry(),
    )

    verification = state_backup.verify_backup(Path(result["bundle"]))
    assert restored["status"] == "verified"
    assert restored["envelope_root_sha256"] == verification["envelope_root_sha256"]
    assert restored["receipt_root_sha256"] == verification["receipt_root_sha256"]
    assert restored["empty_target_created"] is True
    assert restored["external_leases_restored"] is False
    assert restored["external_state_reused"] is False
    assert restored["nonterminal_runs_before_reconcile"] == [
        {"run_id": run["run_id"], "recorded_state": "assigned"}
    ]
    reconcile = restored["post_restore_reconcile"]
    assert reconcile["mode"] == "fresh-external-readback"
    assert reconcile["default"] == "fail-closed"
    assert reconcile["lease_reactivation"] is False
    assert reconcile["result"]["orphaned"] == [run["run_id"]]
    assert reconcile["remaining_nonterminal_runs"] == []
    assert reconcile["remaining_reservation_count"] == 0
    assert receipt_path.is_file()


def test_restore_test_freshly_observes_external_run(registry_factory, tmp_path: Path):
    root, state_root, _registry, store, dispatcher = _setup(registry_factory, tmp_path)
    run = _claim(dispatcher)
    store.bind(run["run_id"], "grabowski-task", "external-1")
    result = state_backup.create_backup(
        state_root=state_root,
        backup_root=tmp_path / "artifacts/merges/bureau-state-backups",
    )
    adapter = ObservingAdapter("running")

    restored = state_backup.restore_test(
        bundle=Path(result["bundle"]),
        scratch_root=tmp_path / "scratch",
        registry_root=root,
        adapters=AdapterRegistry([adapter]),
    )

    reconcile = restored["post_restore_reconcile"]
    assert adapter.observed == ["external-1"]
    assert reconcile["result"]["refreshed"] == [run["run_id"]]
    assert reconcile["remaining_nonterminal_runs"] == [
        {
            "run_id": run["run_id"],
            "recorded_state": "running",
            "external_system": "grabowski-task",
            "external_id": "external-1",
        }
    ]


def test_restore_test_fails_closed_when_external_run_cannot_be_observed(
    registry_factory, tmp_path: Path
):
    root, state_root, _registry, store, dispatcher = _setup(registry_factory, tmp_path)
    run = _claim(dispatcher)
    store.bind(run["run_id"], "grabowski-task", "external-1")
    result = state_backup.create_backup(
        state_root=state_root,
        backup_root=tmp_path / "artifacts/merges/bureau-state-backups",
    )

    with pytest.raises(state_backup.StateBackupError, match="unobserved"):
        state_backup.restore_test(
            bundle=Path(result["bundle"]),
            scratch_root=tmp_path / "scratch",
            registry_root=root,
            adapters=AdapterRegistry(),
        )


def test_restore_test_revalidates_restored_receipt_bytes(
    registry_factory, tmp_path: Path, monkeypatch
):
    _root, state_root, registry, store, dispatcher = _setup(registry_factory, tmp_path)
    run = _claim(dispatcher)
    complete_run(registry, store, run["run_id"], {"proof": {"result": "passed"}})
    result = state_backup.create_backup(
        state_root=state_root,
        backup_root=tmp_path / "artifacts/merges/bureau-state-backups",
    )
    original_copy2 = state_backup.shutil.copy2

    def copy2_then_tamper_receipt(source, destination, *args, **kwargs):
        copied = original_copy2(source, destination, *args, **kwargs)
        destination_path = Path(destination)
        if destination_path.parent.name == "receipts":
            destination_path.write_text("{}\n", encoding="utf-8")
        return copied

    monkeypatch.setattr(state_backup.shutil, "copy2", copy2_then_tamper_receipt)
    with pytest.raises(state_backup.StateBackupError, match="receipt"):
        state_backup.restore_test(
            bundle=Path(result["bundle"]),
            scratch_root=tmp_path / "scratch",
            registry_root=_root,
            adapters=AdapterRegistry(),
        )


def test_latest_bundle_skips_invalid_newer_directory(registry_factory, tmp_path: Path):
    _root, state_root, _registry, _store, dispatcher = _setup(registry_factory, tmp_path)
    _claim(dispatcher)
    backup_root = tmp_path / "artifacts/merges/bureau-state-backups"
    result = state_backup.create_backup(state_root=state_root, backup_root=backup_root)
    invalid = backup_root / "zzzz-invalid"
    invalid.mkdir()
    (invalid / "manifest.json").write_text("{}\n", encoding="utf-8")

    assert state_backup.latest_bundle(backup_root) == Path(result["bundle"])


def test_manifest_marks_existing_restic_source_without_claiming_upload(
    registry_factory, tmp_path: Path
):
    _root, state_root, _registry, _store, dispatcher = _setup(registry_factory, tmp_path)
    _claim(dispatcher)
    result = state_backup.create_backup(
        state_root=state_root,
        backup_root=tmp_path / "artifacts/merges/bureau-state-backups",
    )
    manifest = json.loads((Path(result["bundle"]) / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["offsite"]["staging_contract"] == "existing-restic-source"
    assert manifest["offsite"]["encryption"] == "restic"
    assert "offsite_snapshot_completed" in manifest["offsite"]["does_not_establish"]
