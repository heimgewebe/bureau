from __future__ import annotations

import json
from pathlib import Path

import pytest

from bureau.core import (
    Dispatcher,
    Registry,
    StateStore,
    complete_run,
    grabowski_handoff,
)
from bureau.legacy import NoEligibleTask
from bureau.rlens_policy import evaluate_task_rlens_policy
from bureau.schema_validation import DocumentSchemaError, SchemaSet

ROOT = Path(__file__).resolve().parents[1]
HEX64 = "a" * 64
HEX40 = "b" * 40


def rlens_ref() -> dict:
    return {
        "schema_version": 1,
        "repo": "lenskit",
        "stem": "lenskit-max-260701-1454",
        "manifest_sha256": HEX64,
        "bundle_commit": HEX40,
        "live_commit_at_claim": HEX40,
        "freshness_status": "fresh_exact",
        "task_profile": "repo_work",
        "preflight_status": "pass",
        "source": "grabowski.rlens_context_pack",
        "does_not_establish": ["repo_understood", "claims_true"],
    }


def schemas() -> SchemaSet:
    return SchemaSet(ROOT / "schemas")


def first_task_path(root: Path) -> Path:
    return sorted((root / "registry/tasks").glob("*.json"))[0]


def update_first_task(root: Path, **updates) -> dict:
    path = first_task_path(root)
    task = json.loads(path.read_text())
    task.update(updates)
    path.write_text(json.dumps(task))
    return task


def setup_dispatcher(root: Path, tmp_path: Path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("BUREAU_STATE_DIR", str(state))
    registry = Registry.load(root)
    store = StateStore(state / "bureau.sqlite3")
    return registry, store, Dispatcher(registry, store)


def test_schema_accepts_all_deterministic_rlens_modes() -> None:
    for mode in ["opportunistic", "required", "strict", "live-first", "external-safe"]:
        task = {
            "schema_version": 1,
            "id": "BUR-2026-002-T003",
            "initiative": "BUR-2026-002",
            "title": "rLens mode policy",
            "state": "planned",
            "execution": {"mode": "interactive-agent", "policy": "review-before-effect"},
            "claims": [],
            "acceptance": [{"id": "mode-policy", "assertion": "mode is deterministic"}],
            "rlens_policy": {"mode": mode, "task_profile": "repo_work"},
        }
        schemas().validate("task", task, f"task:{mode}")


def test_schema_rejects_unknown_rlens_mode() -> None:
    task = {
        "schema_version": 1,
        "id": "BUR-2026-002-T003",
        "initiative": "BUR-2026-002",
        "title": "rLens mode policy",
        "state": "planned",
        "execution": {"mode": "interactive-agent", "policy": "review-before-effect"},
        "claims": [],
        "acceptance": [{"id": "mode-policy", "assertion": "mode is deterministic"}],
        "rlens_policy": {"mode": "maybe"},
    }
    with pytest.raises(DocumentSchemaError, match="maybe"):
        schemas().validate("task", task, "task")


def test_policy_evaluation_blocks_required_without_ref_or_skip() -> None:
    task = {"rlens_policy": {"mode": "required", "task_profile": "repo_work"}}

    result = evaluate_task_rlens_policy(task)

    assert result["mode"] == "required"
    assert result["requires_context"] is True
    assert result["status"] == "blocked"
    assert result["block_reason"] == "rlens_policy_required_requires_context_ref_or_skip_reason"


def test_required_task_without_ref_or_skip_is_not_eligible(registry_factory, tmp_path, monkeypatch):
    root = registry_factory(1)
    update_first_task(root, rlens_policy={"mode": "required", "task_profile": "repo_work"})
    _registry, _store, dispatcher = setup_dispatcher(root, tmp_path, monkeypatch)

    frontier = dispatcher.frontier({"repository"})

    assert frontier[0]["eligible"] is False
    assert "rlens policy blocked" in " ".join(frontier[0]["reasons"])
    reason_text = " ".join(frontier[0]["reasons"])
    assert "rlens_policy_required_requires_context_ref_or_skip_reason" in reason_text
    with pytest.raises(NoEligibleTask):
        dispatcher.claim_next("worker", ("repository",))


def test_required_task_with_skip_reason_records_policy_in_envelope_and_receipt(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    update_first_task(
        root,
        rlens_policy={
            "mode": "required",
            "task_profile": "repo_work",
            "skip_reason": "not_yet_generated",
        },
    )
    registry, store, dispatcher = setup_dispatcher(root, tmp_path, monkeypatch)

    claimed = dispatcher.claim_next("worker", ("repository",))

    policy = claimed["envelope"]["rlens_context_policy"]
    assert policy["mode"] == "required"
    assert policy["status"] == "skipped"
    assert policy["skip_reason"] == "not_yet_generated"
    receipt = complete_run(
        registry,
        store,
        claimed["run"]["run_id"],
        {"proof": {"ok": True}},
    )["receipt"]
    assert receipt["rlens_context_policy"]["status"] == "skipped"
    assert receipt["rlens_context_policy"]["mode"] == "required"


def test_strict_task_with_ref_records_satisfied_policy(registry_factory, tmp_path, monkeypatch):
    root = registry_factory(1)
    update_first_task(
        root,
        rlens_policy={"mode": "strict", "task_profile": "pr_review"},
        rlens_context_ref=rlens_ref(),
    )
    _registry, _store, dispatcher = setup_dispatcher(root, tmp_path, monkeypatch)

    claimed = dispatcher.claim_next("worker", ("repository",))

    assert claimed["envelope"]["rlens_context_ref"]["repo"] == "lenskit"
    assert claimed["envelope"]["rlens_context_policy"]["mode"] == "strict"
    assert claimed["envelope"]["rlens_context_policy"]["status"] == "satisfied"
    assert claimed["envelope"]["rlens_context_policy"]["has_context_ref"] is True


def test_live_first_task_without_ref_records_not_required_policy(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    update_first_task(root, rlens_policy={"mode": "live-first", "task_profile": "runtime"})
    _registry, _store, dispatcher = setup_dispatcher(root, tmp_path, monkeypatch)

    claimed = dispatcher.claim_next("worker", ("repository",))

    policy = claimed["envelope"]["rlens_context_policy"]
    assert policy["mode"] == "live-first"
    assert policy["requires_context"] is False
    assert policy["status"] == "not_required"
    assert policy["skip_reason"] == "live_first_primary"


def test_handoff_includes_rlens_context_policy(registry_factory, tmp_path, monkeypatch):
    root = registry_factory(1)
    update_first_task(
        root,
        rlens_policy={
            "mode": "external-safe",
            "skip_reason": "external_safe_export_blocked",
        },
    )
    registry, store, dispatcher = setup_dispatcher(root, tmp_path, monkeypatch)

    claimed = dispatcher.claim_next("worker", ("repository",))
    handoff = grabowski_handoff(registry, store, claimed["run"]["run_id"])

    assert handoff["rlens_context_policy"]["mode"] == "external-safe"
    assert handoff["rlens_context_policy"]["status"] == "skipped"
