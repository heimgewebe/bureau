from __future__ import annotations

import subprocess
from pathlib import Path

from bureau.closure import (
    RepositorySource,
    github_repo_slug_from_remote_url,
    inventory_existing_work,
    merge_lanes,
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
    monkeypatch.setattr("bureau.closure.list_open_pull_requests", lambda root: [open_pr()])

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
        "bureau.closure.list_open_pull_requests",
        lambda root: [open_pr(53, branch="automation/remote-only", draft=True)],
    )

    inventory = inventory_existing_work([RepositorySource("repo", repo, "repo:repo")])
    candidate = next(item for item in inventory["candidates"] if item["kind"] == "open_pull_request")

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
