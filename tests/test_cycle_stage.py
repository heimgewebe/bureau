from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import bureau.cycle_stage as cycle_stage
from bureau import cli as bureau_cli


def test_stage_inventory_is_exact() -> None:
    assert cycle_stage.STAGE_TARGETS == {
        "discovery": ("bureau_cycle.discovery_runner", "main"),
        "curator": ("bureau_cycle.curator_runner", "main"),
        "operator": ("bureau_cycle.operator_runner", "main"),
        "verifier": ("bureau_cycle.verifier_runner", "run"),
        "closure": ("bureau.closure_runner", "main"),
    }


@pytest.mark.parametrize("stage", tuple(cycle_stage.STAGE_TARGETS))
def test_dispatches_exact_stage_callable_and_restores_argv(
    stage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str, list[str]]] = []
    module_name, callable_name = cycle_stage.STAGE_TARGETS[stage]

    def fake_import(name: str) -> SimpleNamespace:
        def entrypoint() -> int:
            calls.append((name, callable_name, list(cycle_stage.sys.argv)))
            return 17

        return SimpleNamespace(**{callable_name: entrypoint})

    previous_argv = cycle_stage.sys.argv
    monkeypatch.setattr(cycle_stage.importlib, "import_module", fake_import)

    assert cycle_stage.run_stage(stage, ["--probe"]) == 17
    assert calls == [(module_name, callable_name, [module_name, "--probe"])]
    assert cycle_stage.sys.argv is previous_argv


def test_unknown_stage_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported Bureau cycle stage"):
        cycle_stage.run_stage("unknown")


def test_cli_parser_accepts_manifest_bound_cycle_commands() -> None:
    run_args = bureau_cli.parser().parse_args(["cycle-run", "discovery", "--probe"])
    assert run_args.command == "cycle-run"
    assert run_args.stage == "discovery"
    assert run_args.stage_args == ["--probe"]

    audit_args = bureau_cli.parser().parse_args(
        [
            "cycle-deployment",
            "--manifest",
            "/tmp/manifest.json",
            "--unit-root",
            "/tmp/units",
            "--shim-root",
            "/tmp/shims",
        ]
    )
    assert audit_args.command == "cycle-deployment"
    assert audit_args.manifest == Path("/tmp/manifest.json")
    assert audit_args.unit_root == Path("/tmp/units")
    assert audit_args.shim_root == Path("/tmp/shims")


def test_cycle_run_rejects_non_immutable_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    emitted: list[dict[str, object]] = []
    called: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(bureau_cli, "resolve_registry_root", lambda _value: (tmp_path, "explicit"))
    monkeypatch.setattr(
        bureau_cli,
        "bureau_runtime_identity",
        lambda _root, state_path=None: {
            "module": {"source_kind": "source-checkout"},
            "manifest": {"valid": False},
            "registry": {"bureau_project": False},
        },
    )
    monkeypatch.setattr(bureau_cli, "emit", lambda value, _json_output: emitted.append(value))
    monkeypatch.setattr(
        cycle_stage,
        "run_stage",
        lambda stage, argv: called.append((stage, list(argv))) or 0,
    )

    assert bureau_cli.main(["cycle-run", "discovery"]) == 2
    assert called == []
    assert emitted[-1]["status"] == "immutable-runtime-required"


def test_cycle_run_dispatches_only_from_valid_immutable_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[tuple[str, list[str]]] = []
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
            "registry_selection": "canonical-runtime-default",
        },
    )
    monkeypatch.setattr(
        cycle_stage,
        "run_stage",
        lambda stage, argv: called.append((stage, list(argv))) or 19,
    )

    assert bureau_cli.main(["cycle-run", "verifier", "--probe"]) == 19
    assert called == [("verifier", ["--probe"])]


def test_cycle_run_rejects_explicit_registry_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    emitted: list[dict[str, object]] = []
    called: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(bureau_cli, "resolve_registry_root", lambda _value: (tmp_path, "explicit"))
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
            "registry_selection": "explicit",
        },
    )
    monkeypatch.setattr(bureau_cli, "emit", lambda value, _json_output: emitted.append(value))
    monkeypatch.setattr(
        cycle_stage,
        "run_stage",
        lambda stage, argv: called.append((stage, list(argv))) or 0,
    )

    assert bureau_cli.main(["--root", str(tmp_path), "cycle-run", "operator"]) == 2
    assert called == []
    assert emitted[-1]["status"] == "immutable-runtime-required"
