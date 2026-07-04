from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from bureau.cabinet_bridge import CabinetBridgeError
from bureau.cabinet_bridge_review import main, review_preview


def preview_payload() -> dict[str, object]:
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
                {"id": "no-auto-effect", "assertion": "Preview creates no operational effect."},
            ],
            "metadata": {
                "source": "cabinet_bridge_probe",
                "source_candidate_id": "claim:ready",
                "source_candidate": candidate,
                "dispatch_allowed": False,
                "queue_mutation_allowed": False,
                "task_creation_allowed": False,
            },
        },
    }


def write_preview(path: Path, payload: dict[str, object] | None = None) -> None:
    value = payload or preview_payload()
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class CabinetBridgeReviewTests(unittest.TestCase):
    def test_review_gate_requires_human_review_without_effects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            preview = Path(directory) / "preview.json"
            write_preview(preview)
            result = review_preview(preview)
            self.assertEqual(result["kind"], "cabinet_bridge_preview_review_gate")
            self.assertEqual(result["status"], "requires_human_review")
            self.assertTrue(result["reviewRequired"])
            self.assertFalse(result["importAllowed"])
            self.assertFalse(result["dispatchAllowed"])
            self.assertFalse(result["queueMutationAllowed"])
            self.assertFalse(result["taskCreationAllowed"])
            self.assertEqual(result["taskId"], "BUR-CAB-BRIDGE-001")

    def test_review_gate_rejects_dispatch_enabled_preview(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            preview = Path(directory) / "preview.json"
            payload = preview_payload()
            payload["dispatchAllowed"] = True
            write_preview(preview, payload)
            with self.assertRaisesRegex(CabinetBridgeError, "dispatchAllowed"):
                review_preview(preview)

    def test_review_gate_rejects_write_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            preview = Path(directory) / "preview.json"
            payload = preview_payload()
            task = payload["task"]
            assert isinstance(task, dict)
            claims = task["claims"]
            assert isinstance(claims, list)
            claim = claims[0]
            assert isinstance(claim, dict)
            claim["mode"] = "write"
            write_preview(preview, payload)
            with self.assertRaisesRegex(CabinetBridgeError, "read-only"):
                review_preview(preview)

    def test_cli_emits_review_gate_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            preview = Path(directory) / "preview.json"
            write_preview(preview)
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                result = main(["--json", "--preview", str(preview)])
            self.assertEqual(result, 0)
            payload = json.loads(stream.getvalue())
            self.assertEqual(payload["status"], "requires_human_review")
            self.assertEqual(payload["sourceCandidateId"], "claim:ready")


if __name__ == "__main__":
    unittest.main()
