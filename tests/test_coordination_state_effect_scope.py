from __future__ import annotations

from types import SimpleNamespace

from bureau import cli as bureau_cli
from bureau.effect_scope import (
    COORDINATION_STATE_MUTATION,
    classify_command_effect_scope,
)


def test_fail_is_coordination_state_mutation() -> None:
    args = SimpleNamespace(command="fail")

    assert bureau_cli._command_mutates(args) is True
    assert bureau_cli._command_effect_scope(args) == COORDINATION_STATE_MUTATION
    assert (
        classify_command_effect_scope("fail", mutates=True)
        == COORDINATION_STATE_MUTATION
    )


def test_complete_is_coordination_state_mutation() -> None:
    args = SimpleNamespace(command="complete")

    assert bureau_cli._command_mutates(args) is True
    assert bureau_cli._command_effect_scope(args) == COORDINATION_STATE_MUTATION
    assert (
        classify_command_effect_scope("complete", mutates=True)
        == COORDINATION_STATE_MUTATION
    )


def test_workspace_cleanup_is_coordination_state_mutation() -> None:
    args = bureau_cli._parse_arguments(
        ["workspace-cleanup", "BUR-RUN-20260813T120000Z-0123456789"]
    )

    assert bureau_cli._command_mutates(args) is True
    assert bureau_cli._command_effect_scope(args) == COORDINATION_STATE_MUTATION
    assert (
        classify_command_effect_scope("workspace-cleanup", mutates=True)
        == COORDINATION_STATE_MUTATION
    )


def test_acceptance_authenticate_is_coordination_state_mutation() -> None:
    args = bureau_cli._parse_arguments(
        [
            "acceptance-authenticate",
            "BUR-RUN-20260812T120000Z-0123456789",
            "criterion",
            "--expected-evidence-sha256",
            "a" * 64,
            "--reviewer",
            "reviewer",
        ]
    )

    assert bureau_cli._command_mutates(args) is True
    assert bureau_cli._command_effect_scope(args) == COORDINATION_STATE_MUTATION
    assert (
        classify_command_effect_scope("acceptance-authenticate", mutates=True)
        == COORDINATION_STATE_MUTATION
    )


def test_doctor_repair_is_coordination_state_mutation() -> None:
    args = bureau_cli._parse_arguments(["doctor", "--repair"])

    assert bureau_cli._command_mutates(args) is True
    assert bureau_cli._command_effect_scope(args) == COORDINATION_STATE_MUTATION
    assert (
        classify_command_effect_scope("doctor", mutates=True)
        == COORDINATION_STATE_MUTATION
    )


def test_read_only_doctor_modes_remain_read_only() -> None:
    for argv in (
        ["doctor"],
        ["doctor", "--inventory", "broad-bureau-leases"],
    ):
        args = bureau_cli._parse_arguments(argv)

        assert bureau_cli._command_mutates(args) is False
        assert bureau_cli._command_effect_scope(args) == "read_only"
