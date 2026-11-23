from __future__ import annotations

import json
from pathlib import Path

from bureau.core import Dispatcher, Registry, StateStore
from bureau.merged_task_intake import intake_merged_task_specs


def _clone_task(root: Path, task_id: str, **changes) -> dict:
    task = json.loads((root / "registry/tasks/BUR-TEST-001-T001.json").read_text())
    task["id"] = task_id
    task["title"] = f"Task {task_id}"
    for key, value in changes.items():
        task[key] = value
    (root / f"registry/tasks/{task_id}.json").write_text(json.dumps(task))
    return task


def _published_store(root: Path, tmp_path: Path) -> StateStore:
    """StateStore as on the host: every TaskSpec of the old main is published."""
    store = StateStore(tmp_path / "state" / "bureau.sqlite3", state_root=tmp_path / "state")
    store.import_registry_task_specs(Registry.load(root))
    return store


def _eligible(root: Path, store: StateStore) -> set[str]:
    explanation = Dispatcher(Registry.load(root), store).explain_next({"repository"})
    return {
        item["task_id"]
        for item in explanation["frontier"]
        if item.get("eligible")
    }


def test_merged_main_task_reaches_state_store_and_pickup(registry_factory, tmp_path) -> None:
    root = registry_factory(1, mode="write")
    store = _published_store(root, tmp_path)

    # A protected merge adds two new TaskSpecs: one ready, one planned.
    _clone_task(root, "BUR-TEST-001-T010", priority={"lane": "now", "rank": 0})
    _clone_task(root, "BUR-TEST-001-T011", state="planned")
    registry = Registry.load(root)
    assert store.task_spec("BUR-TEST-001-T010") is None
    assert "BUR-TEST-001-T010" not in _eligible(root, store)

    result = intake_merged_task_specs(registry, store)

    assert result["status"] == "imported"
    assert {item["task_id"] for item in result["imported"]} == {
        "BUR-TEST-001-T010",
        "BUR-TEST-001-T011",
    }
    assert store.task_spec("BUR-TEST-001-T010")["spec"]["state"] == "ready"
    assert store.task_spec("BUR-TEST-001-T011")["spec"]["state"] == "planned"
    eligible = _eligible(root, store)
    assert "BUR-TEST-001-T010" in eligible  # ready -> visible for pickup
    assert "BUR-TEST-001-T011" not in eligible  # planned stays planned

    # Repeat is a no-op.
    again = intake_merged_task_specs(Registry.load(root), store)
    assert again["status"] == "unchanged"
    assert again["imported"] == []
    assert store.task_spec("BUR-TEST-001-T010")["revision"] == 1


def test_existing_state_store_revision_is_never_overwritten(registry_factory, tmp_path) -> None:
    root = registry_factory(1, mode="write")
    store = _published_store(root, tmp_path)
    before = store.task_spec("BUR-TEST-001-T001")
    # Git projection drifts for an already published task.
    task = json.loads((root / "registry/tasks/BUR-TEST-001-T001.json").read_text())
    task["title"] = "changed in git"
    (root / "registry/tasks/BUR-TEST-001-T001.json").write_text(json.dumps(task))

    result = intake_merged_task_specs(Registry.load(root), store)

    assert result["status"] == "unchanged"
    assert store.task_spec("BUR-TEST-001-T001") == before


def test_conflict_is_reported_per_task_without_blocking_others(
    registry_factory, tmp_path, monkeypatch
) -> None:
    root = registry_factory(1, mode="write")
    store = _published_store(root, tmp_path)
    _clone_task(root, "BUR-TEST-001-T020")
    _clone_task(root, "BUR-TEST-001-T021")
    registry = Registry.load(root)

    original = store.seed_missing_registry_task_spec

    def refuse_one(registry_value, task_id):
        if task_id == "BUR-TEST-001-T020":
            # e.g. another writer published a different revision concurrently
            from bureau.task_specs import TaskSpecError

            raise TaskSpecError("exact TaskSpec divergence for BUR-TEST-001-T020")
        return original(registry_value, task_id)

    monkeypatch.setattr(store, "seed_missing_registry_task_spec", refuse_one)
    result = intake_merged_task_specs(registry, store)

    assert result["status"] == "conflicts"
    assert result["conflicts"] == [
        {
            "task_id": "BUR-TEST-001-T020",
            "code": "seed-refused",
            "detail": "exact TaskSpec divergence for BUR-TEST-001-T020",
        }
    ]
    assert [item["task_id"] for item in result["imported"]] == ["BUR-TEST-001-T021"]
    assert store.task_spec("BUR-TEST-001-T021") is not None
    assert store.task_spec("BUR-TEST-001-T020") is None


