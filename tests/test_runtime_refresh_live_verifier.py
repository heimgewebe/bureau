"""Current-source host and verifier evidence must precede a selected authority's verified CAS."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bureau import runtime_refresh as refresh
from bureau_cycle import verifier_runner

MAIN = "9" * 40
MANIFEST = "a" * 64
INSTALLED = datetime(2026, 10, 29, 18, 0, tzinfo=timezone.utc)
STARTED = INSTALLED + timedelta(seconds=15)
FINISHED = STARTED + timedelta(seconds=2)
OBSERVED = STARTED + timedelta(seconds=10)
NOW_MONOTONIC_USEC = 1_000_000_000
SERVICE_START_USEC = NOW_MONOTONIC_USEC - 10_000_000


def _proof_fixture(tmp_path: Path):
    latest_path = tmp_path / "verifier" / "latest.json"
    run_path = latest_path.parent / "runs" / "verifier-run.json"
    run_path.parent.mkdir(parents=True)
    receipt = {
        "schema_version": 2,
        "contract_version": 2,
        "cycle_id": "2026-10-29T20",
        "stage": "verifier",
        "runtime_launch_identity": {
            "schema_version": 1,
            "kind": "bureau_runtime_launch_identity_at_verifier_entry",
            "source_commit": MAIN,
            "manifest_sha256": MANIFEST,
        },
        "run_id": "verifier-test-post-install",
        "trigger": "manual-regression",
        "started_at": refresh.isoformat(STARTED),
        "finished_at": refresh.isoformat(FINISHED),
        "lifecycle_state": "terminal",
        "result": "completed",
        "degraded": False,
        "critical": False,
        "evidence": [],
        "next_action": "ok",
        "receipt_path": str(run_path),
    }
    def save_receipt():
        data = json.dumps(receipt, sort_keys=True, indent=2) + "\n"
        latest_path.write_text(data, encoding="utf-8")
        run_path.write_text(data, encoding="utf-8")
    save_receipt()
    intent = {"main_commit": MAIN, "prefix": str(tmp_path), "bin_dir": str(tmp_path / "bin")}
    install = {"manifest_sha256": MANIFEST, "installed_at": refresh.isoformat(INSTALLED)}
    persisted = {"source_commit": MAIN, "manifest_sha256": MANIFEST, "scheduler": None}
    result = {"status": "deployed", "effect_started": True, "install_receipt": install,
              "readback": persisted}
    state = {"service_start": SERVICE_START_USEC, "status": "success",
             "readback": copy.deepcopy(persisted)}
    def reader(**kwargs):
        assert kwargs["expected_commit"] == MAIN
        return state["readback"]
    def runner(argv):
        return subprocess.CompletedProcess(
            argv, 0, "\n".join((
                "LoadState=loaded",
                "ActiveState=inactive",
                "Result=" + state["status"],
                "ExecMainCode=1",
                "ExecMainStatus=0",
                "ExecMainStartTimestampMonotonic=" + str(state["service_start"]),
            )) + "\n", "",
        )
    def prove():
        return refresh._live_verifier_closeout_evidence(
            intent=intent,
            result=result,
            live_readback=reader,
            status_runner=runner,
            latest_path=latest_path,
            observed_unix=OBSERVED.timestamp(),
            observed_monotonic_usec=NOW_MONOTONIC_USEC,
        )
    return prove, receipt, save_receipt, state, result


def test_post_install_verifier_current_source_produces_typed_evidence(tmp_path: Path) -> None:
    prove, _, _, _, _ = _proof_fixture(tmp_path)
    evidence = prove()
    assert evidence["source_commit"] == MAIN
    assert evidence["manifest_sha256"] == MANIFEST
    assert evidence["verifier_result"] == "completed"
    refresh._validate_live_verifier_closeout_evidence(
        evidence, source_commit=MAIN, manifest_sha256=MANIFEST,
    )
    altered = {**evidence, "verifier_result": "partial"}
    with pytest.raises(refresh.RuntimeRefreshError):
        refresh._validate_live_verifier_closeout_evidence(
            altered, source_commit=MAIN, manifest_sha256=MANIFEST,
        )


@pytest.mark.parametrize(
    "kind",
    [
        "old-run", "failed-run", "critical-run", "stale-service",
        "failed-service", "successor", "old-runtime-verifier", "no-runtime-identity",
    ],
)
def test_post_install_proof_rejects_noncurrent_or_unhealthy_execution(
    tmp_path: Path, kind: str,
) -> None:
    prove, receipt, save, state, _ = _proof_fixture(tmp_path)
    if kind == "old-run":
        receipt["started_at"] = refresh.isoformat(INSTALLED - timedelta(seconds=10))
        receipt["finished_at"] = refresh.isoformat(INSTALLED - timedelta(seconds=9))
        save()
    elif kind == "failed-run":
        receipt["result"] = "failed"
        save()
    elif kind == "critical-run":
        receipt["critical"] = True
        save()
    elif kind == "stale-service":
        state["service_start"] = NOW_MONOTONIC_USEC - 40_000_000
    elif kind == "failed-service":
        state["status"] = "failed"
    elif kind == "successor":
        state["readback"]["source_commit"] = "b" * 40
    elif kind == "old-runtime-verifier":
        receipt["runtime_launch_identity"]["source_commit"] = "b" * 40
        save()
    elif kind == "no-runtime-identity":
        receipt.pop("runtime_launch_identity")
        save()
    with pytest.raises(refresh.RuntimeRefreshError) as error:
        prove()
    assert error.value.code.startswith("authority-closeout-live-")


def test_post_install_proof_rejects_fake_latest_without_run(tmp_path: Path) -> None:
    prove, receipt, save, _, _ = _proof_fixture(tmp_path)
    receipt["receipt_path"] = str(tmp_path / "other" / "injected.json")
    save()
    with pytest.raises(refresh.RuntimeRefreshError, match="durable run artifact"):
        prove()


def test_live_verifier_optin_contract_rejects_unknown_config() -> None:
    assert refresh._required_post_refresh_verifier_contract({"metadata": {}}) is False
    assert refresh._required_post_refresh_verifier_contract({
        "metadata": {"post_refresh_live_verifier": refresh.POST_REFRESH_LIVE_VERIFIER_CONTRACT}
    }) is True
    with pytest.raises(refresh.RuntimeRefreshError, match="contract"):
        refresh._required_post_refresh_verifier_contract({
            "metadata": {"post_refresh_live_verifier": {"service": "anything.service"}}
        })


def test_closeout_cas_path_calls_live_verifier_before_terminal_write() -> None:
    import inspect
    source = inspect.getsource(refresh._closeout_runtime_refresh_authority)
    assert source.index("_live_verifier_closeout_evidence(") < source.index(
        "_put_authority_task("
    )
    assert "closeout[\"live_verifier_evidence\"] = live_verifier_evidence" in source


def test_verifier_captures_launch_manifest_source_before_cutover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "deployment-manifest.json"
    path.write_text(json.dumps({
        "kind": "bureau_runtime_deployment", "source_commit": MAIN,
    }), encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setenv("BUREAU_RUNTIME_MANIFEST", str(path))
    monkeypatch.setenv("BUREAU_RUNTIME_MANIFEST_SHA256", digest)
    assert verifier_runner._runtime_launch_identity() == {
        "schema_version": 1,
        "kind": "bureau_runtime_launch_identity_at_verifier_entry",
        "source_commit": MAIN,
        "manifest_sha256": digest,
    }
    path.write_text(json.dumps({
        "kind": "bureau_runtime_deployment", "source_commit": "b" * 40,
    }), encoding="utf-8")
    assert verifier_runner._runtime_launch_identity() is None
    monkeypatch.delenv("BUREAU_RUNTIME_MANIFEST_SHA256")
    assert verifier_runner._runtime_launch_identity() is None

def test_verifier_run_copy_must_match_exact_latest_bytes(tmp_path: Path) -> None:
    prove, receipt, _, _, _ = _proof_fixture(tmp_path)
    run_path = Path(receipt["receipt_path"])
    run_path.write_text(json.dumps(receipt, indent=1), encoding="utf-8")
    with pytest.raises(refresh.RuntimeRefreshError, match="durable run artifact"):
        prove()
