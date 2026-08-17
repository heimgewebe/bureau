from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from bureau import supply_runner as supply_runner_module
from bureau.agent_frontier import build_frontier_report
from bureau.core import Dispatcher, StateStore
from bureau.cycle_contract import CONTRACT_VERSION, SCHEMA_VERSION
from bureau.supply_runner import FrontierObservation, reconcile_merged_supply_publication
from bureau.task_supply import (
    FALLBACK_METADATA_KEY,
    REVIEW_REASON,
    SupplyError,
    SupplyPolicy,
    _load_frontier_snapshot,
    build_registry_supply_report,
    build_supply_report,
    classify_frontier,
    default_fallback_acceptance_contracts,
    file_sha256,
    publish_supply_plan,
    sha256_json,
)
from bureau.v2 import Registry

HEAD = "a" * 40
QUEUE_SHA = "b" * 64
SNAPSHOT_SHA = "c" * 64
NOW = "2026-08-02T10:00:00Z"
NEXT_BUCKET = "2026-08-03T10:00:00Z"
TYPED_ACCEPTANCE = {
    category: [
        {
            "id": f"{category}-artifact",
            "assertion": f"The reviewed {category} validation artifact has the expected digest.",
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


def frontier_item(
    task_id: str,
    *,
    state: str = "ready",
    reasons: list[str] | None = None,
    title: str | None = None,
) -> dict:
    return {
        "task_id": task_id,
        "title": title or task_id,
        "effective_state": state,
        "queue_lane": "next",
        "eligible": not reasons,
        "claim_reasons": list(reasons or []),
        "reasons": list(reasons or []),
    }


def report(
    tmp_path: Path,
    frontier: list[dict],
    *,
    task_documents: dict[str, dict] | None = None,
    policy: SupplyPolicy | None = None,
    generated_at: str = NOW,
    approval_available: bool = False,
    runtime_healthy: bool = True,
    mutation_authority: bool = True,
    environment_blockers: tuple[str, ...] = (),
    catalog_blockers: dict[str, tuple[str, ...]] | None = None,
    registry_head: str = HEAD,
    queue_sha256: str = QUEUE_SHA,
    registry_root: Path | None = None,
    repository: Path | None = None,
    acceptance_contracts: dict[str, list[dict]] | None = TYPED_ACCEPTANCE,
) -> dict:
    root = registry_root or tmp_path
    repo = repository or tmp_path
    selected_policy = policy or SupplyPolicy()
    return build_supply_report(
        frontier,
        task_documents=task_documents,
        policy=selected_policy,
        generated_at=generated_at,
        repository=repo,
        registry_root=root,
        registry_head=registry_head,
        queue_sha256=queue_sha256,
        approval_available=approval_available,
        runtime_healthy=runtime_healthy,
        mutation_authority=mutation_authority,
        environment_blockers=environment_blockers,
        catalog_blockers=catalog_blockers,
        acceptance_contracts=acceptance_contracts,
    )


def copy_registry(project_root: Path, destination: Path) -> Path:
    shutil.copytree(project_root / "registry", destination / "registry")
    shutil.copytree(project_root / "schemas", destination / "schemas")
    return destination


def task_documents(root: Path) -> dict[str, dict]:
    result = {}
    for path in (root / "registry/tasks").glob("*.json"):
        value = json.loads(path.read_text(encoding="utf-8"))
        result[value["id"]] = value
    return result


def test_policy_requires_real_floor_and_hysteresis() -> None:
    with pytest.raises(ValueError, match="at least 8"):
        SupplyPolicy(floor=7)
    with pytest.raises(ValueError, match="greater than the floor"):
        SupplyPolicy(floor=8, refill_target=8)
    assert SupplyPolicy().floor == 8
    assert SupplyPolicy().refill_target > SupplyPolicy().floor


def test_raw_ready_is_not_claimability(tmp_path: Path) -> None:
    result = report(
        tmp_path,
        [frontier_item("REAL-T001", reasons=["missing capabilities: python"])],
    )
    assert result["metrics"]["raw_ready_count"] == 1
    assert result["metrics"]["normal_claimable_count"] == 0
    assert result["metrics"]["blocked_ready_count"] == 1
    assert result["blocked_ready"][0]["reasons"] == ["missing capabilities: python"]


def test_review_only_task_needs_explicit_approval(tmp_path: Path) -> None:
    item = frontier_item("REAL-T001", reasons=[REVIEW_REASON])
    without_approval = report(tmp_path, [item], approval_available=False)
    with_approval = report(tmp_path, [item], approval_available=True)
    assert without_approval["metrics"]["normal_claimable_count"] == 0
    assert with_approval["metrics"]["normal_claimable_count"] == 1


def test_approval_does_not_erase_other_blockers() -> None:
    result = classify_frontier(
        [
            frontier_item(
                "REAL-T001",
                reasons=[REVIEW_REASON, "component.bureau.core:write conflicts with active run"],
            )
        ],
        approval_available=True,
    )
    assert result["blocked_ready"][0]["reasons"] == [
        "component.bureau.core:write conflicts with active run"
    ]


def test_normal_and_fallback_claimability_are_separate(tmp_path: Path) -> None:
    fallback_task = {
        "id": "FALLBACK-T001",
        "state": "ready",
        "metadata": {
            FALLBACK_METADATA_KEY: {
                "schema_version": 1,
                "category": "maintenance",
                "open_key": "open",
            }
        },
    }
    result = report(
        tmp_path,
        [frontier_item("REAL-T001"), frontier_item("FALLBACK-T001")],
        task_documents={"FALLBACK-T001": fallback_task},
    )
    assert result["metrics"]["normal_claimable_count"] == 1
    assert result["metrics"]["fallback_claimable_count"] == 1
    assert result["normal_claimable"][0]["task_id"] == "REAL-T001"
    assert result["fallback_claimable"][0]["task_id"] == "FALLBACK-T001"


def test_floor_hysteresis_does_not_refill_at_floor(tmp_path: Path) -> None:
    frontier = [frontier_item(f"REAL-T{index:03d}") for index in range(8)]
    result = report(tmp_path, frontier)
    assert result["status"] == "satisfied"
    assert result["metrics"]["total_claimable_count"] == 8
    assert result["metrics"]["shortage_to_target"] == 0
    assert result["proposals"] == []


def test_empty_frontier_refills_toward_target_but_is_bounded(tmp_path: Path) -> None:
    result = report(tmp_path, [])
    assert result["status"] == "refill-proposed"
    assert result["metrics"]["shortage_to_target"] == 12
    assert result["metrics"]["new_proposal_count"] == 4
    assert [item["category"] for item in result["proposals"]] == [
        "maintenance",
        "care",
        "audit",
        "diagnosis",
    ]
    assert [
        item["task"]["claims"][0]["resource"] for item in result["proposals"]
    ] == [
        "component.bureau.core",
        "component.bureau.docs",
        "component.bureau.docs",
        "component.bureau.core",
    ]
    assert all(item["claimable"] is False for item in result["proposals"])
    assert all(item["canonical_publication_required"] for item in result["proposals"])


def test_default_fallback_acceptance_is_typed_and_publishable(tmp_path: Path) -> None:
    result = report(tmp_path, [], acceptance_contracts=None)

    assert result["status"] == "refill-proposed"
    assert result["publication_plan"]["status"] == "authorized"
    assert result["publication_plan"]["actions"]
    contracts = default_fallback_acceptance_contracts()
    assert set(contracts) == {spec["category"] for spec in result["proposals"]} | {
        "registry-reconciliation",
        "queue-reconciliation",
        "error-investigation",
    }
    for proposal in result["proposals"]:
        assert proposal["blockers"] == []
        criteria = proposal["task"]["acceptance"]
        assert criteria == contracts[proposal["category"]]
        assert all(criterion["evidence_type"] == "object" for criterion in criteria)
        assert all(criterion["verifier"] == "manual_observation" for criterion in criteria)
        assert all(
            criterion["verifier_config"]["observation_scope"].startswith(
                f"bureau-task-supply:{proposal['category']}:"
            )
            for criterion in criteria
        )


def test_missing_mutation_authority_is_explicit_blocker(tmp_path: Path) -> None:
    result = report(tmp_path, [], mutation_authority=False)
    assert result["status"] == "blocked"
    assert result["blockers"] == ["registry-mutation-authority-unavailable"]
    assert result["publication_plan"]["status"] == "preview-only"
    assert result["proposals"]


def test_untyped_fallback_catalog_refuses_task_proposal_and_publication(
    tmp_path: Path,
) -> None:
    result = report(tmp_path, [], acceptance_contracts={})

    assert result["status"] == "blocked"
    assert result["publication_plan"]["actions"] == []
    assert result["proposals"]
    assert all(
        proposal["blockers"] == ["acceptance-contract-unresolved"]
        for proposal in result["proposals"]
    )
    assert all("task" not in proposal for proposal in result["proposals"])


def test_runtime_and_environment_blockers_are_preserved(tmp_path: Path) -> None:
    result = report(
        tmp_path,
        [],
        runtime_healthy=False,
        environment_blockers=(
            "foreign-workspace:/tmp/other",
            "dirty-worktree:/repo",
        ),
        catalog_blockers={"maintenance": ("lease-conflict:component.bureau.core",)},
    )
    assert result["status"] == "blocked"
    assert result["blockers"] == [
        "dirty-worktree:/repo",
        "foreign-workspace:/tmp/other",
        "lease-conflict:component.bureau.core",
        "required-runtime-unhealthy",
    ]
    assert result["proposals"][0]["blockers"] == [
        "dirty-worktree:/repo",
        "foreign-workspace:/tmp/other",
        "lease-conflict:component.bureau.core",
        "required-runtime-unhealthy",
    ]


def test_fingerprint_is_stable_in_bucket_and_open_key_survives_next_bucket(
    tmp_path: Path,
) -> None:
    first = report(tmp_path, [], generated_at=NOW)
    repeated = report(tmp_path, [], generated_at=NOW)
    later = report(tmp_path, [], generated_at=NEXT_BUCKET)
    assert first["proposals"][0]["fingerprint"] == repeated["proposals"][0]["fingerprint"]
    assert first["proposals"][0]["fingerprint"] != later["proposals"][0]["fingerprint"]
    assert first["proposals"][0]["open_key"] == later["proposals"][0]["open_key"]


def test_existing_nonterminal_fallback_is_reused(tmp_path: Path) -> None:
    first = report(tmp_path, [])
    proposed = first["proposals"][0]
    existing = proposed["task"]
    second = report(tmp_path, [], task_documents={existing["id"]: existing})
    assert second["proposals"][0]["action"] == "reuse"
    assert second["proposals"][0]["task_id"] == existing["id"]
    assert "task" not in second["proposals"][0]
    assert second["metrics"]["new_proposal_count"] == 4
    assert second["metrics"]["proposal_count"] == 5
    assert second["proposals"][0]["blockers"] == [
        "existing-fallback-not-present-in-authoritative-frontier"
    ]


def test_fallback_closed_only_by_authoritative_state_is_not_reused_forever(
    tmp_path: Path,
) -> None:
    first = report(tmp_path, [], generated_at=NOW)
    published = first["proposals"][0]["task"]
    closed = frontier_item(published["id"], state="verified", reasons=["state is verified"])

    # The document still says "ready"; the authoritative frontier says otherwise, so the
    # category is released instead of being pinned to a closed task.
    same_bucket = report(tmp_path, [closed], task_documents={published["id"]: published})
    assert same_bucket["proposals"][0]["action"] == "create"
    assert same_bucket["proposals"][0]["blockers"] == [
        "fallback-task-id-already-canonical-in-current-bucket"
    ]

    replaced = report(
        tmp_path,
        [closed],
        task_documents={published["id"]: published},
        generated_at=NEXT_BUCKET,
    )
    assert replaced["proposals"][0]["action"] == "create"
    assert replaced["proposals"][0]["task_id"] != published["id"]
    assert replaced["proposals"][0]["blockers"] == []


def test_terminal_fallback_id_blocks_only_its_category_in_the_same_bucket(
    tmp_path: Path,
) -> None:
    first = report(tmp_path, [], generated_at=NOW)
    terminal = dict(first["proposals"][0]["task"], state="cancelled")
    second = report(tmp_path, [], task_documents={terminal["id"]: terminal}, generated_at=NOW)
    blocked = second["proposals"][0]
    assert blocked["action"] == "create"
    assert blocked["task_id"] == terminal["id"]
    assert blocked["blockers"] == ["fallback-task-id-already-canonical-in-current-bucket"]
    actions = second["publication_plan"]["actions"]
    assert terminal["id"] not in [action["task_id"] for action in actions]
    assert second["publication_plan"]["status"] == "authorized"
    assert second["metrics"]["new_proposal_count"] == 4

    later = report(
        tmp_path,
        [],
        task_documents={terminal["id"]: terminal},
        generated_at=NEXT_BUCKET,
    )
    assert later["proposals"][0]["blockers"] == []


def test_report_and_plan_digests_bind_full_payload(tmp_path: Path) -> None:
    result = report(tmp_path, [])
    report_payload = {key: value for key, value in result.items() if key != "report_sha256"}
    plan = result["publication_plan"]
    plan_payload = {key: value for key, value in plan.items() if key != "plan_sha256"}
    assert result["report_sha256"] == sha256_json(report_payload)
    assert plan["plan_sha256"] == sha256_json(plan_payload)


def test_publish_requires_explicit_authority(tmp_path: Path) -> None:
    result = report(tmp_path, [], mutation_authority=False)
    plan = result["publication_plan"]
    with pytest.raises(SupplyError, match="authority"):
        publish_supply_plan(
            plan,
            mutation_authorized=False,
            expected_plan_sha256=plan["plan_sha256"],
            head_reader=lambda _root: HEAD,
        )


def test_publish_fails_closed_on_head_or_queue_drift(tmp_path: Path) -> None:
    project_root = Path(__file__).parents[1]
    root = copy_registry(project_root, tmp_path / "registry-copy")
    queue_path = root / "registry/queue.json"
    result = report(
        tmp_path,
        [],
        registry_root=root,
        repository=root,
        registry_head=HEAD,
        queue_sha256=file_sha256(queue_path),
    )
    plan = result["publication_plan"]
    queue_before = queue_path.read_bytes()
    with pytest.raises(SupplyError, match="head changed"):
        publish_supply_plan(
            plan,
            mutation_authorized=True,
            expected_plan_sha256=plan["plan_sha256"],
            head_reader=lambda _root: "c" * 40,
        )
    assert queue_path.read_bytes() == queue_before
    queue_path.write_text(queue_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    changed = queue_path.read_bytes()
    with pytest.raises(SupplyError, match="queue changed"):
        publish_supply_plan(
            plan,
            mutation_authorized=True,
            expected_plan_sha256=plan["plan_sha256"],
            head_reader=lambda _root: HEAD,
        )
    assert queue_path.read_bytes() == changed


def test_authorized_publish_creates_canonical_tasks_and_valid_registry(tmp_path: Path) -> None:
    project_root = Path(__file__).parents[1]
    root = copy_registry(project_root, tmp_path / "registry-copy")
    queue_path = root / "registry/queue.json"
    result = report(
        tmp_path,
        [],
        task_documents=task_documents(root),
        registry_root=root,
        repository=root,
        registry_head=HEAD,
        queue_sha256=file_sha256(queue_path),
    )
    plan = result["publication_plan"]
    publication = publish_supply_plan(
        plan,
        mutation_authorized=True,
        expected_plan_sha256=plan["plan_sha256"],
        head_reader=lambda _root: HEAD,
    )
    assert publication["status"] == "published"
    assert publication["post_publication_registry_valid"] is True
    assert len(publication["created_task_ids"]) == 4
    registry = Registry.load(root)
    for task_id in publication["created_task_ids"]:
        assert task_id in registry.tasks
        assert task_id in registry.queue["later"]
        assert registry.tasks[task_id].raw["metadata"][FALLBACK_METADATA_KEY][
            "generated_by_task"
        ] == "OPERATOR-INTEGRATION-LOOP-V1-T014"


def test_publish_rolls_back_all_files_when_post_validation_fails(tmp_path: Path) -> None:
    project_root = Path(__file__).parents[1]
    root = copy_registry(project_root, tmp_path / "registry-copy")
    queue_path = root / "registry/queue.json"
    queue_before = queue_path.read_bytes()
    result = report(
        tmp_path,
        [],
        registry_root=root,
        repository=root,
        registry_head=HEAD,
        queue_sha256=file_sha256(queue_path),
    )
    plan = result["publication_plan"]

    def reject_registry(_root: str | Path) -> Registry:
        raise RuntimeError("synthetic post-publication validation failure")

    with pytest.raises(RuntimeError, match="synthetic"):
        publish_supply_plan(
            plan,
            mutation_authorized=True,
            expected_plan_sha256=plan["plan_sha256"],
            head_reader=lambda _root: HEAD,
            registry_loader=reject_registry,
        )
    assert queue_path.read_bytes() == queue_before
    for action in plan["actions"]:
        if action["action"] == "create":
            assert not (root / action["task_path"]).exists()


def empty_source_state() -> dict:
    return {
        "schema_version": 2,
        "contract_version": 2,
        "updated_at": NOW,
        "source_revisions": [],
        "candidate_fingerprints": [],
        "documents": {},
    }


def test_agent_frontier_missing_optional_supply_report_is_not_invalid(
    tmp_path: Path,
) -> None:
    frontier_report = build_frontier_report(
        empty_source_state(),
        task_supply_report_path=tmp_path / "missing-supply.json",
        generated_at=NOW,
    )
    assert "task_supply" not in frontier_report["scanner_summary"]
    assert all(
        not item["kind"].startswith("claimable_task_supply")
        for item in frontier_report["bottlenecks"]
    )


def test_agent_frontier_consumes_supply_report_without_granting_authority(
    tmp_path: Path,
) -> None:
    supply = report(tmp_path, [], mutation_authority=False)
    supply_path = tmp_path / "supply.json"
    supply_path.write_text(json.dumps(supply), encoding="utf-8")
    frontier_report = build_frontier_report(
        empty_source_state(),
        task_supply_report_path=supply_path,
        generated_at=NOW,
    )
    summary = frontier_report["scanner_summary"]["task_supply"]
    assert summary["status"] == "blocked"
    assert summary["metrics"]["normal_claimable_count"] == 0
    assert any(
        item["kind"] == "claimable_task_supply_blocked"
        for item in frontier_report["bottlenecks"]
    )
    assert frontier_report["next_action"].startswith("resolve the exact supply blockers")
    schema = json.loads(
        (Path(__file__).parents[1] / "schemas/agent-frontier-report.v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(schema).validate(frontier_report)


def test_agent_frontier_rejects_stale_revision_bound_supply_report(tmp_path: Path) -> None:
    root = copy_registry(Path(__file__).parents[1], tmp_path / "registry-repo")
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "Bureau Test"],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "bureau-test@example.invalid"],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "add", "registry", "schemas"],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", "initial registry"],
        check=True, capture_output=True, text=True,
    )
    report_head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    queue_path = root / "registry/queue.json"
    supply = report(
        tmp_path,
        [],
        mutation_authority=False,
        registry_head=report_head,
        queue_sha256=file_sha256(queue_path),
        registry_root=root,
        repository=root,
    )
    supply_path = tmp_path / "supply.json"
    supply_path.write_text(json.dumps(supply), encoding="utf-8")

    queue_path.write_text(queue_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(root), "add", "registry/queue.json"],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", "terminal lifecycle transition"],
        check=True, capture_output=True, text=True,
    )

    frontier_report = build_frontier_report(
        empty_source_state(),
        registry_root=root,
        task_supply_report_path=supply_path,
        generated_at=NOW,
    )
    summary = frontier_report["scanner_summary"]["task_supply"]
    assert summary["available"] is False
    assert summary["invalid"] is True
    assert summary["stale"] is True
    assert summary["reason"] == "registry-binding-stale"
    assert "metrics" not in summary
    assert any(
        item["kind"] == "claimable_task_supply_report_invalid"
        for item in frontier_report["bottlenecks"]
    )
    assert frontier_report["next_action"].startswith("regenerate and verify")


def test_agent_frontier_validates_runtime_snapshot_inventory_binding(tmp_path: Path) -> None:
    root = copy_registry(Path(__file__).parents[1], tmp_path / "snapshot")
    queue_path = root / "registry/queue.json"
    inventory = {
        "schema_version": 1,
        "kind": "bureau_registry_snapshot",
        "source_commit": HEAD,
        "tree_sha256": "d" * 64,
        "paths": [],
    }
    (root / ".bureau-runtime-snapshot.json").write_text(json.dumps(inventory), encoding="utf-8")
    supply = report(
        tmp_path, [], mutation_authority=False, registry_head=HEAD,
        queue_sha256=file_sha256(queue_path), registry_root=root, repository=root,
    )
    supply_path = tmp_path / "supply-snapshot.json"
    supply_path.write_text(json.dumps(supply), encoding="utf-8")
    frontier_report = build_frontier_report(
        empty_source_state(),
        registry_root=root,
        task_supply_report_path=supply_path,
        generated_at=NOW,
    )
    summary = frontier_report["scanner_summary"]["task_supply"]
    assert summary["available"] is True
    assert summary["status"] == "blocked"


def test_real_frontier_candidate_ranks_ahead_of_supply_fallback(tmp_path: Path) -> None:
    supply = report(tmp_path, [], mutation_authority=True)
    supply_path = tmp_path / "supply.json"
    supply_path.write_text(json.dumps(supply), encoding="utf-8")
    source = empty_source_state()
    source["documents"] = {
        "repo:grabowski:docs/plan.md": {
            "source_id": "repo:grabowski",
            "source_revision": "1" * 40,
            "source_path": "docs/plan.md",
            "project": "grabowski",
            "sha256": "2" * 64,
            "candidates": [
                {
                    "fingerprint": "normal-work",
                    "candidate_kind": "structured-task",
                    "status": "open",
                    "summary": "Implement current normal work",
                    "confidence": "high",
                    "source_anchor": "item:T001",
                }
            ],
        }
    }
    frontier_report = build_frontier_report(
        source,
        task_supply_report_path=supply_path,
        generated_at=NOW,
    )
    assert frontier_report["selected_frontier"]
    assert frontier_report["next_action"].startswith("review selected_frontier[0]")


def registry_snapshot(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): file_sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_all_catalog_candidates_blocked_makes_report_blocked(tmp_path: Path) -> None:
    blockers = {
        category: (f"lease-conflict:{category}",)
        for category in (
            "maintenance",
            "care",
            "audit",
            "diagnosis",
            "registry-reconciliation",
            "queue-reconciliation",
            "error-investigation",
        )
    }
    result = report(tmp_path, [], catalog_blockers=blockers)
    assert result["status"] == "blocked"
    assert result["publication_plan"]["status"] == "preview-only"
    assert result["publication_plan"]["actions"] == []
    assert result["blockers"] == sorted(
        f"lease-conflict:{category}" for category in blockers
    )


def test_blocked_category_is_not_in_authorized_publication_plan(tmp_path: Path) -> None:
    result = report(
        tmp_path,
        [],
        catalog_blockers={"maintenance": ("lease-conflict:maintenance",)},
    )
    assert result["status"] == "refill-proposed"
    assert result["publication_plan"]["status"] == "authorized"
    assert result["proposals"][0]["category"] == "maintenance"
    assert result["proposals"][0]["blockers"] == ["lease-conflict:maintenance"]
    assert all(
        action["category"] != "maintenance"
        for action in result["publication_plan"]["actions"]
    )


def test_publish_rejects_unknown_action_before_mutation(tmp_path: Path) -> None:
    project_root = Path(__file__).parents[1]
    root = copy_registry(project_root, tmp_path / "registry-copy")
    queue_path = root / "registry/queue.json"
    before = registry_snapshot(root)
    result = report(
        tmp_path,
        [],
        registry_root=root,
        repository=root,
        registry_head=HEAD,
        queue_sha256=file_sha256(queue_path),
    )
    plan = json.loads(json.dumps(result["publication_plan"]))
    plan["actions"][0]["action"] = "overwrite"
    plan["plan_sha256"] = sha256_json(
        {key: value for key, value in plan.items() if key != "plan_sha256"}
    )
    with pytest.raises(SupplyError, match="unsupported action"):
        publish_supply_plan(
            plan,
            mutation_authorized=True,
            expected_plan_sha256=plan["plan_sha256"],
            head_reader=lambda _root: HEAD,
        )
    assert registry_snapshot(root) == before


def test_publish_revalidates_typed_acceptance_before_any_mutation(tmp_path: Path) -> None:
    project_root = Path(__file__).parents[1]
    root = copy_registry(project_root, tmp_path / "registry-copy")
    queue_path = root / "registry/queue.json"
    before = registry_snapshot(root)
    result = report(
        tmp_path,
        [],
        registry_root=root,
        repository=root,
        registry_head=HEAD,
        queue_sha256=file_sha256(queue_path),
    )
    plan = json.loads(json.dumps(result["publication_plan"]))
    plan["actions"][0]["task"]["acceptance"] = [
        {"id": "legacy", "assertion": "legacy prose"}
    ]
    plan["plan_sha256"] = sha256_json(
        {key: value for key, value in plan.items() if key != "plan_sha256"}
    )

    with pytest.raises(SupplyError, match="publication task contract is invalid"):
        publish_supply_plan(
            plan,
            mutation_authorized=True,
            expected_plan_sha256=plan["plan_sha256"],
            head_reader=lambda _root: HEAD,
        )

    assert registry_snapshot(root) == before


@pytest.mark.parametrize(
    "payload",
    [
        [frontier_item("REAL-T001")],
        {"frontier": [frontier_item("REAL-T001")]},
        {"result": {"items": [frontier_item("REAL-T001")]}},
        {"payload": {"tasks": [frontier_item("REAL-T001")]}},
    ],
)
def test_frontier_snapshot_accepts_supported_authoritative_shapes(
    tmp_path: Path, payload: object
) -> None:
    path = tmp_path / "frontier.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert _load_frontier_snapshot(path)[0]["task_id"] == "REAL-T001"


def test_frontier_snapshot_rejects_ambiguous_or_malformed_input(tmp_path: Path) -> None:
    path = tmp_path / "frontier.json"
    path.write_text(json.dumps({"summary": {"ready": 12}}), encoding="utf-8")
    with pytest.raises(SupplyError, match="no object list"):
        _load_frontier_snapshot(path)
    path.write_text(json.dumps({"frontier": ["not-an-object"]}), encoding="utf-8")
    with pytest.raises(SupplyError, match="no object list"):
        _load_frontier_snapshot(path)


def test_registry_preview_defaults_runtime_unhealthy_and_is_read_only(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).parents[1]
    root = copy_registry(project_root, tmp_path / "registry-copy")
    before = registry_snapshot(root)
    result = build_registry_supply_report(
        registry_root=root,
        frontier=[],
        acceptance_contracts=TYPED_ACCEPTANCE,
        head_reader=lambda _root: HEAD,
    )
    assert result["status"] == "blocked"
    assert result["runtime_healthy"] is False
    assert result["blockers"] == [
        "frontier-queue-sha256-unbound",
        "frontier-registry-head-unbound",
        "frontier-snapshot-sha256-unbound",
        "registry-mutation-authority-unavailable",
        "required-runtime-unhealthy",
    ]
    assert registry_snapshot(root) == before


def test_registry_preview_requires_explicit_runtime_and_authority_for_plan(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).parents[1]
    root = copy_registry(project_root, tmp_path / "registry-copy")
    before = registry_snapshot(root)
    queue_digest = file_sha256(root / "registry/queue.json")
    result = build_registry_supply_report(
        registry_root=root,
        frontier=[],
        runtime_healthy=True,
        mutation_authority=True,
        frontier_registry_head=HEAD,
        frontier_queue_sha256=queue_digest,
        frontier_snapshot_sha256=SNAPSHOT_SHA,
        head_reader=lambda _root: HEAD,
        acceptance_contracts=TYPED_ACCEPTANCE,
    )
    assert result["status"] == "refill-proposed"
    assert result["publication_plan"]["status"] == "authorized"
    assert registry_snapshot(root) == before


def test_frontier_ineligible_marker_and_duplicate_ids_fail_closed() -> None:
    first = frontier_item("REAL-T001")
    second = frontier_item("REAL-T001")
    second["eligible"] = False
    result = classify_frontier([first, second])
    assert len(result["normal_claimable"]) == 1
    assert result["items"][1]["claimable"] is False
    assert result["items"][1]["reasons"] == [
        "authoritative-frontier-duplicate-task-id"
    ]
    no_reason = frontier_item("REAL-T002")
    no_reason["eligible"] = False
    result = classify_frontier([no_reason])
    assert result["blocked_ready"][0]["reasons"] == [
        "authoritative-frontier-not-eligible"
    ]


def test_frontier_cannot_spoof_fallback_metadata_without_canonical_task() -> None:
    item = frontier_item("REAL-T001")
    item["metadata"] = {
        FALLBACK_METADATA_KEY: {
            "schema_version": 1,
            "category": "maintenance",
            "open_key": "spoofed",
        }
    }
    result = classify_frontier([item], task_documents={})
    assert result["normal_claimable"][0]["task_id"] == "REAL-T001"
    assert result["fallback_claimable"] == []


def test_registry_preview_rejects_stale_frontier_bindings(tmp_path: Path) -> None:
    project_root = Path(__file__).parents[1]
    root = copy_registry(project_root, tmp_path / "registry-copy")
    fallback_frontier = [
        frontier_item(task_id)
        for task_id, task in task_documents(root).items()
        if FALLBACK_METADATA_KEY in task.get("metadata", {})
    ]
    result = build_registry_supply_report(
        registry_root=root,
        frontier=fallback_frontier,
        runtime_healthy=True,
        mutation_authority=True,
        frontier_registry_head="d" * 40,
        frontier_queue_sha256="e" * 64,
        frontier_snapshot_sha256=SNAPSHOT_SHA,
        head_reader=lambda _root: HEAD,
        acceptance_contracts=TYPED_ACCEPTANCE,
    )
    assert result["status"] == "blocked"
    assert result["blockers"] == [
        "frontier-queue-sha256-mismatch",
        "frontier-registry-head-mismatch",
    ]


def test_agent_frontier_rejects_tampered_supply_report(tmp_path: Path) -> None:
    supply = report(tmp_path, [], mutation_authority=False)
    supply["metrics"]["total_claimable_count"] = 99
    supply_path = tmp_path / "supply.json"
    supply_path.write_text(json.dumps(supply), encoding="utf-8")
    frontier_report = build_frontier_report(
        empty_source_state(),
        task_supply_report_path=supply_path,
        generated_at=NOW,
    )
    summary = frontier_report["scanner_summary"]["task_supply"]
    assert summary["invalid"] is True
    assert summary["reason"] == "report-digest-mismatch"
    assert any(
        item["kind"] == "claimable_task_supply_report_invalid"
        for item in frontier_report["bottlenecks"]
    )
    assert frontier_report["next_action"].startswith("regenerate and verify")


def test_publish_rejects_path_traversal_task_binding(tmp_path: Path) -> None:
    project_root = Path(__file__).parents[1]
    root = copy_registry(project_root, tmp_path / "registry-copy")
    queue_path = root / "registry/queue.json"
    before = registry_snapshot(root)
    result = report(
        tmp_path,
        [],
        registry_root=root,
        repository=root,
        registry_head=HEAD,
        queue_sha256=file_sha256(queue_path),
    )
    plan = json.loads(json.dumps(result["publication_plan"]))
    action = plan["actions"][0]
    action["task_id"] = "../../escape"
    action["task"]["id"] = "../../escape"
    action["task_path"] = "registry/tasks/../../escape.json"
    plan["plan_sha256"] = sha256_json(
        {key: value for key, value in plan.items() if key != "plan_sha256"}
    )
    with pytest.raises(SupplyError, match="task binding"):
        publish_supply_plan(
            plan,
            mutation_authorized=True,
            expected_plan_sha256=plan["plan_sha256"],
            head_reader=lambda _root: HEAD,
        )
    assert registry_snapshot(root) == before


def test_canonical_publication_feeds_normal_pickup_and_preserves_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = Path(__file__).parents[1]
    root = copy_registry(project_root, tmp_path / "registry-copy")
    queue_path = root / "registry/queue.json"
    result = report(
        tmp_path,
        [],
        task_documents=task_documents(root),
        registry_root=root,
        repository=root,
        registry_head=HEAD,
        queue_sha256=file_sha256(queue_path),
    )
    plan = result["publication_plan"]
    task_id = next(
        action["task_id"] for action in plan["actions"] if action["action"] == "create"
    )
    assert task_id not in Registry.load(root).tasks

    publish_supply_plan(
        plan,
        mutation_authorized=True,
        expected_plan_sha256=plan["plan_sha256"],
        head_reader=lambda _root: HEAD,
    )
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "Bureau Test"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "bureau-test@example.invalid"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "add", "registry", "schemas"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", "test registry snapshot"],
        check=True,
        capture_output=True,
        text=True,
    )
    registry = Registry.load(root)
    dispatcher = Dispatcher(registry, StateStore(tmp_path / "state.sqlite3"))
    monkeypatch.setattr(
        dispatcher,
        "_runtime_execution_truth",
        lambda: {
            "schema_version": 1,
            "status": "clear",
            "execution_blocked": False,
        },
    )
    without_capabilities = {
        item["task_id"]: item for item in dispatcher.frontier(set())
    }[task_id]
    assert without_capabilities["eligible"] is False
    assert any(
        reason.startswith("missing capabilities:")
        for reason in without_capabilities["claim_reasons"]
    )

    task = registry.tasks[task_id]
    with_capabilities = {
        item["task_id"]: item
        for item in dispatcher.frontier(set(task.capabilities))
    }[task_id]
    assert with_capabilities["eligible"] is False
    assert with_capabilities["claim_reasons"] == [REVIEW_REASON]

    approved_intent = dispatcher.claim_intent(
        "fallback-pickup-test",
        task.capabilities,
        task_id=task_id,
        approved=True,
    )
    assert approved_intent["intent"]["task_id"] == task_id
    assert dispatcher.store.list_runs() == []


