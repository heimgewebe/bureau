import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TERMINAL_STATES = {"verified", "cancelled", "superseded"}
STATESTORE_TERMINALIZED_EXCEPTIONS = {
    'OPERATOR-ECOSYSTEM-REDUNDANCY-V1-T084',
    'OPERATOR-ECOSYSTEM-REDUNDANCY-V1-T085',
    'WELTGEWEBE-OS-V1-T021',
    'WELTGEWEBE-OS-V1-T043',
    'WELTGEWEBE-OS-V1-T048',
    'WELTGEWEBE-OS-V1-T068',
    'WELTGEWEBE-OS-V1-T070',
    'WELTGEWEBE-OS-V1-T080',
    'WELTGEWEBE-PR-MAINTENANCE-V2-T005',
    'WELTGEWEBE-PR-MAINTENANCE-V2-T007',
    'WELTGEWEBE-PR-MAINTENANCE-V2-T008',
    'WELTGEWEBE-PR-MAINTENANCE-V2-T009',
}


def load(path: str) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


class CommonThingResourceCutoverTests(unittest.TestCase):
    def test_commonthing_is_active_and_weltgewebe_is_historical(self) -> None:
        commonthing = load("registry/resources/commonthing.json")
        self.assertEqual(commonthing["id"], "repo.commonthing")
        self.assertEqual(commonthing["type"], "git-repository")
        self.assertEqual(commonthing["path"], "/home/alex/repos/commonthing")
        self.assertEqual(commonthing["github_slug"], "heimgewebe/commonthing")
        self.assertEqual(commonthing["grabowski_key"], "repo:/home/alex/repos/commonthing")

        historical = load("registry/resources/weltgewebe.json")
        self.assertEqual(historical["id"], "repo.weltgewebe")
        self.assertEqual(historical["type"], "external")
        self.assertEqual(historical["metadata"]["canonical_successor"], "repo.commonthing")
        self.assertEqual(historical["metadata"]["historical_path"], "/home/alex/repos/weltgewebe")

    def test_active_weltgewebe_components_use_commonthing_parent(self) -> None:
        for path in (
            "registry/resources/weltgewebe-map-motion-t064.json",
            "registry/resources/weltgewebe-node-panel-t006.json",
            "registry/resources/weltgewebe-report-truth-t052.json",
        ):
            self.assertEqual(load(path)["parent"], "repo.commonthing")

    def test_old_repo_claims_are_bounded_to_terminal_history(self) -> None:
        declared_nonterminal_old_claims = set()
        for path in sorted((ROOT / "registry/tasks").glob("*.json")):
            text = path.read_text(encoding="utf-8")
            if "repo.weltgewebe" not in text:
                continue
            task = json.loads(text)
            if task.get("state") not in TERMINAL_STATES:
                declared_nonterminal_old_claims.add(task.get("id") or task.get("task_id"))
        self.assertEqual(declared_nonterminal_old_claims, STATESTORE_TERMINALIZED_EXCEPTIONS)


if __name__ == "__main__":
    unittest.main()
