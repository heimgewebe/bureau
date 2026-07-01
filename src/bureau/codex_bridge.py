from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .cycle_contract import atomic_json, utc_now

BRIDGE_SCHEMA_VERSION = 1
DECISION_SCHEMA_VERSION = 1
ALLOWED_ACTIONS = frozenset(
    {
        "idle",
        "block",
        "propose_binding",
        "propose_task_execution",
        "request_human_review",
    }
)
MUTATION_INTENT_KEYS = frozenset(
    {
        "mutates",
        "mutation",
        "mutations",
        "registry_mutation",
        "task_mutation",
        "bureau_mutation",
    }
)
MAX_CAPTURE_CHARS = 100_000

CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
CodexRunner = Callable[[Sequence[str], str, Path, int], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class BridgeConfig:
    repo_root: Path
    state_base: Path
    output_root: Path
    health_path: Path
    frontier_report_path: Path
    closure_plan_path: Path
    closure_lanes_path: Path
    backend: str = "none"
    fixture_decision_path: Path | None = None
    bureau_command: tuple[str, ...] = ()
    bureau_state_root: Path | None = None
    codex_command: tuple[str, ...] = ("codex", "exec")
    codex_timeout_seconds: int = 300
    run_id: str | None = None


def default_state_base() -> Path:
    return Path.home() / ".local/state"


def default_bureau_command() -> tuple[str, ...]:
    return (sys.executable, "-m", "bureau.cli")


def default_config(
    *,
    repo_root: Path | None = None,
    state_base: Path | None = None,
    output_root: Path | None = None,
    backend: str = "none",
    fixture_decision_path: Path | None = None,
    bureau_command: tuple[str, ...] | None = None,
    bureau_state_root: Path | None = None,
    codex_command: tuple[str, ...] | None = None,
    codex_timeout_seconds: int = 300,
    run_id: str | None = None,
) -> BridgeConfig:
    selected_state_base = (state_base or default_state_base()).expanduser()
    return BridgeConfig(
        repo_root=(repo_root or Path.cwd()).expanduser(),
        state_base=selected_state_base,
        output_root=(output_root or selected_state_base / "bureau-codex-bridge").expanduser(),
        health_path=selected_state_base / "bureau-cycle/health.json",
        frontier_report_path=selected_state_base / "bureau-agent-frontier/latest-report.json",
        closure_plan_path=selected_state_base / "bureau-closure/plan.json",
        closure_lanes_path=selected_state_base / "bureau-closure/lanes.json",
        backend=backend,
        fixture_decision_path=fixture_decision_path.expanduser()
        if fixture_decision_path is not None
        else None,
        bureau_command=bureau_command or default_bureau_command(),
        bureau_state_root=bureau_state_root.expanduser() if bureau_state_root is not None else None,
        codex_command=codex_command or ("codex", "exec"),
        codex_timeout_seconds=codex_timeout_seconds,
        run_id=run_id,
    )


def generate_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"codex-bridge-{stamp}-{uuid.uuid4().hex[:12]}"


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _load_json_observation(name: str, path: Path) -> dict[str, Any]:
    observation: dict[str, Any] = {
        "name": name,
        "path": str(path),
        "available": False,
        "data": None,
        "error": None,
    }
    try:
        observation["data"] = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        observation["error"] = "missing"
    except json.JSONDecodeError as exc:
        observation["error"] = f"invalid_json: line {exc.lineno} column {exc.colno}"
    except OSError as exc:
        observation["error"] = f"read_failed: {exc}"
    else:
        observation["available"] = True
    return observation


def _subprocess_runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )


