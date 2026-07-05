from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from bureau import codex_bridge

ROOT = Path(__file__).parents[1]
SERVICE = ROOT / "ops/systemd/bureau-codex-bridge.service"
TIMER = ROOT / "ops/systemd/bureau-codex-bridge.timer"


class FakeRunner:
    def __init__(
        self,
        *,
        status_returncode: int = 0,
        check_returncode: int = 0,
        check_returncodes: list[int] | None = None,
    ):
        self.calls: list[list[str]] = []
        self.status_returncode = status_returncode
        self.check_returncode = check_returncode
        self.check_returncodes = list(check_returncodes or [])

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
            returncode = (
                self.check_returncodes.pop(0)
                if self.check_returncodes
                else self.check_returncode
            )
            return subprocess.CompletedProcess(
                list(command),
                returncode,
                stdout=json.dumps({"valid": returncode == 0}),
                stderr="" if returncode == 0 else "failed",
            )
        raise AssertionError(f"unexpected command: {command}")


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def write_inputs(
    state_base: Path,
    *,
    health: dict | None = None,
    frontier: bool | dict = True,
) -> None:
    write_json(
        state_base / "bureau-cycle/health.json",
        health if health is not None else {"critical": False, "allow_next_dispatch": True},
    )
    if isinstance(frontier, dict):
        write_json(state_base / "bureau-agent-frontier/latest-report.json", frontier)
    elif frontier:
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


def config(
    tmp_path: Path,
    *,
    backend: str = "none",
    fixture: Path | None = None,
    repo_root: Path = ROOT,
    binding_gate: bool = False,
):
    state_base = tmp_path / "state"
    return codex_bridge.default_config(
        repo_root=repo_root,
        state_base=state_base,
        output_root=tmp_path / "bridge-state",
        backend=backend,
        fixture_decision_path=fixture,
        bureau_command=("bureau",),
        run_id="run-test",
        binding_gate=binding_gate,
    )


def blocker_codes(receipt: dict) -> set[str]:
    return {blocker["code"] for blocker in receipt["blockers"]}


