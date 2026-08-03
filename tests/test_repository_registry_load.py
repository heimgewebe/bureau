from __future__ import annotations

import json
from pathlib import Path

import pytest

from bureau.core import Registry, ValidationError


def test_repository_registry_loads_current_checkout():
    root = Path(__file__).resolve().parents[1]
    registry = Registry.load(root)
    assert registry.tasks


def test_repository_inventory_catalogues_agent_control_surface():
    root = Path(__file__).resolve().parents[1]
    resource_path = root / "registry/resources/agent-control-surface.json"
    raw = json.loads(resource_path.read_text(encoding="utf-8"))
    registry = Registry.load(root)
    resource = registry.resources["repo.agent-control-surface"]

    assert resource.type == "git-repository"
    assert resource.parent == "repo"
    assert resource.path == "/home/alex/repos/heimgewebe/agent-control-surface"
    assert resource.github_slug == "heimgewebe/agent-control-surface"
    assert (
        resource.grabowski_key
        == "repo:/home/alex/repos/heimgewebe/agent-control-surface"
    )
    assert raw["metadata"] == {
        "purpose": (
            "local manual control surface for Jules sessions and guarded step-by-step "
            "Git workflows"
        ),
        "scope": "catalog-only",
        "visibility": "public",
        "capabilities": ["repository", "shell", "git", "github"],
        "lifecycle": "transition",
        "lifecycle_reviewed_at": "2026-07-26",
        "lifecycle_evidence_refs": [
            "docs/audits/repository-lifecycle-classification-2026-07-19.v1.json",
            "bureau:OPERATOR-ECOSYSTEM-REDUNDANCY-V1-T034",
        ],
        "boundaries": [
            "no_autonomous_task_dispatch",
            "no_task_priority_authority",
            "no_merge_authorization",
            "no_remote_access_security",
        ],
    }


def _write_resource(root: Path, name: str, value: dict) -> None:
    (root / f"registry/resources/{name}.json").write_text(
        json.dumps(value), encoding="utf-8"
    )


def test_repository_inventory_catalogues_local_dashboard_without_github_slug():
    root = Path(__file__).resolve().parents[1]
    resource = Registry.load(root).resources["repo.heim-pc-dashboard"]

    assert resource.type == "git-repository"
    assert resource.parent == "repo"
    assert resource.path == "/home/alex/repos/heim-pc-dashboard-chatgpt-app"
    assert resource.github_slug is None
    assert resource.grabowski_key == "repo:/home/alex/repos/heim-pc-dashboard-chatgpt-app"


def test_registry_rejects_duplicate_resource_id(registry_factory) -> None:
    root = registry_factory()
    _write_resource(
        root,
        "duplicate-id",
        {
            "schema_version": 1,
            "id": "repo.alpha",
            "type": "component",
            "parent": "repo",
        },
    )

    with pytest.raises(ValidationError, match=r"duplicate id repo\.alpha"):
        Registry.load(root)


def test_registry_rejects_exact_path_collision_across_resource_types(registry_factory) -> None:
    root = registry_factory()
    shared = str(root / "shared")
    _write_resource(
        root,
        "repository-path",
        {
            "schema_version": 1,
            "id": "repo.path-owner",
            "type": "git-repository",
            "parent": "repo",
            "path": shared,
            "grabowski_key": f"repo:{shared}",
        },
    )
    _write_resource(
        root,
        "component-path",
        {
            "schema_version": 1,
            "id": "component.path-collision",
            "type": "component",
            "parent": "repo",
            "path": shared,
        },
    )

    with pytest.raises(ValidationError, match="share path"):
        Registry.load(root)


def test_registry_rejects_repo_key_on_non_repository_type(registry_factory) -> None:
    root = registry_factory()
    _write_resource(
        root,
        "type-collision",
        {
            "schema_version": 1,
            "id": "component.repo-key",
            "type": "component",
            "parent": "repo",
            "path": str(root / "component"),
            "grabowski_key": f"repo:{root / 'component'}",
        },
    )

    with pytest.raises(ValidationError, match="repo grabowski_key but is not a git-repository"):
        Registry.load(root)


def test_registry_rejects_duplicate_grabowski_key(registry_factory) -> None:
    root = registry_factory()
    shared_key = "component:shared-contract"
    for suffix in ("a", "b"):
        _write_resource(
            root,
            f"duplicate-key-{suffix}",
            {
                "schema_version": 1,
                "id": f"component.duplicate-key-{suffix}",
                "type": "component",
                "parent": "repo",
                "path": str(root / f"component-{suffix}"),
                "grabowski_key": shared_key,
            },
        )

    with pytest.raises(ValidationError, match="share grabowski_key"):
        Registry.load(root)


def test_registry_rejects_repository_key_that_does_not_match_path(registry_factory) -> None:
    root = registry_factory()
    _write_resource(
        root,
        "mismatched-repository-key",
        {
            "schema_version": 1,
            "id": "repo.mismatched-key",
            "type": "git-repository",
            "parent": "repo",
            "path": str(root / "actual"),
            "grabowski_key": f"repo:{root / 'other'}",
        },
    )

    with pytest.raises(ValidationError, match="grabowski_key must be"):
        Registry.load(root)
