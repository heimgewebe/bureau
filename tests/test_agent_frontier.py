from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from bureau.agent_frontier import build_frontier_report, run_frontier_cycle
from bureau.cycle_contract import validate_receipt


def source_state() -> dict:
    return {
        "schema_version": 2,
        "contract_version": 2,
        "updated_at": "2026-06-30T05:30:00Z",
        "source_revisions": [],
        "candidate_fingerprints": ["a", "b", "c", "d"],
        "documents": {
            "repo:lenskit:docs/roadmap.md": {
                "source_id": "repo:lenskit",
                "source_revision": "a" * 40,
                "source_path": "docs/roadmap.md",
                "project": "lenskit",
                "sha256": "1" * 64,
                "candidates": [
                    {
                        "fingerprint": "a",
                        "candidate_kind": "structured-task",
                        "status": "open",
                        "summary": "Build deterministic Lens Card proof fixtures",
                        "confidence": "high",
                        "source_anchor": "item:LC-001",
                    }
                ],
            },
            "repo:vibe-lab:docs/backlog.md": {
                "source_id": "repo:vibe-lab",
                "source_revision": "b" * 40,
                "source_path": "docs/backlog.md",
                "project": "vibe-lab",
                "sha256": "2" * 64,
                "candidates": [
                    {
                        "fingerprint": "b",
                        "candidate_kind": "planning-item",
                        "status": "open",
                        "summary": "Explore future theme variants",
                        "confidence": "medium",
                        "source_anchor": "L12",
                    }
                ],
            },
            "repo:grabowski:docs/archive/old-plan.md": {
                "source_id": "repo:grabowski",
                "source_revision": "c" * 40,
                "source_path": "docs/archive/old-plan.md",
                "project": "grabowski",
                "sha256": "3" * 64,
                "candidates": [
                    {
                        "fingerprint": "c",
                        "candidate_kind": "unchecked-item",
                        "status": "open",
                        "summary": "Legacy archived task should not be promoted",
                        "confidence": "high",
                        "source_anchor": "L3",
                    }
                ],
            },
            "repo:weltgewebe:docs/plan.md": {
                "source_id": "repo:weltgewebe",
                "source_revision": "d" * 40,
                "source_path": "docs/plan.md",
                "project": "weltgewebe",
                "sha256": "4" * 64,
                "candidates": [
                    {
                        "fingerprint": "d",
                        "candidate_kind": "unchecked-item",
                        "status": "partial",
                        "summary": "Already registered task",
                        "confidence": "high",
                        "source_anchor": "L8",
                    }
                ],
            },
        },
    }


def make_registry(root: Path) -> Path:
    task_dir = root / "registry/tasks"
    task_dir.mkdir(parents=True)
    (task_dir / "BUR-TEST-001-T001.json").write_text(
        json.dumps(
            {
                "id": "BUR-TEST-001-T001",
                "title": "Already registered task",
                "metadata": {"frontier_fingerprint": "z"},
            }
        ),
        encoding="utf-8",
    )
    return root


def test_frontier_ranks_focus_candidate_and_rejects_known_or_stale(tmp_path: Path) -> None:
    registry = make_registry(tmp_path / "registry-root")

    report = build_frontier_report(
        source_state(),
        registry_root=registry,
        source_state_path=tmp_path / "source-state.json",
        focus_repositories=("weltgewebe", "lenskit", "grabowski"),
        limit=3,
        generated_at="2026-06-30T05:55:00Z",
    )

    assert report["metrics"]["candidate_count"] == 4
    assert report["metrics"]["selected_frontier_count"] == 2
    assert report["selected_frontier"][0]["project"] == "lenskit"
    assert report["selected_frontier"][0]["suggested_worker_profile"] == "codex-readonly-scout"
    rejected = {item["fingerprint"]: item["rejected_reason"] for item in report["rejected_sample"]}
    assert rejected["c"] == "stale_or_archived_source_path"
    assert rejected["d"] == "already_registered_title"


def test_frontier_report_matches_schema(tmp_path: Path) -> None:
    schema = json.loads(Path("schemas/agent-frontier-report.v1.schema.json").read_text())
    registry = make_registry(tmp_path / "registry-root")
    report = build_frontier_report(
        source_state(),
        registry_root=registry,
        generated_at="2026-06-30T05:55:00Z",
    )

    Draft202012Validator(schema).validate(report)


def test_frontier_cycle_writes_report_and_terminal_receipt(tmp_path: Path) -> None:
    source_path = tmp_path / "source-state.json"
    source_path.write_text(json.dumps(source_state()), encoding="utf-8")
    scanner_latest = tmp_path / "scanner-latest.json"
    scanner_latest.write_text(
        json.dumps({"metrics": {"candidate_count": 4, "new_candidate_count": 0}}),
        encoding="utf-8",
    )
    closure_plan = tmp_path / "closure-plan.json"
    closure_plan.write_text(
        json.dumps({"selected_lane_count": 1, "unbound_selected_rejected_count": 12}),
        encoding="utf-8",
    )
    state_root = tmp_path / "frontier-state"

    result = run_frontier_cycle(
        source_state_path=source_path,
        scanner_latest_path=scanner_latest,
        closure_plan_path=closure_plan,
        registry_root=make_registry(tmp_path / "registry-root"),
        state_root=state_root,
        limit=2,
    )

    report_path = Path(result["report_path"])
    assert report_path.is_file()
    receipt = result["receipt"]
    assert receipt["stage"] == "frontier"
    assert receipt["result"] == "completed"
    assert validate_receipt(receipt, expected_stage="frontier") == []
    assert (state_root / "latest.json").is_file()
    assert (state_root / "latest-report.json").is_file()
