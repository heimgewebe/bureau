from __future__ import annotations

from bureau.core import Dispatcher, Registry, StateStore
from bureau.v2 import state_root_hygiene


def setup_state(root, tmp_path, monkeypatch):
    state_root = tmp_path / "state"
    monkeypatch.setenv("BUREAU_STATE_DIR", str(state_root))
    registry = Registry.load(root)
    store = StateStore(state_root / "bureau.sqlite3")
    return registry, store


def test_active_state_root_entries_stay_known_only(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    registry, store = setup_state(root, tmp_path, monkeypatch)

    report = Dispatcher(registry, store).doctor()["state_root_hygiene"]

    assert report["healthy"] is True
    assert report["archive_candidate_entries"] == []
    assert report["unknown_entries"] == []
    known = {entry["name"]: entry["class"] for entry in report["known_entries"]}
    active_known = {
        "bureau.sqlite3": "sqlite-database",
        "envelopes": "envelope-directory",
        "receipts": "receipt-directory",
    }
    for name, class_name in active_known.items():
        assert known[name] == class_name
    assert set(known.values()) <= {
        "envelope-directory",
        "receipt-directory",
        "sqlite-database",
        "sqlite-sidecar",
    }


def test_configured_state_database_sidecars_stay_known(
    registry_factory, tmp_path
):
    root = registry_factory(1)
    state_root = tmp_path / "custom-state"
    registry = Registry.load(root)
    store = StateStore(state_root / "custom.sqlite3")
    (state_root / "custom.sqlite3-wal").write_text("", encoding="utf-8")
    (state_root / "custom.sqlite3-shm").write_text("", encoding="utf-8")

    report = Dispatcher(registry, store).doctor()["state_root_hygiene"]

    assert report["healthy"] is True
    assert report["archive_candidate_entries"] == []
    assert report["unknown_entries"] == []
    known = {entry["name"]: entry["class"] for entry in report["known_entries"]}
    assert known["custom.sqlite3"] == "sqlite-database"
    assert known["custom.sqlite3-wal"] == "sqlite-sidecar"
    assert known["custom.sqlite3-shm"] == "sqlite-sidecar"


def test_legacy_state_root_artifacts_are_archive_candidates(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    registry, store = setup_state(root, tmp_path, monkeypatch)
    candidates = {
        "archived-untracked": ("directory", "legacy-archive-directory"),
        "merge-gatekeeper-runs": ("directory", "legacy-merge-gatekeeper-runs"),
        "manual-maintenance-20260628T084045Z": (
            "directory",
            "legacy-manual-maintenance-directory",
        ),
        "pre-foundation-20260628T071047Z": (
            "directory",
            "legacy-pre-foundation-directory",
        ),
        "recovery-20260628T093430Z": (
            "directory",
            "legacy-recovery-directory",
        ),
        "bureau.before-t005-reverify-20260702T182019Z.sqlite3": (
            "file",
            "legacy-sqlite-backup",
        ),
        "coding-delegator-20260630T0903.json": (
            "file",
            "legacy-coding-delegator-artifact",
        ),
        "evidence-BUR-RUN-20260627T180009Z-0579f4c484.json": (
            "file",
            "legacy-evidence-artifact",
        ),
        "lenskit-codex-handoff-20260630T0921.json": (
            "file",
            "legacy-agent-handoff-artifact",
        ),
        "merge-gatekeeper-latest.json": (
            "file",
            "legacy-merge-gatekeeper-artifact",
        ),
        "ollama-wg-generated.py": ("file", "legacy-operator-artifact"),
        "pr825-merged.json": ("file", "legacy-pr-merge-artifact"),
        "review-steward-20260630T0823.json": (
            "file",
            "legacy-review-steward-artifact",
        ),
        "run-goose-weltgewebe.sh": ("file", "legacy-operator-artifact"),
        "run-qwen-weltgewebe.sh": ("file", "legacy-operator-artifact"),
        "weltgewebe-finalize-prompt.txt": (
            "file",
            "legacy-weltgewebe-artifact",
        ),
        "wg-coordinator.00": ("file", "legacy-wg-artifact"),
        "wg-source.b64.0": ("file", "legacy-wg-artifact"),
    }
    for name, (entry_type, _) in candidates.items():
        entry = store.state_root / name
        if entry_type == "directory":
            entry.mkdir()
        else:
            entry.write_text("legacy artifact", encoding="utf-8")

    report = Dispatcher(registry, store).doctor()["state_root_hygiene"]

    assert report["healthy"] is True
    assert report["unknown_entries"] == []
    assert report["archive_candidate_count"] == len(candidates)
    archive_classes = {
        entry["name"]: entry["class"]
        for entry in report["archive_candidate_entries"]
    }
    expected_classes = {
        name: class_name for name, (_, class_name) in candidates.items()
    }
    assert archive_classes == expected_classes
    known_names = {entry["name"] for entry in report["known_entries"]}
    assert set(candidates).isdisjoint(known_names)


def test_loose_state_root_notes_and_helpers_remain_unknown(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    registry, store = setup_state(root, tmp_path, monkeypatch)
    notes = store.state_root / "notes.txt"
    helper = store.state_root / "read_bounded.py"
    notes.write_text("human note", encoding="utf-8")
    helper.write_text("scratch helper\n", encoding="utf-8")

    report = Dispatcher(registry, store).doctor()["state_root_hygiene"]

    assert report["healthy"] is False
    assert report["archive_candidate_entries"] == []
    assert report["unknown_entries"] == [
        {"name": "notes.txt", "type": "file", "class": "unknown"},
        {"name": "read_bounded.py", "type": "file", "class": "unknown"},
    ]


def test_malformed_archive_like_directory_remains_unknown(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    registry, store = setup_state(root, tmp_path, monkeypatch)
    foreign = store.state_root / "manual-maintenance"
    foreign.mkdir()

    report = Dispatcher(registry, store).doctor()["state_root_hygiene"]

    assert report["healthy"] is False
    assert report["archive_candidate_entries"] == []
    assert report["unknown_entries"] == [
        {"name": "manual-maintenance", "type": "directory", "class": "unknown"}
    ]


def test_unknown_entries_stay_hard_findings_with_archive_candidates(
    registry_factory, tmp_path, monkeypatch
):
    root = registry_factory(1)
    registry, store = setup_state(root, tmp_path, monkeypatch)
    archive_candidate = store.state_root / "pr825-merged.json"
    unknown = store.state_root / "foreign-prompt.txt"
    archive_candidate.write_text("{}\n", encoding="utf-8")
    unknown.write_text("not bureau state", encoding="utf-8")

    doctor = Dispatcher(registry, store).doctor(repair=True)
    report = doctor["state_root_hygiene"]

    assert doctor["healthy"] is False
    assert report["healthy"] is False
    assert report["archive_candidate_entries"] == [
        {
            "name": "pr825-merged.json",
            "type": "file",
            "class": "legacy-pr-merge-artifact",
        }
    ]
    assert report["unknown_entries"] == [
        {"name": "foreign-prompt.txt", "type": "file", "class": "unknown"}
    ]
    assert archive_candidate.read_text(encoding="utf-8") == "{}\n"
    assert unknown.read_text(encoding="utf-8") == "not bureau state"


def test_missing_state_root_report_keeps_archive_candidate_shape(tmp_path):
    state_root = tmp_path / "missing-state"

    report = state_root_hygiene(state_root, state_root / "bureau.sqlite3")

    assert report["available"] is False
    assert report["healthy"] is False
    assert report["error"] == "missing"
    assert report["known_entries"] == []
    assert report["archive_candidate_entries"] == []
    assert report["unknown_entries"] == []
    assert report["known_count"] == 0
    assert report["archive_candidate_count"] == 0
    assert report["unknown_count"] == 0


def test_not_directory_state_root_report_keeps_archive_candidate_shape(tmp_path):
    state_root = tmp_path / "state-file"
    state_root.write_text("not a directory", encoding="utf-8")

    report = state_root_hygiene(state_root, tmp_path / "bureau.sqlite3")

    assert report["available"] is False
    assert report["healthy"] is False
    assert report["error"] == "not-directory"
    assert report["known_entries"] == []
    assert report["archive_candidate_entries"] == []
    assert report["unknown_entries"] == []
    assert report["known_count"] == 0
    assert report["archive_candidate_count"] == 0
    assert report["unknown_count"] == 0
