from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


source_path = Path("src/bureau/v2.py")
source = source_path.read_text(encoding="utf-8")

source = replace_once(
    source,
    '''def _state_store_initiative_registry(
    registry: Registry, store: StateStore
) -> tuple[Registry, dict[str, Any]]:
    valid_states = {
        "inbox",
        "candidate",
        "committed",
        "active",
        "waiting",
        "completion-ready",
        "completed",
        "dropped",
    }
    with store.connect() as connection:
        rows = list(
            connection.execute(
                "SELECT initiative_id,state,updated_at FROM initiative_status "
                "ORDER BY initiative_id"
            )
        )''',
    '''def _state_store_initiative_registry(
    registry: Registry,
    store: StateStore,
    *,
    connection: sqlite3.Connection | None = None,
) -> tuple[Registry, dict[str, Any]]:
    valid_states = {
        "inbox",
        "candidate",
        "committed",
        "active",
        "waiting",
        "completion-ready",
        "completed",
        "dropped",
    }
    if connection is None:
        with store.connect() as read_connection:
            return _state_store_initiative_registry(
                registry, store, connection=read_connection
            )
    rows = list(
        connection.execute(
            "SELECT initiative_id,state,updated_at FROM initiative_status "
            "ORDER BY initiative_id"
        )
    )''',
    "initiative authority connection binding",
)

source = replace_once(
    source,
    '''            fresh_overlays = store.overlays(connection, operational_registry)
            fresh_verification_stamps = _current_verification_stamps(
                operational_registry, connection
            )
            fresh_task_candidates = _structural_task_reconcile_candidates(
                operational_registry,
                fresh_overlays,
                task_revisions,
                fresh_verification_stamps,
            )''',
    '''            fresh_initiative_registry, _ = _state_store_initiative_registry(
                registry, store, connection=connection
            )
            fresh_operational_registry = copy.copy(operational_registry)
            fresh_operational_registry.initiatives = fresh_initiative_registry.initiatives
            fresh_overlays = store.overlays(connection, fresh_operational_registry)
            fresh_verification_stamps = _current_verification_stamps(
                fresh_operational_registry, connection
            )
            fresh_task_candidates = _structural_task_reconcile_candidates(
                fresh_operational_registry,
                fresh_overlays,
                task_revisions,
                fresh_verification_stamps,
            )''',
    "reconcile initiative authority refresh",
)

