from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

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
    (bundle / "self-review.json").write_text(
        json.dumps(review, indent=2) + "\n", encoding="utf-8"
    )


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
    (plans / "test-plan.json").write_text(
        json.dumps(plan, indent=2) + "\n", encoding="utf-8"
    )


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
    state_root, destination, plan_path, _ = write_plan(
        tmp_path, names=("first.txt", "second.txt")
    )
    review_plan(plan_path)

    result = apply_state_root_migration_plan(plan_path)

    assert result["idempotent_rerun"] is False
    assert result["receipt_sha256"]
    assert not (state_root / "first.txt").exists()
    assert (destination / "first.txt").read_text(encoding="utf-8") == (
        "content for first.txt\n"
    )
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
    state_root, destination, plan_path, _ = write_plan(
        tmp_path, names=("first.txt", "second.txt")
    )
    review_plan(plan_path)
    original_rename_at = state_root_artifacts._rename_at

    def fail_second(source_descriptor, source_name, destination_descriptor, destination_name):
        if source_name == "second.txt":
            raise OSError("simulated interruption")
        return original_rename_at(
            source_descriptor,
            source_name,
            destination_descriptor,
            destination_name,
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
    state_root, destination, plan_path, _ = write_plan(
        tmp_path, names=("first.txt", "second.txt")
    )
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
    state_root, destination, plan_path, _ = write_plan(
        tmp_path, names=("first.txt", "second.txt")
    )
    review_plan(plan_path)
    result = apply_state_root_migration_plan(plan_path)
    original_rename_at = state_root_artifacts._rename_at

    def fail_first_restore(
        source_descriptor,
        source_name,
        destination_descriptor,
        destination_name,
    ):
        if source_name == "first.txt":
            raise OSError("simulated rollback interruption")
        return original_rename_at(
            source_descriptor,
            source_name,
            destination_descriptor,
            destination_name,
        )

    monkeypatch.setattr(state_root_artifacts, "_rename_at", fail_first_restore)
    with pytest.raises(OSError, match="simulated rollback interruption"):
        rollback_state_root_migration(Path(result["receipt_path"]))

    assert not (state_root / "first.txt").exists()
    assert not (state_root / "second.txt").exists()
    assert (destination / "first.txt").exists()
    assert (destination / "second.txt").exists()


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



def write_bound_plan(
    tmp_path: Path,
    *,
    state_root: Path,
    destination_root: Path,
    reference_root: Path,
    names=("artifact.txt",),
):
    state_root.mkdir(parents=True)
    for name in names:
        (state_root / name).write_text(f"content for {name}\n", encoding="utf-8")
    (reference_root / "registry").mkdir(parents=True)
    (reference_root / "docs").mkdir()
    plan_path = tmp_path / "plans" / "bound-migration.json"
    result = write_state_root_migration_plan(
        state_root,
        list(names),
        destination_root,
        plan_path,
        reference_root=reference_root,
    )
    review_plan(plan_path)
    return plan_path, result


def test_plan_binds_operational_directory_device_and_inode(tmp_path):
    state_root = tmp_path / "source-anchor" / "state"
    destination = tmp_path / "destination-anchor" / "quarantine" / "run-1"
    destination.parent.parent.mkdir()
    reference_root = tmp_path / "reference-anchor" / "repo"
    _, plan = write_bound_plan(
        tmp_path,
        state_root=state_root,
        destination_root=destination,
        reference_root=reference_root,
    )

    anchors = plan["directory_anchors"]
    assert set(anchors) == {
        "state_root",
        "state_root_parent",
        "destination_base",
        "reference_root",
        "reference_root_parent",
    }
    for anchor in anchors.values():
        assert anchor["path"].startswith("/")
        assert isinstance(anchor["device"], int)
        assert isinstance(anchor["inode"], int) and anchor["inode"] > 0
    assert plan["platform_contract"] == {
        "mutation_mode": "linux-descriptor-relative-v1",
        "no_follow": True,
        "silent_fallback": False,
    }


def test_apply_rejects_replaced_source_ancestor_before_effect(tmp_path):
    source_anchor = tmp_path / "source-anchor"
    state_root = source_anchor / "state"
    destination = tmp_path / "quarantine" / "run-1"
    reference_root = tmp_path / "reference" / "repo"
    plan_path, _ = write_bound_plan(
        tmp_path,
        state_root=state_root,
        destination_root=destination,
        reference_root=reference_root,
    )
    original_anchor = tmp_path / "source-anchor-reviewed"
    source_anchor.rename(original_anchor)
    state_root.mkdir(parents=True)
    (state_root / "artifact.txt").write_text("decoy\n", encoding="utf-8")

    with pytest.raises(legacy.StateError, match="identity mismatch"):
        apply_state_root_migration_plan(plan_path)

    assert (original_anchor / "state" / "artifact.txt").exists()
    assert (state_root / "artifact.txt").read_text(encoding="utf-8") == "decoy\n"
    assert not destination.exists()


def test_apply_rejects_replaced_destination_ancestor_before_effect(tmp_path):
    destination_anchor = tmp_path / "destination-anchor"
    destination_anchor.mkdir()
    state_root = tmp_path / "source" / "state"
    destination = destination_anchor / "quarantine" / "run-1"
    reference_root = tmp_path / "reference" / "repo"
    plan_path, _ = write_bound_plan(
        tmp_path,
        state_root=state_root,
        destination_root=destination,
        reference_root=reference_root,
    )
    reviewed_anchor = tmp_path / "destination-anchor-reviewed"
    destination_anchor.rename(reviewed_anchor)
    destination_anchor.mkdir()

    with pytest.raises(legacy.StateError, match="identity mismatch"):
        apply_state_root_migration_plan(plan_path)

    assert (state_root / "artifact.txt").exists()
    assert not (reviewed_anchor / "quarantine").exists()
    assert not (destination_anchor / "quarantine").exists()


def test_apply_rejects_replaced_reference_ancestor_before_effect(tmp_path):
    reference_anchor = tmp_path / "reference-anchor"
    reference_root = reference_anchor / "repo"
    state_root = tmp_path / "source" / "state"
    destination = tmp_path / "quarantine" / "run-1"
    plan_path, _ = write_bound_plan(
        tmp_path,
        state_root=state_root,
        destination_root=destination,
        reference_root=reference_root,
    )
    reviewed_anchor = tmp_path / "reference-anchor-reviewed"
    reference_anchor.rename(reviewed_anchor)
    (reference_anchor / "repo" / "registry").mkdir(parents=True)
    (reference_anchor / "repo" / "docs").mkdir()

    with pytest.raises(legacy.StateError, match="identity mismatch"):
        apply_state_root_migration_plan(plan_path)

    assert (state_root / "artifact.txt").exists()
    assert not destination.exists()


def test_apply_rejects_symlinked_destination_ancestor_before_effect(tmp_path):
    destination_anchor = tmp_path / "destination-anchor"
    destination_anchor.mkdir()
    state_root = tmp_path / "source" / "state"
    destination = destination_anchor / "quarantine" / "run-1"
    reference_root = tmp_path / "reference" / "repo"
    plan_path, _ = write_bound_plan(
        tmp_path,
        state_root=state_root,
        destination_root=destination,
        reference_root=reference_root,
    )
    reviewed_anchor = tmp_path / "destination-anchor-reviewed"
    destination_anchor.rename(reviewed_anchor)
    destination_anchor.symlink_to(reviewed_anchor, target_is_directory=True)

    with pytest.raises(legacy.StateError, match="symlink or non-directory"):
        apply_state_root_migration_plan(plan_path)

    assert (state_root / "artifact.txt").exists()
    assert not (reviewed_anchor / "quarantine").exists()


def test_concurrent_source_ancestor_replacement_is_compensated(tmp_path, monkeypatch):
    source_anchor = tmp_path / "source-anchor"
    state_root = source_anchor / "state"
    destination = tmp_path / "quarantine" / "run-1"
    reference_root = tmp_path / "reference" / "repo"
    plan_path, _ = write_bound_plan(
        tmp_path,
        state_root=state_root,
        destination_root=destination,
        reference_root=reference_root,
    )
    original_rename_at = state_root_artifacts._rename_at
    reviewed_anchor = tmp_path / "source-anchor-reviewed"
    replaced = False

    def replace_ancestor_then_rename(
        source_descriptor,
        source_name,
        destination_descriptor,
        destination_name,
    ):
        nonlocal replaced
        if not replaced:
            source_anchor.rename(reviewed_anchor)
            state_root.mkdir(parents=True)
            replaced = True
        return original_rename_at(
            source_descriptor,
            source_name,
            destination_descriptor,
            destination_name,
        )

    monkeypatch.setattr(
        state_root_artifacts,
        "_rename_at",
        replace_ancestor_then_rename,
    )
    with pytest.raises(legacy.StateError, match="identity mismatch"):
        apply_state_root_migration_plan(plan_path)

    assert (reviewed_anchor / "state" / "artifact.txt").exists()
    assert not (state_root / "artifact.txt").exists()
    assert not destination.exists()


def test_all_entries_preflight_before_first_descriptor_relative_rename(tmp_path):
    state_root, destination, plan_path, _ = write_plan(
        tmp_path, names=("first.txt", "second.txt")
    )
    review_plan(plan_path)
    (state_root / "second.txt").write_text("drifted\n", encoding="utf-8")

    with pytest.raises(legacy.StateError, match="changed since review"):
        apply_state_root_migration_plan(plan_path)

    assert (state_root / "first.txt").exists()
    assert (state_root / "second.txt").exists()
    assert not destination.exists()


def test_cross_device_boundary_is_fail_closed(monkeypatch):
    source = SimpleNamespace(st_dev=101)
    monkeypatch.setattr(
        state_root_artifacts.os,
        "fstat",
        lambda descriptor: SimpleNamespace(st_dev=202),
    )

    with pytest.raises(legacy.StateError, match="same-filesystem"):
        state_root_artifacts._require_same_filesystem(source, 9)


def test_unsupported_descriptor_platform_refuses_plan_without_effect(tmp_path, monkeypatch):
    state_root = tmp_path / "state"
    state_root.mkdir()
    (state_root / "artifact.txt").write_text("artifact\n", encoding="utf-8")
    reference_root = empty_reference_root(tmp_path)
    destination = tmp_path / "quarantine" / "run-1"
    plan_path = tmp_path / "migration.json"
    monkeypatch.setattr(
        state_root_artifacts,
        "_descriptor_relative_support_error",
        lambda: "synthetic unsupported platform",
    )

    with pytest.raises(legacy.StateError, match="unsupported"):
        write_state_root_migration_plan(
            state_root,
            ["artifact.txt"],
            destination,
            plan_path,
            reference_root=reference_root,
        )

    assert (state_root / "artifact.txt").exists()
    assert not destination.exists()
    assert not plan_path.exists()


def test_rollback_rejects_replaced_destination_ancestor_without_effect(tmp_path):
    destination_anchor = tmp_path / "destination-anchor"
    destination_anchor.mkdir()
    state_root = tmp_path / "source" / "state"
    destination = destination_anchor / "quarantine" / "run-1"
    reference_root = tmp_path / "reference" / "repo"
    plan_path, _ = write_bound_plan(
        tmp_path,
        state_root=state_root,
        destination_root=destination,
        reference_root=reference_root,
    )
    result = apply_state_root_migration_plan(plan_path)
    reviewed_anchor = tmp_path / "destination-anchor-reviewed"
    destination_anchor.rename(reviewed_anchor)
    destination_anchor.mkdir()

    with pytest.raises(legacy.StateError, match="identity mismatch"):
        rollback_state_root_migration(Path(result["receipt_path"]))

    assert not (state_root / "artifact.txt").exists()
    assert (reviewed_anchor / "quarantine" / "run-1" / "artifact.txt").exists()


def test_legacy_receipt_cannot_silently_use_weaker_path_mutation(tmp_path):
    state_root, destination, plan_path, _ = write_plan(tmp_path)
    review_plan(plan_path)
    result = apply_state_root_migration_plan(plan_path)
    receipt_path = Path(result["receipt_path"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["schema_version"] = 1
    receipt.pop("receipt_sha256")
    receipt["receipt_sha256"] = canonical_sha256(receipt)
    receipt_path.write_text(legacy.canonical_json(receipt) + "\n", encoding="utf-8")

    with pytest.raises(legacy.StateError, match="unsupported"):
        rollback_state_root_migration(receipt_path)

    assert not (state_root / "artifact.txt").exists()
    assert (destination / "artifact.txt").exists()



def test_receipt_binds_destination_root_and_direct_parent(tmp_path):
    destination_anchor = tmp_path / "destination-anchor"
    destination_anchor.mkdir()
    state_root = tmp_path / "source" / "state"
    destination = destination_anchor / "quarantine" / "run-1"
    reference_root = tmp_path / "reference" / "repo"
    plan_path, _ = write_bound_plan(
        tmp_path,
        state_root=state_root,
        destination_root=destination,
        reference_root=reference_root,
    )

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


def test_rollback_rejects_replaced_direct_destination_parent(tmp_path):
    destination_anchor = tmp_path / "destination-anchor"
    destination_anchor.mkdir()
    state_root = tmp_path / "source" / "state"
    destination = destination_anchor / "quarantine" / "run-1"
    reference_root = tmp_path / "reference" / "repo"
    plan_path, _ = write_bound_plan(
        tmp_path,
        state_root=state_root,
        destination_root=destination,
        reference_root=reference_root,
    )
    result = apply_state_root_migration_plan(plan_path)

    reviewed_parent = destination_anchor / "quarantine-reviewed"
    destination.parent.rename(reviewed_parent)
    destination.parent.mkdir()
    destination.mkdir()
    (destination / "artifact.txt").write_text("decoy\n", encoding="utf-8")

    with pytest.raises(legacy.StateError, match="identity mismatch"):
        rollback_state_root_migration(Path(result["receipt_path"]))

    assert not (state_root / "artifact.txt").exists()
    assert (reviewed_parent / "run-1" / "artifact.txt").exists()
    assert (destination / "artifact.txt").read_text(encoding="utf-8") == "decoy\n"
