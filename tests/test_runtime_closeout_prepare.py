from __future__ import annotations

import json
import sqlite3
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest

from bureau import cli as bureau_cli
from bureau import v2 as bureau_v2
from bureau.core import RunStateConflict, sha256_json

RUN_ID = "BUR-RUN-20260827T190022Z-f726fb971c"
TASK_ID = "OPERATOR-INTEGRATION-LOOP-V1-FU-EXACT-RUNTIME-CLOSEOUT-LEASE-20260827"
TASK_SHA256 = "1" * 64
PLAN_SHA256 = "2" * 64


def _valid_claim_intent(run_id: str = RUN_ID) -> dict[str, Any]:
    intent: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "task_id": TASK_ID,
        "worker_id": "worker:test",
        "kind": "interactive-agent",
        "capabilities": [],
        "resource": None,
        "task_sha256": TASK_SHA256,
        "plan_sha256": PLAN_SHA256,
        "required_resource_keys": [],
        "lease_owner_id": f"bureau-claim:{run_id}",
        "created_at": "2026-08-27T19:00:22Z",
        "expires_at_unix": 2_000_000_000,
        "workspace": None,
        "operator_approval": {},
        "runtime_truth_sha256": "4" * 64,
        "does_not_establish": [],
        "intent_sha256": "",
    }
    intent["intent_sha256"] = bureau_v2.coordinated_claim_intent_sha256(intent)
    return intent


class _FakeStore:
    def __init__(
        self,
        state_root: Path,
        *,
        state: str = "assigned",
        run_id: str = RUN_ID,
        coordinated: bool = True,
        claim_intent: dict[str, Any] | None = None,
    ):
        self.state_root = state_root
        if claim_intent is not None:
            envelope = {"claim_intent": claim_intent}
        elif coordinated:
            envelope = {"claim_intent": _valid_claim_intent(run_id)}
        else:
            envelope = {}
        envelope_sha256 = sha256_json(envelope)
        self._run = {
            "run_id": run_id,
            "task_id": TASK_ID,
            "state": state,
            "task_sha256": TASK_SHA256,
            "plan_sha256": PLAN_SHA256,
            "envelope_sha256": envelope_sha256,
        }
        self._connection = sqlite3.connect(":memory:")
        self._connection.row_factory = sqlite3.Row
        self._connection.execute(
            "CREATE TABLE runs("
            "run_id TEXT PRIMARY KEY,envelope_json TEXT NOT NULL,envelope_sha256 TEXT NOT NULL)"
        )
        self._connection.execute(
            "INSERT INTO runs(run_id,envelope_json,envelope_sha256) VALUES(?,?,?)",
            (run_id, json.dumps(envelope), envelope_sha256),
        )

    def run(self, run_id: str) -> dict[str, object]:
        assert run_id == self._run["run_id"]
        return dict(self._run)

    def connect(self) -> nullcontext[sqlite3.Connection]:
        return nullcontext(self._connection)


def test_runtime_closeout_prepare_emits_one_exact_grabowski_lease_request(tmp_path: Path) -> None:
    store = _FakeStore(tmp_path)
    result = bureau_cli.runtime_closeout_prepare(store, RUN_ID)

    closeout_root = (tmp_path / "runtime-closeout" / RUN_ID).resolve()
    expected_metadata = {
        "task_id": TASK_ID,
        "run_id": RUN_ID,
        "operation": "runtime-closeout",
    }
    expected_request = {
        "owner_id": f"bureau-runtime-closeout:{RUN_ID}",
        "resource_keys": [f"path:{closeout_root}"],
        "purpose": f"Bureau exact-runtime closeout {RUN_ID}",
        "ttl_seconds": 300,
        "metadata": expected_metadata,
    }

    assert result["kind"] == "bureau_runtime_closeout_lease_prepare"
    assert result["status"] == "ready"
    assert result["effect_started"] is False
    assert result["run_id"] == RUN_ID
    assert result["task_id"] == TASK_ID
    assert result["task_sha256"] == TASK_SHA256
    assert result["plan_sha256"] == PLAN_SHA256
    assert result["envelope_sha256"] == store._run["envelope_sha256"]
    assert result["closeout_owner_id"] == expected_request["owner_id"]
    assert result["closeout_resource_key"] == expected_request["resource_keys"][0]
    assert result["minimum_remaining_seconds"] == 180
    assert result["grabowski_resource_acquire"] == {
        "tool": "grabowski_resource_acquire",
        "arguments": expected_request,
        "arguments_sha256": sha256_json(expected_request),
    }
    assert result["required_live_validation"] == {
        "owner_id": expected_request["owner_id"],
        "task_id": TASK_ID,
        "required_resource_keys": expected_request["resource_keys"],
        "required_metadata": expected_metadata,
        "minimum_remaining_seconds": 180,
    }
    unsigned = dict(result)
    receipt_sha256 = unsigned.pop("prepare_receipt_sha256")
    assert receipt_sha256 == sha256_json(unsigned)
    assert "lease_acquisition" in result["does_not_establish"]
    assert "deployment_authority" in result["does_not_establish"]


