from pathlib import Path

path = Path("tests/test_operator_intake.py")
text = path.read_text()
old = "def test_task_revision_identity_guard_rejects_retained_identifier_behind_action_suffix_swap() -> None:\n"
new = (
    "def test_task_revision_identity_guard_rejects_retained_identifier_behind_action_suffix_swap(\n"
    ") -> None:\n"
)
count = text.count(old)
if count != 1:
    raise SystemExit(f"expected exactly one lint preimage, got {count}")
path.write_text(text.replace(old, new))
