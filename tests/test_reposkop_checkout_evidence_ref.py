from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jsonschema import Draft202012Validator

from bureau.schema_validation import DocumentSchemaError, SchemaSet
from bureau.v2 import complete_run, plan_sha256

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads(
    (ROOT / "schemas" / "reposkop-checkout-evidence-ref.v1.schema.json").read_text(
        encoding="utf-8"
    )
)
VALIDATOR = Draft202012Validator(SCHEMA)
RECEIPT_SCHEMA = json.loads(
    (ROOT / "schemas" / "receipt.v1.schema.json").read_text(encoding="utf-8")
)
RECEIPT_VALIDATOR = Draft202012Validator(RECEIPT_SCHEMA)


class _QueryResult:
    def __init__(self, row: object) -> None:
        self.row = row

    def fetchone(self) -> object:
        return self.row


class _FakeConnection:
    def __init__(self, store: _FakeStore) -> None:
        self.store = store

    def execute(self, statement: str, _parameters: object = None) -> _QueryResult:
        if "SELECT * FROM runs WHERE run_id=?" in statement:
            return _QueryResult(
                {
                    **self.store.run_record,
                    "envelope_json": json.dumps(self.store.envelope),
                }
            )
        if "SELECT receipt_json FROM receipts" in statement:
            return _QueryResult(None)
        if "INSERT INTO receipts" in statement:
            self.store.receipt_written = True
        if "INSERT INTO task_status" in statement:
            self.store.verified_written = True
        if "UPDATE runs SET state='succeeded'" in statement:
            self.store.run_record["state"] = "succeeded"
        if "SELECT state FROM runs" in statement:
            return _QueryResult({"state": self.store.run_record["state"]})
        return _QueryResult(None)


class _FakeStore:
    def __init__(
        self,
        root: Path,
        run_record: dict[str, object],
        envelope: dict[str, object],
    ) -> None:
        self.root = root
        self.run_record = run_record
        self.envelope = envelope
        self.immediate_calls = 0
        self.receipt_written = False
        self.verified_written = False

    def receipt(self, _run_id: str) -> None:
        return None

    def run(self, _run_id: str) -> dict[str, object]:
        return dict(self.run_record)

    @staticmethod
    def public_run(row: dict[str, object]) -> dict[str, object]:
        return {key: value for key, value in row.items() if key != "envelope_json"}

    @contextmanager
    def connect(self):
        yield _FakeConnection(self)

    @contextmanager
    def immediate(self):
        self.immediate_calls += 1
        yield _FakeConnection(self)

    def event(self, *_arguments: object) -> None:
        return None

    def receipt_path(self, run_id: str) -> Path:
        return self.root / f"{run_id}.json"


