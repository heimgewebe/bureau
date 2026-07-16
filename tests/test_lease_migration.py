from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from bureau import legacy
from bureau.cli import main
from bureau.core import Registry
from bureau.lease_contract import BROAD_BUREAU_REPOSITORY_KEY, registry_bureau_lease_findings
from bureau.lease_migration import (
    LeaseMigrationError,
    apply_lease_migration_plan,
    broad_bureau_lease_inventory,
    lease_migration_plan,
)

TASK_ID = "BUR-TEST-001-T001"
MIGRATION_ID = "BUREAU-TRUTH-MODEL-V2-T013"


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *args], text=True
    ).strip()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def setup_registry(registry_factory, *, catalog_entry: dict | None = None) -> Path:
    root = registry_factory(1)
    task_path = root / "registry/tasks" / f"{TASK_ID}.json"
    task = json.loads(task_path.read_text())
    task["state"] = "planned"
    task["priority"] = {"lane": "later", "rank": 10}
    task["execution"]["grabowski_resources"] = [BROAD_BUREAU_REPOSITORY_KEY]
    task["claims"] = [
        {"resource": "repo.bureau", "mode": "write", "isolation": "worktree"}
    ]
    task_path.write_text(json.dumps(task, indent=2) + "\n")
    queue_path = root / "registry/queue.json"
    queue = json.loads(queue_path.read_text())
    queue["lanes"] = {"now": [], "next": [], "later": []}
    queue_path.write_text(json.dumps(queue, indent=2) + "\n")

    resources = [
        {
            "schema_version": 1,
            "id": "repo.bureau",
            "type": "git-repository",
            "parent": "root",
            "path": "/home/alex/repos/bureau",
            "grabowski_key": BROAD_BUREAU_REPOSITORY_KEY,
        },
        {
            "schema_version": 1,
            "id": "component.bureau.core",
            "type": "component",
            "parent": "repo.bureau",
            "path": "/home/alex/repos/bureau/src/bureau",
            "grabowski_key": "path:/home/alex/repos/bureau/.bureau-scopes/core-code",
        },
    ]
    for index, resource in enumerate(resources, 10):
        (root / f"registry/resources/{index}.json").write_text(
            json.dumps(resource, indent=2) + "\n"
        )

    entry = catalog_entry
    if entry is None:
        entry = {
            "replacement_claim_resources": ["component.bureau.core"],
            "replacement_grabowski_resources": [
                "path:/home/alex/repos/bureau/.bureau-scopes/core-code"
            ],
            "include_initiative_resource": True,
            "rationale": "The test task changes only Bureau core code and its task record.",
            "effect_boundaries": ["Bureau core code", "exact task record"],
        }
    catalog = {
        "schema_version": 1,
        "migration_id": MIGRATION_ID,
        "source_broad_resource_key": BROAD_BUREAU_REPOSITORY_KEY,
        "source_claim_resource": "repo.bureau",
        "entries": {} if catalog_entry is False else {TASK_ID: entry},
        "does_not_establish": ["queue mutation"],
    }
    catalog_path = root / "registry/lease-migrations" / f"{MIGRATION_ID}.json"
    catalog_path.parent.mkdir(parents=True)
    catalog_path.write_text(json.dumps(catalog, indent=2) + "\n")

    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    git(root, "config", "user.name", "Bureau Test")
    git(root, "config", "user.email", "bureau-test@example.invalid")
    git(root, "add", ".")
    git(root, "commit", "-m", "fixture")
    return root


def review_plan(plan: dict, path: Path) -> None:
    plan["review"] = {
        "required": True,
        "status": "reviewed",
        "reviewer": "test-reviewer",
        "reviewed_at": "2026-07-15T09:00:00Z",
        "approved_plan_sha256": plan["plan_sha256"],
    }
    path.write_text(legacy.canonical_json(plan) + "\n")


def result_payload(text: str) -> dict:
    value = json.loads(text)
    return value.get("result", value)


