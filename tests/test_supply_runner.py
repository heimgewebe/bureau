from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from bureau.agent_frontier import default_task_supply_report, load_task_supply_summary
from bureau.core import Dispatcher
from bureau.supply_runner import (
    FrontierObservation,
    default_state_root,
    observe_authoritative_frontier,
    report_path,
    run_supply_cycle,
    snapshot_path,
)
from bureau.task_supply import REVIEW_REASON, SupplyError, SupplyPolicy, _load_frontier_snapshot
from bureau.v2 import Registry

HEAD = "d" * 40
NOW = "2026-08-05T06:00:00Z"
LATER_SAME_BUCKET = "2026-08-05T18:00:00Z"
CAPABILITIES = ("bureau", "grabowski", "python", "repository", "testing")
TYPED_ACCEPTANCE = {
    category: [
        {
            "id": f"{category}-artifact",
            "assertion": f"The reviewed {category} artifact matches the expected digest.",
            "evidence_type": "object",
            "verifier": "artifact_hash_matches",
            "verifier_config": {"artifact_sha256": f"{index:x}" * 64},
        }
    ]
    for index, category in enumerate(
        (
            "maintenance",
            "care",
            "audit",
            "diagnosis",
            "registry-reconciliation",
            "queue-reconciliation",
            "error-investigation",
        ),
        start=1,
    )
}


def head_reader(_root: Path) -> str:
    return HEAD


def registry_copy(tmp_path: Path) -> Path:
    """Copy the canonical Registry without its current fallback inventory.

    Supply behaviour must be reproducible from the catalog alone, not from whichever
    fallbacks the live Registry happens to carry when the suite runs.
    """
    project_root = Path(__file__).resolve().parents[1]
    root = tmp_path / "registry-copy"
    shutil.copytree(project_root / "registry", root / "registry")
    shutil.copytree(project_root / "schemas", root / "schemas")
    queue_path = root / "registry/queue.json"
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    for path in (root / "registry/tasks").glob("*-FB-*.json"):
        for lane in queue["lanes"].values():
            if path.stem in lane:
                lane.remove(path.stem)
        path.unlink()
    queue_path.write_text(json.dumps(queue, indent=2) + "\n", encoding="utf-8")
    Registry.load(root)
    return root


def mark_terminal(root: Path, task_id: str) -> Path:
    path = root / f"registry/tasks/{task_id}.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["state"] = "cancelled"
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    queue_path = root / "registry/queue.json"
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    for lane in queue["lanes"].values():
        if task_id in lane:
            lane.remove(task_id)
    queue_path.write_text(json.dumps(queue, indent=2) + "\n", encoding="utf-8")
    Registry.load(root)
    return path


def unbound_candidate(index: int) -> dict:
    """A scored frontier candidate that was never bound to a canonical Bureau task."""
    return {
        "task_id": "",
        "title": f"selected candidate {index}",
        "effective_state": "",
        "queue_lane": None,
        "eligible": True,
        "claim_reasons": [],
        "reasons": [],
    }


def claimable_task(task_id: str) -> dict:
    return {
        "task_id": task_id,
        "title": task_id,
        "effective_state": "ready",
        "queue_lane": "next",
        "eligible": True,
        "claim_reasons": [],
        "reasons": [],
    }


def published_fallback_items(root: Path) -> list[dict]:
    """Published fallbacks as the dispatcher sees them: ready, but review-gated."""
    registry = Registry.load(root)
    return [
        {
            "task_id": task_id,
            "title": task.raw["title"],
            "effective_state": task.raw["state"],
            "queue_lane": "later",
            "eligible": False,
            "claim_reasons": [REVIEW_REASON],
            "reasons": [REVIEW_REASON],
        }
        for task_id, task in sorted(registry.tasks.items())
        if "-FB-" in task_id
    ]


def observation(frontier: list[dict], *, runtime_healthy: bool = True) -> FrontierObservation:
    return FrontierObservation(
        frontier=tuple(frontier),
        runtime_healthy=runtime_healthy,
        runtime_blocker_codes=(),
        capabilities=CAPABILITIES,
    )


def observer_for(*frontiers: list[dict]):
    """Successive observations: the last one repeats for post-publication readback."""
    queued = list(frontiers)

    def observe(**_kwargs: object) -> FrontierObservation:
        return observation(queued.pop(0) if len(queued) > 1 else queued[0])

    return observe


def cycle(
    tmp_path: Path,
    root: Path,
    frontier: list[dict],
    *,
    observer=None,
    mutation_authority: bool = True,
    publish: bool = True,
    generated_at: str = NOW,
    policy: SupplyPolicy | None = None,
) -> dict:
    return run_supply_cycle(
        registry_root=root,
        capabilities=CAPABILITIES,
        state_root=tmp_path / "supply-state",
        policy=policy or SupplyPolicy(),
        mutation_authority=mutation_authority,
        publish=publish,
        generated_at=generated_at,
        acceptance_contracts=TYPED_ACCEPTANCE,
        observer=observer or observer_for(frontier),
        head_reader=head_reader,
    )


