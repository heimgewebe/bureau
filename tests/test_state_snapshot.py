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


@pytest.mark.parametrize("tracked_existing", [False, True])
def test_source_pr_bridge_transports_prebuilt_snapshot_bytes_unchanged(
    tmp_path: Path, registry_factory, monkeypatch, tracked_existing: bool
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
    if tracked_existing:
        tracked_snapshot = registry / state_snapshot.PUBLIC_SNAPSHOT_PATH
        tracked_snapshot.parent.mkdir(parents=True, exist_ok=True)
        tracked_snapshot.write_text("{}\n", encoding="utf-8")
    origin, checkout = _git_checkout(tmp_path, registry)
    push_origin = tmp_path / "snapshot-push-origin.git"
    subprocess.run(
        ["git", "init", "--bare", str(push_origin)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    _run_git(checkout, "remote", "set-url", "--push", "origin", str(push_origin))

    def fake_json(arguments, *, allow_not_found=False):
        assert arguments == [
            "api",
            f"repos/{source_pr_bridge.DEFAULT_REPOSITORY}/git/ref/heads/"
            f"{source_pr_bridge.STATE_SNAPSHOT_BRANCH}",
        ]
        assert allow_not_found is True
        return None

    original_git = source_pr_bridge._git

    def forbid_shared_worktree_admin(repo, *arguments):
        assert arguments[:1] != ("worktree",)
        return original_git(repo, *arguments)

    monkeypatch.setattr(source_pr_bridge, "_git", forbid_shared_worktree_admin)
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
        push_origin, "rev-parse", f"refs/heads/{source_pr_bridge.STATE_SNAPSHOT_BRANCH}"
    )
    assert (
        subprocess.run(
            [
                "git",
                "-C",
                str(origin),
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/heads/{source_pr_bridge.STATE_SNAPSHOT_BRANCH}",
            ],
            check=False,
            timeout=30,
        ).returncode
        == 1
    )
    observed = subprocess.run(
        [
            "git",
            "-C",
            str(push_origin),
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


def test_snapshot_publisher_skips_identical_branch_until_main_changes(
    tmp_path: Path, registry_factory, monkeypatch
) -> None:
    registry = registry_factory(task_count=2)
    state_root, _store = _state_store(tmp_path, registry)
    manifest = _runtime_manifest(tmp_path, registry)
    snapshot = tmp_path / "public-state.json"
    state_snapshot.export_public_snapshot(
        output=snapshot,
        state_root=state_root,
        runtime_manifest=manifest,
    )
    expected = snapshot.read_bytes()
    origin, checkout = _git_checkout(tmp_path, registry)
    branch = source_pr_bridge.STATE_SNAPSHOT_BRANCH
    repo = source_pr_bridge.DEFAULT_REPOSITORY
    ref_args = ["api", f"repos/{repo}/git/ref/heads/{branch}"]

    def branch_absent(args, *, allow_not_found=False):
        assert args == ref_args and allow_not_found is True
        return None

    monkeypatch.setattr(source_pr_bridge, "_json", branch_absent)
    first = source_pr_bridge.publish_state_snapshot(checkout, snapshot=snapshot)
    assert first["status"] == "published"
    old_head = _run_git(origin, "rev-parse", f"refs/heads/{branch}")
    first_base = _run_git(origin, "rev-parse", "refs/heads/main")
    old_content = subprocess.run(
        ["git", "-C", str(origin), "show", f"{old_head}:{state_snapshot.PUBLIC_SNAPSHOT_PATH}"],
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout
    assert old_content == expected
    calls: list[list[str]] = []

    def remote_branch(args, *, allow_not_found=False):
        calls.append(args)
        if args == ref_args:
            assert allow_not_found is True
            return {"object": {"sha": old_head}}
        if args == ["api", f"repos/{repo}/commits/{old_head}"]:
            return {
                "sha": old_head,
                "parents": [{"sha": first_base}],
                "files": [
                    {
                        "filename": state_snapshot.PUBLIC_SNAPSHOT_PATH.as_posix(),
                        "status": "modified",
                    },
                ],
            }
        if args == [
            "api",
            f"repos/{repo}/contents/{state_snapshot.PUBLIC_SNAPSHOT_PATH}",
            "--method",
            "GET",
            "-f",
            f"ref={branch}",
        ]:
            return {"encoding": "base64", "content": base64.b64encode(expected).decode()}
        raise AssertionError(f"unexpected GitHub access: {args!r}")

    monkeypatch.setattr(source_pr_bridge, "_json", remote_branch)
    same = source_pr_bridge.publish_state_snapshot(checkout, snapshot=snapshot)
    assert same["status"] == "not-applied"
    assert same["reason"] == "snapshot-already-current"
    assert same["head_sha"] == old_head
    assert same["snapshot_sha256"] == first["snapshot_sha256"]
    assert _run_git(origin, "rev-parse", f"refs/heads/{branch}") == old_head
    assert calls.count(ref_args) == 2  # preimage and unchanged-head readback
    assert len(calls) == 4  # plus exact commit parent and public bytes
    assert _run_git(checkout, "status", "--porcelain") == ""

    # Identical redacted bytes are not an excuse to leave the review branch
    # based on an obsolete main revision after a protected release.
    _run_git(checkout, "config", "user.name", "Bureau Snapshot Test")
    _run_git(checkout, "config", "user.email", "snapshot-test@example.invalid")
    (checkout / "new-main.txt").write_text("new base revision\n", encoding="utf-8")
    _run_git(checkout, "add", "--", "new-main.txt")
    _run_git(checkout, "commit", "-m", "new protected main for snapshot test")
    _run_git(checkout, "push", "origin", "main")
    new_base = _run_git(origin, "rev-parse", "refs/heads/main")
    assert new_base != first_base
    calls.clear()
    refreshed = source_pr_bridge.publish_state_snapshot(checkout, snapshot=snapshot)
    assert refreshed["status"] == "published"
    assert refreshed["base_sha"] == new_base
    new_head = _run_git(origin, "rev-parse", f"refs/heads/{branch}")
    assert new_head != old_head
    assert _run_git(origin, "rev-parse", f"{new_head}^") == new_base
    assert snapshot.read_bytes() == expected
    assert calls == [ref_args, ["api", f"repos/{repo}/commits/{old_head}"]]
    assert _run_git(checkout, "status", "--porcelain") == ""


def test_snapshot_publisher_rebuilds_branch_with_extra_file_even_if_bytes_match(
    tmp_path: Path, registry_factory, monkeypatch
) -> None:
    """No-op requires the COMPLETE remote commit diff to contain only the snapshot."""
    registry = registry_factory(task_count=2)
    state_root, _store = _state_store(tmp_path, registry)
    manifest = _runtime_manifest(tmp_path, registry)
    snapshot = tmp_path / "public-state.json"
    state_snapshot.export_public_snapshot(
        output=snapshot,
        state_root=state_root,
        runtime_manifest=manifest,
    )
    expected = snapshot.read_bytes()
    origin, checkout = _git_checkout(tmp_path, registry)
    branch = source_pr_bridge.STATE_SNAPSHOT_BRANCH
    repo = source_pr_bridge.DEFAULT_REPOSITORY
    ref_args = ["api", f"repos/{repo}/git/ref/heads/{branch}"]

    def no_remote_branch(args, *, allow_not_found=False):
        assert args == ref_args and allow_not_found is True
        return None

    monkeypatch.setattr(source_pr_bridge, "_json", no_remote_branch)
    initial = source_pr_bridge.publish_state_snapshot(checkout, snapshot=snapshot)
    assert initial["status"] == "published"
    old_head = _run_git(origin, "rev-parse", f"refs/heads/{branch}")
    base_sha = _run_git(origin, "rev-parse", "refs/heads/main")

    # Build a real adversarial branch commit with the SAME public snapshot
    # and one forbidden extra file, directly parented to the current main.
    tampered = tmp_path / "tampered"
    subprocess.run(
        ["git", "clone", "--shared", str(origin), str(tampered)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    _run_git(tampered, "config", "user.name", "Bureau Adversarial Test")
    _run_git(tampered, "config", "user.email", "branch-tamper@example.invalid")
    _run_git(tampered, "checkout", "-B", "tampered", base_sha)
    public = tampered / state_snapshot.PUBLIC_SNAPSHOT_PATH
    public.parent.mkdir(parents=True, exist_ok=True)
    public.write_bytes(expected)
    (tampered / "unexpected.txt").write_text("not part of public state\n", encoding="utf-8")
    _run_git(
        tampered,
        "add",
        "--",
        state_snapshot.PUBLIC_SNAPSHOT_PATH.as_posix(),
        "unexpected.txt",
    )
    _run_git(tampered, "commit", "-m", "adversarial snapshot plus extra file")
    adversarial_head = _run_git(tampered, "rev-parse", "HEAD")
    assert _run_git(tampered, "rev-parse", "HEAD^") == base_sha
    # A bare Git origin cannot update a ref to an object it has not received.
    _run_git(origin, "fetch", str(tampered), adversarial_head)
    _run_git(origin, "update-ref", f"refs/heads/{branch}", adversarial_head, old_head)

    def actual_remote(args, *, allow_not_found=False):
        if args == ref_args:
            assert allow_not_found is True
            return {"object": {"sha": adversarial_head}}
        if args == ["api", f"repos/{repo}/commits/{adversarial_head}"]:
            return {
                "sha": adversarial_head,
                "parents": [{"sha": base_sha}],
                "files": [
                    {
                        "filename": state_snapshot.PUBLIC_SNAPSHOT_PATH.as_posix(),
                        "status": "modified",
                    },
                    {"filename": "unexpected.txt", "status": "added"},
                ],
            }
        if args == [
            "api",
            f"repos/{repo}/contents/{state_snapshot.PUBLIC_SNAPSHOT_PATH}",
            "--method",
            "GET",
            "-f",
            f"ref={branch}",
        ]:
            return {"encoding": "base64", "content": base64.b64encode(expected).decode()}
        raise AssertionError(f"unexpected GitHub access: {args!r}")

    monkeypatch.setattr(source_pr_bridge, "_json", actual_remote)
    rebuilt = source_pr_bridge.publish_state_snapshot(checkout, snapshot=snapshot)
    assert rebuilt["status"] == "published"
    fixed_head = _run_git(origin, "rev-parse", f"refs/heads/{branch}")
    assert fixed_head != adversarial_head
    assert _run_git(origin, "rev-parse", f"{fixed_head}^") == base_sha
    changed_paths = _run_git(
        origin, "diff-tree", "--no-commit-id", "--name-only", "-r", fixed_head
    ).splitlines()
    assert changed_paths == [state_snapshot.PUBLIC_SNAPSHOT_PATH.as_posix()]
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
    observed_calls: list[list[str]] = []

    def fake_json(arguments, *, allow_not_found=False):
        observed_calls.append(arguments)
        return next(responses)

    monkeypatch.setattr(source_pr_bridge, "_json", fake_json)

    result = source_pr_bridge.reconcile(
        branch=source_pr_bridge.STATE_SNAPSHOT_BRANCH,
        kind="state-snapshot",
        snapshot=snapshot_path,
    )
    assert result["status"] == "no_change"
    assert observed_calls[1] == [
        "api",
        f"repos/{source_pr_bridge.DEFAULT_REPOSITORY}/contents/"
        f"{state_snapshot.PUBLIC_SNAPSHOT_PATH.as_posix()}",
        "--method",
        "GET",
        "-f",
        f"ref={source_pr_bridge.STATE_SNAPSHOT_BRANCH}",
    ]


def test_checked_in_public_snapshot_is_valid_if_present() -> None:
    path = Path(__file__).parents[1] / state_snapshot.PUBLIC_SNAPSHOT_PATH
    if not path.exists():
        pytest.skip("no public snapshot is checked in on this revision")
    assert (
        state_snapshot.validate_public_snapshot(state_snapshot.read_public_snapshot(path))["status"]
        == "valid"
    )


def test_state_snapshot_already_current_reconciles_instead_of_closing_review_pr(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """An idempotent publisher must not withdraw an open redacted-state PR."""
    snapshot = tmp_path / "public-state.json"
    snapshot.write_text("publisher mock holds snapshot bytes", encoding="utf-8")
    calls: list[str] = []

    def fake_publish(root, *, snapshot, repository, base, branch):
        assert root == tmp_path.resolve()
        calls.append("publish")
        return {
            "status": "not-applied",
            "reason": "snapshot-already-current",
            "head_sha": "a" * 40,
        }

    def fake_reconcile(repository, base, branch, *, kind, auto_merge, snapshot):
        assert kind == "state-snapshot"
        assert snapshot == tmp_path / "public-state.json"
        calls.append("reconcile")
        return {"status": "no_change", "pull_request": 2388, "branch": branch}

    def forbidden_withdraw(*args, **kwargs):
        pytest.fail("identical snapshot must never close its existing review PR")

    monkeypatch.setattr(source_pr_bridge, "publish_state_snapshot", fake_publish)
    monkeypatch.setattr(source_pr_bridge, "reconcile", fake_reconcile)
    monkeypatch.setattr(source_pr_bridge, "withdraw_open_proposal", forbidden_withdraw)
    assert (
        source_pr_bridge.main(
            [
                "--kind",
                "state-snapshot",
                "--publish",
                "--root",
                str(tmp_path),
                "--snapshot",
                str(snapshot),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "no_change"
    assert result["pull_request"] == 2388
    assert result["publish"]["reason"] == "snapshot-already-current"
    assert calls == ["publish", "reconcile"]


def test_now_refill_not_applied_still_withdraws_stale_proposal(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The existing Now-refill no-demand withdrawal contract must remain intact."""
    calls: list[str] = []

    def fake_now_refill(*args, **kwargs):
        calls.append("publish")
        return {"status": "not-applied", "reason": "no-refill-needed"}

    def fake_withdraw(repository, base, branch):
        calls.append("withdraw")
        return {"status": "withdrawn", "pull_request": 1234}

    def forbidden_reconcile(*args, **kwargs):
        pytest.fail("now-refill no-demand must still withdraw its stale proposal")

    monkeypatch.setattr(source_pr_bridge, "publish_now_refill", fake_now_refill)
    monkeypatch.setattr(source_pr_bridge, "withdraw_open_proposal", fake_withdraw)
    monkeypatch.setattr(source_pr_bridge, "reconcile", forbidden_reconcile)
    assert (
        source_pr_bridge.main(
            [
                "--kind",
                "now-refill",
                "--publish",
                "--root",
                str(tmp_path),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "withdrawn"
    assert calls == ["publish", "withdraw"]


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
    assert "ConditionFileIsExecutable=%h/.local/bin/bureau" in service
    assert service.count("ExecStart=") == 1
    assert "source-pr-bridge --kind state-snapshot --publish" in service
    assert "--auto-merge" not in service
    assert "--state-root %h/.local/state/bureau" in service
    assert "--runtime-manifest %h/.local/share/bureau/deployment-manifest.json" in service
    assert "bureau-state-snapshot export" not in service
    assert "--snapshot " not in service
    assert "ReadOnlyPaths=%h/.local/state/bureau %h/.local/share/bureau" in service
    assert "ReadWritePaths=%h/repos/bureau" in service
    assert "ProtectHome=read-only" in service
    assert "StateDirectory=bureau-state-snapshot" not in service
    assert "OnCalendar=*-*-* *:0/15:00" in timer
    assert "Unit=bureau-state-snapshot.service" in timer
