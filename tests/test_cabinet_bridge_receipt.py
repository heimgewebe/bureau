from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from bureau.cabinet_bridge import CabinetBridgeError
from bureau.cabinet_bridge_receipt import create_review_receipt, main


def gate_payload() -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "kind": "cabinet_bridge_preview_review_gate",
        "status": "requires_human_review",
        "reviewRequired": True,
        "importAllowed": False,
        "dispatchAllowed": False,
        "queueMutationAllowed": False,
        "taskCreationAllowed": False,
        "taskId": "BUR-CAB-BRIDGE-001",
        "sourceCandidateId": "claim:ready",
    }


def write_gate(path: Path, payload: dict[str, Any]) -> None:
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    path.write_text(rendered, encoding="utf-8")


class CabinetBridgeReceiptTests(unittest.TestCase):
    def test_receipt_records_review_without_effects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            gate = Path(directory) / "gate.json"
            write_gate(gate, gate_payload())
            receipt = create_review_receipt(
                gate,
                reviewer="alex",
                decision="ready-for-design",
                evidence=["manual review"],
            )
        self.assertEqual(receipt["kind"], "cabinet_bridge_review_receipt")
        self.assertEqual(receipt["status"], "review_recorded")
        self.assertEqual(receipt["decision"], "ready-for-design")
        self.assertFalse(receipt["importAllowed"])
        self.assertTrue(receipt["importReviewRequired"])
        self.assertFalse(receipt["dispatchAllowed"])
        self.assertFalse(receipt["queueMutationAllowed"])
        self.assertFalse(receipt["taskCreationAllowed"])
        self.assertEqual(receipt["sourceGate"]["taskId"], "BUR-CAB-BRIDGE-001")

    def test_receipt_requires_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            gate = Path(directory) / "gate.json"
            write_gate(gate, gate_payload())
            with self.assertRaisesRegex(CabinetBridgeError, "evidence"):
                create_review_receipt(
                    gate,
                    reviewer="alex",
                    decision="ready-for-design",
                    evidence=[],
                )

    def test_receipt_rejects_gate_with_import_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            gate = Path(directory) / "gate.json"
            payload = gate_payload()
            payload["importAllowed"] = True
            write_gate(gate, payload)
            with self.assertRaisesRegex(CabinetBridgeError, "importAllowed"):
                create_review_receipt(
                    gate,
                    reviewer="alex",
                    decision="ready-for-design",
                    evidence=["manual review"],
                )

    def test_cli_emits_json_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            gate = Path(directory) / "gate.json"
            write_gate(gate, gate_payload())
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                result = main(
                    [
                        "--json",
                        "--review-gate",
                        str(gate),
                        "--reviewer",
                        "alex",
                        "--decision",
                        "ready-for-design",
                        "--evidence",
                        "manual review",
                    ]
                )
        self.assertEqual(result, 0)
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["status"], "review_recorded")
        self.assertTrue(payload["importReviewRequired"])
        self.assertEqual(payload["sourceGate"]["sourceCandidateId"], "claim:ready")


if __name__ == "__main__":
    unittest.main()
