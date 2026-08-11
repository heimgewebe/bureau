from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bureau.acceptance import EVIDENCE_KIND, PASSED, UNKNOWN
from bureau.closure_observer import (
    EVIDENCE_BUNDLE_KIND,
    evaluate_run,
    reconcile_run,
    reconcile_runs,
    reconcile_state_evidence,
)
from bureau.core import Dispatcher, Registry, StateStore

TASK_SHA = "a" * 64
PLAN_SHA = "b" * 64
NOW = "2026-08-10T20:00:00Z"


def manual_criterion() -> dict[str, object]:
    return {
        "id": "manual",
        "assertion": "manual observation confirms effect",
        "evidence_type": "object",
        "verifier": "manual_observation",
        "verifier_config": {"observation_scope": "manual:test"},
    }


def manual_evidence(
    *,
    observed_at: str = "2026-08-10T19:59:00Z",
    task_sha256: str = TASK_SHA,
    plan_sha256: str = PLAN_SHA,
) -> dict[str, object]:
    return {
        "manual": {
            "schema_version": 1,
            "kind": EVIDENCE_KIND,
            "criterion_id": "manual",
            "evidence_type": "manual_observation",
            "source": {"authority": "manual", "reference": "operator:test"},
            "observed_at": observed_at,
            "revision": {
                "task_sha256": task_sha256,
                "plan_sha256": plan_sha256,
                "observation_scope": "manual:test",
            },
            "facts": {
                "accepted": True,
                "observer": "operator",
                "observation": "live behavior confirmed",
                "observation_scope": "manual:test",
            },
        }
    }


class FakeStore:
    def __init__(
        self,
        root: Path,
        *,
        state: str = "assigned",
        criteria: list[dict[str, object]] | None = None,
    ) -> None:
        self.root = root
        self._run = {
            "run_id": "RUN-1",
            "task_id": "TASK-1",
            "state": state,
            "task_sha256": TASK_SHA,
            "plan_sha256": PLAN_SHA,
        }
        self._envelope = {
            "run_id": "RUN-1",
            "task_id": "TASK-1",
            "task": {
                "id": "TASK-1",
                "acceptance": criteria if criteria is not None else [manual_criterion()],
            },
        }
        self._path = root / "RUN-1.json"
        self._path.write_text(json.dumps(self._envelope), encoding="utf-8")

    def run(self, run_id: str) -> dict[str, Any]:
        assert run_id == "RUN-1"
        return dict(self._run)

    def list_runs(self) -> list[dict[str, Any]]:
        return [dict(self._run)]

    def envelope_path(self, run_id: str) -> Path:
        assert run_id == "RUN-1"
        return self._path

    def receipt(self, run_id: str) -> dict[str, Any] | None:
        assert run_id == "RUN-1"
        return {"run_id": run_id, "receipt_sha256": "f" * 64}


def test_passing_typed_evidence_calls_existing_completion_path_once(tmp_path: Path) -> None:
    store = FakeStore(tmp_path)
    calls: list[dict[str, Any]] = []

    def completion(registry, observed_store, run_id, evidence):
        calls.append(
            {
                "registry": registry,
                "store": observed_store,
                "run_id": run_id,
                "evidence": evidence,
            }
        )
        return {"idempotent": False, "receipt": {"receipt_sha256": "f" * 64}}

    result = reconcile_run(
        "registry",
        store,
        "RUN-1",
        manual_evidence(),
        now=NOW,
        completion=completion,
        authenticated_criterion_ids={"manual"},
    )

    assert result["state"] == "terminalized"
    assert result["mutated"] is True
    assert result["evaluation"]["state"] == PASSED
    assert len(calls) == 1
    assert calls[0]["run_id"] == "RUN-1"
    assert calls[0]["evidence"]["_typed_acceptance"]["state"] == PASSED


def test_stale_evidence_stays_open_and_never_calls_completion(tmp_path: Path) -> None:
    store = FakeStore(tmp_path)
    calls: list[str] = []

    def completion(registry, observed_store, run_id, evidence):
        calls.append(run_id)
        return {"idempotent": False}

    result = reconcile_run(
        "registry",
        store,
        "RUN-1",
        manual_evidence(observed_at="2026-08-08T00:00:00Z"),
        now=NOW,
        completion=completion,
        authenticated_criterion_ids={"manual"},
    )

    assert result["state"] == "open"
    assert result["mutated"] is False
    assert result["evaluation"]["state"] == UNKNOWN
    assert calls == []


def test_untyped_acceptance_stays_open(tmp_path: Path) -> None:
    store = FakeStore(
        tmp_path,
        criteria=[{"id": "legacy", "assertion": "legacy prose criterion"}],
    )

    result = evaluate_run(store, "RUN-1", {"legacy": True}, now=NOW)

    assert result["state"] == "open"
    assert result["evaluation"]["state"] == UNKNOWN
    assert result["evaluation"]["criteria"][0]["reason"] == "criterion-is-not-typed"