POST_MERGE_PR = 77
POST_MERGE_HEAD = "d" * 40
POST_MERGE_COMMIT = "e" * 40
POST_MERGE_CURRENT_HEAD = "f" * 40
POST_MERGE_CAPABILITIES = ["repository", "python", "testing", "bureau", "grabowski"]


def post_merge_case(tmp_path: Path) -> dict:
    project_root = Path(__file__).parents[1]
    root = copy_registry(project_root, tmp_path / "post-merge-registry")
    queue_path = root / "registry/queue.json"
    supply = report(
        tmp_path,
        [],
        task_documents=task_documents(root),
        registry_root=root,
        repository=root,
        registry_head=HEAD,
        queue_sha256=file_sha256(queue_path),
    )
    publication = publish_supply_plan(
        supply["publication_plan"],
        mutation_authorized=True,
        expected_plan_sha256=supply["publication_plan"]["plan_sha256"],
        head_reader=lambda _root: HEAD,
    )
    assert len(publication["created_tasks"]) == 4
    receipt_path = tmp_path / "task-supply-run.json"
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "cycle_id": "2026-08-02T12",
        "stage": "watchdog",
        "run_id": "task-supply-test-run",
        "trigger": "local-task-supply",
        "started_at": "2026-08-02T10:00:00Z",
        "finished_at": "2026-08-02T10:01:00Z",
        "lifecycle_state": "terminal",
        "result": "completed",
        "degraded": False,
        "evidence": [
            {
                "kind": "task_supply_publication",
                "publication_result_sha256": publication["result_sha256"],
                "publication_result": publication,
            }
        ],
        "next_action": "wait for the exact Supply publication merge",
        "receipt_path": str(receipt_path),
    }
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    files = {
        binding["task_path"]: {
            "status": "added",
            "file_sha256": binding["file_sha256"],
            "spec_sha256": binding["spec_sha256"],
        }
        for binding in publication["created_tasks"]
    }
    files["registry/queue.json"] = {
        "status": "modified",
        "file_sha256": publication["queue_sha256_after"],
    }
    pull_request = {
        "number": POST_MERGE_PR,
        "state": "MERGED",
        "merged_at": "2026-08-02T10:03:00Z",
        "merge_commit": POST_MERGE_COMMIT,
        "head": POST_MERGE_HEAD,
        "branch": "automation/task-supply-test",
        "base": "main",
        "base_sha": publication["registry_head_before"],
        "files": files,
    }
    state_db = tmp_path / "post-merge-state.sqlite3"
    StateStore(state_db)
    return {
        "root": root,
        "publication": publication,
        "run_receipt": receipt_path,
        "run_receipt_sha256": file_sha256(receipt_path),
        "pull_request": pull_request,
        "state_db": state_db,
        "reconciliation_receipt": tmp_path / "post-merge-reconciliation.json",
    }


