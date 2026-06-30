from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .cycle_contract import (
    CONTRACT_VERSION,
    atomic_json,
    cycle_id,
    utc_now,
    validate_receipt,
)
from .cycle_contract import (
    SCHEMA_VERSION as CYCLE_SCHEMA_VERSION,
)

AGENT_FRONTIER_SCHEMA_VERSION = 1
DEFAULT_FRONTIER_LIMIT = 8
DEFAULT_REJECT_LIMIT = 50
DEFAULT_BINDING_LIMIT = 8
DEFAULT_FOCUS_REPOSITORIES = ("weltgewebe", "lenskit", "grabowski")
CANONICAL_TASK_ID_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+$")
SOURCE_MARKERS = (
    "roadmap",
    "blueprint",
    "blaupause",
    "masterplan",
    "plan",
    "board",
    "backlog",
    "fahrplan",
    "next",
    "task",
    "todo",
    "checklist",
)
STALE_PATH_MARKERS = ("archive", "archiv", "deprecated", "legacy", "old", "kopie", "copy")
KIND_SCORE = {
    "structured-task": 34,
    "unchecked-item": 24,
    "planning-item": 16,
    "active-planning-document": 5,
}
STATUS_SCORE = {
    "partial": 18,
    "blocked": 14,
    "planned": 12,
    "in progress": 12,
    "in-progress": 12,
    "in arbeit": 12,
    "open": 8,
}
CONFIDENCE_SCORE = {"high": 14, "medium": 7, "low": 1}


def default_state_root() -> Path:
    return Path(
        os.environ.get(
            "BUREAU_AGENT_FRONTIER_STATE_ROOT",
            Path.home() / ".local/state/bureau-agent-frontier",
        )
    ).expanduser()


def default_scanner_state_root() -> Path:
    return Path(
        os.environ.get(
            "BUREAU_DISCOVERY_STATE_ROOT",
            Path.home() / ".local/state/bureau-halfhour-operator",
        )
    ).expanduser()


def default_source_state() -> Path:
    return default_scanner_state_root() / "source-state.json"


def default_scanner_latest() -> Path:
    return default_scanner_state_root() / "latest.json"


def default_closure_plan() -> Path:
    return Path(
        os.environ.get("BUREAU_CLOSURE_PLAN", Path.home() / ".local/state/bureau-closure/plan.json")
    ).expanduser()


def closure_lanes_path() -> Path:
    return Path.home() / ".local" / "state" / "bureau-closure" / "lanes.json"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def normalize_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def normalized_key(value: Any) -> str:
    return normalize_text(value).casefold()


def project_tokens(project: Any) -> tuple[str, ...]:
    raw = normalize_text(project)
    if not raw:
        return ()
    return tuple(part.strip() for part in re.split(r"[,/|]", raw) if part.strip())


def candidate_documents(source_state: dict[str, Any]) -> list[dict[str, Any]]:
    documents = source_state.get("documents", {})
    if not isinstance(documents, dict):
        return []
    result: list[dict[str, Any]] = []
    for document_key, document in documents.items():
        if not isinstance(document, dict):
            continue
        for candidate in document.get("candidates", []):
            if not isinstance(candidate, dict):
                continue
            item = dict(candidate)
            item.setdefault("source_id", document.get("source_id"))
            item.setdefault("source_revision", document.get("source_revision"))
            item.setdefault("source_path", document.get("source_path"))
            item.setdefault("project", document.get("project"))
            item["document_key"] = document_key
            item["document_sha256"] = document.get("sha256")
            result.append(item)
    return result


def registry_task_signatures(registry_root: Path | None) -> dict[str, Any]:
    if registry_root is None:
        return {"titles": set(), "fingerprints": set(), "task_ids": set(), "available": False}
    task_dir = registry_root / "registry/tasks"
    titles: set[str] = set()
    fingerprints: set[str] = set()
    task_ids: set[str] = set()
    if not task_dir.is_dir():
        return {
            "titles": titles,
            "fingerprints": fingerprints,
            "task_ids": task_ids,
            "available": False,
        }
    for path in sorted(task_dir.glob("*.json")):
        raw = load_json(path, {})
        if not isinstance(raw, dict):
            continue
        task_id = raw.get("id")
        if isinstance(task_id, str):
            task_ids.add(task_id)
        title = raw.get("title")
        if isinstance(title, str):
            titles.add(normalized_key(title))
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        for key in (
            "source_candidate_fingerprint",
            "discovery_fingerprint",
            "frontier_fingerprint",
            "candidate_fingerprint",
        ):
            value = metadata.get(key)
            if isinstance(value, str) and value:
                fingerprints.add(value)
    return {"titles": titles, "fingerprints": fingerprints, "task_ids": task_ids, "available": True}


