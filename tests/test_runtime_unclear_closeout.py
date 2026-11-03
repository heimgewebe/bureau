"""Source-bound regression tests for historical UNCLEAR => SUPERSEDED only."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
from test_runtime_refresh import after_effect_unclear_fixture

from bureau import runtime_refresh as refresh
from bureau import runtime_unclear_closeout as closeout
from bureau import task_specs

NEW_SOURCE = "e" * 40
SUCCESSOR = "BUREAU-PROTECTED-VERIFIED-SUCCESSOR-TEST"


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict, object, Path]:
    options, intent, store, result_path, artifacts = after_effect_unclear_fixture(tmp_path)
    (artifacts["backup"] / "bureau").write_text(
        "#!/bin/sh\npost-install launcher changed\n", encoding="utf-8"
    )
    # Synthetic fixture's historical Git object ids are not backed by a
    # checkout; the production CLI still proves actual Git ancestry itself.
    monkeypatch.setattr(refresh, "_prove_source_ancestry", lambda **_: None)
    persisted = refresh.reconcile_after_effect_unclear_runtime_refresh_authority(
        **options, create_record=True
    )["reconciliation"]
    original = store.task_spec(options["approval_task_id"])
    assert original is not None

    manifest_path = Path(intent["prefix"]) / "deployment-manifest.json"
    manifest = refresh.read_json(manifest_path)
    manifest["source_commit"] = NEW_SOURCE
    manifest = refresh.bind_digest(
        manifest, refresh.RUNTIME_MANIFEST_PAYLOAD_DIGEST_FIELD
    )
    manifest_path.write_bytes(refresh.canonical_bytes(manifest))
    new_manifest_sha = refresh.sha256_bytes(manifest_path.read_bytes())

    successor_intent = refresh.bind_digest({
        "schema_version": 1,
        "kind": "bureau_runtime_refresh_intent",
        "approval_task_id": SUCCESSOR,
        "prefix": str(Path(intent["prefix"])),
    }, "intent_sha256")
    successor_intent_sha = successor_intent["intent_sha256"]
    successor_spec = copy.deepcopy(original["spec"])
    successor_spec["id"] = SUCCESSOR
    successor_spec["state"] = "verified"
    successor_spec["title"] = "Verified later runtime successor test fixture"
    successor_spec["metadata"]["runtime_closeout"] = {
        "intent_sha256": successor_intent_sha,
        "manifest_sha256": new_manifest_sha,
    }
    # A fixture-only historical import inserts the exact simulated verified
    # successor without misrepresenting a user-facing successful release.
    with store.immediate() as db:
        receipt = task_specs._put_legacy_registry_import(
            db, successor_spec, idempotency_key="test:successor-source",
            expected_revision=None, source="test-successor-fixture",
        )
    successor_revision = receipt["revision"]
    successor_sha = receipt["spec_sha256"]
    successor_intent_path = (Path(intent["state_root"]) / "intents"
                             / (successor_intent_sha + ".json"))
    successor_intent_path.write_bytes(refresh.canonical_bytes(successor_intent))

    healthy_path = tmp_path / "healthy-native-verifier-receipt.json"
    healthy_path.write_text('{"fixture":true}\n', encoding="utf-8")
    healthy_sha = refresh.sha256_bytes(healthy_path.read_bytes())
    later = {
        "task_id": SUCCESSOR,
        "revision": successor_revision,
        "spec_sha256": successor_sha,
        "runtime_result_sha256": "b" * 64,
        "source_commit": NEW_SOURCE,
        "manifest_sha256": new_manifest_sha,
        "live_verifier_receipt_sha256": healthy_sha,
        "successor_closeout_sha256": "f" * 64,
    }
    monkeypatch.setattr(closeout, "_verified_successor", lambda **_: later)
    monkeypatch.setattr(
        refresh, "_current_runtime_continuity",
        lambda **_: {
            "source_commit": NEW_SOURCE,
            "manifest_sha256": new_manifest_sha,
            "registry_tree_sha256": "a" * 64,
        },
    )
    monkeypatch.setattr(refresh, "_prove_source_ancestry", lambda **_: None)
    kwargs = {
        "state_root": Path(intent["state_root"]),
        "source_repository": options["source_repository"],
        "original_task_id": options["approval_task_id"],
        "original_revision": options["expected_revision"],
        "original_spec_sha256": options["expected_spec_sha256"],
        "original_intent_sha256": options["intent_sha256"],
        "original_result_sha256": options["result_sha256"],
        "reconciliation_sha256": persisted["reconciliation_sha256"],
        "successor_task_id": SUCCESSOR,
        "healthy_verifier_receipt": healthy_path,
        "healthy_verifier_sha256": healthy_sha,
        "store": store,
    }
    return kwargs, store, result_path


def test_preview_authenticates_original_and_never_mutates_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kwargs, store, result_path = _setup(tmp_path, monkeypatch)
    old = store.task_spec(kwargs["original_task_id"])
    result_bytes = result_path.read_bytes()
    preview = closeout.preview(**kwargs)
    assert preview["status"] == "ready_for_exact_cas"
    assert preview["original_revision"] == old["revision"]
    assert len(preview["preview_sha256"]) == 64
    assert store.task_spec(kwargs["original_task_id"]) == old
    assert result_path.read_bytes() == result_bytes


def test_apply_supersedes_in_new_revision_without_rewriting_original_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kwargs, store, result_path = _setup(tmp_path, monkeypatch)
    old = store.task_spec(kwargs["original_task_id"])
    raw = result_path.read_bytes()
    preview = closeout.preview(**kwargs)
    done = closeout.apply(expected_preview_sha256=preview["preview_sha256"], **kwargs)
    assert done["status"] == "superseded"
    assert done["revision"] == old["revision"] + 1
    assert done["supersession"]["original_effect_status"] == "unclear"
    assert done["supersession"]["original_effect_started"] is True
    assert "original_installer_succeeded" in done["supersession"]["does_not_establish"]
    refresh.verify_digest(done["supersession"], "supersession_sha256")
    current = store.task_spec(kwargs["original_task_id"])
    assert current["spec"]["state"] == "superseded"
    with store.connect() as connection:
        original = task_specs.get_revision(
            connection, kwargs["original_task_id"], old["revision"]
        )
    assert original["spec"] == old["spec"]
    assert result_path.read_bytes() == raw
    replay = closeout.preview(**kwargs)
    assert replay["idempotent_replay"] is True
    assert replay["preview_sha256"] == preview["preview_sha256"]
    again = closeout.apply(expected_preview_sha256=preview["preview_sha256"], **kwargs)
    assert again["idempotent_replay"] is True
    assert store.task_spec(kwargs["original_task_id"]) == current


def test_wrong_preview_fails_before_cas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kwargs, store, result_path = _setup(tmp_path, monkeypatch)
    old = store.task_spec(kwargs["original_task_id"])
    result_bytes = result_path.read_bytes()
    with pytest.raises(closeout.HistoricalUnclearCloseoutError,
                       match="preview-stale-or-mismatched"):
        closeout.apply(expected_preview_sha256="0" * 64, **kwargs)
    assert store.task_spec(kwargs["original_task_id"]) == old
    assert result_path.read_bytes() == result_bytes


def test_original_result_or_historical_receipt_missing_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kwargs, store, result_path = _setup(tmp_path, monkeypatch)
    before = store.task_spec(kwargs["original_task_id"])
    result_path.unlink()
    with pytest.raises((refresh.RuntimeRefreshError, OSError, ValueError)):
        closeout.preview(**kwargs)
    assert store.task_spec(kwargs["original_task_id"]) == before


def test_verified_successor_and_original_are_distinct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kwargs, store, _ = _setup(tmp_path, monkeypatch)
    kwargs["successor_task_id"] = kwargs["original_task_id"]
    old = store.task_spec(kwargs["original_task_id"])
    with pytest.raises(closeout.HistoricalUnclearCloseoutError,
                       match="successor-is-original"):
        closeout.preview(**kwargs)
    assert store.task_spec(kwargs["original_task_id"]) == old


def test_successor_revision_drift_prevents_supersession(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kwargs, store, _ = _setup(tmp_path, monkeypatch)
    view = closeout.preview(**kwargs)
    original = store.task_spec(kwargs["original_task_id"])
    successor = store.task_spec(SUCCESSOR)
    spec = copy.deepcopy(successor["spec"])
    spec["title"] = "Drifted after preview"
    with store.immediate() as db:
        task_specs._put_legacy_registry_import(
            db, spec, idempotency_key="test:successor-drift",
            expected_revision=successor["revision"], source="test-fixture",
        )
    with pytest.raises(closeout.HistoricalUnclearCloseoutError,
                       match="successor-cas-preimage-changed"):
        closeout.apply(expected_preview_sha256=view["preview_sha256"], **kwargs)
    assert store.task_spec(kwargs["original_task_id"]) == original


@pytest.mark.parametrize(
    ("damage", "reason"),
    [
        ("degraded", "verifier-retained-not-healthy-source-bound"),
        ("partial", "verifier-retained-not-healthy-source-bound"),
        ("wrong_source", "verifier-retained-not-healthy-source-bound"),
        ("before_install", "verifier-retained-not-healthy-source-bound"),
        ("wrong_sha", "verifier-retained-sha256-drift"),
    ],
)
def test_retained_native_verifier_fails_closed_on_wrong_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str, reason: str,
) -> None:
    from bureau_cycle import cycle_contract

    # The native cycle receipt schema is independently verified elsewhere; this
    # tests this additional source/manifest/health binding against bad receipts.
    monkeypatch.setattr(cycle_contract, "validate_receipt", lambda *_a, **_k: [])
    root = tmp_path / "state/bureau/runtime-refresh"
    runs = tmp_path / "state/bureau-verifier/runs"
    runs.mkdir(parents=True)
    path = runs / "native-verified.json"
    receipt = {
        "schema_version": 1,
        "status": "ok",
        "result": "completed",
        "degraded": False,
        "critical": False,
        "findings": [],
        "receipt_path": str(path),
        "started_at": "2026-11-03T18:39:00Z",
        "finished_at": "2026-11-03T18:39:01Z",
        "runtime_launch_identity": {
            "schema_version": 1,
            "kind": "bureau_runtime_launch_identity_at_verifier_entry",
            "source_commit": NEW_SOURCE,
            "manifest_sha256": "a" * 64,
        },
    }
    if damage == "degraded":
        receipt["degraded"] = True
    elif damage == "partial":
        receipt["result"] = "partial"
    elif damage == "wrong_source":
        receipt["runtime_launch_identity"]["source_commit"] = "b" * 40
    elif damage == "before_install":
        receipt["started_at"] = "2026-11-03T17:00:00Z"
    path.write_bytes(refresh.canonical_bytes(receipt))
    expected = refresh.sha256_bytes(path.read_bytes())
    if damage == "wrong_sha":
        expected = "e" * 64
    with pytest.raises(closeout.HistoricalUnclearCloseoutError, match=reason):
        closeout._healthy_retained_verifier(
            state_root=root,
            path=path,
            expected_sha256=expected,
            source=NEW_SOURCE,
            manifest_sha256="a" * 64,
            installed_at="2026-11-03T18:19:00Z",
        )


def test_retained_native_verifier_accepts_exact_completed_non_degraded_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bureau_cycle import cycle_contract

    monkeypatch.setattr(cycle_contract, "validate_receipt", lambda *_a, **_k: [])
    root = tmp_path / "state/bureau/runtime-refresh"
    runs = tmp_path / "state/bureau-verifier/runs"
    runs.mkdir(parents=True)
    path = runs / "native-verified.json"
    receipt = {
        "schema_version": 1,
        "status": "ok",
        "result": "completed",
        "degraded": False,
        "critical": False,
        "findings": [],
        "receipt_path": str(path),
        "started_at": "2026-11-03T18:39:00Z",
        "finished_at": "2026-11-03T18:39:01Z",
        "runtime_launch_identity": {
            "schema_version": 1,
            "kind": "bureau_runtime_launch_identity_at_verifier_entry",
            "source_commit": NEW_SOURCE,
            "manifest_sha256": "a" * 64,
        },
    }
    path.write_bytes(refresh.canonical_bytes(receipt))
    digest = refresh.sha256_bytes(path.read_bytes())
    actual = closeout._healthy_retained_verifier(
        state_root=root, path=path, expected_sha256=digest,
        source=NEW_SOURCE, manifest_sha256="a" * 64,
        installed_at="2026-11-03T18:19:00Z",
    )
    assert actual["verifier_degraded"] is False
    assert actual["verifier_result"] == "completed"
    assert actual["verifier_receipt_sha256"] == digest
