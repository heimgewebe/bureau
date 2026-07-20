from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bureau.status_capsule import (
    CapsuleError,
    _atomic_json,
    _compact_repo_balls,
    _safe_extract,
    _seal,
    failure_path,
    main,
    read_capsule,
    write_capsule,
)
from bureau.v2 import StateStore

NOW = datetime(2026, 7, 12, 10, 0, tzinfo=timezone.utc)


def git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout.strip()


def make_git_registry(root: Path) -> str:
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Bureau Test")
    git(root, "config", "user.email", "bureau@example.invalid")
    git(root, "add", "registry", "schemas")
    git(root, "commit", "-m", "test: registry")
    head = git(root, "rev-parse", "HEAD")
    git(root, "update-ref", "refs/remotes/origin/main", head)
    return head


def setup_capsule_sources(registry_factory, tmp_path: Path) -> tuple[Path, Path, Path, str]:
    root = registry_factory(2, mode="write")
    head = make_git_registry(root)
    state_root = tmp_path / "runtime-state"
    StateStore(state_root / "bureau.sqlite3", state_root)
    output = tmp_path / "readonly" / "status-capsule.json"
    return root, state_root, output, head


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted((root / "registry").rglob("*")):
        if not path.is_file():
            continue
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()



def test_safe_extract_uses_explicit_filter_and_extracts_regular_entries(
    tmp_path, monkeypatch
):
    archive = tmp_path / "registry.tar"
    payload = b"{}\n"
    with tarfile.open(archive, "w") as handle:
        directory = tarfile.TarInfo("registry/")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        handle.addfile(directory)
        regular = tarfile.TarInfo("registry/queue.json")
        regular.mode = 0o644
        regular.size = len(payload)
        handle.addfile(regular, io.BytesIO(payload))

    observed = {}
    original_extractall = tarfile.TarFile.extractall

    def capture_extractall(
        self, path=".", members=None, *, numeric_owner=False, filter=None
    ):
        observed["filter"] = filter
        return original_extractall(
            self,
            path,
            members=members,
            numeric_owner=numeric_owner,
            filter=filter,
        )

    monkeypatch.setattr(tarfile.TarFile, "extractall", capture_extractall)
    destination = tmp_path / "extract"
    destination.mkdir()

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        _safe_extract(archive, destination)

    assert observed["filter"] == "fully_trusted"
    assert (destination / "registry").is_dir()
    assert (destination / "registry/queue.json").read_bytes() == payload


@pytest.mark.parametrize(
    ("member_name", "entry_type", "linkname", "expected_error"),
    [
        ("../escape", tarfile.REGTYPE, "", "unsafe path"),
        ("registry/symlink", tarfile.SYMTYPE, "queue.json", "unsupported link"),
        (
            "registry/hardlink",
            tarfile.LNKTYPE,
            "registry/queue.json",
            "unsupported link",
        ),
        ("registry/fifo", tarfile.FIFOTYPE, "", "unsupported entry"),
    ],
)
def test_safe_extract_refuses_traversal_links_and_special_entries(
    tmp_path, member_name, entry_type, linkname, expected_error
):
    archive = tmp_path / "unsafe.tar"
    member = tarfile.TarInfo(member_name)
    member.type = entry_type
    member.linkname = linkname
    payload = b"unsafe" if entry_type == tarfile.REGTYPE else None
    if payload is not None:
        member.size = len(payload)

    with tarfile.open(archive, "w") as handle:
        handle.addfile(member, io.BytesIO(payload) if payload is not None else None)

    destination = tmp_path / "extract"
    destination.mkdir()
    with pytest.raises(CapsuleError, match=expected_error):
        _safe_extract(archive, destination)

    assert not (tmp_path / "escape").exists()


def test_atomic_write_resolves_parent_alias_before_publication(tmp_path, monkeypatch):
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    alias = tmp_path / "parent-alias"
    alias.symlink_to(real_parent, target_is_directory=True)
    observed: dict[str, Path] = {}

    from bureau import status_capsule as module

    def capture(path: Path, _content: str) -> None:
        observed["path"] = path

    monkeypatch.setattr(module.legacy, "atomic_write", capture)

    _atomic_json(alias / "capsule.json", _seal({
        "schema_version": 1,
        "kind": "bureau-status-capsule",
    }))

    assert observed["path"] == real_parent.resolve() / "capsule.json"