def post_merge_observer(publication: dict) -> FrontierObservation:
    projected = []
    for binding in publication["created_tasks"]:
        projected.append(
            {
                "task_id": binding["task_id"],
                "effective_state": "ready",
                "queue_lane": "later",
                "eligible": False,
                "resource_eligible": False,
                "claim_reasons": [REVIEW_REASON],
                "reasons": [REVIEW_REASON],
            }
        )
    return FrontierObservation(
        frontier=tuple(projected),
        runtime_healthy=True,
        runtime_blocker_codes=(),
        capabilities=tuple(POST_MERGE_CAPABILITIES),
    )


def reconcile_case(
    case: dict,
    *,
    mode: str,
    pull_request: dict | None = None,
    expected_run_receipt_sha256: str | None = None,
    reconciliation_receipt: Path | None = None,
    observer=None,
    pull_request_reader=None,
) -> dict:
    selected_pr = pull_request or case["pull_request"]
    selected_pr_reader = (
        pull_request_reader
        if pull_request_reader is not None
        else lambda _repository, _number, _bindings: selected_pr
    )
    return reconcile_merged_supply_publication(
        registry_root=case["root"],
        run_receipt_path=case["run_receipt"],
        expected_run_receipt_sha256=(
            expected_run_receipt_sha256 or case["run_receipt_sha256"]
        ),
        repository="heimgewebe/bureau",
        pull_request_number=POST_MERGE_PR,
        capabilities=POST_MERGE_CAPABILITIES,
        state_db=case["state_db"],
        mode=mode,
        reconciliation_receipt_path=(
            reconciliation_receipt
            if reconciliation_receipt is not None
            else case["reconciliation_receipt"]
        ),
        pull_request_reader=selected_pr_reader,
        head_reader=lambda _root: POST_MERGE_CURRENT_HEAD,
        merge_ancestor_checker=lambda _root, _ancestor, _descendant: True,
        observer=(
            observer
            if observer is not None
            else lambda **_kwargs: post_merge_observer(case["publication"])
        ),
    )


