from __future__ import annotations

import json
import shutil
from pathlib import Path

from bureau.systemkatalog_boundary import validate_boundary

ROOT = Path(__file__).resolve().parents[1]


def _copy_boundary_fixture(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for relative in (
        "src/bureau/cli.py",
        "src/bureau/systemkatalog_boundary.py",
        "pyproject.toml",
        "Makefile",
        "README.md",
        "docs/operations.md",
        "docs/cabinet-bridge-import-review-contract-v0.md",
        "docs/cabinet-bridge-import-review-contract-v0.policy.json",
        "docs/archive/cabinet-era/cabinet-bridge-import-review-contract-v0.md",
        "docs/archive/cabinet-era/cabinet-bridge-import-review-contract-v0.policy.json",
    ):
        source = ROOT / relative
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    (root / "ops/systemd").mkdir(parents=True, exist_ok=True)
    return root


def _kinds(report: dict) -> set[str]:
    return {item["kind"] for item in report["violations"]}


def test_current_repository_passes_static_systemkatalog_boundary() -> None:
    report = validate_boundary(ROOT)
    assert report["status"] == "pass", report
    assert report["checked"]["retired_modules"] == 8
    assert report["checked"]["retired_cli_commands"] == 7
    assert report["checked"]["retired_console_scripts"] == 5


def test_boundary_rejects_restored_retired_module(tmp_path: Path) -> None:
    root = _copy_boundary_fixture(tmp_path)
    module = root / "src/bureau/cabinet_graph.py"
    module.write_text("# restored\n", encoding="utf-8")
    assert "retired_module_present" in _kinds(validate_boundary(root))


def test_boundary_rejects_restored_cli_command(tmp_path: Path) -> None:
    root = _copy_boundary_fixture(tmp_path)
    cli = root / "src/bureau/cli.py"
    cli.write_text(
        cli.read_text(encoding="utf-8") + '\nRETIRED = "systemkatalog-promote"\n',
        encoding="utf-8",
    )
    assert "retired_cli_command_present" in _kinds(validate_boundary(root))


def test_boundary_rejects_restored_console_script(tmp_path: Path) -> None:
    root = _copy_boundary_fixture(tmp_path)
    pyproject = root / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    marker = "[tool.setuptools]"
    restored = (
        'bureau-systemkatalog-frontier-reader = '
        '"bureau.cabinet_frontier_reader:main"\n\n'
    )
    pyproject.write_text(text.replace(marker, restored + marker), encoding="utf-8")
    kinds = _kinds(validate_boundary(root))
    assert "retired_console_script_present" in kinds
    assert "retired_console_target_present" in kinds


def test_boundary_rejects_retired_command_in_systemd_unit(tmp_path: Path) -> None:
    root = _copy_boundary_fixture(tmp_path)
    service = root / "ops/systemd/retired.service"
    service.write_text(
        "[Service]\nExecStart=/usr/bin/bureau systemkatalog-import-reviewed\n",
        encoding="utf-8",
    )
    assert "retired_cli_reference_present" in _kinds(validate_boundary(root))


def test_boundary_rejects_stale_cabinet_default_path(tmp_path: Path) -> None:
    root = _copy_boundary_fixture(tmp_path)
    source = root / "src/bureau/legacy_reader.py"
    source.write_text(
        'PATH = "repos/cabinet/registry/ecosystem/bureau-bridge.json"\n',
        encoding="utf-8",
    )
    assert "retired_default_path_present" in _kinds(validate_boundary(root))


def test_boundary_rejects_archive_pointer_hash_drift(tmp_path: Path) -> None:
    root = _copy_boundary_fixture(tmp_path)
    policy = root / "docs/cabinet-bridge-import-review-contract-v0.policy.json"
    payload = json.loads(policy.read_text(encoding="utf-8"))
    payload["archive_sha256"] = "0" * 64
    policy.write_text(json.dumps(payload), encoding="utf-8")
    assert "policy_pointer_hash_mismatch" in _kinds(validate_boundary(root))
