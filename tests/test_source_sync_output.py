from __future__ import annotations

import json

import pytest

from bureau.source_sync_output import SourceSyncOutputError, main, parse_source_sync_outputs

COMMIT_SHA = "1" * 40
DOCUMENT_SHA256 = "2" * 64


def _result(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "changed": True,
        "commit_sha": COMMIT_SHA,
        "document_sha256": DOCUMENT_SHA256,
    }
    result.update(overrides)
    return result


def test_accepts_current_result_envelope() -> None:
    outputs = parse_source_sync_outputs(
        {"schema_version": 1, "result": _result(), "runtime_identity": {}}
    )

    assert outputs == {
        "changed": "true",
        "source_commit": COMMIT_SHA,
        "document_sha256": DOCUMENT_SHA256,
    }


def test_accepts_legacy_direct_result() -> None:
    outputs = parse_source_sync_outputs(_result(changed=False))

    assert outputs["changed"] == "false"
    assert outputs["source_commit"] == COMMIT_SHA
    assert outputs["document_sha256"] == DOCUMENT_SHA256


def test_rejects_ambiguous_envelope_and_top_level_fields() -> None:
    with pytest.raises(SourceSyncOutputError, match="ambiguous top-level"):
        parse_source_sync_outputs({"result": _result(), "changed": True})


@pytest.mark.parametrize("changed", ["true", 1, 0, None])
def test_rejects_non_boolean_changed(changed: object) -> None:
    with pytest.raises(SourceSyncOutputError, match="changed must be a boolean"):
        parse_source_sync_outputs(_result(changed=changed))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("commit_sha", "A" * 40, "lowercase 40-character Git SHA"),
        ("commit_sha", "1" * 39, "lowercase 40-character Git SHA"),
        ("document_sha256", "2" * 63, "lowercase 64-character SHA-256"),
        ("document_sha256", "g" * 64, "lowercase 64-character SHA-256"),
    ],
)
def test_rejects_invalid_hash_outputs(field: str, value: str, message: str) -> None:
    with pytest.raises(SourceSyncOutputError, match=message):
        parse_source_sync_outputs(_result(**{field: value}))


def test_rejects_missing_fields() -> None:
    result = _result()
    result.pop("document_sha256")

    with pytest.raises(SourceSyncOutputError, match="document_sha256"):
        parse_source_sync_outputs({"result": result})


def test_rejects_non_object_result() -> None:
    with pytest.raises(SourceSyncOutputError, match=r"report\.result must be a JSON object"):
        parse_source_sync_outputs({"result": []})


def test_cli_writes_exact_github_outputs(tmp_path, capsys) -> None:
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"result": _result()}), encoding="utf-8")

    assert main([str(report)]) == 0
    assert capsys.readouterr().out == (
        "changed=true\n"
        f"source_commit={COMMIT_SHA}\n"
        f"document_sha256={DOCUMENT_SHA256}\n"
    )


def test_cli_fails_closed_for_invalid_json(tmp_path, capsys) -> None:
    report = tmp_path / "report.json"
    report.write_text("not-json", encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        main([str(report)])

    assert exc_info.value.code == 2
    assert "source-sync output error" in capsys.readouterr().err
