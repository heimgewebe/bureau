from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bureau import doctor, state_backup
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


def test_verify_backup_normalizes_semantically_malformed_database(
    registry_factory, tmp_path: Path
):
    _root, state_root, _registry, _store, dispatcher = _setup(registry_factory, tmp_path)
    run = _claim(dispatcher)
    backup_root = tmp_path / "artifacts/merges/bureau-state-backups"
    result = state_backup.create_backup(state_root=state_root, backup_root=backup_root)
    bundle = Path(result["bundle"])
    database_path = bundle / "bureau.sqlite3"

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE runs SET workspace_path=? WHERE run_id=?",
            (sqlite3.Binary(b"\xff"), run["run_id"]),
        )
        connection.commit()

    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    database_bytes = database_path.read_bytes()
    manifest["database"]["sha256"] = hashlib.sha256(database_bytes).hexdigest()
    manifest["database"]["bytes"] = len(database_bytes)
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    manifest["manifest_sha256"] = hashlib.sha256(
        json.dumps(
            unsigned,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")

    with pytest.raises(state_backup.StateBackupError, match="backup verification failed") as exc:
        state_backup.verify_backup(bundle)
    assert isinstance(exc.value.__cause__, TypeError)

    observation = doctor.observe_backup(backup_root)
    assert observation["status"] == "unavailable"


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


def _retention_test_policy(*, recent_max_count: int = 1) -> dict:
    return {
        "schema_version": 1,
        "kind": "bureau_local_retention_policy",
        "policy_id": "test-retention-v1",
        "stores": {
            "state_backups": {
                "recent_seconds": 0,
                "recent_max_count": recent_max_count,
                "restore_receipt_max_age_seconds": 36 * 60 * 60,
                "tiers": [],
            },
            "registry_snapshots": {
                "recent_seconds": 0,
                "recent_max_count": recent_max_count,
                "rollback_manifest_depth": 2,
                "tiers": [],
            },
            "review_receipts": {
                "recent_seconds": 0,
                "recent_max_count": recent_max_count,
                "tiers": [],
            },
        },
    }


def _iso_at(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def _write_retention_backup(root: Path, *, timestamp: int, suffix: str) -> Path:
    bundle = root / f"20260101T000000.000000Z-{suffix}"
    bundle.mkdir()
    manifest = {
        "schema_version": state_backup.SCHEMA_VERSION,
        "kind": "bureau_state_backup_manifest",
        "bundle_id": bundle.name,
        "created_at": _iso_at(timestamp),
    }
    manifest["manifest_sha256"] = state_backup._retention_digest(
        manifest, "manifest_sha256"
    )
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
    )
    (bundle / "payload").write_bytes(b"x" * 17)
    return bundle


def _snapshot_tree_digest(root: Path, paths: list[str]) -> str:
    digest = hashlib.sha256()
    for item in paths:
        relative = Path(item)
        content = (root / relative).read_bytes()
        encoded = relative.as_posix().encode()
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _write_retention_snapshot(
    root: Path, *, timestamp: int, suffix: str, source_commit: str
) -> Path:
    snapshot = root / f"{suffix}-tree{suffix}"
    snapshot.mkdir()
    (snapshot / "payload.txt").write_text(f"{suffix}\n", encoding="utf-8")
    inventory = {
        "schema_version": 1,
        "kind": "bureau_registry_snapshot",
        "source_commit": source_commit,
        "tree_sha256": _snapshot_tree_digest(snapshot, ["payload.txt"]),
        "paths": ["payload.txt"],
    }
    (snapshot / ".bureau-runtime-snapshot.json").write_text(
        json.dumps(inventory, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.utime(snapshot, (timestamp, timestamp))
    return snapshot


def _write_review_receipt(root: Path, *, timestamp: int, suffix: str) -> Path:
    receipt = root / f"review-steward-{suffix}.json"
    receipt.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": f"review-steward-{suffix}",
                "reviewed_at": _iso_at(timestamp),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return receipt


def _retention_fixture(tmp_path: Path, *, now: int = 2_000_000_000) -> dict:
    backup_root = tmp_path / "backups"
    runtime_prefix = tmp_path / "runtime"
    snapshot_root = runtime_prefix / "registry-snapshots"
    runtime_backups = runtime_prefix / "backups" / "rollback-1"
    closure_root = tmp_path / "closure"
    review_root = closure_root / "review-receipts"
    for root in (
        backup_root,
        snapshot_root,
        runtime_backups,
        review_root,
    ):
        root.mkdir(parents=True, exist_ok=True)

    backup_old = _write_retention_backup(
        backup_root, timestamp=now - 300, suffix="old"
    )
    backup_middle = _write_retention_backup(
        backup_root, timestamp=now - 200, suffix="middle"
    )
    backup_new = _write_retention_backup(
        backup_root, timestamp=now - 100, suffix="new"
    )
    restore_root = backup_root / "restore-tests"
    restore_root.mkdir()
    (restore_root / "latest.json").write_text(
        json.dumps({"status": "verified", "bundle": str(backup_old)}) + "\n",
        encoding="utf-8",
    )

    snapshot_old = _write_retention_snapshot(
        snapshot_root,
        timestamp=now - 300,
        suffix="a" * 12,
        source_commit="a" * 40,
    )
    snapshot_middle = _write_retention_snapshot(
        snapshot_root,
        timestamp=now - 200,
        suffix="b" * 12,
        source_commit="b" * 40,
    )
    snapshot_new = _write_retention_snapshot(
        snapshot_root,
        timestamp=now - 100,
        suffix="c" * 12,
        source_commit="c" * 40,
    )
    rollback_manifest = runtime_backups / "deployment-manifest.json"
    rollback_manifest.write_text(
        json.dumps(
            {
                "canonical_registry_root": str(snapshot_middle),
                "rollback": {
                    "directory": None,
                    "manifest": None,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (runtime_prefix / "deployment-manifest.json").write_text(
        json.dumps(
            {
                "canonical_registry_root": str(snapshot_new),
                "rollback": {
                    "manifest": str(rollback_manifest),
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    review_old = _write_review_receipt(
        review_root, timestamp=now - 300, suffix="old"
    )
    review_middle = _write_review_receipt(
        review_root, timestamp=now - 200, suffix="middle"
    )
    review_new = _write_review_receipt(
        review_root, timestamp=now - 100, suffix="new"
    )
    (closure_root / "review-latest.json").write_text(
        json.dumps({"receipt_path": str(review_new)}) + "\n",
        encoding="utf-8",
    )
    (closure_root / "lanes.json").write_text(
        json.dumps(
            {
                "lanes": [
                    {
                        "lane_id": "lane-1",
                        "state": "reviewing",
                        "review_evidence": {"receipt_path": str(review_middle)},
                    }
                ]
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "now": now,
        "backup_root": backup_root,
        "runtime_prefix": runtime_prefix,
        "closure_root": closure_root,
        "backup_old": backup_old,
        "backup_middle": backup_middle,
        "backup_new": backup_new,
        "snapshot_old": snapshot_old,
        "snapshot_middle": snapshot_middle,
        "snapshot_new": snapshot_new,
        "review_old": review_old,
        "review_middle": review_middle,
        "review_new": review_new,
    }


def test_local_retention_plan_protects_restore_runtime_rollback_and_lane_references(
    tmp_path: Path,
):
    fixture = _retention_fixture(tmp_path)
    plan = state_backup.build_local_retention_plan(
        backup_root=fixture["backup_root"],
        runtime_prefix=fixture["runtime_prefix"],
        closure_root=fixture["closure_root"],
        policy=_retention_test_policy(),
        now_unix=fixture["now"],
    )

    assert plan["safe_to_apply"] is True
    assert plan["summary"]["candidate_count"] == 3
    candidates = {
        (store, Path(item["path"]))
        for store, result in plan["stores"].items()
        for item in result["candidates"]
    }
    assert candidates == {
        ("state_backups", fixture["backup_middle"]),
        ("registry_snapshots", fixture["snapshot_old"]),
        ("review_receipts", fixture["review_old"]),
    }
    retained = {
        Path(item["path"]): set(item["reasons"])
        for result in plan["stores"].values()
        for item in result["retained"]
    }
    assert "latest-verified-restore-test" in retained[fixture["backup_old"]]
    assert "current-runtime" in retained[fixture["snapshot_new"]]
    assert "rollback-manifest-depth:1" in retained[fixture["snapshot_middle"]]
    assert "review-latest" in retained[fixture["review_new"]]
    assert "lane-review-binding" in retained[fixture["review_middle"]]
    assert plan["plan_sha256"] == state_backup._retention_digest(
        plan, "plan_sha256"
    )
    assert plan["automatic_cleanup_authorized"] is False


def test_local_retention_recent_window_is_hard_count_bounded(tmp_path: Path):
    fixture = _retention_fixture(tmp_path)
    policy = _retention_test_policy(recent_max_count=2)
    policy["stores"]["review_receipts"]["recent_seconds"] = 10_000
    for index in range(20):
        _write_review_receipt(
            fixture["closure_root"] / "review-receipts",
            timestamp=fixture["now"] - index,
            suffix=f"dense-{index:02d}",
        )

    plan = state_backup.build_local_retention_plan(
        backup_root=fixture["backup_root"],
        runtime_prefix=fixture["runtime_prefix"],
        closure_root=fixture["closure_root"],
        policy=policy,
        now_unix=fixture["now"],
    )

    recent_kept = [
        item
        for item in plan["stores"]["review_receipts"]["retained"]
        if "recent-window" in item["reasons"]
    ]
    assert len(recent_kept) == 2
    assert plan["stores"]["review_receipts"]["candidate_count"] >= 19


def test_local_retention_apply_removes_only_exact_planned_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fixture = _retention_fixture(tmp_path)
    plan = state_backup.build_local_retention_plan(
        backup_root=fixture["backup_root"],
        runtime_prefix=fixture["runtime_prefix"],
        closure_root=fixture["closure_root"],
        policy=_retention_test_policy(),
        now_unix=fixture["now"],
    )
    receipt_path = tmp_path / "receipts" / "apply.json"
    monkeypatch.setattr(
        state_backup,
        "_retention_recovery_gate",
        lambda *_args, **_kwargs: {"status": "verified", "test": True},
    )

    result = state_backup.apply_local_retention_plan(
        plan,
        expected_plan_sha256=plan["plan_sha256"],
        confirmation=f"APPLY:{plan['plan_sha256']}",
        receipt_path=receipt_path,
        now_unix=fixture["now"],
    )

    assert result["state"] == "complete"
    assert result["candidate_count"] == 3
    assert result["recovery_gate"] == {"status": "verified", "test": True}
    assert not fixture["backup_middle"].exists()
    assert not fixture["snapshot_old"].exists()
    assert not fixture["review_old"].exists()
    for protected in (
        fixture["backup_old"],
        fixture["backup_new"],
        fixture["snapshot_middle"],
        fixture["snapshot_new"],
        fixture["review_middle"],
        fixture["review_new"],
    ):
        assert protected.exists()
    stored = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert stored["receipt_sha256"] == state_backup._retention_digest(
        stored, "receipt_sha256"
    )

    replayed = state_backup.apply_local_retention_plan(
        plan,
        expected_plan_sha256=plan["plan_sha256"],
        confirmation=f"APPLY:{plan['plan_sha256']}",
        receipt_path=receipt_path,
        now_unix=fixture["now"],
    )
    assert replayed["replayed"] is True


def test_local_retention_apply_fails_closed_after_candidate_tamper(tmp_path: Path):
    fixture = _retention_fixture(tmp_path)
    plan = state_backup.build_local_retention_plan(
        backup_root=fixture["backup_root"],
        runtime_prefix=fixture["runtime_prefix"],
        closure_root=fixture["closure_root"],
        policy=_retention_test_policy(),
        now_unix=fixture["now"],
    )
    fixture["review_old"].write_text(
        fixture["review_old"].read_text(encoding="utf-8") + " ",
        encoding="utf-8",
    )

    with pytest.raises(
        state_backup.StateBackupError,
        match=r"fresh local retention readback is blocked|no longer eligible",
    ):
        state_backup.apply_local_retention_plan(
            plan,
            expected_plan_sha256=plan["plan_sha256"],
            confirmation=f"APPLY:{plan['plan_sha256']}",
            receipt_path=tmp_path / "tamper-receipt.json",
            now_unix=fixture["now"],
        )
    assert fixture["backup_middle"].exists()
    assert fixture["snapshot_old"].exists()


def test_local_retention_plan_blocks_missing_protected_reference(tmp_path: Path):
    fixture = _retention_fixture(tmp_path)
    (fixture["closure_root"] / "review-latest.json").write_text(
        json.dumps(
            {
                "receipt_path": str(
                    fixture["closure_root"] / "review-receipts" / "missing.json"
                )
            }
        )
        + "\n",
        encoding="utf-8",
    )

    plan = state_backup.build_local_retention_plan(
        backup_root=fixture["backup_root"],
        runtime_prefix=fixture["runtime_prefix"],
        closure_root=fixture["closure_root"],
        policy=_retention_test_policy(),
        now_unix=fixture["now"],
    )

    assert plan["safe_to_apply"] is False
    assert plan["summary"]["error_count"] >= 1
    assert any(
        item.get("reason") == "protected-reference-target-missing"
        for item in plan["errors"]
    )


def test_local_retention_apply_requires_exact_hash_and_confirmation(tmp_path: Path):
    fixture = _retention_fixture(tmp_path)
    plan = state_backup.build_local_retention_plan(
        backup_root=fixture["backup_root"],
        runtime_prefix=fixture["runtime_prefix"],
        closure_root=fixture["closure_root"],
        policy=_retention_test_policy(),
        now_unix=fixture["now"],
    )

    with pytest.raises(state_backup.StateBackupError, match="plan hash mismatch"):
        state_backup.apply_local_retention_plan(
            plan,
            expected_plan_sha256="0" * 64,
            confirmation="APPLY:" + "0" * 64,
            receipt_path=tmp_path / "wrong-hash.json",
            now_unix=fixture["now"],
        )
    with pytest.raises(state_backup.StateBackupError, match="confirmation mismatch"):
        state_backup.apply_local_retention_plan(
            plan,
            expected_plan_sha256=plan["plan_sha256"],
            confirmation="wrong",
            receipt_path=tmp_path / "wrong-confirmation.json",
            now_unix=fixture["now"],
        )


def test_local_retention_plan_blocks_unfinished_delete_quarantine(tmp_path: Path):
    fixture = _retention_fixture(tmp_path)
    residue = fixture["backup_root"] / ".retention-delete-deadbeef"
    residue.mkdir()
    (residue / "marker").write_text("partial\n", encoding="utf-8")

    plan = state_backup.build_local_retention_plan(
        backup_root=fixture["backup_root"],
        runtime_prefix=fixture["runtime_prefix"],
        closure_root=fixture["closure_root"],
        policy=_retention_test_policy(),
        now_unix=fixture["now"],
    )

    assert plan["safe_to_apply"] is False
    assert any(
        item.get("store") == "state_backups"
        and item.get("reason") == "unfinished-retention-delete"
        for item in plan["errors"]
    )


def _write_restore_gate_receipt(
    fixture: dict,
    *,
    tested_at_unix: int | None = None,
    bundle: Path | None = None,
) -> dict:
    historical = bundle or fixture["backup_old"]
    manifest = json.loads(
        (historical / "manifest.json").read_text(encoding="utf-8")
    )
    receipt = {
        "schema_version": 1,
        "kind": "bureau_state_restore_test_receipt",
        "status": "verified",
        "tested_at": _iso_at(
            tested_at_unix if tested_at_unix is not None else fixture["now"] - 60
        ),
        "bundle": str(historical),
        "manifest_sha256": manifest["manifest_sha256"],
        "authoritative_root_sha256": "a" * 64,
        "event_count": 17,
        "envelope_root_sha256": "b" * 64,
        "receipt_root_sha256": "c" * 64,
        "empty_target_created": True,
        "external_leases_restored": False,
        "external_state_reused": False,
        "post_restore_reconcile": {
            "status": "reconciled",
            "mode": "fresh-external-readback",
            "default": "fail-closed",
            "lease_reactivation": False,
        },
    }
    receipt["receipt_sha256"] = state_backup._retention_digest(
        receipt, "receipt_sha256"
    )
    path = fixture["backup_root"] / "restore-tests" / "latest.json"
    path.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
    return receipt


def test_retention_recovery_gate_revalidates_historical_and_restores_newest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fixture = _retention_fixture(tmp_path)
    historical_receipt = _write_restore_gate_receipt(fixture)
    plan = state_backup.build_local_retention_plan(
        backup_root=fixture["backup_root"],
        runtime_prefix=fixture["runtime_prefix"],
        closure_root=fixture["closure_root"],
        policy=_retention_test_policy(),
        now_unix=fixture["now"],
    )
    calls: dict[str, list[Path]] = {"verify": [], "restore": []}

    def fake_verify(bundle: Path):
        bundle = Path(bundle)
        calls["verify"].append(bundle)
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        return {
            "status": "verified",
            "bundle": str(bundle),
            "manifest_sha256": manifest["manifest_sha256"],
            "authoritative_root_sha256": "a" * 64,
            "event_count": 17,
            "envelope_root_sha256": "b" * 64,
            "receipt_root_sha256": "c" * 64,
        }

    def fake_restore_test(*, bundle: Path, **_kwargs):
        bundle = Path(bundle)
        calls["restore"].append(bundle)
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        return {
            "status": "verified",
            "tested_at": _iso_at(fixture["now"]),
            "manifest_sha256": manifest["manifest_sha256"],
            "authoritative_root_sha256": "d" * 64,
            "receipt_sha256": "e" * 64,
        }

    monkeypatch.setattr(state_backup, "verify_backup", fake_verify)
    monkeypatch.setattr(state_backup, "restore_test", fake_restore_test)
    monkeypatch.setattr(
        state_backup,
        "_runtime_registry_root",
        lambda: fixture["runtime_prefix"] / "registry-snapshots",
    )

    result = state_backup._retention_recovery_gate(
        plan, now_unix=fixture["now"]
    )

    assert result["status"] == "verified"
    assert result["historical"]["receipt_sha256"] == historical_receipt["receipt_sha256"]
    assert calls["verify"] == [fixture["backup_old"]]
    assert calls["restore"] == [fixture["backup_new"]]
    assert result["distinct_recovery_points"] is True


def test_retention_recovery_gate_rejects_stale_historical_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fixture = _retention_fixture(tmp_path)
    _write_restore_gate_receipt(
        fixture,
        tested_at_unix=fixture["now"] - 40 * 60 * 60,
    )
    plan = state_backup.build_local_retention_plan(
        backup_root=fixture["backup_root"],
        runtime_prefix=fixture["runtime_prefix"],
        closure_root=fixture["closure_root"],
        policy=_retention_test_policy(),
        now_unix=fixture["now"],
    )
    monkeypatch.setattr(
        state_backup,
        "verify_backup",
        lambda *_args, **_kwargs: pytest.fail("stale receipt must fail first"),
    )

    with pytest.raises(state_backup.StateBackupError, match="receipt is stale"):
        state_backup._retention_recovery_gate(plan, now_unix=fixture["now"])


def test_retention_recovery_gate_requires_distinct_historical_point(
    tmp_path: Path,
):
    fixture = _retention_fixture(tmp_path)
    _write_restore_gate_receipt(fixture, bundle=fixture["backup_new"])
    policy = _retention_test_policy(recent_max_count=2)
    policy["stores"]["state_backups"]["recent_seconds"] = 10_000
    plan = state_backup.build_local_retention_plan(
        backup_root=fixture["backup_root"],
        runtime_prefix=fixture["runtime_prefix"],
        closure_root=fixture["closure_root"],
        policy=policy,
        now_unix=fixture["now"],
    )

    with pytest.raises(
        state_backup.StateBackupError,
        match="historical restore point must differ",
    ):
        state_backup._retention_recovery_gate(plan, now_unix=fixture["now"])
