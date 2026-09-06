import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(path: str) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


class LaborResourceBootstrapTests(unittest.TestCase):
    def test_labor_is_active_and_vibe_lab_is_retained_after_rebind(self) -> None:
        labor = load("registry/resources/labor.json")
        self.assertEqual(labor["id"], "repo.labor")
        self.assertEqual(labor["type"], "git-repository")
        self.assertEqual(labor["path"], "/home/alex/repos/labor")
        self.assertEqual(labor["github_slug"], "heimgewebe/labor")
        self.assertEqual(labor["grabowski_key"], "repo:/home/alex/repos/labor")
        self.assertNotEqual(labor.get("metadata", {}).get("lifecycle"), "retired")

        vibe_lab = load("registry/resources/vibe-lab.json")
        self.assertEqual(vibe_lab["id"], "repo.vibe-lab")
        self.assertEqual(vibe_lab["type"], "git-repository")
        self.assertEqual(vibe_lab["path"], "/home/alex/repos/vibe-lab")
        self.assertEqual(vibe_lab["github_slug"], "heimgewebe/vibe-lab")
        self.assertEqual(vibe_lab["grabowski_key"], "repo:/home/alex/repos/vibe-lab")
        self.assertEqual(vibe_lab["metadata"]["lifecycle"], "retired")
        self.assertTrue(vibe_lab["metadata"]["coordination_only"])

    def test_rebound_current_registry_tasks_claim_labor(self) -> None:
        migrated_tasks = {
            "CCM-V1-T005.json",
            "HEIMGEWEBE-RESILIENZ-V1-T014.json",
            "OPERATOR-ECOSYSTEM-REDUNDANCY-V1-T005.json",
            "OPERATOR-INTEGRATION-LOOP-V1-T007.json",
            "OPERATOR-ML-READINESS-V1-T002.json",
            "OPERATOR-ML-READINESS-V1-T004.json",
        }
        for name in sorted(migrated_tasks):
            task = load(f"registry/tasks/{name}")
            claimed_resources = {
                claim.get("resource")
                for claim in task.get("claims", [])
                if isinstance(claim, dict)
            }
            self.assertIn("repo.labor", claimed_resources, name)
            self.assertNotIn("repo.vibe-lab", claimed_resources, name)


if __name__ == "__main__":
    unittest.main()
