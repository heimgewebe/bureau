from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bureau.gemini_review_lane import main, review_with_gemini


class GeminiReviewLaneTests(unittest.TestCase):
    def test_valid_json_review_is_proposal_only(self) -> None:
        def runner(argv: list[str], timeout: int, cwd: Path) -> subprocess.CompletedProcess[str]:
            self.assertEqual(argv[1:3], ["--sandbox", "--print"])
            self.assertNotIn(str(Path.cwd()), str(cwd))
            payload = {
                "schemaVersion": 1,
                "status": "proposal",
                "summary": "one finding",
                "findings": [
                    {
                        "severity": "p3",
                        "path": "a.py",
                        "line": 1,
                        "issue": "x",
                        "suggestion": "y",
                    }
                ],
                "effectAllowed": False,
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

        with tempfile.TemporaryDirectory() as directory:
            diff = Path(directory) / "diff.patch"
            diff.write_text("diff --git a/a.py b/a.py\n+print('x')\n", encoding="utf-8")
            with mock.patch(
                "bureau.gemini_review_lane.shutil.which",
                return_value="/usr/bin/gemini",
            ):
                receipt = review_with_gemini(diff, runner=runner)
        self.assertEqual(receipt["kind"], "gemini_proposal_review_lane_receipt")
        self.assertEqual(receipt["status"], "proposal")
        self.assertEqual(receipt["mode"], "proposal_only")
        self.assertFalse(receipt["writeAllowed"])
        self.assertFalse(receipt["pushAllowed"])
        self.assertFalse(receipt["mergeAllowed"])
        self.assertFalse(receipt["runtimeMutationAllowed"])
        self.assertFalse(receipt["credentialAccessAllowed"])
        self.assertFalse(receipt["laneActivationAllowed"])
        self.assertFalse(receipt["effectPerformed"])

    def test_non_json_output_blocks_instead_of_creating_effect(self) -> None:
        def runner(argv: list[str], timeout: int, cwd: Path) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 0, "not json", "")

        with tempfile.TemporaryDirectory() as directory:
            diff = Path(directory) / "diff.patch"
            diff.write_text("diff --git a/a.py b/a.py\n", encoding="utf-8")
            with mock.patch(
                "bureau.gemini_review_lane.shutil.which",
                return_value="/usr/bin/gemini",
            ):
                receipt = review_with_gemini(diff, runner=runner)
        self.assertEqual(receipt["status"], "blocked")
        self.assertIn("non_json_output", receipt["blockedReason"])
        self.assertFalse(receipt["effectPerformed"])

    def test_rejects_sensitive_input_before_runner(self) -> None:
        def runner(argv: list[str], timeout: int, cwd: Path) -> subprocess.CompletedProcess[str]:
            raise AssertionError("runner must not be called")

        with tempfile.TemporaryDirectory() as directory:
            diff = Path(directory) / "diff.patch"
            diff.write_text("+TOKEN=example\n", encoding="utf-8")
            with (
                mock.patch(
                    "bureau.gemini_review_lane.shutil.which",
                    return_value="/usr/bin/gemini",
                ),
                self.assertRaisesRegex(ValueError, "secret pattern"),
            ):
                review_with_gemini(diff, runner=runner)

    def test_cli_writes_blocked_receipt_for_missing_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            diff = Path(directory) / "diff.patch"
            output = Path(directory) / "receipt.json"
            diff.write_text("diff --git a/a.py b/a.py\n", encoding="utf-8")
            with mock.patch("bureau.gemini_review_lane.shutil.which", return_value=None):
                stream = io.StringIO()
                with contextlib.redirect_stdout(stream):
                    rc = main([
                        "--diff-file",
                        str(diff),
                        "--command",
                        "missing-gemini",
                        "--output",
                        str(output),
                        "--json",
                    ])
            receipt = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(rc, 0)
        self.assertEqual(receipt["status"], "blocked")
        self.assertEqual(receipt["blockedReason"], "gemini_executable_missing")
        self.assertFalse(receipt["dispatchAllowed"])


if __name__ == "__main__":
    unittest.main()
