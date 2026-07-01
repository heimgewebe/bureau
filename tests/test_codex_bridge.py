from __future__ import annotations

import json
import subprocess
from pathlib import Path

from bureau import codex_bridge

ROOT = Path(__file__).parents[1]
SERVICE = ROOT / "ops/systemd/bureau-codex-bridge.service"
TIMER = ROOT / "ops/systemd/bureau-codex-bridge.timer"


class FakeRunner:
    def __init__(self, *, status_returncode: int = 0, check_returncode: int = 0):
        self.calls: list[list[str]] = []
        self.status_returncode = status_returncode
        self.check_returncode = check_returncode

    def __call__(self, command):
        self.calls.append(list(command))
        action = command[-1]
        if action == "status":
            return subprocess.CompletedProcess(
                list(command),
                self.status_returncode,
                stdout=json.dumps({"runs": [], "tasks": {"ready": 1}}),
                stderr="",
            )
        if action == "check":
            return subprocess.CompletedProcess(
                list(command),
                self.check_returncode,
                stdout=json.dumps({"valid": self.check_returncode == 0}),
                stderr="" if self.check_returncode == 0 else "failed",
            )
        raise AssertionError(f"unexpected command: {command}")


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def write_inputs(state_base: Path, *, health: dict | None = None, frontier: bool = True) -> None:
    write_json(
        state_base / "bureau-cycle/health.json",
        health if health is not None else {"critical": False, "allow_next_dispatch": True},
    )
    if frontier:
        write_json(
            state_base / "bureau-agent-frontier/latest-report.json",
            {"schema_version": 1, "selected_frontier": []},
        )
    write_json(
        state_base / "bureau-closure/plan.json",
        {"schema_version": 1, "selected_lane_count": 0},
    )
    write_json(
        state_base / "bureau-closure/lanes.json",
        {"schema_version": 1, "lanes": []},
    )


def config(tmp_path: Path, *, backend: str = "none", fixture: Path | None = None):
    state_base = tmp_path / "state"
    return codex_bridge.default_config(
        repo_root=ROOT,
        state_base=state_base,
        output_root=tmp_path / "bridge-state",
        backend=backend,
        fixture_decision_path=fixture,
        bureau_command=("bureau",),
        run_id="run-test",
    )


def blocker_codes(receipt: dict) -> set[str]:
    return {blocker["code"] for blocker in receipt["blockers"]}


def test_collect_context_reads_all_sources_and_bureau_commands(tmp_path):
    selected = config(tmp_path)
    write_inputs(selected.state_base)
    runner = FakeRunner()

    context = codex_bridge.collect_context(selected, runner=runner)

    assert context["run_id"] == "run-test"
    assert context["blockers"] == []
    assert context["sources"]["health"]["data"]["allow_next_dispatch"] is True
    assert context["sources"]["frontier"]["available"] is True
    assert context["sources"]["bureau_status"]["ok"] is True
    assert context["sources"]["bureau_check"]["ok"] is True
    assert [call[-1] for call in runner.calls] == ["status", "check"]


def test_bridge_blocks_on_health_critical_and_dispatch_closed(tmp_path):
    selected = config(tmp_path)
    write_inputs(
        selected.state_base,
        health={"critical": True, "allow_next_dispatch": False},
    )

    result = codex_bridge.run_bridge(selected, runner=FakeRunner())

    receipt = result["receipt"]
    assert receipt["blocked"] is True
    assert {"health_critical", "dispatch_not_allowed"} <= blocker_codes(receipt)


def test_bridge_blocks_when_frontier_report_is_missing(tmp_path):
    selected = config(tmp_path)
    write_inputs(selected.state_base, frontier=False)

    result = codex_bridge.run_bridge(selected, runner=FakeRunner())

    assert result["receipt"]["blocked"] is True
    assert "missing_frontier" in blocker_codes(result["receipt"])


def test_bridge_blocks_invalid_fixture_decision(tmp_path):
    fixture = tmp_path / "decision.json"
    write_json(fixture, {"schema_version": 1, "action": "execute", "confidence": 0.8})
    selected = config(tmp_path, backend="fixture", fixture=fixture)
    write_inputs(selected.state_base)

    result = codex_bridge.run_bridge(selected, runner=FakeRunner())

    receipt = result["receipt"]
    assert receipt["blocked"] is True
    assert "invalid_decision" in blocker_codes(receipt)
    assert receipt["decision_valid"] is False
    assert receipt["mutation_performed"] is False


