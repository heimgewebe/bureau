from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "bur-2026-004-t005-ops-packaging-decision.md"


def _doc() -> str:
    return DOC.read_text(encoding="utf-8")


def test_ops_packaging_decision_exists_and_names_task() -> None:
    text = _doc()
    assert "BUR-2026-004-T005" in text
    assert "Keep Bureau Core and Bureau Ops in the existing `bureau` Python distribution" in text
    assert "Do not create a separate `bureau-ops` package" in text


def test_ops_packaging_decision_states_packaging_criteria() -> None:
    text = _doc()
    for criterion in (
        "Stable core API",
        "Entry-point stability",
        "Deployment portability",
        "CI coverage",
        "Operator cost",
        "Reversibility",
    ):
        assert criterion in text
    assert "Until these criteria are met, extraction is blocked." in text


def test_ops_packaging_decision_identifies_core_api_surface() -> None:
    text = _doc()
    for surface in (
        "Registry task and initiative JSON schemas",
        "Queue JSON contract",
        "`bureau.cli check` / registry validation result",
        "State-root doctor report",
        "rLens policy report CLI",
        "Entry-point inventory report",
    ):
        assert surface in text
    assert "not stable Core API" in text
    assert "direct imports from `src/bureau/legacy.py`" in text


def test_ops_packaging_decision_weighs_deployment_ci_and_operator_cost() -> None:
    text = _doc()
    for term in (
        "systemd reference deployment",
        "Packaging",
        "CI",
        "Operator cost",
        "Failure diagnosis",
        "Six known service/timer pairs",
    ):
        assert term in text
    assert "keep existing packaged console scripts as stable compatibility shims" in text
    assert "migrate one reference `ops/systemd/*.service` file" in text


def test_ops_packaging_decision_has_non_decision_boundary() -> None:
    text = _doc()
    for boundary in (
        "remove any console script",
        "change any `ops/systemd/` unit",
        "create a new package",
        "declare `legacy.py` removable",
        "prove runtime units are installed or healthy",
    ):
        assert boundary in text
