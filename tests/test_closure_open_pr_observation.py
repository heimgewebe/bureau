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


def test_changes_requested_open_pr_needs_revision() -> None:
    state, finishability, action = open_pull_request_lane_state(
        open_pr(merge_state="CLEAN", review_decision="CHANGES_REQUESTED", draft=False)
    )
    assert state == "needs_revision"
    assert finishability == 0.35
    assert "requested GitHub pull-request changes" in action


def test_changes_requested_open_pr_takes_precedence_over_draft() -> None:
    state, _finishability, action = open_pull_request_lane_state(
        open_pr(merge_state="CLEAN", review_decision="CHANGES_REQUESTED", draft=True)
    )
    assert state == "needs_revision"
    assert "requested GitHub pull-request changes" in action


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


def test_successful_github_observation_resets_stale_pr_state() -> None:
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
        "next_best_action": "bind branch to canonical task or create planned closure task",
    }
    next_candidate["fingerprint"] = candidate_fingerprint(next_candidate)

    lanes = merge_lanes(
        {
            "candidates": [next_candidate],
            "github_observations": [
                {
                    "repo": "/tmp/repo",
                    "observed_github_state": {
                        "state": "observed",
                        "source": "gh pr list --state open",
                        "open_pull_request_count": 0,
                        "complete": True,
                        "limit": 200,
                    },
                }
            ],
        },
        existing,
    )

    lane = lanes["lanes"][0]
    assert lane["state"] == "planned"
    assert lane["pr"] is None
    assert lane["observed_github_state"] is None
    assert lane["task_id"] == "BUR-2026-001-T999"
    assert lane["metadata"]["github_pr_observation_reset"]["previous_pr"] == 54


def test_capped_github_observation_preserves_previous_pr_context() -> None:
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
                        "state": "observed",
                        "source": "gh pr list --state open",
                        "open_pull_request_count": 200,
                        "complete": False,
                        "limit": 200,
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
    assert lane["metadata"]["github_observation_incomplete"]["complete"] is False
    assert "github_pr_observation_reset" not in lane["metadata"]