def test_inventory_is_stable_hash_bound_and_semantic(registry_factory) -> None:
    root = setup_registry(registry_factory)
    registry = Registry.load(root)

    first = broad_bureau_lease_inventory(registry)
    second = broad_bureau_lease_inventory(registry)

    assert first == second
    assert first["count"] == 1
    assert first["actionable_count"] == 1
    assert first["refused_count"] == 0
    entry = first["entries"][0]
    assert entry["task_id"] == TASK_ID
    assert entry["current_task_sha256"] == registry.tasks[TASK_ID].sha256
    assert len(entry["initiative_plan_sha256"]) == 64
    assert entry["replacement_task_resource_key"].endswith(f"/{TASK_ID}.json")
    assert entry["semantic_input"]["replacement_claim_resources"] == [
        "component.bureau.core"
    ]
    assert first["inventory_sha256"] == legacy.sha256_json(
        {key: value for key, value in first.items() if key != "inventory_sha256"}
    )


def test_inventory_refuses_missing_or_ambiguous_semantics(registry_factory) -> None:
    root = setup_registry(registry_factory, catalog_entry=False)
    inventory = broad_bureau_lease_inventory(Registry.load(root))

    assert inventory["actionable_count"] == 0
    assert inventory["refused_count"] == 1
    refusal = inventory["entries"][0]["unresolved_semantic_inputs"]
    assert refusal == [
        {
            "code": "missing-semantic-catalog-entry",
            "detail": "no reviewed semantic mapping exists",
        }
    ]


def test_plan_and_apply_refuse_any_unresolved_semantics(
    registry_factory, tmp_path
) -> None:
    root = setup_registry(registry_factory, catalog_entry=False)
    registry = Registry.load(root)

    plan = lease_migration_plan(registry, batch_size=1)

    assert plan["applicable"] is False
    assert plan["blocked_by_refusals"] is True
    assert plan["task_ids"] == []
    assert plan["proposals"] == []
    assert plan["refusals"][0]["task_id"] == TASK_ID

    plan_path = tmp_path / "refused-plan.json"
    review_plan(plan, plan_path)
    with pytest.raises(LeaseMigrationError, match="unresolved semantic refusals"):
        apply_lease_migration_plan(registry, plan_path)
    assert git(root, "status", "--porcelain") == ""


def test_dry_run_is_bounded_deterministic_and_read_only(registry_factory) -> None:
    root = setup_registry(registry_factory)
    before = {
        path.relative_to(root): file_sha256(path)
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }
    registry = Registry.load(root)

    first = lease_migration_plan(registry, batch_size=1)
    second = lease_migration_plan(registry, batch_size=1)

    after = {
        path.relative_to(root): file_sha256(path)
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }
    assert first == second
    assert before == after
    assert git(root, "status", "--porcelain") == ""
    assert first["task_ids"] == [TASK_ID]
    assert len(first["proposals"]) == 1
    assert first["proposals"][0]["state_before"] == "planned"
    assert first["proposals"][0]["state_after"] == "planned"
    assert first["proposals"][0]["priority_before"] == {
        "lane": "later",
        "rank": 10,
    }
    assert first["combined_diff_sha256"] == hashlib.sha256(
        first["combined_diff"].encode()
    ).hexdigest()
    unsigned = {
        key: value for key, value in first.items() if key not in {"plan_sha256", "review"}
    }
    assert first["plan_sha256"] == legacy.sha256_json(unsigned)


@pytest.mark.parametrize("batch_size", [0, 6])
def test_batch_size_outside_one_to_five_is_refused(
    registry_factory, batch_size: int
) -> None:
    root = setup_registry(registry_factory)
    with pytest.raises(LeaseMigrationError, match="between 1 and 5"):
        lease_migration_plan(Registry.load(root), batch_size=batch_size)