def candidate_rejection(candidate: dict[str, Any], signatures: dict[str, Any]) -> str | None:
    summary = normalize_text(candidate.get("summary") or candidate.get("target_outcome"))
    if len(summary) < 8:
        return "summary_too_short"
    fingerprint = candidate.get("fingerprint")
    if isinstance(fingerprint, str) and fingerprint in signatures["fingerprints"]:
        return "already_registered_fingerprint"
    if normalized_key(summary) in signatures["titles"]:
        return "already_registered_title"
    path = normalized_key(candidate.get("source_path"))
    parts = {part for part in re.split(r"[/_. -]", path) if part}
    if parts.intersection(STALE_PATH_MARKERS):
        return "stale_or_archived_source_path"
    return None


def score_candidate(
    candidate: dict[str, Any],
    *,
    focus_repositories: tuple[str, ...],
    signatures: dict[str, Any],
) -> dict[str, Any]:
    rejected = candidate_rejection(candidate, signatures)
    summary = normalize_text(candidate.get("summary") or candidate.get("target_outcome"))
    project = normalize_text(candidate.get("project"))
    tokens = project_tokens(project)
    kind = normalize_text(candidate.get("candidate_kind"))
    status = normalized_key(candidate.get("status"))
    confidence = normalized_key(candidate.get("confidence"))
    path = normalize_text(candidate.get("source_path"))
    path_key = path.casefold()
    focus_hits = sorted({token for token in tokens if token in focus_repositories})
    score = 0
    reasons: list[str] = []

    if focus_hits:
        score += 36
        reasons.append("focus_repository")
    if kind in KIND_SCORE:
        score += KIND_SCORE[kind]
        reasons.append(f"kind:{kind}")
    if status in STATUS_SCORE:
        score += STATUS_SCORE[status]
        reasons.append(f"status:{status}")
    if confidence in CONFIDENCE_SCORE:
        score += CONFIDENCE_SCORE[confidence]
        reasons.append(f"confidence:{confidence}")
    marker_hits = [marker for marker in SOURCE_MARKERS if marker in path_key]
    if marker_hits:
        score += min(16, 4 * len(marker_hits))
        reasons.append("planning_source_path")
    if CANONICAL_TASK_ID_RE.search(summary):
        score += 8
        reasons.append("mentions_canonical_task")
    if status == "blocked":
        recommended_action = "investigate blocker before promotion"
    elif score >= 76:
        recommended_action = "promote one bounded Bureau task or dispatch a read-only scout"
    elif score >= 56:
        recommended_action = "review for Bureau task promotion"
    else:
        recommended_action = "keep observed; insufficient priority for this cycle"

    return {
        "fingerprint": candidate.get("fingerprint"),
        "score": score,
        "eligible": rejected is None,
        "rejected_reason": rejected,
        "project": project,
        "focus_hits": focus_hits,
        "candidate_kind": kind,
        "status": status,
        "confidence": confidence,
        "summary": summary[:500],
        "source_id": candidate.get("source_id"),
        "source_revision": candidate.get("source_revision"),
        "source_path": path,
        "source_anchor": candidate.get("source_anchor"),
        "external_id": candidate.get("external_id"),
        "reasons": reasons,
        "recommended_action": recommended_action,
        "suggested_worker_profile": suggested_worker_profile(project, path, kind, status),
    }


