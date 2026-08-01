from __future__ import annotations

from pathlib import Path

path = Path("src/bureau/broad_scope_dispatcher.py")
text = path.read_text(encoding="utf-8")

signature = '''    def claim_intent(
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
signature_with_break_glass = '''    def claim_intent(
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
if text.count(signature) != 1:
    raise SystemExit(
        "expected one public Dispatcher.claim_intent wrapper signature, "
        f"observed {text.count(signature)}"
    )
text = text.replace(signature, signature_with_break_glass, 1)

forward = '''                base_dir=base_dir,
                approved=approved,
                approval_source=approval_source,
'''
forward_with_break_glass = '''                base_dir=base_dir,
                approved=approved,
                break_glass=break_glass,
                approval_source=approval_source,
'''
if text.count(forward) != 1:
    raise SystemExit(
        "expected one public Dispatcher approval forwarding block, "
        f"observed {text.count(forward)}"
    )
text = text.replace(forward, forward_with_break_glass, 1)
path.write_text(text, encoding="utf-8")

print("Forwarded break-glass through the public Dispatcher wrapper.")
