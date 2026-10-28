"""Unavailable never-seen sources must not stall unrelated Bureau discovery."""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from bureau_cycle import discovery


class UnavailableDiscoverySourcesTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        self.state.mkdir()
        patches = {
            "STATE": self.state,
            "REGISTRY": self.state / "source-registry.json",
            "SOURCE_STATE": self.state / "source-state.json",
            "RUNS": self.state / "runs",
            "INBOX": self.state / "inbox",
            "LOCK": self.state / "scanner.lock",
        }
        for attribute, value in patches.items():
            scoped = patch.object(discovery, attribute, value)
            scoped.start()
            self.addCleanup(scoped.stop)
        self.healthy = self.root / "healthy"
        self.missing = self.root / "missing"
        self.vault = self.root / "vault"
        self._make_repo(self.healthy)
        self._write_registry()
        self._write_previous()

    def _git(self, repo: Path, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
        )

    def _make_repo(self, repo: Path) -> None:
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        self._git(repo, "config", "user.email", "bureau-test@example.invalid")
        self._git(repo, "config", "user.name", "Bureau Test")
        (repo / "plan.md").write_text("# Roadmap\n- TODO: test eligible source\n")
        self._git(repo, "add", "--", "plan.md")
        self._git(repo, "commit", "-qm", "add plan")

    def _write_registry(self) -> None:
        discovery.REGISTRY.write_text(json.dumps({
            "repositories": [
                {"name": "healthy", "source_id": "repo:healthy", "enabled": True,
                 "root": str(self.healthy), "ref": "HEAD", "planning_files": ["plan.md"]},
                {"name": "missing", "source_id": "repo:missing", "enabled": True,
                 "root": str(self.missing), "ref": "HEAD", "planning_files": ["plan.md"]},
            ],
            "vault_root": str(self.vault), "vault_ref": "HEAD",
        }))

    def _write_previous(self, documents: dict | None = None) -> None:
        discovery.SOURCE_STATE.write_text(json.dumps({
            "documents": {} if documents is None else documents,
            "candidate_fingerprints": [],
        }))

    def _scan(self) -> dict:
        with redirect_stdout(io.StringIO()):
            self.assertEqual(discovery.main(), 0)
        return json.loads((self.state / "latest.json").read_text())

    def test_never_seen_missing_sources_do_not_block_other_checked_out_repo(self) -> None:
        for _ in range(2):
            report = self._scan()
            self.assertTrue(report["promotion_allowed"])
            self.assertFalse(report["degraded"])
            self.assertEqual([], report["scanner_errors"])
            self.assertEqual(["repo:healthy"], [s["source_id"] for s in report["source_revisions"]])
            self.assertEqual(
                {"repo:missing", "vault:gewebe"},
                {s["source_id"] for s in report["unavailable_sources"]},
            )
            self.assertEqual(
                ["repo:healthy"],
                json.loads(discovery.SOURCE_STATE.read_text())["observed_source_ids"],
            )

    def test_previously_seen_empty_repository_still_blocks_promotion(self) -> None:
        previous = json.loads(discovery.SOURCE_STATE.read_text())
        previous["source_revisions"] = [{
            "source_id": "repo:missing", "root": str(self.missing),
            "ref": "HEAD", "revision": "a" * 40,
        }]
        discovery.SOURCE_STATE.write_text(json.dumps(previous))
        for _ in range(3):
            report = self._scan()
            self.assertFalse(report["promotion_allowed"])
            self.assertEqual(["repo:missing"], [e["source_id"] for e in report["scanner_errors"]])
            self.assertIn(
                "repo:missing",
                json.loads(discovery.SOURCE_STATE.read_text())["observed_source_ids"],
            )

    def test_previously_seen_missing_repository_still_blocks_promotion(self) -> None:
        self._write_previous({
            "repo:missing:plan.md": {
                "source_id": "repo:missing", "source_path": "plan.md",
                "sha256": "a" * 64, "candidates": [],
            },
        })
        report = self._scan()
        self.assertFalse(report["promotion_allowed"])
        self.assertEqual("partial", report["result"])
        self.assertEqual(["repo:missing"], [e["source_id"] for e in report["scanner_errors"]])
        self.assertIn(
            "repo:missing:plan.md",
            json.loads(discovery.SOURCE_STATE.read_text())["documents"],
        )

    def test_previously_seen_missing_vault_still_blocks_promotion(self) -> None:
        self._write_previous({
            "vault:gewebe:project/plan.md": {
                "source_id": "vault:gewebe", "source_path": "project/plan.md",
                "sha256": "b" * 64, "candidates": [],
            },
        })
        report = self._scan()
        self.assertFalse(report["promotion_allowed"])
        self.assertEqual(["vault:gewebe"], [e["source_id"] for e in report["scanner_errors"]])

    def test_successfully_observed_empty_source_remains_required_after_disappearance(self) -> None:
        registry = json.loads(discovery.REGISTRY.read_text())
        registry["repositories"][1]["planning_files"] = []
        discovery.REGISTRY.write_text(json.dumps(registry))
        self._make_repo(self.missing)
        initial = self._scan()
        self.assertTrue(initial["promotion_allowed"])
        self.assertIn("repo:missing", {
            item["source_id"] for item in initial["source_revisions"]
        })
        # No documents were ever stored for this source.
        self.assertFalse(any(
            item.get("source_id") == "repo:missing"
            for item in json.loads(discovery.SOURCE_STATE.read_text())["documents"].values()
        ))
        shutil.rmtree(self.missing)
        for _ in range(3):
            report = self._scan()
            self.assertFalse(report["promotion_allowed"])
            self.assertEqual(["repo:missing"], [e["source_id"] for e in report["scanner_errors"]])
            saved = json.loads(discovery.SOURCE_STATE.read_text())
            self.assertNotIn("repo:missing", {
                item["source_id"] for item in saved["source_revisions"]
            })
            self.assertIn("repo:missing", saved["observed_source_ids"])

    def test_previously_observed_vault_without_documents_stays_required(self) -> None:
        previous = json.loads(discovery.SOURCE_STATE.read_text())
        previous["source_revisions"] = [{
            "source_id": "vault:gewebe", "root": str(self.vault),
            "ref": "HEAD", "revision": "b" * 40,
        }]
        discovery.SOURCE_STATE.write_text(json.dumps(previous))
        for _ in range(3):
            report = self._scan()
            self.assertFalse(report["promotion_allowed"])
            self.assertEqual(["vault:gewebe"], [e["source_id"] for e in report["scanner_errors"]])
            self.assertIn(
                "vault:gewebe",
                json.loads(discovery.SOURCE_STATE.read_text())["observed_source_ids"],
            )

    def test_invalid_persisted_observation_ids_fail_before_promotion(self) -> None:
        previous = json.loads(discovery.SOURCE_STATE.read_text())
        previous["observed_source_ids"] = ["repo:healthy", None]
        discovery.SOURCE_STATE.write_text(json.dumps(previous))
        with self.assertRaisesRegex(RuntimeError, "invalid observed source ids"):
            self._scan()

    def test_existing_broken_checkout_not_mistaken_for_optional_absence(self) -> None:
        self.missing.mkdir()
        report = self._scan()
        self.assertFalse(report["promotion_allowed"])
        self.assertEqual(["repo:missing"], [e["source_id"] for e in report["scanner_errors"]])

    def test_source_reappearing_is_scanned_on_next_run(self) -> None:
        first = self._scan()
        self.assertTrue(first["promotion_allowed"])
        self._make_repo(self.missing)
        second = self._scan()
        self.assertTrue(second["promotion_allowed"])
        self.assertEqual({"repo:healthy", "repo:missing"},
                         {s["source_id"] for s in second["source_revisions"]})
        self.assertEqual(["vault:gewebe"], [s["source_id"] for s in second["unavailable_sources"]])

    def test_absent_only_sources_cannot_claim_complete_discovery(self) -> None:
        registry = json.loads(discovery.REGISTRY.read_text())
        registry["repositories"] = registry["repositories"][1:]
        discovery.REGISTRY.write_text(json.dumps(registry))
        report = self._scan()
        self.assertFalse(report["promotion_allowed"])
        self.assertEqual("partial", report["result"])
        self.assertEqual([], report["source_revisions"])
        self.assertEqual("source-registry", report["scanner_errors"][0]["source_id"])

    def test_runtime_mirrors_stay_byte_identical(self) -> None:
        source_root = Path(__file__).resolve().parents[1] / "src"
        self.assertEqual(
            (source_root / "bureau_cycle/discovery.py").read_bytes(),
            (source_root / "bureau/discovery.py").read_bytes(),
        )


if __name__ == "__main__":
    unittest.main()