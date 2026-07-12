from __future__ import annotations

import json

import pytest

from bureau import cli as bureau_cli
from bureau import queue_reconcile as queue_reconcile_module
from bureau.core import Registry, StateError, StateStore
from bureau.legacy import ValidationError
from bureau.queue_reconcile import (
    apply_queue_reconcile_plan,
    queue_reconcile_report,
    write_queue_reconcile_plan,
)


def _task_path(root, task_id):
    return root / "registry/tasks" / f"{task_id}.json"


def _queue_path(root):
    return root / "registry/queue.json"


def _remove_from_queue(root, task_id: str) -> None:
    path = _queue_path(root)
    queue = json.loads(path.read_text())
    for lane in queue["lanes"].values():
        while task_id in lane:
            lane.remove(task_id)
    path.write_text(json.dumps(queue))


def _move_to_later(root, task_id: str) -> None:
    path = _queue_path(root)
    queue = json.loads(path.read_text())
    for lane in queue["lanes"].values():
        while task_id in lane:
            lane.remove(task_id)
    queue["lanes"]["later"].append(task_id)
    path.write_text(json.dumps(queue))


def _set_task(root, task_id: str, **changes) -> None:
    path = _task_path(root, task_id)
    task = json.loads(path.read_text())
    for key, value in changes.items():
        if key == "priority_lane":
            task.setdefault("priority", {})["lane"] = value
        else:
            task[key] = value
    path.write_text(json.dumps(task))


def _report(root, tmp_path):
    return queue_reconcile_report(
        Registry.load(root), StateStore(tmp_path / "state" / "bureau.sqlite3")
    )


def test_queue_reconcile_reports_unqueued_ready_priority_now(registry_factory, tmp_path):
    root = registry_factory(1)
    _remove_from_queue(root, "BUR-TEST-001-T001")

    report = _report(root, tmp_path)

    assert report["read_only"] is True
    assert report["queue_canonical"] is True
    assert report["summary"]["promote_to_now_candidates"] == 1
    assert report["findings"][0]["code"] == "unqueued-ready-priority-now"
    assert report["findings"][0]["rule"] == "ready_priority_now_should_be_queued_or_explained"
    assert report["findings"][0]["proposed_action"] == {
        "operation": "add_to_queue",
        "target_lane": "now",
    }


def test_queue_reconcile_reports_unqueued_priority_next(registry_factory, tmp_path):
    root = registry_factory(1)
    _remove_from_queue(root, "BUR-TEST-001-T001")
    _set_task(root, "BUR-TEST-001-T001", state="planned", priority_lane="next")

    report = _report(root, tmp_path)

    assert report["summary"]["promote_to_next_candidates"] == 1
    assert {item["code"] for item in report["findings"]} >= {
        "unqueued-open-priority-next"
    }


def test_queue_reconcile_reports_queued_later_priority_now_as_lane_mismatch(
    registry_factory, tmp_path
):
    root = registry_factory(1)
    _move_to_later(root, "BUR-TEST-001-T001")

    report = _report(root, tmp_path)

    assert report["summary"]["lane_mismatch_candidates"] == 1
    assert report["findings"][0]["code"] == "queued-later-priority-now-or-next"
    assert (
        report["findings"][0]["rule"]
        == "canonical_queue_lane_should_match_current_priority_or_document_drift"
    )
    assert report["findings"][0]["proposed_action"]["operation"] == "review_lane"


def test_queue_reconcile_terminal_queued_is_blocked_by_registry(registry_factory, tmp_path):
    root = registry_factory(1)
    _move_to_later(root, "BUR-TEST-001-T001")
    _set_task(root, "BUR-TEST-001-T001", state="cancelled")

    with pytest.raises(
        ValidationError,
        match="queue later has terminal task BUR-TEST-001-T001 with state cancelled",
    ):
        _report(root, tmp_path)


def test_queue_reconcile_is_read_only(registry_factory, tmp_path):
    root = registry_factory(1)
    queue_before = _queue_path(root).read_text()
    task_before = _task_path(root, "BUR-TEST-001-T001").read_text()

    _report(root, tmp_path)

    assert _queue_path(root).read_text() == queue_before
    assert _task_path(root, "BUR-TEST-001-T001").read_text() == task_before


