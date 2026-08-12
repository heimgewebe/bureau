from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .acceptance import validate_acceptance_contract

SCHEMA_FILES = {
    "resource": "resource.v1.schema.json",
    "initiative": "initiative.v1.schema.json",
    "task": "task.v1.schema.json",
    "queue": "queue.v1.schema.json",
    "execution-envelope": "execution-envelope.v1.schema.json",
    "receipt": "receipt.v1.schema.json",
    "source": "source.v1.schema.json",
    "agent-run-evidence-ref": "agent-run-evidence-ref.v1.schema.json",
    "agent-memory-claim": "agent-memory-claim.v1.schema.json",
}


class DocumentSchemaError(ValueError):
    """One Bureau JSON document does not satisfy its declared contract."""


class SchemaSet:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self._validators: dict[str, Draft202012Validator] = {}
        for kind, name in SCHEMA_FILES.items():
            path = self.root / name
            try:
                schema = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise DocumentSchemaError(f"cannot load schema {path}: {exc}") from exc
            Draft202012Validator.check_schema(schema)
            self._validators[kind] = Draft202012Validator(
                schema,
                format_checker=FormatChecker(),
            )

    def validate(self, kind: str, value: dict[str, Any], source: Path | str) -> None:
        validator = self._validators[kind]
        errors = sorted(
            validator.iter_errors(value),
            key=lambda item: tuple(str(part) for part in item.absolute_path),
        )
        if not errors:
            return
        rendered: list[str] = []
        for error in errors:
            location = ".".join(str(item) for item in error.absolute_path) or "$"
            rendered.append(f"{source}: {location}: {error.message}")
        raise DocumentSchemaError("\n".join(rendered))

    def validate_task_write(self, value: dict[str, Any], source: Path | str) -> None:
        """Validate a new/revised TaskSpec structurally and semantically."""

        self.validate("task", value, source)
        validate_acceptance_contract(value)


@lru_cache(maxsize=1)
def default_schema_set() -> SchemaSet:
    """Return the schemas shipped beside the Bureau source/release tree."""

    return SchemaSet(Path(__file__).resolve().parents[2] / "schemas")


def validate_task_write(value: dict[str, Any], source: Path | str) -> None:
    """Strict shared TaskSpec write validation for callers without a Registry."""

    default_schema_set().validate_task_write(value, source)