old_close = '''def close_ready_initiatives(registry: Registry, store: StateStore) -> list[dict[str, Any]]:
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
        lifecycle.setdefault("completed_at", legacy.utc_now())
        legacy.atomic_write(path, json.dumps(raw, indent=2, ensure_ascii=False) + "\\n")
        # Explicit closure must advance the StateStore overlay too.  Write the
        # Registry document first so an interrupted StateStore update remains
        # retryable: a stale completion-ready overlay will select this closure
        # again on the next invocation.
        store.set_initiative_state(raw["id"], "completed")
        changed.append({"initiative_id": raw["id"], "path": str(path)})
    return changed
'''
new_close = '''def close_ready_initiatives(registry: Registry, store: StateStore) -> list[dict[str, Any]]:
    operational_registry, task_authority, _ = authoritative_task_registry(registry, store)
    changed: list[dict[str, Any]] = []
    with store.immediate() as connection:
        if task_authority.get("kind") == "bureau-state-store-task-specs":
            try:
                current_projection = task_specs.current_projection(connection)
            except task_specs.TaskSpecError as exc:
                raise legacy.StateError(str(exc)) from exc
            if (
                task_specs.projection_root(current_projection)
                != task_authority.get("task_spec_root_sha256")
            ):
                raise legacy.StateError(
                    "TaskSpec projection changed during explicit closure"
                )

        fresh_initiative_registry, _ = _state_store_initiative_registry(
            registry, store, connection=connection
        )
        fresh_operational_registry = copy.copy(operational_registry)
        fresh_operational_registry.initiatives = fresh_initiative_registry.initiatives
        overlays = store.overlays(connection, fresh_operational_registry)
        verification_stamps = _current_verification_stamps(
            fresh_operational_registry, connection
        )
        diagnostics = {
            item["initiative_id"]: item
            for item in _lifecycle_diagnostics_from_overlays(
                fresh_operational_registry, registry, overlays, verification_stamps
            )
        }

        for path in registry._files(registry.root / "registry/initiatives"):
            raw = legacy.read_json(path)
            diagnostic = diagnostics.get(raw.get("id"))
            if diagnostic is None or diagnostic["recommended_state"] != "completion-ready":
                continue
            raw["state"] = "completed"
            raw["commitment"] = "completed"
            metadata = raw.setdefault("metadata", {})
            lifecycle = metadata.setdefault("lifecycle", {})
            lifecycle.setdefault("completed_at", legacy.utc_now())
            legacy.atomic_write(path, json.dumps(raw, indent=2, ensure_ascii=False) + "\\n")

            # Registry-first recovery remains intentional: if the DB write or
            # commit fails, the stale completion-ready row selects this already
            # completed file on retry.  Evidence and the authoritative StateStore
            # completion are nevertheless bound by this one write transaction.
            now = legacy.utc_now()
            connection.execute(
                "INSERT INTO initiative_status(initiative_id,state,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(initiative_id) DO UPDATE SET "
                "state=excluded.state,updated_at=excluded.updated_at",
                (raw["id"], "completed", now),
            )
            store.event(
                connection,
                "initiative-state-set",
                {"initiative_id": raw["id"], "state": "completed"},
                initiative_id=raw["id"],
            )
            changed.append({"initiative_id": raw["id"], "path": str(path)})
    return changed
'''
source = replace_once(source, old_close, new_close, "atomic close-ready evidence binding")
source_path.write_text(source, encoding="utf-8")


closure_tests_path = Path("tests/test_closure_bridge.py")
closure_tests = closure_tests_path.read_text(encoding="utf-8")
closure_tests = replace_once(
    closure_tests,
    '''import json
from pathlib import Path

from bureau.core import Dispatcher, Registry, StateStore''',
    '''import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from bureau.core import Dispatcher, Registry, StateError, StateStore''',
    "closure race test imports",
)
closure_race_test = r'''


def test_lifecycle_reconcile_rechecks_bridge_initiative_authority_inside_transaction(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(2)
    task_id = _make_completed_review_task(root)
    dependency_id = "BUR-TEST-001-T002"

    task_path = root / f"registry/tasks/{task_id}.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task["depends_on"] = [dependency_id]
    task_path.write_text(json.dumps(task), encoding="utf-8")

    preliminary = Registry.load(root)
    dependency_path = root / f"registry/tasks/{dependency_id}.json"
    dependency = json.loads(dependency_path.read_text(encoding="utf-8"))
    dependency["state"] = "verified"
    dependency.setdefault("metadata", {})["verification"] = {
        "task_sha256": task_revision_sha256(dependency),
        "plan_sha256": plan_sha256(preliminary, dependency["initiative"]),
    }
    dependency_path.write_text(json.dumps(dependency), encoding="utf-8")
    queue_path = root / "registry/queue.json"
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    for lane in queue["lanes"].values():
        while dependency_id in lane:
            lane.remove(dependency_id)
    queue_path.write_text(json.dumps(queue), encoding="utf-8")

    plan_path = tmp_path / "closure-plan-race.json"
    _write_plan(plan_path, task_id)
    monkeypatch.setenv("BUREAU_CLOSURE_PLAN", str(plan_path))
    registry, store, _dispatcher = _setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    store.set_initiative_state("BUR-TEST-001", "active")

    preview = reconcile_initiative_lifecycle(registry, store)
    assert preview["task_candidate_count"] == 1
    assert preview["task_candidates"][0]["task_id"] == task_id

    original_immediate = store.immediate
    injected = False

    @contextmanager
    def complete_initiative_before_reconcile_transaction():
        nonlocal injected
        if not injected:
            injected = True
            with original_immediate() as race_connection:
                race_connection.execute(
                    "INSERT INTO initiative_status(initiative_id,state,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(initiative_id) DO UPDATE SET "
                    "state=excluded.state,updated_at=excluded.updated_at",
                    ("BUR-TEST-001", "completed", "2026-08-09T06:00:00Z"),
                )
        with original_immediate() as connection:
            yield connection

    monkeypatch.setattr(store, "immediate", complete_initiative_before_reconcile_transaction)
    with pytest.raises(StateError, match="task lifecycle gates changed during lifecycle reconcile"):
        reconcile_initiative_lifecycle(registry, store, apply=True)

    current = store.task_spec(task_id)
    assert current is not None
    assert current["spec"]["state"] == "planned"
'''.rstrip() + "\n"
closure_tests = closure_tests.rstrip() + closure_race_test
closure_tests_path.write_text(closure_tests, encoding="utf-8")


