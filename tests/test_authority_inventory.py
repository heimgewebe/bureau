from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from bureau import legacy
from bureau.authority_inventory import (
    _installed_systemd_consumers,
    _systemd_probe,
    authority_inventory,
    main,
)
from bureau.cli import main as bureau_main


def _fixture_root(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    (root / "src/bureau").mkdir(parents=True)
    (root / "src/bureau_cycle").mkdir(parents=True)
    (root / "src/bureau_cycle/__init__.py").write_text("", encoding="utf-8")
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
    (root / "src/bureau/scanner.py").write_text(
        "MARKERS = ('git push', 'gh pr create', 'update_ref')\ndef scan():\n    return MARKERS\n",
        encoding="utf-8",
    )
    (root / "src/bureau/wrapped_transport.py").write_text(
        """
import subprocess
from bureau.v2 import Registry

def _gh(arguments):
    return subprocess.run(['gh', *arguments])

def _git(*arguments):
    return subprocess.run(['git', *arguments])

def publish(root):
    Registry.load(root)
    _git('add', 'registry/queue.json')
    _git('commit', '-m', 'queue update')
    _git('push', 'origin', 'HEAD')
    _gh(['pr', 'create', '--title', 'queue update'])
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (root / "src/bureau_cycle/operator.py").write_text(
        """
from pathlib import Path

def atomic_json(path, value):
    path.write_text('{}')

def run():
    atomic_json(Path.home() / '.local/state/bureau-cycle/latest.json', {})
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
    writer = next(item for item in first["consumers"] if item["path"].endswith("writer.py"))
    assert writer["writes"] == ["git_registry", "state_store"]
    assert writer["assumed_authorities"] == ["git_registry", "state_store"]
    assert writer["freshness_contract"] == (
        "source-revision-bound-static-detection;live-source-freshness-unobserved"
    )
    assert writer["target_authority"] == "state-store-api"
    assert writer["target_interface"] == "state-store-api"
    assert writer["migration_disposition"] == ("split-dual-writer-and-remove-operational-git-write")
    dashboard = next(
        item for item in first["consumers"] if item["path"] == "external:heim-pc-dashboard"
    )
    assert dashboard["freshness_contract"] == "bounded-dashboard-snapshot-readback"
    assert not any(item["path"] == "src/bureau_cycle/__init__.py" for item in first["consumers"])
    cycle = next(
        item for item in first["consumers"] if item["path"] == "src/bureau_cycle/operator.py"
    )
    assert cycle["reads"] == ["cycle_state"]
    assert cycle["writes"] == ["cycle_state"]
    assert cycle["target_authority"] == "state-store-api"
    assert cycle["migration_disposition"] == "migrate-cycle-side-state-to-state-store"
    assert first["summary"]["cycle_state_writer_count"] == 1
    scanner = next(item for item in first["consumers"] if item["path"] == "src/bureau/scanner.py")
    assert "github_transport" not in scanner["writes"]
    wrapped = next(
        item for item in first["consumers"] if item["path"] == "src/bureau/wrapped_transport.py"
    )
    assert wrapped["writes"] == ["git_registry", "github_transport"]
    assert wrapped["evidence"]["git_write_command"] is True
    assert wrapped["evidence"]["github_write_command"] is True
    assert wrapped["evidence"]["command_wrapper_kinds"] == ["gh", "git"]
    assert any(item["code"] == "dual-operational-writer" for item in first["findings"])
    assert any(
        item["code"] == "operational-side-state-writer-to-migrate"
        and item["path"] == "src/bureau_cycle/operator.py"
        for item in first["findings"]
    )
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


def test_canonical_cli_exposes_read_only_authority_inventory(tmp_path: Path, capsys) -> None:
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


def test_inventory_declares_runtime_installer_authority(tmp_path: Path) -> None:
    root, state = _fixture_root(tmp_path)
    installer = root / "ops/install-bureau-runtime.py"
    installer.parent.mkdir(parents=True, exist_ok=True)
    installer.write_text(
        "immutable_release_path = True\n"
        "approval_intent = True\n"
        "ensure_registry_snapshot = True\n"
        "marker = 'rev-parse\", \"origin/main\"'\n",
        encoding="utf-8",
    )

    value = authority_inventory(root, state_path=state, probe_systemd=False)

    consumer = next(
        item for item in value["consumers"] if item["path"] == "ops/install-bureau-runtime.py"
    )
    assert consumer["kind"] == "runtime-installer"
    assert consumer["target_authority"] == (
        "github-code-authority-and-immutable-runtime-deployment"
    )
    assert consumer["migration_disposition"] == "retain-as-explicit-deployment-authority"
    assert consumer["freshness_contract"] == (
        "source-head-origin-main-and-approval-intent-bound"
    )
    assert consumer["evidence"] == {
        "approval_intent_gate": True,
        "immutable_release_marker": True,
        "origin_main_gate": True,
        "registry_snapshot_writer": True,
    }
    assert value["complete"] is True


def test_inventory_fails_closed_for_incomplete_runtime_installer_contract(
    tmp_path: Path,
) -> None:
    root, state = _fixture_root(tmp_path)
    installer = root / "ops/install-bureau-runtime.py"
    installer.parent.mkdir(parents=True, exist_ok=True)
    installer.write_text("immutable_release_path = True\n", encoding="utf-8")

    value = authority_inventory(root, state_path=state, probe_systemd=False)

    assert value["complete"] is False
    assert value["status"] == "incomplete"
    finding = next(
        item
        for item in value["findings"]
        if item["code"] == "runtime-installer-contract-incomplete"
    )
    assert finding["path"] == "ops/install-bureau-runtime.py"
    assert finding["severity"] == "error"
    assert finding["detail"] == (
        "runtime installer lacks required authority markers: "
        "approval_intent_gate, origin_main_gate, registry_snapshot_writer"
    )


def test_systemd_probe_compares_source_user_and_system_units(
    tmp_path: Path, monkeypatch
) -> None:
    root, _state = _fixture_root(tmp_path)
    (root / "ops/systemd/bureau-missing.timer").write_text(
        "[Timer]\nOnCalendar=hourly\n", encoding="utf-8"
    )

    def fake_run(argv, **_kwargs):
        scope = "user" if "--user" in argv else "system"
        if "list-unit-files" in argv:
            stdout = (
                "bureau-bridge.service static -\n"
                "bureau-gpt-connector-probe.service static -\n"
                "bureau-gpt-probe.timer enabled -\n"
                if scope == "user"
                else "bureau-system-audit.service enabled -\n"
            )
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        names = [value for value in argv if value.startswith("bureau-")]
        blocks = []
        for name in names:
            exec_start = (
                "{ path=/home/test/.local/bin/bureau ; "
                "argv[]=/home/test/.local/bin/bureau status ; }"
                if name.endswith(".service")
                else ""
            )
            blocks.append(
                "\n".join(
                    [
                        f"Id={name}",
                        "LoadState=loaded" if "missing" not in name else "LoadState=not-found",
                        "ActiveState=inactive",
                        "SubState=dead",
                        "UnitFileState=enabled",
                        f"FragmentPath=/tmp/{scope}/{name}"
                        if "missing" not in name
                        else "FragmentPath=",
                        f"ExecStart={exec_start}",
                    ]
                )
            )
        return SimpleNamespace(returncode=0, stdout="\n\n".join(blocks) + "\n", stderr="")

    monkeypatch.setattr("bureau.authority_inventory.subprocess.run", fake_run)

    value = _systemd_probe(root, enabled=True)

    assert value["live_available"] is True
    assert value["declared_not_installed"] == ["bureau-missing.timer"]
    assert value["installed_not_declared"] == {
        "user": ["bureau-gpt-connector-probe.service", "bureau-gpt-probe.timer"],
        "system": ["bureau-system-audit.service"],
    }
    consumers = _installed_systemd_consumers(value)
    assert {item["path"] for item in consumers} == {
        "systemd:system:bureau-system-audit.service",
        "systemd:user:bureau-gpt-connector-probe.service",
        "systemd:user:bureau-gpt-probe.timer",
    }
    service = next(
        item
        for item in consumers
        if item["path"] == "systemd:user:bureau-gpt-connector-probe.service"
    )
    assert service["writes"] == ["delegated_cli"]
    assert service["freshness_contract"] == (
        "invocation-time-systemd-unit-file-and-state-bound"
    )


def test_systemd_list_unit_files_treats_empty_exit_one_as_no_matches(
    monkeypatch,
) -> None:
    def fake_run(_argv, **_kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr("bureau.authority_inventory.subprocess.run", fake_run)

    from bureau.authority_inventory import _systemd_list_unit_files

    units, error = _systemd_list_unit_files("system")

    assert units == []
    assert error is None
