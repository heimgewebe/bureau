from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from bureau.core import Dispatcher, Registry, StateError, StateStore
from bureau.v2 import (
    close_ready_initiatives,
    closure_bridge_task_ids,
    plan_sha256,
    reconcile_initiative_lifecycle,
    task_revision_sha256,
)


def _setup(root: Path, tmp_path: Path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("BUREAU_STATE_DIR", str(state))
    registry = Registry.load(root)
    store = StateStore(state / "bureau.sqlite3")
    return registry, store, Dispatcher(registry, store)




def _move_to_next(root: Path, task_id: str) -> None:
    queue_path = root / "registry/queue.json"
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    for lane in queue["lanes"].values():
        while task_id in lane:
            lane.remove(task_id)
    queue["lanes"]["next"].append(task_id)
    queue_path.write_text(json.dumps(queue), encoding="utf-8")

def _write_plan(path: Path, task_id: str, *, valid: bool = True) -> None:
    brief = path.parent / f"{task_id}-brief.json"
    brief.write_text(json.dumps({"task_id": task_id}), encoding="utf-8")
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "selected_lane_count": 1,
                "canonical_task_bound_count": 1 if valid else 0,
                "unbound_selected_rejected_count": 1,
                "selected_lanes": [
                    {
                        "lane_id": "lane-test",
                        "task_id": task_id,
                        "state": "planned",
                        "metadata": {"canonical_task_binding": "manual-test"},
                        "grabowski_brief": str(brief),
                    }
                ],
                "briefs": [{"lane_id": "lane-test", "path": str(brief), "valid": True}],
            }
        ),
        encoding="utf-8",
    )


def _make_completed_review_task(root: Path) -> str:
    initiative_path = root / "registry/initiatives/main.json"
    initiative = json.loads(initiative_path.read_text(encoding="utf-8"))
    initiative["state"] = "completed"
    initiative["commitment"] = "completed"
    initiative_path.write_text(json.dumps(initiative), encoding="utf-8")

    task_path = root / "registry/tasks/BUR-TEST-001-T001.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task["state"] = "planned"
    task["execution"]["policy"] = "review-before-effect"
    _move_to_next(root, str(task["id"]))
    task_path.write_text(json.dumps(task), encoding="utf-8")
    return str(task["id"])


def test_bridge_accepts_registry_canonical_task_id_pattern(tmp_path):
    path = tmp_path / "plan.json"
    task_id = "OPS-CLOSURE-T001"
    _write_plan(path, task_id)
    assert closure_bridge_task_ids(path) == {task_id}


def test_bridge_honors_closure_state_root(tmp_path, monkeypatch):
    state_root = tmp_path / "closure-state"
    state_root.mkdir()
    task_id = "OPS-CLOSURE-T001"
    _write_plan(state_root / "plan.json", task_id)
    monkeypatch.delenv("BUREAU_CLOSURE_PLAN", raising=False)
    monkeypatch.setenv("BUREAU_CLOSURE_STATE_ROOT", str(state_root))

    assert closure_bridge_task_ids() == {task_id}


def test_bridge_rejects_unbound_plan(tmp_path):
    path = tmp_path / "plan.json"
    task_id = "OPS-CLOSURE-T001"
    _write_plan(path, task_id, valid=False)
    assert closure_bridge_task_ids(path) == set()


def test_closure_selected_review_task_can_be_claimed(registry_factory, tmp_path, monkeypatch):
    root = registry_factory(1)
    task_id = _make_completed_review_task(root)
    plan_path = tmp_path / "closure-plan.json"
    _write_plan(plan_path, task_id)
    monkeypatch.setenv("BUREAU_CLOSURE_PLAN", str(plan_path))

    registry, _store, dispatcher = _setup(root, tmp_path, monkeypatch)

    assert closure_bridge_task_ids(plan_path) == {task_id}
    explained = dispatcher.explain_next({"repository"})
    assert explained["selected"]["task_id"] == task_id
    assert explained["selected"]["closure_bridge"] is True
    assert explained["runtime_truth"]["selected_via"] == "closure_bridge"
    assert explained["runtime_truth"]["health_blocks_normal_claim"] is True
    assert explained["runtime_truth"]["repair_task_required"] is False
    claimed = dispatcher.claim_next("worker", ("repository",))
    assert claimed["run"]["task_id"] == task_id
    assert registry.tasks[task_id].policy == "review-before-effect"

