from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
LANE_STATES = {
    "discovered",
    "bound",
    "planned",
    "ready",
    "active",
    "reviewing",
    "needs_revision",
    "ci_failed",
    "merge_candidate",
    "merge_ready",
    "merged",
    "verified",
    "closed",
    "blocked",
    "obsolete",
    "paused",
}
AGENT_BRIEF_REQUIRED_FIELDS = (
    "goal",
    "context_summary",
    "target_files_or_search_scope",
    "acceptance_criteria",
    "non_goals",
    "allowed_changes",
    "forbidden_changes",
    "validation_commands",
    "expected_handoff_format",
)
DEFAULT_WIP_LIMITS = {
    "max_active_coding_lanes": 2,
    "max_review_lanes": 1,
    "max_merge_lanes": 1,
    "max_same_repo_mutating_lanes": 1,
    "max_new_ready_promotions_per_cycle": 1,
    "max_revision_rounds_before_manual_review": 3,
    "max_selected_lanes": 4,
}
STATE_PRIORITY = {
    "merge_ready": 100,
    "merge_candidate": 90,
    "ci_failed": 80,
    "needs_revision": 75,
    "reviewing": 70,
    "active": 60,
    "ready": 50,
    "planned": 40,
    "bound": 30,
    "discovered": 20,
    "blocked": 10,
    "paused": 0,
    "obsolete": -10,
    "merged": -20,
    "verified": -30,
    "closed": -40,
}
MUTATING_STATES = {"planned", "ready", "active", "needs_revision", "ci_failed"}
REVIEW_STATES = {"reviewing", "needs_revision", "ci_failed"}
MERGE_STATES = {"merge_candidate", "merge_ready"}
CANONICAL_TASK_REQUIRED_STATES = MUTATING_STATES | REVIEW_STATES | MERGE_STATES
CANONICAL_BUREAU_TASK_ID_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+$")
UNBOUND_NEXT_ACTION = "bind to canonical Bureau task before dispatch"


@dataclass(frozen=True)
class RepositorySource:
    name: str
    root: Path
    source_id: str


def github_repo_slug_from_remote_url(remote_url: str | None) -> str | None:
    if not isinstance(remote_url, str):
        return None
    value = remote_url.strip()
    if not value:
        return None
    patterns = (
        r"^git@github\.com:([^/]+)/([^/]+?)(?:\.git)?/?$",
        r"^ssh://(?:git@)?github\.com(?::\d+)?/([^/]+)/([^/]+?)(?:\.git)?/?$",
        r"^https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$",
    )
    for pattern in patterns:
        match = re.match(pattern, value)
        if match:
            owner, repo = match.groups()
            if owner and repo:
                return f"{owner}/{repo}"
    return None


def github_repo_slug(repo: Path) -> str | None:
    return github_repo_slug_from_remote_url(git_stdout(repo, "config", "--get", "remote.origin.url"))


