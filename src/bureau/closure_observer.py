from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

from bureau import legacy, runtime_refresh, state_events
from bureau.acceptance import (
    EVIDENCE_KIND,
    PASSED,
    AcceptanceContractError,
    criterion_contract,
    evaluate_acceptance,
    validate_acceptance_contract,
)
from bureau.v2 import (
    TERMINAL_STATES,
    RunStateConflict,
    StateStore,
    _complete_run_after_typed_evaluation,
    _load_validated_stored_receipt,
)

ACTIVE_CLOSEOUT_STATES = {"assigned", "running", "verifying"}
TERMINAL_RUN_STATES = set(TERMINAL_STATES)
EVIDENCE_BUNDLE_KIND = "bureau.acceptance_evidence_bundle"
EVIDENCE_DIRECTORY = "acceptance-evidence"
EVIDENCE_QUARANTINE_DIRECTORY = "acceptance-evidence-quarantine"
MAX_EVIDENCE_BUNDLE_BYTES = 1_048_576
INVALID_ACCEPTANCE_DIAGNOSTIC_KIND = "bureau.invalid_acceptance_contract_diagnostic"
MAX_MANUAL_REVIEWER_LENGTH = 200
MAX_MANUAL_AUTHENTICATION_EVENTS_PER_RUN = 1000

Completion = Callable[[Any, StateStore, str, dict[str, Any]], dict[str, Any]]
EvidenceProvider = Callable[[dict[str, Any], dict[str, Any]], Mapping[str, Any]]
AuthenticationRecords = Mapping[str, Mapping[str, Any]]
AuthenticationProvider = Callable[
    [dict[str, Any], dict[str, Any], Mapping[str, Any]], AuthenticationRecords
]
GitHubReader = Callable[[list[str]], Any]
PRODUCTION_AUTHENTICATION_ADAPTERS = {
    "code_merged": "runtime_refresh.gh_json",
    "required_ci_green": "runtime_refresh.gh_json",
}


class EvidenceAdapterUnavailable(RuntimeError):
    """A primary evidence adapter could not produce an authoritative observation."""

    def __init__(
        self,
        *,
        authority: str,
        adapter: str,
        target: Mapping[str, Any],
        detail: str,
    ) -> None:
        super().__init__(detail)
        self.authority = authority
        self.adapter = adapter
        self.target = dict(target)
        self.detail = detail

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "bureau.evidence_adapter_unavailable",
            "authority": self.authority,
            "adapter": self.adapter,
            "target": self.target,
            "detail": self.detail,
            "does_not_establish": [
                "evidence-rejection",
                "evidence-authenticity",
                "task-completion",
            ],
        }


def _load_envelope(
    store: StateStore, run_id: str, expected_sha256: str | None
) -> dict[str, Any]:
    path = store.envelope_path(run_id)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"run envelope is unreadable: {type(exc).__name__}: {exc}") from exc
    if not isinstance(value, dict) or value.get("run_id") != run_id:
        raise ValueError("run envelope does not match run id")
    task = value.get("task")
    if not isinstance(task, dict) or task.get("id") != value.get("task_id"):
        raise ValueError("run envelope task binding is invalid")
    if not isinstance(expected_sha256, str) or legacy.sha256_json(value) != expected_sha256:
        raise ValueError("run envelope integrity mismatch")
    return value


def _invalid_acceptance_observation(
    run: Mapping[str, Any],
    task: Mapping[str, Any],
    error: AcceptanceContractError,
) -> dict[str, Any]:
    repair_action = (
        "Register a new TaskSpec revision with non-empty executable typed acceptance, "
        "then explicitly disposition this legacy run through the normal run lifecycle "
        "and create a new revision-bound run; do not edit the active envelope or treat "
        "this diagnostic as terminal evidence."
    )
    diagnostic: dict[str, Any] = {
        "schema_version": 1,
        "kind": INVALID_ACCEPTANCE_DIAGNOSTIC_KIND,
        "task_id": str(run.get("task_id") or task.get("id") or ""),
        "run_id": str(run.get("run_id") or ""),
        "task_sha256": run.get("task_sha256"),
        "plan_sha256": run.get("plan_sha256"),
        "diagnostics": error.diagnostics,
        "repair_action": repair_action,
        "does_not_establish": [
            "task-completion",
            "terminal-run-state",
            "permission-to-edit-active-envelope",
        ],
    }
    diagnostic["fingerprint"] = legacy.sha256_json(diagnostic)
    return {
        "schema_version": 1,
        "kind": "bureau.closeout_observation",
        "run_id": run.get("run_id"),
        "task_id": run.get("task_id"),
        "state": "open",
        "reason": "invalid-acceptance-contract",
        "diagnostic": diagnostic,
        "mutated": False,
    }


