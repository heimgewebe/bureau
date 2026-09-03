from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bureau import legacy, task_specs
from bureau.v2 import StateStore

DEPLOYED = "a" * 40
MAIN = "b" * 40


def _activation_observation(
    ancestry_patch: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
    return {
        "deployed_source_commit": DEPLOYED,
        "main_commit": MAIN,
        "source_ancestry": ancestry,
        "runtime_source_identity": {
            "schema_version": 1,
            "status": "proven",
            "deployed_source_commit": DEPLOYED,
            "registry_source_commit": DEPLOYED,
            "registry_reasons": [],
        },
    }


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
        store.put_runtime_refresh_protected_publication_activation_task_spec(
            {},
            idempotency_key="test-malformed-ancestry",
            expected_revision=1,
            activation_observation=_activation_observation(ancestry_patch),
            activation_evidence={},
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

    result = store.put_runtime_refresh_protected_publication_activation_task_spec(
        {},
        idempotency_key="test-valid-ancestry",
        expected_revision=1,
        activation_observation=_activation_observation(),
        activation_evidence={},
    )

    assert result == expected
