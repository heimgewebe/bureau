from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
