from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from bureau import cli as bureau_cli
from bureau import closure_observer, legacy, state_events
from bureau.acceptance import EVIDENCE_KIND, PASSED, UNKNOWN
from bureau.closure_observer import (
    EVIDENCE_BUNDLE_KIND,
    EvidenceAdapterUnavailable,
    authenticate_state_evidence,
    evaluate_run,
    reconcile_run,
    reconcile_runs,
    reconcile_state_evidence,
    record_manual_acceptance_authentication,
)
from bureau.core import (
    Dispatcher,
    Registry,
    StateError,
    StateStore,
    task_revision_sha256,
)
from bureau.v2 import RunStateConflict, fail_run, state_root_hygiene

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
        self._run["envelope_sha256"] = legacy.sha256_json(self._envelope)
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


def test_passing_typed_evidence_calls_custom_completion_once(tmp_path: Path) -> None:
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
    assert calls[0]["evidence"] == manual_evidence()
    assert "_typed_acceptance" not in calls[0]["evidence"]


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


def test_untyped_acceptance_returns_stable_bound_diagnostic_without_terminalization(
    tmp_path: Path,
) -> None:
    store = FakeStore(
        tmp_path,
        criteria=[{"id": "legacy", "assertion": "legacy prose criterion"}],
    )

    result = evaluate_run(store, "RUN-1", {"legacy": True}, now=NOW)
    repeated = evaluate_run(store, "RUN-1", {"legacy": True}, now=NOW)

    assert result["state"] == "open"
    assert result["reason"] == "invalid-acceptance-contract"
    assert result["mutated"] is False
    diagnostic = result["diagnostic"]
    assert diagnostic["task_id"] == "TASK-1"
    assert diagnostic["run_id"] == "RUN-1"
    assert diagnostic["task_sha256"] == TASK_SHA
    assert diagnostic["plan_sha256"] == PLAN_SHA
    assert diagnostic["diagnostics"][0]["path"] == "$.acceptance[0].evidence_type"
    assert "Register a new TaskSpec revision" in diagnostic["repair_action"]
    assert repeated["diagnostic"]["fingerprint"] == diagnostic["fingerprint"]

    calls: list[str] = []

    def completion(registry, observed_store, run_id, evidence):
        calls.append(run_id)
        return {"idempotent": False}

    reconciled = reconcile_run(
        "registry", store, "RUN-1", {"legacy": True}, now=NOW, completion=completion
    )
    assert reconciled["state"] == "open"
    assert reconciled["mutated"] is False
    assert calls == []


def test_untyped_acceptance_is_diagnosed_before_evidence_provider(tmp_path: Path) -> None:
    store = FakeStore(
        tmp_path,
        criteria=[{"id": "legacy", "assertion": "legacy prose criterion"}],
    )
    provider_calls: list[str] = []

    def provider(run, envelope):
        provider_calls.append(str(run["run_id"]))
        raise AssertionError("invalid acceptance must not reach evidence collection")

    result = reconcile_runs("registry", store, provider, now=NOW)

    assert result["terminalized_count"] == 0
    assert result["open_count"] == 1
    assert result["observations"][0]["reason"] == "invalid-acceptance-contract"
    assert provider_calls == []


def test_unreadable_envelope_is_open_not_failed(tmp_path: Path) -> None:
    store = FakeStore(tmp_path)
    store.envelope_path("RUN-1").write_text("not-json", encoding="utf-8")

    result = evaluate_run(store, "RUN-1", manual_evidence(), now=NOW)

    assert result["state"] == "open"
    assert result["reason"] == "envelope-unreadable"
    assert result["mutated"] is False


def test_envelope_sidecar_hash_drift_stays_open(tmp_path: Path) -> None:
    store = FakeStore(tmp_path)
    tampered = dict(store._envelope)
    tampered["task"] = {
        **store._envelope["task"],
        "acceptance": [
            {
                **manual_criterion(),
                "verifier_config": {"observation_scope": "manual:tampered"},
            }
        ],
    }
    store.envelope_path("RUN-1").write_text(json.dumps(tampered), encoding="utf-8")

    result = evaluate_run(store, "RUN-1", manual_evidence(), now=NOW)

    assert result["state"] == "open"
    assert result["reason"] == "envelope-unreadable"
    assert "envelope integrity mismatch" in result["detail"]
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


class TwoRunStore:
    def __init__(self, root: Path) -> None:
        self._runs: dict[str, dict[str, Any]] = {}
        self._paths: dict[str, Path] = {}
        for index in (1, 2):
            run_id = f"RUN-{index}"
            task_id = f"TASK-{index}"
            envelope = {
                "run_id": run_id,
                "task_id": task_id,
                "task": {"id": task_id, "acceptance": [manual_criterion()]},
            }
            run = {
                "run_id": run_id,
                "task_id": task_id,
                "state": "assigned",
                "task_sha256": TASK_SHA,
                "plan_sha256": PLAN_SHA,
                "envelope_sha256": legacy.sha256_json(envelope),
            }
            path = root / f"{run_id}.json"
            path.write_text(json.dumps(envelope), encoding="utf-8")
            self._runs[run_id] = run
            self._paths[run_id] = path

    def run(self, run_id: str) -> dict[str, Any]:
        return dict(self._runs[run_id])

    def list_runs(self) -> list[dict[str, Any]]:
        return [dict(self._runs[run_id]) for run_id in ("RUN-1", "RUN-2")]

    def envelope_path(self, run_id: str) -> Path:
        return self._paths[run_id]

    def receipt(self, run_id: str) -> dict[str, Any] | None:
        return None


def test_one_completion_conflict_does_not_abort_later_runs(tmp_path: Path) -> None:
    store = TwoRunStore(tmp_path)
    completed: list[str] = []

    def provider(run, envelope):
        return manual_evidence()

    def authenticate(run, envelope, evidence):
        return {"manual": {"kind": "test-authentication"}}

    def completion(registry, observed_store, run_id, evidence):
        if run_id == "RUN-1":
            raise RunStateConflict(
                "stale-baseline",
                "run baseline changed",
                run_id=run_id,
            )
        completed.append(run_id)
        return {"idempotent": False}

    result = reconcile_runs(
        "registry",
        store,
        provider,
        now=NOW,
        completion=completion,
        authentication_provider=authenticate,
    )

    assert result["observed_run_count"] == 2
    assert result["open_count"] == 1
    assert result["terminalized_count"] == 1
    assert result["observations"][0]["reason"] == "completion-conflict"
    assert result["observations"][0]["conflict"]["code"] == "stale-baseline"
    assert result["observations"][0]["mutated"] is False
    assert result["observations"][1]["state"] == "terminalized"
    assert completed == ["RUN-2"]


def test_adapter_unavailable_does_not_abort_later_runs(tmp_path: Path) -> None:
    store = TwoRunStore(tmp_path)
    completed: list[str] = []

    def provider(run, envelope):
        return manual_evidence()

    def authenticate(run, envelope, evidence):
        if run["run_id"] == "RUN-1":
            raise EvidenceAdapterUnavailable(
                authority="github",
                adapter="runtime_refresh.gh_json",
                target={"repository": "heimgewebe/test", "pull_request": 7},
                detail="TimeoutError: GitHub unavailable",
            )
        return {"manual": {"kind": "test-authentication"}}

    def completion(registry, observed_store, run_id, evidence):
        completed.append(run_id)
        return {"idempotent": False}

    result = reconcile_runs(
        "registry",
        store,
        provider,
        now=NOW,
        completion=completion,
        authentication_provider=authenticate,
    )

    assert result["observed_run_count"] == 2
    assert result["open_count"] == 1
    assert result["terminalized_count"] == 1
    blocked = result["observations"][0]
    assert blocked["reason"] == "evidence-adapter-unavailable"
    assert blocked["adapter"]["kind"] == "bureau.evidence_adapter_unavailable"
    assert blocked["adapter"]["authority"] == "github"
    assert blocked["adapter"]["adapter"] == "runtime_refresh.gh_json"
    assert blocked["mutated"] is False
    assert result["observations"][1]["state"] == "terminalized"
    assert completed == ["RUN-2"]


def test_authenticated_typed_pass_uses_real_post_evaluation_cas_writer(
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
        authentication_records={
            "manual": {
                "schema_version": 1,
                "kind": "bureau.acceptance_source_authentication",
                "authority": "manual",
                "authenticated": True,
            }
        },
    )

    assert result["state"] == "terminalized"
    assert result["completion"]["idempotent"] is False
    assert store.run(run["run_id"])["state"] == "succeeded"
    receipt = store.receipt(run["run_id"])
    assert receipt is not None
    assert result["evaluation"]["state"] == PASSED
    assert receipt["evidence"]["manual"]["kind"] == EVIDENCE_KIND
    assert receipt["evidence"]["manual"]["revision"]["task_sha256"] == run["task_sha256"]
    assert receipt["evidence"]["manual"]["_source_authentication"]["authenticated"] is True
    assert "_typed_acceptance" not in receipt["evidence"]


