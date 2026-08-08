from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bureau import cli as bureau_cli
from bureau import legacy
from bureau import queue_reconcile as queue_reconcile_module
from bureau import registry_truth as registry_truth_module
from bureau.core import Dispatcher, Registry, StateStore
from bureau.queue_reconcile import (
    apply_queue_reconcile_plan,
    queue_reconcile_report,
    write_queue_reconcile_plan,
)


def _task_path(root: Path, task_id: str) -> Path:
    return root / "registry/tasks" / f"{task_id}.json"


def _queue_path(root: Path) -> Path:
    return root / "registry/queue.json"


def _read_queue(root: Path) -> dict:
    return json.loads(_queue_path(root).read_text())


def _remove_from_queue(root: Path, task_id: str) -> None:
    queue = _read_queue(root)
    for lane in queue["lanes"].values():
        while task_id in lane:
            lane.remove(task_id)
    _queue_path(root).write_text(json.dumps(queue))


def _move_in_queue(root: Path, task_id: str, target_lane: str) -> None:
    queue = _read_queue(root)
    for lane in queue["lanes"].values():
        while task_id in lane:
            lane.remove(task_id)
    queue["lanes"][target_lane].append(task_id)
    _queue_path(root).write_text(json.dumps(queue))


def _set_task(root: Path, task_id: str, **changes) -> None:
    path = _task_path(root, task_id)
    task = json.loads(path.read_text())
    for key, value in changes.items():
        if key == "priority_lane":
            task.setdefault("priority", {})["lane"] = value
        elif key == "priority_rank":
            task.setdefault("priority", {})["rank"] = value
        else:
            task[key] = value
    path.write_text(json.dumps(task))


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def _git_init(root: Path) -> str:
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "bureau-tests@example.invalid")
    _git(root, "config", "user.name", "Bureau Tests")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "fixture")
    return _git(root, "rev-parse", "HEAD")


def _review_plan(path: Path) -> dict:
    plan = json.loads(path.read_text())
    plan["review"] = {
        "required": True,
        "status": "reviewed",
        "reviewer": "test-reviewer",
        "reviewed_at": "2026-08-08T00:00:00Z",
    }
    path.write_text(json.dumps(plan))
    return plan


def _report(root: Path, store: StateStore, *, resource: str | None = None) -> dict:
    return queue_reconcile_report(
        Registry.load(root), store, resource=resource, _check_runtime=False
    )


@pytest.fixture(autouse=True)
def clear_runtime_execution_gate(monkeypatch):
    monkeypatch.setattr(
        Dispatcher,
        "_runtime_execution_truth",
        lambda self: {
            "schema_version": 1,
            "status": "clear",
            "execution_blocked": False,
            "blocker_codes": [],
        },
    )


def test_report_marks_queue_as_compatibility_only_and_finds_unqueued_now(
    registry_factory, tmp_path
):
    root = registry_factory(1)
    task_id = "BUR-TEST-001-T001"
    _remove_from_queue(root, task_id)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3", tmp_path / "state")

    report = _report(root, store)

    assert report["read_only"] is True
    assert report["queue_canonical"] is False
    assert report["queue_authoritative"] is False
    assert report["queue_role"] == "compatibility_projection_only"
    finding = next(item for item in report["findings"] if item.get("task_id") == task_id)
    assert finding["code"] == "unqueued-ready-priority-now"
    assert finding["proposed_action"] == {
        "operation": "add_to_queue",
        "target_lane": "now",
    }
    assert report["compatibility_queue"]["lanes"]["now"] == [task_id]


def test_ready_next_is_projected_without_manual_admission_when_now_supply_is_full(
    registry_factory, tmp_path
):
    root = registry_factory(3)
    task_id = "BUR-TEST-001-T003"
    _set_task(root, task_id, priority_lane="next")
    _remove_from_queue(root, task_id)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3", tmp_path / "state")

    report = _report(root, store)

    finding = next(item for item in report["findings"] if item.get("task_id") == task_id)
    assert finding["code"] == "unqueued-open-priority-next"
    assert task_id in report["compatibility_queue"]["lanes"]["next"]
    assert report["summary"]["promote_to_next_candidates"] == 1


def test_later_queue_entry_is_moved_to_projected_now(registry_factory, tmp_path):
    root = registry_factory(1)
    task_id = "BUR-TEST-001-T001"
    _move_in_queue(root, task_id, "later")
    store = StateStore(tmp_path / "state" / "bureau.sqlite3", tmp_path / "state")

    report = _report(root, store)

    finding = next(item for item in report["findings"] if item.get("task_id") == task_id)
    assert finding["code"] == "queued-later-priority-now-or-next"
    assert finding["proposed_action"] == {
        "operation": "move_in_queue",
        "target_lane": "now",
    }


