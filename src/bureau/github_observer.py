"""Read-only GitHub pull-request observer.

Implements BUR-2026-005-T003: import PR, check, review and merge facts as
source-attributed evidence. GitHub keeps authority over PR, review and CI
facts; this module only observes. It binds pull requests to Bureau runs and
tasks with explicit markers first, branch heuristics only as a weak fallback,
and fails closed on ambiguity or unavailable sources.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import legacy
from .v2 import (
    OpenPullRequestObservationError,
    _github_repository_for_path,
    _read_only_state_rows,
    _runtime_state_db_path,
)

GITHUB_OBSERVATION_SCHEMA_VERSION = 1

GH_PR_LIST_FIELDS = (
    "number,title,url,state,isDraft,headRefName,headRefOid,baseRefName,"
    "mergeStateStatus,reviewDecision,statusCheckRollup,body,labels,updatedAt"
)

BUREAU_RUN_MARKER_RE = re.compile(r"Bureau-Run:\s*([A-Za-z0-9][A-Za-z0-9._/-]*)")
BUREAU_TASK_MARKER_RE = re.compile(r"Bureau-Task:\s*([A-Za-z0-9][A-Za-z0-9._/-]*)")
BUREAU_TASK_LINE_RE = re.compile(
    r"(?im)^\s*Bureau-Tasks?\s*:\s*(?P<value>[^\n#]+)\s*$"
)
BUREAU_TASK_LABEL_RE = re.compile(
    r"(?i)^\s*Bureau[-_ ]Task(?:s)?\s*(?::|/|=)\s*(?P<value>.+?)\s*$"
)
BUREAU_BINDING_EXCEPTION_LINE_RE = re.compile(
    r"(?im)^\s*Bureau-(?:PR-)?Task-Binding-Exception\s*:\s*(?P<value>[^\n#]+)\s*$"
)
BUREAU_BINDING_EXCEPTION_LABEL_RE = re.compile(
    r"(?i)^\s*Bureau[-_ ](?:PR[-_ ])?Task[-_ ]Binding[-_ ]Exception\s*"
    r"(?::|/|=)\s*(?P<value>.+?)\s*$"
)

BINDING_BUREAU_RUN = "bureau_run_marker"
BINDING_BUREAU_TASK = "bureau_task_marker"
BINDING_BRANCH_FALLBACK = "branch_fallback"
BINDING_UNMATCHED = "unmatched"
BINDING_AMBIGUOUS = "ambiguous"

BINDING_CONFIDENCE = {
    BINDING_BUREAU_RUN: 1.0,
    BINDING_BUREAU_TASK: 0.95,
    BINDING_BRANCH_FALLBACK: 0.55,
}

CI_UNKNOWN = "ci_unknown"
CI_PENDING = "ci_pending"
CI_FAILED = "ci_failed"
CI_PASSED = "ci_passed"

_FAILED_CONCLUSIONS = {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}
_PASSED_CONCLUSIONS = {"SUCCESS"}
_NEUTRAL_CONCLUSIONS = {"NEUTRAL", "SKIPPED"}
_PENDING_STATUSES = {"QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "REQUESTED", "EXPECTED"}
TERMINAL_TASK_STATES = {"verified", "cancelled", "superseded"}
FOLLOW_UP_KEYS = {
    "follow_up",
    "follow_up_task",
    "follow_up_tasks",
    "follow_up_refs",
    "follow_up_semantics",
    "next_task",
    "next_tasks",
    "successor_task",
    "superseded_by",
    "continuation_task",
}

OBSERVATION_DOES_NOT_ESTABLISH = (
    "task_completion",
    "merge_readiness",
    "ci_sufficiency",
    "runtime_correctness",
    "security_correctness",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _marker_items(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[,\s]+", value) if item.strip()]


def _append_unique(target: list[str], values: list[str]) -> None:
    for value in values:
        if value not in target:
            target.append(value)


def _label_names(labels: Any) -> list[str]:
    if not isinstance(labels, list | tuple | set):
        return []
    names: list[str] = []
    for label in labels:
        if isinstance(label, dict):
            name = label.get("name")
            if isinstance(name, str):
                names.append(name)
        elif isinstance(label, str):
            names.append(label)
    return names


def extract_markers(*texts: str | None, labels: Any = None) -> dict[str, list[str]]:
    """Collect unique structured Bureau-Run/Bureau-Task markers.

    Supported task markers are body lines such as ``Bureau-Task: TASK`` or
    ``Bureau-Tasks: TASK-1, TASK-2`` and labels such as ``Bureau-Task: TASK``.
    The observer still accepts the legacy single ``Bureau-Task`` marker as a
    compatibility fallback; broad branch matching remains lower-confidence.
    """
    runs: list[str] = []
    tasks: list[str] = []
    for text in texts:
        if not text:
            continue
        for value in BUREAU_RUN_MARKER_RE.findall(text):
            _append_unique(runs, [value])
        for match in BUREAU_TASK_LINE_RE.finditer(text):
            _append_unique(tasks, _marker_items(match.group("value")))
        for value in BUREAU_TASK_MARKER_RE.findall(text):
            _append_unique(tasks, [value])
    for label in _label_names(labels):
        match = BUREAU_TASK_LABEL_RE.match(label)
        if match:
            _append_unique(tasks, _marker_items(match.group("value")))
    return {"runs": runs, "tasks": tasks}


def extract_binding_exception(*texts: str | None, labels: Any = None) -> dict[str, str] | None:
    """Return a schema-visible exception for intentionally taskless PRs.

    Exceptions must be visible in observed GitHub metadata: either a PR body
    line or a label named ``Bureau-PR-Task-Binding-Exception: <reason>``.
    They only suppress binding hard findings; they never prove merge readiness.
    """
    for text in texts:
        if not text:
            continue
        match = BUREAU_BINDING_EXCEPTION_LINE_RE.search(text)
        if match and match.group("value").strip():
            return {"source": "body", "reason": match.group("value").strip()}
    for label in _label_names(labels):
        match = BUREAU_BINDING_EXCEPTION_LABEL_RE.match(label)
        if match and match.group("value").strip():
            return {"source": "label", "reason": match.group("value").strip()}
    return None


def _check_item_state(item: dict[str, Any]) -> str:
    conclusion = str(item.get("conclusion") or "").upper()
    if conclusion in _FAILED_CONCLUSIONS:
        return "failed"
    if conclusion in _PASSED_CONCLUSIONS:
        return "passed"
    if conclusion in _NEUTRAL_CONCLUSIONS:
        return "neutral"
    status = str(item.get("status") or item.get("state") or "").upper()
    if status in _FAILED_CONCLUSIONS or status in {"FAILURE", "ERROR"}:
        return "failed"
    if status == "SUCCESS":
        return "passed"
    if status in _PENDING_STATUSES:
        return "pending"
    return "unknown"


def summarize_checks(rollup: Any) -> dict[str, Any]:
    """Summarize a gh statusCheckRollup without inferring correctness.

    A passing summary proves only the listed jobs on the observed head.
    """
    items: list[dict[str, str]] = []
    if isinstance(rollup, list):
        for entry in rollup:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or entry.get("context") or "unnamed")
            items.append({"name": name, "state": _check_item_state(entry)})
    states = {item["state"] for item in items}
    if "failed" in states:
        summary = CI_FAILED
    elif "pending" in states:
        summary = CI_PENDING
    elif any(value == "passed" for value in states) and states.issubset({"passed", "neutral"}):
        summary = CI_PASSED
    else:
        summary = CI_UNKNOWN
    return {"summary": summary, "items": items}


def _branch_candidate_tasks(
    head_ref: str,
    known_task_ids: set[str],
    runs_by_branch: dict[str, set[str]],
) -> set[str]:
    branch = head_ref.lower()
    candidates = {
        task_id for task_id in known_task_ids if task_id and task_id.lower() in branch
    }
    candidates.update(runs_by_branch.get(head_ref, set()))
    return candidates


def bind_pull_request(
    markers: dict[str, list[str]],
    head_ref: str,
    *,
    known_task_ids: set[str],
    runs_by_id: dict[str, dict[str, Any]],
    runs_by_branch: dict[str, set[str]],
) -> dict[str, Any]:
    """Bind one PR to a Bureau run/task. Ambiguity fails closed."""
    binding: dict[str, Any] = {
        "binding": BINDING_UNMATCHED,
        "confidence": None,
        "task_id": None,
        "run_id": None,
        "ambiguous_reason": None,
        "notes": [],
    }
    run_markers = markers["runs"]
    task_markers = markers["tasks"]
    if len(run_markers) > 1:
        binding["binding"] = BINDING_AMBIGUOUS
        binding["ambiguous_reason"] = "multiple-bureau-run-markers"
        return binding
    if len(task_markers) > 1:
        binding["binding"] = BINDING_AMBIGUOUS
        binding["ambiguous_reason"] = "multiple-bureau-task-markers"
        return binding
    if run_markers:
        run_id = run_markers[0]
        binding["binding"] = BINDING_BUREAU_RUN
        binding["confidence"] = BINDING_CONFIDENCE[BINDING_BUREAU_RUN]
        binding["run_id"] = run_id
        run_row = runs_by_id.get(run_id)
        run_task = run_row.get("task_id") if run_row else None
        if task_markers and run_task and task_markers[0] != run_task:
            binding["binding"] = BINDING_AMBIGUOUS
            binding["confidence"] = None
            binding["ambiguous_reason"] = "run-marker-task-marker-conflict"
            return binding
        binding["task_id"] = run_task or (task_markers[0] if task_markers else None)
        if run_row is None:
            binding["notes"].append("run-marker-not-found-in-state-store")
        return binding
    if task_markers:
        task_id = task_markers[0]
        binding["binding"] = BINDING_BUREAU_TASK
        binding["confidence"] = BINDING_CONFIDENCE[BINDING_BUREAU_TASK]
        binding["task_id"] = task_id
        if known_task_ids and task_id not in known_task_ids:
            binding["notes"].append("task-marker-not-found-in-registry")
        return binding
    candidates = _branch_candidate_tasks(head_ref, known_task_ids, runs_by_branch)
    if len(candidates) == 1:
        binding["binding"] = BINDING_BRANCH_FALLBACK
        binding["confidence"] = BINDING_CONFIDENCE[BINDING_BRANCH_FALLBACK]
        binding["task_id"] = next(iter(candidates))
        binding["notes"].append("branch-heuristic-is-weak-evidence")
        return binding
    if len(candidates) > 1:
        binding["binding"] = BINDING_AMBIGUOUS
        binding["ambiguous_reason"] = "multiple-task-candidates-for-branch"
        binding["notes"].append("candidates: " + ", ".join(sorted(candidates)))
        return binding
    return binding


def github_pull_requests(
    repository: str,
    *,
    gh_bin: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """Fetch open PR facts via gh. Any failure yields a blocked result."""
    binary = gh_bin or os.environ.get("BUREAU_GH_BIN", "gh")
    command = [
        binary,
        "pr",
        "list",
        "--repo",
        repository,
        "--state",
        "open",
        "--limit",
        "100",
        "--json",
        GH_PR_LIST_FIELDS,
    ]
    try:
        result = subprocess.run(
            command, text=True, capture_output=True, check=False, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "available": False,
            "pull_requests": [],
            "error": f"gh unavailable: {type(exc).__name__}: {exc}",
        }
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
        return {
            "available": False,
            "pull_requests": [],
            "error": f"gh pr list failed for {repository}: {detail}",
        }
    try:
        value = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        return {
            "available": False,
            "pull_requests": [],
            "error": f"gh pr list returned invalid JSON for {repository}: {exc}",
        }
    if not isinstance(value, list):
        return {
            "available": False,
            "pull_requests": [],
            "error": f"gh pr list returned non-list JSON for {repository}",
        }
    return {
        "available": True,
        "pull_requests": [item for item in value if isinstance(item, dict)],
        "error": None,
    }


def _blocked_observation(
    repository: str | None, reason: str, observed_at: str
) -> dict[str, Any]:
    return {
        "schema_version": GITHUB_OBSERVATION_SCHEMA_VERSION,
        "source": "github",
        "repository": repository,
        "observed_at": observed_at,
        "healthy": False,
        "binding_healthy": False,
        "blocked_reason": reason,
        "hard_findings": [
            {
                "severity": "blocker",
                "code": "github-observation-blocked",
                "message": reason,
            }
        ],
        "pull_requests": [],
        "does_not_establish": list(OBSERVATION_DOES_NOT_ESTABLISH),
    }


def _run_index(
    state_path: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, set[str]], list[str]]:
    notes: list[str] = []
    state = _read_only_state_rows(state_path)
    if not state.get("available"):
        notes.append(f"state-store-unavailable: {state.get('error', 'unknown')}")
        return {}, {}, notes
    runs_by_id: dict[str, dict[str, Any]] = {}
    runs_by_branch: dict[str, set[str]] = {}
    for row in state["rows"].get("runs", []):
        run_id = row.get("run_id")
        if not isinstance(run_id, str):
            continue
        runs_by_id[run_id] = row
        branch = row.get("workspace_branch")
        task_id = row.get("task_id")
        if isinstance(branch, str) and branch and isinstance(task_id, str):
            runs_by_branch.setdefault(branch, set()).add(task_id)
    return runs_by_id, runs_by_branch, notes


def _repository_resource_id(repository: str | None) -> str | None:
    if not repository or "/" not in repository:
        return None
    name = repository.rsplit("/", 1)[1].lower().replace("-", "_")
    if not name or not re.fullmatch(r"[a-z0-9_]+", name):
        return None
    return f"repo.{name}"


def _task_claims_resource(
    task: Any, resource_id: str | None, registry: legacy.Registry | None
) -> bool | None:
    if resource_id is None:
        return None
    claims = getattr(task, "claims", None)
    if claims is None:
        return None
    resources = getattr(registry, "resources", {}) if registry is not None else {}
    for claim in claims:
        claim_resource = getattr(claim, "resource", None)
        if claim_resource == resource_id:
            return True
        if isinstance(claim_resource, str) and legacy.overlaps(
            claim_resource, resource_id, resources
        ):
            return True
    return False


def _raw_metadata(task: Any) -> dict[str, Any]:
    raw = getattr(task, "raw", None)
    if not isinstance(raw, dict):
        return {}
    metadata = raw.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _has_follow_up_semantics(task: Any) -> bool:
    raw = getattr(task, "raw", None)
    metadata = _raw_metadata(task)
    candidates: list[Any] = []
    if isinstance(raw, dict):
        candidates.extend(raw.get(key) for key in FOLLOW_UP_KEYS)
    candidates.extend(metadata.get(key) for key in FOLLOW_UP_KEYS)
    return any(value not in (None, "", [], {}, False) for value in candidates)


def observe_pull_requests(
    root: Path,
    *,
    repository: str | None = None,
    registry: legacy.Registry | None = None,
    state_db: Path | None = None,
    state_root: Path | None = None,
    pull_requests: list[dict[str, Any]] | None = None,
    gh_bin: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Observe open PRs and bind them to Bureau runs and tasks.

    Returns evidence only. A blocked or ambiguous observation is reported as
    such and never coerced into success.
    """
    observed_at = now or _utc_now()
    if repository is None:
        try:
            repository = _github_repository_for_path(root)
        except OpenPullRequestObservationError as exc:
            return _blocked_observation(None, str(exc), observed_at)
        if repository is None:
            return _blocked_observation(
                None, f"no GitHub repository resolvable for {root}", observed_at
            )
    if pull_requests is None:
        fetched = github_pull_requests(repository, gh_bin=gh_bin)
        if not fetched["available"]:
            return _blocked_observation(repository, fetched["error"], observed_at)
        pull_requests = fetched["pull_requests"]
    known_task_ids = set(registry.tasks) if registry is not None else set()
    state_path = _runtime_state_db_path(state_db, state_root)
    runs_by_id, runs_by_branch, state_notes = _run_index(state_path)
    observations: list[dict[str, Any]] = []
    for pull_request in pull_requests:
        number = pull_request.get("number")
        if not isinstance(number, int):
            continue
        labels = pull_request.get("labels")
        title = str(pull_request.get("title") or "")
        body = str(pull_request.get("body") or "")
        markers = extract_markers(title, body, labels=labels)
        binding_exception = extract_binding_exception(title, body, labels=labels)
        head_ref = str(pull_request.get("headRefName") or "")
        binding = bind_pull_request(
            markers,
            head_ref,
            known_task_ids=known_task_ids,
            runs_by_id=runs_by_id,
            runs_by_branch=runs_by_branch,
        )
        review_decision = str(pull_request.get("reviewDecision") or "")
        observations.append(
            {
                "repository": repository,
                "number": number,
                "url": str(pull_request.get("url") or ""),
                "title": title,
                "state": str(pull_request.get("state") or "OPEN"),
                "is_draft": bool(pull_request.get("isDraft", False)),
                "head_ref": head_ref,
                "head_sha": str(pull_request.get("headRefOid") or ""),
                "base_ref": str(pull_request.get("baseRefName") or ""),
                "merge_state": str(pull_request.get("mergeStateStatus") or "UNKNOWN"),
                "review_decision": review_decision,
                "review_blocked": review_decision == "CHANGES_REQUESTED",
                "checks": summarize_checks(pull_request.get("statusCheckRollup")),
                "updated_at": str(pull_request.get("updatedAt") or ""),
                "observed_at": observed_at,
                "binding_exception": binding_exception,
                **binding,
            }
        )
    _mark_shared_task_ambiguity(observations)
    hard_findings = _binding_hard_findings(observations, registry=registry, repository=repository)
    return {
        "schema_version": GITHUB_OBSERVATION_SCHEMA_VERSION,
        "source": "github",
        "repository": repository,
        "observed_at": observed_at,
        "healthy": True,
        "binding_healthy": not hard_findings,
        "blocked_reason": None,
        "hard_findings": hard_findings,
        "notes": state_notes,
        "pull_requests": observations,
        "does_not_establish": list(OBSERVATION_DOES_NOT_ESTABLISH),
    }