def test_write_and_read_capsule_exposes_required_truth(
    registry_factory, tmp_path
):
    root, state_root, output, head = setup_capsule_sources(registry_factory, tmp_path)

    result = write_capsule(
        root,
        canonical_repo=root,
        state_root=state_root,
        output=output,
        freshness_seconds=900,
        now=NOW,
    )
    read = read_capsule(output, now=NOW + timedelta(seconds=30))

    assert result["written"] is True
    assert read["status"] == "fresh"
    snapshot = read["snapshot"]
    assert snapshot["registry"]["source"] == "local-origin-main-archive"
    assert snapshot["registry"]["git_head"] == head
    assert snapshot["registry"]["root"] == str(root.resolve())
    assert snapshot["registry"]["source_scope"] == "local-origin-main-without-fetch"
    assert snapshot["registry"]["observed_ref"] == "origin/main"
    assert snapshot["registry"]["observed_ref_head"] == head
    assert snapshot["registry"]["remote_freshness"] == "not-observed"
    assert "canonical_current" not in snapshot["registry"]
    assert snapshot["collector"]["distribution"] == "heimgewebe-bureau"
    assert snapshot["collector"]["module"] == "bureau.status_capsule"
    from bureau import status_capsule as status_capsule_module

    expected_collector_sha = hashlib.sha256(
        Path(status_capsule_module.__file__).read_bytes()
    ).hexdigest()
    assert snapshot["collector"]["module_sha256"] == expected_collector_sha
    assert snapshot["collector"]["identity_scope"] == "running-module-bytes"
    assert snapshot["registry"]["registry_sha256"]
    assert snapshot["state_store"]["integrity"] == "ok"
    assert snapshot["state_store"]["foreign_key_errors"] == []
    assert snapshot["runs"]["active_count"] == 0
    assert snapshot["leases"]["active_count"] == 0
    assert snapshot["repo_balls"]["summary"]["repositories"] == 2
    assert snapshot["doctor"]["healthy"] is True
    assert snapshot["doctor"]["migration_applied_to_copy"] is False
    assert (
        snapshot["doctor"]["source_schema_version"]
        == snapshot["doctor"]["database"]["schema_version"]
    )
    assert snapshot["registry_truth"]["healthy"] is True
    assert snapshot["created_at"] == "2026-07-12T10:00:00Z"
    assert snapshot["freshness"]["threshold_seconds"] == 900
    assert snapshot["observation_scope"] == {
        "registry": "local-origin-main-without-fetch",
        "state_store": "consistent-read-only-sqlite-backup",
        "github": "not-observed",
        "grabowski": "not-required",
        "network": "not-used",
    }
    assert "shell_authority" in snapshot["does_not_establish"]
    assert "snapshot_authenticity_or_signature" in snapshot["does_not_establish"]
    assert "remote_origin_freshness" in snapshot["does_not_establish"]
    assert snapshot["last_successful_snapshot"] is None
    assert output.stat().st_mode & 0o777 == 0o600


def test_reader_remains_available_without_registry_database_or_grabowski(
    registry_factory, tmp_path
):
    root, state_root, output, _head = setup_capsule_sources(registry_factory, tmp_path)
    write_capsule(
        root,
        canonical_repo=root,
        state_root=state_root,
        output=output,
        now=NOW,
    )

    shutil.rmtree(root)
    shutil.rmtree(state_root)
    read = read_capsule(output, now=NOW + timedelta(seconds=60))

    assert read["status"] == "fresh"
    assert read["snapshot"]["state_store"]["integrity"] == "ok"
    assert read["snapshot"]["registry"]["git_head"]


def test_reader_does_not_call_git_or_sqlite(
    registry_factory, tmp_path, monkeypatch
):
    root, state_root, output, _head = setup_capsule_sources(registry_factory, tmp_path)
    write_capsule(
        root,
        canonical_repo=root,
        state_root=state_root,
        output=output,
        now=NOW,
    )

    from bureau import status_capsule as module

    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("reader must not call git")
        ),
    )
    monkeypatch.setattr(
        module.sqlite3,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("reader must not open SQLite")
        ),
    )

    read = read_capsule(output, now=NOW + timedelta(seconds=1))

    assert read["status"] == "fresh"



