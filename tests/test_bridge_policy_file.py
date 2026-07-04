from pathlib import Path


def test_bridge_policy_file_exists() -> None:
    assert Path("docs/cabinet-bridge-import-review-contract-v0.policy.json").is_file()
