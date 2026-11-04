"""Evidence-bound, non-success terminal disposition for an old unclear Bureau effect.

This deliberately cannot run or retry an installer. It creates one new TaskSpec
revision, never alters the original immutable authority revision or journals.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

from . import runtime_refresh as refresh
from . import task_specs
from .read_only_state import ReadOnlyStateStore


class HistoricalUnclearCloseoutError(ValueError):
    pass


def _require(ok: bool, code: str) -> None:
    if not ok:
        raise HistoricalUnclearCloseoutError(code)


def _healthy_retained_verifier(
    *, state_root: Path, path: Path, expected_sha256: str,
    source: str, manifest_sha256: str, installed_at: str,
) -> dict[str, Any]:
    """Accept one retained native *completed* systemd verifier, not volatile latest.

    A later unrelated failed task may degrade the next verifier, but cannot
    erase a genuine post-install healthy run on the exact installed release.
    """
    _require(refresh._is_sha256(expected_sha256), "verifier-hash-invalid")
    root = state_root.parent.parent / "bureau-verifier" / "runs"
    path = path.expanduser()
    _require(
        path.is_absolute() and path.parent.resolve() == root.resolve()
        and path.is_file() and not path.is_symlink(),
        "verifier-retained-path-invalid",
    )
    raw = path.read_bytes()
    _require(
        refresh.sha256_bytes(raw) == expected_sha256,
        "verifier-retained-sha256-drift",
    )
    try:
        receipt = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HistoricalUnclearCloseoutError("verifier-retained-invalid-json") from exc
    from bureau_cycle.cycle_contract import validate_receipt
    errors = validate_receipt(receipt, expected_stage="verifier")
    _require(not errors, "verifier-retained-native-contract-invalid")
    _require(
        receipt.get("receipt_path") == str(path)
        and receipt.get("result") == "completed"
        and receipt.get("degraded") is False
        and receipt.get("critical") is False
        and receipt.get("findings") == []
        and receipt.get("runtime_launch_identity") == {
            "schema_version": 1,
            "kind": "bureau_runtime_launch_identity_at_verifier_entry",
            "source_commit": source,
            "manifest_sha256": manifest_sha256,
        }
        and refresh.parse_time(receipt["started_at"]) >= refresh.parse_time(installed_at)
        and refresh.parse_time(receipt["finished_at"]) >= refresh.parse_time(receipt["started_at"]),
        "verifier-retained-not-healthy-source-bound",
    )
    return {
        "source_commit": source,
        "manifest_sha256": manifest_sha256,
        "verifier_result": "completed",
        "verifier_degraded": False,
        "verifier_receipt_sha256": expected_sha256,
    }


def _verified_successor(
    *,
    store: Any,
    root: Path,
    successor_task_id: str,
    source_repository: Path,
    healthy_verifier_receipt: Path,
    healthy_verifier_sha256: str,
    live_verifier: Any,
) -> dict[str, Any]:
    successor = refresh._read_authority_task(store, successor_task_id)
    spec = successor["spec"]
    metadata = spec.get("metadata", {})
    closeout = refresh._validated_runtime_closeout(
        metadata.get("runtime_closeout")
    )
    _require(spec.get("state") == "verified", "successor-not-verified")
    _require(closeout["task_id"] == successor_task_id, "successor-task-drift")
    _require(
        isinstance(closeout.get("live_verifier_evidence"), dict),
        "successor-has-no-live-verifier",
    )

    intent_path = root / "intents" / (closeout["intent_sha256"] + ".json")
    intent = refresh.read_json(intent_path)
    refresh.verify_digest(intent, "intent_sha256")
    _require(
        intent["approval_task_id"] == successor_task_id
        and intent["main_commit"] == closeout["source_commit"]
        and intent["target_sha256"] == closeout["target_sha256"],
        "successor-intent-drift",
    )
    result_path = root / "attempts" / intent["target_sha256"] / "result.json"
    result = refresh._validate_result_for_intent(refresh.read_json(result_path), intent)
    receipt = result.get("install_receipt")
    _require(
        result.get("result_sha256") == closeout["runtime_result_sha256"]
        and result.get("status") == "deployed"
        and result.get("effect_started") is True
        and isinstance(receipt, dict)
        and receipt.get("manifest_sha256") == closeout["manifest_sha256"],
        "successor-result-not-deployed",
    )
    continuity = refresh._current_runtime_continuity(
        prefix=Path(intent["prefix"]),
        intended_main_commit=closeout["source_commit"],
        source_repository=source_repository,
    )
    _require(
        continuity["source_commit"] == closeout["source_commit"]
        and continuity["manifest_sha256"] == closeout["manifest_sha256"],
        "successor-no-longer-serving",
    )
    # The retained verifier proves an earlier installation. Revalidate the
    # actual serving package/module, all three launchers and scheduler now,
    # against the same authenticated installer/result readback. Both preview
    # and apply (including candidate reconstruction) execute this guard.
    persisted_readback = result.get("readback")
    _require(isinstance(persisted_readback, dict), "successor-readback-missing")
    current_readback = refresh.readback_install(
        expected_commit=closeout["source_commit"],
        prefix=Path(intent["prefix"]),
        bin_dir=Path(intent["bin_dir"]),
        install_receipt=receipt,
    )
    _require(
        refresh._install_closeout_readbacks_match(
            persisted_readback, current_readback, receipt
        ),
        "successor-installed-runtime-drift",
    )
    live = (
        live_verifier(intent=intent, result=result)
        if live_verifier is not None else _healthy_retained_verifier(
            state_root=root,
            path=healthy_verifier_receipt,
            expected_sha256=healthy_verifier_sha256,
            source=closeout["source_commit"],
            manifest_sha256=closeout["manifest_sha256"],
            installed_at=receipt["installed_at"],
        )
    )
    _require(
        live.get("source_commit") == closeout["source_commit"]
        and live.get("manifest_sha256") == closeout["manifest_sha256"]
        and live.get("verifier_result") == "completed"
        and live.get("verifier_degraded") is False
        and refresh._is_sha256(live.get("verifier_receipt_sha256")),
        "successor-live-verifier-not-healthy",
    )
    return {
        "task_id": successor_task_id,
        "revision": successor["revision"],
        "spec_sha256": successor["spec_sha256"],
        "runtime_result_sha256": closeout["runtime_result_sha256"],
        "source_commit": closeout["source_commit"],
        "manifest_sha256": closeout["manifest_sha256"],
        "live_verifier_receipt_sha256": live["verifier_receipt_sha256"],
        "successor_closeout_sha256": refresh.sha256_bytes(
            refresh.canonical_bytes(closeout)
        ),
    }


def _historical_proof(
    *,
    store: Any,
    state_root: Path,
    source_repository: Path,
    original_task_id: str,
    original_revision: int,
    original_spec_sha256: str,
    original_intent_sha256: str,
    original_result_sha256: str,
    reconciliation_sha256: str,
) -> dict[str, Any]:
    current = refresh._read_authority_task(store, original_task_id)
    _require(
        current["revision"] == original_revision
        and current["spec_sha256"] == original_spec_sha256
        and current["spec"]["state"] == "ready",
        "original-authority-not-ready-exact",
    )
    from .runtime_refresh import reconcile_after_effect_unclear_runtime_refresh_authority
    original_store = ReadOnlyStateStore(store.path, store.state_root)
    preview = reconcile_after_effect_unclear_runtime_refresh_authority(
        state_root=state_root,
        approval_task_id=original_task_id,
        expected_revision=original_revision,
        expected_spec_sha256=original_spec_sha256,
        intent_sha256=original_intent_sha256,
        result_sha256=original_result_sha256,
        source_repository=source_repository,
        authority_store=original_store,
        create_record=False,
    )
    historical_path = Path(preview["path"])
    _require(historical_path.is_file() and not historical_path.is_symlink(),
             "original-reconciliation-not-persisted")
    persisted = refresh.read_json(historical_path)
    refresh.verify_digest(persisted, "reconciliation_sha256")
    _require(
        persisted["reconciliation_sha256"] == reconciliation_sha256
        and persisted["status"] == "historical_effect_authenticated",
        "original-reconciliation-drift",
    )
    for field in (
        "task_id", "original_taskspec_revision", "original_taskspec_sha256",
        "original_intent_sha256", "original_result_sha256", "original_started_sha256",
        "effect_history_sha256", "installer_receipt_file_sha256",
        "historical_manifest_sha256", "historical_source_commit",
    ):
        _require(persisted.get(field) == preview["reconciliation"].get(field),
                 "original-evidence-changed:" + field)
    _require(
        persisted["original_taskspec_revision"] == original_revision
        and persisted["original_taskspec_sha256"] == original_spec_sha256,
        "original-reconciliation-authority-drift",
    )
    return persisted


def preview(
    *,
    state_root: Path,
    source_repository: Path,
    original_task_id: str,
    original_revision: int,
    original_spec_sha256: str,
    original_intent_sha256: str,
    original_result_sha256: str,
    reconciliation_sha256: str,
    successor_task_id: str,
    healthy_verifier_receipt: Path,
    healthy_verifier_sha256: str,
    store: Any | None = None,
    live_verifier: Any = None,
) -> dict[str, Any]:
    """Read-only preview. No journal, installer, lease or TaskSpec mutation."""
    state_root = state_root.expanduser().resolve()
    source_repository = source_repository.expanduser().resolve()
    _require(original_task_id != successor_task_id, "successor-is-original")
    _require(
        all(refresh._is_sha256(x) for x in (
            original_spec_sha256, original_intent_sha256,
            original_result_sha256, reconciliation_sha256,
        )),
        "original-binding-invalid",
    )
    # Preview must never initialize or migrate the operational database.
    # apply() deliberately supplies its writable StateStore to this reader.
    store = store if store is not None else refresh._default_read_only_authority_store()
    refresh._authority_store_binding(store)
    current = refresh._read_authority_task(store, original_task_id)
    original_state = current["spec"].get("state")
    terminal = current["spec"].get("metadata", {}).get(
        "runtime_historical_unclear_supersession"
    )
    if original_state == "superseded" and isinstance(terminal, dict):
        _require(
            current["revision"] == original_revision + 1
            and terminal.get("original_taskspec_sha256") == original_spec_sha256
            and terminal.get("original_result_sha256") == original_result_sha256
            and terminal.get("reconciliation_sha256") == reconciliation_sha256
            and terminal.get("successor_task_id") == successor_task_id,
            "terminal-replay-mismatch",
        )
        refresh.verify_digest(terminal, "supersession_sha256")
        basis = {
            "schema_version": 1,
            "kind": "bureau_historical_unclear_supersession_preview",
            "status": "ready_for_exact_cas",
            "original_task_id": original_task_id,
            "original_revision": original_revision,
            "original_spec_sha256": original_spec_sha256,
            "successor_task_id": successor_task_id,
            "successor_revision": terminal["successor_revision"],
            "successor_spec_sha256": terminal["successor_spec_sha256"],
            "supersession_sha256": terminal["supersession_sha256"],
            "new_spec_sha256": current["spec_sha256"],
        }
        original_revision_data = refresh._read_authority_task_revision(
            store, original_task_id, original_revision
        )
        _require(original_revision_data["spec_sha256"] == original_spec_sha256,
                 "original-historical-revision-drift")
        return {"status": "already_superseded", "task_id": original_task_id,
                "current_revision": current["revision"],
                "spec_sha256": current["spec_sha256"],
                "terminal": terminal,
                "preview_sha256": refresh.bind_digest(basis, "preview_sha256")["preview_sha256"],
                "idempotent_replay": True}

    historical = _historical_proof(
        store=store, state_root=state_root, source_repository=source_repository,
        original_task_id=original_task_id, original_revision=original_revision,
        original_spec_sha256=original_spec_sha256,
        original_intent_sha256=original_intent_sha256,
        original_result_sha256=original_result_sha256,
        reconciliation_sha256=reconciliation_sha256,
    )
    later = _verified_successor(
        store=store, root=state_root, successor_task_id=successor_task_id,
        source_repository=source_repository,
        healthy_verifier_receipt=healthy_verifier_receipt,
        healthy_verifier_sha256=healthy_verifier_sha256,
        live_verifier=live_verifier,
    )
    _require(
        historical["historical_source_commit"] != later["source_commit"],
        "successor-source-not-newer",
    )
    refresh._prove_source_ancestry(
        ancestor=historical["historical_source_commit"],
        descendant=later["source_commit"],
        source_repository=source_repository,
        error_code="historical-unclear-source-diverged",
        message="verified newer source is not descended from historical unclear install",
    )

    evidence = {
        "schema_version": 1,
        "kind": "bureau_runtime_historical_unclear_supersession",
        "status": "superseded",
        "task_id": original_task_id,
        "original_taskspec_revision": original_revision,
        "original_taskspec_sha256": original_spec_sha256,
        "original_intent_sha256": original_intent_sha256,
        "original_result_sha256": original_result_sha256,
        "reconciliation_sha256": reconciliation_sha256,
        "original_effect_status": "unclear",
        "original_effect_started": True,
        "historical_source_commit": historical["historical_source_commit"],
        "original_bureau_launcher_archived_matches":
            historical["original_bureau_launcher_archived_matches"],
        "successor_task_id": successor_task_id,
        "successor_revision": later["revision"],
        "successor_spec_sha256": later["spec_sha256"],
        "successor_result_sha256": later["runtime_result_sha256"],
        "successor_source_commit": later["source_commit"],
        "successor_manifest_sha256": later["manifest_sha256"],
        "successor_closeout_sha256": later["successor_closeout_sha256"],
        "live_verifier_receipt_sha256": later["live_verifier_receipt_sha256"],
        "does_not_establish": [
            "original_installer_succeeded",
            "original_bureau_launcher_equivalence",
            "original_lease_release",
            "original_result_or_ledger_rewrite",
            "safe_retry_of_original_effect",
            "new_installation_authority",
        ],
    }
    evidence = refresh.bind_digest(evidence, "supersession_sha256")
    new_spec = copy.deepcopy(current["spec"])
    new_spec["state"] = "superseded"
    new_spec["metadata"]["runtime_historical_unclear_supersession"] = evidence
    from .acceptance import AcceptanceContractError
    from .schema_validation import DocumentSchemaError, validate_task_write
    try:
        validate_task_write(new_spec, "TaskSpec:" + original_task_id)
    except (DocumentSchemaError, AcceptanceContractError) as exc:
        raise HistoricalUnclearCloseoutError("superseded-taskspec-invalid") from exc
    material = {
        "schema_version": 1,
        "kind": "bureau_historical_unclear_supersession_preview",
        "status": "ready_for_exact_cas",
        "original_task_id": original_task_id,
        "original_revision": original_revision,
        "original_spec_sha256": original_spec_sha256,
        "successor_task_id": successor_task_id,
        "successor_revision": later["revision"],
        "successor_spec_sha256": later["spec_sha256"],
        "supersession_sha256": evidence["supersession_sha256"],
        "new_spec_sha256": task_specs.task_spec_digest(new_spec),
    }
    return refresh.bind_digest(material, "preview_sha256")


def apply(*, expected_preview_sha256: str, **kwargs: Any) -> dict[str, Any]:
    _require(refresh._is_sha256(expected_preview_sha256),
             "expected-preview-invalid")
    store = kwargs.get("store") or refresh._default_authority_store()
    view = preview(**{**kwargs, "store": store})
    if view.get("idempotent_replay"):
        _require(
            view["preview_sha256"] == expected_preview_sha256,
            "terminal-replay-preview-mismatch",
        )
        return view
    _require(view["preview_sha256"] == expected_preview_sha256,
             "preview-stale-or-mismatched")
    original_task_id = kwargs["original_task_id"]
    successor_task_id = kwargs["successor_task_id"]
    original_revision = kwargs["original_revision"]
    original_spec_sha256 = kwargs["original_spec_sha256"]
    historical = kwargs["reconciliation_sha256"]
    reconciled = kwargs["original_result_sha256"]
    # Preserve the exact preview's proof as source of the candidate, not guessed
    # metadata reconstructed from current mutable globals.
    replacement = _build_candidate_from_preview(
        store=store, kwargs=kwargs, preview_value=view
    )
    key = f"runtime-refresh-historical-unclear-supersession:{original_task_id}:{reconciled}"
    with store.immediate() as connection:
        current = task_specs.get_current(connection, original_task_id)
        successor = task_specs.get_current(connection, successor_task_id)
        _require(
            current is not None
            and current["revision"] == original_revision
            and current["spec_sha256"] == original_spec_sha256
            and current["spec"]["state"] == "ready",
            "original-cas-preimage-changed",
        )
        _require(
            successor is not None
            and successor["revision"] == view["successor_revision"]
            and successor["spec_sha256"] == view["successor_spec_sha256"]
            and successor["spec"]["state"] == "verified",
            "successor-cas-preimage-changed",
        )
        root = kwargs["state_root"].expanduser().resolve()
        immutable_path = (root / "after-effect-unclear-reconciliations"
                          / (kwargs["original_intent_sha256"] + "-" + reconciled + ".json"))
        original_record = refresh.read_json(immutable_path)
        refresh.verify_digest(original_record, "reconciliation_sha256")
        _require(original_record["reconciliation_sha256"] == historical,
                 "original-reconciliation-changed-at-cas")
        original_intent = refresh.read_json(
            root / "intents" / (kwargs["original_intent_sha256"] + ".json")
        )
        refresh.verify_digest(original_intent, "intent_sha256")
        _require(
            original_intent["approval_task_id"] == original_task_id
            and original_intent["intent_sha256"] == kwargs["original_intent_sha256"],
            "original-intent-drift-at-cas",
        )
        old_attempt = root / "attempts" / original_intent["target_sha256"]
        original_result = refresh.read_json(old_attempt / "result.json")
        original_start = refresh.read_json(old_attempt / "started.json")
        refresh.verify_digest(original_result, "result_sha256")
        refresh.verify_digest(original_start, "start_sha256")
        _require(
            original_result["result_sha256"] == reconciled
            and original_result["status"] == "unclear"
            and original_result["effect_started"] is True
            and original_start["start_sha256"] == original_record["original_started_sha256"],
            "original-effect-drift-at-cas",
        )
        original_installer_receipt = Path(original_record["installer_receipt_path"])
        _require(
            original_installer_receipt.is_file()
            and not original_installer_receipt.is_symlink()
            and refresh.sha256_bytes(original_installer_receipt.read_bytes())
                == original_record["installer_receipt_file_sha256"],
            "original-installer-receipt-drift-at-cas",
        )
        healthy_path = kwargs["healthy_verifier_receipt"].expanduser()
        _require(
            healthy_path.is_file() and not healthy_path.is_symlink()
            and refresh.sha256_bytes(healthy_path.read_bytes())
                == kwargs["healthy_verifier_sha256"],
            "verifier-retained-drift-at-cas",
        )
        new_manifest, manifest_sha = refresh.load_manifest(
            Path(refresh.read_json(
                root / "intents" / (
                    successor["spec"]["metadata"]["runtime_closeout"]["intent_sha256"] + ".json"
                )
            )["prefix"]) / "deployment-manifest.json"
        )
        _require(
            manifest_sha == successor["spec"]["metadata"]["runtime_closeout"]["manifest_sha256"]
            and new_manifest["source_commit"] == replacement["metadata"][
                "runtime_historical_unclear_supersession"]["successor_source_commit"],
            "live-source-drift-at-cas",
        )
        result = task_specs.put(
            connection, replacement, idempotency_key=key,
            expected_revision=original_revision,
            source="runtime-refresh-historical-unclear-supersession",
        )
    readback = refresh._read_authority_task(store, original_task_id)
    _require(
        readback["revision"] == result["revision"]
        and readback["spec_sha256"] == result["spec_sha256"]
        and readback["spec"]["state"] == "superseded",
        "supersession-cas-readback-drift",
    )
    return {
        "status": "superseded",
        "task_id": original_task_id,
        "revision": readback["revision"],
        "spec_sha256": readback["spec_sha256"],
        "supersession": readback["spec"]["metadata"][
            "runtime_historical_unclear_supersession"],
        "idempotent_replay": False,
    }


def _build_candidate_from_preview(
    *, store: Any, kwargs: dict[str, Any], preview_value: dict[str, Any]
) -> dict[str, Any]:
    """Reconstruct the exact verified proof and pin its hash to the prior preview."""
    original = refresh._read_authority_task(store, kwargs["original_task_id"])
    root = kwargs["state_root"].expanduser().resolve()
    path = root / "after-effect-unclear-reconciliations" / (
        kwargs["original_intent_sha256"] + "-" + kwargs["original_result_sha256"] + ".json"
    )
    historical = refresh.read_json(path)
    later = _verified_successor(
        store=store, root=root, successor_task_id=kwargs["successor_task_id"],
        source_repository=kwargs["source_repository"],
        healthy_verifier_receipt=kwargs["healthy_verifier_receipt"],
        healthy_verifier_sha256=kwargs["healthy_verifier_sha256"],
        live_verifier=kwargs.get("live_verifier"),
    )
    evidence = refresh.bind_digest({
        "schema_version": 1,
        "kind": "bureau_runtime_historical_unclear_supersession",
        "status": "superseded",
        "task_id": kwargs["original_task_id"],
        "original_taskspec_revision": kwargs["original_revision"],
        "original_taskspec_sha256": kwargs["original_spec_sha256"],
        "original_intent_sha256": kwargs["original_intent_sha256"],
        "original_result_sha256": kwargs["original_result_sha256"],
        "reconciliation_sha256": kwargs["reconciliation_sha256"],
        "original_effect_status": "unclear",
        "original_effect_started": True,
        "historical_source_commit": historical["historical_source_commit"],
        "original_bureau_launcher_archived_matches":
            historical["original_bureau_launcher_archived_matches"],
        "successor_task_id": kwargs["successor_task_id"],
        "successor_revision": later["revision"],
        "successor_spec_sha256": later["spec_sha256"],
        "successor_result_sha256": later["runtime_result_sha256"],
        "successor_source_commit": later["source_commit"],
        "successor_manifest_sha256": later["manifest_sha256"],
        "successor_closeout_sha256": later["successor_closeout_sha256"],
        "live_verifier_receipt_sha256": later["live_verifier_receipt_sha256"],
        "does_not_establish": [
            "original_installer_succeeded",
            "original_bureau_launcher_equivalence",
            "original_lease_release",
            "original_result_or_ledger_rewrite",
            "safe_retry_of_original_effect",
            "new_installation_authority",
        ],
    }, "supersession_sha256")
    _require(evidence["supersession_sha256"] == preview_value["supersession_sha256"],
             "supersession-preview-proof-drift")
    spec = copy.deepcopy(original["spec"])
    spec["state"] = "superseded"
    spec["metadata"]["runtime_historical_unclear_supersession"] = evidence
    _require(task_specs.task_spec_digest(spec) == preview_value["new_spec_sha256"],
             "supersession-preview-task-drift")
    return spec


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Historical UNCLEAR -> SUPERSEDED only; no installer effect"
    )
    p.add_argument("--state-root", type=Path,
                   default=Path.home() / ".local/state/bureau/runtime-refresh")
    p.add_argument("--source-repository", type=Path, required=True)
    p.add_argument("--original-task-id", required=True)
    p.add_argument("--original-revision", type=int, required=True)
    p.add_argument("--original-spec-sha256", required=True)
    p.add_argument("--original-intent-sha256", required=True)
    p.add_argument("--original-result-sha256", required=True)
    p.add_argument("--reconciliation-sha256", required=True)
    p.add_argument("--successor-task-id", required=True)
    p.add_argument("--healthy-verifier-receipt", type=Path, required=True)
    p.add_argument("--healthy-verifier-sha256", required=True)
    p.add_argument("--expected-preview-sha256")
    p.add_argument("--apply", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    p = parser()
    a = p.parse_args(argv)
    kw = {
        "state_root": a.state_root, "source_repository": a.source_repository,
        "original_task_id": a.original_task_id,
        "original_revision": a.original_revision,
        "original_spec_sha256": a.original_spec_sha256,
        "original_intent_sha256": a.original_intent_sha256,
        "original_result_sha256": a.original_result_sha256,
        "reconciliation_sha256": a.reconciliation_sha256,
        "successor_task_id": a.successor_task_id,
        "healthy_verifier_receipt": a.healthy_verifier_receipt,
        "healthy_verifier_sha256": a.healthy_verifier_sha256,
    }
    try:
        if a.apply:
            if not a.expected_preview_sha256:
                p.error("--apply requires --expected-preview-sha256")
            outcome = apply(expected_preview_sha256=a.expected_preview_sha256, **kw)
        else:
            outcome = preview(**kw)
    except (HistoricalUnclearCloseoutError, refresh.RuntimeRefreshError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(outcome, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())