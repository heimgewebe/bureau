from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from bureau.cabinet_graph import (
    CabinetGraphError,
    derive_diagnostic_candidates,
    frontier_export,
    graph_report,
    load_graph,
    promote_frontier_candidate,
    repository_nodes,
    summarize_graph,
)


def write_graph(path: Path, graph: dict) -> None:
    path.write_text(json.dumps(graph, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def repo_node(name: str, dimensions: list[str] | None = None) -> dict:
    return {
        "schemaVersion": 1,
        "kind": "ecosystem_node",
        "id": f"repo:{name.lower()}",
        "nodeType": "repository",
        "name": name,
        "status": "observed",
        "healthDimensions": dimensions or ["reference_freshness"],
        "sources": [
            {
                "type": "cabinet",
                "ref": "werkstatt/20 Werkzeuge/Repository Reference.md",
                "observedAt": "2026-06-23T18:38:45+00:00",
            }
        ],
    }


def graph(nodes: list[dict], warnings: list[str] | None = None) -> dict:
    return {
        "schemaVersion": 1,
        "kind": "ecosystem_graph",
        "source": {"type": "cabinet_repository_references", "trackedReferences": len(nodes)},
        "nodes": nodes,
        "warnings": warnings or [],
    }


class CabinetGraphReaderTests(unittest.TestCase):
    def test_loads_valid_graph_and_summarizes_without_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "graph.json"
            write_graph(path, graph([repo_node("cabinet")]))
            loaded = load_graph(path)
            self.assertEqual([node["name"] for node in repository_nodes(loaded)], ["cabinet"])
            summary = summarize_graph(path)
            self.assertEqual(summary.node_count, 1)
            self.assertEqual(summary.repository_count, 1)
            self.assertEqual(summary.warning_count, 0)
            self.assertEqual(summary.candidate_count, 0)

    def test_derives_read_only_candidates_for_drift_and_dirty_worktree(self) -> None:
        loaded = graph(
            [
                repo_node(
                    "steuerboard",
                    ["reference_freshness", "dirty_import_worktree", "review_import_drift"],
                )
            ]
        )
        candidates = derive_diagnostic_candidates(loaded)
        self.assertEqual(len(candidates), 2)
        self.assertEqual(
            sorted(candidate["suggestedAction"] for candidate in candidates),
            ["diagnose_repository_reference_drift", "review_import_worktree_snapshot"],
        )
        for candidate in candidates:
            self.assertFalse(candidate["dispatchAllowed"])
            self.assertEqual(candidate["source"], "cabinet_ecosystem_graph")
            self.assertEqual(candidate["repository"], "steuerboard")
            self.assertEqual(candidate["targetNode"], "repo:steuerboard")
            self.assertTrue(candidate["evidence"])

    def test_graph_report_is_read_only_surface(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "graph.json"
            write_graph(
                path,
                graph(
                    [
                        repo_node(
                            "steuerboard",
                            ["review_import_drift", "dirty_import_worktree"],
                        )
                    ]
                ),
            )
            report = graph_report(path)
            self.assertEqual(report["kind"], "cabinet_graph_report")
            self.assertEqual(report["mode"], "read_only")
            self.assertFalse(report["dispatchAllowed"])
            self.assertEqual(report["summary"]["candidateCount"], 2)
            self.assertEqual(len(report["candidates"]), 2)

    def test_cli_emits_read_only_json_report(self) -> None:
        from bureau.cli import main

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "graph.json"
            write_graph(path, graph([repo_node("cabinet")]))
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                result = main(["--json", "cabinet-graph", "--graph", str(path)])
            self.assertEqual(result, 0)
            payload = json.loads(stream.getvalue())
            self.assertEqual(payload["kind"], "cabinet_graph_report")
            self.assertFalse(payload["dispatchAllowed"])
            self.assertEqual(payload["summary"]["repositoryCount"], 1)

    def test_rejects_missing_file_and_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"
            with self.assertRaisesRegex(CabinetGraphError, "missing"):
                load_graph(missing)
            bad = Path(directory) / "bad.json"
            bad.write_text("{not-json\n", encoding="utf-8")
            with self.assertRaisesRegex(CabinetGraphError, "invalid JSON"):
                load_graph(bad)

    def test_rejects_wrong_graph_kind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wrong.json"
            write_graph(path, {"schemaVersion": 1, "kind": "other", "source": {}, "nodes": []})
            with self.assertRaisesRegex(CabinetGraphError, "kind"):
                load_graph(path)

    def test_rejects_non_object_node(self) -> None:
        with self.assertRaisesRegex(CabinetGraphError, "node 0"):
            repository_nodes(graph(["not-a-node"]))  # type: ignore[list-item]

    def test_non_repository_nodes_are_ignored(self) -> None:
        service = {
            "schemaVersion": 1,
            "kind": "ecosystem_node",
            "id": "service:daemon",
            "nodeType": "service",
            "name": "daemon",
            "status": "observed",
            "sources": [{"type": "manual", "ref": "test"}],
        }
        self.assertEqual(
            [node["name"] for node in repository_nodes(graph([repo_node("cabinet"), service]))],
            ["cabinet"],
        )

    def test_frontier_export_keeps_candidates_non_executable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "graph.json"
            write_graph(
                path,
                graph([repo_node("bureau", ["review_import_drift"])]),
            )
            export = frontier_export(path)
            self.assertEqual(export["kind"], "cabinet_frontier_export")
            self.assertEqual(export["mode"], "read_only")
            self.assertFalse(export["dispatchAllowed"])
            self.assertFalse(export["queueMutationAllowed"])
            self.assertFalse(export["taskCreationAllowed"])
            self.assertEqual(export["candidateCount"], 1)
            self.assertEqual(export["candidates"][0]["repository"], "bureau")
            self.assertFalse(export["candidates"][0]["dispatchAllowed"])

    def test_cli_emits_read_only_frontier_export(self) -> None:
        from bureau.cli import main

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "graph.json"
            write_graph(path, graph([repo_node("bureau", ["dirty_import_worktree"])]))
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                result = main(["--json", "cabinet-frontier", "--graph", str(path)])
            self.assertEqual(result, 0)
            payload = json.loads(stream.getvalue())
            self.assertEqual(payload["kind"], "cabinet_frontier_export")
            self.assertFalse(payload["dispatchAllowed"])
            self.assertFalse(payload["queueMutationAllowed"])
            self.assertEqual(payload["candidateCount"], 1)


if __name__ == "__main__":
    unittest.main()


class CabinetPromotionGateTests(unittest.TestCase):
    def _export(self) -> dict:
        return {
            "schemaVersion": 1,
            "kind": "cabinet_frontier_export",
            "dispatchAllowed": False,
            "queueMutationAllowed": False,
            "taskCreationAllowed": False,
            "candidates": [
                {
                    "schemaVersion": 1,
                    "kind": "bureau_frontier_candidate",
                    "id": "cabinet-graph:repo-bureau:review-import-drift",
                    "source": "cabinet_ecosystem_graph",
                    "targetNode": "repo:bureau",
                    "repository": "bureau",
                    "reason": "Cabinet graph reports drift between review HEAD and import HEAD.",
                    "risk": "medium",
                    "suggestedAction": "diagnose_repository_reference_drift",
                    "dispatchAllowed": False,
                    "evidence": [{"type": "cabinet", "ref": "test"}],
                }
            ],
        }

    def test_promotion_gate_requires_explicit_approval(self) -> None:
        export = self._export()
        with self.assertRaisesRegex(CabinetGraphError, "approve"):
            promote_frontier_candidate(
                export,
                candidate_id=export["candidates"][0]["id"],
                task_id="BUR-CAB-ECO-001",
                initiative="BUR-CAB-ECO",
                target_proof="A reviewed proof exists.",
                approve=False,
            )

    def test_promotion_gate_returns_non_dispatching_task_proposal(self) -> None:
        export = self._export()
        result = promote_frontier_candidate(
            export,
            candidate_id=export["candidates"][0]["id"],
            task_id="BUR-CAB-ECO-001",
            initiative="BUR-CAB-ECO",
            target_proof="A reviewed proof exists.",
            approve=True,
        )
        self.assertEqual(result["kind"], "cabinet_frontier_promotion")
        self.assertFalse(result["dispatchAllowed"])
        self.assertFalse(result["queueMutationAllowed"])
        self.assertFalse(result["taskCreationAllowed"])
        task = result["task"]
        self.assertEqual(task["id"], "BUR-CAB-ECO-001")
        self.assertEqual(task["execution"]["policy"], "review-before-effect")
        self.assertEqual(task["execution"]["mode"], "manual")
        self.assertFalse(task["metadata"]["dispatch_allowed"])