def test_child_waits_for_parent_and_dangling_reference_is_conflict(tmp_path) -> None:
    from types import SimpleNamespace

    def spec(task_id: str, **metadata) -> dict:
        value = {
            "schema_version": 1,
            "id": task_id,
            "initiative": "TEST-V1",
            "title": task_id,
            "state": "planned",
            "priority": {"lane": "later", "rank": 1},
            "depends_on": [],
            "execution": {"mode": "manual", "policy": "review-before-effect"},
            "claims": [],
            "required_capabilities": [],
            "acceptance": [{"id": "legacy", "assertion": "legacy prose"}],
        }
        if metadata:
            value["metadata"] = metadata
        return value

    def task(raw: dict):
        return SimpleNamespace(raw=raw, state=raw["state"], depends_on=raw["depends_on"])

    store = StateStore(tmp_path / "bureau.sqlite3", state_root=tmp_path)
    base = SimpleNamespace(root=tmp_path, tasks={"OLD": task(spec("OLD"))})
    store.import_registry_task_specs(base)
    # Child sorts before its parent; it must still import after it.
    tasks = {
        "A-CHILD": task(spec("A-CHILD", parent_task="Z-PARENT")),
        "B-ORPHAN": task(spec("B-ORPHAN", parent_task="MISSING")),
        "OLD": base.tasks["OLD"],
        "Z-PARENT": task(spec("Z-PARENT")),
    }
    registry = SimpleNamespace(root=tmp_path, tasks=tasks)

    result = intake_merged_task_specs(registry, store)

    assert [item["task_id"] for item in result["imported"]] == ["Z-PARENT", "A-CHILD"]
    assert result["conflicts"][0]["task_id"] == "B-ORPHAN"
    assert result["conflicts"][0]["code"] == "unknown-reference"
    assert store.task_spec("B-ORPHAN") is None


def test_baseline_skips_historical_git_only_tasks(registry_factory, tmp_path) -> None:
    root = registry_factory(1, mode="write")
    store = _published_store(root, tmp_path)
    _clone_task(root, "BUR-TEST-001-T030")
    _clone_task(root, "BUR-TEST-001-T031")
    (root / "registry/intake-baseline.json").write_text(
        json.dumps({"schema_version": 1, "task_ids": ["BUR-TEST-001-T030"]})
    )

    result = intake_merged_task_specs(Registry.load(root), store)

    assert [item["task_id"] for item in result["imported"]] == ["BUR-TEST-001-T031"]
    assert result["baseline_skipped"] == 1
    assert store.task_spec("BUR-TEST-001-T030") is None


def test_empty_state_store_is_left_to_git_bootstrap(registry_factory, tmp_path) -> None:
    root = registry_factory(1, mode="write")
    store = StateStore(tmp_path / "state" / "bureau.sqlite3", state_root=tmp_path / "state")
    result = intake_merged_task_specs(Registry.load(root), store)
    assert result["status"] == "skipped"
    assert store.task_spec("BUR-TEST-001-T001") is None


def test_repository_baseline_lists_only_existing_tasks() -> None:
    root = Path(__file__).resolve().parents[1]
    baseline = json.loads((root / "registry/intake-baseline.json").read_text())
    for task_id in baseline["task_ids"]:
        assert (root / f"registry/tasks/{task_id}.json").is_file(), task_id


def test_mutually_referencing_parent_and_child_import_atomically(tmp_path) -> None:
    from types import SimpleNamespace

    def spec(task_id: str, depends_on=(), **metadata) -> dict:
        value = {
            "schema_version": 1,
            "id": task_id,
            "initiative": "TEST-V1",
            "title": task_id,
            "state": "planned",
            "priority": {"lane": "later", "rank": 1},
            "depends_on": list(depends_on),
            "execution": {"mode": "manual", "policy": "review-before-effect"},
            "claims": [],
            "required_capabilities": [],
            "acceptance": [{"id": "legacy", "assertion": "legacy prose"}],
        }
        if metadata:
            value["metadata"] = metadata
        return value

    def task(raw: dict):
        return SimpleNamespace(raw=raw, state=raw["state"], depends_on=raw["depends_on"])

    store = StateStore(tmp_path / "bureau.sqlite3", state_root=tmp_path)
    old = task(spec("OLD"))
    store.import_registry_task_specs(SimpleNamespace(root=tmp_path, tasks={"OLD": old}))
    tasks = {
        "OLD": old,
        "P": task(spec("P", depends_on=["C"])),
        "C": task(spec("C", parent_task="P")),
    }

    result = intake_merged_task_specs(SimpleNamespace(root=tmp_path, tasks=tasks), store)

    assert result["conflicts"] == []
    assert sorted(item["task_id"] for item in result["imported"]) == ["C", "P"]
    assert store.task_spec("P") is not None and store.task_spec("C") is not None
