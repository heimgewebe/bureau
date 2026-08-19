from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import legacy, task_specs
from .acceptance import AcceptanceContractError
from .schema_validation import DocumentSchemaError, default_schema_set
from .task_specs import task_spec_digest
from .v2 import Registry, StateStore

SUPPLY_SCHEMA_VERSION = 1
SUPPLY_KIND = "bureau_task_supply_report"
FALLBACK_METADATA_KEY = "supply_fallback"
TERMINAL_TASK_STATES = frozenset({"verified", "cancelled", "superseded"})
REVIEW_REASON = "execution is interactive-agent/review-before-effect"
MISSING_CAPABILITIES_PREFIX = "missing capabilities: "
STRUCTURAL_UNREACHABLE_BLOCKER = "claimable-supply-floor-structurally-unreachable"


class SupplyError(RuntimeError):
    """Fail-closed task-supply contract violation."""


@dataclass(frozen=True)
class SupplyPolicy:
    schema_version: int = SUPPLY_SCHEMA_VERSION
    floor: int = 8
    refill_target: int = 12
    max_new_per_cycle: int = 4
    bucket_hours: int = 24

    def __post_init__(self) -> None:
        if self.schema_version != SUPPLY_SCHEMA_VERSION:
            raise ValueError(f"unsupported supply policy schema: {self.schema_version}")
        if self.floor < 8:
            raise ValueError("claimable supply floor must be at least 8")
        if self.refill_target <= self.floor:
            raise ValueError("refill target must be greater than the floor")
        if not 1 <= self.max_new_per_cycle <= 16:
            raise ValueError("max_new_per_cycle must be between 1 and 16")
        if not 1 <= self.bucket_hours <= 168:
            raise ValueError("bucket_hours must be between 1 and 168")


DEFAULT_SUPPLY_POLICY = SupplyPolicy()


@dataclass(frozen=True)
class FallbackSpec:
    category: str
    title: str
    goal: str
    capabilities: tuple[str, ...]
    claim_resource: str
    claim_mode: str
    scope_path: str
    acceptance: tuple[str, ...]
    repository_resource: str | None = None
    execution_policy: str = "review-before-effect"


_BASE_FALLBACK_CATALOG: tuple[FallbackSpec, ...] = (
    FallbackSpec(
        category="maintenance",
        title="Perform one bounded Bureau maintenance repair",
        goal=(
            "Select one current, reproducible Bureau maintenance defect, repair only its "
            "declared scope, and produce a revision-bound validation receipt."
        ),
        capabilities=("repository", "python", "testing", "bureau", "grabowski"),
        claim_resource="component.bureau.core",
        claim_mode="write",
        scope_path="src/bureau",
        acceptance=(
            "The selected defect is reproduced before repair and bound to an exact revision.",
            "The patch stays inside the declared scope and does not reset foreign work.",
            "Focused tests and the relevant Bureau validation gate pass.",
        ),
    ),
    FallbackSpec(
        category="care",
        title="Perform one bounded Bureau care pass",
        goal=(
            "Reduce one concrete hygiene, documentation, or operability burden without "
            "creating a second source of truth."
        ),
        capabilities=("repository", "documentation", "bureau", "grabowski"),
        claim_resource="component.bureau.docs",
        claim_mode="write",
        scope_path="docs",
        acceptance=(
            "One current care burden and its operational consequence are evidenced.",
            "The change removes or consolidates material instead of duplicating it.",
            "The resulting documentation remains linked to canonical Registry truth.",
        ),
    ),
    FallbackSpec(
        category="audit",
        title="Audit one bounded Bureau contract",
        goal=(
            "Audit one current Bureau contract against live code, Registry, runtime, and "
            "tests; repair only a proven gap or close out with reproducible evidence."
        ),
        capabilities=("repository", "audit", "python", "testing", "bureau", "grabowski"),
        claim_resource="component.bureau.docs",
        claim_mode="write",
        scope_path="docs/evidence",
        acceptance=(
            "The audited contract, revision, method, and evidence sources are explicit.",
            "Belegt, plausibel, and unresolved findings remain separated.",
            "Any repair has a negative regression test or the no-change closeout is justified.",
        ),
    ),
    FallbackSpec(
        category="diagnosis",
        title="Diagnose one current Bureau bottleneck",
        goal=(
            "Diagnose one current throughput or reliability bottleneck and produce either a "
            "bounded repair or a canonically registered follow-up with exact blockers."
        ),
        capabilities=("repository", "python", "testing", "bureau", "grabowski"),
        claim_resource="component.bureau.core",
        claim_mode="write",
        scope_path="tests",
        acceptance=(
            "The bottleneck is reproduced from current state rather than historical summary.",
            "The diagnosis names causal evidence, uncertainty, and excluded explanations.",
            "The outcome is either a tested repair or one bounded canonical follow-up.",
        ),
    ),
    FallbackSpec(
        category="registry-reconciliation",
        title="Reconcile one Bureau Registry drift",
        goal=(
            "Reconcile one current Registry drift without rewriting historical evidence or "
            "weakening fail-closed lifecycle checks."
        ),
        capabilities=("repository", "python", "bureau", "grabowski"),
        claim_resource="component.bureau.registry",
        claim_mode="write",
        scope_path="registry",
        acceptance=(
            "The drift is demonstrated against current canonical Registry and runtime state.",
            "The minimal reconciliation preserves unrelated queue and task semantics.",
            "Registry validation and lifecycle readback pass after the change.",
        ),
    ),
    FallbackSpec(
        category="error-investigation",
        title="Investigate one reproducible Bureau error",
        goal=(
            "Reproduce one current Bureau error, isolate the smallest causal surface, and "
            "add a regression test before or with the repair."
        ),
        capabilities=("repository", "python", "testing", "bureau", "grabowski"),
        claim_resource="component.bureau.core",
        claim_mode="write",
        scope_path="tests",
        acceptance=(
            "A deterministic reproduction or explicit non-reproducibility boundary exists.",
            "The causal claim is separated from symptoms and unrelated warnings.",
            "A regression test fails before the repair and passes afterwards when applicable.",
        ),
    ),
)


_SCOUT_REPOSITORIES: tuple[tuple[str, str], ...] = (
    ("commonworld", "repo.commonworld"),
    ("schauwerk", "repo.schauwerk"),
    ("chronik", "repo.chronik"),
    ("lenskit", "repo.lenskit"),
    ("systemkatalog", "repo.systemkatalog"),
    ("heimlern", "repo.heimlern"),
    ("semantah", "repo.semantah"),
    ("wgx", "repo.wgx"),
    ("aussensensor", "repo.aussensensor"),
    ("steuerboard", "repo.steuerboard"),
    ("plexer", "repo.plexer"),
    ("mitschreiber", "repo.mitschreiber"),
)