@pytest.mark.parametrize("drift", ["unmerged", "task", "queue", "base", "receipt"])
def test_post_merge_reconciliation_fails_closed_before_state_effect(
    tmp_path: Path,
    drift: str,
) -> None:
    case = post_merge_case(tmp_path)
    pull_request = json.loads(json.dumps(case["pull_request"]))
    expected_receipt = case["run_receipt_sha256"]
    if drift == "unmerged":
        pull_request["state"] = "OPEN"
        pull_request["merged_at"] = None
    elif drift == "task":
        task_path = case["publication"]["created_tasks"][0]["task_path"]
        pull_request["files"][task_path]["spec_sha256"] = "0" * 64
    elif drift == "queue":
        pull_request["files"]["registry/queue.json"]["file_sha256"] = "0" * 64
    elif drift == "base":
        pull_request["base_sha"] = "0" * 40
    else:
        expected_receipt = "0" * 64
    with pytest.raises(SupplyError):
        reconcile_case(
            case,
            mode="apply",
            pull_request=pull_request,
            expected_run_receipt_sha256=expected_receipt,
        )
    store = StateStore(case["state_db"])
    assert all(
        store.task_spec(binding["task_id"]) is None
        for binding in case["publication"]["created_tasks"]
    )
    assert not case["reconciliation_receipt"].exists()


