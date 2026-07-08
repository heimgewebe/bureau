from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from bureau.cabinet_bridge import CabinetBridgeError
from bureau.cabinet_frontier_reader import (
    create_frontier_receipt,
    main,
    preview_frontier_candidate,
    read_frontier,
    review_frontier_preview,
)


def candidate(
    *,
    risk: str = "low",
    effect: bool = False,
    candidate_id: str = "frontier:cabinet:ready",
) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "kind": "cabinet_frontier_candidate",
        "contractVersion": "1",
        "contractPath": "docs/contracts/cabinet-frontier-v1.md",
        "schemaPath": "docs/contracts/cabinet-frontier-v1.schema.json",
        "id": candidate_id,
        "createdAt": "2026-07-08T04:00:00Z",
        "source": {
            "repository": "heimgewebe/cabinet",
            "commit": "a" * 40,
            "maintenanceReportStatus": "pass",
            "maintenanceReportRef": "scripts/write_cabinet_maintenance_report.py",
            "signalRefs": ["signal:local_git:cabinet:maintenance-report:status:pass:aaaaaaaaaaaa"],
        },
        "target": {"repository": "heimgewebe/cabinet", "organ": "cabinet"},
        "proposal": {
            "title": "Review Cabinet frontier candidate",
            "summary": "Candidate is proposal-only.",
            "nextAction": "review_candidate",
            "responsibleOrgan": "cabinet",
            "risk": risk,
            "priorityHint": "later",
        },
        "acceptance": [{"id": "proposal-only", "assertion": "No direct effect."}],
        "evidence": [{"type": "cabinet_maintenance_report_candidate", "ref": "claim:ready"}],
        "forbiddenEffects": [
            "bureau_task_creation",
            "queue_mutation",
            "agent_dispatch",
            "merge_or_push",
            "runtime_mutation",
            "cleanup_action",
            "dump_generation",
            "authority_inference",
        ],
        "effectFlags": {
            "taskCreationAllowed": effect,
            "queueMutationAllowed": False,
            "dispatchAllowed": False,
            "mergeOrPushAllowed": False,
            "runtimeMutationAllowed": False,
            "cleanupAllowed": False,
            "dumpGenerationAllowed": False,
            "authorityInferenceAllowed": False,
        },
        "doesNotEstablish": [
            "task_approval",
            "merge_readiness",
            "runtime_correctness",
            "claim_truth",
            "autonomous_dispatch",
            "bureau_import_implemented",
            "bureau_task_created",
        ],
    }


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    rendered = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
    path.write_text(rendered, encoding="utf-8")


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class CabinetFrontierReaderTests(unittest.TestCase):
    def test_reader_accepts_valid_candidate_without_effects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frontier.jsonl"
            write_jsonl(path, [candidate()])
            report = read_frontier(path)
        self.assertEqual(report["kind"], "cabinet_frontier_reader_report")
        self.assertEqual(report["admissibleCount"], 1)
        self.assertEqual(report["blockedCount"], 0)
        self.assertFalse(report["importAllowed"])
        self.assertFalse(report["dispatchAllowed"])
        self.assertFalse(report["queueMutationAllowed"])
        self.assertFalse(report["taskCreationAllowed"])

    def test_reader_blocks_high_risk_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frontier.jsonl"
            write_jsonl(path, [candidate(risk="high")])
            report = read_frontier(path)
        self.assertEqual(report["admissibleCount"], 0)
        self.assertIn("high_risk_requires_human_release", report["candidates"][0]["reasons"])

    def test_reader_blocks_enabled_effect_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frontier.jsonl"
            write_jsonl(path, [candidate(effect=True)])
            report = read_frontier(path)
        self.assertIn("effect_flags_not_false", report["candidates"][0]["reasons"])

    def test_reader_detects_registry_collision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tasks = root / "registry/tasks"
            tasks.mkdir(parents=True)
            write_json(
                tasks / "task.json",
                {"metadata": {"source_frontier_candidate_id": "frontier:cabinet:ready"}},
            )
            path = root / "frontier.jsonl"
            write_jsonl(path, [candidate()])
            report = read_frontier(path, registry_root=root)
        self.assertIn("existing_task_source_collision", report["candidates"][0]["reasons"])

    def test_reader_fails_closed_on_invalid_registry_task_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tasks = root / "registry/tasks"
            tasks.mkdir(parents=True)
            (tasks / "broken.json").write_text("{", encoding="utf-8")
            path = root / "frontier.jsonl"
            write_jsonl(path, [candidate()])
            with self.assertRaisesRegex(CabinetBridgeError, "invalid JSON"):
                read_frontier(path, registry_root=root)

    def test_reader_blocks_duplicate_forbidden_effect_entries(self) -> None:
        value = candidate()
        value["forbiddenEffects"] = value["forbiddenEffects"] + ["bureau_task_creation"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frontier.jsonl"
            write_jsonl(path, [value])
            report = read_frontier(path)
        self.assertIn("forbidden_effects_not_exact", report["candidates"][0]["reasons"])

    def test_preview_review_receipt_keep_non_effects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frontier = root / "frontier.jsonl"
            report_path = root / "report.json"
            preview_path = root / "preview.json"
            gate_path = root / "gate.json"
            write_jsonl(frontier, [candidate()])
            write_json(report_path, read_frontier(frontier))
            preview = preview_frontier_candidate(
                report_path,
                candidate_id="frontier:cabinet:ready",
                approve=True,
            )
            write_json(preview_path, preview)
            gate = review_frontier_preview(preview_path)
            write_json(gate_path, gate)
            receipt = create_frontier_receipt(
                gate_path,
                reviewer="alex",
                decision="ready-for-design",
                evidence=["manual review"],
            )
        self.assertEqual(preview["kind"], "cabinet_frontier_preview")
        self.assertEqual(gate["kind"], "cabinet_frontier_review_gate")
        self.assertEqual(receipt["kind"], "cabinet_frontier_review_receipt")
        for payload in (preview, gate, receipt):
            self.assertFalse(payload["importAllowed"])
            self.assertFalse(payload["dispatchAllowed"])
            self.assertFalse(payload["queueMutationAllowed"])
            self.assertFalse(payload["taskCreationAllowed"])

    def test_preview_rejects_blocked_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frontier = root / "frontier.jsonl"
            report_path = root / "report.json"
            write_jsonl(frontier, [candidate(risk="high")])
            write_json(report_path, read_frontier(frontier))
            with self.assertRaisesRegex(CabinetBridgeError, "not admissible"):
                preview_frontier_candidate(
                    report_path,
                    candidate_id="frontier:cabinet:ready",
                    approve=True,
                )

    def test_cli_read_emits_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frontier.jsonl"
            write_jsonl(path, [candidate()])
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                rc = main(["read", "--frontier", str(path), "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(stream.getvalue())["kind"], "cabinet_frontier_reader_report")

    def test_cli_receipt_emits_json(self) -> None:
        gate = {
            "schemaVersion": 1,
            "kind": "cabinet_frontier_review_gate",
            "status": "requires_human_review",
            "reviewRequired": True,
            "importAllowed": False,
            "dispatchAllowed": False,
            "queueMutationAllowed": False,
            "taskCreationAllowed": False,
            "sourceCandidateId": "frontier:cabinet:ready",
            "targetRepository": "heimgewebe/cabinet",
            "targetOrgan": "cabinet",
            "risk": "low",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gate.json"
            write_json(path, gate)
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                rc = main([
                    "receipt",
                    "--review-gate",
                    str(path),
                    "--reviewer",
                    "alex",
                    "--decision",
                    "ready-for-design",
                    "--evidence",
                    "manual review",
                    "--json",
                ])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(stream.getvalue())["status"], "review_recorded")


if __name__ == "__main__":
    unittest.main()
