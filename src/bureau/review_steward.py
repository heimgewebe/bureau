from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bureau.closure import (
    CANONICAL_TASK_REQUIRED_STATES,
    atomic_json,
    is_canonical_bureau_task_id,
    load_json,
    sha256_json,
    utc_now,
    validate_brief,
)

SCHEMA_VERSION = 1
REVIEW_STATES = {
    "reviewing",
    "needs_revision",
    "ci_failed",
    "merge_candidate",
    "blocked",
    "obsolete",
}
TERMINAL_STATES = {"closed", "merged", "verified", "obsolete"}
FAIL_CHECK_VALUES = {
    "ACTION_REQUIRED",
    "CANCELLED",
    "ERROR",
    "FAILURE",
    "FAILED",
    "STARTUP_FAILURE",
    "STALE",
    "TIMED_OUT",
}
PASS_CHECK_VALUES = {"COMPLETED", "NEUTRAL", "PASSED", "SKIPPED", "SUCCESS"}
APPROVED_REVIEW_VALUES = {"APPROVED"}
REVISION_REVIEW_VALUES = {"CHANGES_REQUESTED"}

PrStatusProvider = Callable[[dict[str, Any]], dict[str, Any]]


def default_state_root() -> Path:
    return Path.home() / ".local/state/bureau-closure"


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def run_command(cwd: Path, argv: list[str], timeout: int = 20) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            check=False,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "argv": argv,
            "returncode": None,
            "stdout": "",
            "stderr": str(exc),
            "ok": False,
        }
    return {
        "argv": argv,
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
        "ok": completed.returncode == 0,
    }


def repo_snapshot(repo_value: Any) -> dict[str, Any]:
    if not isinstance(repo_value, str) or not repo_value:
        return {"available": False, "reason": "missing_repo_path"}
    repo = Path(repo_value).expanduser()
    if not repo.exists():
        return {"available": False, "reason": "repo_path_absent", "repo": str(repo)}
    if (
        not (repo / ".git").exists()
        and not run_command(repo, ["git", "rev-parse", "--git-dir"])["ok"]
    ):
        return {"available": False, "reason": "not_a_git_repository", "repo": str(repo)}

    status = run_command(repo, ["git", "status", "--short"])
    branch_status = run_command(repo, ["git", "status", "--branch", "--short"])
    branch = run_command(repo, ["git", "branch", "--show-current"])
    head = run_command(repo, ["git", "rev-parse", "--short", "HEAD"])
    diff_names = run_command(repo, ["git", "diff", "--name-only", "--"])
    diff_stat = run_command(repo, ["git", "diff", "--stat", "--"])
    dirty = bool(status["stdout"])
    return {
        "available": True,
        "repo": str(repo),
        "branch": branch["stdout"] or None,
        "head": head["stdout"] or None,
        "dirty": dirty,
        "status_short": status["stdout"],
        "branch_status": branch_status["stdout"],
        "diff_files": [line for line in diff_names["stdout"].splitlines() if line],
        "diff_stat": diff_stat["stdout"],
        "commands_ok": all(
            item["ok"] for item in [status, branch_status, branch, head, diff_names, diff_stat]
        ),
    }


def load_briefs(brief_root: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if not brief_root.exists():
        return result
    for path in sorted(brief_root.glob("*.json")):
        brief = load_json(path, None)
        if not isinstance(brief, dict):
            continue
        lane_id = brief.get("lane_id")
        if isinstance(lane_id, str):
            result[lane_id] = {
                "path": str(path),
                "brief": brief,
                "valid": not validate_brief(brief),
                "errors": validate_brief(brief),
                "sha256": sha256_json(brief),
            }
    return result


def selected_lane_ids(plan: dict[str, Any], lanes: list[dict[str, Any]]) -> list[str]:
    selected = plan.get("selected_lanes") if isinstance(plan, dict) else None
    ids = [lane.get("lane_id") for lane in selected or [] if isinstance(lane, dict)]
    ids = [item for item in ids if isinstance(item, str)]
    if ids:
        return ids
    fallback = []
    for lane in lanes:
        state = str(lane.get("state"))
        if state not in TERMINAL_STATES and state != "paused":
            lane_id = lane.get("lane_id")
            if isinstance(lane_id, str):
                fallback.append(lane_id)
    return fallback


def truthy_evidence(value: Any) -> bool:
    if value in (None, "", [], {}) or value is False:
        return False
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"missing", "none", "not run", "unknown", "n/a"}:
            return False
    return True


def nested_values(value: Any, keys: set[str]) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in keys:
                found.append(child)
            found.extend(nested_values(child, keys))
    elif isinstance(value, list):
        for child in value:
            found.extend(nested_values(child, keys))
    return found