def test_post_merge_reconciliation_revalidates_pr_before_state_effect(
    tmp_path: Path,
) -> None:
    case = post_merge_case(tmp_path)
    drifted_pr = json.loads(json.dumps(case["pull_request"]))
    task_path = case["publication"]["created_tasks"][0]["task_path"]
    drifted_pr["files"][task_path]["spec_sha256"] = "0" * 64
    calls = 0

    def moving_pr(_repository, _number, _bindings):
        nonlocal calls
        calls += 1
        return case["pull_request"] if calls == 1 else drifted_pr

    with pytest.raises(SupplyError, match="merged Supply TaskSpec binding drifted"):
        reconcile_case(case, mode="apply", pull_request_reader=moving_pr)

    assert calls == 2
    store = StateStore(case["state_db"])
    assert all(
        store.task_spec(binding["task_id"]) is None
        for binding in case["publication"]["created_tasks"]
    )
    assert not case["reconciliation_receipt"].exists()


def test_post_merge_reconciliation_imports_missing_specs_and_replays_receipt(
    tmp_path: Path,
) -> None:
    case = post_merge_case(tmp_path)
    queue_before = (case["root"] / "registry/queue.json").read_bytes()
    preview = reconcile_case(case, mode="preview")
    assert preview["status"] == "ready"
    assert preview["effect_started"] is False
    assert {item["status"] for item in preview["state_preimage"]} == {"missing"}
    assert not case["reconciliation_receipt"].exists()

    applied = reconcile_case(case, mode="apply")
    assert applied["status"] == "completed"
    assert applied["effect_started"] is True
    assert {item["status"] for item in applied["state_results"]} == {"imported"}
    assert case["reconciliation_receipt"].is_file()
    store = StateStore(case["state_db"])
    for binding in case["publication"]["created_tasks"]:
        current = store.task_spec(binding["task_id"])
        assert current is not None
        assert current["revision"] == 1
        assert current["spec_sha256"] == binding["spec_sha256"]
    assert (case["root"] / "registry/queue.json").read_bytes() == queue_before

    replay = reconcile_case(case, mode="readback")
    assert replay["idempotent_replay"] is True
    assert replay["effect_started"] is False
    assert replay["receipt_sha256"] == applied["receipt_sha256"]