def suggested_worker_profile(project: str, source_path: str, kind: str, status: str) -> str:
    low = f"{project} {source_path} {kind} {status}".casefold()
    if status == "blocked":
        return "chatgpt-context-review"
    if any(marker in low for marker in ("grabowski", "bureau", "infra", "systemd", "ops/")):
        return "grabowski-local-readonly"
    if any(marker in low for marker in ("weltgewebe", "lenskit", "repo", "src/", "tests/")):
        return "codex-readonly-scout"
    return "chatgpt-curation"


def counter_dict(values: list[Any], *, limit: int | None = None) -> dict[str, int]:
    counts = Counter(str(value or "") for value in values)
    items = counts.most_common(limit) if limit is not None else sorted(counts.items())
    return {key: count for key, count in items if key}


def load_optional_summary(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"available": False}
    value = load_json(path, None)
    if not isinstance(value, dict):
        return {"available": False, "path": str(path)}
    summary: dict[str, Any] = {"available": True, "path": str(path)}
    for key in (
        "cycle_id",
        "run_id",
        "result",
        "degraded",
        "promotion_allowed",
        "selected_lane_count",
        "unbound_selected_rejected_count",
        "canonical_task_bound_count",
    ):
        if key in value:
            summary[key] = value[key]
    metrics = value.get("metrics")
    if isinstance(metrics, dict):
        summary["metrics"] = {
            key: metrics[key]
            for key in (
                "candidate_count",
                "new_candidate_count",
                "documents_changed",
                "scanner_error_count",
            )
            if key in metrics
        }
    return summary


def score_closure_lane(lane: dict[str, Any], focus_repositories: tuple[str, ...]) -> dict[str, Any]:
    state = normalized_key(lane.get("state"))
    repo_name = normalize_text(lane.get("repo_name"))
    branch = normalize_text(lane.get("branch"))
    task_id = lane.get("task_id")
    terminal = {"obsolete", "merged", "verified", "closed", "cancelled", "superseded"}
    rejected_reason = None
    if state in terminal:
        rejected_reason = "terminal_or_obsolete_lane"
    elif isinstance(task_id, str) and CANONICAL_TASK_ID_RE.fullmatch(task_id):
        rejected_reason = "already_bound_to_canonical_task"
    elif not branch:
        rejected_reason = "missing_branch"
    score = 0
    reasons: list[str] = []
    state_scores = {"active": 90, "blocked": 84, "planned": 62, "ready": 58, "discovered": 44}
    if state in state_scores:
        score += state_scores[state]
        reasons.append(f"state:{state}")
    if repo_name in focus_repositories:
        score += 24
        reasons.append("focus_repository")
    finishability = lane.get("finishability")
    if isinstance(finishability, int | float):
        score += min(20, max(0, round(float(finishability) * 20)))
        reasons.append("finishability")
    if branch.startswith(("feat/", "fix/", "plan/")):
        score += 6
        reasons.append("work_branch")
    return {
        "lane_id": lane.get("lane_id"),
        "score": score,
        "eligible": rejected_reason is None,
        "rejected_reason": rejected_reason,
        "repo_name": repo_name,
        "repo": lane.get("repo"),
        "branch": branch,
        "state": state,
        "task_id": task_id,
        "finishability": finishability,
        "next_action": lane.get("next_action"),
        "reasons": reasons,
        "recommended_action": "bind this lane to one canonical Bureau task before dispatch",
        "suggested_worker_profile": suggested_worker_profile(
            repo_name, branch, "closure-lane", state
        ),
    }


def load_closure_lane_assessments(
    path: Path | None,
    *,
    focus_repositories: tuple[str, ...],
    limit: int = DEFAULT_BINDING_LIMIT,
    reject_limit: int = DEFAULT_REJECT_LIMIT,
) -> dict[str, Any]:
    value = load_json(path, {}) if path is not None else {}
    lanes = value.get("lanes", []) if isinstance(value, dict) else []
    if not isinstance(lanes, list):
        lanes = []
    assessed = [
        score_closure_lane(item, focus_repositories) for item in lanes if isinstance(item, dict)
    ]
    eligible = [item for item in assessed if item["eligible"]]
    rejected = [item for item in assessed if not item["eligible"]]
    eligible.sort(
        key=lambda item: (-int(item["score"]), str(item["repo_name"]), str(item["branch"]))
    )
    rejected.sort(
        key=lambda item: (
            str(item["rejected_reason"]),
            str(item["repo_name"]),
            str(item["branch"]),
        )
    )
    return {
        "available": bool(path and value),
        "path": str(path) if path else None,
        "lane_count": len(lanes),
        "eligible_count": len(eligible),
        "rejected_count": len(rejected),
        "selected": eligible[:limit],
        "rejected_sample": rejected[:reject_limit],
    }


