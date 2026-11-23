"""Import TaskSpecs from merged ``main`` into the authoritative StateStore.

A TaskSpec that reached ``main`` through a protected, CI-checked merge is
reviewed. Before this step it still stayed ``git_projection_only`` (invisible to
dispatch) until a separate operator intake ceremony published it.

``intake_merged_task_specs`` runs inside ``bureau reconcile`` when the registry
is the installed canonical runtime snapshot (that is exactly the merged ``main``
the runtime refresh deployed). For every Git TaskSpec that is missing from the
StateStore it uses ``StateStore.seed_missing_registry_task_spec`` (missing-only,
never overwrites an existing revision). Properties:

* idempotent: a repeat finds nothing missing and changes nothing;
* per-task: a conflict is reported for that task only, the others still import;
* references first: a task is imported only once its ``depends_on`` and
  ``metadata.parent_task`` exist in the StateStore (or are imported in the same
  pass), so the authoritative overlay never sees a dangling reference;
  mutually referencing tasks (a parent depending on its child) are imported
  together in one transaction;
* lifecycle unchanged: the TaskSpec keeps its own ``state``; ``ready`` becomes
  claimable through the normal gates, ``planned`` stays planned.

``registry/intake-baseline.json`` may list historical Git-only TaskSpecs that
were deliberately never published; they are skipped until removed from it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import legacy, task_specs

BASELINE_PATH = Path("registry/intake-baseline.json")
SCHEMA_VERSION = 1


def load_baseline(root: Path) -> frozenset[str]:
    path = root / BASELINE_PATH
    if not path.is_file():
        return frozenset()
    value = json.loads(path.read_text(encoding="utf-8"))
    ids = value.get("task_ids") if isinstance(value, dict) else None
    if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
        raise legacy.StateError(f"invalid intake baseline: {path}")
    return frozenset(ids)


def _references(task: Any) -> set[str]:
    refs = {str(item) for item in getattr(task, "depends_on", ()) or ()}
    metadata = task.raw.get("metadata") if isinstance(task.raw, dict) else None
    parent = metadata.get("parent_task") if isinstance(metadata, dict) else None
    if isinstance(parent, str) and parent:
        refs.add(parent)
    return refs


def intake_merged_task_specs(registry: Any, store: Any) -> dict[str, Any]:
    """Seed missing merged-main TaskSpecs into ``store``; never overwrite."""
    with store.connect() as connection:
        existing = set(task_specs.current_projection(connection)["tasks"])
    if not existing:
        # Pure Git bootstrap: dispatch already reads Git directly.
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "bureau_merged_task_intake",
            "status": "skipped",
            "reason": "state-store-has-no-task-specs",
            "imported": [],
            "conflicts": [],
        }
    baseline = load_baseline(Path(registry.root))
    candidates = {
        task_id: task
        for task_id, task in registry.tasks.items()
        if task_id not in existing and task_id not in baseline
    }
    imported: list[dict[str, Any]] = []
    conflicts: list[dict[str, str]] = []
    available = set(existing)
    pending = dict(sorted(candidates.items()))
    progress = True
    while pending and progress:
        progress = False
        for task_id, task in list(pending.items()):
            missing = _references(task) - available
            if missing - set(pending):
                conflicts.append(
                    {
                        "task_id": task_id,
                        "code": "unknown-reference",
                        "detail": "references not in StateStore or merged main: "
                        + ", ".join(sorted(missing - set(pending))),
                    }
                )
                del pending[task_id]
                progress = True
                continue
            if missing:
                continue  # wait until the referenced task is imported in this pass
            try:
                result = store.seed_missing_registry_task_spec(registry, task_id)
            except (legacy.StateError, task_specs.TaskSpecError) as exc:
                conflicts.append(
                    {"task_id": task_id, "code": "seed-refused", "detail": str(exc)}
                )
            else:
                imported.append(
                    {
                        "task_id": task_id,
                        "state": task.state,
                        "revision": result.get("revision"),
                        "spec_sha256": result.get("spec_sha256"),
                        "changed": bool(result.get("changed")),
                    }
                )
                available.add(task_id)
            del pending[task_id]
            progress = True
    if pending:
        # Only mutually referencing tasks remain (e.g. a parent that depends on
        # its own child). Every reference points into the group or into the
        # StateStore, so import the group in one transaction.
        group = sorted(pending)
        try:
            with store.immediate() as connection:
                results = [
                    task_specs.seed_missing_registry_task(connection, registry, task_id)
                    for task_id in group
                ]
        except (legacy.StateError, task_specs.TaskSpecError) as exc:
            conflicts.extend(
                {"task_id": task_id, "code": "seed-refused", "detail": str(exc)}
                for task_id in group
            )
        else:
            for task_id, result in zip(group, results, strict=True):
                imported.append(
                    {
                        "task_id": task_id,
                        "state": pending[task_id].state,
                        "revision": result.get("revision"),
                        "spec_sha256": result.get("spec_sha256"),
                        "changed": bool(result.get("changed")),
                        "atomic_group": group,
                    }
                )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "bureau_merged_task_intake",
        "status": "conflicts" if conflicts else ("imported" if imported else "unchanged"),
        "baseline_skipped": len([t for t in registry.tasks if t in baseline and t not in existing]),
        "imported": imported,
        "conflicts": conflicts,
        "does_not_establish": [
            "claimability beyond the normal claim gates",
            "overwrite of existing StateStore TaskSpec revisions",
        ],
    }