def test_queue_reconcile_cli_emits_json(registry_factory, tmp_path, capsys):
    root = registry_factory(1)
    state = StateStore(tmp_path / "state" / "bureau.sqlite3")

    result = bureau_cli.main(
        [
            "--root",
            str(root),
            "--state-db",
            str(state.path),
            "--json",
            "queue-reconcile",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert result == 0
    assert output["command"] == "queue-reconcile"
    assert output["read_only"] is True


def test_queue_reconcile_can_filter_by_repository_resource(registry_factory, tmp_path):
    root = registry_factory(2)
    _remove_from_queue(root, "BUR-TEST-001-T002")
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")

    report = queue_reconcile_report(registry, store, resource="repo.beta")

    task_findings = {
        item["task_id"] for item in report["findings"] if "task_id" in item
    }
    assert task_findings == {"BUR-TEST-001-T002"}
    assert report["resource"] == "repo.beta"



def _review_plan(path):
    plan = json.loads(path.read_text())
    plan["review"] = {
        "required": True,
        "status": "reviewed",
        "reviewer": "test-reviewer",
        "reviewed_at": "2026-07-09T00:00:00Z",
    }
    path.write_text(json.dumps(plan))
    return plan


def test_queue_reconcile_write_plan_requires_review_before_apply(
    registry_factory, tmp_path
):
    root = registry_factory(1)
    _remove_from_queue(root, "BUR-TEST-001-T001")
    _set_task(root, "BUR-TEST-001-T001", state="planned", priority_lane="next")
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    plan_path = tmp_path / "plans" / "queue-plan.json"

    plan = write_queue_reconcile_plan(registry, store, plan_path)

    assert plan["review"]["status"] == "pending"
    assert plan["actions"] == [
        {
            "operation": "add_to_queue",
            "target_lane": "next",
            "task_id": "BUR-TEST-001-T001",
            "source_finding_code": "unqueued-open-priority-next",
            "effective_state": "planned",
            "priority_lane": "next",
        }
    ]
    with pytest.raises(Exception, match="not reviewed"):
        apply_queue_reconcile_plan(registry, store, plan_path)


def test_queue_reconcile_apply_no_actions_is_byte_stable_no_op(
    registry_factory, tmp_path, monkeypatch
):
    monkeypatch.setattr(queue_reconcile_module, "_git_head", lambda _root: "a" * 40)
    root = registry_factory(1)
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    plan_path = tmp_path / "plans" / "queue-plan.json"
    plan = write_queue_reconcile_plan(registry, store, plan_path)
    assert plan["actions"] == []
    _review_plan(plan_path)
    before = _queue_path(root).read_bytes()

    result = apply_queue_reconcile_plan(registry, store, plan_path)

    assert result["applied"] is False
    assert result["no_op"] is True
    assert result["post_gates"] is None
    assert _queue_path(root).read_bytes() == before


def test_queue_reconcile_apply_reviewed_plan_promotes_next_and_runs_gates(
    registry_factory, tmp_path, monkeypatch
):
    monkeypatch.setattr(queue_reconcile_module, "_git_head", lambda _root: "a" * 40)
    root = registry_factory(1)
    _remove_from_queue(root, "BUR-TEST-001-T001")
    _set_task(root, "BUR-TEST-001-T001", state="ready", priority_lane="next")
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    plan_path = tmp_path / "plans" / "queue-plan.json"
    write_queue_reconcile_plan(registry, store, plan_path)
    _review_plan(plan_path)

    result = apply_queue_reconcile_plan(registry, store, plan_path)
    queue = json.loads(_queue_path(root).read_text())

    assert result["applied"] is True
    assert result["no_op"] is False
    assert result["registry_git_head"] == "a" * 40
    assert result["post_gates"] == {
        "bureau_check": True,
        "doctor_healthy": True,
        "registry_truth_healthy": True,
    }
    assert queue["lanes"]["next"] == ["BUR-TEST-001-T001"]
    assert queue["lanes"]["now"] == []


def test_queue_reconcile_apply_refuses_stale_plan_without_mutation(
    registry_factory, tmp_path, monkeypatch
):
    monkeypatch.setattr(queue_reconcile_module, "_git_head", lambda _root: "a" * 40)
    root = registry_factory(2)
    _remove_from_queue(root, "BUR-TEST-001-T001")
    _set_task(root, "BUR-TEST-001-T001", state="planned", priority_lane="next")
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    plan_path = tmp_path / "plans" / "queue-plan.json"
    write_queue_reconcile_plan(registry, store, plan_path)
    _review_plan(plan_path)
    _remove_from_queue(root, "BUR-TEST-001-T002")
    before = _queue_path(root).read_text()

    with pytest.raises(Exception, match="queue changed"):
        apply_queue_reconcile_plan(registry, store, plan_path)

    assert _queue_path(root).read_text() == before


def test_queue_reconcile_apply_refuses_changed_registry_head_without_mutation(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    _remove_from_queue(root, "BUR-TEST-001-T001")
    _set_task(root, "BUR-TEST-001-T001", state="ready", priority_lane="next")
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    plan_path = tmp_path / "plans" / "queue-plan.json"
    heads = iter(["a" * 40, "b" * 40])
    monkeypatch.setattr(queue_reconcile_module, "_git_head", lambda _root: next(heads))
    write_queue_reconcile_plan(registry, store, plan_path)
    _review_plan(plan_path)
    before = _queue_path(root).read_text()

    with pytest.raises(StateError, match="registry git head changed"):
        apply_queue_reconcile_plan(registry, store, plan_path)

    assert _queue_path(root).read_text() == before

def test_queue_reconcile_apply_refuses_missing_registry_head_without_mutation(
    registry_factory, tmp_path, monkeypatch
):
    monkeypatch.setattr(queue_reconcile_module, "_git_head", lambda _root: "a" * 40)
    root = registry_factory(1)
    _remove_from_queue(root, "BUR-TEST-001-T001")
    _set_task(root, "BUR-TEST-001-T001", state="ready", priority_lane="next")
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    plan_path = tmp_path / "plans" / "queue-plan.json"
    write_queue_reconcile_plan(registry, store, plan_path)
    plan = _review_plan(plan_path)
    plan["registry"]["git_head"] = None
    plan_path.write_text(json.dumps(plan))
    before = _queue_path(root).read_text()

    with pytest.raises(StateError, match="lacks a bound registry git head"):
        apply_queue_reconcile_plan(registry, store, plan_path)

    assert _queue_path(root).read_text() == before


def test_queue_reconcile_apply_refuses_different_registry_root_without_mutation(
    registry_factory, tmp_path, monkeypatch
):
    monkeypatch.setattr(queue_reconcile_module, "_git_head", lambda _root: "a" * 40)
    root = registry_factory(1)
    _remove_from_queue(root, "BUR-TEST-001-T001")
    _set_task(root, "BUR-TEST-001-T001", state="ready", priority_lane="next")
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    plan_path = tmp_path / "plans" / "queue-plan.json"
    write_queue_reconcile_plan(registry, store, plan_path)
    plan = _review_plan(plan_path)
    plan["registry"]["root"] = str(tmp_path / "other-registry")
    plan_path.write_text(json.dumps(plan))
    before = _queue_path(root).read_text()

    with pytest.raises(StateError, match="registry root does not match"):
        apply_queue_reconcile_plan(registry, store, plan_path)

    assert _queue_path(root).read_text() == before


def test_queue_reconcile_apply_refuses_coherently_tampered_actions(
    registry_factory, tmp_path, monkeypatch
):
    monkeypatch.setattr(queue_reconcile_module, "_git_head", lambda _root: "a" * 40)
    root = registry_factory(1)
    _remove_from_queue(root, "BUR-TEST-001-T001")
    _set_task(root, "BUR-TEST-001-T001", state="ready", priority_lane="next")
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    plan_path = tmp_path / "plans" / "queue-plan.json"
    write_queue_reconcile_plan(registry, store, plan_path)
    plan = _review_plan(plan_path)
    plan["actions"][0]["target_lane"] = "now"
    expected = plan["expected_queue_after"]
    for lane in expected["lanes"].values():
        while "BUR-TEST-001-T001" in lane:
            lane.remove("BUR-TEST-001-T001")
    expected["lanes"]["now"].append("BUR-TEST-001-T001")
    plan["expected_queue_after_sha256"] = queue_reconcile_module._queue_sha256(
        expected
    )
    plan_path.write_text(json.dumps(plan))
    before = _queue_path(root).read_text()

    with pytest.raises(StateError, match="actions changed since dry-run"):
        apply_queue_reconcile_plan(registry, store, plan_path)

    assert _queue_path(root).read_text() == before


def test_queue_reconcile_apply_refuses_head_change_before_effect(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    _remove_from_queue(root, "BUR-TEST-001-T001")
    _set_task(root, "BUR-TEST-001-T001", state="ready", priority_lane="next")
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    plan_path = tmp_path / "plans" / "queue-plan.json"
    heads = iter(["a" * 40, "a" * 40, "b" * 40])
    monkeypatch.setattr(queue_reconcile_module, "_git_head", lambda _root: next(heads))
    write_queue_reconcile_plan(registry, store, plan_path)
    _review_plan(plan_path)
    before = _queue_path(root).read_text()

    with pytest.raises(StateError, match="registry git head changed"):
        apply_queue_reconcile_plan(registry, store, plan_path)

    assert _queue_path(root).read_text() == before


def test_queue_reconcile_apply_rolls_back_head_change_after_write(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    _remove_from_queue(root, "BUR-TEST-001-T001")
    _set_task(root, "BUR-TEST-001-T001", state="ready", priority_lane="next")
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    plan_path = tmp_path / "plans" / "queue-plan.json"
    heads = iter(["a" * 40, "a" * 40, "a" * 40, "b" * 40])
    monkeypatch.setattr(queue_reconcile_module, "_git_head", lambda _root: next(heads))
    write_queue_reconcile_plan(registry, store, plan_path)
    _review_plan(plan_path)
    before = _queue_path(root).read_text()

    with pytest.raises(StateError, match="registry git head changed"):
        apply_queue_reconcile_plan(registry, store, plan_path)

    assert _queue_path(root).read_text() == before


def test_queue_reconcile_apply_rolls_back_head_change_after_gates(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    _remove_from_queue(root, "BUR-TEST-001-T001")
    _set_task(root, "BUR-TEST-001-T001", state="ready", priority_lane="next")
    registry = Registry.load(root)
    store = StateStore(tmp_path / "state" / "bureau.sqlite3")
    plan_path = tmp_path / "plans" / "queue-plan.json"
    heads = iter(["a" * 40, "a" * 40, "a" * 40, "a" * 40, "b" * 40])
    monkeypatch.setattr(queue_reconcile_module, "_git_head", lambda _root: next(heads))
    write_queue_reconcile_plan(registry, store, plan_path)
    _review_plan(plan_path)
    before = _queue_path(root).read_text()

    with pytest.raises(StateError, match="registry git head changed"):
        apply_queue_reconcile_plan(registry, store, plan_path)

    assert _queue_path(root).read_text() == before


def test_queue_reconcile_cli_writes_plan(registry_factory, tmp_path, capsys):
    root = registry_factory(1)
    _remove_from_queue(root, "BUR-TEST-001-T001")
    _set_task(root, "BUR-TEST-001-T001", state="planned", priority_lane="next")
    state = StateStore(tmp_path / "state" / "bureau.sqlite3")
    plan_path = tmp_path / "plans" / "queue-plan.json"

    result = bureau_cli.main(
        [
            "--root",
            str(root),
            "--state-db",
            str(state.path),
            "--json",
            "queue-reconcile",
            "--write-plan",
            str(plan_path),
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert result == 0
    assert output["command"] == "queue-reconcile-plan"
    assert output["path"] == str(plan_path)
    assert plan_path.exists()