def _upper_or_unknown(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip().upper()
    return "UNKNOWN"


def _normalise_open_pull_request(raw: dict[str, Any]) -> dict[str, Any] | None:
    number = raw.get("number") or raw.get("pr")
    if not isinstance(number, int):
        try:
            number = int(str(number))
        except (TypeError, ValueError):
            return None
    branch = raw.get("headRefName") or raw.get("head_ref_name") or raw.get("branch")
    if not isinstance(branch, str) or not branch.strip():
        return None
    merge_state = _upper_or_unknown(raw.get("mergeStateStatus") or raw.get("merge_state_status"))
    review_decision = _upper_or_unknown(raw.get("reviewDecision") or raw.get("review_decision"))
    is_draft = bool(raw.get("isDraft") or raw.get("is_draft"))
    return {
        "pr": number,
        "pr_title": str(raw.get("title") or raw.get("pr_title") or ""),
        "pr_url": str(raw.get("url") or raw.get("pr_url") or ""),
        "branch": branch.strip(),
        "head_ref_name": branch.strip(),
        "observed_github_state": {
            "state": "open",
            "merge_state_status": merge_state,
            "review_decision": review_decision,
            "is_draft": is_draft,
            "source": "gh pr list --state open",
        },
    }


def list_open_pull_requests(repo: Path) -> list[dict[str, Any]]:
    slug = github_repo_slug(repo)
    if slug is None:
        return []
    env = {**os.environ, "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1", "PAGER": "cat"}
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                slug,
                "--state",
                "open",
                "--json",
                "number,title,url,headRefName,isDraft,reviewDecision,mergeStateStatus",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode:
        return []
    try:
        raw_values = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(raw_values, list):
        return []
    normalised: list[dict[str, Any]] = []
    for raw in raw_values:
        if not isinstance(raw, dict):
            continue
        pr = _normalise_open_pull_request(raw)
        if pr is not None:
            normalised.append(pr)
    return normalised


def open_pull_request_lane_state(pr: dict[str, Any]) -> tuple[str, float, str]:
    observed = pr.get("observed_github_state")
    if not isinstance(observed, dict):
        observed = {}
    merge_state = _upper_or_unknown(observed.get("merge_state_status"))
    review_decision = _upper_or_unknown(observed.get("review_decision"))
    is_draft = bool(observed.get("is_draft"))
    if merge_state == "DIRTY":
        return (
            "needs_revision",
            0.35,
            "resolve observed GitHub pull-request conflicts before review handoff",
        )
    if merge_state in {"UNSTABLE", "UNKNOWN"}:
        return (
            "ci_failed",
            0.4,
            "inspect observed GitHub checks before advancing this pull request",
        )
    if is_draft:
        return "reviewing", 0.6, "continue draft pull-request review; do not merge"
    if merge_state == "CLEAN" and review_decision == "APPROVED":
        return (
            "merge_candidate",
            0.9,
            "hand to merge gatekeeper after independently verifying checks and reviews",
        )
    if merge_state == "CLEAN":
        return "reviewing", 0.7, "wait for or request review before merge-gatekeeper handoff"
    return "reviewing", 0.5, "inspect observed GitHub pull-request state before dispatch"


def apply_open_pull_request(candidate: dict[str, Any], pr: dict[str, Any]) -> dict[str, Any]:
    state, finishability, next_action = open_pull_request_lane_state(pr)
    candidate.update(
        {
            "pr": pr.get("pr"),
            "pr_title": pr.get("pr_title"),
            "pr_url": pr.get("pr_url"),
            "observed_github_state": pr.get("observed_github_state"),
            "proposed_state": state,
            "finishability": finishability,
            "next_best_action": next_action,
            "risk": "medium",
        }
    )
    return candidate


def open_pull_request_candidate(source: RepositorySource, pr: dict[str, Any]) -> dict[str, Any]:
    state, finishability, next_action = open_pull_request_lane_state(pr)
    candidate = {
        "kind": "open_pull_request",
        "repo": str(source.root),
        "repo_name": source.name,
        "source_id": source.source_id,
        "branch": pr.get("branch"),
        "pr": pr.get("pr"),
        "pr_title": pr.get("pr_title"),
        "pr_url": pr.get("pr_url"),
        "observed_github_state": pr.get("observed_github_state"),
        "proposed_state": state,
        "finishability": finishability,
        "next_best_action": next_action,
        "risk": "medium",
    }
    candidate["fingerprint"] = candidate_fingerprint(candidate)
    return candidate


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def git(repo: Path, *arguments: str, timeout: int = 10) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_PAGER": "cat",
        "PAGER": "cat",
    }
    return subprocess.run(
        [
            "git",
            "--no-pager",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "diff.external=",
            "-c",
            "core.attributesFile=/dev/null",
            "-C",
            str(repo),
            *arguments,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def git_stdout(repo: Path, *arguments: str) -> str | None:
    result = git(repo, *arguments)
    if result.returncode:
        return None
    return result.stdout.strip()


def default_state_root() -> Path:
    return Path(
        os.environ.get("BUREAU_CLOSURE_STATE_ROOT", Path.home() / ".local/state/bureau-closure")
    ).expanduser()


def default_source_registry() -> Path:
    return Path(
        os.environ.get(
            "BUREAU_DISCOVERY_REGISTRY",
            Path.home() / ".local/state/bureau-halfhour-operator/source-registry.json",
        )
    ).expanduser()


def load_repository_sources(path: Path | None = None) -> list[RepositorySource]:
    registry = load_json(path or default_source_registry(), {})
    values: list[RepositorySource] = []
    if isinstance(registry, dict):
        for item in registry.get("repositories", []):
            if not isinstance(item, dict) or not item.get("enabled", True):
                continue
            root = item.get("root")
            if not isinstance(root, str):
                continue
            repo_root = Path(root).expanduser()
            if not (repo_root / ".git").exists():
                continue
            values.append(
                RepositorySource(
                    name=str(item.get("name") or repo_root.name),
                    root=repo_root,
                    source_id=str(item.get("source_id") or f"repo:{repo_root.name}"),
                )
            )
    if values:
        return values
    fallback = Path.home() / "repos"
    return (
        [
            RepositorySource(path.name, path, f"repo:{path.name}")
            for path in sorted(fallback.iterdir())
            if (path / ".git").exists()
        ]
        if fallback.is_dir()
        else []
    )


def main_branch(repo: Path) -> str | None:
    origin_head = git_stdout(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if origin_head and "/" in origin_head:
        return origin_head.split("/", 1)[1]
    for candidate in ("main", "master"):
        if git(repo, "show-ref", "--verify", f"refs/heads/{candidate}").returncode == 0:
            return candidate
        if git(repo, "show-ref", "--verify", f"refs/remotes/origin/{candidate}").returncode == 0:
            return candidate
    return None


def branch_is_merged(repo: Path, branch: str, base: str | None) -> bool:
    if base is None:
        return False
    target = f"origin/{base}"
    if git(repo, "rev-parse", "--verify", target).returncode != 0:
        target = base
    return git(repo, "merge-base", "--is-ancestor", branch, target).returncode == 0


def local_branches(repo: Path) -> list[dict[str, Any]]:
    output = git_stdout(
        repo,
        "for-each-ref",
        "--format=%(refname:short)%09%(objectname)%09%(upstream:short)",
        "refs/heads",
    )
    if not output:
        return []
    base = main_branch(repo)
    result: list[dict[str, Any]] = []
    for line in output.splitlines():
        parts = line.split("\t")
        branch = parts[0]
        if branch in {"main", "master"}:
            continue
        result.append(
            {
                "branch": branch,
                "head": parts[1] if len(parts) > 1 else None,
                "upstream": parts[2] if len(parts) > 2 else None,
                "merged": branch_is_merged(repo, branch, base),
                "base": base,
            }
        )
    return result


def worktrees(repo: Path) -> list[dict[str, Any]]:
    output = git_stdout(repo, "worktree", "list", "--porcelain")
    if not output:
        return []
    items: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in [*output.splitlines(), ""]:
        if not line:
            if current:
                path = Path(str(current.get("path", ""))).expanduser()
                if path.exists():
                    current["dirty"] = bool(git_stdout(path, "status", "--porcelain"))
                items.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current["path"] = value
        elif key == "HEAD":
            current["head"] = value
        elif key == "branch":
            current["branch"] = value.replace("refs/heads/", "")
        elif key == "detached":
            current["detached"] = True
    return items


def recent_failed_tasks(task_db: Path, horizon_seconds: int = 3 * 60 * 60) -> list[dict[str, Any]]:
    if not task_db.is_file():
        return []
    now = int(datetime.now(timezone.utc).timestamp())
    threshold = now - horizon_seconds
    try:
        connection = sqlite3.connect(f"file:{task_db}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT task_id,state,cwd,updated_at_unix,unit
            FROM tasks
            WHERE state='failed' AND updated_at_unix>=?
            ORDER BY updated_at_unix DESC
            """,
            (threshold,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()
    return [dict(row) for row in rows]


def candidate_fingerprint(candidate: dict[str, Any]) -> str:
    return hashlib.sha256(
        "\0".join(
            str(candidate.get(key, ""))
            for key in ("kind", "repo", "branch", "pr", "task_id", "source")
        ).encode("utf-8")
    ).hexdigest()


def inventory_existing_work(
    repositories: list[RepositorySource],
    *,
    task_db: Path | None = None,
    max_repositories: int | None = None,
) -> dict[str, Any]:
    selected = repositories[:max_repositories] if max_repositories else repositories
    candidates: list[dict[str, Any]] = []
    for source in selected:
        open_pull_requests = list_open_pull_requests(source.root)
        open_prs_by_branch = {
            str(pr.get("branch")): pr for pr in open_pull_requests if pr.get("branch")
        }
        matched_open_pr_numbers: set[int] = set()
        for branch in local_branches(source.root):
            open_pr = open_prs_by_branch.get(str(branch["branch"]))
            if open_pr is None and branch["merged"]:
                state = "obsolete"
                next_action = "confirm branch is merged and archive lane"
                finishability = 0.95
            else:
                state = "planned"
                next_action = "bind branch to canonical task or create planned closure task"
                finishability = 0.55
            candidate = {
                "kind": "branch",
                "repo": str(source.root),
                "repo_name": source.name,
                "source_id": source.source_id,
                "branch": branch["branch"],
                "head": branch["head"],
                "upstream": branch["upstream"],
                "base": branch["base"],
                "merged": branch["merged"],
                "proposed_state": state,
                "finishability": finishability,
                "next_best_action": next_action,
                "risk": "low" if branch["merged"] else "medium",
            }
            if open_pr is not None:
                candidate = apply_open_pull_request(candidate, open_pr)
                if isinstance(open_pr.get("pr"), int):
                    matched_open_pr_numbers.add(open_pr["pr"])
            candidate["fingerprint"] = candidate_fingerprint(candidate)
            candidates.append(candidate)
        for open_pr in open_pull_requests:
            if open_pr.get("pr") in matched_open_pr_numbers:
                continue
            candidates.append(open_pull_request_candidate(source, open_pr))
        for tree in worktrees(source.root):
            branch = tree.get("branch")
            if not branch or branch in {"main", "master"}:
                continue
            if not tree.get("dirty"):
                continue
            candidate = {
                "kind": "dirty_worktree",
                "repo": str(source.root),
                "repo_name": source.name,
                "source_id": source.source_id,
                "branch": branch,
                "worktree": tree.get("path"),
                "head": tree.get("head"),
                "proposed_state": "active",
                "finishability": 0.45,
                "next_best_action": (
                    "review dirty worktree and bind it to an existing task before dispatch"
                ),
                "risk": "medium",
            }
            candidate["fingerprint"] = candidate_fingerprint(candidate)
            candidates.append(candidate)
    repo_by_root = {str(source.root): source for source in selected}
    for task in recent_failed_tasks(
        task_db or Path.home() / ".local/state/grabowski/tasks.sqlite3"
    ):
        cwd = str(task.get("cwd") or "")
        source = next((item for root, item in repo_by_root.items() if cwd.startswith(root)), None)
        if source is None:
            continue
        candidate = {
            "kind": "recent_failed_task",
            "repo": str(source.root),
            "repo_name": source.name,
            "source_id": source.source_id,
            "branch": git_stdout(Path(cwd), "branch", "--show-current")
            if Path(cwd).exists()
            else None,
            "task_id": task.get("task_id"),
            "unit": task.get("unit"),
            "proposed_state": "ci_failed",
            "finishability": 0.35,
            "next_best_action": "inspect failed task logs and continue the same lane if canonical",
            "risk": "medium",
        }
        candidate["fingerprint"] = candidate_fingerprint(candidate)
        candidates.append(candidate)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "repository_count": len(selected),
        "candidate_count": len(candidates),
        "candidates": sorted(
            candidates,
            key=lambda item: (
                -float(item.get("finishability", 0)),
                item.get("repo_name", ""),
                item.get("branch") or "",
                item.get("kind", ""),
            ),
        ),
    }


def read_manual_intents(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    values: list[dict[str, Any]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            values.append({"line": lineno, "invalid": True, "raw": line[:200]})
            continue
        if isinstance(value, dict):
            values.append(value)
    return values


def lane_id_for(candidate: dict[str, Any]) -> str:
    stem = "-".join(
        part
        for part in (
            str(candidate.get("repo_name") or Path(str(candidate.get("repo", "repo"))).name),
            str(candidate.get("branch") or candidate.get("task_id") or candidate.get("kind")),
        )
        if part
    )
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in stem).strip("-")
    return f"lane-{safe[:72]}-{candidate['fingerprint'][:10]}"


def initial_lane_state(candidate: dict[str, Any]) -> str:
    proposed = str(candidate.get("proposed_state") or "planned")
    return proposed if proposed in LANE_STATES else "planned"


def apply_manual_priority(lane: dict[str, Any], intents: list[dict[str, Any]]) -> dict[str, Any]:
    target_strings = {
        str(lane.get("lane_id")),
        str(lane.get("task_id")),
        str(lane.get("repo")),
        str(lane.get("branch")),
        str(lane.get("pr")),
    }
    matched: list[dict[str, Any]] = []
    for intent in intents:
        target = str(intent.get("target", ""))
        if not target:
            continue
        if any(target and (target == value or target in value) for value in target_strings):
            matched.append(intent)
    if matched:
        lane["manual_intents"] = matched[-5:]
        lane["manual_priority"] = matched[-1].get("priority", "normal")
    return lane


def default_bureau_registry_root() -> Path | None:
    configured = os.environ.get("BUREAU_REGISTRY_ROOT")
    if configured:
        return Path(configured).expanduser()
    cwd = Path.cwd()
    if (cwd / "registry/tasks").is_dir():
        return cwd
    fallback = Path.home() / "repos/bureau"
    if (fallback / "registry/tasks").is_dir():
        return fallback
    return None


def load_canonical_task_states(registry_root: Path | None = None) -> dict[str, str]:
    root = registry_root or default_bureau_registry_root()
    if root is None:
        return {}
    task_dir = root / "registry/tasks"
    if not task_dir.is_dir():
        return {}
    states: dict[str, str] = {}
    for path in sorted(task_dir.glob("*.json")):
        raw = load_json(path, None)
        if not isinstance(raw, dict):
            continue
        task_id = raw.get("id")
        state = raw.get("state")
        if isinstance(task_id, str) and isinstance(state, str):
            states[task_id] = state
    return states


def apply_canonical_task_state(
    lane: dict[str, Any], task_states: dict[str, str]
) -> dict[str, Any]:
    task_id = lane.get("task_id")
    if not isinstance(task_id, str) or not CANONICAL_BUREAU_TASK_ID_RE.fullmatch(task_id):
        return lane
    task_state = task_states.get(task_id)
    if not task_state:
        return lane
    metadata = lane.setdefault("metadata", {})
    if isinstance(metadata, dict):
        metadata["canonical_task_state"] = task_state
    if task_state == "verified":
        lane["state"] = "verified"
        lane["next_action"] = "canonical Bureau task is verified; do not select lane"
    elif task_state in {"cancelled", "superseded"}:
        lane["state"] = "obsolete"
        lane["next_action"] = "canonical Bureau task is terminal; inspect before continuing"
    return lane


def merge_lanes(
    inventory: dict[str, Any],
    existing: dict[str, Any] | None = None,
    manual_intents: list[dict[str, Any]] | None = None,
    canonical_task_states: dict[str, str] | None = None,
) -> dict[str, Any]:
    previous = existing if isinstance(existing, dict) else {}
    by_fingerprint = {
        lane.get("fingerprint"): lane
        for lane in previous.get("lanes", [])
        if isinstance(lane, dict) and lane.get("fingerprint")
    }
    lanes: list[dict[str, Any]] = []
    seen: set[str] = set()
    intents = manual_intents or []
    for candidate in inventory.get("candidates", []):
        fingerprint = candidate["fingerprint"]
        old = dict(by_fingerprint.get(fingerprint, {}))
        state = (
            old.get("state") if old.get("state") in LANE_STATES else initial_lane_state(candidate)
        )
        if state in {"closed", "verified", "merged"} and not candidate.get("merged"):
            state = "needs_revision"
        lane = {
            **old,
            "schema_version": SCHEMA_VERSION,
            "lane_id": old.get("lane_id") or lane_id_for(candidate),
            "fingerprint": fingerprint,
            "state": state,
            "repo": candidate.get("repo"),
            "repo_name": candidate.get("repo_name"),
            "branch": candidate.get("branch"),
            "pr": candidate.get("pr"),
            "pr_title": candidate.get("pr_title"),
            "pr_url": candidate.get("pr_url"),
            "observed_github_state": candidate.get("observed_github_state"),
            "task_id": old.get("task_id") or candidate.get("task_id"),
            "source_candidate": candidate,
            "risk": candidate.get("risk", "medium"),
            "finishability": candidate.get("finishability", 0.0),
            "next_action": candidate.get("next_best_action"),
            "updated_at": utc_now(),
        }
        lane = apply_manual_priority(lane, intents)
        lane = apply_canonical_task_state(lane, canonical_task_states or {})
        lanes.append(lane)
        seen.add(fingerprint)
    for lane in previous.get("lanes", []):
        if isinstance(lane, dict) and lane.get("fingerprint") not in seen:
            retained = dict(lane)
            if retained.get("state") not in {"closed", "merged", "verified", "obsolete"}:
                retained["state"] = "blocked"
                retained["next_action"] = "source candidate disappeared; inspect before continuing"
                retained["updated_at"] = utc_now()
            retained = apply_manual_priority(retained, intents)
            retained = apply_canonical_task_state(retained, canonical_task_states or {})
            lanes.append(retained)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "lane_count": len(lanes),
        "lanes": sorted(lanes, key=lane_sort_key),
    }


def lane_sort_key(lane: dict[str, Any]) -> tuple[Any, ...]:
    manual_boost = 20 if lane.get("manual_priority") in {"high", "urgent"} else 0
    return (
        -(STATE_PRIORITY.get(str(lane.get("state")), 0) + manual_boost),
        -float(lane.get("finishability") or 0),
        str(lane.get("repo_name") or ""),
        str(lane.get("branch") or ""),
    )


def is_canonical_bureau_task_id(value: Any) -> bool:
    return isinstance(value, str) and bool(CANONICAL_BUREAU_TASK_ID_RE.fullmatch(value))


def lane_requires_canonical_task_id(lane: dict[str, Any]) -> bool:
    return str(lane.get("state")) in CANONICAL_TASK_REQUIRED_STATES


def rejected_unbound_lane(lane: dict[str, Any]) -> dict[str, Any]:
    return {
        "lane_id": lane.get("lane_id"),
        "repo_name": lane.get("repo_name"),
        "branch": lane.get("branch"),
        "state": lane.get("state"),
        "task_id": lane.get("task_id"),
        "reason": "missing_canonical_bureau_task_id",
    }


def select_lanes_for_plan_with_evidence(
    lanes: list[dict[str, Any]], limits: dict[str, int] | None = None
) -> dict[str, Any]:
    selected: list[dict[str, Any]] = []
    rejected_unbound: list[dict[str, Any]] = []
    limits = {**DEFAULT_WIP_LIMITS, **(limits or {})}
    per_repo: dict[str, int] = {}
    coding = review = merge = 0
    for lane in sorted(lanes, key=lane_sort_key):
        if len(selected) >= limits["max_selected_lanes"]:
            break
        state = str(lane.get("state"))
        if state in {"closed", "verified", "merged", "obsolete", "paused", "blocked"}:
            continue
        if lane_requires_canonical_task_id(lane) and not is_canonical_bureau_task_id(
            lane.get("task_id")
        ):
            lane["next_action"] = UNBOUND_NEXT_ACTION
            blockers = lane.setdefault("selection_blockers", [])
            if "missing_canonical_bureau_task_id" not in blockers:
                blockers.append("missing_canonical_bureau_task_id")
            rejected_unbound.append(rejected_unbound_lane(lane))
            continue
        repo = str(lane.get("repo"))
        if state in MUTATING_STATES:
            if coding >= limits["max_active_coding_lanes"]:
                continue
            if per_repo.get(repo, 0) >= limits["max_same_repo_mutating_lanes"]:
                continue
            coding += 1
            per_repo[repo] = per_repo.get(repo, 0) + 1
        if state in REVIEW_STATES:
            if review >= limits["max_review_lanes"]:
                continue
            review += 1
        if state in MERGE_STATES:
            if merge >= limits["max_merge_lanes"]:
                continue
            merge += 1
        selected.append(lane)
        if len(selected) >= limits["max_selected_lanes"]:
            break
        if (
            coding >= limits["max_active_coding_lanes"]
            and review >= limits["max_review_lanes"]
            and merge >= limits["max_merge_lanes"]
        ):
            break
    return {
        "selected_lanes": selected,
        "rejected_unbound_lanes": rejected_unbound,
        "unbound_selected_rejected_count": len(rejected_unbound),
        "canonical_task_bound_count": sum(
            1 for lane in selected if is_canonical_bureau_task_id(lane.get("task_id"))
        ),
    }


def select_lanes_for_plan(
    lanes: list[dict[str, Any]], limits: dict[str, int] | None = None
) -> list[dict[str, Any]]:
    return select_lanes_for_plan_with_evidence(lanes, limits)["selected_lanes"]


def brief_for_lane(lane: dict[str, Any]) -> dict[str, Any]:
    source = (
        lane.get("source_candidate", {}) if isinstance(lane.get("source_candidate"), dict) else {}
    )
    validation = ["git status --short", "run the task-specific focused tests from the owning repo"]
    if lane.get("state") in {"merge_candidate", "merge_ready"}:
        validation.append("verify CI is green and review threads are resolved before merge")
    return {
        "schema_version": SCHEMA_VERSION,
        "brief_type": "grabowski_closure_lane_brief",
        "lane_id": lane["lane_id"],
        "task_id": lane.get("task_id"),
        "repo": lane.get("repo"),
        "branch": lane.get("branch"),
        "pr": lane.get("pr"),
        "pr_title": lane.get("pr_title"),
        "pr_url": lane.get("pr_url"),
        "observed_github_state": lane.get("observed_github_state"),
        "state": lane.get("state"),
        "goal": lane.get("next_action") or "advance this closure lane toward merge-ready state",
        "context_summary": (
            "Existing Bureau work must be continued, reviewed, repaired, or prepared for merge. "
            "Do not start unrelated work. Preserve claims and receipts."
        ),
        "target_files_or_search_scope": [
            item for item in [lane.get("repo"), lane.get("branch"), source.get("worktree")] if item
        ],
        "acceptance_criteria": [
            "changes are bound to this lane and task identity",
            "all relevant focused tests are run or a blocker is recorded",
            "no foreign claim or unrelated dirty worktree is overwritten",
            "a receipt or handoff note records result, evidence, and next state",
        ],
        "non_goals": [
            "do not create unrelated discovery tasks",
            "do not merge without merge-gatekeeper evidence",
            "do not weaken tests or safety gates",
        ],
        "allowed_changes": [
            "minimal changes required to advance the lane",
            "tests and documentation directly tied to acceptance criteria",
        ],
        "forbidden_changes": [
            "secret exposure",
            "unbounded repository rewrites",
            "force-push to protected branches",
            "modifying unrelated worktrees",
        ],
        "validation_commands": validation,
        "expected_handoff_format": {
            "summary": "what changed or why blocked",
            "tests": "commands and outcomes",
            "lane_state": "recommended next lane state",
            "evidence": "paths, commits, PRs, or receipts",
        },
        "created_at": utc_now(),
        "source_candidate_fingerprint": lane.get("fingerprint"),
    }


def validate_brief(brief: dict[str, Any]) -> list[str]:
    missing = [field for field in AGENT_BRIEF_REQUIRED_FIELDS if field not in brief]
    empty = [
        field for field in AGENT_BRIEF_REQUIRED_FIELDS if brief.get(field) in (None, "", [], {})
    ]
    return [
        *(f"missing field: {field}" for field in missing),
        *(f"empty field: {field}" for field in empty),
    ]


def write_briefs(lanes: list[dict[str, Any]], brief_root: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for lane in lanes:
        brief = brief_for_lane(lane)
        errors = validate_brief(brief)
        if errors:
            result.append({"lane_id": lane.get("lane_id"), "valid": False, "errors": errors})
            continue
        path = brief_root / f"{lane['lane_id']}.json"
        atomic_json(path, brief)
        lane["grabowski_brief"] = str(path)
        result.append(
            {
                "lane_id": lane["lane_id"],
                "valid": True,
                "path": str(path),
                "sha256": sha256_json(brief),
            }
        )
    return result


def run_closure_cycle(
    *,
    state_root: Path | None = None,
    source_registry: Path | None = None,
    max_repositories: int | None = None,
) -> dict[str, Any]:
    state = state_root or default_state_root()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    sources = load_repository_sources(source_registry)
    inventory = inventory_existing_work(sources, max_repositories=max_repositories)
    intents_path = state / "manual-intents.jsonl"
    intents = read_manual_intents(intents_path)
    existing = load_json(state / "lanes.json", {})
    canonical_task_states = load_canonical_task_states()
    lanes = merge_lanes(inventory, existing, intents, canonical_task_states)
    selection = select_lanes_for_plan_with_evidence(lanes["lanes"])
    selected = selection["selected_lanes"]
    briefs = write_briefs(selected, state / "briefs")
    plan = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "wip_limits": DEFAULT_WIP_LIMITS,
        "manual_intent_count": len(intents),
        "inventory_path": str(state / "inventory.json"),
        "lanes_path": str(state / "lanes.json"),
        "selected_lane_count": len(selected),
        "unbound_selected_rejected_count": selection["unbound_selected_rejected_count"],
        "rejected_unbound_lanes": selection["rejected_unbound_lanes"],
        "canonical_task_bound_count": selection["canonical_task_bound_count"],
        "canonical_task_state_count": len(canonical_task_states),
        "selected_lanes": selected,
        "briefs": briefs,
        "next_action": (
            "delegate only selected lanes with valid Grabowski briefs; "
            "prefer closure before new discovery"
        ),
    }
    atomic_json(state / "inventory.json", inventory)
    atomic_json(state / "lanes.json", lanes)
    atomic_json(state / "plan.json", plan)
    return plan


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="bureau-closure")
    result.add_argument("command", choices=["run", "inventory", "brief-check"])
    result.add_argument("--state-root")
    result.add_argument("--source-registry")
    result.add_argument("--max-repositories", type=int)
    result.add_argument("--brief")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    state = Path(args.state_root).expanduser() if args.state_root else None
    registry = Path(args.source_registry).expanduser() if args.source_registry else None
    if args.command == "run":
        value = run_closure_cycle(
            state_root=state,
            source_registry=registry,
            max_repositories=args.max_repositories,
        )
    elif args.command == "inventory":
        value = inventory_existing_work(
            load_repository_sources(registry),
            max_repositories=args.max_repositories,
        )
    elif args.command == "brief-check":
        if not args.brief:
            raise SystemExit("--brief is required")
        brief = load_json(Path(args.brief).expanduser(), None)
        value = {
            "valid": isinstance(brief, dict) and not validate_brief(brief),
            "errors": validate_brief(brief)
            if isinstance(brief, dict)
            else ["brief is not an object"],
        }
    else:
        raise AssertionError(args.command)
    print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
