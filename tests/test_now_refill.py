from __future__ import annotations

import json
from pathlib import Path

import pytest

from bureau import legacy
from bureau.core import StateStore
from bureau.now_refill import NowRefillPolicy, apply_now_refill, build_now_refill_report
from bureau.v2 import Registry


def _task_ids(root: Path) -> list[str]:
    queue = json.loads((root / "registry/queue.json").read_text())
    return list(queue["lanes"]["now"])


def _move_all_to_next(root: Path, *, review_before_effect: bool = False) -> list[str]:
    queue_path = root / "registry/queue.json"
    queue = json.loads(queue_path.read_text())
    ids = list(queue["lanes"]["now"])
    queue["lanes"] = {"now": [], "next": ids, "later": []}
    queue_path.write_text(json.dumps(queue))
    for task_id in ids:
        path = root / f"registry/tasks/{task_id}.json"
        task = json.loads(path.read_text())
        task["priority"]["lane"] = "next"
        if review_before_effect:
            task["execution"]["policy"] = "review-before-effect"
        path.write_text(json.dumps(task))
    return ids


def test_empty_now_refills_from_structurally_runnable_next(registry_factory, tmp_path):
    root = registry_factory(task_count=3)
    ids = _move_all_to_next(root, review_before_effect=True)
    store = StateStore(tmp_path / "state" / "state.sqlite3", tmp_path / "state")
    registry = Registry.load(root)

    preview = build_now_refill_report(
        registry,
        store,
        policy=NowRefillPolicy(floor=2, target=3, max_promotions=3),
    )
    assert preview["status"] == "refill-planned"
    assert [item["task_id"] for item in preview["promotions"]] == ids
    assert all(item["structural_reasons"] == [] for item in preview["promotions"])
    assert all(item["actor_dependent_reasons"] for item in preview["promotions"])

    result = apply_now_refill(
        registry,
        store,
        authority="test-operator",
        policy=NowRefillPolicy(floor=2, target=3, max_promotions=3),
    )
    assert result["applied"] is True
    queue = json.loads((root / "registry/queue.json").read_text())
    assert queue["lanes"]["now"] == ids
    assert queue["lanes"]["next"] == []
    assert result["post_readback"]["status"] == "satisfied"


def test_structural_blocker_excludes_candidate(registry_factory, tmp_path):
    root = registry_factory(task_count=3)
    ids = _move_all_to_next(root)
    first_path = root / f"registry/tasks/{ids[0]}.json"
    first = json.loads(first_path.read_text())
    first["depends_on"] = [ids[2]]
    first_path.write_text(json.dumps(first))
    store = StateStore(tmp_path / "state" / "state.sqlite3", tmp_path / "state")

    report = build_now_refill_report(
        Registry.load(root),
        store,
        policy=NowRefillPolicy(floor=2, target=3, max_promotions=3),
    )
    promoted = [item["task_id"] for item in report["promotions"]]
    assert ids[0] not in promoted
    assert promoted == ids[1:]


def test_hysteresis_does_not_refill_at_floor(registry_factory, tmp_path):
    root = registry_factory(task_count=3)
    ids = _task_ids(root)
    queue_path = root / "registry/queue.json"
    queue = json.loads(queue_path.read_text())
    queue["lanes"] = {"now": ids[:2], "next": ids[2:], "later": []}
    queue_path.write_text(json.dumps(queue))
    store = StateStore(tmp_path / "state" / "state.sqlite3", tmp_path / "state")

    report = build_now_refill_report(
        Registry.load(root),
        store,
        policy=NowRefillPolicy(floor=2, target=3, max_promotions=3),
    )
    assert report["status"] == "satisfied"
    assert report["promotions"] == []


def test_apply_requires_explicit_authority(registry_factory, tmp_path):
    root = registry_factory(task_count=2)
    _move_all_to_next(root)
    store = StateStore(tmp_path / "state" / "state.sqlite3", tmp_path / "state")
    with pytest.raises(legacy.StateError, match="authority"):
        apply_now_refill(
            Registry.load(root),
            store,
            authority="",
            policy=NowRefillPolicy(floor=1, target=2, max_promotions=2),
        )


def test_manual_task_is_not_promoted(registry_factory, tmp_path):
    root = registry_factory(task_count=2)
    ids = _move_all_to_next(root)
    manual_path = root / f"registry/tasks/{ids[0]}.json"
    manual = json.loads(manual_path.read_text())
    manual["execution"]["mode"] = "manual"
    manual["execution"]["policy"] = "manual"
    manual_path.write_text(json.dumps(manual))
    store = StateStore(tmp_path / "state" / "state.sqlite3", tmp_path / "state")

    report = build_now_refill_report(
        Registry.load(root),
        store,
        policy=NowRefillPolicy(floor=1, target=2, max_promotions=2),
    )
    assert [item["task_id"] for item in report["promotions"]] == ids[1:]
