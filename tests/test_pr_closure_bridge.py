from __future__ import annotations

import json
from pathlib import Path

from bureau.pr_closure_bridge import apply_auto_verify, pr_binding, scan_tasks

HEAD_SHA = "1" * 40
MERGE_SHA = "2" * 40
OBSERVED_AT = "2026-07-05T15:00:00Z"


def write_task(root: Path, task: dict) -> Path:
    path = root / "registry" / "tasks" / f"{task['id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(task, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def read_task(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def merged_pr() -> dict:
    return {
        "number": 7,
        "state": "MERGED",
        "mergedAt": "2026-07-05T14:00:00Z",
        "mergeCommit": {"oid": MERGE_SHA},
        "headRefOid": HEAD_SHA,
        "url": "https://github.com/heimgewebe/example/pull/7",
    }


def test_pr_binding_accepts_canonical_metadata() -> None:
    task = {
        "metadata": {
            "pr_closure": {
                "repo": "heimgewebe/example",
                "number": 7,
                "expected_head_sha": HEAD_SHA,
                "auto_verify": True,
                "non_claims": ["does not prove deployment"],
            }
        }
    }

    binding = pr_binding(task)

    assert binding is not None
    assert binding.repo == "heimgewebe/example"
    assert binding.number == 7
    assert binding.expected_head_sha == HEAD_SHA
    assert binding.auto_verify is True
    assert binding.non_claims == ("does not prove deployment",)


def test_scan_emits_close_ready_receipt_for_merged_bound_pr(tmp_path: Path) -> None:
    task_path = write_task(
        tmp_path,
        {
            "schema_version": 1,
            "id": "DEMO-T001",
            "title": "Demo",
            "state": "ready",
            "metadata": {
                "pr_closure": {
                    "repo": "heimgewebe/example",
                    "number": 7,
                    "expected_head_sha": HEAD_SHA,
                    "non_claims": ["does not prove deployment"],
                }
            },
        },
    )

    findings = scan_tasks(tmp_path, lambda binding: merged_pr(), observed_at=OBSERVED_AT)

    assert task_path.exists()
    assert len(findings) == 1
    finding = findings[0]
    assert finding["close_ready"] is True
    assert finding["blockers"] == []
    assert finding["receipt"]["task_id"] == "DEMO-T001"
    evidence = finding["receipt"]["evidence"]
    assert evidence["repo"] == "heimgewebe/example"
    assert evidence["pr_number"] == 7
    assert evidence["head_sha"] == HEAD_SHA
    assert evidence["merge_commit"] == MERGE_SHA
    assert evidence["non_claims"] == ["does not prove deployment"]


def test_scan_blocks_when_head_sha_does_not_match(tmp_path: Path) -> None:
    write_task(
        tmp_path,
        {
            "schema_version": 1,
            "id": "DEMO-T002",
            "title": "Demo",
            "state": "ready",
            "metadata": {
                "pr_closure": {
                    "repo": "heimgewebe/example",
                    "number": 7,
                    "expected_head_sha": "3" * 40,
                }
            },
        },
    )

    findings = scan_tasks(tmp_path, lambda binding: merged_pr(), observed_at=OBSERVED_AT)

    assert findings[0]["close_ready"] is False
    assert findings[0]["blockers"] == ["pull request head sha does not match binding"]


def test_apply_auto_verify_only_when_explicitly_enabled(tmp_path: Path) -> None:
    task_path = write_task(
        tmp_path,
        {
            "schema_version": 1,
            "id": "DEMO-T003",
            "title": "Demo",
            "state": "ready",
            "metadata": {
                "pr_closure": {
                    "repo": "heimgewebe/example",
                    "number": 7,
                    "expected_head_sha": HEAD_SHA,
                    "auto_verify": True,
                }
            },
        },
    )
    findings = scan_tasks(tmp_path, lambda binding: merged_pr(), observed_at=OBSERVED_AT)

    changed = apply_auto_verify(tmp_path, findings, observed_at=OBSERVED_AT)

    assert changed == ["registry/tasks/DEMO-T003.json"]
    task = read_task(task_path)
    assert task["state"] == "verified"
    closure = task["metadata"]["verification"]["pr_closure"]
    assert closure["kind"] == "bureau.pr_closure_receipt"
    assert closure["evidence"]["merge_commit"] == MERGE_SHA


def test_apply_auto_verify_does_not_close_without_auto_verify(tmp_path: Path) -> None:
    task_path = write_task(
        tmp_path,
        {
            "schema_version": 1,
            "id": "DEMO-T004",
            "title": "Demo",
            "state": "ready",
            "metadata": {
                "pr_closure": {
                    "repo": "heimgewebe/example",
                    "number": 7,
                    "expected_head_sha": HEAD_SHA,
                }
            },
        },
    )
    findings = scan_tasks(tmp_path, lambda binding: merged_pr(), observed_at=OBSERVED_AT)

    changed = apply_auto_verify(tmp_path, findings, observed_at=OBSERVED_AT)

    assert changed == []
    assert read_task(task_path)["state"] == "ready"


def test_scan_ignores_unbound_tasks(tmp_path: Path) -> None:
    write_task(
        tmp_path,
        {"schema_version": 1, "id": "DEMO-T005", "title": "Demo", "state": "ready"},
    )

    assert scan_tasks(tmp_path, lambda binding: merged_pr(), observed_at=OBSERVED_AT) == []
