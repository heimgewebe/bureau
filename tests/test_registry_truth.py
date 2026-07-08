from __future__ import annotations

import json
import subprocess
from pathlib import Path

from bureau.cli import main
from bureau.registry_truth import registry_truth_diagnostics


def _machine_metadata(*, task_id: str = "RBV1-T002") -> dict[str, object]:
    return {
        "registry_truth": {
            "schema_version": 1,
            "status": "satisfied",
            "evidence": [
                {
                    "kind": "pull_request",
                    "authority": "github",
                    "number": 861,
                    "task_id": task_id,
                    "source_ref": "heimgewebe/bureau#861",
                    "merge_commit_sha": "a" * 40,
                    "diff_sha256": "b" * 64,
                }
            ],
        },
        "verification": {"task_sha256": "c" * 64, "plan_sha256": "d" * 64},
    }


def _write_task(root: Path, task_id: str, *, state: str, metadata=None, execution=None) -> None:
    task_dir = root / "registry" / "tasks"
    task_dir.mkdir(parents=True, exist_ok=True)
    task = {
        "schema_version": 1,
        "id": task_id,
        "initiative": "RBV1",
        "title": task_id,
        "state": state,
        "goal": "test task",
        "execution": execution or {"mode": "interactive-agent", "policy": "review-before-effect"},
        "claims": [],
        "acceptance": [{"id": "done", "assertion": "done"}],
    }
    if metadata is not None:
        task["metadata"] = metadata
    (task_dir / f"{task_id}.json").write_text(json.dumps(task) + "\n", encoding="utf-8")


def _write_queue(root: Path, *task_ids: str) -> None:
    queue_dir = root / "registry"
    queue_dir.mkdir(parents=True, exist_ok=True)
    (queue_dir / "queue.json").write_text(
        json.dumps({"schema_version": 1, "lanes": {"next": list(task_ids)}}) + "\n",
        encoding="utf-8",
    )


def test_satisfied_task_cannot_remain_non_terminal_or_queued(tmp_path: Path) -> None:
    _write_task(tmp_path, "RBV1-T002", state="planned", metadata=_machine_metadata())
    _write_queue(tmp_path, "RBV1-T002")

    result = registry_truth_diagnostics(tmp_path, probe_baselines=False)

    assert result["healthy"] is False
    issues = {(item["task_id"], item["issue"]) for item in result["errors"]}
    assert ("RBV1-T002", "satisfied_task_non_terminal") in issues
    assert ("RBV1-T002", "satisfied_task_still_queued") in issues


def test_satisfied_verified_task_with_evidence_is_healthy_when_not_queued(tmp_path: Path) -> None:
    _write_task(tmp_path, "RBV1-T002", state="verified", metadata=_machine_metadata())
    _write_queue(tmp_path)

    result = registry_truth_diagnostics(tmp_path, probe_baselines=False)

    assert result["healthy"] is True
    assert result["errors"] == []


def test_verified_task_without_hash_binding_is_error(tmp_path: Path) -> None:
    metadata = {
        "registry_truth": {
            "schema_version": 1,
            "status": "satisfied",
            "evidence": [
                {
                    "kind": "pull_request",
                    "authority": "github",
                    "number": 861,
                    "task_id": "RBV1-T002",
                    "source_ref": "heimgewebe/bureau#861",
                    "merge_commit_sha": "a" * 40,
                }
            ],
        }
    }
    _write_task(tmp_path, "RBV1-T002", state="verified", metadata=metadata)
    _write_queue(tmp_path)

    result = registry_truth_diagnostics(tmp_path, probe_baselines=False)

    assert result["healthy"] is False
    assert any(item["issue"] == "verified_task_without_hash_binding" for item in result["errors"])


def test_verified_task_still_queued_is_error(tmp_path: Path) -> None:
    _write_task(tmp_path, "RBV1-T002", state="verified", metadata=_machine_metadata())
    _write_queue(tmp_path, "RBV1-T002")

    result = registry_truth_diagnostics(tmp_path, probe_baselines=False)

    assert result["healthy"] is False
    assert any(item["issue"] == "terminal_task_still_queued" for item in result["errors"])


def test_ai_summary_alone_is_not_closeout_evidence(tmp_path: Path) -> None:
    metadata = {
        "registry_truth": {
            "schema_version": 1,
            "status": "satisfied",
            "evidence": [
                {"kind": "ai_summary", "summary": "Looks done."},
            ],
        },
        "verification": {"task_sha256": "c" * 64, "plan_sha256": "d" * 64},
    }
    _write_task(tmp_path, "RBV1-T002", state="verified", metadata=metadata)
    _write_queue(tmp_path)

    result = registry_truth_diagnostics(tmp_path, probe_baselines=False)

    assert result["healthy"] is False
    issues = {item["issue"] for item in result["errors"]}
    assert "verified_task_ai_summary_only_evidence" in issues
    assert "verified_task_without_machine_closeout_evidence" in issues


def test_missing_baseline_commit_is_reported_without_completion_claim(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
    missing_commit = "0" * 40
    _write_task(
        tmp_path,
        "RBV1-T003",
        state="planned",
        execution={
            "mode": "interactive-agent",
            "policy": "review-before-effect",
            "working_repository": str(repo),
            "baseline_commit": missing_commit,
        },
    )
    _write_queue(tmp_path, "RBV1-T003")

    result = registry_truth_diagnostics(tmp_path)

    assert result["healthy"] is True
    assert result["errors"] == []
    assert any(
        item["issue"] == "baseline_commit_not_present"
        and item["baseline_commit"] == missing_commit
        and item["baseline_status"] == "missing"
        for item in result["warnings"]
    )
    assert "runtime_correctness" in result["does_not_establish"]


def test_missing_verified_baseline_commit_is_hard_freshness_error(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
    missing_commit = "1" * 40
    _write_task(
        tmp_path,
        "RBV1-T004",
        state="verified",
        metadata=_machine_metadata(task_id="RBV1-T004"),
        execution={
            "mode": "interactive-agent",
            "policy": "review-before-effect",
            "working_repository": str(repo),
            "baseline_commit": missing_commit,
        },
    )
    _write_queue(tmp_path)

    result = registry_truth_diagnostics(tmp_path)

    assert result["healthy"] is False
    assert any(
        item["issue"] == "baseline_commit_not_present"
        and item["severity"] == "error"
        and item["task_id"] == "RBV1-T004"
        for item in result["errors"]
    )


def test_registry_truth_cli_bypasses_eager_registry_validation(tmp_path: Path, capsys) -> None:
    metadata = _machine_metadata()
    _write_task(tmp_path, "RBV1-T002", state="verified", metadata=metadata)
    _write_queue(tmp_path)

    rc = main(["--root", str(tmp_path), "registry-truth", "--no-baseline-probe"])

    assert rc == 0
    assert "healthy: True" in capsys.readouterr().out
