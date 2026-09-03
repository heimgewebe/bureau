from pathlib import Path

source_path = Path("src/bureau/operator_intake.py")
source = source_path.read_text()


def replace_once(old: str, new: str) -> None:
    global source
    count = source.count(old)
    if count != 1:
        raise SystemExit(f"expected one source match, got {count}: {old[:100]!r}")
    source = source.replace(old, new)


replace_once(
    '    if token.endswith("s") and not token.endswith(("ss", "us", "is", "as", "os")):\n',
    '    if token.endswith("s") and not token.endswith(("ss", "us", "is")):\n',
)

replace_once(
    '''def _task_revision_subject_sequence(value: Any) -> list[str]:
    tokens = _task_revision_tokens(value)
    if len(tokens) > 1 and _task_revision_strong_leading_identity(value) is None:
        tokens = tokens[1:]
    return [token for token in tokens if token not in _TASK_REVISION_GENERIC_TOKENS]
''',
    '''def _task_revision_raw_subject_sequence(value: Any) -> list[str]:
    tokens = _TASK_REVISION_TOKEN_RE.findall(str(value or "").strip())
    if len(tokens) > 1 and _task_revision_strong_leading_identity(value) is None:
        tokens = tokens[1:]
    return [
        token
        for token in tokens
        if token.casefold() not in _TASK_REVISION_GENERIC_TOKENS
    ]


def _task_revision_subject_sequence(value: Any) -> list[str]:
    return [token.casefold() for token in _task_revision_raw_subject_sequence(value)]
''',
)

replace_once(
    '''    before_subject = _task_revision_subject_sequence(before)
    after_subject = _task_revision_subject_sequence(after)
    return (
        len(before_subject) == len(after_subject)
        and len(before_subject) >= 1
        and before_subject[:-1] == after_subject[:-1]
        and _task_revision_plural_equivalent(before_subject[-1], after_subject[-1])
    )
''',
    '''    before_raw_subject = _task_revision_raw_subject_sequence(before)
    after_raw_subject = _task_revision_raw_subject_sequence(after)
    before_subject = [token.casefold() for token in before_raw_subject]
    after_subject = [token.casefold() for token in after_raw_subject]
    return (
        len(before_subject) == len(after_subject)
        and len(before_subject) >= 1
        and before_subject[:-1] == after_subject[:-1]
        # The broad regular-s lane is prose-only. Preserve original token case so
        # title-cased technology/product identifiers such as `Rails`/`Rail` cannot
        # collapse after the ordinary comparison view is case-folded. Lower-case
        # nouns still use pairwise inflection, including schemas/schema and logos/logo.
        and before_raw_subject[-1].islower()
        and after_raw_subject[-1].islower()
        and _task_revision_plural_equivalent(before_subject[-1], after_subject[-1])
    )
''',
)

source_path.write_text(source)

test_path = Path("tests/test_operator_intake.py")
tests = test_path.read_text()
marker = "def test_task_revision_identity_guard_rejects_title_cased_identifier_as_plural"
if marker in tests:
    raise SystemExit("inflection follow-up tests already present")

tests += '''


def test_task_revision_identity_guard_rejects_title_cased_identifier_as_plural() -> None:
    before = _identity_revision_task(
        title="Maintain Ruby Rails",
        resource="repo.shared",
        goal="Serve HTTP application framework",
    )
    after = _identity_revision_task(
        title="Maintain Ruby Rail",
        resource="repo.shared",
        goal="Track locomotive infrastructure",
    )
    with pytest.raises(OperatorIntakeError) as raised:
        operator_intake_module._validate_task_revision_identity_continuity(before, after)
    assert raised.value.code == "task-revision-identity-discontinuity"


@pytest.mark.parametrize(
    ("plural", "singular"),
    [("schemas", "schema"), ("logos", "logo")],
)
def test_task_revision_identity_guard_allows_lowercase_as_os_regular_plural(
    plural: str, singular: str
) -> None:
    before = _identity_revision_task(
        title=f"Improve database {plural}",
        resource="repo.database",
        acceptance_ids=("database-contract",),
    )
    after = _identity_revision_task(
        title=f"Improve database {singular}",
        resource="repo.database",
        acceptance_ids=("database-contract",),
    )
    operator_intake_module._validate_task_revision_identity_continuity(before, after)
'''

test_path.write_text(tests)
