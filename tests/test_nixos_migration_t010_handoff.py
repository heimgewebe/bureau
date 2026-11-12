from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "registry" / "tasks"


def load_task(suffix: str) -> dict:
    return json.loads((TASKS / f"HEIM-PC-NIXOS-MIGRATION-V1-{suffix}.json").read_text(encoding="utf-8"))


class T010HandoffContractTests(unittest.TestCase):
    def test_current_t010_planned_contract_does_not_delegate_activation_to_t011(self) -> None:
        t010 = load_task("T010")
        handoff = next(item for item in t010["acceptance"] if item["id"] == "t011-and-t003-published-source-handoff")
        self.assertIn("final Build/closure-identity source", t010["goal"])
        self.assertNotIn("activation-reproof source", t010["goal"])
        self.assertIn("Build-only receipt", handoff["assertion"])
        self.assertIn("target-bound Activation receipt", handoff["assertion"])
        self.assertEqual(t010["metadata"]["final_closure_build_task"], "HEIM-PC-NIXOS-MIGRATION-V1-T011")
        self.assertEqual(t010["metadata"]["final_activation_receipt_owner"], "HEIM-PC-NIXOS-MIGRATION-V1-T003")
        self.assertNotIn("final_activation_reproof_task", t010["metadata"])
        self.assertEqual(t010["depends_on"], ["HEIM-PC-NIXOS-MIGRATION-V1-T009"])

    def test_build_and_activation_have_distinct_owners_and_gates(self) -> None:
        t011, t003 = load_task("T011"), load_task("T003")
        self.assertIn("without activating it", t011["goal"])
        self.assertIn("does not authorize the Activation effect class", t011["execution"]["approval"]["note"])
        self.assertIn("target-bound Activation receipt", t003["goal"])
        self.assertIn("prior/recovery runtime", t003["goal"])
        self.assertEqual(t003["execution"]["approval"]["required_level"], "break_glass")
        self.assertIn(t011["id"], t003["depends_on"])
        self.assertNotIn(t003["id"], t011["depends_on"])
        self.assertIn("component.heim-pc.nixos-managed-activation", [c["resource"] for c in t003["claims"]])


if __name__ == "__main__":
    unittest.main()
