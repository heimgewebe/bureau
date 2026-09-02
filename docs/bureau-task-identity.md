# Bureau task identity

Status: 2026-09-02

## Invariant

A Bureau task is identified globally only by its canonical full `task_id`.
A trailing local ordinal such as `T191` is namespace-local metadata, not a
global identifier. Different initiatives or lanes may therefore legitimately
contain distinct canonical task IDs that end in the same local ordinal.

Examples:

- `GRABOWSKI-OPERATOR-SURFACE-V1-T191`
- `REPOGROUND-UNUSED-AUTHORITY-CLOSEOUT-V1-T191`

Both may exist at the same time. The string `T191` alone does not identify
either one.

## Allowed references

Globally consumed task references must use one of these forms:

1. the exact canonical full task ID; or
2. an explicit namespace plus local ordinal, resolved through the canonical
   task-reference resolver and accepted only when exactly one canonical task
   matches that pair.

A bare local ordinal is always fail-closed, including when the current Registry
happens to contain only one matching task. Temporary uniqueness must never
become implicit authority.

## Enforcement

- The Registry exposes `task_reference_assessment()` and the public core API
  exposes the task-identity helpers. Bare local ordinals are rejected and the
  diagnostic reports matching canonical IDs when known.
- Registry validation rejects bare local ordinals used as canonical task IDs,
  dependency references, `metadata.parent_task` references, or queue entries.
- Permanent Registry registration rejects a bare local ordinal before task
  publication. When current-main or open-PR observations contain matching
  canonical tasks, the diagnostic names those candidates and requires a full
  ID or explicit namespace.
- A new canonical full ID is not rejected merely because another namespace
  already uses the same trailing local ordinal. This is not a collision.

## Compatibility

Existing canonical task IDs, dependency edges, receipts, claims, PR bindings,
and historical evidence remain unchanged. There is no mass renumbering.

Human-facing prose may show a short ordinal as a convenience only when the
canonical full ID is present in the same unambiguous context. Authority,
handoff, persistence, mutation, closeout, and machine-to-machine references
must not rely on the short ordinal alone.