def fallback_task_ids(root: Path) -> set[str]:
    return {path.stem for path in (root / "registry/tasks").glob("*-FB-*.json")}


def test_selected_but_unbound_candidates_refill_toward_target(tmp_path: Path) -> None:
    root = registry_copy(tmp_path)
    before = fallback_task_ids(root)
    frontier = [unbound_candidate(index) for index in range(8)]

    summary = cycle(tmp_path, root, frontier)

    assert summary["metrics"]["normal_claimable_count"] == 0
    assert summary["metrics"]["total_claimable_count"] == 0
    assert summary["metrics"]["shortage_to_target"] == 12
    assert summary["status"] == "refill-proposed"
    assert summary["publication"]["status"] == "published"

    created = summary["publication"]["created_task_ids"]
    assert len(created) == SupplyPolicy().max_new_per_cycle
    assert len(set(created)) == len(created)
    assert set(created) & before == set()

    registry = Registry.load(root)
    for task_id in created:
        assert task_id in registry.tasks
        assert task_id in registry.queue["later"]

    # The published work is canonical, not yet claimable: normal gates still decide.
    readback = {
        item["task_id"]: item
        for item in summary["publication"]["post_publication_readback"]
    }
    assert set(readback) == set(created)
    assert all(item["claimable"] is False for item in readback.values())


def test_written_report_is_consumable_by_the_agent_frontier(tmp_path: Path) -> None:
    root = registry_copy(tmp_path)
    summary = cycle(tmp_path, root, [unbound_candidate(index) for index in range(8)])

    consumed = load_task_supply_summary(Path(summary["report_path"]))

    assert consumed["available"] is True
    assert consumed.get("invalid") is not True
    assert consumed["report_sha256"] == summary["report_sha256"]
    assert consumed["metrics"]["total_claimable_count"] == 0
    assert consumed["metrics"]["floor"] == 8


def test_runner_report_path_matches_the_agent_frontier_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BUREAU_TASK_SUPPLY_STATE_ROOT", raising=False)
    monkeypatch.delenv("BUREAU_TASK_SUPPLY_REPORT", raising=False)

    assert report_path(default_state_root()) == default_task_supply_report()


def test_snapshot_is_revision_bound_and_readable_by_the_preview_contract(tmp_path: Path) -> None:
    root = registry_copy(tmp_path)
    frontier = [unbound_candidate(index) for index in range(8)]

    summary = cycle(tmp_path, root, frontier, mutation_authority=False, publish=False)

    snapshot = json.loads(snapshot_path(tmp_path / "supply-state").read_text(encoding="utf-8"))
    assert snapshot["registry"]["head"] == HEAD
    assert snapshot["registry"]["queue_sha256"] == summary["registry"]["queue_sha256"]
    assert len(_load_frontier_snapshot(snapshot_path(tmp_path / "supply-state"))) == 8


def test_manifest_bound_registry_head_refreshes_snapshot_without_git(tmp_path: Path) -> None:
    root = registry_copy(tmp_path)
    frontier = [unbound_candidate(index) for index in range(8)]
    assert not (root / ".git").exists()

    summary = run_supply_cycle(
        registry_root=root,
        capabilities=CAPABILITIES,
        state_root=tmp_path / "supply-state",
        mutation_authority=False,
        publish=False,
        generated_at=NOW,
        registry_head=HEAD,
        observer=observer_for(frontier),
    )

    assert summary["registry"]["head"] == HEAD
    snapshot = json.loads(snapshot_path(tmp_path / "supply-state").read_text(encoding="utf-8"))
    assert snapshot["registry"]["head"] == HEAD
    assert summary["publication"]["attempted"] is False


def test_manifest_bound_registry_head_cannot_publish(tmp_path: Path) -> None:
    root = registry_copy(tmp_path)
    queue_before = (root / "registry/queue.json").read_bytes()

    with pytest.raises(SupplyError, match="read-only"):
        run_supply_cycle(
            registry_root=root,
            capabilities=CAPABILITIES,
            state_root=tmp_path / "supply-state",
            mutation_authority=True,
            publish=True,
            generated_at=NOW,
            registry_head=HEAD,
            observer=observer_for([unbound_candidate(index) for index in range(8)]),
        )

    assert (root / "registry/queue.json").read_bytes() == queue_before


def test_manifest_bound_registry_head_is_strict() -> None:
    with pytest.raises(SupplyError, match="40-character"):
        run_supply_cycle(
            registry_root=Path("."),
            capabilities=CAPABILITIES,
            registry_head="not-a-commit",
            observer=observer_for([]),
        )


def test_missing_mutation_authority_is_an_explicit_blocked_status(tmp_path: Path) -> None:
    root = registry_copy(tmp_path)
    before = fallback_task_ids(root)
    queue_before = (root / "registry/queue.json").read_bytes()

    summary = cycle(
        tmp_path,
        root,
        [unbound_candidate(index) for index in range(8)],
        mutation_authority=False,
    )

    assert summary["status"] == "blocked"
    assert "registry-mutation-authority-unavailable" in summary["blockers"]
    assert summary["publication"]["attempted"] is False
    assert summary["publication"]["status"] == "preview-only"
    assert "authority" in summary["publication"]["reason"]
    assert fallback_task_ids(root) == before
    assert (root / "registry/queue.json").read_bytes() == queue_before


