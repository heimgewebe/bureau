from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from bureau import source_pr_bridge
from bureau.now_refill import NowRefillPolicy


class FakeRunner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, arguments, *, allow_not_found=False):
        self.calls.append((list(arguments), allow_not_found))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def encoded(value):
    return json.dumps(value)


def test_reconcile_returns_branch_absent(monkeypatch):
    runner = FakeRunner([None])
    monkeypatch.setattr(source_pr_bridge, "_run", runner)
    result = source_pr_bridge.reconcile()
    assert result["status"] == "branch_absent"
    assert runner.calls[0][1] is True


def test_reconcile_returns_no_change_when_branch_is_not_ahead(monkeypatch):
    runner = FakeRunner(
        [
            encoded({"object": {"sha": "abc"}}),
            encoded({"ahead_by": 0}),
        ]
    )
    monkeypatch.setattr(source_pr_bridge, "_run", runner)
    result = source_pr_bridge.reconcile()
    assert result["status"] == "no_change"
    assert result["head_sha"] == "abc"
    assert result["ahead_by"] == 0


def test_reconcile_creates_missing_pull_request(monkeypatch):
    runner = FakeRunner(
        [
            encoded({"object": {"sha": "abc"}}),
            encoded({"ahead_by": 1}),
            encoded([]),
            "https://github.com/heimgewebe/bureau/pull/9",
        ]
    )
    monkeypatch.setattr(source_pr_bridge, "_run", runner)
    result = source_pr_bridge.reconcile()
    assert result["status"] == "created"
    assert result["head_sha"] == "abc"
    assert result["url"].endswith("/9")
    assert runner.calls[-1][0][:2] == ["pr", "create"]


def test_reconcile_updates_open_pull_request(monkeypatch):
    runner = FakeRunner(
        [
            encoded({"object": {"sha": "abc"}}),
            encoded({"ahead_by": 2}),
            encoded([{"number": 8, "url": "https://github.com/heimgewebe/bureau/pull/8"}]),
            "",
        ]
    )
    monkeypatch.setattr(source_pr_bridge, "_run", runner)
    result = source_pr_bridge.reconcile()
    assert result["status"] == "updated"
    assert result["pull_request"] == 8
    assert runner.calls[-1][0][:3] == ["pr", "edit", "8"]


def test_now_refill_bridge_creates_pr_and_enables_auto_merge(monkeypatch):
    runner = FakeRunner(
        [
            encoded({"object": {"sha": "def"}}),
            encoded({"ahead_by": 1}),
            encoded([]),
            "https://github.com/heimgewebe/bureau/pull/10",
            "",
        ]
    )
    monkeypatch.setattr(source_pr_bridge, "_run", runner)
    result = source_pr_bridge.reconcile(
        branch=source_pr_bridge.NOW_REFILL_BRANCH,
        kind="now-refill",
        auto_merge=True,
    )
    assert result["status"] == "created"
    assert result["pull_request"] == 10
    assert result["auto_merge_requested"] is True
    assert runner.calls[-1][0][:3] == ["pr", "merge", "10"]


def _run_git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout.strip()


def _move_all_now_to_next_review_before_effect(root: Path) -> list[str]:
    queue_path = root / "registry/queue.json"
    queue = json.loads(queue_path.read_text())
    ids = list(queue["lanes"]["now"])
    queue["lanes"] = {"now": [], "next": ids, "later": []}
    queue_path.write_text(json.dumps(queue))
    for task_id in ids:
        task_path = root / f"registry/tasks/{task_id}.json"
        task = json.loads(task_path.read_text())
        task["priority"]["lane"] = "next"
        task["execution"]["policy"] = "review-before-effect"
        task_path.write_text(json.dumps(task))
    return ids


