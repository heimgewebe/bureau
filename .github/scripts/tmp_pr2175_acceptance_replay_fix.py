from __future__ import annotations

from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one match, got {count}")
    return text.replace(old, new, 1)


source_path = Path("src/bureau/runtime_refresh.py")
source = source_path.read_text(encoding="utf-8")

helper_anchor = r'''    return bind_digest(evidence, "evidence_sha256")


def _validate_no_run_acceptance_replay_binding(
'''
helper_replacement = r'''    return bind_digest(evidence, "evidence_sha256")


def _historical_no_run_closeout_acceptance_evidence(
    *,
    store: Any,
    closeout: dict[str, Any],
) -> dict[str, Any]:
    stable_closeout = {
        key: value
        for key, value in closeout.items()
        if key != "acceptance_evidence"
    }
    try:
        with store.connect() as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT revision,spec_sha256 FROM task_spec_revisions "
                    "WHERE task_id=? AND source=? ORDER BY revision",
                    (closeout["task_id"], "runtime-refresh-no-run-closeout"),
                )
            ]
    except (legacy.StateError, OSError, sqlite3.Error) as exc:
        raise RuntimeRefreshError(
            "authority-closeout-acceptance-history-invalid",
            "cannot read the immutable no-run closeout TaskSpec history",
            details={"error": str(exc)},
        ) from exc

    candidates: list[dict[str, Any]] = []
    for row in rows:
        try:
            historical_task_spec = store.task_spec_by_digest(
                closeout["task_id"], str(row["spec_sha256"])
            )
        except (legacy.StateError, OSError, sqlite3.Error) as exc:
            raise RuntimeRefreshError(
                "authority-closeout-acceptance-history-invalid",
                "cannot authenticate a historical no-run closeout TaskSpec revision",
                details={"revision": row["revision"], "error": str(exc)},
            ) from exc
        if (
            historical_task_spec is None
            or historical_task_spec.get("revision") != int(row["revision"])
        ):
            raise RuntimeRefreshError(
                "authority-closeout-acceptance-history-invalid",
                "historical no-run closeout revision does not match its TaskSpec digest",
                details={
                    "revision": row["revision"],
                    "task_spec_sha256": row["spec_sha256"],
                },
            )
        historical_spec = historical_task_spec.get("spec")
        historical_metadata = (
            historical_spec.get("metadata") if isinstance(historical_spec, dict) else None
        )
        historical_closeout_value = (
            historical_metadata.get("runtime_closeout")
            if isinstance(historical_metadata, dict)
            else None
        )
        if historical_closeout_value is None:
            continue
        try:
            historical_closeout = _validated_runtime_closeout(historical_closeout_value)
        except RuntimeRefreshError as exc:
            raise RuntimeRefreshError(
                "authority-closeout-acceptance-history-invalid",
                "historical no-run closeout revision contains invalid closeout evidence",
                details={"revision": row["revision"], "error": str(exc)},
            ) from exc
        historical_stable = {
            key: value
            for key, value in historical_closeout.items()
            if key != "acceptance_evidence"
        }
        if historical_stable == stable_closeout:
            candidates.append(
                {
                    "revision": int(row["revision"]),
                    "closeout": historical_closeout,
                }
            )

    if len(candidates) != 1:
        raise RuntimeRefreshError(
            "authority-closeout-acceptance-history-invalid",
            "no-run closeout has no unique immutable TaskSpec history anchor",
            details={
                "candidate_revisions": [item["revision"] for item in candidates],
            },
        )
    historical_acceptance_evidence = candidates[0]["closeout"].get(
        "acceptance_evidence"
    )
    if historical_acceptance_evidence is None:
        raise RuntimeRefreshError(
            "authority-closeout-acceptance-history-invalid",
            "typed no-run acceptance evidence has no immutable historical origin",
            details={"revision": candidates[0]["revision"]},
        )
    return historical_acceptance_evidence


def _validate_no_run_acceptance_replay_binding(
'''
source = replace_once(
    source,
    helper_anchor,
    helper_replacement,
    "runtime helper insertion",
)

