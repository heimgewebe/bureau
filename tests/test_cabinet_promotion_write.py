from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from bureau.cabinet_graph import CabinetGraphError, promote_frontier_candidate
from bureau.cabinet_promotion_write import write_promotion_task


def export_fixture() -> dict:
    return {
        "schemaVersion": 1,
        "kind": "cabinet_frontier_export",
        "dispatchAllowed": False,
        "queueMutationAllowed": False,
        "taskCreationAllowed": False,
        "candidates": [
            {
                "schemaVersion": 1,
                "kind": "bureau_frontier_candidate",
                "id": "cabinet-graph:repo-bureau:review-import-drift",
                "source": "cabinet_ecosystem_graph",
                "targetNode": "repo:bureau",
                "repository": "bureau",
                "reason": "Cabinet graph reports drift between review HEAD and import HEAD.",
                "risk": "medium",
                "suggestedAction": "diagnose_repository_reference_drift",
                "dispatchAllowed": False,
                "evidence": [{"type": "cabinet", "ref": "test"}],
            }
        ],
    }


def promotion_fixture() -> dict:
    export = export_fixture()
    return promote_frontier_candidate(
        export,
        candidate_id=export["candidates"][0]["id"],
        task_id="BUR-CAB-ECO-001",
        initiative="BUR-CAB-ECO",
        target_proof="A reviewed proof exists.",
        approve=True,
    )


class CabinetPromotionWriteTests(unittest.TestCase):
    def test_writes_task_proposal_file_only(self) -> None:
        promotion = promotion_fixture()
        with tempfile.TemporaryDirectory() as directory:
            task_path = Path(directory) / "task.json"
            receipt = write_promotion_task(promotion, task_path)
            written = json.loads(task_path.read_text(encoding="utf-8"))

        self.assertEqual(receipt["kind"], "cabinet_promotion_task_write")
        self.assertEqual(receipt["mode"], "file_only")
        self.assertFalse(receipt["dispatchAllowed"])
        self.assertFalse(receipt["queueMutationAllowed"])
        self.assertFalse(receipt["taskCreationAllowed"])
        self.assertFalse(receipt["registryMutationAllowed"])
        self.assertEqual(written["id"], "BUR-CAB-ECO-001")
        self.assertEqual(written["metadata"]["source"], "cabinet_frontier_export")
        self.assertFalse(written["metadata"]["dispatch_allowed"])
        self.assertFalse(written["metadata"]["queue_mutation_allowed"])
        self.assertFalse(written["metadata"]["task_creation_allowed"])

    def test_refuses_existing_file(self) -> None:
        promotion = promotion_fixture()
        with tempfile.TemporaryDirectory() as directory:
            task_path = Path(directory) / "task.json"
            task_path.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(CabinetGraphError, "already exists"):
                write_promotion_task(promotion, task_path)

    def test_rejects_promotion_that_allows_dispatch(self) -> None:
        promotion = promotion_fixture()
        promotion["dispatchAllowed"] = True
        with tempfile.TemporaryDirectory() as directory:  # noqa: SIM117
            with self.assertRaisesRegex(CabinetGraphError, "dispatchAllowed"):
                write_promotion_task(promotion, Path(directory) / "task.json")

    def test_cli_writes_task_file_without_registry_load(self) -> None:
        from bureau.cli import main

        export = export_fixture()
        with tempfile.TemporaryDirectory() as directory:
            export_path = Path(directory) / "frontier.json"
            task_path = Path(directory) / "task.json"
            export_path.write_text(json.dumps(export), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = main(
                    [
                        "--json",
                        "--root",
                        str(Path(directory) / "not-a-registry"),
                        "cabinet-promote",
                        "--frontier-export",
                        str(export_path),
                        "--candidate-id",
                        export["candidates"][0]["id"],
                        "--task-id",
                        "BUR-CAB-ECO-001",
                        "--initiative",
                        "BUR-CAB-ECO",
                        "--target-proof",
                        "A reviewed proof exists.",
                        "--approve",
                        "--write-task",
                        str(task_path),
                    ]
                )
            payload = json.loads(output.getvalue())
            written = json.loads(task_path.read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertEqual(payload["kind"], "cabinet_frontier_promotion")
        self.assertEqual(payload["write"]["kind"], "cabinet_promotion_task_write")
        self.assertFalse(payload["dispatchAllowed"])
        self.assertFalse(payload["queueMutationAllowed"])
        self.assertFalse(payload["taskCreationAllowed"])
        self.assertEqual(written["id"], "BUR-CAB-ECO-001")


if __name__ == "__main__":
    unittest.main()


class CabinetPromotionTaskValidationTests(unittest.TestCase):
    def test_validates_written_task_file_without_registry_mutation(self) -> None:
        from bureau.cabinet_promotion_write import validate_promotion_task_file

        promotion = promotion_fixture()
        with tempfile.TemporaryDirectory() as directory:
            task_path = Path(directory) / "task.json"
            write_promotion_task(promotion, task_path)
            receipt = validate_promotion_task_file(task_path)

        self.assertEqual(receipt["kind"], "cabinet_promotion_task_validation")
        self.assertEqual(receipt["mode"], "file_only")
        self.assertTrue(receipt["valid"])
        self.assertEqual(receipt["taskId"], "BUR-CAB-ECO-001")
        self.assertFalse(receipt["dispatchAllowed"])
        self.assertFalse(receipt["queueMutationAllowed"])
        self.assertFalse(receipt["taskCreationAllowed"])
        self.assertFalse(receipt["registryMutationAllowed"])

    def test_validation_rejects_non_read_claim(self) -> None:
        from bureau.cabinet_promotion_write import validate_promotion_task_file

        promotion = promotion_fixture()
        with tempfile.TemporaryDirectory() as directory:
            task_path = Path(directory) / "task.json"
            write_promotion_task(promotion, task_path)
            task = json.loads(task_path.read_text(encoding="utf-8"))
            task["claims"][0]["mode"] = "write"
            task_path.write_text(json.dumps(task), encoding="utf-8")
            with self.assertRaisesRegex(CabinetGraphError, "read-only"):
                validate_promotion_task_file(task_path)

    def test_cli_validates_task_file_without_registry_load(self) -> None:
        from bureau.cli import main

        promotion = promotion_fixture()
        with tempfile.TemporaryDirectory() as directory:
            task_path = Path(directory) / "task.json"
            write_promotion_task(promotion, task_path)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = main(
                    [
                        "--json",
                        "--root",
                        str(Path(directory) / "not-a-registry"),
                        "cabinet-validate-task",
                        "--task-file",
                        str(task_path),
                    ]
                )
            payload = json.loads(output.getvalue())

        self.assertEqual(result, 0)
        self.assertEqual(payload["kind"], "cabinet_promotion_task_validation")
        self.assertEqual(payload["taskId"], "BUR-CAB-ECO-001")
        self.assertFalse(payload["registryMutationAllowed"])
