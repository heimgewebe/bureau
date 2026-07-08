from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

KIND = "gemini_proposal_review_lane_receipt"
VERSION = 1
DEFAULT_MAX_BYTES = 120_000
SECRET_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"BEGIN [A-Z ]*PRIVATE KEY",
        r"GITHUB_TOKEN\s*=",
        r"DATABASE_URL\s*=",
        r"AWS_SECRET_ACCESS_KEY\s*=",
        r"PASSWORD\s*=",
        r"SECRET\s*=",
        r"TOKEN\s*=",
    ]
]
ALLOWED_STATUSES = {"proposal", "blocked", "no_action"}

RunCommand = Callable[[list[str], int, Path], subprocess.CompletedProcess[str]]


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _read_bounded_text(path: str | Path, *, max_bytes: int) -> tuple[str, dict[str, Any]]:
    source = Path(path).expanduser()
    if not source.is_file() or source.is_symlink():
        raise ValueError(f"input artifact must be a regular file: {source}")
    size = source.stat().st_size
    if size > max_bytes:
        raise ValueError(f"input artifact exceeds max bytes: {size} > {max_bytes}")
    text = source.read_text(encoding="utf-8")
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            raise ValueError(f"input artifact rejected by secret pattern: {pattern.pattern}")
    return text, {"path": str(source), "bytes": size, "sha256": _sha(text)}


def _run(argv: list[str], timeout_seconds: int, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=False,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout_seconds,
    )


def _prompt(diff_text: str, brief_text: str | None) -> str:
    schema = {
        "schemaVersion": 1,
        "status": "proposal | blocked | no_action",
        "summary": "short summary",
        "findings": [
            {
                "severity": "p1 | p2 | p3 | note",
                "path": "file path or null",
                "line": "line number or null",
                "issue": "finding",
                "suggestion": "proposal only",
            }
        ],
        "effectAllowed": False,
    }
    return "\n".join(
        [
            "You are Gemini in a proposal-only external review lane.",
            "Return only valid JSON. Do not use Markdown fences.",
            "Allowed statuses are proposal, blocked, no_action.",
            (
                "You may propose findings, but you must not claim to write, "
                "push, merge, mutate runtime, dispatch agents or access credentials."
            ),
            "Set effectAllowed to false.",
            "Output schema:",
            json.dumps(schema, ensure_ascii=False, sort_keys=True),
            "Task brief:",
            brief_text or "No extra brief provided.",
            "Diff to review:",
            diff_text,
        ]
    )


def _parse_review_output(text: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"non_json_output: {exc.msg}"
    if not isinstance(parsed, dict):
        return None, "output_not_object"
    if parsed.get("schemaVersion") != 1:
        return None, "schemaVersion_not_1"
    status = parsed.get("status")
    if status not in ALLOWED_STATUSES:
        return None, "status_not_allowed"
    if parsed.get("effectAllowed") is not False:
        return None, "effectAllowed_not_false"
    findings = parsed.get("findings")
    if findings is not None and not isinstance(findings, list):
        return None, "findings_not_list"
    return parsed, None


def review_with_gemini(
    diff_file: str | Path,
    *,
    brief_file: str | Path | None = None,
    command: str = "gemini",
    timeout_seconds: int = 120,
    max_bytes: int = DEFAULT_MAX_BYTES,
    runner: RunCommand = _run,
) -> dict[str, Any]:
    executable = shutil.which(command)
    if executable is None:
        return _blocked_receipt("gemini_executable_missing", command=command)
    diff_text, diff_artifact = _read_bounded_text(diff_file, max_bytes=max_bytes)
    brief_text = None
    brief_artifact = None
    if brief_file is not None:
        brief_text, brief_artifact = _read_bounded_text(brief_file, max_bytes=max_bytes)
    prompt = _prompt(diff_text, brief_text)
    argv = [executable, "--sandbox", "--print", prompt]
    try:
        with tempfile.TemporaryDirectory(prefix="bureau-gemini-review-") as directory:
            cwd = Path(directory)
            result = runner(argv, timeout_seconds, cwd)
    except subprocess.TimeoutExpired:
        return _blocked_receipt(
            "gemini_review_timeout",
            command=command,
            input_artifacts=[diff_artifact, brief_artifact] if brief_artifact else [diff_artifact],
            prompt_sha256=_sha(prompt),
        )
    output = result.stdout or ""
    parsed, parse_error = _parse_review_output(output)
    input_artifacts = [diff_artifact]
    if brief_artifact is not None:
        input_artifacts.append(brief_artifact)
    base = {
        "schemaVersion": VERSION,
        "kind": KIND,
        "mode": "proposal_only",
        "inputArtifacts": input_artifacts,
        "promptSha256": _sha(prompt),
        "command": [Path(executable).name, "--sandbox", "--print", "<prompt>"],
        "returncode": result.returncode,
        "outputSha256": _sha(output),
        "dispatchAllowed": False,
        "queueMutationAllowed": False,
        "writeAllowed": False,
        "pushAllowed": False,
        "mergeAllowed": False,
        "runtimeMutationAllowed": False,
        "credentialAccessAllowed": False,
        "laneActivationAllowed": False,
        "effectPerformed": False,
    }
    if result.returncode != 0:
        return {**base, "status": "blocked", "blockedReason": "gemini_returned_nonzero"}
    if parsed is None:
        return {**base, "status": "blocked", "blockedReason": parse_error}
    return {**base, "status": parsed["status"], "review": parsed}


def _blocked_receipt(
    reason: str,
    *,
    command: str,
    input_artifacts: list[dict[str, Any] | None] | None = None,
    prompt_sha256: str | None = None,
) -> dict[str, Any]:
    return {
        "schemaVersion": VERSION,
        "kind": KIND,
        "mode": "proposal_only",
        "status": "blocked",
        "blockedReason": reason,
        "command": [command, "--sandbox", "--print", "<prompt>"],
        "inputArtifacts": [item for item in (input_artifacts or []) if item is not None],
        "promptSha256": prompt_sha256,
        "dispatchAllowed": False,
        "queueMutationAllowed": False,
        "writeAllowed": False,
        "pushAllowed": False,
        "mergeAllowed": False,
        "runtimeMutationAllowed": False,
        "credentialAccessAllowed": False,
        "laneActivationAllowed": False,
        "effectPerformed": False,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="bureau-gemini-review-lane")
    result.add_argument("--diff-file", required=True)
    result.add_argument("--brief-file")
    result.add_argument("--command", default="gemini")
    result.add_argument("--timeout-seconds", type=int, default=120)
    result.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    result.add_argument("--output", type=Path)
    result.add_argument("--json", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        value = review_with_gemini(
            args.diff_file,
            brief_file=args.brief_file,
            command=args.command,
            timeout_seconds=args.timeout_seconds,
            max_bytes=args.max_bytes,
        )
    except ValueError as exc:
        value = _blocked_receipt(str(exc), command=args.command)
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