tests_path = Path("tests/test_v2.py")
tests = tests_path.read_text(encoding="utf-8")
tests = replace_once(
    tests,
    '''import threading
import time
from concurrent.futures import ThreadPoolExecutor''',
    '''import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager''',
    "close-ready race test import",
)
close_race_test = r'''


def test_close_ready_revalidates_tasks_inside_completion_transaction(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    registry, store, _ = setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    task_id = next(iter(registry.tasks))

    current = store.task_spec(task_id)
    assert current is not None
    task_spec = json.loads(json.dumps(current["spec"]))
    task_spec["state"] = "verified"
    store.put_task_spec(
        task_spec,
        idempotency_key="close-race-verified-state",
        expected_revision=current["revision"],
        source="test",
    )

    operational, _, _ = bureau_v2.authoritative_task_registry(registry, store)
    current = store.task_spec(task_id)
    assert current is not None
    task_spec = json.loads(json.dumps(current["spec"]))
    task = operational.tasks[task_id]
    task_spec.setdefault("metadata", {})["verification"] = {
        "task_sha256": task.sha256,
        "plan_sha256": plan_sha256(operational, task.initiative),
    }
    store.put_task_spec(
        task_spec,
        idempotency_key="close-race-verification-evidence",
        expected_revision=current["revision"],
        source="test",
    )

    reconciled = bureau_v2.reconcile_initiative_lifecycle(registry, store, apply=True)
    assert reconciled["changed"][0]["to_state"] == "completion-ready"
    initiative_path = root / "registry/initiatives/main.json"
    initiative_before = initiative_path.read_bytes()

    original_immediate = store.immediate
    injected = False

    @contextmanager
    def mutate_task_before_close_transaction():
        nonlocal injected
        if not injected:
            injected = True
            with original_immediate() as race_connection:
                current_spec = bureau_v2.task_specs.get_current(race_connection, task_id)
                assert current_spec is not None
                mutated = json.loads(json.dumps(current_spec["spec"]))
                mutated["state"] = "planned"
                metadata = dict(mutated.get("metadata", {}))
                metadata.pop("verification", None)
                mutated["metadata"] = metadata
                bureau_v2.task_specs.put(
                    race_connection,
                    mutated,
                    idempotency_key="close-race-task-reopened",
                    expected_revision=int(current_spec["revision"]),
                    source="test",
                )
        with original_immediate() as connection:
            yield connection

    monkeypatch.setattr(store, "immediate", mutate_task_before_close_transaction)
    with pytest.raises(StateError, match="TaskSpec projection changed during explicit closure"):
        close_ready_initiatives(registry, store)

    assert initiative_path.read_bytes() == initiative_before
    with store.connect() as connection:
        row = connection.execute(
            "SELECT state FROM initiative_status WHERE initiative_id=?",
            ("BUR-TEST-001",),
        ).fetchone()
    assert row is not None
    assert row["state"] == "completion-ready"
'''.rstrip() + "\n"
marker = "def test_completed_lifecycle_accepts_mixed_terminal_task_states("
tests = replace_once(
    tests,
    marker,
    close_race_test + "\n\n" + marker,
    "close-ready race regression insertion",
)
tests_path.write_text(tests, encoding="utf-8")
