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
    _canonical_runtime_registry_head,
    _runtime_bound_registry_head,
    _runtime_bound_state_store_paths,
    default_state_root,
    observe_authoritative_frontier,
    report_path,
    run_supply_cycle,
    snapshot_path,
)
from bureau.task_supply import (
    CONTROLLER_APPROVAL_CAPABILITY,
    FALLBACK_CATALOG,
    REVIEW_REASON,
    SupplyError,
    SupplyPolicy,
    _load_frontier_snapshot,
    sha256_json,
)
from bureau.v2 import Registry, StateStore

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
            "verifier_config": {"artifact_sha256": f"{index % 16:x}" * 64},
        }
    ]
    for index, category in enumerate(
        (spec.category for spec in FALLBACK_CATALOG),
        start=1,
    )
}


def head_reader(_root: Path) -> str:
    return HEAD


def fallback_inventory_closure(task_dir: Path) -> set[str]:
    """Return fallback tasks plus every task transitively depending on them."""
    documents = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in task_dir.glob("*.json")
    }
    removed = {task_id for task_id in documents if "-FB-" in task_id}
    while True:
        dependents = {
            task_id
            for task_id, document in documents.items()
            if task_id not in removed
            and any(dependency in removed for dependency in document.get("depends_on", []))
        }
        if not dependents:
            return removed
        removed.update(dependents)


def test_fallback_inventory_closure_removes_transitive_dependents(tmp_path: Path) -> None:
    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    documents = {
        "DEMO-FB-ONE": [],
        "DEMO-FU-DIRECT": ["DEMO-FB-ONE"],
        "DEMO-FU-TRANSITIVE": ["DEMO-FU-DIRECT"],
        "DEMO-REAL": [],
        "DEMO-FU-UNRELATED": ["DEMO-REAL"],
    }
    for task_id, depends_on in documents.items():
        (task_dir / f"{task_id}.json").write_text(
            json.dumps({"id": task_id, "depends_on": depends_on}),
            encoding="utf-8",
        )

    assert fallback_inventory_closure(task_dir) == {
        "DEMO-FB-ONE",
        "DEMO-FU-DIRECT",
        "DEMO-FU-TRANSITIVE",
    }


def registry_copy(tmp_path: Path) -> Path:
    """Copy the canonical Registry without its current fallback inventory.

    Supply behaviour must be reproducible from the catalog alone, not from whichever
    fallbacks the live Registry happens to carry when the suite runs. Tasks that
    transitively depend on removed fallback work are excluded as well so this
    synthetic catalog remains dependency-valid without weakening dependency edges.
    """
    project_root = Path(__file__).resolve().parents[1]
    root = tmp_path / "registry-copy"
    shutil.copytree(project_root / "registry", root / "registry")
    shutil.copytree(project_root / "schemas", root / "schemas")
    queue_path = root / "registry/queue.json"
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    task_dir = root / "registry/tasks"
    for task_id in sorted(fallback_inventory_closure(task_dir)):
        for lane in queue["lanes"].values():
            if task_id in lane:
                lane.remove(task_id)
        (task_dir / f"{task_id}.json").unlink()
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


PROFILE_EVIDENCE_TASK_ID = "WELTGEWEBE-OS-V1-T052"
PACKABLE_BASE_TASK_IDS = (
    "AUDIO-CONTROL-PLANE-V1-T011",
    "AUDIO-CONTROL-PLANE-V1-T040",
    "CCM-V1-T006",
    "CCM-V1-T007",
    "COMMONWORLD-PUBLIC-GLOBE-V1-T002",
)
SATISFIED_PACKABLE_TASK_IDS = (
    "AUDIO-CONTROL-PLANE-V1-T011",
    "AUDIO-CONTROL-PLANE-V1-T040",
    "BUR-2026-003-T005",
    "BUR-2026-005-T007",
    "BUREAU-CONTROL-PLANE-V3-FU-RUNTIME-REFRESH-AFTER-CLOSEOUT-FIX-20260810",
    "CCM-V1-T006",
    "CCM-V1-T007",
    "COMMONWORLD-PUBLIC-GLOBE-V1-T002",
)


