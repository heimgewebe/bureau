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
}


def test_deleted_repository_resources_are_coordination_only() -> None:
    for name in RETIRED:
        data = json.loads(
            (ROOT / "registry" / "resources" / f"{name}.json").read_text(
                encoding="utf-8"
            )
        )
        metadata = data["metadata"]
        assert metadata["lifecycle"] == "retired"
        assert metadata["coordination_only"] is True
        assert metadata["retired_on"] == "2026-09-18"
        claims = set(metadata["does_not_establish"])
        assert {
            "remote_repository_exists",
            "fleet_membership",
            "task_supply_target",
            "runtime_role",
            "execution_authority",
        } <= claims


def test_deleted_repositories_are_not_scout_supply_targets() -> None:
    resources = {resource for _name, resource in task_supply._SCOUT_REPOSITORIES}
    assert resources.isdisjoint({f"repo.{name}" for name in RETIRED})
