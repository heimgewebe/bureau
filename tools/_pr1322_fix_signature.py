from __future__ import annotations

from pathlib import Path

path = Path("src/bureau/v2.py")
text = path.read_text(encoding="utf-8")

accidental = '''        approved: bool = False,
        break_glass: bool = False,
        approval_source: str = "coordinated claim intent",
'''
original = '''        approved: bool = False,
        approval_source: str = "coordinated claim intent",
'''

if text.count(accidental) != 1:
    raise SystemExit(
        "expected exactly one accidental generic signature replacement, "
        f"observed {text.count(accidental)}"
    )
text = text.replace(accidental, original, 1)

claim_signature = '''    def claim_intent(
        self,
        worker_id: str,
        capabilities: tuple[str, ...],
        kind: str = "interactive-agent",
        *,
        task_id: str | None = None,
        resource: str | None = None,
        base_dir: Path | None = None,
        approved: bool = False,
        approval_source: str = "coordinated claim intent",
    ) -> dict[str, Any]:
'''
claim_signature_with_break_glass = '''    def claim_intent(
        self,
        worker_id: str,
        capabilities: tuple[str, ...],
        kind: str = "interactive-agent",
        *,
        task_id: str | None = None,
        resource: str | None = None,
        base_dir: Path | None = None,
        approved: bool = False,
        break_glass: bool = False,
        approval_source: str = "coordinated claim intent",
    ) -> dict[str, Any]:
'''

if text.count(claim_signature) != 1:
    raise SystemExit(
        "expected exactly one effective Dispatcher.claim_intent signature, "
        f"observed {text.count(claim_signature)}"
    )
text = text.replace(claim_signature, claim_signature_with_break_glass, 1)
path.write_text(text, encoding="utf-8")

print("Corrected the exact Dispatcher.claim_intent break-glass signature.")
