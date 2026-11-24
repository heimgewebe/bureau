from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

task_supply = importlib.import_module("bureau.task_supply")
legacy = importlib.import_module("bureau.legacy")
ARCHIVED_2026_11_15 = {
    "semantah",
    "plexer",
    "leitstand",
    "obsidian-bridge",
    "alpha-lab",
    "capacity-marketplace",
}
RETIRED = {
    "vault-gewebe",
    "vault-privat",
    "aussensensor",
    "mitschreiber",
    "hauski",
    "heimlern",
    "leitwerk",
    "hauski-audio",
    "heimserver",
    "heimgeist",
    "agent-control-surface",
    "demo-repository",
} | ARCHIVED_2026_11_15


def test_retired_and_archived_repository_resources_are_coordination_only() -> None:
    for name in RETIRED:
        data = json.loads(
            (ROOT / "registry" / "resources" / f"{name}.json").read_text(
                encoding="utf-8"
            )
        )
        metadata = data["metadata"]
        assert metadata["lifecycle"] == "retired"
        assert metadata["coordination_only"] is True
        assert metadata["mutation_claims_allowed"] is False
        expected_date = "2026-11-15" if name in ARCHIVED_2026_11_15 else "2026-09-18"
        assert metadata["retired_on"] == expected_date
        claims = set(metadata["does_not_establish"])
        assert {
            "remote_repository_exists",
            "fleet_membership",
            "task_supply_target",
            "runtime_role",
            "execution_authority",
        } <= claims




def test_six_archived_repositories_keep_github_and_fleet_evidence() -> None:
    for name in ARCHIVED_2026_11_15:
        path = ROOT / "registry" / "resources" / f"{name}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        metadata = data["metadata"]
        assert metadata["lifecycle"] == "retired"
        refs = metadata["lifecycle_evidence_refs"]
        assert "https://github.com/" + data["github_slug"] in refs
        assert "https://github.com/heimgewebe/metarepo/pull/756" in refs
        assert "https://github.com/heimgewebe/systemkatalog/pull/187" in refs


def test_retired_legacy_leitstand_task_has_no_active_write_route() -> None:
    path = ROOT / "registry" / "tasks" / "WELTGEWEBE-OS-V1-T008.json"
    task = json.loads(path.read_text(encoding="utf-8"))
    assert task["state"] == "superseded"  # Archived source matches the terminal TaskSpec
    assert task["claims"] == [
        {"resource": "repo.leitstand", "mode": "read", "isolation": "none"}
    ]
    cleanup = task["metadata"]["bureau_cleanup"]
    assert cleanup["retired_resources"] == ["repo.leitstand"]
    assert cleanup["task_status_receipt_sha256"] == (
        "5ccdb94defc8bb6f456fee69171fd236754d9a3f5ac0b1efdaaaabf7c2c40739"
    )


def test_deleted_repositories_are_not_scout_supply_targets() -> None:
    resources = {resource for _name, resource in task_supply._SCOUT_REPOSITORIES}
    assert resources.isdisjoint({f"repo.{name}" for name in RETIRED})


def test_mutation_forbidden_retired_repositories_have_no_nonterminal_mutation_claims() -> None:
    registry = legacy.Registry.load(ROOT)
    mutation_forbidden = {
        resource.id
        for resource in registry.resources.values()
        if isinstance(resource.metadata, dict)
        and resource.metadata.get("lifecycle") == "retired"
        and resource.metadata.get("coordination_only") is True
        and resource.metadata.get("mutation_claims_allowed") is False
    }
    assert mutation_forbidden == {f"repo.{name}" for name in RETIRED}

    offenders = [
        {
            "task_id": task.id,
            "state": task.state,
            "resource": claim.resource,
            "mode": claim.mode,
        }
        for task in registry.tasks.values()
        if task.state not in legacy.TERMINAL_TASK_STATES
        for claim in task.claims
        if claim.resource in mutation_forbidden and claim.mode != "read"
    ]

    assert offenders == []


def test_nonterminal_tasks_do_not_keep_retired_repositories_in_active_candidate_metadata() -> None:
    retired_repositories = {f"heimgewebe/{name}" for name in RETIRED}
    offenders = []

    for task_path in sorted((ROOT / "registry" / "tasks").glob("*.json")):
        task = json.loads(task_path.read_text(encoding="utf-8"))
        if task["state"] in legacy.TERMINAL_TASK_STATES:
            continue
        metadata = task.get("metadata", {})
        for field in ("observed_repositories", "unregistered_resource_repositories"):
            values = metadata.get(field, [])
            if not isinstance(values, list):
                continue
            for repository in sorted(retired_repositories.intersection(values)):
                offenders.append(
                    {
                        "task_id": task["id"],
                        "field": field,
                        "repository": repository,
                    }
                )

    assert offenders == []


def test_retired_mitschreiber_registration_task_is_terminal() -> None:
    task = json.loads(
        (
            ROOT
            / "registry"
            / "tasks"
            / "OPERATOR-ECOSYSTEM-REDUNDANCY-V1-T047.json"
        ).read_text(encoding="utf-8")
    )

    assert task["state"] in legacy.TERMINAL_TASK_STATES
    cleanup = task["metadata"]["bureau_cleanup"]
    assert cleanup["reason"] == "repository-retired-coordination-only"
    assert cleanup["retired_resources"] == ["repo.mitschreiber"]