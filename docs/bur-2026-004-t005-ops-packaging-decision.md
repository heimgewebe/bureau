# BUR-2026-004-T005 — Bureau Ops packaging decision

Status: decision v1

Task: `BUR-2026-004-T005`

Initiative: `BUR-2026-004`

Source plan: `docs/plans/bureau-boundary-ops-v1.md`

## Decision

Keep Bureau Core and Bureau Ops in the existing `bureau` Python distribution for now. Do not create a separate `bureau-ops` package or repository in this phase.

The next packaging move is internal consolidation, not extraction:

1. keep existing packaged console scripts as stable compatibility shims;
2. add grouped `bureau ops ...` aliases incrementally behind the main `bureau` CLI;
3. keep `ops/systemd/` as the local Linux reference deployment profile;
4. revisit package extraction only after grouped aliases, compatibility telemetry, and deployment migration evidence exist.

## Packaging criteria

A separate `bureau-ops` package is allowed only if all criteria below are met.

| Criterion | Required evidence before extraction | Current assessment |
| --- | --- | --- |
| Stable core API | Bureau Core exposes a small, documented API that Ops can consume without importing volatile internals. | Not complete. Core surfaces are identifiable, but Ops still imports repository-local modules directly. |
| Entry-point stability | Grouped `bureau ops ...` aliases exist and old console scripts remain tested compatibility shims. | Planned by T004; not implemented broadly yet. |
| Deployment portability | systemd reference units can run through the future public command surface without changing semantics. | Not proven. T004 inventories six service/timer pairs but does not migrate them. |
| CI coverage | CI validates Core and Ops package boundaries separately, including no accidental Core import of Ops. | Not present. Current CI validates one distribution. |
| Operator cost | Extraction reduces operator confusion or install risk more than it adds release, versioning and venv overhead. | Not proven. Current evidence points to higher overhead if extraction is premature. |
| Reversibility | Extraction can be rolled back without losing task registry, receipts, state-root or scheduler compatibility. | Not proven. State and deployment compatibility still need stronger boundaries. |

Until these criteria are met, extraction is blocked.

## Stable Bureau Core API surface

The stable Core API surface for Ops consumers is intentionally narrow:

| Surface | Status | Notes |
| --- | --- | --- |
| Registry task and initiative JSON schemas | Stable public contract | Ops may read and write through documented task/initiative files and schema validation. |
| Queue JSON contract | Stable public contract | Ops may observe and update queue entries only through explicit registry changes and validation. |
| `bureau.cli check` / registry validation result | Stable operational check | Ops may use this as the canonical local registry health check. |
| Cabinet import policy and task claim guard reports | Stable read/guard surfaces | Ops may depend on guard outputs, not on hidden implementation details. |
| State-root doctor report | Stable read-only diagnostic surface | Ops may report unknown state artifacts; automatic deletion remains out of scope. |
| rLens policy report CLI | Stable read-only report surface | Ops may use policy reports as context-policy evidence, not as correctness proof. |
| Entry-point inventory report | Stable decision input | Ops may use the generated inventory for migration planning, not as runtime truth. |

The following are not stable Core API for a separate package yet:

- direct imports from `src/bureau/legacy.py`;
- arbitrary `StateStore` internals beyond documented receipts, envelopes and diagnostics;
- private helper modules used by specific bridges;
- systemd unit paths as an architectural contract;
- GitHub, Grabowski, Cabinet, Chronik or Leitstand facts after they have been copied into Bureau-shaped receipts.

## Deployment and operator-cost assessment

| Area | If Ops stays in the current package | If Ops is extracted now |
| --- | --- | --- |
| systemd reference deployment | Existing units and venv install paths keep working. Future grouped aliases can be piloted without unit churn. | Six known service/timer pairs would need package, venv and command migration plans. Hidden user units may break. |
| Packaging | One Python distribution, one editable install, one CI matrix. | At least two distributions, dependency pins, version compatibility, release order and installer docs. |
| CI | Existing `make validate` and GitHub matrix continue to cover Core plus Ops. | Requires new boundary tests, import-linting, package build checks and cross-package integration tests. |
| Operator cost | Lower immediate cost; boundary remains documented rather than physically enforced. | Higher cost; adds release and installation complexity before benefits are proven. |
| Failure diagnosis | Current state-root, queue and bridge diagnostics stay local to one repo. | Diagnoses must separate Core failure, Ops package failure, packaging mismatch and deployment mismatch. |
| Reversibility | Easy: aliases and docs can be adjusted in one repo. | Harder: extracting and later reintegrating packages changes imports, installers and systemd paths. |

## Chosen path

Adopt the following staged path.

### Phase 0 — Current decision

- Keep one `bureau` package.
- Keep all existing console scripts.
- Treat Bureau Ops as an explicit layer inside the repository, not as a separate distribution.
- Forbid package extraction until the criteria above are re-evaluated with evidence.

### Phase 1 — Grouped command pilot

Implement one low-risk grouped command alias, for example:

```text
bureau ops source-pr-bridge run
```

The existing `bureau-source-pr-bridge` command remains supported and is tested as the compatibility shim.

### Phase 2 — Boundary checks

Add checks that make the internal boundary visible before extraction:

- Ops-to-Core import inventory;
- Core-must-not-import-Ops rule;
- explicit list of stable Core APIs consumed by Ops;
- CI job that fails when a new Ops module depends on an unstable Core module without a decision note.

### Phase 3 — Deployment migration proof

Only after grouped aliases exist and boundary checks are green:

- migrate one reference `ops/systemd/*.service` file to a grouped alias;
- keep the old console script installed;
- document rollback;
- prove static unit tests and CLI tests still pass.

### Phase 4 — Extraction re-evaluation

Reconsider `bureau-ops` only if:

- grouped aliases are complete enough for reference units;
- hidden-user risk is reduced by compatibility telemetry or broad grep coverage;
- package-boundary CI exists;
- operator install steps become simpler or materially safer after extraction.

## Explicit non-decisions

This decision does not:

- remove any console script;
- change any `ops/systemd/` unit;
- create a new package;
- declare `legacy.py` removable;
- prove runtime units are installed or healthy;
- assert that all hidden external users have been found.

## Immediate follow-up

The smallest safe follow-up is a single grouped alias pilot for one existing Ops command, with the old command retained as the supported path. Do not start package extraction before that pilot and the boundary checks exist.
