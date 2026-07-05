"""Bridge merged GitHub pull requests back into Bureau task closure evidence.

The bridge is intentionally conservative: a merged PR is only sufficient for
Bureau task closure when the task carries an explicit PR closure binding.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TASK_TERMINAL_STATES = {"verified", "completed", "done", "closed", "cancelled", "superseded"}
GITHUB_REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
SHA40 = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class PullRequestBinding:
    repo: str
    number: int
    trigger: str = "pr_merged"
    expected_head_sha: str | None = None
    post_merge_required: bool = False
    auto_verify: bool = False
    non_claims: tuple[str, ...] = ()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def dump_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def task_files(root: Path) -> Iterable[Path]:
    tasks_dir = root / "registry" / "tasks"
    if not tasks_dir.exists():
        return ()
    return sorted(tasks_dir.glob("*.json"))


def _first_mapping(*values: Any) -> dict[str, Any] | None:
    for value in values:
        if isinstance(value, dict):
            return value
    return None


def pr_binding(task: dict[str, Any]) -> PullRequestBinding | None:
    """Return the explicit PR closure binding for a task, if present.

    Canonical location:
      metadata.pr_closure = {
        "repo": "owner/repo",
        "number": 123,
        "trigger": "pr_merged",
        "expected_head_sha": "...",        # optional
        "post_merge_required": false,       # optional, default false
        "auto_verify": false,               # optional, default false
        "non_claims": ["..."]              # optional
      }

    A few legacy aliases are accepted to keep the bridge useful for already
    registered tasks, but closure still requires an explicit object.
    """

    metadata = task.get("metadata")
    if not isinstance(metadata, dict):
        return None
    raw = _first_mapping(
        metadata.get("pr_closure"),
        metadata.get("pull_request_closure"),
        metadata.get("github_pull_request"),
        metadata.get("github_pr"),
    )
    if raw is None:
        return None

    repo = raw.get("repo") or raw.get("repository")
    number = raw.get("number", raw.get("pr", raw.get("pr_number")))
    trigger = str(raw.get("trigger", "pr_merged"))
    expected_head_sha = raw.get("expected_head_sha") or raw.get("head_sha")
    post_merge_required = bool(raw.get("post_merge_required", False))
    auto_verify = bool(raw.get("auto_verify", False))
    raw_non_claims = raw.get("non_claims", raw.get("does_not_establish", ()))

    if not isinstance(repo, str) or not GITHUB_REPO.fullmatch(repo):
        return None
    try:
        number_int = int(number)
    except (TypeError, ValueError):
        return None
    if number_int <= 0:
        return None
    if trigger != "pr_merged":
        return None
    if expected_head_sha is not None and (
        not isinstance(expected_head_sha, str) or not SHA40.fullmatch(expected_head_sha)
    ):
        return None
    if isinstance(raw_non_claims, (list, tuple)):
        non_claims = tuple(str(item) for item in raw_non_claims)
    else:
        non_claims = ()

    return PullRequestBinding(
        repo=repo,
        number=number_int,
        trigger=trigger,
        expected_head_sha=expected_head_sha,
        post_merge_required=post_merge_required,
        auto_verify=auto_verify,
        non_claims=non_claims,
    )


def task_is_terminal(task: dict[str, Any]) -> bool:
    return str(task.get("state", "")).lower() in TASK_TERMINAL_STATES


def pr_is_merged(pr: dict[str, Any]) -> bool:
    if pr.get("merged") is True:
        return True
    if str(pr.get("state", "")).upper() == "MERGED":
        return True
    return bool(pr.get("mergedAt") or pr.get("merged_at"))


def pr_head_sha(pr: dict[str, Any]) -> str | None:
    sha = pr.get("headRefOid") or pr.get("head_sha")
    return sha if isinstance(sha, str) and SHA40.fullmatch(sha) else None


def pr_merge_commit(pr: dict[str, Any]) -> str | None:
    value = pr.get("mergeCommit") or pr.get("merge_commit")
    if isinstance(value, dict):
        value = value.get("oid") or value.get("sha")
    return value if isinstance(value, str) and SHA40.fullmatch(value) else None


def closure_blockers(
    task: dict[str, Any], binding: PullRequestBinding, pr: dict[str, Any]
) -> list[str]:
    blockers: list[str] = []
    if task_is_terminal(task):
        blockers.append("task is already terminal")
    if not pr_is_merged(pr):
        blockers.append("pull request is not merged")
    head_sha = pr_head_sha(pr)
    if binding.expected_head_sha and head_sha != binding.expected_head_sha:
        blockers.append("pull request head sha does not match binding")
    if binding.post_merge_required:
        blockers.append("post-merge validation is explicitly required")
    return blockers


def closure_receipt(
    task: dict[str, Any],
    binding: PullRequestBinding,
    pr: dict[str, Any],
    *,
    observed_at: str | None = None,
) -> dict[str, Any]:
    task_id = str(task.get("id", ""))
    return {
        "schema_version": 1,
        "kind": "bureau.pr_closure_receipt",
        "task_id": task_id,
        "outcome": "completed-by-merged-pr",
        "observed_at": observed_at or utc_now(),
        "evidence": {
            "repo": binding.repo,
            "pr_number": binding.number,
            "pr_url": pr.get("url"),
            "pr_state": pr.get("state"),
            "merged_at": pr.get("mergedAt") or pr.get("merged_at"),
            "head_sha": pr_head_sha(pr),
            "expected_head_sha": binding.expected_head_sha,
            "merge_commit": pr_merge_commit(pr),
            "post_merge_required": binding.post_merge_required,
            "auto_verify": binding.auto_verify,
            "non_claims": list(binding.non_claims)
            or [
                "merged PR evidence does not prove deployment",
                "merged PR evidence does not prove dependent Bureau tasks are complete",
            ],
        },
    }


def scan_tasks(
    root: Path,
    pr_lookup: Callable[[PullRequestBinding], dict[str, Any] | None],
    *,
    observed_at: str | None = None,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for path in task_files(root):
        task = load_json(path)
        binding = pr_binding(task)
        if binding is None:
            continue
        pr = pr_lookup(binding)
        if pr is None:
            findings.append(
                {
                    "task_id": task.get("id"),
                    "task_path": str(path.relative_to(root)),
                    "repo": binding.repo,
                    "pr_number": binding.number,
                    "close_ready": False,
                    "blockers": ["pull request state unavailable"],
                }
            )
            continue
        blockers = closure_blockers(task, binding, pr)
        ready = not blockers
        finding: dict[str, Any] = {
            "task_id": task.get("id"),
            "task_path": str(path.relative_to(root)),
            "repo": binding.repo,
            "pr_number": binding.number,
            "close_ready": ready,
            "auto_verify": binding.auto_verify,
            "blockers": blockers,
        }
        if ready:
            finding["receipt"] = closure_receipt(task, binding, pr, observed_at=observed_at)
        findings.append(finding)
    return findings


def apply_auto_verify(
    root: Path, findings: list[dict[str, Any]], *, observed_at: str | None = None
) -> list[str]:
    changed: list[str] = []
    stamp = observed_at or utc_now()
    for finding in findings:
        if not finding.get("close_ready") or not finding.get("auto_verify"):
            continue
        path = root / str(finding["task_path"])
        task = load_json(path)
        if task_is_terminal(task):
            continue
        task["state"] = "verified"
        metadata = task.setdefault("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
            task["metadata"] = metadata
        verification = metadata.setdefault("verification", {})
        if not isinstance(verification, dict):
            verification = {}
            metadata["verification"] = verification
        verification["pr_closure"] = finding["receipt"]
        verification["verified_at"] = stamp
        dump_json(path, task)
        changed.append(str(path.relative_to(root)))
    return changed


def gh_pr_lookup(binding: PullRequestBinding) -> dict[str, Any] | None:
    args = [
        "gh",
        "pr",
        "view",
        str(binding.number),
        "--repo",
        binding.repo,
        "--json",
        "number,state,mergedAt,mergeCommit,headRefOid,baseRefOid,url,title,headRefName,baseRefName",
    ]
    completed = subprocess.run(args, check=False, capture_output=True, text=True, timeout=30)
    if completed.returncode != 0:
        return None
    value = json.loads(completed.stdout)
    return value if isinstance(value, dict) else None


def file_pr_lookup(state_dir: Path) -> Callable[[PullRequestBinding], dict[str, Any] | None]:
    def lookup(binding: PullRequestBinding) -> dict[str, Any] | None:
        safe_repo = binding.repo.replace("/", "__")
        for path in (
            state_dir / f"{safe_repo}__{binding.number}.json",
            state_dir / f"{binding.number}.json",
        ):
            if path.exists():
                return load_json(path)
        return None

    return lookup


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="Bureau repository root")
    parser.add_argument(
        "--pr-state-dir",
        default=None,
        help="Read PR JSON from a directory instead of querying GitHub",
    )
    parser.add_argument("--apply", action="store_true", help="Apply auto_verify task closures")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root).resolve()
    lookup = (
        file_pr_lookup(Path(args.pr_state_dir).resolve()) if args.pr_state_dir else gh_pr_lookup
    )
    observed_at = utc_now()
    findings = scan_tasks(root, lookup, observed_at=observed_at)
    changed = apply_auto_verify(root, findings, observed_at=observed_at) if args.apply else []
    result = {
        "schema_version": 1,
        "observed_at": observed_at,
        "findings": findings,
        "changed": changed,
    }
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for finding in findings:
            status = "READY" if finding.get("close_ready") else "BLOCKED"
            print(f"{status} {finding['task_id']} {finding['repo']}#{finding['pr_number']}")
            for blocker in finding.get("blockers", []):
                print(f"  - {blocker}")
        if changed:
            print("changed:")
            for path in changed:
                print(f"  - {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
