# Bureau rLens context policy v1

Status: implemented for `BUR-2026-002-T003`.

## Purpose

Bureau can make rLens/RepoBrief context use visible without turning rLens into a live-state authority or mandatory dependency for every task.

A task may declare:

```json
{
  "rlens_policy": {
    "mode": "required",
    "task_profile": "repo_work"
  }
}
```

Supported modes are deterministic:

| Mode | Meaning | Requires `rlens_context_ref` or skip reason |
|---|---|---|
| `opportunistic` | rLens may help, but is not required | no |
| `required` | normal repo/code/delegated work should carry bounded rLens context | yes |
| `strict` | PR review, roadmap/status, architecture/security/export work | yes |
| `live-first` | runtime/deploy/service work where live observation is primary | no |
| `external-safe` | lower-trust or non-local agents may receive context packs, not raw dumps | yes |

## Enforcement

For `required`, `strict`, and `external-safe`, Bureau blocks task claiming unless one of these is present:

- a valid `rlens_context_ref`; or
- `rlens_policy.skip_reason` with a machine-readable reason.

The generated execution envelope records `rlens_context_policy`. Receipts copy this policy forward, so a completed run can be audited for whether rLens context was satisfied, skipped, blocked, or not required.

## Boundary

The policy does not fetch, refresh, validate, or generate rLens bundles. It only records and enforces coordination evidence.

## Non-claims

This policy does not establish actual agent reading, answer correctness, repo understanding, claim truth, runtime correctness, test sufficiency, review completeness, or merge readiness.

## CLI report

Bureau also exposes a read-only registry report:

```bash
python -m bureau.cli --root . --json rlens-policy
python -m bureau.cli --root . --json rlens-policy --task-id BUR-2026-002-T003
python -m bureau.cli --root . rlens-policy --strict
```

`--strict` returns a non-zero exit code only when explicit `rlens_policy` entries block. Inferred task classes for legacy registry entries are reported as `policy-missing` and do not block by themselves. This keeps adoption incremental while making missing policy visible.
