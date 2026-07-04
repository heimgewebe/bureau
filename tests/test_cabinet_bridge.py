from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from bureau.cabinet_bridge import CabinetBridgeError, bridge_probe


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_fixture(root: Path, claims: list[dict]) -> Path:
    registry = root / "registry/ecosystem"
    registry.mkdir(parents=True, exist_ok=True)
    (root / "docs/blueprints").mkdir(parents=True, exist_ok=True)
    (root / "docs/blueprints/cabinet-bureau-bridge-v0.md").write_text("# Bridge\n")
    write_json(root / "docs/blueprints/o.json", {"schema_version": 1})
    write_json(root / "registry/ecosystem/nodes.json", {"nodes": []})
    write_json(root / "registry/ecosystem/edges.json", {"edges": []})
    (root / "registry/ecosystem/claims.jsonl").write_text(
        "".join(json.dumps(claim, sort_keys=True) + "\n" for claim in claims),
        encoding="utf-8",
    )
    policy = {
        "schema_version": 1,
        "direction": "cabinet_to_bureau_read_only_candidate_signal",
        "source_owner": "repo:cabinet",
        "target_consumer": "repo:bureau",
        "canonical_doc": "docs/blueprints/cabinet-bureau-bridge-v0.md",
        "allowed_sources": [
            "registry/ecosystem/claims.jsonl",
            "registry/ecosystem/nodes.json",
            "registry/ecosystem/edges.json",
            "docs/blueprints/o.json",
        ],
        "admissible_candidate_statuses": ["evidenced", "approved"],
        "required_candidate_fields": [
            "id",
            "status",
            "evidence",
            "expires_at_or_refresh_hint",
            "next_action",
            "responsible_organ",
        ],
        "blocked_statuses": ["plausible", "expired", "unverified"],
        "prohibited_effects": ["task_write", "runtime_write"],
        "organ_roles": {"cabinet": "owner", "bureau": "reader"},
        "does_not_establish": ["task_approval", "claim_truth"],
    }
    policy_path = root / "registry/ecosystem/bureau-bridge.json"
    write_json(policy_path, policy)
    return policy_path


class CabinetBridgeProbeTests(unittest.TestCase):
    def test_probe_classifies_admissible_and_blocked_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            policy_path = write_fixture(
                Path(directory),
                [
                    {
                        "id": "claim:ready",
                        "status": "evidenced",
                        "evidence": ["docs/example.md"],
                        "expires_at": "2099-01-01",
                        "next_action": "preview_only",
                        "responsible_organ": "bureau",
                    },
                    {
                        "id": "claim:blocked",
                        "status": "plausible",
                        "evidence": [],
                        "expires_at": "2099-01-01",
                    },
                ],
            )
            report = bridge_probe(policy_path, today=date(2026, 7, 4))
            self.assertEqual(report["kind"], "cabinet_bureau_bridge_probe")
            self.assertEqual(report["mode"], "read_only")
            self.assertFalse(report["dispatchAllowed"])
            self.assertFalse(report["queueMutationAllowed"])
            self.assertFalse(report["taskCreationAllowed"])
            self.assertEqual(report["admissibleCount"], 1)
            by_id = {item["id"]: item for item in report["candidates"]}
            self.assertEqual(by_id["claim:ready"]["decision"], "admissible")
            self.assertEqual(by_id["claim:blocked"]["decision"], "blocked")
            self.assertIn("blocked_status:plausible", by_id["claim:blocked"]["reasons"])
            self.assertIn("claim_truth", report["doesNotEstablish"])

    def test_probe_blocks_expired_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            policy_path = write_fixture(
                Path(directory),
                [
                    {
                        "id": "claim:expired",
                        "status": "evidenced",
                        "evidence": ["docs/example.md"],
                        "expires_at": "2026-01-01",
                        "next_action": "preview_only",
                        "responsible_organ": "bureau",
                    }
                ],
            )
            report = bridge_probe(policy_path, today=date(2026, 7, 4))
            self.assertEqual(report["admissibleCount"], 0)
            self.assertIn("expired", report["candidates"][0]["reasons"])

    def test_cli_emits_probe_report_without_loading_bureau_registry(self) -> None:
        from bureau.cli import main

        with tempfile.TemporaryDirectory() as directory:
            policy_path = write_fixture(Path(directory), [])
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                result = main(["--json", "cabinet-bridge-probe", "--bridge-policy", str(policy_path)])
            self.assertEqual(result, 0)
            payload = json.loads(stream.getvalue())
            self.assertEqual(payload["kind"], "cabinet_bureau_bridge_probe")
            self.assertFalse(payload["dispatchAllowed"])
            self.assertEqual(payload["candidateCount"], 0)

    def test_missing_claim_source_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy_path = write_fixture(root, [])
            (root / "registry/ecosystem/claims.jsonl").unlink()
            with self.assertRaisesRegex(CabinetBridgeError, "missing"):
                bridge_probe(policy_path)


if __name__ == "__main__":
    unittest.main()
