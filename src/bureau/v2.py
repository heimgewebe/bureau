from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import uuid
from collections.abc import Iterator
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


def _grabowski_worker_policy() -> dict[str, Any]:
    configured = os.environ.get("BUREAU_WORKER_ROUTING_CONFIG")
    path = Path(configured).expanduser() if configured else Path.home() / ".config/grabowski/worker-routing.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _external_agent_profile(task: legacy.Task, worker_id: str, kind: str) -> str | None:
    explicit = task.execution.get("worker_profile") or task.execution.get("preferred_worker_profile")
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
    ):
        super().__init__(registry, store)
        self.registry = registry
        self.store = store
        self.adapters = adapters or AdapterRegistry()

    def frontier(self, capabilities: set[str]) -> list[dict[str, Any]]:
        with self.store.connect() as connection:
            runs = self.store.active_runs(connection)
            reservations = self.store.reservations(connection)
            overlays = self.store.overlays(connection, self.registry)
            return [
                {
                    "task_id": task.id,
                    "title": task.title,
                    "effective_state": overlays.get(task.id, task.state),
                    "eligible": not (
                        reasons := self.reasons(task, capabilities, runs, reservations, overlays)
                    ),
                    "reasons": reasons,
                }
                for task in self.registry.ordered_tasks()
            ]

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
            worker = connection.execute(
                "SELECT * FROM workers WHERE worker_id=?", (worker_id,)
            ).fetchone()
            worker_capabilities = set(json.loads(worker["capabilities_json"]))
            runs = self.store.active_runs(connection)
            reservations = self.store.reservations(connection)
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
        eligible = next((item for item in frontier if item["eligible"]), None)
        return {"selected": eligible, "frontier": frontier}

    def doctor(self, repair: bool = False) -> dict[str, Any]:
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
            for lane, task_ids in self.registry.queue.items():
                for task_id in task_ids:
                    task = self.registry.tasks[task_id]
                    effective = overlays.get(task_id, task.state)
                    if effective != "ready":
                        queue_findings.append(
                            {"task_id": task_id, "lane": lane, "effective_state": effective}
                        )
        lifecycle = lifecycle_diagnostics(self.registry, self.store)
        lifecycle_findings = [item for item in lifecycle if not item["consistent"]]
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
        )
        return {
            "healthy": healthy,
            "database": integrity,
            "state_root": str(self.store.state_root),
            "missing_envelopes": missing_envelopes,
            "missing_receipts": missing_receipts,
            "stale_tasks": stale_tasks,
            "workspace_findings": workspace_findings,
            "queue_findings": queue_findings,
            "lifecycle": lifecycle,
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
    keys = set(task.execution.get("grabowski_resources", []))
    for claim in task.claims:
        key = registry.resources[claim.resource].grabowski_key
        if key:
            keys.add(key)
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
