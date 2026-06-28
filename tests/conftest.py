from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest


@pytest.fixture
def registry_factory(tmp_path: Path):
    source = Path(__file__).parents[1]

    def create(task_count: int = 3, mode: str = "read", max_active: int = 20) -> Path:
        root = tmp_path / f"registry-{task_count}-{mode}"
        for folder in ("registry/initiatives", "registry/tasks", "registry/resources", "schemas"):
            (root / folder).mkdir(parents=True, exist_ok=True)
        for schema in (source / "schemas").glob("*.json"):
            shutil.copy2(schema, root / "schemas" / schema.name)
        initiative = {
            "schema_version": 1,
            "id": "BUR-TEST-001",
            "title": "Test",
            "state": "active",
            "commitment": "now",
            "goal": "Test goal",
            "completion": ["done"],
            "parallelism": {"max_active_tasks": max_active},
        }
        (root / "registry/initiatives/main.json").write_text(json.dumps(initiative))
        resources = [
            {"schema_version": 1, "id": "root", "type": "group"},
            {
                "schema_version": 1,
                "id": "repo",
                "type": "git-repository",
                "parent": "root",
                "path": str(root),
            },
            {"schema_version": 1, "id": "repo.alpha", "type": "component", "parent": "repo"},
            {"schema_version": 1, "id": "repo.beta", "type": "component", "parent": "repo"},
            {
                "schema_version": 1,
                "id": "cpu",
                "type": "capacity",
                "parent": "root",
                "capacity": 30,
            },
        ]
        for index, resource in enumerate(resources):
            (root / f"registry/resources/{index}.json").write_text(json.dumps(resource))
        ids = []
        for index in range(task_count):
            task_id = f"BUR-TEST-001-T{index + 1:03d}"
            ids.append(task_id)
            task = {
                "schema_version": 1,
                "id": task_id,
                "initiative": "BUR-TEST-001",
                "title": f"Task {index + 1}",
                "state": "ready",
                "depends_on": [],
                "required_capabilities": ["repository"],
                "priority": {"lane": "now", "rank": index},
                "execution": {
                    "mode": "interactive-agent",
                    "policy": "autonomous",
                    "working_repository": str(root),
                },
                "claims": [
                    {
                        "resource": "repo.alpha" if index % 2 == 0 else "repo.beta",
                        "mode": mode,
                        "isolation": "worktree",
                    },
                    {"resource": "cpu", "mode": "capacity", "amount": 1},
                ],
                "acceptance": [{"id": "proof", "assertion": "proof exists"}],
            }
            (root / f"registry/tasks/{task_id}.json").write_text(json.dumps(task))
        (root / "registry/queue.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "queue_policy": "skip-blocked",
                    "lanes": {"now": ids, "next": [], "later": []},
                }
            )
        )
        return root

    return create
