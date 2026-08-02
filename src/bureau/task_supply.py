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

from .v2 import Registry

SUPPLY_SCHEMA_VERSION = 1
SUPPLY_KIND = "bureau_task_supply_report"
FALLBACK_METADATA_KEY = "supply_fallback"
TERMINAL_TASK_STATES = frozenset({"verified", "cancelled", "superseded"})
REVIEW_REASON = "execution is interactive-agent/review-before-effect"


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


FALLBACK_CATALOG: tuple[FallbackSpec, ...] = (
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
        category="queue-reconciliation",
        title="Reconcile one Bureau queue drift",
        goal=(
            "Remove or add exactly one queue entry whose current task truth proves the "
            "existing queue projection stale."
        ),
        capabilities=("repository", "python", "bureau", "grabowski"),
        claim_resource="component.bureau.registry",
        claim_mode="write",
        scope_path="registry/queue.json",
        acceptance=(
            "Task state, implementation status, and queue position are freshly read back.",
            "The patch changes only the proven stale queue projection.",
            "Queue and Registry validation pass without broad automatic admission.",
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
) -> dict[str, tuple[str, dict[str, Any]]]:
    result: dict[str, tuple[str, dict[str, Any]]] = {}
    for task_id, task in sorted(task_documents.items()):
        marker = _task_fallback_metadata(task)
        if marker is None or task.get("state") in TERMINAL_TASK_STATES:
            continue
        open_key = marker.get("open_key")
        if isinstance(open_key, str) and open_key:
            result.setdefault(open_key, (task_id, marker))
    return result


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
) -> dict[str, Any]:
    exact_scope = repository / spec.scope_path
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
        "execution": {
            "mode": "interactive-agent",
            "policy": "review-before-effect",
            "working_repository": str(repository),
            "approval": {
                "action_class": "repository_mutation",
                "required_level": "operator",
                "note": (
                    "Bounded supply fallback. Re-read live state, preserve foreign work, and "
                    "stop when the stated scope or evidence no longer matches."
                ),
            },
            "grabowski_resources": [f"path:{exact_scope}"],
        },
        "claims": [
            {
                "resource": spec.claim_resource,
                "mode": spec.claim_mode,
                "isolation": "worktree" if spec.claim_mode == "write" else "none",
            }
        ],
        "acceptance": [
            {"id": f"{spec.category}-{index}", "assertion": assertion}
            for index, assertion in enumerate(spec.acceptance, start=1)
        ],
        "metadata": {
            FALLBACK_METADATA_KEY: {
                "schema_version": SUPPLY_SCHEMA_VERSION,
                "category": spec.category,
                "fingerprint": fingerprint,
                "open_key": open_key,
                "time_bucket": bucket,
                "scope_path": spec.scope_path,
                "generated_by_task": "OPERATOR-INTEGRATION-LOOP-V1-T014",
                "does_not_establish": [
                    "claimability before canonical Registry publication",
                    "permission to bypass current leases or open-PR guards",
                    "merge or deployment authority",
                ],
            }
        },
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
    refill_triggered = total_claimable < policy.floor
    shortage = max(0, policy.refill_target - total_claimable) if refill_triggered else 0
    proposal_limit = min(shortage, policy.max_new_per_cycle, len(FALLBACK_CATALOG))
    bucket = time_bucket(now, policy.bucket_hours)
    existing = _existing_fallbacks(documents)
    global_blockers = sorted(
        {
            str(blocker)
            for blocker in environment_blockers
            if isinstance(blocker, str) and blocker
        }
    )
    if not runtime_healthy:
        global_blockers.append("required-runtime-unhealthy")
    category_blocks = catalog_blockers or {}
    frontier_by_task = {
        str(item["task_id"]): item
        for item in classification["items"]
        if isinstance(item.get("task_id"), str) and item["task_id"]
    }
    proposals: list[dict[str, Any]] = []
    created_count = 0
    for index, spec in enumerate(FALLBACK_CATALOG):
        if created_count >= proposal_limit:
            break
        open_key = _catalog_open_key(spec, str(repository_path), initiative_id)
        fingerprint = _catalog_fingerprint(open_key, bucket)
        blockers = set(global_blockers) | {
            str(blocker)
            for blocker in category_blocks.get(spec.category, ())
            if isinstance(blocker, str) and blocker
        }
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
        task = _fallback_task(
            task_id=task_id,
            initiative_id=initiative_id,
            spec=spec,
            repository=repository_path,
            fingerprint=fingerprint,
            open_key=open_key,
            bucket=bucket,
            rank=900 + index,
        )
        proposals.append(
            {
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
                "task": task,
            }
        )
        if not blockers:
            created_count += 1
    publishable_proposals = [
        proposal
        for proposal in proposals
        if proposal["action"] == "create" and not proposal["blockers"]
    ]
    report_blockers = list(global_blockers)
    if total_claimable < policy.floor and not mutation_authority:
        report_blockers.append("registry-mutation-authority-unavailable")
    if total_claimable < policy.floor and mutation_authority and not publishable_proposals:
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
    if total_claimable >= policy.floor:
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
    head_reader: Callable[[Path], str] = _git_head,
    registry_loader: Callable[[str | Path], Registry] = Registry.load,
) -> dict[str, Any]:
    if not mutation_authorized:
        raise SupplyError("registry mutation authority is required")
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
        raise SupplyError("publication plan has no Registry binding")
    registry_root = Path(str(registry_info.get("root"))).resolve()
    queue_path = registry_root / "registry/queue.json"
    expected_head = str(registry_info.get("head") or "")
    expected_queue_sha256 = str(registry_info.get("queue_sha256") or "")
    if head_reader(registry_root) != expected_head:
        raise SupplyError("Registry head changed after plan review")
    if not queue_path.is_file() or file_sha256(queue_path) != expected_queue_sha256:
        raise SupplyError("Registry queue changed after plan review")
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
    queue_before = queue_path.read_bytes()
    created_paths: list[Path] = []
    try:
        queue = json.loads(queue_before.decode("utf-8"))
        lanes = queue.get("lanes")
        if not isinstance(lanes, dict) or not isinstance(lanes.get("later"), list):
            raise SupplyError("Registry queue has no later lane")
        tasks_root = (registry_root / "registry/tasks").resolve()
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
            target = (registry_root / str(relative_path)).resolve()
            if target.parent != tasks_root:
                raise SupplyError("publication target escapes the Registry task directory")
            if target.exists():
                raise SupplyError(f"publication target already exists: {relative_path}")
            created_paths.append(target)
            _atomic_write_json(target, task)
            if task_id not in lanes["later"]:
                lanes["later"].append(task_id)
        _atomic_write_json(queue_path, queue)
        registry_loader(registry_root)
    except Exception:
        with queue_path.open("wb") as handle:
            handle.write(queue_before)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(queue_path.parent)
        for path in reversed(created_paths):
            path.unlink(missing_ok=True)
            _fsync_directory(path.parent)
        raise
    result = {
        "schema_version": SUPPLY_SCHEMA_VERSION,
        "kind": "bureau_task_supply_publication_result",
        "status": "published",
        "plan_sha256": expected_plan_sha256,
        "registry_head_before": expected_head,
        "queue_sha256_before": expected_queue_sha256,
        "queue_sha256_after": file_sha256(queue_path),
        "created_task_ids": [str(action["task_id"]) for action in create_actions],
        "reused_task_ids": [
            str(action["task_id"]) for action in actions if action.get("action") == "reuse"
        ],
        "post_publication_registry_valid": True,
        "does_not_establish": [
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
    repository = registry.resources.get("repo.bureau")
    repository_path = (
        Path(repository.path)
        if repository is not None and repository.path
        else registry_root
    )
    return build_supply_report(
        frontier,
        task_documents=_frontier_documents(registry),
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