def evidence_present(lane: dict[str, Any], keys: set[str]) -> bool:
    values: list[Any] = []
    for key in keys:
        values.append(lane.get(key))
    for container_key in (
        "evidence",
        "handoff_evidence",
        "validation_result",
        "acceptance_result",
    ):
        container = lane.get(container_key)
        if not isinstance(container, dict):
            continue
        for key in keys:
            values.append(container.get(key))
    return any(truthy_evidence(item) for item in values)


def normalize_pr_status(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict) or not raw:
        return {"available": False, "reason": "missing_pr_evidence", "checks": "unknown"}
    if raw.get("available") is False:
        return {**raw, "checks": raw.get("checks", "unknown")}

    review_decision = raw.get("reviewDecision") or raw.get("review_decision")
    state = raw.get("state")
    is_draft = bool(raw.get("isDraft") or raw.get("draft"))
    merged = bool(raw.get("merged")) or raw.get("mergeStateStatus") == "MERGED"
    rollup = raw.get("statusCheckRollup") or raw.get("checks") or []
    failures: list[str] = []
    pending: list[str] = []
    passed: list[str] = []
    if isinstance(rollup, list):
        for item in rollup:
            if not isinstance(item, dict):
                continue
            name = str(
                item.get("name") or item.get("context") or item.get("workflowName") or "check"
            )
            value = str(item.get("conclusion") or item.get("status") or "").upper()
            if value in FAIL_CHECK_VALUES:
                failures.append(name)
            elif value in PASS_CHECK_VALUES:
                passed.append(name)
            else:
                pending.append(name)
    elif isinstance(rollup, str):
        value = rollup.upper()
        if value in {"FAILED", "FAILURE"}:
            failures.append("statusCheckRollup")
        elif value in {"PASSED", "SUCCESS"}:
            passed.append("statusCheckRollup")
        elif value:
            pending.append("statusCheckRollup")

    if failures:
        checks = "failed"
    elif pending:
        checks = "pending"
    elif passed:
        checks = "passed"
    else:
        checks = raw.get("checks", "unknown")
    return {
        "available": True,
        "number": raw.get("number"),
        "url": raw.get("url"),
        "state": state,
        "is_draft": is_draft,
        "merged": merged,
        "review_decision": review_decision,
        "merge_state": raw.get("mergeStateStatus") or raw.get("merge_state"),
        "checks": checks,
        "failed_checks": failures,
        "pending_checks": pending,
        "passed_check_count": len(passed),
    }


def gh_pr_status(lane: dict[str, Any]) -> dict[str, Any]:
    pr = lane.get("pr")
    repo = lane.get("repo")
    if not pr:
        return {"available": False, "reason": "no_pr"}
    if not repo:
        return {"available": False, "reason": "missing_repo_path"}
    if shutil.which("gh") is None:
        return {"available": False, "reason": "gh_not_available"}
    result = run_command(
        Path(str(repo)).expanduser(),
        [
            "gh",
            "pr",
            "view",
            str(pr),
            "--json",
            "number,url,state,isDraft,merged,reviewDecision,mergeStateStatus,statusCheckRollup",
        ],
        timeout=30,
    )
    if not result["ok"]:
        return {
            "available": False,
            "reason": "gh_pr_view_failed",
            "stderr": result["stderr"][:1000],
        }
    try:
        return json.loads(result["stdout"])
    except json.JSONDecodeError as exc:
        return {"available": False, "reason": "gh_json_decode_failed", "error": str(exc)}


