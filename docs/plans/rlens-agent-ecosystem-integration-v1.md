# rLens Agent Ecosystem Integration v1

Status: planned  
Owner layer: Bureau coordination, with Lenskit/Grabowski/Cabinet/Vibe-Lab execution slices  
Created: 2026-07-01

## Purpose

Make rLens the evidence backbone for repository and agent work without turning rLens into a control plane, review oracle, or live-state authority.

The plan replaces three unsafe simplifications:

1. "Give every agent raw dump access." → Use typed, task-profiled rLens context access.
2. "Keep every dump always current." → Detect staleness at use time and generate fresh dumps only when required.
3. "Prove agents read the dumps." → Require consumption declarations and receipts; do not claim actual understanding.

## Layer boundaries

| Layer | Owns | Must not claim |
|---|---|---|
| Lenskit/rLens | deterministic snapshots, canonical ranges, health, query/context artifacts | repo understanding, review completeness, runtime correctness |
| Grabowski | local read-only rLens broker, live git checks, bounded operator execution | dump truth beyond recorded snapshot, automatic approval |
| Bureau | task commitment, execution envelopes, receipts, dependencies | knowledge-base truth, live repo state |
| Cabinet | dated repository references, decisions, agent briefings | live state unless explicitly observed |
| Vibe-Lab | measurement of agent workflows and context conditions | superiority before repeated evidence |

## Access model

rLens access is task-profiled, not agent-name based.

| Mode | Use when | rLens requirement |
|---|---|---|
| opportunistic | trivial local edits, no repo status claim | optional discovery or no rLens |
| required | normal repo work, code changes, delegated agent work | bundle discovery, freshness check, preflight or explicit skip reason |
| strict | PR review, roadmap/status claims, architecture, security/export, merge prep | fresh-or-explicitly-stale bundle, required-reading pass/warn policy, range/citation evidence |
| live-first | services, ports, deploy/runtime issues | live tools primary, rLens only as repo/doc context |
| external-safe | non-local or lower-trust agents | context pack only; no raw dumps; export-safety gate when needed |

## Freshness policy

No global claim that all dumps are current is allowed.

Required guarantee:

> A required/strict agent run must not silently use a stale rLens bundle.

Freshness classes:

- `fresh_exact`: bundle commit equals live repo HEAD and live worktree is clean.
- `fresh_dirty_unverified`: commit matches but dirty worktree identity is not proven.
- `stale_head`: bundle commit differs from live repo HEAD.
- `unknown`: repo or bundle provenance is missing/unreadable.
- `not_required`: task mode does not need a current dump.

Operating rule:

1. Discover latest bundle dynamically from the merges area.
2. Read status/health sidecars.
3. Compare bundle commit to live repository HEAD when a live repo is in scope.
4. For required/strict tasks: generate or request a fresh bundle if stale/unknown, or record an explicit stale override.
5. Never mutate existing dump artifacts in place.

## Enforcement model

The process can enforce evidence use, not cognition.

Minimum required evidence for required/strict agent work:

- `rlens_context_ref` in Bureau task/run/receipt when Bureau owns the work.
- rLens bundle stem, manifest hash, task profile, freshness status, preflight status.
- Agent answer-compliance or equivalent declaration when a delegated agent was used.
- Explicit `does_not_establish` list.

This does not establish actual reading, correctness, all relevant context use, test sufficiency, regression absence, or runtime behavior.

## Implementation phases

### Phase 1 — First hardening slice

Goal: make the minimum verified path available.

Implemented/targeted slices:

- Lenskit: `bundle_manifest` is treated as the self-role of `--bundle-manifest` preflight input.
- Grabowski: add read-only `rlens_bundle_discover`, `rlens_bundle_status`, `rlens_freshness_check`.
- Bureau: allow optional `rlens_context_ref` in task, execution envelope, and receipt schemas.

Acceptance:

- Lenskit focused agent-consumption tests pass.
- Grabowski generated contracts are current; rLens bundle tests pass.
- Bureau schema tests accept valid refs and reject unknown ref fields.

### Phase 2 — Context pack and preflight bridge

Goal: move from discovery/status to usable agent handoff context.

Work:

- Add Grabowski wrappers for Lenskit `agent-consumption preflight`, `query`, and `range get`.
- Add `rlens_context_pack` as a bounded output containing task profile, snippets, range refs, non-claims, and answer-compliance template.
- Keep raw canonical dumps out of default agent handoffs.

Acceptance:

- A delegated agent task receives a context pack without raw dump access.
- Pack includes explicit freshness and non-claims.
- Query output shape is normalized by the wrapper.

### Phase 3 — Bureau enforcement

Goal: make rLens use visible in the coordination layer.

Work:

- Add task-class policy for opportunistic/required/strict/live-first modes.
- Require or explicitly skip `rlens_context_ref` for repo/PR/roadmap/architecture tasks.
- Include `rlens_context_ref` in receipts when present.

Acceptance:

- Required/strict Bureau tasks without ref or skip reason are blocked or reported as incomplete.
- Runtime/live-first tasks remain possible without false rLens requirements.

### Phase 4 — Cabinet import

Goal: refresh Cabinet repository references from rLens evidence without live-state inflation.

Work:

- Create a Cabinet importer for rLens repository references.
- Update ecosystem graph sources with bundle refs and freshness classes.
- Generate agent briefings from rLens context refs.

Acceptance:

- Cabinet cards clearly say dated snapshot, not live state.
- Old repository references are marked stale instead of silently replaced.

### Phase 5 — Vibe-Lab measurement

Goal: measure whether rLens context improves agent work.

Conditions:

- no rLens
- reading pack only
- context pack
- full canonical dump for trusted review
- context pack plus consumption trace

Metrics:

- hallucinated path count
- unsupported status claim count
- missing evidence count
- review finding quality
- rework count
- time-to-usable-output where measurable

Acceptance:

- No default expansion of agent dump access without comparative evidence.

## Stop rules

Stop or downgrade the rLens requirement if:

- the task is live-runtime-first and rLens would add stale documentation noise;
- export/security context lacks a valid export-safety path;
- bundle provenance is missing and no fresh scan can be generated;
- context-pack generation exceeds the value of a trivial task;
- agent wrappers begin treating health/status as truth or merge approval.

## Does not establish

This plan does not establish:

- rLens improves agent quality;
- any existing dump is globally current;
- any agent actually read or understood a dump;
- PRs are merge-ready;
- tests are sufficient;
- runtime behavior is correct;
- secret or PII absence.
