from __future__ import annotations

from pathlib import Path

path = Path("tests/test_claim_guard.py")
text = path.read_text(encoding="utf-8")
old = '''    with pytest.raises(
        bureau_v2.legacy.StateError,
        match="unexpected open PR nonconflict evidence",
    ):
        dispatcher.commit_claim_intent(intent, None)
'''
new = '''    with pytest.raises(
        bureau_v2.legacy.StateError,
        match="intent differs from issued identity",
    ):
        dispatcher.commit_claim_intent(intent, None)
'''
if text.count(old) != 1:
    raise SystemExit(
        "expected one rehashed nonconflict-evidence assertion, "
        f"observed {text.count(old)}"
    )
path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("Aligned nonconflict tamper expectation with issuance authority.")
