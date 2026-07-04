from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from bureau.cabinet_bridge import CabinetBridgeError
from bureau.cabinet_bridge_preview import main, preview_bridge_candidate


def write_report(path: Path) -> None:
    report = {
        "schemaVersion": 1,
        "kind": "cabinet_bureau_bridge_probe",
        "mode": "read_only",
        "dispatchAllowed": False,
        "queueMutationAllowed": False,
        "taskCreationAllowed": False,
        "candidateCount": 2,
        "admissibleCount": 1,
        "blockedCount": 1,
        "candidates": [
            {
                "id": "claim:ready",
                "status": "evidenced",
                "decision": "admissible",
                "reasons": [],
                "evidence": ["docs/example.md"],
                "expires_at": "2099-01-01",
                "next_action": "preview_only",
                "responsible_organ": "bureau",
            },
            {
                "id": "claim:blocked",
                "status": "plausible",
                "decision": "blocked",
                "reasons": ["blocked_status:plausible"],
                "evidence": [],
                "expires_at": "2099-01-01",
            },
        ],
    }
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class CabinetBridgePreviewTests(unittest.TestCase):
    def test_preview_requires_explicit_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            write_report(report)
            with self.assertRaisesRegex(CabinetBridgeError, "approve"):
                preview_bridge_candidate(
                    report,
                    candidate_id="claim:ready",
                    task_id="BUR-CAB-001",
                    initiative="BUR-CAB",
                    target_proof="Reviewed proof exists.",
                    approve=False,
                )

    def test_preview_returns_manual_proposal_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            write_report(report)
            result = preview_bridge_candidate(
                report,
                candidate_id="claim:ready",
                task_id="BUR-CAB-001",
                initiative="BUR-CAB",
                target_proof="Reviewed proof exists.",
                approve=True,
            )
            self.assertEqual(result["kind"], "cabinet_bridge_promotion_preview")
            self.assertEqual(result["mode"], "proposal_only")
            self.assertFalse(result["dispatchAllowed"])
            self.assertFalse(result["queueMutationAllowed"])
            self.assertFalse(result["taskCreationAllowed"])
            task = result["task"]
            self.assertEqual(task["id"], "BUR-CAB-001")
            self.assertEqual(task["execution"]["policy"], "review-before-effect")
            self.assertEqual(task["claims"][0]["resource"], "organ.bureau")
            self.assertFalse(task["metadata"]["dispatch_allowed"])

    def test_preview_rejects_blocked_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            write_report(report)
            with self.assertRaisesRegex(CabinetBridgeError, "not admissible"):
                preview_bridge_candidate(
                    report,
                    candidate_id="claim:blocked",
                    task_id="BUR-CAB-001",
                    initiative="BUR-CAB",
                    target_proof="Reviewed proof exists.",
                    approve=True,
                )

    def test_cli_emits_json_preview(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            write_report(report)
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                result = main(
                    [
                        "--json",
                        "--probe-report",
                        str(report),
                        "--candidate-id",
                        "claim:ready",
                        "--task-id",
                        "BUR-CAB-001",
                        "--initiative",
                        "BUR-CAB",
                        "--target-proof",
                        "Reviewed proof exists.",
                        "--approve",
                    ]
                )
            self.assertEqual(result, 0)
            payload = json.loads(stream.getvalue())
            self.assertEqual(payload["task"]["metadata"]["source_candidate_id"], "claim:ready")


if __name__ == "__main__":
    unittest.main()