def explicit_blockers(lane: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    for key in ("blockers", "selection_blockers", "review_blockers"):
        value = lane.get(key)
        if isinstance(value, list):
            blockers.extend(str(item) for item in value if item)
        elif truthy_evidence(value):
            blockers.append(str(value))
    return sorted(set(blockers))


def classify_lane(
    lane: dict[str, Any],
    *,
    brief: dict[str, Any] | None,
    repo: dict[str, Any],
    pr: dict[str, Any],
) -> dict[str, Any]:
    previous_state = str(lane.get("state") or "")
    reasons: list[str] = []
    blockers = explicit_blockers(lane)

    if previous_state in TERMINAL_STATES:
        return {
            "state": "obsolete",
            "reasons": ["lane already terminal or obsolete"],
            "blockers": blockers,
        }
    if lane.get("source_candidate", {}).get("merged") is True:
        return {
            "state": "obsolete",
            "reasons": ["source branch is already merged"],
            "blockers": blockers,
        }

    task_id = lane.get("task_id")
    if previous_state in CANONICAL_TASK_REQUIRED_STATES and not is_canonical_bureau_task_id(
        task_id
    ):
        blockers.append("missing_canonical_bureau_task_id")
    if brief is None:
        blockers.append("missing_grabowski_brief")
    elif not brief.get("valid"):
        blockers.append("invalid_grabowski_brief")
    if not repo.get("available"):
        blockers.append(str(repo.get("reason") or "repo_unavailable"))

    if blockers:
        return {
            "state": "blocked",
            "reasons": ["hard blocker present"],
            "blockers": sorted(set(blockers)),
        }

    if pr.get("merged") is True:
        return {
            "state": "obsolete",
            "reasons": ["pull request already merged"],
            "blockers": [],
        }
    if pr.get("state") == "CLOSED":
        return {
            "state": "blocked",
            "reasons": ["pull request is closed without merged evidence"],
            "blockers": ["closed_pull_request"],
        }
    if pr.get("checks") == "failed" or previous_state == "ci_failed":
        return {
            "state": "ci_failed",
            "reasons": ["CI/check evidence is failing"],
            "blockers": [],
        }
    if pr.get("review_decision") in REVISION_REVIEW_VALUES:
        return {
            "state": "needs_revision",
            "reasons": ["review requested changes"],
            "blockers": [],
        }
    if repo.get("dirty"):
        return {
            "state": "needs_revision",
            "reasons": ["local worktree has uncommitted diff"],
            "blockers": [],
        }

    tests_present = evidence_present(
        lane, {"tests", "test_evidence", "validation", "validation_evidence"}
    )
    acceptance_present = evidence_present(
        lane, {"acceptance", "acceptance_evidence", "acceptance_criteria_evidence"}
    )
    approved = pr.get("review_decision") in APPROVED_REVIEW_VALUES
    checks_passed = pr.get("checks") == "passed"
    open_pr = pr.get("available") is True and pr.get("state") in {"OPEN", None}
    non_draft = pr.get("is_draft") is False

    if (
        open_pr
        and non_draft
        and checks_passed
        and approved
        and tests_present
        and acceptance_present
    ):
        return {
            "state": "merge_candidate",
            "reasons": ["tests, CI, review, acceptance, and clean diff evidence are present"],
            "blockers": [],
        }

    if not tests_present:
        reasons.append("missing focused test evidence")
    if not acceptance_present:
        reasons.append("missing acceptance evidence")
    if pr.get("available") is not True:
        reasons.append(f"missing PR/CI evidence: {pr.get('reason', 'unknown')}")
    elif pr.get("checks") != "passed":
        reasons.append(f"CI/check evidence is {pr.get('checks', 'unknown')}")
    if pr.get("review_decision") not in APPROVED_REVIEW_VALUES:
        reasons.append("review approval evidence missing")
    if pr.get("is_draft"):
        reasons.append("pull request is draft")

    return {
        "state": "reviewing",
        "reasons": reasons or ["no terminal review signal found"],
        "blockers": [],
    }


def next_action_for(state: str) -> str:
    if state == "merge_candidate":
        return "hand to merge gatekeeper; steward must not merge"
    if state == "ci_failed":
        return "repair failing checks within the bound lane and rerun focused validation"
    if state == "needs_revision":
        return "address review or diff issues within the bound lane"
    if state == "blocked":
        return "clear hard blockers before dispatch or merge review"
    if state == "obsolete":
        return "archive lane after confirming no live work remains"
    return "collect missing tests, CI, review, diff, and acceptance evidence"


def review_one_lane(
    lane: dict[str, Any],
    *,
    brief: dict[str, Any] | None,
    pr_status_provider: PrStatusProvider,
) -> dict[str, Any]:
    repo = repo_snapshot(lane.get("repo"))
    pr = normalize_pr_status(pr_status_provider(lane))
    classification = classify_lane(lane, brief=brief, repo=repo, pr=pr)
    state = classification["state"]
    return {
        "schema_version": SCHEMA_VERSION,
        "reviewed_at": utc_now(),
        "lane_id": lane.get("lane_id"),
        "task_id": lane.get("task_id"),
        "repo": lane.get("repo"),
        "branch": lane.get("branch"),
        "previous_state": lane.get("state"),
        "recommended_state": state,
        "reasons": classification["reasons"],
        "blockers": classification["blockers"],
        "next_action": next_action_for(state),
        "evidence": {
            "brief": brief,
            "repo": repo,
            "pr": pr,
            "tests_present": evidence_present(
                lane, {"tests", "test_evidence", "validation", "validation_evidence"}
            ),
            "acceptance_present": evidence_present(
                lane, {"acceptance", "acceptance_evidence", "acceptance_criteria_evidence"}
            ),
        },
    }


def review_closure_lanes(
    *,
    state_root: Path | None = None,
    max_lanes: int | None = None,
    pr_status_provider: PrStatusProvider = gh_pr_status,
) -> dict[str, Any]:
    state = state_root or default_state_root()
    lanes_doc = load_json(state / "lanes.json", {})
    plan = load_json(state / "plan.json", {})
    lanes = lanes_doc.get("lanes") if isinstance(lanes_doc, dict) else None
    if not isinstance(lanes, list):
        raise RuntimeError(f"missing lanes array: {state / 'lanes.json'}")
    briefs = load_briefs(state / "briefs")
    target_ids = selected_lane_ids(plan if isinstance(plan, dict) else {}, lanes)
    if max_lanes is not None:
        target_ids = target_ids[:max_lanes]
    target_set = set(target_ids)

    run_id = f"review-steward-{stamp()}-{uuid.uuid4().hex[:12]}"
    receipt_path = state / "review-receipts" / f"{run_id}.json"
    reviews: list[dict[str, Any]] = []
    updated_lanes: list[dict[str, Any]] = []
    for lane in lanes:
        if not isinstance(lane, dict):
            updated_lanes.append(lane)
            continue
        if lane.get("lane_id") not in target_set:
            updated_lanes.append(lane)
            continue
        lane_id = str(lane.get("lane_id"))
        review = review_one_lane(
            lane,
            brief=briefs.get(lane_id),
            pr_status_provider=pr_status_provider,
        )
        review["receipt_path"] = str(receipt_path)
        updated = dict(lane)
        updated["state"] = review["recommended_state"]
        updated["next_action"] = review["next_action"]
        updated["review_evidence"] = review
        updated["reviewed_at"] = review["reviewed_at"]
        updated_lanes.append(updated)
        reviews.append(review)

    counts = {state_name: 0 for state_name in sorted(REVIEW_STATES)}
    for review in reviews:
        counts[str(review["recommended_state"])] += 1
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "reviewed_at": utc_now(),
        "state_root": str(state),
        "plan_sha256": sha256_json(plan) if isinstance(plan, dict) else None,
        "lane_count": len(lanes),
        "selected_lane_count": len(target_ids),
        "reviewed_lane_count": len(reviews),
        "classification_counts": counts,
        "reviews": reviews,
        "next_action": "report only review findings and merge candidates; do not merge",
        "receipt_path": str(receipt_path),
    }
    lanes_doc = dict(lanes_doc)
    lanes_doc["lanes"] = updated_lanes
    lanes_doc["reviewed_at"] = receipt["reviewed_at"]
    lanes_doc["latest_review_receipt"] = str(receipt_path)
    atomic_json(state / "lanes.json", lanes_doc)
    atomic_json(receipt_path, receipt)
    atomic_json(state / "review-latest.json", receipt)
    return receipt


