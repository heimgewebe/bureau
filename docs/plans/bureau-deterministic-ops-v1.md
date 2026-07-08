# Bureau deterministic operations hardening v1

## Problem

The user considered a local Ollama secretary for Bureau. A live probe found that the local models are available but not reliable enough for Bureau maintenance authority. The safer path is to keep Bureau deterministic and improve the surfaces that provide secretary-like visibility without adding model-dependent truth.

## Decision

Do not introduce an Ollama Bureau secretary. Optimize Bureau through deterministic guards and read-only projections.

## Phases

### Phase 1 — Queue readiness invariant

`now` must not contain non-ready work. Queue reconciliation should fail closed or emit an explicit repair plan when a lane contains a task whose registry state, dependencies or conflicts make that lane invalid.

### Phase 2 — Per-repository active ball guard

Bureau should support one active ball per repository, not one global ball. The projection must make ambiguous or conflicting active work visible per repo.

### Phase 3 — PR task binding guard v2

Open working PRs should bind exactly one valid Bureau task unless an explicit, schema-visible exception exists. Multi-task and no-task PRs should become merge blockers, not soft warnings.

### Phase 4 — Closeout evidence guard

`verified` and `done` must require typed evidence, source authority, hash binding and queue cleanup. AI or prose summaries are insufficient.

### Phase 5 — Deterministic status projection

A deterministic report should show active balls, queue state, open PR binding, blocked work, stale evidence and next safe actions. This supplies the useful secretary view without granting AI authority.

### Phase 6 — AI exclusion policy

The Bureau authority boundary should state that AI output has no direct maintenance authority. Any future model-assisted lane requires a benchmark, deterministic validation and no-mutation contract.

## Acceptance standard

A phase is accepted only when tests or schema checks prove the invariant. Good-sounding summaries are not sufficient evidence.
