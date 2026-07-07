from __future__ import annotations

import stat
from pathlib import Path

from bureau.github_observer import (
    BINDING_AMBIGUOUS,
    BINDING_BRANCH_FALLBACK,
    BINDING_BUREAU_RUN,
    BINDING_BUREAU_TASK,
    BINDING_UNMATCHED,
    OBSERVATION_DOES_NOT_ESTABLISH,
    bind_pull_request,
    extract_markers,
    observation_is_stale,
    observe_pull_requests,
    summarize_checks,
)


def pull_request(
    number: int = 7,
    *,
    title: str = "Example",
    body: str = "",
    branch: str = "feat/example",
    review_decision: str = "",
    draft: bool = False,
    rollup: list[dict[str, object]] | None = None,
    state: str = "OPEN",
) -> dict[str, object]:
    return {
        "number": number,
        "title": title,
        "body": body,
        "url": f"https://github.com/heimgewebe/bureau/pull/{number}",
        "state": state,
        "isDraft": draft,
        "headRefName": branch,
        "headRefOid": "a" * 40,
        "baseRefName": "main",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": review_decision,
        "statusCheckRollup": rollup,
        "updatedAt": "2026-07-07T11:00:00Z",
    }


def bind(markers, branch="feat/example", *, tasks=(), runs=None, branches=None):
    return bind_pull_request(
        markers,
        branch,
        known_task_ids=set(tasks),
        runs_by_id=runs or {},
        runs_by_branch=branches or {},
    )


def observe(pull_requests, tmp_path: Path, **kwargs):
    return observe_pull_requests(
        tmp_path,
        repository="heimgewebe/bureau",
        pull_requests=pull_requests,
        state_db=tmp_path / "missing.sqlite3",
        **kwargs,
    )


def fake_gh(tmp_path: Path, script: str) -> str:
    path = tmp_path / "fake-gh"
    path.write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def test_extract_markers_collects_unique_run_and_task_markers() -> None:
    markers = extract_markers(
        "Bureau-Run: BUR-RUN-20260707T000000Z-abc",
        "Bureau-Task: BUR-2026-005-T003\nBureau-Task: BUR-2026-005-T003",
    )
    assert markers == {
        "runs": ["BUR-RUN-20260707T000000Z-abc"],
        "tasks": ["BUR-2026-005-T003"],
    }


def test_bureau_run_marker_binds_with_full_confidence() -> None:
    runs = {"BUR-RUN-1": {"run_id": "BUR-RUN-1", "task_id": "BUR-X-T001"}}
    binding = bind({"runs": ["BUR-RUN-1"], "tasks": []}, runs=runs)
    assert binding["binding"] == BINDING_BUREAU_RUN
    assert binding["confidence"] == 1.0
    assert binding["task_id"] == "BUR-X-T001"
    assert binding["run_id"] == "BUR-RUN-1"


def test_unknown_run_marker_stays_bound_but_noted() -> None:
    binding = bind({"runs": ["BUR-RUN-404"], "tasks": []})
    assert binding["binding"] == BINDING_BUREAU_RUN
    assert binding["task_id"] is None
    assert "run-marker-not-found-in-state-store" in binding["notes"]


def test_bureau_task_marker_binds_with_lower_confidence() -> None:
    binding = bind({"runs": [], "tasks": ["BUR-X-T001"]}, tasks={"BUR-X-T001"})
    assert binding["binding"] == BINDING_BUREAU_TASK
    assert binding["confidence"] == 0.95
    assert binding["task_id"] == "BUR-X-T001"


def test_branch_fallback_yields_weak_confidence() -> None:
    binding = bind(
        {"runs": [], "tasks": []},
        branch="feat/bur-x-t001-observer",
        tasks={"BUR-X-T001", "BUR-X-T002"},
    )
    assert binding["binding"] == BINDING_BRANCH_FALLBACK
    assert binding["confidence"] == 0.55
    assert binding["task_id"] == "BUR-X-T001"
    assert "branch-heuristic-is-weak-evidence" in binding["notes"]


def test_no_match_stays_unmatched() -> None:
    binding = bind({"runs": [], "tasks": []}, branch="feat/unrelated", tasks={"BUR-X-T001"})
    assert binding["binding"] == BINDING_UNMATCHED
    assert binding["confidence"] is None
    assert binding["task_id"] is None


def test_multiple_branch_candidates_fail_closed() -> None:
    binding = bind(
        {"runs": [], "tasks": []},
        branch="feat/bur-x-t001-and-bur-x-t002",
        tasks={"BUR-X-T001", "BUR-X-T002"},
    )
    assert binding["binding"] == BINDING_AMBIGUOUS
    assert binding["ambiguous_reason"] == "multiple-task-candidates-for-branch"
    assert binding["confidence"] is None


def test_conflicting_markers_fail_closed() -> None:
    runs = {"BUR-RUN-1": {"run_id": "BUR-RUN-1", "task_id": "BUR-X-T001"}}
    binding = bind({"runs": ["BUR-RUN-1"], "tasks": ["BUR-X-T002"]}, runs=runs)
    assert binding["binding"] == BINDING_AMBIGUOUS
    assert binding["ambiguous_reason"] == "run-marker-task-marker-conflict"
    assert binding["confidence"] is None

    binding = bind({"runs": [], "tasks": ["BUR-X-T001", "BUR-X-T002"]})
    assert binding["binding"] == BINDING_AMBIGUOUS
    assert binding["ambiguous_reason"] == "multiple-bureau-task-markers"