def make_binding_registry(
    root: Path,
    *,
    existing_task: dict | None = None,
) -> Path:
    for folder in ("registry/initiatives", "registry/tasks", "registry/resources", "schemas"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    for schema in (ROOT / "schemas").glob("*.json"):
        shutil.copy2(schema, root / "schemas" / schema.name)
    write_json(
        root / "registry/initiatives/BUR-TEST-001.json",
        {
            "schema_version": 1,
            "id": "BUR-TEST-001",
            "title": "Binding Test",
            "state": "active",
            "commitment": "now",
            "goal": "Test binding task creation",
            "completion": ["done"],
            "parallelism": {"max_active_tasks": 2},
        },
    )
    write_json(
        root / "registry/resources/root.json",
        {"schema_version": 1, "id": "root", "type": "group"},
    )
    write_json(
        root / "registry/resources/repo.json",
        {"schema_version": 1, "id": "repo", "type": "group", "parent": "root"},
    )
    write_json(
        root / "registry/resources/grabowski.json",
        {
            "schema_version": 1,
            "id": "repo.grabowski",
            "type": "git-repository",
            "parent": "repo",
            "path": "/tmp/grabowski",
            "grabowski_key": "repo:/tmp/grabowski",
        },
    )
    write_json(
        root / "registry/queue.json",
        {
            "schema_version": 1,
            "queue_policy": "skip-blocked",
            "lanes": {"now": [], "next": [], "later": []},
        },
    )
    if existing_task is not None:
        write_json(root / f"registry/tasks/{existing_task['id']}.json", existing_task)
    return root


def binding_lane(*, lane_id: str = "lane-grabowski-new", branch: str = "feat/new") -> dict:
    return {
        "lane_id": lane_id,
        "score": 120,
        "eligible": True,
        "rejected_reason": None,
        "repo_name": "grabowski",
        "repo": "/tmp/grabowski",
        "branch": branch,
        "state": "active",
        "task_id": None,
        "finishability": 0.5,
        "next_action": "bind to canonical Bureau task before dispatch",
        "reasons": ["state:active"],
        "recommended_action": "bind this lane to one canonical Bureau task before dispatch",
        "suggested_worker_profile": "grabowski-local-readonly",
    }


def binding_frontier(lane: dict) -> dict:
    return {
        "schema_version": 1,
        "selected_frontier": [],
        "closure_binding_frontier": [lane],
    }


def write_decision(
    tmp_path: Path,
    *,
    lane_id: str = "lane-grabowski-new",
    confidence: float = 0.8,
) -> Path:
    fixture = tmp_path / "decision.json"
    write_json(
        fixture,
        {
            "schema_version": 1,
            "action": "propose_binding",
            "confidence": confidence,
            "lane_id": lane_id,
        },
    )
    return fixture


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


def test_binding_gate_disabled_blocks_and_writes_no_task(tmp_path):
    root = make_binding_registry(tmp_path / "registry-root")
    lane = binding_lane()
    fixture = write_decision(tmp_path)
    selected = config(tmp_path, backend="fixture", fixture=fixture, repo_root=root)
    write_inputs(selected.state_base, frontier=binding_frontier(lane))

    result = codex_bridge.run_bridge(selected, runner=FakeRunner())

    receipt = result["receipt"]
    assert receipt["blocked"] is True
    assert "binding_gate_disabled" in blocker_codes(receipt)
    assert receipt["binding_result"]["status"] == "disabled"
    assert receipt["mutation_performed"] is False
    assert sorted((root / "registry/tasks").glob("*.json")) == []


def test_valid_propose_binding_writes_one_planned_task(tmp_path):
    root = make_binding_registry(tmp_path / "registry-root")
    lane = binding_lane()
    fixture = write_decision(tmp_path)
    selected = config(
        tmp_path,
        backend="fixture",
        fixture=fixture,
        repo_root=root,
        binding_gate=True,
    )
    write_inputs(selected.state_base, frontier=binding_frontier(lane))
    runner = FakeRunner()

    result = codex_bridge.run_bridge(selected, runner=runner)

    receipt = result["receipt"]
    task_files = sorted((root / "registry/tasks").glob("*.json"))
    assert receipt["blocked"] is False
    assert receipt["mutation_performed"] is True
    assert receipt["binding_result"]["status"] == "written"
    assert receipt["binding_result"]["task_id"] == "BUR-TEST-001-T001"
    assert [path.name for path in task_files] == ["BUR-TEST-001-T001.json"]
    task = json.loads(task_files[0].read_text(encoding="utf-8"))
    assert task["state"] == "planned"
    assert task["execution"]["policy"] == "review-before-effect"
    assert task["metadata"]["closure_lane_ids"] == [lane["lane_id"]]
    assert task["metadata"]["source_repository"] == lane["repo"]
    assert task["metadata"]["source_branch"] == lane["branch"]
    assert [call[-1] for call in runner.calls] == ["status", "check", "check"]


def test_duplicate_lane_blocks_binding_task_write(tmp_path):
    lane = binding_lane()
    existing = {
        "schema_version": 1,
        "id": "BUR-TEST-001-T001",
        "initiative": "BUR-TEST-001",
        "title": "Existing binding",
        "state": "planned",
        "goal": "Existing lane binding",
        "depends_on": [],
        "required_capabilities": ["repository", "shell"],
        "priority": {"lane": "next", "rank": 10},
        "execution": {"mode": "interactive-agent", "policy": "review-before-effect"},
        "claims": [{"resource": "repo.grabowski", "mode": "write", "isolation": "worktree"}],
        "acceptance": [{"id": "proof", "assertion": "proof exists"}],
        "metadata": {"closure_lane_ids": [lane["lane_id"]]},
    }
    root = make_binding_registry(tmp_path / "registry-root", existing_task=existing)
    fixture = write_decision(tmp_path)
    selected = config(
        tmp_path,
        backend="fixture",
        fixture=fixture,
        repo_root=root,
        binding_gate=True,
    )
    write_inputs(selected.state_base, frontier=binding_frontier(lane))

    result = codex_bridge.run_bridge(selected, runner=FakeRunner())

    assert result["receipt"]["blocked"] is True
    assert "binding_duplicate_existing_task" in blocker_codes(result["receipt"])
    assert result["receipt"]["mutation_performed"] is False
    assert sorted(path.name for path in (root / "registry/tasks").glob("*.json")) == [
        "BUR-TEST-001-T001.json"
    ]


def test_low_confidence_blocks_binding_task_write(tmp_path):
    root = make_binding_registry(tmp_path / "registry-root")
    lane = binding_lane()
    fixture = write_decision(tmp_path, confidence=0.74)
    selected = config(
        tmp_path,
        backend="fixture",
        fixture=fixture,
        repo_root=root,
        binding_gate=True,
    )
    write_inputs(selected.state_base, frontier=binding_frontier(lane))

    result = codex_bridge.run_bridge(selected, runner=FakeRunner())

    assert result["receipt"]["blocked"] is True
    assert "binding_low_confidence" in blocker_codes(result["receipt"])
    assert result["receipt"]["mutation_performed"] is False
    assert sorted((root / "registry/tasks").glob("*.json")) == []


def test_post_check_failure_rolls_back_binding_task(tmp_path):
    root = make_binding_registry(tmp_path / "registry-root")
    lane = binding_lane()
    fixture = write_decision(tmp_path)
    selected = config(
        tmp_path,
        backend="fixture",
        fixture=fixture,
        repo_root=root,
        binding_gate=True,
    )
    write_inputs(selected.state_base, frontier=binding_frontier(lane))

    result = codex_bridge.run_bridge(
        selected,
        runner=FakeRunner(check_returncodes=[0, 2]),
    )

    receipt = result["receipt"]
    assert receipt["blocked"] is True
    assert "binding_post_check_failed" in blocker_codes(receipt)
    assert receipt["binding_result"]["status"] == "rolled_back"
    assert receipt["mutation_performed"] is False
    assert sorted((root / "registry/tasks").glob("*.json")) == []


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
