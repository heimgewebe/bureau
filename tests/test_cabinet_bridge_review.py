from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from bureau.cabinet_bridge import CabinetBridgeError
from bureau.cabinet_bridge_review import main, review_preview


def preview_payload() -> dict[str, Any]:
    candidate = {
        "id": "claim:ready",
        "status": "evidenced",
        "decision": "admissible",
        "reasons": [],
        "responsible_organ": "bureau",
    }
    return {
        "schemaVersion": 1,
        "kind": "cabinet_bridge_promotion_preview",
        "mode": "proposal_only",
        "approved": True,
        "importAllowed": False,
        "dispatchAllowed": False,
        "queueMutationAllowed": False,
        "taskCreationAllowed": False,
        "task": {
            "schema_version": 1,
            "id": "BUR-CAB-BRIDGE-001",
            "initiative": "BUR-CAB",
            "title": "Review Cabinet bridge candidate claim:ready",
            "state": "planned",
            "goal": "Review Cabinet bridge candidate claim:ready before any import.",
            "required_capabilities": ["review"],
            "priority": {"lane": "next", "rank": 65},
            "execution": {"mode": "manual", "policy": "review-before-effect"},
            "claims": [{"resource": "organ.bureau", "mode": "read", "isolation": "none"}],
            "acceptance": [
                {"id": "target-proof", "assertion": "Reviewed proof exists."},
                {"id": "no-auto-effect", "assertion": "No operational effect."},
            ],
            "metadata": {
                "source": "cabinet_bridge_probe",
                "source_candidate_id": "claim:ready",
                "source_candidate": candidate,
                "import_allowed": False,
                "dispatch_allowed": False,
                "queue_mutation_allowed": False,
                "task_creation_allowed": False,
            },
        },
    }


def write_preview(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def expect_rejected(payload: dict[str, Any], pattern: str) -> None:
    with tempfile.TemporaryDirectory() as directory:
        preview = Path(directory) / "preview.json"
        write_preview(preview, payload)
        with unittest.TestCase().assertRaisesRegex(CabinetBridgeError, pattern):
            review_preview(preview)


class CabinetBridgeReviewTests(unittest.TestCase):
    def test_review_gate_requires_human_review_without_effects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            preview = Path(directory) / "preview.json"
            write_preview(preview, preview_payload())
            result = review_preview(preview)
        self.assertEqual(result["kind"], "cabinet_bridge_preview_review_gate")
        self.assertEqual(result["status"], "requires_human_review")
        self.assertTrue(result["reviewRequired"])
        self.assertFalse(result["importAllowed"])
        self.assertFalse(result["dispatchAllowed"])
        self.assertFalse(result["queueMutationAllowed"])
        self.assertFalse(result["taskCreationAllowed"])
        self.assertEqual(result["taskId"], "BUR-CAB-BRIDGE-001")

    def test_review_gate_rejects_unapproved_preview(self) -> None:
        payload = preview_payload()
        payload["approved"] = False
        expect_rejected(payload, "approved")

    def test_review_gate_rejects_import_enabled_preview(self) -> None:
        payload = preview_payload()
        payload["importAllowed"] = True
        expect_rejected(payload, "importAllowed")

    def test_review_gate_rejects_dispatch_enabled_preview(self) -> None:
        payload = preview_payload()
        payload["dispatchAllowed"] = True
        expect_rejected(payload, "dispatchAllowed")

    def test_review_gate_rejects_import_enabled_metadata(self) -> None:
        payload = preview_payload()
        payload["task"]["metadata"]["import_allowed"] = True
        expect_rejected(payload, "import_allowed")

    def test_review_gate_rejects_extra_capability(self) -> None:
        payload = preview_payload()
        payload["task"]["required_capabilities"].append("write")
        expect_rejected(payload, "review-only")

    def test_review_gate_rejects_empty_claims(self) -> None:
        payload = preview_payload()
        payload["task"]["claims"] = []
        expect_rejected(payload, "at least one")

    def test_review_gate_rejects_write_claim(self) -> None:
        payload = preview_payload()
        payload["task"]["claims"][0]["mode"] = "write"
        expect_rejected(payload, "read-only")

    def test_cli_emits_review_gate_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            preview = Path(directory) / "preview.json"
            write_preview(preview, preview_payload())
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                result = main(["--json", "--preview", str(preview)])
        self.assertEqual(result, 0)
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["status"], "requires_human_review")
        self.assertEqual(payload["sourceCandidateId"], "claim:ready")


if __name__ == "__main__":
    unittest.main()
