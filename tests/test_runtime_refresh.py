from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bureau import runtime_refresh as refresh

NOW = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
DEPLOYED = "1" * 40
MAIN = "2" * 40
HEAD = "3" * 40


def write_manifest(path: Path, source_commit: str = DEPLOYED, **extra: Any) -> dict[str, Any]:
    value = {
        "schema_version": 1,
        "kind": "bureau_runtime_deployment",
        "source_commit": source_commit,
        **extra,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(refresh.canonical_bytes(value))
    return value


def green_pr_detail(main_commit: str = MAIN) -> dict[str, Any]:
    return {
        "number": 42,
        "state": "MERGED",
        "isDraft": False,
        "mergedAt": "2026-07-14T07:30:00Z",
        "mergeCommit": {"oid": main_commit},
        "headRefOid": HEAD,
        "baseRefName": "main",
        "url": "https://example.invalid/pr/42",
        "statusCheckRollup": [
            {"name": "validate (3.10)", "conclusion": "SUCCESS"},
            {"name": "validate (3.12)", "conclusion": "SUCCESS"},
        ],
    }


def github_fixture(
    *,
    main_commit: str = MAIN,
    second_main: str | None = None,
    detail: dict[str, Any] | None = None,
    associated: list[dict[str, Any]] | None = None,
    ahead_by: int = 1,
):
    calls: list[list[str]] = []
    main_reads = 0

    def github(arguments: list[str]) -> Any:
        nonlocal main_reads
        calls.append(arguments)
        joined = " ".join(arguments)
        if joined == "api repos/heimgewebe/bureau/commits/main":
            main_reads += 1
            return {"sha": second_main if main_reads > 1 and second_main else main_commit}
        if joined.endswith(f"repos/heimgewebe/bureau/commits/{main_commit}/pulls"):
            return (
                associated
                if associated is not None
                else [
                    {
                        "number": 42,
                        "merge_commit_sha": main_commit,
                        "merged_at": "2026-07-14T07:30:00Z",
                        "base": {"ref": "main"},
                    }
                ]
            )
        if arguments[:3] == ["pr", "view", "42"]:
            return detail if detail is not None else green_pr_detail(main_commit)
        if joined == f"api repos/heimgewebe/bureau/compare/{DEPLOYED}...{main_commit}":
            return {"ahead_by": ahead_by}
        raise AssertionError(arguments)

    return github, calls


def candidate(tmp_path: Path, **github_options: Any) -> tuple[dict[str, Any], Path]:
    manifest_path = tmp_path / "prefix/deployment-manifest.json"
    write_manifest(manifest_path)
    github, _ = github_fixture(**github_options)
    value = refresh.observe_runtime_refresh(
        repository="heimgewebe/bureau",
        manifest_path=manifest_path,
        now=NOW,
        github=github,
    )
    return value, manifest_path


def prepare_candidate_intent(
    tmp_path: Path,
) -> tuple[dict[str, Any], Path, dict[str, Any], Path]:
    observed, manifest_path = candidate(tmp_path)
    state_root = (tmp_path / "state").resolve()
    intent, intent_path = refresh.prepare_intent(
        candidate=observed,
        state_root=state_root,
        prefix=(tmp_path / "prefix").resolve(),
        bin_dir=(tmp_path / "bin").resolve(),
        remote_url="file:///tmp/bureau.git",
        authorized_by="chatgpt",
        authorization="User explicitly authorized T016 implementation.",
        now=NOW,
    )
    return observed, manifest_path, intent, intent_path


def lease_for(
    root: Path,
    intent: dict[str, Any],
    *,
    owner_id: str = "chatgpt-t016",
    expires_at: datetime | None = None,
    omit: set[str] | None = None,
) -> tuple[dict[str, Any], Path]:
    database = root / "resources.sqlite3"
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO metadata(key, value) VALUES('schema_version', '1')")
    connection.execute(
        """
        CREATE TABLE leases (
            resource_key TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            purpose TEXT NOT NULL,
            acquired_at_unix INTEGER NOT NULL,
            updated_at_unix INTEGER NOT NULL,
            expires_at_unix INTEGER NOT NULL,
            metadata_sha256 TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            reclaimed_from_owner TEXT
        )
        """
    )
    acquired = int((NOW - timedelta(minutes=1)).timestamp())
    expiry = int((expires_at or NOW + timedelta(hours=1)).timestamp())
    omitted = omit or set()
    for key in intent["required_resource_keys"]:
        if key in omitted:
            continue
        connection.execute(
            "INSERT INTO leases VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (key, owner_id, "test", acquired, acquired, expiry, "a" * 64, "{}"),
        )
    connection.commit()
    connection.close()
    database.chmod(0o600)
    return {"owner_id": owner_id, "task_id": "grabowski-task-t016"}, database


def test_observe_reports_already_current_without_pr_lookup(tmp_path: Path) -> None:
    manifest = tmp_path / "deployment-manifest.json"
    write_manifest(manifest, MAIN)
    calls: list[list[str]] = []

    def github(arguments: list[str]) -> Any:
        calls.append(arguments)
        return {"sha": MAIN}

    result = refresh.observe_runtime_refresh(
        repository="heimgewebe/bureau",
        manifest_path=manifest,
        now=NOW,
        github=github,
    )

    assert result["status"] == "already_current"
    assert result["lag_commits"] == 0
    assert calls == [["api", "repos/heimgewebe/bureau/commits/main"]]
    refresh.verify_digest(result, "observation_sha256")


def test_observe_binds_exact_merged_main_and_green_ci(tmp_path: Path) -> None:
    result, _ = candidate(tmp_path)

    assert result["status"] == "candidate"
    assert result["main_commit"] == MAIN
    assert result["pull_request"] == {
        "number": 42,
        "url": "https://example.invalid/pr/42",
        "head_commit": HEAD,
        "merge_commit": MAIN,
    }
    assert set(result["check_summary"]) == {
        "validate (3.10)",
        "validate (3.12)",
    }
    assert all(item["state"] == "success" for item in result["check_summary"].values())
    assert result["lag_commits"] == 1
    assert len(result["target_sha256"]) == 64


def test_observe_alerts_after_freshness_slo(tmp_path: Path) -> None:
    result, manifest = candidate(tmp_path)
    github, _ = github_fixture()
    result = refresh.observe_runtime_refresh(
        repository="heimgewebe/bureau",
        manifest_path=manifest,
        now=NOW + timedelta(hours=3),
        slo_seconds=3600,
        github=github,
    )
    assert result["status"] == "alert"
    assert result["age_seconds"] > result["slo_seconds"]


def test_observe_blocks_failed_or_missing_ci(tmp_path: Path) -> None:
    detail = green_pr_detail()
    detail["statusCheckRollup"] = [{"name": "validate (3.10)", "conclusion": "FAILURE"}]
    result, _ = candidate(tmp_path, detail=detail)

    assert result["status"] == "blocked"
    assert result["reason_codes"] == ["required-ci-not-green"]
    assert result["check_summary"]["validate (3.10)"]["state"] == "failure"
    assert result["check_summary"]["validate (3.12)"]["state"] == "missing"


def test_observe_rejects_skipped_required_ci(tmp_path: Path) -> None:
    detail = green_pr_detail()
    detail["statusCheckRollup"] = [
        {"name": "validate (3.10)", "conclusion": "SUCCESS"},
        {"name": "validate (3.12)", "conclusion": "SKIPPED"},
    ]
    result, _ = candidate(tmp_path, detail=detail)

    assert result["status"] == "blocked"
    assert result["reason_codes"] == ["required-ci-not-green"]
    assert result["check_summary"]["validate (3.12)"]["state"] == "failure"


def test_observe_blocks_ambiguous_pr_and_main_drift(tmp_path: Path) -> None:
    result, _ = candidate(
        tmp_path,
        associated=[],
        second_main="4" * 40,
    )

    assert result["status"] == "blocked"
    assert "merged-main-pr-ambiguous" in result["reason_codes"]
    assert "main-changed-during-observation" in result["reason_codes"]


def test_prepare_intent_is_hash_bound_and_requires_authorization(tmp_path: Path) -> None:
    observed, _, intent, intent_path = prepare_candidate_intent(tmp_path)

    assert intent_path.is_file()
    assert intent["target_sha256"] == observed["target_sha256"]
    assert intent["expected_deployed_source_commit"] == DEPLOYED
    assert intent["required_resource_keys"] == sorted(intent["required_resource_keys"])
    assert f"path:{tmp_path.resolve() / 'bin/bureau'}" in intent["required_resource_keys"]
    refresh.verify_digest(intent, "intent_sha256")

    with pytest.raises(refresh.RuntimeRefreshError, match="authorization"):
        refresh.prepare_intent(
            candidate=observed,
            state_root=(tmp_path / "other-state").resolve(),
            prefix=(tmp_path / "other-prefix").resolve(),
            bin_dir=(tmp_path / "other-bin").resolve(),
            remote_url="file:///tmp/bureau.git",
            authorized_by="",
            authorization="",
            now=NOW,
        )


def test_prepare_intent_rejects_tampered_or_blocked_candidate(tmp_path: Path) -> None:
    observed, _ = candidate(tmp_path)
    observed["main_commit"] = "9" * 40
    with pytest.raises(refresh.RuntimeRefreshError, match="does not match"):
        refresh.prepare_intent(
            candidate=observed,
            state_root=(tmp_path / "state").resolve(),
            prefix=(tmp_path / "prefix").resolve(),
            bin_dir=(tmp_path / "bin").resolve(),
            remote_url="file:///tmp/bureau.git",
            authorized_by="chatgpt",
            authorization="explicit authorization",
            now=NOW,
        )

    blocked, _ = candidate(
        tmp_path / "blocked",
        associated=[],
    )
    with pytest.raises(refresh.RuntimeRefreshError) as blocked_error:
        refresh.prepare_intent(
            candidate=blocked,
            state_root=(tmp_path / "blocked-state").resolve(),
            prefix=(tmp_path / "blocked-prefix").resolve(),
            bin_dir=(tmp_path / "blocked-bin").resolve(),
            remote_url="file:///tmp/bureau.git",
            authorized_by="chatgpt",
            authorization="explicit authorization",
            now=NOW,
        )
    assert blocked_error.value.code == "candidate-not-deployable"


def test_lease_binding_requires_live_complete_private_database(tmp_path: Path) -> None:
    _, _, intent, _ = prepare_candidate_intent(tmp_path)
    binding, incomplete_db = lease_for(
        tmp_path / "incomplete",
        intent,
        omit={intent["required_resource_keys"][-1]},
    )
    with pytest.raises(refresh.RuntimeRefreshError) as missing:
        refresh.validate_live_lease_binding(intent, binding, resource_db=incomplete_db, now=NOW)
    assert missing.value.code == "lease-resources-missing"

    binding, expired_db = lease_for(
        tmp_path / "expired",
        intent,
        expires_at=NOW + timedelta(minutes=2),
    )
    with pytest.raises(refresh.RuntimeRefreshError) as expiry:
        refresh.validate_live_lease_binding(intent, binding, resource_db=expired_db, now=NOW)
    assert expiry.value.code == "lease-expired"

    binding, live_db = lease_for(tmp_path / "live", intent)
    observed = refresh.validate_live_lease_binding(intent, binding, resource_db=live_db, now=NOW)
    assert observed["owner_id"] == binding["owner_id"]
    assert observed["resource_keys"] == intent["required_resource_keys"]
    assert len(observed["lease_snapshots"]) == len(intent["required_resource_keys"])

    live_db.chmod(0o644)
    with pytest.raises(refresh.RuntimeRefreshError) as public:
        refresh.validate_live_lease_binding(intent, binding, resource_db=live_db, now=NOW)
    assert public.value.code == "lease-database-mode-invalid"


def git(cwd: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def create_remote(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.invalid")
    (source / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    git(source, "add", "tracked.txt")
    git(source, "commit", "-m", "initial")
    head = git(source, "rev-parse", "HEAD")
    bare = tmp_path / "remote.git"
    git(tmp_path, "clone", "--bare", str(source), str(bare))
    return bare, head


def test_lease_database_rejects_file_and_parent_symlinks(tmp_path: Path) -> None:
    _, _, intent, _ = prepare_candidate_intent(tmp_path)
    binding, live_db = lease_for(tmp_path / "real", intent)

    file_link = tmp_path / "resources-link.sqlite3"
    file_link.symlink_to(live_db)
    with pytest.raises(refresh.RuntimeRefreshError) as file_error:
        refresh.validate_live_lease_binding(intent, binding, resource_db=file_link, now=NOW)
    assert file_error.value.code == "lease-database-symlink"

    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(live_db.parent, target_is_directory=True)
    with pytest.raises(refresh.RuntimeRefreshError) as parent_error:
        refresh.validate_live_lease_binding(
            intent,
            binding,
            resource_db=linked_parent / live_db.name,
            now=NOW,
        )
    assert parent_error.value.code == "lease-database-parent-symlink"


def test_environment_cannot_redirect_production_lease_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GRABOWSKI_RESOURCE_DB", "/tmp/fake-resources.sqlite3")
    assert (
        Path("~/.local/state/grabowski/resources.sqlite3").expanduser()
        == refresh.DEFAULT_GRABOWSKI_RESOURCE_DB
    )


def test_prepare_source_checkout_is_clean_detached_and_exact(tmp_path: Path) -> None:
    remote, head = create_remote(tmp_path)
    workspace = tmp_path / "state/workspaces" / head

    identity = refresh.prepare_source_checkout(
        remote_url=str(remote),
        workspace=workspace,
        expected_commit=head,
        workspaces_root=tmp_path / "state/workspaces",
    )

    assert identity["head"] == head
    assert identity["origin_main"] == head
    assert identity["detached"] is True
    assert identity["dirty"] is False
    assert git(workspace, "status", "--porcelain=v1") == ""

    (workspace / "foreign.txt").write_text("dirty", encoding="utf-8")
    with pytest.raises(refresh.RuntimeRefreshError) as dirty:
        refresh.validate_source_checkout(workspace, head, str(remote))
    assert dirty.value.code == "source-dirty"


def test_prepare_source_checkout_fails_closed_on_origin_drift(tmp_path: Path) -> None:
    remote, _head = create_remote(tmp_path)
    with pytest.raises(refresh.RuntimeRefreshError) as error:
        refresh.prepare_source_checkout(
            remote_url=str(remote),
            workspace=tmp_path / "state/workspaces/wrong",
            expected_commit="f" * 40,
            workspaces_root=tmp_path / "state/workspaces",
        )
    assert error.value.code == "origin-main-drift"


def test_apply_success_is_one_shot_and_preserves_foreign_dirty_checkout(
    tmp_path: Path,
) -> None:
    observed, manifest_path, intent, intent_path = prepare_candidate_intent(tmp_path)
    dirty_checkout = tmp_path / "conventional-main"
    dirty_checkout.mkdir()
    sentinel = dirty_checkout / "foreign-change.txt"
    sentinel.write_text("do not touch", encoding="utf-8")
    calls = {"source": 0, "install": 0, "readback": 0}

    def observer(**_: Any) -> dict[str, Any]:
        return observed

    def source_preparer(**kwargs: Any) -> dict[str, Any]:
        calls["source"] += 1
        workspace = kwargs["workspace"]
        workspace.mkdir(parents=True)
        return {"root": str(workspace), "head": MAIN, "dirty": False, "detached": True}

    def installer(**_: Any) -> dict[str, Any]:
        calls["install"] += 1
        return {"manifest_sha256": "a" * 64, "rollback": {"directory": "/rollback"}}

    def readback(**_: Any) -> dict[str, Any]:
        calls["readback"] += 1
        return {"source_commit": MAIN, "check_valid": True, "runtime_identity_valid": True}

    lease_binding, resource_db = lease_for(tmp_path / "leases", intent)
    result = refresh.apply_runtime_refresh(
        intent_path=intent_path,
        lease_binding=lease_binding,
        manifest_path=manifest_path,
        state_root=Path(intent["state_root"]),
        resource_db=resource_db,
        now=NOW,
        observer=observer,
        source_preparer=source_preparer,
        installer=installer,
        readback=readback,
    )
    reused = refresh.apply_runtime_refresh(
        intent_path=intent_path,
        lease_binding=lease_binding,
        manifest_path=manifest_path,
        state_root=Path(intent["state_root"]),
        resource_db=resource_db,
        now=NOW,
        observer=observer,
        source_preparer=source_preparer,
        installer=installer,
        readback=readback,
    )

    assert result["status"] == "deployed"
    assert reused["status"] == "deployed"
    assert reused["reused"] is True
    assert calls == {"source": 1, "install": 1, "readback": 1}
    assert not Path(intent["workspace"]).exists()
    assert sentinel.read_text(encoding="utf-8") == "do not touch"
    refresh.verify_digest(result, "result_sha256")


def test_distinct_intents_for_same_target_share_one_effect_attempt(tmp_path: Path) -> None:
    observed, manifest_path, first_intent, first_path = prepare_candidate_intent(tmp_path)
    second_intent, second_path = refresh.prepare_intent(
        candidate=observed,
        state_root=Path(first_intent["state_root"]),
        prefix=Path(first_intent["prefix"]),
        bin_dir=Path(first_intent["bin_dir"]),
        remote_url=first_intent["remote_url"],
        authorized_by="chatgpt",
        authorization="Second explicit authorization for the same exact target.",
        now=NOW + timedelta(seconds=1),
    )
    assert first_intent["intent_sha256"] != second_intent["intent_sha256"]
    assert first_intent["target_sha256"] == second_intent["target_sha256"]
    binding, resource_db = lease_for(tmp_path / "leases", first_intent)
    effects = 0

    def source_preparer(**kwargs: Any) -> dict[str, Any]:
        nonlocal effects
        effects += 1
        kwargs["workspace"].mkdir(parents=True)
        return {"head": MAIN, "root": str(kwargs["workspace"])}

    def installer(**_: Any) -> dict[str, Any]:
        return {"manifest_sha256": "a" * 64}

    def readback(**_: Any) -> dict[str, Any]:
        return {"source_commit": MAIN, "check_valid": True}

    first = refresh.apply_runtime_refresh(
        intent_path=first_path,
        lease_binding=binding,
        manifest_path=manifest_path,
        state_root=Path(first_intent["state_root"]),
        resource_db=resource_db,
        now=NOW,
        observer=lambda **_: observed,
        source_preparer=source_preparer,
        installer=installer,
        readback=readback,
    )
    second = refresh.apply_runtime_refresh(
        intent_path=second_path,
        lease_binding=binding,
        manifest_path=manifest_path,
        state_root=Path(first_intent["state_root"]),
        resource_db=resource_db,
        now=NOW + timedelta(seconds=1),
        observer=lambda **_: pytest.fail("target result must be reused before observation"),
        source_preparer=lambda **_: pytest.fail("source preparation must not repeat"),
        installer=lambda **_: pytest.fail("installer must not repeat"),
    )

    assert first["status"] == "deployed"
    assert second["status"] == "deployed"
    assert second["reused"] is True
    assert second["intent_sha256"] == first_intent["intent_sha256"]
    assert effects == 1


def test_apply_installer_failure_is_durable_unclear_and_never_retried(
    tmp_path: Path,
) -> None:
    observed, manifest_path, intent, intent_path = prepare_candidate_intent(tmp_path)
    calls = 0

    def observer(**_: Any) -> dict[str, Any]:
        return observed

    def source_preparer(**kwargs: Any) -> dict[str, Any]:
        kwargs["workspace"].mkdir(parents=True)
        return {"head": MAIN}

    def installer(**_: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        raise refresh.RuntimeRefreshError(
            "installer-returned-nonzero", "effect outcome is not established"
        )

    lease_binding, resource_db = lease_for(tmp_path / "leases", intent)
    first = refresh.apply_runtime_refresh(
        intent_path=intent_path,
        lease_binding=lease_binding,
        manifest_path=manifest_path,
        state_root=Path(intent["state_root"]),
        resource_db=resource_db,
        now=NOW,
        observer=observer,
        source_preparer=source_preparer,
        installer=installer,
    )
    second = refresh.apply_runtime_refresh(
        intent_path=intent_path,
        lease_binding=lease_binding,
        manifest_path=manifest_path,
        state_root=Path(intent["state_root"]),
        resource_db=resource_db,
        now=NOW,
        observer=observer,
        source_preparer=source_preparer,
        installer=installer,
    )

    assert first["status"] == "unclear"
    assert first["effect_started"] is True
    assert first["workspace_preserved"] is True
    assert second["status"] == "unclear"
    assert second["reused"] is True
    assert calls == 1


def test_apply_existing_start_without_result_is_unclear_without_execution(
    tmp_path: Path,
) -> None:
    _, manifest_path, intent, intent_path = prepare_candidate_intent(tmp_path)
    attempt = Path(intent["state_root"]) / "attempts" / intent["target_sha256"]
    refresh.create_only(
        attempt / "started.json",
        refresh.canonical_bytes({"kind": "attempt", "effect_started": False}),
    )
    executed = False

    def observer(**_: Any) -> dict[str, Any]:
        nonlocal executed
        executed = True
        raise AssertionError("observer must not run")

    lease_binding, resource_db = lease_for(tmp_path / "leases", intent)
    result = refresh.apply_runtime_refresh(
        intent_path=intent_path,
        lease_binding=lease_binding,
        manifest_path=manifest_path,
        state_root=Path(intent["state_root"]),
        resource_db=resource_db,
        now=NOW,
        observer=observer,
    )

    assert result["status"] == "unclear_existing_attempt"
    assert result["reused"] is True
    assert executed is False


def test_apply_already_current_deduplicates_without_installer(tmp_path: Path) -> None:
    observed, manifest_path, intent, intent_path = prepare_candidate_intent(tmp_path)
    live = dict(observed)
    live.update(
        {
            "status": "already_current",
            "deployed_source_commit": MAIN,
            "main_commit": MAIN,
            "reason_codes": [],
        }
    )
    live = refresh.bind_digest(live, "observation_sha256")
    write_manifest(manifest_path, MAIN)

    lease_binding, resource_db = lease_for(tmp_path / "leases", intent)
    result = refresh.apply_runtime_refresh(
        intent_path=intent_path,
        lease_binding=lease_binding,
        manifest_path=manifest_path,
        state_root=Path(intent["state_root"]),
        resource_db=resource_db,
        now=NOW,
        observer=lambda **_: live,
        source_preparer=lambda **_: pytest.fail("source preparation must not run"),
        installer=lambda **_: pytest.fail("installer must not run"),
    )

    assert result["status"] == "already_current"
    assert result["effect_started"] is False


def test_readback_validates_both_launchers_and_runtime_identity(tmp_path: Path) -> None:
    prefix = tmp_path / "prefix"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    manifest_path = prefix / "deployment-manifest.json"
    write_manifest(
        manifest_path,
        MAIN,
        release_id="release",
        package_tree_sha256="a" * 64,
        canonical_registry_tree_sha256="b" * 64,
    )
    bureau = bin_dir / "bureau"
    bureau.write_text(
        """#!/usr/bin/env python3
import json, sys
if sys.argv[-1] == 'check':
    print(json.dumps({'result': {'valid': True}}))
else:
    print(json.dumps({'result': {'status': 'ok'}, 'runtime_identity': {'manifest': {
        'valid': True,
        'source_commit': '"""
        + MAIN
        + """',
        'observed_package_tree_sha256': '"""
        + "a" * 64
        + """',
        'canonical_registry': {'valid': True, 'observed_tree_sha256': '"""
        + "b" * 64
        + """'}
    }}}))
""",
        encoding="utf-8",
    )
    bureau.chmod(0o755)
    runner = bin_dir / "bureau-runtime-refresh"
    runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    runner.chmod(0o755)
    receipt = {
        "manifest_sha256": refresh.sha256_bytes(manifest_path.read_bytes()),
        "launcher_sha256": refresh.sha256_bytes(bureau.read_bytes()),
        "runtime_refresh_launcher_sha256": refresh.sha256_bytes(runner.read_bytes()),
        "rollback": {"directory": "/rollback"},
    }

    result = refresh.readback_install(
        expected_commit=MAIN,
        prefix=prefix,
        bin_dir=bin_dir,
        install_receipt=receipt,
    )

    assert result["check_valid"] is True
    assert result["runtime_identity_valid"] is True
    assert result["source_commit"] == MAIN
    assert result["rollback"] == {"directory": "/rollback"}


def load_installer_module() -> Any:
    path = Path(__file__).parents[1] / "ops/install-bureau-runtime.py"
    spec = importlib.util.spec_from_file_location("install_bureau_runtime", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_installer_wrapper_selects_refresh_entrypoint_and_backs_up_both(
    tmp_path: Path,
) -> None:
    installer = load_installer_module()
    rendered = installer.wrapper(
        tmp_path / "deployment-manifest.json",
        "a" * 64,
        "bureau.runtime_refresh",
    ).decode()
    assert "importlib.import_module('bureau.runtime_refresh')" in rendered
    assert installer.MANAGED_MARKER in rendered

    prefix = tmp_path / "prefix"
    manifest = prefix / "deployment-manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("manifest", encoding="utf-8")
    primary = tmp_path / "bin/bureau"
    secondary = tmp_path / "bin/bureau-runtime-refresh"
    primary.parent.mkdir()
    primary.write_text("primary", encoding="utf-8")
    secondary.write_text("secondary", encoding="utf-8")

    backup = installer._backup_existing(prefix, manifest, primary, secondary)

    assert Path(backup["manifest"]).read_text(encoding="utf-8") == "manifest"
    assert Path(backup["launcher"]).read_text(encoding="utf-8") == "primary"
    assert Path(backup["runtime_refresh_launcher"]).read_text(encoding="utf-8") == "secondary"


def test_status_reports_terminal_and_unresolved_attempts(tmp_path: Path) -> None:
    manifest = tmp_path / "prefix/deployment-manifest.json"
    write_manifest(manifest)
    state = tmp_path / "state"
    terminal = refresh.bind_digest(
        {
            "kind": "bureau_runtime_refresh_result",
            "status": "deployed",
        },
        "result_sha256",
    )
    refresh.create_only(
        state / "attempts/terminal/result.json",
        refresh.canonical_bytes(terminal),
    )
    refresh.create_only(
        state / "attempts/unresolved/started.json",
        refresh.canonical_bytes({"kind": "start"}),
    )

    result = refresh.status_report(state, manifest)

    assert {item["status"] for item in result["attempts"]} == {
        "deployed",
        "unclear_existing_attempt",
    }


def test_real_installer_publishes_working_refresh_launcher(tmp_path: Path) -> None:
    repository = Path(__file__).parents[1]
    staged = tmp_path / "staged"
    shutil.copytree(
        repository,
        staged,
        ignore=shutil.ignore_patterns(
            ".git",
            ".pytest_cache",
            ".ruff_cache",
            "__pycache__",
            "*.pyc",
        ),
    )
    git(staged, "init", "-b", "main")
    git(staged, "config", "user.name", "Test")
    git(staged, "config", "user.email", "test@example.invalid")
    git(staged, "add", ".")
    git(staged, "commit", "-m", "synthetic T016 source")
    bare = tmp_path / "bureau.git"
    git(tmp_path, "clone", "--bare", str(staged), str(bare))
    clean = tmp_path / "clean"
    git(tmp_path, "clone", str(bare), str(clean))
    git(clean, "remote", "set-url", "origin", str(bare))
    git(clean, "fetch", "origin", "main")

    prefix = tmp_path / "prefix"
    bin_dir = tmp_path / "bin"
    install = subprocess.run(
        [
            sys.executable,
            str(clean / "ops/install-bureau-runtime.py"),
            "--source",
            str(clean),
            "--prefix",
            str(prefix),
            "--bin-dir",
            str(bin_dir),
        ],
        cwd=clean,
        check=True,
        text=True,
        capture_output=True,
    )
    receipt = json.loads(install.stdout.strip().splitlines()[-1])
    bureau = bin_dir / "bureau"
    runner = bin_dir / "bureau-runtime-refresh"

    assert bureau.is_file() and os.access(bureau, os.X_OK)
    assert runner.is_file() and os.access(runner, os.X_OK)
    assert receipt["launcher_sha256"] == refresh.sha256_bytes(bureau.read_bytes())
    assert receipt["runtime_refresh_launcher_sha256"] == refresh.sha256_bytes(runner.read_bytes())

    check = subprocess.run(
        [str(bureau), "--json", "check"],
        check=True,
        text=True,
        capture_output=True,
    )
    assert json.loads(check.stdout)["result"]["valid"] is True
    status = subprocess.run(
        [
            str(runner),
            "--state-root",
            str(tmp_path / "refresh-state"),
            "status",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    payload = json.loads(status.stdout)
    assert payload["kind"] == "bureau_runtime_refresh_status"
    assert payload["deployed_source_commit"] == git(clean, "rev-parse", "HEAD")