def test_unreadable_envelope_is_open_not_failed(tmp_path: Path) -> None:
    store = FakeStore(tmp_path)
    store.envelope_path("RUN-1").write_text("not-json", encoding="utf-8")

    result = evaluate_run(store, "RUN-1", manual_evidence(), now=NOW)

    assert result["state"] == "open"
    assert result["reason"] == "envelope-unreadable"
    assert result["mutated"] is False


def test_terminal_run_is_read_back_without_new_mutation(tmp_path: Path) -> None:
    store = FakeStore(tmp_path, state="succeeded")

    result = evaluate_run(store, "RUN-1", {}, now=NOW)

    assert result["state"] == "already_terminal"
    assert result["mutated"] is False
    assert result["receipt"]["run_id"] == "RUN-1"


def test_evidence_provider_failure_is_unknown_open_state(tmp_path: Path) -> None:
    store = FakeStore(tmp_path)
    calls: list[str] = []

    def provider(run, envelope):
        raise OSError("source temporarily unavailable")

    def completion(registry, observed_store, run_id, evidence):
        calls.append(run_id)
        return {"idempotent": False}

    result = reconcile_runs(
        "registry",
        store,
        provider,
        now=NOW,
        completion=completion,
    )

    assert result["terminalized_count"] == 0
    assert result["open_count"] == 1
    assert result["observations"][0]["reason"] == "evidence-provider-unavailable"
    assert calls == []


