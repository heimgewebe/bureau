from __future__ import annotations

import json

from bureau import cli as bureau_cli
from bureau.core import Registry, StateStore
from bureau.v2 import Dispatcher
from bureau.what_now import what_now_report


def _task_path(root, task_id: str):
    return root / "registry/tasks" / f"{task_id}.json"


def _write_task(root, task_id: str, task: dict):
    _task_path(root, task_id).write_text(json.dumps(task), encoding="utf-8")


def _load_task(root, task_id: str) -> dict:
    return json.loads(_task_path(root, task_id).read_text(encoding="utf-8"))


def _move_task_to_queue_lane(root, task_id: str, lane_name: str, index: int = 0) -> None:
    queue_path = root / "registry/queue.json"
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    for lane in queue["lanes"].values():
        while task_id in lane:
            lane.remove(task_id)
    queue["lanes"][lane_name].insert(index, task_id)
    queue_path.write_text(json.dumps(queue), encoding="utf-8")


def test_what_now_ranks_eligible_tasks_from_registry_truth(registry_factory, tmp_path):
    root = registry_factory(3)
    first = _load_task(root, "BUR-TEST-001-T001")
    first["state"] = "blocked"
    first["priority"] = {"lane": "now", "rank": 0}
    _write_task(root, "BUR-TEST-001-T001", first)
    _move_task_to_queue_lane(root, "BUR-TEST-001-T001", "later", 0)
    second = _load_task(root, "BUR-TEST-001-T002")
    second["priority"] = {"lane": "next", "rank": 20}
    _write_task(root, "BUR-TEST-001-T002", second)
    third = _load_task(root, "BUR-TEST-001-T003")
    third["priority"] = {"lane": "now", "rank": 5}
    _write_task(root, "BUR-TEST-001-T003", third)
    _move_task_to_queue_lane(root, "BUR-TEST-001-T003", "now", 0)
    _move_task_to_queue_lane(root, "BUR-TEST-001-T002", "next", 0)

    registry = Registry.load(root)
    dispatcher = Dispatcher(registry, StateStore(tmp_path / "state" / "bureau.sqlite3"))
    report = what_now_report(dispatcher, {"repository"})

    assert report["read_only"] is True
    assert report["selected"]["task_id"] == "BUR-TEST-001-T003"
    assert [item["task_id"] for item in report["eligible"]] == [
        "BUR-TEST-001-T003",
        "BUR-TEST-001-T002",
    ]
    assert report["ranked"][0]["rank_basis"]["priority_rank"] == 5
    assert report["ranked"][0]["rank_basis"]["resource_claims"][0]["resource"] == "repo.alpha"
    assert report["ranked"][2]["reasons"] == ["state is blocked"]


def test_what_now_exposes_runtime_truth_and_blockers_when_none_eligible(
    registry_factory, tmp_path
):
    root = registry_factory(2)
    first = _load_task(root, "BUR-TEST-001-T001")
    first["depends_on"] = ["BUR-TEST-001-T002"]
    _write_task(root, "BUR-TEST-001-T001", first)
    _move_task_to_queue_lane(root, "BUR-TEST-001-T001", "next", 0)
    second = _load_task(root, "BUR-TEST-001-T002")
    second["state"] = "planned"
    _write_task(root, "BUR-TEST-001-T002", second)
    _move_task_to_queue_lane(root, "BUR-TEST-001-T002", "later", 0)

    registry = Registry.load(root)
    dispatcher = Dispatcher(registry, StateStore(tmp_path / "state" / "bureau.sqlite3"))
    report = what_now_report(dispatcher, {"repository"})

    assert report["selected"] is None
    assert report["runtime_truth"]["next_task_available"] is False
    assert report["blocked"][0]["task_id"] == "BUR-TEST-001-T001"
    assert report["blocked"][0]["rank_basis"]["depends_on"] == [
        {"task_id": "BUR-TEST-001-T002", "state": "planned"}
    ]
    assert any(
        "dependency BUR-TEST-001-T002 is planned" in reason
        for reason in report["blocked"][0]["reasons"]
    )


def test_what_now_cli_emits_json(registry_factory, tmp_path, capsys):
    root = registry_factory(1)
    state = StateStore(tmp_path / "state" / "bureau.sqlite3")

    result = bureau_cli.main(
        [
            "--root",
            str(root),
            "--state-db",
            str(state.path),
            "--json",
            "what-now",
            "--capability",
            "repository",
            "--limit",
            "3",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert result == 0
    assert output["command"] == "what-now"
    assert output["selected"]["task_id"] == "BUR-TEST-001-T001"
    assert output["ranked"][0]["rank"] == 1