def build_frontier_report(
    source_state: dict[str, Any],
    *,
    registry_root: Path | None = None,
    source_state_path: Path | None = None,
    scanner_latest_path: Path | None = None,
    closure_plan_path: Path | None = None,
    closure_lanes_path: Path | None = None,
    focus_repositories: tuple[str, ...] = DEFAULT_FOCUS_REPOSITORIES,
    limit: int = DEFAULT_FRONTIER_LIMIT,
    reject_limit: int = DEFAULT_REJECT_LIMIT,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if limit < 1 or limit > 100:
        raise ValueError("frontier limit must be between 1 and 100")
    if reject_limit < 0 or reject_limit > 500:
        raise ValueError("reject limit must be between 0 and 500")
    if not isinstance(source_state, dict):
        raise ValueError("source state must be a JSON object")
    signatures = registry_task_signatures(registry_root)
    candidates = candidate_documents(source_state)
    assessments = [
        score_candidate(
            candidate,
            focus_repositories=tuple(sorted(set(focus_repositories))),
            signatures=signatures,
        )
        for candidate in candidates
    ]
    eligible = [item for item in assessments if item["eligible"]]
    eligible.sort(
        key=lambda item: (-int(item["score"]), str(item["project"]), str(item["summary"]))
    )
    rejected = [item for item in assessments if not item["eligible"]]
    rejected.sort(
        key=lambda item: (
            str(item["rejected_reason"]),
            str(item["project"]),
            str(item["summary"]),
        )
    )
    selected = eligible[:limit]
    projects = [item.get("project") for item in assessments]
    kinds = [item.get("candidate_kind") for item in assessments]
    statuses = [item.get("status") for item in assessments]
    scanner_summary = load_optional_summary(scanner_latest_path)
    closure_summary = load_optional_summary(closure_plan_path)
    binding = load_closure_lane_assessments(
        closure_lanes_path,
        focus_repositories=tuple(sorted(set(focus_repositories))),
    )
    bottlenecks: list[dict[str, Any]] = []
    if len(candidates) and not selected:
        bottlenecks.append(
            {
                "kind": "latent_backlog_without_frontier_selection",
                "severity": "high",
                "detail": (
                    "source candidates exist but all were rejected or scored below selection window"
                ),
            }
        )
    unbound = closure_summary.get("unbound_selected_rejected_count")
    selected_lanes = closure_summary.get("selected_lane_count")
    if isinstance(unbound, int) and unbound > 0:
        severity = "high" if unbound > max(10, int(selected_lanes or 0) * 3) else "medium"
        bottlenecks.append(
            {
                "kind": "closure_binding_backlog",
                "severity": severity,
                "detail": "closure planner rejected lanes without canonical Bureau task binding",
                "count": unbound,
            }
        )
    scanner_metrics = scanner_summary.get("metrics") if isinstance(scanner_summary, dict) else None
    if (
        isinstance(scanner_metrics, dict)
        and scanner_metrics.get("candidate_count", 0) > 0
        and scanner_metrics.get("new_candidate_count", 0) == 0
    ):
        bottlenecks.append(
            {
                "kind": "delta_only_discovery_idle",
                "severity": "medium",
                "detail": (
                    "scanner has a backlog but no new candidates in the latest delta handoff"
                ),
                "candidate_count": scanner_metrics.get("candidate_count"),
            }
        )
    report = {
        "schema_version": AGENT_FRONTIER_SCHEMA_VERSION,
        "generated_at": generated_at or utc_now(),
        "cycle_id": cycle_id(),
        "frontier_role": "read-only backlog governor",
        "source_state_path": str(source_state_path) if source_state_path else None,
        "source_state_updated_at": source_state.get("updated_at"),
        "source_state_sha256": sha256_json(source_state),
        "closure_lanes_path": str(closure_lanes_path) if closure_lanes_path else None,
        "registry_root": str(registry_root) if registry_root else None,
        "registry_available": bool(signatures["available"]),
        "focus_repositories": list(tuple(sorted(set(focus_repositories)))),
        "limits": {"selected_frontier": limit, "rejected_sample": reject_limit},
        "metrics": {
            "candidate_count": len(candidates),
            "eligible_candidate_count": len(eligible),
            "rejected_candidate_count": len(rejected),
            "selected_frontier_count": len(selected),
            "registered_task_count": len(signatures["task_ids"]),
            "registered_title_count": len(signatures["titles"]),
            "closure_lane_count": binding["lane_count"],
            "eligible_binding_candidate_count": binding["eligible_count"],
            "rejected_binding_candidate_count": binding["rejected_count"],
            "selected_binding_candidate_count": len(binding["selected"]),
        },
        "candidate_counts": {
            "by_project": counter_dict(projects, limit=20),
            "by_kind": counter_dict(kinds),
            "by_status": counter_dict(statuses),
        },
        "scanner_summary": scanner_summary,
        "closure_summary": closure_summary,
        "bottlenecks": bottlenecks,
        "selected_frontier": selected,
        "rejected_sample": rejected[:reject_limit],
        "closure_binding_frontier": binding["selected"],
        "closure_binding_rejected_sample": binding["rejected_sample"],
        "does_not_do": [
            "does not mutate Bureau registry",
            "does not dispatch external agents",
            "does not merge branches",
        ],
        "next_action": next_action(selected, bottlenecks),
    }
    report["report_sha256"] = sha256_json({k: v for k, v in report.items() if k != "report_sha256"})
    return report


def next_action(selected: list[dict[str, Any]], bottlenecks: list[dict[str, Any]]) -> str:
    if any(item.get("kind") == "closure_binding_backlog" for item in bottlenecks):
        return (
            "bind the highest-scoring closure/backlog candidate "
            "to one canonical Bureau task before dispatch"
        )
    if selected:
        return "review selected_frontier[0] and promote at most one bounded task this cycle"
    return "keep observing; no safe promotion candidate selected"


def write_frontier_report(report: dict[str, Any], state_root: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = state_root / "runs" / f"{stamp}-agent-frontier-report.json"
    atomic_json(report_path, report)
    atomic_json(state_root / "latest-report.json", report)
    return report_path


def run_frontier_cycle(
    *,
    source_state_path: Path | None = None,
    scanner_latest_path: Path | None = None,
    closure_plan_path: Path | None = None,
    closure_lanes_file: Path | None = None,
    registry_root: Path | None = None,
    state_root: Path | None = None,
    focus_repositories: tuple[str, ...] = DEFAULT_FOCUS_REPOSITORIES,
    limit: int = DEFAULT_FRONTIER_LIMIT,
) -> dict[str, Any]:
    selected_state_root = state_root or default_state_root()
    selected_source_state = source_state_path or default_source_state()
    selected_scanner_latest = scanner_latest_path or default_scanner_latest()
    selected_closure_plan = closure_plan_path or default_closure_plan()
    selected_lanes_file = closure_lanes_file or closure_lanes_path()
    selected_registry_root = registry_root or Path.cwd()
    selected_state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    (selected_state_root / "runs").mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    selected_cycle = cycle_id()
    run_id = f"agent-frontier-{stamp}"
    started_at = utc_now()
    evidence: list[dict[str, Any]] = []
    degraded = False
    result = "idle"
    report_path: Path | None = None
    try:
        source_state = load_json(selected_source_state, None)
        if not isinstance(source_state, dict):
            raise RuntimeError(f"missing or invalid source state: {selected_source_state}")
        report = build_frontier_report(
            source_state,
            registry_root=selected_registry_root,
            source_state_path=selected_source_state,
            scanner_latest_path=selected_scanner_latest,
            closure_plan_path=selected_closure_plan,
            closure_lanes_path=selected_lanes_file,
            focus_repositories=focus_repositories,
            limit=limit,
        )
        report_path = write_frontier_report(report, selected_state_root)
        selected_count = int(report["metrics"]["selected_frontier_count"])
        result = "completed" if selected_count else "idle"
        evidence.append(
            {
                "kind": "agent_frontier_report",
                "path": str(report_path),
                "report_sha256": report["report_sha256"],
                "candidate_count": report["metrics"]["candidate_count"],
                "eligible_candidate_count": report["metrics"]["eligible_candidate_count"],
                "selected_frontier_count": selected_count,
                "bottleneck_count": len(report["bottlenecks"]),
            }
        )
        if any(item.get("severity") == "high" for item in report["bottlenecks"]):
            degraded = False
    except Exception as exc:  # terminal receipt first; no silent skipped cycle
        degraded = True
        result = "failed"
        evidence.append({"kind": "agent_frontier_error", "error": str(exc)[:2000]})
    receipt_path = selected_state_root / "runs" / f"{stamp}-{run_id}.json"
    receipt = {
        "schema_version": CYCLE_SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "cycle_id": selected_cycle,
        "stage": "frontier",
        "run_id": run_id,
        "trigger": "local-agent-frontier-governor",
        "started_at": started_at,
        "finished_at": utc_now(),
        "lifecycle_state": "terminal",
        "result": result,
        "degraded": degraded,
        "evidence": evidence,
        "next_action": (
            "use agent frontier report to promote at most one bounded task this cycle"
            if not degraded
            else "repair agent frontier inputs before using backlog promotion"
        ),
        "receipt_path": str(receipt_path),
    }
    errors = validate_receipt(receipt, expected_stage="frontier", expected_cycle_id=selected_cycle)
    if errors:
        raise RuntimeError("agent frontier receipt contract failed: " + "; ".join(errors))
    atomic_json(receipt_path, receipt)
    atomic_json(selected_state_root / "latest.json", receipt)
    return {"receipt": receipt, "report_path": str(report_path) if report_path else None}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="bureau-agent-frontier")
    result.add_argument("--source-state", default=str(default_source_state()))
    result.add_argument("--scanner-latest", default=str(default_scanner_latest()))
    result.add_argument("--closure-plan", default=str(default_closure_plan()))
    result.add_argument("--closure-lanes", default=str(closure_lanes_path()))
    result.add_argument("--registry-root", default=".")
    result.add_argument("--state-root", default=str(default_state_root()))
    result.add_argument("--limit", type=int, default=DEFAULT_FRONTIER_LIMIT)
    result.add_argument("--focus-repo", action="append", default=[])
    result.add_argument("--write-state", action="store_true")
    result.add_argument("--json", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    focus = tuple(args.focus_repo) if args.focus_repo else DEFAULT_FOCUS_REPOSITORIES
    if args.write_state:
        result = run_frontier_cycle(
            source_state_path=Path(args.source_state).expanduser(),
            scanner_latest_path=Path(args.scanner_latest).expanduser(),
            closure_plan_path=Path(args.closure_plan).expanduser(),
            closure_lanes_file=Path(args.closure_lanes).expanduser(),
            registry_root=Path(args.registry_root).expanduser(),
            state_root=Path(args.state_root).expanduser(),
            focus_repositories=focus,
            limit=args.limit,
        )
        payload: Any = (
            result
            if args.json
            else {
                "status": result["receipt"]["result"],
                "degraded": result["receipt"]["degraded"],
                "report": result["report_path"],
                "receipt": result["receipt"]["receipt_path"],
            }
        )
        print(
            json.dumps(
                payload,
                indent=2 if args.json else None,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0 if not result["receipt"].get("degraded") else 1
    source_state = load_json(Path(args.source_state).expanduser(), None)
    if not isinstance(source_state, dict):
        raise RuntimeError(f"missing or invalid source state: {args.source_state}")
    report = build_frontier_report(
        source_state,
        registry_root=Path(args.registry_root).expanduser(),
        source_state_path=Path(args.source_state).expanduser(),
        scanner_latest_path=Path(args.scanner_latest).expanduser(),
        closure_plan_path=Path(args.closure_plan).expanduser(),
        closure_lanes_path=Path(args.closure_lanes).expanduser(),
        focus_repositories=focus,
        limit=args.limit,
    )
    print(json.dumps(report, indent=2 if args.json else None, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
