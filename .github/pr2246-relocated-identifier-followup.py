from pathlib import Path

path = Path("src/bureau/operator_intake.py")
source = path.read_text()
old = '''    if ignore_leading_action:
        if (
            len(before_raw_tokens) > 1
            and _task_revision_strong_leading_identity(before) is None
        ):
            before_raw_tokens = before_raw_tokens[1:]
        if (
            len(after_raw_tokens) > 1
            and _task_revision_strong_leading_identity(after) is None
        ):
            after_raw_tokens = after_raw_tokens[1:]
    before_tokens = {
'''
new = '''    if ignore_leading_action:
        if (
            len(before_raw_tokens) > 1
            and _task_revision_strong_leading_identity(before) is None
        ):
            before_raw_tokens = before_raw_tokens[1:]
        if (
            len(after_raw_tokens) > 1
            and _task_revision_strong_leading_identity(after) is None
        ):
            after_raw_tokens = after_raw_tokens[1:]
    else:
        # Preserve the full-token corroboration boundary, but do not penalize a
        # strong technical identity merely because a rewrite moves that exact
        # identity behind one new leading action token. This is deliberately
        # asymmetric and exact: removing or replacing the identifier does not
        # qualify and therefore still falls through to the ordinary full view.
        before_strong_identity = _task_revision_strong_leading_identity(before)
        after_strong_identity = _task_revision_strong_leading_identity(after)
        if (
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
    before_tokens = {
'''
count = source.count(old)
if count != 1:
    raise SystemExit(f"expected exactly one text-evidence preimage, got {count}")
path.write_text(source.replace(old, new))
