from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from bureau import legacy
from bureau.authority_inventory import authority_inventory, main
from bureau.cli import main as bureau_main


def _fixture_root(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    (root / "src/bureau").mkdir(parents=True)
    (root / ".github/workflows").mkdir(parents=True)
    (root / "ops/systemd").mkdir(parents=True)

    (root / "src/bureau/writer.py").write_text(
        """
from pathlib import Path
from bureau.core import Registry, StateStore

def mutate(root, state):
    registry = Registry.load(root)
    store = StateStore(state, state.parent)
    (root / 'registry/queue.json').write_text('{}')
    return registry, store
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (root / "src/bureau/reader.py").write_text(
        """
from bureau.read_only_state import ReadOnlyStateStore

def read(state):
    return ReadOnlyStateStore(state, state.parent)
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (root / ".github/workflows/registry.yml").write_text(
        """
name: Bureau Registry transport
on: pull_request
permissions:
  contents: write
jobs:
  publish:
    steps:
      - run: git push origin HEAD
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (root / "ops/systemd/bureau-bridge.service").write_text(
        """
[Service]
ExecStart=/home/test/.local/bin/bureau source-pr-bridge
""".strip()
        + "\n",
        encoding="utf-8",
    )

    state = tmp_path / "bureau.sqlite3"
    connection = sqlite3.connect(state)
    connection.execute("PRAGMA user_version=3")
    for table in ("task_status", "runs", "events", "task_claims", "receipt_records"):
        connection.execute(f'CREATE TABLE "{table}" (id TEXT)')
        connection.execute(f'INSERT INTO "{table}" VALUES (?)', (table,))
    connection.commit()
    connection.close()
    return root, state


def test_inventory_is_read_only_hash_bound_and_classifies_dual_writers(
    tmp_path: Path,
) -> None:
    root, state = _fixture_root(tmp_path)
    before = state.read_bytes()

    first = authority_inventory(root, state_path=state, probe_systemd=False)
    second = authority_inventory(root, state_path=state, probe_systemd=False)

    assert first == second
    assert state.read_bytes() == before
    assert first["complete"] is True
    assert first["state_store"]["integrity"] == "ok"
    assert first["state_store"]["schema_version"] == 3
    assert set(first["state_store"]["table_counts"].values()) == {1}
    for consumer in first["consumers"]:
        assert consumer["assumed_authorities"] == sorted(
            set(consumer["reads"]).union(consumer["writes"])
        )
        assert consumer["freshness_contract"]
        assert consumer["target_interface"] == consumer["target_authority"]
    writer = next(
        item for item in first["consumers"] if item["path"].endswith("writer.py")
    )
    assert writer["writes"] == ["git_registry", "state_store"]
    assert writer["assumed_authorities"] == ["git_registry", "state_store"]
    assert writer["freshness_contract"] == (
        "source-revision-bound-static-detection;live-source-freshness-unobserved"
    )
    assert writer["target_authority"] == "state-store-api"
    assert writer["target_interface"] == "state-store-api"
    assert writer["migration_disposition"] == (
        "split-dual-writer-and-remove-operational-git-write"
    )
    dashboard = next(
        item
        for item in first["consumers"]
        if item["path"] == "external:heim-pc-dashboard"
    )
    assert dashboard["freshness_contract"] == "bounded-dashboard-snapshot-readback"
    assert any(item["code"] == "dual-operational-writer" for item in first["findings"])
    assert first["inventory_sha256"] == legacy.sha256_json(
        {key: value for key, value in first.items() if key != "inventory_sha256"}
    )


def test_inventory_marks_python_parse_failure_incomplete(tmp_path: Path) -> None:
    root, state = _fixture_root(tmp_path)
    (root / "src/bureau/broken.py").write_text("def broken(:\n", encoding="utf-8")

    value = authority_inventory(root, state_path=state, probe_systemd=False)

    assert value["complete"] is False
    assert value["status"] == "incomplete"
    assert value["summary"]["error_count"] == 1
    assert any(item["code"] == "python-scan-failed" for item in value["findings"])


def test_module_cli_emits_machine_readable_inventory(tmp_path: Path, capsys) -> None:
    root, state = _fixture_root(tmp_path)

    exit_code = main(
        [
            "--root",
            str(root),
            "--state-db",
            str(state),
            "--skip-systemd",
            "--json",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "bureau_authority_inventory"
    assert payload["read_only"] is True
    assert payload["complete"] is True
    assert payload["summary"]["consumer_count"] >= 6
    assert payload["does_not_establish"] == [
        "mutation_authority",
        "consumer_runtime_health_when_live_probe_is_unavailable",
        "cutover_readiness",
        "safe_removal_of_any_writer",
    ]


def test_canonical_cli_exposes_read_only_authority_inventory(
    tmp_path: Path, capsys
) -> None:
    root, state = _fixture_root(tmp_path)

    exit_code = bureau_main(
        [
            "--root",
            str(root),
            "--state-db",
            str(state),
            "--json",
            "authority-inventory",
            "--skip-systemd",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    inventory = payload.get("result", payload)
    assert inventory["kind"] == "bureau_authority_inventory"
    assert inventory["read_only"] is True
    assert inventory["complete"] is True
    assert inventory["state_store"]["path"] == str(state)
    assert inventory["systemd"]["enabled"] is False
