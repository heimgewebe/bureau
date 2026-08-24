"""Typed task closeout for completed direct work that never had a Bureau run.

This path is deliberately narrower than normal run completion. It accepts only
current StateStore-backed ``ready`` TaskSpecs whose complete acceptance contract
uses ``manual_observation`` verifiers, proves that no run exists for the task,
and requires a reviewer distinct from every evidence producer. Preview is
read-only; apply is CAS-bound to the exact preview and stores the full typed
acceptance receipt in ``metadata.verification`` without fabricating a run.
"""

from __future__ import annotations

import copy
import json
import os
import stat
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import legacy, task_specs
from .acceptance import (
    PASSED,
    AcceptanceContractError,
    criterion_contract,
    evaluate_acceptance,
    validate_acceptance_contract,
)
from .v2 import (
    StateStore,
    _authoritative_task_registry_from_connection,
    authoritative_task_registry,
    plan_sha256,
    task_revision_sha256,
    verification_stamp,
)

BUNDLE_KIND = "bureau.task_no_run_acceptance_evidence_bundle"
PREVIEW_KIND = "bureau.task_no_run_closeout_preview"
EVALUATION_KIND = "bureau.task_no_run_acceptance_evaluation"
VERIFICATION_KIND = "bureau.task_no_run_closeout_verification"
RECEIPT_KIND = "bureau.task_no_run_closeout_receipt"
SCHEMA_VERSION = 1
MAX_EVIDENCE_BUNDLE_BYTES = 262_144

_DOES_NOT_ESTABLISH = [
    "run_identity",
    "retroactive_claim_authority",
    "synthetic_run_authority",
    "bureau_execution_of_the_completed_work",
    "merge_authority",
    "deployment_authority",
]


def _utc_now(now: str | None = None) -> str:
    if now is not None:
        try:
            parsed = datetime.fromisoformat(now.replace("Z", "+00:00"))
        except (ValueError, OverflowError, OSError) as exc:
            raise legacy.StateError("task no-run closeout now value is invalid") from exc
        if parsed.tzinfo is None:
            raise legacy.StateError("task no-run closeout now value must be timezone-aware")
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _reviewer(value: str) -> str:
    reviewer = value.strip()
    if not reviewer or len(reviewer) > 200:
        raise legacy.StateError("task no-run closeout reviewer must contain 1..200 characters")
    return reviewer


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)