def profile_evidence_task() -> dict:
    """Canonical nonclaimable task proving the worker's fallback capability profile."""
    reason = "fixture capability-profile evidence only"
    return {
        "task_id": PROFILE_EVIDENCE_TASK_ID,
        "title": PROFILE_EVIDENCE_TASK_ID,
        "effective_state": "planned",
        "queue_lane": "later",
        "eligible": False,
        "claim_reasons": [reason],
        "reasons": [reason],
    }


def packable_base_frontier() -> list[dict]:
    return [
        *(claimable_task(task_id) for task_id in PACKABLE_BASE_TASK_IDS),
        profile_evidence_task(),
    ]


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
    controller_capabilities: tuple[str, ...] = (),
    generated_at: str = NOW,
    policy: SupplyPolicy | None = None,
) -> dict:
    return run_supply_cycle(
        registry_root=root,
        capabilities=CAPABILITIES,
        state_root=tmp_path / "supply-state",
        state_store_root=tmp_path / "bureau-state",
        policy=policy or SupplyPolicy(),
        mutation_authority=mutation_authority,
        publish=publish,
        controller_capabilities=controller_capabilities,
        generated_at=generated_at,
        acceptance_contracts=TYPED_ACCEPTANCE,
        observer=observer or observer_for(frontier),
        head_reader=head_reader,
    )


def fallback_task_ids(root: Path) -> set[str]:
    return {path.stem for path in (root / "registry/tasks").glob("*-FB-*.json")}


def test_selected_but_unbound_candidates_fail_closed_without_worker_profile(
    tmp_path: Path,
) -> None:
    root = registry_copy(tmp_path)
    before = fallback_task_ids(root)
    queue_before = (root / "registry/queue.json").read_bytes()
    frontier = [unbound_candidate(index) for index in range(8)]

    summary = cycle(tmp_path, root, frontier)

    assert summary["metrics"]["normal_claimable_count"] == 0
    assert summary["metrics"]["total_claimable_count"] == 0
    assert summary["metrics"]["joint_claimable_count"] == 0
    assert summary["metrics"]["shortage_to_target"] == 12
    assert summary["status"] == "blocked"
    assert "worker-capability-profile-unbound" in summary["blockers"]
    persisted = json.loads(Path(summary["report_path"]).read_text(encoding="utf-8"))
    assert persisted["feasibility"]["worker_profile"]["bound"] is False
    assert summary["publication"]["attempted"] is False
    assert summary["publication"]["status"] == "preview-only"
    assert summary["publication"]["created_task_ids"] == []
    assert fallback_task_ids(root) == before
    assert (root / "registry/queue.json").read_bytes() == queue_before


def test_runner_persists_controller_capabilities_separately_from_worker_profile(
    tmp_path: Path,
) -> None:
    root = registry_copy(tmp_path)
    summary = cycle(
        tmp_path,
        root,
        [unbound_candidate(index) for index in range(8)],
        mutation_authority=False,
        publish=False,
        controller_capabilities=(CONTROLLER_APPROVAL_CAPABILITY,),
    )

    assert summary["controller_capabilities"] == [CONTROLLER_APPROVAL_CAPABILITY]
    persisted = json.loads(Path(summary["report_path"]).read_text(encoding="utf-8"))
    assert persisted["feasibility"]["controller_profile"]["capabilities"] == [
        CONTROLLER_APPROVAL_CAPABILITY
    ]
    assert persisted["feasibility"]["worker_profile"]["bound"] is False
    assert persisted["approval_available"] is False


def test_written_report_is_consumable_by_the_agent_frontier(tmp_path: Path) -> None:
    root = registry_copy(tmp_path)
    summary = cycle(tmp_path, root, [unbound_candidate(index) for index in range(8)])

    consumed = load_task_supply_summary(Path(summary["report_path"]))

    assert consumed["available"] is True
    assert consumed.get("invalid") is not True
    assert consumed["report_sha256"] == summary["report_sha256"]
    assert consumed["metrics"]["total_claimable_count"] == 0
    assert consumed["metrics"]["floor"] == 8


