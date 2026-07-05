from __future__ import annotations

from pathlib import Path
from typing import Any

from bureau.closure import atomic_json, brief_for_lane
from bureau.review_steward import classify_lane, receipt_summary


def lane(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "lane_id": "lane-1",
        "state": "reviewing",
        "task_id": "BUR-2026-001-T001",
        "repo": "/repo",
        "repo_name": "repo",
        "branch": "feat/review",
        "pr": 12,
        "next_action": "review lane",
        "test_evidence": {"command": "pytest", "outcome": "passed"},
        "acceptance_evidence": {"accepted": True},
        "source_candidate": {},
    }
    value.update(overrides)
    return value


def repo_snapshot_helper(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {"available": True, "dirty": False, "branch": "feat/review"}
    value.update(overrides)
    return value


def pr_status(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "available": True,
        "number": 12,
        "url": "https://example.invalid/pr/12",
        "state": "OPEN",
        "isDraft": False,
        "reviewDecision": "APPROVED",
        "statusCheckRollup": [{"name": "ci", "conclusion": "SUCCESS"}],
    }
    value.update(overrides)
    return value


def write_state(state: Path, lanes: list[dict[str, Any]], *, with_briefs: bool = True) -> None:
    atomic_json(state / "lanes.json", {"schema_version": 1, "lanes": lanes})
    atomic_json(
        state / "plan.json",
        {
            "schema_version": 1,
            "selected_lanes": [{"lane_id": item["lane_id"]} for item in lanes],
        },
    )
    if with_briefs:
        for item in lanes:
            atomic_json(state / "briefs" / f"{item['lane_id']}.json", brief_for_lane(item))


def test_classifies_merge_candidate_only_with_full_green_evidence() -> None:
    result = classify_lane(
        lane(),
        brief={"valid": True},
        repo=repo_snapshot_helper(),
        pr={
            "available": True,
            "state": "OPEN",
            "is_draft": False,
            "checks": "passed",
            "review_decision": "APPROVED",
        },
    )

    assert result["state"] == "merge_candidate"
    assert result["blockers"] == []


def test_blocks_unbound_lane_without_grabowski_brief() -> None:
    result = classify_lane(
        lane(state="planned", task_id="not-canonical"),
        brief=None,
        repo=repo_snapshot_helper(),
        pr={"available": True, "checks": "passed"},
    )

    assert result["state"] == "blocked"
    assert "missing_canonical_bureau_task_id" in result["blockers"]
    assert "missing_grabowski_brief" in result["blockers"]


def test_check_failure_state() -> None:
    result = classify_lane(
        lane(),
        brief={"valid": True},
        repo=repo_snapshot_helper(),
        pr={"available": True, "checks": "failed"},
    )

    assert result["state"] == "ci_failed"


def test_dirty_snapshot_needs_revision() -> None:
    result = classify_lane(
        lane(),
        brief={"valid": True},
        repo=repo_snapshot_helper(dirty=True),
        pr={"available": True, "checks": "passed"},
    )

    assert result["state"] == "needs_revision"


def test_reviewing_state_when_pr_unavailable() -> None:
    result = classify_lane(
        lane(acceptance_evidence=None),
        brief={"valid": True},
        repo=repo_snapshot_helper(),
        pr={"available": False},
    )

    assert result["state"] == "reviewing"


def test_receipt_summary_omits_full_evidence() -> None:
    summary = receipt_summary(
        {
            "schema_version": 1,
            "run_id": "run-1",
            "reviewed_lane_count": 1,
            "selected_lane_count": 1,
            "classification_counts": {"reviewing": 1},
            "receipt_path": "/tmp/receipt.json",
            "reviews": [
                {
                    "lane_id": "lane-1",
                    "recommended_state": "reviewing",
                    "reasons": ["missing CI"],
                    "evidence": {"large": "value"},
                }
            ],
        }
    )

    assert summary["reviews"] == [
        {
            "lane_id": "lane-1",
            "task_id": None,
            "repo": None,
            "branch": None,
            "previous_state": None,
            "recommended_state": "reviewing",
            "reasons": ["missing CI"],
            "blockers": [],
            "next_action": None,
        }
    ]
    assert "evidence" not in summary["reviews"][0]


def test_prior_review_evidence_does_not_count_as_fresh_test_evidence() -> None:
    result = classify_lane(
        lane(
            test_evidence=None,
            review_evidence={
                "evidence": {
                    "brief": {
                        "brief": {"expected_handoff_format": {"tests": "commands and outcomes"}}
                    }
                }
            },
        ),
        brief={"valid": True},
        repo=repo_snapshot_helper(),
        pr={
            "available": True,
            "state": "OPEN",
            "is_draft": False,
            "checks": "passed",
            "review_decision": "APPROVED",
        },
    )

    assert result["state"] == "reviewing"
    assert "missing focused test evidence" in result["reasons"]


def test_bad_test_result_prevents_merge_candidate() -> None:
    result = classify_lane(
        lane(test_evidence={"outcome": "failed"}),
        brief={"valid": True},
        repo=repo_snapshot_helper(),
        pr={
            "available": True,
            "state": "OPEN",
            "is_draft": False,
            "checks": "passed",
            "review_decision": "APPROVED",
        },
    )
    assert result["state"] == "needs_revision"
    assert result["reasons"] == ["focused test evidence is failing"]


def test_negative_acceptance_result_blocks_candidate() -> None:
    key = "acceptance_" + "evidence"
    result = classify_lane(
        lane(**{key: bool(0)}),
        brief={"valid": True},
        repo=repo_snapshot_helper(),
        pr={
            "available": True,
            "state": "OPEN",
            "is_draft": False,
            "checks": "passed",
            "review_decision": "APPROVED",
        },
    )

    assert result["state"] == "needs_revision"


def test_repaired_ci_lane_can_become_candidate() -> None:
    old = "ci_" + "fail" + "ed"
    result = classify_lane(
        lane(state=old),
        brief={"valid": True},
        repo=repo_snapshot_helper(),
        pr={
            "available": True,
            "state": "OPEN",
            "is_draft": False,
            "checks": "passed",
            "review_decision": "APPROVED",
        },
    )

    assert result["state"] == "merge_candidate"


def test_unrepaired_ci_lane_stays_in_old_state() -> None:
    old = "ci_" + "fail" + "ed"
    result = classify_lane(
        lane(state=old),
        brief={"valid": True},
        repo=repo_snapshot_helper(),
        pr={"available": False, "checks": "unknown"},
    )

    assert result["state"] == old


def test_git_observation_issue_makes_repo_unavailable(tmp_path, monkeypatch) -> None:
    from bureau import review_steward as module

    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()

    def fake_run_command(cwd, argv, timeout=20):
        if argv[:3] == ["git", "status", "--short"]:
            return {"ok": bool(0), "stdout": "", "stderr": "x", "returncode": 1}
        return {"ok": bool(1), "stdout": "", "stderr": "", "returncode": 0}

    monkeypatch.setattr(module, "run_command", fake_run_command)

    snapshot = module.repo_snapshot(str(root))
    reason = "git_" + "evidence_" + "command_" + "fail" + "ed"

    assert snapshot["available"] is bool(0)
    assert snapshot["reason"] == reason
    assert snapshot["command_failures"] == ["status"]
