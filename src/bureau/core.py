from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ACTIVE_STATES = ("assigned", "running", "verifying")
LANE_ORDER = {"now": 0, "next": 1, "later": 2}
ID_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+$")
RESOURCE_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,254}$")


class BureauError(RuntimeError):
    """Base Bureau error."""


class ValidationError(BureauError):
    """Registry validation failed."""


class NoEligibleTask(BureauError):
    """No task can currently be assigned."""


class ConflictError(BureauError):
    """A requested claim conflicts with an active reservation."""


class StateError(BureauError):
    """An invalid state transition was requested."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def default_state_dir() -> Path:
    configured = os.environ.get("BUREAU_STATE_DIR")
    return (
        Path(configured).expanduser().resolve()
        if configured
        else (Path.home() / ".local/state/bureau").resolve()
    )


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValidationError(f"cannot parse {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{path} must contain one JSON object")
    return value


@dataclass(frozen=True)
class Resource:
    id: str
    type: str
    parent: str | None
    capacity: int | None
    path: str | None
    grabowski_key: str | None


@dataclass(frozen=True)
class Initiative:
    id: str
    title: str
    state: str
    commitment: str
    max_active_tasks: int
    current_plan: dict[str, Any] | None


@dataclass(frozen=True)
class Claim:
    resource: str
    mode: str
    amount: int = 1
    isolation: str = "none"

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> Claim:
        return cls(
            resource=str(raw["resource"]),
            mode=str(raw["mode"]),
            amount=int(raw.get("amount", 1)),
            isolation=str(raw.get("isolation", "none")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "resource": self.resource,
            "mode": self.mode,
            "amount": self.amount,
            "isolation": self.isolation,
        }


@dataclass(frozen=True)
class Task:
    id: str
    initiative: str
    title: str
    state: str
    depends_on: tuple[str, ...]
    capabilities: tuple[str, ...]
    lane: str
    rank: int
    execution: dict[str, Any]
    claims: tuple[Claim, ...]
    acceptance: tuple[dict[str, Any], ...]
    raw: dict[str, Any]
    sha256: str

    @property
    def policy(self) -> str:
        return str(self.execution["policy"])

    @property
    def mode(self) -> str:
        return str(self.execution["mode"])


@dataclass(frozen=True)
class Reservation:
    run_id: str
    resource: str
    mode: str
    amount: int


def ancestors(resource_id: str, resources: dict[str, Resource]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    current = resource_id
    while current in resources and resources[current].parent:
        parent = resources[current].parent
        if parent is None or parent in seen:
            break
        result.append(parent)
        seen.add(parent)
        current = parent
    return tuple(result)


def overlaps(left: str, right: str, resources: dict[str, Resource]) -> bool:
    return (
        left == right or left in ancestors(right, resources) or right in ancestors(left, resources)
    )


def modes_conflict(left: str, right: str) -> bool:
    if "exclusive" in {left, right}:
        return True
    if left == right == "read":
        return False
    if left == right == "capacity":
        return False
    if {left, right} == {"capacity", "read"}:
        return False
    return "write" in {left, right} or left != right


def claim_conflicts(
    claim: Claim,
    active: list[Reservation],
    resources: dict[str, Resource],
) -> list[str]:
    reasons: list[str] = []
    for held in active:
        if overlaps(claim.resource, held.resource, resources) and modes_conflict(
            claim.mode, held.mode
        ):
            reasons.append(
                f"{claim.resource}:{claim.mode} conflicts with "
                f"{held.resource}:{held.mode} held by {held.run_id}"
            )
    if claim.mode == "capacity":
        resource = resources[claim.resource]
        if resource.capacity is None:
            reasons.append(f"capacity resource {claim.resource} has no capacity")
        else:
            used = sum(
                item.amount
                for item in active
                if item.resource == claim.resource and item.mode == "capacity"
            )
            if used + claim.amount > resource.capacity:
                reasons.append(
                    f"{claim.resource} capacity exceeded: {used}+{claim.amount}>{resource.capacity}"
                )
    return reasons


class Registry:
    """Durable Git-backed Bureau registry."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.resources: dict[str, Resource] = {}
        self.initiatives: dict[str, Initiative] = {}
        self.tasks: dict[str, Task] = {}
        self.queue: dict[str, list[str]] = {"now": [], "next": [], "later": []}
        self.queue_policy = "skip-blocked"
        self.positions: dict[str, tuple[int, int]] = {}

    @classmethod
    def load(cls, root: str | Path) -> Registry:
        instance = cls(Path(root))
        instance._load()
        instance.validate()
        return instance

    @staticmethod
    def _files(path: Path) -> list[Path]:
        return sorted(path.glob("*.json")) if path.exists() else []

    def _load(self) -> None:
        for path in self._files(self.root / "registry/resources"):
            raw = read_json(path)
            item = Resource(
                id=str(raw.get("id", "")),
                type=str(raw.get("type", "")),
                parent=raw.get("parent"),
                capacity=raw.get("capacity"),
                path=raw.get("path"),
                grabowski_key=raw.get("grabowski_key"),
            )
            self._unique(self.resources, item.id, item, path)
        for path in self._files(self.root / "registry/initiatives"):
            raw = read_json(path)
            item = Initiative(
                id=str(raw.get("id", "")),
                title=str(raw.get("title", "")),
                state=str(raw.get("state", "")),
                commitment=str(raw.get("commitment", "")),
                max_active_tasks=int(raw.get("parallelism", {}).get("max_active_tasks", 1)),
                current_plan=raw.get("current_plan"),
            )
            self._unique(self.initiatives, item.id, item, path)
        for path in self._files(self.root / "registry/tasks"):
            raw = read_json(path)
            acceptance: list[dict[str, Any]] = []
            for index, criterion in enumerate(raw.get("acceptance", []), 1):
                if isinstance(criterion, str):
                    acceptance.append({"id": f"criterion-{index}", "assertion": criterion})
                elif isinstance(criterion, dict):
                    acceptance.append(dict(criterion))
                else:
                    raise ValidationError(f"invalid acceptance criterion in {path}")
            priority = raw.get("priority", {})
            item = Task(
                id=str(raw.get("id", "")),
                initiative=str(raw.get("initiative", "")),
                title=str(raw.get("title", "")),
                state=str(raw.get("state", "")),
                depends_on=tuple(raw.get("depends_on", [])),
                capabilities=tuple(sorted(set(raw.get("required_capabilities", [])))),
                lane=str(priority.get("lane", "later")),
                rank=int(priority.get("rank", 1000)),
                execution=dict(raw.get("execution", {})),
                claims=tuple(Claim.from_raw(value) for value in raw.get("claims", [])),
                acceptance=tuple(acceptance),
                raw=raw,
                sha256=sha256_json(raw),
            )
            self._unique(self.tasks, item.id, item, path)
        queue_path = self.root / "registry/queue.json"
        if queue_path.exists():
            raw = read_json(queue_path)
            self.queue_policy = str(raw.get("queue_policy", "skip-blocked"))
            lanes = raw.get("lanes", {})
            for lane in LANE_ORDER:
                self.queue[lane] = list(lanes.get(lane, []))
                for index, task_id in enumerate(self.queue[lane]):
                    if task_id in self.positions:
                        raise ValidationError(f"task {task_id} appears twice in queue")
                    self.positions[task_id] = (LANE_ORDER[lane], index)

    @staticmethod
    def _unique(mapping: dict[str, Any], key: str, item: Any, path: Path) -> None:
        if not key:
            raise ValidationError(f"missing id in {path}")
        if key in mapping:
            raise ValidationError(f"duplicate id {key} in {path}")
        mapping[key] = item

    def validate(self) -> None:
        errors: list[str] = []
        initiative_states = {
            "inbox",
            "candidate",
            "committed",
            "active",
            "waiting",
            "completed",
            "dropped",
        }
        commitments = {"now", "next", "later", "waiting", "someday"}
        task_states = {
            "inbox",
            "planned",
            "ready",
            "blocked",
            "verified",
            "cancelled",
            "superseded",
        }
        modes = {"read", "write", "exclusive", "capacity"}
        policies = {"autonomous", "review-before-effect", "manual", "prohibited"}
        execution_modes = {"interactive-agent", "grabowski-task", "grabowski-operation", "manual"}

        for resource in self.resources.values():
            if not RESOURCE_RE.fullmatch(resource.id):
                errors.append(f"invalid resource id {resource.id}")
            if resource.parent and resource.parent not in self.resources:
                errors.append(f"resource {resource.id} has unknown parent {resource.parent}")
            if resource.type == "capacity" and (not resource.capacity or resource.capacity < 1):
                errors.append(f"capacity resource {resource.id} needs positive capacity")
        errors.extend(self._resource_cycles())

        for initiative in self.initiatives.values():
            if not ID_RE.fullmatch(initiative.id):
                errors.append(f"invalid initiative id {initiative.id}")
            if not initiative.title:
                errors.append(f"initiative {initiative.id} has no title")
            if initiative.state not in initiative_states:
                errors.append(f"initiative {initiative.id} has invalid state {initiative.state}")
            if initiative.commitment not in commitments:
                errors.append(
                    f"initiative {initiative.id} has invalid commitment {initiative.commitment}"
                )
            if initiative.max_active_tasks < 1:
                errors.append(f"initiative {initiative.id} has invalid parallelism")

        for task in self.tasks.values():
            if not ID_RE.fullmatch(task.id):
                errors.append(f"invalid task id {task.id}")
            if task.initiative not in self.initiatives:
                errors.append(f"task {task.id} has unknown initiative {task.initiative}")
            if task.state not in task_states:
                errors.append(f"task {task.id} has invalid state {task.state}")
            if task.lane not in LANE_ORDER:
                errors.append(f"task {task.id} has invalid lane {task.lane}")
            if not task.acceptance:
                errors.append(f"task {task.id} has no acceptance criteria")
            criterion_ids = [criterion.get("id") for criterion in task.acceptance]
            if None in criterion_ids or len(set(criterion_ids)) != len(criterion_ids):
                errors.append(f"task {task.id} has invalid acceptance IDs")
            if task.execution.get("policy") not in policies:
                errors.append(f"task {task.id} has invalid execution policy")
            if task.execution.get("mode") not in execution_modes:
                errors.append(f"task {task.id} has invalid execution mode")
            if task.mode == "manual" and task.policy == "autonomous":
                errors.append(f"manual task {task.id} cannot be autonomous")
            for dependency in task.depends_on:
                if dependency not in self.tasks:
                    errors.append(f"task {task.id} has unknown dependency {dependency}")
            for claim in task.claims:
                if claim.resource not in self.resources:
                    errors.append(f"task {task.id} claims unknown resource {claim.resource}")
                if claim.mode not in modes:
                    errors.append(f"task {task.id} has invalid claim mode {claim.mode}")
                if claim.amount < 1:
                    errors.append(f"task {task.id} has invalid claim amount")
                if claim.resource in self.resources and claim.mode == "capacity":
                    limit = self.resources[claim.resource].capacity
                    if limit is None or claim.amount > limit:
                        errors.append(f"task {task.id} exceeds capacity {claim.resource}")
        errors.extend(self._task_cycles())
        for lane, task_ids in self.queue.items():
            for task_id in task_ids:
                if task_id not in self.tasks:
                    errors.append(f"queue {lane} has unknown task {task_id}")
        if errors:
            raise ValidationError("\n".join(sorted(set(errors))))

    def _resource_cycles(self) -> list[str]:
        errors: list[str] = []
        for resource_id in self.resources:
            seen = {resource_id}
            current = resource_id
            while current in self.resources and self.resources[current].parent:
                parent = self.resources[current].parent
                if parent in seen:
                    errors.append(f"resource parent cycle contains {resource_id}")
                    break
                if parent is None:
                    break
                seen.add(parent)
                current = parent
        return errors

    def _task_cycles(self) -> list[str]:
        visiting: set[str] = set()
        visited: set[str] = set()
        errors: list[str] = []

        def visit(node: str, path: list[str]) -> None:
            if node in visiting:
                start = path.index(node)
                errors.append("dependency cycle: " + " -> ".join([*path[start:], node]))
                return
            if node in visited or node not in self.tasks:
                return
            visiting.add(node)
            for dependency in self.tasks[node].depends_on:
                visit(dependency, [*path, dependency])
            visiting.remove(node)
            visited.add(node)

        for task_id in self.tasks:
            visit(task_id, [task_id])
        return errors

    def ordered_tasks(self) -> list[Task]:
        def key(task: Task) -> tuple[int, int, int, str]:
            position = self.positions.get(task.id)
            if position:
                return position[0], position[1], task.rank, task.id
            return LANE_ORDER[task.lane], 10_000_000, task.rank, task.id

        return sorted(self.tasks.values(), key=key)

    def summary(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "initiatives": len(self.initiatives),
            "tasks": len(self.tasks),
            "resources": len(self.resources),
            "queue_policy": self.queue_policy,
        }


