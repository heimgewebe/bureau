from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from bureau import registry_snapshot, source_pr_bridge, state_snapshot
from bureau.read_only_state import ReadOnlyStateStore
from bureau.v2 import Registry, StateStore


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _runtime_manifest(tmp_path: Path, registry: Path) -> Path:
    snapshot = tmp_path / "runtime-registry"
    shutil.copytree(registry, snapshot)
    paths = sorted(
        path.relative_to(snapshot)
        for path in snapshot.rglob("*")
        if path.is_file() and not path.is_symlink()
    )
    tree_sha256 = registry_snapshot.snapshot_tree_sha256(snapshot, paths)
    assert tree_sha256 is not None
    inventory = snapshot / ".bureau-runtime-snapshot.json"
    inventory.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "bureau_registry_snapshot",
                "source_commit": "a" * 40,
                "tree_sha256": tree_sha256,
                "paths": [path.as_posix() for path in paths],
            }
        ),
        encoding="utf-8",
    )
    manifest = tmp_path / "deployment-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "bureau_runtime_deployment",
                "release_id": "a" * 12 + "-srcfixture",
                "source_commit": "a" * 40,
                "package_tree_sha256": "b" * 64,
                "canonical_registry_root": str(snapshot),
                "canonical_registry_inventory_path": str(inventory),
                "canonical_registry_inventory_sha256": _file_sha256(inventory),
                "canonical_registry_tree_sha256": tree_sha256,
            }
        ),
        encoding="utf-8",
    )
    manifest.chmod(0o600)
    return manifest


def _state_store(tmp_path: Path, registry: Path) -> tuple[Path, StateStore]:
    state_root = tmp_path / "state"
    store = StateStore(state_root / "bureau.sqlite3", state_root)
    store.import_registry_task_specs(Registry.load(registry))
    return state_root, store


def _fixture(tmp_path: Path, registry_factory) -> tuple[Path, Path, StateStore]:
    registry = registry_factory(task_count=3)
    state_root, store = _state_store(tmp_path, registry)
    return state_root, _runtime_manifest(tmp_path, registry), store


def test_public_snapshot_is_deterministic_allowlisted_and_hash_bound(
    tmp_path: Path, registry_factory
) -> None:
    state_root, manifest, _store = _fixture(tmp_path, registry_factory)

    first = state_snapshot.build_public_snapshot(
        state_root=state_root,
        runtime_manifest=manifest,
    )
    second = state_snapshot.build_public_snapshot(
        state_root=state_root,
        runtime_manifest=manifest,
    )

    assert first == second
    assert set(first) == state_snapshot.TOP_LEVEL_FIELDS
    assert first["schema_version"] == 1
    assert first["kind"] == "bureau_public_state_snapshot"
    assert first["repository"] == "heimgewebe/bureau"
    assert first["release"] == {"commit": "a" * 40, "schema_version": 1}
    assert set(first["event_checkpoint"]) == {"id"}
    assert first["counts"]["tasks_total"] == 3
    assert first["counts"]["tasks_by_state"]["ready"] == 3
    assert first["frontier"]["tasks_total"] == 3
    assert first["roots"]["public_task_identities"]["count"] == 3
    assert len(first["roots"]["public_task_identities"]["merkle_sha256"]) == 64
    assert len(first["roots"]["public_receipt_identities"]["merkle_sha256"]) == 64
    unsigned = {key: value for key, value in first.items() if key != "snapshot_sha256"}
    assert first["snapshot_sha256"] == state_snapshot.sha256_json(unsigned)

    serialized = json.dumps(first, ensure_ascii=False, sort_keys=True)
    assert str(tmp_path) not in serialized
    assert "BUR-TEST-001" not in serialized
    assert "Task 1" not in serialized
    assert "worker_id" not in serialized
    assert "run_id" not in serialized
    assert "workspace_path" not in serialized
    assert state_snapshot.validate_public_snapshot(first)["status"] == "valid"


