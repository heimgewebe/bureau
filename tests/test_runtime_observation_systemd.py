"""Static safety checks for the runtime observation reference units.

systemd is a reference deployment, not a Bureau Core dependency. These tests
only read the unit files; they assert that the units stay one-shot, hardened
and free of dispatch, merge, completion, queue or cleanup mutations.
"""

from __future__ import annotations

from pathlib import Path

OPS = Path(__file__).parents[1] / "ops/systemd"

SERVICES = (
    OPS / "bureau-status-projection.service",
    OPS / "bureau-reconcile.service",
)
TIMERS = (
    OPS / "bureau-status-projection.timer",
    OPS / "bureau-reconcile.timer",
)

FORBIDDEN_SUBCOMMANDS = (
    "claim-next",
    "checkout-next",
    "complete",
    "fail",
    "handoff",
    "workspace-cleanup",
    "workspace-create",
    "systemkatalog-import-reviewed",
    "source-sync",
    "verification-stamp",
    "merge",
)

REQUIRED_HARDENING = (
    "Type=oneshot",
    "NoNewPrivileges=true",
    "PrivateTmp=true",
    "ProtectSystem=strict",
    "ProtectHome=read-only",
    "UMask=0077",
)

PRIVILEGED_ONLY_HARDENING = (
    "ProtectKernelTunables=true",
    "ProtectKernelModules=true",
    "ProtectControlGroups=true",
)


def test_reference_units_exist() -> None:
    for unit in (*SERVICES, *TIMERS):
        assert unit.is_file(), unit


def test_services_are_hardened_oneshots() -> None:
    for service in SERVICES:
        text = service.read_text(encoding="utf-8")
        for line in REQUIRED_HARDENING:
            assert line in text, f"{service.name} is missing {line}"
        for line in PRIVILEGED_ONLY_HARDENING:
            assert line not in text, f"{service.name} must not use {line} (user unit)"


def test_services_run_only_allowed_one_shot_commands() -> None:
    for service in SERVICES:
        text = service.read_text(encoding="utf-8")
        exec_lines = [
            line for line in text.splitlines() if line.startswith("ExecStart=")
        ]
        assert len(exec_lines) == 1, f"{service.name} must have exactly one ExecStart"
        exec_line = exec_lines[0]
        for forbidden in FORBIDDEN_SUBCOMMANDS:
            assert forbidden not in exec_line, (
                f"{service.name} must not run '{forbidden}'"
            )
        assert "ExecStartPre" not in text
        assert "ExecStopPost" not in text


def test_projection_service_is_read_only() -> None:
    text = (OPS / "bureau-status-projection.service").read_text(encoding="utf-8")
    assert "status-projection" in text
    assert "ReadWritePaths" not in text, "the projection unit must stay read-only"


def test_reconcile_service_is_bounded_to_the_state_root() -> None:
    text = (OPS / "bureau-reconcile.service").read_text(encoding="utf-8")
    assert " reconcile" in text
    assert "ReadWritePaths=%h/.local/state/bureau" in text
    assert "--stale-after" in text


def test_timers_reference_their_services_and_persist() -> None:
    for timer in TIMERS:
        text = timer.read_text(encoding="utf-8")
        assert "Persistent=true" in text
        assert "WantedBy=timers.target" in text
        assert f"Unit={timer.stem}.service" in text
