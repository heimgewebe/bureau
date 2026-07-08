from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bureau.gemini_preflight import gemini_preflight, main


class GeminiPreflightTests(unittest.TestCase):
    def test_missing_binary_blocks_without_lane_activation(self) -> None:
        with mock.patch("bureau.gemini_preflight.shutil.which", return_value=None):
            result = gemini_preflight(command="missing-gemini")
        self.assertEqual(result["status"], "blocked_unavailable")
        self.assertFalse(result["laneEnabled"])
        self.assertFalse(result["effectFlags"]["laneActivationAllowed"])
        self.assertFalse(result["effectFlags"]["writeAllowed"])

    def test_observed_binary_still_blocks_until_auth_quota_review(self) -> None:
        def runner(argv: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
            output = "gemini 1.0" if argv[-1] == "--version" else "--print\n--sandbox\n"
            return subprocess.CompletedProcess(argv, 0, output, "")

        with mock.patch("bureau.gemini_preflight.shutil.which", return_value="/usr/bin/gemini"):
            result = gemini_preflight(runner=runner)
        self.assertEqual(result["status"], "blocked_pending_auth_quota_review")
        self.assertTrue(result["capabilities"]["versionObserved"])
        self.assertTrue(result["capabilities"]["noninteractiveModeObserved"])
        self.assertTrue(result["capabilities"]["sandboxFlagObserved"])
        self.assertFalse(result["capabilities"]["authQuotaObserved"])
        self.assertFalse(result["laneEnabled"])

    def test_help_without_print_or_sandbox_reports_incomplete_preflight(self) -> None:
        def runner(argv: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
            output = "gemini 1.0" if argv[-1] == "--version" else "usage only\n"
            return subprocess.CompletedProcess(argv, 0, output, "")

        with mock.patch("bureau.gemini_preflight.shutil.which", return_value="/usr/bin/gemini"):
            result = gemini_preflight(runner=runner)
        self.assertEqual(result["status"], "blocked_preflight_incomplete")
        self.assertIn("noninteractive_mode_not_observed", result["blockedReasons"])
        self.assertIn("sandbox_flag_not_observed", result["blockedReasons"])


    def test_model_and_generation_probe_make_preflight_ready_for_lane_design(self) -> None:
        def runner(argv: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
            if argv[-1] == "--version":
                output = "gemini 1.0"
            elif argv[-1] == "--help":
                output = "--print\n--sandbox\n"
            elif argv[-1] == "models":
                output = "Gemini test model\n"
            else:
                output = "GEMINI_PREFLIGHT_OK\n"
            return subprocess.CompletedProcess(argv, 0, output, "")

        with mock.patch("bureau.gemini_preflight.shutil.which", return_value="/usr/bin/gemini"):
            result = gemini_preflight(
                runner=runner,
                observe_models=True,
                active_generation_probe=True,
            )
        self.assertEqual(result["status"], "ready_for_proposal_lane_design")
        self.assertTrue(result["capabilities"]["modelAccessObserved"])
        self.assertTrue(result["capabilities"]["activeGenerationProbeObserved"])
        self.assertTrue(result["capabilities"]["authQuotaObserved"])
        self.assertFalse(result["laneEnabled"])
        self.assertFalse(result["effectFlags"]["laneActivationAllowed"])
        self.assertEqual(result["blockedReasons"], [])

    def test_active_probe_must_return_expected_marker(self) -> None:
        def runner(argv: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
            if argv[-1] == "--version":
                output = "gemini 1.0"
            elif argv[-1] == "--help":
                output = "--print\n--sandbox\n"
            elif argv[-1] == "models":
                output = "Gemini test model\n"
            else:
                output = "unexpected text\n"
            return subprocess.CompletedProcess(argv, 0, output, "")

        with mock.patch("bureau.gemini_preflight.shutil.which", return_value="/usr/bin/gemini"):
            result = gemini_preflight(
                runner=runner,
                observe_models=True,
                active_generation_probe=True,
            )
        self.assertEqual(result["status"], "blocked_pending_auth_quota_review")
        self.assertFalse(result["capabilities"]["activeGenerationProbeObserved"])
        self.assertFalse(result["capabilities"]["authQuotaObserved"])

    def test_cli_can_write_report_without_enabling_lane(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "gemini-preflight.json"
            with mock.patch("bureau.gemini_preflight.shutil.which", return_value=None):
                stream = io.StringIO()
                with contextlib.redirect_stdout(stream):
                    rc = main(["--command", "missing-gemini", "--output", str(output), "--json"])
            payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(rc, 0)
        self.assertEqual(payload["kind"], "gemini_proposal_lane_preflight")
        self.assertFalse(payload["laneEnabled"])
        self.assertFalse(payload["effectFlags"]["dispatchAllowed"])


if __name__ == "__main__":
    unittest.main()
