from __future__ import annotations

import json
from pathlib import Path

from bureau.legacy import sha256_json
from bureau.v2 import task_revision_sha256

ROOT = Path(__file__).resolve().parents[1]
TASK_ID = "OPERATOR-ECOSYSTEM-REDUNDANCY-V1-T026"
INITIATIVE_ID = "OPERATOR-ECOSYSTEM-REDUNDANCY-V1"

task = json.loads(
    (ROOT / "registry" / "tasks" / f"{TASK_ID}.json").read_text(encoding="utf-8")
)
initiative = json.loads(
    (ROOT / "registry" / "initiatives" / f"{INITIATIVE_ID}.json").read_text(
        encoding="utf-8"
    )
)

raise RuntimeError(
    "T026_HASH_PROBE "
    f"task_sha256={task_revision_sha256(task)} "
    f"plan_sha256={sha256_json(initiative.get('current_plan') or {})}"
)
