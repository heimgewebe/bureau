from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import TextIO

_RESULT_FIELDS = {"changed", "commit_sha", "document_sha256"}
_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SourceSyncOutputError(ValueError):
    """Raised when source-sync output cannot be mapped to trusted workflow outputs."""


def _result_payload(report: object) -> dict[str, object]:
    if not isinstance(report, dict):
        raise SourceSyncOutputError("report must be a JSON object")

    if "result" not in report:
        return report

    conflicting_fields = sorted(_RESULT_FIELDS.intersection(report))
    if conflicting_fields:
        names = ", ".join(conflicting_fields)
        raise SourceSyncOutputError(
            f"enveloped report also defines ambiguous top-level result fields: {names}"
        )

    result = report["result"]
    if not isinstance(result, dict):
        raise SourceSyncOutputError("report.result must be a JSON object")
    return result


def parse_source_sync_outputs(report: object) -> dict[str, str]:
    """Validate direct or enveloped source-sync JSON and return safe GitHub outputs."""

    payload = _result_payload(report)

    changed = payload.get("changed")
    if type(changed) is not bool:
        raise SourceSyncOutputError("result.changed must be a boolean")

    commit_sha = payload.get("commit_sha")
    if not isinstance(commit_sha, str) or _COMMIT_SHA_RE.fullmatch(commit_sha) is None:
        raise SourceSyncOutputError("result.commit_sha must be a lowercase 40-character Git SHA")

    document_sha256 = payload.get("document_sha256")
    if (
        not isinstance(document_sha256, str)
        or _SHA256_RE.fullmatch(document_sha256) is None
    ):
        raise SourceSyncOutputError(
            "result.document_sha256 must be a lowercase 64-character SHA-256"
        )

    return {
        "changed": "true" if changed else "false",
        "source_commit": commit_sha,
        "document_sha256": document_sha256,
    }


def write_github_outputs(outputs: dict[str, str], stream: TextIO) -> None:
    for key in ("changed", "source_commit", "document_sha256"):
        stream.write(f"{key}={outputs[key]}\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract validated GitHub outputs from Bureau source-sync JSON."
    )
    parser.add_argument("report", type=Path, help="Path to the source-sync JSON report")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        outputs = parse_source_sync_outputs(report)
    except (OSError, json.JSONDecodeError, SourceSyncOutputError) as exc:
        parser.exit(2, f"source-sync output error: {exc}\n")

    write_github_outputs(outputs, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