def _mark_shared_task_ambiguity(observations: list[dict[str, Any]]) -> None:
    by_task: dict[str, list[dict[str, Any]]] = {}
    for observation in observations:
        task_id = observation.get("task_id")
        if isinstance(task_id, str) and task_id:
            by_task.setdefault(task_id, []).append(observation)
    for task_id, bound in by_task.items():
        if len(bound) < 2:
            continue
        numbers = ", ".join(str(item["number"]) for item in bound)
        for observation in bound:
            observation["binding"] = BINDING_AMBIGUOUS
            observation["confidence"] = None
            observation["ambiguous_reason"] = "multiple-open-prs-for-task"
            observation["notes"].append(f"pull requests bound to {task_id}: {numbers}")


def _binding_hard_findings(
    observations: list[dict[str, Any]],
    *,
    registry: legacy.Registry | None = None,
    repository: str | None = None,
) -> list[dict[str, Any]]:
    """Return fail-closed binding findings without hiding usable PR facts."""
    findings: list[dict[str, Any]] = []
    expected_resource = _repository_resource_id(repository)
    registry_resources = getattr(registry, "resources", {}) if registry is not None else {}
    if (
        expected_resource is not None
        and expected_resource not in registry_resources
        and "repo" in registry_resources
    ):
        expected_resource = "repo"
    for observation in observations:
        if observation.get("binding_exception"):
            continue
        number = observation.get("number")
        binding = observation.get("binding")
        task_id = observation.get("task_id")
        if binding == BINDING_UNMATCHED:
            findings.append(
                {
                    "severity": "blocker",
                    "code": "missing-github-task-binding",
                    "message": "open PR has no valid Bureau task binding",
                    "number": number,
                    "task_id": None,
                }
            )
            continue
        if binding == BINDING_AMBIGUOUS:
            findings.append(
                {
                    "severity": "blocker",
                    "code": "ambiguous-github-binding",
                    "message": observation.get("ambiguous_reason") or "ambiguous GitHub binding",
                    "number": number,
                    "task_id": task_id,
                }
            )
            continue
        if not isinstance(task_id, str) or not task_id:
            findings.append(
                {
                    "severity": "blocker",
                    "code": "missing-github-task-binding",
                    "message": "open PR binding did not resolve to a Bureau task",
                    "number": number,
                    "task_id": None,
                }
            )
            continue
        if registry is None:
            continue
        task = registry.tasks.get(task_id)
        if task is None:
            findings.append(
                {
                    "severity": "blocker",
                    "code": "invalid-github-task-binding",
                    "message": "bound Bureau task does not exist in registry",
                    "number": number,
                    "task_id": task_id,
                }
            )
            continue
        claims_expected = _task_claims_resource(task, expected_resource, registry)
        if claims_expected is False:
            findings.append(
                {
                    "severity": "blocker",
                    "code": "wrong-repository-github-task-binding",
                    "message": (
                        "bound task does not claim expected repository resource "
                        f"{expected_resource}"
                    ),
                    "number": number,
                    "task_id": task_id,
                }
            )
        state = str(getattr(task, "state", ""))
        if state in TERMINAL_TASK_STATES and not _has_follow_up_semantics(task):
            findings.append(
                {
                    "severity": "blocker",
                    "code": "terminal-github-task-binding",
                    "message": "bound task is terminal and has no explicit follow-up semantics",
                    "number": number,
                    "task_id": task_id,
                }
            )
    return findings