def _load_bundle(path: Path) -> tuple[dict[str, Any], str]:
    try:
        path_before = path.lstat()
    except OSError as exc:
        raise legacy.StateError(
            f"task no-run closeout evidence bundle is unavailable: {exc}"
        ) from exc
    if not stat.S_ISREG(path_before.st_mode):
        raise legacy.StateError(
            "task no-run closeout evidence bundle must be a regular non-symlink file"
        )
    if path_before.st_size > MAX_EVIDENCE_BUNDLE_BYTES:
        raise legacy.StateError("task no-run closeout evidence bundle exceeds size limit")
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
        opened_before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_before.st_mode)
            or opened_before.st_dev != path_before.st_dev
            or opened_before.st_ino != path_before.st_ino
        ):
            raise legacy.StateError(
                "task no-run closeout evidence bundle changed before descriptor binding"
            )
        chunks: list[bytes] = []
        remaining = MAX_EVIDENCE_BUNDLE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        opened_after = os.fstat(descriptor)
        path_after = path.lstat()
    except legacy.StateError:
        raise
    except OSError as exc:
        raise legacy.StateError(
            f"task no-run closeout evidence bundle is unreadable: {exc}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(raw) > MAX_EVIDENCE_BUNDLE_BYTES:
        raise legacy.StateError("task no-run closeout evidence bundle exceeds size limit")
    identity = _file_identity(opened_after)
    if (
        _file_identity(path_before) != identity
        or _file_identity(opened_before) != identity
        or _file_identity(path_after) != identity
        or len(raw) != opened_after.st_size
    ):
        raise legacy.StateError(
            "task no-run closeout evidence bundle changed while its exact bytes were read"
        )
    try:
        bundle = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise legacy.StateError(
            f"task no-run closeout evidence bundle JSON is invalid: {exc}"
        ) from exc
    if not isinstance(bundle, dict):
        raise legacy.StateError("task no-run closeout evidence bundle must be an object")
    return bundle, legacy.sha256_json(bundle)


def _normalized_evaluation(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": EVALUATION_KIND,
        "task_id": value.get("task_id"),
        "task_sha256": value.get("task_sha256"),
        "plan_sha256": value.get("plan_sha256"),
        "evaluated_at": value.get("evaluated_at"),
        "state": value.get("state"),
        "criteria": value.get("criteria"),
        "cross_criterion_revision": value.get("cross_criterion_revision"),
        "domains": value.get("domains"),
        "does_not_establish": list(_DOES_NOT_ESTABLISH),
    }


def _validate_bundle_binding(
    bundle: Mapping[str, Any],
    *,
    task_id: str,
    task_sha256: str,
    plan_sha256_value: str,
    criterion_ids: list[str],
) -> Mapping[str, Any]:
    expected = {
        "schema_version": SCHEMA_VERSION,
        "kind": BUNDLE_KIND,
        "task_id": task_id,
        "task_sha256": task_sha256,
        "plan_sha256": plan_sha256_value,
    }
    for key, value in expected.items():
        if bundle.get(key) != value:
            raise legacy.StateError(f"task no-run closeout evidence bundle binding mismatch: {key}")
    evidence = bundle.get("evidence")
    if not isinstance(evidence, Mapping):
        raise legacy.StateError("task no-run closeout evidence must be an object")
    observed_ids = {str(key) for key in evidence}
    expected_ids = set(criterion_ids)
    if observed_ids != expected_ids:
        missing = sorted(expected_ids - observed_ids)
        extra = sorted(observed_ids - expected_ids)
        raise legacy.StateError(
            "task no-run closeout evidence criterion set mismatch"
            f"; missing={missing}; extra={extra}"
        )
    return evidence


def _manual_authentications(
    criteria: list[Mapping[str, Any]],
    evidence: Mapping[str, Any],
    reviewer: str,
) -> dict[str, dict[str, Any]]:
    authenticated: dict[str, dict[str, Any]] = {}
    for criterion in criteria:
        criterion_id = str(criterion["id"])
        contract = criterion_contract(criterion)
        if contract is None:
            raise legacy.StateError(f"task no-run closeout criterion {criterion_id!r} is not typed")
        if contract.get("verifier") != "manual_observation":
            raise legacy.StateError(
                "task no-run closeout supports only manual_observation criteria; "
                f"criterion {criterion_id!r} uses {contract.get('verifier')!r}"
            )
        item = evidence.get(criterion_id)
        if not isinstance(item, Mapping):
            raise legacy.StateError(
                f"task no-run closeout evidence for {criterion_id!r} is missing"
            )
        facts = item.get("facts")
        observer = facts.get("observer") if isinstance(facts, Mapping) else None
        if not isinstance(observer, str) or not observer.strip():
            raise legacy.StateError(
                f"task no-run closeout evidence producer for {criterion_id!r} is missing"
            )
        observer = observer.strip()
        if observer == reviewer:
            raise legacy.StateError(
                "task no-run closeout reviewer must differ from every evidence producer"
            )
        config = contract.get("verifier_config")
        assert isinstance(config, Mapping)
        authenticated[criterion_id] = {
            "schema_version": SCHEMA_VERSION,
            "kind": "bureau.task_no_run_source_authentication",
            "criterion_id": criterion_id,
            "verifier": "manual_observation",
            "authority": "manual",
            "reviewer": reviewer,
            "observer": observer,
            "observation_scope": config.get("observation_scope"),
            "evidence_sha256": legacy.sha256_json(dict(item)),
        }
    return authenticated


def _preview_from_connection(
    registry: Any,
    connection: Any,
    *,
    task_id: str,
    evidence_path: Path,
    reviewer: str,
    now: str | None,
) -> dict[str, Any]:
    effective, authority, revisions = _authoritative_task_registry_from_connection(
        registry, connection
    )
    if task_id not in effective.tasks:
        raise legacy.StateError(f"unknown task {task_id}")
    task = effective.tasks[task_id]
    revision = revisions.get(task_id)
    if not isinstance(revision, dict) or not isinstance(revision.get("revision"), int):
        raise legacy.StateError(
            "task no-run closeout requires an authoritative StateStore TaskSpec revision"
        )
    existing_verification = task.raw.get("metadata", {}).get("verification", {})
    replaying = (
        task.state == "verified"
        and isinstance(existing_verification, Mapping)
        and existing_verification.get("kind") == VERIFICATION_KIND
    )
    if task.state != "ready" and not replaying:
        raise legacy.StateError(
            f"task no-run closeout requires ready state; observed {task.state!r}"
        )
    runs = connection.execute(
        "SELECT run_id,state FROM runs WHERE task_id=? ORDER BY run_id", (task_id,)
    ).fetchall()
    if runs:
        raise legacy.StateError(
            f"task no-run closeout refuses task {task_id} because {len(runs)} Bureau run(s) exist"
        )
    status_row = connection.execute(
        "SELECT task_sha256,plan_sha256,state,receipt_sha256 FROM task_status WHERE task_id=?",
        (task_id,),
    ).fetchone()
    if status_row is not None and not replaying:
        raise legacy.StateError(
            "task no-run closeout refuses an existing task_status completion projection"
        )
    try:
        validate_acceptance_contract(task.raw)
    except AcceptanceContractError as exc:
        raise legacy.StateError(
            f"task no-run closeout acceptance contract is invalid: {exc}"
        ) from exc
    criteria = list(task.raw.get("acceptance", []))
    if not criteria:
        raise legacy.StateError("task no-run closeout requires at least one acceptance criterion")
    criterion_ids = [str(item["id"]) for item in criteria]
    stable_task_sha256 = task_revision_sha256(task.raw)
    current_plan_sha256 = plan_sha256(effective, task.initiative)
    bundle, bundle_sha256 = _load_bundle(evidence_path)
    evidence = _validate_bundle_binding(
        bundle,
        task_id=task_id,
        task_sha256=stable_task_sha256,
        plan_sha256_value=current_plan_sha256,
        criterion_ids=criterion_ids,
    )
    authentications = _manual_authentications(criteria, evidence, reviewer)
    evaluated = evaluate_acceptance(
        criteria,
        evidence,
        task_id=task_id,
        run_id="",
        task_sha256=stable_task_sha256,
        plan_sha256=current_plan_sha256,
        now=_utc_now(now),
        authenticated_criterion_ids=set(authentications),
    )
    evaluation = _normalized_evaluation(evaluated)
    if evaluation.get("state") != PASSED:
        reasons = [
            f"{item.get('criterion_id')}:{item.get('reason')}"
            for item in evaluation.get("criteria", [])
            if item.get("state") != PASSED
        ]
        raise legacy.StateError(
            "task no-run closeout acceptance is not fully passed: " + ", ".join(reasons)
        )
    basis_revision = revision["revision"]
    basis_spec_sha256 = revision["spec_sha256"]
    if replaying:
        prior_receipt = existing_verification.get("receipt")
        if not isinstance(prior_receipt, Mapping):
            raise legacy.StateError("task no-run closeout existing verification receipt is missing")
        stored_receipt_sha256 = prior_receipt.get("receipt_sha256")
        unsigned_receipt = {
            key: value for key, value in prior_receipt.items() if key != "receipt_sha256"
        }
        if (
            not isinstance(stored_receipt_sha256, str)
            or legacy.sha256_json(unsigned_receipt) != stored_receipt_sha256
            or existing_verification.get("receipt_sha256") != stored_receipt_sha256
        ):
            raise legacy.StateError(
                "task no-run closeout existing verification receipt integrity mismatch"
            )
        basis_revision = prior_receipt.get("task_spec_revision")
        basis_spec_sha256 = prior_receipt.get("task_spec_sha256")
        if not isinstance(basis_revision, int) or not isinstance(basis_spec_sha256, str):
            raise legacy.StateError(
                "task no-run closeout existing verification preimage binding is invalid"
            )
        if (
            status_row is None
            or status_row["task_sha256"] != stable_task_sha256
            or status_row["plan_sha256"] != current_plan_sha256
            or status_row["state"] != "verified"
            or status_row["receipt_sha256"] != stored_receipt_sha256
        ):
            raise legacy.StateError(
                "task no-run closeout existing task_status projection binding mismatch"
            )
    preview_basis = {
        "schema_version": SCHEMA_VERSION,
        "kind": PREVIEW_KIND,
        "task_id": task_id,
        "task_spec_revision": basis_revision,
        "task_spec_sha256": basis_spec_sha256,
        "task_sha256": stable_task_sha256,
        "plan_sha256": current_plan_sha256,
        "evidence_bundle_sha256": bundle_sha256,
        "criterion_ids": criterion_ids,
        "reviewer": reviewer,
        "target_state": "verified",
        "no_run_proven": True,
        "authority_kind": authority.get("kind"),
        "does_not_establish": list(_DOES_NOT_ESTABLISH),
    }
    preview_sha256 = legacy.sha256_json(preview_basis)
    return {
        **preview_basis,
        "status": "already-verified" if replaying else "ready-to-apply",
        "preview_sha256": preview_sha256,
        "observed_at": _utc_now(now),
        "evaluation": evaluation,
        "authentications": authentications,
        "bundle": bundle,
        "existing_verification": dict(existing_verification) if replaying else None,
    }


def preview_task_no_run_closeout(
    registry: Any,
    store: StateStore,
    task_id: str,
    evidence_path: Path,
    *,
    reviewer: str,
    now: str | None = None,
) -> dict[str, Any]:
    reviewer_value = _reviewer(reviewer)
    with store.connect() as connection:
        preview = _preview_from_connection(
            registry,
            connection,
            task_id=task_id,
            evidence_path=evidence_path,
            reviewer=reviewer_value,
            now=now,
        )
    return {
        key: value
        for key, value in preview.items()
        if key not in {"bundle", "existing_verification"}
    }


def _receipt(
    preview: Mapping[str, Any], *, reviewer: str, closed_at: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    evaluation = dict(preview["evaluation"])
    authentications = copy.deepcopy(preview["authentications"])
    bundle = copy.deepcopy(preview["bundle"])
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "kind": RECEIPT_KIND,
        "status": "verified",
        "task_id": preview["task_id"],
        "task_spec_revision": preview["task_spec_revision"],
        "task_spec_sha256": preview["task_spec_sha256"],
        "task_sha256": preview["task_sha256"],
        "plan_sha256": preview["plan_sha256"],
        "preview_sha256": preview["preview_sha256"],
        "evidence_bundle_sha256": preview["evidence_bundle_sha256"],
        "reviewer": reviewer,
        "closed_at": closed_at,
        "evidence": bundle["evidence"],
        "evaluation": evaluation,
        "authentications": authentications,
        "does_not_establish": list(_DOES_NOT_ESTABLISH),
    }
    receipt_sha256 = legacy.sha256_json(receipt)
    verification = {
        "schema_version": SCHEMA_VERSION,
        "kind": VERIFICATION_KIND,
        "task_sha256": preview["task_sha256"],
        "plan_sha256": preview["plan_sha256"],
        "receipt_sha256": receipt_sha256,
        "preview_sha256": preview["preview_sha256"],
        "evidence_bundle_sha256": preview["evidence_bundle_sha256"],
        "reviewer": reviewer,
        "closed_at": closed_at,
        "receipt": {**receipt, "receipt_sha256": receipt_sha256},
        "does_not_establish": list(_DOES_NOT_ESTABLISH),
    }
    return receipt, verification


def _validated_replay(
    preview: Mapping[str, Any], expected_preview_sha256: str
) -> dict[str, Any] | None:
    existing = preview.get("existing_verification")
    if not isinstance(existing, Mapping):
        return None
    if existing.get("preview_sha256") != expected_preview_sha256:
        raise legacy.StateError("task no-run closeout existing verification preview mismatch")
    receipt = existing.get("receipt")
    if not isinstance(receipt, Mapping):
        raise legacy.StateError("task no-run closeout existing verification receipt is missing")
    stored_sha256 = receipt.get("receipt_sha256")
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if not isinstance(stored_sha256, str) or legacy.sha256_json(unsigned) != stored_sha256:
        raise legacy.StateError(
            "task no-run closeout existing verification receipt integrity mismatch"
        )
    if existing.get("receipt_sha256") != stored_sha256:
        raise legacy.StateError("task no-run closeout existing verification digest mismatch")
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": RECEIPT_KIND,
        "status": "verified",
        "task_id": preview["task_id"],
        "idempotent": True,
        "receipt_sha256": stored_sha256,
        "verification": dict(existing),
        "preview_sha256": expected_preview_sha256,
        "does_not_establish": list(_DOES_NOT_ESTABLISH),
    }


