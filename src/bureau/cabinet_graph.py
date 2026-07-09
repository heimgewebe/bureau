from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .approval import explicit_operator_approval, require_approval

DEFAULT_GRAPH_PATH = Path.home() / "repos/cabinet/steuerung/10 Lage/ecosystem-graph.json"


class CabinetGraphError(RuntimeError):
    """Raised when the Cabinet ecosystem graph cannot be consumed safely."""


@dataclass(frozen=True)
class CabinetGraphSummary:
    graph_path: str
    node_count: int
    repository_count: int
    warning_count: int
    candidate_count: int


def _expect_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CabinetGraphError(f"{label} must be an object")
    return value


def _expect_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise CabinetGraphError(f"{label} must be a list")
    return value


def load_graph(path: str | Path = DEFAULT_GRAPH_PATH) -> dict[str, Any]:
    """Load and minimally validate a Cabinet ecosystem graph.

    This is intentionally read-only. It validates only the envelope Bureau needs
    before deriving local diagnostic candidates. Full graph authorship remains in
    Cabinet.
    """
    graph_path = Path(path)
    try:
        raw = graph_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise CabinetGraphError(f"Cabinet graph missing: {graph_path}") from exc
    except OSError as exc:
        raise CabinetGraphError(
            f"Cabinet graph cannot be read: {graph_path}: {exc.__class__.__name__}"
        ) from exc

    try:
        graph = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CabinetGraphError(f"Cabinet graph is invalid JSON: {exc.msg}") from exc

    graph = _expect_dict(graph, "Cabinet graph")
    if graph.get("schemaVersion") != 1:
        raise CabinetGraphError("Cabinet graph schemaVersion must be 1")
    if graph.get("kind") != "ecosystem_graph":
        raise CabinetGraphError("Cabinet graph kind must be ecosystem_graph")
    _expect_list(graph.get("nodes"), "Cabinet graph nodes")
    _expect_dict(graph.get("source"), "Cabinet graph source")
    warnings = graph.get("warnings", [])
    if not isinstance(warnings, list):
        raise CabinetGraphError("Cabinet graph warnings must be a list when present")
    return graph


