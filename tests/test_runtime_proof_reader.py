"""Regressions for the trusted primary-reader wiring at exact-runtime closeout.

These tests do not manufacture a Kubernetes runtime proof or grant T044 acceptance.
"""

from __future__ import annotations

from test_v2 import _prepare_runtime_closeout_case

from bureau import closure_observer
from bureau import v2 as bureau_v2


def test_runtime_closeout_passes_trusted_reader_to_source_authentication(
    registry_factory, tmp_path, monkeypatch
) -> None:
    """A caller-controlled proof adapter must reach the real closure boundary."""
    case = _prepare_runtime_closeout_case(registry_factory, tmp_path, monkeypatch)
    observed_readers = []
    original_authenticate = closure_observer.authenticate_state_evidence

    def observe_authentication(*args, **kwargs):
        observed_readers.append(kwargs.get("runtime_proof_reader"))
        return original_authenticate(*args, **kwargs)

    monkeypatch.setattr(closure_observer, "authenticate_state_evidence", observe_authentication)

    def trusted_reader(_request):
        raise AssertionError("non-runtime fixture must not request a T044 proof")

    outcome = bureau_v2.runtime_closeout(
        case["store"],
        case["run_id"],
        case["evidence_path"],
        resource_db=case["database"],
        runtime_proof_reader=trusted_reader,
    )
    assert outcome["status"] == "succeeded"
    assert observed_readers == [trusted_reader]


def test_cli_embedded_runtime_reader_is_passed_to_dispatcher(
    registry_factory, tmp_path, monkeypatch, capsys
) -> None:
    """Only trusted in-process callers can inject a primary reader."""
    from bureau import cli as bureau_cli

    root = registry_factory(1)
    observed = {}

    class CapturingDispatcher:
        def __init__(self, registry, store, adapters, **kwargs):
            observed.update(kwargs)

    monkeypatch.setattr(bureau_cli, "Dispatcher", CapturingDispatcher)

    def primary_reader(_request):
        raise AssertionError("status is read-only; no runtime proof was requested")

    result = bureau_cli.main(
        [
            "--root",
            str(root),
            "--state-db",
            str(tmp_path / "cli-runtime-reader.sqlite3"),
            "--json",
            "status",
        ],
        runtime_proof_reader=primary_reader,
    )
    capsys.readouterr()
    assert result == 0
    assert observed["runtime_proof_reader"] is primary_reader


def test_cli_without_primary_reader_remains_fail_closed(
    registry_factory, tmp_path, monkeypatch, capsys
) -> None:
    """The executable CLI must not fabricate an observer when none is installed."""
    from bureau import cli as bureau_cli

    root = registry_factory(1)
    observed = {}

    class CapturingDispatcher:
        def __init__(self, registry, store, adapters, **kwargs):
            observed.update(kwargs)

    monkeypatch.setattr(bureau_cli, "Dispatcher", CapturingDispatcher)
    result = bureau_cli.main(
        [
            "--root",
            str(root),
            "--state-db",
            str(tmp_path / "cli-runtime-no-reader.sqlite3"),
            "--json",
            "status",
        ]
    )
    capsys.readouterr()
    assert result == 0
    assert "runtime_proof_reader" not in observed


def test_cli_exact_runtime_closeout_receives_trusted_reader(
    registry_factory, tmp_path, monkeypatch, capsys
) -> None:
    """Embedded caller callback must reach the actual exact-closeout entry point."""
    import json

    from bureau import cli as bureau_cli

    case = _prepare_runtime_closeout_case(registry_factory, tmp_path, monkeypatch)
    sha = case["deployed_commit"]
    registry_root = str(case["snapshot_root"])
    identity = {
        "registry": {
            "bureau_project": True,
            "role": "canonical-runtime-snapshot",
            "root": registry_root,
            "head": sha,
            "origin_main": sha,
            "head_equals_origin_main": True,
            "dirty": False,
        },
        "manifest": {
            "valid": True,
            "source_commit": sha,
            "canonical_registry": {
                "valid": True,
                "root": registry_root,
                "source_commit": sha,
            },
        },
        "compatibility": {
            "status": "canonical-read-only",
            "mutation_allowed": False,
            "reason_codes": ["canonical-registry-read-only"],
        },
    }
    monkeypatch.setattr(
        bureau_cli,
        "resolve_registry_root",
        lambda _value: (case["snapshot_root"], "canonical-runtime-default"),
    )
    monkeypatch.setattr(
        bureau_cli,
        "bureau_runtime_identity",
        lambda *args, **kwargs: json.loads(json.dumps(identity)),
    )
    observed = {}

    def stub_closeout(store, run_id, evidence_path, *, runtime_proof_reader=None):
        observed["run_id"] = run_id
        observed["reader"] = runtime_proof_reader
        return {"status": "succeeded"}

    monkeypatch.setattr(bureau_cli, "runtime_closeout", stub_closeout)

    def primary_reader(_request):
        raise AssertionError("only a T044 proof should ever invoke the reader")

    result = bureau_cli.main(
        [
            "--state-root",
            str(case["store"].state_root),
            "--json",
            "complete",
            case["run_id"],
            "--evidence",
            str(case["evidence_path"]),
            "--exact-runtime",
        ],
        runtime_proof_reader=primary_reader,
    )
    output = json.loads(capsys.readouterr().out)
    assert result == 0
    assert output["result"]["status"] == "succeeded"
    assert observed == {
        "run_id": case["run_id"],
        "reader": primary_reader,
    }
