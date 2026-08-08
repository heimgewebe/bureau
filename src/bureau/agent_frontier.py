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
from .task_supply import SupplyError, SupplyPolicy, _git_head, file_sha256

AGENT_FRONTIER_SCHEMA_VERSION = 1
DEFAULT_FRONTIER_LIMIT = 8
DEFAULT_REJECT_LIMIT = 50
DEFAULT_BINDING_LIMIT = 8
SUPPLY_REGENERATION_DIRNAME = "task-supply-regeneration"
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


def default_task_supply_report() -> Path:
    return Path(
        os.environ.get(
            "BUREAU_TASK_SUPPLY_REPORT",
            Path.home() / ".local/state/bureau-task-supply/latest-report.json",
        )
    ).expanduser()


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


def current_registry_binding(registry_root: Path) -> tuple[str, str]:
    inventory_path = registry_root / ".bureau-runtime-snapshot.json"
    if inventory_path.is_file() and not inventory_path.is_symlink():
        inventory = load_json(inventory_path, None)
        if (
            not isinstance(inventory, dict)
            or inventory.get("schema_version") != 1
            or inventory.get("kind") != "bureau_registry_snapshot"
            or not isinstance(inventory.get("source_commit"), str)
            or re.fullmatch(r"[0-9a-f]{40}", inventory["source_commit"]) is None
        ):
            raise SupplyError("invalid canonical Registry snapshot inventory")
        current_head = inventory["source_commit"]
    else:
        current_head = _git_head(registry_root)
    return current_head, file_sha256(registry_root / "registry/queue.json")


def load_task_supply_summary(
    path: Path | None, *, registry_root: Path | None = None
) -> dict[str, Any]:
    if path is None:
        return {"available": False}
    if not path.is_file():
        return {"available": False, "path": str(path)}
    value = load_json(path, None)
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("kind") != "bureau_task_supply_report"
    ):
        return {
            "available": False,
            "path": str(path),
            "invalid": True,
            "reason": "schema-or-kind-invalid",
        }
    claimed_digest = value.get("report_sha256")
    observed_digest = sha256_json(
        {key: item for key, item in value.items() if key != "report_sha256"}
    )
    if claimed_digest != observed_digest:
        return {
            "available": False,
            "path": str(path),
            "invalid": True,
            "reason": "report-digest-mismatch",
            "claimed_report_sha256": claimed_digest,
            "observed_report_sha256": observed_digest,
        }
    if registry_root is not None:
        registry = value.get("registry")
        if not isinstance(registry, dict):
            return {
                "available": False,
                "path": str(path),
                "invalid": True,
                "reason": "registry-binding-missing",
            }
        try:
            current_head, current_queue_sha256 = current_registry_binding(registry_root)
        except (OSError, SupplyError):
            return {
                "available": False,
                "path": str(path),
                "invalid": True,
                "reason": "registry-binding-unverifiable",
            }
        report_head = registry.get("head")
        report_queue_sha256 = registry.get("queue_sha256")
        if report_head != current_head or report_queue_sha256 != current_queue_sha256:
            return {
                "available": False,
                "path": str(path),
                "invalid": True,
                "stale": True,
                "reason": "registry-binding-stale",
                "report_sha256": claimed_digest,
                "report_registry_head": report_head,
                "current_registry_head": current_head,
                "report_queue_sha256": report_queue_sha256,
                "current_queue_sha256": current_queue_sha256,
            }
    metrics = value.get("metrics")
    compact_metrics = {}
    if isinstance(metrics, dict):
        compact_metrics = {
            key: metrics[key]
            for key in (
                "raw_ready_count",
                "normal_claimable_count",
                "fallback_claimable_count",
                "total_claimable_count",
                "blocked_ready_count",
                "floor",
                "refill_target",
                "shortage_to_target",
                "proposal_count",
                "blocked_proposal_count",
            )
            if key in metrics
        }
    return {
        "available": True,
        "path": str(path),
        "status": value.get("status"),
        "report_sha256": value.get("report_sha256"),
        "metrics": compact_metrics,
        "blockers": [
            blocker
            for blocker in value.get("blockers", [])
            if isinstance(blocker, str)
        ],
        "publication_plan_sha256": (
            value.get("publication_plan", {}).get("plan_sha256")
            if isinstance(value.get("publication_plan"), dict)
            else None
        ),
    }



def _supply_regeneration_policy(report_path: Path) -> SupplyPolicy:
    value = load_json(report_path, None)
    raw = value.get("policy") if isinstance(value, dict) else None
    if not isinstance(raw, dict):
        raise SupplyError("stale task-supply report has no policy binding")
    fields: dict[str, int] = {}
    for name in ("floor", "refill_target", "max_new_per_cycle", "bucket_hours"):
        item = raw.get(name)
        if not isinstance(item, int) or isinstance(item, bool) or item < 1:
            raise SupplyError(f"stale task-supply policy field is invalid: {name}")
        fields[name] = item
    return SupplyPolicy(**fields)


def _supply_regeneration_capabilities(report_path: Path) -> tuple[str, ...]:
    snapshot = report_path.parent / "frontier-snapshot.json"
    value = load_json(snapshot, None)
    capabilities = value.get("capabilities") if isinstance(value, dict) else None
    if (
        isinstance(value, dict)
        and value.get("schema_version") == 1
        and value.get("kind") == "bureau_authoritative_frontier_snapshot"
        and isinstance(capabilities, list)
        and capabilities
        and all(isinstance(item, str) and item for item in capabilities)
    ):
        return tuple(sorted(set(capabilities)))
    raise SupplyError("task-supply regeneration capabilities are unavailable")


