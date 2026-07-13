from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

import pytest

from bureau import legacy, state_root_artifacts
from bureau.cli import _command_mutates, parser
from bureau.state_root_artifacts import (
    apply_state_root_migration_plan,
    managed_state_root_inventory,
    rollback_state_root_migration,
    write_state_root_migration_plan,
)


def canonical_sha256(value):
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def write_completion_evidence(state_root: Path):
    bundle = state_root / "evidence" / "grabowski-completion"
    bundle.mkdir(parents=True)
    diff_bytes = b"diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-old\n+new\n"
    (bundle / "pr.diff").write_bytes(diff_bytes)
    review = {
        "schema_version": 1,
        "kind": "bureau_pr_self_review",
        "repository": "heimgewebe/bureau",
        "pull_request": 523,
        "reviewed_head": "a" * 40,
        "base_head": "b" * 40,
        "github_diff_sha256": hashlib.sha256(diff_bytes).hexdigest(),
        "github_diff_bytes": len(diff_bytes),
        "axes": {
            axis: {"result": "PASS", "evidence": [f"{axis} checked"]}
            for axis in (
                "correctness",
                "integration",
                "regression_risk",
                "security",
                "tests",
            )
        },
        "conclusion": "PASS",
        "merge_condition": "head and diff unchanged",
        "reviewed_at_unix": 1,
    }
    review["review_sha256"] = canonical_sha256(review)
    (bundle / "self-review.json").write_text(json.dumps(review, indent=2) + "\n", encoding="utf-8")


def write_reviewed_plan(state_root: Path):
    plans = state_root / "plans"
    plans.mkdir()
    task_id = "TEST-INITIATIVE-V1-T001"
    plan = {
        "schema_version": 2,
        "command": "live-promote-plan",
        "event_id": 42,
        "initiative": "TEST-INITIATIVE-V1",
        "task_id": task_id,
        "source_event": {
            "event_id": 42,
            "record": {
                "kind": "candidate_task",
                "candidate_id": "candidate-test",
                "status": "observed",
            },
        },
        "task_json": {
            "schema_version": 1,
            "id": task_id,
            "initiative": "TEST-INITIATIVE-V1",
            "title": "Test task",
            "state": "planned",
        },
        "review": {"required": True, "status": "pending"},
        "does_not_establish": ["queue_mutation", "claim_authority"],
    }
    generated_sha256 = canonical_sha256(plan)
    plan["review"] = {
        "required": True,
        "status": "reviewed",
        "reviewer": "test-reviewer",
    }
    plan["plan_sha256"] = generated_sha256
    (plans / "test-plan.json").write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")


def empty_reference_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "registry").mkdir(parents=True)
    (root / "docs").mkdir()
    return root


def review_plan(path: Path):
    value = json.loads(path.read_text(encoding="utf-8"))
    value["review"] = {
        "required": True,
        "status": "reviewed",
        "reviewer": "test-reviewer",
        "reviewed_at": "2026-07-13T18:00:00Z",
        "review_payload_sha256": value["review_payload_sha256"],
        "entries_sha256": value["entries_sha256"],
        "destination_root": value["destination_root"],
    }
    path.write_text(legacy.canonical_json(value) + "\n", encoding="utf-8")
    return value


def write_plan(tmp_path: Path, names=("artifact.txt",)):
    state_root = tmp_path / "state"
    state_root.mkdir()
    for name in names:
        (state_root / name).write_text(f"content for {name}\n", encoding="utf-8")
    reference_root = empty_reference_root(tmp_path)
    destination = tmp_path / "quarantine" / "run-1"
    plan_path = tmp_path / "plans" / "migration.json"
    result = write_state_root_migration_plan(
        state_root,
        list(names),
        destination,
        plan_path,
        reference_root=reference_root,
    )
    return state_root, destination, plan_path, result