def canonical_runtime_identity(root: Path, *, head: str = HEAD) -> dict[str, object]:
    resolved = str(root.resolve())
    return {
        "compatibility": {"status": "canonical-read-only"},
        "manifest": {
            "valid": True,
            "source_commit": head,
            "canonical_registry": {
                "valid": True,
                "root": resolved,
                "source_commit": head,
            },
        },
        "registry": {
            "root": resolved,
            "head": head,
            "role": "canonical-runtime-snapshot",
        },
    }


def test_runtime_bound_registry_head_uses_verified_canonical_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "canonical-snapshot"
    root.mkdir()
    monkeypatch.setattr(
        "bureau.supply_runner.bureau_runtime_identity",
        lambda observed: canonical_runtime_identity(observed),
    )

    assert _runtime_bound_registry_head(root) == HEAD


def test_canonical_runtime_registry_head_rejects_identity_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "canonical-snapshot"
    root.mkdir()
    identity = canonical_runtime_identity(root)
    identity["registry"]["head"] = "e" * 40  # type: ignore[index]
    monkeypatch.setattr("bureau.supply_runner.bureau_runtime_identity", lambda _root: identity)

    with pytest.raises(SupplyError, match="cannot bind task-supply revision"):
        _canonical_runtime_registry_head(root)


