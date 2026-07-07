# BUR-2026-004-T003 Legacy Module Dependency Audit

Status: closes BUR-2026-004-T003.

## Purpose

Audit current dependencies on src/bureau/legacy.py before any compatibility reduction. This is a read-only audit and changes no runtime behavior.

## Summary decision

Decision: keep legacy.py for now; reduce it only by extraction.

legacy.py is still used for registry dataclasses, public compatibility names, state-store compatibility, dispatcher-era semantics, JSON/hash/time helpers, and run evidence compatibility. It should not be removed in one step.

## Import map

Direct imports:

| Consumer | Dependencies | Role |
|---|---|---|
| src/bureau/core.py | errors, dataclasses, conflict helpers, JSON/hash/time helpers | public compatibility facade |
| src/bureau/weltgewebe_source.py | ValidationError, atomic_write, canonical_json, read_json, sha256_json | source import/sync helper |

Module references:

| Consumer | Representative references | Role |
|---|---|---|
| src/bureau/v2.py | Registry, Task, Reservation, StateError, ACTIVE_STATES, atomic_write, read_json, sha256_json, utc_now, canonical_json, Dispatcher | v2 still depends on legacy data and helper semantics |
| src/bureau/github_observer.py | Registry, parse_time | GitHub observation still accepts the existing registry shape and time parser |

CLI references go through bureau.core. Commands still expose Registry, StateStore, Dispatcher, complete_run, fail_run, grabowski_handoff and create_workspace under stable public names.

## Test map

| Test file | Why it matters |
|---|---|
| tests/test_bureau.py | original public API, dispatcher, completion, handoff and workspace flows |
| tests/test_claim_guard.py | claim and reservation compatibility |
| tests/test_closure_bridge.py | registry and dispatcher integration |
| tests/test_state_root_hygiene.py | state-root legacy artifact classification |
| tests/test_v2.py | v2 compatibility with registry, store, dispatcher and lifecycle paths |
| tests/test_github_observer.py | registry shape and parse_time behavior |
| tests/test_status_projection.py | state-store rows and task overlays |

## State compatibility

The old state schema remains observable and relevant:

- workers
- runs
- reservations
- task_status
- receipts

Sensitive fields include envelope_json, envelope_sha256, task_sha256, external_system, external_id, workspace_path, workspace_branch, receipt_json and receipt_sha256. Status projection, reconcile and doctor paths read those surfaces. Old envelopes and receipts are evidence, not disposable cache.

## Re-export map

bureau.core is the compatibility facade. Legacy-origin public names include BureauError, ValidationError, NoEligibleTask, ConflictError, StateError, Resource, Initiative, Claim, Task, Reservation, utc_now, parse_time, canonical_json, sha256_json, default_state_dir, atomic_write, read_json, ancestors, overlaps, modes_conflict, claim_conflicts and ACTIVE_STATES.

## Decision criteria

Keep while core.py re-exports legacy-origin names, v2.py imports legacy, old state rows remain relevant, tests cover compatibility, or GitHub/status projection depends on existing registry/time behavior.

Deprecate after shared helpers move to a neutral module, v2 owns or imports neutral data contracts, core.py can preserve public names without importing legacy.py directly, old state DB/readback tests exist, and docs mark legacy.py as a compatibility shim.

Remove only after source modules no longer import legacy.py, tests no longer need legacy-facing names except compatibility checks, old receipts and envelopes remain readable through the replacement path, a release note names the compatibility boundary, and no queued active task relies on legacy public API behavior.

## Smallest safe follow-up PR

Add a neutral helper module for utc_now, parse_time, canonical_json, sha256_json, atomic_write and read_json. Re-export those helpers from legacy.py and core.py. Add tests proving hashes and old imports remain stable. Do not touch StateStore, Dispatcher, Registry or Task models in that PR.

## Acceptance mapping

- import-map: covered by direct import, module-reference, CLI and test maps.
- state-compatibility: covered by the state compatibility section.
- decision-frame: covered by keep/deprecate/remove criteria and the follow-up PR.

## Does not establish

This audit does not establish that legacy.py is well designed, that v2 migration is complete, that runtime state is drift-free, that old receipts are semantically complete, or that removal is safe.