def _read_only_scout_spec(name: str, resource: str) -> FallbackSpec:
    return FallbackSpec(
        category=f"scout-{name}",
        title=f"Scout one bounded {name} maintenance opportunity",
        goal=(
            f"Read the current {name} repository at an exact revision and return one "
            "evidence-bound actionable maintenance finding or a no-change proof. Do not "
            "mutate the repository from this task."
        ),
        capabilities=("repository", "bureau", "grabowski"),
        claim_resource=resource,
        claim_mode="read",
        scope_path=".",
        acceptance=(
            "The inspected repository and exact revision are recorded before analysis.",
            "The scout performs no repository, runtime, queue, or StateStore mutation.",
            (
                "The outcome is one bounded actionable finding with evidence or a "
                "reproducible no-change proof."
            ),
        ),
        repository_resource=resource,
        execution_policy="autonomous",
    )


READ_ONLY_SCOUT_CATALOG: tuple[FallbackSpec, ...] = tuple(
    _read_only_scout_spec(name, resource) for name, resource in _SCOUT_REPOSITORIES
)
FALLBACK_CATALOG: tuple[FallbackSpec, ...] = (
    *_BASE_FALLBACK_CATALOG,
    *READ_ONLY_SCOUT_CATALOG,
)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid generated_at timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise ValueError("generated_at must include a timezone")
    return parsed.astimezone(timezone.utc)


