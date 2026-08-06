from __future__ import annotations

from pathlib import Path

import pytest

from bureau import cli as bureau_cli
from bureau import source_pr_bridge


def test_parser_forwards_source_pr_bridge_arguments() -> None:
    args = bureau_cli._parse_arguments(
        [
            "source-pr-bridge",
            "--kind",
            "now-refill",
            "--publish",
            "--root",
            "/tmp/repo",
        ]
    )

    assert args.command == "source-pr-bridge"
    assert args.bridge_args == [
        "--kind",
        "now-refill",
        "--publish",
        "--root",
        "/tmp/repo",
    ]


def test_source_pr_bridge_rejects_non_immutable_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    emitted: list[dict[str, object]] = []
    called: list[list[str]] = []
    monkeypatch.setattr(
        bureau_cli,
        "resolve_registry_root",
        lambda _value: (tmp_path, "explicit"),
    )
    monkeypatch.setattr(
        bureau_cli,
        "bureau_runtime_identity",
        lambda _root, state_path=None: {
            "module": {"source_kind": "development"},
            "manifest": {"valid": False, "canonical_registry": {"valid": True}},
            "registry": {"bureau_project": False},
        },
    )
    monkeypatch.setattr(
        bureau_cli,
        "emit",
        lambda value, _json_output: emitted.append(value),
    )
    monkeypatch.setattr(
        source_pr_bridge,
        "main",
        lambda argv=None: called.append(list(argv or [])) or 0,
    )

    assert bureau_cli.main(["source-pr-bridge", "--kind", "now-refill"]) == 2
    assert called == []
    assert emitted[-1]["status"] == "immutable-runtime-required"
    assert emitted[-1]["reason_codes"] == [
        "source-pr-bridge-outside-manifest-release"
    ]


def test_source_pr_bridge_dispatches_from_valid_immutable_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[list[str]] = []
    monkeypatch.setattr(
        bureau_cli,
        "resolve_registry_root",
        lambda _value: (tmp_path, "canonical-runtime-default"),
    )
    monkeypatch.setattr(
        bureau_cli,
        "bureau_runtime_identity",
        lambda _root, state_path=None: {
            "module": {"source_kind": "immutable-release"},
            "manifest": {
                "valid": True,
                "canonical_registry": {"valid": True, "root": str(tmp_path)},
            },
            "registry": {"bureau_project": True, "root": str(tmp_path)},
        },
    )
    monkeypatch.setattr(
        source_pr_bridge,
        "main",
        lambda argv=None: called.append(list(argv or [])) or 23,
    )

    assert (
        bureau_cli.main(
            [
                "source-pr-bridge",
                "--kind",
                "now-refill",
                "--publish",
                "--root",
                "/srv/bureau",
            ]
        )
        == 23
    )
    assert called == [
        ["--kind", "now-refill", "--publish", "--root", "/srv/bureau"]
    ]