def test_lifecycle_reconcile_preserves_planned_closure_bridge(
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

    plan_path = tmp_path / "closure-plan-reconcile.json"
    _write_plan(plan_path, task_id)
    monkeypatch.setenv("BUREAU_CLOSURE_PLAN", str(plan_path))
    registry, store, dispatcher = _setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)

    preview = reconcile_initiative_lifecycle(registry, store)
    assert preview["task_candidate_count"] == 0
    current = store.task_spec(task_id)
    assert current is not None
    assert current["spec"]["state"] == "planned"

    explained = dispatcher.explain_next({"repository"})
    assert explained["selected"]["task_id"] == task_id
    assert explained["selected"]["closure_bridge"] is True


def test_lifecycle_reconcile_preserves_bridge_during_registry_first_recovery(
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

    plan_path = tmp_path / "closure-plan-partial-recovery.json"
    _write_plan(plan_path, task_id)
    monkeypatch.setenv("BUREAU_CLOSURE_PLAN", str(plan_path))

    initiative_path = root / "registry/initiatives/main.json"
    completed_initiative = json.loads(initiative_path.read_text(encoding="utf-8"))
    stale_initiative = dict(completed_initiative)
    stale_initiative["state"] = "active"
    stale_initiative["commitment"] = "now"
    initiative_path.write_text(json.dumps(stale_initiative), encoding="utf-8")
    registry, store, _dispatcher = _setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    store.set_initiative_state("BUR-TEST-001", "completion-ready")
    initiative_path.write_text(json.dumps(completed_initiative), encoding="utf-8")

    preview = reconcile_initiative_lifecycle(registry, store)
    assert preview["task_candidate_count"] == 0
    current = store.task_spec(task_id)
    assert current is not None
    assert current["spec"]["state"] == "planned"

    applied = reconcile_initiative_lifecycle(registry, store, apply=True)
    assert applied["changed_task_count"] == 0
    assert applied["changed_count"] == 0
    current = store.task_spec(task_id)
    assert current is not None
    assert current["spec"]["state"] == "planned"

    closed = close_ready_initiatives(registry, store)
    assert [item["initiative_id"] for item in closed] == ["BUR-TEST-001"]
    with store.connect() as connection:
        initiative_state = connection.execute(
            "SELECT state FROM initiative_status WHERE initiative_id=?",
            ("BUR-TEST-001",),
        ).fetchone()
    assert initiative_state is not None
    assert initiative_state["state"] == "completed"
    current = store.task_spec(task_id)
    assert current is not None
    assert current["spec"]["state"] == "planned"

    dispatcher = Dispatcher(Registry.load(root), store)
    explained = dispatcher.explain_next({"repository"})
    assert explained["selected"]["task_id"] == task_id
    assert explained["selected"]["closure_bridge"] is True


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


def test_lifecycle_reconcile_rechecks_registry_first_recovery_inside_transaction(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    task_id = _make_completed_review_task(root)
    plan_path = tmp_path / "closure-plan-registry-race.json"
    _write_plan(plan_path, task_id)
    monkeypatch.setenv("BUREAU_CLOSURE_PLAN", str(plan_path))

    initiative_path = root / "registry/initiatives/main.json"
    completed_initiative = json.loads(initiative_path.read_text(encoding="utf-8"))
    stale_initiative = dict(completed_initiative)
    stale_initiative["state"] = "active"
    stale_initiative["commitment"] = "now"
    initiative_path.write_text(json.dumps(stale_initiative), encoding="utf-8")
    registry, store, _dispatcher = _setup(root, tmp_path, monkeypatch)
    store.import_registry_task_specs(registry)
    store.set_initiative_state("BUR-TEST-001", "completion-ready")

    preview = reconcile_initiative_lifecycle(registry, store)
    assert preview["task_candidate_count"] == 0
    assert preview["candidates"][0]["to_state"] == "active"

    original_immediate = store.immediate
    injected = False

    @contextmanager
    def complete_git_initiative_before_reconcile_transaction():
        nonlocal injected
        if not injected:
            injected = True
            initiative_path.write_text(
                json.dumps(completed_initiative), encoding="utf-8"
            )
        with original_immediate() as connection:
            yield connection

    monkeypatch.setattr(
        store, "immediate", complete_git_initiative_before_reconcile_transaction
    )
    with pytest.raises(
        StateError, match="initiative lifecycle inputs changed during reconcile"
    ):
        reconcile_initiative_lifecycle(registry, store, apply=True)

    with store.connect() as connection:
        initiative_state = connection.execute(
            "SELECT state FROM initiative_status WHERE initiative_id=?",
            ("BUR-TEST-001",),
        ).fetchone()
    assert initiative_state is not None
    assert initiative_state["state"] == "completion-ready"
    current = store.task_spec(task_id)
    assert current is not None
    assert current["spec"]["state"] == "planned"
