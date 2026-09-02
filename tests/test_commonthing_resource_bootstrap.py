import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(path: str) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


class CommonThingResourceBootstrapTests(unittest.TestCase):
    def test_commonthing_and_weltgewebe_are_both_active_during_bootstrap(self) -> None:
        commonthing = load("registry/resources/commonthing.json")
        self.assertEqual(commonthing["id"], "repo.commonthing")
        self.assertEqual(commonthing["type"], "git-repository")
        self.assertEqual(commonthing["path"], "/home/alex/repos/commonthing")
        self.assertEqual(commonthing["github_slug"], "heimgewebe/commonthing")
        self.assertEqual(commonthing["grabowski_key"], "repo:/home/alex/repos/commonthing")

        weltgewebe = load("registry/resources/weltgewebe.json")
        self.assertEqual(weltgewebe["id"], "repo.weltgewebe")
        self.assertEqual(weltgewebe["type"], "git-repository")
        self.assertEqual(weltgewebe["path"], "/home/alex/repos/weltgewebe")
        self.assertEqual(weltgewebe["github_slug"], "heimgewebe/weltgewebe")

    def test_components_remain_on_weltgewebe_until_task_specs_are_migrated(self) -> None:
        for path in (
            "registry/resources/weltgewebe-map-motion-t064.json",
            "registry/resources/weltgewebe-node-panel-t006.json",
            "registry/resources/weltgewebe-report-truth-t052.json",
        ):
            self.assertEqual(load(path)["parent"], "repo.weltgewebe")

    def test_registry_tasks_do_not_claim_commonthing_before_state_store_cutover(self) -> None:
        claiming_tasks = []
        for path in sorted((ROOT / "registry/tasks").glob("*.json")):
            if '"resource": "repo.commonthing"' in path.read_text(encoding="utf-8"):
                claiming_tasks.append(path.name)
        self.assertEqual(claiming_tasks, [])


if __name__ == "__main__":
    unittest.main()
