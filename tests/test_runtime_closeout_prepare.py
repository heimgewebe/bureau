from __future__ import annotations

from pathlib import Path

import pytest

from bureau import cli as bureau_cli
from bureau.core import RunStateConflict, sha256_json

RUN_ID = "BUR-RUN-20260827T190022Z-f726fb971c"
TASK_ID = "OPERATOR-INTEGRATION-LOOP-V1-FU-EXACT-RUNTIME-CLOSEOUT-LEASE-20260827"
TASK_SHA256 = "1" * 64
PLAN_SHA256 = "2" * 64
ENVELOPE_SHA256 = "3" * 64


class _FakeStore:
    def __init__(self, state_root: Path, *, state: str = "assigned", run_id: str = RUN_ID):
        self.state_root = state_root
        self._run = {
            "run_id": run_id,
            "task_id": TASK_ID,
            "state": state,
            "task_sha256": TASK_SHA256,
            "plan_sha256": PLAN_SHA256,
            "envelope_sha256": ENVELOPE_SHA256,
        }

    def run(self, run_id: str) -> dict[str, object]:
        assert run_id == self._run["run_id"]
        return dict(self._run)


def test_runtime_closeout_prepare_emits_one_exact_grabowski_lease_request(tmp_path: Path) -> None:
    result = bureau_cli.runtime_closeout_prepare(_FakeStore(tmp_path), RUN_ID)

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
    assert result["envelope_sha256"] == ENVELOPE_SHA256
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