def test_post_merge_reconciliation_preserves_existing_divergence(
    tmp_path: Path,
) -> None:
    case = post_merge_case(tmp_path)
    first = case["publication"]["created_tasks"][0]
    registry = Registry.load(case["root"])
    divergent = json.loads(json.dumps(registry.tasks[first["task_id"]].raw))
    divergent["title"] = divergent["title"] + " divergent-test"
    store = StateStore(case["state_db"])
    stored = store.put_task_spec(
        divergent,
        idempotency_key="post-merge-divergence-test",
        expected_revision=None,
        source="test-divergence",
    )
    before = store.task_spec(first["task_id"])
    assert before is not None
    assert before["spec_sha256"] != first["spec_sha256"]

    applied = reconcile_case(case, mode="apply")
    assert applied["status"] == "completed-with-divergence"
    rows = {item["task_id"]: item for item in applied["state_results"]}
    assert rows[first["task_id"]]["status"] == "divergent-preserved"
    assert rows[first["task_id"]]["after"] == {
        "revision": stored["revision"],
        "spec_sha256": before["spec_sha256"],
    }
    assert sum(item["status"] == "imported" for item in applied["state_results"]) == 3
    after = store.task_spec(first["task_id"])
    assert after is not None
    assert after["revision"] == before["revision"]
    assert after["spec_sha256"] == before["spec_sha256"]


