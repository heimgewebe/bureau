from __future__ import annotations

import json
from pathlib import Path

from bureau.schema_validation import SchemaSet

ROOT = Path(__file__).resolve().parents[1]
REVIEW_DOC = ROOT / "docs" / "reports" / "agent-run-evidence-review.v1.json"


def load_review() -> dict:
    return json.loads(REVIEW_DOC.read_text(encoding="utf-8"))


def test_review_document_embeds_valid_agent_run_evidence_ref() -> None:
    review = load_review()
    assert review["schema"] == "bureau.agent-run-evidence-review.v1"
    assert review["decision"] == "review_document_only_no_live_placement"
    assert len(review["evidence_refs"]) == 1

    schemas = SchemaSet(ROOT / "schemas")
    schemas.validate(
        "agent-run-evidence-ref",
        review["evidence_refs"][0],
        REVIEW_DOC,
    )


def test_review_document_declares_non_live_boundaries() -> None:
    review = load_review()
    limits = set(review["does_not_establish"])

    assert "does_not_create_task_acceptance" in limits
    assert "does_not_create_receipt_evidence" in limits
    assert "does_not_trigger_any_action" in limits
