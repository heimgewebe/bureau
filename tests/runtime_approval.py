from __future__ import annotations

import hashlib
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bureau import runtime_refresh as refresh

TASK_ID = "BUR-2026-003-T009"


def write_runtime_approval_intent(
    source: Path,
    directory: Path,
    *,
    label: str,
    expires_at: datetime | None = None,
) -> Path:
    source_head = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    reference = hashlib.sha256(f"{source_head}:{label}".encode()).hexdigest()
    evidence = refresh.approval.break_glass_approval(
        source=f"test:{label}",
        approved=True,
        reviewer="pytest",
        reference=reference,
        task_id=TASK_ID,
        scope="runtime_mutation",
    )
    decision = refresh.approval.require_approval(
        "runtime_mutation",
        evidence,
        expected_reference=reference,
        task_id=TASK_ID,
    )
    now = datetime.now(timezone.utc)
    intent = refresh.bind_digest(
        {
            "schema_version": 1,
            "kind": "bureau_runtime_refresh_intent",
            "main_commit": source_head,
            "target_sha256": reference,
            "approval_task_id": TASK_ID,
            "runtime_approval": decision,
            "created_at": refresh.isoformat(now),
            "expires_at": refresh.isoformat(expires_at or now + timedelta(hours=1)),
        },
        "intent_sha256",
    )
    path = directory / f"runtime-approval-{label}.json"
    path.write_bytes(refresh.canonical_bytes(intent))
    return path
