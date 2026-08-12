from __future__ import annotations

from typing import Any

from .core import Registry, StateError, StateStore
from .live_register import (
    candidate_content_fingerprint,
    candidate_source_fingerprint,
)
from .operator_intake import (
    OPERATOR_INTAKE_SCHEMA_VERSION,
    candidate_record_request,
)

SOURCE_CANDIDATE_KINDS = {
    "conversation",
    "github-issue",
    "source-observer",
    "doctor",
    "local-fallback",
}


def _required_text(value: Any, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise StateError(f"{field} must be a string")
    normalized = " ".join(value.split()).strip()
    if not normalized:
        raise StateError(f"{field} must not be empty")
    if len(normalized) > maximum:
        raise StateError(f"{field} must be at most {maximum} characters")
    return normalized


def candidate_request(
    *,
    source_kind: str,
    source_locator: str,
    title: str,
    desired_outcome: str,
    repo: str | None = None,
    source_sha256: str | None = None,
    observed_at: str | None = None,
    task_id: str | None = None,
    note: str | None = None,
    catalog_validation: str = "deferred",
) -> dict[str, Any]:
    """Normalize one source finding into the sole operator Candidate Event path.

    This adapter performs no connector call and owns no persistence. GitHub issues
    are observations only; the StateStore live-register journal remains the durable
    local record and the Registry/TaskSpec publication path remains authoritative.
    """
    checked_kind = _required_text(source_kind, field="source_kind", maximum=80).casefold()
    if checked_kind not in SOURCE_CANDIDATE_KINDS:
        allowed = ", ".join(sorted(SOURCE_CANDIDATE_KINDS))
        raise StateError(f"source_kind must be one of: {allowed}")
    checked_locator = _required_text(
        source_locator,
        field="source_locator",
        maximum=2000,
    )
    checked_title = _required_text(title, field="title", maximum=240)
    checked_outcome = _required_text(
        desired_outcome,
        field="desired_outcome",
        maximum=4000,
    )
    source_fingerprint = candidate_source_fingerprint(
        source_kind=checked_kind,
        source_locator=checked_locator,
        source_sha256=source_sha256,
    )
    content_fingerprint = candidate_content_fingerprint(
        title=checked_title,
        desired_outcome=checked_outcome,
        repo=repo,
        task_id=task_id,
    )
    return {
        "schema_version": OPERATOR_INTAKE_SCHEMA_VERSION,
        "idempotency_key": (f"candidate:{source_fingerprint}:{content_fingerprint}"),
        "title": checked_title,
        "source_kind": checked_kind,
        "desired_outcome": checked_outcome,
        "repo": repo,
        "source_locator": checked_locator,
        "source_sha256": source_sha256,
        "observed_at": observed_at,
        "task_id": task_id,
        "note": note,
        "catalog_validation": catalog_validation,
    }


def record_candidate(
    registry: Registry | None,
    store: StateStore,
    **finding: Any,
) -> dict[str, Any]:
    """Durably record or replay one normalized source candidate locally."""
    return candidate_record_request(
        registry,
        store,
        candidate_request(**finding),
    )


def discovery_candidate_request(
    finding: dict[str, Any],
    *,
    repo: str | None = None,
    task_id: str | None = None,
    source_sha256: str | None = None,
    catalog_validation: str = "deferred",
) -> dict[str, Any]:
    """Adapt the existing discovery.py finding shape without becoming a store."""
    if not isinstance(finding, dict):
        raise StateError("discovery finding must be an object")
    source_id = _required_text(finding.get("source_id"), field="source_id", maximum=500)
    source_path = _required_text(
        finding.get("source_path"),
        field="source_path",
        maximum=1500,
    )
    anchor = _required_text(
        finding.get("external_id") or finding.get("source_anchor"),
        field="source_anchor",
        maximum=500,
    )
    digest = source_sha256 or finding.get("fingerprint")
    if digest is not None and not isinstance(digest, str):
        raise StateError("source_sha256 must be a string")
    summary = _required_text(finding.get("summary"), field="summary", maximum=1000)
    return candidate_request(
        source_kind="source-observer",
        source_locator=f"{source_id}:{source_path}#{anchor}",
        source_sha256=digest,
        title=summary[:240],
        desired_outcome=_required_text(
            finding.get("target_outcome") or summary,
            field="target_outcome",
            maximum=4000,
        ),
        repo=repo,
        task_id=task_id,
        note=(
            f"Observed by deterministic source discovery at revision "
            f"{finding.get('source_revision') or 'unknown'}"
        ),
        catalog_validation=catalog_validation,
    )


def record_discovery_candidate(
    registry: Registry | None,
    store: StateStore,
    finding: dict[str, Any],
    **binding: Any,
) -> dict[str, Any]:
    return candidate_record_request(
        registry,
        store,
        discovery_candidate_request(finding, **binding),
    )
