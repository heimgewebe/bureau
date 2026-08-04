from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import bureau.cycle_deployment as deployment
from bureau.cycle_deployment import (
    SOURCES,
    STAGES,
    STATE_DIRECTORIES,
    CycleDeploymentError,
    audit_cycle_deployment,
)
from bureau.runtime_identity import _package_tree_sha256

REPO_ROOT = Path(__file__).parents[1]


def _copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _fixture(tmp_path: Path) -> dict[str, Path]:
    release_build = tmp_path / "release-build"
    units = tmp_path / "units"
    shims = tmp_path / "shims"
    release_build.mkdir()
    units.mkdir()
    shims.mkdir()

    _copy(REPO_ROOT / "pyproject.toml", release_build / "pyproject.toml")
    shutil.copytree(
        REPO_ROOT / "src/bureau",
        release_build / "src/bureau",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copytree(
        REPO_ROOT / "src/bureau_cycle",
        release_build / "src/bureau_cycle",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )

    for _stage, name, _module in STAGES:
        for suffix in (".service", ".timer"):
            canonical = REPO_ROOT / "ops" / "systemd" / f"{name}{suffix}"
            _copy(canonical, release_build / "ops" / "systemd" / f"{name}{suffix}")
            _copy(canonical, units / f"{name}{suffix}")
        canonical_shim = REPO_ROOT / "ops" / "systemd" / "libexec" / name
        _copy(canonical_shim, release_build / "ops" / "systemd" / "libexec" / name)
        _copy(canonical_shim, shims / name)

    package_tree_sha256 = _package_tree_sha256(release_build)
    assert package_tree_sha256 is not None
    source_commit = "a" * 40
    release_id = f"{source_commit[:12]}-src{package_tree_sha256[:12]}"
    release = tmp_path / release_id
    release_build.rename(release)

    manifest = tmp_path / "deployment-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "bureau_runtime_deployment",
                "source_commit": source_commit,
                "release_id": release_id,
                "immutable_release_path": str(release),
                "package_tree_sha256": package_tree_sha256,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return {
        "release": release,
        "units": units,
        "shims": shims,
        "manifest": manifest,
        "cycle_deployment_module": release / "src" / "bureau" / "cycle_deployment.py",
        "cycle_module": release / "src" / "bureau_cycle" / "__init__.py",
        "closure_module": release / "src" / "bureau" / "closure_runner.py",
    }


def _audit(fixture: dict[str, Path]) -> dict[str, object]:
    return audit_cycle_deployment(
        manifest_path=fixture["manifest"],
        unit_root=fixture["units"],
        shim_root=fixture["shims"],
        module_paths={
            "bureau.cycle_deployment": fixture["cycle_deployment_module"],
            "bureau_cycle": fixture["cycle_module"],
            "bureau.closure_runner": fixture["closure_module"],
        },
    )


def test_five_stages_and_source_ownership_are_in_canonical_release(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    result = _audit(fixture)

    assert result["status"] == "ok"
    assert result["activatable"] is False
    assert result["read_only"] is True
    assert result["self_heal"] is False
    assert result["compatibility_name"] == "bureau-halfhour-operator"
    assert result["release_identity"]["matches"] is True
    assert [item["name"] for item in result["stages"]] == [
        "discovery",
        "curator",
        "operator",
        "verifier",
        "closure",
    ]
    assert len(result["canonical_sources"]) == len(SOURCES)
    assert all(stage["service"]["matches"] for stage in result["stages"])
    assert all(stage["timer"]["matches"] for stage in result["stages"])
    assert all(stage["compatibility_shim"]["matches"] for stage in result["stages"])


def test_fresh_profile_state_contract_is_private_and_exact(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    result = _audit(fixture)

    for stage_result, (stage, name, _module) in zip(result["stages"], STAGES, strict=True):
        directories = STATE_DIRECTORIES[stage]
        read_write_paths = [f"%h/.local/state/{item}" for item in directories]
        assert stage_result["state_contract"] == {
            "directories": list(directories),
            "mode": "0700",
            "read_write_paths": read_write_paths,
        }
        service = (
            fixture["release"] / "ops" / "systemd" / f"{name}.service"
        ).read_text(encoding="utf-8")
        lines = service.splitlines()
        assert lines.count("StateDirectory=" + " ".join(directories)) == 1
        assert lines.count("StateDirectoryMode=0700") == 1
        assert lines.count("ReadWritePaths=" + " ".join(read_write_paths)) == 1
        assert "ProtectHome=read-only" in lines
        assert "NoNewPrivileges=yes" in lines


def test_missing_state_directory_contract_fails_closed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    service = fixture["release"] / "ops" / "systemd" / "bureau-curator.service"
    service.write_text(
        service.read_text(encoding="utf-8").replace(
            "StateDirectory=bureau-curator\n", ""
        ),
        encoding="utf-8",
    )

    with pytest.raises(CycleDeploymentError) as caught:
        _audit(fixture)
    assert caught.value.code == "canonical-unit-state-directory"


def test_broad_state_write_path_fails_closed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    service = fixture["release"] / "ops" / "systemd" / "bureau-operator-control.service"
    service.write_text(
        service.read_text(encoding="utf-8").replace(
            "ReadWritePaths=%h/.local/state/bureau-operator",
            "ReadWritePaths=%h/.local/state",
        ),
        encoding="utf-8",
    )

    with pytest.raises(CycleDeploymentError) as caught:
        _audit(fixture)
    assert caught.value.code == "canonical-unit-state-paths"


def test_release_package_tree_drift_is_reported_without_repair(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    target = fixture["release"] / "src" / "bureau_cycle" / "common.py"
    target.write_text(target.read_text(encoding="utf-8") + "# drift\n", encoding="utf-8")
    before = target.read_bytes()

    result = _audit(fixture)

    assert result["status"] == "drift"
    assert result["release_identity"]["matches"] is False
    assert any(item["code"] == "runtime-release-package-tree-drift" for item in result["findings"])
    assert target.read_bytes() == before


def test_services_and_shims_use_manifest_bound_launcher_without_mutable_sources(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    for stage, name, _module in STAGES:
        service = (fixture["release"] / "ops" / "systemd" / f"{name}.service").read_text(
            encoding="utf-8"
        )
        shim = (fixture["release"] / "ops" / "systemd" / "libexec" / name).read_text(
            encoding="utf-8"
        )
        assert f"ExecStart=%h/.local/bin/bureau cycle-run {stage}" in service
        assert f'exec "$HOME/.local/bin/bureau" cycle-run {stage} "$@"' in shim
        for forbidden in ("repos/bureau", "PYTHONPATH=", ".local/libexec/bureau_cycle"):
            assert forbidden not in service
            assert forbidden not in shim


def test_invalid_canonical_service_contract_fails_closed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    service = fixture["release"] / "ops" / "systemd" / "bureau-curator.service"
    text = service.read_text(encoding="utf-8")
    service.write_text(
        text.replace(
            "ExecStart=%h/.local/bin/bureau cycle-run curator",
            "ExecStart=/usr/bin/python3 -m bureau_cycle.curator_runner",
        ),
        encoding="utf-8",
    )

    with pytest.raises(CycleDeploymentError) as caught:
        _audit(fixture)
    assert caught.value.code == "canonical-unit-execstart"


def test_missing_canonical_service_hardening_fails_closed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    service = fixture["release"] / "ops" / "systemd" / "bureau-verifier-control.service"
    service.write_text(
        service.read_text(encoding="utf-8").replace("NoNewPrivileges=yes\n", ""),
        encoding="utf-8",
    )

    with pytest.raises(CycleDeploymentError) as caught:
        _audit(fixture)
    assert caught.value.code == "canonical-unit-hardening"


def test_closure_service_preserves_github_observation_network(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    closure = fixture["release"] / "ops" / "systemd" / "bureau-closure-planner.service"
    text = closure.read_text(encoding="utf-8")

    assert "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6" in text.splitlines()
    assert _audit(fixture)["status"] == "ok"

    closure.write_text(
        text.replace(
            "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
            "RestrictAddressFamilies=AF_UNIX",
        ),
        encoding="utf-8",
    )
    with pytest.raises(CycleDeploymentError) as caught:
        _audit(fixture)
    assert caught.value.code == "canonical-unit-hardening"


def test_canonical_mutable_source_token_fails_closed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    service = fixture["release"] / "ops" / "systemd" / "bureau-operator-control.service"
    service.write_text(
        service.read_text(encoding="utf-8") + "Environment=PYTHONPATH=%h/repos/bureau/src\n",
        encoding="utf-8",
    )

    with pytest.raises(CycleDeploymentError) as caught:
        _audit(fixture)
    assert caught.value.code == "canonical-unit-mutable-source"


def test_compatibility_shims_are_executable_and_mode_is_audited(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    for _stage, name, _module in STAGES:
        assert (fixture["release"] / "ops" / "systemd" / "libexec" / name).stat().st_mode & 0o111

    live = fixture["shims"] / "bureau-curator"
    live.chmod(0o644)
    result = _audit(fixture)

    assert result["status"] == "drift"
    assert any(item["code"] == "live-shim-not-executable" for item in result["findings"])


def test_non_executable_canonical_shim_fails_closed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    canonical = fixture["release"] / "ops" / "systemd" / "libexec" / "bureau-operator-control"
    canonical.chmod(0o644)

    with pytest.raises(CycleDeploymentError) as caught:
        _audit(fixture)
    assert caught.value.code == "canonical-shim-not-executable"


def test_drift_is_deterministic_and_never_self_healed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    target = fixture["units"] / "bureau-curator.service"
    target.write_text(
        target.read_text(encoding="utf-8") + "# drift\n",
        encoding="utf-8",
    )
    before = target.read_bytes()

    first = _audit(fixture)
    second = _audit(fixture)

    assert first == second
    assert first["status"] == "drift"
    assert any(item["code"] == "live-service-drift" for item in first["findings"])
    assert target.read_bytes() == before


def test_missing_live_file_is_reported_and_not_created(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    target = fixture["shims"] / "bureau-operator-control"
    target.unlink()

    result = _audit(fixture)

    assert any(item["code"] == "live-shim-missing" for item in result["findings"])
    assert not target.exists()


def test_symlink_live_file_fails_closed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    target = fixture["units"] / "bureau-verifier-control.service"
    target.unlink()
    target.symlink_to(fixture["release"] / "ops" / "systemd" / "bureau-verifier-control.service")

    with pytest.raises(CycleDeploymentError) as caught:
        _audit(fixture)
    assert caught.value.code == "file-symlink"


def test_root_and_internal_directory_symlinks_fail_closed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    unit_alias = tmp_path / "unit-alias"
    unit_alias.symlink_to(fixture["units"], target_is_directory=True)
    with pytest.raises(CycleDeploymentError) as caught:
        audit_cycle_deployment(
            manifest_path=fixture["manifest"],
            unit_root=unit_alias,
            shim_root=fixture["shims"],
            module_paths={
                "bureau_cycle": fixture["cycle_module"],
                "bureau.closure_runner": fixture["closure_module"],
            },
        )
    assert caught.value.code == "root-symlink"

    cycle_dir = fixture["release"] / "src" / "bureau_cycle"
    real_dir = fixture["release"] / "src" / "cycle-real"
    cycle_dir.rename(real_dir)
    cycle_dir.symlink_to(real_dir, target_is_directory=True)
    with pytest.raises(CycleDeploymentError) as caught:
        _audit(fixture)
    assert caught.value.code == "file-symlink"


def test_canonical_source_symlink_fails_closed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    target = fixture["release"] / "src" / "bureau_cycle" / "common.py"
    outside = tmp_path / "outside.py"
    outside.write_text("# outside\n", encoding="utf-8")
    target.unlink()
    target.symlink_to(outside)

    with pytest.raises(CycleDeploymentError) as caught:
        _audit(fixture)
    assert caught.value.code == "file-symlink"


def test_runtime_module_must_be_inside_immutable_release(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("# outside\n", encoding="utf-8")

    result = audit_cycle_deployment(
        manifest_path=fixture["manifest"],
        unit_root=fixture["units"],
        shim_root=fixture["shims"],
        module_paths={"bureau_cycle": outside},
    )

    assert result["status"] == "drift"
    assert any(item["code"] == "runtime-module-outside-release" for item in result["findings"])


def test_cli_exit_codes_and_deterministic_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = _fixture(tmp_path)
    monkeypatch.setattr(
        deployment,
        "_default_modules",
        lambda: {
            "bureau_cycle": fixture["cycle_module"],
            "bureau.closure_runner": fixture["closure_module"],
        },
    )
    args = [
        "--manifest",
        str(fixture["manifest"]),
        "--unit-root",
        str(fixture["units"]),
        "--shim-root",
        str(fixture["shims"]),
    ]

    assert deployment.main(args) == 0
    ok = json.loads(capsys.readouterr().out)
    assert ok["status"] == "ok"

    (fixture["shims"] / "bureau-curator").write_text("# drift\n", encoding="utf-8")
    assert deployment.main(args) == 1
    drift = json.loads(capsys.readouterr().out)
    assert drift["status"] == "drift"

    fixture["manifest"].write_text("not json", encoding="utf-8")
    assert deployment.main(args) == 2
    invalid = json.loads(capsys.readouterr().out)
    assert invalid["status"] == "invalid"


def test_package_contains_only_active_scheduler_sources() -> None:
    imported_names = {path.name for path in (REPO_ROOT / "src" / "bureau_cycle").glob("*.py")}
    assert imported_names == {
        "__init__.py",
        "common.py",
        "curator_runner.py",
        "cycle_contract.py",
        "discovery.py",
        "discovery_runner.py",
        "operator_runner.py",
        "verifier_runner.py",
    }
