"""T044 pre-effect gates: an executable Registry document is not a claim grant."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from bureau.acceptance import t044_claim_admission_reasons, validate_acceptance_contract
from bureau.core import Dispatcher, Registry, StateStore


def t044_task() -> dict:
    root = Path(__file__).resolve().parents[1]
    return json.loads(
        (root / "registry/tasks/WELTGEWEBE-OS-V1-T044.json").read_text(encoding="utf-8")
    )


def test_t044_unselected_green_remains_readable_but_not_claimable() -> None:
    task = t044_task()
    validate_acceptance_contract(task)
    reasons = t044_claim_admission_reasons(task, runtime_reader_registered=False)
    assert any("authorized Green target" in reason for reason in reasons)


def test_t044_frozen_green_still_requires_primary_runtime_reader() -> None:
    task = copy.deepcopy(t044_task())
    for criterion in task["acceptance"]:
        if criterion["verifier"] == "runtime_proof_bundle":
            criterion["verifier_config"].update(
                expected_runtime_revision="a" * 40,
                expected_target_identity="authorized-green",
            )
    validate_acceptance_contract(task)
    assert any(
        "trusted primary runtime proof reader not registered" in reason
        for reason in t044_claim_admission_reasons(task, runtime_reader_registered=False)
    )
    # Caller registration is only a prerequisite, not authenticated source evidence.
    assert t044_claim_admission_reasons(task, runtime_reader_registered=True) == []


def test_dispatcher_blocks_t044_before_claim_for_missing_green(tmp_path: Path) -> None:
    registry = Registry.load(Path(__file__).resolve().parents[1])
    store = StateStore(tmp_path / "bureau.sqlite3", state_root=tmp_path)
    dispatcher = Dispatcher(
        registry, store, open_pr_reservations_provider=lambda _registry: []
    )
    task = dispatcher.registry.tasks["WELTGEWEBE-OS-V1-T044"]
    reasons = dispatcher.reasons(task, set(task.capabilities), [], [], {})
    assert any("T044 claim blocked: authorized Green target" in reason for reason in reasons)
    assert store.list_runs() == []
