from __future__ import annotations

import subprocess
from pathlib import Path

from bureau.closure import (
    OpenPullRequestObservation,
    RepositorySource,
    candidate_fingerprint,
    github_repo_slug_from_remote_url,
    inventory_existing_work,
    merge_lanes,
    observe_open_pull_requests,
    open_pull_request_lane_state,
)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, text=True, capture_output=True)


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test User"], check=True)
    (repo / "README.md").write_text("main\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "initial")
    git(repo, "switch", "-c", "feat/example")
    (repo / "feature.txt").write_text("feature\n", encoding="utf-8")
    git(repo, "add", "feature.txt")
    git(repo, "commit", "-m", "feature")
    git(repo, "switch", "main")
    return repo


def open_pr(
    number: int = 52,
    *,
    branch: str = "feat/example",
    merge_state: str = "CLEAN",
    review_decision: str = "APPROVED",
    draft: bool = False,
) -> dict[str, object]:
    return {
        "pr": number,
        "pr_title": "Example PR",
        "pr_url": f"https://github.com/heimgewebe/bureau/pull/{number}",
        "branch": branch,
        "head_ref_name": branch,
        "observed_github_state": {
            "state": "open",
            "merge_state_status": merge_state,
            "review_decision": review_decision,
            "is_draft": draft,
            "source": "test",
        },
    }


def test_github_repo_slug_from_remote_url_accepts_ssh_and_https() -> None:
    assert (
        github_repo_slug_from_remote_url("git@github.com:heimgewebe/bureau.git")
        == "heimgewebe/bureau"
    )
    assert (
        github_repo_slug_from_remote_url("https://github.com/heimgewebe/bureau.git")
        == "heimgewebe/bureau"
    )
    assert (
        github_repo_slug_from_remote_url("ssh://git@github.com/heimgewebe/bureau.git")
        == "heimgewebe/bureau"
    )
    assert github_repo_slug_from_remote_url("https://example.invalid/heimgewebe/bureau.git") is None


def test_inventory_attaches_open_pr_to_existing_branch(tmp_path: Path, monkeypatch) -> None:
    repo = make_repo(tmp_path)
    monkeypatch.setattr(
        "bureau.closure.observe_open_pull_requests",
        lambda root: OpenPullRequestObservation([open_pr()]),
    )

    inventory = inventory_existing_work([RepositorySource("repo", repo, "repo:repo")])
    candidate = next(item for item in inventory["candidates"] if item["kind"] == "branch")

    assert candidate["branch"] == "feat/example"
    assert candidate["pr"] == 52
    assert candidate["pr_title"] == "Example PR"
    assert candidate["pr_url"] == "https://github.com/heimgewebe/bureau/pull/52"
    assert candidate["observed_github_state"]["merge_state_status"] == "CLEAN"
    assert candidate["proposed_state"] == "merge_candidate"

    lanes = merge_lanes(inventory)
    assert lanes["lanes"][0]["pr"] == 52
    assert lanes["lanes"][0]["observed_github_state"]["review_decision"] == "APPROVED"


def test_inventory_records_open_pr_without_local_branch(tmp_path: Path, monkeypatch) -> None:
    repo = make_repo(tmp_path)
    monkeypatch.setattr(
        "bureau.closure.observe_open_pull_requests",
        lambda root: OpenPullRequestObservation(
            [open_pr(53, branch="automation/remote-only", draft=True)]
        ),
    )

    inventory = inventory_existing_work([RepositorySource("repo", repo, "repo:repo")])
    candidate = next(
        item for item in inventory["candidates"] if item["kind"] == "open_pull_request"
    )

    assert candidate["branch"] == "automation/remote-only"
    assert candidate["pr"] == 53
    assert candidate["proposed_state"] == "reviewing"
    assert candidate["observed_github_state"]["is_draft"] is True


def test_clean_open_pr_without_approval_is_not_merge_candidate() -> None:
    state, _finishability, _action = open_pull_request_lane_state(
        open_pr(merge_state="CLEAN", review_decision="", draft=False)
    )
    assert state == "reviewing"


def test_clean_approved_open_pr_is_merge_candidate() -> None:
    state, _finishability, action = open_pull_request_lane_state(
        open_pr(merge_state="CLEAN", review_decision="APPROVED", draft=False)
    )
    assert state == "merge_candidate"
    assert "merge gatekeeper" in action


def test_observe_open_pull_requests_records_adapter_failure(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr("bureau.closure.github_repo_slug", lambda root: "heimgewebe/bureau")

    def fail(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("gh missing")

    monkeypatch.setattr("bureau.closure.subprocess.run", fail)

    observation = observe_open_pull_requests(repo)

    assert observation.pull_requests == []
    assert observation.blocked is not None
    assert observation.blocked["state"] == "blocked"
    assert observation.blocked["reason"] == "adapter_unavailable"


def test_blocked_github_observation_preserves_existing_pr_lane() -> None:
    candidate = {
        "kind": "branch",
        "repo": "/tmp/repo",
        "repo_name": "repo",
        "branch": "feat/example",
        "pr": 54,
        "pr_title": "Example PR",
        "pr_url": "https://github.com/heimgewebe/bureau/pull/54",
        "observed_github_state": {"state": "open", "source": "test"},
        "proposed_state": "merge_candidate",
        "finishability": 0.9,
    }
    candidate["fingerprint"] = candidate_fingerprint(candidate)
    existing = merge_lanes({"candidates": [candidate]})
    existing["lanes"][0]["task_id"] = "BUR-2026-001-T999"
    next_candidate = {
        "kind": "branch",
        "repo": "/tmp/repo",
        "repo_name": "repo",
        "branch": "feat/example",
        "proposed_state": "planned",
        "finishability": 0.55,
    }
    next_candidate["fingerprint"] = candidate_fingerprint(next_candidate)

    lanes = merge_lanes(
        {
            "candidates": [next_candidate],
            "github_observations": [
                {
                    "repo": "/tmp/repo",
                    "observed_github_state": {
                        "state": "blocked",
                        "source": "gh pr list --state open",
                        "reason": "adapter_failed",
                    },
                }
            ],
        },
        existing,
    )

    lane = lanes["lanes"][0]
    assert lane["state"] == "blocked"
    assert lane["pr"] == 54
    assert lane["task_id"] == "BUR-2026-001-T999"
    assert lane["observed_github_state"]["state"] == "open"
    assert lane["metadata"]["github_observation_blocked"]["reason"] == "adapter_failed"


def test_remote_only_pr_lane_migrates_when_branch_is_checked_out(
    tmp_path: Path, monkeypatch
) -> None:
    repo = make_repo(tmp_path)
    source = RepositorySource("repo", repo, "repo:repo")
    monkeypatch.setattr(
        "bureau.closure.observe_open_pull_requests",
        lambda root: OpenPullRequestObservation(
            [open_pr(53, branch="automation/remote-only")]
        ),
    )
    existing = merge_lanes(inventory_existing_work([source]))
    existing["lanes"][0]["task_id"] = "BUR-2026-001-T053"

    git(repo, "switch", "-c", "automation/remote-only")
    (repo / "remote.txt").write_text("remote\n", encoding="utf-8")
    git(repo, "add", "remote.txt")
    git(repo, "commit", "-m", "remote branch")
    git(repo, "switch", "main")

    lanes = merge_lanes(inventory_existing_work([source]), existing)

    assert len(lanes["lanes"]) == 2
    remote_lane = next(item for item in lanes["lanes"] if item["pr"] == 53)
    assert remote_lane["branch"] == "automation/remote-only"
    assert remote_lane["task_id"] == "BUR-2026-001-T053"
    assert remote_lane["metadata"]["migrated_from_fingerprint"]
    assert all(
        item["next_action"] != "source candidate disappeared; inspect before continuing"
        for item in lanes["lanes"]
    )


def test_cross_repository_pr_is_not_attached_to_same_named_local_branch(
    tmp_path: Path, monkeypatch
) -> None:
    repo = make_repo(tmp_path)
    fork_pr = open_pr()
    fork_pr["is_cross_repository"] = True
    fork_pr["observed_github_state"]["is_cross_repository"] = True
    monkeypatch.setattr(
        "bureau.closure.observe_open_pull_requests",
        lambda root: OpenPullRequestObservation([fork_pr]),
    )

    inventory = inventory_existing_work([RepositorySource("repo", repo, "repo:repo")])

    branch_candidate = next(item for item in inventory["candidates"] if item["kind"] == "branch")
    pr_candidate = next(
        item for item in inventory["candidates"] if item["kind"] == "open_pull_request"
    )
    assert branch_candidate.get("pr") is None
    assert pr_candidate["pr"] == 52
