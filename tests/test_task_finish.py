from __future__ import annotations

import json
from pathlib import Path

from bureau.task_finish import apply_ready, scan

HEAD_SHA = "1" * 40
MERGE_SHA = "2" * 40


def write_task(root: Path, task: dict) -> None:
    path = root / "registry" / "tasks" / f"{task['id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(task), encoding="utf-8")


def write_evidence(root: Path, evidence: dict) -> Path:
    path = root / "heimgewebe__example__7.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    return path


def merged_evidence() -> dict:
    return {
        "state": "MERGED",
        "mergedAt": "2026-07-05T15:00:00Z",
        "headRefOid": HEAD_SHA,
        "mergeCommit": {"oid": MERGE_SHA},
    }


def test_scan_reports_ready_receipt_for_merged_bound_task(tmp_path: Path) -> None:
    write_task(
        tmp_path,
        {
            "id": "DEMO-T001",
            "state": "ready",
            "metadata": {
                "pr_completion": {"repo": "heimgewebe/example", "number": 7, "head_sha": HEAD_SHA}
            },
        },
    )
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    write_evidence(evidence_dir, merged_evidence())
    findings = scan(tmp_path, evidence_dir)
    assert findings[0]["ready"] is True
    assert findings[0]["blockers"] == []


def test_scan_blocks_unmerged_evidence(tmp_path: Path) -> None:
    write_task(
        tmp_path,
        {
            "id": "DEMO-T002",
            "state": "ready",
            "metadata": {"pr_completion": {"repo": "heimgewebe/example", "number": 7}},
        },
    )
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    write_evidence(evidence_dir, {"state": "OPEN", "headRefOid": HEAD_SHA})
    findings = scan(tmp_path, evidence_dir)
    assert findings[0]["ready"] is False
    assert findings[0]["blockers"] == ["pull request is not merged"]


def test_scan_blocks_head_sha_mismatch(tmp_path: Path) -> None:
    write_task(
        tmp_path,
        {
            "id": "DEMO-T003",
            "state": "ready",
            "metadata": {
                "pr_completion": {"repo": "heimgewebe/example", "number": 7, "head_sha": "3" * 40}
            },
        },
    )
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    write_evidence(evidence_dir, merged_evidence())
    findings = scan(tmp_path, evidence_dir)
    assert findings[0]["ready"] is False
    assert findings[0]["blockers"] == ["pull request head sha does not match"]


def test_apply_ready_verifies_only_auto_verify_tasks(tmp_path: Path) -> None:
    write_task(
        tmp_path,
        {
            "id": "DEMO-T004",
            "state": "ready",
            "metadata": {
                "pr_completion": {
                    "repo": "heimgewebe/example",
                    "number": 7,
                    "head_sha": HEAD_SHA,
                    "auto_verify": True,
                }
            },
        },
    )
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    write_evidence(evidence_dir, merged_evidence())
    findings = scan(tmp_path, evidence_dir)
    changed = apply_ready(tmp_path, findings, "2026-07-05T16:00:00Z")
    assert changed == ["registry/tasks/DEMO-T004.json"]
    task = json.loads((tmp_path / changed[0]).read_text(encoding="utf-8"))
    assert task["state"] == "verified"
    assert task["metadata"]["verification"]["pr_completion"]["kind"]


def test_apply_ready_leaves_non_auto_verify_task_unchanged(tmp_path: Path) -> None:
    write_task(
        tmp_path,
        {
            "id": "DEMO-T005",
            "state": "ready",
            "metadata": {
                "pr_completion": {"repo": "heimgewebe/example", "number": 7, "head_sha": HEAD_SHA}
            },
        },
    )
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    write_evidence(evidence_dir, merged_evidence())
    findings = scan(tmp_path, evidence_dir)
    changed = apply_ready(tmp_path, findings, "2026-07-05T16:00:00Z")
    assert changed == []


def test_scan_receipt_contains_typed_source_hashes(tmp_path: Path) -> None:
    write_task(
        tmp_path,
        {
            "id": "DEMO-T006",
            "state": "ready",
            "metadata": {
                "pr_completion": {"repo": "heimgewebe/example", "number": 7, "head_sha": HEAD_SHA}
            },
        },
    )
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    write_evidence(evidence_dir, merged_evidence())

    receipt = scan(tmp_path, evidence_dir)[0]["receipt"]

    assert receipt["schema_version"] == 2
    assert receipt["receipt_id"] == f"pr-completion:heimgewebe/example#7:{HEAD_SHA}"
    assert len(receipt["receipt_sha256"]) == 64
    assert receipt["evidence"]["source"] == "github_pull_request"
    assert receipt["evidence"]["source_ref"] == (
        f"github-pr:heimgewebe/example#7@{HEAD_SHA}:{MERGE_SHA}"
    )
    assert len(receipt["evidence"]["evidence_sha256"]) == 64


def test_scan_blocks_ai_or_prose_only_evidence(tmp_path: Path) -> None:
    write_task(
        tmp_path,
        {
            "id": "DEMO-T007",
            "state": "ready",
            "metadata": {"pr_completion": {"repo": "heimgewebe/example", "number": 7}},
        },
    )
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    write_evidence(evidence_dir, {"ai_summary": "looks merged"})

    finding = scan(tmp_path, evidence_dir)[0]

    assert finding["ready"] is False
    assert finding["blockers"] == ["AI or prose-only evidence is insufficient"]


def test_apply_ready_writes_top_level_evidence_fields(tmp_path: Path) -> None:
    write_task(
        tmp_path,
        {
            "id": "DEMO-T008",
            "state": "ready",
            "metadata": {
                "pr_completion": {
                    "repo": "heimgewebe/example",
                    "number": 7,
                    "head_sha": HEAD_SHA,
                    "auto_verify": True,
                }
            },
        },
    )
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    write_evidence(evidence_dir, merged_evidence())

    changed = apply_ready(tmp_path, scan(tmp_path, evidence_dir), "2026-07-05T16:00:00Z")

    task = json.loads((tmp_path / changed[0]).read_text(encoding="utf-8"))
    verification = task["metadata"]["verification"]
    assert verification["receipt_id"] == f"pr-completion:heimgewebe/example#7:{HEAD_SHA}"
    assert len(verification["receipt_sha256"]) == 64
    assert verification["source"] == "github_pull_request"
    assert verification["source_ref"] == f"github-pr:heimgewebe/example#7@{HEAD_SHA}:{MERGE_SHA}"
    assert len(verification["evidence_sha256"]) == 64