def time_bucket(generated_at: str, bucket_hours: int) -> str:
    parsed = parse_timestamp(generated_at)
    width = bucket_hours * 3600
    return str(int(parsed.timestamp()) // width)


def _task_fallback_metadata(task: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(task, Mapping):
        return None
    metadata = task.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    marker = metadata.get(FALLBACK_METADATA_KEY)
    if not isinstance(marker, Mapping) or marker.get("schema_version") != SUPPLY_SCHEMA_VERSION:
        return None
    return dict(marker)


def _item_reasons(item: Mapping[str, Any]) -> list[str]:
    value = item.get("claim_reasons")
    if not isinstance(value, list):
        value = item.get("reasons")
    if not isinstance(value, list):
        return []
    return [str(reason) for reason in value if isinstance(reason, str) and reason]


def _effective_reasons(
    item: Mapping[str, Any],
    *,
    approval_available: bool,
    runtime_healthy: bool,
) -> list[str]:
    reasons = _item_reasons(item)
    if approval_available:
        reasons = [reason for reason in reasons if reason != REVIEW_REASON]
    if not runtime_healthy:
        reasons.append("required runtime is unhealthy")
    return sorted(set(reasons))


def _missing_capabilities_from_reasons(reasons: Sequence[str]) -> set[str]:
    result: set[str] = set()
    for reason in reasons:
        if not reason.startswith(MISSING_CAPABILITIES_PREFIX):
            continue
        result.update(
            item.strip()
            for item in reason[len(MISSING_CAPABILITIES_PREFIX) :].split(",")
            if item.strip()
        )
    return result


def derive_worker_capability_profile(
    frontier: Sequence[Mapping[str, Any]],
    task_documents: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    present: set[str] = set()
    missing: set[str] = set()
    evidence_task_ids: list[str] = []
    for item in frontier:
        task_id = str(item.get("task_id") or "")
        task = task_documents.get(task_id)
        if not isinstance(task, Mapping):
            continue
        raw_required = task.get("required_capabilities")
        if not isinstance(raw_required, list):
            continue
        required = {
            capability
            for capability in raw_required
            if isinstance(capability, str) and capability
        }
        observed_missing = _missing_capabilities_from_reasons(_item_reasons(item))
        missing.update(observed_missing)
        present.update(required - observed_missing)
        if required or observed_missing:
            evidence_task_ids.append(task_id)
    conflicts = present & missing
    capabilities = present - conflicts
    return {
        "schema_version": SUPPLY_SCHEMA_VERSION,
        "bound": bool(present or missing) and not conflicts,
        "source": "authoritative-frontier-task-capability-reasons",
        "capabilities": sorted(capabilities),
        "missing_capabilities": sorted(missing - conflicts),
        "conflicting_capabilities": sorted(conflicts),
        "evidence_task_ids": sorted(set(evidence_task_ids)),
    }


def _task_claims(task: Mapping[str, Any] | None) -> tuple[legacy.Claim, ...] | None:
    if not isinstance(task, Mapping):
        return None
    raw_claims = task.get("claims")
    if not isinstance(raw_claims, list):
        return None
    try:
        return tuple(
            legacy.Claim.from_raw(dict(claim))
            for claim in raw_claims
            if isinstance(claim, Mapping)
        )
    except (KeyError, TypeError, ValueError):
        return None


def _claims_conflict(
    left: Sequence[legacy.Claim],
    right: Sequence[legacy.Claim],
    resources: Mapping[str, legacy.Resource],
) -> tuple[str, str] | None:
    resource_map = dict(resources)
    for left_claim in left:
        for right_claim in right:
            if legacy.overlaps(left_claim.resource, right_claim.resource, resource_map) and (
                legacy.modes_conflict(left_claim.mode, right_claim.mode)
            ):
                return left_claim.resource, right_claim.resource
    return None


def _jointly_packable_claimable(
    classification: Mapping[str, Any],
    task_documents: Mapping[str, Mapping[str, Any]],
    resources: Mapping[str, legacy.Resource],
) -> tuple[list[str], list[tuple[legacy.Claim, ...]], dict[str, str]]:
    selected_ids: list[str] = []
    selected_claims: list[tuple[legacy.Claim, ...]] = []
    excluded: dict[str, str] = {}
    for item in classification["items"]:
        if item.get("claimable") is not True:
            continue
        task_id = str(item.get("task_id") or "")
        claims = _task_claims(task_documents.get(task_id))
        if claims is None:
            excluded[task_id] = "task-spec-claims-unavailable"
            continue
        conflict = next(
            (
                pair
                for held in selected_claims
                if (pair := _claims_conflict(claims, held, resources)) is not None
            ),
            None,
        )
        if conflict is not None:
            excluded[task_id] = f"pairwise-resource-conflict:{conflict[0]}:{conflict[1]}"
            continue
        selected_ids.append(task_id)
        selected_claims.append(claims)
    return selected_ids, selected_claims, excluded


def _catalog_capability_blockers(
    spec: FallbackSpec,
    worker_profile: Mapping[str, Any] | None,
    *,
    feasibility_required: bool,
) -> list[str]:
    if not feasibility_required:
        return []
    if not isinstance(worker_profile, Mapping) or worker_profile.get("bound") is not True:
        return ["worker-capability-profile-unbound"]
    present = {
        item
        for item in worker_profile.get("capabilities", [])
        if isinstance(item, str) and item
    }
    observed_missing = {
        item
        for item in worker_profile.get("missing_capabilities", [])
        if isinstance(item, str) and item
    }
    required = set(spec.capabilities)
    missing = sorted(required & observed_missing)
    unknown = sorted(required - present - observed_missing)
    blockers: list[str] = []
    if missing:
        blockers.append("missing-worker-capabilities:" + ",".join(missing))
    if unknown:
        blockers.append("worker-capability-evidence-missing:" + ",".join(unknown))
    return blockers


def _catalog_approval_blockers(
    spec: FallbackSpec,
    *,
    approval_available: bool,
    feasibility_required: bool,
) -> list[str]:
    if (
        feasibility_required
        and spec.execution_policy == "review-before-effect"
        and not approval_available
    ):
        return ["operator-approval-unavailable"]
    return []


def _structural_capability_possible(
    spec: FallbackSpec, worker_profile: Mapping[str, Any] | None
) -> bool:
    """Return whether capability evidence does not prove this fallback impossible.

    Structural reachability is an optimistic upper bound. Unknown or unbound
    capabilities therefore remain possible; only explicitly observed missing
    capabilities may reduce the upper bound. Actual publication stays fail-closed
    through ``_catalog_capability_blockers``.
    """
    if not isinstance(worker_profile, Mapping):
        return True
    observed_missing = {
        item
        for item in worker_profile.get("missing_capabilities", [])
        if isinstance(item, str) and item
    }
    return not bool(set(spec.capabilities) & observed_missing)


def _max_packable_catalog_count(
    specs: Sequence[FallbackSpec],
    resources: Mapping[str, legacy.Resource],
    *,
    held_claim_sets: Sequence[Sequence[legacy.Claim]] = (),
) -> int:
    """Return the exact maximum additional pairwise-compatible catalog capacity.

    Branch-and-bound keeps the calculation exact without the combinatorial full
    powerset walk that becomes expensive once the bounded ecosystem scout reserve
    is present. Existing jointly claimable work is treated as already held.
    """
    candidates: list[tuple[legacy.Claim, ...]] = []
    for spec in specs:
        claims = (legacy.Claim(spec.claim_resource, spec.claim_mode),)
        if any(
            _claims_conflict(claims, held, resources) is not None
            for held in held_claim_sets
        ):
            continue
        candidates.append(claims)

    best = 0

    def visit(index: int, selected: list[tuple[legacy.Claim, ...]]) -> None:
        nonlocal best
        if len(selected) + len(candidates) - index <= best:
            return
        if index >= len(candidates):
            best = max(best, len(selected))
            return
        claims = candidates[index]
        if all(_claims_conflict(claims, held, resources) is None for held in selected):
            selected.append(claims)
            visit(index + 1, selected)
            selected.pop()
        visit(index + 1, selected)

    visit(0, [])
    return best


def classify_frontier(
    frontier: Sequence[Mapping[str, Any]],
    *,
    task_documents: Mapping[str, Mapping[str, Any]] | None = None,
    approval_available: bool = False,
    runtime_healthy: bool = True,
) -> dict[str, Any]:
    documents = task_documents or {}
    items: list[dict[str, Any]] = []
    seen_task_ids: set[str] = set()
    for position, item in enumerate(frontier):
        task_id = str(item.get("task_id") or "")
        fallback = _task_fallback_metadata(documents.get(task_id))
        state = str(item.get("effective_state") or item.get("state") or "")
        raw_reasons = _item_reasons(item)
        reasons = _effective_reasons(
            item,
            approval_available=approval_available,
            runtime_healthy=runtime_healthy,
        )
        if not task_id:
            reasons.append("authoritative-frontier-task-id-missing")
        elif task_id in seen_task_ids:
            reasons.append("authoritative-frontier-duplicate-task-id")
        else:
            seen_task_ids.add(task_id)
        approval_override = approval_available and set(raw_reasons) == {REVIEW_REASON}
        frontier_eligible = (
            item.get("eligible") is True
            or item.get("claimable") is True
            or approval_override
        )
        if not frontier_eligible and not reasons:
            reasons.append("authoritative-frontier-not-eligible")
        reasons = sorted(set(reasons))
        claimable = state == "ready" and frontier_eligible and not reasons
        items.append(
            {
                "position": position,
                "task_id": task_id,
                "title": item.get("title"),
                "effective_state": state,
                "queue_lane": item.get("queue_lane"),
                "frontier_eligible": frontier_eligible,
                "fallback": fallback is not None,
                "fallback_category": None if fallback is None else fallback.get("category"),
                "claimable": claimable,
                "reasons": reasons,
            }
        )
    raw_ready = [item for item in items if item["effective_state"] == "ready"]
    normal_claimable = [item for item in items if item["claimable"] and not item["fallback"]]
    fallback_claimable = [item for item in items if item["claimable"] and item["fallback"]]
    blocked_ready = [item for item in raw_ready if not item["claimable"]]
    return {
        "items": items,
        "raw_ready": raw_ready,
        "normal_claimable": normal_claimable,
        "fallback_claimable": fallback_claimable,
        "blocked_ready": blocked_ready,
    }


def _existing_fallbacks(
    task_documents: Mapping[str, Mapping[str, Any]],
    *,
    terminal_task_ids: frozenset[str] = frozenset(),
) -> dict[str, tuple[str, dict[str, Any]]]:
    result: dict[str, tuple[str, dict[str, Any]]] = {}
    for task_id, task in sorted(task_documents.items()):
        marker = _task_fallback_metadata(task)
        if (
            marker is None
            or task.get("state") in TERMINAL_TASK_STATES
            or task_id in terminal_task_ids
        ):
            continue
        open_key = marker.get("open_key")
        if isinstance(open_key, str) and open_key:
            result.setdefault(open_key, (task_id, marker))
    return result


def _fallback_repository(
    spec: FallbackSpec,
    default_repository: Path,
    resources: Mapping[str, legacy.Resource],
) -> Path | None:
    if spec.repository_resource is None:
        return default_repository
    resource = resources.get(spec.repository_resource)
    path = resource.path if resource is not None else None
    if not isinstance(path, str) or not path:
        return None
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        return None
    return candidate.resolve(strict=False)


def _fallback_repository_blockers(
    spec: FallbackSpec,
    default_repository: Path,
    resources: Mapping[str, legacy.Resource],
) -> list[str]:
    if _fallback_repository(spec, default_repository, resources) is not None:
        return []
    resource = spec.repository_resource or "default"
    return [f"fallback-repository-resource-unavailable:{resource}"]


def _catalog_open_key(spec: FallbackSpec, repository: str, initiative_id: str) -> str:
    return sha256_json(
        {
            "schema_version": SUPPLY_SCHEMA_VERSION,
            "category": spec.category,
            "repository": repository,
            "initiative_id": initiative_id,
            "claim_resource": spec.claim_resource,
            "claim_mode": spec.claim_mode,
            "scope_path": spec.scope_path,
        }
    )


def _catalog_fingerprint(open_key: str, bucket: str) -> str:
    return sha256_json(
        {
            "schema_version": SUPPLY_SCHEMA_VERSION,
            "open_key": open_key,
            "time_bucket": bucket,
        }
    )


def _fallback_task_id(initiative_id: str, spec: FallbackSpec, fingerprint: str) -> str:
    category = spec.category.replace("-", "").upper()
    return f"{initiative_id}-FB-{category}-{fingerprint[:10].upper()}"


def _fallback_task(
    *,
    task_id: str,
    initiative_id: str,
    spec: FallbackSpec,
    repository: Path,
    fingerprint: str,
    open_key: str,
    bucket: str,
    rank: int,
    acceptance: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    exact_scope = (repository / spec.scope_path).resolve(strict=False)
    execution: dict[str, Any] = {
        "mode": "interactive-agent",
        "policy": spec.execution_policy,
        "working_repository": str(repository),
        "grabowski_resources": [f"path:{exact_scope}"],
    }
    if spec.claim_mode in {"write", "exclusive"}:
        execution["approval"] = {
            "action_class": "repository_mutation",
            "required_level": "operator",
            "note": (
                "Bounded supply fallback. Re-read live state, preserve foreign work, and "
                "stop when the stated scope or evidence no longer matches."
            ),
        }
    return {
        "schema_version": 1,
        "id": task_id,
        "initiative": initiative_id,
        "title": spec.title,
        "goal": spec.goal,
        "state": "ready",
        "depends_on": [],
        "required_capabilities": list(spec.capabilities),
        "priority": {"lane": "later", "rank": rank},
        "execution": execution,
        "claims": [
            {
                "resource": spec.claim_resource,
                "mode": spec.claim_mode,
                "isolation": "worktree" if spec.claim_mode == "write" else "none",
            }
        ],
        "acceptance": json.loads(json.dumps(list(acceptance))),
        "metadata": {
            FALLBACK_METADATA_KEY: {
                "schema_version": SUPPLY_SCHEMA_VERSION,
                "category": spec.category,
                "fingerprint": fingerprint,
                "open_key": open_key,
                "time_bucket": bucket,
                "scope_path": spec.scope_path,
                "repository_resource": spec.repository_resource,
                "generated_by_task": "OPERATOR-INTEGRATION-LOOP-V1-T014",
                "does_not_establish": [
                    "claimability before canonical StateStore publication",
                    "permission to bypass current leases or open-PR guards",
                    "merge or deployment authority",
                ],
            }
        },
    }


def default_fallback_acceptance_contracts() -> dict[str, list[dict[str, Any]]]:
    """Build executable typed acceptance from the server-owned fallback catalog.

    Fallback TaskSpecs are created before a concrete implementation PR or artifact
    exists, so their semantic criteria use the bounded manual-observation verifier.
    Repository, CI, merge and runtime gates remain separate lifecycle authorities.
    Explicit caller-provided acceptance mappings still override this default and are
    validated unchanged; an explicit empty mapping therefore remains fail-closed.
    """

    return {
        spec.category: [
            {
                "id": f"{spec.category}-{index:02d}",
                "assertion": assertion,
                "evidence_type": "object",
                "verifier": "manual_observation",
                "verifier_config": {
                    "observation_scope": (
                        f"bureau-task-supply:{spec.category}:{index:02d}"
                    )
                },
            }
            for index, assertion in enumerate(spec.acceptance, start=1)
        ]
        for spec in FALLBACK_CATALOG
    }


def build_supply_report(
    frontier: Sequence[Mapping[str, Any]],
    *,
    task_documents: Mapping[str, Mapping[str, Any]] | None = None,
    policy: SupplyPolicy | None = None,
    generated_at: str | None = None,
    repository: str | Path,
    registry_root: str | Path,
    registry_head: str,
    queue_sha256: str,
    initiative_id: str = "OPERATOR-INTEGRATION-LOOP-V1",
    approval_available: bool = False,
    runtime_healthy: bool = True,
    mutation_authority: bool = False,
    environment_blockers: Iterable[str] = (),
    catalog_blockers: Mapping[str, Sequence[str]] | None = None,
    frontier_snapshot_sha256: str | None = None,
    acceptance_contracts: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    worker_profile: Mapping[str, Any] | None = None,
    feasibility_required: bool = False,
    resource_definitions: Mapping[str, legacy.Resource] | None = None,
) -> dict[str, Any]:
    policy = policy or DEFAULT_SUPPLY_POLICY
    now = generated_at or utc_now()
    repository_path = Path(repository).resolve()
    registry_path = Path(registry_root).resolve()
    if len(registry_head) != 40 or any(char not in "0123456789abcdef" for char in registry_head):
        raise ValueError("registry_head must be a lowercase 40-character Git object id")
    if len(queue_sha256) != 64 or any(char not in "0123456789abcdef" for char in queue_sha256):
        raise ValueError("queue_sha256 must be a lowercase SHA-256 digest")
    if frontier_snapshot_sha256 is not None and (
        len(frontier_snapshot_sha256) != 64
        or any(char not in "0123456789abcdef" for char in frontier_snapshot_sha256)
    ):
        raise ValueError("frontier_snapshot_sha256 must be a lowercase SHA-256 digest")
    documents = dict(task_documents or {})
    classification = classify_frontier(
        frontier,
        task_documents=documents,
        approval_available=approval_available,
        runtime_healthy=runtime_healthy,
    )
    normal_count = len(classification["normal_claimable"])
    fallback_count = len(classification["fallback_claimable"])
    total_claimable = normal_count + fallback_count
    resources = dict(resource_definitions or {})
    catalog = FALLBACK_CATALOG if feasibility_required else _BASE_FALLBACK_CATALOG
    if feasibility_required and resources:
        joint_task_ids, joint_claim_sets, joint_excluded = _jointly_packable_claimable(
            classification, documents, resources
        )
    elif feasibility_required:
        joint_task_ids, joint_claim_sets, joint_excluded = [], [], {
            str(item.get("task_id") or ""): "resource-conflict-model-unbound"
            for item in classification["items"]
            if item.get("claimable") is True
        }
    else:
        joint_task_ids = [
            str(item["task_id"])
            for item in classification["items"]
            if item.get("claimable") is True
        ]
        joint_claim_sets = []
        joint_excluded = {}
    joint_claimable_count = (
        len(joint_task_ids) if feasibility_required else total_claimable
    )
    refill_triggered = joint_claimable_count < policy.floor
    shortage = (
        max(0, policy.refill_target - joint_claimable_count) if refill_triggered else 0
    )
    proposal_limit = min(shortage, policy.max_new_per_cycle, len(catalog))
    bucket = time_bucket(now, policy.bucket_hours)
    global_blockers = sorted(
        {
            str(blocker)
            for blocker in environment_blockers
            if isinstance(blocker, str) and blocker
        }
    )
    if not runtime_healthy:
        global_blockers.append("required-runtime-unhealthy")
    if feasibility_required:
        if not isinstance(worker_profile, Mapping) or worker_profile.get("bound") is not True:
            global_blockers.append("worker-capability-profile-unbound")
        if not resources:
            global_blockers.append("resource-conflict-model-unbound")
    category_blocks = catalog_blockers or {}
    fallback_repositories = {
        spec.category: _fallback_repository(spec, repository_path, resources)
        for spec in catalog
    }
    category_feasibility = {
        spec.category: [
            *_catalog_capability_blockers(
                spec, worker_profile, feasibility_required=feasibility_required
            ),
            *_fallback_repository_blockers(spec, repository_path, resources),
            *_catalog_approval_blockers(
                spec,
                approval_available=approval_available,
                feasibility_required=feasibility_required,
            ),
        ]
        for spec in catalog
    }
    explicit_acceptance = (
        default_fallback_acceptance_contracts()
        if acceptance_contracts is None
        else acceptance_contracts
    )
    frontier_by_task = {
        str(item["task_id"]): item
        for item in classification["items"]
        if isinstance(item.get("task_id"), str) and item["task_id"]
    }
    # A task document can still read "ready" after the state store closed it. The
    # authoritative frontier decides, otherwise a closed fallback is reused forever
    # and its category never refills again.
    existing = _existing_fallbacks(
        documents,
        terminal_task_ids=frozenset(
            task_id
            for task_id, item in frontier_by_task.items()
            if item.get("effective_state") in TERMINAL_TASK_STATES
        ),
    )
    structural_catalog: list[FallbackSpec] = []
    if feasibility_required and resources:
        for spec in catalog:
            target_repository = fallback_repositories[spec.category]
            open_key = _catalog_open_key(
                spec, str(target_repository or repository_path), initiative_id
            )
            explicit_category_blockers = [
                blocker
                for blocker in category_blocks.get(spec.category, ())
                if isinstance(blocker, str) and blocker
            ]
            if (
                target_repository is not None
                and open_key not in existing
                and _structural_capability_possible(spec, worker_profile)
                and not _fallback_repository_blockers(spec, repository_path, resources)
                and not _catalog_approval_blockers(
                    spec,
                    approval_available=approval_available,
                    feasibility_required=feasibility_required,
                )
                and not explicit_category_blockers
            ):
                structural_catalog.append(spec)
    structural_additional_capacity = (
        _max_packable_catalog_count(
            structural_catalog, resources, held_claim_sets=joint_claim_sets
        )
        if feasibility_required and resources
        else len(catalog)
    )
    structural_capacity_upper_bound = (
        joint_claimable_count + structural_additional_capacity
        if feasibility_required
        else total_claimable + len(catalog)
    )
    floor_reachable = (
        structural_capacity_upper_bound >= policy.floor
        if feasibility_required
        else True
    )
    if feasibility_required and not floor_reachable:
        global_blockers.append(STRUCTURAL_UNREACHABLE_BLOCKER)
    global_blockers = sorted(set(global_blockers))
    proposals: list[dict[str, Any]] = []
    created_count = 0
    planned_claim_sets: list[tuple[legacy.Claim, ...]] = []
    for index, spec in enumerate(catalog):
        if created_count >= proposal_limit:
            break
        target_repository = fallback_repositories[spec.category]
        open_key = _catalog_open_key(
            spec, str(target_repository or repository_path), initiative_id
        )
        fingerprint = _catalog_fingerprint(open_key, bucket)
        blockers = (
            set(global_blockers)
            | set(category_feasibility[spec.category])
            | {
                str(blocker)
                for blocker in category_blocks.get(spec.category, ())
                if isinstance(blocker, str) and blocker
            }
        )
        existing_match = existing.get(open_key)
        if existing_match is not None:
            existing_id, existing_marker = existing_match
            observed = frontier_by_task.get(existing_id)
            if observed is None:
                blockers.add("existing-fallback-not-present-in-authoritative-frontier")
                currently_claimable = False
            else:
                currently_claimable = bool(observed.get("claimable"))
                if not currently_claimable:
                    observed_reasons = observed.get("reasons")
                    if isinstance(observed_reasons, list) and observed_reasons:
                        blockers.update(str(reason) for reason in observed_reasons)
                    else:
                        blockers.add("existing-fallback-not-claimable")
            proposals.append(
                {
                    "category": spec.category,
                    "action": "reuse",
                    "task_id": existing_id,
                    "open_key": open_key,
                    "fingerprint": existing_marker.get("fingerprint"),
                    "time_bucket": existing_marker.get("time_bucket"),
                    "blockers": sorted(blockers),
                    "claimable": currently_claimable,
                    "canonical_publication_required": False,
                    "detail": "reuse existing nonterminal canonical fallback",
                }
            )
            continue
        task_id = _fallback_task_id(initiative_id, spec, fingerprint)
        if task_id in documents:
            # A terminal fallback keeps its canonical document for the rest of its time
            # bucket. Creating the identical id again would abort the whole publication,
            # so this category waits for the next bucket instead of deadlocking the rest.
            blockers.add("fallback-task-id-already-canonical-in-current-bucket")
        typed_criteria = explicit_acceptance.get(spec.category)
        task: dict[str, Any] | None = None
        if not isinstance(typed_criteria, Sequence) or isinstance(
            typed_criteria, (str, bytes)
        ) or not typed_criteria:
            blockers.add("acceptance-contract-unresolved")
        elif target_repository is None:
            blockers.update(category_feasibility[spec.category])
        else:
            task = _fallback_task(
                task_id=task_id,
                initiative_id=initiative_id,
                spec=spec,
                repository=target_repository,
                fingerprint=fingerprint,
                open_key=open_key,
                bucket=bucket,
                rank=900 + index,
                acceptance=typed_criteria,
            )
            try:
                default_schema_set().validate_task_write(
                    task, f"task-supply:{spec.category}:{task_id}"
                )
            except (DocumentSchemaError, AcceptanceContractError):
                blockers.add("acceptance-contract-invalid")
                task = None
        candidate_claims = (legacy.Claim(spec.claim_resource, spec.claim_mode),)
        if feasibility_required and not blockers and task is not None:
            conflict = next(
                (
                    pair
                    for held in (*joint_claim_sets, *planned_claim_sets)
                    if (pair := _claims_conflict(candidate_claims, held, resources))
                    is not None
                ),
                None,
            )
            if conflict is not None:
                blockers.add(
                    f"pairwise-resource-conflict:{conflict[0]}:{conflict[1]}"
                )
        proposal = {
            "category": spec.category,
            "action": "create",
            "task_id": task_id,
            "open_key": open_key,
            "fingerprint": fingerprint,
            "time_bucket": bucket,
            "blockers": sorted(blockers),
            "claimable": False,
            "canonical_publication_required": True,
            "task_path": f"registry/tasks/{task_id}.json",
            "queue_lane": "later",
        }
        if task is not None:
            proposal["task"] = task
        proposals.append(proposal)
        if not blockers:
            created_count += 1
            if feasibility_required:
                planned_claim_sets.append(candidate_claims)
    publishable_proposals = [
        proposal
        for proposal in proposals
        if proposal["action"] == "create" and not proposal["blockers"]
    ]
    report_blockers = list(global_blockers)
    if joint_claimable_count < policy.floor and not mutation_authority:
        report_blockers.append("registry-mutation-authority-unavailable")
    if (
        joint_claimable_count < policy.floor
        and mutation_authority
        and not publishable_proposals
    ):
        proposal_blockers = {
            str(blocker)
            for proposal in proposals
            for blocker in proposal["blockers"]
            if isinstance(blocker, str) and blocker
        }
        if proposal_blockers:
            report_blockers.extend(sorted(proposal_blockers))
        else:
            report_blockers.append("fallback-catalog-exhausted-without-publishable-candidate")
    report_blockers = sorted(set(report_blockers))
    if joint_claimable_count >= policy.floor:
        status = "satisfied"
    elif report_blockers:
        status = "blocked"
    else:
        status = "refill-proposed"
    actions = [
        {
            "action": proposal["action"],
            "category": proposal["category"],
            "task_id": proposal["task_id"],
            "task_path": proposal.get("task_path"),
            "queue_lane": proposal.get("queue_lane"),
            "open_key": proposal["open_key"],
            "fingerprint": proposal.get("fingerprint"),
            "blockers": proposal["blockers"],
            "task": proposal.get("task"),
        }
        for proposal in proposals
        if not proposal["blockers"]
    ]
    projected_joint_claimable_count = joint_claimable_count + len(
        publishable_proposals
    )
    plan = {
        "schema_version": SUPPLY_SCHEMA_VERSION,
        "kind": "bureau_task_supply_publication_plan",
        "generated_at": now,
        "registry": {
            "root": str(registry_path),
            "head": registry_head,
            "queue_sha256": queue_sha256,
        },
        "mutation_authority_observed": mutation_authority,
        "status": (
            "authorized"
            if mutation_authority and not report_blockers and publishable_proposals
            else "preview-only"
        ),
        "blockers": report_blockers,
        "actions": actions,
        "does_not_establish": [
            "completed Registry mutation",
            "claimability before post-publication eligibility readback",
            "merge authority",
        ],
    }
    plan["plan_sha256"] = sha256_json(
        {key: value for key, value in plan.items() if key != "plan_sha256"}
    )
    report = {
        "schema_version": SUPPLY_SCHEMA_VERSION,
        "kind": SUPPLY_KIND,
        "generated_at": now,
        "status": status,
        "policy": asdict(policy),
        "registry": plan["registry"],
        "frontier_source": {
            "snapshot_sha256": frontier_snapshot_sha256,
            "bound": frontier_snapshot_sha256 is not None,
        },
        "runtime_healthy": runtime_healthy,
        "approval_available": approval_available,
        "mutation_authority_observed": mutation_authority,
        "metrics": {
            "raw_ready_count": len(classification["raw_ready"]),
            "normal_claimable_count": normal_count,
            "fallback_claimable_count": fallback_count,
            "total_claimable_count": total_claimable,
            "joint_claimable_count": joint_claimable_count,
            "projected_joint_claimable_count": projected_joint_claimable_count,
            "structural_capacity_upper_bound": (
                structural_capacity_upper_bound if feasibility_required else None
            ),
            "blocked_ready_count": len(classification["blocked_ready"]),
            "floor": policy.floor,
            "refill_target": policy.refill_target,
            "shortage_to_target": shortage,
            "proposal_count": len(proposals),
            "new_proposal_count": sum(
                proposal["action"] == "create" and not proposal["blockers"]
                for proposal in proposals
            ),
            "blocked_proposal_count": sum(
                bool(proposal["blockers"]) for proposal in proposals
            ),
            "reused_proposal_count": sum(
                proposal["action"] == "reuse" for proposal in proposals
            ),
        },
        "feasibility": {
            "schema_version": SUPPLY_SCHEMA_VERSION,
            "required": feasibility_required,
            "worker_profile": (
                dict(worker_profile) if isinstance(worker_profile, Mapping) else None
            ),
            "joint_claimable_task_ids": joint_task_ids,
            "pairwise_excluded": joint_excluded,
            "structural_additional_capacity": (
                structural_additional_capacity if feasibility_required else None
            ),
            "structural_capacity_upper_bound": (
                structural_capacity_upper_bound if feasibility_required else None
            ),
            "projected_joint_claimable_count": projected_joint_claimable_count,
            "floor_reachable": floor_reachable,
            "catalog_capability_blockers": {
                category: blockers
                for category, blockers in sorted(category_feasibility.items())
                if blockers
            },
            "does_not_establish": [
                "permission to bypass capability gates",
                "permission to bypass leases or open-PR guards",
                "maximum future supply beyond the current evidence-bound profile",
            ],
        },
        "normal_claimable": classification["normal_claimable"],
        "fallback_claimable": classification["fallback_claimable"],
        "blocked_ready": classification["blocked_ready"],
        "blockers": report_blockers,
        "proposals": proposals,
        "publication_plan": plan,
        "does_not_establish": [
            "hidden or ephemeral work authority",
            "claimability before canonical publication and current eligibility checks",
            "permission to bypass leases, capabilities, runtime health, or open-PR guards",
            "automatic merge or deployment authority",
        ],
    }
    report["report_sha256"] = sha256_json(
        {key: value for key, value in report.items() if key != "report_sha256"}
    )
    return report


def _git_head(root: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    value = completed.stdout.strip()
    if completed.returncode != 0 or len(value) != 40:
        raise SupplyError("cannot read current Registry Git head")
    return value


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent), text=True
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def publish_supply_plan(
    plan: Mapping[str, Any],
    *,
    mutation_authorized: bool,
    expected_plan_sha256: str,
    state_db: Path | None = None,
    state_store_root: Path | None = None,
    head_reader: Callable[[Path], str] = _git_head,
    registry_loader: Callable[[str | Path], Registry] = Registry.load,
) -> dict[str, Any]:
    """Publish one bounded refill atomically to the authoritative StateStore."""
    del head_reader, registry_loader
    if not mutation_authorized:
        raise SupplyError("StateStore mutation authority is required")
    if state_db is None and state_store_root is None:
        raise SupplyError("StateStore publication requires an explicit Bureau StateStore path")
    claimed_plan_sha256 = plan.get("plan_sha256")
    observed_plan_sha256 = sha256_json(
        {key: value for key, value in plan.items() if key != "plan_sha256"}
    )
    if (
        claimed_plan_sha256 != expected_plan_sha256
        or observed_plan_sha256 != expected_plan_sha256
    ):
        raise SupplyError("publication plan digest mismatch")
    if plan.get("status") != "authorized" or plan.get("blockers"):
        raise SupplyError("publication plan is not authorized and blocker-free")
    registry_info = plan.get("registry")
    if not isinstance(registry_info, Mapping):
        raise SupplyError("publication plan has no Registry trace binding")
    expected_head = str(registry_info.get("head") or "")
    expected_queue_sha256 = str(registry_info.get("queue_sha256") or "")
    actions = plan.get("actions")
    if not isinstance(actions, list):
        raise SupplyError("publication plan actions must be a list")
    if any(not isinstance(action, Mapping) for action in actions):
        raise SupplyError("publication action must be an object")
    allowed_actions = {"create", "reuse"}
    if any(action.get("action") not in allowed_actions for action in actions):
        raise SupplyError("publication plan contains an unsupported action")
    create_actions = [action for action in actions if action.get("action") == "create"]
    if not create_actions:
        raise SupplyError("publication plan contains no create actions")
    if any(action.get("blockers") for action in actions):
        raise SupplyError("publication action retains safety blockers")

    prepared: list[tuple[str, dict[str, Any], str]] = []
    for action in create_actions:
        task = action.get("task")
        task_id = action.get("task_id")
        relative_path = action.get("task_path")
        if (
            not isinstance(task, dict)
            or not isinstance(task_id, str)
            or not task_id
            or "/" in task_id
            or "\\" in task_id
            or task.get("id") != task_id
            or relative_path != f"registry/tasks/{task_id}.json"
            or action.get("queue_lane") != "later"
        ):
            raise SupplyError("publication action task binding is invalid")
        try:
            default_schema_set().validate_task_write(
                task, f"task-supply-publication:{task_id}"
            )
        except (DocumentSchemaError, AcceptanceContractError) as exc:
            raise SupplyError(f"publication task contract is invalid: {exc}") from exc
        prepared.append((task_id, task, task_spec_digest(task)))

    store = StateStore(state_db, state_store_root)
    revisions: list[dict[str, Any]] = []
    try:
        with store.immediate() as connection:
            for task_id, task, expected_digest in prepared:
                written = task_specs.put(
                    connection,
                    task,
                    idempotency_key=f"task-supply:{expected_plan_sha256}:{task_id}",
                    expected_revision=None,
                    source="task-supply-reviewed-plan",
                )
                if written.get("spec_sha256") != expected_digest:
                    raise SupplyError(
                        f"StateStore TaskSpec digest diverged during Supply publication: {task_id}"
                    )
                revisions.append(written)
            projection = task_specs.current_projection(connection)
            task_spec_root_sha256 = task_specs.projection_root(projection)
    except task_specs.TaskSpecError as exc:
        raise SupplyError(f"StateStore Supply publication rejected: {exc}") from exc

    current = {task_id: store.task_spec(task_id) for task_id, _, _ in prepared}
    for task_id, _task, expected_digest in prepared:
        observed = current[task_id]
        if observed is None or observed.get("spec_sha256") != expected_digest:
            raise SupplyError(f"StateStore Supply readback drifted: {task_id}")
    replay = store.replay_projection()
    result = {
        "schema_version": SUPPLY_SCHEMA_VERSION,
        "kind": "bureau_task_supply_publication_result",
        "status": "published",
        "publication_mode": "state_store",
        "plan_sha256": expected_plan_sha256,
        "coordination_state_root": str(store.state_root.expanduser().resolve()),
        "registry_head_at_plan": expected_head,
        "compatibility_queue_sha256_at_plan": expected_queue_sha256,
        "queue_mutated": False,
        "created_task_ids": [task_id for task_id, _, _ in prepared],
        "created_tasks": [
            {
                "task_id": task_id,
                "task_path": f"registry/tasks/{task_id}.json",
                "queue_lane": "later",
                "spec_sha256": expected_digest,
                "state_store_revision": current[task_id]["revision"],
            }
            for task_id, _task, expected_digest in prepared
        ],
        "task_spec_revisions": revisions,
        "task_spec_root_sha256": task_spec_root_sha256,
        "authoritative_root_sha256": replay["authoritative_root_sha256"],
        "reused_task_ids": [
            str(action["task_id"]) for action in actions if action.get("action") == "reuse"
        ],
        "post_publication_state_store_valid": True,
        "does_not_establish": [
            "Git task projection",
            "compatibility queue mutation",
            "current claimability after publication",
            "merge authority",
            "runtime deployment",
        ],
    }
    result["result_sha256"] = sha256_json(
        {key: value for key, value in result.items() if key != "result_sha256"}
    )
    return result

def _frontier_documents(registry: Registry) -> dict[str, dict[str, Any]]:
    return {task_id: task.raw for task_id, task in registry.tasks.items()}


def _load_frontier_snapshot(path: Path) -> list[Mapping[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SupplyError(f"cannot read authoritative frontier snapshot: {path}") from exc
    candidates: Any = value
    if isinstance(value, dict):
        for key in ("frontier", "items", "tasks"):
            if isinstance(value.get(key), list):
                candidates = value[key]
                break
        else:
            for envelope_key in ("result", "payload", "data"):
                envelope = value.get(envelope_key)
                if envelope_key == "result" and isinstance(envelope, list):
                    candidates = envelope
                    break
                if not isinstance(envelope, dict):
                    continue
                for key in ("frontier", "items", "tasks"):
                    if isinstance(envelope.get(key), list):
                        candidates = envelope[key]
                        break
                if isinstance(candidates, list):
                    break
    if not isinstance(candidates, list) or any(
        not isinstance(item, Mapping) for item in candidates
    ):
        raise SupplyError("frontier snapshot has no object list")
    return candidates


def build_registry_supply_report(
    *,
    registry_root: Path,
    frontier: Sequence[Mapping[str, Any]],
    policy: SupplyPolicy | None = None,
    approval_available: bool = False,
    runtime_healthy: bool = False,
    mutation_authority: bool = False,
    environment_blockers: Sequence[str] = (),
    frontier_registry_head: str | None = None,
    frontier_queue_sha256: str | None = None,
    frontier_snapshot_sha256: str | None = None,
    frontier_task_spec_root_sha256: str | None = None,
    frontier_task_documents_sha256: str | None = None,
    task_documents: Mapping[str, Mapping[str, Any]] | None = None,
    acceptance_contracts: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    head_reader: Callable[[Path], str] = _git_head,
) -> dict[str, Any]:
    policy = policy or DEFAULT_SUPPLY_POLICY
    registry = Registry.load(registry_root)
    head = head_reader(registry_root)
    queue_digest = file_sha256(registry_root / "registry/queue.json")
    blockers = list(environment_blockers)
    if frontier_registry_head is None:
        blockers.append("frontier-registry-head-unbound")
    elif frontier_registry_head != head:
        blockers.append("frontier-registry-head-mismatch")
    if frontier_queue_sha256 is None:
        blockers.append("frontier-queue-sha256-unbound")
    elif frontier_queue_sha256 != queue_digest:
        blockers.append("frontier-queue-sha256-mismatch")
    if frontier_snapshot_sha256 is None:
        blockers.append("frontier-snapshot-sha256-unbound")
    if task_documents is not None:
        if frontier_task_spec_root_sha256 is None:
            blockers.append("frontier-task-spec-root-sha256-unbound")
        elif (
            len(frontier_task_spec_root_sha256) != 64
            or any(
                char not in "0123456789abcdef"
                for char in frontier_task_spec_root_sha256
            )
        ):
            raise SupplyError(
                "frontier_task_spec_root_sha256 must be a lowercase SHA-256 digest"
            )
        if frontier_task_documents_sha256 is None:
            blockers.append("frontier-task-documents-sha256-unbound")
        elif (
            len(frontier_task_documents_sha256) != 64
            or any(
                char not in "0123456789abcdef"
                for char in frontier_task_documents_sha256
            )
        ):
            raise SupplyError(
                "frontier_task_documents_sha256 must be a lowercase SHA-256 digest"
            )
    repository = registry.resources.get("repo.bureau")
    repository_path = (
        Path(repository.path)
        if repository is not None and repository.path
        else registry_root
    )
    registry_documents = _frontier_documents(registry)
    authoritative_documents = (
        {str(task_id): dict(document) for task_id, document in task_documents.items()}
        if task_documents is not None
        else None
    )
    if (
        authoritative_documents is not None
        and frontier_task_documents_sha256 is not None
        and sha256_json(authoritative_documents) != frontier_task_documents_sha256
    ):
        blockers.append("frontier-task-documents-sha256-mismatch")

    registry_fallback_task_ids: list[str] = []
    if authoritative_documents is None:
        documents = registry_documents
    else:
        documents = dict(authoritative_documents)
        frontier_task_ids = sorted(
            {
                str(item.get("task_id") or "")
                for item in frontier
                if str(item.get("task_id") or "")
            }
        )
        for task_id in frontier_task_ids:
            if task_id in documents:
                continue
            registry_document = registry_documents.get(task_id)
            if registry_document is None:
                continue
            documents[task_id] = dict(registry_document)
            registry_fallback_task_ids.append(task_id)

    worker_profile = derive_worker_capability_profile(frontier, documents)
    if authoritative_documents is not None:
        worker_profile["task_document_source"] = (
            "authoritative-dispatcher-task-specs-with-bounded-registry-fallback"
            if registry_fallback_task_ids
            else "authoritative-dispatcher-task-specs"
        )
        worker_profile["task_spec_root_sha256"] = frontier_task_spec_root_sha256
        worker_profile["task_documents_sha256"] = frontier_task_documents_sha256
        worker_profile["registry_fallback_task_ids"] = registry_fallback_task_ids
    return build_supply_report(
        frontier,
        task_documents=documents,
        policy=policy,
        repository=repository_path,
        registry_root=registry_root,
        registry_head=head,
        queue_sha256=queue_digest,
        approval_available=approval_available,
        runtime_healthy=runtime_healthy,
        mutation_authority=mutation_authority,
        environment_blockers=blockers,
        frontier_snapshot_sha256=frontier_snapshot_sha256,
        acceptance_contracts=acceptance_contracts,
        worker_profile=worker_profile,
        feasibility_required=True,
        resource_definitions=registry.resources,
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="bureau-task-supply")
    result.add_argument("--registry-root", default=".")
    result.add_argument("--frontier-report", required=True)
    result.add_argument("--frontier-head", required=True)
    result.add_argument("--frontier-queue-sha256", required=True)
    result.add_argument("--floor", type=int, default=8)
    result.add_argument("--refill-target", type=int, default=12)
    result.add_argument("--max-new-per-cycle", type=int, default=4)
    result.add_argument("--bucket-hours", type=int, default=24)
    result.add_argument("--approval-available", action="store_true")
    result.add_argument("--runtime-healthy", action="store_true")
    result.add_argument("--mutation-authority", action="store_true")
    result.add_argument("--environment-blocker", action="append", default=[])
    result.add_argument("--json", action="store_true")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    policy = SupplyPolicy(
        floor=args.floor,
        refill_target=args.refill_target,
        max_new_per_cycle=args.max_new_per_cycle,
        bucket_hours=args.bucket_hours,
    )
    frontier_path = Path(args.frontier_report).expanduser()
    report = build_registry_supply_report(
        registry_root=Path(args.registry_root).expanduser(),
        frontier=_load_frontier_snapshot(frontier_path),
        policy=policy,
        approval_available=args.approval_available,
        runtime_healthy=args.runtime_healthy,
        mutation_authority=args.mutation_authority,
        environment_blockers=tuple(args.environment_blocker),
        frontier_registry_head=args.frontier_head,
        frontier_queue_sha256=args.frontier_queue_sha256,
        frontier_snapshot_sha256=file_sha256(frontier_path),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.json else None, sort_keys=True))
    return 0 if report["status"] != "blocked" else 2


if __name__ == "__main__":
    raise SystemExit(main())
