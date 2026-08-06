from __future__ import annotations

from pathlib import Path

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
