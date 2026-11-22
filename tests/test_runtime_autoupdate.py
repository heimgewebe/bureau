from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bureau import runtime_autoupdate as auto
from bureau import runtime_refresh as rr

MAIN = "b" * 40
DEPLOYED = "a" * 40
NOW = datetime(2026, 11, 22, 21, 0, tzinfo=timezone.utc)


def _manifest(path: Path, source: str = DEPLOYED) -> str:
    value = {"schema_version": 1, "kind": "bureau_runtime_deployment", "source_commit": source}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(rr.canonical_bytes(value))
    return rr.load_manifest(path)[1]


def _observation(manifest_sha: str, **overrides) -> dict:
    value = {
        "schema_version": 1,
        "kind": "bureau_runtime_refresh_observation",
        "main_commit": MAIN,
        "deployed_source_commit": DEPLOYED,
        "deployed_manifest_sha256": manifest_sha,
        "pull_request": {"number": 1, "merge_commit": MAIN},
        "check_summary": {"validate (3.12)": {"state": "success"}},
        "source_ancestry": {"status": "proven"},
        "status": "candidate",
        "reason_codes": [],
        "recovery_action": {"action": "prepare-intent", "eligible": True},
        "observed_at": rr.isoformat(NOW),
    }
    value.update(overrides)
    value.pop("observation_sha256", None)
    return rr.bind_digest(value, "observation_sha256")


def _write(path: Path, value: dict) -> Path:
    path.write_bytes(rr.canonical_bytes(value))
    return path


# --- standing approval validator -------------------------------------------------


def test_standing_approval_accepts_green_descendant(tmp_path: Path) -> None:
    manifest = tmp_path / "deployment-manifest.json"
    obs = _write(tmp_path / "obs.json", _observation(_manifest(manifest)))
    approval = rr.validate_standing_auto_update_approval(
        obs, expected_source_commit=MAIN, manifest_path=manifest, now=NOW
    )
    assert approval["kind"] == rr.STANDING_AUTO_UPDATE_KIND
    assert approval["main_commit"] == MAIN
    assert approval["previous_source_commit"] == DEPLOYED


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"main_commit": "c" * 40}, "auto-update-source-mismatch"),
        ({"status": "blocked", "recovery_action": {"eligible": False}}, "auto-update-not-eligible"),
        (
            {"check_summary": {"validate (3.12)": {"state": "failure"}}},
            "auto-update-checks-not-green",
        ),
        ({"check_summary": {}}, "auto-update-checks-not-green"),
        ({"source_ancestry": {"status": "rejected"}}, "auto-update-ancestry-unproven"),
        (
            {"observed_at": rr.isoformat(NOW - timedelta(hours=2))},
            "auto-update-observation-stale",
        ),
        ({"deployed_manifest_sha256": "0" * 64}, "auto-update-manifest-drift"),
    ],
)
def test_standing_approval_rejects(tmp_path: Path, overrides: dict, code: str) -> None:
    manifest = tmp_path / "deployment-manifest.json"
    obs = _write(tmp_path / "obs.json", _observation(_manifest(manifest), **overrides))
    with pytest.raises(rr.RuntimeRefreshError) as excinfo:
        rr.validate_standing_auto_update_approval(
            obs, expected_source_commit=MAIN, manifest_path=manifest, now=NOW
        )
    assert excinfo.value.code == code


def test_standing_approval_rejects_tampered_observation(tmp_path: Path) -> None:
    manifest = tmp_path / "deployment-manifest.json"
    value = _observation(_manifest(manifest))
    value["check_summary"] = {}
    obs = _write(tmp_path / "obs.json", value)
    with pytest.raises(rr.RuntimeRefreshError):
        rr.validate_standing_auto_update_approval(
            obs, expected_source_commit=MAIN, manifest_path=manifest, now=NOW
        )


# --- orchestration ----------------------------------------------------------------


