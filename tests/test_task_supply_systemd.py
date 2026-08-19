from pathlib import Path

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
    assert "RestrictAddressFamilies=AF_UNIX" in text


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
    assert runtime_refresh.RUNTIME_LAUNCHER_ENTRYPOINTS == (
        ("bureau", "bureau.cli"),
        ("bureau-runtime-refresh", "bureau.runtime_refresh"),
        ("bureau-status-capsule", "bureau.status_capsule"),
    )


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
