from __future__ import annotations

import json
from pathlib import Path

import pytest

TERMINAL_QUEUE_STATES = {"verified", "cancelled", "superseded"}


def test_registry_queue_does_not_contain_terminal_tasks():
    root = Path(__file__).parents[1]
    queue = json.loads((root / "registry/queue.json").read_text(encoding="utf-8"))
    findings = []
    for lane, task_ids in queue["lanes"].items():
        for task_id in task_ids:
            task_path = root / "registry/tasks" / f"{task_id}.json"
            task = json.loads(task_path.read_text(encoding="utf-8"))
            state = task["state"]
            if state in TERMINAL_QUEUE_STATES:
                findings.append(
                    {
                        "lane": lane,
                        "task_id": task_id,
                        "state": state,
                    }
                )
    assert findings == []


def test_registry_rejects_non_ready_task_in_now(registry_factory):
    from bureau.core import Registry
    from bureau.legacy import ValidationError

    root = registry_factory(1)
    task_path = root / "registry/tasks/BUR-TEST-001-T001.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task["state"] = "planned"
    task_path.write_text(json.dumps(task), encoding="utf-8")

    with pytest.raises(
        ValidationError,
        match="queue now has non-ready task BUR-TEST-001-T001 with state planned",
    ):
        Registry.load(root)
