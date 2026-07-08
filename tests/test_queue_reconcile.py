from __future__ import annotations

import json

from bureau import cli as bureau_cli
from bureau.core import Registry, StateStore
from bureau.queue_reconcile import queue_reconcile_report


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
    return queue_reconcile_report(Registry.load(root), StateStore(tmp_path / "bureau.sqlite3"))


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


def test_queue_reconcile_reports_terminal_queued_as_error(registry_factory, tmp_path):
    root = registry_factory(1)
    _move_to_later(root, "BUR-TEST-001-T001")
    _set_task(root, "BUR-TEST-001-T001", state="cancelled")

    report = _report(root, tmp_path)

    finding = next(item for item in report["findings"] if item["code"] == "terminal-task-in-queue")
    assert report["summary"]["blockers"] >= 1
    assert finding["rule"] == "queued_terminal_tasks_are_invalid"
    assert finding["proposed_action"] == {"operation": "remove_from_queue", "target_lane": None}


def test_queue_reconcile_is_read_only(registry_factory, tmp_path):
    root = registry_factory(1)
    queue_before = _queue_path(root).read_text()
    task_before = _task_path(root, "BUR-TEST-001-T001").read_text()

    _report(root, tmp_path)

    assert _queue_path(root).read_text() == queue_before
    assert _task_path(root, "BUR-TEST-001-T001").read_text() == task_before


def test_queue_reconcile_cli_emits_json(registry_factory, tmp_path, capsys):
    root = registry_factory(1)
    state = StateStore(tmp_path / "bureau.sqlite3")

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
    store = StateStore(tmp_path / "bureau.sqlite3")

    report = queue_reconcile_report(registry, store, resource="repo.beta")

    task_findings = {
        item["task_id"] for item in report["findings"] if "task_id" in item
    }
    assert task_findings == {"BUR-TEST-001-T002"}
    assert report["resource"] == "repo.beta"
