from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from bureau.schema_validation import DocumentSchemaError, SchemaSet
from bureau.verifiable_memory import evaluate_memory_recall

ROOT = Path(__file__).resolve().parents[1]
HEX64 = "a" * 64
OTHER_HEX64 = "b" * 64


def schemas() -> SchemaSet:
    return SchemaSet(ROOT / "schemas")


def memory_record(**evidence_updates):
    evidence = {
        "kind": "repobrief_citation",
        "citation_id": "cit_0123456789abcdef",
        "expected_sha256": HEX64,
        "generated_at": "2026-07-09T10:00:00Z",
        "max_age_hours": 24,
        "freshness_basis": "repobrief_snapshot_generated_at",
        "does_not_establish": ["claim_truth", "repo_truth"],
    }
    evidence.update(evidence_updates)
    return {
        "schema_version": 1,
        "memory_id": "memory:repobrief:rpu-v1-t015",
        "claim_text": "RepoBrief evidence-bound memory must revalidate citations on recall.",
        "topic": "repobrief-memory",
        "created_at": "2026-07-09T11:00:00Z",
        "repo": "bureau",
        "snapshot_stem": "bureau-max-260709-1000",
        "freshness_status": "fresh_exact",
        "evidence": [evidence],
        "does_not_establish": ["memory_claim_truth", "source_truth"],
    }


def test_agent_memory_claim_schema_accepts_citation_bound_memory_shape() -> None:
    record = memory_record()

    schemas().validate("agent-memory-claim", record, "memory")


def test_agent_memory_claim_schema_requires_evidence_hash() -> None:
    record = memory_record()
    del record["evidence"][0]["expected_sha256"]

    with pytest.raises(DocumentSchemaError, match="expected_sha256"):
        schemas().validate("agent-memory-claim", record, "memory")


def test_recall_check_allows_context_only_when_hash_and_freshness_hold() -> None:
    record = memory_record()

    result = evaluate_memory_recall(
        record,
        {
            "cit_0123456789abcdef": {
                "observed_sha256": HEX64,
                "generated_at": "2026-07-09T10:00:00Z",
            }
        },
        checked_at=datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc),
    )

    assert result["overall_status"] == "still_established"
    assert result["usable_for_context"] is True
    assert result["presentable_as_source_truth"] is False
    assert "memory_claim_truth" in result["does_not_establish"]


def test_recall_check_detects_changed_evidence_before_use() -> None:
    result = evaluate_memory_recall(
        memory_record(),
        {"cit_0123456789abcdef": {"observed_sha256": OTHER_HEX64}},
        checked_at=datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc),
    )

    assert result["overall_status"] == "changed"
    assert result["usable_for_context"] is False
    assert result["evidence"][0]["reason"] == "hash_mismatch"


def test_recall_check_detects_stale_evidence_before_use() -> None:
    result = evaluate_memory_recall(
        memory_record(max_age_hours=1),
        {
            "cit_0123456789abcdef": {
                "observed_sha256": HEX64,
                "generated_at": "2026-07-09T10:00:00Z",
            }
        },
        checked_at=datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc),
    )

    assert result["overall_status"] == "stale"
    assert result["usable_for_context"] is False


def test_recall_check_detects_missing_evidence_before_use() -> None:
    result = evaluate_memory_recall(
        memory_record(),
        {},
        checked_at=datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc),
    )

    assert result["overall_status"] == "missing"
    assert result["usable_for_context"] is False


def test_recall_check_detects_unverifiable_evidence_before_use() -> None:
    result = evaluate_memory_recall(
        memory_record(expected_sha256="not-a-hash"),
        {"cit_0123456789abcdef": {"observed_sha256": HEX64}},
        checked_at=datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc),
    )

    assert result["overall_status"] == "unverifiable"
    assert result["usable_for_context"] is False


def test_recall_check_rejects_non_hex_sha256_before_use() -> None:
    result = evaluate_memory_recall(
        memory_record(expected_sha256="g" * 64),
        {"cit_0123456789abcdef": {"observed_sha256": "g" * 64}},
        checked_at=datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc),
    )

    assert result["overall_status"] == "unverifiable"
    assert result["usable_for_context"] is False


def test_recall_check_rejects_invalid_max_age_before_use() -> None:
    result = evaluate_memory_recall(
        memory_record(max_age_hours="soon"),
        {"cit_0123456789abcdef": {"observed_sha256": HEX64}},
        checked_at=datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc),
    )

    assert result["overall_status"] == "unverifiable"
    assert result["usable_for_context"] is False


def test_revalidation_result_is_schema_compatible() -> None:
    record = memory_record()
    result = evaluate_memory_recall(
        record,
        {
            "cit_0123456789abcdef": {
                "observed_sha256": HEX64,
                "generated_at": "2026-07-09T10:00:00Z",
            }
        },
        checked_at=datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc),
    )
    stored = {**record, "last_revalidation": result}

    schemas().validate("agent-memory-claim", stored, "memory")
