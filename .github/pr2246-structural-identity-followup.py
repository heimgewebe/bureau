from pathlib import Path

source_path = Path("src/bureau/operator_intake.py")
source = source_path.read_text()


def replace_once(old: str, new: str) -> None:
    global source
    count = source.count(old)
    if count != 1:
        raise SystemExit(f"expected one source match, got {count}: {old[:120]!r}")
    source = source.replace(old, new)


replace_once(
    '''def _task_revision_plural_equivalent(before_token: str, after_token: str) -> bool:
    if before_token == after_token:
        return False
    return (
        _task_revision_plural_base(before_token) == after_token
        or _task_revision_plural_base(after_token) == before_token
    )
''',
    '''def _task_revision_plural_equivalent(before_token: str, after_token: str) -> bool:
    if before_token == after_token:
        return False
    # Pairwise `+es` handling is safer than another stemming suffix table because
    # the candidate singular is known. It covers bus/buses, status/statuses,
    # hero/heroes and tomato/tomatoes without turning case/cases into cas/cases.
    for plural, singular in (
        (before_token, after_token),
        (after_token, before_token),
    ):
        if singular.endswith(("s", "o")) and plural == f"{singular}es":
            return True
    return (
        _task_revision_plural_base(before_token) == after_token
        or _task_revision_plural_base(after_token) == before_token
    )
''',
)

replace_once(
    '''def _task_revision_strong_leading_identity(value: Any) -> str | None:
    raw_tokens = _TASK_REVISION_TOKEN_RE.findall(str(value or "").strip())
    if len(raw_tokens) <= 1:
        return None
    token = raw_tokens[0]
    # A leading repository/package/path-like identifier is subject evidence even
    # though the generic prose model normally treats position zero as an action
    # slot. Keep short all-caps domain acronyms (API/SSH/MCP/CI) for the same
    # reason, but never promote known process words merely because they are cased.
    if any(char.isdigit() or char in "./:@+#" for char in token):
        return token.casefold()
    # A single hyphen is common in process terms such as `pre-check`; reserve
    # hyphen-only promotion for multi-segment technical compounds.
    if token.count("-") >= 2:
        return token.casefold()
    letters = "".join(char for char in token if char.isalpha())
    if (
        2 <= len(letters) <= 4
        and letters.isupper()
        and token.casefold() not in _TASK_REVISION_GENERIC_TOKENS
    ):
        return token.casefold()
    return None
''',
    '''def _task_revision_strong_token_identity(token: str) -> str | None:
    # Repository/package/path identifiers and short all-caps domain acronyms are
    # strong identity evidence. Keep this token-local so the same rule can inspect
    # the first subject token after an optional action prefix.
    if any(char.isdigit() or char in "./:@+#" for char in token):
        return token.casefold()
    if token.count("-") >= 2:
        return token.casefold()
    letters = "".join(char for char in token if char.isalpha())
    if (
        2 <= len(letters) <= 4
        and letters.isupper()
        and token.casefold() not in _TASK_REVISION_GENERIC_TOKENS
    ):
        return token.casefold()
    return None


def _task_revision_strong_leading_identity(value: Any) -> str | None:
    raw_tokens = _TASK_REVISION_TOKEN_RE.findall(str(value or "").strip())
    if len(raw_tokens) <= 1:
        return None
    return _task_revision_strong_token_identity(raw_tokens[0])


def _task_revision_subject_leading_identity(value: Any) -> str | None:
    raw_tokens = _TASK_REVISION_TOKEN_RE.findall(str(value or "").strip())
    if not raw_tokens:
        return None
    index = 0
    if len(raw_tokens) > 1 and raw_tokens[0].casefold() in _TASK_REVISION_GENERIC_TOKENS:
        index = 1
    return _task_revision_strong_token_identity(raw_tokens[index])
''',
)

replace_once(
    '''    before_strong_leading_identity = _task_revision_strong_leading_identity(before)
    after_strong_leading_identity = _task_revision_strong_leading_identity(after)
    retained_strong_leading_identity = (
        before_strong_leading_identity is not None
        and before_strong_leading_identity == after_strong_leading_identity
    )
''',
    '''    before_strong_leading_identity = _task_revision_subject_leading_identity(before)
    after_strong_leading_identity = _task_revision_subject_leading_identity(after)
    retained_strong_leading_identity = (
        before_strong_leading_identity is not None
        and before_strong_leading_identity == after_strong_leading_identity
    )
    technical_identity_mismatch = (
        before_strong_leading_identity != after_strong_leading_identity
        and (
            before_strong_leading_identity is not None
            or after_strong_leading_identity is not None
        )
    )
    before_all_tokens = _task_revision_tokens(before)
    after_all_tokens = _task_revision_tokens(after)
    leading_token_changed = bool(before_all_tokens and after_all_tokens) and (
        before_all_tokens[0] != after_all_tokens[0]
    )
''',
)