def test_managed_inventory_reports_every_child_and_producer(tmp_path):
    state_root = tmp_path / "state"
    state_root.mkdir()
    write_completion_evidence(state_root)
    write_reviewed_plan(state_root)

    result = managed_state_root_inventory(state_root)

    assert result["healthy"] is True
    by_name = {item["name"]: item for item in result["entries"]}
    evidence = by_name["evidence"]["children"][0]
    assert evidence["valid"] is True
    assert evidence["producer"]["pull_request"] == 523
    assert {item["name"] for item in evidence["children"]} == {
        "pr.diff",
        "self-review.json",
    }
    reviewed_plan = by_name["plans"]["children"][0]
    assert reviewed_plan["producer"]["event_id"] == 42
    assert reviewed_plan["authority"] == "proposal-only"
    assert reviewed_plan["retention_class"] == "until-applied-or-superseded"


def test_migration_plan_is_create_only_and_review_bound(tmp_path):
    state_root, destination, plan_path, result = write_plan(tmp_path)

    assert result["review"]["status"] == "pending"
    assert result["entries"][0]["source"] == str(state_root / "artifact.txt")
    assert result["entries"][0]["destination"] == str(destination / "artifact.txt")
    with pytest.raises(FileExistsError):
        write_state_root_migration_plan(
            state_root,
            ["artifact.txt"],
            destination,
            plan_path,
            reference_root=tmp_path / "repo",
        )
    with pytest.raises(legacy.StateError, match="not reviewed"):
        apply_state_root_migration_plan(plan_path)


def test_reviewed_migration_rejects_operational_payload_tampering(tmp_path):
    state_root, destination, plan_path, _ = write_plan(tmp_path)
    review_plan(plan_path)
    original_reference_root = tmp_path / "repo"
    (original_reference_root / "registry" / "live-binding.json").write_text(
        str(state_root / "artifact.txt"), encoding="utf-8"
    )
    bypass_reference_root = tmp_path / "bypass-repo"
    (bypass_reference_root / "registry").mkdir(parents=True)
    (bypass_reference_root / "docs").mkdir()
    value = json.loads(plan_path.read_text(encoding="utf-8"))
    value["reference_root"] = str(bypass_reference_root.resolve())
    plan_path.write_text(legacy.canonical_json(value) + "\n", encoding="utf-8")

    with pytest.raises(legacy.StateError, match="payload digest mismatch"):
        apply_state_root_migration_plan(plan_path)

    assert (state_root / "artifact.txt").exists()
    assert not destination.exists()


def test_reviewed_migration_apply_is_atomic_receipted_and_idempotent(tmp_path):
    state_root, destination, plan_path, _ = write_plan(tmp_path, names=("first.txt", "second.txt"))
    review_plan(plan_path)

    result = apply_state_root_migration_plan(plan_path)

    assert result["idempotent_rerun"] is False
    assert result["receipt_sha256"]
    assert not (state_root / "first.txt").exists()
    assert (destination / "first.txt").read_text(encoding="utf-8") == ("content for first.txt\n")
    rerun = apply_state_root_migration_plan(plan_path)
    assert rerun["idempotent_rerun"] is True
    assert rerun["reviewed_plan_sha256"] == result["reviewed_plan_sha256"]


