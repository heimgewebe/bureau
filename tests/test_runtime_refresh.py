from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from runtime_approval import write_runtime_approval_intent

from bureau import legacy, registry_snapshot, runtime_identity
from bureau import runtime_refresh as refresh
from bureau.v2 import StateStore

NOW = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
DEPLOYED = "1" * 40
MAIN = "2" * 40
HEAD = "3" * 40


class FakeUserSystemd:
    def __init__(
        self,
        unit_root: Path,
        *,
        runtime_unit_root: Path | None = None,
        timer_states: dict[str, tuple[str, str]] | None = None,
        fail_once: tuple[str, ...] | None = None,
    ) -> None:
        self.unit_root = unit_root
        (self.unit_root / "timers.target.wants").mkdir(parents=True, exist_ok=True)
        self.runtime_unit_root = runtime_unit_root or unit_root.parent / "runtime-user"
        self.runtime_unit_root.mkdir(parents=True, exist_ok=True)
        self.commands: list[tuple[str, ...]] = []
        self.fail_once = fail_once
        self.failed = False
        self.states: dict[str, dict[str, str]] = {}
        configured = timer_states or {}
        for name in refresh.RUNTIME_SCHEDULER_NAMES:
            for kind in ("timer", "service"):
                unit = f"{name}.{kind}"
                fragment = unit_root / unit
                loaded = fragment.is_file() and not fragment.is_symlink()
                if kind == "timer":
                    unit_file, active = configured.get(name, ("disabled", "inactive"))
                    sub = "waiting" if active == "active" else "dead"
                    if not loaded:
                        unit_file = ""
                else:
                    unit_file, active, sub = "static", "inactive", "dead"
                self.states[unit] = {
                    "LoadState": "loaded" if loaded else "not-found",
                    "UnitFileState": unit_file if loaded else "",
                    "ActiveState": active,
                    "SubState": sub,
                    "FragmentPath": str(fragment) if loaded else "",
                    "Result": "success",
                }
                if kind == "timer" and loaded:
                    if unit_file == "enabled":
                        self._enablement_link(unit, runtime=False).parent.mkdir(
                            parents=True, exist_ok=True
                        )
                        self._enablement_link(unit, runtime=False).symlink_to(
                            f"../{unit}"
                        )
                    elif unit_file == "enabled-runtime":
                        self._enablement_link(unit, runtime=True).parent.mkdir(
                            parents=True, exist_ok=True
                        )
                        self._enablement_link(unit, runtime=True).symlink_to(
                            str(fragment)
                        )

    def _enablement_link(self, unit: str, *, runtime: bool) -> Path:
        root = self.runtime_unit_root if runtime else self.unit_root
        return root / "timers.target.wants" / unit

    def _refresh_unit_file_state(self, unit: str) -> None:
        state = self.states[unit]
        if state["LoadState"] != "loaded":
            state["UnitFileState"] = ""
        elif os.path.lexists(self._enablement_link(unit, runtime=False)):
            state["UnitFileState"] = "enabled"
        elif os.path.lexists(self._enablement_link(unit, runtime=True)):
            state["UnitFileState"] = "enabled-runtime"
        else:
            state["UnitFileState"] = "disabled"

    def _reload(self) -> None:
        for unit, state in self.states.items():
            fragment = self.unit_root / unit
            loaded = fragment.is_file() and not fragment.is_symlink()
            state["LoadState"] = "loaded" if loaded else "not-found"
            state["FragmentPath"] = str(fragment) if loaded else ""
            if unit.endswith(".timer"):
                self._refresh_unit_file_state(unit)
            elif loaded:
                state["UnitFileState"] = "static"
            else:
                state["UnitFileState"] = ""
            if not loaded:
                state["ActiveState"] = "inactive"
                state["SubState"] = "dead"

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        arguments = tuple(argv[2:])
        self.commands.append(arguments)
        if self.fail_once == arguments and not self.failed:
            self.failed = True
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="injected failure")
        command = arguments[0]
        if command == "show":
            unit = arguments[1]
            properties = arguments[-1].removeprefix("--property=").split(",")
            output = "\n".join(
                f"{key}={self.states[unit][key]}" for key in properties
            )
            return subprocess.CompletedProcess(argv, 0, stdout=output + "\n", stderr="")
        if command == "daemon-reload":
            self._reload()
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        runtime = "--runtime" in arguments
        now = "--now" in arguments
        units = [item for item in arguments[1:] if not item.startswith("--")]
        if command in {"enable", "disable"}:
            for unit in units:
                state = self.states[unit]
                if command == "enable":
                    link = self._enablement_link(unit, runtime=runtime)
                    link.parent.mkdir(parents=True, exist_ok=True)
                    if os.path.lexists(link):
                        link.unlink()
                    link.symlink_to(str(self.unit_root / unit))
                else:
                    for root in (self.unit_root, self.runtime_unit_root):
                        for link in root.glob(f"*.target.wants/{unit}"):
                            if link.is_symlink():
                                link.unlink()
                self._refresh_unit_file_state(unit)
                if now:
                    state["ActiveState"] = "active" if command == "enable" else "inactive"
                    state["SubState"] = "waiting" if command == "enable" else "dead"
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if command in {"start", "stop"}:
            for unit in units:
                state = self.states[unit]
                state["ActiveState"] = "active" if command == "start" else "inactive"
                state["SubState"] = (
                    "waiting" if command == "start" and unit.endswith(".timer") else "dead"
                )
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if command == "reset-failed":
            for unit in units:
                self.states[unit]["Result"] = "success"
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected systemctl command: {arguments}")


def successful_systemd_analyze(argv: list[str]) -> subprocess.CompletedProcess[str]:
    assert argv[:3] == ["systemd-analyze", "--user", "verify"]
    assert len(argv[3:]) == len(refresh.RUNTIME_SCHEDULER_NAMES) * 2
    return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def successful_cycle_validation(**kwargs: Any) -> dict[str, Any]:
    release = kwargs["canonical_root"]
    assert all(
        path.is_relative_to(release) for path in kwargs["module_paths"].values()
    )
    return {
        "status": "ok",
        "activatable": False,
        "read_only": True,
        "self_heal": False,
    }


def scheduler_release(tmp_path: Path) -> tuple[Path, str, str]:
    source_commit = "9" * 40
    release_id = f"{source_commit[:12]}-srcscheduler"
    release = tmp_path / "runtime/releases" / release_id
    shutil.copytree(Path(__file__).parents[1] / "ops/systemd", release / "ops/systemd")
    return release, release_id, source_commit


def _github_result(
    *, returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["gh"], returncode, stdout=stdout, stderr=stderr)


def test_github_preflight_retries_readonly_503_then_succeeds(monkeypatch) -> None:
    outcomes = [
        _github_result(returncode=1, stderr="HTTP 503: Service Unavailable"),
        _github_result(returncode=1, stderr="non-200 OK status code: 503 Service Unavailable"),
        _github_result(returncode=0, stdout='{"number": 2044}'),
    ]
    calls: list[list[str]] = []
    sleeps: list[float] = []

    def run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return outcomes.pop(0)

    monkeypatch.setattr(refresh, "_run", run)
    result = refresh.gh_preflight_json(
        ["pr", "view", "2044", "--repo", "heimgewebe/bureau", "--json", "number"],
        sleeper=sleeps.append,
    )

    assert result == {"number": 2044}
    assert len(calls) == 3
    assert sleeps == list(refresh.GITHUB_PREFLIGHT_RETRY_DELAYS_SECONDS)


def test_github_preflight_503_retry_is_bounded(monkeypatch) -> None:
    calls = 0
    sleeps: list[float] = []

    def run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return _github_result(returncode=1, stderr="HTTP 503: Service Unavailable")

    monkeypatch.setattr(refresh, "_run", run)
    with pytest.raises(refresh.RuntimeRefreshError) as error:
        refresh.gh_preflight_json(
            ["api", "repos/heimgewebe/bureau/commits/main"], sleeper=sleeps.append
        )

    assert error.value.code == "command-failed"
    assert calls == len(refresh.GITHUB_PREFLIGHT_RETRY_DELAYS_SECONDS) + 1
    assert sleeps == list(refresh.GITHUB_PREFLIGHT_RETRY_DELAYS_SECONDS)


def test_github_preflight_non_503_failure_is_not_retried(monkeypatch) -> None:
    calls = 0

    def run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return _github_result(returncode=1, stderr="HTTP 404: Not Found")

    monkeypatch.setattr(refresh, "_run", run)
    with pytest.raises(refresh.RuntimeRefreshError):
        refresh.gh_preflight_json(
            ["api", "repos/heimgewebe/bureau/commits/main"],
            sleeper=lambda _delay: pytest.fail("non-503 failure must not sleep"),
        )

    assert calls == 1


@pytest.mark.parametrize(
    "arguments",
    [
        ["api", "--method", "POST", "repos/heimgewebe/bureau/issues"],
        ["api", "-XPOST", "repos/heimgewebe/bureau/issues"],
        ["api", "repos/heimgewebe/bureau/issues", "-ftitle=test"],
        ["api", "--method", "GET", "--method", "GET", "repos/heimgewebe/bureau"],
    ],
)
def test_github_preflight_unsafe_command_shape_is_never_retried(
    monkeypatch, arguments: list[str]
) -> None:
    calls = 0

    def run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return _github_result(returncode=1, stderr="HTTP 503: Service Unavailable")

    monkeypatch.setattr(refresh, "_run", run)
    with pytest.raises(refresh.RuntimeRefreshError):
        refresh.gh_preflight_json(
            arguments,
            sleeper=lambda _delay: pytest.fail("unsafe command must not sleep"),
        )

    assert calls == 1


def runtime_authority_spec(task_id: str, *, state: str = "ready") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "id": task_id,
        "initiative": "TEST-RUNTIME-AUTHORITY",
        "title": "Hermetic runtime refresh authority",
        "goal": "Authorize exactly one target-bound runtime refresh.",
        "state": state,
        "priority": {"lane": "now", "rank": 1},
        "depends_on": [],
        "required_capabilities": ["bureau"],
        "claims": [
            {
                "resource": "component.bureau.runtime",
                "mode": "write",
                "isolation": "worktree",
            }
        ],
        "execution": {
            "mode": "interactive-agent",
            "policy": "review-before-effect",
            "approval": {
                "action_class": "runtime_mutation",
                "required_level": "break_glass",
                "note": "Hermetic single-use runtime authority.",
            },
        },
        "acceptance": [
            {
                "id": "runtime-authority-proof",
                "assertion": "The hermetic runtime authority remains target-bound.",
                "evidence_type": "object",
                "verifier": "manual_observation",
                "verifier_config": {"observation_scope": f"test:{task_id}:runtime-authority"},
            }
        ],
        "rollback": {"strategy": "Preserve the last immutable runtime."},
        "metadata": {
            "runtime_refresh_authority": {
                "schema_version": 1,
                "mode": "single-use-target-bound",
                "single_use": True,
                "successor_task_required_after_terminal": True,
                "required_action_class": "runtime_mutation",
                "required_approval_level": "break_glass",
                "required_claim_resource": "component.bureau.runtime",
                "required_task_state": ["ready", "active"],
                "target_binding": "candidate.target_sha256",
                "forbid_foreign_task_substitution": True,
                "forbid_historical_target_reuse": True,
                "no_run_closeout_acceptance": {
                    "schema_version": 1,
                    "kind": refresh.RUNTIME_AUTHORITY_NO_RUN_ACCEPTANCE_KIND,
                    "criteria": {
                        "runtime-authority-proof": {
                            "verifier": refresh.RUNTIME_AUTHORITY_NO_RUN_ACCEPTANCE_VERIFIER,
                            "required_evidence": [
                                "approval-intent",
                                "runtime-result",
                                "single-use-history",
                                "immutable-readback",
                                "state-store-integrity",
                                "lease-release",
                                "run-lifecycle",
                            ],
                        }
                    },
                },
            }
        },
    }


def source_precondition_contract() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "policy": "registered-source-or-verified-target-ancestor",
        "identity_sources": [
            "deployment-manifest.source_commit",
            "canonical-registry.source_commit",
        ],
        "require_deployment_registry_identity_match": True,
        "registered_deployed_source_commit": DEPLOYED,
        "registered_manifest_sha256": "a" * 64,
        "registered_registry_source_commit": DEPLOYED,
        "ancestry_verification": "git-merge-base-is-ancestor",
        "require_target_freshness": True,
        "required_before": ["prepare-intent", "apply"],
        "fail_closed": True,
        "does_not_establish": ["ancestry without fresh proof"],
    }


def seed_authority_store(
    root: Path, task_id: str, *, state: str = "ready", source_precondition: bool = False
) -> StateStore:
    state_root = root.resolve()
    store = StateStore(state_root / "bureau.sqlite3", state_root)
    spec = runtime_authority_spec(task_id, state=state)
    if source_precondition:
        spec["metadata"]["runtime_refresh_authority"]["mode"] = (
            refresh.RUNTIME_AUTHORITY_MODE_SOURCE_PRECONDITION
        )
        spec["metadata"]["runtime_refresh_authority"]["no_run_closeout_acceptance"][
            "criteria"
        ]["runtime-authority-proof"]["required_evidence"].append("source-precondition")
        spec["metadata"]["runtime_refresh_authority"]["source_precondition"] = (
            source_precondition_contract()
        )
    store.put_task_spec(
        spec,
        idempotency_key=f"seed:{task_id}:{state}",
        expected_revision=None,
        source="test",
    )
    return store