def test_runtime_closeout_prepare_rejects_uncoordinated_claim_next_run(tmp_path: Path) -> None:
    store = _FakeStore(tmp_path, coordinated=False)

    with pytest.raises(RunStateConflict) as exc_info:
        bureau_cli.runtime_closeout_prepare(store, RUN_ID)

    assert exc_info.value.code == "runtime-closeout-pickup-lease-required"
    assert exc_info.value.payload()["effect_applied"] is False


def test_runtime_closeout_prepare_rejects_malformed_coordinated_intent(tmp_path: Path) -> None:
    store = _FakeStore(tmp_path, claim_intent={"schema_version": 1})

    with pytest.raises(
        bureau_v2.legacy.StateError,
        match="coordinated claim intent fields are not exact",
    ):
        bureau_cli.runtime_closeout_prepare(store, RUN_ID)


def test_runtime_closeout_prepare_is_explicitly_read_only() -> None:
    args = bureau_cli.parser().parse_args(["runtime-closeout-prepare", RUN_ID])

    assert bureau_cli._command_mutates(args) is False
    assert bureau_cli._command_effect_scope(args) == "read_only"


def test_runtime_closeout_prepare_rejects_terminal_run(tmp_path: Path) -> None:
    store = _FakeStore(tmp_path, state="succeeded")

    with pytest.raises(RunStateConflict) as exc_info:
        bureau_cli.runtime_closeout_prepare(store, RUN_ID)

    assert exc_info.value.code == "runtime-closeout-run-not-active"
    assert exc_info.value.payload()["effect_applied"] is False


def test_runtime_closeout_prepare_rejects_missing_revision_binding(tmp_path: Path) -> None:
    store = _FakeStore(tmp_path)
    store._run["plan_sha256"] = None

    with pytest.raises(RunStateConflict) as exc_info:
        bureau_cli.runtime_closeout_prepare(store, RUN_ID)

    assert exc_info.value.code == "runtime-closeout-run-binding-invalid"
    assert exc_info.value.details == {"field": "plan_sha256"}


def test_runtime_closeout_prepare_rejects_path_escape(tmp_path: Path) -> None:
    escaped_run_id = "../outside"
    store = _FakeStore(tmp_path, run_id=escaped_run_id)

    with pytest.raises(RunStateConflict) as exc_info:
        bureau_cli.runtime_closeout_prepare(store, escaped_run_id)

    assert exc_info.value.code == "runtime-closeout-temp-root-invalid"


def test_runtime_closeout_prepare_rejects_symlink_parent(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "runtime-closeout").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RunStateConflict) as exc_info:
        bureau_cli.runtime_closeout_prepare(_FakeStore(tmp_path), RUN_ID)

    assert exc_info.value.code == "runtime-closeout-temp-root-invalid"


def test_runtime_closeout_prepare_rejects_nonempty_existing_root(tmp_path: Path) -> None:
    closeout_root = tmp_path / "runtime-closeout" / RUN_ID
    closeout_root.mkdir(parents=True)
    (closeout_root / "leftover.json").write_text("{}", encoding="utf-8")

    with pytest.raises(RunStateConflict) as exc_info:
        bureau_cli.runtime_closeout_prepare(_FakeStore(tmp_path), RUN_ID)

    assert exc_info.value.code == "runtime-closeout-temp-root-not-empty"
