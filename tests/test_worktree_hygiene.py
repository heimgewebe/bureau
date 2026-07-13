from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bureau import worktree_hygiene
from bureau.core import StateError
from bureau.worktree_hygiene import (
    apply_worktree_cleanup_plan,
    parse_worktree_porcelain,
    worktree_cleanup_plan,
    worktree_hygiene_report,
    write_worktree_cleanup_plan,
)


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def make_repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "bureau-test@example.invalid")
    git(root, "config", "user.name", "Bureau Test")
    (root / "README.md").write_text("test\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "initial")
    return root


def add_merged_worktree(root: Path, path: Path, branch: str = "linked") -> None:
    git(root, "worktree", "add", str(path), "-b", branch)
    (path / "feature.txt").write_text(branch + "\n", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-m", f"add {branch}")
    git(root, "merge", "--no-ff", branch, "-m", f"merge {branch}")


def review_plan(path: Path) -> dict:
    plan = json.loads(path.read_text(encoding="utf-8"))
    plan["review"] = {
        **plan["review"],
        "status": "reviewed",
        "reviewer": "test-reviewer",
        "reviewed_at": "2026-07-13T15:00:00Z",
        "repository_identity_sha256": plan["repository_identity_sha256"],
        "candidate_states_sha256": plan["candidate_states_sha256"],
    }
    path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return plan


def test_parse_worktree_porcelain_records_detached_branch_and_lock() -> None:
    raw = (
        "worktree /repo\n"
        "HEAD abc\n"
        "branch refs/heads/main\n"
        "\n"
        "worktree /tmp/wt\n"
        "HEAD def\n"
        "detached\n"
        "locked maintenance\n"
    )

    items = parse_worktree_porcelain(raw)

    assert items == [
        {"worktree": "/repo", "HEAD": "abc", "branch": "refs/heads/main"},
        {
            "worktree": "/tmp/wt",
            "HEAD": "def",
            "detached": "",
            "locked": "maintenance",
        },
    ]


def test_worktree_hygiene_report_is_read_only_and_reports_many_worktrees(tmp_path):
    root = make_repository(tmp_path)
    linked = tmp_path / "linked"
    git(root, "worktree", "add", str(linked), "-b", "linked")

    report = worktree_hygiene_report(root, max_count=1)

    assert report["read_only"] is True
    assert report["healthy"] is True
    assert report["summary"]["worktrees"] == 2
    assert "many-worktrees" in {item["code"] for item in report["findings"]}
    assert any(item["path"].endswith("linked") for item in report["worktrees"])
    assert "process_or_lease_absence" in report["does_not_establish"]


def test_cleanup_plan_requires_explicit_absolute_merged_clean_candidate(tmp_path):
    root = make_repository(tmp_path)
    linked = tmp_path / "linked"
    add_merged_worktree(root, linked)

    plan = worktree_cleanup_plan(root, [str(linked)])

    assert plan["command"] == "worktree-cleanup-plan"
    assert plan["review"]["status"] == "pending"
    assert [item["path"] for item in plan["candidates"]] == [str(linked.resolve())]
    assert plan["candidates"][0]["head_merged_to_current_head"] is True
    assert plan["candidates"][0]["dirty"] is False
    assert "external_lease_absence" in plan["does_not_establish"]

    with pytest.raises(StateError, match="absolute path"):
        worktree_cleanup_plan(root, ["../linked"])
    with pytest.raises(StateError, match="canonical worktree"):
        worktree_cleanup_plan(root, [str(root)])
    with pytest.raises(StateError, match="duplicate"):
        worktree_cleanup_plan(root, [str(linked), str(linked)])


def test_cleanup_plan_refuses_dirty_unmerged_missing_locked_and_process_use(
    tmp_path, monkeypatch
):
    root = make_repository(tmp_path)
    unmerged = tmp_path / "unmerged"
    git(root, "worktree", "add", str(unmerged), "-b", "unmerged")
    (unmerged / "unmerged.txt").write_text("unmerged\n", encoding="utf-8")
    git(unmerged, "add", ".")
    git(unmerged, "commit", "-m", "unmerged change")

    with pytest.raises(StateError, match="unmerged worktree cleanup"):
        worktree_cleanup_plan(root, [str(unmerged)])

    (unmerged / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(StateError, match="dirty worktree cleanup"):
        worktree_cleanup_plan(root, [str(unmerged)])
    (unmerged / "dirty.txt").unlink()

    with pytest.raises(StateError, match="not a linked worktree"):
        worktree_cleanup_plan(root, [str(tmp_path / "missing")])

    git(root, "worktree", "lock", str(unmerged), "--reason", "active")
    with pytest.raises(StateError, match="locked worktree cleanup"):
        worktree_cleanup_plan(root, [str(unmerged)])
    git(root, "worktree", "unlock", str(unmerged))

    git(root, "merge", "--no-ff", "unmerged", "-m", "merge unmerged")
    monkeypatch.setattr(
        worktree_hygiene,
        "_process_references",
        lambda _path: [{"pid": 42, "cwd": str(unmerged), "argv_preview": []}],
    )
    with pytest.raises(StateError, match=r"active process.*42"):
        worktree_cleanup_plan(root, [str(unmerged)])


def test_process_reference_detects_key_value_path_argument(tmp_path):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    proc_root = tmp_path / "proc"
    pid_root = proc_root / "42"
    pid_root.mkdir(parents=True)
    (pid_root / "cmdline").write_bytes(
        b"worker\0--cwd=" + str(candidate).encode("utf-8") + b"/child\0"
    )

    references = worktree_hygiene._process_references(candidate, proc_root=proc_root)

    assert [item["pid"] for item in references] == [42]


def test_cleanup_plan_refuses_overlapping_candidate_paths(tmp_path, monkeypatch):
    root = make_repository(tmp_path)
    parent = tmp_path / "parent"
    child = parent / "child"
    parent.mkdir()
    child.mkdir()
    report = {
        "root": str(root.resolve()),
        "git_common_dir": str((root / ".git").resolve()),
        "worktrees": [
            {
                "path": str(root.resolve()),
                "head": "root",
                "branch": "refs/heads/main",
                "detached": False,
                "bare": False,
                "locked": False,
                "path_exists": True,
                "dirty": False,
                "head_merged_to_current_head": True,
            },
            {
                "path": str(parent.resolve()),
                "head": "parent",
                "branch": "refs/heads/parent",
                "detached": False,
                "bare": False,
                "locked": False,
                "path_exists": True,
                "dirty": False,
                "head_merged_to_current_head": True,
            },
            {
                "path": str(child.resolve()),
                "head": "child",
                "branch": "refs/heads/child",
                "detached": False,
                "bare": False,
                "locked": False,
                "path_exists": True,
                "dirty": False,
                "head_merged_to_current_head": True,
            },
        ],
    }
    monkeypatch.setattr(worktree_hygiene, "_process_references", lambda _path: [])

    with pytest.raises(StateError, match="overlapping worktree cleanup"):
        worktree_hygiene._select_candidates(
            report, [str(parent), str(child)], check_processes=True
        )


def test_write_plan_refuses_plan_inside_candidate(tmp_path):
    root = make_repository(tmp_path)
    linked = tmp_path / "linked"
    add_merged_worktree(root, linked)

    with pytest.raises(StateError, match="stored inside"):
        write_worktree_cleanup_plan(
            root, [str(linked)], linked / "cleanup-plan.json"
        )


def test_apply_requires_review_bound_to_repository_and_candidate_hashes(tmp_path):
    root = make_repository(tmp_path)
    linked = tmp_path / "linked"
    add_merged_worktree(root, linked)
    plan_path = tmp_path / "cleanup-plan.json"
    write_worktree_cleanup_plan(root, [str(linked)], plan_path)

    with pytest.raises(StateError, match="not reviewed"):
        apply_worktree_cleanup_plan(root, plan_path)

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["review"] = {
        **plan["review"],
        "status": "reviewed",
        "reviewer": "reviewer",
        "reviewed_at": "2026-07-13T15:00:00Z",
        "repository_identity_sha256": plan["repository_identity_sha256"],
        "candidate_states_sha256": "wrong",
    }
    plan_path.write_text(json.dumps(plan) + "\n", encoding="utf-8")
    with pytest.raises(StateError, match="not bound to cleanup candidate states"):
        apply_worktree_cleanup_plan(root, plan_path)


def test_apply_refuses_candidate_drift_after_review(tmp_path):
    root = make_repository(tmp_path)
    linked = tmp_path / "linked"
    add_merged_worktree(root, linked)
    plan_path = tmp_path / "cleanup-plan.json"
    write_worktree_cleanup_plan(root, [str(linked)], plan_path)
    review_plan(plan_path)

    (linked / "new-dirty.txt").write_text("dirty after review\n", encoding="utf-8")

    with pytest.raises(StateError, match="dirty worktree cleanup"):
        apply_worktree_cleanup_plan(root, plan_path)
    assert linked.is_dir()


def test_apply_removes_only_reviewed_worktree_preserves_branch_and_reports_fresh_state(
    tmp_path,
):
    root = make_repository(tmp_path)
    linked = tmp_path / "linked"
    other = tmp_path / "other"
    add_merged_worktree(root, linked, "linked")
    add_merged_worktree(root, other, "other")
    plan_path = tmp_path / "cleanup-plan.json"
    write_worktree_cleanup_plan(root, [str(linked)], plan_path, max_count=1)
    review_plan(plan_path)

    result = apply_worktree_cleanup_plan(root, plan_path)

    assert result["applied"] is True
    assert [item["path"] for item in result["removed_worktrees"]] == [
        str(linked.resolve())
    ]
    assert result["branches_deleted"] == []
    assert not linked.exists()
    assert other.is_dir()
    assert git(root, "show-ref", "--verify", "refs/heads/linked")
    remaining = {item["path"] for item in result["post_clean_report"]["worktrees"]}
    assert str(linked.resolve()) not in remaining
    assert str(other.resolve()) in remaining
    assert result["post_clean_report"]["command"] == "worktree-hygiene"


def test_apply_rolls_back_prior_removal_when_later_candidate_changes(
    tmp_path, monkeypatch
):
    root = make_repository(tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"
    add_merged_worktree(root, first, "first")
    add_merged_worktree(root, second, "second")
    plan_path = tmp_path / "cleanup-plan.json"
    write_worktree_cleanup_plan(root, [str(first), str(second)], plan_path)
    review_plan(plan_path)

    original_select = worktree_hygiene._select_candidates
    calls = {"count": 0}

    def fail_second(report, candidates, *, check_processes):
        calls["count"] += 1
        if calls["count"] == 3:
            raise StateError("simulated second-candidate drift")
        return original_select(report, candidates, check_processes=check_processes)

    monkeypatch.setattr(worktree_hygiene, "_select_candidates", fail_second)

    with pytest.raises(StateError, match="simulated second-candidate drift"):
        apply_worktree_cleanup_plan(root, plan_path)

    assert first.is_dir()
    assert second.is_dir()
    assert git(root, "show-ref", "--verify", "refs/heads/first")
    assert git(root, "show-ref", "--verify", "refs/heads/second")