def test_post_merge_readback_without_receipt_is_read_only(tmp_path: Path) -> None:
    case = post_merge_case(tmp_path)
    readback = reconcile_case(case, mode="readback")
    assert readback["status"] == "receipt-missing"
    assert readback["effect_started"] is False
    assert {item["status"] for item in readback["state_readback"]} == {"missing"}
    store = StateStore(case["state_db"])
    assert all(
        store.task_spec(binding["task_id"]) is None
        for binding in case["publication"]["created_tasks"]
    )


def test_post_merge_dispatcher_failure_requires_readback_before_retry(tmp_path: Path) -> None:
    case = post_merge_case(tmp_path)

    def fail_dispatcher(**_kwargs):
        raise RuntimeError("dispatcher-readback-failed")

    with pytest.raises(
        SupplyError,
        match=r"StateStore effect may already be complete.*run exact readback",
    ):
        reconcile_case(case, mode="apply", observer=fail_dispatcher)

    assert not case["reconciliation_receipt"].exists()
    store = StateStore(case["state_db"])
    assert all(
        store.task_spec(binding["task_id"])["spec_sha256"] == binding["spec_sha256"]
        for binding in case["publication"]["created_tasks"]
    )
    readback = reconcile_case(case, mode="readback")
    assert readback["status"] == "receipt-missing"
    assert {item["status"] for item in readback["state_readback"]} == {
        "expected-present"
    }
    assert readback["effect_started"] is False



