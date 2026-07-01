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
    graph_report,
    load_graph,
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


if __name__ == "__main__":
    unittest.main()
