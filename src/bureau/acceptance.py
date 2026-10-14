from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any

SCHEMA_VERSION = 1
EVIDENCE_KIND = "bureau.acceptance_evidence"
EVALUATION_KIND = "bureau.acceptance_evaluation"
PASSED = "passed"
FAILED = "failed"
UNKNOWN = "unknown"
STATES = {PASSED, FAILED, UNKNOWN}
MAX_SOAK_SECONDS = 315_576_000

# Frozen T044 authority contract. This code constant is independent of the mutable
# TaskSpec: substituting a manual observation for a production proof must fail.
T044_REQUIRED_ACCEPTANCE = MappingProxyType(
    {
        "production-topology": (
            "runtime_proof_bundle",
            "weltgewebe:t044:production-topology",
            (
                "cluster-identity",
                "gitops-revision",
                "public-dns",
                "load-balancer",
                "tls",
                "secrets",
                "identities",
                "failure-domains",
            ),
        ),
        "cutover-contract": (
            "runtime_proof_bundle",
            "weltgewebe:t044:cutover-contract",
            (
                "signed-plan",
                "responsible-operators",
                "data-sequence",
                "traffic-sequence",
                "automatic-stop",
                "rollback-contract",
            ),
        ),
        "staging-gitops-runtime": (
            "manual_observation",
            "weltgewebe:t044:staging-gitops-runtime",
            (),
        ),
        "t048-platform-parity": (
            "runtime_proof_bundle",
            "weltgewebe:t044:t048-platform-parity",
            (
                "functional-parity",
                "security-parity",
                "data-persistence",
                "backup-restore",
                "recovery",
                "latency",
                "resource-budgets",
            ),
        ),
        "semantic-search-continuity": (
            "manual_observation",
            "weltgewebe:t044:semantic-search-continuity",
            (),
        ),
        "data-transition": (
            "runtime_proof_bundle",
            "weltgewebe:t044:data-transition",
            (
                "postgres-continuity",
                "jetstream-continuity",
                "object-file-continuity",
                "event-reconciliation",
                "writer-fencing",
            ),
        ),
        "progressive-traffic": (
            "runtime_proof_bundle",
            "weltgewebe:t044:progressive-traffic",
            (
                "traffic-stages",
                "slo-observation",
                "error-budget",
                "mutation-integrity",
                "automatic-stop",
                "rollback-verification",
            ),
        ),
        "production-recovery": (
            "runtime_proof_bundle",
            "weltgewebe:t044:production-recovery",
            (
                "pitr",
                "blank-cluster-restore",
                "secret-key-recovery",
                "blue-rollback",
                "rto-rpo-evidence",
            ),
        ),
        "operator-readiness": (
            "runtime_proof_bundle",
            "weltgewebe:t044:operator-readiness",
            (
                "slo-evidence",
                "rto-rpo-metrics",
                "operator-projections",
                "deployment-revision",
                "action-authority",
            ),
        ),
        "legacy-retirement": (
            "runtime_proof_bundle",
            "weltgewebe:t044:legacy-retirement",
            (
                "observation-window",
                "owner-approval",
                "rollback-window",
                "sole-writer",
                "cleanup-inventory",
            ),
        ),
        "live-readback": (
            "runtime_proof_bundle",
            "weltgewebe:t044:live-readback",
            (
                "web-api-commit",
                "gitops-revision",
                "workload-digests",
                "data-continuity",
                "writer-authority",
            ),
        ),
    }
)

_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Each verifier is a bounded contract. A criterion selects one by name through
# ``verifier`` and must use object evidence. The contract then fixes the
# authority class, revision binding and maximum evidence age; caller-provided
# evidence cannot weaken those boundaries.
VERIFIER_CONTRACTS: dict[str, dict[str, Any]] = {
    "code_merged": {
        "domain": "product",
        "authorities": ("github",),
        "max_age_seconds": 86_400,
        "config_fields": ("repository", "pull_request", "head_sha", "base_ref"),
        "revision_binding": (
            "task_sha256",
            "plan_sha256",
            "head_sha",
            "base_ref",
            "merge_commit_sha",
        ),
    },
    "required_ci_green": {
        "domain": "product",
        "authorities": ("github",),
        "max_age_seconds": 21_600,
        "config_fields": (
            "repository",
            "pull_request",
            "head_sha",
            "base_ref",
            "required_checks",
        ),
        "revision_binding": ("task_sha256", "plan_sha256", "head_sha", "base_ref"),
    },
    "deployment_complete": {
        "domain": "deployment",
        "authorities": ("grabowski", "target-runtime"),
        "max_age_seconds": 86_400,
        "config_fields": ("deployment_revision",),
        "revision_binding": ("task_sha256", "plan_sha256", "deployment_revision"),
    },
    "runtime_commit_contains": {
        "domain": "runtime",
        "authorities": ("grabowski", "target-runtime"),
        "max_age_seconds": 3_600,
        "config_fields": ("repository", "required_commit"),
        "revision_binding": (
            "task_sha256",
            "plan_sha256",
            "required_commit",
            "observed_commit",
        ),
    },
    "live_probe_passed": {
        "domain": "runtime",
        "authorities": ("grabowski", "target-runtime"),
        "max_age_seconds": 900,
        "config_fields": ("runtime_revision", "probe_id"),
        "revision_binding": (
            "task_sha256",
            "plan_sha256",
            "runtime_revision",
            "probe_id",
        ),
    },
    "runtime_proof_bundle": {
        "domain": "runtime",
        "authorities": ("grabowski", "target-runtime"),
        "max_age_seconds": 900,
        "config_fields": (
            "observation_scope",
            "required_probes",
            "expected_runtime_revision",
            "expected_target_identity",
        ),
        "revision_binding": (
            "task_sha256",
            "plan_sha256",
            "observation_scope",
            "runtime_revision",
            "target_identity",
        ),
    },
    "manual_observation": {
        "domain": "observation",
        "authorities": ("manual",),
        "max_age_seconds": 86_400,
        "config_fields": ("observation_scope",),
        "revision_binding": ("task_sha256", "plan_sha256", "observation_scope"),
    },
    "duration_soak_completed": {
        "domain": "observation",
        "authorities": ("grabowski", "target-runtime", "manual"),
        "max_age_seconds": 86_400,
        "config_fields": ("required_seconds", "observation_scope"),
        "revision_binding": ("task_sha256", "plan_sha256", "observation_scope"),
    },
    "no_effect_verified": {
        "domain": "observation",
        "authorities": ("bureau", "grabowski"),
        "max_age_seconds": 3_600,
        "config_fields": ("scope_sha256",),
        "revision_binding": ("task_sha256", "plan_sha256", "scope_sha256"),
    },
    "artifact_hash_matches": {
        "domain": "product",
        "authorities": ("artifact-store", "bureau", "grabowski"),
        "max_age_seconds": 86_400,
        "config_fields": ("artifact_sha256",),
        "revision_binding": ("task_sha256", "plan_sha256", "artifact_sha256"),
    },
}