def _run(tmp_path: Path, observation: dict, **fakes):
    calls: list[str] = []
    state_root = tmp_path / "state"

    def observer(**_kwargs):
        calls.append("observe")
        return observation

    def source_preparer(*, workspace: Path, expected_commit: str, **_kwargs):
        calls.append("clone")
        assert expected_commit == MAIN
        workspace.mkdir(parents=True)
        return {"root": str(workspace), "head": expected_commit}

    def installer(*, standing_auto_update: Path, **_kwargs):
        calls.append("install")
        assert json.loads(standing_auto_update.read_text())["main_commit"] == MAIN
        return {"receipt_path": "r.json", "rollback": {"directory": str(tmp_path / "bk")}}

    def readback(**_kwargs):
        calls.append("readback")
        return {"release_id": "rel", "manifest_sha256": "m" * 64}

    def rollback(receipt, *, manifest_path):
        calls.append("rollback")
        return []

    kwargs = {
        "observer": observer,
        "source_preparer": source_preparer,
        "installer": installer,
        "readback": readback,
        "rollback": rollback,
    }
    kwargs.update(fakes)
    result = auto.run_auto_update(
        state_root=state_root,
        manifest_path=tmp_path / "deployment-manifest.json",
        prefix=tmp_path / "prefix",
        bin_dir=tmp_path / "bin",
        user_unit_dir=tmp_path / "units",
        libexec_dir=tmp_path / "libexec",
        runtime_user_unit_dir=tmp_path / "run-units",
        resource_db=tmp_path / "missing.sqlite3",
        now=lambda: NOW,
        **kwargs,
    )
    return result, calls, state_root


def test_auto_update_activates_green_main(tmp_path: Path) -> None:
    result, calls, state_root = _run(tmp_path, _observation("f" * 64))
    assert result["status"] == "activated"
    assert calls == ["observe", "clone", "install", "readback"]
    assert auto.exit_code(result) == 0
    last = json.loads((state_root / "auto-update/last-result.json").read_text())
    assert last["status"] == "activated"
    assert list((state_root / "auto-update/history").glob("*-activated.json"))
    assert not any((state_root / "auto-update/workspaces").iterdir())


def test_auto_update_unchanged_when_current(tmp_path: Path) -> None:
    result, calls, _ = _run(
        tmp_path, _observation("f" * 64, status="already_current", deployed_source_commit=MAIN)
    )
    assert result["status"] == "unchanged"
    assert calls == ["observe"]


def test_auto_update_waits_for_ci(tmp_path: Path) -> None:
    result, calls, _ = _run(
        tmp_path,
        _observation("f" * 64, status="blocked", reason_codes=["required-ci-not-green"]),
    )
    assert result["status"] == "waiting"
    assert auto.exit_code(result) == 0
    assert calls == ["observe"]


def test_auto_update_blocks_on_divergence(tmp_path: Path) -> None:
    result, calls, _ = _run(
        tmp_path,
        _observation(
            "f" * 64, status="blocked", reason_codes=["deployed-source-not-main-ancestor"]
        ),
    )
    assert result["status"] == "blocked"
    assert auto.exit_code(result) != 0
    assert calls == ["observe"]


def test_auto_update_rolls_back_on_failed_readback(tmp_path: Path) -> None:
    def readback(**_kwargs):
        raise rr.RuntimeRefreshError("readback-check-failed", "check invalid")

    result, calls, _ = _run(tmp_path, _observation("f" * 64), readback=readback)
    assert result["status"] == "rolled_back"
    assert result["error"]["code"] == "readback-check-failed"
    assert "rollback" in calls
    assert auto.exit_code(result) != 0


def test_auto_update_reports_failed_rollback(tmp_path: Path) -> None:
    def readback(**_kwargs):
        raise rr.RuntimeRefreshError("readback-check-failed", "check invalid")

    def rollback(receipt, *, manifest_path):
        return [{"operation": "restore-manifest", "error": "boom"}]

    result, _, _ = _run(tmp_path, _observation("f" * 64), readback=readback, rollback=rollback)
    assert result["status"] == "failed"
    assert result["rollback_failures"]


