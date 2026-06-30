from __future__ import annotations

from pathlib import Path
from typing import Any

from bureau.closure import atomic_json, brief_for_lane
from bureau.review_steward import classify_lane


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


def repo_snapshot(**overrides: Any) -> dict[str, Any]:
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
        repo=repo_snapshot(),
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

# receipt tests pending


def test_blocks_unbound_lane_without_grabowski_brief() -> None:
    result = classify_lane(
        lane(state="planned", task_id="not-canonical"),
        brief=None,
        repo=repo_snapshot(),
        pr={"available": True, "checks": "passed"},
    )

    assert result["state"] == "blocked"
    assert "missing_canonical_bureau_task_id" in result["blockers"]
    assert "missing_grabowski_brief" in result["blockers"]


def test_check_failure_state() -> None:
    result = classify_lane(
        lane(),
        brief={"valid": True},
        repo=repo_snapshot(),
        pr={"available": True, "checks": "failed"},
    )

    assert result["state"] == "ci_failed"


def test_dirty_snapshot_needs_revision() -> None:
    result = classify_lane(
        lane(),
        brief={"valid": True},
        repo=repo_snapshot(dirty=True),
        pr={"available": True, "checks": "passed"},
    )

    assert result["state"] == "needs_revision"


def test_reviewing_state_when_pr_unavailable() -> None:
    result = classify_lane(
        lane(acceptance_evidence=None),
        brief={"valid": True},
        repo=repo_snapshot(),
        pr={"available": False},
    )

    assert result["state"] == "reviewing"
