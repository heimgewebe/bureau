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


@pytest.fixture(autouse=True)
def isolate_task_supply_binding_tests_from_live_fallbacks(request, monkeypatch):
    """Keep binding tests independent of fallback tasks materialized in Registry."""
    isolated_tests = {
        "test_registry_preview_requires_explicit_runtime_and_authority_for_plan",
        "test_registry_preview_rejects_stale_frontier_bindings",
    }
    if request.node.name not in isolated_tests:
        return

    module = request.module
    original_copy_registry = module.copy_registry

    def copy_without_fallbacks(project_root: Path, destination: Path) -> Path:
        root = original_copy_registry(project_root, destination)
        fallback_ids = set()
        for task_path in (root / "registry/tasks").glob("*.json"):
            task = json.loads(task_path.read_text(encoding="utf-8"))
            if "supply_fallback" not in task.get("metadata", {}):
                continue
            fallback_ids.add(task["id"])
            task_path.unlink()

        queue_path = root / "registry/queue.json"
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        for lane in queue.get("lanes", {}).values():
            if isinstance(lane, list):
                lane[:] = [task_id for task_id in lane if task_id not in fallback_ids]
        queue_path.write_text(
            json.dumps(queue, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return root

    monkeypatch.setattr(module, "copy_registry", copy_without_fallbacks)


@pytest.fixture(autouse=True)
def disable_open_pr_claim_guard_by_default(monkeypatch):
    monkeypatch.setenv("BUREAU_OPEN_PR_CLAIM_GUARD", "0")
