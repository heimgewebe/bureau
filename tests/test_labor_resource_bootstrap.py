import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(path: str) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


class LaborResourceBootstrapTests(unittest.TestCase):
    def test_labor_and_vibe_lab_are_both_registered_during_bootstrap(self) -> None:
        labor = load("registry/resources/labor.json")
        self.assertEqual(labor["id"], "repo.labor")
        self.assertEqual(labor["type"], "git-repository")
        self.assertEqual(labor["path"], "/home/alex/repos/labor")
        self.assertEqual(labor["github_slug"], "heimgewebe/labor")
        self.assertEqual(labor["grabowski_key"], "repo:/home/alex/repos/labor")

        vibe_lab = load("registry/resources/vibe-lab.json")
        self.assertEqual(vibe_lab["id"], "repo.vibe-lab")
        self.assertEqual(vibe_lab["type"], "git-repository")
        self.assertEqual(vibe_lab["path"], "/home/alex/repos/vibe-lab")
        self.assertEqual(vibe_lab["github_slug"], "heimgewebe/vibe-lab")
        self.assertEqual(vibe_lab["grabowski_key"], "repo:/home/alex/repos/vibe-lab")

    def test_registry_tasks_do_not_claim_labor_before_state_store_rebind(self) -> None:
        claiming_tasks = []
        for path in sorted((ROOT / "registry/tasks").glob("*.json")):
            if '"resource": "repo.labor"' in path.read_text(encoding="utf-8"):
                claiming_tasks.append(path.name)
        self.assertEqual(claiming_tasks, [])


if __name__ == "__main__":
    unittest.main()
