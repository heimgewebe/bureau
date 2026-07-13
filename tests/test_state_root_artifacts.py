from __future__ import annotations

import hashlib
import json
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
    original_rename = Path.rename

    def fail_second(source, target):
        if source.name == "second.txt":
            raise OSError("simulated interruption")
        return original_rename(source, target)

    monkeypatch.setattr(Path, "rename", fail_second)
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
