from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bureau import legacy, runtime_refresh, task_specs
from bureau.v2 import StateStore

DEPLOYED = "a" * 40
MAIN = "b" * 40


def _activation_observation(
    ancestry_patch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    observed_at = runtime_refresh.isoformat(runtime_refresh.utc_now())
    ancestry: dict[str, Any] = {
        "schema_version": 1,
        "status": "proven",
        "method": "github-compare",
        "deployed_source_commit": DEPLOYED,
        "main_commit": MAIN,
        "compare_status": "ahead",
        "ahead_by": 1,
        "behind_by": 0,
        "merge_base_commit": DEPLOYED,
    }
    if ancestry_patch:
        ancestry.update(ancestry_patch)
    observation: dict[str, Any] = {
        "schema_version": 1,
        "kind": "bureau_runtime_refresh_observation",
        "repository": runtime_refresh.DEFAULT_REPOSITORY,
        "main_commit": MAIN,
        "pull_request": {
            "number": 2222,
            "url": None,
            "head_commit": "c" * 40,
            "merge_commit": MAIN,
        },
        "merged_at": observed_at,
        "required_checks": list(
            runtime_refresh.DEFAULT_AUTHORITY_ADOPTION_REQUIRED_CHECKS
        ),
        "check_summary": {
            name: {"state": "success", "observed_states": ["success"]}
            for name in runtime_refresh.DEFAULT_AUTHORITY_ADOPTION_REQUIRED_CHECKS
        },
        "deployed_source_commit": DEPLOYED,
        "deployed_manifest_sha256": "d" * 64,
        "source_ancestry": ancestry,
        "runtime_source_identity": {
            "schema_version": 1,
            "status": "proven",
            "deployed_source_commit": DEPLOYED,
            "registry_source_commit": DEPLOYED,
            "registry_reasons": [],
        },
        "lag_commits": 1,
        "scheduler_target_state": "source-not-current",
        "age_seconds": 0,
        "slo_seconds": runtime_refresh.DEFAULT_SLO_SECONDS,
        "status": "candidate",
        "reason_codes": [],
        "recovery_action": {
            "action": "prepare-intent",
            "eligible": True,
            "requires_authorization": True,
        },
        "observed_at": observed_at,
        "does_not_establish": list(
            runtime_refresh.RUNTIME_AUTHORITY_ACTIVATION_OBSERVATION_DOES_NOT_ESTABLISH
        ),
    }
    observation["target_sha256"] = runtime_refresh.sha256_bytes(
        runtime_refresh.canonical_bytes(runtime_refresh._target_payload(observation))
    )
    return runtime_refresh.bind_digest(observation, "observation_sha256")


def _activation_evidence(
    observation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    bound_observation = observation or _activation_observation()
    evidence = {
        field: None
        for field in runtime_refresh.RUNTIME_AUTHORITY_ACTIVATION_EVIDENCE_REQUIRED_FIELDS
    }
    evidence["observation"] = bound_observation
    evidence["installed_runtime_validation"] = {
        "deployment_manifest_sha256": bound_observation["deployed_manifest_sha256"]
    }
    return evidence


@pytest.mark.parametrize(
    "ancestry_patch",
    [
        {"method": "unsupported"},
        {"ahead_by": 0},
        {"behind_by": 1},
        {"merge_base_commit": "f" * 40},
    ],
)
def test_state_store_activation_rejects_malformed_proven_source_ancestry_before_delegate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ancestry_patch: dict[str, Any],
) -> None:
    state_root = (tmp_path / "state").resolve()
    store = StateStore(state_root / "bureau.sqlite3", state_root)
    delegated = False

    def unexpected_delegate(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal delegated
        del args, kwargs
        delegated = True
        return {"delegated": True}

    monkeypatch.setattr(
        task_specs,
        "put_runtime_refresh_protected_publication_activation",
        unexpected_delegate,
    )

    with pytest.raises(
        legacy.StateError,
        match="runtime-refresh protected-publication activation mutation contract is invalid",
    ):
        observation = _activation_observation(ancestry_patch)
        store.put_runtime_refresh_protected_publication_activation_task_spec(
            {},
            idempotency_key="test-malformed-ancestry",
            expected_revision=1,
            activation_observation=observation,
            activation_evidence=_activation_evidence(observation),
        )

    assert delegated is False


def test_state_store_activation_rejects_noncanonical_evidence_before_delegate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = (tmp_path / "noncanonical-evidence").resolve()
    store = StateStore(state_root / "bureau.sqlite3", state_root)
    delegated = False

    def unexpected_delegate(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal delegated
        del args, kwargs
        delegated = True
        return {"delegated": True}

    monkeypatch.setattr(
        task_specs,
        "put_runtime_refresh_protected_publication_activation",
        unexpected_delegate,
    )
    observation = _activation_observation()
    evidence = _activation_evidence(observation)
    evidence["unexpected"] = True

    with pytest.raises(legacy.StateError, match="activation mutation contract is invalid"):
        store.put_runtime_refresh_protected_publication_activation_task_spec(
            {},
            idempotency_key="test-noncanonical-evidence",
            expected_revision=1,
            activation_observation=observation,
            activation_evidence=evidence,
        )

    assert delegated is False


def test_state_store_activation_rejects_missing_manifest_digest_binding_before_delegate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = (tmp_path / "missing-manifest-digest").resolve()
    store = StateStore(state_root / "bureau.sqlite3", state_root)
    delegated = False

    def unexpected_delegate(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal delegated
        del args, kwargs
        delegated = True
        return {"delegated": True}

    monkeypatch.setattr(
        task_specs,
        "put_runtime_refresh_protected_publication_activation",
        unexpected_delegate,
    )
    observation = _activation_observation()
    evidence = _activation_evidence(observation)
    evidence["observation"] = dict(observation)
    evidence["observation"].pop("deployed_manifest_sha256")
    evidence["installed_runtime_validation"].pop("deployment_manifest_sha256")

    with pytest.raises(legacy.StateError, match="activation mutation contract is invalid"):
        store.put_runtime_refresh_protected_publication_activation_task_spec(
            {},
            idempotency_key="test-missing-manifest-digest",
            expected_revision=1,
            activation_observation=observation,
            activation_evidence=evidence,
        )

    assert delegated is False


@pytest.mark.parametrize("missing_field", ["repository", "required_checks", "check_summary"])
def test_state_store_activation_rejects_incomplete_canonical_observation_before_delegate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_field: str,
) -> None:
    state_root = (tmp_path / f"missing-observation-{missing_field}").resolve()
    store = StateStore(state_root / "bureau.sqlite3", state_root)
    delegated = False

    def unexpected_delegate(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal delegated
        del args, kwargs
        delegated = True
        return {"delegated": True}

    monkeypatch.setattr(
        task_specs,
        "put_runtime_refresh_protected_publication_activation",
        unexpected_delegate,
    )
    observation = _activation_observation()
    observation.pop("observation_sha256")
    observation.pop(missing_field)
    observation["target_sha256"] = runtime_refresh.sha256_bytes(
        runtime_refresh.canonical_bytes(runtime_refresh._target_payload(observation))
    )
    observation = runtime_refresh.bind_digest(observation, "observation_sha256")
    evidence = _activation_evidence(observation)

    with pytest.raises(legacy.StateError, match="activation mutation contract is invalid"):
        store.put_runtime_refresh_protected_publication_activation_task_spec(
            {},
            idempotency_key=f"test-missing-observation-{missing_field}",
            expected_revision=1,
            activation_observation=observation,
            activation_evidence=evidence,
        )

    assert delegated is False


def test_state_store_activation_allows_valid_source_ancestry_to_atomic_delegate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = (tmp_path / "state").resolve()
    store = StateStore(state_root / "bureau.sqlite3", state_root)
    expected = {"delegated": True}

    def delegate(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        return expected

    monkeypatch.setattr(
        task_specs,
        "put_runtime_refresh_protected_publication_activation",
        delegate,
    )

    observation = _activation_observation()
    result = store.put_runtime_refresh_protected_publication_activation_task_spec(
        {},
        idempotency_key="test-valid-ancestry",
        expected_revision=1,
        activation_observation=observation,
        activation_evidence=_activation_evidence(observation),
    )

    assert result == expected
