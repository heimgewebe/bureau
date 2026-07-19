from __future__ import annotations

import json
from pathlib import Path

import pytest

from bureau import legacy
from bureau.generation_closeout import (
    generation_closeout_sha256,
    validate_generation_closeout,
)
from bureau.v2 import Registry, plan_sha256, task_revision_sha256


def transition() -> dict:
    return {
        "schema_version": 1,
        "transition_id": "repoground-cutover-v1",
        "successor_ref": "github-pr:heimgewebe/grabowski#301@dfdc7a2",
        "predecessors": [
            {
                "surface_id": "service.rlens",
                "surface_kind": "service",
                "source_ref": "systemd-user:rlens.service",
            },
            {
                "surface_id": "source.repobrief",
                "surface_kind": "source-checkout",
                "source_ref": "git:heimgewebe/repo-brief@main",
            },
            {
                "surface_id": "catalog.legacy-name",
                "surface_kind": "catalog-semantics",
                "source_ref": "systemkatalog:nodes.json#lenskit",
            },
            {
                "surface_id": "recovery.runtime",
                "surface_kind": "recovery",
                "source_ref": "receipt:runtime-predecessor-recovery",
            },
        ],
        "does_not_establish": [
            "source_truth",
            "effect_authority",
            "recovery_completion",
        ],
    }


def valid_closeout(value: dict | None = None) -> dict:
    value = value or transition()
    closeout = {
        "schema_version": 1,
        "transition_id": value["transition_id"],
        "classifications": [
            {
                "surface_id": "service.rlens",
                "disposition": "removed",
                "source_ref": "systemd-show:rlens.service:not-found",
                "evidence_sha256": "1" * 64,
                "observed_at": "2026-07-19T09:00:00Z",
            },
            {
                "surface_id": "source.repobrief",
                "disposition": "archived",
                "source_ref": "receipt:repobrief-source-archive",
                "archive_ref": "checkout-archive:repobrief-v1",
                "evidence_sha256": "2" * 64,
                "observed_at": "2026-07-19T09:01:00Z",
            },
            {
                "surface_id": "catalog.legacy-name",
                "disposition": "still-required",
                "source_ref": "systemkatalog:compatibility-projection",
                "reason": "Retained until the last named consumer migrates.",
                "evidence_sha256": "3" * 64,
                "observed_at": "2026-07-19T09:02:00Z",
            },
            {
                "surface_id": "recovery.runtime",
                "disposition": "recovery",
                "source_ref": "receipt:runtime-predecessor-recovery",
                "recovery_ref": "archive:runtime-predecessor-v1",
                "evidence_sha256": "4" * 64,
                "observed_at": "2026-07-19T09:03:00Z",
            },
        ],
        "does_not_establish": [
            "source_truth",
            "effect_authority",
            "recovery_completion",
        ],
        "closeout_sha256": "0" * 64,
    }
    closeout["closeout_sha256"] = generation_closeout_sha256(value, closeout)
    return closeout


def task(state: str = "verified", *, include_closeout: bool = True) -> dict:
    metadata = {"generation_transition": transition()}
    if include_closeout:
        metadata["generation_closeout"] = valid_closeout(metadata["generation_transition"])
    return {
        "schema_version": 1,
        "id": "TEST-GENERATION-T001",
        "initiative": "TEST-GENERATION",
        "title": "Generation cutover",
        "state": state,
        "execution": {"mode": "interactive-agent", "policy": "review-before-effect"},
        "claims": [],
        "acceptance": [{"id": "proof", "assertion": "proof"}],
        "metadata": metadata,
    }


def write_task(root: Path, raw: dict) -> Path:
    path = root / "registry/tasks/BUR-TEST-001-T001.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def registry_task(root: Path) -> dict:
    return json.loads((root / "registry/tasks/BUR-TEST-001-T001.json").read_text())


def remove_from_queue(root: Path, task_id: str) -> None:
    path = root / "registry/queue.json"
    queue = json.loads(path.read_text(encoding="utf-8"))
    for lane in queue["lanes"].values():
        while task_id in lane:
            lane.remove(task_id)
    path.write_text(json.dumps(queue), encoding="utf-8")


def stamp_verified(root: Path, raw: dict) -> dict:
    raw["state"] = "verified"
    registry = Registry.load(root)
    raw.setdefault("metadata", {})["verification"] = {
        "task_sha256": task_revision_sha256(raw),
        "plan_sha256": plan_sha256(registry, raw["initiative"]),
    }
    return raw


def test_non_generation_task_is_unchanged() -> None:
    self_contained = task()
    self_contained["metadata"] = {"arbitrary_existing_metadata": True}
    assert validate_generation_closeout(self_contained) == []


def test_planned_transition_may_exist_before_closeout() -> None:
    assert validate_generation_closeout(task("planned", include_closeout=False)) == []