def test_runtime_bound_registry_head_preserves_git_checkout_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    (root / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    monkeypatch.setattr(
        "bureau.supply_runner.bureau_runtime_identity",
        lambda _root: pytest.fail("Git checkouts must not require runtime identity"),
    )

    assert _runtime_bound_registry_head(root) is None

def test_runtime_bound_state_store_uses_configured_default_for_canonical_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_root = tmp_path / "bureau-state"
    monkeypatch.setenv("BUREAU_STATE_DIR", str(state_root))

    state_db, bound_root = _runtime_bound_state_store_paths(
        registry_head=HEAD,
        state_db=None,
        state_store_root=None,
    )

    assert state_db is None
    assert bound_root == state_root.resolve()


def test_runtime_bound_state_store_preserves_explicit_paths(tmp_path: Path) -> None:
    state_root = tmp_path / "explicit-state"
    state_db = state_root / "custom.sqlite3"

    assert _runtime_bound_state_store_paths(
        registry_head=HEAD,
        state_db=state_db,
        state_store_root=state_root,
    ) == (state_db, state_root)


def test_runtime_bound_state_store_keeps_source_checkout_fail_closed() -> None:
    assert _runtime_bound_state_store_paths(
        registry_head=None,
        state_db=None,
        state_store_root=None,
    ) == (None, None)


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


def test_runner_uses_catalog_typed_acceptance_without_injected_mapping(
    tmp_path: Path,
) -> None:
    root = registry_copy(tmp_path)
    frontier = [unbound_candidate(index) for index in range(8)]

    summary = run_supply_cycle(
        registry_root=root,
        capabilities=CAPABILITIES,
        state_root=tmp_path / "supply-state",
        mutation_authority=True,
        publish=False,
        generated_at=NOW,
        observer=observer_for(frontier),
        head_reader=head_reader,
    )

    assert summary["status"] == "blocked"
    assert "worker-capability-profile-unbound" in summary["blockers"]
    assert summary["publication"]["attempted"] is False
    persisted = json.loads(Path(summary["report_path"]).read_text(encoding="utf-8"))
    assert persisted["publication_plan"]["status"] == "preview-only"
    assert persisted["proposals"]
    for proposal in persisted["proposals"]:
        criteria = proposal["task"]["acceptance"]
        assert all(criterion["evidence_type"] == "object" for criterion in criteria)
        assert all(criterion["verifier"] == "manual_observation" for criterion in criteria)


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


def test_manifest_bound_registry_head_can_publish_state_store_only(tmp_path: Path) -> None:
    root = registry_copy(tmp_path)
    queue_before = (root / "registry/queue.json").read_bytes()
    summary = run_supply_cycle(
        registry_root=root,
        capabilities=CAPABILITIES,
        state_root=tmp_path / "supply-state",
        state_store_root=tmp_path / "bureau-state",
        mutation_authority=True,
        publish=True,
        generated_at=NOW,
        registry_head=HEAD,
        observer=observer_for([unbound_candidate(index) for index in range(8)]),
    )
    assert summary["publication"]["status"] in {"published", "preview-only"}
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


def test_published_fallbacks_live_only_in_state_store(tmp_path: Path) -> None:
    root = registry_copy(tmp_path)
    queue_before = (root / "registry/queue.json").read_bytes()
    first = cycle(tmp_path, root, packable_base_frontier())
    assert first["publication"]["status"] == "published"
    created = first["publication"]["created_task_ids"]
    assert created
    store = StateStore(state_root=tmp_path / "bureau-state")
    for task_id in created:
        assert store.task_spec(task_id) is not None
        assert not (root / f"registry/tasks/{task_id}.json").exists()
    assert (root / "registry/queue.json").read_bytes() == queue_before



def test_repeated_unbound_cycles_never_publish_or_duplicate_fallbacks(
    tmp_path: Path,
) -> None:
    root = registry_copy(tmp_path)
    before = fallback_task_ids(root)
    queue_before = (root / "registry/queue.json").read_bytes()
    starved = [unbound_candidate(index) for index in range(8)]

    first = cycle(tmp_path, root, starved)
    second = cycle(tmp_path, root, starved)
    third = cycle(tmp_path, root, starved)

    for summary in (first, second, third):
        assert summary["status"] == "blocked"
        assert "worker-capability-profile-unbound" in summary["blockers"]
        assert summary["publication"]["status"] == "preview-only"
        assert summary["publication"]["created_task_ids"] == []
    assert fallback_task_ids(root) == before
    assert (root / "registry/queue.json").read_bytes() == queue_before


def test_state_store_fallback_can_be_terminalized_without_git_task_materialization(
    tmp_path: Path,
) -> None:
    root = registry_copy(tmp_path)
    first = cycle(tmp_path, root, packable_base_frontier())
    task_id = first["publication"]["created_task_ids"][0]
    store = StateStore(state_root=tmp_path / "bureau-state")
    current = store.task_spec(task_id)
    assert current is not None
    terminal = json.loads(json.dumps(current["spec"]))
    terminal["state"] = "cancelled"
    written = store.put_task_spec(
        terminal,
        idempotency_key=f"test-terminal:{task_id}",
        expected_revision=current["revision"],
        source="test",
    )
    assert written["spec"]["state"] == "cancelled"
    assert not (root / f"registry/tasks/{task_id}.json").exists()



def test_unbound_profile_remains_fail_closed_across_same_bucket_cycles(
    tmp_path: Path,
) -> None:
    root = registry_copy(tmp_path)
    before = fallback_task_ids(root)
    starved = [unbound_candidate(index) for index in range(8)]

    first = cycle(tmp_path, root, starved)
    second = cycle(tmp_path, root, starved, generated_at=LATER_SAME_BUCKET)

    assert first["status"] == "blocked"
    assert second["status"] == "blocked"
    assert first["publication"]["created_task_ids"] == []
    assert second["publication"]["created_task_ids"] == []
    assert "worker-capability-profile-unbound" in first["blockers"]
    assert "worker-capability-profile-unbound" in second["blockers"]
    assert fallback_task_ids(root) == before


def test_bound_jointly_feasible_frontier_publishes_only_compatible_refill(
    tmp_path: Path,
) -> None:
    root = registry_copy(tmp_path)
    task_ids = [
        "AUDIO-CONTROL-PLANE-V1-T029",
        "AUDIO-CONTROL-PLANE-V1-T040",
        "GRABOWSKI-OPERATOR-SURFACE-V1-FU-PICKUP-EXPIRED-SAME-OWNER-LEASE-RECOVERY-20260812",
        "OPERATOR-ECOSYSTEM-REDUNDANCY-V1-T076",
        "OPERATOR-INTEGRATION-LOOP-V1-T033",
        "WELTGEWEBE-OS-V1-T041",
        "BUREAU-CONTROL-PLANE-V3-T005",
    ]

    summary = cycle(
        tmp_path,
        root,
        [claimable_task(task_id) for task_id in task_ids],
    )

    assert summary["metrics"]["total_claimable_count"] == 7
    assert summary["metrics"]["joint_claimable_count"] == 7
    assert summary["status"] == "refill-proposed"
    assert summary["publication"]["status"] == "published"
    assert len(summary["publication"]["created_task_ids"]) == 4
    persisted = json.loads(Path(summary["report_path"]).read_text(encoding="utf-8"))
    assert persisted["feasibility"]["worker_profile"]["bound"] is True
    assert persisted["metrics"]["joint_claimable_count"] == 7
    assert persisted["metrics"]["projected_joint_claimable_count"] == 11
    assert persisted["feasibility"]["floor_reachable"] is True
    created = [
        proposal
        for proposal in persisted["proposals"]
        if proposal["action"] == "create" and not proposal["blockers"]
    ]
    assert [proposal["category"] for proposal in created] == [
        "scout-commonworld",
        "scout-schauwerk",
        "scout-chronik",
        "scout-lenskit",
    ]
    assert all(proposal["task"]["claims"][0]["mode"] == "read" for proposal in created)
    assert all("approval" not in proposal["task"]["execution"] for proposal in created)


def test_normal_jointly_claimable_work_is_not_displaced_by_fallbacks(
    tmp_path: Path,
) -> None:
    root = registry_copy(tmp_path)
    before = fallback_task_ids(root)
    queue_before = (root / "registry/queue.json").read_bytes()

    summary = cycle(
        tmp_path,
        root,
        [claimable_task(task_id) for task_id in SATISFIED_PACKABLE_TASK_IDS],
    )

    assert summary["status"] == "satisfied"
    assert summary["metrics"]["normal_claimable_count"] == 8
    assert summary["metrics"]["total_claimable_count"] == 8
    assert summary["metrics"]["joint_claimable_count"] == 8
    assert summary["metrics"]["proposal_count"] == 0
    assert summary["metrics"]["shortage_to_target"] == 0
    assert summary["publication"]["attempted"] is False
    assert summary["publication"]["created_task_ids"] == []
    assert fallback_task_ids(root) == before
    assert (root / "registry/queue.json").read_bytes() == queue_before


def test_claimable_items_without_task_specs_fail_closed_for_joint_packability(
    tmp_path: Path,
) -> None:
    root = registry_copy(tmp_path)
    before = fallback_task_ids(root)
    queue_before = (root / "registry/queue.json").read_bytes()

    summary = cycle(
        tmp_path,
        root,
        [claimable_task(f"REAL-T{index:03d}") for index in range(8)],
    )

    assert summary["metrics"]["normal_claimable_count"] == 8
    assert summary["metrics"]["total_claimable_count"] == 8
    assert summary["metrics"]["joint_claimable_count"] == 0
    assert summary["status"] == "blocked"
    assert "worker-capability-profile-unbound" in summary["blockers"]
    persisted = json.loads(Path(summary["report_path"]).read_text(encoding="utf-8"))
    assert len(persisted["feasibility"]["pairwise_excluded"]) == 8
    assert set(persisted["feasibility"]["pairwise_excluded"].values()) == {
        "task-spec-claims-unavailable"
    }
    assert summary["publication"]["attempted"] is False
    assert summary["publication"]["created_task_ids"] == []
    assert fallback_task_ids(root) == before
    assert (root / "registry/queue.json").read_bytes() == queue_before


def test_runner_uses_dispatcher_task_specs_for_worker_profile_when_registry_drifted(
    tmp_path: Path,
) -> None:
    root = registry_copy(tmp_path)
    drift_id = (
        "GRABOWSKI-OPERATOR-SURFACE-V1-FU-RUNTIME-REFRESH-LEASE-CONTRACT-20260809"
    )
    control_id = "OPERATOR-INTEGRATION-LOOP-V1-T033"
    missing_id = "OPERATOR-INTEGRATION-LOOP-V1-T034"
    registry = Registry.load(root)
    authoritative_documents = {
        task_id: dict(task.raw) for task_id, task in registry.tasks.items()
    }
    authoritative_documents[drift_id] = {
        **authoritative_documents[drift_id],
        "required_capabilities": [
            "repository",
            "python",
            "testing",
            "grabowski",
            "runtime-deployment",
        ],
    }
    authoritative_documents[missing_id] = {
        **authoritative_documents[missing_id],
        "required_capabilities": ["github", "review-evidence"],
    }
    drift_item = {
        "task_id": drift_id,
        "title": drift_id,
        "effective_state": "ready",
        "queue_lane": "later",
        "eligible": False,
        "claim_reasons": ["missing capabilities: runtime-deployment"],
        "reasons": ["missing capabilities: runtime-deployment"],
    }
    missing_legacy_capabilities = {
        "task_id": missing_id,
        "title": missing_id,
        "effective_state": "ready",
        "queue_lane": "later",
        "eligible": False,
        "claim_reasons": ["missing capabilities: github, review-evidence"],
        "reasons": ["missing capabilities: github, review-evidence"],
    }

    def observe(**_kwargs: object) -> FrontierObservation:
        return FrontierObservation(
            frontier=(
                drift_item,
                claimable_task(control_id),
                missing_legacy_capabilities,
            ),
            runtime_healthy=True,
            runtime_blocker_codes=(),
            capabilities=CAPABILITIES,
            task_documents=authoritative_documents,
            task_spec_root_sha256="a" * 64,
            task_documents_sha256=sha256_json(authoritative_documents),
        )

    summary = cycle(
        tmp_path,
        root,
        [],
        observer=observe,
        mutation_authority=False,
        publish=False,
    )

    persisted = json.loads(Path(summary["report_path"]).read_text(encoding="utf-8"))
    profile = persisted["feasibility"]["worker_profile"]
    assert profile["bound"] is True
    assert profile["capabilities"] == sorted(CAPABILITIES)
    assert profile["conflicting_capabilities"] == []
    assert {"github", "review-evidence", "runtime-deployment"} <= set(
        profile["missing_capabilities"]
    )
    assert drift_id in profile["evidence_task_ids"]
    assert profile["task_document_source"] == "authoritative-dispatcher-task-specs"
    assert profile["task_spec_root_sha256"] == "a" * 64
    assert profile["task_documents_sha256"] == sha256_json(authoritative_documents)
    assert profile["registry_fallback_task_ids"] == []
    assert persisted["feasibility"]["joint_claimable_task_ids"] == [control_id]
    snapshot = json.loads(snapshot_path(tmp_path / "supply-state").read_text(encoding="utf-8"))
    assert snapshot["registry"]["task_spec_root_sha256"] == "a" * 64
    assert snapshot["registry"]["task_documents_sha256"] == sha256_json(
        authoritative_documents
    )


def test_runner_registry_fallback_is_bounded_to_missing_frontier_task_document(
    tmp_path: Path,
) -> None:
    root = registry_copy(tmp_path)
    fallback_id = "OPERATOR-INTEGRATION-LOOP-V1-T033"
    registry = Registry.load(root)
    authoritative_documents = {
        task_id: dict(task.raw) for task_id, task in registry.tasks.items()
    }
    authoritative_documents.pop(fallback_id)

    def observe(**_kwargs: object) -> FrontierObservation:
        return FrontierObservation(
            frontier=(claimable_task(fallback_id),),
            runtime_healthy=True,
            runtime_blocker_codes=(),
            capabilities=CAPABILITIES,
            task_documents=authoritative_documents,
            task_spec_root_sha256="b" * 64,
            task_documents_sha256=sha256_json(authoritative_documents),
        )

    summary = cycle(
        tmp_path,
        root,
        [],
        observer=observe,
        mutation_authority=False,
        publish=False,
    )

    persisted = json.loads(Path(summary["report_path"]).read_text(encoding="utf-8"))
    profile = persisted["feasibility"]["worker_profile"]
    assert profile["bound"] is True
    assert profile["task_document_source"] == (
        "authoritative-dispatcher-task-specs-with-bounded-registry-fallback"
    )
    assert profile["registry_fallback_task_ids"] == [fallback_id]
    assert profile["task_documents_sha256"] == sha256_json(authoritative_documents)
    assert persisted["feasibility"]["joint_claimable_task_ids"] == [fallback_id]
    assert fallback_id not in persisted["feasibility"]["pairwise_excluded"]


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
    assert result.task_documents is not None
    assert result.task_spec_root_sha256 is not None
    assert len(result.task_spec_root_sha256) == 64
    assert result.task_documents_sha256 is not None
    assert result.task_documents is not None
    assert result.task_documents_sha256 == sha256_json(result.task_documents)
    assert all(
        item["task_id"] in result.task_documents
        for item in result.frontier
        if item.get("task_id")
    )
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
