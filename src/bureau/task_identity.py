from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

_BARE_LOCAL_ORDINAL_RE = re.compile(r"^T[0-9]+$", re.IGNORECASE | re.ASCII)
_NAMESPACED_LOCAL_ORDINAL_RE = re.compile(
    r"^(?P<namespace>.+?)(?P<separator>[-_.:])(?P<ordinal>T[0-9]+)$",
    re.IGNORECASE | re.ASCII,
)


def local_task_ordinal(task_id: str) -> str | None:
    """Return the namespace-local trailing T-number of a task reference."""
    if not isinstance(task_id, str):
        return None
    normalized = task_id.strip()
    if _BARE_LOCAL_ORDINAL_RE.fullmatch(normalized):
        return normalized.upper()
    match = _NAMESPACED_LOCAL_ORDINAL_RE.fullmatch(normalized)
    return match.group("ordinal").upper() if match is not None else None


def task_namespace(task_id: str) -> str | None:
    """Return the explicit namespace of a canonical id with a trailing T-number."""
    if not isinstance(task_id, str):
        return None
    match = _NAMESPACED_LOCAL_ORDINAL_RE.fullmatch(task_id.strip())
    return match.group("namespace") if match is not None else None


def is_bare_local_task_ordinal(reference: str) -> bool:
    """Whether *reference* is only a local T-number and therefore not global identity."""
    return (
        isinstance(reference, str)
        and _BARE_LOCAL_ORDINAL_RE.fullmatch(reference.strip()) is not None
    )


def canonical_task_reference_contract(
    task_id: str,
    known_task_ids: Iterable[str],
) -> dict[str, Any]:
    """Describe the non-ambiguous reference contract for one canonical task id.

    Local ordinals are intentionally reusable inside namespaces. They are never
    global identities, even when only one current task happens to use them.
    """
    known = sorted({value for value in known_task_ids if isinstance(value, str) and value})
    local_ordinal = local_task_ordinal(task_id)
    same_local_ordinal_task_ids = (
        sorted(
            value
            for value in known
            if value != task_id and local_task_ordinal(value) == local_ordinal
        )
        if local_ordinal is not None
        else []
    )
    return {
        "schema_version": 1,
        "canonical_task_id": task_id,
        "canonical_reference_required": True,
        "bare_local_reference_allowed": False,
        "namespace": task_namespace(task_id),
        "local_ordinal": local_ordinal,
        "local_ordinal_scope": "namespace_local" if local_ordinal is not None else None,
        "same_local_ordinal_task_ids": same_local_ordinal_task_ids,
    }


def assess_task_reference(
    reference: str,
    known_task_ids: Iterable[str],
    *,
    namespace: str | None = None,
) -> dict[str, Any]:
    """Resolve canonical ids, or an explicit namespace plus local ordinal.

    A bare local ordinal is never accepted as global identity. Supplying an
    explicit namespace is safe only when it selects exactly one canonical id.
    """
    normalized = reference.strip() if isinstance(reference, str) else ""
    normalized_namespace = namespace.strip() if isinstance(namespace, str) else None
    known = sorted({value for value in known_task_ids if isinstance(value, str) and value})

    if is_bare_local_task_ordinal(normalized):
        ordinal = normalized.upper()
        candidates = sorted(
            value for value in known if local_task_ordinal(value) == ordinal
        )
        if normalized_namespace:
            scoped_candidates = sorted(
                value
                for value in candidates
                if task_namespace(value) == normalized_namespace
            )
            if len(scoped_candidates) == 1:
                return {
                    "status": "resolved",
                    "reason": "explicit_namespace_local_ordinal",
                    "reference": normalized,
                    "namespace": normalized_namespace,
                    "local_ordinal": ordinal,
                    "canonical_task_id": scoped_candidates[0],
                    "candidate_task_ids": scoped_candidates,
                    "canonical_reference_required": True,
                }
            return {
                "status": "rejected",
                "reason": (
                    "ambiguous_namespace_local_ordinal"
                    if len(scoped_candidates) > 1
                    else "namespace_local_ordinal_not_found"
                ),
                "reference": normalized,
                "namespace": normalized_namespace,
                "local_ordinal": ordinal,
                "candidate_task_ids": scoped_candidates or candidates,
                "canonical_reference_required": True,
            }
        return {
            "status": "rejected",
            "reason": "bare_local_ordinal_not_global_identity",
            "reference": normalized,
            "local_ordinal": ordinal,
            "candidate_task_ids": candidates,
            "canonical_reference_required": True,
        }

    if normalized in known:
        return {
            "status": "resolved",
            "reason": "exact_canonical_task_id",
            "reference": normalized,
            "canonical_task_id": normalized,
            "candidate_task_ids": [normalized],
            "canonical_reference_required": True,
        }

    return {
        "status": "rejected",
        "reason": "unknown_canonical_task_id",
        "reference": normalized,
        "candidate_task_ids": [],
        "canonical_reference_required": True,
    }
