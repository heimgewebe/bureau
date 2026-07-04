from __future__ import annotations

from pathlib import Path


def test_bridge_import_review_doc_exists() -> None:
    text = Path("docs/cabinet-bridge-import-review-contract-v0.md").read_text(encoding="utf-8")
    assert "importReviewRequired == true" in text
    assert "importAllowed == false" in text
    assert "cabinet-ci-review-gate" in text