def _trim(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= MAX_CAPTURE_CHARS:
        return value
    return value[:MAX_CAPTURE_CHARS] + "\n...[truncated]"


def _bureau_args(config: BridgeConfig, action: str) -> list[str]:
    command = list(config.bureau_command or default_bureau_command())
    command.extend(["--root", str(config.repo_root), "--json"])
    if config.bureau_state_root is not None:
        command.extend(["--state-root", str(config.bureau_state_root)])
    command.append(action)
    return command


def _command_observation(
    name: str,
    semantic_command: str,
    command: Sequence[str],
    runner: CommandRunner,
) -> dict[str, Any]:
    try:
        process = runner(command)
    except Exception as exc:
        return {
            "name": name,
            "semantic_command": semantic_command,
            "command": list(command),
            "returncode": None,
            "ok": False,
            "stdout": "",
            "stderr": "",
            "stdout_json": None,
            "json_error": None,
            "error": f"{type(exc).__name__}: {exc}",
        }

    stdout = process.stdout or ""
    stderr = process.stderr or ""
    parsed: Any = None
    json_error: str | None = None
    if stdout.strip():
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError as exc:
            json_error = f"invalid_json: line {exc.lineno} column {exc.colno}"
    ok = process.returncode == 0 and json_error is None
    return {
        "name": name,
        "semantic_command": semantic_command,
        "command": list(command),
        "returncode": process.returncode,
        "ok": ok,
        "stdout": _trim(stdout),
        "stderr": _trim(stderr),
        "stdout_json": parsed,
        "json_error": json_error,
        "error": None,
    }


def _blocker(code: str, source: str, detail: str) -> dict[str, str]:
    return {"code": code, "source": source, "detail": detail}


def _context_blockers(sources: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    blockers: list[dict[str, str]] = []
    health = sources["health"]
    health_data = health.get("data") if health.get("available") else None
    if isinstance(health_data, dict):
        if health_data.get("critical") is True:
            blockers.append(_blocker("health_critical", "health", "critical=true"))
        if health_data.get("allow_next_dispatch") is False:
            blockers.append(
                _blocker(
                    "dispatch_not_allowed",
                    "health",
                    "allow_next_dispatch=false",
                )
            )

    if not sources["frontier"].get("available"):
        blockers.append(
            _blocker(
                "missing_frontier",
                "frontier",
                str(sources["frontier"].get("error") or "unavailable"),
            )
        )
    if not sources["closure_plan"].get("available"):
        blockers.append(
            _blocker(
                "missing_closure_plan",
                "closure_plan",
                str(sources["closure_plan"].get("error") or "unavailable"),
            )
        )
    if not sources["closure_lanes"].get("available"):
        blockers.append(
            _blocker(
                "missing_closure_lanes",
                "closure_lanes",
                str(sources["closure_lanes"].get("error") or "unavailable"),
            )
        )

    bureau_check = sources["bureau_check"]
    check_payload = bureau_check.get("stdout_json")
    check_valid = not (isinstance(check_payload, dict) and check_payload.get("valid") is False)
    if not bureau_check.get("ok") or not check_valid:
        detail = str(
            bureau_check.get("error") or bureau_check.get("stderr") or "bureau check failed"
        )
        blockers.append(_blocker("bureau_check_failed", "bureau_check", detail[:1000]))
    return blockers


def collect_context(
    config: BridgeConfig,
    *,
    run_id: str | None = None,
    runner: CommandRunner = _subprocess_runner,
) -> dict[str, Any]:
    selected_run_id = run_id or config.run_id or generate_run_id()
    sources = {
        "health": _load_json_observation("health", config.health_path),
        "frontier": _load_json_observation("frontier", config.frontier_report_path),
        "closure_plan": _load_json_observation("closure_plan", config.closure_plan_path),
        "closure_lanes": _load_json_observation("closure_lanes", config.closure_lanes_path),
        "bureau_status": _command_observation(
            "bureau_status",
            "bureau status",
            _bureau_args(config, "status"),
            runner,
        ),
        "bureau_check": _command_observation(
            "bureau_check",
            "bureau check",
            _bureau_args(config, "check"),
            runner,
        ),
    }
    context = {
        "schema_version": BRIDGE_SCHEMA_VERSION,
        "run_id": selected_run_id,
        "collected_at": utc_now(),
        "repo_root": str(config.repo_root),
        "state_base": str(config.state_base),
        "sources": sources,
        "blockers": _context_blockers(sources),
        "does_not_do": [
            "does not call OpenAI API",
            "does not allow Codex to mutate Bureau; Codex may only return "
            "decision JSON on stdout",
            "does not mutate Bureau registry or tasks",
            "does not claim, bind, complete, fail, merge, rebase, or deploy",
        ],
    }
    return context


def _truthy_mutation_value(value: Any) -> bool:
    if value in (False, None, "", "none", "false", "False"):
        return False
    return value not in ([], {})


def validate_decision(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return ["decision must be a JSON object"]
    errors: list[str] = []
    if value.get("schema_version") != DECISION_SCHEMA_VERSION:
        errors.append(
            f"schema_version must be {DECISION_SCHEMA_VERSION}, got {value.get('schema_version')!r}"
        )
    action = value.get("action")
    if action not in ALLOWED_ACTIONS:
        errors.append(f"action must be one of {sorted(ALLOWED_ACTIONS)}, got {action!r}")
    confidence = value.get("confidence")
    if (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not 0 <= confidence <= 1
    ):
        errors.append(f"confidence must be a number from 0 to 1, got {confidence!r}")
    for key in sorted(MUTATION_INTENT_KEYS.intersection(value)):
        if _truthy_mutation_value(value[key]):
            errors.append(f"{key} is not allowed in this read-only slice")
    return errors


def _load_fixture_decision(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {
            "backend": "fixture",
            "path": None,
            "decision": None,
            "valid": False,
            "errors": ["fixture decision path is required"],
        }
    try:
        decision = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {
            "backend": "fixture",
            "path": str(path),
            "decision": None,
            "valid": False,
            "errors": ["fixture decision file is missing"],
        }
    except json.JSONDecodeError as exc:
        return {
            "backend": "fixture",
            "path": str(path),
            "decision": None,
            "valid": False,
            "errors": [
                f"fixture decision has invalid JSON at line {exc.lineno} column {exc.colno}"
            ],
        }
    except OSError as exc:
        return {
            "backend": "fixture",
            "path": str(path),
            "decision": None,
            "valid": False,
            "errors": [f"fixture decision cannot be read: {exc}"],
        }
    errors = validate_decision(decision)
    return {
        "backend": "fixture",
        "path": str(path),
        "decision": decision if isinstance(decision, dict) else None,
        "valid": not errors,
        "errors": errors,
    }


def _extract_decision_from_text(value: str) -> tuple[Any | None, str | None]:
    stripped = value.strip()
    if not stripped:
        return None, "empty_stdout"
    candidates = [stripped]
    if "```" in stripped:
        parts = stripped.split("```")
        candidates.extend(part.removeprefix("json").strip() for part in parts)
    if "{" in stripped and "}" in stripped:
        candidates.append(stripped[stripped.find("{") : stripped.rfind("}") + 1])
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return json.loads(candidate), None
        except json.JSONDecodeError:
            continue
    return None, "stdout_did_not_contain_json_object"


def _read_decision_file(path: Path, *, backend: str) -> dict[str, Any]:
    try:
        decision = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {
            "backend": backend,
            "path": str(path),
            "decision": None,
            "valid": False,
            "errors": [f"{backend} decision file is missing"],
        }
    except json.JSONDecodeError as exc:
        return {
            "backend": backend,
            "path": str(path),
            "decision": None,
            "valid": False,
            "errors": [
                f"{backend} decision has invalid JSON at line {exc.lineno} "
                f"column {exc.colno}"
            ],
        }
    except OSError as exc:
        return {
            "backend": backend,
            "path": str(path),
            "decision": None,
            "valid": False,
            "errors": [f"{backend} decision cannot be read: {exc}"],
        }
    errors = validate_decision(decision)
    return {
        "backend": backend,
        "path": str(path),
        "decision": decision if isinstance(decision, dict) else None,
        "valid": not errors,
        "errors": errors,
    }


def _codex_process(
    command: Sequence[str],
    prompt: str,
    cwd: Path,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    existing_node_options = environment.get("NODE_OPTIONS", "")
    if "--jitless" not in existing_node_options.split():
        environment["NODE_OPTIONS"] = f"{existing_node_options} --jitless".strip()
    return subprocess.run(
        [*command, "-C", str(cwd), "-s", "read-only", "-"],
        cwd=cwd,
        input=prompt,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout_seconds,
        env=environment,
    )


def _run_codex_decision(
    config: BridgeConfig,
    *,
    run_dir: Path,
    prompt: str,
    codex_runner: CodexRunner,
) -> dict[str, Any]:
    decision_path = run_dir / "decision.json"
    try:
        process = codex_runner(
            config.codex_command,
            prompt,
            run_dir,
            config.codex_timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return {
            "backend": "codex",
            "path": str(decision_path),
            "command": list(config.codex_command),
            "decision": None,
            "valid": False,
            "errors": ["codex timed out"],
            "returncode": None,
            "stdout": "",
            "stderr": "",
        }
    except OSError as exc:
        return {
            "backend": "codex",
            "path": str(decision_path),
            "command": list(config.codex_command),
            "decision": None,
            "valid": False,
            "errors": [f"codex failed to start: {exc}"],
            "returncode": None,
            "stdout": "",
            "stderr": "",
        }

    stdout = process.stdout or ""
    decision, parse_error = _extract_decision_from_text(stdout)
    if parse_error is None:
        atomic_json(decision_path, decision)
        result = _read_decision_file(decision_path, backend="codex")
    else:
        result = _read_decision_file(decision_path, backend="codex")
        if not result.get("valid"):
            result["errors"] = [parse_error]
    errors = list(result.get("errors", []))
    if process.returncode != 0:
        errors.append(f"codex exited with {process.returncode}")
    return {
        **result,
        "valid": not errors,
        "errors": errors,
        "command": list(config.codex_command),
        "sandbox": "read-only",
        "returncode": process.returncode,
        "stdout": _trim(stdout),
        "stderr": _trim(process.stderr),
    }


def _backend_decision(
    config: BridgeConfig,
    *,
    run_dir: Path,
    prompt: str,
    codex_runner: CodexRunner,
) -> dict[str, Any]:
    if config.backend == "none":
        return {
            "backend": "none",
            "path": None,
            "decision": None,
            "valid": True,
            "errors": [],
        }
    if config.backend == "fixture":
        return _load_fixture_decision(config.fixture_decision_path)
    if config.backend == "codex":
        return _run_codex_decision(
            config,
            run_dir=run_dir,
            prompt=prompt,
            codex_runner=codex_runner,
        )
    return {
        "backend": config.backend,
        "path": None,
        "decision": None,
        "valid": False,
        "errors": [f"unsupported backend: {config.backend}"],
    }


def _decision_blockers(decision_result: dict[str, Any]) -> list[dict[str, str]]:
    if not decision_result.get("valid"):
        return [
            _blocker(
                "invalid_decision",
                "decision",
                "; ".join(str(error) for error in decision_result.get("errors", [])),
            )
        ]
    decision = decision_result.get("decision")
    if isinstance(decision, dict) and decision.get("action") == "block":
        return [
            _blocker(
                "decision_block",
                "decision",
                str(decision.get("rationale") or "fixture decision requested block"),
            )
        ]
    return []


def render_prompt(context: dict[str, Any], decision_result: dict[str, Any]) -> str:
    blockers = context.get("blockers", [])
    source_summary = {
        name: {
            "available": source.get("available", source.get("ok")),
            "path": source.get("path"),
            "semantic_command": source.get("semantic_command"),
            "error": source.get("error") or source.get("json_error"),
        }
        for name, source in context.get("sources", {}).items()
    }
    return (
        "# Bureau Codex Bridge Context\n\n"
        f"Run: `{context['run_id']}`\n\n"
        "If this prompt is executed by the local Codex backend, do not use tools, shell, "
        "apply_patch, or file writes. Return only one JSON object on stdout. The Bureau "
        "bridge will validate it and write decision.json itself.\n\n"
        "## Hard Constraints\n\n"
        "- Do not call the OpenAI API.\n"
        "- Do not use ChatGPT-planned execution.\n"
        "- Do not mutate Bureau registry, tasks, run reservations, claims, envelopes, "
        "worktrees, merges, rebases, deployments, or external executors.\n"
        "- Only return a JSON decision; the bridge records artifacts and receipts.\n\n"
        "## Current Blockers\n\n"
        f"```json\n{json.dumps(blockers, indent=2, ensure_ascii=False, sort_keys=True)}\n```\n\n"
        "## Source Summary\n\n"
        "```json\n"
        f"{json.dumps(source_summary, indent=2, ensure_ascii=False, sort_keys=True)}\n"
        "```\n\n"
        "## Decision Schema\n\n"
        "A backend may only return a JSON object with:\n\n"
        f"- `schema_version`: `{DECISION_SCHEMA_VERSION}`\n"
        f"- `action`: one of `{sorted(ALLOWED_ACTIONS)}`\n"
        "- `confidence`: number from `0` to `1`\n\n"
        "The current backend observation is:\n\n"
        "```json\n"
        f"{json.dumps(decision_result, indent=2, ensure_ascii=False, sort_keys=True)}\n"
        "```\n"
    )


def _result_for(blocked: bool, decision_result: dict[str, Any]) -> str:
    if blocked:
        return "blocked"
    decision = decision_result.get("decision")
    if not isinstance(decision, dict) or decision.get("action") == "idle":
        return "idle"
    return "completed"


def run_bridge(
    config: BridgeConfig,
    *,
    runner: CommandRunner = _subprocess_runner,
    codex_runner: CodexRunner = _codex_process,
) -> dict[str, Any]:
    selected_run_id = config.run_id or generate_run_id()
    run_dir = config.output_root / "runs" / selected_run_id
    started_at = utc_now()
    context = collect_context(config, run_id=selected_run_id, runner=runner)
    pending_decision = {
        "backend": config.backend,
        "path": str(run_dir / "decision.json") if config.backend == "codex" else None,
        "decision": None,
        "valid": True,
        "errors": [],
    }
    prompt = render_prompt(context, pending_decision)
    atomic_json(run_dir / "context.json", context)
    _atomic_text(run_dir / "prompt.md", prompt)
    decision_result = _backend_decision(
        config,
        run_dir=run_dir,
        prompt=prompt,
        codex_runner=codex_runner,
    )
    blockers = [*context["blockers"], *_decision_blockers(decision_result)]
    receipt_path = run_dir / "receipt.json"
    receipt = {
        "schema_version": BRIDGE_SCHEMA_VERSION,
        "run_id": selected_run_id,
        "started_at": started_at,
        "finished_at": utc_now(),
        "backend": config.backend,
        "result": _result_for(bool(blockers), decision_result),
        "blocked": bool(blockers),
        "blockers": blockers,
        "decision": decision_result.get("decision"),
        "decision_valid": decision_result.get("valid"),
        "decision_errors": decision_result.get("errors", []),
        "backend_observation": {
            key: value
            for key, value in decision_result.items()
            if key not in {"decision"}
        },
        "mutation_performed": False,
        "artifacts": {
            "context": str(run_dir / "context.json"),
            "prompt": str(run_dir / "prompt.md"),
            "decision": str(run_dir / "decision.json"),
            "receipt": str(receipt_path),
        },
        "does_not_do": context["does_not_do"],
    }
    atomic_json(receipt_path, receipt)
    return {"context": context, "prompt": prompt, "receipt": receipt, "run_dir": str(run_dir)}


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bureau-codex-bridge")
    parser.add_argument("--repo-root", default=os.environ.get("BUREAU_CODEX_BRIDGE_REPO_ROOT", "."))
    parser.add_argument(
        "--state-base",
        default=os.environ.get("BUREAU_CODEX_BRIDGE_STATE_BASE", str(default_state_base())),
    )
    parser.add_argument("--state-root", default=os.environ.get("BUREAU_CODEX_BRIDGE_STATE_ROOT"))
    parser.add_argument("--health", default=os.environ.get("BUREAU_CODEX_BRIDGE_HEALTH"))
    parser.add_argument("--frontier-report", default=os.environ.get("BUREAU_CODEX_BRIDGE_FRONTIER"))
    parser.add_argument(
        "--closure-plan",
        default=os.environ.get("BUREAU_CODEX_BRIDGE_CLOSURE_PLAN"),
    )
    parser.add_argument(
        "--closure-lanes",
        default=os.environ.get("BUREAU_CODEX_BRIDGE_CLOSURE_LANES"),
    )
    parser.add_argument(
        "--backend",
        choices=("none", "fixture", "codex"),
        default=os.environ.get("BUREAU_CODEX_BRIDGE_BACKEND", "none"),
    )
    parser.add_argument(
        "--fixture-decision",
        default=os.environ.get("BUREAU_CODEX_BRIDGE_FIXTURE_DECISION"),
    )
    parser.add_argument(
        "--bureau-command",
        default=os.environ.get("BUREAU_CODEX_BRIDGE_BUREAU_COMMAND"),
        help="Command used before --root/--json/status, for example 'bureau'.",
    )
    parser.add_argument(
        "--bureau-state-root",
        default=os.environ.get("BUREAU_CODEX_BRIDGE_BUREAU_STATE_ROOT"),
    )
    parser.add_argument(
        "--codex-command",
        default=os.environ.get("BUREAU_CODEX_BRIDGE_CODEX_COMMAND", "codex exec"),
    )
    parser.add_argument(
        "--codex-timeout-seconds",
        type=int,
        default=int(os.environ.get("BUREAU_CODEX_BRIDGE_CODEX_TIMEOUT_SECONDS", "300")),
    )
    parser.add_argument("--run-id", default=os.environ.get("BUREAU_CODEX_BRIDGE_RUN_ID"))
    parser.add_argument("--json", action="store_true")
    return parser


def config_from_args(args: argparse.Namespace) -> BridgeConfig:
    state_base = Path(args.state_base).expanduser()
    bureau_command = (
        tuple(shlex.split(args.bureau_command))
        if args.bureau_command
        else default_bureau_command()
    )
    config = default_config(
        repo_root=Path(args.repo_root).expanduser(),
        state_base=state_base,
        output_root=Path(args.state_root).expanduser()
        if args.state_root
        else state_base / "bureau-codex-bridge",
        backend=args.backend,
        fixture_decision_path=Path(args.fixture_decision).expanduser()
        if args.fixture_decision
        else None,
        bureau_command=bureau_command,
        bureau_state_root=Path(args.bureau_state_root).expanduser()
        if args.bureau_state_root
        else None,
        codex_command=tuple(shlex.split(args.codex_command)),
        codex_timeout_seconds=args.codex_timeout_seconds,
        run_id=args.run_id,
    )
    return BridgeConfig(
        **{
            **config.__dict__,
            "health_path": Path(args.health).expanduser() if args.health else config.health_path,
            "frontier_report_path": Path(args.frontier_report).expanduser()
            if args.frontier_report
            else config.frontier_report_path,
            "closure_plan_path": Path(args.closure_plan).expanduser()
            if args.closure_plan
            else config.closure_plan_path,
            "closure_lanes_path": Path(args.closure_lanes).expanduser()
            if args.closure_lanes
            else config.closure_lanes_path,
        }
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_bridge(config_from_args(args))
    payload = result if args.json else result["receipt"]
    print(
        json.dumps(
            payload,
            indent=2 if args.json else None,
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 1 if result["receipt"]["blocked"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
