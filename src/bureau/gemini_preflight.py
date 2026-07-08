from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

KIND = "gemini_proposal_lane_preflight"
VERSION = 1
DOES_NOT_ESTABLISH = [
    "gemini_schedulable_capacity",
    "gemini_write_authority",
    "agent_dispatch",
    "merge_readiness",
    "runtime_correctness",
    "claim_truth",
]
SENSITIVE_EXCLUSIONS = [
    "credentials",
    "tokens",
    "keys",
    ".env contents",
    "private runtime data",
    "deploy-only material",
    "unreviewed private context",
]
ALLOWED_INPUTS = [
    "explicitly selected public repo diffs",
    "bounded non-secret task briefs",
    "schema-valid Frontier candidates",
    "sanitized review prompts",
]
FORBIDDEN_AUTHORITY = [
    "write files",
    "push branches",
    "merge PRs",
    "mutate runtime",
    "read secrets",
    "dispatch agents",
    "modify Bureau queue",
]

RunCommand = Callable[[list[str], int], subprocess.CompletedProcess[str]]


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _run(argv: list[str], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout_seconds,
    )


def _bounded_output(value: str, limit: int = 4000) -> dict[str, Any]:
    return {
        "sha256": _sha(value),
        "prefix": value[:limit],
        "truncated": len(value) > limit,
    }


def _observe_command(
    executable: str,
    arguments: list[str],
    *,
    timeout_seconds: int,
    runner: RunCommand,
) -> dict[str, Any]:
    argv = [executable, *arguments]
    try:
        result = runner(argv, timeout_seconds)
    except subprocess.TimeoutExpired:
        return {
            "argv": [Path(executable).name, *arguments],
            "ok": False,
            "returncode": None,
            "timedOut": True,
            "output": {"sha256": None, "prefix": "", "truncated": False},
        }
    output = result.stdout or ""
    return {
        "argv": [Path(executable).name, *arguments],
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "timedOut": False,
        "output": _bounded_output(output),
    }