def test_reader_runs_without_third_party_site_packages(
    registry_factory, tmp_path
):
    root, state_root, output, _head = setup_capsule_sources(registry_factory, tmp_path)
    write_capsule(
        root,
        canonical_repo=root,
        state_root=state_root,
        output=output,
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-S",
            "-m",
            "bureau.status_capsule",
            "read",
            "--path",
            str(output),
        ],
        cwd=Path(__file__).parents[1],
        env={
            "HOME": str(tmp_path / "absent-home"),
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "fresh"

def test_reader_reports_stale_by_age(registry_factory, tmp_path):
    root, state_root, output, _head = setup_capsule_sources(registry_factory, tmp_path)
    write_capsule(
        root,
        canonical_repo=root,
        state_root=state_root,
        output=output,
        freshness_seconds=120,
        now=NOW,
    )

    read = read_capsule(output, now=NOW + timedelta(seconds=121))

    assert read["status"] == "stale"
    assert read["age_seconds"] == 121
    assert read["reasons"] == ["snapshot age exceeds freshness threshold"]


def test_missing_or_tampered_capsule_is_unavailable(registry_factory, tmp_path):
    missing = read_capsule(tmp_path / "missing.json", now=NOW)
    assert missing["status"] == "unavailable"

    root, state_root, output, _head = setup_capsule_sources(registry_factory, tmp_path)
    write_capsule(
        root,
        canonical_repo=root,
        state_root=state_root,
        output=output,
        now=NOW,
    )
    raw = json.loads(output.read_text())
    raw["doctor"]["healthy"] = not raw["doctor"]["healthy"]
    output.write_text(json.dumps(raw))

    tampered = read_capsule(output, now=NOW)
    assert tampered["status"] == "unavailable"
    assert tampered["reasons"] == ["bureau-status-capsule content hash mismatch"]


def test_invalid_failure_sidecar_marks_valid_snapshot_stale(
    registry_factory, tmp_path
):
    root, state_root, output, _head = setup_capsule_sources(registry_factory, tmp_path)
    write_capsule(
        root,
        canonical_repo=root,
        state_root=state_root,
        output=output,
        now=NOW,
    )
    failure_path(output).write_text("{}")

    read = read_capsule(output, now=NOW + timedelta(seconds=1))

    assert read["status"] == "stale"
    assert "refresh failure evidence is unreadable" in read["reasons"]
    assert read["last_refresh_failure"]["status"] == "unreadable"


def test_hash_valid_invalid_freshness_is_unavailable(registry_factory, tmp_path):
    root, state_root, output, _head = setup_capsule_sources(registry_factory, tmp_path)
    write_capsule(
        root,
        canonical_repo=root,
        state_root=state_root,
        output=output,
        now=NOW,
    )
    current = json.loads(output.read_text())
    body = {key: value for key, value in current.items() if key != "content_sha256"}
    body["freshness"]["threshold_seconds"] = "invalid"
    output.write_text(json.dumps(_seal(body)))

    read = read_capsule(output, now=NOW)

    assert read["status"] == "unavailable"
    assert read["reasons"] == ["snapshot freshness threshold is invalid"]


def test_future_snapshot_is_stale(registry_factory, tmp_path):
    root, state_root, output, _head = setup_capsule_sources(registry_factory, tmp_path)
    write_capsule(
        root,
        canonical_repo=root,
        state_root=state_root,
        output=output,
        now=NOW + timedelta(seconds=10),
    )

    read = read_capsule(output, now=NOW)

    assert read["status"] == "stale"
    assert read["reasons"] == ["snapshot timestamp is in the future"]


def test_repo_ball_payloads_are_bounded():
    balls = {
        "repo.test": {
            "resource": "repo.test",
            "status": "blocked",
            "current_ball": {
                "kind": "queued_task",
                "task_id": "TEST-T001",
                "title": "x" * 3000,
                "ignored": "secret expansion",
            },
            "active_runs": [{"run_id": str(index)} for index in range(120)],
            "findings": [{"message": "y" * 3000} for _ in range(30)],
            "task_ids": ["TEST-T001"],
        }
    }

    compact = _compact_repo_balls(balls)["repo.test"]

    assert compact["current_ball"]["title"].endswith("…<truncated>")
    assert "ignored" not in compact["current_ball"]
    assert compact["active_runs"]["count"] == 120
    assert compact["active_runs"]["truncated"] is True
    assert len(compact["active_runs"]["items"]) == 100
    assert compact["findings"]["count"] == 30
    assert compact["findings"]["truncated"] is True
    assert len(compact["findings"]["items"]) == 20
    assert compact["findings"]["items"][0]["message"].endswith("…<truncated>")


def test_failed_refresh_preserves_last_success_and_marks_stale(
    registry_factory, tmp_path
):
    root, state_root, output, head = setup_capsule_sources(registry_factory, tmp_path)
    write_capsule(
        root,
        canonical_repo=root,
        state_root=state_root,
        output=output,
        now=NOW,
    )
    before = output.read_bytes()
    shutil.rmtree(root)

    with pytest.raises(CapsuleError, match=r"fatal:|git command failed|not a git repository"):
        write_capsule(
            root,
            canonical_repo=root,
            state_root=state_root,
            output=output,
            now=NOW + timedelta(seconds=30),
        )

    assert output.read_bytes() == before
    assert failure_path(output).is_file()
    read = read_capsule(output, now=NOW + timedelta(seconds=31))
    assert read["status"] == "stale"
    assert "refresh failed after the last successful snapshot" in read["reasons"]
    assert read["last_successful_snapshot"]["registry_head"] == head
    assert read["last_refresh_failure"]["attempted_at"] == "2026-07-12T10:00:30Z"


def test_canonical_archive_ignores_dirty_main_checkout(registry_factory, tmp_path):
    root, state_root, output, head = setup_capsule_sources(registry_factory, tmp_path)
    task = next((root / "registry/tasks").glob("*.json"))
    task.write_text(task.read_text() + "\n")
    assert git(root, "status", "--porcelain")

    write_capsule(
        root,
        canonical_repo=root,
        state_root=state_root,
        output=output,
        now=NOW,
    )
    read = read_capsule(output, now=NOW)

    assert read["status"] == "fresh"
    assert read["snapshot"]["registry"]["git_head"] == head
    assert read["snapshot"]["registry"]["observed_ref"] == "origin/main"
    assert read["snapshot"]["registry"]["remote_freshness"] == "not-observed"



def test_writer_disables_repository_fsmonitor_hook(registry_factory, tmp_path):
    root, state_root, output, _head = setup_capsule_sources(registry_factory, tmp_path)
    sentinel = tmp_path / "fsmonitor-ran"
    hook = tmp_path / "fsmonitor-hook"
    hook.write_text(
        "#!/bin/sh\nprintf ran > " + str(sentinel) + "\nprintf '0\n'\n",
        encoding="utf-8",
    )
    hook.chmod(0o700)
    git(root, "config", "core.fsmonitor", str(hook))

    written = write_capsule(
        root,
        state_root=state_root,
        output=output,
        now=NOW,
    )

    assert written["written"] is True
    assert not sentinel.exists()

def test_clean_root_mode_refuses_dirty_registry(registry_factory, tmp_path):
    root, state_root, output, _head = setup_capsule_sources(registry_factory, tmp_path)
    (root / "registry/queue.json").write_text("{}")

    with pytest.raises(CapsuleError, match="registry root is dirty"):
        write_capsule(root, state_root=state_root, output=output, now=NOW)

    assert not output.exists()


def test_output_inside_source_or_state_is_refused_without_side_effect(
    registry_factory, tmp_path
):
    root, state_root, _output, _head = setup_capsule_sources(registry_factory, tmp_path)
    registry_before = tree_digest(root)
    db = state_root / "bureau.sqlite3"
    database_before = db.read_bytes()
    inside_registry = root / "registry/status-capsule.json"
    inside_state = state_root / "status-capsule.json"

    with pytest.raises(CapsuleError, match="outside registry and state"):
        write_capsule(
            root,
            canonical_repo=root,
            state_root=state_root,
            output=inside_registry,
            now=NOW,
        )
    with pytest.raises(CapsuleError, match="outside registry and state"):
        write_capsule(
            root,
            canonical_repo=root,
            state_root=state_root,
            output=inside_state,
            now=NOW,
        )

    assert not inside_registry.exists()
    assert not failure_path(inside_registry).exists()
    assert not inside_state.exists()
    assert not failure_path(inside_state).exists()
    assert tree_digest(root) == registry_before
    assert db.read_bytes() == database_before




def test_explicit_state_root_is_used_for_markers_and_output_boundary(
    registry_factory, tmp_path
):
    root = registry_factory(2, mode="write")
    make_git_registry(root)
    declared_state_root = tmp_path / "declared-state"
    store = StateStore(declared_state_root / "bureau.sqlite3", declared_state_root)
    outside = tmp_path / "readonly" / "capsule.json"

    written = write_capsule(
        root,
        canonical_repo=root,
        state_db=store.path,
        state_root=declared_state_root,
        output=outside,
        now=NOW,
    )

    assert written["written"] is True
    forbidden = declared_state_root / "forbidden.json"
    with pytest.raises(CapsuleError, match="outside registry and state source roots"):
        write_capsule(
            root,
            canonical_repo=root,
            state_db=store.path,
            state_root=declared_state_root,
            output=forbidden,
            now=NOW,
        )
    assert not forbidden.exists()
    assert not failure_path(forbidden).exists()


def test_inconsistent_state_db_and_root_are_rejected_without_side_effect(
    registry_factory, tmp_path
):
    root = registry_factory(2, mode="write")
    make_git_registry(root)
    database_root = tmp_path / "database"
    store = StateStore(database_root / "bureau.sqlite3", database_root)
    declared_state_root = tmp_path / "different-state-root"
    output = tmp_path / "readonly" / "capsule.json"

    with pytest.raises(CapsuleError, match="directly inside state_root"):
        write_capsule(
            root,
            canonical_repo=root,
            state_db=store.path,
            state_root=declared_state_root,
            output=output,
            now=NOW,
        )

    assert not output.exists()
    assert not failure_path(output).exists()

def test_successful_collection_does_not_mutate_registry_or_state(
    registry_factory, tmp_path
):
    root, state_root, output, _head = setup_capsule_sources(registry_factory, tmp_path)
    db = state_root / "bureau.sqlite3"
    registry_before = tree_digest(root)
    database_before = db.read_bytes()
    database_stat_before = db.stat()
    git_status_before = git(root, "status", "--porcelain=v1", "--untracked-files=all")

    write_capsule(
        root,
        canonical_repo=root,
        state_root=state_root,
        output=output,
        now=NOW,
    )

    assert tree_digest(root) == registry_before
    assert db.read_bytes() == database_before
    database_stat_after = db.stat()
    assert database_stat_after.st_mtime_ns == database_stat_before.st_mtime_ns
    assert git(root, "status", "--porcelain=v1", "--untracked-files=all") == git_status_before


def test_size_limit_preserves_previous_snapshot(
    registry_factory, tmp_path, monkeypatch
):
    root, state_root, output, _head = setup_capsule_sources(registry_factory, tmp_path)
    write_capsule(
        root,
        canonical_repo=root,
        state_root=state_root,
        output=output,
        now=NOW,
    )
    before = output.read_bytes()

    from bureau import status_capsule as module

    monkeypatch.setattr(module, "MAX_CAPSULE_BYTES", 100)
    with pytest.raises(CapsuleError, match="bounded size limit"):
        write_capsule(
            root,
            canonical_repo=root,
            state_root=state_root,
            output=output,
            now=NOW + timedelta(seconds=10),
        )

    assert output.read_bytes() == before
    read = read_capsule(output, now=NOW + timedelta(seconds=11))
    assert read["status"] == "stale"
    assert "refresh failed after the last successful snapshot" in read["reasons"]


def test_existing_output_parent_permissions_are_not_changed(
    registry_factory, tmp_path
):
    root, state_root, _output, _head = setup_capsule_sources(registry_factory, tmp_path)
    parent = tmp_path / "shared-output"
    parent.mkdir(mode=0o755)
    output = parent / "capsule.json"
    mode_before = parent.stat().st_mode

    write_capsule(
        root,
        canonical_repo=root,
        state_root=state_root,
        output=output,
        now=NOW,
    )

    assert parent.stat().st_mode == mode_before
    assert output.is_file()


def test_second_snapshot_links_previous_success(registry_factory, tmp_path):
    root, state_root, output, head = setup_capsule_sources(registry_factory, tmp_path)
    first = write_capsule(
        root,
        canonical_repo=root,
        state_root=state_root,
        output=output,
        now=NOW,
    )
    write_capsule(
        root,
        canonical_repo=root,
        state_root=state_root,
        output=output,
        now=NOW + timedelta(seconds=30),
    )

    read = read_capsule(output, now=NOW + timedelta(seconds=31))
    previous = read["snapshot"]["last_successful_snapshot"]
    assert previous == {
        "created_at": "2026-07-12T10:00:00Z",
        "registry_head": head,
        "content_sha256": first["content_sha256"],
    }


def test_cli_exit_codes_and_independent_read(registry_factory, tmp_path, capsys):
    root, state_root, output, _head = setup_capsule_sources(registry_factory, tmp_path)
    assert (
        main(
            [
                "write",
                "--canonical-repo",
                str(root),
                "--state-root",
                str(state_root),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    write_output = json.loads(capsys.readouterr().out)
    assert write_output["written"] is True

    assert main(["read", "--path", str(output)]) == 0
    read_output = json.loads(capsys.readouterr().out)
    assert read_output["status"] == "fresh"

    assert main(["read", "--path", str(tmp_path / "none.json")]) == 2
    unavailable = json.loads(capsys.readouterr().out)
    assert unavailable["status"] == "unavailable"
