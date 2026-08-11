from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Callable, Mapping
from typing import Any

from bureau import legacy, runtime_refresh
from bureau.acceptance import PASSED, criterion_contract, evaluate_acceptance
from bureau.v2 import (
    RunStateConflict,
    StateStore,
    _load_validated_stored_receipt,
    complete_run,
)

ACTIVE_CLOSEOUT_STATES = {"assigned", "running", "verifying"}
TERMINAL_RUN_STATES = {"succeeded"}
EVIDENCE_BUNDLE_KIND = "bureau.acceptance_evidence_bundle"
EVIDENCE_DIRECTORY = "acceptance-evidence"
MAX_EVIDENCE_BUNDLE_BYTES = 1_048_576

Completion = Callable[[Any, StateStore, str, dict[str, Any]], dict[str, Any]]
EvidenceProvider = Callable[[dict[str, Any], dict[str, Any]], Mapping[str, Any]]
AuthenticationRecords = Mapping[str, Mapping[str, Any]]
AuthenticationProvider = Callable[
    [dict[str, Any], dict[str, Any], Mapping[str, Any]], AuthenticationRecords
]
GitHubReader = Callable[[list[str]], Any]


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
    completion: Completion = complete_run,
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


def _evidence_bundle_path(store: StateStore, run_id: str) -> Any:
    return store.state_root / EVIDENCE_DIRECTORY / f"{run_id}.json"


def retire_terminal_evidence_bundles(registry: Any, store: StateStore) -> dict[str, Any]:
    """Retire validated receipt-backed bundles for succeeded runs.

    The StateStore receipt is the durable audit record after closeout. Keeping
    the producer bundle would make the bounded active-evidence directory grow
    forever, so only a bundle that still validates against the claim-bound run
    and envelope is removed. Unknown, malformed, nonterminal, or receipt-less
    entries are preserved for diagnosis rather than deleted speculatively.
    """

    root = store.state_root / EVIDENCE_DIRECTORY
    result: dict[str, Any] = {
        "schema_version": 1,
        "kind": "bureau.acceptance_evidence_retirement",
        "directory": str(root),
        "retired_count": 0,
        "retired_run_ids": [],
        "preserved_count": 0,
        "errors": [],
    }
    try:
        entries = sorted(root.iterdir(), key=lambda item: item.name)
    except FileNotFoundError:
        return result
    except OSError as exc:
        result["errors"].append(f"{type(exc).__name__}: {exc}")
        return result

    for entry in entries:
        if entry.suffix != ".json":
            result["preserved_count"] += 1
            continue
        run_id = entry.stem
        try:
            run = store.run(run_id)
        except Exception as exc:
            result["preserved_count"] += 1
            result["errors"].append(f"{run_id}: {type(exc).__name__}: {exc}")
            continue
        if run.get("state") != "succeeded":
            result["preserved_count"] += 1
            continue
        try:
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
            envelope = _load_envelope(store, run_id, run.get("envelope_sha256"))
            load_state_evidence_bundle(store, run, envelope)
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
) -> dict[str, dict[str, Any]]:
    """Authenticate bundle claims against primary observers.

    The bundle itself never grants authority. GitHub-backed criteria are
    authenticated by a fresh locally authenticated ``gh`` readback against the
    criterion-frozen repository, PR and head. Other source classes remain
    unauthenticated until an independent adapter/receipt verifier is available.
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
        if (
            verifier not in {"code_merged", "required_ci_green"}
            or not isinstance(config, Mapping)
            or not isinstance(claimed, Mapping)
        ):
            continue
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
                "observer": "runtime_refresh.gh_json",
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
    completion: Completion = complete_run,
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

    def authentication_provider(
        run: dict[str, Any], envelope: dict[str, Any], evidence: Mapping[str, Any]
    ) -> AuthenticationRecords:
        return authenticate_state_evidence(run, envelope, evidence, github=github)

    result = reconcile_runs(
        registry,
        store,
        provider,
        now=now,
        completion=completion,
        authentication_provider=authentication_provider,
    )
    retirement_after = retire_terminal_evidence_bundles(registry, store)
    result["evidence_directory"] = str(store.state_root / EVIDENCE_DIRECTORY)
    result["evidence_retirement"] = {
        "before": retirement_before,
        "after": retirement_after,
        "retired_count": (
            retirement_before["retired_count"] + retirement_after["retired_count"]
        ),
    }
    result["writer"] = "bureau-reconcile"
    return result
