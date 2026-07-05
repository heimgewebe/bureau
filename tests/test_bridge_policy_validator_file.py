from __future__ import annotations

import json
from pathlib import Path

import pytest

from bureau.cabinet_bridge import CabinetBridgeError
from bureau.cabinet_bridge_import_policy import NON_EFFECTS, REQUIRED_INPUTS, validate_policy


def test_policy_validator_module_exists() -> None:
    assert Path("src/bureau/cabinet_bridge_import_policy.py").is_file()


def write_policy_copy(tmp_path: Path) -> Path:
    source = Path("docs/cabinet-bridge-import-review-contract-v0.policy.json")
    target = tmp_path / "policy.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    document = tmp_path / "contract.md"
    document.write_text("contract\n", encoding="utf-8")
    payload["document"] = str(document)
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def test_policy_validator_rejects_enabled_non_effect(tmp_path: Path) -> None:
    policy_path = write_policy_copy(tmp_path)
    payload = json.loads(policy_path.read_text(encoding="utf-8"))
    field = sorted(NON_EFFECTS)[0]
    payload["nonEffects"][field] = True
    policy_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CabinetBridgeError):
        validate_policy(policy_path)


def test_policy_validator_rejects_missing_required_input(tmp_path: Path) -> None:
    policy_path = write_policy_copy(tmp_path)
    payload = json.loads(policy_path.read_text(encoding="utf-8"))
    field = sorted(REQUIRED_INPUTS)[0]
    payload["requiredInputs"].remove(field)
    policy_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CabinetBridgeError):
        validate_policy(policy_path)
