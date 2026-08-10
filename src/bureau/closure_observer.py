from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from bureau.acceptance import PASSED, evaluate_acceptance
from bureau.v2 import StateStore, complete_run

ACTIVE_CLOSEOUT_STATES = {"assigned", "running", "verifying"}
TERMINAL_RUN_STATES = {"succeeded"}

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
