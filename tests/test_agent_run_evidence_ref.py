from __future__ import annotations

import json
from pathlib import Path

import pytest

from bureau.schema_validation import DocumentSchemaError, SchemaSet

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "agent-run-evidence-ref"


def schemas() -> SchemaSet:
    return SchemaSet(ROOT / "schemas")


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def test_local_preview_fixture_matches_schema() -> None:
    schemas().validate(
        "agent-run-evidence-ref",
        load_fixture("local-preview.valid.json"),
        "local-preview.valid.json",
    )


def test_chronik_event_fixture_matches_schema() -> None:
    schemas().validate(
        "agent-run-evidence-ref",
        load_fixture("chronik-event.valid.json"),
        "chronik-event.valid.json",
    )


def test_local_preview_requires_preview_root_name() -> None:
    value = load_fixture("local-preview.valid.json")
    value.pop("preview_root_name")

    with pytest.raises(DocumentSchemaError, match="preview_root_name"):
        schemas().validate("agent-run-evidence-ref", value, "missing-root")


def test_chronik_event_requires_event_id() -> None:
    value = load_fixture("chronik-event.valid.json")
    value.pop("chronik_event_id")

    with pytest.raises(DocumentSchemaError, match="chronik_event_id"):
        schemas().validate("agent-run-evidence-ref", value, "missing-event")


def test_unknown_fields_are_rejected() -> None:
    value = load_fixture("local-preview.valid.json")
    value["raw_payload"] = {"not": "allowed"}

    with pytest.raises(DocumentSchemaError, match="Additional properties"):
        schemas().validate("agent-run-evidence-ref", value, "extra")


def test_does_not_establish_must_not_be_empty() -> None:
    value = load_fixture("local-preview.valid.json")
    value["does_not_establish"] = []

    with pytest.raises(DocumentSchemaError, match="should be non-empty"):
        schemas().validate("agent-run-evidence-ref", value, "limits")