def filter_observation_by_task(
    observation: dict[str, Any], task_id: str
) -> dict[str, Any]:
    """Return one task-scoped observation with binding health recomputed.

    The live observer first evaluates the full open-PR set so shared-task
    ambiguity can fail closed. The CLI may then present a task-scoped view;
    that view must not inherit hard findings from unrelated tasks.
    """
    if not observation.get("healthy"):
        return {
            **observation,
            "pull_requests": [
                item
                for item in observation.get("pull_requests", [])
                if item.get("task_id") == task_id
            ],
        }
    pull_requests = [
        item
        for item in observation.get("pull_requests", [])
        if item.get("task_id") == task_id
    ]
    numbers = {item.get("number") for item in pull_requests}
    hard_findings = [
        item for item in observation.get("hard_findings", []) if item.get("number") in numbers
    ]
    return {
        **observation,
        "pull_requests": pull_requests,
        "binding_healthy": not hard_findings,
        "hard_findings": hard_findings,
    }


def observation_age_seconds(observation: dict[str, Any], now: str | None = None) -> float | None:
    observed_at = observation.get("observed_at")
    if not isinstance(observed_at, str) or not observed_at:
        return None
    try:
        observed = legacy.parse_time(observed_at)
    except ValueError:
        return None
    current = legacy.parse_time(now) if now else datetime.now(timezone.utc)
    return (current - observed).total_seconds()


def observation_is_stale(
    observation: dict[str, Any],
    *,
    max_age_seconds: float,
    now: str | None = None,
) -> bool:
    """A stale or undatable observation must stay visible as stale."""
    age = observation_age_seconds(observation, now)
    return age is None or age > max_age_seconds
