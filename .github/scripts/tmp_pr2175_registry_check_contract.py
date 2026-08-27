from __future__ import annotations

from pathlib import Path

RUNTIME = Path("src/bureau/runtime_refresh.py")
TEST = Path("tests/test_runtime_refresh.py")

runtime_text = RUNTIME.read_text(encoding="utf-8")
old_constants = '''DEFAULT_REQUIRED_CHECKS = ("validate (3.10)", "validate (3.12)")
DEFAULT_AUTHORITY_ADOPTION_REQUIRED_CHECKS = (
    *DEFAULT_REQUIRED_CHECKS,
    "registry-registration-preflight/freshness",
)
'''
new_constants = '''DEFAULT_REQUIRED_CHECKS = (
    "validate (3.10)",
    "validate (3.12)",
    "registry-registration-preflight/freshness",
)
DEFAULT_AUTHORITY_ADOPTION_REQUIRED_CHECKS = DEFAULT_REQUIRED_CHECKS
'''
if runtime_text.count(old_constants) != 1:
    raise SystemExit("runtime required-check contract anchor drift")
RUNTIME.write_text(runtime_text.replace(old_constants, new_constants, 1), encoding="utf-8")

test_text = TEST.read_text(encoding="utf-8")
old_fixture = '''        "statusCheckRollup": [
            {"name": "validate (3.10)", "conclusion": "SUCCESS"},
            {"name": "validate (3.12)", "conclusion": "SUCCESS"},
        ],
    }


def github_fixture(
'''
new_fixture = '''        "statusCheckRollup": [
            {"name": "validate (3.10)", "conclusion": "SUCCESS"},
            {"name": "validate (3.12)", "conclusion": "SUCCESS"},
            {
                "name": "registry-registration-preflight/freshness",
                "conclusion": "SUCCESS",
            },
        ],
    }


def github_fixture(
'''
if test_text.count(old_fixture) != 1:
    raise SystemExit("green PR fixture anchor drift")
test_text = test_text.replace(old_fixture, new_fixture, 1)

old_summary = '''    assert set(result["check_summary"]) == {
        "validate (3.10)",
        "validate (3.12)",
    }
'''
new_summary = '''    assert set(result["check_summary"]) == set(refresh.DEFAULT_REQUIRED_CHECKS)
'''
if test_text.count(old_summary) != 1:
    raise SystemExit("check-summary expectation anchor drift")
test_text = test_text.replace(old_summary, new_summary, 1)

candidate_anchor = '''def _compare_command_error(*, status: int) -> refresh.RuntimeRefreshError:
'''
regression = '''def test_observe_requires_registry_freshness_by_default(tmp_path: Path) -> None:
    detail = green_pr_detail()
    detail["statusCheckRollup"] = [
        {"name": "validate (3.10)", "conclusion": "SUCCESS"},
        {"name": "validate (3.12)", "conclusion": "SUCCESS"},
    ]

    result, _ = candidate(tmp_path, detail=detail)

    assert result["status"] == "blocked"
    assert result["reason_codes"] == ["required-ci-not-green"]
    assert result["required_checks"] == list(refresh.DEFAULT_REQUIRED_CHECKS)
    assert (
        result["check_summary"]["registry-registration-preflight/freshness"]["state"]
        == "missing"
    )


'''
if test_text.count(candidate_anchor) != 1:
    raise SystemExit("candidate regression anchor drift")
if "test_observe_requires_registry_freshness_by_default" in test_text:
    raise SystemExit("registry freshness regression already present")
test_text = test_text.replace(candidate_anchor, regression + candidate_anchor, 1)
TEST.write_text(test_text, encoding="utf-8")
