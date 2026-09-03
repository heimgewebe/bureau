from pathlib import Path

source_path = Path("src/bureau/operator_intake.py")
source = source_path.read_text()
old = '''        if (
            before_strong_identity is not None
            and after_strong_identity is None
            and len(after_raw_tokens) > 1
            and after_raw_tokens[1] == before_strong_identity
        ):
            after_raw_tokens = after_raw_tokens[1:]
        elif (
            after_strong_identity is not None
            and before_strong_identity is None
            and len(before_raw_tokens) > 1
            and before_raw_tokens[1] == after_strong_identity
        ):
            before_raw_tokens = before_raw_tokens[1:]
'''
new = '''        if (
            before_strong_identity is not None
            and after_strong_identity is None
            and len(after_raw_tokens) > 1
            and after_raw_tokens[0] in _TASK_REVISION_GENERIC_TOKENS
            and after_raw_tokens[1] == before_strong_identity
        ):
            after_raw_tokens = after_raw_tokens[1:]
        elif (
            after_strong_identity is not None
            and before_strong_identity is None
            and len(before_raw_tokens) > 1
            and before_raw_tokens[0] in _TASK_REVISION_GENERIC_TOKENS
            and before_raw_tokens[1] == after_strong_identity
        ):
            before_raw_tokens = before_raw_tokens[1:]
'''
count = source.count(old)
if count != 1:
    raise SystemExit(f"expected one relocation preimage, got {count}")
source_path.write_text(source.replace(old, new))

test_path = Path("tests/test_operator_intake.py")
tests = test_path.read_text()
marker = "def test_task_revision_identity_guard_rejects_subject_prefix_before_relocated_identifier"
if marker in tests:
    raise SystemExit("relocation-prefix regression already present")
tests += '''


def test_task_revision_identity_guard_rejects_subject_prefix_before_relocated_identifier() -> None:
    before = _identity_revision_task(
        title="API backup updater service",
        resource="repo.infra",
        goal="Restore backup API health",
    )
    after = _identity_revision_task(
        title="Dashboard API updater service",
        resource="repo.infra",
        goal="Render customer dashboard data",
    )
    with pytest.raises(OperatorIntakeError) as raised:
        operator_intake_module._validate_task_revision_identity_continuity(before, after)
    assert raised.value.code == "task-revision-identity-discontinuity"
'''
test_path.write_text(tests)