def test_post_merge_cli_routes_explicit_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    def fake_reconcile(**kwargs):
        captured.update(kwargs)
        return {"status": "ready", "effect_started": False}

    monkeypatch.setattr(
        supply_runner_module,
        "reconcile_merged_supply_publication",
        fake_reconcile,
    )
    run_receipt = tmp_path / "run.json"
    state_db = tmp_path / "state.sqlite3"
    result = supply_runner_module.main(
        [
            "--registry-root",
            str(tmp_path),
            "--capability",
            "bureau",
            "--reconcile-merged-run",
            str(run_receipt),
            "--expected-run-receipt-sha256",
            "a" * 64,
            "--publication-repository",
            "heimgewebe/bureau",
            "--publication-pr",
            "77",
            "--bureau-state-db",
            str(state_db),
            "--reconcile-preview",
        ]
    )
    assert result == 0
    assert captured["mode"] == "preview"
    assert captured["registry_root"] == tmp_path
    assert captured["run_receipt_path"] == run_receipt
    assert captured["expected_run_receipt_sha256"] == "a" * 64
    assert captured["repository"] == "heimgewebe/bureau"
    assert captured["pull_request_number"] == 77
    assert captured["state_db"] == state_db
    assert captured["reconciliation_receipt_path"] is None
    assert json.loads(capsys.readouterr().out)["status"] == "ready"


def test_supply_runner_receipt_persists_publication_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication_result = {
        "kind": "bureau_task_supply_publication_result",
        "status": "published",
        "created_task_ids": ["DEMO-FB-ONE"],
        "created_tasks": [
            {
                "task_id": "DEMO-FB-ONE",
                "task_path": "registry/tasks/DEMO-FB-ONE.json",
                "queue_lane": "later",
                "file_sha256": "b" * 64,
                "spec_sha256": "c" * 64,
            }
        ],
        "result_sha256": "d" * 64,
    }
    summary = {
        "status": "refill-proposed",
        "report_path": str(tmp_path / "report.json"),
        "report_sha256": "e" * 64,
        "metrics": {"floor": 8},
        "blockers": [],
        "publication": {
            "status": "published",
            "created_task_ids": ["DEMO-FB-ONE"],
            "result": publication_result,
        },
    }
    monkeypatch.setattr(
        supply_runner_module,
        "run_supply_cycle",
        lambda **_kwargs: summary,
    )
    monkeypatch.setattr(
        supply_runner_module,
        "cycle_id",
        lambda: "2026-08-17T12",
    )
    state_root = tmp_path / "supply-state"
    result = supply_runner_module.main(
        [
            "--registry-root",
            str(tmp_path),
            "--capability",
            "bureau",
            "--state-root",
            str(state_root),
        ]
    )
    assert result == 0
    receipts = list((state_root / "runs").glob("*-task-supply.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    evidence = [
        item for item in receipt["evidence"] if item.get("kind") == "task_supply_publication"
    ]
    assert evidence == [
        {
            "kind": "task_supply_publication",
            "publication_result_sha256": "d" * 64,
            "publication_result": publication_result,
        }
    ]
