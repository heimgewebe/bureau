import os
from pathlib import Path

import pytest

from bureau import cli, runtime_identity, runtime_refresh, supply_runner

ROOT = Path(__file__).parents[1]
SERVICE = ROOT / "ops/systemd/bureau-task-supply.service"
TIMER = ROOT / "ops/systemd/bureau-task-supply.timer"
LIBEXEC = ROOT / "ops/systemd/libexec/bureau-task-supply"


def test_task_supply_service_uses_revision_bound_launcher_and_state_store_only() -> None:
    text = SERVICE.read_text(encoding="utf-8")
    exec_lines = [line for line in text.splitlines() if line.startswith("ExecStart=")]
    assert len(exec_lines) == 1
    command = exec_lines[0]
    assert command.startswith("ExecStart=%h/.local/bin/bureau task-supply-run ")
    for capability in ("bureau", "grabowski", "python", "repository", "testing"):
        assert f"--capability {capability}" in command
    assert "--controller-capability operator-approval" in command
    assert "--mutation-authority" in command
    assert "--publish" in command
    assert "--approval-available" not in command
    assert "repos/bureau" not in text
    assert "PYTHONPATH=" not in text
    assert "ProtectHome=read-only" in text
    assert (
        "ReadWritePaths=%h/.local/state/bureau-task-supply %h/.local/state/bureau"
        in text
    )
    assert "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6" in text.splitlines()


def test_task_supply_service_treats_blocked_as_successful_oneshot_only() -> None:
    text = SERVICE.read_text(encoding="utf-8")
    success_exit_status = [
        line for line in text.splitlines() if line.startswith("SuccessExitStatus=")
    ]
    assert supply_runner.BLOCKED_EXIT_STATUS != 2
    assert success_exit_status == [f"SuccessExitStatus={supply_runner.BLOCKED_EXIT_STATUS}"]


def test_task_supply_timer_runs_on_bounded_five_minute_cadence() -> None:
    text = TIMER.read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* *:00/5:30" in text
    assert "AccuracySec=1s" in text
    assert "RandomizedDelaySec=0" in text
    assert "Persistent=false" in text
    assert "Unit=bureau-task-supply.service" in text


def test_task_supply_libexec_is_only_a_stable_launcher_bridge() -> None:
    text = LIBEXEC.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh\nset -eu\n")
    assert 'exec "$HOME/.local/bin/bureau" task-supply-run' in text
    assert "--mutation-authority" in text
    assert "--publish" in text
    assert "--approval-available" not in text
    assert "repos/bureau" not in text
    assert "PYTHONPATH=" not in text


def test_task_supply_is_part_of_runtime_identity_and_refresh_contract() -> None:
    assert "bureau-task-supply" in runtime_identity.SCHEDULER_NAMES
    assert "bureau-task-supply" in runtime_refresh.RUNTIME_SCHEDULER_NAMES
    assert runtime_refresh.REQUIRED_RUNTIME_TIMER == "bureau-task-supply"
    scheduler_keys = runtime_refresh.scheduler_resource_keys(
        user_unit_dir=Path("/test/systemd/user"),
        libexec_dir=Path("/test/libexec"),
    )
    assert "path:/test/systemd/user" not in scheduler_keys
    assert "path:/test/libexec" not in scheduler_keys
    task_supply_paths = (
        Path("/test/systemd/user/bureau-task-supply.service"),
        Path("/test/systemd/user/bureau-task-supply.timer"),
        Path("/test/libexec/bureau-task-supply"),
    )
    for target in task_supply_paths:
        assert f"path:{target}" in scheduler_keys
        assert f"path:{runtime_refresh._scheduler_staging_path(target)}" in scheduler_keys
    assert "service:bureau-task-supply.service" in scheduler_keys
    assert "service:bureau-task-supply.timer" in scheduler_keys
    assert len([key for key in scheduler_keys if key.startswith("path:")]) == 36
    assert runtime_refresh.RUNTIME_LAUNCHER_ENTRYPOINTS == (
        ("bureau", "bureau.cli"),
        ("bureau-runtime-refresh", "bureau.runtime_refresh"),
        ("bureau-status-capsule", "bureau.status_capsule"),
    )