def test_terminal_transition_without_closeout_fails_closed() -> None:
    errors = validate_generation_closeout(task("verified", include_closeout=False))
    assert any("terminal generation transition requires" in item for item in errors)


def test_complete_hash_bound_closeout_is_valid() -> None:
    assert validate_generation_closeout(task()) == []


def test_missing_extra_and_duplicate_predecessors_are_rejected() -> None:
    raw = task()
    classifications = raw["metadata"]["generation_closeout"]["classifications"]
    classifications.pop()
    classifications.append(dict(classifications[0]))
    classifications.append(
        {
            "surface_id": "undeclared.surface",
            "disposition": "removed",
            "source_ref": "receipt:extra",
            "evidence_sha256": "f" * 64,
            "observed_at": "2026-07-19T09:04:00Z",
        }
    )
    errors = validate_generation_closeout(raw)
    assert any("duplicate surface_id service.rlens" in item for item in errors)
    assert any("missing predecessors" in item for item in errors)
    assert any("undeclared predecessors" in item for item in errors)


def test_disposition_specific_evidence_is_required() -> None:
    raw = task()
    closeout = raw["metadata"]["generation_closeout"]
    by_id = {item["surface_id"]: item for item in closeout["classifications"]}
    by_id["source.repobrief"].pop("archive_ref")
    by_id["catalog.legacy-name"].pop("reason")
    by_id["recovery.runtime"].pop("recovery_ref")
    errors = validate_generation_closeout(raw)
    assert any("archive_ref" in item for item in errors)
    assert any("reason" in item for item in errors)
    assert any("recovery_ref" in item for item in errors)


def test_closeout_hash_binds_transition_and_classifications() -> None:
    raw = task()
    raw["metadata"]["generation_closeout"]["classifications"][0][
        "source_ref"
    ] = "systemd-show:rlens.service:changed"
    errors = validate_generation_closeout(raw)
    assert any("closeout_sha256 does not match content" in item for item in errors)


def test_second_truth_boundaries_are_mandatory() -> None:
    raw = task()
    raw["metadata"]["generation_closeout"]["does_not_establish"] = [
        "source_truth"
    ]
    errors = validate_generation_closeout(raw)
    assert any("misses boundaries" in item for item in errors)


def test_transition_second_truth_boundaries_are_mandatory() -> None:
    raw = task()
    raw["metadata"]["generation_transition"]["does_not_establish"] = [
        "source_truth",
        "effect_authority",
        "other",
    ]
    errors = validate_generation_closeout(raw)
    assert any(
        "generation_transition.does_not_establish misses boundaries" in item
        for item in errors
    )


def test_observed_at_requires_timezone_aware_iso_timestamp() -> None:
    raw = task()
    raw["metadata"]["generation_closeout"]["classifications"][0][
        "observed_at"
    ] = "2026-07-19 09:00:00"
    errors = validate_generation_closeout(raw)
    assert any("observed_at must include a timezone" in item for item in errors)


def test_closeout_without_transition_is_rejected() -> None:
    raw = task()
    raw["metadata"].pop("generation_transition")
    errors = validate_generation_closeout(raw)
    assert any("generation_transition must be an object" in item for item in errors)


def test_registry_accepts_nonterminal_transition_without_closeout(registry_factory) -> None:
    root = registry_factory(task_count=1)
    raw = registry_task(root)
    raw["state"] = "ready"
    raw["metadata"] = {"generation_transition": transition()}
    write_task(root, raw)
    Registry.load(root).validate()


def test_registry_rejects_terminal_transition_without_closeout(registry_factory) -> None:
    root = registry_factory(task_count=1)
    raw = registry_task(root)
    raw["metadata"] = {"generation_transition": transition()}
    raw = stamp_verified(root, raw)
    write_task(root, raw)
    remove_from_queue(root, raw["id"])
    with pytest.raises(legacy.ValidationError, match="terminal generation transition"):
        Registry.load(root).validate()


def test_registry_accepts_complete_verified_generation_closeout(registry_factory) -> None:
    root = registry_factory(task_count=1)
    raw = registry_task(root)
    generation = transition()
    raw["metadata"] = {
        "generation_transition": generation,
        "generation_closeout": valid_closeout(generation),
    }
    raw = stamp_verified(root, raw)
    write_task(root, raw)
    remove_from_queue(root, raw["id"])
    Registry.load(root).validate()


def test_task_schema_rejects_archived_classification_without_archive_ref(
    registry_factory,
) -> None:
    root = registry_factory(task_count=1)
    raw = registry_task(root)
    generation = transition()
    closeout = valid_closeout(generation)
    closeout["classifications"][1].pop("archive_ref")
    raw["metadata"] = {
        "generation_transition": generation,
        "generation_closeout": closeout,
    }
    write_task(root, raw)
    with pytest.raises(legacy.ValidationError, match="archive_ref"):
        Registry.load(root)
