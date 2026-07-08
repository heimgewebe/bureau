from __future__ import annotations

import subprocess

from bureau.worktree_hygiene import parse_worktree_porcelain, worktree_hygiene_report


def git(root, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def test_parse_worktree_porcelain_records_detached_and_branch() -> None:
    raw = (
        "worktree /repo\n"
        "HEAD abc\n"
        "branch refs/heads/main\n"
        "\n"
        "worktree /tmp/wt\n"
        "HEAD def\n"
        "detached\n"
    )

    items = parse_worktree_porcelain(raw)

    assert items == [
        {"worktree": "/repo", "HEAD": "abc", "branch": "refs/heads/main"},
        {"worktree": "/tmp/wt", "HEAD": "def", "detached": ""},
    ]


def test_worktree_hygiene_report_is_read_only_and_reports_many_worktrees(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "bureau-test@example.invalid")
    git(root, "config", "user.name", "Bureau Test")
    (root / "README.md").write_text("test\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "initial")
    git(root, "worktree", "add", "../linked", "-b", "linked")

    report = worktree_hygiene_report(root, max_count=1)

    assert report["read_only"] is True
    assert report["healthy"] is True
    assert report["summary"]["worktrees"] == 2
    assert "many-worktrees" in {item["code"] for item in report["findings"]}
    assert any(item["path"].endswith("linked") for item in report["worktrees"])
