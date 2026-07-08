from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        capture_output=True,
        check=False,
    )


def parse_worktree_porcelain(text: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            if current:
                items.append(current)
            current = {"worktree": value}
        elif current:
            current[key] = value
    if current:
        items.append(current)
    return items


def _is_ancestor(root: Path, commit: str, ancestor_of: str) -> bool:
    result = _run(root, "merge-base", "--is-ancestor", commit, ancestor_of)
    return result.returncode == 0


def _dirty_paths(path: Path) -> list[str]:
    result = _run(path, "status", "--porcelain=v1", "--untracked-files=all")
    if result.returncode != 0:
        return [f"git status failed: {result.stderr.strip()}"]
    return [line for line in result.stdout.splitlines() if line]


def worktree_hygiene_report(root: Path, *, max_count: int = 25) -> dict[str, Any]:
    resolved = root.expanduser().resolve()
    raw = _run(resolved, "worktree", "list", "--porcelain")
    findings: list[dict[str, Any]] = []
    if raw.returncode != 0:
        return {
            "schema_version": 1,
            "command": "worktree-hygiene",
            "read_only": True,
            "healthy": False,
            "root": str(resolved),
            "summary": {"worktrees": 0, "findings": 1},
            "findings": [
                {
                    "severity": "error",
                    "code": "worktree-list-failed",
                    "message": "git worktree list --porcelain failed.",
                    "stderr": raw.stderr.strip(),
                }
            ],
            "worktrees": [],
        }
    main_head = _run(resolved, "rev-parse", "HEAD").stdout.strip()
    items = parse_worktree_porcelain(raw.stdout)
    reports: list[dict[str, Any]] = []
    for item in items:
        path = Path(item["worktree"])
        head = item.get("HEAD", "")
        branch = item.get("branch")
        detached = branch is None
        dirty_paths = _dirty_paths(path) if path.exists() else ["worktree path missing"]
        merged_to_main = bool(head) and _is_ancestor(resolved, head, main_head)
        report = {
            "path": str(path),
            "head": head,
            "branch": branch,
            "detached": detached,
            "bare": "bare" in item,
            "dirty": bool(dirty_paths),
            "dirty_paths": dirty_paths,
            "head_merged_to_current_head": merged_to_main,
        }
        reports.append(report)
        if detached and str(path) != str(resolved):
            findings.append(
                {
                    "severity": "warning",
                    "code": "detached-worktree",
                    "message": "Linked worktree is detached.",
                    "path": str(path),
                    "head": head,
                }
            )
        if dirty_paths:
            findings.append(
                {
                    "severity": "warning",
                    "code": "dirty-worktree",
                    "message": "Linked worktree has uncommitted or untracked paths.",
                    "path": str(path),
                    "dirty_paths": dirty_paths[:20],
                }
            )
        if merged_to_main and str(path) != str(resolved):
            findings.append(
                {
                    "severity": "info",
                    "code": "merged-worktree-candidate",
                    "message": (
                        "Worktree head is an ancestor of current main and may be "
                        "cleanup candidate after review."
                    ),
                    "path": str(path),
                    "branch": branch,
                    "head": head,
                }
            )
    if len(items) > max_count:
        findings.append(
            {
                "severity": "warning",
                "code": "many-worktrees",
                "message": "Repository has more linked worktrees than the hygiene threshold.",
                "count": len(items),
                "threshold": max_count,
            }
        )
    summary = {
        "worktrees": len(items),
        "detached": sum(1 for item in reports if item["detached"]),
        "dirty": sum(1 for item in reports if item["dirty"]),
        "merged_to_current_head": sum(
            1 for item in reports if item["head_merged_to_current_head"]
        ),
        "findings": len(findings),
        "warnings": sum(1 for item in findings if item.get("severity") == "warning"),
        "errors": sum(1 for item in findings if item.get("severity") == "error"),
    }
    return {
        "schema_version": 1,
        "command": "worktree-hygiene",
        "read_only": True,
        "healthy": summary["errors"] == 0,
        "root": str(resolved),
        "summary": summary,
        "findings": findings,
        "worktrees": reports,
        "does_not_establish": [
            "safe_to_delete_worktrees",
            "branch_obsolete",
            "remote_branch_obsolete",
            "cleanup_performed",
        ],
    }