def gemini_preflight(
    *,
    command: str = "gemini",
    timeout_seconds: int = 10,
    observe_models: bool = False,
    active_generation_probe: bool = False,
    runner: RunCommand = _run,
) -> dict[str, Any]:
    executable = shutil.which(command)
    if executable is None:
        return {
            "schemaVersion": VERSION,
            "kind": KIND,
            "status": "blocked_unavailable",
            "laneEnabled": False,
            "binary": {"command": command, "found": False},
            "observations": [],
            "capabilities": {
                "versionObserved": False,
                "noninteractiveModeObserved": False,
                "sandboxFlagObserved": False,
                "modelAccessObserved": False,
                "activeGenerationProbeObserved": False,
                "authQuotaObserved": False,
            },
            "contextBoundary": _context_boundary(),
            "effectFlags": _effect_flags(),
            "doesNotEstablish": DOES_NOT_ESTABLISH,
            "blockedReason": "gemini_executable_missing",
        }

    version = _observe_command(
        executable,
        ["--version"],
        timeout_seconds=timeout_seconds,
        runner=runner,
    )
    help_result = _observe_command(
        executable,
        ["--help"],
        timeout_seconds=timeout_seconds,
        runner=runner,
    )
    help_output = help_result.get("output")
    help_text = help_output["prefix"] if isinstance(help_output, dict) else ""
    has_print = "--print" in help_text or "--prompt" in help_text
    has_sandbox = "--sandbox" in help_text
    version_observed = bool(version["ok"])
    blocked: list[str] = []
    if not version_observed:
        blocked.append("version_not_observed")
    if not has_print:
        blocked.append("noninteractive_mode_not_observed")
    if not has_sandbox:
        blocked.append("sandbox_flag_not_observed")
    observations = [version, help_result]
    model_access_observed = False
    generation_observed = False
    if observe_models:
        model_probe = _observe_command(
            executable,
            ["models"],
            timeout_seconds=timeout_seconds,
            runner=runner,
        )
        observations.append(model_probe)
        model_access_observed = bool(model_probe["ok"])
    if active_generation_probe:
        generation_probe = _observe_command(
            executable,
            [
                "--sandbox",
                "--print",
                "Return exactly GEMINI_PREFLIGHT_OK and nothing else.",
            ],
            timeout_seconds=max(timeout_seconds, 45),
            runner=runner,
        )
        observations.append(generation_probe)
        generation_output = generation_probe["output"]
        output_prefix = (
            generation_output.get("prefix", "")
            if isinstance(generation_output, dict)
            else ""
        )
        generation_observed = bool(
            generation_probe["ok"] and "GEMINI_PREFLIGHT_OK" in output_prefix
        )
    if not model_access_observed or not generation_observed:
        blocked.append("auth_quota_not_observed")
    status = (
        "ready_for_proposal_lane_design"
        if not blocked
        else (
            "blocked_pending_auth_quota_review"
            if blocked == ["auth_quota_not_observed"]
            else "blocked_preflight_incomplete"
        )
    )
    return {
        "schemaVersion": VERSION,
        "kind": KIND,
        "status": status,
        "laneEnabled": False,
        "binary": {
            "command": command,
            "found": True,
            "path": executable,
            "pathSha256": _sha(executable),
        },
        "observations": observations,
        "capabilities": {
            "versionObserved": version_observed,
            "noninteractiveModeObserved": has_print,
            "noninteractiveMode": "--print" if has_print else None,
            "outputCapturePath": "stdout_json_or_bounded_stdout",
            "sandboxFlagObserved": has_sandbox,
            "sandboxFlag": "--sandbox" if has_sandbox else None,
            "modelAccessObserved": model_access_observed,
            "activeGenerationProbeObserved": generation_observed,
            "authQuotaObserved": model_access_observed and generation_observed,
        },
        "contextBoundary": _context_boundary(),
        "effectFlags": _effect_flags(),
        "doesNotEstablish": DOES_NOT_ESTABLISH,
        "blockedReasons": blocked,
        "nextAction": (
            "implement_proposal_only_review_lane_with_laneEnabled_false_receipts"
            if not blocked
            else "record_auth_quota_and_policy_review_before_enabling_gemini_lane"
        ),
    }


def _context_boundary() -> dict[str, Any]:
    return {
        "allowedInputs": ALLOWED_INPUTS,
        "sensitiveExclusions": SENSITIVE_EXCLUSIONS,
        "forbiddenAuthority": FORBIDDEN_AUTHORITY,
        "noRepositoryWriteAuthority": True,
        "noCredentialContext": True,
    }


def _effect_flags() -> dict[str, bool]:
    return {
        "writeAllowed": False,
        "pushAllowed": False,
        "mergeAllowed": False,
        "runtimeMutationAllowed": False,
        "credentialAccessAllowed": False,
        "dispatchAllowed": False,
        "queueMutationAllowed": False,
        "laneActivationAllowed": False,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="bureau-gemini-preflight")
    result.add_argument("--command", default="gemini")
    result.add_argument("--timeout-seconds", type=int, default=10)
    result.add_argument("--observe-models", action="store_true")
    result.add_argument("--active-generation-probe", action="store_true")
    result.add_argument("--output", type=Path)
    result.add_argument("--json", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    value = gemini_preflight(
        command=args.command,
        timeout_seconds=args.timeout_seconds,
        observe_models=args.observe_models,
        active_generation_probe=args.active_generation_probe,
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        rendered = json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        args.output.write_text(rendered)
    if args.json or not args.output:
        print(
            json.dumps(
                value,
                indent=2 if args.json else None,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    ok_statuses = {"ready", "ready_for_proposal_lane_design"}
    return 0 if value["status"].startswith("blocked") or value["status"] in ok_statuses else 1


if __name__ == "__main__":
    raise SystemExit(main())