def test_pr_alias_is_seen_even_with_exact_branch_match() -> None:
    branch_candidate = {
        "kind": "branch",
        "repo": "/tmp/repo",
        "repo_name": "repo",
        "branch": "feat/example",
        "proposed_state": "planned",
        "finishability": 0.55,
    }
    branch_candidate["fingerprint"] = candidate_fingerprint(branch_candidate)
    pr_candidate = {
        "kind": "open_pull_request",
        "repo": "/tmp/repo",
        "repo_name": "repo",
        "branch": "feat/example",
        "pr": 54,
        "pr_title": "Example PR",
        "pr_url": "https://github.com/heimgewebe/bureau/pull/54",
        "observed_github_state": {"state": "open", "source": "test"},
        "proposed_state": "reviewing",
        "finishability": 0.75,
    }
    pr_candidate["fingerprint"] = candidate_fingerprint(pr_candidate)
    existing = merge_lanes({"candidates": [branch_candidate, pr_candidate]})
    remote_lane = next(item for item in existing["lanes"] if item.get("pr") == 54)
    remote_lane["task_id"] = "BUR-2026-001-T054"
    next_candidate = {
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
    next_candidate["fingerprint"] = candidate_fingerprint(next_candidate)

    lanes = merge_lanes({"candidates": [next_candidate]}, existing)

    assert len(lanes["lanes"]) == 1
    lane = lanes["lanes"][0]
    assert lane["pr"] == 54
    assert lane["task_id"] == "BUR-2026-001-T054"
    assert lane["metadata"]["merged_alias_fingerprint"] == pr_candidate["fingerprint"]


def test_observe_open_pull_requests_passes_json_fields_as_one_argument(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr("bureau.closure.github_repo_slug", lambda root: "heimgewebe/bureau")
    calls: list[list[str]] = []

    def fake_run(
        args: list[str],
        *unused_args: object,
        **unused_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr("bureau.closure.subprocess.run", fake_run)

    observation = observe_open_pull_requests(repo)

    assert observation.pull_requests == []
    command = calls[0]
    json_index = command.index("--json")
    assert command[json_index + 1] == (
        "number,title,url,headRefName,isDraft,reviewDecision,mergeStateStatus,"
        "headRepositoryOwner,isCrossRepository"
    )
    assert "number" not in command[json_index + 2 :]

def test_observed_pr_preserves_active_lane_state() -> None:
    candidate = {
        "kind": "branch",
        "repo": "/tmp/repo",
        "repo_name": "repo",
        "branch": "feat/example",
        "pr": 54,
        "pr_title": "Example PR",
        "pr_url": "https://github.com/heimgewebe/bureau/pull/54",
        "observed_github_state": {
            "state": "open",
            "merge_state_status": "CLEAN",
            "review_decision": "APPROVED",
            "is_draft": False,
            "source": "test",
        },
        "proposed_state": "merge_candidate",
        "finishability": 0.9,
        "next_best_action": "handoff to merge gatekeeper after final evidence check",
    }
    candidate["fingerprint"] = candidate_fingerprint(candidate)
    existing = merge_lanes({"candidates": [candidate]})
    existing["lanes"][0]["state"] = "active"
    existing["lanes"][0]["task_id"] = "BUR-2026-001-T054"
    existing["lanes"][0]["next_action"] = "continue implementation work"
    existing["lanes"][0]["finishability"] = 0.4

    lanes = merge_lanes({"candidates": [candidate]}, existing)

    lane = lanes["lanes"][0]
    assert lane["state"] == "active"
    assert lane["task_id"] == "BUR-2026-001-T054"
    assert lane["next_action"] == "continue implementation work"
    assert lane["finishability"] == 0.4
    assert lane["pr"] == 54
    assert lane["metadata"]["github_observation_candidate_state"] == "merge_candidate"
    assert lane["metadata"]["preserved_lane_state"] == "active"


def test_observed_pr_preserves_paused_lane_state() -> None:
    candidate = {
        "kind": "branch",
        "repo": "/tmp/repo",
        "repo_name": "repo",
        "branch": "feat/example",
        "pr": 54,
        "pr_title": "Example PR",
        "pr_url": "https://github.com/heimgewebe/bureau/pull/54",
        "observed_github_state": {
            "state": "open",
            "merge_state_status": "CLEAN",
            "review_decision": "APPROVED",
            "is_draft": False,
            "source": "test",
        },
        "proposed_state": "merge_candidate",
        "finishability": 0.9,
        "next_best_action": "handoff to merge gatekeeper after final evidence check",
    }
    candidate["fingerprint"] = candidate_fingerprint(candidate)
    existing = merge_lanes({"candidates": [candidate]})
    existing["lanes"][0]["state"] = "paused"
    existing["lanes"][0]["task_id"] = "BUR-2026-001-T054"
    existing["lanes"][0]["next_action"] = "operator pause before merge handoff"
    existing["lanes"][0]["finishability"] = 0.2

    lanes = merge_lanes({"candidates": [candidate]}, existing)

    lane = lanes["lanes"][0]
    assert lane["state"] == "paused"
    assert lane["task_id"] == "BUR-2026-001-T054"
    assert lane["next_action"] == "operator pause before merge handoff"
    assert lane["finishability"] == 0.2
    assert lane["pr"] == 54
    assert lane["metadata"]["github_observation_candidate_state"] == "merge_candidate"
    assert lane["metadata"]["preserved_lane_state"] == "paused"


def test_observed_changes_requested_pr_preserves_paused_lane_state() -> None:
    candidate = {
        "kind": "branch",
        "repo": "/tmp/repo",
        "repo_name": "repo",
        "branch": "feat/example",
        "pr": 54,
        "pr_title": "Example PR",
        "pr_url": "https://github.com/heimgewebe/bureau/pull/54",
        "observed_github_state": {
            "state": "open",
            "merge_state_status": "CLEAN",
            "review_decision": "CHANGES_REQUESTED",
            "is_draft": False,
            "source": "test",
        },
        "proposed_state": "needs_revision",
        "finishability": 0.35,
        "next_best_action": "address requested GitHub pull-request changes before review handoff",
    }
    candidate["fingerprint"] = candidate_fingerprint(candidate)
    existing = merge_lanes({"candidates": [candidate]})
    existing["lanes"][0]["state"] = "paused"
    existing["lanes"][0]["task_id"] = "BUR-2026-001-T054"
    existing["lanes"][0]["next_action"] = "operator pause before revision handoff"
    existing["lanes"][0]["finishability"] = 0.2

    lanes = merge_lanes({"candidates": [candidate]}, existing)

    lane = lanes["lanes"][0]
    assert lane["state"] == "paused"
    assert lane["task_id"] == "BUR-2026-001-T054"
    assert lane["next_action"] == "operator pause before revision handoff"
    assert lane["finishability"] == 0.2
    assert lane["pr"] == 54
    assert lane["metadata"]["github_observation_candidate_state"] == "needs_revision"
    assert lane["metadata"]["preserved_lane_state"] == "paused"


def test_pr_alias_merge_preserves_alias_binding_metadata() -> None:
    branch_candidate = {
        "kind": "branch",
        "repo": "/tmp/repo",
        "repo_name": "repo",
        "branch": "feat/example",
        "proposed_state": "planned",
        "finishability": 0.55,
    }
    branch_candidate["fingerprint"] = candidate_fingerprint(branch_candidate)
    pr_candidate = {
        "kind": "open_pull_request",
        "repo": "/tmp/repo",
        "repo_name": "repo",
        "branch": "feat/example",
        "pr": 54,
        "pr_title": "Example PR",
        "pr_url": "https://github.com/heimgewebe/bureau/pull/54",
        "observed_github_state": {"state": "open", "source": "test"},
        "proposed_state": "reviewing",
        "finishability": 0.75,
    }
    pr_candidate["fingerprint"] = candidate_fingerprint(pr_candidate)
    existing = merge_lanes({"candidates": [branch_candidate, pr_candidate]})
    remote_lane = next(item for item in existing["lanes"] if item.get("pr") == 54)
    remote_lane["task_id"] = "BUR-2026-001-T054"
    remote_lane["metadata"] = {
        "canonical_task_binding": {
            "task_id": "BUR-2026-001-T054",
            "source": "test",
        }
    }
    next_candidate = {
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
    next_candidate["fingerprint"] = candidate_fingerprint(next_candidate)

    lanes = merge_lanes({"candidates": [next_candidate]}, existing)

    assert len(lanes["lanes"]) == 1
    lane = lanes["lanes"][0]
    assert lane["pr"] == 54
    assert lane["task_id"] == "BUR-2026-001-T054"
    assert lane["metadata"]["canonical_task_binding"]["task_id"] == "BUR-2026-001-T054"
    assert lane["metadata"]["merged_alias_fingerprint"] == pr_candidate["fingerprint"]
