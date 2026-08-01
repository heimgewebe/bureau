from __future__ import annotations

from pathlib import Path

path = Path("tests/test_v2.py")
text = path.read_text(encoding="utf-8")
old = '''    intent["workspace"]["baseline_commit"] = "0" * 40
    intent["intent_sha256"] = bureau_v2.coordinated_claim_intent_sha256(intent)
    binding, database = coordinated_lease_database(tmp_path / "leases", intent)
    with pytest.raises(StateError, match="workspace changed after intent"):
        dispatcher.commit_claim_intent(intent, binding, resource_db=database)
'''
new = '''    intent["workspace"]["baseline_commit"] = "0" * 40
    intent["intent_sha256"] = bureau_v2.coordinated_claim_intent_sha256(intent)
    binding, database = coordinated_lease_database(tmp_path / "leases", intent)
    with pytest.raises(StateError, match="intent differs from issued identity"):
        dispatcher.commit_claim_intent(intent, binding, resource_db=database)
'''
if text.count(old) != 1:
    raise SystemExit(
        "expected one rehashed workspace baseline tamper assertion, "
        f"observed {text.count(old)}"
    )
path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("Aligned rehashed workspace tamper expectation with issuance authority.")