def test_multiple_open_prs_for_one_task_become_ambiguous(tmp_path: Path) -> None:
    result = observe(
        [
            pull_request(1, body="Bureau-Task: BUR-X-T001"),
            pull_request(2, body="Bureau-Task: BUR-X-T001", branch="feat/other"),
        ],
        tmp_path,
    )
    assert result["healthy"] is True
    for observation in result["pull_requests"]:
        assert observation["binding"] == BINDING_AMBIGUOUS
        assert observation["ambiguous_reason"] == "multiple-open-prs-for-task"
        assert observation["confidence"] is None


def test_gh_missing_binary_is_blocked(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BUREAU_GH_BIN", str(tmp_path / "does-not-exist"))
    result = observe_pull_requests(
        tmp_path, repository="heimgewebe/bureau", state_db=tmp_path / "missing.sqlite3"
    )
    assert result["healthy"] is False
    assert "gh unavailable" in result["blocked_reason"]
    assert result["pull_requests"] == []


def test_gh_error_is_blocked(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BUREAU_GH_BIN", fake_gh(tmp_path, "echo boom >&2; exit 1"))
    result = observe_pull_requests(
        tmp_path, repository="heimgewebe/bureau", state_db=tmp_path / "missing.sqlite3"
    )
    assert result["healthy"] is False
    assert "boom" in result["blocked_reason"]


def test_gh_invalid_json_is_blocked(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BUREAU_GH_BIN", fake_gh(tmp_path, "echo not-json"))
    result = observe_pull_requests(
        tmp_path, repository="heimgewebe/bureau", state_db=tmp_path / "missing.sqlite3"
    )
    assert result["healthy"] is False
    assert "invalid JSON" in result["blocked_reason"]


def test_unresolvable_repository_is_blocked(tmp_path: Path) -> None:
    result = observe_pull_requests(tmp_path, state_db=tmp_path / "missing.sqlite3")
    assert result["healthy"] is False
    assert result["repository"] is None


def test_checks_summaries() -> None:
    assert summarize_checks(None)["summary"] == "ci_unknown"
    assert summarize_checks([])["summary"] == "ci_unknown"
    pending = [{"name": "ci", "status": "IN_PROGRESS", "conclusion": None}]
    assert summarize_checks(pending)["summary"] == "ci_pending"
    passed = [{"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}]
    assert summarize_checks(passed)["summary"] == "ci_passed"
    failed = [*passed, {"name": "lint", "status": "COMPLETED", "conclusion": "FAILURE"}]
    assert summarize_checks(failed)["summary"] == "ci_failed"
    mixed = [*pending, {"name": "lint", "conclusion": "FAILURE"}]
    assert summarize_checks(mixed)["summary"] == "ci_failed"
    status_context = [{"context": "external", "state": "PENDING"}]
    assert summarize_checks(status_context)["summary"] == "ci_pending"


def test_ci_pass_does_not_establish_correctness(tmp_path: Path) -> None:
    result = observe(
        [
            pull_request(
                3,
                body="Bureau-Task: BUR-X-T001",
                rollup=[{"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}],
            )
        ],
        tmp_path,
    )
    observation = result["pull_requests"][0]
    assert observation["checks"]["summary"] == "ci_passed"
    assert "ci_sufficiency" in result["does_not_establish"]
    assert "task_completion" in result["does_not_establish"]
    assert set(OBSERVATION_DOES_NOT_ESTABLISH) <= set(result["does_not_establish"])


def test_changes_requested_blocks_review(tmp_path: Path) -> None:
    result = observe(
        [pull_request(4, review_decision="CHANGES_REQUESTED", draft=True)], tmp_path
    )
    observation = result["pull_requests"][0]
    assert observation["review_blocked"] is True
    assert observation["is_draft"] is True
    result = observe([pull_request(5, review_decision="APPROVED")], tmp_path)
    assert result["pull_requests"][0]["review_blocked"] is False


def test_stale_observation_is_detectable() -> None:
    fresh = {"observed_at": "2026-07-07T12:00:00Z"}
    assert (
        observation_is_stale(fresh, max_age_seconds=600, now="2026-07-07T12:05:00Z") is False
    )
    assert (
        observation_is_stale(fresh, max_age_seconds=600, now="2026-07-07T13:00:00Z") is True
    )
    assert observation_is_stale({}, max_age_seconds=600, now="2026-07-07T12:00:00Z") is True
    assert (
        observation_is_stale(
            {"observed_at": "garbage"}, max_age_seconds=600, now="2026-07-07T12:00:00Z"
        )
        is True
    )


def test_state_store_unavailable_is_noted_not_fatal(tmp_path: Path) -> None:
    result = observe([pull_request(6, body="Bureau-Task: BUR-X-T001")], tmp_path)
    assert result["healthy"] is True
    assert any("state-store-unavailable" in note for note in result["notes"])
