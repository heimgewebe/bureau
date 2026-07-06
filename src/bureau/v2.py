from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import legacy
from .adapters import AdapterRegistry
from .schema_validation import DocumentSchemaError, SchemaSet

SCHEMA_VERSION = 3
TERMINAL_STATES = {"succeeded", "failed", "cancelled", "orphaned"}
EVIDENCE_TYPES: dict[str, type | tuple[type, ...]] = {
    "object": dict,
    "string": str,
    "boolean": bool,
    "number": (int, float),
    "array": list,
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
EXTERNAL_AGENT_MARKERS = ("codex", "claude", "cline", "agy", "gemini", "jules")


class OpenPullRequestObservationError(RuntimeError):
    """Raised when claim-time open pull-request observation is inconclusive."""


def github_repository_from_remote_url(remote_url: str) -> str | None:
    """Return owner/repo for GitHub remotes in common SSH/HTTPS forms."""
    value = remote_url.strip()
    if not value:
        return None
    if value.endswith(".git"):
        value = value[:-4]
    marker = "github.com/"
    if value.startswith("git@github.com:"):
        path = value.removeprefix("git@github.com:")
    elif marker in value:
        path = value.split(marker, 1)[1]
    else:
        return None
    path = path.strip("/")
    parts = path.split("/")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    return f"{parts[0]}/{parts[1]}"


def _github_repository_for_path(path: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "remote", "get-url", "origin"],
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OpenPullRequestObservationError(
            f"cannot resolve git remote for {path}: {exc}"
        ) from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
        raise OpenPullRequestObservationError(
            f"cannot resolve git remote for {path}: {detail}"
        )
    return github_repository_from_remote_url(result.stdout.strip())


def _github_open_pull_requests(repository: str) -> list[dict[str, Any]]:
    binary = os.environ.get("BUREAU_GH_BIN", "gh")
    try:
        result = subprocess.run(
            [
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
                "number,title,headRefName,url",
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OpenPullRequestObservationError(
            f"cannot observe open pull requests for {repository}: {exc}"
        ) from exc
    if result.returncode != 0:
        detail = "\n".join(
            part for part in (result.stdout.strip(), result.stderr.strip()) if part
        )
        raise OpenPullRequestObservationError(
            f"gh pr list failed for {repository}: {detail or 'no diagnostic'}"
        )
    try:
        value = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise OpenPullRequestObservationError(
            f"gh pr list returned invalid JSON for {repository}: {exc}"
        ) from exc
    if not isinstance(value, list):
        raise OpenPullRequestObservationError(
            f"gh pr list returned non-list JSON for {repository}"
        )
    return [item for item in value if isinstance(item, dict)]


def open_pull_request_reservations(registry: legacy.Registry) -> list[legacy.Reservation]:
    """Represent open GitHub PRs as conservative repo write blockers.

    The guard is intentionally repository-scoped: if a task wants write access to a
    repository or one of its child resources, an already-open PR for that GitHub
    repository blocks claim selection until the PR is merged or closed.
    """
    if os.environ.get("BUREAU_OPEN_PR_CLAIM_GUARD", "1") in {"0", "false", "False"}:
        return []
    result: list[legacy.Reservation] = []
    observed: dict[str, list[dict[str, Any]]] = {}
    for resource in registry.resources.values():
        if resource.type != "git-repository":
            continue
        if not resource.path:
            raise OpenPullRequestObservationError(
                f"cannot observe configured repository {resource.id}: missing path"
            )
        repository = _github_repository_for_path(Path(resource.path).expanduser())
        if repository is None:
            continue
        if repository not in observed:
            observed[repository] = _github_open_pull_requests(repository)
        pull_requests = observed[repository]
        for pull_request in pull_requests:
            number = pull_request.get("number")
            if not isinstance(number, int):
                continue
            result.append(
                legacy.Reservation(
                    f"open-pr:{repository}#{number}",
                    resource.id,
                    "write-blocker",
                    1,
                )
            )
    return result


def _grabowski_worker_policy() -> dict[str, Any]:
    configured = os.environ.get("BUREAU_WORKER_ROUTING_CONFIG")
    path = (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".config/grabowski/worker-routing.json"
    )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _external_agent_profile(task: legacy.Task, worker_id: str, kind: str) -> str | None:
    explicit = task.execution.get("worker_profile") or task.execution.get(
        "preferred_worker_profile"
    )
    if isinstance(explicit, str) and explicit:
        return explicit
    haystack = " ".join((worker_id, kind, task.mode, task.policy)).lower()
    if worker_id.lower().startswith("grabowski") or kind.lower().startswith("grabowski"):
        return None
    for marker in EXTERNAL_AGENT_MARKERS:
        if marker in haystack:
            return marker
    return None


def _agent_brief_path(task: legacy.Task) -> Path | None:
    raw = (
        task.execution.get("agent_brief_path")
        or task.execution.get("grabowski_agent_brief")
        or task.raw.get("metadata", {}).get("agent_brief_path")
    )
    if not isinstance(raw, str) or not raw.strip():
        return None
    return Path(raw).expanduser()


def validate_agent_brief(task: legacy.Task, worker_id: str, kind: str) -> dict[str, Any]:
    path = _agent_brief_path(task)
    if path is None:
        raise legacy.StateError(
            f"task {task.id} requires a Grabowski agent brief before external dispatch"
        )
    try:
        brief = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise legacy.StateError(f"invalid agent brief for task {task.id}: {exc}") from exc
    if not isinstance(brief, dict):
        raise legacy.StateError(f"agent brief for task {task.id} must be a JSON object")
    missing = [field for field in AGENT_BRIEF_REQUIRED_FIELDS if field not in brief]
    if missing:
        raise legacy.StateError(
            f"agent brief for task {task.id} misses fields: {', '.join(missing)}"
        )
    empty = [field for field in AGENT_BRIEF_REQUIRED_FIELDS if brief[field] in (None, "", [], {})]
    if empty:
        raise legacy.StateError(
            f"agent brief for task {task.id} has empty fields: {', '.join(empty)}"
        )
    return {
        "path": str(path),
        "sha256": legacy.sha256_json(brief),
        "profile": _external_agent_profile(task, worker_id, kind),
    }


def _requires_agent_brief(task: legacy.Task, worker_id: str, kind: str, *, dispatch: bool) -> bool:
    policy = _grabowski_worker_policy().get("policy", {})
    if not policy.get("agent_brief_required", False):
        return False
    profile = _external_agent_profile(task, worker_id, kind)
    if not profile:
        return False
    if task.mode in {"grabowski-task", "grabowski-operation"} and profile is None:
        return False
    return dispatch or profile in set(policy.get("first_external_worker", "").split()) or True


def task_revision_sha256(raw: dict[str, Any]) -> str:
    payload = json.loads(json.dumps(raw))
    payload.pop("state", None)
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        metadata.pop("verification", None)
        if not metadata:
            payload.pop("metadata", None)
    return legacy.sha256_json(payload)


def plan_sha256(registry: legacy.Registry, initiative_id: str) -> str:
    plan = registry.initiatives[initiative_id].current_plan or {}
    return legacy.sha256_json(plan)


def grabowski_resource_keys_for_task(
    resources: dict[str, legacy.Resource],
    task: legacy.Task,
) -> set[str]:
    keys: set[str] = set()
    configured = task.execution.get("grabowski_resources", [])
    if isinstance(configured, list):
        keys.update(item for item in configured if isinstance(item, str) and item.strip())
    for claim in task.claims:
        resource = resources.get(claim.resource)
        if resource is not None and resource.grabowski_key:
            keys.add(resource.grabowski_key)
    return keys


CANONICAL_BUREAU_TASK_RE = legacy.ID_RE
MAX_CLOSURE_BRIDGE_LANES = 4


def _closure_plan_path() -> Path:
    configured = os.environ.get("BUREAU_CLOSURE_PLAN")
    if configured:
        return Path(configured).expanduser()
    state_root = os.environ.get("BUREAU_CLOSURE_STATE_ROOT")
    if state_root:
        return Path(state_root).expanduser() / "plan.json"
    return Path.home() / ".local/state/bureau-closure/plan.json"


def _valid_closure_brief_by_lane(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    briefs = plan.get("briefs")
    if not isinstance(briefs, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for brief in briefs:
        if not isinstance(brief, dict) or brief.get("valid") is not True:
            continue
        lane_id = brief.get("lane_id")
        if isinstance(lane_id, str) and lane_id:
            result[lane_id] = brief
    return result


def closure_bridge_task_ids(plan_path: Path | None = None) -> set[str]:
    path = plan_path or _closure_plan_path()
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return set()
    if not isinstance(plan, dict) or plan.get("degraded") is True:
        return set()
    selected_count = plan.get("selected_lane_count")
    bound_count = plan.get("canonical_task_bound_count")
    rejected_count = plan.get("unbound_selected_rejected_count")
    if not isinstance(selected_count, int) or selected_count < 1:
        return set()
    if selected_count > MAX_CLOSURE_BRIDGE_LANES or selected_count != bound_count:
        return set()
    if not isinstance(rejected_count, int):
        return set()
    lanes = plan.get("selected_lanes")
    if not isinstance(lanes, list) or len(lanes) != selected_count:
        return set()
    valid_brief_by_lane = _valid_closure_brief_by_lane(plan)
    result: set[str] = set()
    for lane in lanes:
        if not isinstance(lane, dict):
            continue
        lane_id = lane.get("lane_id")
        task_id = lane.get("task_id")
        brief_path = lane.get("grabowski_brief")
        metadata = lane.get("metadata")
        if not isinstance(lane_id, str) or lane_id not in valid_brief_by_lane:
            continue
        if not isinstance(task_id, str) or not CANONICAL_BUREAU_TASK_RE.fullmatch(task_id):
            continue
        if not isinstance(brief_path, str) or not brief_path:
            continue
        if valid_brief_by_lane[lane_id].get("path") != brief_path:
            continue
        if not isinstance(metadata, dict) or not metadata.get("canonical_task_binding"):
            continue
        result.add(task_id)
    return result


OPEN_TASK_STATES = {"inbox", "planned", "ready", "blocked", "stale"}


def lifecycle_repair_recommendations(lifecycle: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in lifecycle:
        if item.get("consistent") is True:
            continue
        states = item.get("task_states") if isinstance(item.get("task_states"), dict) else {}
        open_tasks = sorted(
            task_id for task_id, state in states.items() if state in OPEN_TASK_STATES
        )
        result.append(
            {
                "initiative_id": item.get("initiative_id"),
                "declared_state": item.get("declared_state"),
                "recommended_state": item.get("recommended_state"),
                "open_task_count": len(open_tasks),
                "open_tasks": open_tasks,
            }
        )
    return result


def lifecycle_repair_task_candidates(
    repair_recommendations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return read-only pseudo tasks for lifecycle repair work.

    These are not registry tasks and cannot be dispatched. They make the required
    repair visible when lifecycle health blocks normal next-task selection.
    """
    candidates: list[dict[str, Any]] = []
    for recommendation in repair_recommendations:
        initiative_id = recommendation.get("initiative_id") or "unknown"
        open_tasks = recommendation.get("open_tasks")
        if not isinstance(open_tasks, list):
            open_tasks = []
        candidates.append(
            {
                "kind": "bureau_lifecycle_repair_candidate",
                "id": f"lifecycle-repair:{initiative_id}",
                "initiative_id": initiative_id,
                "title": f"Repair lifecycle mismatch for {initiative_id}",
                "reason": (
                    "Initiative state conflicts with open task states; reconcile "
                    "initiative lifecycle before claiming normal work."
                ),
                "declared_state": recommendation.get("declared_state"),
                "recommended_state": recommendation.get("recommended_state"),
                "open_task_count": recommendation.get("open_task_count", 0),
                "open_tasks": open_tasks,
                "dispatch_allowed": False,
                "queue_mutation_allowed": False,
                "task_creation_allowed": False,
                "suggested_action": "reconcile_initiative_lifecycle",
            }
        )
    return candidates


def frontier_runtime_truth(
    frontier: list[dict[str, Any]], lifecycle: list[dict[str, Any]]
) -> dict[str, Any]:
    selected = next((item for item in frontier if item.get("eligible") is True), None)
    eligible = [item for item in frontier if item.get("eligible") is True]
    normal_eligible = [item for item in eligible if item.get("closure_bridge") is not True]
    bridge_eligible = [item for item in eligible if item.get("closure_bridge") is True]
    repair_recommendations = lifecycle_repair_recommendations(lifecycle)
    repair_candidates = lifecycle_repair_task_candidates(repair_recommendations)
    lifecycle_mismatch = bool(repair_recommendations)
    selected_via = None
    if selected is not None:
        selected_via = "closure_bridge" if selected.get("closure_bridge") else "normal"
    return {
        "next_task_available": selected is not None,
        "selected_task_id": selected.get("task_id") if selected is not None else None,
        "selected_via": selected_via,
        "eligible_task_count": len(eligible),
        "normal_task_available": bool(normal_eligible),
        "normal_eligible_task_count": len(normal_eligible),
        "closure_bridge_task_available": bool(bridge_eligible),
        "closure_bridge_eligible_task_count": len(bridge_eligible),
        "lifecycle_mismatch": lifecycle_mismatch,
        "health_blocks_normal_claim": lifecycle_mismatch and not normal_eligible,
        "repair_task_required": lifecycle_mismatch and selected is None,
        "repair_recommendations": repair_recommendations,
        "repair_task_candidate_count": len(repair_candidates),
        "repair_task_candidates": repair_candidates,
    }


def doctor_runtime_truth(*, healthy: bool, lifecycle: list[dict[str, Any]]) -> dict[str, Any]:
    repair_recommendations = lifecycle_repair_recommendations(lifecycle)
    repair_candidates = lifecycle_repair_task_candidates(repair_recommendations)
    lifecycle_mismatch = bool(repair_recommendations)
    return {
        "healthy": healthy,
        "next_task_available": None,
        "selected_task_id": None,
        "selected_via": None,
        "capability_context": "not-evaluated",
        "lifecycle_mismatch": lifecycle_mismatch,
        "health_blocks_normal_claim": lifecycle_mismatch,
        "repair_task_required": lifecycle_mismatch,
        "repair_recommendations": repair_recommendations,
        "repair_task_candidate_count": len(repair_candidates),
        "repair_task_candidates": repair_candidates,
    }


class Registry(legacy.Registry):
    """Git-backed registry with executable JSON Schema contracts."""

    def __init__(self, root: Path):
        super().__init__(root)
        try:
            self.schemas = SchemaSet(self.root / "schemas")
        except DocumentSchemaError as exc:
            raise legacy.ValidationError(str(exc)) from exc

    def _schema_document(self, kind: str, path: Path) -> None:
        raw = legacy.read_json(path)
        try:
            self.schemas.validate(kind, raw, path)
        except DocumentSchemaError as exc:
            raise legacy.ValidationError(str(exc)) from exc
        if kind == "source":
            from .weltgewebe_source import validate_source_document

            validate_source_document(raw)

    def _load(self) -> None:
        for path in self._files(self.root / "registry/resources"):
            self._schema_document("resource", path)
        for path in self._files(self.root / "registry/initiatives"):
            self._schema_document("initiative", path)
        for path in self._files(self.root / "registry/tasks"):
            self._schema_document("task", path)
        for path in self._files(self.root / "registry/sources"):
            self._schema_document("source", path)
        queue = self.root / "registry/queue.json"
        if queue.exists():
            self._schema_document("queue", queue)
        super()._load()
        self.tasks = {
            task_id: replace(task, sha256=task_revision_sha256(task.raw))
            for task_id, task in self.tasks.items()
        }

    def validate(self) -> None:
        super().validate()
        errors: list[str] = []
        for initiative in self.initiatives.values():
            if initiative.state == "completed" and initiative.commitment != "completed":
                errors.append(f"completed initiative {initiative.id} must use commitment completed")
            if initiative.state != "completed" and initiative.commitment == "completed":
                errors.append(
                    f"initiative {initiative.id} cannot use commitment completed before closure"
                )
            plan = initiative.current_plan
            if plan:
                commit = plan.get("commit")
                document_sha = plan.get("document_sha256")
                if bool(commit) != bool(document_sha):
                    errors.append(
                        f"initiative {initiative.id} plan commit and "
                        "document_sha256 must appear together"
                    )
        for task in self.tasks.values():
            if task.mode == "grabowski-task" and not task.execution.get("argv"):
                errors.append(f"grabowski-task {task.id} requires execution.argv")
            if task.mode == "grabowski-task" and not grabowski_resource_keys_for_task(
                self.resources,
                task,
            ):
                errors.append(
                    f"grabowski-task {task.id} requires at least one Grabowski resource key"
                )
            if task.mode == "grabowski-operation" and not task.execution.get("operation"):
                errors.append(f"grabowski-operation {task.id} requires execution.operation")
            if task.state == "verified":
                verification = task.raw.get("metadata", {}).get("verification", {})
                expected_plan = plan_sha256(self, task.initiative)
                if verification.get("task_sha256") != task.sha256:
                    errors.append(f"verified task {task.id} has stale or missing task verification")
                if verification.get("plan_sha256") != expected_plan:
                    errors.append(f"verified task {task.id} has stale or missing plan verification")
        if errors:
            raise legacy.ValidationError("\n".join(errors))


def _state_root_entry_type(entry: Path) -> str:
    if entry.is_symlink():
        return "symlink"
    if entry.is_dir():
        return "directory"
    if entry.is_file():
        return "file"
    return "other"


def _classify_state_root_entry(entry: Path, database_name: str) -> dict[str, str]:
    entry_type = _state_root_entry_type(entry)
    name = entry.name
    sqlite_sidecars = {f"{database_name}-wal", f"{database_name}-shm", f"{database_name}-journal"}
    if name == database_name and entry_type == "file":
        return {"name": name, "type": entry_type, "class": "sqlite-database"}
    if name in sqlite_sidecars and entry_type == "file":
        return {"name": name, "type": entry_type, "class": "sqlite-sidecar"}
    if name == "envelopes" and entry_type == "directory":
        return {"name": name, "type": entry_type, "class": "envelope-directory"}
    if name == "receipts" and entry_type == "directory":
        return {"name": name, "type": entry_type, "class": "receipt-directory"}
    if entry_type == "directory" and (
        name in {"archived-untracked", "merge-gatekeeper-runs"}
        or re.fullmatch(r"(?:manual-maintenance|pre-foundation|recovery)-\d{8}T\d{6}Z", name)
    ):
        return {"name": name, "type": entry_type, "class": "legacy-artifact-directory"}
    if entry_type == "file" and re.fullmatch(
        r"bureau\.before-[A-Za-z0-9._-]+-\d{8}T\d{6}Z\.sqlite3", name
    ):
        return {"name": name, "type": entry_type, "class": "legacy-sqlite-backup"}
    if entry_type == "file" and re.fullmatch(
        r"evidence-BUR-RUN-\d{8}T\d{6}Z-[0-9a-f]{10}\.json", name
    ):
        return {"name": name, "type": entry_type, "class": "legacy-evidence-artifact"}
    if entry_type == "file" and (
        name in {"merge-gatekeeper-latest.json", "notes.txt", "read_bounded.py"}
        or re.fullmatch(r"(?:coding-delegator|lenskit-codex-handoff|review-steward)-\d{8}T\d{4}\.json", name)
        or re.fullmatch(r"pr\d+-merged\.json", name)
        or re.fullmatch(r"ollama-wg-[A-Za-z0-9_.-]+\.(?:json|py|txt)", name)
        or re.fullmatch(r"run-(?:goose|qwen)-weltgewebe\.sh", name)
        or re.fullmatch(r"weltgewebe-[A-Za-z0-9_.-]+\.txt", name)
        or re.fullmatch(r"wg-(?:coordinator\.\d+|source\.b64\.\d+)", name)
    ):
        return {"name": name, "type": entry_type, "class": "legacy-operator-artifact"}
    return {"name": name, "type": entry_type, "class": "unknown"}


def state_root_hygiene(state_root: Path, state_db_path: Path) -> dict[str, Any]:
    if not state_root.exists():
        return {
            "available": False,
            "path": str(state_root),
            "known_entries": [],
            "unknown_entries": [],
            "known_count": 0,
            "unknown_count": 0,
            "healthy": False,
            "error": "missing",
        }
    if not state_root.is_dir():
        return {
            "available": False,
            "path": str(state_root),
            "known_entries": [],
            "unknown_entries": [],
            "known_count": 0,
            "unknown_count": 0,
            "healthy": False,
            "error": "not-directory",
        }
    known_entries: list[dict[str, str]] = []
    unknown_entries: list[dict[str, str]] = []
    for entry in sorted(state_root.iterdir(), key=lambda item: item.name):
        classified = _classify_state_root_entry(entry, state_db_path.name)
        if classified["class"] == "unknown":
            unknown_entries.append(classified)
        else:
            known_entries.append(classified)
    return {
        "available": True,
        "path": str(state_root),
        "known_entries": known_entries,
        "unknown_entries": unknown_entries,
        "known_count": len(known_entries),
        "unknown_count": len(unknown_entries),
        "healthy": not unknown_entries,
    }


class StateStore:
    """Migrating operational store and canonical source for run-time state."""

    def __init__(self, path: Path | None = None, state_root: Path | None = None):
        resolved_path = path.expanduser().resolve() if path is not None else None
        if state_root is not None:
            root = state_root.expanduser().resolve()
            if resolved_path is not None and resolved_path.parent != root:
                raise legacy.StateError("state database must be inside state_root")
        elif resolved_path is not None:
            root = resolved_path.parent
        else:
            root = legacy.default_state_dir()
        self.state_root = root
        self.path = resolved_path if resolved_path is not None else root / "bureau.sqlite3"
        self.envelopes_dir = root / "envelopes"
        self.receipts_dir = root / "receipts"
        self.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.envelopes_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.receipts_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_root, 0o700)
        self._initialize()

    def envelope_path(self, run_id: str) -> Path:
        return self.envelopes_dir / f"{run_id}.json"

    def receipt_path(self, run_id: str) -> Path:
        return self.receipts_dir / f"{run_id}.json"

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        with suppress(FileNotFoundError):
            os.chmod(self.path, 0o600)
        return connection

    @staticmethod
    def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
        return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}

    @classmethod
    def _add_column(cls, connection: sqlite3.Connection, table: str, definition: str) -> None:
        name = definition.split()[0]
        if name not in cls._columns(connection, table):
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

    def _initialize(self) -> None:
        connection = self.connect()
        try:
            current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current_version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"unsupported Bureau state schema {current_version}; "
                    f"maximum is {SCHEMA_VERSION}"
                )
            connection.execute("BEGIN IMMEDIATE")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS workers(
                    worker_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    capabilities_json TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs(
                    run_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    task_sha256 TEXT NOT NULL,
                    plan_sha256 TEXT NOT NULL DEFAULT '',
                    envelope_json TEXT NOT NULL,
                    envelope_sha256 TEXT NOT NULL,
                    dispatch_request_id TEXT,
                    external_system TEXT,
                    external_id TEXT,
                    external_state TEXT,
                    external_observed_at TEXT,
                    workspace_path TEXT,
                    workspace_branch TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    FOREIGN KEY(worker_id) REFERENCES workers(worker_id)
                );
                CREATE TABLE IF NOT EXISTS reservations(
                    run_id TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(run_id, resource_id),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS task_status(
                    task_id TEXT PRIMARY KEY,
                    task_sha256 TEXT NOT NULL DEFAULT '',
                    plan_sha256 TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL,
                    receipt_sha256 TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS receipts(
                    run_id TEXT PRIMARY KEY,
                    receipt_json TEXT NOT NULL,
                    receipt_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS workspaces(
                    run_id TEXT PRIMARY KEY,
                    repository_path TEXT NOT NULL,
                    workspace_path TEXT NOT NULL,
                    branch TEXT NOT NULL,
                    baseline_commit TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    detail TEXT,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS events(
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_run_per_task
                    ON runs(task_id) WHERE state IN ('assigned','running','verifying');
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_run_per_worker
                    ON runs(worker_id) WHERE state IN ('assigned','running','verifying');
                """
            )
            self._add_column(connection, "runs", "plan_sha256 TEXT NOT NULL DEFAULT ''")
            self._add_column(connection, "runs", "dispatch_request_id TEXT")
            self._add_column(connection, "runs", "external_state TEXT")
            self._add_column(connection, "runs", "external_observed_at TEXT")
            self._add_column(connection, "task_status", "task_sha256 TEXT NOT NULL DEFAULT ''")
            self._add_column(connection, "task_status", "plan_sha256 TEXT NOT NULL DEFAULT ''")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS unique_dispatch_request "
                "ON runs(dispatch_request_id) WHERE dispatch_request_id IS NOT NULL"
            )
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def immediate(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def event(
        self,
        connection: sqlite3.Connection,
        event_type: str,
        payload: dict[str, Any],
        run_id: str | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO events(run_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
            (run_id, event_type, legacy.canonical_json(payload), legacy.utc_now()),
        )

    def register_worker(self, worker_id: str, kind: str, capabilities: tuple[str, ...]) -> None:
        if not worker_id or len(worker_id) > 200:
            raise legacy.StateError("worker_id must contain 1-200 characters")
        now = legacy.utc_now()
        with self.immediate() as connection:
            connection.execute(
                """
                INSERT INTO workers(worker_id,kind,capabilities_json,heartbeat_at)
                VALUES(?,?,?,?)
                ON CONFLICT(worker_id) DO UPDATE SET
                    kind=excluded.kind,
                    capabilities_json=excluded.capabilities_json,
                    heartbeat_at=excluded.heartbeat_at
                """,
                (worker_id, kind, legacy.canonical_json(list(capabilities)), now),
            )

    def active_runs(self, connection: sqlite3.Connection) -> list[sqlite3.Row]:
        return connection.execute(
            "SELECT * FROM runs WHERE state IN ('assigned','running','verifying')"
        ).fetchall()

    def reservations(
        self, connection: sqlite3.Connection, exclude: str | None = None
    ) -> list[legacy.Reservation]:
        sql = (
            "SELECT r.run_id,r.resource_id,r.mode,r.amount FROM reservations r "
            "JOIN runs u ON u.run_id=r.run_id "
            "WHERE u.state IN ('assigned','running','verifying')"
        )
        params: tuple[Any, ...] = ()
        if exclude:
            sql += " AND r.run_id<>?"
            params = (exclude,)
        return [
            legacy.Reservation(row["run_id"], row["resource_id"], row["mode"], row["amount"])
            for row in connection.execute(sql, params)
        ]

    def overlays(self, connection: sqlite3.Connection, registry: Registry) -> dict[str, str]:
        result: dict[str, str] = {}
        rows = {row["task_id"]: row for row in connection.execute("SELECT * FROM task_status")}
        for task in registry.tasks.values():
            current_plan = plan_sha256(registry, task.initiative)
            verification = task.raw.get("metadata", {}).get("verification", {})
            if task.state == "verified":
                if (
                    verification.get("task_sha256") == task.sha256
                    and verification.get("plan_sha256") == current_plan
                ):
                    result[task.id] = "verified"
                else:
                    result[task.id] = "stale"
                continue
            row = rows.get(task.id)
            if row is None:
                continue
            if row["state"] == "verified" and (
                row["task_sha256"] != task.sha256 or row["plan_sha256"] != current_plan
            ):
                result[task.id] = "stale"
            else:
                result[task.id] = row["state"]
        return result

    @staticmethod
    def public_run(row: sqlite3.Row) -> dict[str, Any]:
        return {key: row[key] for key in row.keys() if key != "envelope_json"}  # noqa: SIM118

    def run(self, run_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise legacy.StateError(f"unknown run {run_id}")
            result = self.public_run(row)
            result["reservations"] = [
                dict(item)
                for item in connection.execute(
                    "SELECT resource_id,mode,amount,created_at FROM reservations WHERE run_id=?",
                    (run_id,),
                )
            ]
            return result

    def list_runs(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [
                self.public_run(row)
                for row in connection.execute("SELECT * FROM runs ORDER BY created_at DESC")
            ]

    def bind(self, run_id: str, system: str, external_id: str) -> dict[str, Any]:
        now = legacy.utc_now()
        with self.immediate() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None or row["state"] not in legacy.ACTIVE_STATES:
                raise legacy.StateError(f"run {run_id} is not active")
            if row["external_system"] and row["external_system"] != system:
                raise legacy.StateError("run already targets another external system")
            if row["external_id"] and row["external_id"] != external_id:
                raise legacy.StateError("run already has another external binding")
            connection.execute(
                """
                UPDATE runs SET external_system=?,external_id=?,external_state='running',
                    state='running',updated_at=?,heartbeat_at=?,external_observed_at=?
                WHERE run_id=?
                """,
                (system, external_id, now, now, now, run_id),
            )
            self.event(
                connection,
                "external-bound",
                {"system": system, "external_id": external_id},
                run_id,
            )
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return self.public_run(row)

    def prepare_dispatch(self, run_id: str, system: str) -> dict[str, Any]:
        now = legacy.utc_now()
        with self.immediate() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None or row["state"] not in legacy.ACTIVE_STATES:
                raise legacy.StateError(f"run {run_id} is not active")
            if row["external_id"]:
                return self.public_run(row)
            if row["external_system"] and row["external_system"] != system:
                raise legacy.StateError("run already targets another external system")
            connection.execute(
                """
                UPDATE runs SET external_system=?,external_state='dispatching',
                    external_observed_at=?,updated_at=? WHERE run_id=?
                """,
                (system, now, now, run_id),
            )
            self.event(
                connection,
                "dispatch-prepared",
                {"system": system, "request_id": row["dispatch_request_id"]},
                run_id,
            )
        return self.run(run_id)

    def mark_dispatch_uncertain(self, run_id: str, error: str) -> dict[str, Any]:
        now = legacy.utc_now()
        with self.immediate() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None or row["state"] not in legacy.ACTIVE_STATES:
                raise legacy.StateError(f"run {run_id} is not active")
            connection.execute(
                """
                UPDATE runs SET external_state='dispatch-uncertain',error=?,
                    external_observed_at=?,updated_at=? WHERE run_id=?
                """,
                (error, now, now, run_id),
            )
            self.event(connection, "dispatch-uncertain", {"error": error}, run_id)
        return self.run(run_id)

    def heartbeat(self, run_id: str, worker_id: str | None = None) -> dict[str, Any]:
        now = legacy.utc_now()
        with self.immediate() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None or row["state"] not in legacy.ACTIVE_STATES:
                raise legacy.StateError(f"run {run_id} is not active")
            if worker_id is not None and row["worker_id"] != worker_id:
                raise legacy.StateError("worker does not own this run")
            connection.execute(
                "UPDATE runs SET heartbeat_at=?,updated_at=? WHERE run_id=?",
                (now, now, run_id),
            )
            connection.execute(
                "UPDATE workers SET heartbeat_at=? WHERE worker_id=?",
                (now, row["worker_id"]),
            )
            self.event(connection, "run-heartbeat", {}, run_id)
        return self.run(run_id)

    def receipt(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT receipt_json FROM receipts WHERE run_id=?", (run_id,)
            ).fetchone()
        return json.loads(row["receipt_json"]) if row else None

    def integrity(self) -> dict[str, Any]:
        with self.connect() as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            foreign = [dict(row) for row in connection.execute("PRAGMA foreign_key_check")]
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        return {"integrity": integrity, "foreign_key_errors": foreign, "schema_version": version}


class Dispatcher(legacy.Dispatcher):
    def __init__(
        self,
        registry: Registry,
        store: StateStore,
        adapters: AdapterRegistry | None = None,
        open_pr_reservations_provider: Callable[[Registry], list[legacy.Reservation]] | None = None,
    ):
        super().__init__(registry, store)
        self.registry = registry
        self.store = store
        self.adapters = adapters or AdapterRegistry()
        self.open_pr_reservations_provider = (
            open_pr_reservations_provider or open_pull_request_reservations
        )

    def _open_pr_reservations(self, *, strict: bool) -> list[legacy.Reservation]:
        try:
            return list(self.open_pr_reservations_provider(self.registry))
        except OpenPullRequestObservationError as exc:
            if strict:
                raise legacy.StateError(f"open pull request guard failed: {exc}") from exc
            return []

    def _closure_bridge_applies(
        self, task: legacy.Task, state: str, initiative: legacy.Initiative
    ) -> bool:
        if task.id not in closure_bridge_task_ids():
            return False
        return (
            state == "planned"
            and initiative.state == "completed"
            and initiative.commitment == "completed"
            and task.mode == "interactive-agent"
            and task.policy == "review-before-effect"
        )

    def reasons(
        self,
        task: legacy.Task,
        capabilities: set[str],
        runs: list[sqlite3.Row],
        reservations: list[legacy.Reservation],
        overlays: dict[str, str],
    ) -> list[str]:
        result: list[str] = []
        initiative = self.registry.initiatives[task.initiative]
        state = overlays.get(task.id, task.state)
        closure_bridge = self._closure_bridge_applies(task, state, initiative)
        if state != "ready" and not closure_bridge:
            result.append(f"state is {state}")
        if initiative.state != "active" and not closure_bridge:
            result.append(f"initiative state is {initiative.state}")
        if initiative.commitment not in {"now", "next"} and not closure_bridge:
            result.append(f"initiative commitment is {initiative.commitment}")
        if (task.policy != "autonomous" or task.mode == "manual") and not closure_bridge:
            result.append(f"execution is {task.mode}/{task.policy}")
        missing = sorted(set(task.capabilities) - capabilities)
        if missing:
            result.append("missing capabilities: " + ", ".join(missing))
        for dependency in task.depends_on:
            dependency_state = overlays.get(dependency, self.registry.tasks[dependency].state)
            if dependency_state != "verified":
                result.append(f"dependency {dependency} is {dependency_state}")
        if any(row["task_id"] == task.id for row in runs):
            result.append("task already active")
        active_for_initiative = sum(
            1
            for row in runs
            if self.registry.tasks.get(row["task_id"])
            and self.registry.tasks[row["task_id"]].initiative == task.initiative
        )
        if active_for_initiative >= initiative.max_active_tasks:
            result.append(f"initiative parallel limit {initiative.max_active_tasks} reached")
        for claim in task.claims:
            result.extend(legacy.claim_conflicts(claim, reservations, self.registry.resources))
        return result

    def frontier(self, capabilities: set[str]) -> list[dict[str, Any]]:
        open_pr_reservations = self._open_pr_reservations(strict=False)
        with self.store.connect() as connection:
            runs = self.store.active_runs(connection)
            reservations = self.store.reservations(connection) + open_pr_reservations
            overlays = self.store.overlays(connection, self.registry)
            result: list[dict[str, Any]] = []
            for task in self.registry.ordered_tasks():
                state = overlays.get(task.id, task.state)
                initiative = self.registry.initiatives[task.initiative]
                closure_bridge = self._closure_bridge_applies(task, state, initiative)
                reasons = self.reasons(task, capabilities, runs, reservations, overlays)
                result.append(
                    {
                        "task_id": task.id,
                        "title": task.title,
                        "effective_state": state,
                        "eligible": not reasons,
                        "closure_bridge": closure_bridge,
                        "reasons": reasons,
                    }
                )
            return result

    def claim_next(
        self,
        worker_id: str,
        capabilities: tuple[str, ...],
        kind: str = "interactive-agent",
        *,
        reconcile_first: bool = True,
    ) -> dict[str, Any]:
        if reconcile_first:
            self.reconcile()
        self.store.register_worker(worker_id, kind, capabilities)
        envelope: dict[str, Any] | None = None
        run: dict[str, Any] | None = None
        with self.store.immediate() as connection:
            current = connection.execute(
                (
                    "SELECT * FROM runs WHERE worker_id=? "
                    "AND state IN ('assigned','running','verifying')"
                ),
                (worker_id,),
            ).fetchone()
            if current:
                return {
                    "status": "existing-assignment",
                    "run": self.store.public_run(current),
                    "envelope": json.loads(current["envelope_json"]),
                }
            open_pr_reservations = self._open_pr_reservations(strict=True)
            worker = connection.execute(
                "SELECT * FROM workers WHERE worker_id=?", (worker_id,)
            ).fetchone()
            worker_capabilities = set(json.loads(worker["capabilities_json"]))
            runs = self.store.active_runs(connection)
            reservations = self.store.reservations(connection) + open_pr_reservations
            overlays = self.store.overlays(connection, self.registry)
            rejected: list[dict[str, Any]] = []
            selected: legacy.Task | None = None
            for task in self.registry.ordered_tasks():
                reasons = self.reasons(task, worker_capabilities, runs, reservations, overlays)
                if not reasons:
                    selected = task
                    break
                rejected.append({"task_id": task.id, "reasons": reasons})
                if self.registry.queue_policy == "strict" and task.state == "ready":
                    break
            if selected is None:
                raise legacy.NoEligibleTask(legacy.canonical_json({"rejected": rejected}))
            attempt = (
                connection.execute(
                    "SELECT COUNT(*) AS n FROM runs WHERE task_id=?", (selected.id,)
                ).fetchone()["n"]
                + 1
            )
            run_id = (
                "BUR-RUN-"
                + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-")
                + uuid.uuid4().hex[:10]
            )
            now = legacy.utc_now()
            initiative = self.registry.initiatives[selected.initiative]
            current_plan_sha = plan_sha256(self.registry, selected.initiative)
            baseline = selected.execution.get("baseline_commit")
            if baseline is None and initiative.current_plan:
                working = selected.execution.get("working_repository")
                plan_repository = initiative.current_plan.get("repository")
                if working and Path(working).name == plan_repository:
                    baseline = initiative.current_plan.get("commit")
            envelope = {
                "schema_version": 1,
                "run_id": run_id,
                "task_id": selected.id,
                "worker_id": worker_id,
                "task_sha256": selected.sha256,
                "plan_sha256": current_plan_sha,
                "created_at": now,
                "task": selected.raw,
                "claims": [claim.as_dict() for claim in selected.claims],
                "plan": initiative.current_plan,
                "baseline_commit": baseline,
            }
            rlens_context_ref = (
                selected.raw.get("rlens_context_ref")
                or selected.execution.get("rlens_context_ref")
                or selected.raw.get("metadata", {}).get("rlens_context_ref")
            )
            if isinstance(rlens_context_ref, dict):
                envelope["rlens_context_ref"] = rlens_context_ref
            self.registry.schemas.validate("execution-envelope", envelope, f"run:{run_id}")
            envelope_json = legacy.canonical_json(envelope)
            envelope_sha = legacy.sha256_json(envelope)
            request_id = f"{run_id}:dispatch-1"
            connection.execute(
                """
                INSERT INTO runs(run_id,task_id,worker_id,attempt,state,task_sha256,
                    plan_sha256,envelope_json,envelope_sha256,dispatch_request_id,
                    created_at,updated_at,heartbeat_at)
                VALUES(?,?,?,?,'assigned',?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    selected.id,
                    worker_id,
                    attempt,
                    selected.sha256,
                    current_plan_sha,
                    envelope_json,
                    envelope_sha,
                    request_id,
                    now,
                    now,
                    now,
                ),
            )
            for claim in selected.claims:
                connection.execute(
                    (
                        "INSERT INTO reservations("
                        "run_id,resource_id,mode,amount,created_at"
                        ") VALUES(?,?,?,?,?)"
                    ),
                    (run_id, claim.resource, claim.mode, claim.amount, now),
                )
            self.store.event(connection, "run-claimed", {"task_id": selected.id}, run_id)
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            run = self.store.public_run(row)
        assert envelope is not None and run is not None
        legacy.atomic_write(
            self.store.envelope_path(run["run_id"]),
            json.dumps(envelope, indent=2, ensure_ascii=False) + "\n",
        )
        return {
            "status": "claimed",
            "run": run,
            "envelope": envelope,
            "envelope_path": str(self.store.envelope_path(run["run_id"])),
        }

    def checkout_next(
        self,
        worker_id: str,
        capabilities: tuple[str, ...],
        kind: str = "interactive-agent",
        base_dir: Path | None = None,
        dispatch: bool = False,
    ) -> dict[str, Any]:
        reconciliation = self.reconcile()
        claimed = self.claim_next(
            worker_id,
            capabilities,
            kind,
            reconcile_first=False,
        )
        run = claimed["run"]
        task = self.registry.tasks[run["task_id"]]
        brief_gate: dict[str, Any] | None = None
        if _requires_agent_brief(task, worker_id, kind, dispatch=dispatch):
            try:
                brief_gate = validate_agent_brief(task, worker_id, kind)
            except legacy.StateError as exc:
                fail_run(self.store, run["run_id"], f"agent brief preflight failed: {exc}")
                raise
        if any(claim.isolation == "worktree" for claim in task.claims):
            run = create_workspace(self.registry, self.store, run["run_id"], base_dir)
        handoff = grabowski_handoff(self.registry, self.store, run["run_id"])
        result = {**claimed, "run": run, "handoff": handoff, "reconciliation": reconciliation}
        if brief_gate is not None:
            result["agent_brief"] = {**brief_gate, "status": "valid"}
        if dispatch:
            if task.mode != "grabowski-task":
                result["dispatch"] = {"status": "not-applicable", "mode": task.mode}
            else:
                adapter = self.adapters.get("grabowski-task")
                if adapter is None:
                    reason = self.adapters.unavailable_reason("grabowski-task")
                    suffix = f": {reason}" if reason else ""
                    raise legacy.StateError(f"grabowski-task adapter is unavailable{suffix}")
                if run.get("external_id"):
                    result["dispatch"] = {
                        "status": "existing",
                        "system": run["external_system"],
                        "external_id": run["external_id"],
                    }
                else:
                    self.store.prepare_dispatch(run["run_id"], adapter.system)
                    try:
                        external_id = adapter.dispatch(handoff)
                    except Exception as exc:
                        recovered = adapter.recover(handoff["request_id"])
                        if recovered is None:
                            self.store.mark_dispatch_uncertain(run["run_id"], str(exc))
                            raise legacy.StateError(
                                "external dispatch is uncertain; reconcile before retrying"
                            ) from exc
                        external_id = recovered
                    result["run"] = self.store.bind(run["run_id"], adapter.system, external_id)
                    result["dispatch"] = {
                        "status": "started",
                        "system": adapter.system,
                        "external_id": external_id,
                    }
        return result

    def reconcile(self, stale_after: int = 900) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        orphaned: list[str] = []
        reconstructed: list[str] = []
        recovered: list[str] = []
        refreshed: list[str] = []
        verifying: list[str] = []
        terminal: list[str] = []
        unobserved: list[dict[str, str]] = []
        with self.store.immediate() as connection:
            for row in self.store.active_runs(connection):
                path = self.store.envelope_path(row["run_id"])
                if not path.exists():
                    legacy.atomic_write(
                        path,
                        json.dumps(json.loads(row["envelope_json"]), indent=2) + "\n",
                    )
                    reconstructed.append(row["run_id"])

                age = (now - legacy.parse_time(row["heartbeat_at"])).total_seconds()
                if not row["external_system"]:
                    if age <= stale_after:
                        continue
                    connection.execute(
                        "UPDATE runs SET state='orphaned',error=?,updated_at=? WHERE run_id=?",
                        ("stale worker without external executor", legacy.utc_now(), row["run_id"]),
                    )
                    connection.execute("DELETE FROM reservations WHERE run_id=?", (row["run_id"],))
                    self.store.event(connection, "run-orphaned", {}, row["run_id"])
                    orphaned.append(row["run_id"])
                    continue

                adapter = self.adapters.get(row["external_system"])
                if adapter is None:
                    reason = self.adapters.unavailable_reason(row["external_system"])
                    unobserved.append(
                        {
                            "run_id": row["run_id"],
                            "system": row["external_system"],
                            "reason": reason or "adapter unavailable",
                        }
                    )
                    continue

                external_id = row["external_id"]
                if not external_id:
                    try:
                        external_id = adapter.recover(row["dispatch_request_id"])
                    except Exception as exc:
                        unobserved.append(
                            {
                                "run_id": row["run_id"],
                                "system": row["external_system"],
                                "reason": f"dispatch recovery failed: {exc}",
                            }
                        )
                        continue
                    if external_id is None:
                        unobserved.append(
                            {
                                "run_id": row["run_id"],
                                "system": row["external_system"],
                                "reason": "dispatch binding missing",
                            }
                        )
                        continue
                    observed_at = legacy.utc_now()
                    connection.execute(
                        """
                        UPDATE runs SET external_id=?,external_state='running',state='running',
                            external_observed_at=?,heartbeat_at=?,updated_at=? WHERE run_id=?
                        """,
                        (external_id, observed_at, observed_at, observed_at, row["run_id"]),
                    )
                    self.store.event(
                        connection,
                        "dispatch-recovered",
                        {"external_id": external_id},
                        row["run_id"],
                    )
                    recovered.append(row["run_id"])

                try:
                    observation = adapter.observe(external_id)
                except Exception as exc:
                    unobserved.append(
                        {
                            "run_id": row["run_id"],
                            "system": row["external_system"],
                            "reason": f"external observation failed: {exc}",
                        }
                    )
                    continue

                observed_at = legacy.utc_now()
                if observation.state == "running":
                    connection.execute(
                        """
                        UPDATE runs SET external_state='running',external_observed_at=?,
                            heartbeat_at=?,updated_at=? WHERE run_id=?
                        """,
                        (observed_at, observed_at, observed_at, row["run_id"]),
                    )
                    refreshed.append(row["run_id"])
                elif observation.state == "succeeded":
                    connection.execute(
                        """
                        UPDATE runs SET state='verifying',external_state='succeeded',
                            external_observed_at=?,heartbeat_at=?,updated_at=? WHERE run_id=?
                        """,
                        (observed_at, observed_at, observed_at, row["run_id"]),
                    )
                    verifying.append(row["run_id"])
                elif observation.state in {"failed", "cancelled", "interrupted", "missing"}:
                    final = "cancelled" if observation.state == "cancelled" else "failed"
                    connection.execute(
                        """
                        UPDATE runs SET state=?,external_state=?,external_observed_at=?,
                            error=?,updated_at=? WHERE run_id=?
                        """,
                        (
                            final,
                            observation.state,
                            observed_at,
                            legacy.canonical_json(observation.detail),
                            observed_at,
                            row["run_id"],
                        ),
                    )
                    connection.execute("DELETE FROM reservations WHERE run_id=?", (row["run_id"],))
                    self.store.event(
                        connection,
                        "external-terminal",
                        {"state": observation.state},
                        row["run_id"],
                    )
                    terminal.append(row["run_id"])
                else:
                    unobserved.append(
                        {
                            "run_id": row["run_id"],
                            "system": row["external_system"],
                            "reason": "external state unknown",
                        }
                    )
        return {
            "orphaned": orphaned,
            "reconstructed_envelopes": reconstructed,
            "recovered": recovered,
            "refreshed": refreshed,
            "verifying": verifying,
            "terminal": terminal,
            "unobserved": unobserved,
        }

    def explain_next(self, capabilities: set[str]) -> dict[str, Any]:
        frontier = self.frontier(capabilities)
        lifecycle = lifecycle_diagnostics(self.registry, self.store)
        eligible = next((item for item in frontier if item["eligible"]), None)
        return {
            "selected": eligible,
            "frontier": frontier,
            "lifecycle": lifecycle,
            "runtime_truth": frontier_runtime_truth(frontier, lifecycle),
        }

    def doctor(self, repair: bool = False) -> dict[str, Any]:
        from .registry_truth import registry_truth_diagnostics

        integrity = self.store.integrity()
        missing_envelopes: list[str] = []
        missing_receipts: list[str] = []
        stale_tasks: list[str] = []
        workspace_findings: list[dict[str, Any]] = []
        queue_findings: list[dict[str, str]] = []
        with self.store.connect() as connection:
            for row in self.store.active_runs(connection):
                path = self.store.envelope_path(row["run_id"])
                if not path.exists():
                    missing_envelopes.append(row["run_id"])
                    if repair:
                        legacy.atomic_write(
                            path,
                            json.dumps(json.loads(row["envelope_json"]), indent=2) + "\n",
                        )
            for row in connection.execute("SELECT run_id,receipt_json FROM receipts"):
                path = self.store.receipt_path(row["run_id"])
                if not path.exists():
                    missing_receipts.append(row["run_id"])
                    if repair:
                        receipt = json.loads(row["receipt_json"])
                        _materialize_receipt(self.store, receipt)
            overlays = self.store.overlays(connection, self.registry)
            stale_tasks = sorted(task for task, state in overlays.items() if state == "stale")
            for row in connection.execute("SELECT * FROM workspaces WHERE state<>'removed'"):
                path = Path(row["workspace_path"])
                if not path.exists():
                    workspace_findings.append(
                        {
                            "run_id": row["run_id"],
                            "state": row["state"],
                            "finding": "workspace path missing",
                        }
                    )
            terminal_queue_states = {"verified", "cancelled", "superseded"}
            allowed_backlog_states = {"planned", "ready"}
            for lane, task_ids in self.registry.queue.items():
                for task_id in task_ids:
                    task = self.registry.tasks[task_id]
                    effective = overlays.get(task_id, task.state)
                    if effective in terminal_queue_states:
                        queue_findings.append(
                            {
                                "task_id": task_id,
                                "lane": lane,
                                "effective_state": effective,
                                "issue": "terminal-task-in-queue",
                            }
                        )
                    elif lane == "now" and effective != "ready":
                        queue_findings.append(
                            {
                                "task_id": task_id,
                                "lane": lane,
                                "effective_state": effective,
                                "issue": "now-task-not-ready",
                            }
                        )
                    elif lane != "now" and effective not in allowed_backlog_states:
                        queue_findings.append(
                            {
                                "task_id": task_id,
                                "lane": lane,
                                "effective_state": effective,
                                "issue": "backlog-task-not-actionable",
                            }
                        )
        lifecycle = lifecycle_diagnostics(self.registry, self.store)
        lifecycle_findings = [item for item in lifecycle if not item["consistent"]]
        state_root_report = state_root_hygiene(self.store.state_root, self.store.path)
        registry_truth = registry_truth_diagnostics(self.registry.root)
        if repair:
            missing_envelopes = []
            missing_receipts = []
        healthy = (
            integrity["integrity"] == "ok"
            and not integrity["foreign_key_errors"]
            and not missing_envelopes
            and not missing_receipts
            and not stale_tasks
            and not workspace_findings
            and not queue_findings
            and not lifecycle_findings
            and state_root_report["healthy"]
            and registry_truth["healthy"]
        )
        return {
            "healthy": healthy,
            "database": integrity,
            "state_root": str(self.store.state_root),
            "state_root_hygiene": state_root_report,
            "missing_envelopes": missing_envelopes,
            "missing_receipts": missing_receipts,
            "stale_tasks": stale_tasks,
            "workspace_findings": workspace_findings,
            "queue_findings": queue_findings,
            "lifecycle": lifecycle,
            "runtime_truth": doctor_runtime_truth(healthy=healthy, lifecycle=lifecycle),
            "registry_truth": registry_truth,
            "repaired": repair,
        }


def _validate_evidence(criteria: list[dict[str, Any]], evidence: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for criterion in criteria:
        key = criterion["id"]
        if key not in evidence or evidence[key] is None:
            missing.append(key)
            continue
        expected = criterion.get("evidence_type")
        if expected and not isinstance(evidence[key], EVIDENCE_TYPES[expected]):
            raise legacy.StateError(f"evidence {key} must have type {expected}")
    return missing


def _materialize_receipt(store: StateStore, receipt: dict[str, Any]) -> Path:
    path = store.receipt_path(receipt["run_id"])
    legacy.atomic_write(path, json.dumps(receipt, indent=2, ensure_ascii=False) + "\n")
    return path


def complete_run(
    registry: Registry,
    store: StateStore,
    run_id: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    existing = store.receipt(run_id)
    if existing is not None:
        path = _materialize_receipt(store, existing)
        task = registry.tasks.get(existing["task_id"])
        current = bool(
            task
            and existing["task_sha256"] == task.sha256
            and existing["plan_sha256"] == plan_sha256(registry, task.initiative)
        )
        return {
            "receipt": existing,
            "receipt_path": str(path),
            "idempotent": True,
            "current": current,
        }
    run = store.run(run_id)
    if run["state"] not in legacy.ACTIVE_STATES:
        raise legacy.StateError(f"run {run_id} is not active")
    task = registry.tasks[run["task_id"]]
    current_plan_sha = plan_sha256(registry, task.initiative)
    if task.sha256 != run["task_sha256"] or current_plan_sha != run["plan_sha256"]:
        raise legacy.StateError("run baseline is stale; task or plan changed after claim")
    with store.connect() as connection:
        envelope = json.loads(
            connection.execute(
                "SELECT envelope_json FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        )
    criteria = [
        item if isinstance(item, dict) else {"id": f"criterion-{index}"}
        for index, item in enumerate(envelope["task"]["acceptance"], 1)
    ]
    missing = _validate_evidence(criteria, evidence)
    if missing:
        raise legacy.StateError("missing evidence for: " + ", ".join(sorted(missing)))
    receipt = {
        "schema_version": 1,
        "run_id": run_id,
        "task_id": run["task_id"],
        "task_sha256": run["task_sha256"],
        "plan_sha256": run["plan_sha256"],
        "envelope_sha256": run["envelope_sha256"],
        "verified_at": legacy.utc_now(),
        "external": (
            {"system": run["external_system"], "id": run["external_id"]}
            if run["external_system"]
            else None
        ),
        "evidence": {item["id"]: evidence[item["id"]] for item in criteria},
    }
    if isinstance(envelope.get("rlens_context_ref"), dict):
        receipt["rlens_context_ref"] = envelope["rlens_context_ref"]
    receipt_sha = legacy.sha256_json(receipt)
    receipt["receipt_sha256"] = receipt_sha
    registry.schemas.validate("receipt", receipt, f"receipt:{run_id}")
    now = legacy.utc_now()
    with store.immediate() as connection:
        current = connection.execute("SELECT state FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if current is None:
            raise legacy.StateError(f"unknown run {run_id}")
        duplicate = connection.execute(
            "SELECT receipt_json FROM receipts WHERE run_id=?", (run_id,)
        ).fetchone()
        if duplicate:
            receipt = json.loads(duplicate["receipt_json"])
        else:
            connection.execute(
                (
                    "INSERT INTO receipts("
                    "run_id,receipt_json,receipt_sha256,created_at"
                    ") VALUES(?,?,?,?)"
                ),
                (run_id, legacy.canonical_json(receipt), receipt_sha, now),
            )
            connection.execute(
                """
                INSERT INTO task_status(
                    task_id,task_sha256,plan_sha256,state,receipt_sha256,updated_at
                )
                VALUES(?,?,?,'verified',?,?)
                ON CONFLICT(task_id) DO UPDATE SET
                    task_sha256=excluded.task_sha256,
                    plan_sha256=excluded.plan_sha256,
                    state='verified',
                    receipt_sha256=excluded.receipt_sha256,
                    updated_at=excluded.updated_at
                """,
                (run["task_id"], run["task_sha256"], run["plan_sha256"], receipt_sha, now),
            )
            connection.execute(
                "UPDATE runs SET state='succeeded',updated_at=? WHERE run_id=?", (now, run_id)
            )
            connection.execute("DELETE FROM reservations WHERE run_id=?", (run_id,))
            store.event(connection, "run-completed", {"receipt_sha256": receipt_sha}, run_id)
    path = _materialize_receipt(store, receipt)
    return {
        "receipt": receipt,
        "receipt_path": str(path),
        "idempotent": False,
        "current": True,
    }


def fail_run(store: StateStore, run_id: str, error: str, state: str = "failed") -> dict[str, Any]:
    if state not in {"failed", "cancelled", "orphaned"}:
        raise legacy.StateError(f"invalid terminal state {state}")
    with store.immediate() as connection:
        row = connection.execute("SELECT state FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None or row["state"] not in legacy.ACTIVE_STATES:
            raise legacy.StateError(f"run {run_id} is not active")
        connection.execute(
            "UPDATE runs SET state=?,error=?,updated_at=? WHERE run_id=?",
            (state, error, legacy.utc_now(), run_id),
        )
        connection.execute("DELETE FROM reservations WHERE run_id=?", (run_id,))
        store.event(connection, "run-failed", {"state": state, "error": error}, run_id)
    return store.run(run_id)


def grabowski_handoff(registry: Registry, store: StateStore, run_id: str) -> dict[str, Any]:
    run = store.run(run_id)
    task = registry.tasks[run["task_id"]]
    keys = grabowski_resource_keys_for_task(registry.resources, task)
    result: dict[str, Any] = {
        "origin_ref": f"bureau:{run_id}",
        "request_id": run["dispatch_request_id"] or f"{run_id}:dispatch-1",
        "run_id": run_id,
        "task_id": task.id,
        "task_sha256": run["task_sha256"],
        "plan_sha256": run["plan_sha256"],
        "envelope_sha256": run["envelope_sha256"],
        "envelope_path": str(store.envelope_path(run_id)),
        "mode": task.mode,
        "policy": task.policy,
        "host": task.execution.get("preferred_host", "heim-pc"),
        "cwd": run["workspace_path"] or task.execution.get("cwd"),
        "resource_keys": sorted(keys),
        "acceptance": list(task.acceptance),
        "agent_brief_path": task.execution.get("agent_brief_path")
        or task.execution.get("grabowski_agent_brief")
        or task.raw.get("metadata", {}).get("agent_brief_path"),
        "worker_profile": task.execution.get("worker_profile")
        or task.execution.get("preferred_worker_profile"),
        "cpu_weight": int(task.execution.get("cpu_weight", 100)),
        "io_weight": int(task.execution.get("io_weight", 100)),
        "memory_max_bytes": task.execution.get("memory_max_bytes"),
    }
    rlens_context_ref = (
        task.raw.get("rlens_context_ref")
        or task.execution.get("rlens_context_ref")
        or task.raw.get("metadata", {}).get("rlens_context_ref")
    )
    if isinstance(rlens_context_ref, dict):
        result["rlens_context_ref"] = rlens_context_ref
    if task.mode == "grabowski-task":
        result.update(
            argv=task.execution["argv"],
            runtime_seconds=int(task.execution.get("runtime_seconds", 7200)),
            resume_policy=task.execution.get("resume_policy", "verify-then-retry"),
        )
    if task.mode == "grabowski-operation":
        result.update(
            operation=task.execution["operation"],
            parameters=task.execution.get("operation_parameters", {}),
        )
    return result


def _git(repo: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode:
        raise legacy.StateError(result.stderr.strip() or result.stdout.strip())
    return result


def _worktree_paths(repo: Path) -> set[Path]:
    output = _git(repo, "worktree", "list", "--porcelain").stdout
    result: set[Path] = set()
    for line in output.splitlines():
        if line.startswith("worktree "):
            result.add(Path(line.removeprefix("worktree ")).resolve())
    return result


def create_workspace(
    registry: Registry,
    store: StateStore,
    run_id: str,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    run = store.run(run_id)
    task = registry.tasks[run["task_id"]]
    repository = task.execution.get("working_repository")
    if not repository:
        raise legacy.StateError(f"task {task.id} has no working_repository")
    if not any(claim.isolation == "worktree" for claim in task.claims):
        raise legacy.StateError(f"task {task.id} has no worktree-isolated claim")
    if run["workspace_path"]:
        return run
    repo = Path(repository).expanduser().resolve()
    _git(repo, "rev-parse", "--git-dir")
    with store.connect() as connection:
        envelope = json.loads(
            connection.execute(
                "SELECT envelope_json FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        )
    baseline = envelope.get("baseline_commit") or _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "cat-file", "-e", f"{baseline}^{{commit}}")
    root = base_dir.resolve() if base_dir else repo.parent / ".bureau-worktrees"
    destination = (root / run_id).resolve()
    root.mkdir(parents=True, exist_ok=True)
    branch_task = re.sub(r"[^A-Za-z0-9._-]+", "-", task.id).lower()
    branch = f"bureau/{branch_task}/{run_id.rsplit('-', 1)[-1].lower()}"

    registered = _worktree_paths(repo)
    created = False
    if destination in registered:
        current_branch = _git(destination, "branch", "--show-current").stdout.strip()
        if current_branch != branch:
            raise legacy.StateError(
                f"existing workspace {destination} uses unexpected branch {current_branch}"
            )
    elif destination.exists():
        raise legacy.StateError(
            f"workspace destination exists but is not registered: {destination}"
        )
    else:
        branch_exists = (
            _git(
                repo,
                "show-ref",
                "--verify",
                f"refs/heads/{branch}",
                check=False,
            ).returncode
            == 0
        )
        if branch_exists:
            _git(repo, "worktree", "add", str(destination), branch)
        else:
            _git(repo, "worktree", "add", "-b", branch, str(destination), baseline)
        created = True

    now = legacy.utc_now()
    try:
        with store.immediate() as connection:
            connection.execute(
                "UPDATE runs SET workspace_path=?,workspace_branch=?,updated_at=? WHERE run_id=?",
                (str(destination), branch, now, run_id),
            )
            connection.execute(
                """
                INSERT INTO workspaces(run_id,repository_path,workspace_path,branch,baseline_commit,
                    state,created_at,updated_at)
                VALUES(?,?,?,?,?,'active',?,?)
                ON CONFLICT(run_id) DO UPDATE SET workspace_path=excluded.workspace_path,
                    branch=excluded.branch,baseline_commit=excluded.baseline_commit,
                    state='active',updated_at=excluded.updated_at
                """,
                (run_id, str(repo), str(destination), branch, baseline, now, now),
            )
            store.event(connection, "workspace-created", {"path": str(destination)}, run_id)
    except Exception:
        if created:
            _git(repo, "worktree", "remove", "--force", str(destination), check=False)
        raise
    return store.run(run_id)


def workspace_status(store: StateStore, run_id: str) -> dict[str, Any]:
    with store.connect() as connection:
        row = connection.execute("SELECT * FROM workspaces WHERE run_id=?", (run_id,)).fetchone()
        run = connection.execute("SELECT state FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise legacy.StateError(f"run {run_id} has no workspace")
    repo = Path(row["repository_path"])
    path = Path(row["workspace_path"])
    result = dict(row)
    result["exists"] = path.is_dir()
    result["run_state"] = run["state"] if run else None
    if not path.is_dir():
        result.update(dirty=None, head=None, merged=None)
        return result
    result["dirty"] = bool(_git(path, "status", "--porcelain").stdout.strip())
    result["head"] = _git(path, "rev-parse", "HEAD").stdout.strip()
    contains = _git(repo, "branch", "-r", "--contains", result["head"], check=False).stdout
    result["merged"] = any(
        line.strip().endswith(("/main", "/master")) for line in contains.splitlines()
    )
    return result


def preserve_workspace(store: StateStore, run_id: str, reason: str) -> dict[str, Any]:
    with store.immediate() as connection:
        row = connection.execute("SELECT 1 FROM workspaces WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise legacy.StateError(f"run {run_id} has no workspace")
        connection.execute(
            "UPDATE workspaces SET state='preserved',detail=?,updated_at=? WHERE run_id=?",
            (reason, legacy.utc_now(), run_id),
        )
        store.event(connection, "workspace-preserved", {"reason": reason}, run_id)
    return workspace_status(store, run_id)


def cleanup_workspace(store: StateStore, run_id: str, force: bool = False) -> dict[str, Any]:
    status = workspace_status(store, run_id)
    if status["run_state"] not in TERMINAL_STATES:
        raise legacy.StateError("workspace cleanup requires a terminal run")
    if not status["exists"]:
        with store.immediate() as connection:
            connection.execute(
                "UPDATE workspaces SET state='removed',updated_at=? WHERE run_id=?",
                (legacy.utc_now(), run_id),
            )
        return {**status, "cleanup": "already-missing"}
    if status["dirty"] and not force:
        return preserve_workspace(store, run_id, "dirty workspace")
    if not status["merged"] and not force:
        return preserve_workspace(store, run_id, "branch not merged")
    repo = Path(status["repository_path"])
    _git(
        repo,
        "worktree",
        "remove",
        *(["--force"] if force else []),
        status["workspace_path"],
    )
    if status["merged"]:
        _git(repo, "branch", "-d", status["branch"], check=False)
    with store.immediate() as connection:
        connection.execute(
            "UPDATE workspaces SET state='removed',updated_at=? WHERE run_id=?",
            (legacy.utc_now(), run_id),
        )
        store.event(connection, "workspace-removed", {"force": force}, run_id)
    return {**status, "cleanup": "removed"}


def verification_stamp(registry: Registry, store: StateStore, task_id: str) -> dict[str, Any]:
    task = registry.tasks.get(task_id)
    if task is None:
        raise legacy.StateError(f"unknown task {task_id}")
    current_plan = plan_sha256(registry, task.initiative)
    with store.connect() as connection:
        row = connection.execute("SELECT * FROM task_status WHERE task_id=?", (task_id,)).fetchone()
    if row and (
        row["state"] == "verified"
        and row["task_sha256"] == task.sha256
        and row["plan_sha256"] == current_plan
    ):
        return {
            "task_sha256": task.sha256,
            "plan_sha256": current_plan,
            "receipt_sha256": row["receipt_sha256"],
        }
    verification = task.raw.get("metadata", {}).get("verification", {})
    if (
        verification.get("task_sha256") == task.sha256
        and verification.get("plan_sha256") == current_plan
    ):
        return dict(verification)
    raise legacy.StateError(f"task {task_id} has no current verification")


def lifecycle_diagnostics(registry: Registry, store: StateStore) -> list[dict[str, Any]]:
    with store.connect() as connection:
        overlays = store.overlays(connection, registry)
    result: list[dict[str, Any]] = []
    for initiative in registry.initiatives.values():
        tasks = [task for task in registry.tasks.values() if task.initiative == initiative.id]
        states = {task.id: overlays.get(task.id, task.state) for task in tasks}
        open_states = {"inbox", "planned", "ready", "blocked", "stale"}
        if (
            initiative.state == "completed"
            and tasks
            and all(state == "verified" for state in states.values())
        ):
            recommendation = "completed"
        elif initiative.state == "completed":
            recommendation = "reopen-required"
        elif tasks and all(state == "verified" for state in states.values()):
            recommendation = "completion-ready"
        elif any(state == "blocked" for state in states.values()) and not any(
            state == "ready" for state in states.values()
        ):
            recommendation = "waiting"
        elif any(state in open_states for state in states.values()):
            recommendation = "active"
        else:
            recommendation = initiative.state
        result.append(
            {
                "initiative_id": initiative.id,
                "declared_state": initiative.state,
                "recommended_state": recommendation,
                "task_states": states,
                "consistent": initiative.state == recommendation,
            }
        )
    return result




def _runtime_state_db_path(
    state_db: Path | None = None,
    state_root: Path | None = None,
) -> Path:
    if state_db is not None:
        return state_db.expanduser().resolve()
    if state_root is not None:
        return (state_root.expanduser().resolve() / "bureau.sqlite3")
    configured = os.environ.get("BUREAU_STATE_DIR")
    root = Path(configured).expanduser() if configured else Path.home() / ".local/state/bureau"
    return (root / "bureau.sqlite3").resolve()


def _git_read(repo: Path, arguments: list[str]) -> dict[str, Any]:
    result = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(repo), *arguments],
        text=True,
        capture_output=True,
        check=False,
    )
    return {
        "returncode": result.returncode,
        "stdout": result.stdout.rstrip("\n"),
        "stderr": result.stderr.rstrip("\n"),
    }


def _checkout_drift(root: Path, findings: list[dict[str, Any]]) -> dict[str, Any]:
    inside = _git_read(root, ["rev-parse", "--is-inside-work-tree"])
    if inside["returncode"] != 0 or inside["stdout"] != "true":
        findings.append(
            {
                "severity": "blocker",
                "code": "checkout-not-git",
                "message": "Bureau root is not a readable Git worktree.",
            }
        )
        return {"available": False, "root": str(root), "error": inside["stderr"]}

    branch = _git_read(root, ["branch", "--show-current"])
    head = _git_read(root, ["rev-parse", "HEAD"])
    origin_main = _git_read(root, ["rev-parse", "--verify", "origin/main^{commit}"])
    status = _git_read(root, ["status", "--porcelain=v1", "--untracked-files=all"])
    status_failed = status["returncode"] != 0
    dirty_lines = [] if status_failed else [line for line in status["stdout"].splitlines() if line]
    detached = not branch["stdout"]

    report = {
        "available": True,
        "root": str(root),
        "branch": branch["stdout"] or None,
        "detached": detached,
        "head": head["stdout"] if head["returncode"] == 0 else None,
        "origin_main": origin_main["stdout"] if origin_main["returncode"] == 0 else None,
        "head_equals_origin_main": None,
        "dirty": None if status_failed else bool(dirty_lines),
        "dirty_paths": dirty_lines,
    }
    if report["head"] and report["origin_main"]:
        report["head_equals_origin_main"] = report["head"] == report["origin_main"]
    if detached:
        findings.append(
            {
                "severity": "info",
                "code": "checkout-detached",
                "message": "Checkout is detached; this is acceptable for read-only inspection.",
            }
        )
    if status_failed:
        findings.append(
            {
                "severity": "blocker",
                "code": "checkout-status-unreadable",
                "message": "Git status could not be read; checkout cleanliness is unknown.",
                "error": status["stderr"],
            }
        )
    elif dirty_lines:
        findings.append(
            {
                "severity": "warning",
                "code": "checkout-dirty",
                "message": "Checkout contains uncommitted or untracked paths.",
                "paths": dirty_lines,
            }
        )
    else:
        findings.append(
            {
                "severity": "info",
                "code": "checkout-clean",
                "message": "Checkout has no uncommitted or untracked paths.",
            }
        )
    if origin_main["returncode"] != 0:
        findings.append(
            {
                "severity": "warning",
                "code": "origin-main-missing",
                "message": "origin/main is not available locally; no fetch was attempted.",
            }
        )
    elif report["head"] != report["origin_main"]:
        findings.append(
            {
                "severity": "warning",
                "code": "head-differs-origin-main",
                "message": "Checkout HEAD differs from local origin/main.",
                "head": report["head"],
                "origin_main": report["origin_main"],
            }
        )
    return report


def _read_only_state_rows(state_path: Path) -> dict[str, Any]:
    if not state_path.is_file():
        return {"available": False, "path": str(state_path), "error": "missing"}
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{state_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign = [dict(row) for row in connection.execute("PRAGMA foreign_key_check")]
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        required_tables = {"task_status", "runs", "receipts"}
        missing_tables = sorted(required_tables - tables)
        if missing_tables:
            return {
                "available": False,
                "path": str(state_path),
                "integrity": integrity,
                "foreign_key_errors": foreign,
                "schema_version": version,
                "missing_tables": missing_tables,
                "error": "missing required tables: " + ", ".join(missing_tables),
            }
        if version > SCHEMA_VERSION:
            return {
                "available": False,
                "path": str(state_path),
                "integrity": integrity,
                "foreign_key_errors": foreign,
                "schema_version": version,
                "unsupported_schema_version": version,
                "error": (
                    f"unsupported schema version: {version}; "
                    f"maximum supported is {SCHEMA_VERSION}"
                ),
            }
        rows: dict[str, list[dict[str, Any]]] = {}
        for table in ("task_status", "runs", "receipts"):
            if table in tables:
                rows[table] = [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
            else:
                rows[table] = []
        return {
            "available": True,
            "path": str(state_path),
            "integrity": integrity,
            "foreign_key_errors": foreign,
            "schema_version": version,
            "rows": rows,
        }
    except sqlite3.Error as exc:
        return {
            "available": False,
            "path": str(state_path),
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if connection is not None:
            connection.close()


def _read_only_overlays(
    registry: Registry, task_status_rows: list[dict[str, Any]]
) -> dict[str, str]:
    result: dict[str, str] = {}
    rows = {row["task_id"]: row for row in task_status_rows if "task_id" in row}
    for task in registry.tasks.values():
        current_plan = plan_sha256(registry, task.initiative)
        verification = task.raw.get("metadata", {}).get("verification", {})
        if task.state == "verified":
            if (
                verification.get("task_sha256") == task.sha256
                and verification.get("plan_sha256") == current_plan
            ):
                result[task.id] = "verified"
            else:
                result[task.id] = "stale"
            continue
        row = rows.get(task.id)
        if row is None:
            continue
        if row.get("state") == "verified" and (
            row.get("task_sha256") != task.sha256 or row.get("plan_sha256") != current_plan
        ):
            result[task.id] = "stale"
        else:
            state = row.get("state")
            if isinstance(state, str):
                result[task.id] = state
    return result


def _read_only_lifecycle(registry: Registry, overlays: dict[str, str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for initiative in registry.initiatives.values():
        tasks = [task for task in registry.tasks.values() if task.initiative == initiative.id]
        states = {task.id: overlays.get(task.id, task.state) for task in tasks}
        open_states = {"inbox", "planned", "ready", "blocked", "stale"}
        if (
            initiative.state == "completed"
            and tasks
            and all(state == "verified" for state in states.values())
        ):
            recommendation = "completed"
        elif initiative.state == "completed":
            recommendation = "reopen-required"
        elif tasks and all(state == "verified" for state in states.values()):
            recommendation = "completion-ready"
        elif any(state == "blocked" for state in states.values()) and not any(
            state == "ready" for state in states.values()
        ):
            recommendation = "waiting"
        elif any(state in open_states for state in states.values()):
            recommendation = "active"
        else:
            recommendation = initiative.state
        result.append(
            {
                "initiative_id": initiative.id,
                "declared_state": initiative.state,
                "recommended_state": recommendation,
                "task_states": states,
                "consistent": initiative.state == recommendation,
            }
        )
    return result


def _registry_drift(
    registry: Registry, overlays: dict[str, str], findings: list[dict[str, Any]]
) -> dict[str, Any]:
    task_states: dict[str, int] = {}
    stale_verified: list[str] = []
    for task in registry.tasks.values():
        state = overlays.get(task.id, task.state)
        task_states[state] = task_states.get(state, 0) + 1
        if task.state == "verified" and state == "stale":
            stale_verified.append(task.id)
    lifecycle = _read_only_lifecycle(registry, overlays)
    mismatches = [item for item in lifecycle if not item["consistent"]]
    for item in mismatches:
        findings.append(
            {
                "severity": "warning",
                "code": "lifecycle-mismatch",
                "message": "Initiative lifecycle does not match current task states.",
                "initiative_id": item["initiative_id"],
                "declared_state": item["declared_state"],
                "recommended_state": item["recommended_state"],
            }
        )
    if stale_verified:
        findings.append(
            {
                "severity": "blocker",
                "code": "verified-task-drift",
                "message": "Verified registry tasks have stale or missing embedded verification.",
                "task_ids": stale_verified,
            }
        )
    return {
        "valid": True,
        "initiatives": len(registry.initiatives),
        "tasks": len(registry.tasks),
        "resources": len(registry.resources),
        "task_states": task_states,
        "verified_task_drift": stale_verified,
        "lifecycle": lifecycle,
    }


def _receipt_drift(
    registry: Registry,
    state: dict[str, Any],
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    if not state.get("available"):
        findings.append(
            {
                "severity": "blocker",
                "code": "state-db-unavailable",
                "message": "Bureau state database is unavailable for receipt drift inspection.",
                "path": state.get("path"),
                "error": state.get("error"),
            }
        )
        return {"available": False, "stale_tasks": [], "unknown_task_status_rows": []}
    if state.get("integrity") != "ok" or state.get("foreign_key_errors"):
        findings.append(
            {
                "severity": "blocker",
                "code": "state-db-integrity",
                "message": "Bureau state database failed read-only integrity checks.",
                "integrity": state.get("integrity"),
                "foreign_key_errors": state.get("foreign_key_errors"),
            }
        )

    task_status_rows = state["rows"]["task_status"]
    stale_tasks: list[dict[str, Any]] = []
    unknown_status_rows: list[str] = []
    for row in task_status_rows:
        task_id = row.get("task_id")
        if not isinstance(task_id, str) or task_id not in registry.tasks:
            if isinstance(task_id, str):
                unknown_status_rows.append(task_id)
            continue
        task = registry.tasks[task_id]
        current_plan = plan_sha256(registry, task.initiative)
        if row.get("state") == "verified" and (
            row.get("task_sha256") != task.sha256 or row.get("plan_sha256") != current_plan
        ):
            stale_tasks.append(
                {
                    "task_id": task_id,
                    "stored_task_sha256": row.get("task_sha256"),
                    "current_task_sha256": task.sha256,
                    "stored_plan_sha256": row.get("plan_sha256"),
                    "current_plan_sha256": current_plan,
                    "receipt_sha256": row.get("receipt_sha256"),
                }
            )
    active_run_drift: list[dict[str, Any]] = []
    for row in state["rows"]["runs"]:
        if row.get("state") not in legacy.ACTIVE_STATES:
            continue
        task_id = row.get("task_id")
        if not isinstance(task_id, str) or task_id not in registry.tasks:
            active_run_drift.append(
                {"run_id": row.get("run_id"), "task_id": task_id, "reason": "unknown-task"}
            )
            continue
        task = registry.tasks[task_id]
        current_plan = plan_sha256(registry, task.initiative)
        if row.get("task_sha256") != task.sha256 or row.get("plan_sha256") != current_plan:
            active_run_drift.append(
                {
                    "run_id": row.get("run_id"),
                    "task_id": task_id,
                    "stored_task_sha256": row.get("task_sha256"),
                    "current_task_sha256": task.sha256,
                    "stored_plan_sha256": row.get("plan_sha256"),
                    "current_plan_sha256": current_plan,
                }
            )
    if stale_tasks:
        findings.append(
            {
                "severity": "blocker",
                "code": "receipt-drift",
                "message": "Verified task receipts no longer match current task or plan revisions.",
                "task_ids": [item["task_id"] for item in stale_tasks],
            }
        )
    else:
        findings.append(
            {
                "severity": "info",
                "code": "receipt-drift-clear",
                "message": "No verified receipt drift was found.",
            }
        )
    if unknown_status_rows:
        findings.append(
            {
                "severity": "warning",
                "code": "unknown-task-status-row",
                "message": (
                    "State database has task_status rows for tasks absent from the "
                    "registry."
                ),
                "task_ids": unknown_status_rows,
            }
        )
    if active_run_drift:
        findings.append(
            {
                "severity": "blocker",
                "code": "active-run-drift",
                "message": "Active runs no longer match current task or plan revisions.",
                "run_ids": [str(item.get("run_id")) for item in active_run_drift],
            }
        )
    return {
        "available": True,
        "stale_tasks": stale_tasks,
        "active_run_drift": active_run_drift,
        "unknown_task_status_rows": unknown_status_rows,
        "task_status_rows": len(task_status_rows),
        "receipt_rows": len(state["rows"]["receipts"]),
    }


def _runtime_status(findings: list[dict[str, Any]]) -> str:
    severities = {str(item.get("severity")) for item in findings}
    if "blocker" in severities:
        return "blocked"
    if "warning" in severities:
        return "warning"
    return "ok"


def runtime_drift_check(
    root: Path,
    *,
    state_db: Path | None = None,
    state_root: Path | None = None,
) -> dict[str, Any]:
    """Read-only runtime drift report for Bureau's local registry and state."""
    resolved_root = root.expanduser().resolve()
    findings: list[dict[str, Any]] = []
    checkout = _checkout_drift(resolved_root, findings)
    state_path = _runtime_state_db_path(state_db, state_root)
    state = _read_only_state_rows(state_path)
    runtime = {
        "root": str(resolved_root),
        "state_db": str(state_path),
        "state_available": state.get("available") is True,
        "state_integrity": state.get("integrity"),
        "state_schema_version": state.get("schema_version"),
        "read_only": True,
    }
    registry_report: dict[str, Any]
    receipt_report: dict[str, Any]
    try:
        registry = Registry.load(resolved_root)
    except legacy.BureauError as exc:
        findings.append(
            {
                "severity": "blocker",
                "code": "registry-invalid",
                "message": "Bureau registry failed validation.",
                "error": str(exc),
            }
        )
        registry_report = {"valid": False, "error": str(exc)}
        receipt_report = {"available": False, "stale_tasks": [], "unknown_task_status_rows": []}
    else:
        task_status_rows = state.get("rows", {}).get("task_status", [])
        overlays = _read_only_overlays(registry, task_status_rows)
        registry_report = _registry_drift(registry, overlays, findings)
        receipt_report = _receipt_drift(registry, state, findings)
    return {
        "schema_version": 1,
        "command": "runtime-drift-check",
        "read_only": True,
        "status": _runtime_status(findings),
        "runtime": runtime,
        "checkout": checkout,
        "registry": registry_report,
        "receipts": receipt_report,
        "findings": findings,
    }

def close_ready_initiatives(registry: Registry, store: StateStore) -> list[dict[str, Any]]:
    diagnostics = {item["initiative_id"]: item for item in lifecycle_diagnostics(registry, store)}
    changed: list[dict[str, Any]] = []
    for path in registry._files(registry.root / "registry/initiatives"):
        raw = legacy.read_json(path)
        diagnostic = diagnostics.get(raw.get("id"))
        if diagnostic is None or diagnostic["recommended_state"] != "completion-ready":
            continue
        raw["state"] = "completed"
        raw["commitment"] = "completed"
        metadata = raw.setdefault("metadata", {})
        lifecycle = metadata.setdefault("lifecycle", {})
        lifecycle["completed_at"] = legacy.utc_now()
        legacy.atomic_write(path, json.dumps(raw, indent=2, ensure_ascii=False) + "\n")
        changed.append({"initiative_id": raw["id"], "path": str(path)})
    return changed