def test_runtime_authority_adoption_is_exact_idempotent_and_not_execution_authority(
    tmp_path: Path,
) -> None:
    task_id = "BUREAU-RUNTIME-ADOPTION-TEST"
    spec = runtime_authority_spec(task_id, state="planned")
    spec["metadata"]["publication_path"] = {
        "kind": "normal-protected-pull-request",
        "state_store_transition": "seed-missing-preserve-state-store",
    }
    root = tmp_path / "registry-repo"
    task_dir = root / "registry/tasks"
    task_dir.mkdir(parents=True)
    task_path = task_dir / f"{task_id}.json"
    task_path.write_text(json.dumps(spec, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Bureau Test"], cwd=root, check=True)
    (root / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "add", task_path.relative_to(root).as_posix()], cwd=root, check=True)
    subprocess.run(
        ["git", "commit", "-m", "add runtime authority"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()
    task_file_sha256 = hashlib.sha256(task_path.read_bytes()).hexdigest()
    other_id = "BUREAU-UNRELATED-MISSING"
    registry = SimpleNamespace(
        tasks={
            task_id: SimpleNamespace(raw=spec),
            other_id: SimpleNamespace(raw=runtime_authority_spec(other_id, state="planned")),
        }
    )
    store = StateStore(tmp_path / "state/bureau.sqlite3", tmp_path / "state")

    def github(arguments: list[str]) -> Any:
        if arguments == ["api", "repos/test/bureau/branches/main/protection"]:
            return {
                "required_status_checks": {
                    "strict": True,
                    "contexts": [
                        "validate (3.10)",
                        "validate (3.12)",
                        "registry-registration-preflight/freshness",
                    ],
                },
                "allow_force_pushes": {"enabled": False},
                "allow_deletions": {"enabled": False},
            }
        if arguments == ["api", "repos/test/bureau/commits/main"]:
            return {"sha": head}
        if arguments[:3] == ["pr", "view", "7"]:
            return {
                "number": 7,
                "state": "MERGED",
                "isDraft": False,
                "mergedAt": "2026-08-16T12:00:00Z",
                "mergeCommit": {"oid": head},
                "headRefOid": head,
                "baseRefName": "main",
                "statusCheckRollup": [
                    {"name": "validate (3.10)", "conclusion": "SUCCESS"},
                    {"name": "validate (3.12)", "conclusion": "SUCCESS"},
                    {
                        "name": "registry-registration-preflight/freshness",
                        "conclusion": "SUCCESS",
                    },
                ],
                "url": "https://example.invalid/pr/7",
                "files": [{"path": task_path.relative_to(root).as_posix()}],
            }
        raise AssertionError(arguments)

    first = refresh.adopt_runtime_refresh_authority(
        registry_root=root,
        repository="test/bureau",
        approval_task_id=task_id,
        publication_pr=7,
        publication_merge_commit=head,
        expected_main_commit=head,
        expected_task_file_sha256=task_file_sha256,
        authority_store=store,
        github=github,
        registry=registry,
    )
    assert first["status"] == "adopted"
    assert first["state"] == "planned"
    assert first["revision"] == 1
    assert store.task_spec(other_id) is None

    second = refresh.adopt_runtime_refresh_authority(
        registry_root=root,
        repository="test/bureau",
        approval_task_id=task_id,
        publication_pr=7,
        publication_merge_commit=head,
        expected_main_commit=head,
        expected_task_file_sha256=task_file_sha256,
        authority_store=store,
        github=github,
        registry=registry,
    )
    assert second["status"] == "already_present"
    assert second["revision"] == 1

    with pytest.raises(refresh.RuntimeRefreshError) as blocked:
        refresh.validate_authoritative_runtime_refresh_task(
            store=store, approval_task_id=task_id, target_sha256="a" * 64
        )
    assert blocked.value.code == "authority-task-state-invalid"


def test_runtime_authority_adoption_rejects_nonexact_pr_scope_and_missing_marker(
    tmp_path: Path,
) -> None:
    task_id = "BUREAU-RUNTIME-ADOPTION-SCOPE"
    spec = runtime_authority_spec(task_id, state="planned")
    spec["metadata"]["publication_path"] = {
        "kind": "normal-protected-pull-request",
        "state_store_transition": "seed-missing-preserve-state-store",
    }
    root = tmp_path / "registry-repo"
    task_dir = root / "registry/tasks"
    task_dir.mkdir(parents=True)
    task_path = task_dir / f"{task_id}.json"
    task_path.write_text(json.dumps(spec, sort_keys=True) + "\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Bureau Test"], cwd=root, check=True)
    (root / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "add", task_path.relative_to(root).as_posix()], cwd=root, check=True)
    subprocess.run(
        ["git", "commit", "-m", "add runtime authority"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()
    digest = hashlib.sha256(task_path.read_bytes()).hexdigest()
    registry = SimpleNamespace(tasks={task_id: SimpleNamespace(raw=spec)})

    def github(arguments: list[str]) -> Any:
        if arguments == ["api", "repos/test/bureau/branches/main/protection"]:
            return {
                "required_status_checks": {
                    "strict": True,
                    "contexts": [
                        "validate (3.10)",
                        "validate (3.12)",
                        "registry-registration-preflight/freshness",
                    ],
                },
                "allow_force_pushes": {"enabled": False},
                "allow_deletions": {"enabled": False},
            }
        if arguments == ["api", "repos/test/bureau/commits/main"]:
            return {"sha": head}
        if arguments[:3] == ["pr", "view", "8"]:
            return {
                "number": 8,
                "state": "MERGED",
                "isDraft": False,
                "mergedAt": "2026-08-16T12:00:00Z",
                "mergeCommit": {"oid": head},
                "headRefOid": head,
                "baseRefName": "main",
                "statusCheckRollup": [
                    {"name": "validate (3.10)", "conclusion": "SUCCESS"},
                    {"name": "validate (3.12)", "conclusion": "SUCCESS"},
                    {
                        "name": "registry-registration-preflight/freshness",
                        "conclusion": "SUCCESS",
                    },
                ],
                "url": "https://example.invalid/pr/8",
                "files": [
                    {"path": task_path.relative_to(root).as_posix()},
                    {"path": "registry/queue.json"},
                ],
            }
        raise AssertionError(arguments)

    with pytest.raises(refresh.RuntimeRefreshError) as scope_error:
        refresh.verify_runtime_refresh_authority_publication(
            registry_root=root,
            repository="test/bureau",
            approval_task_id=task_id,
            publication_pr=8,
            publication_merge_commit=head,
            expected_main_commit=head,
            expected_task_file_sha256=digest,
            github=github,
            registry=registry,
        )
    assert scope_error.value.code == "authority-adoption-publication-scope-invalid"

    unmarked = runtime_authority_spec("BUREAU-UNMARKED", state="planned")
    with pytest.raises(refresh.RuntimeRefreshError) as marker_error:
        refresh._validate_runtime_refresh_authority_adoption_spec(
            spec=unmarked, approval_task_id="BUREAU-UNMARKED"
        )
    assert marker_error.value.code == "authority-adoption-publication-contract-invalid"


def authority_store_for_intent(intent: dict[str, Any]) -> StateStore:
    binding = intent["authority_state_store"]
    return StateStore(Path(binding["state_db"]), Path(binding["state_root"]))


def revise_authority(
    store: StateStore,
    task_id: str,
    mutate: Any,
    *,
    key: str,
    source: str = "test",
) -> dict[str, Any]:
    current = store.task_spec(task_id)
    assert current is not None
    spec = json.loads(json.dumps(current["spec"]))
    mutate(spec)
    return store.put_task_spec(
        spec,
        idempotency_key=key,
        expected_revision=current["revision"],
        source=source,
    )


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


def write_registry_bound_manifest(
    path: Path,
    *,
    source_commit: str = DEPLOYED,
    registry_source_commit: str | None = None,
) -> dict[str, Any]:
    registry_root = path.parent / "registry-snapshot"
    registry_file = registry_root / "registry/tasks/FIXTURE.json"
    registry_file.parent.mkdir(parents=True, exist_ok=True)
    registry_file.write_text('{"id":"FIXTURE"}\n', encoding="utf-8")
    paths = [Path("registry/tasks/FIXTURE.json")]
    tree_sha256 = registry_snapshot.snapshot_tree_sha256(registry_root, paths)
    assert tree_sha256 is not None
    inventory = registry_root / ".bureau-runtime-snapshot.json"
    inventory.write_bytes(
        refresh.canonical_bytes(
            {
                "schema_version": 1,
                "kind": "bureau_registry_snapshot",
                "source_commit": registry_source_commit or source_commit,
                "tree_sha256": tree_sha256,
                "paths": [item.as_posix() for item in paths],
            }
        )
    )
    return write_manifest(
        path,
        source_commit=source_commit,
        canonical_registry_root=str(registry_root),
        canonical_registry_inventory_path=str(inventory),
        canonical_registry_inventory_sha256=refresh.sha256_bytes(inventory.read_bytes()),
        canonical_registry_tree_sha256=tree_sha256,
    )


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
            {
                "name": "registry-registration-preflight/freshness",
                "conclusion": "SUCCESS",
            },
        ],
    }


def github_fixture(
    *,
    main_commit: str = MAIN,
    second_main: str | None = None,
    detail: dict[str, Any] | None = None,
    associated: list[dict[str, Any]] | None = None,
    ahead_by: int = 1,
    compare_status: str = "ahead",
    behind_by: int = 0,
    merge_base_commit: str = DEPLOYED,
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
            return {
                "status": compare_status,
                "ahead_by": ahead_by,
                "behind_by": behind_by,
                "merge_base_commit": {"sha": merge_base_commit},
            }
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


def test_observe_requires_registry_freshness_by_default(tmp_path: Path) -> None:
    detail = green_pr_detail()
    detail["statusCheckRollup"] = [
        {"name": "validate (3.10)", "conclusion": "SUCCESS"},
        {"name": "validate (3.12)", "conclusion": "SUCCESS"},
    ]

    result, _ = candidate(tmp_path, detail=detail)

    assert result["status"] == "blocked"
    assert result["reason_codes"] == ["required-ci-not-green"]
    assert result["required_checks"] == list(refresh.DEFAULT_REQUIRED_CHECKS)
    assert (
        result["check_summary"]["registry-registration-preflight/freshness"]["state"]
        == "missing"
    )


def _compare_command_error(*, status: int) -> refresh.RuntimeRefreshError:
    message = "Not Found" if status == 404 else "Service Unavailable"
    return refresh.RuntimeRefreshError(
        "command-failed",
        "command failed: gh",
        details={
            "argv": [
                "gh",
                "api",
                f"repos/heimgewebe/bureau/compare/{DEPLOYED}...{MAIN}",
            ],
            "returncode": 1,
            "stdout": json.dumps({"message": message, "status": str(status)}),
            "stderr": f"gh: {message} (HTTP {status})",
        },
    )


def test_observe_falls_back_to_bounded_first_parent_walk_on_compare_404(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "prefix/deployment-manifest.json"
    write_manifest(manifest_path)
    base_github, calls = github_fixture()

    def github(arguments: list[str]) -> Any:
        joined = " ".join(arguments)
        if joined == f"api repos/heimgewebe/bureau/compare/{DEPLOYED}...{MAIN}":
            calls.append(arguments)
            raise _compare_command_error(status=404)
        if joined == f"api repos/heimgewebe/bureau/commits/{MAIN}":
            calls.append(arguments)
            return {"sha": MAIN, "parents": [{"sha": DEPLOYED}]}
        return base_github(arguments)

    result = refresh.observe_runtime_refresh(
        repository="heimgewebe/bureau",
        manifest_path=manifest_path,
        now=NOW,
        github=github,
    )

    assert result["status"] == "candidate"
    assert result["lag_commits"] == 1
    assert ["api", f"repos/heimgewebe/bureau/commits/{MAIN}"] in calls


def test_first_parent_fallback_counts_multiple_fresh_commit_objects() -> None:
    middle = "c" * 40
    commits = {
        MAIN: {"sha": MAIN, "parents": [{"sha": middle}]},
        middle: {"sha": middle, "parents": [{"sha": DEPLOYED}]},
    }
    calls: list[list[str]] = []

    def github(arguments: list[str]) -> Any:
        calls.append(arguments)
        commit = arguments[-1].rsplit("/", 1)[-1]
        return commits[commit]

    assert (
        refresh._first_parent_lag_commits(
            repository="heimgewebe/bureau",
            deployed=DEPLOYED,
            main_commit=MAIN,
            github=github,
        )
        == 2
    )
    assert len(calls) == 2


def test_first_parent_fallback_rejects_cycle_and_budget_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    middle = "c" * 40
    cycle = {
        MAIN: {"sha": MAIN, "parents": [{"sha": middle}]},
        middle: {"sha": middle, "parents": [{"sha": MAIN}]},
    }

    def cyclic_github(arguments: list[str]) -> Any:
        commit = arguments[-1].rsplit("/", 1)[-1]
        return cycle[commit]

    assert (
        refresh._first_parent_lag_commits(
            repository="heimgewebe/bureau",
            deployed=DEPLOYED,
            main_commit=MAIN,
            github=cyclic_github,
        )
        is None
    )

    monkeypatch.setattr(refresh, "MAX_GITHUB_FIRST_PARENT_LAG_COMMITS", 1)
    assert (
        refresh._first_parent_lag_commits(
            repository="heimgewebe/bureau",
            deployed=DEPLOYED,
            main_commit=MAIN,
            github=cyclic_github,
        )
        is None
    )


def test_observe_compare_404_fallback_fails_closed_on_unusable_parent(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "prefix/deployment-manifest.json"
    write_manifest(manifest_path)
    base_github, calls = github_fixture()

    def github(arguments: list[str]) -> Any:
        joined = " ".join(arguments)
        if joined == f"api repos/heimgewebe/bureau/compare/{DEPLOYED}...{MAIN}":
            calls.append(arguments)
            raise _compare_command_error(status=404)
        if joined == f"api repos/heimgewebe/bureau/commits/{MAIN}":
            calls.append(arguments)
            return {"sha": MAIN, "parents": []}
        return base_github(arguments)

    result = refresh.observe_runtime_refresh(
        repository="heimgewebe/bureau",
        manifest_path=manifest_path,
        now=NOW,
        github=github,
    )

    assert result["status"] == "blocked"
    assert result["lag_commits"] is None
    assert "commit-lag-unavailable" in result["reason_codes"]


@pytest.mark.parametrize(
    ("compare_status", "ahead_by", "behind_by", "merge_base_commit"),
    [
        ("diverged", 2, 1, "c" * 40),
        ("behind", 0, 2, "c" * 40),
        ("ahead", 2, 0, "c" * 40),
    ],
)
def test_observe_blocks_when_deployed_source_is_not_proven_main_ancestor(
    tmp_path: Path,
    compare_status: str,
    ahead_by: int,
    behind_by: int,
    merge_base_commit: str,
) -> None:
    value, _manifest = candidate(
        tmp_path,
        compare_status=compare_status,
        ahead_by=ahead_by,
        behind_by=behind_by,
        merge_base_commit=merge_base_commit,
    )

    assert value["status"] == "blocked"
    assert value["lag_commits"] is None
    assert "deployed-source-not-main-ancestor" in value["reason_codes"]
    assert value["source_ancestry"]["status"] == "rejected"


def test_observe_binds_manifest_to_canonical_registry_source(tmp_path: Path) -> None:
    manifest_path = tmp_path / "prefix/deployment-manifest.json"
    write_registry_bound_manifest(manifest_path)
    github, _calls = github_fixture()

    value = refresh.observe_runtime_refresh(
        repository="heimgewebe/bureau",
        manifest_path=manifest_path,
        now=NOW,
        github=github,
    )

    assert value["status"] == "candidate"
    assert value["source_ancestry"]["status"] == "proven"
    assert value["runtime_source_identity"] == {
        "schema_version": 1,
        "status": "proven",
        "deployed_source_commit": DEPLOYED,
        "registry_source_commit": DEPLOYED,
        "registry_reasons": [],
    }


def test_source_precondition_evidence_does_not_change_target_identity() -> None:
    base = {
        "repository": "heimgewebe/bureau",
        "main_commit": MAIN,
        "pull_request": {"number": 42},
        "merged_at": "2026-07-14T07:30:00Z",
        "required_checks": list(refresh.DEFAULT_REQUIRED_CHECKS),
        "check_summary": {},
        "deployed_source_commit": DEPLOYED,
        "deployed_manifest_sha256": "a" * 64,
        "lag_commits": 1,
        "scheduler_target_state": "source-not-current",
        "source_ancestry": {"status": "proven", "merge_base_commit": DEPLOYED},
        "runtime_source_identity": {"status": "proven", "registry_source_commit": DEPLOYED},
    }
    changed_evidence = json.loads(json.dumps(base))
    changed_evidence["source_ancestry"] = {"status": "rejected", "merge_base_commit": "c" * 40}
    changed_evidence["runtime_source_identity"] = {
        "status": "invalid",
        "registry_source_commit": "d" * 40,
    }

    assert refresh._target_payload(base) == refresh._target_payload(changed_evidence)
    assert "source_ancestry" not in refresh._target_payload(base)
    assert "runtime_source_identity" not in refresh._target_payload(base)


def test_observe_marks_registry_source_mismatch_invalid_without_affecting_legacy_authority(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "prefix/deployment-manifest.json"
    write_registry_bound_manifest(manifest_path, registry_source_commit="c" * 40)
    github, _calls = github_fixture()

    value = refresh.observe_runtime_refresh(
        repository="heimgewebe/bureau",
        manifest_path=manifest_path,
        now=NOW,
        github=github,
    )

    assert value["status"] == "candidate"
    assert value["runtime_source_identity"]["status"] == "invalid"
    assert "source-commit-mismatch" in value["runtime_source_identity"]["registry_reasons"]


def test_observe_does_not_fallback_for_non_404_compare_failure(tmp_path: Path) -> None:
    manifest_path = tmp_path / "prefix/deployment-manifest.json"
    write_manifest(manifest_path)
    base_github, _ = github_fixture()

    def github(arguments: list[str]) -> Any:
        joined = " ".join(arguments)
        if joined == f"api repos/heimgewebe/bureau/compare/{DEPLOYED}...{MAIN}":
            raise _compare_command_error(status=503)
        return base_github(arguments)

    with pytest.raises(refresh.RuntimeRefreshError, match="command failed: gh") as caught:
        refresh.observe_runtime_refresh(
            repository="heimgewebe/bureau",
            manifest_path=manifest_path,
            now=NOW,
            github=github,
        )

    assert caught.value.code == "command-failed"


def prepare_candidate_intent(
    tmp_path: Path,
    *,
    task_id: str = "BUR-2026-003-T009",
) -> tuple[dict[str, Any], Path, dict[str, Any], Path]:
    observed, manifest_path = candidate(tmp_path)
    state_root = (tmp_path / "state").resolve()
    authority_store = seed_authority_store(tmp_path / "bureau-state", task_id)
    intent, intent_path = refresh.prepare_intent(
        candidate=observed,
        state_root=state_root,
        prefix=(tmp_path / "prefix").resolve(),
        bin_dir=(tmp_path / "bin").resolve(),
        user_unit_dir=(tmp_path / "systemd/user").resolve(),
        libexec_dir=(tmp_path / "libexec").resolve(),
        remote_url="file:///tmp/bureau.git",
        authorized_by="chatgpt",
        authorization="User explicitly authorized T016 implementation.",
        break_glass=True,
        approval_reference=observed["target_sha256"],
        approval_task_id=task_id,
        now=NOW,
        authority_store=authority_store,
    )
    return observed, manifest_path, intent, intent_path


def prepare_source_precondition_intent(
    tmp_path: Path,
    *,
    task_id: str = "BUREAU-SOURCE-PRECONDITION-TEST",
) -> tuple[dict[str, Any], Path, dict[str, Any], Path]:
    manifest_path = tmp_path / "prefix/deployment-manifest.json"
    write_registry_bound_manifest(manifest_path)
    github, _calls = github_fixture()
    observed = refresh.observe_runtime_refresh(
        repository="heimgewebe/bureau",
        manifest_path=manifest_path,
        now=NOW,
        github=github,
    )
    state_root = (tmp_path / "state").resolve()
    authority_store = seed_authority_store(
        tmp_path / "bureau-state", task_id, source_precondition=True
    )
    intent, intent_path = refresh.prepare_intent(
        candidate=observed,
        state_root=state_root,
        prefix=(tmp_path / "prefix").resolve(),
        bin_dir=(tmp_path / "bin").resolve(),
        user_unit_dir=(tmp_path / "systemd/user").resolve(),
        libexec_dir=(tmp_path / "libexec").resolve(),
        remote_url="file:///tmp/bureau.git",
        authorized_by="chatgpt",
        authorization="User explicitly authorized source-precondition test.",
        break_glass=True,
        approval_reference=observed["target_sha256"],
        approval_task_id=task_id,
        now=NOW,
        authority_store=authority_store,
        observer=lambda **_: observed,
    )
    return observed, manifest_path, intent, intent_path


def test_registry_source_precondition_authorities_use_source_precondition_mode() -> None:
    registry_root = Path(__file__).resolve().parents[1] / "registry" / "tasks"
    violations: list[tuple[str, str | None]] = []
    observed = 0
    for path in sorted(registry_root.glob("*.json")):
        spec = json.loads(path.read_text(encoding="utf-8"))
        authority = spec.get("metadata", {}).get("runtime_refresh_authority", {})
        if authority.get("source_precondition") is None:
            continue
        observed += 1
        if authority.get("mode") != refresh.RUNTIME_AUTHORITY_MODE_SOURCE_PRECONDITION:
            violations.append((path.name, authority.get("mode")))

    assert observed >= 1
    assert violations == []


def test_legacy_authority_mode_rejects_new_source_precondition_generation(
    tmp_path: Path,
) -> None:
    task_id = "BUREAU-RUNTIME-AUTHORITY-LEGACY-MODE-SOURCE-PRECONDITION"
    state_root = tmp_path / "bureau-state"
    store = StateStore(state_root / "bureau.sqlite3", state_root)
    spec = runtime_authority_spec(task_id)
    spec["metadata"]["runtime_refresh_authority"]["source_precondition"] = (
        source_precondition_contract()
    )
    store.put_task_spec(
        spec,
        idempotency_key="seed:legacy-source-precondition-generation",
        expected_revision=None,
        source="test",
    )

    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.validate_authoritative_runtime_refresh_task(
            store=store,
            approval_task_id=task_id,
            target_sha256="a" * 64,
        )

    assert caught.value.code == "authority-contract-generation-invalid"
    assert caught.value.details["required_mode"] == (
        refresh.RUNTIME_AUTHORITY_MODE_SOURCE_PRECONDITION
    )


def test_historical_intent_without_generation_cannot_bind_or_consume_new_authority(
    tmp_path: Path,
) -> None:
    _observed, _manifest_path, current_intent, _intent_path = (
        prepare_source_precondition_intent(
            tmp_path, task_id="BUREAU-SOURCE-PRECONDITION-HISTORICAL-RUNNER"
        )
    )
    store = authority_store_for_intent(current_intent)
    historical_intent = json.loads(json.dumps(current_intent))
    historical_intent["authority_task_spec"].pop("contract_mode")
    historical_intent.pop("source_precondition")
    historical_intent = refresh.bind_digest(historical_intent, "intent_sha256")
    before = store.task_spec(historical_intent["approval_task_id"])
    assert before is not None

    with pytest.raises(refresh.RuntimeRefreshError) as bind_error:
        refresh.bind_runtime_refresh_authority(
            store=store, intent=historical_intent, now=NOW
        )
    assert bind_error.value.code == "authority-runner-contract-generation-missing"
    assert store.task_spec(historical_intent["approval_task_id"]) == before

    baseline = historical_intent["authority_task_spec"]
    binding = {
        "schema_version": refresh.RUNTIME_AUTHORITY_SCHEMA_VERSION,
        "kind": refresh.RUNTIME_AUTHORITY_BINDING_KIND,
        "task_id": historical_intent["approval_task_id"],
        "authority_revision": baseline["revision"],
        "authority_spec_sha256": baseline["spec_sha256"],
        "target_sha256": historical_intent["target_sha256"],
        "intent_sha256": historical_intent["intent_sha256"],
        "bound_at": refresh.isoformat(NOW),
    }

    def simulate_historical_binding(spec: dict[str, Any]) -> None:
        spec["metadata"]["runtime_refresh_authority"]["target_binding_receipt"] = binding

    changed = revise_authority(
        store,
        historical_intent["approval_task_id"],
        simulate_historical_binding,
        key="simulate:historical-runner-binding",
    )
    bound_authority = {
        "task_id": historical_intent["approval_task_id"],
        "revision": changed["revision"],
        "spec_sha256": changed["spec_sha256"],
        "authority_revision": baseline["revision"],
        "authority_spec_sha256": baseline["spec_sha256"],
        "target_binding_receipt": binding,
    }
    result = refresh.bind_digest(
        {
            "schema_version": refresh.SCHEMA_VERSION,
            "kind": "bureau_runtime_refresh_result",
            "status": "deployed",
            "intent_sha256": historical_intent["intent_sha256"],
            "target_sha256": historical_intent["target_sha256"],
            "main_commit": historical_intent["main_commit"],
            "authority_task_spec": bound_authority,
            "finished_at": refresh.isoformat(NOW),
            "effect_started": True,
            "lease_binding": {"lease_binding_sha256": "a" * 64},
        },
        "result_sha256",
    )
    before_consumption = store.task_spec(historical_intent["approval_task_id"])

    with pytest.raises(refresh.RuntimeRefreshError) as consumption_error:
        refresh.consume_runtime_refresh_authority(
            store=store, intent=historical_intent, result=result, now=NOW
        )

    assert consumption_error.value.code == "authority-runner-contract-generation-missing"
    assert store.task_spec(historical_intent["approval_task_id"]) == before_consumption


def test_prepare_intent_rejects_declared_source_precondition_without_registry_identity(
    tmp_path: Path,
) -> None:
    observed, _manifest_path = candidate(tmp_path)
    task_id = "BUREAU-SOURCE-PRECONDITION-MISSING-IDENTITY"
    store = seed_authority_store(tmp_path / "bureau-state", task_id, source_precondition=True)

    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.prepare_intent(
            candidate=observed,
            state_root=(tmp_path / "state").resolve(),
            prefix=(tmp_path / "prefix").resolve(),
            bin_dir=(tmp_path / "bin").resolve(),
            user_unit_dir=(tmp_path / "systemd/user").resolve(),
            libexec_dir=(tmp_path / "libexec").resolve(),
            remote_url="file:///tmp/bureau.git",
            authorized_by="chatgpt",
            authorization="User explicitly authorized source-precondition test.",
            break_glass=True,
            approval_reference=observed["target_sha256"],
            approval_task_id=task_id,
            now=NOW,
            authority_store=store,
            observer=lambda **_: observed,
        )

    assert caught.value.code == "runtime-source-identity-unproven"


def test_prepare_intent_binds_enforced_source_precondition(tmp_path: Path) -> None:
    observed, _manifest_path, intent, _intent_path = prepare_source_precondition_intent(tmp_path)

    assert observed["source_ancestry"]["status"] == "proven"
    assert observed["runtime_source_identity"]["status"] == "proven"
    assert intent["source_precondition"] == source_precondition_contract()


def test_prepare_intent_rebinds_fresh_source_precondition_observation(tmp_path: Path) -> None:
    manifest_path = tmp_path / "prefix/deployment-manifest.json"
    write_registry_bound_manifest(manifest_path)
    github, _calls = github_fixture()
    observed = refresh.observe_runtime_refresh(
        repository="heimgewebe/bureau",
        manifest_path=manifest_path,
        now=NOW,
        github=github,
    )
    fresh = json.loads(json.dumps(observed))
    fresh["observed_at"] = refresh.isoformat(NOW + timedelta(seconds=5))
    fresh.pop("observation_sha256", None)
    fresh = refresh.bind_digest(fresh, "observation_sha256")
    assert fresh["target_sha256"] == observed["target_sha256"]
    assert fresh["observation_sha256"] != observed["observation_sha256"]
    task_id = "BUREAU-SOURCE-PRECONDITION-FRESH-OBSERVATION"
    store = seed_authority_store(tmp_path / "bureau-state", task_id, source_precondition=True)

    intent, _intent_path = refresh.prepare_intent(
        candidate=observed,
        state_root=(tmp_path / "state").resolve(),
        prefix=(tmp_path / "prefix").resolve(),
        bin_dir=(tmp_path / "bin").resolve(),
        user_unit_dir=(tmp_path / "systemd/user").resolve(),
        libexec_dir=(tmp_path / "libexec").resolve(),
        remote_url="file:///tmp/bureau.git",
        authorized_by="chatgpt",
        authorization="User explicitly authorized source-precondition test.",
        break_glass=True,
        approval_reference=observed["target_sha256"],
        approval_task_id=task_id,
        now=NOW,
        authority_store=store,
        observer=lambda **_: fresh,
    )

    assert intent["observation_sha256"] == fresh["observation_sha256"]
    assert intent["expected_manifest_sha256"] == fresh["deployed_manifest_sha256"]


def test_prepare_intent_rejects_fresh_target_drift(tmp_path: Path) -> None:
    manifest_path = tmp_path / "prefix/deployment-manifest.json"
    write_registry_bound_manifest(manifest_path)
    github, _calls = github_fixture()
    observed = refresh.observe_runtime_refresh(
        repository="heimgewebe/bureau",
        manifest_path=manifest_path,
        now=NOW,
        github=github,
    )
    fresh = json.loads(json.dumps(observed))
    fresh["main_commit"] = "9" * 40
    fresh["source_ancestry"] = {
        **fresh["source_ancestry"],
        "main_commit": "9" * 40,
    }
    fresh["target_sha256"] = refresh.sha256_bytes(
        refresh.canonical_bytes(refresh._target_payload(fresh))
    )
    fresh.pop("observation_sha256", None)
    fresh = refresh.bind_digest(fresh, "observation_sha256")
    task_id = "BUREAU-SOURCE-PRECONDITION-FRESH-TARGET-DRIFT"
    store = seed_authority_store(tmp_path / "bureau-state", task_id, source_precondition=True)
    state_root = (tmp_path / "state").resolve()

    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.prepare_intent(
            candidate=observed,
            state_root=state_root,
            prefix=(tmp_path / "prefix").resolve(),
            bin_dir=(tmp_path / "bin").resolve(),
            user_unit_dir=(tmp_path / "systemd/user").resolve(),
            libexec_dir=(tmp_path / "libexec").resolve(),
            remote_url="file:///tmp/bureau.git",
            authorized_by="chatgpt",
            authorization="User explicitly authorized source-precondition test.",
            break_glass=True,
            approval_reference=observed["target_sha256"],
            approval_task_id=task_id,
            now=NOW,
            authority_store=store,
            observer=lambda **_: fresh,
        )

    assert caught.value.code == "source-precondition-target-drift"
    assert not (state_root / "intents").exists()


def test_prepare_intent_uses_explicit_manifest_for_fresh_source_precondition_observation(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "custom-runtime/active-manifest.json"
    write_registry_bound_manifest(manifest_path)
    github, _calls = github_fixture()
    observed = refresh.observe_runtime_refresh(
        repository="heimgewebe/bureau",
        manifest_path=manifest_path,
        now=NOW,
        github=github,
    )
    task_id = "BUREAU-SOURCE-PRECONDITION-CUSTOM-MANIFEST"
    store = seed_authority_store(tmp_path / "bureau-state", task_id, source_precondition=True)
    observed_paths: list[Path] = []

    def observer(**kwargs: Any) -> dict[str, Any]:
        observed_paths.append(kwargs["manifest_path"])
        return observed

    intent, _intent_path = refresh.prepare_intent(
        candidate=observed,
        state_root=(tmp_path / "state").resolve(),
        prefix=(tmp_path / "different-prefix").resolve(),
        bin_dir=(tmp_path / "bin").resolve(),
        user_unit_dir=(tmp_path / "systemd/user").resolve(),
        libexec_dir=(tmp_path / "libexec").resolve(),
        remote_url="file:///tmp/bureau.git",
        authorized_by="chatgpt",
        authorization="User explicitly authorized source-precondition test.",
        break_glass=True,
        approval_reference=observed["target_sha256"],
        approval_task_id=task_id,
        now=NOW,
        authority_store=store,
        manifest_path=manifest_path,
        observer=observer,
    )

    assert observed_paths == [manifest_path.resolve()]
    assert intent["observation_sha256"] == observed["observation_sha256"]


def test_main_prepare_intent_forwards_global_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text("{}\n", encoding="utf-8")
    manifest_path = tmp_path / "custom-runtime/active-manifest.json"
    captured: dict[str, Any] = {}

    def fake_prepare_intent(**kwargs: Any) -> tuple[dict[str, Any], Path]:
        captured.update(kwargs)
        return {"kind": "stub-intent"}, tmp_path / "intent.json"

    monkeypatch.setattr(refresh, "prepare_intent", fake_prepare_intent)

    assert (
        refresh.main(
            [
                "--state-root",
                str(tmp_path / "state"),
                "--manifest",
                str(manifest_path),
                "prepare-intent",
                "--candidate",
                str(candidate_path),
                "--authorized-by",
                "chatgpt",
                "--authorization",
                "Explicit custom manifest forwarding test.",
                "--approval-reference",
                "a" * 64,
                "--approval-task-id",
                "BUREAU-CUSTOM-MANIFEST-CLI",
            ]
        )
        == 0
    )
    assert captured["manifest_path"] == manifest_path.resolve()


def test_apply_persists_source_precondition_proof_before_source_preparation(
    tmp_path: Path,
) -> None:
    observed, manifest_path, intent, intent_path = prepare_source_precondition_intent(tmp_path)
    binding, resource_db = lease_for(tmp_path / "leases", intent)
    state_root = Path(intent["state_root"])
    expected_path = (
        state_root
        / "source-precondition-observations"
        / f"{intent['intent_sha256']}-{observed['observation_sha256']}.json"
    )

    def source_preparer(**_: Any) -> dict[str, Any]:
        assert refresh.read_json(expected_path) == observed
        raise refresh.RuntimeRefreshError(
            "stop-after-source-precondition-proof",
            "test stops after durable pre-effect source proof",
        )

    result = refresh.apply_runtime_refresh(
        intent_path=intent_path,
        lease_binding=binding,
        manifest_path=manifest_path,
        state_root=state_root,
        resource_db=resource_db,
        now=NOW,
        observer=lambda **_: observed,
        source_preparer=source_preparer,
        installer=lambda **_: pytest.fail("installer must not start"),
        readback=lambda **_: pytest.fail("readback must not start"),
    )

    proof = result["source_precondition_evidence"]
    assert result["status"] == "failed"
    assert result["effect_started"] is False
    assert proof["observation_sha256"] == observed["observation_sha256"]
    assert proof["observation_path"] == str(expected_path)
    assert proof["source_ancestry"] == observed["source_ancestry"]
    assert proof["runtime_source_identity"] == observed["runtime_source_identity"]
    started = refresh.read_json(
        state_root / "attempts" / intent["target_sha256"] / "started.json"
    )
    assert started["source_precondition_evidence"] == proof


def test_apply_deployed_result_binds_source_precondition_proof(tmp_path: Path) -> None:
    observed, manifest_path, intent, intent_path = prepare_source_precondition_intent(tmp_path)
    binding, resource_db = lease_for(tmp_path / "deployed-proof-leases", intent)
    state_root = Path(intent["state_root"])

    def source_preparer(**kwargs: Any) -> dict[str, Any]:
        kwargs["workspace"].mkdir(parents=True)
        return {
            "head": MAIN,
            "root": str(kwargs["workspace"]),
            "dirty": False,
            "detached": True,
        }

    result = refresh.apply_runtime_refresh(
        intent_path=intent_path,
        lease_binding=binding,
        manifest_path=manifest_path,
        state_root=state_root,
        resource_db=resource_db,
        now=NOW,
        observer=lambda **_: observed,
        source_preparer=source_preparer,
        installer=lambda **_: {
            "manifest_sha256": "a" * 64,
            "rollback": {"directory": "/rollback"},
        },
        readback=lambda **_: {
            "source_commit": MAIN,
            "manifest_sha256": "a" * 64,
            "check_valid": True,
            "runtime_identity_valid": True,
        },
    )

    proof = result["source_precondition_evidence"]
    assert result["status"] == "deployed"
    assert result["effect_started"] is True
    assert proof["observation_sha256"] == observed["observation_sha256"]
    assert refresh.read_json(Path(proof["observation_path"])) == observed
    started = refresh.read_json(
        state_root / "attempts" / intent["target_sha256"] / "started.json"
    )
    assert started["source_precondition_evidence"] == proof

    proof_path = Path(proof["observation_path"])
    proof_path.unlink()
    with pytest.raises(refresh.RuntimeRefreshError) as missing:
        refresh.apply_runtime_refresh(
            intent_path=intent_path,
            lease_binding=binding,
            manifest_path=manifest_path,
            state_root=state_root,
            resource_db=resource_db,
            now=NOW,
            observer=lambda **_: pytest.fail("result replay must not re-observe"),
            source_preparer=lambda **_: pytest.fail("result replay must not prepare source"),
            installer=lambda **_: pytest.fail("result replay must not install"),
            readback=lambda **_: pytest.fail("result replay must not read back effects"),
        )
    assert missing.value.code == "source-precondition-result-evidence-invalid"

    tampered = json.loads(json.dumps(observed))
    tampered["observed_at"] = refresh.isoformat(NOW + timedelta(seconds=1))
    tampered.pop("observation_sha256", None)
    tampered = refresh.bind_digest(tampered, "observation_sha256")
    assert tampered["observation_sha256"] != observed["observation_sha256"]
    refresh.create_only(proof_path, refresh.canonical_bytes(tampered))
    with pytest.raises(refresh.RuntimeRefreshError) as replaced:
        refresh.apply_runtime_refresh(
            intent_path=intent_path,
            lease_binding=binding,
            manifest_path=manifest_path,
            state_root=state_root,
            resource_db=resource_db,
            now=NOW,
            observer=lambda **_: pytest.fail("result replay must not re-observe"),
            source_preparer=lambda **_: pytest.fail("result replay must not prepare source"),
            installer=lambda **_: pytest.fail("result replay must not install"),
            readback=lambda **_: pytest.fail("result replay must not read back effects"),
        )
    assert replaced.value.code == "source-precondition-result-evidence-invalid"


def test_apply_already_current_result_binds_source_precondition_proof(tmp_path: Path) -> None:
    observed, manifest_path, intent, intent_path = prepare_source_precondition_intent(tmp_path)
    write_registry_bound_manifest(manifest_path, source_commit=MAIN)
    scheduler = {
        "schema_version": refresh.SCHEMA_VERSION,
        "kind": "bureau_runtime_scheduler_readback",
        "source_commit": MAIN,
        "authoritative": True,
    }
    live = dict(observed)
    live.update(
        {
            "status": "already_current",
            "deployed_source_commit": MAIN,
            "deployed_manifest_sha256": refresh.sha256_bytes(manifest_path.read_bytes()),
            "main_commit": MAIN,
            "lag_commits": 0,
            "scheduler_target_state": "converged",
            "reason_codes": [],
            "scheduler": scheduler,
            "source_ancestry": {
                "schema_version": 1,
                "status": "proven",
                "method": "same-commit",
                "deployed_source_commit": MAIN,
                "main_commit": MAIN,
                "compare_status": "identical",
                "ahead_by": 0,
                "behind_by": 0,
                "merge_base_commit": MAIN,
            },
            "runtime_source_identity": {
                "schema_version": 1,
                "status": "proven",
                "deployed_source_commit": MAIN,
                "registry_source_commit": MAIN,
                "registry_reasons": [],
            },
        }
    )
    live["target_sha256"] = refresh.sha256_bytes(
        refresh.canonical_bytes(refresh._target_payload(live))
    )
    live.pop("observation_sha256", None)
    live = refresh.bind_digest(live, "observation_sha256")
    binding, resource_db = lease_for(tmp_path / "already-current-proof-leases", intent)

    result = refresh.apply_runtime_refresh(
        intent_path=intent_path,
        lease_binding=binding,
        manifest_path=manifest_path,
        state_root=Path(intent["state_root"]),
        resource_db=resource_db,
        now=NOW,
        observer=lambda **_: live,
        source_preparer=lambda **_: pytest.fail("already-current must not prepare source"),
        installer=lambda **_: pytest.fail("already-current must not install"),
        readback=lambda **_: pytest.fail("already-current must not read back installer effects"),
    )

    proof = result["source_precondition_evidence"]
    assert result["status"] == "already_current"
    assert result["effect_started"] is False
    assert proof["observation_sha256"] == live["observation_sha256"]
    proof_path = Path(proof["observation_path"])
    assert refresh.read_json(proof_path) == live

    proof_path.unlink()
    with pytest.raises(refresh.RuntimeRefreshError) as missing:
        refresh.apply_runtime_refresh(
            intent_path=intent_path,
            lease_binding=binding,
            manifest_path=manifest_path,
            state_root=Path(intent["state_root"]),
            resource_db=resource_db,
            now=NOW,
            observer=lambda **_: pytest.fail("no-effect replay must not re-observe"),
            source_preparer=lambda **_: pytest.fail("no-effect replay must not prepare source"),
            installer=lambda **_: pytest.fail("no-effect replay must not install"),
            readback=lambda **_: pytest.fail("no-effect replay must not read back effects"),
        )
    assert missing.value.code == "source-precondition-result-evidence-invalid"


def test_apply_rejects_fresh_diverged_source_before_effect(tmp_path: Path) -> None:
    observed, manifest_path, intent, intent_path = prepare_source_precondition_intent(tmp_path)
    binding, resource_db = lease_for(tmp_path / "leases", intent)
    live = json.loads(json.dumps(observed))
    live["source_ancestry"] = {
        **live["source_ancestry"],
        "status": "rejected",
        "method": "github-compare",
        "compare_status": "diverged",
        "ahead_by": 2,
        "behind_by": 1,
        "merge_base_commit": "c" * 40,
    }
    live["target_sha256"] = refresh.sha256_bytes(
        refresh.canonical_bytes(refresh._target_payload(live))
    )
    live.pop("observation_sha256", None)
    live = refresh.bind_digest(live, "observation_sha256")

    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.apply_runtime_refresh(
            intent_path=intent_path,
            lease_binding=binding,
            manifest_path=manifest_path,
            state_root=Path(intent["state_root"]),
            resource_db=resource_db,
            now=NOW,
            observer=lambda **_: live,
            source_preparer=lambda **_: pytest.fail("source preparation must not start"),
            installer=lambda **_: pytest.fail("installer must not start"),
            readback=lambda **_: pytest.fail("readback must not start"),
        )

    assert caught.value.code == "source-ancestry-unproven"


def lease_for(
    root: Path,
    intent: dict[str, Any],
    *,
    schema_version: str = "1",
    lease_contract_version: str | None = "1",
    owner_id: str = "chatgpt-t016",
    expires_at: datetime | None = None,
    omit: set[str] | None = None,
    metadata: dict[str, Any] | None = None,
    metadata_digest: str | None = None,
    current: datetime = NOW,
) -> tuple[dict[str, Any], Path]:
    database = root / "resources.sqlite3"
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute(
        "INSERT INTO metadata(key, value) VALUES('schema_version', ?)",
        (schema_version,),
    )
    if lease_contract_version is not None:
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES('resource_lease_contract_version', ?)",
            (lease_contract_version,),
        )
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
    acquired = int((current - timedelta(minutes=1)).timestamp())
    expiry = int((expires_at or current + timedelta(hours=1)).timestamp())
    omitted = omit or set()
    lease_metadata = metadata or {}
    metadata_json = json.dumps(
        lease_metadata,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = metadata_digest or hashlib.sha256(metadata_json.encode("utf-8")).hexdigest()
    for key in intent["required_resource_keys"]:
        if key in omitted:
            continue
        connection.execute(
            "INSERT INTO leases VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (key, owner_id, "test", acquired, acquired, expiry, digest, metadata_json),
        )
    connection.commit()
    connection.close()
    database.chmod(0o600)
    return {"owner_id": owner_id, "task_id": "grabowski-task-t016"}, database


def release_test_leases(database: Path) -> None:
    connection = sqlite3.connect(database)
    connection.execute("DELETE FROM leases")
    connection.commit()
    connection.close()


def prepare_legacy_cutover(
    tmp_path: Path,
    *,
    current: datetime = NOW,
) -> tuple[dict[str, Any], Path, Path, Path, dict[str, Any]]:
    _, manifest_path, typed_intent, _ = prepare_candidate_intent(tmp_path)
    legacy = dict(typed_intent)
    legacy.pop("runtime_approval")
    legacy.pop("approval_task_id")
    legacy["created_at"] = refresh.isoformat(current)
    legacy["expires_at"] = refresh.isoformat(current + timedelta(minutes=15))
    legacy["nonce"] = "legacy-cutover-test"
    legacy = refresh.bind_digest(legacy, "intent_sha256")
    intent_path = Path(legacy["state_root"]) / "intents" / f"{legacy['intent_sha256']}.json"
    refresh.create_only(intent_path, refresh.canonical_bytes(legacy))
    binding, resource_db = lease_for(tmp_path / "legacy-leases", legacy, current=current)
    normalized = refresh.validate_live_lease_binding(
        legacy, binding, resource_db=resource_db, now=current
    )
    started = refresh.bind_digest(
        {
            "schema_version": refresh.SCHEMA_VERSION,
            "kind": "bureau_runtime_refresh_attempt_start",
            "intent_sha256": legacy["intent_sha256"],
            "target_sha256": legacy["target_sha256"],
            "main_commit": legacy["main_commit"],
            "lease_binding": normalized,
            "started_at": refresh.isoformat(current),
            "effect_started": False,
        },
        "start_sha256",
    )
    refresh.create_only(
        Path(legacy["state_root"]) / "attempts" / legacy["target_sha256"] / "started.json",
        refresh.canonical_bytes(started),
    )
    return legacy, intent_path, manifest_path, resource_db, started


def apply_successfully(
    tmp_path: Path,
    *,
    task_id: str = "BUR-2026-003-T009",
) -> tuple[
    dict[str, Any],
    Path,
    dict[str, Any],
    Path,
    StateStore,
    dict[str, Any],
    Path,
]:
    observed, manifest_path, intent, intent_path = prepare_candidate_intent(
        tmp_path, task_id=task_id
    )
    binding, resource_db = lease_for(tmp_path / "leases", intent)

    def source_preparer(**kwargs: Any) -> dict[str, Any]:
        kwargs["workspace"].mkdir(parents=True)
        return {
            "head": MAIN,
            "root": str(kwargs["workspace"]),
            "dirty": False,
            "detached": True,
        }

    def installer(**_: Any) -> dict[str, Any]:
        return {"manifest_sha256": "a" * 64, "rollback": {"directory": "/rollback"}}

    def readback(**_: Any) -> dict[str, Any]:
        return {
            "source_commit": MAIN,
            "manifest_sha256": "a" * 64,
            "check_valid": True,
            "runtime_identity_valid": True,
        }

    result = refresh.apply_runtime_refresh(
        intent_path=intent_path,
        lease_binding=binding,
        manifest_path=manifest_path,
        state_root=Path(intent["state_root"]),
        resource_db=resource_db,
        now=NOW,
        observer=lambda **_: observed,
        source_preparer=source_preparer,
        installer=installer,
        readback=readback,
    )
    return (
        observed,
        manifest_path,
        intent,
        intent_path,
        authority_store_for_intent(intent),
        result,
        resource_db,
    )


def historical_no_run_success(
    tmp_path: Path,
    *,
    task_id: str,
) -> tuple[dict[str, Any], StateStore, dict[str, Any], Path]:
    _, _, modern_intent, _ = prepare_candidate_intent(tmp_path, task_id=task_id)
    store = authority_store_for_intent(modern_intent)
    intent = dict(modern_intent)
    intent.pop("authority_state_store")
    intent.pop("authority_task_spec")
    intent["nonce"] = "historical-no-run-bootstrap"
    intent = refresh.bind_digest(intent, "intent_sha256")
    intent_path = Path(intent["state_root"]) / "intents" / f"{intent['intent_sha256']}.json"
    refresh.create_only(intent_path, refresh.canonical_bytes(intent))
    lease_binding, resource_db = lease_for(tmp_path / "historical-leases", intent)
    normalized_binding = refresh.validate_live_lease_binding(
        intent, lease_binding, resource_db=resource_db, now=NOW
    )
    started = refresh.bind_digest(
        {
            "schema_version": refresh.SCHEMA_VERSION,
            "kind": "bureau_runtime_refresh_attempt_start",
            "intent_sha256": intent["intent_sha256"],
            "target_sha256": intent["target_sha256"],
            "main_commit": intent["main_commit"],
            "lease_binding": normalized_binding,
            "started_at": refresh.isoformat(NOW),
            "effect_started": False,
        },
        "start_sha256",
    )
    attempt_dir = Path(intent["state_root"]) / "attempts" / intent["target_sha256"]
    refresh.create_only(attempt_dir / "started.json", refresh.canonical_bytes(started))
    readback = {
        "source_commit": intent["main_commit"],
        "manifest_sha256": "a" * 64,
        "check_valid": True,
        "runtime_identity_valid": True,
    }
    result = refresh._write_attempt_result(
        attempt_dir / "result.json",
        {
            "schema_version": refresh.SCHEMA_VERSION,
            "kind": "bureau_runtime_refresh_result",
            "status": "deployed",
            "intent_sha256": intent["intent_sha256"],
            "target_sha256": intent["target_sha256"],
            "main_commit": intent["main_commit"],
            "source_identity": {"head": intent["main_commit"]},
            "install_receipt": {
                "manifest_sha256": "a" * 64,
                "rollback": {"directory": "/rollback"},
            },
            "readback": readback,
            "lease_binding": normalized_binding,
            "finished_at": refresh.isoformat(NOW),
            "effect_started": True,
            "does_not_establish": [
                "future_runtime_health",
                "future_main_stability",
            ],
        },
    )
    return intent, store, result, resource_db


def historical_runtime_artifacts(
    tmp_path: Path,
    intent: dict[str, Any],
    *,
    scheduler: dict[str, Any] | None = None,
    runtime_scheduler_names: tuple[str, ...] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Path]]:
    prefix = Path(intent["prefix"])
    bin_dir = Path(intent["bin_dir"])
    manifest_path = prefix / "deployment-manifest.json"
    release_id = f"{intent['main_commit'][:12]}-srcfixture"
    release = prefix / "releases" / release_id
    module = release / "src/bureau/runtime_identity.py"
    module.parent.mkdir(parents=True)
    release_scheduler_names = runtime_scheduler_names or runtime_identity.SCHEDULER_NAMES
    module.write_text(
        f"MANAGED_PACKAGES = {runtime_identity.MANAGED_PACKAGES!r}\n"
        f"SCHEDULER_NAMES = {release_scheduler_names!r}\n"
        "RUNTIME_FIXTURE = True\n",
        encoding="utf-8",
    )
    cycle = release / "src/bureau_cycle"
    cycle.mkdir(parents=True)
    (cycle / "__init__.py").write_text("CYCLE_FIXTURE = True\n", encoding="utf-8")
    (release / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    schemas = release / "schemas"
    schemas.mkdir()
    (schemas / "task.json").write_text("{}\n", encoding="utf-8")
    systemd = release / "ops/systemd"
    libexec = systemd / "libexec"
    libexec.mkdir(parents=True)
    for name in release_scheduler_names:
        (systemd / f"{name}.service").write_text("[Service]\n", encoding="utf-8")
        (systemd / f"{name}.timer").write_text("[Timer]\n", encoding="utf-8")
        executable = libexec / name
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
    package_tree_sha256 = refresh._historical_package_tree_sha256(release)
    assert package_tree_sha256 is not None
    if release_scheduler_names == runtime_identity.SCHEDULER_NAMES:
        assert runtime_identity._package_tree_sha256(release) == package_tree_sha256
    else:
        assert runtime_identity._package_tree_sha256(release) is None

    registry_root = prefix / "registry-snapshots" / f"{intent['main_commit'][:12]}-fixture"
    registry_task = registry_root / "registry/tasks/FIXTURE.json"
    registry_task.parent.mkdir(parents=True)
    registry_task.write_text('{"id":"FIXTURE"}\n', encoding="utf-8")
    registry_paths = [Path("registry/tasks/FIXTURE.json")]
    registry_tree_sha256 = registry_snapshot.snapshot_tree_sha256(registry_root, registry_paths)
    assert registry_tree_sha256 is not None
    inventory = registry_root / ".bureau-runtime-snapshot.json"
    inventory_value = {
        "schema_version": 1,
        "kind": "bureau_registry_snapshot",
        "source_commit": intent["main_commit"],
        "tree_sha256": registry_tree_sha256,
        "paths": [path.as_posix() for path in registry_paths],
    }
    inventory.write_bytes(refresh.canonical_bytes(inventory_value))
    inventory_sha256 = refresh.sha256_bytes(inventory.read_bytes())

    bin_dir.mkdir(parents=True, exist_ok=True)
    launcher_contents = {
        "bureau": b"#!/bin/sh\necho historical-bureau\n",
        "bureau-runtime-refresh": b"#!/bin/sh\necho historical-refresh\n",
        "bureau-status-capsule": b"#!/bin/sh\necho historical-status\n",
    }
    for name, content in launcher_contents.items():
        path = bin_dir / name
        path.write_bytes(content)
        path.chmod(0o755)

    rollback = {
        "directory": str(prefix / "backups/pre-historical"),
        "manifest": None,
        "launcher": None,
        "runtime_refresh_launcher": None,
        "status_capsule_launcher": None,
    }
    runtime_approval = {
        "schema_version": 1,
        "allowed": True,
        "reference": intent["target_sha256"],
    }
    manifest = {
        "schema_version": 1,
        "kind": "bureau_runtime_deployment",
        "release_id": release_id,
        "source_repository": str(tmp_path / "source"),
        "source_commit": intent["main_commit"],
        "package_tree_sha256": package_tree_sha256,
        "immutable_release_path": str(release),
        "module_path": str(module),
        "module_sha256": refresh.sha256_bytes(module.read_bytes()),
        "canonical_registry_root": str(registry_root),
        "canonical_registry_inventory_path": str(inventory),
        "canonical_registry_inventory_sha256": inventory_sha256,
        "canonical_registry_tree_sha256": registry_tree_sha256,
        "launcher_path": str(bin_dir / "bureau"),
        "runtime_refresh_launcher_path": str(bin_dir / "bureau-runtime-refresh"),
        "status_capsule_launcher_path": str(bin_dir / "bureau-status-capsule"),
        "installed_at": refresh.isoformat(NOW),
        "runtime_approval": runtime_approval,
        "previous_manifest_sha256": None,
        "rollback": rollback,
    }
    if scheduler is not None:
        manifest["scheduler"] = scheduler
    manifest[refresh.RUNTIME_MANIFEST_PAYLOAD_DIGEST_FIELD] = refresh.payload_digest(
        manifest, refresh.RUNTIME_MANIFEST_PAYLOAD_DIGEST_FIELD
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_bytes(refresh.canonical_bytes(manifest))
    manifest_sha256 = refresh.sha256_bytes(manifest_path.read_bytes())
    receipt = {
        "schema_version": 1,
        "kind": "bureau_runtime_install_receipt",
        "release_id": release_id,
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha256,
        "launcher_path": str(bin_dir / "bureau"),
        "launcher_sha256": refresh.sha256_bytes((bin_dir / "bureau").read_bytes()),
        "launcher_written": True,
        "runtime_refresh_launcher_path": str(bin_dir / "bureau-runtime-refresh"),
        "runtime_refresh_launcher_sha256": refresh.sha256_bytes(
            (bin_dir / "bureau-runtime-refresh").read_bytes()
        ),
        "runtime_refresh_launcher_written": True,
        "status_capsule_launcher_path": str(bin_dir / "bureau-status-capsule"),
        "status_capsule_launcher_sha256": refresh.sha256_bytes(
            (bin_dir / "bureau-status-capsule").read_bytes()
        ),
        "status_capsule_launcher_written": True,
        "package_tree_sha256": package_tree_sha256,
        "canonical_registry_root": str(registry_root),
        "canonical_registry_tree_sha256": registry_tree_sha256,
        "rollback": rollback,
        "runtime_approval": runtime_approval,
        "installed_at": refresh.isoformat(NOW),
    }
    if scheduler is not None:
        receipt["scheduler"] = scheduler
    receipt_path = prefix / "receipts" / f"{release_id}-{manifest_sha256[:12]}.json"
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_bytes(refresh.canonical_bytes(receipt))
    install_receipt = {**receipt, "receipt_path": str(receipt_path)}
    historical_readback = refresh.readback_historical_install(
        expected_commit=intent["main_commit"],
        prefix=prefix,
        bin_dir=bin_dir,
        install_receipt=install_receipt,
    )

    backup = prefix / "backups" / "successor-deploy"
    backup.mkdir(parents=True)
    shutil.copy2(manifest_path, backup / "deployment-manifest.json")
    for name in launcher_contents:
        shutil.copy2(bin_dir / name, backup / name)
    successor_commit = "9" * 40
    successor_release_id = f"{successor_commit[:12]}-srcfixture"
    successor_release = prefix / "releases" / successor_release_id
    shutil.copytree(release, successor_release)
    successor_module = successor_release / "src/bureau/runtime_identity.py"
    successor_registry = prefix / "registry-snapshots" / f"{successor_commit[:12]}-fixture"
    shutil.copytree(registry_root, successor_registry)
    successor_inventory = successor_registry / ".bureau-runtime-snapshot.json"
    successor_inventory_value = {**inventory_value, "source_commit": successor_commit}
    successor_inventory.write_bytes(refresh.canonical_bytes(successor_inventory_value))
    successor_manifest = {
        **manifest,
        "release_id": successor_release_id,
        "source_commit": successor_commit,
        "immutable_release_path": str(successor_release),
        "module_path": str(successor_module),
        "module_sha256": refresh.sha256_bytes(successor_module.read_bytes()),
        "canonical_registry_root": str(successor_registry),
        "canonical_registry_inventory_path": str(successor_inventory),
        "canonical_registry_inventory_sha256": refresh.sha256_bytes(
            successor_inventory.read_bytes()
        ),
        "previous_manifest_sha256": manifest_sha256,
    }
    successor_manifest[refresh.RUNTIME_MANIFEST_PAYLOAD_DIGEST_FIELD] = refresh.payload_digest(
        successor_manifest, refresh.RUNTIME_MANIFEST_PAYLOAD_DIGEST_FIELD
    )
    manifest_path.write_bytes(refresh.canonical_bytes(successor_manifest))
    successor_bytes = manifest_path.read_bytes()
    if release_scheduler_names == runtime_identity.SCHEDULER_NAMES:
        assert runtime_identity._package_tree_sha256(successor_release) == package_tree_sha256
    else:
        assert runtime_identity._package_tree_sha256(successor_release) is None
    assert registry_snapshot.canonical_registry_identity(successor_manifest)["valid"] is True
    for name in launcher_contents:
        path = bin_dir / name
        path.write_bytes(f"#!/bin/sh\necho successor-{name}\n".encode())
        path.chmod(0o755)
    assert manifest_path.read_bytes() == successor_bytes
    return install_receipt, historical_readback, {
        "manifest": manifest_path,
        "backup": backup,
        "release": release,
        "registry_task": registry_task,
        "receipt": receipt_path,
    }


def replace_historical_result(
    intent: dict[str, Any],
    result: dict[str, Any],
    *,
    install_receipt: dict[str, Any],
    readback: dict[str, Any],
) -> dict[str, Any]:
    result_path = Path(intent["state_root"]) / "attempts" / intent["target_sha256"] / "result.json"
    result_path.unlink()
    payload = dict(result)
    payload.pop("result_sha256", None)
    payload["install_receipt"] = install_receipt
    payload["readback"] = readback
    return refresh._write_attempt_result(result_path, payload)


def add_authority_run_receipt(
    store: StateStore,
    task_id: str,
    *,
    state: str = "succeeded",
    run_suffix: str = "one",
    with_reservation: bool = False,
    mutate_receipt: Any | None = None,
) -> tuple[str, dict[str, Any]]:
    run_id = f"BUR-RUN-TEST-{run_suffix}"
    worker_id = f"worker-{run_suffix}"
    task_sha256 = "a" * 64
    plan_sha256 = "b" * 64
    criterion_id = "terminal-run-proof"
    task = runtime_authority_spec(task_id)
    task["acceptance"] = [
        {
            "id": criterion_id,
            "assertion": "Terminal run evidence remains authenticated.",
            "evidence_type": "object",
            "verifier": "manual_observation",
            "verifier_config": {"observation_scope": f"test:{run_id}"},
        }
    ]
    envelope = {
        "schema_version": 1,
        "run_id": run_id,
        "task_id": task_id,
        "task_sha256": task_sha256,
        "plan_sha256": plan_sha256,
        "task": task,
    }
    envelope_sha256 = legacy.sha256_json(envelope)
    observed_at = refresh.isoformat(NOW)
    evidence_unsigned = {
        "schema_version": 1,
        "kind": "bureau.acceptance_evidence",
        "criterion_id": criterion_id,
        "evidence_type": "manual_observation",
        "facts": {"accepted": True},
        "observed_at": observed_at,
        "revision": {
            "task_sha256": task_sha256,
            "plan_sha256": plan_sha256,
            "observation_scope": f"test:{run_id}",
        },
        "source": {"authority": "manual", "reference": "test"},
    }
    authentication = {
        "schema_version": 1,
        "kind": "bureau.acceptance_source_authentication",
        "criterion_id": criterion_id,
        "run_id": run_id,
        "task_id": task_id,
        "task_sha256": task_sha256,
        "plan_sha256": plan_sha256,
        "envelope_sha256": envelope_sha256,
        "evidence_sha256": legacy.sha256_json(evidence_unsigned),
        "verifier": "manual_observation",
        "observation_scope": f"test:{run_id}",
        "authority": "manual",
    }
    evidence = {**evidence_unsigned, "_source_authentication": authentication}
    receipt = {
        "schema_version": 1,
        "run_id": run_id,
        "task_id": task_id,
        "task_sha256": task_sha256,
        "plan_sha256": plan_sha256,
        "envelope_sha256": envelope_sha256,
        "evidence": {criterion_id: evidence},
        "verified_at": observed_at,
    }
    if mutate_receipt is not None:
        mutate_receipt(receipt)
    receipt["receipt_sha256"] = legacy.sha256_json(receipt)
    with store.immediate() as connection:
        connection.execute(
            "INSERT INTO workers(worker_id,kind,capabilities_json,heartbeat_at) VALUES(?,?,?,?)",
            (worker_id, "test", "[]", observed_at),
        )
        connection.execute(
            """
            INSERT INTO runs(
                run_id,task_id,worker_id,attempt,state,task_sha256,plan_sha256,
                envelope_json,envelope_sha256,created_at,updated_at,heartbeat_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                task_id,
                worker_id,
                1,
                state,
                task_sha256,
                plan_sha256,
                legacy.canonical_json(envelope),
                envelope_sha256,
                observed_at,
                observed_at,
                observed_at,
            ),
        )
        if with_reservation:
            connection.execute(
                (
                    "INSERT INTO reservations(run_id,resource_id,mode,amount,created_at) "
                    "VALUES(?,?,?,?,?)"
                ),
                (run_id, "component.bureau.core", "write", 1, observed_at),
            )
        connection.execute(
            "INSERT INTO receipts(run_id,receipt_json,receipt_sha256,created_at) VALUES(?,?,?,?)",
            (run_id, legacy.canonical_json(receipt), receipt["receipt_sha256"], observed_at),
        )
    store.envelope_path(run_id).write_text(
        json.dumps(envelope, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return run_id, receipt


def test_observe_reports_already_current_only_when_scheduler_converged(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "deployment-manifest.json"
    scheduler_receipt = {"kind": "bureau_runtime_scheduler_readback"}
    write_manifest(manifest, MAIN, scheduler=scheduler_receipt)
    calls: list[list[str]] = []
    scheduler_calls: list[tuple[dict[str, Any], str]] = []

    def github(arguments: list[str]) -> Any:
        calls.append(arguments)
        return {"sha": MAIN}

    def scheduler_reader(
        receipt: dict[str, Any], *, expected_commit: str
    ) -> dict[str, Any]:
        scheduler_calls.append((receipt, expected_commit))
        return {"authoritative": True}

    result = refresh.observe_runtime_refresh(
        repository="heimgewebe/bureau",
        manifest_path=manifest,
        now=NOW,
        github=github,
        scheduler_reader=scheduler_reader,
    )

    assert result["status"] == "already_current"
    assert result["scheduler_target_state"] == "converged"
    assert result["reason_codes"] == []
    assert result["lag_commits"] == 0
    assert result["recovery_action"] == {
        "action": "none",
        "eligible": False,
        "requires_authorization": False,
    }
    assert calls == [["api", "repos/heimgewebe/bureau/commits/main"]]
    assert scheduler_calls == [(scheduler_receipt, MAIN)]
    refresh.verify_digest(result, "observation_sha256")


def test_observe_source_current_missing_scheduler_requires_prepare_intent(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "deployment-manifest.json"
    write_manifest(manifest, MAIN)

    result = refresh.observe_runtime_refresh(
        repository="heimgewebe/bureau",
        manifest_path=manifest,
        now=NOW,
        github=lambda _arguments: {"sha": MAIN},
    )

    assert result["status"] == "alert"
    assert result["scheduler_target_state"] == "missing-receipt"
    assert result["reason_codes"] == ["scheduler-receipt-missing"]
    assert result["recovery_action"] == {
        "action": "prepare-intent",
        "eligible": True,
        "requires_authorization": True,
    }


def test_observe_source_current_live_scheduler_drift_requires_prepare_intent(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "deployment-manifest.json"
    scheduler_receipt = {"kind": "bureau_runtime_scheduler_readback"}
    write_manifest(manifest, MAIN, scheduler=scheduler_receipt)

    def scheduler_reader(
        _receipt: dict[str, Any], *, expected_commit: str
    ) -> dict[str, Any]:
        assert expected_commit == MAIN
        raise refresh.RuntimeRefreshError(
            "scheduler-readback-load-state-invalid",
            "task-supply timer is not loaded",
        )

    result = refresh.observe_runtime_refresh(
        repository="heimgewebe/bureau",
        manifest_path=manifest,
        now=NOW,
        github=lambda _arguments: {"sha": MAIN},
        scheduler_reader=scheduler_reader,
    )

    assert result["status"] == "alert"
    assert (
        result["scheduler_target_state"]
        == "drift:scheduler-readback-load-state-invalid"
    )
    assert result["reason_codes"] == ["scheduler-readback-load-state-invalid"]
    assert result["recovery_action"]["action"] == "prepare-intent"
    assert result["recovery_action"]["eligible"] is True

    converged = refresh.observe_runtime_refresh(
        repository="heimgewebe/bureau",
        manifest_path=manifest,
        now=NOW,
        github=lambda _arguments: {"sha": MAIN},
        scheduler_reader=lambda _receipt, **_: {
            "schema_version": refresh.SCHEMA_VERSION,
            "kind": "bureau_runtime_scheduler_readback",
            "source_commit": MAIN,
            "authoritative": True,
        },
    )
    assert converged["status"] == "already_current"
    assert converged["scheduler_target_state"] == "converged"
    assert converged["target_sha256"] != result["target_sha256"]


def test_observe_source_current_failed_service_is_not_auto_reconvergeable(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "deployment-manifest.json"
    scheduler_receipt = {"kind": "bureau_runtime_scheduler_readback"}
    write_manifest(manifest, MAIN, scheduler=scheduler_receipt)

    def scheduler_reader(
        _receipt: dict[str, Any], *, expected_commit: str
    ) -> dict[str, Any]:
        assert expected_commit == MAIN
        raise refresh.RuntimeRefreshError(
            "scheduler-required-service-invalid",
            "task-supply service is failed",
        )

    result = refresh.observe_runtime_refresh(
        repository="heimgewebe/bureau",
        manifest_path=manifest,
        now=NOW,
        github=lambda _arguments: {"sha": MAIN},
        scheduler_reader=scheduler_reader,
    )

    assert result["status"] == "blocked"
    assert (
        result["scheduler_target_state"]
        == "blocked:scheduler-required-service-invalid"
    )
    assert result["reason_codes"] == ["scheduler-required-service-invalid"]
    assert result["recovery_action"]["action"] == "resolve-reason-codes"
    assert result["recovery_action"]["eligible"] is False



def test_observe_source_current_scheduler_timeout_blocks(tmp_path: Path) -> None:
    manifest = tmp_path / "deployment-manifest.json"
    scheduler_receipt = {"kind": "bureau_runtime_scheduler_readback"}
    write_manifest(manifest, MAIN, scheduler=scheduler_receipt)
    def scheduler_reader(_receipt: dict[str, Any], *, expected_commit: str) -> dict[str, Any]:
        assert expected_commit == MAIN
        raise subprocess.TimeoutExpired(cmd=["systemctl", "--user"], timeout=60)
    result = refresh.observe_runtime_refresh(
        repository="heimgewebe/bureau", manifest_path=manifest, now=NOW,
        github=lambda _arguments: {"sha": MAIN}, scheduler_reader=scheduler_reader,
    )
    assert result["status"] == "blocked"
    assert result["reason_codes"] == ["scheduler-readback-timeout"]
    assert result["scheduler_target_state"] == "blocked:scheduler-readback-timeout"
    assert result["recovery_action"]["action"] == "resolve-reason-codes"


def test_observe_source_current_scheduler_io_failure_blocks(tmp_path: Path) -> None:
    manifest = tmp_path / "deployment-manifest.json"
    scheduler_receipt = {"kind": "bureau_runtime_scheduler_readback"}
    write_manifest(manifest, MAIN, scheduler=scheduler_receipt)

    def scheduler_reader(
        _receipt: dict[str, Any], *, expected_commit: str
    ) -> dict[str, Any]:
        assert expected_commit == MAIN
        raise FileNotFoundError("scheduler artifact disappeared during readback")

    result = refresh.observe_runtime_refresh(
        repository="heimgewebe/bureau",
        manifest_path=manifest,
        now=NOW,
        github=lambda _arguments: {"sha": MAIN},
        scheduler_reader=scheduler_reader,
    )

    assert result["status"] == "blocked"
    assert result["reason_codes"] == ["scheduler-readback-io-failed"]
    assert result["scheduler_target_state"] == "blocked:scheduler-readback-io-failed"
    assert result["recovery_action"]["action"] == "resolve-reason-codes"
    assert result["recovery_action"]["eligible"] is False


def test_observe_source_current_unsafe_fragment_blocks(tmp_path: Path) -> None:
    manifest = tmp_path / "deployment-manifest.json"
    scheduler_receipt = {"kind": "bureau_runtime_scheduler_readback"}
    write_manifest(manifest, MAIN, scheduler=scheduler_receipt)
    def scheduler_reader(_receipt: dict[str, Any], *, expected_commit: str) -> dict[str, Any]:
        assert expected_commit == MAIN
        raise refresh.RuntimeRefreshError(
            "scheduler-readback-fragment-unsafe", "scheduler path is a directory"
        )
    result = refresh.observe_runtime_refresh(
        repository="heimgewebe/bureau", manifest_path=manifest, now=NOW,
        github=lambda _arguments: {"sha": MAIN}, scheduler_reader=scheduler_reader,
    )
    assert result["status"] == "blocked"
    assert result["reason_codes"] == ["scheduler-readback-fragment-unsafe"]
    assert result["scheduler_target_state"] == "blocked:scheduler-readback-fragment-unsafe"


def test_observe_source_current_invalid_scheduler_evidence_blocks(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "deployment-manifest.json"
    scheduler_receipt = {"kind": "bureau_runtime_scheduler_readback"}
    write_manifest(manifest, MAIN, scheduler=scheduler_receipt)

    def scheduler_reader(
        _receipt: dict[str, Any], *, expected_commit: str
    ) -> dict[str, Any]:
        assert expected_commit == MAIN
        raise refresh.RuntimeRefreshError(
            "scheduler-receipt-invalid",
            "scheduler receipt is malformed",
        )

    result = refresh.observe_runtime_refresh(
        repository="heimgewebe/bureau",
        manifest_path=manifest,
        now=NOW,
        github=lambda _arguments: {"sha": MAIN},
        scheduler_reader=scheduler_reader,
    )

    assert result["status"] == "blocked"
    assert result["scheduler_target_state"] == "blocked:scheduler-receipt-invalid"
    assert result["reason_codes"] == ["scheduler-receipt-invalid"]
    assert result["recovery_action"] == {
        "action": "resolve-reason-codes",
        "eligible": False,
        "reason_codes": ["scheduler-receipt-invalid"],
        "requires_authorization": False,
    }


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
    assert set(result["check_summary"]) == set(refresh.DEFAULT_REQUIRED_CHECKS)
    assert all(item["state"] == "success" for item in result["check_summary"].values())
    assert result["lag_commits"] == 1
    assert result["recovery_action"] == {
        "action": "prepare-intent",
        "eligible": True,
        "requires_authorization": True,
    }
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
    assert result["recovery_action"]["action"] == "prepare-intent"
    assert result["recovery_action"]["eligible"] is True


def test_observe_blocks_failed_or_missing_ci(tmp_path: Path) -> None:
    detail = green_pr_detail()
    detail["statusCheckRollup"] = [{"name": "validate (3.10)", "conclusion": "FAILURE"}]
    result, _ = candidate(tmp_path, detail=detail)

    assert result["status"] == "blocked"
    assert result["reason_codes"] == ["required-ci-not-green"]
    assert result["recovery_action"] == {
        "action": "resolve-reason-codes",
        "eligible": False,
        "reason_codes": ["required-ci-not-green"],
        "requires_authorization": False,
    }
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
    runtime_user_unit_dir = refresh.default_runtime_user_unit_dir()

    assert intent_path.is_file()
    assert intent["target_sha256"] == observed["target_sha256"]
    assert intent["expected_deployed_source_commit"] == DEPLOYED
    assert intent["required_resource_keys"] == sorted(intent["required_resource_keys"])
    assert intent["runtime_user_unit_dir"] == str(runtime_user_unit_dir)
    assert f"path:{tmp_path.resolve() / 'bin/bureau'}" in intent["required_resource_keys"]
    assert (
        f"path:{tmp_path.resolve() / 'bin/bureau-status-capsule'}"
        in intent["required_resource_keys"]
    )
    for name in refresh.RUNTIME_SCHEDULER_NAMES:
        assert (
            f"path:{tmp_path.resolve() / f'systemd/user/{name}.service'}"
            in intent["required_resource_keys"]
        )
        assert (
            f"path:{tmp_path.resolve() / f'systemd/user/{name}.timer'}"
            in intent["required_resource_keys"]
        )
        assert (
            f"path:{tmp_path.resolve() / f'libexec/{name}'}"
            in intent["required_resource_keys"]
        )
        assert (
            f"path:{tmp_path.resolve() / f'systemd/user/timers.target.wants/{name}.timer'}"
            in intent["required_resource_keys"]
        )
        assert (
            f"path:{runtime_user_unit_dir / f'timers.target.wants/{name}.timer'}"
            in intent["required_resource_keys"]
        )
        assert f"service:{name}.service" in intent["required_resource_keys"]
        assert f"service:{name}.timer" in intent["required_resource_keys"]
    assert {
        f"path:{tmp_path.resolve() / 'systemd/user'}",
        f"path:{tmp_path.resolve() / 'systemd/user/timers.target.wants'}",
        f"path:{runtime_user_unit_dir}",
        f"path:{runtime_user_unit_dir / 'timers.target.wants'}",
    }.isdisjoint(intent["required_resource_keys"])
    assert intent["runtime_approval"]["allowed"] is True
    assert intent["runtime_approval"]["required_level"] == "break_glass"
    assert intent["runtime_approval"]["expected_reference"] == observed["target_sha256"]
    refresh.verify_digest(intent, "intent_sha256")

    with pytest.raises(refresh.RuntimeRefreshError) as denied:
        refresh.prepare_intent(
            candidate=observed,
            state_root=(tmp_path / "denied-state").resolve(),
            prefix=(tmp_path / "denied-prefix").resolve(),
            bin_dir=(tmp_path / "denied-bin").resolve(),
            remote_url="file:///tmp/bureau.git",
            authorized_by="chatgpt",
            authorization="ordinary operator authorization",
            break_glass=False,
            approval_reference=observed["target_sha256"],
            approval_task_id="BUR-2026-003-T009",
            now=NOW,
        )
    assert denied.value.code == "runtime-approval-required"

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


def test_scheduler_resource_keys_lease_exact_persistent_and_runtime_wants_links() -> None:
    user_unit_dir = Path("/test/home/.config/systemd/user")
    runtime_user_unit_dir = Path("/run/user/1234/systemd/user")
    libexec_dir = Path("/test/home/.local/libexec")

    keys = refresh.scheduler_resource_keys(
        user_unit_dir=user_unit_dir,
        libexec_dir=libexec_dir,
        runtime_user_unit_dir=runtime_user_unit_dir,
    )

    for name in refresh.RUNTIME_SCHEDULER_NAMES:
        artifact_paths = (
            user_unit_dir / f"{name}.service",
            user_unit_dir / f"{name}.timer",
            libexec_dir / name,
        )
        for artifact_path in artifact_paths:
            assert f"path:{artifact_path}" in keys
            assert f"path:{refresh._scheduler_staging_path(artifact_path)}" in keys
        persistent_wants = user_unit_dir / f"timers.target.wants/{name}.timer"
        runtime_wants = runtime_user_unit_dir / f"timers.target.wants/{name}.timer"
        for wants_path in (persistent_wants, runtime_wants):
            assert f"path:{wants_path}" in keys
            staging_key = f"path:{refresh._scheduler_staging_path(wants_path)}"
            if name == refresh.REQUIRED_RUNTIME_TIMER and wants_path == persistent_wants:
                assert staging_key in keys
            else:
                assert staging_key not in keys
    assert {
        f"path:{user_unit_dir}",
        f"path:{libexec_dir}",
        f"path:{runtime_user_unit_dir}",
        f"path:{user_unit_dir / 'timers.target.wants'}",
        f"path:{runtime_user_unit_dir / 'timers.target.wants'}",
    }.isdisjoint(keys)
    assert len([key for key in keys if key.startswith("path:")]) == 49


def test_scheduler_staging_paths_fail_closed_without_deleting_foreign_entries(
    tmp_path: Path,
) -> None:
    target = tmp_path / "systemd/user/demo.service"
    target.parent.mkdir(parents=True)
    staging = refresh._scheduler_staging_path(target)
    staging.write_text("foreign staging entry\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        refresh.atomic_write(
            target,
            b"candidate\n",
            0o644,
            staging_path=staging,
        )

    assert staging.read_text(encoding="utf-8") == "foreign staging entry\n"
    assert not os.path.lexists(target)

    wants = tmp_path / "systemd/user/timers.target.wants/demo.timer"
    wants.parent.mkdir()
    wants_staging = refresh._scheduler_staging_path(wants)
    wants_staging.write_text("foreign link staging entry\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        refresh._replace_with_symlink(wants, "../demo.timer")

    assert wants_staging.read_text(encoding="utf-8") == "foreign link staging entry\n"
    assert not os.path.lexists(wants)


def test_runtime_user_unit_dir_default_is_xdg_bound_and_home_independent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "unrelated-home"))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert refresh.default_runtime_user_unit_dir() == (
        Path("/run/user") / str(os.getuid()) / "systemd/user"
    )

    runtime_root = (tmp_path / "xdg-runtime").resolve()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_root))
    assert refresh.default_runtime_user_unit_dir() == runtime_root / "systemd/user"

    monkeypatch.setenv("XDG_RUNTIME_DIR", "relative-runtime")
    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.default_runtime_user_unit_dir()
    assert caught.value.code == "runtime-dir-invalid"


def test_apply_rejects_runtime_user_unit_dir_drift_before_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime-at-intent"))
    _observed, manifest_path, intent, intent_path = prepare_candidate_intent(tmp_path)
    binding, resource_db = lease_for(tmp_path / "runtime-path-leases", intent)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime-at-apply"))

    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.apply_runtime_refresh(
            intent_path=intent_path,
            lease_binding=binding,
            manifest_path=manifest_path,
            state_root=Path(intent["state_root"]),
            resource_db=resource_db,
            now=NOW,
            observer=lambda **_: pytest.fail("observer must not run after runtime path drift"),
            source_preparer=lambda **_: pytest.fail("source effect must not run"),
            installer=lambda **_: pytest.fail("installer effect must not run"),
        )

    assert caught.value.code == "runtime-user-unit-dir-drift"
    assert not (Path(intent["state_root"]) / "attempts").exists()


def test_prepare_uses_authoritative_state_store_over_stale_registry_snapshot(
    tmp_path: Path,
) -> None:
    task_id = "BUR-STALE-SNAPSHOT-AUTHORITY"
    observed, _ = candidate(tmp_path)
    snapshot = tmp_path / "installed-registry/registry/tasks" / f"{task_id}.json"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_bytes(refresh.canonical_bytes(runtime_authority_spec(task_id)))
    store = seed_authority_store(tmp_path / "bureau-state", task_id)
    revise_authority(
        store,
        task_id,
        lambda spec: spec.__setitem__("state", "superseded"),
        key="supersede-before-prepare",
    )
    authoritative_before = store.task_spec(task_id)

    with pytest.raises(refresh.RuntimeRefreshError) as error:
        refresh.prepare_intent(
            candidate=observed,
            state_root=(tmp_path / "state").resolve(),
            prefix=(tmp_path / "prefix").resolve(),
            bin_dir=(tmp_path / "bin").resolve(),
            remote_url="file:///tmp/bureau.git",
            authorized_by="operator",
            authorization="Exact stale-snapshot regression approval.",
            break_glass=True,
            approval_reference=observed["target_sha256"],
            approval_task_id=task_id,
            now=NOW,
            authority_store=store,
        )

    assert json.loads(snapshot.read_text(encoding="utf-8"))["state"] == "ready"
    assert error.value.code == "authority-task-terminal"
    assert store.task_spec(task_id) == authoritative_before
    assert not (tmp_path / "state/intents").exists()


def test_apply_rechecks_exact_authoritative_revision_before_any_effect(
    tmp_path: Path,
) -> None:
    _observed, manifest_path, intent, intent_path = prepare_candidate_intent(tmp_path)
    store = authority_store_for_intent(intent)
    revise_authority(
        store,
        intent["approval_task_id"],
        lambda spec: spec["metadata"].__setitem__("drift_marker", "new revision"),
        key="authority-drift-before-apply",
    )
    binding, resource_db = lease_for(tmp_path / "leases", intent)

    with pytest.raises(refresh.RuntimeRefreshError) as error:
        refresh.apply_runtime_refresh(
            intent_path=intent_path,
            lease_binding=binding,
            manifest_path=manifest_path,
            state_root=Path(intent["state_root"]),
            resource_db=resource_db,
            now=NOW,
            observer=lambda **_: pytest.fail("observer must not run after authority drift"),
            source_preparer=lambda **_: pytest.fail("source effect must not run"),
            installer=lambda **_: pytest.fail("installer effect must not run"),
        )

    assert error.value.code == "authority-task-drift"
    assert not (Path(intent["state_root"]) / "attempts" / intent["target_sha256"]).exists()


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("superseded", "authority-task-terminal"),
        ("verified", "authority-task-terminal"),
        ("wrong-task", "authority-task-missing"),
        ("wrong-target", "authority-wrong-target"),
    ],
)
def test_prepare_rejects_non_authorizing_tasks_before_intent_effect(
    tmp_path: Path, case: str, expected_code: str
) -> None:
    task_id = "BUR-EXACT-RUNTIME-AUTHORITY"
    observed, _ = candidate(tmp_path)
    store = seed_authority_store(
        tmp_path / "bureau-state",
        "BUR-OTHER-TASK" if case == "wrong-task" else task_id,
        state=case if case in {"superseded", "verified"} else "ready",
    )
    if case == "wrong-target":
        current = store.task_spec(task_id)
        assert current is not None

        def bind_wrong_target(spec: dict[str, Any]) -> None:
            spec["metadata"]["runtime_refresh_authority"]["target_binding_receipt"] = {
                "schema_version": 1,
                "kind": refresh.RUNTIME_AUTHORITY_BINDING_KIND,
                "task_id": task_id,
                "authority_revision": current["revision"],
                "authority_spec_sha256": current["spec_sha256"],
                "target_sha256": "f" * 64,
                "intent_sha256": "e" * 64,
                "bound_at": refresh.isoformat(NOW),
            }

        revise_authority(store, task_id, bind_wrong_target, key="wrong-target-binding")
    before = store.task_spec(task_id)

    with pytest.raises(refresh.RuntimeRefreshError) as error:
        refresh.prepare_intent(
            candidate=observed,
            state_root=(tmp_path / "state").resolve(),
            prefix=(tmp_path / "prefix").resolve(),
            bin_dir=(tmp_path / "bin").resolve(),
            remote_url="file:///tmp/bureau.git",
            authorized_by="operator",
            authorization="Exact negative authority approval.",
            break_glass=True,
            approval_reference=observed["target_sha256"],
            approval_task_id=task_id,
            now=NOW,
            authority_store=store,
        )

    assert error.value.code == expected_code
    assert store.task_spec(task_id) == before
    assert not (tmp_path / "state/intents").exists()


def test_prepare_intent_omits_stable_launchers_and_requires_only_drift(tmp_path: Path) -> None:
    observed, manifest_path = candidate(tmp_path)
    prefix = (tmp_path / "prefix").resolve()
    bin_dir = (tmp_path / "bin").resolve()
    bin_dir.mkdir(parents=True)
    for name, entrypoint in refresh.RUNTIME_LAUNCHER_ENTRYPOINTS:
        path = bin_dir / name
        path.write_bytes(refresh.stable_launcher_bytes(manifest_path, entrypoint))
        path.chmod(0o755)

    state_root = (tmp_path / "stable-state").resolve()
    stable_authority = seed_authority_store(
        tmp_path / "stable-bureau-state", "BUR-STABLE-LAUNCHER-TEST"
    )
    intent, _ = refresh.prepare_intent(
        candidate=observed,
        state_root=state_root,
        prefix=prefix,
        bin_dir=bin_dir,
        remote_url="file:///tmp/bureau.git",
        authorized_by="chatgpt",
        authorization="Explicit stable-launcher migration approval.",
        break_glass=True,
        approval_reference=observed["target_sha256"],
        approval_task_id="BUR-STABLE-LAUNCHER-TEST",
        now=NOW,
        authority_store=stable_authority,
    )
    launcher_keys = {f"path:{bin_dir / name}" for name, _ in refresh.RUNTIME_LAUNCHER_ENTRYPOINTS}
    assert launcher_keys.isdisjoint(intent["required_resource_keys"])

    status_capsule = bin_dir / "bureau-status-capsule"
    status_capsule.write_text("corrupt\n", encoding="utf-8")
    drift_state = (tmp_path / "drift-state").resolve()
    repair_authority = seed_authority_store(
        tmp_path / "repair-bureau-state", "BUR-STABLE-LAUNCHER-REPAIR"
    )
    drift_intent, _ = refresh.prepare_intent(
        candidate=observed,
        state_root=drift_state,
        prefix=prefix,
        bin_dir=bin_dir,
        remote_url="file:///tmp/bureau.git",
        authorized_by="chatgpt",
        authorization="Explicit launcher repair approval.",
        break_glass=True,
        approval_reference=observed["target_sha256"],
        approval_task_id="BUR-STABLE-LAUNCHER-REPAIR",
        now=NOW,
        authority_store=repair_authority,
    )
    assert f"path:{status_capsule}" in drift_intent["required_resource_keys"]
    assert f"path:{bin_dir / 'bureau'}" not in drift_intent["required_resource_keys"]
    assert (
        f"path:{bin_dir / 'bureau-runtime-refresh'}" not in drift_intent["required_resource_keys"]
    )


def test_apply_blocks_launcher_drift_absent_from_intent_before_observer(tmp_path: Path) -> None:
    observed, manifest_path = candidate(tmp_path)
    prefix = (tmp_path / "prefix").resolve()
    bin_dir = (tmp_path / "bin").resolve()
    bin_dir.mkdir(parents=True)
    for name, entrypoint in refresh.RUNTIME_LAUNCHER_ENTRYPOINTS:
        path = bin_dir / name
        path.write_bytes(refresh.stable_launcher_bytes(manifest_path, entrypoint))
        path.chmod(0o755)
    state_root = (tmp_path / "state").resolve()
    authority_store = seed_authority_store(tmp_path / "bureau-state", "BUR-STABLE-LAUNCHER-DRIFT")
    intent, intent_path = refresh.prepare_intent(
        candidate=observed,
        state_root=state_root,
        prefix=prefix,
        bin_dir=bin_dir,
        remote_url="file:///tmp/bureau.git",
        authorized_by="chatgpt",
        authorization="Explicit drift-gate approval.",
        break_glass=True,
        approval_reference=observed["target_sha256"],
        approval_task_id="BUR-STABLE-LAUNCHER-DRIFT",
        now=NOW,
        authority_store=authority_store,
    )
    status_capsule = bin_dir / "bureau-status-capsule"
    status_capsule.write_text("drifted\n", encoding="utf-8")
    binding, resource_db = lease_for(tmp_path / "drift-leases", intent)
    with pytest.raises(refresh.RuntimeRefreshError) as error:
        refresh.apply_runtime_refresh(
            intent_path=intent_path,
            lease_binding=binding,
            manifest_path=manifest_path,
            state_root=state_root,
            resource_db=resource_db,
            now=NOW,
            observer=lambda **_: pytest.fail("observer must not run after launcher drift"),
        )
    assert error.value.code == "launcher-drift-after-intent"
    assert error.value.details["resource_keys"] == [f"path:{status_capsule}"]


def test_scheduler_resources_are_required_by_live_lease_validation(tmp_path: Path) -> None:
    _, _, intent, _ = prepare_candidate_intent(tmp_path)
    missing_key = (
        f"path:{refresh._scheduler_staging_path(tmp_path.resolve() / 'libexec/bureau-task-supply')}"
    )
    assert f"path:{tmp_path.resolve() / 'systemd/user'}" not in intent[
        "required_resource_keys"
    ]
    assert f"path:{tmp_path.resolve() / 'libexec'}" not in intent[
        "required_resource_keys"
    ]
    binding, resource_db = lease_for(
        tmp_path / "scheduler-lease-gap", intent, omit={missing_key}
    )

    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.validate_live_lease_binding(
            intent,
            binding,
            resource_db=resource_db,
            now=NOW,
        )

    assert caught.value.code == "lease-resources-missing"
    assert caught.value.details["missing"] == [missing_key]


@pytest.mark.parametrize("mode", [0o644, 0o755])
def test_atomic_write_applies_exact_mode_under_restrictive_umask(
    tmp_path: Path, mode: int
) -> None:
    path = tmp_path / f"artifact-{mode:o}"
    previous_umask = os.umask(0o077)
    try:
        refresh.atomic_write(path, b"exact mode\n", mode)
    finally:
        os.umask(previous_umask)

    assert path.read_bytes() == b"exact mode\n"
    assert path.stat().st_mode & 0o777 == mode


def test_scheduler_readback_distinguishes_replaceable_and_unsafe_fragments(
    tmp_path: Path,
) -> None:
    release, release_id, source_commit = scheduler_release(tmp_path)
    user_unit_dir = tmp_path / "live-systemd"
    libexec_dir = tmp_path / "live-libexec"
    user_unit_dir.mkdir()
    libexec_dir.mkdir()
    artifacts = refresh._scheduler_artifacts(
        release=release, user_unit_dir=user_unit_dir, libexec_dir=libexec_dir
    )
    prior_timers = {
        name: {
            "LoadState": "not-found",
            "UnitFileState": "disabled",
            "ActiveState": "inactive",
            "SubState": "dead",
            "FragmentPath": "",
        }
        for name in refresh.RUNTIME_SCHEDULER_NAMES
    }
    timer_intent = {name: "absent" for name in refresh.RUNTIME_SCHEDULER_NAMES}
    with pytest.raises(refresh.RuntimeRefreshError) as missing:
        refresh._scheduler_readback(
            source_commit=source_commit, release_id=release_id, release=release,
            user_unit_dir=user_unit_dir, libexec_dir=libexec_dir,
            prior_timers=prior_timers, timer_intent=timer_intent,
            command_runner=lambda _argv: pytest.fail("systemd read must not run"),
        )
    assert missing.value.code == "scheduler-readback-fragment-replaceable"
    assert missing.value.details["kind"] == "absent"
    first_path = Path(artifacts[0]["live_path"])
    first_path.mkdir(parents=True)
    with pytest.raises(refresh.RuntimeRefreshError) as unsafe:
        refresh._scheduler_readback(
            source_commit=source_commit, release_id=release_id, release=release,
            user_unit_dir=user_unit_dir, libexec_dir=libexec_dir,
            prior_timers=prior_timers, timer_intent=timer_intent,
            command_runner=lambda _argv: pytest.fail("systemd read must not run"),
        )
    assert unsafe.value.code == "scheduler-readback-fragment-unsafe"


def test_release_task_supply_absent_live_is_converged_enabled_and_waiting(
    tmp_path: Path,
) -> None:
    release, release_id, source_commit = scheduler_release(tmp_path)
    unit_root = tmp_path / "systemd/user"
    libexec_root = tmp_path / "libexec"
    rollback = tmp_path / "rollback"
    rollback.mkdir()
    unit_root.mkdir(parents=True)
    libexec_root.mkdir()
    systemd = FakeUserSystemd(unit_root)
    shutil.rmtree(systemd.runtime_unit_root)
    assert not systemd.runtime_unit_root.exists()

    result = refresh.converge_user_scheduler(
        source_commit=source_commit,
        release_id=release_id,
        release=release,
        user_unit_dir=unit_root,
        libexec_dir=libexec_root,
        runtime_user_unit_dir=systemd.runtime_unit_root,
        rollback_directory=rollback,
        manifest_path=tmp_path / "candidate-manifest.json",
        command_runner=systemd,
        validation_runner=successful_systemd_analyze,
        cycle_validator=successful_cycle_validation,
    )

    assert ("daemon-reload",) in systemd.commands
    assert (
        "start",
        "bureau-task-supply.timer",
    ) in systemd.commands
    task_timer = result["timers"]["bureau-task-supply"]
    assert task_timer == {
        "LoadState": "loaded",
        "UnitFileState": "enabled",
        "ActiveState": "active",
        "SubState": "waiting",
        "FragmentPath": str(unit_root / "bureau-task-supply.timer"),
    }
    assert result["services"]["bureau-task-supply"]["Result"] == "success"
    assert result["authoritative"] is True
    assert result["source_commit"] == source_commit
    assert result["release_id"] == release_id
    assert len(result["changed_artifacts"]) == len(refresh.RUNTIME_SCHEDULER_NAMES) * 3
    for artifact in result["artifacts"]:
        assert artifact["matches_release"] is True
        assert Path(artifact["live_path"]).read_bytes() == Path(
            artifact["source_path"]
        ).read_bytes()
    preimage = json.loads(
        Path(result["rollback_preimage_path"]).read_text(encoding="utf-8")
    )
    assert {item["preimage_kind"] for item in preimage["artifacts"]} == {"absent"}
    repeated_readback = refresh.readback_user_scheduler(
        result,
        expected_commit=source_commit,
        command_runner=systemd,
    )
    assert repeated_readback["timers"]["bureau-task-supply"] == task_timer

    live_service = unit_root / "bureau-task-supply.service"
    live_service.write_bytes(live_service.read_bytes() + b"# drift\n")
    with pytest.raises(refresh.RuntimeRefreshError) as drift:
        refresh.readback_user_scheduler(
            result,
            expected_commit=source_commit,
            command_runner=systemd,
        )
    assert drift.value.code == "scheduler-readback-fragment-mismatch"


def test_scheduler_convergence_preserves_existing_timer_intent(tmp_path: Path) -> None:
    release, release_id, source_commit = scheduler_release(tmp_path)
    unit_root = tmp_path / "systemd/user"
    libexec_root = tmp_path / "libexec"
    unit_root.mkdir(parents=True)
    libexec_root.mkdir()
    for name in refresh.RUNTIME_SCHEDULER_NAMES:
        for suffix in ("service", "timer"):
            (unit_root / f"{name}.{suffix}").write_text(
                f"old {name}.{suffix}\n", encoding="utf-8"
            )
        executable = libexec_root / name
        executable.write_text(f"old {name}\n", encoding="utf-8")
        executable.chmod(0o755)
    systemd = FakeUserSystemd(
        unit_root,
        timer_states={
            "bureau-curator": ("enabled", "active"),
            "bureau-verifier-control": ("enabled-runtime", "inactive"),
            "bureau-operator-control": ("disabled", "inactive"),
        },
    )
    rollback = tmp_path / "rollback"
    rollback.mkdir()

    result = refresh.converge_user_scheduler(
        source_commit=source_commit,
        release_id=release_id,
        release=release,
        user_unit_dir=unit_root,
        libexec_dir=libexec_root,
        runtime_user_unit_dir=systemd.runtime_unit_root,
        rollback_directory=rollback,
        manifest_path=tmp_path / "candidate-manifest.json",
        command_runner=systemd,
        validation_runner=successful_systemd_analyze,
        cycle_validator=successful_cycle_validation,
    )

    assert result["timers"]["bureau-curator"]["UnitFileState"] == "enabled"
    assert result["timers"]["bureau-curator"]["SubState"] == "waiting"
    assert result["timers"]["bureau-verifier-control"]["UnitFileState"] == "enabled-runtime"
    assert result["timers"]["bureau-verifier-control"]["ActiveState"] == "inactive"
    assert result["timers"]["bureau-operator-control"]["UnitFileState"] == "disabled"
    assert result["prior_timer_intent"]["bureau-curator"] == "enabled"
    assert result["prior_timer_intent"]["bureau-verifier-control"] == "enabled-runtime"
    assert result["timers"]["bureau-task-supply"]["UnitFileState"] == "enabled"
    assert not any(command[0] in {"enable", "disable"} for command in systemd.commands)


def test_scheduler_convergence_rejects_missing_persistent_wants_parent_before_effects(
    tmp_path: Path,
) -> None:
    release, release_id, source_commit = scheduler_release(tmp_path)
    unit_root = tmp_path / "systemd/user"
    libexec_root = tmp_path / "libexec"
    unit_root.mkdir(parents=True)
    libexec_root.mkdir()
    systemd = FakeUserSystemd(unit_root)
    shutil.rmtree(unit_root / "timers.target.wants")
    commands_before = list(systemd.commands)

    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.converge_user_scheduler(
            source_commit=source_commit,
            release_id=release_id,
            release=release,
            user_unit_dir=unit_root,
            libexec_dir=libexec_root,
            runtime_user_unit_dir=systemd.runtime_unit_root,
            rollback_directory=tmp_path / "rollback",
            manifest_path=tmp_path / "candidate-manifest.json",
            command_runner=systemd,
            validation_runner=successful_systemd_analyze,
            cycle_validator=successful_cycle_validation,
        )

    assert caught.value.code == "scheduler-parent-invalid"
    assert caught.value.details["label"] == "persistent timer enablement directory"
    assert systemd.commands == commands_before
    assert not (tmp_path / "rollback").exists()


def test_foreign_timer_enablement_link_survives_convergence_and_later_rollback(
    tmp_path: Path,
) -> None:
    release, release_id, source_commit = scheduler_release(tmp_path)
    unit_root = tmp_path / "systemd/user"
    libexec_root = tmp_path / "libexec"
    unit_root.mkdir(parents=True)
    libexec_root.mkdir()
    for name in refresh.RUNTIME_SCHEDULER_NAMES:
        for suffix in ("service", "timer"):
            (unit_root / f"{name}.{suffix}").write_text(
                f"old {name}.{suffix}\n", encoding="utf-8"
            )
        executable = libexec_root / name
        executable.write_text(f"old {name}\n", encoding="utf-8")
        executable.chmod(0o755)
    systemd = FakeUserSystemd(unit_root)
    foreign_link = (
        unit_root / "custom-maintenance.target.wants/bureau-operator-control.timer"
    )
    foreign_link.parent.mkdir()
    foreign_link.symlink_to("../bureau-operator-control.timer")

    first_rollback = tmp_path / "first-rollback"
    first_rollback.mkdir()
    refresh.converge_user_scheduler(
        source_commit=source_commit,
        release_id=release_id,
        release=release,
        user_unit_dir=unit_root,
        libexec_dir=libexec_root,
        runtime_user_unit_dir=systemd.runtime_unit_root,
        rollback_directory=first_rollback,
        manifest_path=tmp_path / "candidate-manifest.json",
        command_runner=systemd,
        validation_runner=successful_systemd_analyze,
        cycle_validator=successful_cycle_validation,
    )

    assert foreign_link.is_symlink()
    assert os.readlink(foreign_link) == "../bureau-operator-control.timer"
    assert systemd.states["bureau-operator-control.timer"]["UnitFileState"] == (
        "disabled"
    )

    later_rollback = tmp_path / "later-rollback"
    later_rollback.mkdir()
    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.converge_user_scheduler(
            source_commit=source_commit,
            release_id=release_id,
            release=release,
            user_unit_dir=unit_root,
            libexec_dir=libexec_root,
            runtime_user_unit_dir=systemd.runtime_unit_root,
            rollback_directory=later_rollback,
            manifest_path=tmp_path / "candidate-manifest.json",
            command_runner=systemd,
            validation_runner=successful_systemd_analyze,
            cycle_validator=successful_cycle_validation,
            after_activation=lambda _scheduler: (_ for _ in ()).throw(
                OSError("injected later transaction failure")
            ),
        )

    assert caught.value.code == "scheduler-convergence-rolled-back"
    assert foreign_link.is_symlink()
    assert os.readlink(foreign_link) == "../bureau-operator-control.timer"
    assert not any(command[0] in {"enable", "disable"} for command in systemd.commands)


@pytest.mark.parametrize(
    ("task_supply_intent", "task_supply_active"),
    [
        ("absent", "inactive"),
        ("disabled", "active"),
        ("enabled", "inactive"),
        ("enabled-runtime", "active"),
    ],
)
def test_required_timer_rollback_restores_exact_link_intent_and_active_state(
    tmp_path: Path,
    task_supply_intent: str,
    task_supply_active: str,
) -> None:
    release, release_id, source_commit = scheduler_release(tmp_path)
    unit_root = tmp_path / "systemd/user"
    libexec_root = tmp_path / "libexec"
    unit_root.mkdir(parents=True)
    libexec_root.mkdir()
    for name in refresh.RUNTIME_SCHEDULER_NAMES:
        if name == refresh.REQUIRED_RUNTIME_TIMER and task_supply_intent == "absent":
            continue
        for suffix in ("service", "timer"):
            (unit_root / f"{name}.{suffix}").write_text(
                f"preimage {name}.{suffix}\n", encoding="utf-8"
            )
        executable = libexec_root / name
        executable.write_text(f"preimage {name}\n", encoding="utf-8")
        executable.chmod(0o755)
    configured = (
        {}
        if task_supply_intent == "absent"
        else {
            refresh.REQUIRED_RUNTIME_TIMER: (
                task_supply_intent,
                task_supply_active,
            )
        }
    )
    systemd = FakeUserSystemd(unit_root, timer_states=configured)
    state_before = json.loads(json.dumps(systemd.states))
    persistent_link = systemd._enablement_link(
        "bureau-task-supply.timer", runtime=False
    )
    runtime_link = systemd._enablement_link("bureau-task-supply.timer", runtime=True)

    def link_preimage(path: Path) -> tuple[str, str | None]:
        return (
            ("symlink", os.readlink(path))
            if path.is_symlink()
            else ("absent", None)
        )

    links_before = {
        persistent_link: link_preimage(persistent_link),
        runtime_link: link_preimage(runtime_link),
    }
    rollback = tmp_path / "rollback"
    rollback.mkdir()

    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.converge_user_scheduler(
            source_commit=source_commit,
            release_id=release_id,
            release=release,
            user_unit_dir=unit_root,
            libexec_dir=libexec_root,
            runtime_user_unit_dir=systemd.runtime_unit_root,
            rollback_directory=rollback,
            manifest_path=tmp_path / "candidate-manifest.json",
            command_runner=systemd,
            validation_runner=successful_systemd_analyze,
            cycle_validator=successful_cycle_validation,
            after_activation=lambda _scheduler: (_ for _ in ()).throw(
                OSError("injected post-convergence failure")
            ),
        )

    assert caught.value.code == "scheduler-convergence-rolled-back"
    assert systemd.states == state_before
    assert {path: link_preimage(path) for path in links_before} == links_before
    assert not any(command[0] in {"enable", "disable"} for command in systemd.commands)
    preimage = json.loads(
        Path(caught.value.details["preimage_path"]).read_text(encoding="utf-8")
    )
    assert {
        record["path"]: record["preimage_kind"]
        for record in preimage["enablement_links"]
    } == {
        str(persistent_link): links_before[persistent_link][0],
        str(runtime_link): links_before[runtime_link][0],
    }


def test_partial_systemd_failure_restores_exact_fragment_and_state_preimage(
    tmp_path: Path,
) -> None:
    release, release_id, source_commit = scheduler_release(tmp_path)
    unit_root = tmp_path / "systemd/user"
    libexec_root = tmp_path / "libexec"
    unit_root.mkdir(parents=True)
    libexec_root.mkdir()
    original: dict[Path, tuple[bytes, int]] = {}
    for name in refresh.RUNTIME_SCHEDULER_NAMES:
        for suffix in ("service", "timer"):
            path = unit_root / f"{name}.{suffix}"
            path.write_bytes(f"preimage {name}.{suffix}\n".encode())
            path.chmod(0o644)
            original[path] = (path.read_bytes(), 0o644)
        path = libexec_root / name
        path.write_bytes(f"preimage {name}\n".encode())
        path.chmod(0o755)
        original[path] = (path.read_bytes(), 0o755)
    configured = {
        "bureau-curator": ("enabled", "active"),
        "bureau-operator-control": ("disabled", "inactive"),
    }
    systemd = FakeUserSystemd(
        unit_root,
        timer_states=configured,
        fail_once=("start", "bureau-task-supply.timer"),
    )
    state_before = json.loads(json.dumps(systemd.states))
    rollback = tmp_path / "rollback"
    rollback.mkdir()

    previous_umask = os.umask(0o077)
    try:
        with pytest.raises(refresh.RuntimeRefreshError) as caught:
            refresh.converge_user_scheduler(
                source_commit=source_commit,
                release_id=release_id,
                release=release,
                user_unit_dir=unit_root,
                libexec_dir=libexec_root,
                runtime_user_unit_dir=systemd.runtime_unit_root,
                rollback_directory=rollback,
                manifest_path=tmp_path / "candidate-manifest.json",
                command_runner=systemd,
                validation_runner=successful_systemd_analyze,
                cycle_validator=successful_cycle_validation,
            )
    finally:
        os.umask(previous_umask)

    assert caught.value.code == "scheduler-convergence-rolled-back"
    assert caught.value.details["safe_to_retry"] is False
    assert Path(caught.value.details["preimage_path"]).is_file()
    assert systemd.states == state_before
    for path, (content, mode) in original.items():
        assert path.read_bytes() == content
        assert path.stat().st_mode & 0o777 == mode
    assert systemd.commands.count(("daemon-reload",)) == 2


def test_failed_required_service_readback_is_rolled_back_without_blind_retry(
    tmp_path: Path,
) -> None:
    release, release_id, source_commit = scheduler_release(tmp_path)
    unit_root = tmp_path / "systemd/user"
    libexec_root = tmp_path / "libexec"
    unit_root.mkdir(parents=True)
    libexec_root.mkdir()
    for name in refresh.RUNTIME_SCHEDULER_NAMES:
        for suffix in ("service", "timer"):
            (unit_root / f"{name}.{suffix}").write_text(
                f"previous {name}.{suffix}\n", encoding="utf-8"
            )
        executable = libexec_root / name
        executable.write_text(f"previous {name}\n", encoding="utf-8")
        executable.chmod(0o755)
    systemd = FakeUserSystemd(unit_root)
    state_before = json.loads(json.dumps(systemd.states))
    injected = False

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        nonlocal injected
        result = systemd(argv)
        if tuple(argv[2:]) == ("start", "bureau-task-supply.timer"):
            systemd.states["bureau-task-supply.service"]["ActiveState"] = "failed"
            systemd.states["bureau-task-supply.service"]["SubState"] = "failed"
            systemd.states["bureau-task-supply.service"]["Result"] = "exit-code"
            injected = True
        return result

    rollback = tmp_path / "rollback"
    rollback.mkdir()
    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.converge_user_scheduler(
            source_commit=source_commit,
            release_id=release_id,
            release=release,
            user_unit_dir=unit_root,
            libexec_dir=libexec_root,
            runtime_user_unit_dir=systemd.runtime_unit_root,
            rollback_directory=rollback,
            manifest_path=tmp_path / "candidate-manifest.json",
            command_runner=runner,
            validation_runner=successful_systemd_analyze,
            cycle_validator=successful_cycle_validation,
        )

    assert injected is True
    assert caught.value.code == "scheduler-convergence-rolled-back"
    assert caught.value.details["cause"]["code"] == "scheduler-required-service-invalid"
    assert caught.value.details["safe_to_retry"] is False
    assert systemd.states == state_before


def test_systemd_analyze_failure_restores_files_without_activation(
    tmp_path: Path,
) -> None:
    release, release_id, source_commit = scheduler_release(tmp_path)
    unit_root = tmp_path / "systemd/user"
    libexec_root = tmp_path / "libexec"
    unit_root.mkdir(parents=True)
    libexec_root.mkdir()
    original: dict[Path, tuple[bytes, int]] = {}
    for name in refresh.RUNTIME_SCHEDULER_NAMES:
        for suffix in ("service", "timer"):
            path = unit_root / f"{name}.{suffix}"
            path.write_bytes(f"preimage {name}.{suffix}\n".encode())
            path.chmod(0o640)
            original[path] = (path.read_bytes(), 0o640)
        path = libexec_root / name
        path.write_bytes(f"preimage {name}\n".encode())
        path.chmod(0o700)
        original[path] = (path.read_bytes(), 0o700)
    systemd = FakeUserSystemd(unit_root)
    state_before = json.loads(json.dumps(systemd.states))
    validation_commands: list[list[str]] = []

    def reject_candidate(argv: list[str]) -> subprocess.CompletedProcess[str]:
        validation_commands.append(argv)
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="injected unit verification failure"
        )

    rollback = tmp_path / "rollback"
    rollback.mkdir()
    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.converge_user_scheduler(
            source_commit=source_commit,
            release_id=release_id,
            release=release,
            user_unit_dir=unit_root,
            libexec_dir=libexec_root,
            runtime_user_unit_dir=systemd.runtime_unit_root,
            rollback_directory=rollback,
            manifest_path=tmp_path / "candidate-manifest.json",
            command_runner=systemd,
            validation_runner=reject_candidate,
            cycle_validator=lambda **_: pytest.fail(
                "cycle validation must not follow failed systemd verification"
            ),
        )

    assert caught.value.code == "scheduler-convergence-rolled-back"
    assert caught.value.details["cause"]["code"] == (
        "scheduler-candidate-systemd-verify-failed"
    )
    assert validation_commands[0][:3] == ["systemd-analyze", "--user", "verify"]
    assert not any(
        command[0] in {"daemon-reload", "enable", "start"}
        for command in systemd.commands
    )
    assert systemd.states == state_before
    for path, (content, mode) in original.items():
        assert path.read_bytes() == content
        assert path.stat().st_mode & 0o777 == mode


def test_candidate_cycle_deployment_drift_prevents_activation(tmp_path: Path) -> None:
    release, release_id, source_commit = scheduler_release(tmp_path)
    unit_root = tmp_path / "systemd/user"
    libexec_root = tmp_path / "libexec"
    unit_root.mkdir(parents=True)
    libexec_root.mkdir()
    systemd = FakeUserSystemd(unit_root)
    rollback = tmp_path / "rollback"
    rollback.mkdir()

    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.converge_user_scheduler(
            source_commit=source_commit,
            release_id=release_id,
            release=release,
            user_unit_dir=unit_root,
            libexec_dir=libexec_root,
            runtime_user_unit_dir=systemd.runtime_unit_root,
            rollback_directory=rollback,
            manifest_path=tmp_path / "candidate-manifest.json",
            command_runner=systemd,
            validation_runner=successful_systemd_analyze,
            cycle_validator=lambda **_: {
                "status": "drift",
                "activatable": False,
                "read_only": True,
                "self_heal": False,
                "findings": [{"code": "injected-drift"}],
            },
        )

    assert caught.value.details["cause"]["code"] == (
        "scheduler-candidate-cycle-deployment-drift"
    )
    assert not any(
        command[0] in {"daemon-reload", "enable", "start"}
        for command in systemd.commands
    )
    for name in refresh.RUNTIME_SCHEDULER_NAMES:
        assert not (unit_root / f"{name}.service").exists()
        assert not (unit_root / f"{name}.timer").exists()
        assert not (libexec_root / name).exists()


def test_incomplete_post_effect_recovery_is_explicit(tmp_path: Path) -> None:
    release, release_id, source_commit = scheduler_release(tmp_path)
    unit_root = tmp_path / "systemd/user"
    libexec_root = tmp_path / "libexec"
    unit_root.mkdir(parents=True)
    libexec_root.mkdir()
    rollback = tmp_path / "rollback"
    rollback.mkdir()
    systemd = FakeUserSystemd(unit_root)

    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.converge_user_scheduler(
            source_commit=source_commit,
            release_id=release_id,
            release=release,
            user_unit_dir=unit_root,
            libexec_dir=libexec_root,
            runtime_user_unit_dir=systemd.runtime_unit_root,
            rollback_directory=rollback,
            manifest_path=tmp_path / "candidate-manifest.json",
            command_runner=systemd,
            validation_runner=successful_systemd_analyze,
            cycle_validator=successful_cycle_validation,
            after_activation=lambda _scheduler: (_ for _ in ()).throw(
                OSError("injected post-effect failure")
            ),
            rollback_effect=lambda: [
                {
                    "operation": ["verify-preimage", "manifest"],
                    "error": "injected incomplete restoration",
                }
            ],
        )

    assert caught.value.code == "scheduler-convergence-recovery-required"
    assert caught.value.details["safe_to_retry"] is False
    assert caught.value.details["rollback_failures"] == [
        {
            "operation": ["verify-preimage", "manifest"],
            "error": "injected incomplete restoration",
        }
    ]


def test_runtime_approval_requires_minimum_remaining_lifetime(tmp_path: Path) -> None:
    observed, _ = candidate(tmp_path)
    authority_store = seed_authority_store(tmp_path / "bureau-state", "BUR-2026-003-T009")
    intent, intent_path = refresh.prepare_intent(
        candidate=observed,
        state_root=(tmp_path / "short-state").resolve(),
        prefix=(tmp_path / "short-prefix").resolve(),
        bin_dir=(tmp_path / "short-bin").resolve(),
        remote_url="file:///tmp/bureau.git",
        authorized_by="chatgpt",
        authorization="Explicit short-lived break-glass approval.",
        break_glass=True,
        approval_reference=observed["target_sha256"],
        approval_task_id="BUR-2026-003-T009",
        ttl_seconds=599,
        now=NOW,
        authority_store=authority_store,
    )

    with pytest.raises(refresh.RuntimeRefreshError) as error:
        refresh.validate_runtime_approval_intent(
            intent_path,
            now=NOW,
            minimum_remaining_seconds=600,
        )

    assert error.value.code == "runtime-approval-validity-too-short"
    assert error.value.details == {
        "remaining_seconds": 599,
        "minimum_remaining_seconds": 600,
    }
    assert not (Path(intent["state_root"]) / "attempts" / intent["target_sha256"]).exists()


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


def test_legacy_cutover_requires_started_attempt_manifest_and_live_leases(
    tmp_path: Path,
) -> None:
    legacy, _, manifest_path, resource_db, started = prepare_legacy_cutover(tmp_path)

    decision = refresh.validate_legacy_runtime_refresh_bootstrap(
        state_root=Path(legacy["state_root"]),
        resource_db=resource_db,
        expected_source_commit=legacy["main_commit"],
        prefix=Path(legacy["prefix"]),
        bin_dir=Path(legacy["bin_dir"]),
        manifest_path=manifest_path,
        now=NOW,
    )

    assert decision["allowed"] is True
    assert decision["required_level"] == "legacy_runtime_operator_gate"
    assert decision["expected_reference"] == legacy["target_sha256"]
    assert decision["legacy_cutover"]["intent_sha256"] == legacy["intent_sha256"]
    assert decision["legacy_cutover"]["start_sha256"] == started["start_sha256"]

    result_path = Path(legacy["state_root"]) / "attempts" / legacy["target_sha256"] / "result.json"
    refresh.create_only(
        result_path,
        refresh.canonical_bytes({"status": "failed-before-cutover"}),
    )
    with pytest.raises(refresh.RuntimeRefreshError) as completed:
        refresh.validate_legacy_runtime_refresh_bootstrap(
            state_root=Path(legacy["state_root"]),
            resource_db=resource_db,
            expected_source_commit=legacy["main_commit"],
            prefix=Path(legacy["prefix"]),
            bin_dir=Path(legacy["bin_dir"]),
            manifest_path=manifest_path,
            now=NOW,
        )
    assert completed.value.code == "runtime-approval-missing"


def test_legacy_cutover_rejects_reacquired_leases(tmp_path: Path) -> None:
    legacy, _, manifest_path, resource_db, _ = prepare_legacy_cutover(tmp_path)
    connection = sqlite3.connect(resource_db)
    connection.execute(
        """
        UPDATE leases
        SET acquired_at_unix = acquired_at_unix + 1,
            updated_at_unix = updated_at_unix + 1,
            expires_at_unix = expires_at_unix + 1
        """
    )
    connection.commit()
    connection.close()

    with pytest.raises(refresh.RuntimeRefreshError) as error:
        refresh.validate_legacy_runtime_refresh_bootstrap(
            state_root=Path(legacy["state_root"]),
            resource_db=resource_db,
            expected_source_commit=legacy["main_commit"],
            prefix=Path(legacy["prefix"]),
            bin_dir=Path(legacy["bin_dir"]),
            manifest_path=manifest_path,
            now=NOW,
        )

    assert error.value.code == "runtime-approval-missing"


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


def test_apply_requires_status_capsule_launcher_lease_before_effect(tmp_path: Path) -> None:
    _observed, manifest_path, intent, intent_path = prepare_candidate_intent(tmp_path)
    status_key = f"path:{Path(intent['bin_dir']) / 'bureau-status-capsule'}"
    assert status_key in intent["required_resource_keys"]

    binding, missing_db = lease_for(tmp_path / "status-missing", intent, omit={status_key})
    with pytest.raises(refresh.RuntimeRefreshError) as missing:
        refresh.apply_runtime_refresh(
            intent_path=intent_path,
            lease_binding=binding,
            manifest_path=manifest_path,
            state_root=Path(intent["state_root"]),
            resource_db=missing_db,
            now=NOW,
            observer=lambda **_: pytest.fail("observer must not run"),
            source_preparer=lambda **_: pytest.fail("source preparation must not run"),
            installer=lambda **_: pytest.fail("installer must not run"),
        )
    assert missing.value.code == "lease-resources-missing"

    binding, foreign_db = lease_for(tmp_path / "status-foreign", intent)
    connection = sqlite3.connect(foreign_db)
    connection.execute(
        "UPDATE leases SET owner_id = ? WHERE resource_key = ?",
        ("foreign-owner", status_key),
    )
    connection.commit()
    connection.close()
    with pytest.raises(refresh.RuntimeRefreshError) as foreign:
        refresh.apply_runtime_refresh(
            intent_path=intent_path,
            lease_binding=binding,
            manifest_path=manifest_path,
            state_root=Path(intent["state_root"]),
            resource_db=foreign_db,
            now=NOW,
            observer=lambda **_: pytest.fail("observer must not run"),
            source_preparer=lambda **_: pytest.fail("source preparation must not run"),
            installer=lambda **_: pytest.fail("installer must not run"),
        )
    assert foreign.value.code == "lease-owner-mismatch"


def test_lease_binding_verifies_metadata_digest_and_required_binding(tmp_path: Path) -> None:
    _, _, intent, _ = prepare_candidate_intent(tmp_path)
    required = {
        "task_id": "grabowski-task-t016",
        "operation": "runtime-refresh",
    }
    binding, live_db = lease_for(tmp_path / "metadata-live", intent, metadata=required)
    observed = refresh.validate_live_lease_binding(
        intent,
        binding,
        resource_db=live_db,
        now=NOW,
        required_metadata=required,
    )
    assert (
        observed["required_metadata_sha256"]
        == hashlib.sha256(
            json.dumps(required, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )
    assert all("metadata_json" not in item for item in observed["lease_snapshots"])

    binding, tampered_db = lease_for(
        tmp_path / "metadata-tampered",
        intent,
        metadata=required,
        metadata_digest="0" * 64,
    )
    with pytest.raises(refresh.RuntimeRefreshError) as digest_error:
        refresh.validate_live_lease_binding(
            intent,
            binding,
            resource_db=tampered_db,
            now=NOW,
            required_metadata=required,
        )
    assert digest_error.value.code == "lease-metadata-digest-mismatch"

    binding, wrong_db = lease_for(
        tmp_path / "metadata-wrong-binding",
        intent,
        metadata={**required, "operation": "other"},
    )
    with pytest.raises(refresh.RuntimeRefreshError) as binding_error:
        refresh.validate_live_lease_binding(
            intent,
            binding,
            resource_db=wrong_db,
            now=NOW,
            required_metadata=required,
        )
    assert binding_error.value.code == "lease-metadata-binding-mismatch"
    assert binding_error.value.details["mismatched"] == {
        "operation": {"expected": "runtime-refresh", "observed": "other"}
    }


def test_lease_binding_uses_lease_contract_not_aggregate_schema(
    tmp_path: Path,
) -> None:
    _, _, intent, _ = prepare_candidate_intent(tmp_path)

    for schema_version in ("1", "2", "3", "4", "99"):
        binding, resource_db = lease_for(
            tmp_path / f"schema-{schema_version}",
            intent,
            schema_version=schema_version,
        )
        if schema_version == "4":
            with sqlite3.connect(resource_db) as connection:
                connection.execute(
                    "CREATE TABLE unrelated_additive_state("
                    "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO unrelated_additive_state VALUES('proof', 'preserved')"
                )
                connection.commit()
        observed = refresh.validate_live_lease_binding(
            intent,
            binding,
            resource_db=resource_db,
            now=NOW,
        )
        assert observed["resource_db_schema_version"] == schema_version
        assert observed["resource_lease_contract_version"] == "1"


def test_lease_binding_fails_closed_on_missing_future_or_malformed_contract(
    tmp_path: Path,
) -> None:
    _, _, intent, _ = prepare_candidate_intent(tmp_path)

    binding, missing_db = lease_for(
        tmp_path / "contract-missing",
        intent,
        schema_version="4",
        lease_contract_version=None,
    )
    with pytest.raises(refresh.RuntimeRefreshError) as missing:
        refresh.validate_live_lease_binding(intent, binding, resource_db=missing_db, now=NOW)
    assert missing.value.code == "lease-contract-metadata-missing"
    assert missing.value.details["aggregate_schema"] == "4"
    assert missing.value.details["observed_rows"] == 0

    binding, future_db = lease_for(
        tmp_path / "contract-future",
        intent,
        schema_version="4",
        lease_contract_version="2",
    )
    with pytest.raises(refresh.RuntimeRefreshError) as future:
        refresh.validate_live_lease_binding(intent, binding, resource_db=future_db, now=NOW)
    assert future.value.code == "lease-contract-version-unsupported"
    assert future.value.details["observed"] == "2"
    assert future.value.details["supported"] == ["1"]
    assert future.value.details["aggregate_schema"] == "4"

    binding, malformed_db = lease_for(
        tmp_path / "contract-malformed",
        intent,
        lease_contract_version="not-a-version",
    )
    with pytest.raises(refresh.RuntimeRefreshError) as malformed:
        refresh.validate_live_lease_binding(intent, binding, resource_db=malformed_db, now=NOW)
    assert malformed.value.code == "lease-contract-version-malformed"


def test_lease_contract_is_validated_before_lease_rows_are_read(tmp_path: Path) -> None:
    _, _, intent, _ = prepare_candidate_intent(tmp_path)
    database = tmp_path / "metadata-only/resources.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO metadata VALUES('schema_version', '4')")
        connection.commit()
    database.chmod(0o600)
    binding = {"owner_id": "chatgpt-t016", "task_id": "grabowski-task-t016"}

    with pytest.raises(refresh.RuntimeRefreshError) as missing:
        refresh.validate_live_lease_binding(intent, binding, resource_db=database, now=NOW)

    assert missing.value.code == "lease-contract-metadata-missing"


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


def test_success_consumes_authority_and_exact_result_replay_is_idempotent(
    tmp_path: Path,
) -> None:
    (
        observed,
        manifest_path,
        intent,
        intent_path,
        store,
        result,
        resource_db,
    ) = apply_successfully(tmp_path)
    current = store.task_spec(intent["approval_task_id"])
    assert current is not None
    authority = current["spec"]["metadata"]["runtime_refresh_authority"]
    consumption = authority["consumption"]
    assert consumption == {
        "schema_version": 1,
        "kind": refresh.RUNTIME_AUTHORITY_CONSUMPTION_KIND,
        "status": "consumed",
        "task_id": intent["approval_task_id"],
        "authority_revision": intent["authority_task_spec"]["revision"],
        "authority_spec_sha256": intent["authority_task_spec"]["spec_sha256"],
        "target_sha256": intent["target_sha256"],
        "intent_sha256": intent["intent_sha256"],
        "result_sha256": result["result_sha256"],
        "consumed_at": refresh.isoformat(NOW),
    }
    revision_after_success = current["revision"]

    replay = refresh.apply_runtime_refresh(
        intent_path=intent_path,
        lease_binding={"owner_id": "ignored", "task_id": "ignored"},
        manifest_path=manifest_path,
        state_root=Path(intent["state_root"]),
        resource_db=resource_db,
        now=NOW + timedelta(seconds=1),
        observer=lambda **_: pytest.fail("result replay must not observe GitHub"),
        source_preparer=lambda **_: pytest.fail("result replay must not prepare source"),
        installer=lambda **_: pytest.fail("result replay must not install"),
    )
    assert replay["result_sha256"] == result["result_sha256"]
    assert replay["reused"] is True
    assert store.task_spec(intent["approval_task_id"])["revision"] == revision_after_success

    later = dict(observed)
    later["main_commit"] = "4" * 40
    later["target_sha256"] = "5" * 64
    later = refresh.bind_digest(later, "observation_sha256")
    with pytest.raises(refresh.RuntimeRefreshError) as error:
        refresh.prepare_intent(
            candidate=later,
            state_root=(tmp_path / "later-state").resolve(),
            prefix=(tmp_path / "prefix").resolve(),
            bin_dir=(tmp_path / "bin").resolve(),
            remote_url="file:///tmp/bureau.git",
            authorized_by="operator",
            authorization="Attempted later target approval.",
            break_glass=True,
            approval_reference=later["target_sha256"],
            approval_task_id=intent["approval_task_id"],
            now=NOW,
            authority_store=store,
        )
    assert error.value.code == "authority-already-consumed"


def test_consumption_rejects_task_spec_drift_after_target_binding(
    tmp_path: Path,
) -> None:
    observed, _manifest_path, intent, _intent_path = prepare_candidate_intent(tmp_path)
    store = authority_store_for_intent(intent)
    bound = refresh.bind_runtime_refresh_authority(store=store, intent=intent, now=NOW)
    result = refresh.bind_digest(
        {
            "schema_version": refresh.SCHEMA_VERSION,
            "kind": "bureau_runtime_refresh_result",
            "status": "deployed",
            "intent_sha256": intent["intent_sha256"],
            "target_sha256": intent["target_sha256"],
            "main_commit": intent["main_commit"],
            "authority_task_spec": bound,
            "finished_at": refresh.isoformat(NOW),
            "effect_started": True,
            "lease_binding": {"lease_binding_sha256": "a" * 64},
        },
        "result_sha256",
    )
    revise_authority(
        store,
        intent["approval_task_id"],
        lambda spec: spec.__setitem__("title", "Concurrent post-binding drift"),
        key="post-binding-drift",
    )

    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.consume_runtime_refresh_authority(
            store=store, intent=intent, result=result, now=NOW
        )

    assert caught.value.code == "authority-task-drift-after-effect"
    current = store.task_spec(intent["approval_task_id"])
    assert current is not None
    assert current["spec"]["metadata"]["runtime_refresh_authority"].get("consumption") is None
    assert observed["target_sha256"] == intent["target_sha256"]


def test_tampered_result_and_consumption_are_rejected_on_replay(tmp_path: Path) -> None:
    (
        _,
        manifest_path,
        intent,
        intent_path,
        store,
        result,
        resource_db,
    ) = apply_successfully(tmp_path)
    result_path = Path(intent["state_root"]) / "attempts" / intent["target_sha256"] / "result.json"
    tampered_result = dict(result)
    tampered_result["status"] = "failed"
    result_path.write_bytes(refresh.canonical_bytes(tampered_result))
    with pytest.raises(refresh.RuntimeRefreshError) as result_error:
        refresh.apply_runtime_refresh(
            intent_path=intent_path,
            lease_binding={"owner_id": "ignored", "task_id": "ignored"},
            manifest_path=manifest_path,
            state_root=Path(intent["state_root"]),
            resource_db=resource_db,
            now=NOW,
        )
    assert result_error.value.code == "digest-mismatch"

    result_path.write_bytes(refresh.canonical_bytes(result))

    def tamper_consumption(spec: dict[str, Any]) -> None:
        spec["metadata"]["runtime_refresh_authority"]["consumption"]["result_sha256"] = "f" * 64

    revise_authority(
        store,
        intent["approval_task_id"],
        tamper_consumption,
        key="tampered-consumption",
    )
    with pytest.raises(refresh.RuntimeRefreshError) as consumption_error:
        refresh.apply_runtime_refresh(
            intent_path=intent_path,
            lease_binding={"owner_id": "ignored", "task_id": "ignored"},
            manifest_path=manifest_path,
            state_root=Path(intent["state_root"]),
            resource_db=resource_db,
            now=NOW,
        )
    assert consumption_error.value.code == "authority-consumption-mismatch"


def test_distinct_intents_for_same_target_share_one_effect_attempt(tmp_path: Path) -> None:
    observed, manifest_path, first_intent, first_path = prepare_candidate_intent(tmp_path)
    authority_store = authority_store_for_intent(first_intent)
    second_intent, second_path = refresh.prepare_intent(
        candidate=observed,
        state_root=Path(first_intent["state_root"]),
        prefix=Path(first_intent["prefix"]),
        bin_dir=Path(first_intent["bin_dir"]),
        remote_url=first_intent["remote_url"],
        authorized_by="chatgpt",
        authorization="Second explicit authorization for the same exact target.",
        break_glass=True,
        approval_reference=observed["target_sha256"],
        approval_task_id="BUR-2026-003-T009",
        now=NOW + timedelta(seconds=1),
        authority_store=authority_store,
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


def test_apply_already_current_target_drift_closes_without_effect_ledger(
    tmp_path: Path,
) -> None:
    observed, manifest_path, intent, intent_path = prepare_candidate_intent(tmp_path)
    write_manifest(manifest_path, MAIN)
    scheduler = {
        "schema_version": refresh.SCHEMA_VERSION,
        "kind": "bureau_runtime_scheduler_readback",
        "source_commit": MAIN,
        "authoritative": True,
    }
    live = dict(observed)
    live.update(
        {
            "status": "already_current",
            "deployed_source_commit": MAIN,
            "deployed_manifest_sha256": refresh.sha256_bytes(manifest_path.read_bytes()),
            "main_commit": MAIN,
            "lag_commits": 0,
            "scheduler_target_state": "converged",
            "reason_codes": [],
            "scheduler": scheduler,
        }
    )
    live["target_sha256"] = refresh.sha256_bytes(
        refresh.canonical_bytes(refresh._target_payload(live))
    )
    live = refresh.bind_digest(live, "observation_sha256")
    assert live["target_sha256"] != intent["target_sha256"]
    lease_binding, resource_db = lease_for(tmp_path / "target-drift-leases", intent)
    result = refresh.apply_runtime_refresh(
        intent_path=intent_path,
        lease_binding=lease_binding,
        manifest_path=manifest_path,
        state_root=Path(intent["state_root"]),
        resource_db=resource_db,
        now=NOW,
        observer=lambda **_: live,
        source_preparer=lambda **_: pytest.fail("no-effect must not prepare source"),
        installer=lambda **_: pytest.fail("no-effect must not install"),
    )
    assert result["status"] == "already_current"
    assert result["effect_started"] is False
    assert result["observed_target_sha256"] == live["target_sha256"]
    attempt_dir = Path(intent["state_root"]) / "attempts" / intent["target_sha256"]
    assert not (attempt_dir / "started.json").exists()
    assert not (attempt_dir / "result.json").exists()
    no_effect = Path(intent["state_root"]) / "no-effect-results" / f"{intent['intent_sha256']}.json"
    assert no_effect.is_file()
    assert refresh.read_json(no_effect)["result_sha256"] == result["result_sha256"]

def test_apply_already_current_deduplicates_without_installer(tmp_path: Path) -> None:
    observed, manifest_path, intent, intent_path = prepare_candidate_intent(tmp_path)
    write_manifest(manifest_path, MAIN)
    scheduler = {
        "schema_version": refresh.SCHEMA_VERSION,
        "kind": "bureau_runtime_scheduler_readback",
        "source_commit": MAIN,
        "authoritative": True,
    }
    live = dict(observed)
    live.update(
        {
            "status": "already_current",
            "deployed_source_commit": MAIN,
            "deployed_manifest_sha256": refresh.sha256_bytes(manifest_path.read_bytes()),
            "main_commit": MAIN,
            "reason_codes": [],
            "scheduler": scheduler,
        }
    )
    live = refresh.bind_digest(live, "observation_sha256")

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
    assert result["scheduler"] == scheduler
    assert result["manifest_sha256"] == live["deployed_manifest_sha256"]


def test_already_current_no_effect_authority_can_closeout(tmp_path: Path) -> None:
    observed, manifest_path, intent, intent_path = prepare_candidate_intent(tmp_path)
    write_manifest(manifest_path, MAIN)
    scheduler = {
        "schema_version": refresh.SCHEMA_VERSION,
        "kind": "bureau_runtime_scheduler_readback",
        "source_commit": MAIN,
        "authoritative": True,
        "services": {
            name: {
                "LoadState": "loaded",
                "FragmentPath": f"/units/{name}.service",
                "ActiveState": "inactive",
                "SubState": "dead",
                "Result": "success",
            }
            for name in refresh.RUNTIME_SCHEDULER_NAMES
        },
    }
    live = dict(observed)
    live.update(
        {
            "status": "already_current",
            "deployed_source_commit": MAIN,
            "deployed_manifest_sha256": refresh.sha256_bytes(manifest_path.read_bytes()),
            "main_commit": MAIN,
            "reason_codes": [],
            "scheduler": scheduler,
        }
    )
    live = refresh.bind_digest(live, "observation_sha256")
    lease_binding, resource_db = lease_for(tmp_path / "no-effect-leases", intent)
    store = authority_store_for_intent(intent)

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
        authority_store=store,
    )
    assert result["status"] == "already_current"
    assert result["effect_started"] is False
    effect_attempt = Path(intent["state_root"]) / "attempts" / intent["target_sha256"]
    assert not (effect_attempt / "started.json").exists()
    assert not (effect_attempt / "result.json").exists()
    assert (
        Path(intent["state_root"])
        / "no-effect-results"
        / f"{intent['intent_sha256']}.json"
    ).is_file()
    release_test_leases(resource_db)

    closeout = refresh.closeout_runtime_refresh_authority(
        state_root=Path(intent["state_root"]),
        approval_task_id=intent["approval_task_id"],
        target_sha256=intent["target_sha256"],
        intent_sha256=intent["intent_sha256"],
        result_sha256=result["result_sha256"],
        resource_db=resource_db,
        now=NOW + timedelta(minutes=20),
        authority_store=store,
        scheduler_readback=lambda receipt, **_: {
            **receipt,
            "services": {
                name: (
                    {
                        **state,
                        "ActiveState": "activating",
                        "SubState": "start",
                    }
                    if name == refresh.REQUIRED_RUNTIME_TIMER
                    else dict(state)
                )
                for name, state in receipt["services"].items()
            },
        },
    )

    assert closeout["closeout"]["status"] == "verified"
    assert closeout["closeout"]["runtime_result_sha256"] == result["result_sha256"]
    current = store.task_spec(intent["approval_task_id"])
    assert current is not None
    assert current["spec"]["state"] == "verified"


@pytest.mark.parametrize(
    "task_id",
    [
        "BUREAU-CONTROL-PLANE-V3-FB-RUNTIME-REFRESH-BROWSER-CONTROL-RESOURCE-20260811",
        "BUREAU-TRUTH-MODEL-V2-T029",
    ],
)
def test_no_run_closeout_is_receipt_bound_and_idempotent(tmp_path: Path, task_id: str) -> None:
    intent, store, result, resource_db = historical_no_run_success(tmp_path, task_id=task_id)
    assert "authority_state_store" not in intent
    assert "authority_task_spec" not in intent
    assert store.list_runs() == []
    release_test_leases(resource_db)

    closeout = refresh.closeout_runtime_refresh_authority(
        state_root=Path(intent["state_root"]),
        approval_task_id=task_id,
        target_sha256=intent["target_sha256"],
        intent_sha256=intent["intent_sha256"],
        result_sha256=result["result_sha256"],
        resource_db=resource_db,
        now=NOW + timedelta(minutes=20),
        authority_store=store,
        readback=lambda **_: result["readback"],
    )
    current = store.task_spec(task_id)
    assert current is not None
    assert current["spec"]["state"] == "verified"
    assert closeout["closeout"]["task_id"] == task_id
    assert closeout["closeout"]["runtime_result_sha256"] == result["result_sha256"]
    assert closeout["closeout"]["target_sha256"] == intent["target_sha256"]
    assert closeout["closeout"]["does_not_establish"] == [
        "retroactive_claim_authority",
        "synthetic_run_authority",
        "runtime_authority_for_later_targets",
        "future_runtime_health",
    ]
    terminal_revision = current["revision"]
    assert store.list_runs() == []

    replay = refresh.closeout_runtime_refresh_authority(
        state_root=Path(intent["state_root"]),
        approval_task_id=task_id,
        target_sha256=intent["target_sha256"],
        intent_sha256=intent["intent_sha256"],
        result_sha256=result["result_sha256"],
        resource_db=resource_db,
        now=NOW + timedelta(minutes=21),
        authority_store=store,
        readback=lambda **_: result["readback"],
    )
    assert replay["idempotent_replay"] is True
    assert store.task_spec(task_id)["revision"] == terminal_revision
    assert store.list_runs() == []


def test_no_run_closeout_rejects_cross_bound_acceptance_evidence_on_replay(
    tmp_path: Path,
) -> None:
    task_id = "BUREAU-NO-RUN-CROSS-BOUND-ACCEPTANCE-EVIDENCE"
    intent, store, result, resource_db = historical_no_run_success(tmp_path, task_id=task_id)
    release_test_leases(resource_db)
    closeout = refresh.closeout_runtime_refresh_authority(
        state_root=Path(intent["state_root"]),
        approval_task_id=task_id,
        target_sha256=intent["target_sha256"],
        intent_sha256=intent["intent_sha256"],
        result_sha256=result["result_sha256"],
        resource_db=resource_db,
        now=NOW + timedelta(minutes=20),
        authority_store=store,
        readback=lambda **_: result["readback"],
    )
    assert closeout["closeout"]["status"] == "verified"

    def transplant_acceptance_evidence(spec: dict[str, Any]) -> None:
        runtime_closeout = spec["metadata"]["runtime_closeout"]
        evidence = json.loads(json.dumps(runtime_closeout["acceptance_evidence"]))
        evidence["runtime_result_sha256"] = "f" * 64
        evidence.pop("evidence_sha256")
        runtime_closeout["acceptance_evidence"] = refresh.bind_digest(
            evidence, "evidence_sha256"
        )

    revise_authority(
        store,
        task_id,
        transplant_acceptance_evidence,
        key="tamper:cross-bound-acceptance-evidence",
    )
    before_replay = store.task_spec(task_id)

    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.closeout_runtime_refresh_authority(
            state_root=Path(intent["state_root"]),
            approval_task_id=task_id,
            target_sha256=intent["target_sha256"],
            intent_sha256=intent["intent_sha256"],
            result_sha256=result["result_sha256"],
            resource_db=resource_db,
            now=NOW + timedelta(minutes=21),
            authority_store=store,
            readback=lambda **_: result["readback"],
        )

    assert caught.value.code == "authority-closeout-acceptance-evidence-binding-invalid"
    assert "runtime_result_sha256" in caught.value.details["mismatched"]
    assert store.task_spec(task_id) == before_replay
    assert store.list_runs() == []


@pytest.mark.parametrize(
    "field",
    [
        "run_evidence_sha256",
        "state_store_root_sha256",
        "effect_history_sha256",
    ],
)
def test_no_run_closeout_rejects_rewritten_acceptance_evidence_hash_on_replay(
    tmp_path: Path,
    field: str,
) -> None:
    task_id = f"BUREAU-NO-RUN-ACCEPTANCE-HISTORY-{field.replace('_', '-').upper()}"
    intent, store, result, resource_db = historical_no_run_success(tmp_path, task_id=task_id)
    release_test_leases(resource_db)
    closeout = refresh.closeout_runtime_refresh_authority(
        state_root=Path(intent["state_root"]),
        approval_task_id=task_id,
        target_sha256=intent["target_sha256"],
        intent_sha256=intent["intent_sha256"],
        result_sha256=result["result_sha256"],
        resource_db=resource_db,
        now=NOW + timedelta(minutes=20),
        authority_store=store,
        readback=lambda **_: result["readback"],
    )
    original = closeout["closeout"]["acceptance_evidence"][field]

    def rewrite_acceptance_evidence(spec: dict[str, Any]) -> None:
        runtime_closeout = spec["metadata"]["runtime_closeout"]
        evidence = json.loads(json.dumps(runtime_closeout["acceptance_evidence"]))
        evidence[field] = ("e" if original == "f" * 64 else "f") * 64
        evidence.pop("evidence_sha256")
        runtime_closeout["acceptance_evidence"] = refresh.bind_digest(
            evidence, "evidence_sha256"
        )

    revise_authority(
        store,
        task_id,
        rewrite_acceptance_evidence,
        key=f"tamper:acceptance-history:{field}",
        source="runtime-refresh-no-run-closeout",
    )
    before_replay = store.task_spec(task_id)

    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.closeout_runtime_refresh_authority(
            state_root=Path(intent["state_root"]),
            approval_task_id=task_id,
            target_sha256=intent["target_sha256"],
            intent_sha256=intent["intent_sha256"],
            result_sha256=result["result_sha256"],
            resource_db=resource_db,
            now=NOW + timedelta(minutes=21),
            authority_store=store,
            readback=lambda **_: result["readback"],
        )

    assert caught.value.code == "authority-closeout-acceptance-evidence-history-drift"
    assert field in caught.value.details["mismatched"]
    assert store.task_spec(task_id) == before_replay
    assert store.list_runs() == []


def test_no_run_closeout_rejects_removed_acceptance_evidence_on_replay(
    tmp_path: Path,
) -> None:
    task_id = "BUREAU-NO-RUN-REMOVED-ACCEPTANCE-EVIDENCE"
    intent, store, result, resource_db = historical_no_run_success(tmp_path, task_id=task_id)
    release_test_leases(resource_db)
    closeout = refresh.closeout_runtime_refresh_authority(
        state_root=Path(intent["state_root"]),
        approval_task_id=task_id,
        target_sha256=intent["target_sha256"],
        intent_sha256=intent["intent_sha256"],
        result_sha256=result["result_sha256"],
        resource_db=resource_db,
        now=NOW + timedelta(minutes=20),
        authority_store=store,
        readback=lambda **_: result["readback"],
    )
    assert closeout["closeout"]["acceptance_evidence"]

    def remove_acceptance_capsule(spec: dict[str, Any]) -> None:
        spec["metadata"]["runtime_closeout"].pop("acceptance_evidence")

    revise_authority(
        store,
        task_id,
        remove_acceptance_capsule,
        key="tamper:remove-typed-acceptance-evidence",
    )
    before_replay = store.task_spec(task_id)

    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.closeout_runtime_refresh_authority(
            state_root=Path(intent["state_root"]),
            approval_task_id=task_id,
            target_sha256=intent["target_sha256"],
            intent_sha256=intent["intent_sha256"],
            result_sha256=result["result_sha256"],
            resource_db=resource_db,
            now=NOW + timedelta(minutes=21),
            authority_store=store,
            readback=lambda **_: result["readback"],
        )

    assert caught.value.code == "authority-closeout-acceptance-evidence-missing"
    assert store.task_spec(task_id) == before_replay
    assert store.list_runs() == []


def test_no_run_closeout_rejects_missing_required_evidence_class_on_replay(
    tmp_path: Path,
) -> None:
    task_id = "BUREAU-NO-RUN-MISSING-REQUIRED-EVIDENCE-CLASS"
    intent, store, result, resource_db = historical_no_run_success(tmp_path, task_id=task_id)
    release_test_leases(resource_db)
    closeout = refresh.closeout_runtime_refresh_authority(
        state_root=Path(intent["state_root"]),
        approval_task_id=task_id,
        target_sha256=intent["target_sha256"],
        intent_sha256=intent["intent_sha256"],
        result_sha256=result["result_sha256"],
        resource_db=resource_db,
        now=NOW + timedelta(minutes=20),
        authority_store=store,
        readback=lambda **_: result["readback"],
    )
    assert closeout["closeout"]["status"] == "verified"

    def drop_required_evidence(spec: dict[str, Any]) -> None:
        runtime_closeout = spec["metadata"]["runtime_closeout"]
        evidence = json.loads(json.dumps(runtime_closeout["acceptance_evidence"]))
        evidence["available_evidence"].remove("state-store-integrity")
        evidence.pop("evidence_sha256")
        runtime_closeout["acceptance_evidence"] = refresh.bind_digest(
            evidence, "evidence_sha256"
        )

    revise_authority(
        store,
        task_id,
        drop_required_evidence,
        key="tamper:drop-required-acceptance-evidence-class",
    )
    before_replay = store.task_spec(task_id)

    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.closeout_runtime_refresh_authority(
            state_root=Path(intent["state_root"]),
            approval_task_id=task_id,
            target_sha256=intent["target_sha256"],
            intent_sha256=intent["intent_sha256"],
            result_sha256=result["result_sha256"],
            resource_db=resource_db,
            now=NOW + timedelta(minutes=21),
            authority_store=store,
            readback=lambda **_: result["readback"],
        )

    assert caught.value.code == "authority-closeout-acceptance-evidence-incomplete"
    assert "runtime-authority-proof" in caught.value.details["missing"]
    assert "state-store-integrity" in caught.value.details["missing"]["runtime-authority-proof"]
    assert store.task_spec(task_id) == before_replay
    assert store.list_runs() == []


def test_no_run_closeout_rejects_frozen_acceptance_contract_drift_on_replay(
    tmp_path: Path,
) -> None:
    task_id = "BUREAU-NO-RUN-FROZEN-ACCEPTANCE-CONTRACT-DRIFT"
    intent, store, result, resource_db = historical_no_run_success(tmp_path, task_id=task_id)
    release_test_leases(resource_db)
    closeout = refresh.closeout_runtime_refresh_authority(
        state_root=Path(intent["state_root"]),
        approval_task_id=task_id,
        target_sha256=intent["target_sha256"],
        intent_sha256=intent["intent_sha256"],
        result_sha256=result["result_sha256"],
        resource_db=resource_db,
        now=NOW + timedelta(minutes=20),
        authority_store=store,
        readback=lambda **_: result["readback"],
    )
    assert closeout["closeout"]["status"] == "verified"

    def expand_frozen_acceptance(spec: dict[str, Any]) -> None:
        spec["acceptance"].append(
            {
                "id": "post-closeout-proof",
                "assertion": "A later frozen criterion must have its own typed evidence.",
                "evidence_type": "object",
                "verifier": "manual_observation",
                "verifier_config": {
                    "observation_scope": f"test:{task_id}:post-closeout-proof"
                },
            }
        )
        authority = spec["metadata"]["runtime_refresh_authority"]
        authority["no_run_closeout_acceptance"]["criteria"]["post-closeout-proof"] = {
            "verifier": refresh.RUNTIME_AUTHORITY_NO_RUN_ACCEPTANCE_VERIFIER,
            "required_evidence": ["state-store-integrity"],
        }

    revise_authority(
        store,
        task_id,
        expand_frozen_acceptance,
        key="revise:frozen-acceptance-contract-after-closeout",
    )
    before_replay = store.task_spec(task_id)

    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.closeout_runtime_refresh_authority(
            state_root=Path(intent["state_root"]),
            approval_task_id=task_id,
            target_sha256=intent["target_sha256"],
            intent_sha256=intent["intent_sha256"],
            result_sha256=result["result_sha256"],
            resource_db=resource_db,
            now=NOW + timedelta(minutes=21),
            authority_store=store,
            readback=lambda **_: result["readback"],
        )

    assert caught.value.code == "authority-closeout-acceptance-contract-drift"
    assert set(caught.value.details["mismatched"]) == {
        "contract_sha256",
        "criterion_ids",
    }
    assert store.task_spec(task_id) == before_replay
    assert store.list_runs() == []


def test_no_run_closeout_rejects_frozen_acceptance_definition_drift_on_replay(
    tmp_path: Path,
) -> None:
    task_id = "BUREAU-NO-RUN-FROZEN-ACCEPTANCE-DEFINITION-DRIFT"
    intent, store, result, resource_db = historical_no_run_success(tmp_path, task_id=task_id)
    release_test_leases(resource_db)
    closeout = refresh.closeout_runtime_refresh_authority(
        state_root=Path(intent["state_root"]),
        approval_task_id=task_id,
        target_sha256=intent["target_sha256"],
        intent_sha256=intent["intent_sha256"],
        result_sha256=result["result_sha256"],
        resource_db=resource_db,
        now=NOW + timedelta(minutes=20),
        authority_store=store,
        readback=lambda **_: result["readback"],
    )
    assert closeout["closeout"]["status"] == "verified"

    def revise_frozen_criterion(spec: dict[str, Any]) -> None:
        criterion = spec["acceptance"][0]
        criterion["assertion"] = "The revised frozen criterion requires new evidence."
        criterion["verifier_config"] = {
            "observation_scope": f"test:{task_id}:revised-runtime-authority"
        }

    revise_authority(
        store,
        task_id,
        revise_frozen_criterion,
        key="revise:frozen-acceptance-definition-after-closeout",
    )
    before_replay = store.task_spec(task_id)

    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.closeout_runtime_refresh_authority(
            state_root=Path(intent["state_root"]),
            approval_task_id=task_id,
            target_sha256=intent["target_sha256"],
            intent_sha256=intent["intent_sha256"],
            result_sha256=result["result_sha256"],
            resource_db=resource_db,
            now=NOW + timedelta(minutes=21),
            authority_store=store,
            readback=lambda **_: result["readback"],
        )

    assert caught.value.code == "authority-closeout-acceptance-contract-drift"
    assert set(caught.value.details["mismatched"]) == {"contract_sha256"}
    assert store.task_spec(task_id) == before_replay
    assert store.list_runs() == []


def test_no_run_closeout_rejects_unbound_acceptance_task_spec_on_replay(
    tmp_path: Path,
) -> None:
    task_id = "BUREAU-NO-RUN-UNBOUND-ACCEPTANCE-TASK-SPEC"
    intent, store, result, resource_db = historical_no_run_success(tmp_path, task_id=task_id)
    release_test_leases(resource_db)
    closeout = refresh.closeout_runtime_refresh_authority(
        state_root=Path(intent["state_root"]),
        approval_task_id=task_id,
        target_sha256=intent["target_sha256"],
        intent_sha256=intent["intent_sha256"],
        result_sha256=result["result_sha256"],
        resource_db=resource_db,
        now=NOW + timedelta(minutes=20),
        authority_store=store,
        readback=lambda **_: result["readback"],
    )
    assert closeout["closeout"]["status"] == "verified"

    def transplant_task_spec_binding(spec: dict[str, Any]) -> None:
        runtime_closeout = spec["metadata"]["runtime_closeout"]
        evidence = json.loads(json.dumps(runtime_closeout["acceptance_evidence"]))
        evidence["task_spec_sha256"] = "f" * 64
        evidence.pop("evidence_sha256")
        runtime_closeout["acceptance_evidence"] = refresh.bind_digest(
            evidence, "evidence_sha256"
        )

    revise_authority(
        store,
        task_id,
        transplant_task_spec_binding,
        key="tamper:unbound-acceptance-task-spec",
    )
    before_replay = store.task_spec(task_id)

    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.closeout_runtime_refresh_authority(
            state_root=Path(intent["state_root"]),
            approval_task_id=task_id,
            target_sha256=intent["target_sha256"],
            intent_sha256=intent["intent_sha256"],
            result_sha256=result["result_sha256"],
            resource_db=resource_db,
            now=NOW + timedelta(minutes=21),
            authority_store=store,
            readback=lambda **_: result["readback"],
        )

    assert caught.value.code == "authority-closeout-acceptance-task-spec-binding-invalid"
    assert caught.value.details["task_spec_sha256"] == "f" * 64
    assert store.task_spec(task_id) == before_replay
    assert store.list_runs() == []


def test_no_run_closeout_rejects_source_precondition_contract_drift_on_replay(
    tmp_path: Path,
) -> None:
    task_id = "BUREAU-NO-RUN-SOURCE-PRECONDITION-CONTRACT-DRIFT"
    observed, manifest_path, intent, intent_path = prepare_source_precondition_intent(
        tmp_path, task_id=task_id
    )
    store = authority_store_for_intent(intent)
    binding, resource_db = lease_for(tmp_path / "source-precondition-replay-leases", intent)

    def source_preparer(**kwargs: Any) -> dict[str, Any]:
        kwargs["workspace"].mkdir(parents=True)
        return {
            "head": MAIN,
            "root": str(kwargs["workspace"]),
            "dirty": False,
            "detached": True,
        }

    result = refresh.apply_runtime_refresh(
        intent_path=intent_path,
        lease_binding=binding,
        manifest_path=manifest_path,
        state_root=Path(intent["state_root"]),
        resource_db=resource_db,
        now=NOW,
        observer=lambda **_: observed,
        source_preparer=source_preparer,
        installer=lambda **_: {
            "manifest_sha256": "a" * 64,
            "rollback": {"directory": "/rollback"},
        },
        readback=lambda **_: {
            "source_commit": MAIN,
            "manifest_sha256": "a" * 64,
            "check_valid": True,
            "runtime_identity_valid": True,
        },
    )
    assert result["status"] == "deployed"
    release_test_leases(resource_db)
    closeout = refresh.closeout_runtime_refresh_authority(
        state_root=Path(intent["state_root"]),
        approval_task_id=task_id,
        target_sha256=intent["target_sha256"],
        intent_sha256=intent["intent_sha256"],
        result_sha256=result["result_sha256"],
        resource_db=resource_db,
        now=NOW + timedelta(minutes=20),
        authority_store=store,
        readback=lambda **_: result["readback"],
    )
    assert closeout["closeout"]["status"] == "verified"

    def drift_source_precondition(spec: dict[str, Any]) -> None:
        authority = spec["metadata"]["runtime_refresh_authority"]
        authority["source_precondition"]["registered_manifest_sha256"] = "b" * 64

    revise_authority(
        store,
        task_id,
        drift_source_precondition,
        key="revise:source-precondition-contract-after-closeout",
    )
    before_replay = store.task_spec(task_id)

    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.closeout_runtime_refresh_authority(
            state_root=Path(intent["state_root"]),
            approval_task_id=task_id,
            target_sha256=intent["target_sha256"],
            intent_sha256=intent["intent_sha256"],
            result_sha256=result["result_sha256"],
            resource_db=resource_db,
            now=NOW + timedelta(minutes=21),
            authority_store=store,
            readback=lambda **_: result["readback"],
        )

    assert caught.value.code == "authority-closeout-acceptance-contract-drift"
    assert set(caught.value.details["mismatched"]) == {"contract_sha256"}
    assert store.task_spec(task_id) == before_replay
    assert store.list_runs() == []


def test_no_run_closeout_rejects_missing_frozen_acceptance_mapping(tmp_path: Path) -> None:
    task_id = "BUREAU-NO-RUN-MISSING-ACCEPTANCE-MAPPING"
    intent, store, result, resource_db = historical_no_run_success(tmp_path, task_id=task_id)
    revise_authority(
        store,
        task_id,
        lambda spec: spec["metadata"]["runtime_refresh_authority"].pop(
            "no_run_closeout_acceptance"
        ),
        key="remove:no-run-acceptance-mapping",
    )
    release_test_leases(resource_db)
    before = store.task_spec(task_id)

    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.closeout_runtime_refresh_authority(
            state_root=Path(intent["state_root"]),
            approval_task_id=task_id,
            target_sha256=intent["target_sha256"],
            intent_sha256=intent["intent_sha256"],
            result_sha256=result["result_sha256"],
            resource_db=resource_db,
            now=NOW + timedelta(minutes=20),
            authority_store=store,
            readback=lambda **_: result["readback"],
        )

    assert caught.value.code == "authority-closeout-acceptance-contract-missing"
    assert store.task_spec(task_id) == before


def test_no_run_closeout_rejects_historical_intent_without_current_source_precondition(
    tmp_path: Path,
) -> None:
    task_id = "BUREAU-NO-RUN-HISTORICAL-INTENT-WITHOUT-SOURCE-PRECONDITION"
    intent, store, result, resource_db = historical_no_run_success(tmp_path, task_id=task_id)

    def require_source_precondition(spec: dict[str, Any]) -> None:
        authority = spec["metadata"]["runtime_refresh_authority"]
        authority["mode"] = refresh.RUNTIME_AUTHORITY_MODE_SOURCE_PRECONDITION
        authority["source_precondition"] = source_precondition_contract()
        authority["no_run_closeout_acceptance"]["criteria"]["runtime-authority-proof"][
            "required_evidence"
        ].append("source-precondition")

    revise_authority(
        store,
        task_id,
        require_source_precondition,
        key="require:source-precondition-after-historical-effect",
    )
    release_test_leases(resource_db)
    before = store.task_spec(task_id)

    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.closeout_runtime_refresh_authority(
            state_root=Path(intent["state_root"]),
            approval_task_id=task_id,
            target_sha256=intent["target_sha256"],
            intent_sha256=intent["intent_sha256"],
            result_sha256=result["result_sha256"],
            resource_db=resource_db,
            now=NOW + timedelta(minutes=20),
            authority_store=store,
            readback=lambda **_: result["readback"],
        )

    assert caught.value.code == "authority-closeout-source-precondition-unproven"
    assert store.task_spec(task_id) == before


def test_historical_readback_uses_release_bound_scheduler_semantics(
    tmp_path: Path,
) -> None:
    task_id = (
        "BUREAU-CONTROL-PLANE-V3-FB-RUNTIME-REFRESH-AFTER-PR2063-"
        "AGENT-COMPETITION-PUBLISH-20260818"
    )
    intent, store, result, resource_db = historical_no_run_success(tmp_path, task_id=task_id)
    historical_scheduler_names = (
        "bureau-halfhour-operator",
        "bureau-curator",
        "bureau-operator-control",
        "bureau-verifier-control",
        "bureau-closure-planner",
    )
    assert historical_scheduler_names != runtime_identity.SCHEDULER_NAMES
    assert "bureau-task-supply" in runtime_identity.SCHEDULER_NAMES
    install_receipt, historical_readback, artifacts = historical_runtime_artifacts(
        tmp_path,
        intent,
        runtime_scheduler_names=historical_scheduler_names,
    )
    assert runtime_identity._package_tree_sha256(artifacts["release"]) is None
    assert (
        refresh._historical_package_tree_sha256(artifacts["release"])
        == install_receipt["package_tree_sha256"]
        == historical_readback["package_tree_sha256"]
    )
    result = replace_historical_result(
        intent,
        result,
        install_receipt=install_receipt,
        readback=historical_readback,
    )
    release_test_leases(resource_db)

    closeout = refresh.closeout_runtime_refresh_authority(
        state_root=Path(intent["state_root"]),
        approval_task_id=task_id,
        target_sha256=intent["target_sha256"],
        intent_sha256=intent["intent_sha256"],
        result_sha256=result["result_sha256"],
        resource_db=resource_db,
        now=NOW + timedelta(minutes=20),
        authority_store=store,
    )

    assert closeout["closeout"]["status"] == "verified"
    current = store.task_spec(task_id)
    assert current is not None
    assert current["spec"]["state"] == "verified"


def test_no_run_closeout_authenticates_historical_runtime_after_successor_deploy(
    tmp_path: Path,
) -> None:
    task_id = "BUREAU-CONTROL-PLANE-V3-FB-RUNTIME-REFRESH-BROWSER-CONTROL-RESOURCE-20260811"
    intent, store, result, resource_db = historical_no_run_success(tmp_path, task_id=task_id)
    install_receipt, historical_readback, artifacts = historical_runtime_artifacts(tmp_path, intent)
    result = replace_historical_result(
        intent,
        result,
        install_receipt=install_receipt,
        readback=historical_readback,
    )
    release_test_leases(resource_db)
    successor_manifest = artifacts["manifest"].read_bytes()

    closeout = refresh.closeout_runtime_refresh_authority(
        state_root=Path(intent["state_root"]),
        approval_task_id=task_id,
        target_sha256=intent["target_sha256"],
        intent_sha256=intent["intent_sha256"],
        result_sha256=result["result_sha256"],
        resource_db=resource_db,
        now=NOW + timedelta(minutes=20),
        authority_store=store,
    )

    current = store.task_spec(task_id)
    assert current is not None
    assert current["spec"]["state"] == "verified"
    assert closeout["closeout"]["runtime_result_sha256"] == result["result_sha256"]
    assert artifacts["manifest"].read_bytes() == successor_manifest
    assert json.loads(successor_manifest)["source_commit"] == "9" * 40
    assert historical_readback["source_commit"] == intent["main_commit"]


def test_no_run_closeout_accepts_result_bound_scheduler_missing_from_historical_readback(
    tmp_path: Path,
) -> None:
    task_id = "BUREAU-CONTROL-PLANE-V3-FB-RUNTIME-REFRESH-BROWSER-CONTROL-RESOURCE-20260811"
    intent, store, result, resource_db = historical_no_run_success(tmp_path, task_id=task_id)
    scheduler = {
        "schema_version": refresh.SCHEMA_VERSION,
        "kind": "bureau_runtime_scheduler_readback",
        "source_commit": intent["main_commit"],
        "authoritative": True,
    }
    install_receipt, historical_readback, _ = historical_runtime_artifacts(
        tmp_path, intent, scheduler=scheduler
    )
    assert "scheduler" not in historical_readback
    result = replace_historical_result(
        intent,
        result,
        install_receipt=install_receipt,
        readback={**historical_readback, "scheduler": scheduler},
    )
    release_test_leases(resource_db)

    closeout = refresh.closeout_runtime_refresh_authority(
        state_root=Path(intent["state_root"]),
        approval_task_id=task_id,
        target_sha256=intent["target_sha256"],
        intent_sha256=intent["intent_sha256"],
        result_sha256=result["result_sha256"],
        resource_db=resource_db,
        now=NOW + timedelta(minutes=20),
        authority_store=store,
    )

    assert closeout["closeout"]["status"] == "verified"
    current = store.task_spec(task_id)
    assert current is not None
    assert current["spec"]["state"] == "verified"


def test_no_run_closeout_rejects_scheduler_not_bound_to_install_receipt(
    tmp_path: Path,
) -> None:
    task_id = "BUREAU-CONTROL-PLANE-V3-FB-RUNTIME-REFRESH-BROWSER-CONTROL-RESOURCE-20260811"
    intent, store, result, resource_db = historical_no_run_success(tmp_path, task_id=task_id)
    scheduler = {
        "schema_version": refresh.SCHEMA_VERSION,
        "kind": "bureau_runtime_scheduler_readback",
        "source_commit": intent["main_commit"],
        "authoritative": True,
    }
    install_receipt, historical_readback, _ = historical_runtime_artifacts(
        tmp_path, intent, scheduler=scheduler
    )
    tampered_scheduler = {**scheduler, "source_commit": "9" * 40}
    result = replace_historical_result(
        intent,
        result,
        install_receipt=install_receipt,
        readback={**historical_readback, "scheduler": tampered_scheduler},
    )
    release_test_leases(resource_db)

    with pytest.raises(refresh.RuntimeRefreshError) as exc_info:
        refresh.closeout_runtime_refresh_authority(
            state_root=Path(intent["state_root"]),
            approval_task_id=task_id,
            target_sha256=intent["target_sha256"],
            intent_sha256=intent["intent_sha256"],
            result_sha256=result["result_sha256"],
            resource_db=resource_db,
            now=NOW + timedelta(minutes=20),
            authority_store=store,
        )

    assert exc_info.value.code == "authority-closeout-readback-drift"
    current = store.task_spec(task_id)
    assert current is not None
    assert current["spec"]["state"] == "ready"


@pytest.mark.parametrize(
    ("damage", "expected_code"),
    [
        ("manifest", "historical-runtime-manifest-evidence-unavailable"),
        ("launcher", "historical-runtime-launcher-evidence-unavailable"),
        ("release", "historical-runtime-release-invalid"),
        ("release-symlink", "historical-runtime-path-symlink"),
        ("package", "historical-runtime-package-mismatch"),
        ("registry", "historical-runtime-registry-snapshot-invalid"),
        ("receipt", "historical-install-receipt-mismatch"),
        ("receipt-symlink", "historical-runtime-path-symlink"),
    ],
)
def test_no_run_closeout_fails_closed_on_damaged_historical_runtime_evidence(
    tmp_path: Path,
    damage: str,
    expected_code: str,
) -> None:
    task_id = "BUREAU-CONTROL-PLANE-V3-FB-RUNTIME-REFRESH-BROWSER-CONTROL-RESOURCE-20260811"
    intent, store, result, resource_db = historical_no_run_success(tmp_path, task_id=task_id)
    install_receipt, historical_readback, artifacts = historical_runtime_artifacts(tmp_path, intent)
    result = replace_historical_result(
        intent,
        result,
        install_receipt=install_receipt,
        readback=historical_readback,
    )
    release_test_leases(resource_db)
    before = store.task_spec(task_id)
    assert before is not None

    if damage == "manifest":
        (artifacts["backup"] / "deployment-manifest.json").unlink()
    elif damage == "launcher":
        (artifacts["backup"] / "bureau").write_text("tampered\n", encoding="utf-8")
    elif damage == "release":
        shutil.rmtree(artifacts["release"])
    elif damage == "release-symlink":
        moved_release = artifacts["release"].with_name(artifacts["release"].name + "-moved")
        artifacts["release"].rename(moved_release)
        artifacts["release"].symlink_to(moved_release, target_is_directory=True)
    elif damage == "package":
        (artifacts["release"] / "src/bureau/runtime_identity.py").write_text(
            "TAMPERED = True\n", encoding="utf-8"
        )
    elif damage == "registry":
        artifacts["registry_task"].write_text('{"id":"TAMPERED"}\n', encoding="utf-8")
    elif damage == "receipt":
        persisted_receipt = json.loads(artifacts["receipt"].read_text(encoding="utf-8"))
        persisted_receipt["installed_at"] = refresh.isoformat(NOW + timedelta(seconds=1))
        artifacts["receipt"].write_bytes(refresh.canonical_bytes(persisted_receipt))
    else:
        moved_receipt = artifacts["receipt"].with_name(artifacts["receipt"].name + ".moved")
        artifacts["receipt"].rename(moved_receipt)
        artifacts["receipt"].symlink_to(moved_receipt)

    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.closeout_runtime_refresh_authority(
            state_root=Path(intent["state_root"]),
            approval_task_id=task_id,
            target_sha256=intent["target_sha256"],
            intent_sha256=intent["intent_sha256"],
            result_sha256=result["result_sha256"],
            resource_db=resource_db,
            now=NOW + timedelta(minutes=20),
            authority_store=store,
        )

    assert caught.value.code == expected_code
    assert store.task_spec(task_id) == before
    assert "runtime_closeout" not in before["spec"]["metadata"]


def test_no_run_closeout_rejects_historical_multi_use_of_single_use_authority(
    tmp_path: Path,
) -> None:
    task_id = "BUREAU-CONTROL-PLANE-V3-FB-RUNTIME-REFRESH-BROWSER-CONTROL-RESOURCE-20260811"
    intent, store, result, resource_db = historical_no_run_success(tmp_path, task_id=task_id)
    state_root = Path(intent["state_root"])
    second_intent = json.loads(json.dumps(intent))
    second_intent["target_sha256"] = "e" * 64
    second_intent["main_commit"] = "4" * 40
    second_intent["nonce"] = "historical-second-use"
    second_intent = refresh.bind_digest(second_intent, "intent_sha256")
    refresh.create_only(
        state_root / "intents" / f"{second_intent['intent_sha256']}.json",
        refresh.canonical_bytes(second_intent),
    )
    second_attempt = state_root / "attempts" / second_intent["target_sha256"]
    second_started = refresh.bind_digest(
        {
            "schema_version": refresh.SCHEMA_VERSION,
            "kind": "bureau_runtime_refresh_attempt_start",
            "intent_sha256": second_intent["intent_sha256"],
            "target_sha256": second_intent["target_sha256"],
            "main_commit": second_intent["main_commit"],
            "lease_binding": result["lease_binding"],
            "started_at": refresh.isoformat(NOW + timedelta(minutes=1)),
            "effect_started": False,
        },
        "start_sha256",
    )
    refresh.create_only(second_attempt / "started.json", refresh.canonical_bytes(second_started))
    second_result = refresh._write_attempt_result(
        second_attempt / "result.json",
        {
            "schema_version": refresh.SCHEMA_VERSION,
            "kind": "bureau_runtime_refresh_result",
            "status": "deployed",
            "intent_sha256": second_intent["intent_sha256"],
            "target_sha256": second_intent["target_sha256"],
            "main_commit": second_intent["main_commit"],
            "source_identity": {"head": second_intent["main_commit"]},
            "install_receipt": result["install_receipt"],
            "readback": {**result["readback"], "source_commit": second_intent["main_commit"]},
            "lease_binding": result["lease_binding"],
            "finished_at": refresh.isoformat(NOW + timedelta(minutes=1)),
            "effect_started": True,
        },
    )
    release_test_leases(resource_db)
    before = store.task_spec(task_id)

    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.closeout_runtime_refresh_authority(
            state_root=state_root,
            approval_task_id=task_id,
            target_sha256=intent["target_sha256"],
            intent_sha256=intent["intent_sha256"],
            result_sha256=result["result_sha256"],
            resource_db=resource_db,
            now=NOW + timedelta(minutes=20),
            authority_store=store,
            readback=lambda **_: result["readback"],
        )

    assert caught.value.code == "authority-closeout-historical-multi-use"
    assert caught.value.details["conflicts"] == [
        {
            "intent_sha256": second_intent["intent_sha256"],
            "target_sha256": second_intent["target_sha256"],
            "result_sha256": second_result["result_sha256"],
            "status": "deployed",
            "main_commit": second_intent["main_commit"],
        }
    ]
    assert store.task_spec(task_id) == before
    assert store.list_runs() == []

    history_before = refresh._runtime_authority_effect_history(state_root, task_id)
    incident = refresh.closeout_historical_multi_use_runtime_refresh_authority(
        state_root=state_root,
        approval_task_id=task_id,
        target_sha256=intent["target_sha256"],
        intent_sha256=intent["intent_sha256"],
        result_sha256=result["result_sha256"],
        resource_db=resource_db,
        now=NOW + timedelta(minutes=20),
        authority_store=store,
        readback=lambda **_: result["readback"],
    )

    assert incident["idempotent_replay"] is False
    assert incident["closeout"]["kind"] == refresh.RUNTIME_AUTHORITY_INCIDENT_CLOSEOUT_KIND
    assert incident["closeout"]["status"] == "superseded"
    assert incident["closeout"]["effect_count"] == 2
    assert incident["closeout"]["conflicting_effect_count"] == 1
    after = store.task_spec(task_id)
    assert after["spec"]["state"] == "superseded"
    assert "runtime_closeout" not in after["spec"]["metadata"]
    assert after["spec"]["metadata"]["runtime_incident_closeout"] == incident["closeout"]
    incident_authority = after["spec"]["metadata"]["runtime_refresh_authority"]
    assert "target_binding_receipt" not in incident_authority
    assert "consumption" not in incident_authority
    assert refresh._runtime_authority_effect_history(state_root, task_id) == history_before
    assert store.list_runs() == []

    replay = refresh.closeout_historical_multi_use_runtime_refresh_authority(
        state_root=state_root,
        approval_task_id=task_id,
        target_sha256=intent["target_sha256"],
        intent_sha256=intent["intent_sha256"],
        result_sha256=result["result_sha256"],
        resource_db=resource_db,
        now=NOW + timedelta(minutes=21),
        authority_store=store,
        readback=lambda **_: result["readback"],
    )
    assert replay["idempotent_replay"] is True
    assert replay["closeout"] == incident["closeout"]

    def restore_incident_closeout(spec: dict[str, Any]) -> None:
        spec["metadata"]["runtime_incident_closeout"] = json.loads(
            json.dumps(incident["closeout"])
        )

    def tamper_incident_evidence(spec: dict[str, Any]) -> None:
        closeout = spec["metadata"]["runtime_incident_closeout"]
        closeout["source_commit"] = "6" * 40
        closeout["manifest_sha256"] = "b" * 64
        closeout["readback_sha256"] = "c" * 64
        closeout["lease_release_sha256"] = "d" * 64

    revise_authority(
        store,
        task_id,
        tamper_incident_evidence,
        key="tamper:incident-replay-evidence-bindings",
    )
    before_evidence_replay = store.task_spec(task_id)
    with pytest.raises(refresh.RuntimeRefreshError) as evidence_drift:
        refresh.closeout_historical_multi_use_runtime_refresh_authority(
            state_root=state_root,
            approval_task_id=task_id,
            target_sha256=intent["target_sha256"],
            intent_sha256=intent["intent_sha256"],
            result_sha256=result["result_sha256"],
            resource_db=resource_db,
            now=NOW + timedelta(minutes=21),
            authority_store=store,
            readback=lambda **_: result["readback"],
        )
    assert evidence_drift.value.code == "authority-incident-closeout-replay-mismatch"
    assert set(evidence_drift.value.details["mismatched"]) == {
        "source_commit",
        "manifest_sha256",
        "readback_sha256",
        "lease_release_sha256",
    }
    assert store.task_spec(task_id) == before_evidence_replay

    revise_authority(
        store,
        task_id,
        restore_incident_closeout,
        key="repair:incident-replay-evidence-bindings",
    )

    def tamper_incident_authority_binding(spec: dict[str, Any]) -> None:
        spec["metadata"]["runtime_incident_closeout"]["authority_revision"] += 1

    revise_authority(
        store,
        task_id,
        tamper_incident_authority_binding,
        key="tamper:incident-authority-revision",
    )
    with pytest.raises(refresh.RuntimeRefreshError) as authority_drift:
        refresh.closeout_historical_multi_use_runtime_refresh_authority(
            state_root=state_root,
            approval_task_id=task_id,
            target_sha256=intent["target_sha256"],
            intent_sha256=intent["intent_sha256"],
            result_sha256=result["result_sha256"],
            resource_db=resource_db,
            now=NOW + timedelta(minutes=21),
            authority_store=store,
            readback=lambda **_: result["readback"],
        )
    assert (
        authority_drift.value.code
        == "authority-incident-closeout-authority-binding-invalid"
    )

    revise_authority(
        store,
        task_id,
        restore_incident_closeout,
        key="repair:incident-authority-revision",
    )

    def add_corrupt_incident_consumption(spec: dict[str, Any]) -> None:
        authority = spec["metadata"]["runtime_refresh_authority"]
        authority["consumption"] = {
            "schema_version": refresh.RUNTIME_AUTHORITY_SCHEMA_VERSION,
            "kind": refresh.RUNTIME_AUTHORITY_CONSUMPTION_KIND,
            "status": "consumed",
            "task_id": task_id,
            "authority_revision": incident["closeout"]["authority_revision"],
            "authority_spec_sha256": incident["closeout"]["authority_spec_sha256"],
            "target_sha256": intent["target_sha256"],
            "intent_sha256": intent["intent_sha256"],
            "result_sha256": "f" * 64,
            "consumed_at": refresh.isoformat(NOW + timedelta(minutes=20)),
        }

    revise_authority(
        store,
        task_id,
        add_corrupt_incident_consumption,
        key="tamper:incident-consumption-replay",
    )
    with pytest.raises(refresh.RuntimeRefreshError) as consumption_drift:
        refresh.closeout_historical_multi_use_runtime_refresh_authority(
            state_root=state_root,
            approval_task_id=task_id,
            target_sha256=intent["target_sha256"],
            intent_sha256=intent["intent_sha256"],
            result_sha256=result["result_sha256"],
            resource_db=resource_db,
            now=NOW + timedelta(minutes=21),
            authority_store=store,
            readback=lambda **_: result["readback"],
        )
    assert consumption_drift.value.code == "authority-consumption-mismatch"

    def replace_with_corrupt_incident_binding(spec: dict[str, Any]) -> None:
        authority = spec["metadata"]["runtime_refresh_authority"]
        authority.pop("consumption", None)
        authority["target_binding_receipt"] = {
            "schema_version": refresh.RUNTIME_AUTHORITY_SCHEMA_VERSION,
            "kind": refresh.RUNTIME_AUTHORITY_BINDING_KIND,
            "task_id": task_id,
            "authority_revision": incident["closeout"]["authority_revision"],
            "authority_spec_sha256": incident["closeout"]["authority_spec_sha256"],
            "target_sha256": "f" * 64,
            "intent_sha256": intent["intent_sha256"],
            "bound_at": refresh.isoformat(NOW + timedelta(minutes=20)),
        }

    revise_authority(
        store,
        task_id,
        replace_with_corrupt_incident_binding,
        key="tamper:incident-target-binding-replay",
    )
    with pytest.raises(refresh.RuntimeRefreshError) as binding_drift:
        refresh.closeout_historical_multi_use_runtime_refresh_authority(
            state_root=state_root,
            approval_task_id=task_id,
            target_sha256=intent["target_sha256"],
            intent_sha256=intent["intent_sha256"],
            result_sha256=result["result_sha256"],
            resource_db=resource_db,
            now=NOW + timedelta(minutes=21),
            authority_store=store,
            readback=lambda **_: result["readback"],
        )
    assert binding_drift.value.code == "authority-target-binding-mismatch"

    def restore_incident_authority(spec: dict[str, Any]) -> None:
        authority = spec["metadata"]["runtime_refresh_authority"]
        authority.pop("consumption", None)
        authority.pop("target_binding_receipt", None)
        restore_incident_closeout(spec)

    revise_authority(
        store,
        task_id,
        restore_incident_authority,
        key="repair:incident-provenance-replay",
    )

    third_intent = json.loads(json.dumps(intent))
    third_intent["target_sha256"] = "d" * 64
    third_intent["main_commit"] = "5" * 40
    third_intent["nonce"] = "historical-third-use-restored-after-closeout"
    third_intent.pop("intent_sha256", None)
    third_intent = refresh.bind_digest(third_intent, "intent_sha256")
    refresh.create_only(
        state_root / "intents" / f"{third_intent['intent_sha256']}.json",
        refresh.canonical_bytes(third_intent),
    )
    third_attempt = state_root / "attempts" / third_intent["target_sha256"]
    third_started = refresh.bind_digest(
        {
            "schema_version": refresh.SCHEMA_VERSION,
            "kind": "bureau_runtime_refresh_attempt_start",
            "intent_sha256": third_intent["intent_sha256"],
            "target_sha256": third_intent["target_sha256"],
            "main_commit": third_intent["main_commit"],
            "lease_binding": result["lease_binding"],
            "started_at": refresh.isoformat(NOW + timedelta(minutes=2)),
            "effect_started": False,
        },
        "start_sha256",
    )
    refresh.create_only(
        third_attempt / "started.json", refresh.canonical_bytes(third_started)
    )
    refresh._write_attempt_result(
        third_attempt / "result.json",
        {
            "schema_version": refresh.SCHEMA_VERSION,
            "kind": "bureau_runtime_refresh_result",
            "status": "deployed",
            "intent_sha256": third_intent["intent_sha256"],
            "target_sha256": third_intent["target_sha256"],
            "main_commit": third_intent["main_commit"],
            "source_identity": {"head": third_intent["main_commit"]},
            "install_receipt": result["install_receipt"],
            "readback": {
                **result["readback"],
                "source_commit": third_intent["main_commit"],
            },
            "lease_binding": result["lease_binding"],
            "finished_at": refresh.isoformat(NOW + timedelta(minutes=2)),
            "effect_started": True,
        },
    )
    before_drift_replay = store.task_spec(task_id)

    with pytest.raises(refresh.RuntimeRefreshError) as drift:
        refresh.closeout_historical_multi_use_runtime_refresh_authority(
            state_root=state_root,
            approval_task_id=task_id,
            target_sha256=intent["target_sha256"],
            intent_sha256=intent["intent_sha256"],
            result_sha256=result["result_sha256"],
            resource_db=resource_db,
            now=NOW + timedelta(minutes=22),
            authority_store=store,
            readback=lambda **_: result["readback"],
        )

    assert drift.value.code == "authority-incident-closeout-history-drift"
    assert drift.value.details["stored_effect_count"] == 2
    assert drift.value.details["current_effect_count"] == 3
    assert store.task_spec(task_id) == before_drift_replay
    assert store.list_runs() == []


def test_incident_replay_rejects_removed_preexisting_provenance_receipt(
    tmp_path: Path,
) -> None:
    task_id = "BUREAU-RUNTIME-INCIDENT-PREEXISTING-PROVENANCE"
    (
        _observed,
        _manifest_path,
        intent,
        _intent_path,
        store,
        result,
        resource_db,
    ) = apply_successfully(tmp_path, task_id=task_id)
    state_root = Path(intent["state_root"])
    before_incident = store.task_spec(task_id)
    assert before_incident is not None
    before_authority = before_incident["spec"]["metadata"]["runtime_refresh_authority"]
    assert before_authority.get("consumption") is not None
    assert before_authority.get("target_binding_receipt") is not None

    second_intent = json.loads(json.dumps(intent))
    second_intent["target_sha256"] = "e" * 64
    second_intent["main_commit"] = "4" * 40
    second_intent["nonce"] = "incident-preexisting-provenance-second-effect"
    second_intent.pop("intent_sha256", None)
    second_intent = refresh.bind_digest(second_intent, "intent_sha256")
    refresh.create_only(
        state_root / "intents" / f"{second_intent['intent_sha256']}.json",
        refresh.canonical_bytes(second_intent),
    )
    second_attempt = state_root / "attempts" / second_intent["target_sha256"]
    refresh.create_only(
        second_attempt / "started.json",
        refresh.canonical_bytes(
            refresh.bind_digest(
                {
                    "schema_version": refresh.SCHEMA_VERSION,
                    "kind": "bureau_runtime_refresh_attempt_start",
                    "intent_sha256": second_intent["intent_sha256"],
                    "target_sha256": second_intent["target_sha256"],
                    "main_commit": second_intent["main_commit"],
                    "lease_binding": result["lease_binding"],
                    "started_at": refresh.isoformat(NOW + timedelta(minutes=1)),
                    "effect_started": False,
                },
                "start_sha256",
            )
        ),
    )
    refresh._write_attempt_result(
        second_attempt / "result.json",
        {
            "schema_version": refresh.SCHEMA_VERSION,
            "kind": "bureau_runtime_refresh_result",
            "status": "deployed",
            "intent_sha256": second_intent["intent_sha256"],
            "target_sha256": second_intent["target_sha256"],
            "main_commit": second_intent["main_commit"],
            "source_identity": {"head": second_intent["main_commit"]},
            "install_receipt": result["install_receipt"],
            "readback": {
                **result["readback"],
                "source_commit": second_intent["main_commit"],
            },
            "lease_binding": result["lease_binding"],
            "finished_at": refresh.isoformat(NOW + timedelta(minutes=1)),
            "effect_started": True,
        },
    )
    release_test_leases(resource_db)

    incident = refresh.closeout_historical_multi_use_runtime_refresh_authority(
        state_root=state_root,
        approval_task_id=task_id,
        target_sha256=intent["target_sha256"],
        intent_sha256=intent["intent_sha256"],
        result_sha256=result["result_sha256"],
        resource_db=resource_db,
        now=NOW + timedelta(minutes=20),
        authority_store=store,
        readback=lambda **_: result["readback"],
    )
    after_incident = store.task_spec(task_id)
    assert after_incident is not None
    after_authority = after_incident["spec"]["metadata"]["runtime_refresh_authority"]
    receipts, receipts_sha256 = refresh._runtime_incident_provenance_receipts(
        after_authority
    )
    assert receipts["consumption"] is not None
    assert receipts["target_binding_receipt"] is not None
    assert incident["closeout"]["provenance_receipts_sha256"] == receipts_sha256

    def remove_binding(spec: dict[str, Any]) -> None:
        authority = spec["metadata"]["runtime_refresh_authority"]
        authority.pop("target_binding_receipt", None)
        _, rewritten_provenance_sha256 = refresh._runtime_incident_provenance_receipts(
            authority
        )
        spec["metadata"]["runtime_incident_closeout"][
            "provenance_receipts_sha256"
        ] = rewritten_provenance_sha256

    revise_authority(
        store,
        task_id,
        remove_binding,
        key="tamper:remove-preexisting-incident-binding",
    )
    before_replay = store.task_spec(task_id)
    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.closeout_historical_multi_use_runtime_refresh_authority(
            state_root=state_root,
            approval_task_id=task_id,
            target_sha256=intent["target_sha256"],
            intent_sha256=intent["intent_sha256"],
            result_sha256=result["result_sha256"],
            resource_db=resource_db,
            now=NOW + timedelta(minutes=21),
            authority_store=store,
            readback=lambda **_: result["readback"],
        )

    assert caught.value.code == "authority-incident-closeout-provenance-drift"
    assert store.task_spec(task_id) == before_replay


def test_no_run_closeout_rejects_missing_release_and_wrong_or_tampered_evidence(
    tmp_path: Path,
) -> None:
    (
        _,
        _,
        intent,
        _,
        store,
        result,
        resource_db,
    ) = apply_successfully(tmp_path)
    revision_before = store.task_spec(intent["approval_task_id"])["revision"]

    with pytest.raises(refresh.RuntimeRefreshError) as live_lease_error:
        refresh.closeout_runtime_refresh_authority(
            state_root=Path(intent["state_root"]),
            approval_task_id=intent["approval_task_id"],
            target_sha256=intent["target_sha256"],
            intent_sha256=intent["intent_sha256"],
            result_sha256=result["result_sha256"],
            resource_db=resource_db,
            now=NOW + timedelta(minutes=20),
            authority_store=store,
            readback=lambda **_: result["readback"],
        )
    assert live_lease_error.value.code == "authority-closeout-leases-not-released"
    assert store.task_spec(intent["approval_task_id"])["revision"] == revision_before

    release_test_leases(resource_db)
    with pytest.raises(refresh.RuntimeRefreshError) as task_error:
        refresh.closeout_runtime_refresh_authority(
            state_root=Path(intent["state_root"]),
            approval_task_id="BUR-WRONG-AUTHORITY",
            target_sha256=intent["target_sha256"],
            intent_sha256=intent["intent_sha256"],
            result_sha256=result["result_sha256"],
            resource_db=resource_db,
            now=NOW + timedelta(minutes=20),
            authority_store=store,
            readback=lambda **_: result["readback"],
        )
    assert task_error.value.code == "authority-closeout-intent-mismatch"

    with pytest.raises(refresh.RuntimeRefreshError) as target_error:
        refresh.closeout_runtime_refresh_authority(
            state_root=Path(intent["state_root"]),
            approval_task_id=intent["approval_task_id"],
            target_sha256="f" * 64,
            intent_sha256=intent["intent_sha256"],
            result_sha256=result["result_sha256"],
            resource_db=resource_db,
            now=NOW + timedelta(minutes=20),
            authority_store=store,
            readback=lambda **_: result["readback"],
        )
    assert target_error.value.code == "authority-closeout-intent-mismatch"

    result_path = Path(intent["state_root"]) / "attempts" / intent["target_sha256"] / "result.json"
    tampered = dict(result)
    tampered["readback"] = {"source_commit": "f" * 40}
    result_path.write_bytes(refresh.canonical_bytes(tampered))
    with pytest.raises(refresh.RuntimeRefreshError) as result_error:
        refresh.closeout_runtime_refresh_authority(
            state_root=Path(intent["state_root"]),
            approval_task_id=intent["approval_task_id"],
            target_sha256=intent["target_sha256"],
            intent_sha256=intent["intent_sha256"],
            result_sha256=result["result_sha256"],
            resource_db=resource_db,
            now=NOW + timedelta(minutes=20),
            authority_store=store,
            readback=lambda **_: result["readback"],
        )
    assert result_error.value.code == "digest-mismatch"
    assert store.task_spec(intent["approval_task_id"])["revision"] == revision_before


def test_no_run_closeout_rejects_conflicting_authoritative_terminal_state(
    tmp_path: Path,
) -> None:
    (
        _,
        _,
        intent,
        _,
        store,
        result,
        resource_db,
    ) = apply_successfully(tmp_path)
    release_test_leases(resource_db)
    revise_authority(
        store,
        intent["approval_task_id"],
        lambda spec: spec.__setitem__("state", "superseded"),
        key="conflicting-terminal-state",
    )
    before = store.task_spec(intent["approval_task_id"])

    with pytest.raises(refresh.RuntimeRefreshError) as error:
        refresh.closeout_runtime_refresh_authority(
            state_root=Path(intent["state_root"]),
            approval_task_id=intent["approval_task_id"],
            target_sha256=intent["target_sha256"],
            intent_sha256=intent["intent_sha256"],
            result_sha256=result["result_sha256"],
            resource_db=resource_db,
            now=NOW + timedelta(minutes=20),
            authority_store=store,
            readback=lambda **_: result["readback"],
        )

    assert error.value.code == "authority-task-terminal"
    assert store.task_spec(intent["approval_task_id"]) == before
    assert "runtime_closeout" not in before["spec"]["metadata"]


@pytest.mark.parametrize(
    ("manifest_extra", "receipt_extra"),
    [
        ({}, {}),
        ({"scheduler": []}, {"scheduler": []}),
        (
            {"scheduler": {"kind": "manifest-scheduler"}},
            {"scheduler": {"kind": "receipt-scheduler"}},
        ),
    ],
)
def test_readback_rejects_missing_malformed_or_mismatched_scheduler_evidence(
    tmp_path: Path,
    manifest_extra: dict[str, Any],
    receipt_extra: dict[str, Any],
) -> None:
    prefix = tmp_path / "prefix"
    manifest_path = prefix / "deployment-manifest.json"
    write_manifest(manifest_path, MAIN, **manifest_extra)
    receipt = {
        "manifest_sha256": refresh.sha256_bytes(manifest_path.read_bytes()),
        **receipt_extra,
    }

    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.readback_install(
            expected_commit=MAIN,
            prefix=prefix,
            bin_dir=tmp_path / "absent-bin",
            install_receipt=receipt,
        )

    assert caught.value.code == "scheduler-manifest-receipt-mismatch"


def test_readback_validates_all_launchers_and_runtime_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = tmp_path / "prefix"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    manifest_path = prefix / "deployment-manifest.json"
    scheduler_receipt = {
        "kind": "bureau_runtime_scheduler_readback",
        "source_commit": MAIN,
        "authoritative": True,
    }
    write_manifest(
        manifest_path,
        MAIN,
        release_id="release",
        package_tree_sha256="a" * 64,
        canonical_registry_tree_sha256="b" * 64,
        scheduler=scheduler_receipt,
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
    status_capsule = bin_dir / "bureau-status-capsule"
    status_capsule.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    status_capsule.chmod(0o755)
    receipt = {
        "manifest_sha256": refresh.sha256_bytes(manifest_path.read_bytes()),
        "launcher_sha256": refresh.sha256_bytes(bureau.read_bytes()),
        "runtime_refresh_launcher_sha256": refresh.sha256_bytes(runner.read_bytes()),
        "status_capsule_launcher_sha256": refresh.sha256_bytes(status_capsule.read_bytes()),
        "rollback": {"directory": "/rollback"},
        "scheduler": scheduler_receipt,
    }
    scheduler_calls: list[tuple[dict[str, Any], str]] = []

    def scheduler_readback(
        evidence: dict[str, Any], *, expected_commit: str
    ) -> dict[str, Any]:
        scheduler_calls.append((evidence, expected_commit))
        return {"kind": "live-scheduler-readback", "authoritative": True}

    monkeypatch.setattr(refresh, "readback_user_scheduler", scheduler_readback)

    result = refresh.readback_install(
        expected_commit=MAIN,
        prefix=prefix,
        bin_dir=bin_dir,
        install_receipt=receipt,
    )

    assert result["check_valid"] is True
    assert result["runtime_identity_valid"] is True
    assert result["source_commit"] == MAIN
    assert result["status_capsule_launcher_sha256"] == receipt["status_capsule_launcher_sha256"]
    assert result["rollback"] == {"directory": "/rollback"}
    assert result["scheduler"] == {
        "kind": "live-scheduler-readback",
        "authoritative": True,
    }
    assert scheduler_calls == [(scheduler_receipt, MAIN)]

    status_capsule.unlink()
    with pytest.raises(refresh.RuntimeRefreshError) as missing:
        refresh.readback_install(
            expected_commit=MAIN,
            prefix=prefix,
            bin_dir=bin_dir,
            install_receipt=receipt,
        )
    assert missing.value.code == "readback-launcher-invalid"

    status_capsule.symlink_to(runner)
    with pytest.raises(refresh.RuntimeRefreshError) as symlinked:
        refresh.readback_install(
            expected_commit=MAIN,
            prefix=prefix,
            bin_dir=bin_dir,
            install_receipt=receipt,
        )
    assert symlinked.value.code == "readback-launcher-invalid"

    status_capsule.unlink()
    status_capsule.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    status_capsule.chmod(0o755)
    bad_receipt = {**receipt, "status_capsule_launcher_sha256": "0" * 64}
    with pytest.raises(refresh.RuntimeRefreshError) as mismatch:
        refresh.readback_install(
            expected_commit=MAIN,
            prefix=prefix,
            bin_dir=bin_dir,
            install_receipt=bad_receipt,
        )
    assert mismatch.value.code == "readback-launcher-mismatch"


def load_installer_module() -> Any:
    path = Path(__file__).parents[1] / "ops/install-bureau-runtime.py"
    spec = importlib.util.spec_from_file_location("install_bureau_runtime", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def clean_installer_source(tmp_path: Path) -> Path:
    repository = Path(__file__).parents[1]
    staged = tmp_path / "installer-staged"
    shutil.copytree(
        repository,
        staged,
        ignore=shutil.ignore_patterns(
            ".git",
            ".review-audits",
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
    git(staged, "commit", "-m", "synthetic scheduler transaction source")
    bare = tmp_path / "installer-bureau.git"
    git(tmp_path, "clone", "--bare", str(staged), str(bare))
    clean = tmp_path / "installer-clean"
    git(tmp_path, "clone", str(bare), str(clean))
    git(clean, "remote", "set-url", "origin", str(bare))
    git(clean, "fetch", "origin", "main")
    return clean


def scheduler_installer_approval(
    source: Path,
    tmp_path: Path,
    *,
    user_unit_dir: Path,
    libexec_dir: Path,
    runtime_user_unit_dir: Path | None = None,
) -> Path:
    bound_runtime_user_unit_dir = (
        runtime_user_unit_dir or refresh.default_runtime_user_unit_dir()
    )
    path = write_runtime_approval_intent(
        source,
        tmp_path,
        label="scheduler-installer-transaction",
    )
    intent = json.loads(path.read_text(encoding="utf-8"))
    intent.pop("intent_sha256")
    intent["user_unit_dir"] = str(user_unit_dir)
    intent["libexec_dir"] = str(libexec_dir)
    intent["runtime_user_unit_dir"] = str(bound_runtime_user_unit_dir)
    intent["required_resource_keys"] = refresh.scheduler_resource_keys(
        user_unit_dir=user_unit_dir,
        libexec_dir=libexec_dir,
        runtime_user_unit_dir=bound_runtime_user_unit_dir,
    )
    path.write_bytes(
        refresh.canonical_bytes(refresh.bind_digest(intent, "intent_sha256"))
    )
    return path


@pytest.mark.parametrize("noncanonical", ["prefix", "bin_dir"])
def test_run_installer_rejects_noncanonical_scheduler_runtime_layout_before_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    noncanonical: str,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    prefix = (home / ".local/share/bureau").resolve()
    bin_dir = (home / ".local/bin").resolve()
    if noncanonical == "prefix":
        prefix = (tmp_path / "custom-prefix").resolve()
    else:
        bin_dir = (tmp_path / "custom-bin").resolve()

    monkeypatch.setattr(
        refresh,
        "_run",
        lambda *_args, **_kwargs: pytest.fail("installer process must not be invoked"),
    )

    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.run_installer(
            source=tmp_path / "source",
            prefix=prefix,
            bin_dir=bin_dir,
            user_unit_dir=tmp_path / "systemd/user",
            libexec_dir=tmp_path / "libexec",
            runtime_user_unit_dir=refresh.default_runtime_user_unit_dir(),
            approval_intent=tmp_path / "approval.json",
        )

    assert caught.value.code == "scheduler-runtime-layout-noncanonical"
    assert caught.value.details["mismatches"] == [noncanonical]


def test_run_installer_rejects_runtime_user_unit_dir_drift_before_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "current-runtime"))
    monkeypatch.setattr(
        refresh,
        "_run",
        lambda *_args, **_kwargs: pytest.fail("installer process must not be invoked"),
    )

    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.run_installer(
            source=tmp_path / "source",
            prefix=(home / ".local/share/bureau").resolve(),
            bin_dir=(home / ".local/bin").resolve(),
            user_unit_dir=tmp_path / "systemd/user",
            libexec_dir=tmp_path / "libexec",
            runtime_user_unit_dir=(tmp_path / "intent-runtime/systemd/user").resolve(),
            approval_intent=tmp_path / "approval.json",
        )

    assert caught.value.code == "runtime-user-unit-dir-drift"


def test_real_installer_rejects_runtime_user_unit_dir_drift_before_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    installer = load_installer_module()
    source = clean_installer_source(tmp_path)
    home = tmp_path / "home"
    intent_runtime_root = (tmp_path / "intent-runtime").resolve()
    runtime_user_unit_dir = intent_runtime_root / "systemd/user"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(intent_runtime_root))
    unit_root = (tmp_path / "custom-systemd/user").resolve()
    libexec_root = (tmp_path / "custom-libexec").resolve()
    approval_path = scheduler_installer_approval(
        source,
        tmp_path,
        user_unit_dir=unit_root,
        libexec_dir=libexec_root,
        runtime_user_unit_dir=runtime_user_unit_dir,
    )
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "drifted-runtime"))
    monkeypatch.setattr(
        installer,
        "atomic_write",
        lambda *_args, **_kwargs: pytest.fail("installer artifacts must not be written"),
    )
    real_subprocess_run = subprocess.run

    def reject_runtime_commands(
        argv: list[str], *args: Any, **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        if argv[0] in {"systemctl", "systemd-analyze"}:
            pytest.fail("systemctl and systemd-analyze must not be invoked")
        return real_subprocess_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", reject_runtime_commands)

    result = installer.main(
        [
            "--source",
            str(source),
            "--prefix",
            str(home / ".local/share/bureau"),
            "--bin-dir",
            str(home / ".local/bin"),
            "--user-unit-dir",
            str(unit_root),
            "--libexec-dir",
            str(libexec_root),
            "--runtime-user-unit-dir",
            str(runtime_user_unit_dir),
            "--approval-intent",
            str(approval_path),
            "--converge-user-systemd",
        ]
    )

    assert result == 2
    error = json.loads(capsys.readouterr().err.strip())["error"]
    assert error["code"] == "runtime-user-unit-dir-drift"
    assert not (home / ".local").exists()
    assert not unit_root.exists()
    assert not libexec_root.exists()


@pytest.mark.parametrize("noncanonical", ["prefix", "bin_dir"])
def test_real_installer_rejects_noncanonical_scheduler_layout_before_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    noncanonical: str,
) -> None:
    installer = load_installer_module()
    source = clean_installer_source(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    prefix = (home / ".local/share/bureau").resolve()
    bin_dir = (home / ".local/bin").resolve()
    if noncanonical == "prefix":
        prefix = (tmp_path / "custom-prefix").resolve()
    else:
        bin_dir = (tmp_path / "custom-bin").resolve()
    unit_root = (tmp_path / "custom-systemd/user").resolve()
    libexec_root = (tmp_path / "custom-libexec").resolve()
    sentinel_paths = [
        prefix / "deployment-manifest.json",
        prefix / "receipts/existing.json",
        *(bin_dir / name for name, _ in refresh.RUNTIME_LAUNCHER_ENTRYPOINTS),
        unit_root / "bureau-task-supply.service",
        unit_root / "bureau-task-supply.timer",
        libexec_root / "bureau-task-supply",
    ]
    for path in sentinel_paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"sentinel {path.name}\n".encode())
    preimage = {path: path.read_bytes() for path in sentinel_paths}

    monkeypatch.setattr(
        installer,
        "atomic_write",
        lambda *_args, **_kwargs: pytest.fail("installer artifacts must not be written"),
    )
    monkeypatch.setattr(
        refresh,
        "atomic_write",
        lambda *_args, **_kwargs: pytest.fail("scheduler artifacts must not be written"),
    )
    real_subprocess_run = subprocess.run
    runtime_commands: list[list[str]] = []

    def reject_runtime_commands(
        argv: list[str], *args: Any, **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        if argv[0] in {"systemctl", "systemd-analyze"}:
            runtime_commands.append(argv)
            pytest.fail("systemctl and systemd-analyze must not be invoked")
        return real_subprocess_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", reject_runtime_commands)

    result = installer.main(
        [
            "--source",
            str(source),
            "--prefix",
            str(prefix),
            "--bin-dir",
            str(bin_dir),
            "--user-unit-dir",
            str(unit_root),
            "--libexec-dir",
            str(libexec_root),
            "--converge-user-systemd",
        ]
    )

    assert result == 2
    error = json.loads(capsys.readouterr().err.strip())["error"]
    assert error["code"] == "scheduler-runtime-layout-noncanonical"
    assert error["details"]["mismatches"] == [noncanonical]
    assert runtime_commands == []
    assert {path: path.read_bytes() for path in sentinel_paths} == preimage
    assert sorted(path for path in prefix.rglob("*") if path.is_file()) == sorted(
        path for path in sentinel_paths if path.is_relative_to(prefix)
    )
    assert sorted(path for path in bin_dir.rglob("*") if path.is_file()) == sorted(
        path for path in sentinel_paths if path.is_relative_to(bin_dir)
    )
    assert sorted(path for path in unit_root.rglob("*") if path.is_file()) == sorted(
        path for path in sentinel_paths if path.is_relative_to(unit_root)
    )
    assert sorted(path for path in libexec_root.rglob("*") if path.is_file()) == sorted(
        path for path in sentinel_paths if path.is_relative_to(libexec_root)
    )


@pytest.mark.parametrize("preexisting_launchers", [False, True])
def test_real_installer_receipt_write_failure_rolls_back_activated_scheduler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    preexisting_launchers: bool,
) -> None:
    installer = load_installer_module()
    source = clean_installer_source(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    prefix = home / ".local/share/bureau"
    bin_dir = home / ".local/bin"
    manifest_path = prefix / "deployment-manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_bytes(b"exact old manifest\n")
    manifest_path.chmod(0o640)
    launcher_paths = [
        bin_dir / "bureau",
        bin_dir / "bureau-runtime-refresh",
        bin_dir / "bureau-status-capsule",
    ]
    if preexisting_launchers:
        bin_dir.mkdir()
        for path in launcher_paths:
            path.write_bytes(f"exact old {path.name}\n".encode())
            path.chmod(0o700)
    install_preimage = {
        path: (path.read_bytes(), path.stat().st_mode & 0o777)
        for path in (manifest_path, *launcher_paths)
        if path.is_file()
    }
    assert bin_dir.exists() is preexisting_launchers
    assert not (prefix / "receipts").exists()
    unit_root = (tmp_path / "transaction-systemd/user").resolve()
    libexec_root = (tmp_path / "transaction-libexec").resolve()
    runtime_root = (tmp_path / "transaction-runtime").resolve()
    runtime_unit_root = runtime_root / "systemd/user"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_root))
    unit_root.mkdir(parents=True)
    libexec_root.mkdir()
    runtime_unit_root.mkdir(parents=True)
    approval_path = scheduler_installer_approval(
        source,
        tmp_path,
        user_unit_dir=unit_root,
        libexec_dir=libexec_root,
        runtime_user_unit_dir=runtime_unit_root,
    )
    systemd = FakeUserSystemd(unit_root, runtime_unit_root=runtime_unit_root)
    state_before = json.loads(json.dumps(systemd.states))

    def fake_run(
        argv: list[str],
        **_kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        if argv[0] == "systemctl":
            return systemd(argv)
        if argv[:3] == ["systemd-analyze", "--user", "verify"]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected runtime command: {argv}")

    real_atomic_write = installer.atomic_write

    def fail_receipt_write(path: Path, data: bytes, mode: int = 0o644) -> None:
        if path.parent == prefix / "receipts":
            real_atomic_write(path, data, mode)
            assert path.parent.is_dir()
            assert path.is_file()
            raise OSError("injected durable receipt write failure")
        real_atomic_write(path, data, mode)

    monkeypatch.setattr(refresh, "_run", fake_run)
    monkeypatch.setattr(installer, "atomic_write", fail_receipt_write)

    result = installer.main(
        [
            "--source",
            str(source),
            "--prefix",
            str(prefix),
            "--bin-dir",
            str(bin_dir),
            "--user-unit-dir",
            str(unit_root),
            "--libexec-dir",
            str(libexec_root),
            "--runtime-user-unit-dir",
            str(runtime_unit_root),
            "--approval-intent",
            str(approval_path),
            "--replace-existing",
            "--converge-user-systemd",
        ]
    )

    assert result == 2
    error = json.loads(capsys.readouterr().err.strip().splitlines()[-1])["error"]
    assert error["code"] == "scheduler-convergence-rolled-back"
    assert "injected durable receipt write failure" in error["details"]["cause"][
        "message"
    ]
    for path, (content, mode) in install_preimage.items():
        assert path.read_bytes() == content
        assert path.stat().st_mode & 0o777 == mode
    assert bin_dir.exists() is preexisting_launchers
    assert not (prefix / "receipts").exists()
    assert systemd.states == state_before
    for name in refresh.RUNTIME_SCHEDULER_NAMES:
        assert not (unit_root / f"{name}.service").exists()
        assert not (unit_root / f"{name}.timer").exists()
        assert not (libexec_root / name).exists()


def test_launcher_directory_fsync_failure_after_replace_restores_exact_preimage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    installer = load_installer_module()
    source = clean_installer_source(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    prefix = home / ".local/share/bureau"
    bin_dir = home / ".local/bin"
    manifest_path = prefix / "deployment-manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_bytes(b"exact old manifest\n")
    manifest_path.chmod(0o640)
    bin_dir.mkdir()
    launcher = bin_dir / "bureau"
    launcher.write_bytes(b"#!/bin/sh\necho exact-old-launcher\n")
    launcher.chmod(0o710)
    launcher_preimage = (launcher.read_bytes(), launcher.stat().st_mode & 0o777)
    for name, entrypoint in refresh.RUNTIME_LAUNCHER_ENTRYPOINTS:
        path = bin_dir / name
        if path == launcher:
            continue
        path.write_bytes(refresh.stable_launcher_bytes(manifest_path, entrypoint))
        path.chmod(0o755)

    unit_root = (tmp_path / "transaction-systemd/user").resolve()
    libexec_root = (tmp_path / "transaction-libexec").resolve()
    runtime_root = (tmp_path / "transaction-runtime").resolve()
    runtime_unit_root = runtime_root / "systemd/user"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_root))
    unit_root.mkdir(parents=True)
    libexec_root.mkdir()
    runtime_unit_root.mkdir(parents=True)
    systemd = FakeUserSystemd(unit_root, runtime_unit_root=runtime_unit_root)
    approval_path = scheduler_installer_approval(
        source,
        tmp_path,
        user_unit_dir=unit_root,
        libexec_dir=libexec_root,
        runtime_user_unit_dir=runtime_unit_root,
    )
    approval = json.loads(approval_path.read_text(encoding="utf-8"))
    approval.pop("intent_sha256")
    approval["required_resource_keys"].append(f"path:{launcher}")
    approval["required_resource_keys"].sort()
    approval_path.write_bytes(
        refresh.canonical_bytes(refresh.bind_digest(approval, "intent_sha256"))
    )

    replaced_launcher = False
    fsync_failure_injected = False
    fsync_armed = False
    real_replace = installer.os.replace
    real_fsync_directory = installer.fsync_directory

    def fake_run(
        argv: list[str], **_kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        if argv[0] == "systemctl":
            return systemd(argv)
        if argv[:3] == ["systemd-analyze", "--user", "verify"]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected runtime command: {argv}")

    def observe_launcher_replace(source_path: str, target_path: str) -> None:
        nonlocal replaced_launcher, fsync_armed
        real_replace(source_path, target_path)
        if Path(target_path) == launcher and not fsync_failure_injected:
            replaced_launcher = True
            fsync_armed = True

    def fail_first_launcher_directory_fsync(path: Path) -> None:
        nonlocal fsync_armed, fsync_failure_injected
        if path == bin_dir and fsync_armed and not fsync_failure_injected:
            fsync_armed = False
            fsync_failure_injected = True
            assert launcher.read_bytes() != launcher_preimage[0]
            raise OSError("injected launcher directory fsync failure after replace")
        real_fsync_directory(path)

    monkeypatch.setattr(installer.os, "replace", observe_launcher_replace)
    monkeypatch.setattr(installer, "fsync_directory", fail_first_launcher_directory_fsync)
    monkeypatch.setattr(refresh, "_run", fake_run)

    result = installer.main(
        [
            "--source",
            str(source),
            "--prefix",
            str(prefix),
            "--bin-dir",
            str(bin_dir),
            "--user-unit-dir",
            str(unit_root),
            "--libexec-dir",
            str(libexec_root),
            "--runtime-user-unit-dir",
            str(runtime_unit_root),
            "--approval-intent",
            str(approval_path),
            "--replace-existing",
            "--enforce-launcher-allowlist",
            "--allowed-launcher-path",
            str(launcher),
            "--converge-user-systemd",
        ]
    )

    assert result == 2
    error = json.loads(capsys.readouterr().err.strip().splitlines()[-1])["error"]
    assert error["code"] == "scheduler-convergence-rolled-back"
    assert "fsync failure after replace" in error["details"]["cause"]["message"]
    assert replaced_launcher is True
    assert fsync_failure_injected is True
    assert launcher.read_bytes() == launcher_preimage[0]
    assert launcher.stat().st_mode & 0o777 == launcher_preimage[1]
    assert manifest_path.read_bytes() == b"exact old manifest\n"
    assert manifest_path.stat().st_mode & 0o777 == 0o640


def test_scheduler_rollback_does_not_restore_unmutated_unleased_launchers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    installer = load_installer_module()
    source = clean_installer_source(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    prefix = home / ".local/share/bureau"
    bin_dir = home / ".local/bin"
    manifest_path = prefix / "deployment-manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_bytes(b"exact old manifest\n")
    manifest_path.chmod(0o640)
    bin_dir.mkdir()
    launcher_paths: dict[str, Path] = {}
    launcher_preimage: dict[Path, bytes] = {}
    for name, entrypoint in refresh.RUNTIME_LAUNCHER_ENTRYPOINTS:
        path = bin_dir / name
        content = refresh.stable_launcher_bytes(manifest_path, entrypoint)
        path.write_bytes(content)
        path.chmod(0o755)
        launcher_paths[name] = path
        launcher_preimage[path] = content

    unit_root = (tmp_path / "transaction-systemd/user").resolve()
    libexec_root = (tmp_path / "transaction-libexec").resolve()
    runtime_root = (tmp_path / "transaction-runtime").resolve()
    runtime_unit_root = runtime_root / "systemd/user"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_root))
    unit_root.mkdir(parents=True)
    libexec_root.mkdir()
    runtime_unit_root.mkdir(parents=True)
    approval_path = scheduler_installer_approval(
        source,
        tmp_path,
        user_unit_dir=unit_root,
        libexec_dir=libexec_root,
        runtime_user_unit_dir=runtime_unit_root,
    )
    approval = json.loads(approval_path.read_text(encoding="utf-8"))
    assert {
        f"path:{path}" for path in launcher_paths.values()
    }.isdisjoint(approval["required_resource_keys"])
    systemd = FakeUserSystemd(unit_root, runtime_unit_root=runtime_unit_root)
    state_before = json.loads(json.dumps(systemd.states))

    def fake_run(
        argv: list[str], **_kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        if argv[0] == "systemctl":
            return systemd(argv)
        if argv[:3] == ["systemd-analyze", "--user", "verify"]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected runtime command: {argv}")

    concurrent_launcher = launcher_paths["bureau"]
    concurrent_content = b"concurrent launcher change\n"
    launcher_atomic_writes: list[Path] = []
    real_atomic_write = installer.atomic_write

    def fail_receipt_after_concurrent_launcher_change(
        path: Path, data: bytes, mode: int = 0o644
    ) -> None:
        if path in launcher_preimage:
            launcher_atomic_writes.append(path)
        if path.parent == prefix / "receipts":
            concurrent_launcher.write_bytes(concurrent_content)
            concurrent_launcher.chmod(0o711)
            real_atomic_write(path, data, mode)
            raise OSError("injected durable receipt write failure")
        real_atomic_write(path, data, mode)

    monkeypatch.setattr(refresh, "_run", fake_run)
    monkeypatch.setattr(
        installer,
        "atomic_write",
        fail_receipt_after_concurrent_launcher_change,
    )

    result = installer.main(
        [
            "--source",
            str(source),
            "--prefix",
            str(prefix),
            "--bin-dir",
            str(bin_dir),
            "--user-unit-dir",
            str(unit_root),
            "--libexec-dir",
            str(libexec_root),
            "--runtime-user-unit-dir",
            str(runtime_unit_root),
            "--approval-intent",
            str(approval_path),
            "--replace-existing",
            "--enforce-launcher-allowlist",
            "--converge-user-systemd",
        ]
    )

    assert result == 2
    error = json.loads(capsys.readouterr().err.strip().splitlines()[-1])["error"]
    assert error["code"] == "scheduler-convergence-rolled-back"
    assert launcher_atomic_writes == []
    assert concurrent_launcher.read_bytes() == concurrent_content
    assert concurrent_launcher.stat().st_mode & 0o777 == 0o711
    for path, content in launcher_preimage.items():
        if path != concurrent_launcher:
            assert path.read_bytes() == content
            assert path.stat().st_mode & 0o777 == 0o755
    assert manifest_path.read_bytes() == b"exact old manifest\n"
    assert manifest_path.stat().st_mode & 0o777 == 0o640
    assert not (prefix / "receipts").exists()
    assert systemd.states == state_before


@pytest.mark.parametrize(
    ("parent_state", "expected_error"),
    [
        ("non-empty", "became non-empty"),
        ("symlink", "became a symlink"),
        ("wrong-type", "has the wrong type"),
    ],
)
def test_installer_rollback_reports_created_parent_drift(
    tmp_path: Path,
    parent_state: str,
    expected_error: str,
) -> None:
    installer = load_installer_module()
    parent = tmp_path / "transaction-created-parent"
    if parent_state == "non-empty":
        parent.mkdir()
        (parent / "foreign").write_text("preserve me\n", encoding="utf-8")
    elif parent_state == "symlink":
        target = tmp_path / "foreign-target"
        target.mkdir()
        parent.symlink_to(target, target_is_directory=True)
    else:
        parent.write_text("foreign type\n", encoding="utf-8")

    failures = installer._restore_install_preimage(
        backup={"manifest": None},
        manifest_path=tmp_path / "absent-manifest.json",
        launchers={},
        mutated_launchers=set(),
        receipt_path=None,
        parent_preimage={"created-parent": (parent, False)},
    )

    assert failures[0]["operation"] == ["remove-created-parent", "created-parent"]
    assert expected_error in failures[0]["error"]
    assert {tuple(failure["operation"]) for failure in failures} == {
        ("remove-created-parent", "created-parent"),
        ("verify-preimage", "created-parent"),
    }
    assert installer._path_lexists(parent)


def test_installer_wrapper_selects_refresh_entrypoint_and_backs_up_both(
    tmp_path: Path,
) -> None:
    installer = load_installer_module()
    rendered = installer.wrapper(
        tmp_path / "deployment-manifest.json",
        "bureau.runtime_refresh",
    ).decode()
    assert "importlib.import_module('bureau.runtime_refresh')" in rendered
    assert installer.MANAGED_MARKER in rendered
    assert "manifest payload digest mismatch" in rendered

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


def test_real_non_systemd_installer_supports_custom_layout(tmp_path: Path) -> None:
    repository = Path(__file__).parents[1]
    staged = tmp_path / "staged"
    shutil.copytree(
        repository,
        staged,
        ignore=shutil.ignore_patterns(
            ".git",
            ".review-audits",
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
    git(clean, "config", "user.name", "Test")
    git(clean, "config", "user.email", "test@example.invalid")
    git(clean, "remote", "set-url", "origin", str(bare))
    git(clean, "fetch", "origin", "main")

    prefix = tmp_path / "prefix"
    bin_dir = tmp_path / "bin"
    command = [
        sys.executable,
        str(clean / "ops/install-bureau-runtime.py"),
        "--source",
        str(clean),
        "--prefix",
        str(prefix),
        "--bin-dir",
        str(bin_dir),
    ]
    denied = subprocess.run(
        command,
        cwd=clean,
        check=False,
        text=True,
        capture_output=True,
    )
    assert denied.returncode != 0
    assert "runtime approval denied" in denied.stderr
    assert not prefix.exists()

    expired_approval = write_runtime_approval_intent(
        clean,
        tmp_path,
        label="expired-installer",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    expired = subprocess.run(
        [*command, "--approval-intent", str(expired_approval)],
        cwd=clean,
        check=False,
        text=True,
        capture_output=True,
    )
    assert expired.returncode != 0
    assert "intent-expired" in expired.stderr
    assert not prefix.exists()

    approval_path = write_runtime_approval_intent(
        clean,
        tmp_path,
        label="real-installer",
    )

    install = subprocess.run(
        [*command, "--approval-intent", str(approval_path)],
        cwd=clean,
        check=False,
        text=True,
        capture_output=True,
        env={**os.environ, "XDG_RUNTIME_DIR": "relative-runtime"},
    )
    assert install.returncode == 0, install.stderr
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
    deployed_head = git(clean, "rev-parse", "HEAD")
    assert payload["deployed_source_commit"] == deployed_head

    launcher_hashes = {
        path.name: refresh.sha256_bytes(path.read_bytes())
        for path in (bureau, runner, bin_dir / "bureau-status-capsule")
    }
    repeated_approval = write_runtime_approval_intent(
        clean,
        tmp_path,
        label="stable-launcher-repeat",
    )
    repeated = subprocess.run(
        [
            *command,
            "--approval-intent",
            str(repeated_approval),
            "--replace-existing",
        ],
        cwd=clean,
        check=False,
        text=True,
        capture_output=True,
    )
    assert repeated.returncode == 0, repeated.stderr
    repeated_receipt = json.loads(repeated.stdout.strip().splitlines()[-1])
    assert repeated_receipt["launcher_written"] is False
    assert repeated_receipt["runtime_refresh_launcher_written"] is False
    assert repeated_receipt["status_capsule_launcher_written"] is False
    assert launcher_hashes == {
        path.name: refresh.sha256_bytes(path.read_bytes())
        for path in (bureau, runner, bin_dir / "bureau-status-capsule")
    }

    status_capsule = bin_dir / "bureau-status-capsule"
    stable_status_capsule = status_capsule.read_bytes()
    manifest_before_launcher_drift = (prefix / "deployment-manifest.json").read_bytes()
    drift_approval = write_runtime_approval_intent(
        clean,
        tmp_path,
        label="stable-launcher-drift",
    )
    status_capsule.write_text("drifted launcher\n", encoding="utf-8")
    drift_blocked = subprocess.run(
        [
            *command,
            "--approval-intent",
            str(drift_approval),
            "--replace-existing",
            "--enforce-launcher-allowlist",
        ],
        cwd=clean,
        check=False,
        text=True,
        capture_output=True,
    )
    assert drift_blocked.returncode != 0
    assert "launcher mutation is not covered" in drift_blocked.stderr
    assert (prefix / "deployment-manifest.json").read_bytes() == manifest_before_launcher_drift
    assert status_capsule.read_text(encoding="utf-8") == "drifted launcher\n"
    status_capsule.write_bytes(stable_status_capsule)
    status_capsule.chmod(0o755)

    symlink_target = tmp_path / "status-capsule-symlink-target"
    symlink_target.write_text("legacy symlink target\n", encoding="utf-8")
    status_capsule.unlink()
    status_capsule.symlink_to(symlink_target)
    symlink_approval = write_runtime_approval_intent(
        clean,
        tmp_path,
        label="stable-launcher-symlink-repair",
    )
    symlink_repair = subprocess.run(
        [
            *command,
            "--approval-intent",
            str(symlink_approval),
            "--replace-existing",
            "--enforce-launcher-allowlist",
            "--allowed-launcher-path",
            str(status_capsule),
        ],
        cwd=clean,
        check=False,
        text=True,
        capture_output=True,
    )
    assert symlink_repair.returncode == 0, symlink_repair.stderr
    symlink_receipt = json.loads(symlink_repair.stdout.strip().splitlines()[-1])
    assert symlink_receipt["launcher_written"] is False
    assert symlink_receipt["runtime_refresh_launcher_written"] is False
    assert symlink_receipt["status_capsule_launcher_written"] is True
    assert status_capsule.is_file() and not status_capsule.is_symlink()
    assert status_capsule.read_bytes() == stable_status_capsule

    manifest_payload = json.loads((prefix / "deployment-manifest.json").read_text())
    manifest_payload["source_commit"] = "f" * 40
    (prefix / "deployment-manifest.json").write_bytes(refresh.canonical_bytes(manifest_payload))
    tampered = subprocess.run(
        [str(bureau), "--json", "check"],
        check=False,
        text=True,
        capture_output=True,
    )
    assert tampered.returncode != 0
    assert "manifest payload digest mismatch" in tampered.stderr
    repeated = subprocess.run(
        [
            *command,
            "--approval-intent",
            str(repeated_approval),
            "--replace-existing",
        ],
        cwd=clean,
        check=False,
        text=True,
        capture_output=True,
    )
    assert repeated.returncode == 0, repeated.stderr

    readme = clean / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8") + "\nLegacy refresh cutover fixture.\n",
        encoding="utf-8",
    )
    git(clean, "add", "README.md")
    git(clean, "commit", "-m", "synthetic legacy cutover target")
    target_head = git(clean, "rev-parse", "HEAD")
    git(clean, "update-ref", "refs/remotes/origin/main", target_head)
    assert target_head != deployed_head
    deployed_manifest, deployed_manifest_sha = refresh.load_manifest(
        prefix / "deployment-manifest.json"
    )
    current = datetime.now(timezone.utc)
    legacy_state = (tmp_path / "legacy-refresh-state").resolve()
    workspace = legacy_state / "workspaces" / target_head
    target_sha = refresh.sha256_bytes(
        refresh.canonical_bytes(
            {
                "kind": "legacy-cutover-test-target",
                "source_commit": target_head,
                "deployed_manifest_sha256": deployed_manifest_sha,
            }
        )
    )
    legacy_intent = refresh.bind_digest(
        {
            "schema_version": refresh.SCHEMA_VERSION,
            "kind": "bureau_runtime_refresh_intent",
            "repository": "heimgewebe/bureau",
            "remote_url": str(bare),
            "main_commit": target_head,
            "pull_request": 1236,
            "merged_at": refresh.isoformat(current),
            "required_checks": list(refresh.DEFAULT_REQUIRED_CHECKS),
            "target_sha256": target_sha,
            "observation_sha256": "b" * 64,
            "expected_deployed_source_commit": deployed_manifest["source_commit"],
            "expected_manifest_sha256": deployed_manifest_sha,
            "state_root": str(legacy_state),
            "prefix": str(prefix.resolve()),
            "bin_dir": str(bin_dir.resolve()),
            "workspace": str(workspace),
            "required_resource_keys": refresh.required_resource_keys(
                state_root=legacy_state,
                prefix=prefix.resolve(),
                bin_dir=bin_dir.resolve(),
                workspace=workspace,
            ),
            "authorized_by": "legacy-runner-test",
            "authorization": "Explicit operator authorization from the old runner.",
            "created_at": refresh.isoformat(current),
            "expires_at": refresh.isoformat(current + timedelta(minutes=15)),
            "nonce": "legacy-installer-cutover",
            "does_not_establish": [
                "external_lease_liveness",
                "merge_authority",
                "automatic_retry_authority",
            ],
        },
        "intent_sha256",
    )
    legacy_intent_path = legacy_state / "intents" / f"{legacy_intent['intent_sha256']}.json"
    refresh.create_only(legacy_intent_path, refresh.canonical_bytes(legacy_intent))
    legacy_binding, legacy_resource_db = lease_for(
        tmp_path / "legacy-installer-leases",
        legacy_intent,
        current=current,
    )
    normalized_binding = refresh.validate_live_lease_binding(
        legacy_intent,
        legacy_binding,
        resource_db=legacy_resource_db,
        now=current,
    )
    legacy_started = refresh.bind_digest(
        {
            "schema_version": refresh.SCHEMA_VERSION,
            "kind": "bureau_runtime_refresh_attempt_start",
            "intent_sha256": legacy_intent["intent_sha256"],
            "target_sha256": legacy_intent["target_sha256"],
            "main_commit": target_head,
            "lease_binding": normalized_binding,
            "started_at": refresh.isoformat(current),
            "effect_started": False,
        },
        "start_sha256",
    )
    refresh.create_only(
        legacy_state / "attempts" / target_sha / "started.json",
        refresh.canonical_bytes(legacy_started),
    )

    legacy_install = subprocess.run(
        [
            *command,
            "--runtime-refresh-state-root",
            str(legacy_state),
            "--resource-db",
            str(legacy_resource_db),
            "--replace-existing",
        ],
        cwd=clean,
        check=False,
        text=True,
        capture_output=True,
    )
    assert legacy_install.returncode == 0, legacy_install.stderr
    legacy_receipt = json.loads(legacy_install.stdout.strip().splitlines()[-1])
    assert legacy_receipt["launcher_written"] is False
    assert legacy_receipt["runtime_refresh_launcher_written"] is False
    assert legacy_receipt["status_capsule_launcher_written"] is False
    assert launcher_hashes == {
        path.name: refresh.sha256_bytes(path.read_bytes())
        for path in (bureau, runner, status_capsule)
    }
    assert legacy_receipt["runtime_approval"]["required_level"] == "legacy_runtime_operator_gate"
    legacy_cutover = legacy_receipt["runtime_approval"]["legacy_cutover"]
    assert legacy_cutover["intent_sha256"] == legacy_intent["intent_sha256"]
    assert legacy_cutover["start_sha256"] == legacy_started["start_sha256"]
    assert legacy_cutover["expected_source_commit"] == target_head
    live_binding_sha256 = legacy_cutover["lease_binding_sha256"]
    assert isinstance(live_binding_sha256, str)
    assert len(live_binding_sha256) == 64
    assert all(character in "0123456789abcdef" for character in live_binding_sha256)
    upgraded_status = subprocess.run(
        [str(runner), "--state-root", str(legacy_state), "status"],
        check=True,
        text=True,
        capture_output=True,
    )
    assert json.loads(upgraded_status.stdout)["deployed_source_commit"] == target_head



def test_no_run_closeout_allows_one_authenticated_succeeded_run(tmp_path: Path) -> None:
    task_id = "BUREAU-CONTROL-PLANE-V3-FB-RUNTIME-REFRESH-BROWSER-CONTROL-RESOURCE-20260811"
    intent, store, result, resource_db = historical_no_run_success(tmp_path, task_id=task_id)
    run_id, _ = add_authority_run_receipt(store, task_id)
    release_test_leases(resource_db)
    run_before = store.run(run_id)
    receipt_before = store.receipt(run_id)
    # The run/receipt fixture is inserted directly to exercise only this guard;
    # preserve the production replay gate while declaring this synthetic projection healthy.
    store.replay_projection = lambda: {
        "matches_current": True,
        "authoritative_root_sha256": "c" * 64,
    }

    closeout = refresh.closeout_runtime_refresh_authority(
        state_root=Path(intent["state_root"]),
        approval_task_id=task_id,
        target_sha256=intent["target_sha256"],
        intent_sha256=intent["intent_sha256"],
        result_sha256=result["result_sha256"],
        resource_db=resource_db,
        now=NOW + timedelta(minutes=20),
        authority_store=store,
        readback=lambda **_: result["readback"],
    )

    assert closeout["closeout"]["status"] == "verified"
    assert store.task_spec(task_id)["spec"]["state"] == "verified"
    assert store.run(run_id) == run_before
    assert store.receipt(run_id) == receipt_before


@pytest.mark.parametrize(
    "state", ["assigned", "running", "verifying", "orphaned", "failed", "cancelled"]
)
def test_no_run_closeout_rejects_non_succeeded_run(tmp_path: Path, state: str) -> None:
    task_id = "BUREAU-CONTROL-PLANE-V3-FB-RUNTIME-REFRESH-BROWSER-CONTROL-RESOURCE-20260811"
    intent, store, result, resource_db = historical_no_run_success(tmp_path, task_id=task_id)
    add_authority_run_receipt(store, task_id, state=state)
    release_test_leases(resource_db)
    before = store.task_spec(task_id)

    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.closeout_runtime_refresh_authority(
            state_root=Path(intent["state_root"]),
            approval_task_id=task_id,
            target_sha256=intent["target_sha256"],
            intent_sha256=intent["intent_sha256"],
            result_sha256=result["result_sha256"],
            resource_db=resource_db,
            now=NOW + timedelta(minutes=20),
            authority_store=store,
            readback=lambda **_: result["readback"],
        )
    assert caught.value.code == "authority-closeout-run-not-succeeded"
    assert store.task_spec(task_id) == before


def test_no_run_closeout_rejects_succeeded_run_with_reservation(tmp_path: Path) -> None:
    task_id = "BUREAU-CONTROL-PLANE-V3-FB-RUNTIME-REFRESH-BROWSER-CONTROL-RESOURCE-20260811"
    intent, store, result, resource_db = historical_no_run_success(tmp_path, task_id=task_id)
    add_authority_run_receipt(store, task_id, with_reservation=True)
    release_test_leases(resource_db)
    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.closeout_runtime_refresh_authority(
            state_root=Path(intent["state_root"]), approval_task_id=task_id,
            target_sha256=intent["target_sha256"], intent_sha256=intent["intent_sha256"],
            result_sha256=result["result_sha256"], resource_db=resource_db,
            now=NOW + timedelta(minutes=20), authority_store=store,
            readback=lambda **_: result["readback"],
        )
    assert caught.value.code == "authority-closeout-run-reservations-present"


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (
            lambda r: r.__setitem__("task_sha256", "c" * 64),
            "authority-closeout-run-receipt-binding-mismatch",
        ),
        (
            lambda r: r.__setitem__("evidence", {}),
            "authority-closeout-run-receipt-acceptance-incomplete",
        ),
        (
            lambda r: r["evidence"]["terminal-run-proof"][
                "_source_authentication"
            ].__setitem__("plan_sha256", "c" * 64),
            "authority-closeout-run-receipt-authentication-invalid",
        ),
    ],
)
def test_no_run_closeout_rejects_bad_succeeded_run_receipt(
    tmp_path: Path, mutation: Any, code: str
) -> None:
    task_id = "BUREAU-CONTROL-PLANE-V3-FB-RUNTIME-REFRESH-BROWSER-CONTROL-RESOURCE-20260811"
    intent, store, result, resource_db = historical_no_run_success(tmp_path, task_id=task_id)
    add_authority_run_receipt(store, task_id, mutate_receipt=mutation)
    release_test_leases(resource_db)
    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.closeout_runtime_refresh_authority(
            state_root=Path(intent["state_root"]), approval_task_id=task_id,
            target_sha256=intent["target_sha256"], intent_sha256=intent["intent_sha256"],
            result_sha256=result["result_sha256"], resource_db=resource_db,
            now=NOW + timedelta(minutes=20), authority_store=store,
            readback=lambda **_: result["readback"],
        )
    assert caught.value.code == code


def test_no_run_closeout_rejects_multiple_runs(tmp_path: Path) -> None:
    task_id = "BUREAU-CONTROL-PLANE-V3-FB-RUNTIME-REFRESH-BROWSER-CONTROL-RESOURCE-20260811"
    intent, store, result, resource_db = historical_no_run_success(tmp_path, task_id=task_id)
    add_authority_run_receipt(store, task_id, run_suffix="one")
    add_authority_run_receipt(store, task_id, run_suffix="two")
    release_test_leases(resource_db)
    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.closeout_runtime_refresh_authority(
            state_root=Path(intent["state_root"]), approval_task_id=task_id,
            target_sha256=intent["target_sha256"], intent_sha256=intent["intent_sha256"],
            result_sha256=result["result_sha256"], resource_db=resource_db,
            now=NOW + timedelta(minutes=20), authority_store=store,
            readback=lambda **_: result["readback"],
        )
    assert caught.value.code == "authority-closeout-run-count-invalid"



def test_no_run_closeout_rejects_missing_succeeded_run_receipt(tmp_path: Path) -> None:
    task_id = "BUREAU-CONTROL-PLANE-V3-FB-RUNTIME-REFRESH-BROWSER-CONTROL-RESOURCE-20260811"
    intent, store, result, resource_db = historical_no_run_success(tmp_path, task_id=task_id)
    run_id, _ = add_authority_run_receipt(store, task_id)
    with store.immediate() as connection:
        connection.execute("DELETE FROM receipts WHERE run_id=?", (run_id,))
    release_test_leases(resource_db)
    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.closeout_runtime_refresh_authority(
            state_root=Path(intent["state_root"]), approval_task_id=task_id,
            target_sha256=intent["target_sha256"], intent_sha256=intent["intent_sha256"],
            result_sha256=result["result_sha256"], resource_db=resource_db,
            now=NOW + timedelta(minutes=20), authority_store=store,
            readback=lambda **_: result["readback"],
        )
    assert caught.value.code == "authority-closeout-run-receipt-missing"


def test_no_run_closeout_rejects_succeeded_run_receipt_digest_tamper(tmp_path: Path) -> None:
    task_id = "BUREAU-CONTROL-PLANE-V3-FB-RUNTIME-REFRESH-BROWSER-CONTROL-RESOURCE-20260811"
    intent, store, result, resource_db = historical_no_run_success(tmp_path, task_id=task_id)
    run_id, receipt = add_authority_run_receipt(store, task_id)
    tampered = dict(receipt)
    tampered["receipt_sha256"] = "0" * 64
    with store.immediate() as connection:
        connection.execute(
            "UPDATE receipts SET receipt_json=? WHERE run_id=?",
            (legacy.canonical_json(tampered), run_id),
        )
    release_test_leases(resource_db)
    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.closeout_runtime_refresh_authority(
            state_root=Path(intent["state_root"]), approval_task_id=task_id,
            target_sha256=intent["target_sha256"], intent_sha256=intent["intent_sha256"],
            result_sha256=result["result_sha256"], resource_db=resource_db,
            now=NOW + timedelta(minutes=20), authority_store=store,
            readback=lambda **_: result["readback"],
        )
    assert caught.value.code == "authority-closeout-run-receipt-digest-mismatch"

def accepted_history_revision(repository: Path) -> str:
    event_name = os.environ.get("GITHUB_EVENT_NAME")
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_name not in {"pull_request", "merge_group"} or not event_path:
        return "HEAD"

    try:
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "HEAD"
    if not isinstance(event, dict):
        return "HEAD"

    if event_name == "pull_request":
        pull_request = event.get("pull_request")
        base = pull_request.get("base") if isinstance(pull_request, dict) else None
    else:
        merge_group = event.get("merge_group")
        base = (
            {"sha": merge_group.get("base_sha")}
            if isinstance(merge_group, dict)
            else None
        )
    candidate = base.get("sha") if isinstance(base, dict) else None
    if not (
        isinstance(candidate, str)
        and len(candidate) == 40
        and all(character in "0123456789abcdefABCDEF" for character in candidate)
    ):
        return "HEAD"

    resolved = subprocess.run(
        ["git", "rev-parse", "--verify", f"{candidate}^{{commit}}"],
        cwd=repository,
        check=False,
        text=True,
        capture_output=True,
    )
    return resolved.stdout.strip() if resolved.returncode == 0 else "HEAD"


def historical_registry_json_blobs(registry_root: Path) -> list[tuple[str, str]]:
    repository = Path(git(registry_root, "rev-parse", "--show-toplevel"))
    relative_registry = registry_root.resolve().relative_to(repository.resolve())
    history_revision = accepted_history_revision(repository)
    result = subprocess.run(
        [
            "git",
            "log",
            history_revision,
            "-m",
            "--root",
            "--raw",
            "--no-abbrev",
            "--no-renames",
            "--diff-filter=AM",
            "--format=commit:%H",
            "--",
            relative_registry.as_posix(),
        ],
        cwd=repository,
        check=True,
        text=True,
        capture_output=True,
    )
    blobs: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        if not line.startswith(":") or "\t" not in line:
            continue
        metadata, path = line.split("\t", 1)
        fields = metadata.split()
        assert len(fields) == 5
        new_blob = fields[3]
        if path.endswith(".json"):
            blobs.append((new_blob, path))
    return blobs


def git_blob_batch(repository: Path, object_ids: set[str]) -> dict[str, bytes]:
    ordered = sorted(object_ids)
    if not ordered:
        return {}
    result = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=repository,
        input=("\n".join(ordered) + "\n").encode(),
        check=True,
        capture_output=True,
    )
    blobs: dict[str, bytes] = {}
    offset = 0
    for requested in ordered:
        header_end = result.stdout.index(b"\n", offset)
        header = result.stdout[offset:header_end].decode("ascii").split()
        assert len(header) == 3
        object_id, object_type, raw_size = header
        assert object_id == requested
        assert object_type == "blob"
        size = int(raw_size)
        content_start = header_end + 1
        content_end = content_start + size
        blobs[requested] = result.stdout[content_start:content_end]
        assert result.stdout[content_end : content_end + 1] == b"\n"
        offset = content_end + 1
    assert offset == len(result.stdout)
    return blobs


def source_precondition_authority_history(registry_root: Path) -> set[str]:
    repository = Path(git(registry_root, "rev-parse", "--show-toplevel"))
    historical = historical_registry_json_blobs(registry_root)
    blob_contents = git_blob_batch(repository, {object_id for object_id, _ in historical})
    authorities: set[str] = set()
    for object_id, path in historical:
        try:
            spec = json.loads(blob_contents[object_id].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(spec, dict):
            continue
        metadata = spec.get("metadata")
        authority = (
            metadata.get("runtime_refresh_authority") if isinstance(metadata, dict) else None
        )
        if isinstance(authority, dict) and "source_precondition" in authority:
            authorities.add(Path(path).name)
    return authorities


def validate_source_precondition_authority_registry(registry_root: Path) -> set[str]:
    observed: set[str] = set()
    for path in sorted(registry_root.glob("*.json")):
        spec = json.loads(path.read_text(encoding="utf-8"))
        metadata = spec.get("metadata")
        authority = (
            metadata.get("runtime_refresh_authority") if isinstance(metadata, dict) else None
        )
        if not isinstance(authority, dict) or "source_precondition" not in authority:
            continue
        observed.add(path.name)
        assert authority["mode"] == refresh.RUNTIME_AUTHORITY_MODE_SOURCE_PRECONDITION
        contract = refresh._validated_no_run_acceptance_contract(
            spec=spec, authority=authority
        )
        assert set(contract["criteria"]) == {
            criterion["id"] for criterion in spec["acceptance"]
        }
        assert any(
            "source-precondition" in item["required_evidence"]
            for item in contract["criteria"].values()
        )

    historical = source_precondition_authority_history(registry_root)
    missing = historical - observed
    assert not missing, f"historical source_precondition authorities disappeared: {sorted(missing)}"
    return observed


def source_precondition_authority_fixture(tmp_path: Path) -> tuple[Path, str]:
    source_root = Path(__file__).parents[1] / "registry/tasks"
    source_path = next(
        path
        for path in sorted(source_root.glob("*.json"))
        if "source_precondition"
        in json.loads(path.read_text(encoding="utf-8"))
        .get("metadata", {})
        .get("runtime_refresh_authority", {})
    )
    repository = tmp_path / "repository"
    target = repository / "registry/tasks"
    target.mkdir(parents=True)
    shutil.copy2(source_path, target / source_path.name)
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.name", "Test")
    git(repository, "config", "user.email", "test@example.invalid")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "baseline source-precondition authority")
    return target, source_path.name


def test_registry_source_precondition_authorities_have_complete_no_run_acceptance_contracts(
) -> None:
    registry_root = Path(__file__).parents[1] / "registry/tasks"
    observed = validate_source_precondition_authority_registry(registry_root)
    assert observed


def test_registry_source_precondition_authorities_allow_uncommitted_addition(
    tmp_path: Path,
) -> None:
    registry_root, source_name = source_precondition_authority_fixture(tmp_path)
    additive = json.loads((registry_root / source_name).read_text(encoding="utf-8"))
    additive["id"] = "BUREAU-TEST-UNCOMMITTED-SOURCE-PRECONDITION-AUTHORITY"
    additive_path = registry_root / f"{additive['id']}.json"
    additive_path.write_text(json.dumps(additive, indent=2) + "\n", encoding="utf-8")

    observed = validate_source_precondition_authority_registry(registry_root)

    assert additive_path.name in observed
    assert additive_path.name not in source_precondition_authority_history(registry_root)


def test_source_precondition_history_skips_non_object_json_blobs(
    tmp_path: Path,
) -> None:
    registry_root, source_name = source_precondition_authority_fixture(tmp_path)
    historical_path = registry_root / "BUREAU-TEST-NONOBJECT-HISTORY.json"
    historical_path.write_text("[]\n", encoding="utf-8")
    git(registry_root, "add", historical_path.name)
    git(registry_root, "commit", "-m", "add historical non-object task json")

    historical_path.write_text(
        json.dumps({"id": "BUREAU-TEST-NONOBJECT-HISTORY", "metadata": {}}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    git(registry_root, "add", historical_path.name)
    git(registry_root, "commit", "-m", "repair historical non-object task json")

    historical = source_precondition_authority_history(registry_root)

    assert source_name in historical
    assert historical_path.name not in historical
    validate_source_precondition_authority_registry(registry_root)


def test_registry_source_precondition_authorities_allow_additive_authority(
    tmp_path: Path,
) -> None:
    registry_root, source_name = source_precondition_authority_fixture(tmp_path)
    additive = json.loads((registry_root / source_name).read_text(encoding="utf-8"))
    additive["id"] = "BUREAU-TEST-ADDITIVE-SOURCE-PRECONDITION-AUTHORITY"
    additive_path = registry_root / f"{additive['id']}.json"
    additive_path.write_text(json.dumps(additive, indent=2) + "\n", encoding="utf-8")
    git(registry_root, "add", additive_path.name)
    git(registry_root, "commit", "-m", "add source-precondition authority")

    observed = validate_source_precondition_authority_registry(registry_root)

    assert additive_path.name in observed
    assert additive_path.name in source_precondition_authority_history(registry_root)


def test_source_precondition_history_ignores_text_match_and_commit_neighbor(
    tmp_path: Path,
) -> None:
    registry_root, source_name = source_precondition_authority_fixture(tmp_path)
    authority = json.loads((registry_root / source_name).read_text(encoding="utf-8"))
    authority["id"] = "BUREAU-TEST-HISTORICAL-SOURCE-PRECONDITION-AUTHORITY"
    authority_path = registry_root / f"{authority['id']}.json"
    authority_path.write_text(json.dumps(authority, indent=2) + "\n", encoding="utf-8")

    textual_decoy = registry_root / "BUREAU-TEST-SOURCE-PRECONDITION-TEXT-DECOY.json"
    textual_decoy.write_text(
        json.dumps(
            {
                "id": "BUREAU-TEST-SOURCE-PRECONDITION-TEXT-DECOY",
                "metadata": {"evidence": "source_precondition"},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    commit_neighbor = registry_root / "BUREAU-TEST-SOURCE-PRECONDITION-NEIGHBOR.json"
    commit_neighbor.write_text(
        json.dumps(
            {
                "id": "BUREAU-TEST-SOURCE-PRECONDITION-NEIGHBOR",
                "metadata": {"evidence": "ordinary"},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    git(registry_root, "add", authority_path.name, textual_decoy.name, commit_neighbor.name)
    git(registry_root, "commit", "-m", "add authority with non-authority neighbors")

    historical = source_precondition_authority_history(registry_root)

    assert authority_path.name in historical
    assert textual_decoy.name not in historical
    assert commit_neighbor.name not in historical


def test_accepted_history_revision_uses_merge_group_base_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_root, _ = source_precondition_authority_fixture(tmp_path)
    repository = registry_root.parents[1]
    accepted_base = git(repository, "rev-parse", "HEAD")
    (repository / "candidate.txt").write_text("candidate\n", encoding="utf-8")
    git(repository, "add", "candidate.txt")
    git(repository, "commit", "-m", "merge-group candidate")

    event_path = tmp_path / "merge-group-event.json"
    event_path.write_text(
        json.dumps({"merge_group": {"base_sha": accepted_base}}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GITHUB_EVENT_NAME", "merge_group")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))

    assert accepted_history_revision(repository) == accepted_base


def test_source_precondition_history_ignores_unaccepted_pr_intermediate_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_root, source_name = source_precondition_authority_fixture(tmp_path)
    repository = registry_root.parents[1]
    accepted_base = git(repository, "rev-parse", "HEAD")

    transient = json.loads((registry_root / source_name).read_text(encoding="utf-8"))
    transient["id"] = "BUREAU-TEST-UNACCEPTED-PR-SOURCE-PRECONDITION-AUTHORITY"
    transient_path = registry_root / f"{transient['id']}.json"
    transient_path.write_text(json.dumps(transient, indent=2) + "\n", encoding="utf-8")
    git(repository, "add", transient_path.relative_to(repository).as_posix())
    git(repository, "commit", "-m", "add transient pull-request authority")
    transient_path.unlink()
    git(repository, "add", "-u")
    git(repository, "commit", "-m", "remove transient pull-request authority")

    event_path = tmp_path / "pull-request-event.json"
    event_path.write_text(
        json.dumps({"pull_request": {"base": {"sha": accepted_base}}}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))

    historical = source_precondition_authority_history(registry_root)

    assert source_name in historical
    assert transient_path.name not in historical
    validate_source_precondition_authority_registry(registry_root)


def test_pr_base_history_still_rejects_accepted_authority_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_root, source_name = source_precondition_authority_fixture(tmp_path)
    repository = registry_root.parents[1]
    accepted_base = git(repository, "rev-parse", "HEAD")
    (registry_root / source_name).unlink()
    git(repository, "add", "-u")
    git(repository, "commit", "-m", "delete accepted source-precondition authority")

    event_path = tmp_path / "pull-request-event.json"
    event_path.write_text(
        json.dumps({"pull_request": {"base": {"sha": accepted_base}}}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))

    with pytest.raises(
        AssertionError, match="historical source_precondition authorities disappeared"
    ):
        validate_source_precondition_authority_registry(registry_root)


def test_registry_source_precondition_authorities_reject_historical_deletion(
    tmp_path: Path,
) -> None:
    registry_root, source_name = source_precondition_authority_fixture(tmp_path)
    (registry_root / source_name).unlink()
    git(registry_root, "add", "-u")
    git(registry_root, "commit", "-m", "delete source-precondition authority")

    with pytest.raises(
        AssertionError, match="historical source_precondition authorities disappeared"
    ):
        validate_source_precondition_authority_registry(registry_root)


def test_source_precondition_history_includes_merge_result_only_authority(
    tmp_path: Path,
) -> None:
    registry_root, source_name = source_precondition_authority_fixture(tmp_path)
    repository = registry_root.parents[1]

    git(repository, "switch", "-c", "side")
    (repository / "side.txt").write_text("side\n", encoding="utf-8")
    git(repository, "add", "side.txt")
    git(repository, "commit", "-m", "side parent")

    git(repository, "switch", "main")
    (repository / "main.txt").write_text("main\n", encoding="utf-8")
    git(repository, "add", "main.txt")
    git(repository, "commit", "-m", "main parent")
    git(repository, "merge", "--no-ff", "--no-commit", "side")

    authority = json.loads((registry_root / source_name).read_text(encoding="utf-8"))
    authority["id"] = "BUREAU-TEST-MERGE-RESULT-SOURCE-PRECONDITION-AUTHORITY"
    authority_path = registry_root / f"{authority['id']}.json"
    authority_path.write_text(json.dumps(authority, indent=2) + "\n", encoding="utf-8")
    git(repository, "add", authority_path.relative_to(repository).as_posix())
    git(repository, "commit", "-m", "merge with result-only authority")

    relative_path = authority_path.relative_to(repository).as_posix()
    for parent in ("HEAD^1", "HEAD^2"):
        absent = subprocess.run(
            ["git", "cat-file", "-e", f"{parent}:{relative_path}"],
            cwd=repository,
            capture_output=True,
        )
        assert absent.returncode != 0

    assert authority_path.name in source_precondition_authority_history(registry_root)

    authority_path.unlink()
    git(repository, "add", "-u")
    git(repository, "commit", "-m", "delete merge-result authority")

    with pytest.raises(
        AssertionError, match="historical source_precondition authorities disappeared"
    ):
        validate_source_precondition_authority_registry(registry_root)


def test_registry_source_precondition_authorities_reject_malformed_addition(
    tmp_path: Path,
) -> None:
    registry_root, source_name = source_precondition_authority_fixture(tmp_path)
    malformed = json.loads((registry_root / source_name).read_text(encoding="utf-8"))
    malformed["id"] = "BUREAU-TEST-MALFORMED-SOURCE-PRECONDITION-AUTHORITY"
    malformed["metadata"]["runtime_refresh_authority"]["mode"] = "invalid-mode"
    malformed_path = registry_root / f"{malformed['id']}.json"
    malformed_path.write_text(json.dumps(malformed, indent=2) + "\n", encoding="utf-8")
    git(registry_root, "add", malformed_path.name)
    git(registry_root, "commit", "-m", "add malformed source-precondition authority")

    with pytest.raises(AssertionError):
        validate_source_precondition_authority_registry(registry_root)



def test_protected_publication_adoption_proof_requires_exact_seed_receipt() -> None:
    from bureau import task_specs as task_specs_module

    task_id = "BUREAU-TEST-PROTECTED-PUBLICATION-ADOPTION"
    merge_commit = "a" * 40
    target_commit = "b" * 40
    historical_spec = runtime_authority_spec(task_id)
    historical_spec["metadata"]["publication_path"] = {
        "kind": "normal-protected-pull-request",
        "scope": f"exactly registry/tasks/{task_id}.json",
        "state_store_transition": "seed-missing-preserve-state-store",
    }
    current_spec = json.loads(json.dumps(historical_spec))
    current_spec["metadata"]["protected_publication_adoption"] = {
        "schema_version": 1,
        "repository": refresh.DEFAULT_REPOSITORY,
        "publication_pr": 2173,
        "publication_merge_commit": merge_commit,
        "required_checks": list(refresh.DEFAULT_AUTHORITY_ADOPTION_REQUIRED_CHECKS),
    }
    raw = (json.dumps(historical_spec, indent=2) + "\n").encode()
    historical_digest = task_specs_module.task_spec_digest(historical_spec)
    key = f"legacy-seed-exact:{task_id}:{historical_digest}"
    receipt = {
        "idempotency_key": key,
        "task_id": task_id,
        "expected_revision": None,
        "requested_sha256": historical_digest,
        "resulting_revision": 1,
        "resulting_task_spec": {
            "task_id": task_id,
            "revision": 1,
            "spec_sha256": historical_digest,
            "spec": historical_spec,
            "source": "legacy-git-exact-seed",
        },
    }

    class ReceiptStore:
        def __init__(self, value):
            self.value = value
            self.seen = None

        def task_spec_mutation_receipt(self, idempotency_key: str):
            self.seen = idempotency_key
            return self.value

    check_rollup = [
        {"name": name, "status": "COMPLETED", "conclusion": "SUCCESS"}
        for name in refresh.DEFAULT_AUTHORITY_ADOPTION_REQUIRED_CHECKS
    ]

    def github(arguments: list[str]):
        if arguments == ["api", f"repos/{refresh.DEFAULT_REPOSITORY}/branches/main/protection"]:
            return {
                "required_status_checks": {
                    "strict": True,
                    "contexts": list(refresh.DEFAULT_AUTHORITY_ADOPTION_REQUIRED_CHECKS),
                },
                "allow_force_pushes": {"enabled": False},
                "allow_deletions": {"enabled": False},
            }
        if arguments[:2] == ["pr", "view"]:
            return {
                "number": 2173,
                "state": "MERGED",
                "isDraft": False,
                "mergedAt": "2026-08-25T00:00:00Z",
                "mergeCommit": {"oid": merge_commit},
                "baseRefName": "main",
                "files": [{"path": f"registry/tasks/{task_id}.json"}],
                "statusCheckRollup": check_rollup,
            }
        if arguments == [
            "api",
            f"repos/{refresh.DEFAULT_REPOSITORY}/contents/registry/tasks/{task_id}.json?ref={merge_commit}",
        ]:
            return {
                "type": "file",
                "path": f"registry/tasks/{task_id}.json",
                "encoding": "base64",
                "content": base64.b64encode(raw).decode(),
            }
        if arguments == [
            "api",
            f"repos/{refresh.DEFAULT_REPOSITORY}/compare/{merge_commit}...{target_commit}",
        ]:
            return {
                "status": "ahead",
                "behind_by": 0,
                "merge_base_commit": {"sha": merge_commit},
            }
        raise AssertionError(arguments)

    store = ReceiptStore(receipt)
    evidence = refresh._prove_protected_publication_adoption(
        store=store,
        spec=current_spec,
        approval_task_id=task_id,
        target_main_commit=target_commit,
        github=github,
    )
    assert store.seen == key
    assert evidence["kind"] == "bureau_runtime_refresh_protected_publication_adoption_evidence"
    assert evidence["adoption_revision"] == 1

    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh._prove_protected_publication_adoption(
            store=ReceiptStore(None),
            spec=current_spec,
            approval_task_id=task_id,
            target_main_commit=target_commit,
            github=github,
        )
    assert caught.value.code == "authority-closeout-protected-publication-adoption-unproven"


def test_registry_resource_intake_proof_binds_resource_and_candidate(
    tmp_path: Path, monkeypatch
) -> None:
    from bureau import operator_intake

    resources = tmp_path / "registry" / "resources"
    resources.mkdir(parents=True)
    (resources / "repo.json").write_text(
        json.dumps({"schema_version": 1, "id": "repo", "type": "namespace", "parent": None})
        + "\n",
        encoding="utf-8",
    )
    expected = {
        "id": "repo.hall-of-memory",
        "type": "git-repository",
        "path": "/home/alex/repos/hall-of-memory",
        "github_slug": "Hall-of-Memory/Hall-of-Memory",
        "grabowski_key": "repo:/home/alex/repos/hall-of-memory",
    }
    (resources / "hall-of-memory.json").write_text(
        json.dumps({"schema_version": 1, "parent": "repo", **expected}) + "\n",
        encoding="utf-8",
    )
    candidate_id = "candidate-5d65409c452e2148623cf9ed"
    spec = runtime_authority_spec("BUREAU-TEST-REGISTRY-RESOURCE-INTAKE")
    spec["metadata"]["no_run_closeout_registry_resource_intake"] = {
        "schema_version": 1,
        "candidate_id": candidate_id,
        "resource": expected,
    }
    monkeypatch.setattr(
        operator_intake,
        "_candidate_assess",
        lambda registry, store, **kwargs: {
            "schema_version": 1,
            "kind": "bureau_candidate_assessment",
            "status": "assessed",
            "candidate_id": candidate_id,
            "event_id": 42,
            "decision": "promote",
            "target": {
                "claims": [
                    {"resource": expected["id"], "mode": "write", "isolation": "worktree"}
                ]
            },
            "missing_fields": [],
        },
    )
    evidence = refresh._prove_registry_resource_intake(
        store=object(),
        spec=spec,
        registry_root=tmp_path,
    )
    assert evidence["resource"] == expected
    assert evidence["candidate_id"] == candidate_id

    bad = json.loads(json.dumps(spec))
    bad_contract = bad["metadata"]["no_run_closeout_registry_resource_intake"]
    bad_contract["resource"]["path"] = "/home/alex/repos/not-hall-of-memory"
    bad_contract["resource"]["grabowski_key"] = "repo:/home/alex/repos/not-hall-of-memory"
    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh._prove_registry_resource_intake(
            store=object(),
            spec=bad,
            registry_root=tmp_path,
        )
    assert caught.value.code == "authority-closeout-registry-resource-intake-unproven"


def test_registry_special_acceptance_criteria_require_dedicated_proof_classes() -> None:
    root = Path(__file__).parents[1] / "registry" / "tasks"
    publication_tasks = [
        "BUREAU-CONTROL-PLANE-V3-FB-RUNTIME-REFRESH-PR2172-SOURCE-CONVERGENCE-20260825",
        "BUREAU-CONTROL-PLANE-V3-FB-RUNTIME-REFRESH-AFTER-PR2174-REPOGROUND-T020-20260825",
    ]
    for task_id in publication_tasks:
        spec = json.loads((root / f"{task_id}.json").read_text(encoding="utf-8"))
        metadata = spec["metadata"]
        assert (
            metadata["protected_publication_adoption"]["repository"]
            == refresh.DEFAULT_REPOSITORY
        )
        mapping = metadata["runtime_refresh_authority"]["no_run_closeout_acceptance"]["criteria"]
        assert mapping["protected-publication-and-missing-only-adoption"]["required_evidence"] == [
            "protected-publication-adoption"
        ]

    hall = json.loads(
        (
            root
            / "BUREAU-CONTROL-PLANE-V3-FB-RUNTIME-REFRESH-HALL-OF-MEMORY-RESOURCE-20260824.json"
        ).read_text(encoding="utf-8")
    )
    hall_metadata = hall["metadata"]
    assert hall_metadata["no_run_closeout_registry_resource_intake"]["candidate_id"] == (
        "candidate-5d65409c452e2148623cf9ed"
    )
    hall_mapping = hall_metadata["runtime_refresh_authority"]["no_run_closeout_acceptance"][
        "criteria"
    ]
    assert "registry-resource-intake" in hall_mapping["hall-of-memory-resource-visible"][
        "required_evidence"
    ]