@pytest.mark.parametrize(
    ("verifier", "config", "authorities"),
    [
        (
            "deployment_complete",
            {"deployment_revision": "deploy-1"},
            ["grabowski", "target-runtime"],
        ),
        (
            "runtime_commit_contains",
            {"repository": "heimgewebe/test", "required_commit": "c" * 40},
            ["grabowski", "target-runtime"],
        ),
        (
            "live_probe_passed",
            {"runtime_revision": "runtime-1", "probe_id": "health"},
            ["grabowski", "target-runtime"],
        ),
        (
            "duration_soak_completed",
            {"required_seconds": 60, "observation_scope": "soak:test"},
            ["grabowski", "target-runtime", "manual"],
        ),
        ("no_effect_verified", {"scope_sha256": "a" * 64}, ["bureau", "grabowski"]),
        (
            "artifact_hash_matches",
            {"artifact_sha256": "b" * 64},
            ["artifact-store", "bureau", "grabowski"],
        ),
    ],
)
def test_unbound_declared_verifiers_report_explicit_adapter_block(
    verifier: str, config: dict[str, Any], authorities: list[str]
) -> None:
    criterion = {
        "id": "criterion",
        "assertion": "declared verifier requires a production adapter",
        "evidence_type": "object",
        "verifier": verifier,
        "verifier_config": config,
    }
    envelope = {"task": {"acceptance": [criterion]}}

    def github(argv):
        pytest.fail(f"GitHub adapter must not be used for {verifier}: {argv}")

    with pytest.raises(EvidenceAdapterUnavailable) as caught:
        authenticate_state_evidence(
            {},
            envelope,
            {"criterion": {}},
            github=github,
        )

    payload = caught.value.payload()
    assert payload["authority"] == "unbound"
    assert payload["adapter"] == f"missing:{verifier}"
    assert payload["target"] == {
        "criterion_id": "criterion",
        "verifier": verifier,
        "permitted_authorities": authorities,
    }
    assert payload["detail"] == (
        f"no production authentication adapter is registered for verifier {verifier}"
    )


def _manual_production_fixture(registry_factory, tmp_path: Path, monkeypatch):
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
    bundle_path = evidence_dir / f"{run['run_id']}.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    return registry, store, run, bundle, bundle_path