def test_repeated_cycles_reuse_instead_of_duplicating_canonical_fallbacks(
    tmp_path: Path,
) -> None:
    root = registry_copy(tmp_path)
    before = fallback_task_ids(root)
    starved = [unbound_candidate(index) for index in range(8)]

    first = cycle(tmp_path, root, starved)
    after_first = fallback_task_ids(root)

    second = cycle(tmp_path, root, starved + published_fallback_items(root))
    after_second = fallback_task_ids(root)

    third = cycle(tmp_path, root, starved + published_fallback_items(root))
    after_third = fallback_task_ids(root)

    assert first["publication"]["status"] == "published"
    assert second["publication"]["status"] == "published"
    # The bounded catalog is exhausted: repetition proposes reuse, never a duplicate.
    assert third["publication"]["status"] == "preview-only"
    assert third["status"] == "blocked"
    assert third["metrics"]["new_proposal_count"] == 0
    assert third["metrics"]["reused_proposal_count"] == len(after_third - before)

    assert len(after_first - before) == 4
    assert len(after_second - before) == 7
    assert after_third == after_second
    assert set(first["publication"]["created_task_ids"]).isdisjoint(
        second["publication"]["created_task_ids"]
    )
    registry = Registry.load(root)
    assert sorted(registry.queue["later"]) == sorted(set(registry.queue["later"]))


def test_terminal_fallback_blocks_only_its_own_category_in_the_same_bucket(
    tmp_path: Path,
) -> None:
    root = registry_copy(tmp_path)
    starved = [unbound_candidate(index) for index in range(8)]
    first = cycle(tmp_path, root, starved)
    terminal_id = first["publication"]["created_task_ids"][0]
    terminal_path = mark_terminal(root, terminal_id)

    second = cycle(
        tmp_path,
        root,
        starved + published_fallback_items(root),
        generated_at=LATER_SAME_BUCKET,
    )

    assert second["publication"]["status"] == "published"
    assert terminal_id not in second["publication"]["created_task_ids"]
    assert second["publication"]["created_task_ids"]
    assert terminal_path.exists()
    assert json.loads(terminal_path.read_text(encoding="utf-8"))["state"] == "cancelled"


def test_normal_claimable_work_is_not_displaced_by_fallbacks(tmp_path: Path) -> None:
    root = registry_copy(tmp_path)
    before = fallback_task_ids(root)
    queue_before = (root / "registry/queue.json").read_bytes()

    summary = cycle(
        tmp_path,
        root,
        [claimable_task(f"REAL-T{index:03d}") for index in range(8)],
    )

    assert summary["status"] == "satisfied"
    assert summary["metrics"]["normal_claimable_count"] == 8
    assert summary["metrics"]["proposal_count"] == 0
    assert summary["metrics"]["shortage_to_target"] == 0
    assert summary["publication"]["attempted"] is False
    assert summary["publication"]["created_task_ids"] == []
    assert fallback_task_ids(root) == before
    assert (root / "registry/queue.json").read_bytes() == queue_before


def test_unhealthy_runtime_keeps_every_candidate_blocked(tmp_path: Path) -> None:
    root = registry_copy(tmp_path)
    before = fallback_task_ids(root)

    def observe(**_kwargs: object) -> FrontierObservation:
        return FrontierObservation(
            frontier=tuple(unbound_candidate(index) for index in range(8)),
            runtime_healthy=False,
            runtime_blocker_codes=("checkout-dirty",),
            capabilities=CAPABILITIES,
        )

    summary = cycle(tmp_path, root, [], observer=observe)

    assert summary["status"] == "blocked"
    assert "required-runtime-unhealthy" in summary["blockers"]
    assert "runtime-blocker:checkout-dirty" in summary["blockers"]
    assert summary["publication"]["attempted"] is False
    assert fallback_task_ids(root) == before


def test_observation_uses_the_canonical_dispatcher_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BUREAU_OPEN_PR_CLAIM_GUARD", "0")
    monkeypatch.setattr(
        Dispatcher,
        "_runtime_execution_truth",
        lambda self: {"schema_version": 1, "status": "clear", "execution_blocked": False},
    )
    root = registry_copy(tmp_path)
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True, text=True)

    result = observe_authoritative_frontier(
        registry_root=root,
        capabilities=CAPABILITIES,
        state_db=tmp_path / "state/bureau.sqlite3",
        state_store_root=tmp_path / "state",
    )

    assert result.runtime_healthy is True
    assert result.capabilities == tuple(sorted(CAPABILITIES))
    assert result.frontier
    assert all("claim_reasons" in item and "effective_state" in item for item in result.frontier)
    # The snapshot stays a bounded projection of the authoritative claim contract.
    assert set(result.frontier[0]) <= {
        "task_id",
        "title",
        "effective_state",
        "queue_lane",
        "eligible",
        "closure_bridge",
        "claim_reasons",
        "reasons",
    }
