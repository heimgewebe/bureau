from __future__ import annotations

from types import SimpleNamespace

from bureau import cli as bureau_cli
from bureau.effect_scope import (
    COORDINATION_STATE_MUTATION,
    REGISTRY_MUTATION,
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


def test_complete_remains_registry_mutation() -> None:
    args = SimpleNamespace(command="complete")

    assert bureau_cli._command_mutates(args) is True
    assert bureau_cli._command_effect_scope(args) == REGISTRY_MUTATION
    assert classify_command_effect_scope("complete", mutates=True) == REGISTRY_MUTATION