class ReposkopCheckoutEvidenceRefTests(unittest.TestCase):
    def base(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "producer": "reposkop",
            "repository": "heimgewebe/example",
            "purpose": "bureau-task-execution",
            "pre_observation_sha256": "a" * 64,
            "post_observation_sha256": "b" * 64,
            "repository_identity_sha256": "c" * 64,
            "checkout_identity_sha256": "d" * 64,
            "transition_sha256": "e" * 64,
            "continuity_sha256": "f" * 64,
            "continuity_state": "explainable_drift",
            "does_not_establish": [
                "task_completion",
                "effect_authorization",
            ],
        }

    def assert_valid(self, value: dict[str, object]) -> None:
        self.assertEqual([], list(VALIDATOR.iter_errors(value)))

    def test_completed_transition_reference_is_valid(self) -> None:
        self.assert_valid(self.base())

    def test_pre_effect_reference_requires_null_post_fields(self) -> None:
        value = self.base()
        value.update(
            {
                "continuity_state": "pre_effect_only",
                "post_observation_sha256": None,
                "transition_sha256": None,
                "continuity_sha256": None,
            }
        )
        self.assert_valid(value)

    def test_completed_state_requires_all_post_digests(self) -> None:
        value = self.base()
        value["transition_sha256"] = None
        self.assertTrue(list(VALIDATOR.iter_errors(value)))

    def receipt(self, reference: dict[str, object]) -> dict[str, object]:
        return {
            "schema_version": 1,
            "run_id": "run-1",
            "task_id": "TASK-1",
            "task_sha256": "1" * 64,
            "plan_sha256": "2" * 64,
            "envelope_sha256": "3" * 64,
            "verified_at": "2026-08-01T12:00:00Z",
            "external": None,
            "evidence": {"reposkop_checkout_ref": reference},
            "receipt_sha256": "4" * 64,
        }

    def receipt_errors(self, reference: dict[str, object]) -> list[object]:
        return list(RECEIPT_VALIDATOR.iter_errors(self.receipt(reference)))

    def test_receipt_schema_accepts_valid_nested_reference(self) -> None:
        self.assertEqual([], self.receipt_errors(self.base()))

    def test_receipt_schema_rejects_malformed_nested_digest(self) -> None:
        reference = self.base()
        reference["checkout_identity_sha256"] = "not-a-digest"
        self.assertTrue(self.receipt_errors(reference))

    def test_receipt_schema_rejects_wrong_nested_producer(self) -> None:
        reference = self.base()
        reference["producer"] = "not-reposkop"
        self.assertTrue(self.receipt_errors(reference))

    def test_receipt_schema_rejects_incomplete_nested_transition(self) -> None:
        reference = self.base()
        reference.pop("transition_sha256")
        self.assertTrue(self.receipt_errors(reference))

    def test_reference_cannot_claim_task_truth(self) -> None:
        value = self.base()
        value["task_verified"] = True
        self.assertTrue(list(VALIDATOR.iter_errors(value)))

    def completion_fixture(self, root: Path) -> tuple[object, _FakeStore]:
        task = SimpleNamespace(sha256="1" * 64, initiative="INIT-1")
        registry = SimpleNamespace(
            tasks={"TASK-1": task},
            initiatives={"INIT-1": SimpleNamespace(current_plan={})},
            schemas=SchemaSet(ROOT / "schemas"),
        )
        run_record = {
            "state": "running",
            "task_id": "TASK-1",
            "task_sha256": task.sha256,
            "plan_sha256": plan_sha256(registry, task.initiative),
            "envelope_sha256": "3" * 64,
            "external_system": None,
            "external_id": None,
        }
        envelope = {
            "task": {
                "acceptance": [
                    {
                        "id": "reposkop_checkout_ref",
                        "evidence_type": "object",
                    }
                ]
            }
        }
        return registry, _FakeStore(root, run_record, envelope)

    @staticmethod
    def close_revision(store: _FakeStore) -> SimpleNamespace:
        task_sha256 = store.run_record["task_sha256"]
        plan_digest = store.run_record["plan_sha256"]
        return SimpleNamespace(
            digests=(task_sha256, plan_digest),
            initiative_id="INIT-1",
            task_sha256=task_sha256,
            plan_sha256=plan_digest,
            task_path="registry/tasks/TASK-1.json",
            initiative_path="registry/initiatives/INIT-1.json",
        )

    def assert_complete_run_accepts(self, reference: dict[str, object]) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry, store = self.completion_fixture(Path(directory))
            with patch(
                "bureau.v2._authoritative_close_revision",
                return_value=self.close_revision(store),
            ):
                result = complete_run(
                    registry,
                    store,
                    "run-1",
                    {"reposkop_checkout_ref": reference},
                )
            self.assertEqual(1, store.immediate_calls)
            self.assertTrue(store.receipt_written)
            self.assertTrue(store.verified_written)
            self.assertEqual("succeeded", store.run_record["state"])
            self.assertTrue(Path(result["receipt_path"]).is_file())

    def test_complete_run_accepts_pre_effect_reference(self) -> None:
        reference = self.base()
        reference.update(
            {
                "continuity_state": "pre_effect_only",
                "post_observation_sha256": None,
                "transition_sha256": None,
                "continuity_sha256": None,
            }
        )
        self.assert_complete_run_accepts(reference)

    def test_complete_run_accepts_completed_transition_reference(self) -> None:
        self.assert_complete_run_accepts(self.base())

    def test_complete_run_rejects_malformed_references_before_mutation(self) -> None:
        malformed_references = []

        wrong_producer = self.base()
        wrong_producer["producer"] = "not-reposkop"
        malformed_references.append(wrong_producer)

        malformed_digest = self.base()
        malformed_digest["checkout_identity_sha256"] = "not-a-digest"
        malformed_references.append(malformed_digest)

        incomplete_transition = self.base()
        incomplete_transition.pop("transition_sha256")
        malformed_references.append(incomplete_transition)

        for reference in malformed_references:
            with self.subTest(reference=reference), tempfile.TemporaryDirectory() as directory:
                registry, store = self.completion_fixture(Path(directory))
                with self.assertRaises(DocumentSchemaError):
                    complete_run(
                        registry,
                        store,
                        "run-1",
                        {"reposkop_checkout_ref": reference},
                    )
                self.assertEqual(1, store.immediate_calls)
                self.assertFalse(store.receipt_written)
                self.assertFalse(store.verified_written)
                self.assertEqual("running", store.run_record["state"])


if __name__ == "__main__":
    unittest.main()