def test_idempotent_rerun_rejects_tampered_receipt(tmp_path):
    _, _, plan_path, _ = write_plan(tmp_path)
    review_plan(plan_path)
    result = apply_state_root_migration_plan(plan_path)
    receipt_path = Path(result["receipt_path"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["entries"][0]["status"] = "tampered"
    receipt_path.write_text(json.dumps(receipt) + "\n", encoding="utf-8")

    with pytest.raises(legacy.StateError, match="receipt does not match"):
        apply_state_root_migration_plan(plan_path)


def test_idempotent_rerun_rejects_destination_drift(tmp_path):
    _, destination, plan_path, _ = write_plan(tmp_path)
    review_plan(plan_path)
    apply_state_root_migration_plan(plan_path)
    (destination / "artifact.txt").write_text("changed after apply\n", encoding="utf-8")

    with pytest.raises(legacy.StateError, match="destination state mismatch"):
        apply_state_root_migration_plan(plan_path)


def test_receipt_write_failure_rolls_back_moved_entries(tmp_path, monkeypatch):
    state_root, destination, plan_path, _ = write_plan(tmp_path)
    review_plan(plan_path)

    def fail_receipt_write(path, content):
        raise OSError("simulated receipt write failure")

    monkeypatch.setattr(state_root_artifacts, "_write_create_only", fail_receipt_write)
    with pytest.raises(OSError, match="simulated receipt write failure"):
        apply_state_root_migration_plan(plan_path)

    assert (state_root / "artifact.txt").exists()
    assert not (destination / "artifact.txt").exists()


def test_migration_refuses_source_drift_without_effect(tmp_path):
    state_root, destination, plan_path, _ = write_plan(tmp_path)
    review_plan(plan_path)
    (state_root / "artifact.txt").write_text("changed\n", encoding="utf-8")

    with pytest.raises(legacy.StateError, match="changed since review"):
        apply_state_root_migration_plan(plan_path)

    assert (state_root / "artifact.txt").read_text(encoding="utf-8") == "changed\n"
    assert not (destination / "artifact.txt").exists()


def test_migration_refuses_destination_collision(tmp_path):
    state_root, destination, plan_path, _ = write_plan(tmp_path)
    review_plan(plan_path)
    destination.mkdir(parents=True)
    (destination / "artifact.txt").write_text("foreign\n", encoding="utf-8")

    with pytest.raises(legacy.StateError, match="collision"):
        apply_state_root_migration_plan(plan_path)

    assert (state_root / "artifact.txt").exists()


def test_interrupted_migration_rolls_back_this_run(tmp_path, monkeypatch):
    state_root, destination, plan_path, _ = write_plan(tmp_path, names=("first.txt", "second.txt"))
    review_plan(plan_path)
    original_rename = state_root_artifacts._rename_at

    def fail_second(source_descriptor, source_name, destination_descriptor, destination_name):
        if source_name == "second.txt":
            raise OSError("simulated interruption")
        return original_rename(
            source_descriptor, source_name, destination_descriptor, destination_name
        )

    monkeypatch.setattr(state_root_artifacts, "_rename_at", fail_second)
    with pytest.raises(OSError, match="simulated interruption"):
        apply_state_root_migration_plan(plan_path)

    assert (state_root / "first.txt").exists()
    assert (state_root / "second.txt").exists()
    assert not (destination / "first.txt").exists()


def test_receipt_rollback_restores_original_paths(tmp_path):
    state_root, destination, plan_path, _ = write_plan(tmp_path)
    review_plan(plan_path)
    result = apply_state_root_migration_plan(plan_path)

    rollback = rollback_state_root_migration(Path(result["receipt_path"]))

    assert rollback["command"] == "state-root-artifacts-migration-rollback"
    assert (state_root / "artifact.txt").exists()
    assert not (destination / "artifact.txt").exists()


def test_receipt_rollback_preflights_all_entries_before_effect(tmp_path):
    state_root, destination, plan_path, _ = write_plan(tmp_path, names=("first.txt", "second.txt"))
    review_plan(plan_path)
    result = apply_state_root_migration_plan(plan_path)
    (state_root / "first.txt").write_text("foreign\n", encoding="utf-8")

    with pytest.raises(legacy.StateError, match="destination collision"):
        rollback_state_root_migration(Path(result["receipt_path"]))

    assert (state_root / "first.txt").read_text(encoding="utf-8") == "foreign\n"
    assert not (state_root / "second.txt").exists()
    assert (destination / "first.txt").exists()
    assert (destination / "second.txt").exists()


def test_receipt_rollback_reverts_this_run_after_rename_failure(tmp_path, monkeypatch):
    state_root, destination, plan_path, _ = write_plan(tmp_path, names=("first.txt", "second.txt"))
    review_plan(plan_path)
    result = apply_state_root_migration_plan(plan_path)
    original_rename = state_root_artifacts._rename_at

    def fail_first_restore(
        source_descriptor, source_name, destination_descriptor, destination_name
    ):
        if source_name == "first.txt":
            raise OSError("simulated rollback interruption")
        return original_rename(
            source_descriptor, source_name, destination_descriptor, destination_name
        )

    monkeypatch.setattr(state_root_artifacts, "_rename_at", fail_first_restore)
    with pytest.raises(OSError, match="simulated rollback interruption"):
        rollback_state_root_migration(Path(result["receipt_path"]))

    assert not (state_root / "first.txt").exists()
    assert not (state_root / "second.txt").exists()
    assert (destination / "first.txt").exists()
    assert (destination / "second.txt").exists()


def test_directory_entry_apply_and_rollback_preserve_bound_tree(tmp_path):
    state_root = tmp_path / "state"
    entry = state_root / "artifact-dir"
    nested = entry / "nested"
    nested.mkdir(parents=True)
    (nested / "payload.txt").write_text("payload\n", encoding="utf-8")
    reference_root = empty_reference_root(tmp_path)
    destination = tmp_path / "quarantine" / "run-1"
    plan_path = tmp_path / "directory-migration.json"

    result = write_state_root_migration_plan(
        state_root,
        ["artifact-dir"],
        destination,
        plan_path,
        reference_root=reference_root,
    )
    assert result["entries"][0]["type"] == "directory"
    assert result["entries"][0]["inode"] == entry.stat().st_ino
    review_plan(plan_path)

    applied = apply_state_root_migration_plan(plan_path)
    moved_entry = destination / "artifact-dir"
    assert (moved_entry / "nested" / "payload.txt").read_text(encoding="utf-8") == ("payload\n")
    assert not entry.exists()

    rollback_state_root_migration(Path(applied["receipt_path"]))
    assert (entry / "nested" / "payload.txt").read_text(encoding="utf-8") == ("payload\n")
    assert not moved_entry.exists()


def test_apply_rejects_replaced_destination_base_before_effect(tmp_path):
    state_root = tmp_path / "state"
    state_root.mkdir()
    (state_root / "artifact.txt").write_text("content\n", encoding="utf-8")
    reference_root = empty_reference_root(tmp_path)
    destination_base = tmp_path / "destination-base"
    destination_base.mkdir()
    destination = destination_base / "quarantine" / "run-1"
    plan_path = tmp_path / "destination-base-migration.json"
    write_state_root_migration_plan(
        state_root,
        ["artifact.txt"],
        destination,
        plan_path,
        reference_root=reference_root,
    )
    review_plan(plan_path)
    parked_base = tmp_path / "destination-base-reviewed"
    os.rename(destination_base, parked_base)
    destination_base.mkdir()

    with pytest.raises(
        legacy.StateError, match="destination-base directory descriptor identity mismatch"
    ):
        apply_state_root_migration_plan(plan_path)

    assert (state_root / "artifact.txt").exists()
    assert not destination.exists()


def test_migration_plan_binds_directory_anchors_and_platform_contract(tmp_path):
    state_root, destination, _, result = write_plan(tmp_path)

    assert result["schema_version"] == 2
    assert result["platform_contract"] == {
        "mutation_mode": "linux-descriptor-relative-v1",
        "no_follow": True,
        "silent_fallback": False,
    }
    anchors = result["directory_anchors"]
    assert anchors["state_root"]["path"] == str(state_root)
    assert anchors["state_root"]["device"] == state_root.stat().st_dev
    assert anchors["state_root"]["inode"] == state_root.stat().st_ino
    assert anchors["destination_base"]["path"] == str(tmp_path)
    assert result["destination_layout"] == {
        "base_path": str(tmp_path),
        "missing_components": ["quarantine", "run-1"],
    }
    assert result["entries"][0]["destination"] == str(destination / "artifact.txt")


def test_apply_rejects_and_compensates_final_entry_replacement(tmp_path, monkeypatch):
    state_root, destination, plan_path, _ = write_plan(tmp_path)
    review_plan(plan_path)
    reviewed_entry = state_root / "artifact.txt"
    parked_entry = state_root / "artifact.reviewed"
    original_rename = state_root_artifacts._rename_at
    swapped = False

    def replace_entry(source_descriptor, source_name, destination_descriptor, destination_name):
        nonlocal swapped
        if not swapped:
            swapped = True
            os.rename(reviewed_entry, parked_entry)
            reviewed_entry.write_text("content for artifact.txt\n", encoding="utf-8")
        return original_rename(
            source_descriptor, source_name, destination_descriptor, destination_name
        )

    monkeypatch.setattr(state_root_artifacts, "_rename_at", replace_entry)
    with pytest.raises(legacy.StateError, match="destination identity mismatch"):
        apply_state_root_migration_plan(plan_path)

    assert parked_entry.read_text(encoding="utf-8") == "content for artifact.txt\n"
    assert reviewed_entry.read_text(encoding="utf-8") == "content for artifact.txt\n"
    assert parked_entry.stat().st_ino != reviewed_entry.stat().st_ino
    assert not destination.exists()


def test_apply_compensates_source_ancestor_replacement(tmp_path, monkeypatch):
    state_root, destination, plan_path, _ = write_plan(tmp_path)
    review_plan(plan_path)
    parked_root = tmp_path / "state-reviewed"
    original_rename = state_root_artifacts._rename_at
    swapped = False

    def replace_ancestor(source_descriptor, source_name, destination_descriptor, destination_name):
        nonlocal swapped
        if not swapped:
            swapped = True
            os.rename(state_root, parked_root)
            state_root.mkdir()
            (state_root / "artifact.txt").write_text("replacement\n", encoding="utf-8")
        return original_rename(
            source_descriptor, source_name, destination_descriptor, destination_name
        )

    monkeypatch.setattr(state_root_artifacts, "_rename_at", replace_ancestor)
    with pytest.raises(
        legacy.StateError, match="state-root directory descriptor identity mismatch"
    ):
        apply_state_root_migration_plan(plan_path)

    assert (parked_root / "artifact.txt").read_text(encoding="utf-8") == (
        "content for artifact.txt\n"
    )
    assert (state_root / "artifact.txt").read_text(encoding="utf-8") == "replacement\n"
    assert not destination.exists()


def test_apply_compensates_reference_root_ancestor_replacement(tmp_path, monkeypatch):
    state_root, destination, plan_path, _ = write_plan(tmp_path)
    review_plan(plan_path)
    reference_root = tmp_path / "repo"
    parked_reference = tmp_path / "repo-reviewed"
    original_rename = state_root_artifacts._rename_at
    swapped = False

    def replace_reference(source_descriptor, source_name, destination_descriptor, destination_name):
        nonlocal swapped
        if not swapped:
            swapped = True
            os.rename(reference_root, parked_reference)
            (reference_root / "registry").mkdir(parents=True)
            (reference_root / "docs").mkdir()
        return original_rename(
            source_descriptor, source_name, destination_descriptor, destination_name
        )

    monkeypatch.setattr(state_root_artifacts, "_rename_at", replace_reference)
    with pytest.raises(
        legacy.StateError, match="reference-root directory descriptor identity mismatch"
    ):
        apply_state_root_migration_plan(plan_path)

    assert (state_root / "artifact.txt").read_text(encoding="utf-8") == (
        "content for artifact.txt\n"
    )
    assert not destination.exists()


def test_apply_never_deletes_replacement_destination_ancestor(tmp_path, monkeypatch):
    state_root, destination, plan_path, _ = write_plan(tmp_path)
    review_plan(plan_path)
    destination_parent = destination.parent
    parked_parent = tmp_path / "quarantine-reviewed"
    original_rename = state_root_artifacts._rename_at
    swapped = False

    def replace_destination(
        source_descriptor, source_name, destination_descriptor, destination_name
    ):
        nonlocal swapped
        if not swapped:
            swapped = True
            os.rename(destination_parent, parked_parent)
            destination.mkdir(parents=True)
        return original_rename(
            source_descriptor, source_name, destination_descriptor, destination_name
        )

    monkeypatch.setattr(state_root_artifacts, "_rename_at", replace_destination)
    with pytest.raises(legacy.StateError, match="incomplete recovery"):
        apply_state_root_migration_plan(plan_path)

    assert (state_root / "artifact.txt").read_text(encoding="utf-8") == (
        "content for artifact.txt\n"
    )
    assert destination.is_dir()
    assert list(destination.iterdir()) == []
    assert parked_parent.is_dir()


@pytest.mark.parametrize("role", ["state", "reference"])
def test_apply_rejects_symlinked_bound_root(tmp_path, role):
    state_root, destination, plan_path, _ = write_plan(tmp_path)
    review_plan(plan_path)
    target = state_root if role == "state" else tmp_path / "repo"
    parked = tmp_path / f"{role}-reviewed"
    os.rename(target, parked)
    target.symlink_to(parked, target_is_directory=True)

    with pytest.raises(legacy.StateError, match="symlink or non-directory"):
        apply_state_root_migration_plan(plan_path)

    assert not destination.exists()


def test_rollback_compensates_state_root_ancestor_replacement(tmp_path, monkeypatch):
    state_root, destination, plan_path, _ = write_plan(tmp_path)
    review_plan(plan_path)
    result = apply_state_root_migration_plan(plan_path)
    parked_root = tmp_path / "state-reviewed"
    original_rename = state_root_artifacts._rename_at
    swapped = False

    def replace_ancestor(source_descriptor, source_name, destination_descriptor, destination_name):
        nonlocal swapped
        if not swapped:
            swapped = True
            os.rename(state_root, parked_root)
            state_root.mkdir()
            (state_root / "artifact.txt").write_text("replacement\n", encoding="utf-8")
        return original_rename(
            source_descriptor, source_name, destination_descriptor, destination_name
        )

    monkeypatch.setattr(state_root_artifacts, "_rename_at", replace_ancestor)
    with pytest.raises(
        legacy.StateError, match="state-root directory descriptor identity mismatch"
    ):
        rollback_state_root_migration(Path(result["receipt_path"]))

    assert (state_root / "artifact.txt").read_text(encoding="utf-8") == "replacement\n"
    assert not (parked_root / "artifact.txt").exists()
    assert (destination / "artifact.txt").read_text(encoding="utf-8") == (
        "content for artifact.txt\n"
    )


def test_apply_rejects_cross_device_destination(tmp_path):
    shared_memory = Path("/dev/shm")
    if not shared_memory.is_dir():
        pytest.skip("/dev/shm is unavailable")
    if shared_memory.stat().st_dev == tmp_path.stat().st_dev:
        pytest.skip("/dev/shm is not a distinct filesystem")
    state_root = tmp_path / "state"
    state_root.mkdir()
    (state_root / "artifact.txt").write_text("content\n", encoding="utf-8")
    reference_root = empty_reference_root(tmp_path)
    with tempfile.TemporaryDirectory(prefix="bureau-t013-", dir=shared_memory) as raw:
        destination = Path(raw) / "run"
        plan_path = tmp_path / "migration-cross-device.json"
        write_state_root_migration_plan(
            state_root,
            ["artifact.txt"],
            destination,
            plan_path,
            reference_root=reference_root,
        )
        review_plan(plan_path)

        with pytest.raises(legacy.StateError, match="same-filesystem"):
            apply_state_root_migration_plan(plan_path)

        assert (state_root / "artifact.txt").exists()
        assert not destination.exists()


def test_mutation_fails_closed_without_descriptor_relative_support(tmp_path, monkeypatch):
    state_root = tmp_path / "state"
    state_root.mkdir()
    (state_root / "artifact.txt").write_text("content\n", encoding="utf-8")
    reference_root = empty_reference_root(tmp_path)
    monkeypatch.setattr(
        state_root_artifacts,
        "_descriptor_relative_support_error",
        lambda: "synthetic unsupported platform",
    )

    with pytest.raises(legacy.StateError, match=r"descriptor-relative.*unsupported"):
        write_state_root_migration_plan(
            state_root,
            ["artifact.txt"],
            tmp_path / "quarantine",
            tmp_path / "migration.json",
            reference_root=reference_root,
        )

    assert (state_root / "artifact.txt").exists()
    assert not (tmp_path / "quarantine").exists()


def test_reference_scan_stays_on_open_descriptor_during_public_root_swap(tmp_path, monkeypatch):
    state_root = tmp_path / "state"
    state_root.mkdir()
    source = state_root / "artifact.txt"
    source.write_text("artifact\n", encoding="utf-8")
    reference_root = empty_reference_root(tmp_path)
    (reference_root / "registry" / "task.json").write_text(
        json.dumps({"evidence": str(source)}), encoding="utf-8"
    )
    parked_reference = tmp_path / "repo-reviewed"
    original_rglob = Path.rglob
    swapped = False

    def swap_before_traversal(base, pattern):
        nonlocal swapped
        if not swapped and base.name == "registry":
            swapped = True
            os.rename(reference_root, parked_reference)
            (reference_root / "registry").mkdir(parents=True)
            (reference_root / "docs").mkdir()
        return original_rglob(base, pattern)

    monkeypatch.setattr(Path, "rglob", swap_before_traversal)
    with pytest.raises(legacy.StateError, match="repository evidence"):
        write_state_root_migration_plan(
            state_root,
            [source.name],
            tmp_path / "quarantine",
            tmp_path / "migration.json",
            reference_root=reference_root,
        )

    assert swapped is True
    assert (parked_reference / "registry" / "task.json").exists()
    assert not (tmp_path / "quarantine").exists()


def test_plan_refuses_process_reference_through_hardlink_alias(tmp_path):
    state_root = tmp_path / "state"
    state_root.mkdir()
    source = state_root / "artifact.txt"
    source.write_text("artifact\n", encoding="utf-8")
    alias = tmp_path / "artifact-alias.txt"
    os.link(source, alias)

    with alias.open("rb"), pytest.raises(legacy.StateError, match="active process"):
        write_state_root_migration_plan(
            state_root,
            [source.name],
            tmp_path / "quarantine",
            tmp_path / "migration.json",
            reference_root=empty_reference_root(tmp_path),
        )

    assert source.exists()
    assert not (tmp_path / "quarantine").exists()


def test_plan_refuses_repository_reference(tmp_path):
    state_root = tmp_path / "state"
    state_root.mkdir()
    source = state_root / "artifact.txt"
    source.write_text("artifact\n", encoding="utf-8")
    reference_root = empty_reference_root(tmp_path)
    (reference_root / "registry" / "task.json").write_text(
        json.dumps({"evidence": str(source)}), encoding="utf-8"
    )

    with pytest.raises(legacy.StateError, match="referenced"):
        write_state_root_migration_plan(
            state_root,
            [source.name],
            tmp_path / "quarantine",
            tmp_path / "migration.json",
            reference_root=reference_root,
        )


def test_plan_refuses_symlink_source(tmp_path):
    state_root = tmp_path / "state"
    state_root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (state_root / "linked.txt").symlink_to(outside)

    with pytest.raises(legacy.StateError, match="symlink"):
        write_state_root_migration_plan(
            state_root,
            ["linked.txt"],
            tmp_path / "quarantine",
            tmp_path / "migration.json",
            reference_root=empty_reference_root(tmp_path),
        )



def test_receipt_binds_destination_root_and_direct_parent(tmp_path):
    state_root, destination, plan_path, _ = write_plan(tmp_path)
    review_plan(plan_path)

    result = apply_state_root_migration_plan(plan_path)
    receipt = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
    anchors = receipt["directory_anchors"]

    assert anchors["destination_root"]["path"] == str(destination)
    assert anchors["destination_root_parent"]["path"] == str(destination.parent)
    destination_info = destination.stat()
    parent_info = destination.parent.stat()
    assert anchors["destination_root"]["device"] == destination_info.st_dev
    assert anchors["destination_root"]["inode"] == destination_info.st_ino
    assert anchors["destination_root_parent"]["device"] == parent_info.st_dev
    assert anchors["destination_root_parent"]["inode"] == parent_info.st_ino
    assert not (state_root / "artifact.txt").exists()


def test_rollback_rejects_replaced_direct_destination_parent(tmp_path):
    state_root, destination, plan_path, _ = write_plan(tmp_path)
    review_plan(plan_path)
    result = apply_state_root_migration_plan(plan_path)

    reviewed_parent = destination.parent.with_name("quarantine-reviewed")
    destination.parent.rename(reviewed_parent)
    destination.parent.mkdir()
    destination.mkdir()
    (destination / "artifact.txt").write_text("decoy\n", encoding="utf-8")

    with pytest.raises(legacy.StateError, match="identity mismatch"):
        rollback_state_root_migration(Path(result["receipt_path"]))

    assert not (state_root / "artifact.txt").exists()
    assert (reviewed_parent / "run-1" / "artifact.txt").exists()
    assert (destination / "artifact.txt").read_text(encoding="utf-8") == "decoy\n"

def test_cli_classifies_read_and_effect_paths():
    read = parser().parse_args(["state-root-artifacts"])
    write = parser().parse_args(
        [
            "state-root-artifacts",
            "--entry",
            "artifact.txt",
            "--destination-root",
            "/tmp/quarantine",
            "--write-plan",
            "/tmp/plan.json",
        ]
    )

    assert _command_mutates(read) is False
    assert _command_mutates(write) is True