def test_state_root_manual_bundle_cannot_self_authenticate_production_writer(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    registry, store, run, bundle, _ = _manual_production_fixture(
        registry_factory, tmp_path, monkeypatch
    )
    bundle["evidence"]["manual"]["_source_authentication"] = {
        "authority": "manual",
        "reviewer": "producer-forgery",
        "authenticated": True,
    }
    evidence_path = store.state_root / "acceptance-evidence" / f"{run['run_id']}.json"
    evidence_path.write_text(json.dumps(bundle), encoding="utf-8")

    result = reconcile_state_evidence(registry, store, now=NOW)

    assert result["writer"] == "bureau-reconcile"
    assert result["terminalized_count"] == 0
    assert result["open_count"] == 1
    blocked = result["observations"][0]
    assert blocked["reason"] == "evidence-authentication-unavailable"
    assert blocked["adapter"]["authority"] == "manual"
    assert blocked["adapter"]["adapter"] == (
        "StateStore.events:manual-acceptance-source-authenticated"
    )
    assert blocked["adapter"]["target"] == {
        "run_id": run["run_id"],
        "task_id": run["task_id"],
        "task_sha256": run["task_sha256"],
        "plan_sha256": run["plan_sha256"],
        "envelope_sha256": run["envelope_sha256"],
        "criterion_id": "manual",
        "verifier": "manual_observation",
        "observation_scope": "manual:test",
        "evidence_sha256": legacy.sha256_json(bundle["evidence"]["manual"]),
        "authority": "manual",
    }
    assert blocked["evaluation"]["criteria"][0]["reason"] == (
        "evidence-source-unauthenticated"
    )
    assert store.run(run["run_id"])["state"] == "assigned"


def test_manual_authentication_event_is_revision_bound_and_idempotent(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    registry, store, run, bundle, _ = _manual_production_fixture(
        registry_factory, tmp_path, monkeypatch
    )
    evidence_sha256 = legacy.sha256_json(bundle["evidence"]["manual"])

    first = record_manual_acceptance_authentication(
        store,
        run["run_id"],
        "manual",
        expected_evidence_sha256=evidence_sha256,
        reviewer="  independent-reviewer  ",
    )
    repeated = record_manual_acceptance_authentication(
        store,
        run["run_id"],
        "manual",
        expected_evidence_sha256=evidence_sha256,
        reviewer="independent-reviewer",
    )

    assert first["idempotent"] is False
    assert repeated["idempotent"] is True
    assert repeated["authentication"] == first["authentication"]
    authentication = first["authentication"]
    assert authentication["kind"] == "bureau.acceptance_source_authentication"
    assert authentication["run_id"] == run["run_id"]
    assert authentication["task_id"] == run["task_id"]
    assert authentication["task_sha256"] == run["task_sha256"]
    assert authentication["plan_sha256"] == run["plan_sha256"]
    assert authentication["envelope_sha256"] == run["envelope_sha256"]
    assert authentication["criterion_id"] == "manual"
    assert authentication["verifier"] == "manual_observation"
    assert authentication["observation_scope"] == "manual:test"
    assert authentication["evidence_sha256"] == evidence_sha256
    assert authentication["reviewer"] == "independent-reviewer"
    assert authentication["observer"] == "independent-reviewer"
    assert authentication["authority"] == "manual"
    assert authentication["journal"]["event_type"] == (
        state_events.MANUAL_ACCEPTANCE_AUTHENTICATION_EVENT_TYPE
    )
    with store.connect() as connection:
        event_count = connection.execute(
            "SELECT COUNT(*) FROM events WHERE run_id=? AND event_type=?",
            (
                run["run_id"],
                state_events.MANUAL_ACCEPTANCE_AUTHENTICATION_EVENT_TYPE,
            ),
        ).fetchone()[0]
    assert event_count == 1

    result = reconcile_state_evidence(registry, store, now=NOW)

    assert result["terminalized_count"] == 1
    receipt = store.receipt(run["run_id"])
    assert receipt is not None
    assert receipt["evidence"]["manual"]["_source_authentication"] == authentication
    with pytest.raises(StateError, match="is not active for acceptance authentication"):
        record_manual_acceptance_authentication(
            store,
            run["run_id"],
            "manual",
            expected_evidence_sha256=evidence_sha256,
            reviewer="independent-reviewer",
        )


def test_acceptance_authenticate_cli_records_manual_journal_event(
    registry_factory, tmp_path: Path, monkeypatch, capsys
) -> None:
    registry, store, run, bundle, _ = _manual_production_fixture(
        registry_factory, tmp_path, monkeypatch
    )
    identity = {
        "registry": {"bureau_project": False},
        "manifest": {"canonical_registry": {}},
        "compatibility": {
            "status": "compatible",
            "mutation_allowed": True,
            "reason_codes": [],
        },
    }
    monkeypatch.setattr(
        bureau_cli, "bureau_runtime_identity", lambda *args, **kwargs: copy.deepcopy(identity)
    )

    exit_code = bureau_cli.main(
        [
            "--root",
            str(registry.root),
            "--state-root",
            str(store.state_root),
            "--json",
            "acceptance-authenticate",
            run["run_id"],
            "manual",
            "--expected-evidence-sha256",
            legacy.sha256_json(bundle["evidence"]["manual"]),
            "--reviewer",
            "cli-independent-reviewer",
        ]
    )

    assert exit_code == 0
    value = json.loads(capsys.readouterr().out)
    assert value["status"] == "authenticated"
    assert value["authentication"]["reviewer"] == "cli-independent-reviewer"
    assert value["authentication"]["criterion_id"] == "manual"
    assert value["runtime_identity"]["command_effect_scope"] == (
        "coordination_state_mutation"
    )


def test_changed_manual_evidence_digest_requires_a_new_attestation(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    registry, store, run, bundle, bundle_path = _manual_production_fixture(
        registry_factory, tmp_path, monkeypatch
    )
    original_sha256 = legacy.sha256_json(bundle["evidence"]["manual"])
    record_manual_acceptance_authentication(
        store,
        run["run_id"],
        "manual",
        expected_evidence_sha256=original_sha256,
        reviewer="independent-reviewer",
    )
    bundle["evidence"]["manual"]["facts"]["observation"] = "different current observation"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    changed_sha256 = legacy.sha256_json(bundle["evidence"]["manual"])
    assert changed_sha256 != original_sha256

    blocked = reconcile_state_evidence(registry, store, now=NOW)

    assert blocked["terminalized_count"] == 0
    observation = blocked["observations"][0]
    assert observation["reason"] == "evidence-authentication-unavailable"
    assert observation["adapter"]["target"]["evidence_sha256"] == changed_sha256
    assert observation["evaluation"]["criteria"][0]["reason"] == (
        "evidence-source-unauthenticated"
    )
    record_manual_acceptance_authentication(
        store,
        run["run_id"],
        "manual",
        expected_evidence_sha256=changed_sha256,
        reviewer="independent-reviewer",
    )
    with store.connect() as connection:
        event_count = connection.execute(
            "SELECT COUNT(*) FROM events WHERE run_id=? AND event_type=?",
            (
                run["run_id"],
                state_events.MANUAL_ACCEPTANCE_AUTHENTICATION_EVENT_TYPE,
            ),
        ).fetchone()[0]
    assert event_count == 2

    completed = reconcile_state_evidence(registry, store, now=NOW)

    assert completed["terminalized_count"] == 1


def test_manual_authentication_journal_bound_fails_closed(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    import bureau.closure_observer as observer

    registry, store, run, bundle, _ = _manual_production_fixture(
        registry_factory, tmp_path, monkeypatch
    )
    monkeypatch.setattr(observer, "MAX_MANUAL_AUTHENTICATION_EVENTS_PER_RUN", 2)
    evidence_sha256 = legacy.sha256_json(bundle["evidence"]["manual"])
    record_manual_acceptance_authentication(
        store,
        run["run_id"],
        "manual",
        expected_evidence_sha256=evidence_sha256,
        reviewer="reviewer-one",
    )
    record_manual_acceptance_authentication(
        store,
        run["run_id"],
        "manual",
        expected_evidence_sha256=evidence_sha256,
        reviewer="reviewer-two",
    )
    with pytest.raises(StateError, match="journal capacity reached"):
        record_manual_acceptance_authentication(
            store,
            run["run_id"],
            "manual",
            expected_evidence_sha256=evidence_sha256,
            reviewer="reviewer-three",
        )
    with store.immediate() as connection:
        connection.execute(
            "INSERT INTO events(run_id,event_type,event_schema_version,payload_json,created_at) "
            "SELECT run_id,event_type,event_schema_version,payload_json,created_at "
            "FROM events WHERE run_id=? AND event_type=? ORDER BY event_id DESC LIMIT 1",
            (run["run_id"], state_events.MANUAL_ACCEPTANCE_AUTHENTICATION_EVENT_TYPE),
        )
    result = reconcile_state_evidence(registry, store, now=NOW)
    assert result["terminalized_count"] == 0
    assert result["observations"][0]["reason"] == (
        "evidence-authentication-provider-unavailable"
    )
    assert "journal bound exceeded" in result["observations"][0]["detail"]


def test_authenticated_stale_manual_evidence_remains_open(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    registry, store, run, bundle, bundle_path = _manual_production_fixture(
        registry_factory, tmp_path, monkeypatch
    )
    bundle["evidence"]["manual"]["observed_at"] = "2026-08-08T19:59:00Z"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    record_manual_acceptance_authentication(
        store,
        run["run_id"],
        "manual",
        expected_evidence_sha256=legacy.sha256_json(bundle["evidence"]["manual"]),
        reviewer="independent-reviewer",
    )

    result = reconcile_state_evidence(registry, store, now=NOW)

    assert result["terminalized_count"] == 0
    assert result["open_count"] == 1
    criterion = result["observations"][0]["evaluation"]["criteria"][0]
    assert criterion["state"] == UNKNOWN
    assert criterion["reason"] == "evidence-stale"
    assert store.run(run["run_id"])["state"] == "assigned"


def test_manual_authentication_rejects_wrong_criterion_digest_scope_and_revision(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    registry, store, run, bundle, bundle_path = _manual_production_fixture(
        registry_factory, tmp_path, monkeypatch
    )
    item = bundle["evidence"]["manual"]
    digest = legacy.sha256_json(item)

    with pytest.raises(StateError, match="unknown acceptance criterion 'missing'"):
        record_manual_acceptance_authentication(
            store,
            run["run_id"],
            "missing",
            expected_evidence_sha256=digest,
            reviewer="reviewer",
        )
    with pytest.raises(StateError, match="manual evidence digest mismatch"):
        record_manual_acceptance_authentication(
            store,
            run["run_id"],
            "manual",
            expected_evidence_sha256="0" * 64,
            reviewer="reviewer",
        )
    with pytest.raises(StateError, match="reviewer must differ"):
        record_manual_acceptance_authentication(
            store,
            run["run_id"],
            "manual",
            expected_evidence_sha256=digest,
            reviewer="operator",
        )
    with pytest.raises(StateError, match="reviewer must differ"):
        record_manual_acceptance_authentication(
            store,
            run["run_id"],
            "manual",
            expected_evidence_sha256=digest,
            reviewer="  operator  ",
        )

    item["revision"]["observation_scope"] = "manual:wrong"
    item["facts"]["observation_scope"] = "manual:wrong"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    with pytest.raises(StateError, match="observation scope mismatch"):
        record_manual_acceptance_authentication(
            store,
            run["run_id"],
            "manual",
            expected_evidence_sha256=legacy.sha256_json(item),
            reviewer="reviewer",
        )

    item["revision"]["observation_scope"] = "manual:test"
    item["facts"]["observation_scope"] = "manual:test"
    item["revision"]["plan_sha256"] = "8" * 64
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    with pytest.raises(StateError, match="plan revision mismatch"):
        record_manual_acceptance_authentication(
            store,
            run["run_id"],
            "manual",
            expected_evidence_sha256=legacy.sha256_json(item),
            reviewer="reviewer",
        )

    item["revision"]["plan_sha256"] = run["plan_sha256"]
    item["revision"]["task_sha256"] = "9" * 64
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    with pytest.raises(StateError, match="task revision mismatch"):
        record_manual_acceptance_authentication(
            store,
            run["run_id"],
            "manual",
            expected_evidence_sha256=legacy.sha256_json(item),
            reviewer="reviewer",
        )
    result = reconcile_state_evidence(registry, store, now=NOW)
    assert result["terminalized_count"] == 0
    assert result["open_count"] == 1
    assert result["observations"][0]["reason"] == (
        "evidence-authentication-unavailable"
    )
    assert store.run(run["run_id"])["state"] == "assigned"

    envelope_path = store.envelope_path(run["run_id"])
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    envelope["worker_id"] = "tampered-worker"
    envelope_path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(StateError, match="run envelope integrity mismatch"):
        record_manual_acceptance_authentication(
            store,
            run["run_id"],
            "manual",
            expected_evidence_sha256=legacy.sha256_json(item),
            reviewer="reviewer",
        )


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
            "base_ref": "main",
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
                "base_ref": "main",
                "merge_commit_sha": merge_sha,
            },
            "facts": {
                "merged": True,
                "head_sha": head_sha,
                "base_ref": "main",
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


def test_manual_authentication_api_rejects_non_manual_verifier(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    _, store, run = _github_production_fixture(registry_factory, tmp_path, monkeypatch)
    evidence_path = store.state_root / "acceptance-evidence" / f"{run['run_id']}.json"
    bundle = json.loads(evidence_path.read_text(encoding="utf-8"))
    digest = legacy.sha256_json(bundle["evidence"]["merge"])

    with pytest.raises(StateError, match="verifier mismatch"):
        record_manual_acceptance_authentication(
            store,
            run["run_id"],
            "merge",
            expected_evidence_sha256=digest,
            reviewer="reviewer",
        )


PR1907_TASK_ID = (
    "GRABOWSKI-OPERATOR-SURFACE-V1-FU-OPTIONAL-CI-PLATFORM-DIAGNOSTICS-20260812"
)
PR1907_HEAD = "1000dc0af21f9ddeacd1823e264a5826c9a1ded6"
PR1907_REQUIRED_CHECKS = ["validate (3.10)", "validate (3.12)"]
PR1907_MANUAL_SCOPES = {
    "platform-inconsistency-classified": (
        "grabowski:pr-review-gate:pr737:optional-actions-platform-inconsistency"
    ),
    "no-silent-ignore": (
        "grabowski:pr-review-gate:pr737:optional-platform-warning-follow-up"
    ),
    "regression": (
        "grabowski:pr-review-gate:pr737-and-candidate-a7e5f751a584281dd7519b28"
    ),
    "delivery": (
        "grabowski:pr-review-gate:pr737-head-1000dc0af21f:review-tests-validation"
    ),
}


def corrected_pr1907_task() -> dict[str, Any]:
    fixture = Path(__file__).with_name("fixtures") / "pr1907-untyped-task.json"
    task = json.loads(fixture.read_text(encoding="utf-8"))
    for criterion in task["acceptance"]:
        criterion["evidence_type"] = "object"
        if criterion["id"] == "required-checks-stay-strict":
            criterion["verifier"] = "required_ci_green"
            criterion["verifier_config"] = {
                "repository": "heimgewebe/grabowski",
                "pull_request": 737,
                "head_sha": PR1907_HEAD,
                "base_ref": "main",
                "required_checks": list(PR1907_REQUIRED_CHECKS),
            }
        else:
            criterion["verifier"] = "manual_observation"
            criterion["verifier_config"] = {
                "observation_scope": PR1907_MANUAL_SCOPES[criterion["id"]]
            }
    return task


def _seed_pr1907_state_run(registry_factory, tmp_path: Path, monkeypatch):
    root = registry_factory(1)
    base_initiative = json.loads(
        (root / "registry/initiatives/main.json").read_text(encoding="utf-8")
    )
    initiative = {
        **base_initiative,
        "id": "GRABOWSKI-OPERATOR-SURFACE-V1",
        "title": "Grabowski operator surface",
    }
    (root / "registry/initiatives/pr1907.json").write_text(
        json.dumps(initiative), encoding="utf-8"
    )
    registry = Registry.load(root)
    state_root = tmp_path / "pr1907-production-state"
    monkeypatch.setenv("BUREAU_STATE_DIR", str(state_root))
    store = StateStore(state_root / "bureau.sqlite3")
    task = corrected_pr1907_task()
    store.put_task_spec(
        task,
        idempotency_key="test:pr1907:typed-revision-1",
        expected_revision=None,
        source="test-authoritative-state-store",
    )
    run_id = "BUR-RUN-20260812T120000Z-1907abc123"
    task_sha256 = task_revision_sha256(task)
    plan_sha256 = legacy.sha256_json({})
    created_at = "2026-08-10T19:58:00Z"
    envelope = {
        "schema_version": 1,
        "run_id": run_id,
        "task_id": task["id"],
        "worker_id": "pr1907-producer",
        "task_sha256": task_sha256,
        "plan_sha256": plan_sha256,
        "created_at": created_at,
        "task": task,
        "claims": copy.deepcopy(task["claims"]),
        "plan": {},
    }
    registry.schemas.validate("execution-envelope", envelope, f"test-run:{run_id}")
    envelope_sha256 = legacy.sha256_json(envelope)
    with store.immediate() as connection:
        connection.execute(
            "INSERT INTO workers(worker_id,kind,capabilities_json,heartbeat_at) "
            "VALUES(?,?,?,?)",
            (
                "pr1907-producer",
                "interactive-agent",
                legacy.canonical_json(task["required_capabilities"]),
                created_at,
            ),
        )
        connection.execute(
            "INSERT INTO runs(run_id,task_id,worker_id,attempt,state,task_sha256,"
            "plan_sha256,envelope_json,envelope_sha256,dispatch_request_id,created_at,"
            "updated_at,heartbeat_at) VALUES(?,?,?,1,'assigned',?,?,?,?,?,?,?,?)",
            (
                run_id,
                task["id"],
                "pr1907-producer",
                task_sha256,
                plan_sha256,
                legacy.canonical_json(envelope),
                envelope_sha256,
                f"{run_id}:dispatch-1",
                created_at,
                created_at,
                created_at,
            ),
        )
        connection.execute(
            "INSERT INTO reservations(run_id,resource_id,mode,amount,created_at) "
            "VALUES(?,?,?,?,?)",
            (run_id, "repo.grabowski", "write", 1, created_at),
        )
        store.event(connection, "run-claimed", {"task_id": task["id"]}, run_id)
    legacy.atomic_write(
        store.envelope_path(run_id),
        json.dumps(envelope, indent=2, ensure_ascii=False) + "\n",
    )
    run = store.run(run_id)
    return registry, store, run, task


def _pr1907_evidence(run: Mapping[str, Any], task: Mapping[str, Any]) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "required-checks-stay-strict": {
            "schema_version": 1,
            "kind": EVIDENCE_KIND,
            "criterion_id": "required-checks-stay-strict",
            "evidence_type": "required_ci_green",
            "source": {
                "authority": "github",
                "reference": f"github-pr:heimgewebe/grabowski#737@{PR1907_HEAD}",
            },
            "observed_at": "2026-08-10T19:59:00Z",
            "revision": {
                "task_sha256": run["task_sha256"],
                "plan_sha256": run["plan_sha256"],
                "head_sha": PR1907_HEAD,
                "base_ref": "main",
            },
            "facts": {
                "complete": True,
                "head_sha": PR1907_HEAD,
                "base_ref": "main",
                "required_checks": list(PR1907_REQUIRED_CHECKS),
                "checks": [
                    {"name": name, "state": "success"}
                    for name in PR1907_REQUIRED_CHECKS
                ],
            },
        }
    }
    criteria = {
        criterion["id"]: criterion for criterion in task["acceptance"]
    }
    for criterion_id, observation_scope in PR1907_MANUAL_SCOPES.items():
        assert criteria[criterion_id]["verifier_config"] == {
            "observation_scope": observation_scope
        }
        evidence[criterion_id] = {
            "schema_version": 1,
            "kind": EVIDENCE_KIND,
            "criterion_id": criterion_id,
            "evidence_type": "manual_observation",
            "source": {
                "authority": "manual",
                "reference": f"producer-observation:{criterion_id}",
            },
            "observed_at": "2026-08-10T19:59:00Z",
            "revision": {
                "task_sha256": run["task_sha256"],
                "plan_sha256": run["plan_sha256"],
                "observation_scope": observation_scope,
            },
            "facts": {
                "accepted": True,
                "observer": "pr1907-producer",
                "observation": f"independently reviewable observation for {criterion_id}",
                "observation_scope": observation_scope,
            },
        }
    return evidence


def test_pr1907_corrected_task_preserves_authoritative_content_and_exact_check_set() -> None:
    fixture = Path(__file__).with_name("fixtures") / "pr1907-untyped-task.json"
    untyped = json.loads(fixture.read_text(encoding="utf-8"))
    typed = corrected_pr1907_task()

    assert legacy.sha256_json(untyped) == (
        "42667ab0105e1aea61b834d192e368016f3faa31a3ff9f366ba0d0da37884946"
    )
    assert {key: value for key, value in typed.items() if key != "acceptance"} == {
        key: value for key, value in untyped.items() if key != "acceptance"
    }
    assert [item["id"] for item in typed["acceptance"]] == [
        "required-checks-stay-strict",
        "platform-inconsistency-classified",
        "no-silent-ignore",
        "regression",
        "delivery",
    ]
    required = typed["acceptance"][0]
    assert required["verifier"] == "required_ci_green"
    assert required["verifier_config"] == {
        "repository": "heimgewebe/grabowski",
        "pull_request": 737,
        "head_sha": PR1907_HEAD,
        "base_ref": "main",
        "required_checks": PR1907_REQUIRED_CHECKS,
    }
    assert "Analyze (actions)" not in required["verifier_config"]["required_checks"]
    manual_scopes = [
        item["verifier_config"]["observation_scope"]
        for item in typed["acceptance"][1:]
    ]
    assert len(set(manual_scopes)) == 4


def test_canonical_pr1907_registry_task_matches_corrected_contract() -> None:
    task_path = (
        Path(__file__).parents[1]
        / "registry"
        / "tasks"
        / f"{PR1907_TASK_ID}.json"
    )
    canonical = json.loads(task_path.read_text(encoding="utf-8"))

    assert canonical == corrected_pr1907_task()


def test_pr1907_real_production_reconcile_requires_four_manual_attestations(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    registry, store, run, task = _seed_pr1907_state_run(
        registry_factory, tmp_path, monkeypatch
    )
    evidence = _pr1907_evidence(run, task)
    evidence_dir = store.state_root / "acceptance-evidence"
    evidence_dir.mkdir()
    evidence_path = evidence_dir / f"{run['run_id']}.json"
    evidence_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": EVIDENCE_BUNDLE_KIND,
                "run_id": run["run_id"],
                "task_id": run["task_id"],
                "task_sha256": run["task_sha256"],
                "plan_sha256": run["plan_sha256"],
                "evidence": evidence,
            }
        ),
        encoding="utf-8",
    )
    github_calls: list[list[str]] = []

    def github(argv):
        github_calls.append(argv)
        return {
            "number": 737,
            "state": "OPEN",
            "isDraft": False,
            "mergedAt": None,
            "mergeCommit": None,
            "headRefOid": PR1907_HEAD,
            "baseRefName": "main",
            "statusCheckRollup": [
                {"name": "validate (3.10)", "conclusion": "SUCCESS"},
                {"name": "validate (3.12)", "conclusion": "SUCCESS"},
                {"name": "Analyze (actions)", "conclusion": "FAILURE"},
            ],
            "url": "https://github.com/heimgewebe/grabowski/pull/737",
        }

    producer_only = reconcile_state_evidence(registry, store, now=NOW, github=github)

    assert producer_only["terminalized_count"] == 0
    assert store.run(run["run_id"])["state"] == "assigned"
    observation = producer_only["observations"][0]
    assert observation["reason"] == "evidence-authentication-unavailable"
    assert len(observation["authentication_unavailable"]) == 4
    states = {
        item["criterion_id"]: (item["state"], item["reason"])
        for item in observation["evaluation"]["criteria"]
    }
    assert states["required-checks-stay-strict"] == (
        "passed",
        "required-checks-green",
    )
    assert all(
        states[criterion_id] == ("unknown", "evidence-source-unauthenticated")
        for criterion_id in PR1907_MANUAL_SCOPES
    )
    assert all(reason != "criterion-is-not-typed" for _, reason in states.values())

    for criterion_id in PR1907_MANUAL_SCOPES:
        record_manual_acceptance_authentication(
            store,
            run["run_id"],
            criterion_id,
            expected_evidence_sha256=legacy.sha256_json(evidence[criterion_id]),
            reviewer="pr1907-independent-reviewer",
        )

    completed = reconcile_state_evidence(registry, store, now=NOW, github=github)

    assert completed["terminalized_count"] == 1
    assert completed["observations"][0]["state"] == "terminalized"
    assert completed["observations"][0]["evaluation"]["state"] == PASSED
    completed_criteria = completed["observations"][0]["evaluation"]["criteria"]
    assert len(completed_criteria) == 5
    assert all(item["state"] == PASSED for item in completed_criteria)
    assert all(item["reason"] != "criterion-is-not-typed" for item in completed_criteria)
    assert store.run(run["run_id"])["state"] == "succeeded"
    receipt = store.receipt(run["run_id"])
    assert receipt is not None
    for criterion_id, scope in PR1907_MANUAL_SCOPES.items():
        authentication = receipt["evidence"][criterion_id]["_source_authentication"]
        assert authentication["authority"] == "manual"
        assert authentication["reviewer"] == "pr1907-independent-reviewer"
        assert authentication["observation_scope"] == scope
        assert authentication["evidence_sha256"] == legacy.sha256_json(
            evidence[criterion_id]
        )
        assert authentication["journal"]["event_type"] == (
            state_events.MANUAL_ACCEPTANCE_AUTHENTICATION_EVENT_TYPE
        )
    required_config = task["acceptance"][0]["verifier_config"]
    assert required_config["required_checks"] == PR1907_REQUIRED_CHECKS
    assert "Analyze (actions)" not in required_config["required_checks"]
    assert len(github_calls) == 2


def test_state_root_github_bundle_terminalizes_only_after_live_authentication(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    registry, store, run = _github_production_fixture(registry_factory, tmp_path, monkeypatch)
    calls: list[list[str]] = []

    def github(argv):
        calls.append(argv)
        return merged_pr_detail()

    evidence_path = store.state_root / "acceptance-evidence" / f"{run['run_id']}.json"
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
        "base_ref": "main",
    }
    assert len(authentication["live_observation_sha256"]) == 64
    assert result["evidence_retirement"]["after"]["retired_count"] == 1
    assert result["evidence_retirement"]["retired_count"] == 1
    assert not evidence_path.exists()

    # Simulate a historical producer bundle left behind by an older runtime.
    residue = {
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
    evidence_path.write_text(json.dumps(residue), encoding="utf-8")

    repeat = reconcile_state_evidence(registry, store, now=NOW, github=github)

    assert repeat["observed_run_count"] == 0
    assert repeat["evidence_retirement"]["before"]["retired_count"] == 1
    assert repeat["evidence_retirement"]["retired_count"] == 1
    assert not evidence_path.exists()


def test_state_root_unmerged_github_bundle_authenticates_known_failure(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    registry, store, run = _github_production_fixture(registry_factory, tmp_path, monkeypatch)
    evidence_path = store.state_root / "acceptance-evidence" / f"{run['run_id']}.json"
    bundle = json.loads(evidence_path.read_text(encoding="utf-8"))
    merge = bundle["evidence"]["merge"]
    merge["revision"]["merge_commit_sha"] = None
    merge["facts"]["merged"] = False
    merge["facts"]["merge_commit_sha"] = None
    evidence_path.write_text(json.dumps(bundle), encoding="utf-8")

    def github(argv):
        return {
            **merged_pr_detail(),
            "state": "OPEN",
            "mergedAt": None,
            "mergeCommit": None,
        }

    result = reconcile_state_evidence(registry, store, now=NOW, github=github)

    assert result["terminalized_count"] == 0
    assert result["open_count"] == 1
    evaluation = result["observations"][0]["evaluation"]
    assert evaluation["state"] == "failed"
    assert evaluation["criteria"][0]["state"] == "failed"
    assert evaluation["criteria"][0]["reason"] == "merge-observed-not-complete"
    assert evidence_path.exists()
    assert store.run(run["run_id"])["state"] == "assigned"


def _write_terminal_residue(store: StateStore, run: dict[str, Any]) -> Path:
    evidence_path = store.state_root / "acceptance-evidence" / f"{run['run_id']}.json"
    evidence_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": EVIDENCE_BUNDLE_KIND,
                "run_id": run["run_id"],
                "task_id": run["task_id"],
                "task_sha256": run["task_sha256"],
                "plan_sha256": run["plan_sha256"],
                "evidence": github_merge_evidence(
                    task_sha256=run["task_sha256"],
                    plan_sha256=run["plan_sha256"],
                ),
            }
        ),
        encoding="utf-8",
    )
    return evidence_path


def test_terminal_evidence_retirement_requires_receipt_run_binding(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    registry, store, run = _github_production_fixture(registry_factory, tmp_path, monkeypatch)

    result = reconcile_state_evidence(
        registry, store, now=NOW, github=lambda argv: merged_pr_detail()
    )
    assert result["terminalized_count"] == 1

    receipt = store.receipt(run["run_id"])
    assert receipt is not None
    receipt["envelope_sha256"] = "9" * 64
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    receipt_sha256 = legacy.sha256_json(unsigned)
    receipt["receipt_sha256"] = receipt_sha256
    with store.immediate() as connection:
        connection.execute(
            "UPDATE receipts SET receipt_json=?,receipt_sha256=? WHERE run_id=?",
            (json.dumps(receipt), receipt_sha256, run["run_id"]),
        )

    evidence_path = _write_terminal_residue(store, run)
    repeat = reconcile_state_evidence(
        registry, store, now=NOW, github=lambda argv: merged_pr_detail()
    )

    assert repeat["observed_run_count"] == 0
    assert repeat["evidence_retirement"]["retired_count"] == 0
    assert evidence_path.exists()
    assert any(
        "stored receipt binding mismatch: envelope_sha256" in error
        for error in repeat["evidence_retirement"]["before"]["errors"]
    )


def test_terminal_evidence_retirement_requires_valid_receipt_digest(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    registry, store, run = _github_production_fixture(registry_factory, tmp_path, monkeypatch)

    result = reconcile_state_evidence(
        registry, store, now=NOW, github=lambda argv: merged_pr_detail()
    )
    assert result["terminalized_count"] == 1

    with store.immediate() as connection:
        connection.execute(
            "UPDATE receipts SET receipt_sha256=? WHERE run_id=?",
            ("0" * 64, run["run_id"]),
        )

    evidence_path = _write_terminal_residue(store, run)
    repeat = reconcile_state_evidence(
        registry, store, now=NOW, github=lambda argv: merged_pr_detail()
    )

    assert repeat["observed_run_count"] == 0
    assert repeat["evidence_retirement"]["retired_count"] == 0
    assert evidence_path.exists()
    assert any(
        "stored receipt integrity mismatch" in error
        for error in repeat["evidence_retirement"]["before"]["errors"]
    )


@pytest.mark.parametrize("terminal_state", ["failed", "cancelled", "orphaned"])
def test_non_success_terminal_run_retires_bound_producer_bundle(
    registry_factory, tmp_path: Path, monkeypatch, terminal_state: str
) -> None:
    registry, store, run = _github_production_fixture(registry_factory, tmp_path, monkeypatch)
    evidence_path = store.state_root / "acceptance-evidence" / f"{run['run_id']}.json"

    fail_run(store, run["run_id"], f"terminalized as {terminal_state}", terminal_state)
    assert store.run(run["run_id"])["state"] == terminal_state
    assert store.receipt(run["run_id"]) is None
    assert evidence_path.exists()

    result = reconcile_state_evidence(
        registry, store, now=NOW, github=lambda argv: merged_pr_detail()
    )

    assert result["observed_run_count"] == 0
    assert result["terminalized_count"] == 0
    assert result["evidence_retirement"]["before"]["retired_count"] == 1
    assert result["evidence_retirement"]["retired_count"] == 1
    assert not evidence_path.exists()


def test_unknown_run_json_residue_is_quarantined_once_with_digest_evidence(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    registry, store, _ = _github_production_fixture(registry_factory, tmp_path, monkeypatch)
    evidence_dir = store.state_root / "acceptance-evidence"
    residue = evidence_dir / "BUR-RUN-20990101T000000Z-deadbeef-user-closeout.json"
    payload = {
        "queue-reconciliation-1": {"accepted": True},
        "queue-reconciliation-2": {"accepted": True},
    }
    raw = (json.dumps(payload, sort_keys=True) + "\n").encode()
    residue.write_bytes(raw)

    result = reconcile_state_evidence(
        registry, store, now=NOW, github=lambda argv: merged_pr_detail()
    )

    retirement = result["evidence_retirement"]
    assert retirement["quarantined_count"] == 1
    record = retirement["before"]["quarantined_entries"][0]
    assert record["reason"] == "unknown-run"
    assert record["classification"] == "unknown-run-json-residue"
    assert record["source_name"] == residue.name
    assert record["source_sha256"] == hashlib.sha256(raw).hexdigest()
    quarantine = Path(record["quarantine_path"])
    quarantine_root = Path(retirement["before"]["quarantine_directory"])
    assert quarantine.read_bytes() == raw
    assert quarantine.stat().st_mode & 0o777 == 0o600
    assert quarantine_root == closure_observer._evidence_quarantine_path(store)
    assert quarantine.parent.parent == quarantine_root
    assert quarantine_root.stat().st_mode & 0o777 == 0o700
    binding_path = closure_observer._evidence_quarantine_binding_path(store)
    assert binding_path.is_file()
    assert binding_path.stat().st_mode & 0o777 == 0o600
    assert binding_path.parent == store.state_root / "receipts"
    assert quarantine_root.parent == store.state_root / "receipts"
    assert quarantine_root.is_relative_to(store.state_root)
    assert quarantine_root.is_relative_to(store.state_root / "receipts")
    assert not residue.exists()
    hygiene = state_root_hygiene(store.state_root, store.path)
    assert hygiene["healthy"] is True
    assert hygiene["unknown_entries"] == []

    repeat = reconcile_state_evidence(
        registry, store, now=NOW, github=lambda argv: merged_pr_detail()
    )

    assert repeat["evidence_retirement"]["quarantined_count"] == 0
    assert quarantine.read_bytes() == raw
    assert not any(
        "deadbeef-user-closeout" in error
        for error in repeat["evidence_retirement"]["before"]["errors"]
    )


def test_unknown_run_quarantine_move_failure_keeps_source_and_no_partial_destination(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    registry, store, _ = _github_production_fixture(registry_factory, tmp_path, monkeypatch)
    evidence_dir = store.state_root / "acceptance-evidence"
    residue = evidence_dir / "BUR-RUN-20990101T000000Z-movefail00.json"
    raw = json.dumps({"accepted": True}).encode()
    residue.write_bytes(raw)

    def fail_replace(source, destination):
        raise OSError("simulated atomic move failure")

    monkeypatch.setattr(closure_observer, "_atomic_quarantine_move", fail_replace)
    result = reconcile_state_evidence(
        registry, store, now=NOW, github=lambda argv: merged_pr_detail()
    )

    assert result["evidence_retirement"]["quarantined_count"] == 0
    assert residue.read_bytes() == raw
    quarantine_root = closure_observer._evidence_quarantine_path(store)
    assert list(quarantine_root.iterdir()) == []
    assert any(
        "simulated atomic move failure" in error
        for error in result["evidence_retirement"]["before"]["errors"]
    )


def test_unknown_run_binding_receipt_survives_pre_root_crash_window(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    registry, store, _ = _github_production_fixture(registry_factory, tmp_path, monkeypatch)
    evidence_dir = store.state_root / "acceptance-evidence"
    residue = evidence_dir / "BUR-RUN-20990101T000000Z-pre-root000.json"
    raw = json.dumps({"accepted": True}).encode()
    residue.write_bytes(raw)
    quarantine_root = closure_observer._evidence_quarantine_path(store)
    binding_path = closure_observer._evidence_quarantine_binding_path(store)
    binding_path.write_text(
        json.dumps(closure_observer._evidence_quarantine_binding(store)),
        encoding="utf-8",
    )
    binding_path.chmod(0o600)
    assert not quarantine_root.exists()

    result = reconcile_state_evidence(
        registry, store, now=NOW, github=lambda argv: merged_pr_detail()
    )

    assert result["evidence_retirement"]["quarantined_count"] == 1
    assert quarantine_root.is_dir()
    record = result["evidence_retirement"]["before"]["quarantined_entries"][0]
    assert Path(record["quarantine_path"]).read_bytes() == raw
    assert not residue.exists()


def test_unknown_run_quarantine_rejects_unbound_private_path_collision(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    registry, store, _ = _github_production_fixture(registry_factory, tmp_path, monkeypatch)
    evidence_dir = store.state_root / "acceptance-evidence"
    residue = evidence_dir / "BUR-RUN-20990101T000000Z-binding0000.json"
    raw = json.dumps({"accepted": True}).encode()
    residue.write_bytes(raw)
    quarantine_root = closure_observer._evidence_quarantine_path(store)
    quarantine_root.mkdir(mode=0o700)
    (quarantine_root / "foreign.json").write_text("{}", encoding="utf-8")

    result = reconcile_state_evidence(
        registry, store, now=NOW, github=lambda argv: merged_pr_detail()
    )

    assert result["evidence_retirement"]["quarantined_count"] == 0
    assert residue.read_bytes() == raw
    assert (quarantine_root / "foreign.json").read_text(encoding="utf-8") == "{}"
    assert any(
        "acceptance evidence quarantine binding is missing" in error
        or "unbound acceptance evidence quarantine root is not empty" in error
        for error in result["evidence_retirement"]["before"]["errors"]
    )


def test_unknown_run_manifestless_pre_move_transaction_is_cleaned(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    registry, store, _ = _github_production_fixture(registry_factory, tmp_path, monkeypatch)
    evidence_dir = store.state_root / "acceptance-evidence"
    residue = evidence_dir / "BUR-RUN-20990101T000000Z-premanifest.json"
    raw = json.dumps({"accepted": True}).encode()
    residue.write_bytes(raw)
    quarantine_root = closure_observer._evidence_quarantine_directory(store)
    assert quarantine_root is not None
    orphan = quarantine_root / ".pending-orphan-pre-manifest"
    orphan.mkdir(mode=0o700)

    result = reconcile_state_evidence(
        registry, store, now=NOW, github=lambda argv: merged_pr_detail()
    )

    retirement = result["evidence_retirement"]
    assert retirement["before"]["abandoned_pre_move_count"] == 1
    assert not orphan.exists()
    assert retirement["quarantined_count"] == 1
    record = retirement["before"]["quarantined_entries"][0]
    assert Path(record["quarantine_path"]).read_bytes() == raw
    assert not residue.exists()


def test_unknown_run_temp_manifest_pre_move_transaction_is_cleaned(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    registry, store, _ = _github_production_fixture(registry_factory, tmp_path, monkeypatch)
    evidence_dir = store.state_root / "acceptance-evidence"
    residue = evidence_dir / "BUR-RUN-20990101T000000Z-tempmanifest.json"
    raw = json.dumps({"accepted": True}).encode()
    residue.write_bytes(raw)
    quarantine_root = closure_observer._evidence_quarantine_directory(store)
    assert quarantine_root is not None
    orphan = quarantine_root / ".pending-orphan-temp-manifest"
    orphan.mkdir(mode=0o700)
    temporary_manifest = orphan / ".manifest.tmp"
    temporary_manifest.write_text("{}\n", encoding="utf-8")
    temporary_manifest.chmod(0o600)

    result = reconcile_state_evidence(
        registry, store, now=NOW, github=lambda argv: merged_pr_detail()
    )

    retirement = result["evidence_retirement"]
    assert retirement["before"]["abandoned_pre_move_count"] == 1
    assert not orphan.exists()
    assert retirement["quarantined_count"] == 1
    assert not residue.exists()


def test_unknown_run_fifo_is_rejected_without_blocking_reconcile(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    registry, store, _ = _github_production_fixture(registry_factory, tmp_path, monkeypatch)
    evidence_dir = store.state_root / "acceptance-evidence"
    residue = evidence_dir / "BUR-RUN-20990101T000000Z-fifo000000.json"
    closure_observer.os.mkfifo(residue, 0o600)

    result = reconcile_state_evidence(
        registry, store, now=NOW, github=lambda argv: merged_pr_detail()
    )

    retirement = result["evidence_retirement"]
    assert retirement["quarantined_count"] == 0
    assert residue.exists()
    assert any(
        "must be a regular non-symlink file" in error
        for phase in ("before", "after")
        for error in retirement[phase]["errors"]
    )


def test_unknown_run_interrupted_atomic_preservation_publish_recovers(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    _, store, _ = _github_production_fixture(registry_factory, tmp_path, monkeypatch)
    evidence_dir = store.state_root / "acceptance-evidence"
    residue = evidence_dir / "BUR-RUN-20990101T000000Z-preservecrash.json"
    malformed = b"not-json"
    residue.write_bytes(malformed)
    root = closure_observer._evidence_quarantine_directory(store)
    assert root is not None
    source_stat = residue.stat()
    transaction, _ = closure_observer._create_quarantine_transaction(
        store,
        root,
        residue,
        residue.stem,
        hashlib.sha256(malformed).hexdigest(),
        (source_stat.st_dev, source_stat.st_ino, source_stat.st_nlink),
    )
    staged = transaction / "payload.json"
    closure_observer.os.replace(residue, staged)
    real_replace = closure_observer.os.replace

    def crash_before_preservation_publish(source, destination):
        if Path(destination) == transaction / "preservation.json":
            raise SystemExit("simulated crash before preservation publish")
        return real_replace(source, destination)

    monkeypatch.setattr(
        closure_observer.os, "replace", crash_before_preservation_publish
    )
    with pytest.raises(SystemExit, match="simulated crash before preservation publish"):
        closure_observer._recover_quarantine_transactions(store)

    assert transaction.is_dir()
    assert not (transaction / "preservation.json").exists()
    assert list(transaction.glob(".preservation-*.tmp"))

    monkeypatch.setattr(closure_observer.os, "replace", real_replace)
    recovered = closure_observer._recover_quarantine_transactions(store)

    assert recovered["recovered_count"] == 1
    assert recovered["errors"] == []
    preserved = [item for item in root.iterdir() if item.name.startswith("preserved-")]
    assert len(preserved) == 1
    assert (preserved[0] / "payload.json").read_bytes() == malformed
    receipt = json.loads((preserved[0] / "preservation.json").read_text(encoding="utf-8"))
    assert receipt["kind"] == "bureau.acceptance_evidence_quarantine_preservation"
    assert not list(preserved[0].glob(".preservation-*.tmp"))
    assert not any(item.name.startswith(".pending-") for item in root.iterdir())


def test_unknown_run_partial_preservation_receipt_is_replaced_and_finalized(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    _, store, _ = _github_production_fixture(registry_factory, tmp_path, monkeypatch)
    evidence_dir = store.state_root / "acceptance-evidence"
    residue = evidence_dir / "BUR-RUN-20990101T000000Z-partialreceipt.json"
    malformed = b"not-json"
    residue.write_bytes(malformed)
    root = closure_observer._evidence_quarantine_directory(store)
    assert root is not None
    source_stat = residue.stat()
    transaction, _ = closure_observer._create_quarantine_transaction(
        store,
        root,
        residue,
        residue.stem,
        hashlib.sha256(malformed).hexdigest(),
        (source_stat.st_dev, source_stat.st_ino, source_stat.st_nlink),
    )
    closure_observer.os.replace(residue, transaction / "payload.json")
    preservation_path = transaction / "preservation.json"
    preservation_path.write_bytes(b'{"schema_version":1')
    preservation_path.chmod(0o600)

    recovered = closure_observer._recover_quarantine_transactions(store)

    assert recovered["recovered_count"] == 1
    assert recovered["errors"] == []
    preserved = [item for item in root.iterdir() if item.name.startswith("preserved-")]
    assert len(preserved) == 1
    receipt = json.loads((preserved[0] / "preservation.json").read_text(encoding="utf-8"))
    assert receipt["kind"] == "bureau.acceptance_evidence_quarantine_preservation"
    assert "interrupted preservation receipt recovered" in receipt["reason"]
    assert (preserved[0] / "payload.json").read_bytes() == malformed


def test_unknown_run_manifest_only_transaction_is_retired_without_source(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    _, store, _ = _github_production_fixture(registry_factory, tmp_path, monkeypatch)
    evidence_dir = store.state_root / "acceptance-evidence"
    residue = evidence_dir / "BUR-RUN-20990101T000000Z-manifestonly.json"
    raw = json.dumps({"accepted": True}).encode()
    residue.write_bytes(raw)
    root = closure_observer._evidence_quarantine_directory(store)
    assert root is not None
    source_stat = residue.stat()
    transaction, _ = closure_observer._create_quarantine_transaction(
        store,
        root,
        residue,
        residue.stem,
        hashlib.sha256(raw).hexdigest(),
        (source_stat.st_dev, source_stat.st_ino, source_stat.st_nlink),
    )
    residue.unlink()

    recovered = closure_observer._recover_quarantine_transactions(store)

    assert recovered["abandoned_pre_move_count"] == 1
    assert recovered["recovered_count"] == 0
    assert recovered["errors"] == []
    assert not transaction.exists()
    assert not any(item.name.startswith(".pending-") for item in root.iterdir())


def test_unknown_run_interrupted_staging_is_recovered_on_next_reconcile(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    registry, store, _ = _github_production_fixture(registry_factory, tmp_path, monkeypatch)
    evidence_dir = store.state_root / "acceptance-evidence"
    residue = evidence_dir / "BUR-RUN-20990101T000000Z-crash00000.json"
    raw = json.dumps({"accepted": True, "generation": 1}).encode()
    residue.write_bytes(raw)
    real_finalize = closure_observer._finalize_quarantine_transaction

    def simulate_process_loss(
        store_arg, root, transaction, source_path, run_id, manifest
    ):
        raise SystemExit("simulated crash after durable move")

    monkeypatch.setattr(
        closure_observer, "_finalize_quarantine_transaction", simulate_process_loss
    )
    with pytest.raises(SystemExit, match="simulated crash after durable move"):
        reconcile_state_evidence(
            registry, store, now=NOW, github=lambda argv: merged_pr_detail()
        )

    quarantine_root = closure_observer._evidence_quarantine_path(store)
    pending = [
        item for item in quarantine_root.iterdir() if item.name.startswith(".pending-")
    ]
    assert len(pending) == 1
    assert not residue.exists()
    assert (pending[0] / "manifest.json").is_file()
    assert (pending[0] / "payload.json").read_bytes() == raw
    # The quarantine lives under receipts, so loss of the producer-directory
    # pathname cannot strand a durable moved payload. Preserve unrelated fixture
    # evidence by moving the whole directory outside the StateRoot.
    detached_evidence = tmp_path / "detached-acceptance-evidence"
    evidence_dir.rename(detached_evidence)
    assert not evidence_dir.exists()
    assert list(detached_evidence.glob("*.json"))

    monkeypatch.setattr(
        closure_observer, "_finalize_quarantine_transaction", real_finalize
    )
    recovered = reconcile_state_evidence(
        registry, store, now=NOW, github=lambda argv: merged_pr_detail()
    )

    retirement = recovered["evidence_retirement"]
    assert retirement["before"]["recovered_quarantine_count"] == 1
    recovery_record = retirement["before"]["recovered_quarantine_entries"][0]
    assert recovery_record["reason"] == "interrupted-quarantine-recovered"
    assert recovery_record["source_name"] == residue.name
    assert Path(recovery_record["quarantine_path"]).read_bytes() == raw
    assert not any(
        item.name.startswith(".pending-") for item in quarantine_root.iterdir()
    )
    assert retirement["quarantined_count"] == 0


def test_unknown_run_recovery_rebinds_same_digest_different_inode(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    registry, store, _ = _github_production_fixture(registry_factory, tmp_path, monkeypatch)
    evidence_dir = store.state_root / "acceptance-evidence"
    residue = evidence_dir / "BUR-RUN-20990101T000000Z-samebytes00.json"
    raw = json.dumps({"generation": 1}).encode()
    residue.write_bytes(raw)
    source_sha256 = hashlib.sha256(raw).hexdigest()
    source_stat = residue.stat()
    root = closure_observer._evidence_quarantine_directory(store)
    assert root is not None
    transaction, _ = closure_observer._create_quarantine_transaction(
        store,
        root,
        residue,
        residue.stem,
        source_sha256,
        (source_stat.st_dev, source_stat.st_ino, source_stat.st_nlink),
    )
    staged = transaction / "payload.json"
    staged.write_bytes(raw)
    staged_stat = staged.stat()
    assert staged_stat.st_ino != source_stat.st_ino
    residue.unlink()

    recovered = reconcile_state_evidence(
        registry, store, now=NOW, github=lambda argv: merged_pr_detail()
    )

    retirement = recovered["evidence_retirement"]["before"]
    assert retirement["recovered_quarantine_count"] == 1
    record = retirement["recovered_quarantine_entries"][0]
    assert record["reason"] == "mismatched-quarantine-payload-recovered"
    payload = Path(record["quarantine_path"])
    assert payload.read_bytes() == raw
    final_manifest = json.loads((payload.parent / "manifest.json").read_text(encoding="utf-8"))
    assert final_manifest["source_sha256"] == source_sha256
    assert final_manifest["source_device"] == payload.stat().st_dev
    assert final_manifest["source_inode"] == payload.stat().st_ino
    assert not any(item.name.startswith(".pending-") for item in root.iterdir())


def test_unknown_run_atomic_replacement_is_restored_instead_of_unlinked(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    registry, store, _ = _github_production_fixture(registry_factory, tmp_path, monkeypatch)
    evidence_dir = store.state_root / "acceptance-evidence"
    residue = evidence_dir / "BUR-RUN-20990101T000000Z-race000000.json"
    original = json.dumps({"generation": 1}).encode()
    replacement = json.dumps({"generation": 2}).encode()
    residue.write_bytes(original)
    real_move = closure_observer._atomic_quarantine_move
    raced = False

    def replace_after_producer_race(source, destination):
        nonlocal raced
        if Path(source) == residue and not raced:
            raced = True
            producer = residue.with_name(residue.name + ".producer")
            producer.write_bytes(replacement)
            closure_observer.os.replace(producer, residue)
        return real_move(source, destination)

    monkeypatch.setattr(
        closure_observer, "_atomic_quarantine_move", replace_after_producer_race
    )
    result = reconcile_state_evidence(
        registry, store, now=NOW, github=lambda argv: merged_pr_detail()
    )

    assert raced is True
    retirement = result["evidence_retirement"]
    assert retirement["before"]["quarantined_count"] == 0
    assert any(
        "changed during quarantine (restored)" in error
        for error in retirement["before"]["errors"]
    )
    # The after-phase sees the safely restored replacement as a stable unknown
    # run and can then quarantine that exact replacement inode normally.
    assert retirement["after"]["quarantined_count"] == 1
    assert retirement["quarantined_count"] == 1
    record = retirement["after"]["quarantined_entries"][0]
    assert Path(record["quarantine_path"]).read_bytes() == replacement
    assert record["source_sha256"] == hashlib.sha256(replacement).hexdigest()
    assert not residue.exists()


def test_unknown_run_mismatched_payload_is_finalized_when_source_is_repopulated(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    registry, store, _ = _github_production_fixture(registry_factory, tmp_path, monkeypatch)
    evidence_dir = store.state_root / "acceptance-evidence"
    residue = evidence_dir / "BUR-RUN-20990101T000000Z-raceoccupied.json"
    original = json.dumps({"generation": 1}).encode()
    moved = json.dumps({"generation": 2}).encode()
    repopulated = json.dumps({"generation": 3}).encode()
    residue.write_bytes(original)
    real_move = closure_observer._atomic_quarantine_move
    raced = False

    def replace_move_and_repopulate(source, destination):
        nonlocal raced
        if Path(source) == residue and not raced:
            producer = residue.with_name(residue.name + ".producer")
            producer.write_bytes(moved)
            closure_observer.os.replace(producer, residue)
            real_move(source, destination)
            residue.write_bytes(repopulated)
            raced = True
            return None
        return real_move(source, destination)

    monkeypatch.setattr(
        closure_observer, "_atomic_quarantine_move", replace_move_and_repopulate
    )
    result = reconcile_state_evidence(
        registry, store, now=NOW, github=lambda argv: merged_pr_detail()
    )

    assert raced is True
    retirement = result["evidence_retirement"]
    assert retirement["before"]["quarantined_count"] == 1
    before_record = retirement["before"]["quarantined_entries"][0]
    assert before_record["race_resolution"] == "staged-payload-preserved"
    assert before_record["source_sha256"] == hashlib.sha256(moved).hexdigest()
    assert Path(before_record["quarantine_path"]).read_bytes() == moved
    assert retirement["after"]["quarantined_count"] == 1
    after_record = retirement["after"]["quarantined_entries"][0]
    assert after_record.get("race_resolution") is None
    assert after_record["source_sha256"] == hashlib.sha256(repopulated).hexdigest()
    assert Path(after_record["quarantine_path"]).read_bytes() == repopulated
    assert retirement["quarantined_count"] == 2
    assert not residue.exists()
    quarantine_root = closure_observer._evidence_quarantine_path(store)
    assert not any(
        item.name.startswith(".pending-") for item in quarantine_root.iterdir()
    )

    repeat = reconcile_state_evidence(
        registry, store, now=NOW, github=lambda argv: merged_pr_detail()
    )
    assert repeat["evidence_retirement"]["quarantined_count"] == 0
    assert not any(
        "staged payload digest mismatch" in error
        for phase in ("before", "after")
        for error in repeat["evidence_retirement"][phase]["errors"]
    )


def test_unknown_run_unreadable_race_payload_is_preserved_without_pending_loop(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    registry, store, _ = _github_production_fixture(registry_factory, tmp_path, monkeypatch)
    evidence_dir = store.state_root / "acceptance-evidence"
    residue = evidence_dir / "BUR-RUN-20990101T000000Z-raceopaque00.json"
    original = json.dumps({"generation": 1}).encode()
    malformed = b"not-json"
    repopulated = json.dumps({"generation": 3}).encode()
    residue.write_bytes(original)
    real_move = closure_observer._atomic_quarantine_move
    raced = False

    def move_malformed_and_repopulate(source, destination):
        nonlocal raced
        if Path(source) == residue and not raced:
            producer = residue.with_name(residue.name + ".producer")
            producer.write_bytes(malformed)
            closure_observer.os.replace(producer, residue)
            real_move(source, destination)
            residue.write_bytes(repopulated)
            raced = True
            return None
        return real_move(source, destination)

    monkeypatch.setattr(
        closure_observer, "_atomic_quarantine_move", move_malformed_and_repopulate
    )
    result = reconcile_state_evidence(
        registry, store, now=NOW, github=lambda argv: merged_pr_detail()
    )

    assert raced is True
    retirement = result["evidence_retirement"]
    assert any(
        "opaque-preserved-at:" in error
        for error in retirement["before"]["errors"]
    )
    quarantine_root = closure_observer._evidence_quarantine_path(store)
    preserved = [
        item for item in quarantine_root.iterdir() if item.name.startswith("preserved-")
    ]
    assert len(preserved) == 1
    assert (preserved[0] / "payload.json").read_bytes() == malformed
    preservation = json.loads(
        (preserved[0] / "preservation.json").read_text(encoding="utf-8")
    )
    assert preservation["kind"] == "bureau.acceptance_evidence_quarantine_preservation"
    assert "payload-content-digest" in preservation["does_not_establish"]
    assert not any(
        item.name.startswith(".pending-") for item in quarantine_root.iterdir()
    )
    assert retirement["after"]["quarantined_count"] == 1
    after_record = retirement["after"]["quarantined_entries"][0]
    assert Path(after_record["quarantine_path"]).read_bytes() == repopulated

    repeat = reconcile_state_evidence(
        registry, store, now=NOW, github=lambda argv: merged_pr_detail()
    )
    assert repeat["evidence_retirement"]["quarantined_count"] == 0
    assert not any(
        "staged payload" in error
        for phase in ("before", "after")
        for error in repeat["evidence_retirement"][phase]["errors"]
    )


def test_unknown_run_hardlinked_source_is_frozen_into_private_inode(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    registry, store, _ = _github_production_fixture(registry_factory, tmp_path, monkeypatch)
    evidence_dir = store.state_root / "acceptance-evidence"
    residue = evidence_dir / "BUR-RUN-20990101T000000Z-hardlink000.json"
    raw = json.dumps({"accepted": True}).encode()
    residue.write_bytes(raw)
    external_link = tmp_path / "external-hardlink.json"
    closure_observer.os.link(residue, external_link)

    result = reconcile_state_evidence(
        registry, store, now=NOW, github=lambda argv: merged_pr_detail()
    )

    record = result["evidence_retirement"]["before"]["quarantined_entries"][0]
    quarantine = Path(record["quarantine_path"])
    assert quarantine.read_bytes() == raw
    assert quarantine.stat().st_nlink == 1
    assert quarantine.stat().st_mode & 0o777 == 0o600
    external_link.write_bytes(json.dumps({"accepted": False}).encode())
    assert quarantine.read_bytes() == raw
    assert hashlib.sha256(quarantine.read_bytes()).hexdigest() == record["source_sha256"]


def test_unknown_run_canonical_bundle_is_classified_and_quarantined(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    registry, store, _ = _github_production_fixture(registry_factory, tmp_path, monkeypatch)
    evidence_dir = store.state_root / "acceptance-evidence"
    unknown_run_id = "BUR-RUN-20990101T000000Z-cafebabe00"
    residue = evidence_dir / f"{unknown_run_id}.json"
    residue.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": EVIDENCE_BUNDLE_KIND,
                "run_id": unknown_run_id,
                "task_id": "UNKNOWN-T001",
                "task_sha256": "1" * 64,
                "plan_sha256": "2" * 64,
                "evidence": {},
            }
        ),
        encoding="utf-8",
    )

    result = reconcile_state_evidence(
        registry, store, now=NOW, github=lambda argv: merged_pr_detail()
    )

    record = result["evidence_retirement"]["before"]["quarantined_entries"][0]
    assert record["classification"] == "unknown-run-bundle"
    assert record["declared_run_id"] == unknown_run_id
    assert Path(record["quarantine_path"]).is_file()
    assert not residue.exists()


def test_malformed_unknown_run_evidence_is_preserved_for_diagnosis(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    registry, store, _ = _github_production_fixture(registry_factory, tmp_path, monkeypatch)
    evidence_dir = store.state_root / "acceptance-evidence"
    residue = evidence_dir / "BUR-RUN-20990101T000000Z-badbadbad0.json"
    residue.write_text("not-json", encoding="utf-8")

    result = reconcile_state_evidence(
        registry, store, now=NOW, github=lambda argv: merged_pr_detail()
    )

    assert result["evidence_retirement"]["quarantined_count"] == 0
    assert residue.exists()
    assert any(
        "unknown-run acceptance evidence is unreadable" in error
        for error in result["evidence_retirement"]["before"]["errors"]
    )


def test_symlinked_unknown_run_evidence_is_preserved_for_diagnosis(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    registry, store, _ = _github_production_fixture(registry_factory, tmp_path, monkeypatch)
    evidence_dir = store.state_root / "acceptance-evidence"
    external = tmp_path / "unknown-run-evidence.json"
    external.write_text(json.dumps({"accepted": True}), encoding="utf-8")
    residue = evidence_dir / "BUR-RUN-20990101T000000Z-symlink000.json"
    residue.symlink_to(external)

    result = reconcile_state_evidence(
        registry, store, now=NOW, github=lambda argv: merged_pr_detail()
    )

    assert result["evidence_retirement"]["quarantined_count"] == 0
    assert residue.is_symlink()
    assert external.is_file()
    assert any(
        "unknown-run acceptance evidence is unreadable" in error
        for error in result["evidence_retirement"]["before"]["errors"]
    )


def test_malformed_terminal_bundle_is_preserved_for_diagnosis(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    registry, store, run = _github_production_fixture(registry_factory, tmp_path, monkeypatch)
    evidence_path = store.state_root / "acceptance-evidence" / f"{run['run_id']}.json"
    fail_run(store, run["run_id"], "worker failed", "failed")
    evidence_path.write_text("not-json", encoding="utf-8")

    result = reconcile_state_evidence(registry, store, now=NOW)

    assert result["observed_run_count"] == 0
    assert result["evidence_retirement"]["retired_count"] == 0
    assert result["evidence_retirement"]["before"]["preserved_count"] == 1
    assert evidence_path.exists()
    assert any(
        "acceptance evidence bundle is unreadable" in error
        for error in result["evidence_retirement"]["before"]["errors"]
    )


def _replace_evidence_root_with_symlink(
    store: StateStore, run: dict[str, Any], tmp_path: Path
) -> tuple[Path, Path]:
    root = store.state_root / "acceptance-evidence"
    source = root / f"{run['run_id']}.json"
    payload = source.read_text(encoding="utf-8")
    source.unlink()
    root.rmdir()
    external = tmp_path / "external-acceptance-evidence"
    external.mkdir()
    external_bundle = external / source.name
    external_bundle.write_text(payload, encoding="utf-8")
    root.symlink_to(external, target_is_directory=True)
    return root, external_bundle


def test_symlinked_evidence_root_is_not_scanned_for_terminal_retirement(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    registry, store, run = _github_production_fixture(registry_factory, tmp_path, monkeypatch)
    root, external_bundle = _replace_evidence_root_with_symlink(store, run, tmp_path)
    fail_run(store, run["run_id"], "worker failed", "failed")

    result = reconcile_state_evidence(registry, store, now=NOW)

    assert root.is_symlink()
    assert external_bundle.exists()
    assert result["observed_run_count"] == 0
    assert result["evidence_retirement"]["retired_count"] == 0
    assert any(
        "acceptance evidence root must be a real non-symlink directory" in error
        for error in result["evidence_retirement"]["before"]["errors"]
    )


def test_symlinked_evidence_root_is_not_read_for_active_closeout(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    registry, store, run = _github_production_fixture(registry_factory, tmp_path, monkeypatch)
    root, external_bundle = _replace_evidence_root_with_symlink(store, run, tmp_path)

    result = reconcile_state_evidence(
        registry, store, now=NOW, github=lambda argv: merged_pr_detail()
    )

    assert root.is_symlink()
    assert external_bundle.exists()
    assert result["terminalized_count"] == 0
    assert result["open_count"] == 1
    blocked = result["observations"][0]
    assert blocked["reason"] == "evidence-provider-unavailable"
    assert "acceptance evidence root must be a real non-symlink directory" in blocked["detail"]
    assert store.run(run["run_id"])["state"] == "assigned"


def test_github_adapter_timeout_is_explicit_open_observation(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    registry, store, run = _github_production_fixture(registry_factory, tmp_path, monkeypatch)

    def github(argv):
        raise TimeoutError("GitHub unavailable")

    result = reconcile_state_evidence(registry, store, now=NOW, github=github)

    assert result["terminalized_count"] == 0
    assert result["open_count"] == 1
    blocked = result["observations"][0]
    assert blocked["state"] == "open"
    assert blocked["reason"] == "evidence-adapter-unavailable"
    assert blocked["adapter"] == {
        "schema_version": 1,
        "kind": "bureau.evidence_adapter_unavailable",
        "authority": "github",
        "adapter": "runtime_refresh.gh_json",
        "target": {"repository": "heimgewebe/test", "pull_request": 7},
        "detail": "TimeoutError: GitHub unavailable",
        "does_not_establish": [
            "evidence-rejection",
            "evidence-authenticity",
            "task-completion",
        ],
    }
    assert blocked["mutated"] is False
    assert result["evidence_retirement"]["retired_count"] == 0
    evidence_path = store.state_root / "acceptance-evidence" / f"{run['run_id']}.json"
    assert evidence_path.exists()
    assert store.run(run["run_id"])["state"] == "assigned"


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

def test_retargeted_github_pr_stays_open(
    registry_factory, tmp_path: Path, monkeypatch
) -> None:
    registry, store, run = _github_production_fixture(registry_factory, tmp_path, monkeypatch)

    def github(argv):
        return {**merged_pr_detail(), "baseRefName": "release"}

    result = reconcile_state_evidence(registry, store, now=NOW, github=github)

    assert result["terminalized_count"] == 0
    assert result["open_count"] == 1
    assert result["observations"][0]["evaluation"]["criteria"][0]["reason"] == (
        "evidence-source-unauthenticated"
    )
    assert store.run(run["run_id"])["state"] == "assigned"