def refresh_stale_task_supply_summary(
    report_path: Path,
    *,
    registry_root: Path,
    regeneration_root: Path,
    stale_summary: dict[str, Any],
) -> dict[str, Any]:
    try:
        from . import supply_runner

        current_head, current_queue_sha256 = current_registry_binding(registry_root)
        policy = _supply_regeneration_policy(report_path)
        capabilities = _supply_regeneration_capabilities(report_path)
        regeneration_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        result = supply_runner.run_supply_cycle(
            registry_root=registry_root,
            capabilities=capabilities,
            state_root=regeneration_root,
            policy=policy,
            approval_available=False,
            mutation_authority=False,
            publish=False,
            registry_head=current_head,
        )
        publication = result.get("publication")
        if not isinstance(publication, dict) or publication.get("attempted") is not False:
            raise SupplyError("read-only task-supply regeneration attempted publication")
        refreshed_path = Path(str(result.get("report_path") or ""))
        refreshed = load_task_supply_summary(refreshed_path, registry_root=registry_root)
        if refreshed.get("available") is not True:
            raise SupplyError("regenerated task-supply report failed current binding validation")
        if result.get("registry", {}).get("head") != current_head:
            raise SupplyError("regenerated task-supply report head drifted")
        if result.get("registry", {}).get("queue_sha256") != current_queue_sha256:
            raise SupplyError("regenerated task-supply report queue binding drifted")
        return {
            **refreshed,
            "regeneration": {
                "attempted": True,
                "status": "regenerated",
                "source_reason": "registry-binding-stale",
                "source_report_path": str(report_path),
                "registry_head": current_head,
                "queue_sha256": current_queue_sha256,
                "mutation_authority": False,
                "publish": False,
            },
        }
    except Exception as exc:
        return {
            **stale_summary,
            "regeneration": {
                "attempted": True,
                "status": "failed",
                "source_reason": "registry-binding-stale",
                "source_report_path": str(report_path),
                "error": str(exc)[:500],
                "mutation_authority": False,
                "publish": False,
            },
        }

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
    task_supply_report_path: Path | None = None,
    task_supply_regeneration_root: Path | None = None,
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
    supply_summary = load_task_supply_summary(
        task_supply_report_path, registry_root=registry_root
    )
    if (
        supply_summary.get("stale") is True
        and supply_summary.get("reason") == "registry-binding-stale"
        and task_supply_report_path is not None
        and registry_root is not None
        and task_supply_regeneration_root is not None
    ):
        supply_summary = refresh_stale_task_supply_summary(
            task_supply_report_path,
            registry_root=registry_root,
            regeneration_root=task_supply_regeneration_root,
            stale_summary=supply_summary,
        )
    if supply_summary.get("available") or supply_summary.get("invalid"):
        scanner_summary = {**scanner_summary, "task_supply": supply_summary}
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
    supply_status = supply_summary.get("status")
    if supply_summary.get("invalid"):
        bottlenecks.append(
            {
                "kind": "claimable_task_supply_report_invalid",
                "severity": "high",
                "detail": "configured task-supply report failed schema or digest validation",
                "path": supply_summary.get("path"),
                "reason": supply_summary.get("reason"),
            }
        )
    if supply_summary.get("available") and supply_status in {"blocked", "refill-proposed"}:
        supply_metrics = supply_summary.get("metrics", {})
        bottlenecks.append(
            {
                "kind": (
                    "claimable_task_supply_blocked"
                    if supply_status == "blocked"
                    else "claimable_task_supply_below_floor"
                ),
                "severity": "high",
                "detail": (
                    "authoritative claimable supply is below its configured floor; "
                    "fallback proposals remain non-authoritative until canonical publication"
                ),
                "normal_claimable_count": supply_metrics.get("normal_claimable_count"),
                "fallback_claimable_count": supply_metrics.get("fallback_claimable_count"),
                "total_claimable_count": supply_metrics.get("total_claimable_count"),
                "floor": supply_metrics.get("floor"),
                "refill_target": supply_metrics.get("refill_target"),
                "proposal_count": supply_metrics.get("proposal_count"),
                "blockers": supply_summary.get("blockers", []),
                "publication_plan_sha256": supply_summary.get("publication_plan_sha256"),
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
    if any(item.get("kind") == "claimable_task_supply_report_invalid" for item in bottlenecks):
        return "regenerate and verify the revision-bound task-supply report before fallback work"
    if any(item.get("kind") == "claimable_task_supply_blocked" for item in bottlenecks):
        return "resolve the exact supply blockers before reviewing any fallback publication plan"
    if any(item.get("kind") == "claimable_task_supply_below_floor" for item in bottlenecks):
        return (
            "review and canonically publish the bounded supply plan "
            "before claiming fallback work"
        )
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
    task_supply_report_path: Path | None = None,
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
    selected_supply_report = task_supply_report_path or default_task_supply_report()
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
            task_supply_report_path=selected_supply_report,
            task_supply_regeneration_root=(
                selected_state_root / SUPPLY_REGENERATION_DIRNAME
            ),
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
    result.add_argument("--task-supply-report", default=str(default_task_supply_report()))
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
            task_supply_report_path=Path(args.task_supply_report).expanduser(),
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
        task_supply_report_path=Path(args.task_supply_report).expanduser(),
        focus_repositories=focus,
        limit=args.limit,
    )
    print(json.dumps(report, indent=2 if args.json else None, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