def test_report_is_read_only(registry_factory, tmp_path):
    root = registry_factory(2)
    _remove_from_queue(root, "BUR-TEST-001-T002")
    queue_before = _queue_path(root).read_bytes()
    tasks_before = {
        path.name: path.read_bytes() for path in (root / "registry/tasks").glob("*.json")
    }
    store = StateStore(tmp_path / "state" / "bureau.sqlite3", tmp_path / "state")

    _report(root, store)

    assert _queue_path(root).read_bytes() == queue_before
    assert {
        path.name: path.read_bytes() for path in (root / "registry/tasks").glob("*.json")
    } == tasks_before


def test_resource_filter_preserves_unrelated_compatibility_queue_entries(
    registry_factory, tmp_path
):
    root = registry_factory(2)
    alpha = "BUR-TEST-001-T001"
    beta = "BUR-TEST-001-T002"
    _remove_from_queue(root, beta)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3", tmp_path / "state")

    report = _report(root, store, resource="repo.beta")

    finding_ids = {
        item["task_id"] for item in report["findings"] if isinstance(item.get("task_id"), str)
    }
    assert finding_ids == {beta}
    assert alpha in report["compatibility_queue"]["lanes"]["now"]
    assert beta in report["compatibility_queue"]["lanes"]["now"]
    assert report["resource"] == "repo.beta"


def test_state_store_terminal_task_is_removed_from_compatibility_projection(
    registry_factory, tmp_path
):
    root = registry_factory(1)
    task_id = "BUR-TEST-001-T001"
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3", tmp_path / "state")
    store.import_registry_task_specs(registry)
    current = store.task_spec(task_id)
    assert current is not None
    spec = dict(current["spec"])
    spec["state"] = "cancelled"
    store.put_task_spec(
        spec,
        idempotency_key="queue-reconcile-terminal-state-store",
        expected_revision=1,
        source="test",
    )

    report = _report(root, store)

    assert task_id not in {
        task
        for lane in report["compatibility_queue"]["lanes"].values()
        for task in lane
    }
    finding = next(item for item in report["findings"] if item.get("task_id") == task_id)
    assert finding["code"] == "terminal-task-in-queue"
    assert finding["proposed_action"]["operation"] == "remove_from_queue"


def test_write_plan_requires_review_before_apply(registry_factory, tmp_path):
    root = registry_factory(1)
    task_id = "BUR-TEST-001-T001"
    _remove_from_queue(root, task_id)
    _git_init(root)
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3", tmp_path / "state")
    plan_path = tmp_path / "plans" / "queue-plan.json"

    plan = write_queue_reconcile_plan(registry, store, plan_path)

    assert plan["review"]["status"] == "pending"
    assert plan["expected_queue_after"]["lanes"]["now"] == [task_id]
    with pytest.raises(legacy.StateError, match="not reviewed"):
        apply_queue_reconcile_plan(registry, store, plan_path)


def test_reviewed_plan_materializes_full_compatibility_projection(
    registry_factory, tmp_path
):
    root = registry_factory(2)
    task_id = "BUR-TEST-001-T002"
    _remove_from_queue(root, task_id)
    _git_init(root)
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3", tmp_path / "state")
    plan_path = tmp_path / "plans" / "queue-plan.json"
    plan = write_queue_reconcile_plan(registry, store, plan_path)
    _review_plan(plan_path)

    result = apply_queue_reconcile_plan(registry, store, plan_path)

    assert result["applied"] is True
    assert result["queue_authoritative"] is False
    assert _read_queue(root) == plan["expected_queue_after"]
    assert result["post_gates"]["compatibility_queue_converged"] is True


def test_reviewed_noop_plan_is_byte_stable(registry_factory, tmp_path):
    root = registry_factory(2)
    _git_init(root)
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3", tmp_path / "state")
    plan_path = tmp_path / "plans" / "queue-plan.json"
    write_queue_reconcile_plan(registry, store, plan_path)
    _review_plan(plan_path)
    before = _queue_path(root).read_bytes()

    result = apply_queue_reconcile_plan(registry, store, plan_path)

    assert result["applied"] is False
    assert result["no_op"] is True
    assert _queue_path(root).read_bytes() == before