def test_apply_requires_review_bound_to_exact_plan_sha(registry_factory, tmp_path) -> None:
    root = setup_registry(registry_factory)
    registry = Registry.load(root)
    plan = lease_migration_plan(registry, batch_size=1)
    plan_path = tmp_path / "plan.json"
    plan["review"] = {
        "required": True,
        "status": "reviewed",
        "reviewer": "test-reviewer",
        "reviewed_at": "2026-07-15T09:00:00Z",
        "approved_plan_sha256": "0" * 64,
    }
    plan_path.write_text(legacy.canonical_json(plan) + "\n")

    with pytest.raises(LeaseMigrationError, match="not bound to plan_sha256"):
        apply_lease_migration_plan(registry, plan_path)
    assert git(root, "status", "--porcelain") == ""


def test_reviewed_apply_changes_only_planned_task_scope(registry_factory, tmp_path) -> None:
    root = setup_registry(registry_factory)
    registry = Registry.load(root)
    queue_before = file_sha256(root / "registry/queue.json")
    plan = lease_migration_plan(registry, batch_size=1)
    plan_path = tmp_path / "plan.json"
    review_plan(plan, plan_path)

    result = apply_lease_migration_plan(registry, plan_path)

    assert result["applied"] is True
    assert result["post_gates"] == {
        "registry_load": True,
        "migrated_findings_remaining": 0,
        "queue_unchanged": True,
        "states_unchanged": True,
        "priorities_unchanged": True,
        "changed_paths_exact": True,
    }
    changed = git(root, "status", "--porcelain").splitlines()
    assert changed == [f"M registry/tasks/{TASK_ID}.json"]
    assert file_sha256(root / "registry/queue.json") == queue_before
    after = Registry.load(root).tasks[TASK_ID]
    assert after.state == "planned"
    assert after.raw["priority"] == {"lane": "later", "rank": 10}
    assert [claim.resource for claim in after.claims] == ["component.bureau.core"]
    resources = after.execution["grabowski_resources"]
    assert BROAD_BUREAU_REPOSITORY_KEY not in resources
    assert "path:/home/alex/repos/bureau/.bureau-scopes/core-code" in resources
    assert f"path:/home/alex/repos/bureau/registry/tasks/{TASK_ID}.json" in resources
    assert (
        "path:/home/alex/repos/bureau/registry/initiatives/BUR-TEST-001.json"
        in resources
    )
    assert after.raw["metadata"]["lease_scope_migration"]["migration_id"] == MIGRATION_ID
    assert registry_bureau_lease_findings(Registry.load(root)) == []


def test_apply_refuses_registry_head_drift(registry_factory, tmp_path) -> None:
    root = setup_registry(registry_factory)
    plan = lease_migration_plan(Registry.load(root), batch_size=1)
    plan_path = tmp_path / "plan.json"
    review_plan(plan, plan_path)
    git(root, "commit", "--allow-empty", "-m", "drift")

    with pytest.raises(LeaseMigrationError, match="base commit changed"):
        apply_lease_migration_plan(Registry.load(root), plan_path)
    assert git(root, "status", "--porcelain") == ""


def test_cli_inventory_and_dry_run_do_not_create_state_store(
    registry_factory, tmp_path, capsys
) -> None:
    root = setup_registry(registry_factory)
    state_root = tmp_path / "unused-state"

    inventory_code = main(
        [
            "--root",
            str(root),
            "--state-root",
            str(state_root),
            "--json",
            "doctor",
            "--inventory",
            "broad-bureau-leases",
        ]
    )
    inventory = result_payload(capsys.readouterr().out)
    assert inventory_code == 0
    assert inventory["kind"] == "broad-bureau-lease-inventory"
    assert not state_root.exists()

    dry_run_code = main(
        [
            "--root",
            str(root),
            "--state-root",
            str(state_root),
            "--json",
            "migrate-leases",
            "--dry-run",
            "--batch-size",
            "1",
        ]
    )
    plan = result_payload(capsys.readouterr().out)
    assert dry_run_code == 0
    assert plan["command"] == "migrate-leases-plan"
    assert plan["task_ids"] == [TASK_ID]
    assert not state_root.exists()
    assert git(root, "status", "--porcelain") == ""
