from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from bureau import agent_frontier
from bureau.agent_frontier import build_frontier_report, run_frontier_cycle
from bureau.cycle_contract import validate_receipt
from bureau.task_supply import SupplyError, file_sha256


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


def test_frontier_selects_unbound_closure_lanes(tmp_path: Path) -> None:
    lanes_path = tmp_path / "lanes.json"
    lanes_path.write_text(
        json.dumps(
            {
                "lanes": [
                    {
                        "lane_id": "lane-grabowski-active",
                        "repo_name": "grabowski",
                        "repo": "/tmp/grabowski",
                        "branch": "feat/operator-workspace-v1",
                        "state": "active",
                        "task_id": None,
                        "finishability": 0.45,
                        "next_action": "bind to canonical Bureau task before dispatch",
                    },
                    {
                        "lane_id": "lane-weltgewebe-planned",
                        "repo_name": "weltgewebe",
                        "repo": "/tmp/weltgewebe",
                        "branch": "feat/docmeta-proof",
                        "state": "planned",
                        "task_id": None,
                        "finishability": 0.8,
                    },
                    {
                        "lane_id": "lane-bound",
                        "repo_name": "weltgewebe",
                        "repo": "/tmp/weltgewebe",
                        "branch": "feat/bound",
                        "state": "planned",
                        "task_id": "BUR-2026-001-T999",
                    },
                    {
                        "lane_id": "lane-old",
                        "repo_name": "grabowski",
                        "repo": "/tmp/grabowski",
                        "branch": "feat/old",
                        "state": "obsolete",
                        "task_id": None,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    report = build_frontier_report(
        source_state(),
        closure_lanes_path=lanes_path,
        generated_at="2026-06-30T05:55:00Z",
    )

    assert report["metrics"]["closure_lane_count"] == 4
    assert report["metrics"]["eligible_binding_candidate_count"] == 2
    assert report["metrics"]["selected_binding_candidate_count"] == 2
    assert report["closure_binding_frontier"][0]["lane_id"] == "lane-grabowski-active"
    rejected = {
        item["lane_id"]: item["rejected_reason"]
        for item in report["closure_binding_rejected_sample"]
    }
    assert rejected["lane-bound"] == "already_bound_to_canonical_task"
    assert rejected["lane-old"] == "terminal_or_obsolete_lane"


def _write_stale_supply_fixture(tmp_path: Path) -> tuple[Path, Path, bytes]:
    registry = make_registry(tmp_path / "supply-registry")
    queue_path = registry / "registry/queue.json"
    queue_path.write_text(
        json.dumps({"lanes": {"now": [], "next": [], "later": []}}),
        encoding="utf-8",
    )
    current_head = "b" * 40
    (registry / ".bureau-runtime-snapshot.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "bureau_registry_snapshot",
                "source_commit": current_head,
            }
        ),
        encoding="utf-8",
    )
    supply_root = tmp_path / "supply-state"
    supply_root.mkdir()
    report_path = supply_root / "latest-report.json"
    report = {
        "schema_version": 1,
        "kind": "bureau_task_supply_report",
        "generated_at": "2026-08-08T08:00:00Z",
        "status": "blocked",
        "registry": {
            "root": str(registry),
            "head": "a" * 40,
            "queue_sha256": file_sha256(queue_path),
        },
        "policy": {
            "schema_version": 1,
            "floor": 8,
            "refill_target": 12,
            "max_new_per_cycle": 4,
            "bucket_hours": 24,
        },
        "approval_available": False,
        "mutation_authority_observed": False,
        "metrics": {
            "raw_ready_count": 1,
            "normal_claimable_count": 0,
            "fallback_claimable_count": 0,
            "total_claimable_count": 0,
            "blocked_ready_count": 1,
            "floor": 8,
            "refill_target": 12,
            "shortage_to_target": 12,
            "proposal_count": 0,
            "blocked_proposal_count": 0,
        },
        "blockers": ["registry-mutation-authority-unavailable"],
        "publication_plan": {"plan_sha256": "c" * 64},
    }
    report["report_sha256"] = agent_frontier.sha256_json(report)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    (supply_root / "frontier-snapshot.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "bureau_authoritative_frontier_snapshot",
                "capabilities": ["repository", "python", "testing", "bureau", "grabowski"],
                "frontier": [],
            }
        ),
        encoding="utf-8",
    )
    return registry, report_path, report_path.read_bytes()


def test_stale_supply_is_regenerated_read_only_before_frontier_consumption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, stale_path, stale_bytes = _write_stale_supply_fixture(tmp_path)
    regeneration_root = tmp_path / "frontier-state/task-supply-regeneration"
    calls: list[dict[str, object]] = []

    def fake_cycle(**kwargs: object) -> dict[str, object]:
        calls.append(dict(kwargs))
        selected_root = Path(str(kwargs["state_root"]))
        selected_root.mkdir(parents=True, exist_ok=True)
        queue_sha256 = file_sha256(registry / "registry/queue.json")
        report = {
            "schema_version": 1,
            "kind": "bureau_task_supply_report",
            "generated_at": "2026-08-08T09:00:00Z",
            "status": "blocked",
            "registry": {
                "root": str(registry),
                "head": "b" * 40,
                "queue_sha256": queue_sha256,
            },
            "metrics": {"total_claimable_count": 0, "floor": 8, "refill_target": 12},
            "blockers": ["registry-mutation-authority-unavailable"],
            "publication_plan": {"plan_sha256": "d" * 64},
        }
        report["report_sha256"] = agent_frontier.sha256_json(report)
        fresh_path = selected_root / "latest-report.json"
        fresh_path.write_text(json.dumps(report), encoding="utf-8")
        return {
            "registry": report["registry"],
            "report_path": str(fresh_path),
            "publication": {"attempted": False, "status": "preview-only"},
        }

    import bureau.supply_runner as supply_runner

    monkeypatch.setattr(supply_runner, "run_supply_cycle", fake_cycle)
    report = build_frontier_report(
        source_state(),
        registry_root=registry,
        task_supply_report_path=stale_path,
        task_supply_regeneration_root=regeneration_root,
        generated_at="2026-08-08T09:00:00Z",
    )

    supply = report["scanner_summary"]["task_supply"]
    assert supply["available"] is True
    assert supply["regeneration"]["status"] == "regenerated"
    assert supply["regeneration"]["mutation_authority"] is False
    assert supply["regeneration"]["publish"] is False
    assert not any(
        item["kind"] == "claimable_task_supply_report_invalid"
        for item in report["bottlenecks"]
    )
    assert len(calls) == 1
    second = build_frontier_report(
        source_state(),
        registry_root=registry,
        task_supply_report_path=stale_path,
        task_supply_regeneration_root=regeneration_root,
        generated_at="2026-08-08T09:01:00Z",
    )
    assert second["scanner_summary"]["task_supply"]["regeneration"]["status"] == "regenerated"
    assert len(calls) == 2
    assert calls[0]["mutation_authority"] is False
    assert calls[0]["publish"] is False
    assert calls[0]["registry_head"] == "b" * 40
    assert calls[0]["capabilities"] == (
        "bureau",
        "grabowski",
        "python",
        "repository",
        "testing",
    )
    assert stale_path.read_bytes() == stale_bytes


def test_stale_supply_regeneration_failure_preserves_stale_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, stale_path, stale_bytes = _write_stale_supply_fixture(tmp_path)

    import bureau.supply_runner as supply_runner

    def fail_cycle(**_kwargs: object) -> dict[str, object]:
        raise SupplyError("bounded regeneration failure")

    monkeypatch.setattr(supply_runner, "run_supply_cycle", fail_cycle)
    report = build_frontier_report(
        source_state(),
        registry_root=registry,
        task_supply_report_path=stale_path,
        task_supply_regeneration_root=tmp_path / "frontier-state/task-supply-regeneration",
        generated_at="2026-08-08T09:00:00Z",
    )

    supply = report["scanner_summary"]["task_supply"]
    assert supply["invalid"] is True
    assert supply["stale"] is True
    assert supply["reason"] == "registry-binding-stale"
    assert supply["regeneration"]["status"] == "failed"
    assert "bounded regeneration failure" in supply["regeneration"]["error"]
    assert any(
        item["kind"] == "claimable_task_supply_report_invalid"
        for item in report["bottlenecks"]
    )
    assert report["selected_frontier"]
    assert stale_path.read_bytes() == stale_bytes
