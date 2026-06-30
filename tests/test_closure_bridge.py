from __future__ import annotations

import json
from pathlib import Path

from bureau.core import Dispatcher, Registry, StateStore
from bureau.v2 import closure_bridge_task_ids


def _setup(root: Path, tmp_path: Path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("BUREAU_STATE_DIR", str(state))
    registry = Registry.load(root)
    store = StateStore(state / "bureau.sqlite3")
    return registry, store, Dispatcher(registry, store)


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
                "briefs": [
                    {"lane_id": "lane-test", "path": str(brief), "valid": True}
                ],
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


def test_closure_selected_review_task_can_be_claimed(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    task_id = _make_completed_review_task(root)
    plan_path = tmp_path / "closure-plan.json"
    _write_plan(plan_path, task_id)
    monkeypatch.setenv("BUREAU_CLOSURE_PLAN", str(plan_path))

    registry, _store, dispatcher = _setup(root, tmp_path, monkeypatch)

    assert closure_bridge_task_ids(plan_path) == {task_id}
    explained = dispatcher.explain_next({"repository"})
    assert explained["selected"]["task_id"] == task_id
    claimed = dispatcher.claim_next("worker", ("repository",))
    assert claimed["run"]["task_id"] == task_id
    assert registry.tasks[task_id].policy == "review-before-effect"
