from __future__ import annotations

import json
from pathlib import Path

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