def evaluate_run(
    store: StateStore,
    run_id: str,
    evidence: Mapping[str, Any],
    *,
    now: str | None = None,
    authenticated_criterion_ids: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    run = store.run(run_id)
    if run.get("state") in TERMINAL_RUN_STATES:
        return {
            "schema_version": 1,
            "kind": "bureau.closeout_observation",
            "run_id": run_id,
            "task_id": run.get("task_id"),
            "state": "already_terminal",
            "mutated": False,
            "receipt": store.receipt(run_id),
        }
    if run.get("state") not in ACTIVE_CLOSEOUT_STATES:
        return {
            "schema_version": 1,
            "kind": "bureau.closeout_observation",
            "run_id": run_id,
            "task_id": run.get("task_id"),
            "state": "open",
            "reason": f"run-state-not-closeout-eligible:{run.get('state')}",
            "mutated": False,
        }
    try:
        envelope = _load_envelope(store, run_id, run.get("envelope_sha256"))
    except ValueError as exc:
        return {
            "schema_version": 1,
            "kind": "bureau.closeout_observation",
            "run_id": run_id,
            "task_id": run.get("task_id"),
            "state": "open",
            "reason": "envelope-unreadable",
            "detail": str(exc),
            "mutated": False,
        }
    task = envelope["task"]
    try:
        validate_acceptance_contract(task)
    except AcceptanceContractError as exc:
        return _invalid_acceptance_observation(run, task, exc)
    criteria = task.get("acceptance")
    if not isinstance(criteria, list):
        criteria = []
    evaluation = evaluate_acceptance(
        criteria,
        evidence,
        task_id=str(run["task_id"]),
        run_id=run_id,
        task_sha256=str(run["task_sha256"]),
        plan_sha256=run.get("plan_sha256"),
        now=now,
        authenticated_criterion_ids=authenticated_criterion_ids,
    )
    return {
        "schema_version": 1,
        "kind": "bureau.closeout_observation",
        "run_id": run_id,
        "task_id": run.get("task_id"),
        "state": "ready_to_terminalize" if evaluation["state"] == PASSED else "open",
        "mutated": False,
        "evaluation": evaluation,
    }


def reconcile_run(
    registry: Any,
    store: StateStore,
    run_id: str,
    evidence: Mapping[str, Any],
    *,
    now: str | None = None,
    completion: Completion = _complete_run_after_typed_evaluation,
    authenticated_criterion_ids: set[str] | frozenset[str] | None = None,
    authentication_records: AuthenticationRecords | None = None,
) -> dict[str, Any]:
    observed = evaluate_run(
        store,
        run_id,
        evidence,
        now=now,
        authenticated_criterion_ids=authenticated_criterion_ids,
    )
    if observed["state"] == "already_terminal":
        return observed
    evaluation = observed.get("evaluation")
    if not isinstance(evaluation, dict) or evaluation.get("automatic_terminalization") is not True:
        return observed

    # The typed evaluator is deliberately not a second writer. It authorizes
    # exactly one call into the existing CAS/idempotent closeout path. That path
    # re-reads the authoritative TaskSpec/plan baseline before mutating state.
    completion_evidence: dict[str, Any] = {}
    authentication_records = authentication_records or {}
    for criterion_id, item in evidence.items():
        if isinstance(item, Mapping):
            copied = dict(item)
            authentication = authentication_records.get(str(criterion_id))
            if isinstance(authentication, Mapping):
                copied["_source_authentication"] = dict(authentication)
            completion_evidence[str(criterion_id)] = copied
        else:
            completion_evidence[str(criterion_id)] = item
    completed = completion(registry, store, run_id, completion_evidence)
    return {
        **observed,
        "state": "terminalized",
        "mutated": not bool(completed.get("idempotent")),
        "completion": completed,
    }


def reconcile_runs(
    registry: Any,
    store: StateStore,
    evidence_provider: EvidenceProvider,
    *,
    run_ids: list[str] | None = None,
    now: str | None = None,
    completion: Completion = _complete_run_after_typed_evaluation,
    authentication_provider: AuthenticationProvider | None = None,
) -> dict[str, Any]:
    if run_ids is None:
        run_ids = [
            str(run["run_id"])
            for run in store.list_runs()
            if run.get("state") in ACTIVE_CLOSEOUT_STATES
        ]
    observations: list[dict[str, Any]] = []
    for run_id in run_ids:
        run = store.run(run_id)
        try:
            envelope = _load_envelope(store, run_id, run.get("envelope_sha256"))
            task = envelope.get("task")
            if not isinstance(task, Mapping):
                raise ValueError("run envelope task is not an object")
            try:
                validate_acceptance_contract(task)
            except AcceptanceContractError as exc:
                observations.append(_invalid_acceptance_observation(run, task, exc))
                continue
            evidence = evidence_provider(run, envelope)
        except Exception as exc:  # source outages are unknown, never success/failure
            observations.append(
                {
                    "schema_version": 1,
                    "kind": "bureau.closeout_observation",
                    "run_id": run_id,
                    "task_id": run.get("task_id"),
                    "state": "open",
                    "reason": "evidence-provider-unavailable",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "mutated": False,
                }
            )
            continue
        if not isinstance(evidence, Mapping):
            observations.append(
                {
                    "schema_version": 1,
                    "kind": "bureau.closeout_observation",
                    "run_id": run_id,
                    "task_id": run.get("task_id"),
                    "state": "open",
                    "reason": "evidence-provider-returned-non-object",
                    "mutated": False,
                }
            )
            continue
        authentication_records: AuthenticationRecords = {}
        if authentication_provider is not None:
            try:
                authentication_records = authentication_provider(run, envelope, evidence)
            except EvidenceAdapterUnavailable as exc:
                observations.append(
                    {
                        "schema_version": 1,
                        "kind": "bureau.closeout_observation",
                        "run_id": run_id,
                        "task_id": run.get("task_id"),
                        "state": "open",
                        "reason": "evidence-adapter-unavailable",
                        "adapter": exc.payload(),
                        "mutated": False,
                    }
                )
                continue
            except Exception as exc:
                observations.append(
                    {
                        "schema_version": 1,
                        "kind": "bureau.closeout_observation",
                        "run_id": run_id,
                        "task_id": run.get("task_id"),
                        "state": "open",
                        "reason": "evidence-authentication-provider-unavailable",
                        "detail": f"{type(exc).__name__}: {exc}",
                        "mutated": False,
                    }
                )
                continue
        authenticated_ids = frozenset(authentication_records)
        try:
            observation = reconcile_run(
                registry,
                store,
                run_id,
                evidence,
                now=now,
                completion=completion,
                authenticated_criterion_ids=authenticated_ids,
                authentication_records=authentication_records,
            )
        except RunStateConflict as exc:
            observations.append(
                {
                    "schema_version": 1,
                    "kind": "bureau.closeout_observation",
                    "run_id": run_id,
                    "task_id": run.get("task_id"),
                    "state": "open",
                    "reason": "completion-conflict",
                    "conflict": exc.payload(),
                    "mutated": False,
                }
            )
            continue
        observations.append(observation)
    terminalized = sum(item.get("state") == "terminalized" for item in observations)
    return {
        "schema_version": 1,
        "kind": "bureau.closeout_reconcile",
        "observed_run_count": len(observations),
        "terminalized_count": terminalized,
        "open_count": sum(item.get("state") == "open" for item in observations),
        "observations": observations,
        "does_not_establish": [
            "evidence-source-availability",
            "merge-authority",
            "deployment-authority",
        ],
    }


def _evidence_directory(store: StateStore) -> Any | None:
    root = store.state_root / EVIDENCE_DIRECTORY
    try:
        metadata = root.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("acceptance evidence root must be a real non-symlink directory")
    return root


def _evidence_bundle_path(store: StateStore, run_id: str) -> Any | None:
    root = _evidence_directory(store)
    if root is None:
        return None
    return root / f"{run_id}.json"


def _evidence_quarantine_path(store: StateStore) -> Any:
    return store.state_root.with_name(
        f"{store.state_root.name}-{EVIDENCE_QUARANTINE_DIRECTORY}"
    )


def _evidence_quarantine_directory(store: StateStore) -> Any:
    root = _evidence_quarantine_path(store)
    try:
        metadata = root.lstat()
    except FileNotFoundError:
        root.mkdir(mode=0o700)
        metadata = root.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(
            "acceptance evidence quarantine root must be a real non-symlink directory"
        )
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValueError("acceptance evidence quarantine root must be private")
    return root


def _read_unknown_run_evidence_entry(
    entry: Any,
) -> tuple[bytes, Mapping[str, Any], str, tuple[int, int, int]]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(entry, flags)
    except OSError as exc:
        raise ValueError(
            f"unknown-run acceptance evidence is unreadable: {type(exc).__name__}: {exc}"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(
                "unknown-run acceptance evidence must be a regular non-symlink file"
            )
        if metadata.st_size > MAX_EVIDENCE_BUNDLE_BYTES:
            raise ValueError("unknown-run acceptance evidence exceeds size limit")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            payload = handle.read(MAX_EVIDENCE_BUNDLE_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(payload) > MAX_EVIDENCE_BUNDLE_BYTES:
        raise ValueError("unknown-run acceptance evidence exceeds size limit")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"unknown-run acceptance evidence is unreadable: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(value, Mapping):
        raise ValueError("unknown-run acceptance evidence must be a JSON object")
    identity = (metadata.st_dev, metadata.st_ino, metadata.st_nlink)
    return payload, value, hashlib.sha256(payload).hexdigest(), identity


def _read_matching_quarantine_entry(
    path: Any, payload: bytes, source_sha256: str
) -> None:
    existing, _, existing_sha256, _ = _read_unknown_run_evidence_entry(path)
    if existing != payload or existing_sha256 != source_sha256:
        raise ValueError("acceptance evidence quarantine digest collision")


def _canonical_quarantine_path(root: Any, entry: Any, source_sha256: str) -> Any:
    source_name_sha256 = hashlib.sha256(entry.name.encode("utf-8")).hexdigest()[:16]
    return root / f"{source_name_sha256}.{source_sha256}.json"


def _reserve_quarantine_staging(root: Any, source_sha256: str) -> Any:
    descriptor, staging_name = tempfile.mkstemp(
        prefix=f".incoming-{source_sha256[:16]}-",
        suffix=".json",
        dir=root,
    )
    os.close(descriptor)
    return Path(staging_name)


def _publish_quarantine_destination(
    root: Any,
    entry: Any,
    staging: Any,
    payload: bytes,
    source_sha256: str,
) -> tuple[Any, Any, bool]:
    canonical = _canonical_quarantine_path(root, entry, source_sha256)
    try:
        os.link(staging, canonical, follow_symlinks=False)
    except FileExistsError:
        _read_matching_quarantine_entry(canonical, payload, source_sha256)
        # Preserve the reappeared source inode independently; its content may
        # match the canonical copy while its producer history is distinct.
        return staging, canonical, True
    except OSError as exc:
        raise ValueError(
            f"acceptance evidence quarantine publication failed; preserved-at:{staging}"
        ) from exc
    with suppress(OSError):
        staging.unlink()
    return canonical, canonical, False


def _atomic_quarantine_move(source: Any, destination: Any) -> None:
    os.replace(source, destination)


def _restore_mismatched_quarantine_entry(destination: Any, entry: Any) -> bool:
    try:
        os.link(destination, entry, follow_symlinks=False)
    except (FileExistsError, OSError):
        return False
    # The active path is already restored. Keeping an additional private
    # quarantine link is evidenzerhaltend and safer than a second mutation.
    with suppress(OSError):
        destination.unlink()
    return True


def _quarantine_unknown_run_evidence(
    store: StateStore, entry: Any, run_id: str
) -> dict[str, Any]:
    payload, _, source_sha256, source_identity = _read_unknown_run_evidence_entry(entry)
    root = _evidence_quarantine_directory(store)
    staging = _reserve_quarantine_staging(root, source_sha256)

    try:
        # Move the active pathname into a private staging inode atomically. The
        # canonical content-addressed name is not exposed until this moved inode
        # has been revalidated, so crashes cannot leave a partial canonical
        # quarantine artifact.
        _atomic_quarantine_move(entry, staging)
    except Exception:
        with suppress(OSError):
            staging.unlink()
        raise

    try:
        moved_payload, moved_value, moved_sha256, moved_identity = (
            _read_unknown_run_evidence_entry(staging)
        )
    except Exception as exc:
        restored = _restore_mismatched_quarantine_entry(staging, entry)
        state = "restored" if restored else f"preserved-at:{staging}"
        raise ValueError(
            f"unknown-run acceptance evidence changed during quarantine ({state})"
        ) from exc

    if (
        moved_identity[:2] != source_identity[:2]
        or moved_payload != payload
        or moved_sha256 != source_sha256
    ):
        restored = _restore_mismatched_quarantine_entry(staging, entry)
        state = "restored" if restored else f"preserved-at:{staging}"
        raise ValueError(
            f"unknown-run acceptance evidence changed during quarantine ({state})"
        )

    # Keep the moved inode private before publication. When it has no other
    # hard links, narrow its mode; the 0700 quarantine parent remains the
    # boundary for pre-existing hard links.
    if moved_identity[2] == 1:
        os.chmod(staging, 0o600, follow_symlinks=False)

    destination, canonical, idempotent_replay = _publish_quarantine_destination(
        root, entry, staging, moved_payload, source_sha256
    )

    declared_run_id = moved_value.get("run_id")
    canonical_bundle = (
        moved_value.get("schema_version") == 1
        and moved_value.get("kind") == EVIDENCE_BUNDLE_KIND
        and isinstance(declared_run_id, str)
        and bool(declared_run_id)
    )
    return {
        "run_id": run_id,
        "source_name": entry.name,
        "source_sha256": source_sha256,
        "reason": "unknown-run",
        "classification": (
            "unknown-run-bundle" if canonical_bundle else "unknown-run-json-residue"
        ),
        "declared_run_id": declared_run_id if isinstance(declared_run_id, str) else None,
        "quarantine_path": str(destination),
        "canonical_quarantine_path": str(canonical),
        "idempotent_replay": idempotent_replay,
    }


def retire_terminal_evidence_bundles(registry: Any, store: StateStore) -> dict[str, Any]:
    """Retire validated producer bundles once their run is terminal.

    Succeeded runs require a schema-, digest-, and run-bound durable receipt
    before the producer bundle is removed. Failed, cancelled, and orphaned runs
    are already terminal in the authoritative StateStore, so their producer
    bundles may be retired after the claim-bound envelope and bundle bindings
    validate. Safely classifiable JSON entries whose filename names no
    authoritative run are moved to a content-addressed quarantine. Malformed,
    unsafe, and nonterminal entries remain preserved in place.
    """

    root = store.state_root / EVIDENCE_DIRECTORY
    result: dict[str, Any] = {
        "schema_version": 1,
        "kind": "bureau.acceptance_evidence_retirement",
        "directory": str(root),
        "retired_count": 0,
        "retired_run_ids": [],
        "quarantined_count": 0,
        "quarantined_entries": [],
        "quarantine_directory": str(_evidence_quarantine_path(store)),
        "preserved_count": 0,
        "errors": [],
    }
    try:
        validated_root = _evidence_directory(store)
        if validated_root is None:
            return result
        entries = sorted(validated_root.iterdir(), key=lambda item: item.name)
    except (OSError, ValueError) as exc:
        result["errors"].append(f"{type(exc).__name__}: {exc}")
        return result

    for entry in entries:
        if entry.suffix != ".json":
            result["preserved_count"] += 1
            continue
        run_id = entry.stem
        try:
            run = store.run(run_id)
        except legacy.StateError as exc:
            if str(exc) != f"unknown run {run_id}":
                result["preserved_count"] += 1
                result["errors"].append(f"{run_id}: {type(exc).__name__}: {exc}")
                continue
            try:
                quarantine = _quarantine_unknown_run_evidence(store, entry, run_id)
            except Exception as quarantine_exc:
                result["preserved_count"] += 1
                result["errors"].append(
                    f"{run_id}: {type(quarantine_exc).__name__}: {quarantine_exc}"
                )
                continue
            result["quarantined_count"] += 1
            result["quarantined_entries"].append(quarantine)
            continue
        except Exception as exc:
            result["preserved_count"] += 1
            result["errors"].append(f"{run_id}: {type(exc).__name__}: {exc}")
            continue
        state = run.get("state")
        if state not in TERMINAL_RUN_STATES:
            result["preserved_count"] += 1
            continue
        try:
            envelope = _load_envelope(store, run_id, run.get("envelope_sha256"))
            load_state_evidence_bundle(store, run, envelope)
            if state == "succeeded":
                with store.connect() as connection:
                    receipt_row = connection.execute(
                        "SELECT receipt_json,receipt_sha256 FROM receipts WHERE run_id=?",
                        (run_id,),
                    ).fetchone()
                if receipt_row is None:
                    result["preserved_count"] += 1
                    continue
                receipt = _load_validated_stored_receipt(registry, run_id, receipt_row)
                for field in (
                    "task_id",
                    "task_sha256",
                    "plan_sha256",
                    "envelope_sha256",
                ):
                    if receipt.get(field) != run.get(field):
                        raise ValueError(f"stored receipt binding mismatch: {field}")
            entry.unlink()
        except Exception as exc:
            result["preserved_count"] += 1
            result["errors"].append(f"{run_id}: {type(exc).__name__}: {exc}")
            continue
        result["retired_count"] += 1
        result["retired_run_ids"].append(run_id)
    return result


def load_state_evidence_bundle(
    store: StateStore, run: Mapping[str, Any], envelope: Mapping[str, Any]
) -> Mapping[str, Any]:
    run_id = str(run["run_id"])
    path = _evidence_bundle_path(store, run_id)
    if path is None:
        return {}
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {}
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise ValueError("acceptance evidence bundle must be a regular non-symlink file")
    if metadata.st_size > MAX_EVIDENCE_BUNDLE_BYTES:
        raise ValueError("acceptance evidence bundle exceeds size limit")
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"acceptance evidence bundle is unreadable: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(bundle, dict):
        raise ValueError("acceptance evidence bundle must be a JSON object")
    expected = {
        "schema_version": 1,
        "kind": EVIDENCE_BUNDLE_KIND,
        "run_id": run_id,
        "task_id": run.get("task_id"),
        "task_sha256": run.get("task_sha256"),
        "plan_sha256": run.get("plan_sha256"),
    }
    for key, value in expected.items():
        if bundle.get(key) != value:
            raise ValueError(f"acceptance evidence bundle binding mismatch: {key}")
    task = envelope.get("task")
    if not isinstance(task, Mapping) or task.get("id") != bundle.get("task_id"):
        raise ValueError("acceptance evidence bundle task does not match envelope")
    evidence = bundle.get("evidence")
    if not isinstance(evidence, Mapping):
        raise ValueError("acceptance evidence bundle evidence must be an object")
    return evidence


def _manual_authentication_record(row: Mapping[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(str(row["payload_json"]))
        event_schema_version = int(row["event_schema_version"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("manual acceptance authentication event row is invalid") from exc
    if event_schema_version != state_events.EVENT_SCHEMA_VERSION:
        raise ValueError(
            "manual acceptance authentication event schema version is unsupported"
        )
    try:
        state_events.validate_event(
            str(row["event_type"]), payload, row["run_id"]
        )
    except state_events.StateEventError as exc:
        raise ValueError(f"manual acceptance authentication event is invalid: {exc}") from exc
    return {
        **payload,
        "journal": {
            "event_id": int(row["event_id"]),
            "event_type": str(row["event_type"]),
            "event_schema_version": event_schema_version,
            "created_at": str(row["created_at"]),
        },
    }


def _manual_authentication_payload(
    *,
    run: Mapping[str, Any],
    criterion_id: str,
    observation_scope: str,
    evidence_sha256: str,
    reviewer: str,
) -> dict[str, Any]:
    return {
        "schema_version": state_events.EVENT_SCHEMA_VERSION,
        "kind": state_events.MANUAL_ACCEPTANCE_AUTHENTICATION_KIND,
        "run_id": str(run["run_id"]),
        "task_id": str(run["task_id"]),
        "task_sha256": str(run["task_sha256"]),
        "plan_sha256": str(run["plan_sha256"]),
        "envelope_sha256": str(run["envelope_sha256"]),
        "criterion_id": criterion_id,
        "verifier": "manual_observation",
        "observation_scope": observation_scope,
        "evidence_sha256": evidence_sha256,
        "authority": "manual",
        "reviewer": reviewer,
        "observer": reviewer,
    }


def _manual_criterion(
    task: Mapping[str, Any], criterion_id: str
) -> tuple[Mapping[str, Any], str]:
    try:
        validate_acceptance_contract(task)
    except AcceptanceContractError as exc:
        raise legacy.StateError(
            f"run acceptance contract is invalid: {exc}"
        ) from exc
    criteria = task.get("acceptance")
    assert isinstance(criteria, list)
    criterion = next(
        (
            item
            for item in criteria
            if isinstance(item, Mapping) and item.get("id") == criterion_id
        ),
        None,
    )
    if criterion is None:
        raise legacy.StateError(
            f"unknown acceptance criterion {criterion_id!r} for run task"
        )
    contract = criterion_contract(criterion)
    assert contract is not None
    verifier = contract["verifier"]
    if verifier != "manual_observation":
        raise legacy.StateError(
            f"acceptance criterion {criterion_id!r} verifier mismatch: "
            f"expected 'manual_observation', observed {verifier!r}"
        )
    config = contract["verifier_config"]
    assert isinstance(config, Mapping)
    observation_scope = config["observation_scope"]
    assert isinstance(observation_scope, str)
    return criterion, observation_scope


def _validate_manual_evidence_item(
    item: Mapping[str, Any],
    *,
    run: Mapping[str, Any],
    criterion_id: str,
    observation_scope: str,
) -> None:
    if item.get("schema_version") != 1 or item.get("kind") != EVIDENCE_KIND:
        raise legacy.StateError(
            f"manual evidence item {criterion_id!r} schema/kind mismatch"
        )
    if item.get("criterion_id") != criterion_id:
        raise legacy.StateError(
            f"manual evidence item {criterion_id!r} criterion binding mismatch"
        )
    if item.get("evidence_type") != "manual_observation":
        raise legacy.StateError(
            f"manual evidence item {criterion_id!r} verifier binding mismatch"
        )
    source = item.get("source")
    if not isinstance(source, Mapping) or source.get("authority") != "manual":
        raise legacy.StateError(
            f"manual evidence item {criterion_id!r} source authority mismatch"
        )
    revision = item.get("revision")
    if not isinstance(revision, Mapping):
        raise legacy.StateError(
            f"manual evidence item {criterion_id!r} revision binding is missing"
        )
    if revision.get("task_sha256") != run.get("task_sha256"):
        raise legacy.StateError(
            f"manual evidence item {criterion_id!r} task revision mismatch"
        )
    if revision.get("plan_sha256") != run.get("plan_sha256"):
        raise legacy.StateError(
            f"manual evidence item {criterion_id!r} plan revision mismatch"
        )
    if revision.get("observation_scope") != observation_scope:
        raise legacy.StateError(
            f"manual evidence item {criterion_id!r} observation scope mismatch"
        )
    facts = item.get("facts")
    if not isinstance(facts, Mapping) or facts.get("observation_scope") != observation_scope:
        raise legacy.StateError(
            f"manual evidence item {criterion_id!r} fact observation scope mismatch"
        )


def record_manual_acceptance_authentication(
    store: StateStore,
    run_id: str,
    criterion_id: str,
    *,
    expected_evidence_sha256: str,
    reviewer: str,
) -> dict[str, Any]:
    """Append one independent, revision-bound authentication of producer evidence."""
    if (
        not isinstance(expected_evidence_sha256, str)
        or len(expected_evidence_sha256) != 64
        or any(char not in "0123456789abcdef" for char in expected_evidence_sha256)
    ):
        raise legacy.StateError("expected evidence SHA-256 is invalid")
    if not isinstance(reviewer, str):
        raise legacy.StateError("reviewer identity must contain 1-200 characters")
    reviewer = reviewer.strip()
    if not reviewer or len(reviewer) > MAX_MANUAL_REVIEWER_LENGTH:
        raise legacy.StateError("reviewer identity must contain 1-200 characters")
    with store.immediate() as connection:
        row = connection.execute(
            "SELECT * FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise legacy.StateError(f"unknown run {run_id}")
        run = store.public_run(row)
        if run.get("state") not in ACTIVE_CLOSEOUT_STATES:
            raise legacy.StateError(
                f"run {run_id} is not active for acceptance authentication: "
                f"{run.get('state')}"
            )
        try:
            stored_envelope = json.loads(str(row["envelope_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise legacy.StateError(f"run {run_id} stored envelope JSON is invalid") from exc
        if (
            not isinstance(stored_envelope, dict)
            or legacy.sha256_json(stored_envelope) != run.get("envelope_sha256")
        ):
            raise legacy.StateError(f"run {run_id} stored envelope integrity mismatch")
        try:
            envelope = _load_envelope(store, run_id, str(run["envelope_sha256"]))
        except ValueError as exc:
            raise legacy.StateError(str(exc)) from exc
        if envelope != stored_envelope:
            raise legacy.StateError(f"run {run_id} envelope differs from authoritative state")
        task = envelope.get("task")
        if not isinstance(task, Mapping):
            raise legacy.StateError(f"run {run_id} envelope task is invalid")
        _, observation_scope = _manual_criterion(task, criterion_id)
        try:
            evidence = load_state_evidence_bundle(store, run, envelope)
        except ValueError as exc:
            raise legacy.StateError(str(exc)) from exc
        item = evidence.get(criterion_id)
        if not isinstance(item, Mapping):
            raise legacy.StateError(
                f"current evidence bundle has no object item for criterion {criterion_id!r}"
            )
        evidence_sha256 = legacy.sha256_json(item)
        if evidence_sha256 != expected_evidence_sha256:
            raise legacy.StateError(
                f"manual evidence digest mismatch for criterion {criterion_id!r}: "
                f"expected {expected_evidence_sha256}, observed {evidence_sha256}"
            )
        _validate_manual_evidence_item(
            item,
            run=run,
            criterion_id=criterion_id,
            observation_scope=observation_scope,
        )
        facts = item["facts"]
        assert isinstance(facts, Mapping)
        if reviewer in {facts.get("observer"), run.get("worker_id")}:
            raise legacy.StateError(
                "manual acceptance reviewer must differ from the evidence producer "
                "and run worker"
            )
        payload = _manual_authentication_payload(
            run=run,
            criterion_id=criterion_id,
            observation_scope=observation_scope,
            evidence_sha256=evidence_sha256,
            reviewer=reviewer,
        )
        canonical_payload = legacy.canonical_json(payload)
        identical = connection.execute(
            "SELECT event_id,run_id,event_type,event_schema_version,payload_json,created_at "
            "FROM events WHERE run_id=? AND event_type=? AND payload_json=? "
            "ORDER BY event_id DESC LIMIT 2",
            (
                run_id,
                state_events.MANUAL_ACCEPTANCE_AUTHENTICATION_EVENT_TYPE,
                canonical_payload,
            ),
        ).fetchall()
        for event_row in identical:
            _manual_authentication_record(event_row)
        if len(identical) > 1:
            raise legacy.StateError(
                "manual acceptance authentication journal contains duplicate attestations"
            )
        idempotent = bool(identical)
        if not identical:
            rows = connection.execute(
                "SELECT event_id,run_id,event_type,event_schema_version,payload_json,created_at "
                "FROM events WHERE run_id=? AND event_type=? ORDER BY event_id DESC LIMIT ?",
                (
                    run_id,
                    state_events.MANUAL_ACCEPTANCE_AUTHENTICATION_EVENT_TYPE,
                    MAX_MANUAL_AUTHENTICATION_EVENTS_PER_RUN + 1,
                ),
            ).fetchall()
            for event_row in rows:
                _manual_authentication_record(event_row)
            if len(rows) > MAX_MANUAL_AUTHENTICATION_EVENTS_PER_RUN:
                raise legacy.StateError(
                    "manual acceptance authentication journal bound exceeded"
                )
            if len(rows) == MAX_MANUAL_AUTHENTICATION_EVENTS_PER_RUN:
                raise legacy.StateError(
                    "manual acceptance authentication journal capacity reached"
                )
            store.event(
                connection,
                state_events.MANUAL_ACCEPTANCE_AUTHENTICATION_EVENT_TYPE,
                payload,
                run_id,
            )
            identical = connection.execute(
                "SELECT event_id,run_id,event_type,event_schema_version,payload_json,created_at "
                "FROM events WHERE run_id=? AND event_type=? AND payload_json=? "
                "ORDER BY event_id DESC LIMIT 2",
                (
                    run_id,
                    state_events.MANUAL_ACCEPTANCE_AUTHENTICATION_EVENT_TYPE,
                    canonical_payload,
                ),
            ).fetchall()
        if len(identical) != 1:
            raise legacy.StateError(
                "manual acceptance authentication journal write did not read back uniquely"
            )
        authentication = _manual_authentication_record(identical[0])
    return {
        "schema_version": 1,
        "kind": "bureau.manual_acceptance_authentication_receipt",
        "status": "authenticated",
        "idempotent": idempotent,
        "authentication": authentication,
    }


def _github_source_reference(repository: str, pull_request: int, head_sha: str) -> str:
    return f"github-pr:{repository}#{pull_request}@{head_sha}"


def _github_evidence_matches_live(
    criterion: Mapping[str, Any],
    evidence: Mapping[str, Any],
    detail: Mapping[str, Any],
) -> bool:
    contract = criterion_contract(criterion)
    if contract is None:
        return False
    verifier = contract.get("verifier")
    config = contract.get("verifier_config")
    if verifier not in {"code_merged", "required_ci_green"} or not isinstance(config, Mapping):
        return False
    repository = config.get("repository")
    pull_request = config.get("pull_request")
    head_sha = config.get("head_sha")
    base_ref = config.get("base_ref")
    if (
        not isinstance(repository, str)
        or not isinstance(pull_request, int)
        or not isinstance(head_sha, str)
        or not isinstance(base_ref, str)
        or detail.get("number") != pull_request
        or detail.get("headRefOid") != head_sha
        or detail.get("baseRefName") != base_ref
    ):
        return False
    source = evidence.get("source")
    if not isinstance(source, Mapping):
        return False
    if source.get("authority") != "github" or source.get("reference") != _github_source_reference(
        repository, pull_request, head_sha
    ):
        return False
    facts = evidence.get("facts")
    if not isinstance(facts, Mapping):
        return False

    if verifier == "code_merged":
        merge_commit = detail.get("mergeCommit")
        merge_sha = merge_commit.get("oid") if isinstance(merge_commit, Mapping) else None
        expected = {
            "merged": detail.get("state") == "MERGED" and bool(detail.get("mergedAt")),
            "head_sha": head_sha,
            "base_ref": base_ref,
            "merge_commit_sha": merge_sha,
        }
        return all(facts.get(key) == value for key, value in expected.items())

    required_checks = config.get("required_checks")
    if not isinstance(required_checks, list) or not required_checks:
        return False
    summary = runtime_refresh.summarize_required_checks(
        detail.get("statusCheckRollup"), required_checks
    )
    claimed_rows = facts.get("checks")
    if facts.get("complete") is not True or not isinstance(claimed_rows, list):
        return False
    if facts.get("base_ref") != base_ref:
        return False
    if facts.get("required_checks") not in (None, required_checks):
        return False
    claimed: dict[str, str] = {}
    for row in claimed_rows:
        if not isinstance(row, Mapping):
            return False
        name = row.get("name")
        state = row.get("state")
        if not isinstance(name, str) or not isinstance(state, str) or name in claimed:
            return False
        claimed[name] = state.lower()
    return all(claimed.get(name) == summary[name]["state"] for name in required_checks)


def authenticate_state_evidence(
    run: dict[str, Any],
    envelope: dict[str, Any],
    evidence: Mapping[str, Any],
    *,
    github: GitHubReader = runtime_refresh.gh_json,
    store: StateStore | None = None,
    authentication_unavailable: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Authenticate bundle claims against primary observers.

    The bundle itself never grants authority. GitHub-backed criteria are
    authenticated by a fresh locally authenticated ``gh`` readback against the
    criterion-frozen repository, PR and head. Manual observations are
    authenticated only by an exact digest- and revision-bound StateStore journal
    attestation. A claimed criterion whose declared verifier has no production
    authentication adapter is reported explicitly as adapter-unavailable; it is
    never silently treated as rejected evidence.
    """
    task = envelope.get("task")
    criteria = task.get("acceptance") if isinstance(task, Mapping) else None
    if not isinstance(criteria, list):
        return {}
    authenticated: dict[str, dict[str, Any]] = {}
    cache: dict[tuple[str, int], Mapping[str, Any] | None] = {}
    for criterion in criteria:
        if not isinstance(criterion, Mapping):
            continue
        criterion_id = criterion.get("id")
        contract = criterion_contract(criterion)
        if not isinstance(criterion_id, str) or contract is None:
            continue
        verifier = contract.get("verifier")
        config = contract.get("verifier_config")
        claimed = evidence.get(criterion_id)
        if not isinstance(config, Mapping) or not isinstance(claimed, Mapping):
            continue
        if verifier == "manual_observation":
            evidence_sha256 = legacy.sha256_json(claimed)
            observation_scope = config.get("observation_scope")
            expected_payload = {
                "run_id": run.get("run_id"),
                "task_id": run.get("task_id"),
                "task_sha256": run.get("task_sha256"),
                "plan_sha256": run.get("plan_sha256"),
                "envelope_sha256": run.get("envelope_sha256"),
                "criterion_id": criterion_id,
                "verifier": "manual_observation",
                "observation_scope": observation_scope,
                "evidence_sha256": evidence_sha256,
                "authority": "manual",
            }
            matches: list[Mapping[str, Any]] = []
            if store is not None:
                with store.connect() as connection:
                    rows = connection.execute(
                        "SELECT event_id,run_id,event_type,event_schema_version,"
                        "payload_json,created_at FROM events "
                        "WHERE run_id=? AND event_type=? ORDER BY event_id DESC LIMIT ?",
                        (
                            run.get("run_id"),
                            state_events.MANUAL_ACCEPTANCE_AUTHENTICATION_EVENT_TYPE,
                            MAX_MANUAL_AUTHENTICATION_EVENTS_PER_RUN + 1,
                        ),
                    ).fetchall()
                if len(rows) > MAX_MANUAL_AUTHENTICATION_EVENTS_PER_RUN:
                    raise legacy.StateError(
                        "manual acceptance authentication journal bound exceeded"
                    )
                for row in rows:
                    record = _manual_authentication_record(row)
                    if all(record.get(key) == value for key, value in expected_payload.items()):
                        matches.append(record)
            if matches:
                authenticated[criterion_id] = dict(matches[-1])
                continue
            unavailable = EvidenceAdapterUnavailable(
                authority="manual",
                adapter=(
                    "StateStore.events:"
                    f"{state_events.MANUAL_ACCEPTANCE_AUTHENTICATION_EVENT_TYPE}"
                ),
                target=expected_payload,
                detail=(
                    "no matching manual acceptance source attestation exists for "
                    "the exact current evidence digest and run revision"
                ),
            )
            if authentication_unavailable is None:
                raise unavailable
            authentication_unavailable.append(unavailable.payload())
            continue
        adapter = PRODUCTION_AUTHENTICATION_ADAPTERS.get(str(verifier))
        if adapter is None:
            authorities = contract.get("authorities")
            permitted_authorities = (
                [str(item) for item in authorities]
                if isinstance(authorities, tuple)
                else []
            )
            raise EvidenceAdapterUnavailable(
                authority="unbound",
                adapter=f"missing:{verifier}",
                target={
                    "criterion_id": criterion_id,
                    "verifier": verifier,
                    "permitted_authorities": permitted_authorities,
                },
                detail=(
                    "no production authentication adapter is registered for verifier "
                    f"{verifier}"
                ),
            )
        repository = config.get("repository")
        pull_request = config.get("pull_request")
        if not isinstance(repository, str) or not isinstance(pull_request, int):
            continue
        cache_key = (repository, pull_request)
        if cache_key not in cache:
            target = {"repository": repository, "pull_request": pull_request}
            try:
                detail = github(
                    [
                        "pr",
                        "view",
                        str(pull_request),
                        "--repo",
                        repository,
                        "--json",
                        "number,state,isDraft,mergedAt,mergeCommit,headRefOid,baseRefName,statusCheckRollup,url",
                    ]
                )
            except Exception as exc:
                raise EvidenceAdapterUnavailable(
                    authority="github",
                    adapter="runtime_refresh.gh_json",
                    target=target,
                    detail=f"{type(exc).__name__}: {exc}",
                ) from exc
            if not isinstance(detail, Mapping):
                raise EvidenceAdapterUnavailable(
                    authority="github",
                    adapter="runtime_refresh.gh_json",
                    target=target,
                    detail=f"invalid adapter response type: {type(detail).__name__}",
                )
            cache[cache_key] = detail
        detail = cache[cache_key]
        if detail is not None and _github_evidence_matches_live(criterion, claimed, detail):
            live_facts: dict[str, Any]
            if verifier == "code_merged":
                merge_commit = detail.get("mergeCommit")
                live_facts = {
                    "merged": detail.get("state") == "MERGED" and bool(detail.get("mergedAt")),
                    "merged_at": detail.get("mergedAt"),
                    "head_sha": detail.get("headRefOid"),
                    "base_ref": detail.get("baseRefName"),
                    "merge_commit_sha": (
                        merge_commit.get("oid") if isinstance(merge_commit, Mapping) else None
                    ),
                }
            else:
                required_checks = config.get("required_checks")
                assert isinstance(required_checks, list)
                live_facts = {
                    "head_sha": detail.get("headRefOid"),
                    "base_ref": detail.get("baseRefName"),
                    "required_checks": runtime_refresh.summarize_required_checks(
                        detail.get("statusCheckRollup"), required_checks
                    ),
                }
            canonical_live = {
                "repository": repository,
                "pull_request": pull_request,
                "base_ref": config["base_ref"],
                "verifier": verifier,
                "facts": live_facts,
            }
            authenticated[criterion_id] = {
                "schema_version": 1,
                "kind": "bureau.acceptance_source_authentication",
                "criterion_id": criterion_id,
                "authority": "github",
                "observer": adapter,
                "source_reference": _github_source_reference(
                    repository, pull_request, str(config["head_sha"])
                ),
                "target": {
                    "repository": repository,
                    "pull_request": pull_request,
                    "head_sha": config["head_sha"],
                    "base_ref": config["base_ref"],
                },
                "live_facts": live_facts,
                "live_observation_sha256": hashlib.sha256(
                    runtime_refresh.canonical_bytes(canonical_live)
                ).hexdigest(),
            }
    return authenticated


def reconcile_state_evidence(
    registry: Any,
    store: StateStore,
    *,
    now: str | None = None,
    completion: Completion = _complete_run_after_typed_evaluation,
    github: GitHubReader = runtime_refresh.gh_json,
) -> dict[str, Any]:
    """Consume typed bundles from the canonical StateStore root.

    The existing Bureau reconcile service is the production caller and remains
    the only StateStore writer. Evidence producers only publish bound bundles;
    absent or invalid bundles remain open and cannot terminalize a run.
    """

    retirement_before = retire_terminal_evidence_bundles(registry, store)

    def provider(run: dict[str, Any], envelope: dict[str, Any]) -> Mapping[str, Any]:
        return load_state_evidence_bundle(store, run, envelope)

    unavailable_by_run: dict[str, list[dict[str, Any]]] = {}

    def authentication_provider(
        run: dict[str, Any], envelope: dict[str, Any], evidence: Mapping[str, Any]
    ) -> AuthenticationRecords:
        unavailable: list[dict[str, Any]] = []
        records = authenticate_state_evidence(
            run,
            envelope,
            evidence,
            github=github,
            store=store,
            authentication_unavailable=unavailable,
        )
        if unavailable:
            unavailable_by_run[str(run["run_id"])] = unavailable
        return records

    result = reconcile_runs(
        registry,
        store,
        provider,
        now=now,
        completion=completion,
        authentication_provider=authentication_provider,
    )
    for observation in result["observations"]:
        unavailable = unavailable_by_run.get(str(observation.get("run_id")))
        if unavailable and observation.get("state") == "open":
            observation["reason"] = "evidence-authentication-unavailable"
            observation["authentication_unavailable"] = unavailable
            if len(unavailable) == 1:
                observation["adapter"] = unavailable[0]
    retirement_after = retire_terminal_evidence_bundles(registry, store)
    result["evidence_directory"] = str(store.state_root / EVIDENCE_DIRECTORY)
    result["evidence_retirement"] = {
        "before": retirement_before,
        "after": retirement_after,
        "retired_count": (
            retirement_before["retired_count"] + retirement_after["retired_count"]
        ),
        "quarantined_count": (
            retirement_before["quarantined_count"]
            + retirement_after["quarantined_count"]
        ),
    }
    result["writer"] = "bureau-reconcile"
    return result
