from __future__ import annotations

from pathlib import Path

from bureau.entrypoint_inventory import build_inventory

ROOT = Path(__file__).resolve().parents[1]


def test_inventory_lists_all_packaged_console_scripts() -> None:
    inventory = build_inventory(ROOT)
    names = {entry["name"] for entry in inventory["console_scripts"]}

    assert inventory["summary"]["packaged_console_scripts"] == 16
    assert "bureau" in names
    assert "bureau-agent-scout" in names
    assert "bureau-systemkatalog-frontier-reader" in names
    assert not any(name.startswith("bureau-cabinet-") for name in names)
    assert "bureau-gemini-preflight" in names
    assert "bureau-gemini-review-lane" in names


def test_inventory_covers_systemd_exec_start_commands() -> None:
    inventory = build_inventory(ROOT)
    units = {unit["unit"]: unit for unit in inventory["systemd_units"]}

    assert inventory["summary"]["systemd_services"] == 6
    assert inventory["summary"]["systemd_timers"] == 6
    assert (
        units["ops/systemd/bureau-status-projection.service"]["matched_console_script"]
        == "bureau"
    )
    assert units["ops/systemd/bureau-reconcile.service"]["matched_console_script"] == "bureau"
    assert (
        units["ops/systemd/bureau-source-pr-bridge.service"]["matched_console_script"]
        == "bureau-source-pr-bridge"
    )
    assert (
        units["ops/systemd/bureau-review-steward.service"]["matched_console_script"]
        == "bureau-review-steward"
    )
    assert units["ops/systemd/bureau-agent-frontier.service"]["command_basename"] == (
        "bureau-agent-frontier"
    )
    assert units["ops/systemd/bureau-codex-bridge.service"]["command_basename"] == (
        "bureau-codex-bridge"
    )


def test_inventory_records_hidden_or_module_entrypoints_without_promoting_them() -> None:
    inventory = build_inventory(ROOT)
    modules = {entry["module"] for entry in inventory["module_entrypoints"]}

    assert "bureau.cli" in modules
    assert "bureau.cycle_contract" in modules
    assert "bureau.discovery" in modules
    assert "safe_to_remove_entrypoints" in inventory["does_not_establish"]


def test_consolidation_plan_lists_current_console_scripts_and_units() -> None:
    inventory = build_inventory(ROOT)
    plan = (ROOT / "docs/bur-2026-004-t004-console-entrypoint-consolidation-plan.md").read_text(
        encoding="utf-8"
    )

    for entry in inventory["console_scripts"]:
        assert f"`{entry['name']}`" in plan
    for unit in inventory["systemd_units"]:
        assert f"`{Path(unit['unit']).name}`" in plan
    assert "T014-authorized Cabinet-to-Systemkatalog identity migration" in plan
    assert "No warning may be printed in `--json` mode" in plan


def test_system_catalog_commands_replace_retired_cabinet_aliases() -> None:
    from argparse import _SubParsersAction

    from bureau.cli import parser

    command_choices: set[str] = set()
    for action in parser()._actions:
        if isinstance(action, _SubParsersAction):
            command_choices.update(action.choices)

    expected = {
        "systemkatalog-graph",
        "systemkatalog-frontier",
        "systemkatalog-bridge-probe",
        "systemkatalog-promote",
        "systemkatalog-validate-task",
        "systemkatalog-import-preview",
        "systemkatalog-import-reviewed",
    }
    retired = {name.replace("systemkatalog-", "cabinet-", 1) for name in expected}

    assert expected <= command_choices
    assert retired.isdisjoint(command_choices)