def test_typed_pass_uses_real_complete_run_state_store_path(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    root = registry_factory(1)
    task_path = root / "registry/tasks/BUR-TEST-001-T001.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task["acceptance"] = [manual_criterion()]
    task_path.write_text(json.dumps(task), encoding="utf-8")

    state_root = tmp_path / "integration-state"
    monkeypatch.setenv("BUREAU_STATE_DIR", str(state_root))
    registry = Registry.load(root)
    store = StateStore(state_root / "bureau.sqlite3")
    dispatcher = Dispatcher(registry, store)
    run = dispatcher.claim_next("worker", ("repository",))["run"]

    result = reconcile_run(
        registry,
        store,
        run["run_id"],
        manual_evidence(
            task_sha256=run["task_sha256"],
            plan_sha256=run["plan_sha256"],
        ),
        now=NOW,
        authenticated_criterion_ids={"manual"},
    )

    assert result["state"] == "terminalized"
    assert result["completion"]["idempotent"] is False
    assert store.run(run["run_id"])["state"] == "succeeded"
    receipt = store.receipt(run["run_id"])
    assert receipt is not None
    assert result["evaluation"]["state"] == PASSED
    assert receipt["evidence"]["manual"]["kind"] == EVIDENCE_KIND
    assert receipt["evidence"]["manual"]["revision"]["task_sha256"] == run["task_sha256"]


def test_state_root_manual_bundle_cannot_self_authenticate_production_writer(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    root = registry_factory(1)
    task_path = root / "registry/tasks/BUR-TEST-001-T001.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task["acceptance"] = [manual_criterion()]
    task_path.write_text(json.dumps(task), encoding="utf-8")

    state_root = tmp_path / "production-state"
    monkeypatch.setenv("BUREAU_STATE_DIR", str(state_root))
    registry = Registry.load(root)
    store = StateStore(state_root / "bureau.sqlite3")
    run = Dispatcher(registry, store).claim_next("worker", ("repository",))["run"]
    evidence_dir = state_root / "acceptance-evidence"
    evidence_dir.mkdir()
    bundle = {
        "schema_version": 1,
        "kind": EVIDENCE_BUNDLE_KIND,
        "run_id": run["run_id"],
        "task_id": run["task_id"],
        "task_sha256": run["task_sha256"],
        "plan_sha256": run["plan_sha256"],
        "evidence": manual_evidence(
            task_sha256=run["task_sha256"],
            plan_sha256=run["plan_sha256"],
        ),
    }
    (evidence_dir / f"{run['run_id']}.json").write_text(json.dumps(bundle), encoding="utf-8")

    result = reconcile_state_evidence(registry, store, now=NOW)

    assert result["writer"] == "bureau-reconcile"
    assert result["terminalized_count"] == 0
    assert result["open_count"] == 1
    assert result["observations"][0]["evaluation"]["criteria"][0]["reason"] == (
        "evidence-source-unauthenticated"
    )
    assert store.run(run["run_id"])["state"] == "assigned"


def test_state_root_bundle_binding_drift_stays_open(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    root = registry_factory(1)
    task_path = root / "registry/tasks/BUR-TEST-001-T001.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task["acceptance"] = [manual_criterion()]
    task_path.write_text(json.dumps(task), encoding="utf-8")

    state_root = tmp_path / "drift-state"
    monkeypatch.setenv("BUREAU_STATE_DIR", str(state_root))
    registry = Registry.load(root)
    store = StateStore(state_root / "bureau.sqlite3")
    run = Dispatcher(registry, store).claim_next("worker", ("repository",))["run"]
    evidence_dir = state_root / "acceptance-evidence"
    evidence_dir.mkdir()
    bundle = {
        "schema_version": 1,
        "kind": EVIDENCE_BUNDLE_KIND,
        "run_id": run["run_id"],
        "task_id": run["task_id"],
        "task_sha256": "9" * 64,
        "plan_sha256": run["plan_sha256"],
        "evidence": manual_evidence(
            task_sha256=run["task_sha256"],
            plan_sha256=run["plan_sha256"],
        ),
    }
    (evidence_dir / f"{run['run_id']}.json").write_text(json.dumps(bundle), encoding="utf-8")

    result = reconcile_state_evidence(registry, store, now=NOW)

    assert result["terminalized_count"] == 0
    assert result["open_count"] == 1
    assert result["observations"][0]["reason"] == "evidence-provider-unavailable"
    assert store.run(run["run_id"])["state"] == "assigned"

def github_merge_criterion(head_sha: str = "c" * 40) -> dict[str, object]:
    return {
        "id": "merge",
        "assertion": "exact pull request is merged",
        "evidence_type": "object",
        "verifier": "code_merged",
        "verifier_config": {
            "repository": "heimgewebe/test",
            "pull_request": 7,
            "head_sha": head_sha,
        },
    }


def github_merge_evidence(
    *, task_sha256: str, plan_sha256: str, merge_sha: str = "d" * 40
) -> dict[str, object]:
    head_sha = "c" * 40
    return {
        "merge": {
            "schema_version": 1,
            "kind": EVIDENCE_KIND,
            "criterion_id": "merge",
            "evidence_type": "code_merged",
            "source": {
                "authority": "github",
                "reference": f"github-pr:heimgewebe/test#7@{head_sha}",
            },
            "observed_at": "2026-08-10T19:59:00Z",
            "revision": {
                "task_sha256": task_sha256,
                "plan_sha256": plan_sha256,
                "head_sha": head_sha,
                "merge_commit_sha": merge_sha,
            },
            "facts": {
                "merged": True,
                "head_sha": head_sha,
                "merge_commit_sha": merge_sha,
            },
        }
    }


def merged_pr_detail(merge_sha: str = "d" * 40) -> dict[str, object]:
    return {
        "number": 7,
        "state": "MERGED",
        "isDraft": False,
        "mergedAt": "2026-08-10T19:59:00Z",
        "mergeCommit": {"oid": merge_sha},
        "headRefOid": "c" * 40,
        "baseRefName": "main",
        "statusCheckRollup": [],
        "url": "https://example.invalid/7",
    }


def _github_production_fixture(registry_factory, tmp_path: Path, monkeypatch):
    root = registry_factory(1)
    task_path = root / "registry/tasks/BUR-TEST-001-T001.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task["acceptance"] = [github_merge_criterion()]
    task_path.write_text(json.dumps(task), encoding="utf-8")
    state_root = tmp_path / "github-production-state"
    monkeypatch.setenv("BUREAU_STATE_DIR", str(state_root))
    registry = Registry.load(root)
    store = StateStore(state_root / "bureau.sqlite3")
    run = Dispatcher(registry, store).claim_next("worker", ("repository",))["run"]
    evidence_dir = state_root / "acceptance-evidence"
    evidence_dir.mkdir()
    bundle = {
        "schema_version": 1,
        "kind": EVIDENCE_BUNDLE_KIND,
        "run_id": run["run_id"],
        "task_id": run["task_id"],
        "task_sha256": run["task_sha256"],
        "plan_sha256": run["plan_sha256"],
        "evidence": github_merge_evidence(
            task_sha256=run["task_sha256"], plan_sha256=run["plan_sha256"]
        ),
    }
    (evidence_dir / f"{run['run_id']}.json").write_text(
        json.dumps(bundle), encoding="utf-8"
    )
    return registry, store, run


def test_state_root_github_bundle_terminalizes_only_after_live_authentication(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    registry, store, run = _github_production_fixture(registry_factory, tmp_path, monkeypatch)
    calls: list[list[str]] = []

    def github(argv):
        calls.append(argv)
        return merged_pr_detail()

    result = reconcile_state_evidence(registry, store, now=NOW, github=github)

    assert result["terminalized_count"] == 1
    assert store.run(run["run_id"])["state"] == "succeeded"
    assert calls and calls[0][:4] == ["pr", "view", "7", "--repo"]
    receipt = store.receipt(run["run_id"])
    assert receipt is not None
    authentication = receipt["evidence"]["merge"]["_source_authentication"]
    assert authentication["kind"] == "bureau.acceptance_source_authentication"
    assert authentication["observer"] == "runtime_refresh.gh_json"
    assert authentication["target"] == {
        "repository": "heimgewebe/test",
        "pull_request": 7,
        "head_sha": "c" * 40,
    }
    assert len(authentication["live_observation_sha256"]) == 64


def test_forged_github_bundle_stays_open_when_live_source_disagrees(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    registry, store, run = _github_production_fixture(registry_factory, tmp_path, monkeypatch)

    def github(argv):
        return merged_pr_detail(merge_sha="9" * 40)

    result = reconcile_state_evidence(registry, store, now=NOW, github=github)

    assert result["terminalized_count"] == 0
    assert result["open_count"] == 1
    assert result["observations"][0]["evaluation"]["criteria"][0]["reason"] == (
        "evidence-source-unauthenticated"
    )
    assert store.run(run["run_id"])["state"] == "assigned"