replace_once(
    '''    return continuous, {
        "continuity": round(continuity, 6),
''',
    '''    if technical_identity_mismatch:
        continuous = False
    return continuous, {
        "continuity": round(continuity, 6),
''',
)

replace_once(
    '''        "retained_strong_leading_identity": retained_strong_leading_identity,
        "before_subject_sequence": before_subject,
''',
    '''        "retained_strong_leading_identity": retained_strong_leading_identity,
        "technical_identity_mismatch": technical_identity_mismatch,
        "leading_token_changed": leading_token_changed,
        "before_subject_sequence": before_subject,
''',
)

replace_once(
    '''    if any(
        max(
            int(evidence["shorter_subject_token_count"]),
            int(evidence["full_shorter_subject_token_count"]),
        )
        >= _TASK_REVISION_SUBJECT_OVERLAP_MIN
        for evidence in continuous_evidence
    ):
        return
''',
    '''    if any(
        max(
            int(evidence["shorter_subject_token_count"]),
            int(evidence["full_shorter_subject_token_count"]),
        )
        >= _TASK_REVISION_SUBJECT_OVERLAP_MIN
        and (
            not evidence["leading_token_changed"]
            or bool(exact_acceptance_ids)
        )
        for evidence in continuous_evidence
    ):
        return
''',
)

source_path.write_text(source)

test_path = Path("tests/test_operator_intake.py")
tests = test_path.read_text()
marker = "def test_task_revision_identity_guard_rejects_task_prose_predicate_inversion"
if marker in tests:
    raise SystemExit("structural identity regressions already present")
tests += '''


def test_task_revision_identity_guard_rejects_task_prose_predicate_inversion() -> None:
    before = _identity_revision_task(
        title="Backup retention archive",
        resource="repo.shared",
        goal="Disable customer data deletion",
    )
    after = _identity_revision_task(
        title="Billing dashboard export",
        resource="repo.shared",
        goal="Enable customer data deletion",
    )
    with pytest.raises(OperatorIntakeError) as raised:
        operator_intake_module._validate_task_revision_identity_continuity(before, after)
    assert raised.value.code == "task-revision-identity-discontinuity"


def test_task_revision_identity_guard_rejects_reordered_replaced_technical_identifier() -> None:
    before = _identity_revision_task(
        title="API runtime cleanup",
        resource="repo.shared",
        goal="Repair API runtime state",
    )
    after = _identity_revision_task(
        title="SSH cleanup runtime",
        resource="repo.shared",
        goal="Replace remote shell state",
    )
    with pytest.raises(OperatorIntakeError) as raised:
        operator_intake_module._validate_task_revision_identity_continuity(before, after)
    assert raised.value.code == "task-revision-identity-discontinuity"


def test_task_revision_identity_guard_rejects_retained_identifier_behind_action_suffix_swap() -> None:
    before = _identity_revision_task(
        title="Repair API backup updater service",
        resource="repo.infra",
        goal="Restore backup API health",
    )
    after = _identity_revision_task(
        title="Repair API database updater service",
        resource="repo.infra",
        goal="Repair database API state",
    )
    with pytest.raises(OperatorIntakeError) as raised:
        operator_intake_module._validate_task_revision_identity_continuity(before, after)
    assert raised.value.code == "task-revision-identity-discontinuity"


@pytest.mark.parametrize(
    ("plural", "singular"),
    [
        ("buses", "bus"),
        ("statuses", "status"),
        ("heroes", "hero"),
        ("tomatoes", "tomato"),
    ],
)
def test_task_revision_identity_guard_allows_pairwise_es_plural(
    plural: str, singular: str
) -> None:
    before = _identity_revision_task(
        title=f"Improve game {plural}",
        resource="repo.shared",
        acceptance_ids=("subject-contract",),
    )
    after = _identity_revision_task(
        title=f"Improve game {singular}",
        resource="repo.shared",
        acceptance_ids=("subject-contract",),
    )
    operator_intake_module._validate_task_revision_identity_continuity(before, after)
'''
test_path.write_text(tests)