def _bootstrap_origin_and_checkout(
    tmp_path: Path, registry_factory, *, trigger_refill: bool
) -> tuple[Path, Path, list[str]]:
    """Build a bare 'origin' remote and a real local checkout cloned from it.

    Standing in for GitHub with a local bare repository lets ``publish_now_refill``
    run its real ``git fetch``/``worktree add``/``commit``/``push`` sequence end to
    end, without any network access.
    """
    content_root = registry_factory(task_count=3)
    ids = (
        _move_all_now_to_next_review_before_effect(content_root)
        if trigger_refill
        else []
    )

    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", str(origin)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    seed = tmp_path / "seed"
    shutil.copytree(content_root, seed)
    _run_git(seed, "init")
    _run_git(seed, "checkout", "-b", "main")
    _run_git(seed, "config", "user.name", "Bureau Test")
    _run_git(seed, "config", "user.email", "bureau-test@example.invalid")
    _run_git(seed, "remote", "add", "origin", str(origin))
    _run_git(seed, "add", "-A")
    _run_git(seed, "commit", "-m", "seed registry snapshot")
    _run_git(seed, "push", "origin", "main")

    checkout = tmp_path / "checkout"
    subprocess.run(
        ["git", "clone", str(origin), str(checkout)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    _run_git(checkout, "checkout", "main")
    return origin, checkout, ids


def test_publish_now_refill_pushes_new_branch(tmp_path, registry_factory, monkeypatch):
    origin, checkout, ids = _bootstrap_origin_and_checkout(
        tmp_path, registry_factory, trigger_refill=True
    )

    def fake_json(arguments, *, allow_not_found=False):
        assert arguments == [
            "api",
            f"repos/{source_pr_bridge.DEFAULT_REPOSITORY}"
            f"/git/ref/heads/{source_pr_bridge.NOW_REFILL_BRANCH}",
        ]
        assert allow_not_found is True
        return None

    monkeypatch.setattr(source_pr_bridge, "_json", fake_json)

    result = source_pr_bridge.publish_now_refill(
        checkout,
        state_db=tmp_path / "state" / "state.sqlite3",
        state_root=tmp_path / "state",
        policy=NowRefillPolicy(floor=2, target=3, max_promotions=3),
        authority="test-operator",
    )

    assert result["status"] == "published"
    assert result["branch"] == source_pr_bridge.NOW_REFILL_BRANCH
    assert [item["task_id"] for item in result["promotions"]] == ids

    branch_ref = _run_git(origin, "rev-parse", f"refs/heads/{source_pr_bridge.NOW_REFILL_BRANCH}")
    assert branch_ref == result["head_sha"]
    main_ref = _run_git(origin, "rev-parse", "refs/heads/main")
    assert main_ref == result["base_sha"]

    queue_on_branch = _run_git(origin, "show", f"{branch_ref}:registry/queue.json")
    assert json.loads(queue_on_branch)["lanes"]["now"] == ids

    # The operator's own checkout is untouched: still on main, still clean.
    assert _run_git(checkout, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert _run_git(checkout, "rev-parse", "HEAD") == result["base_sha"]
    assert _run_git(checkout, "status", "--porcelain") == ""
    worktrees = _run_git(checkout, "worktree", "list", "--porcelain")
    assert "bureau-now-refill" not in worktrees


def test_publish_now_refill_ignores_checked_out_stale_local_proposal_branch(
    tmp_path, registry_factory, monkeypatch
):
    origin, checkout, _ids = _bootstrap_origin_and_checkout(
        tmp_path, registry_factory, trigger_refill=True
    )
    stale_worktree = tmp_path / "stale-proposal"
    _run_git(checkout, "branch", source_pr_bridge.NOW_REFILL_BRANCH, "main")
    _run_git(
        checkout,
        "worktree",
        "add",
        str(stale_worktree),
        source_pr_bridge.NOW_REFILL_BRANCH,
    )

    def fake_json(arguments, *, allow_not_found=False):
        assert allow_not_found is True
        return None

    monkeypatch.setattr(source_pr_bridge, "_json", fake_json)

    result = source_pr_bridge.publish_now_refill(
        checkout,
        state_db=tmp_path / "state" / "state.sqlite3",
        state_root=tmp_path / "state",
        policy=NowRefillPolicy(floor=2, target=3, max_promotions=3),
        authority="test-operator",
    )

    assert result["status"] == "published"
    assert _run_git(stale_worktree, "rev-parse", "--abbrev-ref", "HEAD") == (
        source_pr_bridge.NOW_REFILL_BRANCH
    )
    assert _run_git(stale_worktree, "rev-parse", "HEAD") == result["base_sha"]
    assert _run_git(origin, "rev-parse", f"refs/heads/{source_pr_bridge.NOW_REFILL_BRANCH}") == (
        result["head_sha"]
    )



def test_publish_now_refill_is_a_noop_when_not_triggered(tmp_path, registry_factory, monkeypatch):
    # registry_factory's default queue already satisfies the floor: nothing to refill.
    origin, checkout, _ids = _bootstrap_origin_and_checkout(
        tmp_path, registry_factory, trigger_refill=False
    )

    def fail_json(arguments, *, allow_not_found=False):
        raise AssertionError("gh should not be consulted when no refill is applied")

    monkeypatch.setattr(source_pr_bridge, "_json", fail_json)

    result = source_pr_bridge.publish_now_refill(
        checkout,
        state_db=tmp_path / "state" / "state.sqlite3",
        state_root=tmp_path / "state",
        policy=NowRefillPolicy(floor=2, target=3, max_promotions=3),
        authority="test-operator",
    )

    assert result["status"] == "not-applied"
    assert result["refill_status"] == "satisfied"

    branches = _run_git(origin, "branch", "--list")
    assert source_pr_bridge.NOW_REFILL_BRANCH not in branches
    worktrees = _run_git(checkout, "worktree", "list", "--porcelain")
    assert "bureau-now-refill" not in worktrees
