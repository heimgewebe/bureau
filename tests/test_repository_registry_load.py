from __future__ import annotations

import json
from pathlib import Path

from bureau.core import Registry


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