def repository_nodes(graph: dict[str, Any]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for index, raw_node in enumerate(_expect_list(graph.get("nodes"), "Cabinet graph nodes")):
        node = _expect_dict(raw_node, f"Cabinet graph node {index}")
        if node.get("kind") != "ecosystem_node":
            raise CabinetGraphError(f"Cabinet graph node {index} has invalid kind")
        if node.get("nodeType") == "repository":
            nodes.append(node)
    return nodes


def _candidate_id(node: dict[str, Any], suffix: str) -> str:
    raw_id = str(node.get("id", "repo:unknown"))
    safe = raw_id.replace(":", "-").replace("/", "-")
    return f"cabinet-graph:{safe}:{suffix}"


def derive_diagnostic_candidates(graph: dict[str, Any]) -> list[dict[str, Any]]:
    """Derive Bureau-local diagnostic candidates from Cabinet graph health hints.

    These are not Bureau tasks and must not be dispatched automatically. They are
    candidate observations for a future frontier stage.
    """
    candidates: list[dict[str, Any]] = []
    for node in repository_nodes(graph):
        dimensions = node.get("healthDimensions", [])
        if not isinstance(dimensions, list):
            raise CabinetGraphError(
                f"node {node.get('id', '<unknown>')} healthDimensions must be a list"
            )
        source_refs = node.get("sources", [])
        if not isinstance(source_refs, list):
            raise CabinetGraphError(f"node {node.get('id', '<unknown>')} sources must be a list")
        node_name = str(node.get("name", node.get("id", "unknown")))
        node_id = str(node.get("id", "repo:unknown"))

        if "review_import_drift" in dimensions:
            candidates.append(
                {
                    "schemaVersion": 1,
                    "kind": "bureau_frontier_candidate",
                    "id": _candidate_id(node, "review-import-drift"),
                    "source": "cabinet_ecosystem_graph",
                    "targetNode": node_id,
                    "repository": node_name,
                    "reason": "Cabinet graph reports drift between review HEAD and import HEAD.",
                    "risk": "medium",
                    "suggestedAction": "diagnose_repository_reference_drift",
                    "dispatchAllowed": False,
                    "evidence": source_refs,
                }
            )
        if "dirty_import_worktree" in dimensions:
            candidates.append(
                {
                    "schemaVersion": 1,
                    "kind": "bureau_frontier_candidate",
                    "id": _candidate_id(node, "dirty-import-worktree"),
                    "source": "cabinet_ecosystem_graph",
                    "targetNode": node_id,
                    "repository": node_name,
                    "reason": "Cabinet graph reports a dirty worktree at import time.",
                    "risk": "low",
                    "suggestedAction": "review_import_worktree_snapshot",
                    "dispatchAllowed": False,
                    "evidence": source_refs,
                }
            )
    return sorted(candidates, key=lambda candidate: candidate["id"])


def summarize_graph(path: str | Path = DEFAULT_GRAPH_PATH) -> CabinetGraphSummary:
    graph = load_graph(path)
    candidates = derive_diagnostic_candidates(graph)
    warnings = graph.get("warnings", [])
    return CabinetGraphSummary(
        graph_path=str(Path(path)),
        node_count=len(_expect_list(graph.get("nodes"), "Cabinet graph nodes")),
        repository_count=len(repository_nodes(graph)),
        warning_count=len(warnings) if isinstance(warnings, list) else 0,
        candidate_count=len(candidates),
    )


def graph_report(path: str | Path = DEFAULT_GRAPH_PATH) -> dict[str, Any]:
    graph = load_graph(path)
    candidates = derive_diagnostic_candidates(graph)
    warnings = graph.get("warnings", [])
    summary = summarize_graph(path)
    return {
        "schemaVersion": 1,
        "kind": "cabinet_graph_report",
        "mode": "read_only",
        "dispatchAllowed": False,
        "summary": {
            "graphPath": summary.graph_path,
            "nodeCount": summary.node_count,
            "repositoryCount": summary.repository_count,
            "warningCount": summary.warning_count,
            "candidateCount": summary.candidate_count,
        },
        "candidates": candidates,
        "warnings": warnings if isinstance(warnings, list) else [],
    }


def frontier_export(path: str | Path = DEFAULT_GRAPH_PATH) -> dict[str, Any]:
    """Return a read-only Frontier export derived from Cabinet graph candidates.

    The export is intentionally not a Bureau queue and not executable work. It is
    a stable observation surface for later review and promotion.
    """
    candidates = derive_diagnostic_candidates(load_graph(path))
    return {
        "schemaVersion": 1,
        "kind": "cabinet_frontier_export",
        "contract": "bureau-cabinet-frontier-export.v1",
        "mode": "read_only",
        "source": "cabinet_ecosystem_graph",
        "graphPath": str(Path(path)),
        "dispatchAllowed": False,
        "queueMutationAllowed": False,
        "taskCreationAllowed": False,
        "candidateCount": len(candidates),
        "candidates": candidates,
    }


def load_frontier_export(path: str | Path) -> dict[str, Any]:
    """Load and minimally validate a read-only Cabinet frontier export."""
    export_path = Path(path)
    try:
        raw = export_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise CabinetGraphError(f"Cabinet frontier export missing: {export_path}") from exc
    except OSError as exc:
        raise CabinetGraphError(
            f"Cabinet frontier export cannot be read: {export_path}: {exc.__class__.__name__}"
        ) from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CabinetGraphError(f"Cabinet frontier export is invalid JSON: {exc.msg}") from exc
    export = _expect_dict(value, "Cabinet frontier export")
    if export.get("schemaVersion") != 1:
        raise CabinetGraphError("Cabinet frontier export schemaVersion must be 1")
    if export.get("kind") != "cabinet_frontier_export":
        raise CabinetGraphError("Cabinet frontier export kind must be cabinet_frontier_export")
    _validate_frontier_export(export)
    return export


def _validate_frontier_export(export: dict[str, Any]) -> None:
    if export.get("dispatchAllowed") is not False:
        raise CabinetGraphError("Cabinet frontier export must keep dispatchAllowed false")
    if export.get("queueMutationAllowed") is not False:
        raise CabinetGraphError("Cabinet frontier export must keep queueMutationAllowed false")
    if export.get("taskCreationAllowed") is not False:
        raise CabinetGraphError("Cabinet frontier export must keep taskCreationAllowed false")
    _expect_list(export.get("candidates"), "Cabinet frontier export candidates")


def _resource_for_repository(repository: str) -> str:
    safe = "".join(
        character.lower() if character.isalnum() else "." for character in repository.strip()
    ).strip(".")
    return f"repo.{safe or 'unknown'}"


def promote_frontier_candidate(
    export: dict[str, Any],
    *,
    candidate_id: str,
    task_id: str,
    initiative: str,
    target_proof: str,
    approve: bool,
) -> dict[str, Any]:
    """Prepare a Bureau task proposal from one exported Cabinet candidate.

    The gate is explicit and non-mutating: it never writes to the Bureau registry
    and never dispatches work. The returned task is a review-before-effect draft.
    """
    _validate_frontier_export(export)
    target_proof = target_proof.strip()
    task_id = task_id.strip()
    initiative = initiative.strip()
    if not target_proof:
        raise CabinetGraphError("promotion requires non-empty target proof")
    if not task_id:
        raise CabinetGraphError("promotion requires task id")
    if not initiative:
        raise CabinetGraphError("promotion requires initiative id")
    if not approve:
        raise CabinetGraphError("promotion requires explicit --approve")
    approval = require_approval(
        "task_creation_from_external_evidence",
        explicit_operator_approval(
            source="cli --approve",
            approved=approve,
            reference=task_id,
            task_id=task_id,
            scope="task_creation_from_external_evidence",
        ),
        expected_reference=task_id,
        task_id=task_id,
    )

    candidates = _expect_list(export.get("candidates"), "Cabinet frontier export candidates")
    selected = None
    for raw_candidate in candidates:
        candidate = _expect_dict(raw_candidate, "Cabinet frontier candidate")
        if candidate.get("id") == candidate_id:
            selected = candidate
            break
    if selected is None:
        raise CabinetGraphError(f"candidate not found in Cabinet frontier export: {candidate_id}")
    if selected.get("dispatchAllowed") is not False:
        raise CabinetGraphError("candidate must keep dispatchAllowed false")

    repository = str(selected.get("repository", "unknown"))
    suggested_action = str(selected.get("suggestedAction", "review_cabinet_candidate"))
    risk = str(selected.get("risk", "medium"))
    priority_rank = 55 if risk == "medium" else 75
    task = {
        "schema_version": 1,
        "id": task_id.strip(),
        "initiative": initiative.strip(),
        "title": f"Review Cabinet candidate for {repository}",
        "state": "planned",
        "goal": (
            f"Review Cabinet graph candidate {candidate_id}: "
            f"{selected.get('reason', suggested_action)}"
        ),
        "required_capabilities": ["repository", "review"],
        "priority": {"lane": "next", "rank": priority_rank},
        "execution": {"mode": "manual", "policy": "review-before-effect"},
        "claims": [
            {
                "resource": _resource_for_repository(repository),
                "mode": "read",
                "isolation": "none",
            }
        ],
        "acceptance": [
            {"id": "target-proof", "assertion": target_proof},
            {
                "id": "no-auto-dispatch",
                "assertion": (
                    "Promotion creates a review-before-effect task proposal only; "
                    "no dispatch is allowed."
                ),
            },
        ],
        "metadata": {
            "source": "cabinet_frontier_export",
            "source_candidate_id": candidate_id,
            "source_candidate": selected,
            "dispatch_allowed": False,
            "queue_mutation_allowed": False,
            "task_creation_allowed": False,
        },
    }
    return {
        "schemaVersion": 1,
        "kind": "cabinet_frontier_promotion",
        "mode": "proposal_only",
        "approved": True,
        "approval": approval,
        "dispatchAllowed": False,
        "queueMutationAllowed": False,
        "taskCreationAllowed": False,
        "task": task,
    }
