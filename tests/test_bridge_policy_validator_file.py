from pathlib import Path


def test_policy_validator_module_exists() -> None:
    assert Path("src/bureau/cabinet_bridge_import_policy.py").is_file()