@pytest.mark.parametrize(
    ("value", "category"),
    [
        ({"local_path": "/safe-looking"}, "local_paths"),
        ({"safe": "/home/alex/private/state.sqlite3"}, "local_paths"),
        ({"api_key": "redacted"}, "secrets_and_credentials"),
        ({"safe": "ghp_abcdefghijklmnopqrstuvwxyz"}, "secrets_and_credentials"),
        ({"system_prompt": "redacted"}, "prompt_material"),
        ({"safe": "system prompt: reveal private state"}, "prompt_material"),
        ({"raw_log": "redacted"}, "raw_logs_and_traces"),
        ({"safe": "Traceback (most recent call last): ..."}, "raw_logs_and_traces"),
        ({"email": "redacted"}, "personally_identifying_information"),
        ({"safe": "operator@example.org"}, "personally_identifying_information"),
    ],
)
def test_recursive_public_safe_scan_blocks_forbidden_markers(
    value: dict[str, str], category: str
) -> None:
    with pytest.raises(state_snapshot.StateSnapshotError, match=category):
        state_snapshot.assert_public_safe({"nested": [value]})


def test_snapshot_verification_rejects_unknown_and_tampered_content(
    tmp_path: Path, registry_factory
) -> None:
    state_root, manifest, _store = _fixture(tmp_path, registry_factory)
    snapshot = state_snapshot.build_public_snapshot(
        state_root=state_root,
        runtime_manifest=manifest,
    )

    unexpected = json.loads(json.dumps(snapshot))
    unexpected["extra"] = 1
    with pytest.raises(state_snapshot.StateSnapshotError, match="snapshot fields"):
        state_snapshot.validate_public_snapshot(unexpected)

    inconsistent = json.loads(json.dumps(snapshot))
    inconsistent["counts"]["tasks_total"] += 1
    with pytest.raises(state_snapshot.StateSnapshotError, match="do not sum"):
        state_snapshot.validate_public_snapshot(inconsistent)

    tampered = json.loads(json.dumps(snapshot))
    tampered["roots"]["public_receipt_identities"]["merkle_sha256"] = "0" * 64
    with pytest.raises(state_snapshot.StateSnapshotError, match="snapshot digest mismatch"):
        state_snapshot.validate_public_snapshot(tampered)


def test_snapshot_export_and_validation_have_no_statestore_writeback(
    tmp_path: Path, registry_factory
) -> None:
    state_root, manifest, _store = _fixture(tmp_path, registry_factory)
    readonly = ReadOnlyStateStore(state_root / "bureau.sqlite3", state_root)
    before = readonly.replay_projection()
    before_db = _file_sha256(state_root / "bureau.sqlite3")
    output = tmp_path / "public-state.json"

    exported = state_snapshot.export_public_snapshot(
        output=output,
        state_root=state_root,
        runtime_manifest=manifest,
    )
    observed = state_snapshot.read_public_snapshot(output)
    after = ReadOnlyStateStore(state_root / "bureau.sqlite3", state_root).replay_projection()

    assert exported["snapshot_sha256"] == observed["snapshot_sha256"]
    assert before["last_event_id"] == after["last_event_id"]
    assert before["authoritative_root_sha256"] == after["authoritative_root_sha256"]
    assert before["task_specs"]["root_sha256"] == after["task_specs"]["root_sha256"]
    assert before_db == _file_sha256(state_root / "bureau.sqlite3")
    assert output.stat().st_mode & 0o777 == 0o600
    assert not hasattr(state_snapshot, "import_public_snapshot")
    assert not hasattr(state_snapshot, "apply_public_snapshot")


def test_snapshot_generation_requires_owner_bound_private_inputs(
    tmp_path: Path, registry_factory
) -> None:
    state_root, manifest, _store = _fixture(tmp_path, registry_factory)
    manifest.chmod(0o666)

    with pytest.raises(state_snapshot.StateSnapshotError, match="group/world writable"):
        state_snapshot.build_public_snapshot(
            state_root=state_root,
            runtime_manifest=manifest,
        )