def apply_task_no_run_closeout(
    registry: Any,
    store: StateStore,
    task_id: str,
    evidence_path: Path,
    *,
    reviewer: str,
    expected_preview_sha256: str,
    now: str | None = None,
) -> dict[str, Any]:
    reviewer_value = _reviewer(reviewer)
    if len(expected_preview_sha256) != 64 or any(
        char not in "0123456789abcdef" for char in expected_preview_sha256
    ):
        raise legacy.StateError("task no-run closeout expected preview sha256 is invalid")
    closed_at = _utc_now(now)
    with store.immediate() as connection:
        preview = _preview_from_connection(
            registry,
            connection,
            task_id=task_id,
            evidence_path=evidence_path,
            reviewer=reviewer_value,
            now=closed_at,
        )
        if preview["preview_sha256"] != expected_preview_sha256:
            raise legacy.StateError("task no-run closeout preview changed before apply")
        replay = _validated_replay(preview, expected_preview_sha256)
        if replay is not None:
            return replay
        current = task_specs.get_current(connection, task_id)
        if current is None:
            raise legacy.StateError("task no-run closeout TaskSpec disappeared before apply")
        if (
            current["revision"] != preview["task_spec_revision"]
            or current["spec_sha256"] != preview["task_spec_sha256"]
            or current["spec"].get("state") != "ready"
        ):
            raise legacy.StateError("task no-run closeout TaskSpec changed before apply")
        receipt, verification = _receipt(preview, reviewer=reviewer_value, closed_at=closed_at)
        mutated = copy.deepcopy(current["spec"])
        mutated["state"] = "verified"
        metadata = mutated.setdefault("metadata", {})
        metadata["verification"] = verification
        try:
            changed = task_specs.put(
                connection,
                mutated,
                idempotency_key=(f"task-no-run-closeout:{task_id}:{expected_preview_sha256}"),
                expected_revision=int(current["revision"]),
                source="task-no-run-closeout",
            )
        except task_specs.TaskSpecError as exc:
            raise legacy.StateError(str(exc)) from exc
        readback = task_specs.get_current(connection, task_id)
        if readback is None or readback["revision"] != changed["revision"]:
            raise legacy.StateError("task no-run closeout TaskSpec readback revision mismatch")
        if readback["spec"].get("state") != "verified":
            raise legacy.StateError("task no-run closeout TaskSpec readback is not verified")
        if readback["spec"].get("metadata", {}).get("verification") != verification:
            raise legacy.StateError("task no-run closeout verification readback mismatch")
        connection.execute(
            """
            INSERT INTO task_status(
                task_id,task_sha256,plan_sha256,state,receipt_sha256,updated_at
            ) VALUES(?,?,?,'verified',?,?)
            """,
            (
                task_id,
                preview["task_sha256"],
                preview["plan_sha256"],
                verification["receipt_sha256"],
                closed_at,
            ),
        )
        status_readback = connection.execute(
            "SELECT task_sha256,plan_sha256,state,receipt_sha256 FROM task_status WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if (
            status_readback is None
            or status_readback["task_sha256"] != preview["task_sha256"]
            or status_readback["plan_sha256"] != preview["plan_sha256"]
            or status_readback["state"] != "verified"
            or status_readback["receipt_sha256"] != verification["receipt_sha256"]
        ):
            raise legacy.StateError("task no-run closeout task_status readback mismatch")
    effective, _, _ = authoritative_task_registry(registry, store)
    stamp = verification_stamp(effective, store, task_id)
    if (
        stamp.get("task_sha256") != preview["task_sha256"]
        or stamp.get("plan_sha256") != preview["plan_sha256"]
        or stamp.get("receipt_sha256") != verification["receipt_sha256"]
    ):
        raise legacy.StateError("task no-run closeout consumer verification readback mismatch")
    return {
        **receipt,
        "receipt_sha256": verification["receipt_sha256"],
        "idempotent": False,
        "resulting_task_spec_revision": changed["revision"],
        "resulting_task_spec_sha256": changed["spec_sha256"],
        "verification": verification,
        "verification_stamp": stamp,
    }