def test_scheduler_mutations_use_only_exact_reserved_staging_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_unit_dir = tmp_path / "systemd/user"
    libexec_dir = tmp_path / "libexec"
    runtime_user_unit_dir = tmp_path / "runtime-systemd/user"
    (user_unit_dir / "timers.target.wants").mkdir(parents=True)
    libexec_dir.mkdir()
    runtime_user_unit_dir.mkdir(parents=True)
    keys = set(
        runtime_refresh.scheduler_resource_keys(
            user_unit_dir=user_unit_dir,
            libexec_dir=libexec_dir,
            runtime_user_unit_dir=runtime_user_unit_dir,
        )
    )
    service = user_unit_dir / "bureau-task-supply.service"
    timer = user_unit_dir / "bureau-task-supply.timer"
    libexec = libexec_dir / "bureau-task-supply"
    wants = user_unit_dir / "timers.target.wants/bureau-task-supply.timer"
    replace_calls: list[tuple[Path, Path]] = []
    real_replace = runtime_refresh.os.replace

    def observed_replace(source: os.PathLike[str] | str, target: os.PathLike[str] | str) -> None:
        replace_calls.append((Path(source), Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(runtime_refresh.os, "replace", observed_replace)
    for target, mode in ((service, 0o644), (timer, 0o644), (libexec, 0o755)):
        runtime_refresh.atomic_write(
            target,
            b"candidate\n",
            mode,
            staging_path=runtime_refresh._scheduler_staging_path(target),
        )
    runtime_refresh._replace_with_symlink(wants, "../bureau-task-supply.timer")

    backup = tmp_path / "service.preimage"
    backup.write_bytes(b"preimage\n")
    runtime_refresh._restore_artifact(
        {
            "path": str(service),
            "preimage_kind": "file",
            "mode": "0644",
            "sha256": runtime_refresh.sha256_bytes(b"preimage\n"),
            "backup_path": str(backup),
        }
    )
    runtime_refresh._restore_artifact(
        {
            "path": str(wants),
            "preimage_kind": "symlink",
            "target": "../preimage.timer",
        }
    )

    scheduler_targets = {service, timer, libexec, wants}
    scheduler_replaces = [call for call in replace_calls if call[1] in scheduler_targets]
    assert scheduler_replaces
    mutated_paths = {path for call in scheduler_replaces for path in call}
    assert {f"path:{path}" for path in mutated_paths}.issubset(keys)
    assert all(
        source == runtime_refresh._scheduler_staging_path(target)
        for source, target in scheduler_replaces
    )
    assert all(".tmp-" not in path.name for path in mutated_paths)
    assert {
        f"path:{user_unit_dir}",
        f"path:{libexec_dir}",
        f"path:{runtime_user_unit_dir}",
        f"path:{user_unit_dir / 'timers.target.wants'}",
        f"path:{runtime_user_unit_dir / 'timers.target.wants'}",
    }.isdisjoint(keys)


def test_scheduler_symlink_fsync_failure_restores_with_same_reserved_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_unit_dir = tmp_path / "systemd/user"
    wants_parent = user_unit_dir / "timers.target.wants"
    wants_parent.mkdir(parents=True)
    libexec_dir = tmp_path / "libexec"
    libexec_dir.mkdir()
    runtime_user_unit_dir = tmp_path / "runtime-systemd/user"
    runtime_user_unit_dir.mkdir(parents=True)
    wants = wants_parent / "bureau-task-supply.timer"
    wants.symlink_to("../preimage.timer")
    stage = runtime_refresh._scheduler_staging_path(wants)
    keys = set(
        runtime_refresh.scheduler_resource_keys(
            user_unit_dir=user_unit_dir,
            libexec_dir=libexec_dir,
            runtime_user_unit_dir=runtime_user_unit_dir,
        )
    )
    assert {f"path:{wants}", f"path:{stage}"}.issubset(keys)

    real_fsync = runtime_refresh._fsync_directory
    fail_once = True

    def fail_after_replace(path: Path) -> None:
        nonlocal fail_once
        if path == wants_parent and fail_once:
            fail_once = False
            raise OSError("injected scheduler directory fsync failure after replace")
        real_fsync(path)

    monkeypatch.setattr(runtime_refresh, "_fsync_directory", fail_after_replace)
    with pytest.raises(OSError, match="fsync failure after replace"):
        runtime_refresh._replace_with_symlink(wants, "../candidate.timer")

    assert wants.is_symlink()
    assert os.readlink(wants) == "../candidate.timer"
    assert not os.path.lexists(stage)
    runtime_refresh._restore_artifact(
        {
            "path": str(wants),
            "preimage_kind": "symlink",
            "target": "../preimage.timer",
        }
    )
    assert os.readlink(wants) == "../preimage.timer"
    assert not os.path.lexists(stage)


def test_task_supply_cli_delegates_to_runner_with_canonical_registry(
    monkeypatch, tmp_path: Path
) -> None:
    canonical_root = tmp_path / "canonical-registry"
    canonical_root.mkdir()
    identity = {
        "module": {"source_kind": "immutable-release"},
        "manifest": {
            "valid": True,
            "canonical_registry": {"valid": True, "root": str(canonical_root)},
        },
        "registry": {"root": str(canonical_root), "bureau_project": False},
    }
    monkeypatch.setattr(
        cli,
        "resolve_registry_root",
        lambda configured: (canonical_root, "canonical-runtime-default"),
    )
    monkeypatch.setattr(
        cli,
        "bureau_runtime_identity",
        lambda root, state_path: dict(identity),
    )
    observed: dict[str, list[str]] = {}

    def fake_supply_main(argv):
        observed["argv"] = list(argv)
        return 0

    monkeypatch.setattr(supply_runner, "main", fake_supply_main)

    result = cli.main(
        [
            "task-supply-run",
            "--capability",
            "bureau",
            "--mutation-authority",
            "--publish",
            "--registry-root",
            "/tmp/caller-must-not-win",
        ]
    )

    assert result == 0
    assert observed["argv"][-2:] == ["--registry-root", str(canonical_root)]
    assert observed["argv"][:-2] == [
        "--capability",
        "bureau",
        "--mutation-authority",
        "--publish",
        "--registry-root",
        "/tmp/caller-must-not-win",
    ]