class StateStore:
    """Operational SQLite state for workers, runs and reservations."""

    def __init__(self, path: Path | None = None):
        self.path = (path or default_state_dir() / "bureau.sqlite3").resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._initialize()

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

    def _initialize(self) -> None:
        with self.connect() as connection:
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
                    envelope_json TEXT NOT NULL,
                    envelope_sha256 TEXT NOT NULL,
                    external_system TEXT,
                    external_id TEXT,
                    workspace_path TEXT,
                    workspace_branch TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    FOREIGN KEY(worker_id) REFERENCES workers(worker_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_run_per_task
                    ON runs(task_id) WHERE state IN ('assigned','running','verifying');
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_run_per_worker
                    ON runs(worker_id) WHERE state IN ('assigned','running','verifying');
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
                """
            )

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

    def register_worker(self, worker_id: str, kind: str, capabilities: tuple[str, ...]) -> None:
        if not worker_id or len(worker_id) > 200:
            raise StateError("worker_id must contain 1-200 characters")
        now = utc_now()
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
                (worker_id, kind, canonical_json(list(capabilities)), now),
            )

    def active_runs(self, connection: sqlite3.Connection) -> list[sqlite3.Row]:
        return connection.execute(
            "SELECT * FROM runs WHERE state IN ('assigned','running','verifying')"
        ).fetchall()

    def reservations(
        self, connection: sqlite3.Connection, exclude: str | None = None
    ) -> list[Reservation]:
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
            Reservation(row["run_id"], row["resource_id"], row["mode"], row["amount"])
            for row in connection.execute(sql, params)
        ]

    def overlays(self, connection: sqlite3.Connection) -> dict[str, str]:
        return {
            row["task_id"]: row["state"]
            for row in connection.execute("SELECT task_id,state FROM task_status")
        }

    def public_run(self, row: sqlite3.Row) -> dict[str, Any]:
        return {key: row[key] for key in row.keys() if key != "envelope_json"}  # noqa: SIM118

    def run(self, run_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise StateError(f"unknown run {run_id}")
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
        now = utc_now()
        with self.immediate() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None or row["state"] not in ACTIVE_STATES:
                raise StateError(f"run {run_id} is not active")
            if row["external_system"] and (
                row["external_system"] != system or row["external_id"] != external_id
            ):
                raise StateError("run already has another external binding")
            connection.execute(
                "UPDATE runs SET external_system=?,external_id=?,state='running',updated_at=?,heartbeat_at=? WHERE run_id=?",  # noqa: E501
                (system, external_id, now, now, run_id),
            )
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return self.public_run(row)


class Dispatcher:
    """Deterministic frontier and atomic assignment."""

    def __init__(self, registry: Registry, store: StateStore):
        self.registry = registry
        self.store = store

    def reasons(
        self,
        task: Task,
        capabilities: set[str],
        runs: list[sqlite3.Row],
        reservations: list[Reservation],
        overlays: dict[str, str],
    ) -> list[str]:
        result: list[str] = []
        initiative = self.registry.initiatives[task.initiative]
        state = overlays.get(task.id, task.state)
        if state != "ready":
            result.append(f"state is {state}")
        if initiative.state != "active":
            result.append(f"initiative state is {initiative.state}")
        if initiative.commitment not in {"now", "next"}:
            result.append(f"initiative commitment is {initiative.commitment}")
        if task.policy != "autonomous" or task.mode == "manual":
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
            result.extend(claim_conflicts(claim, reservations, self.registry.resources))
        return result

    def frontier(self, capabilities: set[str]) -> list[dict[str, Any]]:
        with self.store.connect() as connection:
            runs = self.store.active_runs(connection)
            reservations = self.store.reservations(connection)
            overlays = self.store.overlays(connection)
            return [
                {
                    "task_id": task.id,
                    "title": task.title,
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
    ) -> dict[str, Any]:
        self.store.register_worker(worker_id, kind, capabilities)
        envelope: dict[str, Any] | None = None
        run: dict[str, Any] | None = None
        with self.store.immediate() as connection:
            current = connection.execute(
                "SELECT * FROM runs WHERE worker_id=? AND state IN ('assigned','running','verifying')",  # noqa: E501
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
            overlays = self.store.overlays(connection)
            rejected: list[dict[str, Any]] = []
            selected: Task | None = None
            for task in self.registry.ordered_tasks():
                reasons = self.reasons(task, worker_capabilities, runs, reservations, overlays)
                if not reasons:
                    selected = task
                    break
                rejected.append({"task_id": task.id, "reasons": reasons})
                if self.registry.queue_policy == "strict" and task.state == "ready":
                    break
            if selected is None:
                raise NoEligibleTask(canonical_json({"rejected": rejected}))
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
            now = utc_now()
            initiative = self.registry.initiatives[selected.initiative]
            envelope = {
                "schema_version": 1,
                "run_id": run_id,
                "task_id": selected.id,
                "worker_id": worker_id,
                "task_sha256": selected.sha256,
                "created_at": now,
                "task": selected.raw,
                "claims": [claim.as_dict() for claim in selected.claims],
                "plan": initiative.current_plan,
            }
            envelope_json = canonical_json(envelope)
            envelope_sha = sha256_json(envelope)
            connection.execute(
                """
                INSERT INTO runs(run_id,task_id,worker_id,attempt,state,task_sha256,
                    envelope_json,envelope_sha256,created_at,updated_at,heartbeat_at)
                VALUES(?,?,?,?,'assigned',?,?,?,?,?,?)
                """,
                (
                    run_id,
                    selected.id,
                    worker_id,
                    attempt,
                    selected.sha256,
                    envelope_json,
                    envelope_sha,
                    now,
                    now,
                    now,
                ),
            )
            for claim in selected.claims:
                connection.execute(
                    "INSERT INTO reservations(run_id,resource_id,mode,amount,created_at) VALUES(?,?,?,?,?)",  # noqa: E501
                    (run_id, claim.resource, claim.mode, claim.amount, now),
                )
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            run = self.store.public_run(row)
        assert envelope is not None and run is not None
        path = default_state_dir() / "envelopes" / f"{run['run_id']}.json"
        atomic_write(path, json.dumps(envelope, indent=2, ensure_ascii=False) + "\n")
        return {"status": "claimed", "run": run, "envelope": envelope, "envelope_path": str(path)}

    def expand_claim(self, run_id: str, claim: Claim, reason: str) -> dict[str, Any]:
        if claim.resource not in self.registry.resources:
            raise StateError(f"unknown resource {claim.resource}")
        with self.store.immediate() as connection:
            run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None or run["state"] not in ACTIVE_STATES:
                raise StateError(f"run {run_id} is not active")
            if connection.execute(
                "SELECT 1 FROM reservations WHERE run_id=? AND resource_id=?",
                (run_id, claim.resource),
            ).fetchone():
                raise StateError(f"run already reserves {claim.resource}")
            reasons = claim_conflicts(
                claim, self.store.reservations(connection, exclude=run_id), self.registry.resources
            )
            if reasons:
                raise ConflictError("\n".join(reasons))
            connection.execute(
                "INSERT INTO reservations(run_id,resource_id,mode,amount,created_at) VALUES(?,?,?,?,?)",  # noqa: E501
                (run_id, claim.resource, claim.mode, claim.amount, utc_now()),
            )
            connection.execute(
                "UPDATE runs SET error=?,updated_at=? WHERE run_id=?",
                (f"claim expanded: {reason}", utc_now(), run_id),
            )
        return self.store.run(run_id)

    def reconcile(self, stale_after: int = 900) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        orphaned: list[str] = []
        reconstructed: list[str] = []
        with self.store.immediate() as connection:
            for row in self.store.active_runs(connection):
                path = default_state_dir() / "envelopes" / f"{row['run_id']}.json"
                if not path.exists():
                    atomic_write(
                        path, json.dumps(json.loads(row["envelope_json"]), indent=2) + "\n"
                    )
                    reconstructed.append(row["run_id"])
                if (now - parse_time(row["heartbeat_at"])).total_seconds() <= stale_after:
                    continue
                if row["external_system"] and row["external_id"]:
                    continue
                connection.execute(
                    "UPDATE runs SET state='orphaned',error=?,updated_at=? WHERE run_id=?",
                    ("stale worker without external executor", utc_now(), row["run_id"]),
                )
                connection.execute("DELETE FROM reservations WHERE run_id=?", (row["run_id"],))
                orphaned.append(row["run_id"])
        return {"orphaned": orphaned, "reconstructed_envelopes": reconstructed}

    def conflict_matrix(self) -> list[dict[str, Any]]:
        tasks = self.registry.ordered_tasks()
        matrix: list[dict[str, Any]] = []
        for index, left in enumerate(tasks):
            held = [
                Reservation("other", claim.resource, claim.mode, claim.amount)
                for claim in left.claims
            ]
            for right in tasks[index + 1 :]:
                reasons: list[str] = []
                for claim in right.claims:
                    reasons.extend(claim_conflicts(claim, held, self.registry.resources))
                if reasons:
                    matrix.append({"left": left.id, "right": right.id, "reasons": reasons})
        return matrix


def complete_run(
    registry: Registry,
    store: StateStore,
    run_id: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    run = store.run(run_id)
    if run["state"] not in ACTIVE_STATES:
        raise StateError(f"run {run_id} is not active")
    with store.connect() as connection:
        envelope = json.loads(
            connection.execute(
                "SELECT envelope_json FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        )
    required = []
    for index, criterion in enumerate(envelope["task"]["acceptance"], 1):
        required.append(criterion["id"] if isinstance(criterion, dict) else f"criterion-{index}")
    missing = sorted(set(required) - set(evidence))
    if missing:
        raise StateError("missing evidence for: " + ", ".join(missing))
    receipt = {
        "schema_version": 1,
        "run_id": run_id,
        "task_id": run["task_id"],
        "task_sha256": run["task_sha256"],
        "envelope_sha256": run["envelope_sha256"],
        "verified_at": utc_now(),
        "external": (
            {"system": run["external_system"], "id": run["external_id"]}
            if run["external_system"]
            else None
        ),
        "evidence": {key: evidence[key] for key in required},
    }
    receipt_sha = sha256_json(receipt)
    receipt["receipt_sha256"] = receipt_sha
    path = default_state_dir() / "receipts" / f"{run_id}.json"
    atomic_write(path, json.dumps(receipt, indent=2, ensure_ascii=False) + "\n")
    now = utc_now()
    with store.immediate() as connection:
        connection.execute(
            "INSERT INTO receipts(run_id,receipt_json,receipt_sha256,created_at) VALUES(?,?,?,?)",
            (run_id, canonical_json(receipt), receipt_sha, now),
        )
        connection.execute(
            """
            INSERT INTO task_status(task_id,state,receipt_sha256,updated_at)
            VALUES(?,'verified',?,?)
            ON CONFLICT(task_id) DO UPDATE SET state='verified',receipt_sha256=excluded.receipt_sha256,updated_at=excluded.updated_at
            """,  # noqa: E501
            (run["task_id"], receipt_sha, now),
        )
        connection.execute(
            "UPDATE runs SET state='succeeded',updated_at=? WHERE run_id=?", (now, run_id)
        )
        connection.execute("DELETE FROM reservations WHERE run_id=?", (run_id,))
    return {"receipt": receipt, "receipt_path": str(path)}


def fail_run(store: StateStore, run_id: str, error: str, state: str = "failed") -> dict[str, Any]:
    if state not in {"failed", "cancelled", "orphaned"}:
        raise StateError(f"invalid terminal state {state}")
    with store.immediate() as connection:
        row = connection.execute("SELECT state FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None or row["state"] not in ACTIVE_STATES:
            raise StateError(f"run {run_id} is not active")
        connection.execute(
            "UPDATE runs SET state=?,error=?,updated_at=? WHERE run_id=?",
            (state, error, utc_now(), run_id),
        )
        connection.execute("DELETE FROM reservations WHERE run_id=?", (run_id,))
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
        "request_id": f"{run_id}:dispatch-1",
        "run_id": run_id,
        "task_id": task.id,
        "task_sha256": run["task_sha256"],
        "envelope_sha256": run["envelope_sha256"],
        "envelope_path": str(default_state_dir() / "envelopes" / f"{run_id}.json"),
        "mode": task.mode,
        "policy": task.policy,
        "host": task.execution.get("preferred_host", "heim-pc"),
        "cwd": run["workspace_path"] or task.execution.get("cwd"),
        "resource_keys": sorted(keys),
        "acceptance": list(task.acceptance),
    }
    if task.mode == "grabowski-task":
        result.update(
            argv=task.execution["argv"],
            runtime_seconds=int(task.execution.get("runtime_seconds", 7200)),
            resume_policy=task.execution.get("resume_policy", "verify-then-retry"),
        )
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
        raise StateError(f"task {task.id} has no working_repository")
    if not any(claim.isolation == "worktree" for claim in task.claims):
        raise StateError(f"task {task.id} has no worktree-isolated claim")
    if run["workspace_path"]:
        return run
    repo = Path(repository).expanduser().resolve()
    if subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--git-dir"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode:
        raise StateError(f"not a Git repository: {repo}")
    root = base_dir.resolve() if base_dir else repo.parent / ".bureau-worktrees"
    destination = root / run_id
    root.mkdir(parents=True, exist_ok=True)
    branch_task = re.sub(r"[^A-Za-z0-9._-]+", "-", task.id).lower()
    branch = f"bureau/{branch_task}/{run_id.rsplit('-', 1)[-1].lower()}"
    result = subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", branch, str(destination), "HEAD"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise StateError(result.stderr.strip() or result.stdout.strip())
    with store.immediate() as connection:
        connection.execute(
            "UPDATE runs SET workspace_path=?,workspace_branch=?,updated_at=? WHERE run_id=?",
            (str(destination), branch, utc_now(), run_id),
        )
    return store.run(run_id)