validation_anchor = r'''    if historical_mismatched:
        raise RuntimeRefreshError(
            "authority-closeout-acceptance-task-spec-binding-invalid",
            "no-run acceptance evidence does not match its bound historical TaskSpec contract",
            details={
                "revision": historical_task_spec["revision"],
                "mismatched": historical_mismatched,
            },
        )


def _validated_runtime_closeout(value: Any) -> dict[str, Any]:
'''
validation_replacement = r'''    if historical_mismatched:
        raise RuntimeRefreshError(
            "authority-closeout-acceptance-task-spec-binding-invalid",
            "no-run acceptance evidence does not match its bound historical TaskSpec contract",
            details={
                "revision": historical_task_spec["revision"],
                "mismatched": historical_mismatched,
            },
        )

    historical_acceptance_evidence = _historical_no_run_closeout_acceptance_evidence(
        store=store,
        closeout=closeout,
    )
    history_mismatched = {
        key: {
            "historical": historical_acceptance_evidence.get(key),
            "acceptance_evidence": acceptance_evidence.get(key),
        }
        for key in sorted(
            set(historical_acceptance_evidence) | set(acceptance_evidence)
        )
        if historical_acceptance_evidence.get(key) != acceptance_evidence.get(key)
    }
    if history_mismatched:
        raise RuntimeRefreshError(
            "authority-closeout-acceptance-evidence-history-drift",
            (
                "stored no-run acceptance evidence no longer matches its immutable "
                "closeout revision"
            ),
            details={"mismatched": history_mismatched},
        )


def _validated_runtime_closeout(value: Any) -> dict[str, Any]:
'''
source = replace_once(
    source,
    validation_anchor,
    validation_replacement,
    "runtime history binding insertion",
)
source_path.write_text(source, encoding="utf-8")

tests_path = Path("tests/test_runtime_refresh.py")
tests = tests_path.read_text(encoding="utf-8")
test_anchor = r'''    assert caught.value.code == "authority-closeout-acceptance-evidence-binding-invalid"
    assert "runtime_result_sha256" in caught.value.details["mismatched"]
    assert store.task_spec(task_id) == before_replay
    assert store.list_runs() == []


def test_no_run_closeout_rejects_removed_acceptance_evidence_on_replay(
'''
test_replacement = r'''    assert caught.value.code == "authority-closeout-acceptance-evidence-binding-invalid"
    assert "runtime_result_sha256" in caught.value.details["mismatched"]
    assert store.task_spec(task_id) == before_replay
    assert store.list_runs() == []


@pytest.mark.parametrize(
    "field",
    [
        "run_evidence_sha256",
        "state_store_root_sha256",
        "effect_history_sha256",
    ],
)
def test_no_run_closeout_rejects_rewritten_acceptance_evidence_hash_on_replay(
    tmp_path: Path,
    field: str,
) -> None:
    task_id = f"BUREAU-NO-RUN-ACCEPTANCE-HISTORY-{field.replace('_', '-').upper()}"
    intent, store, result, resource_db = historical_no_run_success(tmp_path, task_id=task_id)
    release_test_leases(resource_db)
    closeout = refresh.closeout_runtime_refresh_authority(
        state_root=Path(intent["state_root"]),
        approval_task_id=task_id,
        target_sha256=intent["target_sha256"],
        intent_sha256=intent["intent_sha256"],
        result_sha256=result["result_sha256"],
        resource_db=resource_db,
        now=NOW + timedelta(minutes=20),
        authority_store=store,
        readback=lambda **_: result["readback"],
    )
    original = closeout["closeout"]["acceptance_evidence"][field]

    def rewrite_acceptance_evidence(spec: dict[str, Any]) -> None:
        runtime_closeout = spec["metadata"]["runtime_closeout"]
        evidence = json.loads(json.dumps(runtime_closeout["acceptance_evidence"]))
        evidence[field] = ("e" if original == "f" * 64 else "f") * 64
        evidence.pop("evidence_sha256")
        runtime_closeout["acceptance_evidence"] = refresh.bind_digest(
            evidence, "evidence_sha256"
        )

    revise_authority(
        store,
        task_id,
        rewrite_acceptance_evidence,
        key=f"tamper:acceptance-history:{field}",
    )
    before_replay = store.task_spec(task_id)

    with pytest.raises(refresh.RuntimeRefreshError) as caught:
        refresh.closeout_runtime_refresh_authority(
            state_root=Path(intent["state_root"]),
            approval_task_id=task_id,
            target_sha256=intent["target_sha256"],
            intent_sha256=intent["intent_sha256"],
            result_sha256=result["result_sha256"],
            resource_db=resource_db,
            now=NOW + timedelta(minutes=21),
            authority_store=store,
            readback=lambda **_: result["readback"],
        )

    assert caught.value.code == "authority-closeout-acceptance-evidence-history-drift"
    assert field in caught.value.details["mismatched"]
    assert store.task_spec(task_id) == before_replay
    assert store.list_runs() == []


def test_no_run_closeout_rejects_removed_acceptance_evidence_on_replay(
'''
tests = replace_once(
    tests,
    test_anchor,
    test_replacement,
    "runtime replay regression insertion",
)
tests_path.write_text(tests, encoding="utf-8")

Path(".github/workflows/tmp-pr2175-acceptance-replay-fix.yml").unlink()
Path(".github/scripts/tmp_pr2175_acceptance_replay_fix.py").unlink()
