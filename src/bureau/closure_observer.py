from __future__ import annotations

import json
import stat
from collections.abc import Callable, Mapping
from typing import Any

from bureau.acceptance import PASSED, evaluate_acceptance
from bureau.v2 import StateStore, complete_run

ACTIVE_CLOSEOUT_STATES = {"assigned", "running", "verifying"}
TERMINAL_RUN_STATES = {"succeeded"}
EVIDENCE_BUNDLE_KIND = "bureau.acceptance_evidence_bundle"
EVIDENCE_DIRECTORY = "acceptance-evidence"
MAX_EVIDENCE_BUNDLE_BYTES = 1_048_576

Completion = Callable[[Any, StateStore, str, dict[str, Any]], dict[str, Any]]
EvidenceProvider = Callable[[dict[str, Any], dict[str, Any]], Mapping[str, Any]]


def _load_envelope(store: StateStore, run_id: str) -> dict[str, Any]:
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
    return value


def evaluate_run(
    store: StateStore,
    run_id: str,
    evidence: Mapping[str, Any],
    *,
    now: str | None = None,
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
        envelope = _load_envelope(store, run_id)
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
    completion: Completion = complete_run,
) -> dict[str, Any]:
    observed = evaluate_run(store, run_id, evidence, now=now)
    if observed["state"] == "already_terminal":
        return observed
    evaluation = observed.get("evaluation")
    if not isinstance(evaluation, dict) or evaluation.get("automatic_terminalization") is not True:
        return observed

    # The typed evaluator is deliberately not a second writer. It authorizes
    # exactly one call into the existing CAS/idempotent closeout path. That path
    # re-reads the authoritative TaskSpec/plan baseline before mutating state.
    completion_evidence = dict(evidence)
    completion_evidence["_typed_acceptance"] = evaluation
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
    completion: Completion = complete_run,
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
            envelope = _load_envelope(store, run_id)
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
        observations.append(
            reconcile_run(
                registry,
                store,
                run_id,
                evidence,
                now=now,
                completion=completion,
            )
        )
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


def _evidence_bundle_path(store: StateStore, run_id: str) -> Any:
    return store.state_root / EVIDENCE_DIRECTORY / f"{run_id}.json"


def load_state_evidence_bundle(
    store: StateStore, run: Mapping[str, Any], envelope: Mapping[str, Any]
) -> Mapping[str, Any]:
    run_id = str(run["run_id"])
    path = _evidence_bundle_path(store, run_id)
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


def reconcile_state_evidence(
    registry: Any,
    store: StateStore,
    *,
    now: str | None = None,
    completion: Completion = complete_run,
) -> dict[str, Any]:
    """Consume typed bundles from the canonical StateStore root.

    The existing Bureau reconcile service is the production caller and remains
    the only StateStore writer. Evidence producers only publish bound bundles;
    absent or invalid bundles remain open and cannot terminalize a run.
    """

    def provider(run: dict[str, Any], envelope: dict[str, Any]) -> Mapping[str, Any]:
        return load_state_evidence_bundle(store, run, envelope)

    result = reconcile_runs(
        registry,
        store,
        provider,
        now=now,
        completion=completion,
    )
    result["evidence_directory"] = str(store.state_root / EVIDENCE_DIRECTORY)
    result["writer"] = "bureau-reconcile"
    return result
