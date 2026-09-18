import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(path: str) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


class CommonThingResourceBootstrapTests(unittest.TestCase):
    def test_commonthing_is_the_active_repository_identity(self) -> None:
        commonthing = load("registry/resources/commonthing.json")
        self.assertEqual(commonthing["id"], "repo.commonthing")
        self.assertEqual(commonthing["type"], "git-repository")
        self.assertEqual(commonthing["path"], "/home/alex/repos/commonthing")
        self.assertEqual(commonthing["github_slug"], "heimgewebe/commonthing")
        self.assertEqual(commonthing["grabowski_key"], "repo:/home/alex/repos/commonthing")

        weltgewebe = load("registry/resources/weltgewebe.json")
        self.assertEqual(weltgewebe["id"], "repo.weltgewebe")
        self.assertEqual(weltgewebe["type"], "external")
        self.assertNotIn("path", weltgewebe)
        self.assertNotIn("github_slug", weltgewebe)
        self.assertNotIn("grabowski_key", weltgewebe)
        self.assertEqual(
            weltgewebe["metadata"]["canonical_successor"], "repo.commonthing"
        )
        self.assertEqual(
            weltgewebe["metadata"]["historical_path"], "/home/alex/repos/weltgewebe"
        )
        self.assertEqual(
            weltgewebe["metadata"]["historical_github_slug"], "heimgewebe/weltgewebe"
        )
        self.assertTrue(weltgewebe["metadata"]["retired_at"])

    def test_historical_components_remain_beneath_retired_weltgewebe(self) -> None:
        for path in (
            "registry/resources/weltgewebe-map-motion-t064.json",
            "registry/resources/weltgewebe-node-panel-t006.json",
            "registry/resources/weltgewebe-report-truth-t052.json",
        ):
            self.assertEqual(load(path)["parent"], "repo.weltgewebe")

    def test_only_commonthing_exposes_current_product_git_coordinates(self) -> None:
        current_coordinates = []
        for path in sorted((ROOT / "registry/resources").glob("*.json")):
            resource = load(str(path.relative_to(ROOT)))
            if resource.get("path") in {
                "/home/alex/repos/commonthing",
                "/home/alex/repos/weltgewebe",
            } or resource.get("github_slug") in {
                "heimgewebe/commonthing",
                "heimgewebe/weltgewebe",
            }:
                current_coordinates.append(resource["id"])
        self.assertEqual(current_coordinates, ["repo.commonthing"])


if __name__ == "__main__":
    unittest.main()