def test_auto_update_installer_self_rollback_is_reported(tmp_path: Path) -> None:
    def installer(**_kwargs):
        raise rr.RuntimeRefreshError("scheduler-convergence-rolled-back", "restored")

    result, _, _ = _run(tmp_path, _observation("f" * 64), installer=installer)
    assert result["status"] == "rolled_back"
    assert result["stage"] == "install"


def test_auto_update_skips_when_runtime_leased(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "missing.sqlite3"
    db.write_text("")
    monkeypatch.setattr(
        auto, "_live_runtime_leases", lambda *a, **k: [{"resource_key": "path:/x"}]
    )
    result, calls, _ = _run(tmp_path, _observation("f" * 64))
    assert result["status"] == "busy"
    assert calls == []


def test_rollback_restores_manifest_and_launcher(tmp_path: Path) -> None:
    backup = tmp_path / "backups/1"
    backup.mkdir(parents=True)
    (backup / "deployment-manifest.json").write_text("old-manifest")
    (backup / "bureau").write_text("old-launcher")
    (backup / "bureau").chmod(0o755)
    manifest = tmp_path / "deployment-manifest.json"
    manifest.write_text("new-manifest")
    launcher = tmp_path / "bin/bureau"
    launcher.parent.mkdir()
    launcher.write_text("new-launcher")
    receipt = {
        "launcher_path": str(launcher),
        "rollback": {
            "directory": str(backup),
            "manifest": str(backup / "deployment-manifest.json"),
            "launcher": str(backup / "bureau"),
            "launcher_kind": "file",
        },
    }
    assert auto.rollback_to_previous(receipt, manifest_path=manifest) == []
    assert manifest.read_text() == "old-manifest"
    assert launcher.read_text() == "old-launcher"


def test_rollback_without_backup_is_failure(tmp_path: Path) -> None:
    failures = auto.rollback_to_previous({}, manifest_path=tmp_path / "m.json")
    assert failures


def test_run_installer_standing_argv(tmp_path: Path, monkeypatch) -> None:
    seen: list[list[str]] = []

    def fake_run(argv, *, cwd=None, timeout=60):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true}\n', stderr="")

    monkeypatch.setattr(rr, "_run", fake_run)
    monkeypatch.setattr(rr, "validate_scheduler_runtime_layout", lambda **_: None)
    monkeypatch.setattr(rr, "validate_runtime_user_unit_dir", lambda path: path)
    obs = tmp_path / "obs.json"
    rr.run_installer(
        source=tmp_path,
        prefix=tmp_path / "p",
        bin_dir=tmp_path / "b",
        user_unit_dir=tmp_path / "u",
        libexec_dir=tmp_path / "l",
        runtime_user_unit_dir=tmp_path / "r",
        standing_auto_update=obs,
    )
    argv = seen[0]
    assert argv[argv.index("--standing-auto-update") + 1] == str(obs)
    assert "--approval-intent" not in argv
    assert "--converge-user-systemd" in argv
    with pytest.raises(rr.RuntimeRefreshError):
        rr.run_installer(
            source=tmp_path,
            prefix=tmp_path / "p",
            bin_dir=tmp_path / "b",
            user_unit_dir=tmp_path / "u",
            libexec_dir=tmp_path / "l",
            runtime_user_unit_dir=tmp_path / "r",
        )


def test_autoupdate_unit_is_managed_scheduler() -> None:
    from bureau import runtime_identity

    assert "bureau-runtime-autoupdate" in rr.RUNTIME_SCHEDULER_NAMES
    assert "bureau-runtime-autoupdate" in runtime_identity.SCHEDULER_NAMES
    root = Path(__file__).resolve().parents[1]
    installer = (root / "ops/install-bureau-runtime.py").read_text()
    assert '"bureau-runtime-autoupdate"' in installer
    shim = root / "ops/systemd/libexec/bureau-runtime-autoupdate"
    assert "bureau-runtime-refresh\" auto-update" in shim.read_text()