# A named contract is executable only when the evaluator below has bounded
# semantics for it.  Keep this explicit rather than treating additions to the
# descriptive contract table as executable by default.
_EXECUTABLE_VERIFIERS = frozenset(
    {
        "code_merged",
        "required_ci_green",
        "deployment_complete",
        "runtime_commit_contains",
        "live_probe_passed",
        "manual_observation",
        "runtime_proof_bundle",
        "duration_soak_completed",
        "no_effect_verified",
        "artifact_hash_matches",
    }
)

_PASS_CHECK_STATES = {"success", "passed", "neutral", "skipped"}
_FAIL_CHECK_STATES = {"failure", "failed", "error", "cancelled", "timed_out"}
_PENDING_CHECK_STATES = {"pending", "queued", "in_progress", "waiting", "requested"}


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OverflowError, OSError):
        return None
    if parsed.tzinfo is None:
        return None
    try:
        return parsed.astimezone(timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _now(value: str | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    parsed = _parse_time(value)
    if parsed is None:
        raise ValueError("now must be an ISO-8601 timestamp with timezone")
    return parsed


def _sha(value: Any) -> bool:
    return isinstance(value, str) and _SHA_RE.fullmatch(value) is not None


def _sha256(value: Any) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _valid_soak_seconds(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return 0 <= value <= MAX_SOAK_SECONDS
    if isinstance(value, float):
        return math.isfinite(value) and 0 <= value <= MAX_SOAK_SECONDS
    return False


def _criterion_verifier_config(
    verifier: str, criterion: Mapping[str, Any]
) -> dict[str, Any] | None:
    raw = criterion.get("verifier_config")
    if not isinstance(raw, Mapping):
        return None
    contract = VERIFIER_CONTRACTS.get(verifier)
    if contract is None or set(raw) != set(contract["config_fields"]):
        return None

    def nonempty(field: str) -> str | None:
        value = raw.get(field)
        return value if isinstance(value, str) and value.strip() else None

    def repository() -> str | None:
        value = nonempty("repository")
        if value is None or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value) is None:
            return None
        return value

    if verifier == "code_merged":
        repo = repository()
        pull_request = raw.get("pull_request")
        head_sha = raw.get("head_sha")
        base_ref = nonempty("base_ref")
        if (
            repo is None
            or not isinstance(pull_request, int)
            or isinstance(pull_request, bool)
            or pull_request < 1
            or not _sha(head_sha)
            or base_ref is None
        ):
            return None
        return {
            "repository": repo,
            "pull_request": pull_request,
            "head_sha": head_sha,
            "base_ref": base_ref,
        }

    if verifier == "required_ci_green":
        repo = repository()
        pull_request = raw.get("pull_request")
        head_sha = raw.get("head_sha")
        base_ref = nonempty("base_ref")
        required_checks = raw.get("required_checks")
        if (
            repo is None
            or not isinstance(pull_request, int)
            or isinstance(pull_request, bool)
            or pull_request < 1
            or not _sha(head_sha)
            or base_ref is None
            or not isinstance(required_checks, list)
            or not required_checks
            or any(not isinstance(name, str) or not name.strip() for name in required_checks)
            or len(set(required_checks)) != len(required_checks)
        ):
            return None
        return {
            "repository": repo,
            "pull_request": pull_request,
            "head_sha": head_sha,
            "base_ref": base_ref,
            "required_checks": list(required_checks),
        }

    if verifier == "deployment_complete":
        value = nonempty("deployment_revision")
        return {"deployment_revision": value} if value is not None else None

    if verifier == "runtime_commit_contains":
        repo = repository()
        required_commit = raw.get("required_commit")
        if repo is None or not _sha(required_commit):
            return None
        return {"repository": repo, "required_commit": required_commit}

    if verifier == "live_probe_passed":
        runtime_revision = nonempty("runtime_revision")
        probe_id = nonempty("probe_id")
        if runtime_revision is None or probe_id is None:
            return None
        return {"runtime_revision": runtime_revision, "probe_id": probe_id}

    if verifier == "runtime_proof_bundle":
        scope = nonempty("observation_scope")
        probes = raw.get("required_probes")
        release = raw.get("expected_runtime_revision")
        target = raw.get("expected_target_identity")
        # T044 has no selected production Green revision yet. Both null values
        # make the Registry structurally valid but evaluation fail closed until
        # a separately authorized TaskSpec revision freezes both identifiers.
        if (release is None) != (target is None):
            return None
        if release is not None and (
            not isinstance(release, str)
            or re.fullmatch(r"[0-9a-f]{40}", release) is None
            or not isinstance(target, str)
            or re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{2,159}", target) is None
        ):
            return None
        if (
            scope is None
            or re.fullmatch(r"weltgewebe:t044:[a-z][a-z0-9-]{1,63}", scope) is None
            or not isinstance(probes, list)
            or not 2 <= len(probes) <= 24
            or any(
                not isinstance(probe, str) or re.fullmatch(r"[a-z][a-z0-9-]{1,63}", probe) is None
                for probe in probes
            )
            or len(set(probes)) != len(probes)
        ):
            return None
        return {
            "observation_scope": scope,
            "required_probes": list(probes),
            "expected_runtime_revision": release,
            "expected_target_identity": target,
        }

    if verifier == "manual_observation":
        scope = nonempty("observation_scope")
        return {"observation_scope": scope} if scope is not None else None

    if verifier == "duration_soak_completed":
        required_seconds = raw.get("required_seconds")
        scope = nonempty("observation_scope")
        if not _valid_soak_seconds(required_seconds) or scope is None:
            return None
        return {"required_seconds": required_seconds, "observation_scope": scope}

    if verifier == "no_effect_verified":
        if not _sha256(raw.get("scope_sha256")):
            return None
        return {"scope_sha256": raw["scope_sha256"]}

    if verifier == "artifact_hash_matches":
        if not _sha256(raw.get("artifact_sha256")):
            return None
        return {"artifact_sha256": raw["artifact_sha256"]}

    return None


def criterion_contract(criterion: Mapping[str, Any]) -> dict[str, Any] | None:
    verifier = criterion.get("verifier")
    if not isinstance(verifier, str):
        return None
    contract = VERIFIER_CONTRACTS.get(verifier)
    if contract is None or criterion.get("evidence_type") != "object":
        return None
    verifier_config = _criterion_verifier_config(verifier, criterion)
    if verifier_config is None:
        return None
    return {
        "verifier": verifier,
        "evidence_type": "object",
        "verifier_config": verifier_config,
        **contract,
    }


class AcceptanceContractError(ValueError):
    """One TaskSpec cannot be executed under the typed acceptance contract."""

    def __init__(self, diagnostics: Sequence[Mapping[str, Any]]) -> None:
        self.diagnostics = [dict(item) for item in diagnostics]
        rendered: list[str] = []
        for item in self.diagnostics:
            missing = item.get("missing_fields") or []
            invalid = item.get("invalid_fields") or []
            suffix: list[str] = []
            if missing:
                suffix.append("missing=" + ",".join(str(field) for field in missing))
            if invalid:
                suffix.append("invalid=" + ",".join(str(field) for field in invalid))
            detail = f"; {'; '.join(suffix)}" if suffix else ""
            rendered.append(
                f"task {item['task_id']} criterion {item['criterion_id']} "
                f"at {item['path']}: {item['message']}{detail}"
            )
        super().__init__("\n".join(rendered))


def _acceptance_diagnostic(
    *,
    task_id: str,
    criterion_id: str,
    path: str,
    code: str,
    message: str,
    missing_fields: Sequence[str] = (),
    invalid_fields: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "criterion_id": criterion_id,
        "path": path,
        "code": code,
        "message": message,
        "missing_fields": list(missing_fields),
        "invalid_fields": list(invalid_fields),
    }


def validate_acceptance_contract(task: Mapping[str, Any]) -> None:
    """Validate the complete executable acceptance contract for one TaskSpec.

    This is deliberately stricter than ``task.v1`` structural validation.  Old
    Registry documents and replayed events remain readable, while every caller
    that can admit, execute, or write work can use this single semantic gate.
    """

    raw_task_id = task.get("id") if isinstance(task, Mapping) else None
    task_id = raw_task_id if isinstance(raw_task_id, str) and raw_task_id else "<missing-task-id>"
    criteria = task.get("acceptance") if isinstance(task, Mapping) else None
    diagnostics: list[dict[str, Any]] = []
    if not isinstance(criteria, list) or not criteria:
        invalid = () if isinstance(criteria, list) else ("acceptance",)
        diagnostics.append(
            _acceptance_diagnostic(
                task_id=task_id,
                criterion_id="<acceptance>",
                path="$.acceptance",
                code="acceptance-empty" if isinstance(criteria, list) else "acceptance-invalid",
                message="acceptance must contain at least one executable typed criterion",
                missing_fields=("acceptance",) if criteria is None else (),
                invalid_fields=invalid,
            )
        )
        raise AcceptanceContractError(diagnostics)

    seen_ids: dict[str, int] = {}
    for index, raw_criterion in enumerate(criteria):
        criterion_path = f"$.acceptance[{index}]"
        if not isinstance(raw_criterion, Mapping):
            diagnostics.append(
                _acceptance_diagnostic(
                    task_id=task_id,
                    criterion_id=f"<criterion-{index}>",
                    path=criterion_path,
                    code="criterion-invalid",
                    message="criterion must be an object",
                    invalid_fields=("criterion",),
                )
            )
            continue

        raw_criterion_id = raw_criterion.get("id")
        criterion_id = (
            raw_criterion_id
            if isinstance(raw_criterion_id, str) and raw_criterion_id
            else f"<criterion-{index}>"
        )
        criterion_has_error = False
        if not isinstance(raw_criterion_id, str) or not raw_criterion_id:
            diagnostics.append(
                _acceptance_diagnostic(
                    task_id=task_id,
                    criterion_id=criterion_id,
                    path=f"{criterion_path}.id",
                    code=(
                        "criterion-id-missing"
                        if raw_criterion_id is None
                        else "criterion-id-invalid"
                    ),
                    message="criterion id must be a non-empty string",
                    missing_fields=("id",) if raw_criterion_id is None else (),
                    invalid_fields=() if raw_criterion_id is None else ("id",),
                )
            )
            criterion_has_error = True
        else:
            first_index = seen_ids.get(raw_criterion_id)
            if first_index is not None:
                diagnostics.append(
                    _acceptance_diagnostic(
                        task_id=task_id,
                        criterion_id=criterion_id,
                        path=f"{criterion_path}.id",
                        code="duplicate-criterion-id",
                        message=(f"criterion id duplicates $.acceptance[{first_index}].id"),
                        invalid_fields=("id",),
                    )
                )
                criterion_has_error = True
            else:
                seen_ids[raw_criterion_id] = index

        evidence_type = raw_criterion.get("evidence_type")
        if evidence_type is None:
            diagnostics.append(
                _acceptance_diagnostic(
                    task_id=task_id,
                    criterion_id=criterion_id,
                    path=f"{criterion_path}.evidence_type",
                    code="evidence-type-missing",
                    message="evidence_type is required and must be exactly 'object'",
                    missing_fields=("evidence_type",),
                )
            )
            criterion_has_error = True
        elif evidence_type != "object":
            diagnostics.append(
                _acceptance_diagnostic(
                    task_id=task_id,
                    criterion_id=criterion_id,
                    path=f"{criterion_path}.evidence_type",
                    code="evidence-type-invalid",
                    message="evidence_type must be exactly 'object'",
                    invalid_fields=("evidence_type",),
                )
            )
            criterion_has_error = True

        verifier = raw_criterion.get("verifier")
        if verifier is None:
            diagnostics.append(
                _acceptance_diagnostic(
                    task_id=task_id,
                    criterion_id=criterion_id,
                    path=f"{criterion_path}.verifier",
                    code="verifier-missing",
                    message="verifier is required",
                    missing_fields=("verifier",),
                )
            )
            criterion_has_error = True
        elif not isinstance(verifier, str) or verifier not in VERIFIER_CONTRACTS:
            diagnostics.append(
                _acceptance_diagnostic(
                    task_id=task_id,
                    criterion_id=criterion_id,
                    path=f"{criterion_path}.verifier",
                    code="verifier-unknown",
                    message=f"verifier {verifier!r} is not a known bounded verifier",
                    invalid_fields=("verifier",),
                )
            )
            criterion_has_error = True
        elif verifier not in _EXECUTABLE_VERIFIERS:
            diagnostics.append(
                _acceptance_diagnostic(
                    task_id=task_id,
                    criterion_id=criterion_id,
                    path=f"{criterion_path}.verifier",
                    code="verifier-not-executable",
                    message=f"verifier {verifier!r} has no executable evaluator",
                    invalid_fields=("verifier",),
                )
            )
            criterion_has_error = True

        verifier_config = raw_criterion.get("verifier_config")
        if verifier_config is None:
            diagnostics.append(
                _acceptance_diagnostic(
                    task_id=task_id,
                    criterion_id=criterion_id,
                    path=f"{criterion_path}.verifier_config",
                    code="verifier-config-missing",
                    message="verifier_config is required and must exactly match the verifier",
                    missing_fields=("verifier_config",),
                )
            )
            criterion_has_error = True
        elif not isinstance(verifier_config, Mapping):
            diagnostics.append(
                _acceptance_diagnostic(
                    task_id=task_id,
                    criterion_id=criterion_id,
                    path=f"{criterion_path}.verifier_config",
                    code="verifier-config-invalid",
                    message="verifier_config must be an object",
                    invalid_fields=("verifier_config",),
                )
            )
            criterion_has_error = True
        elif isinstance(verifier, str) and verifier in VERIFIER_CONTRACTS:
            if _criterion_verifier_config(verifier, raw_criterion) is None:
                expected_fields = frozenset(VERIFIER_CONTRACTS[verifier]["config_fields"])
                observed_fields = {str(field) for field in verifier_config}
                missing_config_fields = tuple(
                    f"verifier_config.{field}"
                    for field in sorted(expected_fields - observed_fields)
                )
                unexpected_config_fields = tuple(
                    f"verifier_config.{field}"
                    for field in sorted(observed_fields - expected_fields)
                )
                invalid_config_fields = unexpected_config_fields
                if not missing_config_fields and not invalid_config_fields:
                    invalid_config_fields = tuple(
                        f"verifier_config.{field}" for field in sorted(expected_fields)
                    )
                diagnostics.append(
                    _acceptance_diagnostic(
                        task_id=task_id,
                        criterion_id=criterion_id,
                        path=f"{criterion_path}.verifier_config",
                        code="verifier-config-mismatch",
                        message=(f"verifier_config does not exactly match verifier {verifier!r}"),
                        missing_fields=missing_config_fields,
                        invalid_fields=invalid_config_fields or ("verifier_config",),
                    )
                )
                criterion_has_error = True

        if not criterion_has_error and criterion_contract(raw_criterion) is None:
            diagnostics.append(
                _acceptance_diagnostic(
                    task_id=task_id,
                    criterion_id=criterion_id,
                    path=criterion_path,
                    code="criterion-not-executable",
                    message="criterion does not resolve to an executable typed contract",
                    invalid_fields=("evidence_type", "verifier", "verifier_config"),
                )
            )

    # Pin criterion identities, verifier classes, scopes and exact probe names
    # before either claim admission or eventual evidence reconciliation.
    if task_id == "WELTGEWEBE-OS-V1-T044":
        observed_ids = {
            item.get("id")
            for item in criteria
            if isinstance(item, Mapping) and isinstance(item.get("id"), str)
        }
        if len(criteria) != len(T044_REQUIRED_ACCEPTANCE) or observed_ids != set(
            T044_REQUIRED_ACCEPTANCE
        ):
            diagnostics.append(
                _acceptance_diagnostic(
                    task_id=task_id,
                    criterion_id="<acceptance>",
                    path="$.acceptance",
                    code="t044-proof-contract-identity-mismatch",
                    message="t044-proof-contract: exact eleven criterion ids are mandatory",
                    invalid_fields=("acceptance",),
                )
            )
        for index, item in enumerate(criteria):
            if not isinstance(item, Mapping):
                continue
            identity = item.get("id")
            expected = T044_REQUIRED_ACCEPTANCE.get(identity) if isinstance(identity, str) else None
            config = item.get("verifier_config")
            if expected is None or not isinstance(config, Mapping):
                continue
            verifier, scope, probes = expected
            actual_probes = (
                tuple(config["required_probes"])
                if isinstance(config.get("required_probes"), list)
                else ()
                if "required_probes" not in config
                else None
            )
            if (
                item.get("verifier") != verifier
                or config.get("observation_scope") != scope
                or actual_probes != probes
            ):
                diagnostics.append(
                    _acceptance_diagnostic(
                        task_id=task_id,
                        criterion_id=identity,
                        path=f"$.acceptance[{index}]",
                        code="t044-proof-contract-unpinned",
                        message=(
                            "t044-proof-contract: verifier, observation scope and required "
                            "probes must match the immutable T044 acceptance contract"
                        ),
                        invalid_fields=("verifier", "verifier_config"),
                    )
                )

    # A T044 TaskSpec can contain nine individually valid runtime criteria
    # while referring to different clusters or releases. Reject that before
    # dispatch, not only after evidence has been produced at closeout. Both
    # expected identifiers may remain None while Green is not authorized.
    t044_target: tuple[str | None, str | None] | None = None
    for index, item in enumerate(criteria):
        if not isinstance(item, Mapping) or item.get("verifier") != "runtime_proof_bundle":
            continue
        contract = criterion_contract(item)
        if contract is None:
            continue  # The individual malformed contract is diagnosed above.
        config = contract["verifier_config"]
        if not config["observation_scope"].startswith("weltgewebe:t044:"):
            continue
        pair = (
            config["expected_runtime_revision"],
            config["expected_target_identity"],
        )
        if t044_target is None:
            t044_target = pair
        elif pair != t044_target:
            diagnostics.append(
                _acceptance_diagnostic(
                    task_id=task_id,
                    criterion_id=str(item["id"]),
                    path=f"$.acceptance[{index}].verifier_config",
                    code="t044-target-inconsistent",
                    message=(
                        "t044-target-inconsistent: all T044 runtime proof bundles "
                        "must freeze one identical release and Green target"
                    ),
                    invalid_fields=(
                        "verifier_config.expected_runtime_revision",
                        "verifier_config.expected_target_identity",
                    ),
                )
            )

    if diagnostics:
        raise AcceptanceContractError(diagnostics)


def t044_claim_admission_reasons(
    task: Mapping[str, Any],
    *,
    runtime_reader_registered: bool = False,
    registered_resources: Mapping[str, Any] | None = None,
) -> list[str]:
    """Block T044 claims before *any* workload starts unless its target and source exist.

    Structural TaskSpec readability deliberately permits nine null target
    placeholders. Claiming/executing a production cutover does not.
    A true registration flag is supplied only by trusted operator code,
    never by task evidence or the worker.
    """
    if task.get("id") != "WELTGEWEBE-OS-V1-T044":
        return []
    try:
        validate_acceptance_contract(task)
    except AcceptanceContractError as exc:
        return [f"T044 acceptance contract invalid: {str(exc).partition(chr(10))[0]}"]
    criteria = task["acceptance"]
    proofs = [c for c in criteria if c["verifier"] == "runtime_proof_bundle"]
    manual = [c for c in criteria if c["verifier"] == "manual_observation"]
    if len(criteria) != 11 or len(proofs) != 9 or len(manual) != 2:
        return ["T044 claim blocked: missing or changed eleven-criterion proof contract"]
    config = proofs[0]["verifier_config"]
    if config["expected_runtime_revision"] is None or config["expected_target_identity"] is None:
        return ["T044 claim blocked: authorized Green target and runtime revision not frozen"]
    # Contract validation above enforces one target/release for all nine.
    if not runtime_reader_registered:
        return ["T044 claim blocked: trusted primary runtime proof reader not registered"]

    # Reader availability alone does not own the two runtimes. A TaskSpec is
    # allowed to name only the Green identity; the separately reviewed Bureau
    # resource registry resolves that identity to one specific exclusive
    # runtime scope, rather than trusting a producer-supplied claim.
    if registered_resources is None:
        return ["T044 claim blocked: runtime resource registry unavailable"]

    def attr(resource: Any, field: str) -> Any:
        if isinstance(resource, Mapping):
            return resource.get(field)
        return getattr(resource, field, None)

    blue_id = "runtime.commonserver"
    blue = registered_resources.get(blue_id)
    if (
        blue is None
        or attr(blue, "type") != "service"
        or not isinstance(attr(blue, "grabowski_key"), str)
        or not attr(blue, "grabowski_key")
    ):
        return ["T044 claim blocked: canonical Blue runtime resource unavailable"]

    green_matches: list[str] = []
    for resource_id, resource in registered_resources.items():
        if not isinstance(resource_id, str) or not resource_id.startswith("runtime."):
            continue
        metadata = attr(resource, "metadata")
        if (
            attr(resource, "id") == resource_id
            and attr(resource, "type") == "service"
            and isinstance(attr(resource, "grabowski_key"), str)
            and bool(attr(resource, "grabowski_key"))
            and isinstance(metadata, Mapping)
            and metadata.get("t044_role") == "green"
            and metadata.get("target_identity") == config["expected_target_identity"]
            and metadata.get("expected_runtime_revision") == config["expected_runtime_revision"]
        ):
            green_matches.append(resource_id)
    if len(green_matches) != 1:
        return [
            "T044 claim blocked: Green target must match exactly one registered runtime resource"
        ]
    green_id = green_matches[0]
    green = registered_resources[green_id]

    def ancestors(resource_id: str) -> tuple[set[str], bool]:
        chain: set[str] = set()
        current = resource_id
        while current in registered_resources:
            parent = attr(registered_resources[current], "parent")
            if not isinstance(parent, str) or not parent or parent in chain or parent == current:
                return chain, False
            chain.add(parent)
            # The generic registry root is a grouping sentinel, never a host proof.
            if parent == "root":
                return chain, True
            if parent not in registered_resources:
                return chain, False
            current = parent
        return chain, False

    blue_ancestors, blue_chain_complete = ancestors(blue_id)
    green_ancestors, green_chain_complete = ancestors(green_id)
    if not (blue_chain_complete and green_chain_complete):
        return ["T044 claim blocked: runtime ancestor missing or cyclic in resource registry"]
    if (
        green_id == blue_id
        or blue_id in green_ancestors
        or green_id in blue_ancestors
        or attr(green, "grabowski_key") == attr(blue, "grabowski_key")
    ):
        return ["T044 claim blocked: Green is an alias or overlapping resource of production Blue"]

    # Two differently named sibling services can still use one host or fault
    # domain. Resource types alone cannot authenticate physical identity.
    # Compare operator keys across *all* ancestors and the runtimes themselves:
    # renaming an alias to "service", "group" or "Host" must not hide overlap.
    def operator_key_owners(resource_ids: set[str]) -> dict[str, set[str]]:
        owners: dict[str, set[str]] = {}
        for resource_id in resource_ids:
            resource = registered_resources.get(resource_id)
            key = attr(resource, "grabowski_key") if resource is not None else None
            if isinstance(key, str) and key:
                owners.setdefault(key, set()).add(resource_id)
        return owners

    blue_key_owners = operator_key_owners(blue_ancestors | {blue_id})
    green_key_owners = operator_key_owners(green_ancestors | {green_id})
    alias_keys = {
        key for key in blue_key_owners.keys() & green_key_owners.keys()
        if any(
            blue_resource != green_resource
            for blue_resource in blue_key_owners[key]
            for green_resource in green_key_owners[key]
        )
    }
    shared_physical_domains = sorted(
        {f"resource:{ancestor_id}" for ancestor_id in blue_ancestors & green_ancestors
         if ancestor_id in registered_resources
         and attr(registered_resources[ancestor_id], "type") in {"host", "failure-domain"}}
        | {f"grabowski-key:{key}" for key in alias_keys}
    )
    if shared_physical_domains:
        return [
            "T044 claim blocked: shared registered physical failure-domain: "
            + ", ".join(shared_physical_domains)
        ]

    # Unknown/non-group shared ancestors can conceal one physical resource even
    # when the registry marks it as a service instead of a host or fault domain.
    shared_non_group = sorted(
        ancestor_id for ancestor_id in blue_ancestors & green_ancestors
        if ancestor_id != "root"
        and attr(registered_resources[ancestor_id], "type") not in {"group", "git-repository"}
    )
    if shared_non_group:
        return [
            "T044 claim blocked: shared non-group runtime ancestor: "
            + ", ".join(shared_non_group)
        ]

    raw_claims = task.get("claims")
    exclusive = (
        {
            claim.get("resource")
            for claim in raw_claims
            if isinstance(claim, Mapping)
            and claim.get("mode") == "exclusive"
            and claim.get("amount", 1) == 1
            and claim.get("isolation", "none") == "none"
        }
        if isinstance(raw_claims, list)
        else set()
    )
    missing_claims = sorted({blue_id, green_id} - exclusive)
    if missing_claims:
        return [
            "T044 claim blocked: exclusive Blue and Green runtime resource claims required: "
            + ", ".join(missing_claims)
        ]

    # This operator-owned identity is part of the immutable TaskSpec snapshot
    # carried by each run envelope. A matching logical Green name alone cannot
    # authorize a changed host, Grabowski lease key or resource id.
    metadata = task.get("metadata")
    frozen_binding = (
        metadata.get("t044_runtime_identity")
        if isinstance(metadata, Mapping)
        else None
    )
    expected_binding = {
        "schema_version": 1,
        "blue_resource_id": blue_id,
        "blue_grabowski_key": attr(blue, "grabowski_key"),
        "green_resource_id": green_id,
        "green_grabowski_key": attr(green, "grabowski_key"),
        "runtime_revision": config["expected_runtime_revision"],
        "target_identity": config["expected_target_identity"],
    }
    if not isinstance(frozen_binding, Mapping) or dict(frozen_binding) != expected_binding:
        return [
            "T044 claim blocked: frozen runtime resource identity differs from "
            "current Blue/Green ids, Grabowski keys, revision or target"
        ]
    return []


def typed_criterion_contracts(criteria: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    invalid: list[str] = []
    for criterion in criteria:
        criterion_id = criterion.get("id")
        if not isinstance(criterion_id, str) or not criterion_id:
            invalid.append("<missing-id>")
            continue
        contract = criterion_contract(criterion)
        if contract is None:
            invalid.append(criterion_id)
            continue
        items.append({"criterion_id": criterion_id, **contract})
    return {
        "schema_version": SCHEMA_VERSION,
        "complete": not invalid,
        "criteria": items,
        "invalid_criterion_ids": invalid,
    }


def _unknown(
    criterion_id: str, verifier: str | None, reason: str, domain: str | None
) -> dict[str, Any]:
    return {
        "criterion_id": criterion_id,
        "verifier": verifier,
        "domain": domain,
        "state": UNKNOWN,
        "reason": reason,
    }


def _fact_state(
    verifier: str, facts: Mapping[str, Any], verifier_config: Mapping[str, Any]
) -> tuple[str, str]:
    if verifier == "code_merged":
        merged = facts.get("merged")
        if merged is True and _sha(facts.get("head_sha")) and _sha(facts.get("merge_commit_sha")):
            return PASSED, "merged-head-observed"
        if (
            merged is False
            and _sha(facts.get("head_sha"))
            and facts.get("merge_commit_sha") is None
        ):
            return FAILED, "merge-observed-not-complete"
        return UNKNOWN, "merge-facts-incomplete"

    if verifier == "required_ci_green":
        checks = facts.get("checks")
        required_checks = verifier_config.get("required_checks")
        if not isinstance(required_checks, list) or not required_checks:
            return UNKNOWN, "required-check-contract-invalid"
        if facts.get("complete") is not True:
            return UNKNOWN, "required-check-observation-incomplete"
        reported_required_checks = facts.get("required_checks")
        if reported_required_checks is not None and reported_required_checks != required_checks:
            return UNKNOWN, "required-check-set-mismatch"
        if not isinstance(checks, list) or not checks:
            return UNKNOWN, "required-checks-unavailable"
        observed: dict[str, str] = {}
        for check in checks:
            if not isinstance(check, Mapping):
                return UNKNOWN, "required-check-row-invalid"
            name = check.get("name")
            state = check.get("state")
            if not isinstance(name, str) or not name or not isinstance(state, str):
                return UNKNOWN, "required-check-row-incomplete"
            lowered = state.lower()
            if name in observed and observed[name] != lowered:
                return UNKNOWN, "required-check-row-conflict"
            observed[name] = lowered
        missing = [name for name in required_checks if name not in observed]
        if missing:
            return UNKNOWN, "required-check-missing"
        required_states = [observed[name] for name in required_checks]
        if any(state in _FAIL_CHECK_STATES for state in required_states):
            return FAILED, "required-check-failed"
        if any(
            state in _PENDING_CHECK_STATES or state not in _PASS_CHECK_STATES
            for state in required_states
        ):
            return UNKNOWN, "required-check-not-terminal-green"
        return PASSED, "required-checks-green"

    if verifier == "deployment_complete":
        if facts.get("failed") is True:
            return FAILED, "deployment-observed-failed"
        if facts.get("completed") is True and _sha256(facts.get("receipt_sha256")):
            return PASSED, "deployment-receipt-complete"
        return UNKNOWN, "deployment-not-completely-observed"

    if verifier == "runtime_commit_contains":
        contains = facts.get("contains_required_commit")
        if contains is True:
            return PASSED, "runtime-contains-required-commit"
        if contains is False:
            return FAILED, "runtime-does-not-contain-required-commit"
        return UNKNOWN, "runtime-containment-unknown"

    if verifier == "live_probe_passed":
        result = facts.get("result")
        if isinstance(result, str):
            lowered = result.lower()
            if lowered in {"pass", "passed", "success"}:
                return PASSED, "live-probe-passed"
            if lowered in {"fail", "failed", "failure", "error"}:
                return FAILED, "live-probe-failed"
        return UNKNOWN, "live-probe-unknown"

    if verifier == "runtime_proof_bundle":
        required = verifier_config.get("required_probes")
        probes = facts.get("probes")
        if (
            not isinstance(required, list)
            or not isinstance(probes, list)
            or len(probes) != len(required)
            or not isinstance(facts.get("runtime_revision"), str)
            or not isinstance(facts.get("target_identity"), str)
            or not isinstance(facts.get("observer"), str)
            or not facts["observer"].strip()
        ):
            return UNKNOWN, "runtime-probes-incomplete"
        observed: set[str] = set()
        failed = False
        expected_fields = {
            "id",
            "result",
            "receipt_sha256",
            "receipt_ref",
            "runtime_revision",
            "target_identity",
        }
        for probe in probes:
            if not isinstance(probe, Mapping) or set(probe) != expected_fields:
                return UNKNOWN, "runtime-probe-row-invalid"
            name = probe.get("id")
            if (
                not isinstance(name, str)
                or name not in required
                or name in observed
                or not _sha256(probe.get("receipt_sha256"))
                or not isinstance(probe.get("receipt_ref"), str)
                or not probe["receipt_ref"].strip()
                or len(probe["receipt_ref"]) > 256
                or probe.get("runtime_revision") != facts["runtime_revision"]
                or probe.get("target_identity") != facts["target_identity"]
            ):
                return UNKNOWN, "runtime-probe-binding-invalid"
            observed.add(name)
            outcome = probe.get("result")
            if not isinstance(outcome, str) or outcome not in {"passed", "failed"}:
                return UNKNOWN, "runtime-probe-result-invalid"
            failed |= outcome == "failed"
        if observed != set(required):
            return UNKNOWN, "runtime-probes-missing"
        return (
            (FAILED, "runtime-probe-failed")
            if failed
            else (PASSED, "all-bound-runtime-probes-passed")
        )

    if verifier == "manual_observation":
        accepted = facts.get("accepted")
        observer = facts.get("observer")
        observation = facts.get("observation")
        if (
            not isinstance(observer, str)
            or not observer
            or not isinstance(observation, str)
            or not observation
        ):
            return UNKNOWN, "manual-observation-incomplete"
        if accepted is True:
            return PASSED, "manual-observation-accepted"
        if accepted is False:
            return FAILED, "manual-observation-rejected"
        return UNKNOWN, "manual-observation-undecided"

    if verifier == "duration_soak_completed":
        required = verifier_config.get("required_seconds")
        observed = facts.get("observed_seconds")
        if not _valid_soak_seconds(required):
            return UNKNOWN, "soak-contract-invalid"
        reported_required = facts.get("required_seconds")
        if reported_required is not None and reported_required != required:
            return UNKNOWN, "soak-requirement-mismatch"
        if not _valid_soak_seconds(observed):
            return UNKNOWN, "soak-observation-invalid"
        if facts.get("failed") is True:
            return FAILED, "soak-observed-failed"
        if facts.get("completed") is not True:
            return UNKNOWN, "soak-still-running"
        if observed < required:
            return FAILED, "soak-duration-too-short"
        return PASSED, "soak-duration-satisfied"

    if verifier == "no_effect_verified":
        count = facts.get("effect_count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            return UNKNOWN, "effect-count-unavailable"
        if count == 0:
            return PASSED, "no-effect-observed"
        return FAILED, "unexpected-effect-observed"

    if verifier == "artifact_hash_matches":
        expected = facts.get("expected_sha256")
        observed = facts.get("observed_sha256")
        if not _sha256(expected) or not _sha256(observed):
            return UNKNOWN, "artifact-hash-unavailable"
        if expected == observed:
            return PASSED, "artifact-hash-matches"
        return FAILED, "artifact-hash-mismatch"

    return UNKNOWN, "unsupported-verifier"


def _revision_valid(
    contract: Mapping[str, Any],
    evidence: Mapping[str, Any],
    *,
    task_sha256: str,
    plan_sha256: str | None,
) -> tuple[bool, str]:
    revision = evidence.get("revision")
    if not isinstance(revision, Mapping):
        return False, "revision-binding-missing"
    if contract.get("verifier") == "runtime_proof_bundle":
        expected = contract.get("verifier_config")
        if not isinstance(expected, Mapping):
            return False, "runtime-proof-config-missing"
        expected_release = expected.get("expected_runtime_revision")
        expected_target = expected.get("expected_target_identity")
        if expected_release is None or expected_target is None:
            return False, "runtime-proof-target-not-frozen"
        if plan_sha256 is None or not _sha(revision.get("runtime_revision")):
            return False, "runtime-proof-revision-or-plan-missing"
        if (
            revision.get("runtime_revision") != expected_release
            or revision.get("target_identity") != expected_target
        ):
            return False, "runtime-proof-frozen-target-mismatch"
    if revision.get("task_sha256") != task_sha256 or not _sha256(task_sha256):
        return False, "task-revision-mismatch"
    if plan_sha256 is not None and (
        revision.get("plan_sha256") != plan_sha256 or not _sha256(plan_sha256)
    ):
        return False, "plan-revision-mismatch"
    required = contract.get("revision_binding")
    if not isinstance(required, tuple):
        return False, "verifier-revision-contract-invalid"
    verifier = contract.get("verifier")
    for field in required:
        if field == "plan_sha256" and plan_sha256 is None:
            continue
        value = revision.get(field)
        if verifier == "code_merged" and field == "merge_commit_sha" and value is None:
            continue
        if not isinstance(value, str) or not value:
            return False, f"revision-field-missing:{field}"
        if field.endswith("_sha256") and not _sha256(value):
            return False, f"revision-field-invalid:{field}"
        if field in {
            "head_sha",
            "merge_commit_sha",
            "required_commit",
            "observed_commit",
        } and not _sha(value):
            return False, f"revision-field-invalid:{field}"

    config = contract.get("verifier_config")
    if not isinstance(config, Mapping):
        return False, "verifier-config-invalid"
    target_bindings: dict[str, tuple[tuple[str, str], ...]] = {
        "code_merged": (("head_sha", "head_sha"), ("base_ref", "base_ref")),
        "required_ci_green": (("head_sha", "head_sha"), ("base_ref", "base_ref")),
        "deployment_complete": (("deployment_revision", "deployment_revision"),),
        "runtime_commit_contains": (("required_commit", "required_commit"),),
        "live_probe_passed": (
            ("runtime_revision", "runtime_revision"),
            ("probe_id", "probe_id"),
        ),
        "manual_observation": (("observation_scope", "observation_scope"),),
        "runtime_proof_bundle": (("observation_scope", "observation_scope"),),
        "duration_soak_completed": (("observation_scope", "observation_scope"),),
        "no_effect_verified": (("scope_sha256", "scope_sha256"),),
        "artifact_hash_matches": (("artifact_sha256", "artifact_sha256"),),
    }
    for revision_field, config_field in target_bindings.get(str(verifier), ()):
        if revision.get(revision_field) != config.get(config_field):
            return False, f"criterion-target-mismatch:{revision_field}"
    return True, "revision-bound"


def _facts_match_revision(
    verifier: str, revision: Mapping[str, Any], facts: Mapping[str, Any]
) -> tuple[bool, str]:
    bindings: dict[str, tuple[tuple[str, str], ...]] = {
        "code_merged": (
            ("head_sha", "head_sha"),
            ("base_ref", "base_ref"),
            ("merge_commit_sha", "merge_commit_sha"),
        ),
        "required_ci_green": (("head_sha", "head_sha"), ("base_ref", "base_ref")),
        "deployment_complete": (("deployment_revision", "deployment_revision"),),
        "runtime_commit_contains": (
            ("required_commit", "required_commit"),
            ("observed_commit", "observed_commit"),
        ),
        "live_probe_passed": (
            ("runtime_revision", "runtime_revision"),
            ("probe_id", "probe_id"),
        ),
        "manual_observation": (("observation_scope", "observation_scope"),),
        "runtime_proof_bundle": (
            ("observation_scope", "observation_scope"),
            ("runtime_revision", "runtime_revision"),
            ("target_identity", "target_identity"),
        ),
        "duration_soak_completed": (("observation_scope", "observation_scope"),),
        "no_effect_verified": (("scope_sha256", "scope_sha256"),),
        "artifact_hash_matches": (("expected_sha256", "artifact_sha256"),),
    }
    for fact_field, revision_field in bindings.get(verifier, ()):
        if facts.get(fact_field) != revision.get(revision_field):
            return False, f"fact-revision-mismatch:{fact_field}"
    return True, "facts-revision-bound"


def evaluate_criterion(
    criterion: Mapping[str, Any],
    evidence: Any,
    *,
    task_sha256: str,
    plan_sha256: str | None,
    now: str | None = None,
    source_authenticated: bool = False,
) -> dict[str, Any]:
    criterion_id = criterion.get("id")
    if not isinstance(criterion_id, str) or not criterion_id:
        return _unknown("<missing-id>", None, "criterion-id-invalid", None)
    contract = criterion_contract(criterion)
    if contract is None:
        return _unknown(criterion_id, None, "criterion-is-not-typed", None)
    verifier = str(contract["verifier"])
    domain = str(contract["domain"])
    if not isinstance(evidence, Mapping):
        return _unknown(criterion_id, verifier, "evidence-missing-or-unreadable", domain)
    if evidence.get("schema_version") != SCHEMA_VERSION or evidence.get("kind") != EVIDENCE_KIND:
        return _unknown(criterion_id, verifier, "evidence-contract-invalid", domain)
    if evidence.get("criterion_id") != criterion_id or evidence.get("evidence_type") != verifier:
        return _unknown(criterion_id, verifier, "evidence-binding-mismatch", domain)
    source = evidence.get("source")
    authority = source.get("authority") if isinstance(source, Mapping) else None
    if authority not in contract["authorities"]:
        return _unknown(criterion_id, verifier, "evidence-authority-invalid", domain)
    source_reference = source.get("reference") if isinstance(source, Mapping) else None
    if not isinstance(source_reference, str) or not source_reference.strip():
        return _unknown(criterion_id, verifier, "evidence-source-reference-missing", domain)
    if not source_authenticated:
        return _unknown(criterion_id, verifier, "evidence-source-unauthenticated", domain)
    observed_at = _parse_time(evidence.get("observed_at"))
    current = _now(now)
    if observed_at is None:
        return _unknown(criterion_id, verifier, "evidence-time-invalid", domain)
    if observed_at > current:
        return _unknown(criterion_id, verifier, "evidence-from-future", domain)
    age = (current - observed_at).total_seconds()
    if age > int(contract["max_age_seconds"]):
        return _unknown(criterion_id, verifier, "evidence-stale", domain)
    validity = evidence.get("validity")
    if isinstance(validity, Mapping) and validity.get("not_after") is not None:
        not_after = _parse_time(validity.get("not_after"))
        if not_after is None or current > not_after:
            return _unknown(criterion_id, verifier, "evidence-validity-expired", domain)
    revision_ok, revision_reason = _revision_valid(
        contract,
        evidence,
        task_sha256=task_sha256,
        plan_sha256=plan_sha256,
    )
    if not revision_ok:
        return _unknown(criterion_id, verifier, revision_reason, domain)
    facts = evidence.get("facts")
    if not isinstance(facts, Mapping):
        return _unknown(criterion_id, verifier, "evidence-facts-missing", domain)
    revision = evidence.get("revision")
    assert isinstance(revision, Mapping)  # established by _revision_valid
    facts_ok, facts_reason = _facts_match_revision(verifier, revision, facts)
    if not facts_ok:
        return _unknown(criterion_id, verifier, facts_reason, domain)
    derived_state, reason = _fact_state(verifier, facts, contract["verifier_config"])
    claimed_state = evidence.get("status")
    if claimed_state is not None:
        if claimed_state not in STATES:
            return _unknown(criterion_id, verifier, "evidence-status-invalid", domain)
        if claimed_state != derived_state:
            return _unknown(criterion_id, verifier, "contradictory-evidence", domain)
    return {
        "criterion_id": criterion_id,
        "verifier": verifier,
        "domain": domain,
        "state": derived_state,
        "reason": reason,
        "authority": authority,
        "source_reference": source_reference,
        "observed_at": evidence.get("observed_at"),
        "age_seconds": age,
        "revision_reason": revision_reason,
    }


_SHARED_REVISION_FIELDS = frozenset({"head_sha", "runtime_revision", "target_identity"})


def _shared_revision_scope(contract: Mapping[str, Any], field: str) -> str | None:
    """Return the target identity within which a revision field must agree."""
    if contract.get("verifier") == "runtime_proof_bundle" and field in {
        "runtime_revision",
        "target_identity",
    }:
        config = contract.get("verifier_config")
        scope = config.get("observation_scope") if isinstance(config, Mapping) else None
        if isinstance(scope, str) and scope.startswith("weltgewebe:t044:"):
            return "weltgewebe:t044"
    if field != "head_sha":
        return None
    verifier = contract.get("verifier")
    config = contract.get("verifier_config")
    if verifier not in {"code_merged", "required_ci_green"} or not isinstance(config, Mapping):
        return None
    repository = config.get("repository")
    pull_request = config.get("pull_request")
    if not isinstance(repository, str) or not repository or not isinstance(pull_request, int):
        return None
    return f"github-pr:{repository}#{pull_request}"


def _cross_criterion_revision_consistency(
    criteria: Sequence[Mapping[str, Any]],
    evidence: Mapping[str, Any],
    results: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    passed_ids = {str(item.get("criterion_id")) for item in results if item.get("state") == PASSED}
    observed: dict[str, dict[str, str]] = {}
    comparison_groups: dict[tuple[str, str], dict[str, str]] = {}
    for criterion in criteria:
        criterion_id = criterion.get("id")
        if not isinstance(criterion_id, str) or criterion_id not in passed_ids:
            continue
        contract = criterion_contract(criterion)
        item = evidence.get(criterion_id)
        revision = item.get("revision") if isinstance(item, Mapping) else None
        if contract is None or not isinstance(revision, Mapping):
            continue
        required = contract.get("revision_binding")
        if not isinstance(required, tuple):
            continue
        for field in _SHARED_REVISION_FIELDS.intersection(required):
            value = revision.get(field)
            if not isinstance(value, str) or not value:
                continue
            observed.setdefault(field, {})[criterion_id] = value
            scope = _shared_revision_scope(contract, field)
            if scope is not None:
                comparison_groups.setdefault((field, scope), {})[criterion_id] = value
    conflicts = [
        {"field": field, "criteria": bindings}
        for (field, _scope), bindings in sorted(comparison_groups.items())
        if len(set(bindings.values())) > 1
    ]
    return {
        "state": UNKNOWN if conflicts else PASSED,
        "shared_fields": observed,
        "conflicts": conflicts,
    }


def evaluate_acceptance(
    criteria: Sequence[Mapping[str, Any]],
    evidence: Mapping[str, Any],
    *,
    task_id: str,
    run_id: str,
    task_sha256: str,
    plan_sha256: str | None,
    now: str | None = None,
    authenticated_criterion_ids: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    evaluated_at = (_now(now)).isoformat().replace("+00:00", "Z")
    authenticated = authenticated_criterion_ids or frozenset()
    results = [
        evaluate_criterion(
            criterion,
            evidence.get(str(criterion.get("id"))),
            task_sha256=task_sha256,
            plan_sha256=plan_sha256,
            now=evaluated_at,
            source_authenticated=str(criterion.get("id")) in authenticated,
        )
        for criterion in criteria
    ]
    consistency = _cross_criterion_revision_consistency(criteria, evidence, results)
    states = {item["state"] for item in results}
    if FAILED in states:
        state = FAILED
    elif not results or UNKNOWN in states or consistency["state"] == UNKNOWN:
        state = UNKNOWN
    elif states == {PASSED}:
        state = PASSED
    else:
        state = UNKNOWN
    domains = sorted({str(item["domain"]) for item in results if item.get("domain")})
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": EVALUATION_KIND,
        "task_id": task_id,
        "run_id": run_id,
        "task_sha256": task_sha256,
        "plan_sha256": plan_sha256,
        "evaluated_at": evaluated_at,
        "state": state,
        "automatic_terminalization": state == PASSED,
        "criteria": results,
        "cross_criterion_revision": consistency,
        "domains": domains,
        "does_not_establish": [
            "merge_authority",
            "deployment_authority",
            "freshness_beyond_each_verifier_contract",
        ],
    }


def typed_evaluation_signal(value: Any) -> str | None:
    if not isinstance(value, Mapping) or value.get("kind") != EVALUATION_KIND:
        return None
    if value.get("schema_version") != SCHEMA_VERSION:
        return UNKNOWN
    state = value.get("state")
    if state not in STATES:
        return UNKNOWN
    automatic = value.get("automatic_terminalization")
    if state == PASSED and automatic is not True:
        return UNKNOWN
    if state != PASSED and automatic is True:
        return UNKNOWN
    return state