def test_stale_queue_preimage_refuses_without_mutation(registry_factory, tmp_path):
    root = registry_factory(1)
    task_id = "BUR-TEST-001-T001"
    _remove_from_queue(root, task_id)
    _git_init(root)
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3", tmp_path / "state")
    plan_path = tmp_path / "plans" / "queue-plan.json"
    write_queue_reconcile_plan(registry, store, plan_path)
    _review_plan(plan_path)
    queue = _read_queue(root)
    queue["lanes"]["later"].append(task_id)
    _queue_path(root).write_text(json.dumps(queue))
    changed = _queue_path(root).read_bytes()

    with pytest.raises(legacy.StateError, match="queue changed"):
        apply_queue_reconcile_plan(registry, store, plan_path)
    assert _queue_path(root).read_bytes() == changed


def test_registry_head_drift_refuses_without_mutation(registry_factory, tmp_path):
    root = registry_factory(1)
    task_id = "BUR-TEST-001-T001"
    _remove_from_queue(root, task_id)
    _git_init(root)
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3", tmp_path / "state")
    plan_path = tmp_path / "plans" / "queue-plan.json"
    write_queue_reconcile_plan(registry, store, plan_path)
    _review_plan(plan_path)
    before = _queue_path(root).read_bytes()
    (root / "unrelated.txt").write_text("new head\n")
    _git(root, "add", "unrelated.txt")
    _git(root, "commit", "-m", "advance fixture")

    with pytest.raises(legacy.StateError, match="git head changed"):
        apply_queue_reconcile_plan(registry, store, plan_path)
    assert _queue_path(root).read_bytes() == before


def test_state_store_frontier_drift_refuses_without_mutation(registry_factory, tmp_path):
    root = registry_factory(1)
    task_id = "BUR-TEST-001-T001"
    _remove_from_queue(root, task_id)
    _git_init(root)
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3", tmp_path / "state")
    store.import_registry_task_specs(registry)
    plan_path = tmp_path / "plans" / "queue-plan.json"
    write_queue_reconcile_plan(registry, store, plan_path)
    _review_plan(plan_path)
    before = _queue_path(root).read_bytes()
    current = store.task_spec(task_id)
    assert current is not None
    spec = dict(current["spec"])
    spec["state"] = "blocked"
    store.put_task_spec(
        spec,
        idempotency_key="queue-reconcile-frontier-drift",
        expected_revision=1,
        source="test",
    )

    with pytest.raises(legacy.StateError, match="frontier changed"):
        apply_queue_reconcile_plan(registry, store, plan_path)
    assert _queue_path(root).read_bytes() == before


def test_failed_post_gate_rolls_back_queue_bytes(registry_factory, tmp_path, monkeypatch):
    root = registry_factory(1)
    task_id = "BUR-TEST-001-T001"
    _remove_from_queue(root, task_id)
    _git_init(root)
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3", tmp_path / "state")
    plan_path = tmp_path / "plans" / "queue-plan.json"
    write_queue_reconcile_plan(registry, store, plan_path)
    _review_plan(plan_path)
    before = _queue_path(root).read_bytes()
    monkeypatch.setattr(
        registry_truth_module,
        "registry_truth_diagnostics",
        lambda root: {"healthy": False},
    )

    with pytest.raises(legacy.StateError, match="post-apply gates failed"):
        apply_queue_reconcile_plan(registry, store, plan_path)
    assert _queue_path(root).read_bytes() == before


def test_cli_report_emits_compatibility_contract(registry_factory, tmp_path, capsys):
    root = registry_factory(1)
    state = StateStore(tmp_path / "state" / "bureau.sqlite3", tmp_path / "state")

    result = bureau_cli.main(
        [
            "--root",
            str(root),
            "--state-db",
            str(state.path),
            "--state-root",
            str(state.state_root),
            "--json",
            "queue-reconcile",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert result == 0
    assert output["command"] == "queue-reconcile"
    assert output["queue_authoritative"] is False
    assert output["read_only"] is True


def test_plan_actions_are_bound_to_current_projection(registry_factory, tmp_path):
    root = registry_factory(2)
    _remove_from_queue(root, "BUR-TEST-001-T002")
    _git_init(root)
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3", tmp_path / "state")
    plan_path = tmp_path / "plans" / "queue-plan.json"

    plan = write_queue_reconcile_plan(registry, store, plan_path)
    report = queue_reconcile_module.queue_reconcile_report(registry, store)

    assert plan["frontier_projection_sha256"] == report["frontier_projection_sha256"]
    assert plan["expected_queue_after"] == report["compatibility_queue"]
    assert plan["actions"]
    assert all(action.get("task_id") != "" for action in plan["actions"])
