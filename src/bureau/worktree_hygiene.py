from __future__ import annotations

import hashlib
import os
import subprocess
from contextlib import suppress
from pathlib import Path
from typing import Any

from . import legacy
from .approval import require_approval, reviewed_plan_approval

CLEANUP_PLAN_SCHEMA_VERSION = 1
CLEANUP_PLAN_COMMAND = "worktree-cleanup-plan"
CLEANUP_APPLY_COMMAND = "worktree-cleanup-apply"


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


def _git_value(root: Path, *args: str) -> str:
    result = _run(root, *args)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown git error"
        raise legacy.StateError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout.strip()


def _common_dir(root: Path) -> Path:
    value = Path(_git_value(root, "rev-parse", "--git-common-dir"))
    if not value.is_absolute():
        value = root / value
    return value.resolve()


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _process_references(path: Path, *, proc_root: Path = Path("/proc")) -> list[dict[str, Any]]:
    """Return current processes whose cwd or argv references one worktree path.

    Linux procfs is treated as a fail-closed execution prerequisite. A process
    disappearing during the scan is normal and ignored; inability to inspect
    procfs itself is not.
    """

    if not proc_root.is_dir():
        raise legacy.StateError("process reference check unavailable: /proc is missing")
    target = path.resolve()
    references: list[dict[str, Any]] = []
    try:
        entries = sorted(proc_root.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise legacy.StateError(
            f"process reference check unavailable: {type(exc).__name__}: {exc}"
        ) from exc
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        cwd_value: str | None = None
        argv: list[str] = []
        with suppress(OSError):
            cwd_value = os.readlink(entry / "cwd")
        with suppress(OSError):
            raw = (entry / "cmdline").read_bytes()
            argv = [
                item.decode("utf-8", errors="replace")
                for item in raw.split(b"\0")
                if item
            ]
        cwd_matches = False
        if cwd_value:
            cwd_text = cwd_value.removesuffix(" (deleted)")
            try:
                cwd_path = Path(cwd_text).resolve(strict=False)
                cwd_matches = cwd_path == target or _path_is_within(cwd_path, target)
            except OSError:
                cwd_matches = False
        argv_matches = False
        for value in argv:
            candidates = [value]
            if "=" in value:
                candidates.append(value.split("=", 1)[1])
            for argument in candidates:
                if argument == str(target) or argument.startswith(str(target) + os.sep):
                    argv_matches = True
                    break
            if argv_matches:
                break
        if cwd_matches or argv_matches:
            references.append(
                {
                    "pid": pid,
                    "cwd": cwd_value,
                    "argv_preview": argv[:8],
                }
            )
            if len(references) >= 50:
                break
    return references


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
    current_head = _git_value(resolved, "rev-parse", "HEAD")
    items = parse_worktree_porcelain(raw.stdout)
    reports: list[dict[str, Any]] = []
    for item in items:
        path = Path(item["worktree"])
        head = item.get("HEAD", "")
        branch = item.get("branch")
        detached = branch is None
        path_exists = path.is_dir()
        dirty_paths = _dirty_paths(path) if path_exists else ["worktree path missing"]
        merged_to_current_head = bool(head) and _is_ancestor(resolved, head, current_head)
        report = {
            "path": str(path),
            "head": head,
            "branch": branch,
            "detached": detached,
            "bare": "bare" in item,
            "locked": "locked" in item,
            "lock_reason": item.get("locked") or None,
            "prunable": "prunable" in item,
            "prunable_reason": item.get("prunable") or None,
            "path_exists": path_exists,
            "dirty": bool(dirty_paths),
            "dirty_paths": dirty_paths,
            "head_merged_to_current_head": merged_to_current_head,
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
        if report["locked"]:
            findings.append(
                {
                    "severity": "warning",
                    "code": "locked-worktree",
                    "message": "Linked worktree is locked and cannot be a cleanup candidate.",
                    "path": str(path),
                    "reason": report["lock_reason"],
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
        if merged_to_current_head and str(path) != str(resolved):
            findings.append(
                {
                    "severity": "info",
                    "code": "merged-worktree-candidate",
                    "message": (
                        "Worktree head is an ancestor of current HEAD and may be "
                        "a cleanup candidate after explicit review."
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
        "locked": sum(1 for item in reports if item["locked"]),
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
        "repository_head": current_head,
        "git_common_dir": str(_common_dir(resolved)),
        "summary": summary,
        "findings": findings,
        "worktrees": reports,
        "does_not_establish": [
            "safe_to_delete_worktrees",
            "branch_obsolete",
            "remote_branch_obsolete",
            "process_or_lease_absence",
            "cleanup_performed",
        ],
    }


def _candidate_identity(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": item["path"],
        "head": item["head"],
        "branch": item["branch"],
        "detached": item["detached"],
        "bare": item["bare"],
        "locked": item["locked"],
        "path_exists": item["path_exists"],
        "dirty": item["dirty"],
        "head_merged_to_current_head": item["head_merged_to_current_head"],
    }


def _repository_identity(report: dict[str, Any]) -> dict[str, Any]:
    worktrees = report.get("worktrees")
    if not isinstance(worktrees, list) or not worktrees:
        raise legacy.StateError("worktree hygiene report has no repository worktrees")
    return {
        "root": report["root"],
        "git_common_dir": report["git_common_dir"],
        "canonical_worktree": worktrees[0]["path"],
    }


def _select_candidates(
    report: dict[str, Any], candidates: list[str], *, check_processes: bool
) -> list[dict[str, Any]]:
    if not candidates:
        raise legacy.StateError("worktree cleanup plan requires at least one explicit candidate")
    indexed = {
        str(Path(item["path"]).resolve(strict=False)): item
        for item in report.get("worktrees", [])
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    canonical = Path(_repository_identity(report)["canonical_worktree"]).resolve(strict=False)
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_candidate in candidates:
        raw_path = Path(raw_candidate).expanduser()
        if not raw_path.is_absolute():
            raise legacy.StateError(
                f"worktree cleanup candidate must be an absolute path: {raw_candidate}"
            )
        candidate = raw_path.resolve(strict=False)
        candidate_text = str(candidate)
        if candidate_text in seen:
            raise legacy.StateError(f"duplicate worktree cleanup candidate: {candidate_text}")
        seen.add(candidate_text)
        item = indexed.get(candidate_text)
        if item is None:
            raise legacy.StateError(
                f"worktree cleanup candidate is not a linked worktree: {candidate_text}"
            )
        if candidate == canonical:
            raise legacy.StateError("canonical worktree cannot be a cleanup candidate")
        if item.get("bare"):
            raise legacy.StateError(f"bare worktree cleanup is forbidden: {candidate_text}")
        if item.get("locked"):
            raise legacy.StateError(f"locked worktree cleanup is forbidden: {candidate_text}")
        if not item.get("path_exists"):
            raise legacy.StateError(f"missing worktree cleanup is forbidden: {candidate_text}")
        if item.get("dirty"):
            raise legacy.StateError(f"dirty worktree cleanup is forbidden: {candidate_text}")
        if not item.get("head_merged_to_current_head"):
            raise legacy.StateError(
                f"unmerged worktree cleanup is forbidden: {candidate_text}"
            )
        if check_processes:
            references = _process_references(candidate)
            if references:
                pids = ", ".join(str(item["pid"]) for item in references)
                raise legacy.StateError(
                    f"worktree cleanup candidate is referenced by active process(es) {pids}: "
                    f"{candidate_text}"
                )
        selected.append(_candidate_identity(item))
    selected = sorted(selected, key=lambda item: item["path"])
    for index, item in enumerate(selected):
        item_path = Path(item["path"])
        for other in selected[index + 1 :]:
            other_path = Path(other["path"])
            if _path_is_within(other_path, item_path) or _path_is_within(
                item_path, other_path
            ):
                raise legacy.StateError(
                    "overlapping worktree cleanup candidates are forbidden: "
                    f"{item_path} and {other_path}"
                )
    return selected


def worktree_cleanup_plan(
    root: Path, candidates: list[str], *, max_count: int = 25
) -> dict[str, Any]:
    report = worktree_hygiene_report(root, max_count=max_count)
    if not report["healthy"]:
        raise legacy.StateError("worktree hygiene report is unhealthy")
    selected = _select_candidates(report, candidates, check_processes=True)
    repository = _repository_identity(report)
    candidate_states_sha256 = legacy.sha256_json(selected)
    repository_identity_sha256 = legacy.sha256_json(repository)
    return {
        "schema_version": CLEANUP_PLAN_SCHEMA_VERSION,
        "command": CLEANUP_PLAN_COMMAND,
        "created_at": legacy.utc_now(),
        "max_count": max_count,
        "repository": repository,
        "repository_head_at_plan": report["repository_head"],
        "repository_identity_sha256": repository_identity_sha256,
        "candidates": selected,
        "candidate_states_sha256": candidate_states_sha256,
        "review": {
            "required": True,
            "status": "pending",
            "instructions": (
                "Review every candidate path and identity. To apply, set status to reviewed, "
                "add reviewer and reviewed_at, and copy repository_identity_sha256 plus "
                "candidate_states_sha256 into this review object."
            ),
        },
        "execution_preconditions": [
            "Acquire the dedicated Bureau worktree-admin effect gate immediately before apply.",
            "Confirm no foreign exact path lease covers any candidate.",
            "Run apply from the same repository identity bound in this plan.",
        ],
        "does_not_establish": [
            "branch_obsolete",
            "remote_branch_obsolete",
            "external_lease_absence",
            "process_absence_beyond_procfs_visibility",
            "cleanup_authority_without_review",
        ],
    }


def write_worktree_cleanup_plan(
    root: Path,
    candidates: list[str],
    path: str | Path,
    *,
    max_count: int = 25,
) -> dict[str, Any]:
    plan = worktree_cleanup_plan(root, candidates, max_count=max_count)
    target = Path(path).expanduser().resolve(strict=False)
    for candidate in plan["candidates"]:
        candidate_path = Path(candidate["path"])
        if target == candidate_path or _path_is_within(target, candidate_path):
            raise legacy.StateError("cleanup plan cannot be stored inside a cleanup candidate")
    legacy.atomic_write(target, legacy.canonical_json(plan) + "\n")
    return {**plan, "path": str(target)}


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise legacy.StateError(
            f"worktree cleanup plan cannot be read: {type(exc).__name__}: {exc}"
        ) from exc


def _load_reviewed_cleanup_plan(path: str | Path) -> tuple[Path, dict[str, Any], str]:
    target = Path(path).expanduser().resolve(strict=True)
    plan_sha256 = _file_sha256(target)
    plan = legacy.read_json(target)
    if (
        plan.get("schema_version") != CLEANUP_PLAN_SCHEMA_VERSION
        or plan.get("command") != CLEANUP_PLAN_COMMAND
    ):
        raise legacy.StateError("worktree cleanup plan has unsupported schema or command")
    review = plan.get("review")
    if not isinstance(review, dict) or review.get("status") != "reviewed":
        raise legacy.StateError("worktree cleanup plan is not reviewed")
    if not review.get("reviewer") or not review.get("reviewed_at"):
        raise legacy.StateError("reviewed worktree cleanup plan requires reviewer and reviewed_at")
    if review.get("repository_identity_sha256") != plan.get(
        "repository_identity_sha256"
    ):
        raise legacy.StateError("review is not bound to repository identity")
    if review.get("candidate_states_sha256") != plan.get("candidate_states_sha256"):
        raise legacy.StateError("review is not bound to cleanup candidate states")
    plan["approval"] = require_approval(
        "worktree_cleanup",
        reviewed_plan_approval(
            reviewer=str(review["reviewer"]),
            reference=str(target),
            approved=True,
            scope="worktree_cleanup",
        ),
        expected_reference=str(target),
    )
    return target, plan, plan_sha256


def _restore_removed_candidate(root: Path, candidate: dict[str, Any]) -> dict[str, Any]:
    path = candidate["path"]
    branch = candidate.get("branch")
    if branch:
        short_branch = str(branch).removeprefix("refs/heads/")
        result = _run(root, "worktree", "add", path, short_branch)
    else:
        result = _run(root, "worktree", "add", "--detach", path, candidate["head"])
    return {
        "path": path,
        "restored": result.returncode == 0,
        "returncode": result.returncode,
        "stderr": result.stderr.strip(),
    }


def apply_worktree_cleanup_plan(root: Path, path: str | Path) -> dict[str, Any]:
    plan_path, plan, reviewed_plan_sha256 = _load_reviewed_cleanup_plan(path)
    resolved = root.expanduser().resolve()
    report_before = worktree_hygiene_report(
        resolved, max_count=int(plan.get("max_count", 25))
    )
    if not report_before["healthy"]:
        raise legacy.StateError("pre-clean worktree hygiene report is unhealthy")
    repository = _repository_identity(report_before)
    if repository != plan.get("repository"):
        raise legacy.StateError("worktree cleanup repository identity changed since review")
    if legacy.sha256_json(repository) != plan.get("repository_identity_sha256"):
        raise legacy.StateError("worktree cleanup repository identity hash mismatch")
    planned_candidates = plan.get("candidates")
    if not isinstance(planned_candidates, list) or not planned_candidates:
        raise legacy.StateError("worktree cleanup plan has no candidates")
    candidate_paths = [str(item.get("path", "")) for item in planned_candidates]
    selected = _select_candidates(report_before, candidate_paths, check_processes=True)
    if selected != planned_candidates:
        raise legacy.StateError("worktree cleanup candidate state changed since review")
    if legacy.sha256_json(selected) != plan.get("candidate_states_sha256"):
        raise legacy.StateError("worktree cleanup candidate state hash mismatch")
    for candidate in selected:
        candidate_path = Path(candidate["path"])
        if plan_path == candidate_path or _path_is_within(plan_path, candidate_path):
            raise legacy.StateError("reviewed cleanup plan is stored inside a candidate")

    removed: list[dict[str, Any]] = []
    try:
        for candidate in selected:
            if _file_sha256(plan_path) != reviewed_plan_sha256:
                raise legacy.StateError("reviewed cleanup plan changed before effect")
            current_report = worktree_hygiene_report(
                resolved, max_count=int(plan.get("max_count", 25))
            )
            current = _select_candidates(
                current_report, [candidate["path"]], check_processes=True
            )[0]
            if current != candidate:
                raise legacy.StateError(
                    f"worktree cleanup candidate changed before effect: {candidate['path']}"
                )
            result = _run(resolved, "worktree", "remove", candidate["path"])
            if result.returncode != 0:
                detail = result.stderr.strip() or result.stdout.strip() or "unknown git error"
                raise legacy.StateError(
                    f"git worktree remove refused {candidate['path']}: {detail}"
                )
            removed.append(candidate)
        if _file_sha256(plan_path) != reviewed_plan_sha256:
            raise legacy.StateError("reviewed cleanup plan changed during effect")
        post_report = worktree_hygiene_report(
            resolved, max_count=int(plan.get("max_count", 25))
        )
        remaining = {item["path"] for item in post_report.get("worktrees", [])}
        unexpected = [item["path"] for item in removed if item["path"] in remaining]
        if unexpected or not post_report["healthy"]:
            raise legacy.StateError(
                "post-clean worktree hygiene gate failed: "
                + legacy.canonical_json(
                    {"unexpected_remaining": unexpected, "healthy": post_report["healthy"]}
                )
            )
    except Exception as exc:
        rollback = [
            _restore_removed_candidate(resolved, candidate)
            for candidate in reversed(removed)
            if not Path(candidate["path"]).exists()
        ]
        if rollback and not all(item["restored"] for item in rollback):
            raise legacy.StateError(
                f"worktree cleanup failed and rollback was incomplete: {exc}; "
                + legacy.canonical_json(rollback)
            ) from exc
        raise

    return {
        "schema_version": 1,
        "command": CLEANUP_APPLY_COMMAND,
        "applied": True,
        "plan_path": str(plan_path),
        "reviewed_plan_sha256": reviewed_plan_sha256,
        "repository": repository,
        "repository_head_before": report_before["repository_head"],
        "repository_head_after": post_report["repository_head"],
        "removed_worktrees": removed,
        "branches_deleted": [],
        "approval": plan.get("approval"),
        "post_clean_report": post_report,
        "does_not_establish": [
            "branch_obsolete",
            "remote_branch_obsolete",
            "permission_to_delete_branches",
            "future_process_or_lease_absence",
            "process_absence_beyond_procfs_visibility",
        ],
    }
