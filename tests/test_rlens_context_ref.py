from __future__ import annotations

from pathlib import Path

import pytest

from bureau.schema_validation import DocumentSchemaError, SchemaSet

ROOT = Path(__file__).resolve().parents[1]
HEX64 = "a" * 64
HEX40 = "b" * 40


def rlens_ref() -> dict:
    return {
        "schema_version": 1,
        "repo": "lenskit",
        "stem": "lenskit-max-260701-1454",
        "manifest_sha256": HEX64,
        "bundle_commit": HEX40,
        "live_commit_at_claim": HEX40,
        "freshness_status": "fresh_exact",
        "task_profile": "pr_review",
        "preflight_status": "pass",
        "source": "grabowski.rlens_freshness_check",
        "does_not_establish": ["repo_understood", "claims_true"],
    }


def schemas() -> SchemaSet:
    return SchemaSet(ROOT / "schemas")


def test_execution_envelope_accepts_optional_rlens_context_ref() -> None:
    envelope = {
        "schema_version": 1,
        "run_id": "BUR-RUN-20260701T120000Z-abcdef1234",
        "task_id": "BUR-2026-002-T001",
        "worker_id": "worker-1",
        "task_sha256": HEX64,
        "plan_sha256": HEX64,
        "created_at": "2026-07-01T12:00:00Z",
        "task": {},
        "claims": [],
        "rlens_context_ref": rlens_ref(),
    }
    schemas().validate("execution-envelope", envelope, "envelope")


def test_receipt_accepts_optional_rlens_context_ref() -> None:
    receipt = {
        "schema_version": 1,
        "run_id": "BUR-RUN-20260701T120000Z-abcdef1234",
        "task_id": "BUR-2026-002-T001",
        "task_sha256": HEX64,
        "plan_sha256": HEX64,
        "envelope_sha256": HEX64,
        "verified_at": "2026-07-01T12:00:00Z",
        "external": None,
        "evidence": {"done": True},
        "receipt_sha256": HEX64,
        "rlens_context_ref": rlens_ref(),
    }
    schemas().validate("receipt", receipt, "receipt")


def test_task_accepts_optional_rlens_context_ref() -> None:
    task = {
        "schema_version": 1,
        "id": "BUR-2026-002-T001",
        "initiative": "BUR-2026-002",
        "title": "Use rLens context refs",
        "state": "planned",
        "execution": {"mode": "interactive-agent", "policy": "review-before-effect"},
        "claims": [],
        "acceptance": [{"id": "schema", "assertion": "schema accepts the ref"}],
        "rlens_context_ref": rlens_ref(),
    }
    schemas().validate("task", task, "task")


def test_rlens_context_ref_rejects_unknown_fields() -> None:
    envelope = {
        "schema_version": 1,
        "run_id": "BUR-RUN-20260701T120000Z-abcdef1234",
        "task_id": "BUR-2026-002-T001",
        "worker_id": "worker-1",
        "task_sha256": HEX64,
        "plan_sha256": HEX64,
        "created_at": "2026-07-01T12:00:00Z",
        "task": {},
        "claims": [],
        "rlens_context_ref": {**rlens_ref(), "unsupported_extra": True},
    }
    with pytest.raises(DocumentSchemaError, match="Additional properties"):
        schemas().validate("execution-envelope", envelope, "envelope")
