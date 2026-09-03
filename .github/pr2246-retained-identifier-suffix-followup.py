from pathlib import Path

source_path = Path("src/bureau/operator_intake.py")
source = source_path.read_text()
old = '''    shorter_subject_sequence_count = min(len(before_subject), len(after_subject))
    all_overlap_is_shared_suffix = shared_subject_suffix_count == subject_overlap
    # Position zero is deliberately action-agnostic: an unseen process verb must
    # not become identity evidence just because a static weak-token list omitted
    # it. If every surviving overlap is merely a trailing suffix and another
    # subject token changed, require independent goal/acceptance evidence instead.
    # Explicit identifiers/acronyms are the narrow exception because the tokenizer
    # already treats them as atomic subject identities.
    shared_suffix_only_collision = (
        not retained_strong_leading_identity
        and shared_subject_suffix_count >= _TASK_REVISION_SUBJECT_OVERLAP_MIN
        and shared_subject_suffix_count < shorter_subject_sequence_count
        and all_overlap_is_shared_suffix
    )
'''
new = '''    shorter_subject_sequence_count = min(len(before_subject), len(after_subject))
    retained_strong_identity_overlap = int(
        retained_strong_leading_identity
        and before_strong_leading_identity in before_subject
        and after_strong_leading_identity in after_subject
    )
    non_identity_subject_overlap = max(
        0, subject_overlap - retained_strong_identity_overlap
    )
    all_non_identity_overlap_is_shared_suffix = (
        shared_subject_suffix_count == non_identity_subject_overlap
    )
    # Position zero is deliberately action-agnostic: an unseen process verb must
    # not become identity evidence just because a static weak-token list omitted
    # it. A retained technical identifier is useful evidence, but it cannot by
    # itself legitimize a changed subject when every *other* surviving token is
    # merely a shared trailing suffix. This keeps exact API/package identity as
    # corroboration without letting `API backup updater service` become
    # `API database updater service` under the same permanent task id.
    shared_suffix_only_collision = (
        shared_subject_suffix_count >= _TASK_REVISION_SUBJECT_OVERLAP_MIN
        and shared_subject_suffix_count < shorter_subject_sequence_count
        and all_non_identity_overlap_is_shared_suffix
    )
'''
count = source.count(old)
if count != 1:
    raise SystemExit(f"expected one suffix-collision preimage, got {count}")
source = source.replace(old, new)
old_details = '''        "all_overlap_is_shared_suffix": all_overlap_is_shared_suffix,
        "shared_suffix_only_collision": shared_suffix_only_collision,
'''
new_details = '''        "retained_strong_identity_overlap": retained_strong_identity_overlap,
        "non_identity_subject_overlap": non_identity_subject_overlap,
        "all_non_identity_overlap_is_shared_suffix": all_non_identity_overlap_is_shared_suffix,
        "shared_suffix_only_collision": shared_suffix_only_collision,
'''
count = source.count(old_details)
if count != 1:
    raise SystemExit(f"expected one evidence-details preimage, got {count}")
source_path.write_text(source.replace(old_details, new_details))

test_path = Path("tests/test_operator_intake.py")
tests = test_path.read_text()
marker = "def test_task_revision_identity_guard_rejects_retained_broad_identifier_suffix_swap"
if marker in tests:
    raise SystemExit("retained-identifier suffix regression already present")
tests += '''


def test_task_revision_identity_guard_rejects_retained_broad_identifier_suffix_swap() -> None:
    before = _identity_revision_task(
        title="API backup updater service",
        resource="repo.infra",
        goal="Restore backup API health",
    )
    after = _identity_revision_task(
        title="API database updater service",
        resource="repo.infra",
        goal="Repair database API state",
    )
    with pytest.raises(OperatorIntakeError) as raised:
        operator_intake_module._validate_task_revision_identity_continuity(before, after)
    assert raised.value.code == "task-revision-identity-discontinuity"
'''
test_path.write_text(tests)
