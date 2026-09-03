from pathlib import Path

source_path = Path("src/bureau/operator_intake.py")
source = source_path.read_text()
old = '''    # A single subject token is intentionally a weak continuity signal. It is
    # accepted only when at least one typed acceptance criterion contract also
    # remains continuous, so an unseen one-word process label such as "Upgrade"
    # cannot carry identity by
    # itself while compact real subjects such as "contracts" remain revisable.
    if continuous_evidence and continuous_acceptance_ids:
        return
'''
new = '''    # A single subject token is intentionally a weak continuity signal. It is
    # accepted only when at least one complete typed acceptance criterion remains
    # exactly unchanged. Semantic assertion similarity is not enough here: an
    # inverted predicate can retain nearly all tokens while reversing the contract.
    # This keeps compact real subjects such as "contracts" revisable without
    # allowing a weak text anchor plus acceptance-word overlap to reuse an identity.
    if continuous_evidence and exact_acceptance_ids:
        return
'''
count = source.count(old)
if count != 1:
    raise SystemExit(f"expected one weak-acceptance preimage, got {count}")
source_path.write_text(source.replace(old, new))

test_path = Path("tests/test_operator_intake.py")
tests = test_path.read_text()
marker = "def test_task_revision_identity_guard_rejects_predicate_inversion_as_weak_subject_support"
if marker in tests:
    raise SystemExit("weak-acceptance regression already present")
tests += '''


def test_task_revision_identity_guard_rejects_predicate_inversion_as_weak_subject_support() -> None:
    before = _identity_revision_task(
        title="contracts",
        resource="repo.shared",
        goal="Restore archived snapshots",
    )
    after = _identity_revision_task(
        title="contracts",
        resource="repo.shared",
        goal="Render billing balances",
    )
    shared_contract = {
        "evidence_type": "object",
        "verifier": "manual_observation",
        "verifier_config": {"observation_scope": "shared-contract"},
    }
    before["acceptance"] = [
        {
            "id": "proof-a",
            "assertion": "Verify backup policy prevents customer data deletion in production",
            **shared_contract,
        }
    ]
    after["acceptance"] = [
        {
            **before["acceptance"][0],
            "assertion": "Verify backup policy permits customer data deletion in production",
        }
    ]
    with pytest.raises(OperatorIntakeError) as raised:
        operator_intake_module._validate_task_revision_identity_continuity(before, after)
    assert raised.value.code == "task-revision-identity-discontinuity"
'''
test_path.write_text(tests)