def receipt_summary(receipt: dict[str, Any]) -> dict[str, Any]:
    reviews = receipt.get("reviews") if isinstance(receipt, dict) else []
    compact_reviews = []
    if isinstance(reviews, list):
        for review in reviews:
            if not isinstance(review, dict):
                continue
            compact_reviews.append(
                {
                    "lane_id": review.get("lane_id"),
                    "task_id": review.get("task_id"),
                    "repo": review.get("repo"),
                    "branch": review.get("branch"),
                    "previous_state": review.get("previous_state"),
                    "recommended_state": review.get("recommended_state"),
                    "reasons": review.get("reasons", []),
                    "blockers": review.get("blockers", []),
                    "next_action": review.get("next_action"),
                }
            )
    return {
        "schema_version": receipt.get("schema_version"),
        "run_id": receipt.get("run_id"),
        "reviewed_at": receipt.get("reviewed_at"),
        "state_root": receipt.get("state_root"),
        "selected_lane_count": receipt.get("selected_lane_count"),
        "reviewed_lane_count": receipt.get("reviewed_lane_count"),
        "classification_counts": receipt.get("classification_counts"),
        "reviews": compact_reviews,
        "receipt_path": receipt.get("receipt_path"),
        "next_action": receipt.get("next_action"),
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="bureau-review-steward")
    result.add_argument("command", choices=["run"])
    result.add_argument("--state-root")
    result.add_argument("--max-lanes", type=int)
    result.add_argument("--full-json", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "run":
        value = review_closure_lanes(
            state_root=Path(args.state_root).expanduser() if args.state_root else None,
            max_lanes=args.max_lanes,
        )
    else:
        raise AssertionError(args.command)
    output = value if args.full_json else receipt_summary(value)
    print(json.dumps(output, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