def test_bridge_accepts_valid_fixture_decision_without_mutation(tmp_path):
    fixture = tmp_path / "decision.json"
    decision = {
        "schema_version": 1,
        "action": "propose_task_execution",
        "confidence": 0.75,
        "rationale": "fixture-only proposal",
    }
    write_json(fixture, decision)
    selected = config(tmp_path, backend="fixture", fixture=fixture)
    write_inputs(selected.state_base)

    result = codex_bridge.run_bridge(selected, runner=FakeRunner())

    receipt = result["receipt"]
    run_dir = Path(result["run_dir"])
    assert receipt["blocked"] is False
    assert receipt["result"] == "completed"
    assert receipt["decision"] == decision
    assert receipt["mutation_performed"] is False
    assert (run_dir / "context.json").is_file()
    assert (run_dir / "prompt.md").is_file()
    assert (run_dir / "receipt.json").is_file()


def test_bridge_blocks_when_bureau_check_fails(tmp_path):
    selected = config(tmp_path)
    write_inputs(selected.state_base)

    result = codex_bridge.run_bridge(selected, runner=FakeRunner(check_returncode=2))

    assert result["receipt"]["blocked"] is True
    assert "bureau_check_failed" in blocker_codes(result["receipt"])


def test_codex_bridge_service_is_read_only_except_bridge_state():
    text = SERVICE.read_text(encoding="utf-8")
    assert "ProtectSystem=strict" in text
    assert "ProtectHome=read-only" in text
    assert "ReadWritePaths=%h/.local/state/bureau-codex-bridge" in text
    assert "NoNewPrivileges=true" in text
    assert "RestrictAddressFamilies=AF_UNIX" in text
    assert "bureau-codex-bridge" in text


def test_codex_bridge_timer_runs_at_57():
    text = TIMER.read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* *:57:00" in text
    assert "Unit=bureau-codex-bridge.service" in text


def test_bridge_runs_codex_backend_in_run_directory(tmp_path):
    selected = config(tmp_path, backend="codex")
    write_inputs(selected.state_base)
    seen: dict[str, object] = {}

    def fake_codex(command, prompt, cwd, timeout_seconds):
        seen["command"] = list(command)
        seen["prompt"] = prompt
        seen["cwd"] = cwd
        seen["timeout_seconds"] = timeout_seconds
        decision = {
            "schema_version": 1,
            "action": "request_human_review",
            "confidence": 0.8,
            "rationale": "valid codex fixture",
        }
        return subprocess.CompletedProcess(list(command), 0, stdout=json.dumps(decision), stderr="")

    result = codex_bridge.run_bridge(
        selected,
        runner=FakeRunner(),
        codex_runner=fake_codex,
    )

    receipt = result["receipt"]
    run_dir = Path(result["run_dir"])
    assert receipt["blocked"] is False
    assert receipt["result"] == "completed"
    assert receipt["mutation_performed"] is False
    assert receipt["decision"]["action"] == "request_human_review"
    assert receipt["backend_observation"]["backend"] == "codex"
    assert receipt["backend_observation"]["returncode"] == 0
    assert seen["command"] == ["codex", "exec"]
    assert seen["cwd"] == run_dir
    assert "decision" in receipt["artifacts"]
    assert receipt["artifacts"]["decision"].endswith("decision.json")
    assert (run_dir / "decision.json").is_file()


def test_bridge_blocks_when_codex_writes_no_decision(tmp_path):
    selected = config(tmp_path, backend="codex")
    write_inputs(selected.state_base)

    def fake_codex(command, prompt, cwd, timeout_seconds):
        return subprocess.CompletedProcess(list(command), 0, stdout="no decision", stderr="")

    result = codex_bridge.run_bridge(
        selected,
        runner=FakeRunner(),
        codex_runner=fake_codex,
    )

    receipt = result["receipt"]
    assert receipt["blocked"] is True
    assert "invalid_decision" in blocker_codes(receipt)
    assert receipt["decision_valid"] is False
    assert "stdout_did_not_contain_json_object" in receipt["decision_errors"]
    assert receipt["mutation_performed"] is False
