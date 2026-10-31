from __future__ import annotations

from pathlib import Path

import pytest

from bureau_cycle import verifier_runner


def test_helper_uses_current_package_root() -> None:
    expected = Path(verifier_runner.__file__).resolve().parents[1]

    assert verifier_runner.HELPER_ENV["PYTHONPATH"] == str(expected)


def test_helper_runs_with_sanitized_environment(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setitem(verifier_runner.HELPER_ENV, "HOME", str(tmp_path))

    result = verifier_runner.helper(
        ["begin", "--stage", "verifier", "--trigger", "pytest"],
    )

    assert result["returncode"] == 0, result["stderr"]
    assert result["json"]["stage"] == "verifier"
    assert result["json"]["trigger"] == "pytest"
    assert result["json"]["lifecycle_state"] == "running"


def test_system_binary_prefers_nixos_profile_without_store_hash(tmp_path: Path) -> None:
    nixos = tmp_path / "nix-profile"
    legacy = tmp_path / "usr-bin"
    nixos.mkdir()
    legacy.mkdir()
    for root in (nixos, legacy):
        for name in ("git", "systemctl"):
            path = root / name
            path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            path.chmod(0o755)

    for name in ("git", "systemctl"):
        assert verifier_runner._system_binary(name, roots=(nixos, legacy)) == str(nixos / name)


def test_system_binary_falls_back_to_standard_linux(tmp_path: Path) -> None:
    nixos = tmp_path / "unavailable-nix-profile"
    legacy = tmp_path / "usr-bin"
    legacy.mkdir()
    program = legacy / "git"
    program.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    program.chmod(0o755)

    assert verifier_runner._system_binary("git", roots=(nixos, legacy)) == str(program)


@pytest.mark.parametrize("name", ["git", "systemctl"])
def test_system_binary_never_uses_untrusted_process_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str,
) -> None:
    poison = tmp_path / "poisoned-path"
    poison.mkdir()
    dummy = poison / name
    marker = tmp_path / "untrusted-executed"
    dummy.write_text(
        f"#!/bin/sh\nprintf executed > {marker}\n",
        encoding="utf-8",
    )
    dummy.chmod(0o755)
    monkeypatch.setenv("PATH", str(poison))

    with pytest.raises(FileNotFoundError, match=f"trusted system binary unavailable: {name}"):
        verifier_runner._system_binary(name, roots=(tmp_path / "absent-system-bin",))

    assert not marker.exists()


def test_service_hygiene_exempts_drkonqi_reporter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reporter = "drkonqi-coredump-launcher@365-4120-56721_56722-0.service"
    failed = [
        f"{reporter} loaded failed failed Launch DrKonqi for coredump",
        "grabowski-task-42-a1.service loaded failed failed One-off Grabowski job",
    ]
    monkeypatch.setattr(verifier_runner, "systemctl_lines", lambda args: failed)
    receipt: dict = {}
    verifier_runner.service_hygiene(receipt)
    assert receipt["metrics"]["systemd_failed_transient_unit_count"] == 2
    assert receipt["metrics"]["systemd_failed_non_transient_unit_count"] == 0
    assert receipt.get("findings", []) == []
    assert receipt["evidence"][0]["kind"] == "systemd_failed_units"
    assert receipt["evidence"][0]["non_transient_units"] == []


def test_service_hygiene_still_degrades_on_bureau_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reporter = "drkonqi-coredump-launcher@366-73739-68265_68266-0.service"
    failed = [
        f"{reporter} loaded failed failed Crashed-app reporter",
        "bureau-halfhour-operator.service loaded failed failed Productive scanner",
    ]
    monkeypatch.setattr(verifier_runner, "systemctl_lines", lambda args: failed)
    receipt: dict = {}
    verifier_runner.service_hygiene(receipt)
    assert receipt["metrics"]["systemd_failed_transient_unit_count"] == 1
    assert receipt["metrics"]["systemd_failed_non_transient_unit_count"] == 1
    findings = receipt.get("findings", [])
    assert len(findings) == 1
    assert findings[0]["kind"] == "non_transient_failed_units"
    assert findings[0]["units"] == ["bureau-halfhour-operator.service"]
