from pathlib import Path

SERVICE = Path(__file__).parents[1] / "ops/systemd/bureau-source-pr-bridge.service"


def test_user_service_avoids_privileged_capability_hardening():
    text = SERVICE.read_text(encoding="utf-8")
    for value in (
        "ProtectKernelTunables=true",
        "ProtectKernelModules=true",
        "ProtectControlGroups=true",
    ):
        assert value not in text
    for value in (
        "NoNewPrivileges=true",
        "PrivateTmp=true",
        "ProtectSystem=strict",
        "ProtectHome=read-only",
    ):
        assert value in text


def test_user_service_uses_manifest_bound_bureau_launcher():
    text = SERVICE.read_text(encoding="utf-8")
    exec_starts = [line for line in text.splitlines() if line.startswith("ExecStart=")]

    assert len(exec_starts) == 2
    assert all(
        line.startswith("ExecStart=%h/.local/bin/bureau source-pr-bridge")
        for line in exec_starts
    )
    assert "bureau-source-pr-bridge/venv" not in text