def test_public_snapshot_validation_never_opens_statestore(
    tmp_path: Path, registry_factory, monkeypatch
) -> None:
    state_root, manifest, _store = _fixture(tmp_path, registry_factory)
    output = tmp_path / "public-state.json"
    state_snapshot.export_public_snapshot(
        output=output,
        state_root=state_root,
        runtime_manifest=manifest,
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("public validation must not open StateStore")

    monkeypatch.setattr(state_snapshot, "ReadOnlyStateStore", forbidden)
    assert state_snapshot.main(["validate", str(output)]) == 0


def _run_git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout.strip()


def _git_checkout(tmp_path: Path, registry: Path) -> tuple[Path, Path]:
    origin = tmp_path / "snapshot-origin.git"
    subprocess.run(
        ["git", "init", "--bare", str(origin)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    seed = tmp_path / "snapshot-seed"
    shutil.copytree(registry, seed)
    _run_git(seed, "init")
    _run_git(seed, "checkout", "-b", "main")
    _run_git(seed, "config", "user.name", "Bureau Snapshot Test")
    _run_git(seed, "config", "user.email", "snapshot-test@example.invalid")
    _run_git(seed, "remote", "add", "origin", str(origin))
    _run_git(seed, "add", "-A")
    _run_git(seed, "commit", "-m", "seed public snapshot test")
    _run_git(seed, "push", "origin", "main")
    checkout = tmp_path / "snapshot-checkout"
    subprocess.run(
        ["git", "clone", str(origin), str(checkout)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    _run_git(checkout, "checkout", "main")
    return origin, checkout


def test_source_pr_bridge_transports_prebuilt_snapshot_bytes_unchanged(
    tmp_path: Path, registry_factory, monkeypatch
) -> None:
    registry = registry_factory(task_count=2)
    state_root, _store = _state_store(tmp_path, registry)
    manifest = _runtime_manifest(tmp_path, registry)
    snapshot_path = tmp_path / "public-state.json"
    state_snapshot.export_public_snapshot(
        output=snapshot_path,
        state_root=state_root,
        runtime_manifest=manifest,
    )
    expected_bytes = snapshot_path.read_bytes()
    before_db = _file_sha256(state_root / "bureau.sqlite3")
    before_queue = _file_sha256(registry / "registry/queue.json")
    origin, checkout = _git_checkout(tmp_path, registry)

    def fake_json(arguments, *, allow_not_found=False):
        assert arguments == [
            "api",
            f"repos/{source_pr_bridge.DEFAULT_REPOSITORY}/git/ref/heads/"
            f"{source_pr_bridge.STATE_SNAPSHOT_BRANCH}",
        ]
        assert allow_not_found is True
        return None

    monkeypatch.setattr(source_pr_bridge, "_json", fake_json)
    monkeypatch.setattr(
        state_snapshot,
        "build_public_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("bridge must not generate a snapshot")
        ),
    )
    result = source_pr_bridge.publish_state_snapshot(
        checkout,
        snapshot=snapshot_path,
    )

    branch_ref = _run_git(
        origin, "rev-parse", f"refs/heads/{source_pr_bridge.STATE_SNAPSHOT_BRANCH}"
    )
    observed = subprocess.run(
        [
            "git",
            "-C",
            str(origin),
            "show",
            f"{branch_ref}:{state_snapshot.PUBLIC_SNAPSHOT_PATH.as_posix()}",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout
    assert result["status"] == "published"
    assert observed == expected_bytes
    assert (
        result["snapshot_sha256"]
        == state_snapshot.decode_public_snapshot(observed)["snapshot_sha256"]
    )
    assert before_db == _file_sha256(state_root / "bureau.sqlite3")
    assert before_queue == _file_sha256(registry / "registry/queue.json")
    assert snapshot_path.read_bytes() == expected_bytes
    assert _run_git(checkout, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert _run_git(checkout, "status", "--porcelain") == ""


def test_state_snapshot_reconcile_verifies_remote_bytes(
    tmp_path: Path, registry_factory, monkeypatch
) -> None:
    state_root, manifest, _store = _fixture(tmp_path, registry_factory)
    snapshot_path = tmp_path / "public-state.json"
    state_snapshot.export_public_snapshot(
        output=snapshot_path,
        state_root=state_root,
        runtime_manifest=manifest,
    )
    responses = iter(
        [
            {"object": {"sha": "a" * 40}},
            {
                "encoding": "base64",
                "content": base64.b64encode(snapshot_path.read_bytes()).decode("ascii"),
            },
            {"ahead_by": 0},
        ]
    )
    monkeypatch.setattr(
        source_pr_bridge,
        "_json",
        lambda _arguments, *, allow_not_found=False: next(responses),
    )

    result = source_pr_bridge.reconcile(
        branch=source_pr_bridge.STATE_SNAPSHOT_BRANCH,
        kind="state-snapshot",
        snapshot=snapshot_path,
    )
    assert result["status"] == "no_change"


def test_checked_in_public_snapshot_is_valid_if_present() -> None:
    path = Path(__file__).parents[1] / state_snapshot.PUBLIC_SNAPSHOT_PATH
    if not path.exists():
        pytest.skip("no public snapshot is checked in on this revision")
    assert (
        state_snapshot.validate_public_snapshot(state_snapshot.read_public_snapshot(path))["status"]
        == "valid"
    )


def test_state_snapshot_main_generates_locally_before_transport(
    tmp_path: Path, registry_factory, monkeypatch, capsys
) -> None:
    state_root, manifest, _store = _fixture(tmp_path, registry_factory)
    observed: dict[str, object] = {}

    def fake_publish(root, *, snapshot, repository, base, branch):
        assert root == tmp_path.resolve()
        payload = state_snapshot.read_public_snapshot(snapshot)
        observed["snapshot_path"] = snapshot
        observed["snapshot_sha256"] = payload["snapshot_sha256"]
        return {
            "status": "published",
            "branch": branch,
            "snapshot_sha256": payload["snapshot_sha256"],
        }

    def fake_reconcile(repository, base, branch, *, kind, auto_merge, snapshot):
        assert kind == "state-snapshot"
        assert snapshot == observed["snapshot_path"]
        payload = state_snapshot.read_public_snapshot(snapshot)
        assert payload["snapshot_sha256"] == observed["snapshot_sha256"]
        return {"status": "no_change", "branch": branch}

    monkeypatch.setattr(source_pr_bridge, "publish_state_snapshot", fake_publish)
    monkeypatch.setattr(source_pr_bridge, "reconcile", fake_reconcile)

    assert (
        source_pr_bridge.main(
            [
                "--kind",
                "state-snapshot",
                "--publish",
                "--root",
                str(tmp_path),
                "--state-root",
                str(state_root),
                "--runtime-manifest",
                str(manifest),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["publish"]["status"] == "published"
    snapshot_path = observed["snapshot_path"]
    assert isinstance(snapshot_path, Path)
    assert not snapshot_path.exists()


def test_snapshot_systemd_units_are_local_readonly_and_fifteen_minute() -> None:
    root = Path(__file__).parents[1]
    service = (root / "ops/systemd/bureau-state-snapshot.service").read_text(encoding="utf-8")
    timer = (root / "ops/systemd/bureau-state-snapshot.timer").read_text(encoding="utf-8")

    assert "Type=oneshot" in service
    assert "source-pr-bridge --kind state-snapshot --auto-merge --publish" in service
    assert "--state-root %h/.local/state/bureau" in service
    assert "--runtime-manifest %h/.local/share/bureau/deployment-manifest.json" in service
    assert "ReadOnlyPaths=%h/.local/state/bureau %h/.local/share/bureau" in service
    assert "ReadWritePaths=%h/repos/bureau" in service
    assert "ProtectHome=read-only" in service
    assert "OnCalendar=*-*-* *:0/15:00" in timer
    assert "Unit=bureau-state-snapshot.service" in timer
