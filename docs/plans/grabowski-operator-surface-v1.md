# Grabowski Operator Surface v1

Status: planned
Owner layer: Bureau coordination
Primary implementation repo: `heimgewebe/grabowski`
Created: 2026-07-05

## Thesis / antithesis / synthesis

Thesis: Grabowski does not primarily lack tools. It lacks a compact operator-facing surface that tells a fresh session what is true, what is blocked, what is allowed, and which grip is safe next.

Antithesis: Adding `situation`, `recall`, `grip_list`, `job_start`, capability profiles and friction summaries can create yet another ceremony layer on top of an already large tool surface.

Synthesis: The plan must reduce decision entropy, not add vocabulary. Build a small read-only situation spine first, then expose intent-shaped grips, then feed friction and evidence-bound recall back into planning. Durable jobs and session attenuation come later, after the read side can prove what it is seeing.

## Alternative axis

Do not optimize for "Grabowski can do more". Optimize for:

> A fresh operator can determine in 30 seconds: current state, active ball, blocked gates, next allowed grip and why that grip is safe.

This reframes the work from capability expansion to orientation compression.

## Source weighting

1. Primary runtime and registry evidence: Grabowski contracts, generated MCP catalog, receipts, Bureau task registry, local Git status and PR/check state.
2. Local operator evidence: friction records, review receipts, task runs, failed reconciliations and stale snapshot indicators.
3. Design documents: grip roadmap, autonomy/restplan documents and organ-boundary notes.
4. Chat analysis and screenshots: valid for intent and prioritization, not sufficient as implementation evidence.

## Resonance and contrast check

Reading A: The proposal is the right correction. Grabowski becomes less of a tool drawer and more of an operator with situation, intent and feedback.

Reading B: The proposal is the next abstraction blanket. It may rename complexity while leaving the operator to choose among even more surfaces.

Resolution: Reading A wins only if each slice removes or bundles an existing operator decision. Any slice that merely adds a new noun without deleting uncertainty must stop or be narrowed.

## Organ boundaries

| Organ | Role in this plan | Must not become |
| --- | --- | --- |
| Grabowski | execution, receipts, grips, local operational state | unbounded shell or memory oracle |
| Bureau | task and claim truth, ordering, completion evidence | second runtime monitor or knowledge base |
| Cabinet | ecosystem coherence radar and contradiction surfacing | task queue or execution authority |
| Heimlern | offline learning from routing outcomes and friction | live policy switch |
| Lenskit / RepoBrief | repository context and citable snapshots | live runtime truth |
| Leitstand | observation and visualization | control plane |

## Phase order

### Phase 1 — Situation spine and snapshot digest

Goal: add a read-only `grabowski_situation` / `situation` grip that summarizes the active operational state and includes a stale-tool-picture warning.

Minimum fields:

- repository, branch, head, base and dirty state when a repo is in scope;
- open PR, review/check status and stale review signals when available;
- Bureau task, claim, active run, blockers and dependency state when a Bureau binding exists;
- running jobs, leases and failed reconciliations when observable;
- capability/tool contract digest, runtime started_at and source commit;
- next safe grip recommendation with reason and non-claims.

This phase is read-only. It does not refresh connectors, mutate repositories, dispatch work or complete tasks.

### Phase 2 — First-class grip surface

Goal: expose a small `grip_list` / `grip_run` contract over existing narrow runners. Grips must be intent-shaped and receipt-bound.

A grip entry should state:

- name and purpose;
- target resource and scope;
- effect class: read-only, normal mutation, privileged mutation or prohibited;
- risk level, irreversibility and recovery path;
- preconditions and expected receipt shape;
- whether it is allowed for the current session/profile.

Bad grips such as `do-everything` are out of scope. The first target set is small: orient, worktree-orient, branch-publish, pr-create-or-update, pr-check-readiness and post-merge-sync.

### Phase 3 — Friction summary closes the loop

Goal: turn repeated friction records into a read-only summary that proposes which grip or small task to improve next.

The summary may prioritize; it may not execute. It should group repeated command chains, blocked gates, stale snapshot incidents, review/merge handoff loops and missing receipt fields.

### Phase 4 — Evidence-bound operator recall

Goal: create episodic operator memory as a derived, source-bound layer, not free-form recollection.

A recall item must contain:

- topic;
- situation;
- attempt;
- result;
- learned rule;
- evidence references to receipts, PRs, Bureau tasks or friction records;
- explicit `does_not_establish` entries.

Heimlern may later analyze these records and propose routing changes, but no live routing or merge policy changes follow automatically.

### Phase 5 — Durable jobs and push-notify model

Goal: avoid polling for long tasks and blocked gates by introducing durable job identity and optional `notify_on_done` metadata.

A job must have a job id, owner, scope, started_at, expected receipt, terminal status and notification target. Push behavior is deferred until job identity and receipt finalization are reliable.

### Phase 6 — Enforceable session attenuation

Goal: replace live all-or-nothing exposure with session-scoped capability profiles.

A session profile should make read roots, write roots, allowed grips, forbidden hosts and max risk level explicit. High-impact actions require explicit target, reason, expiry and recovery path. Documentation-only profiles are insufficient; the boundary must be enforced by the runtime/tool layer before being trusted.

## Task cut

1. `GRABOWSKI-OPERATOR-SURFACE-V1-T001` — add read-only situation grip and snapshot digest.
2. `GRABOWSKI-OPERATOR-SURFACE-V1-T002` — expose first-class grip list/run MCP surface.
3. `GRABOWSKI-OPERATOR-SURFACE-V1-T003` — add friction summary with next-grip proposal.
4. `GRABOWSKI-OPERATOR-SURFACE-V1-T004` — add evidence-bound operator recall contract and exporter.
5. `GRABOWSKI-OPERATOR-SURFACE-V1-T005` — add durable job identity and notify-on-done design slice.
6. `GRABOWSKI-OPERATOR-SURFACE-V1-T006` — add enforceable session capability profiles.

## Relationship to existing Bureau work

This plan does not replace `GRIP-ROADMAP-V1`. It narrows the next operator-facing layer around situation, grips and feedback. `GRIP-ROADMAP-V1-T001` remains the immediate worktree-orientation slice already in the queue. The first task here should follow it or be implemented in a non-conflicting worktree.

The existing `docs/grabowski-restplan-v2.md` remains the deeper security/autonomy plan. Its capability-profile direction is reused here, but this registration gives it Bureau task shape.

## Risk / benefit

Benefits:

- lower session-start context cost;
- fewer wrong-checkout and stale-snapshot errors;
- more consistent selection of safe next grips;
- friction becomes planning input instead of a failure diary;
- recall becomes evidence-bound rather than mythic memory;
- session privileges become bounded enough for unattended or semi-attended runs later.

Risks:

- new surfaces can increase ceremony if they do not replace decisions;
- recall can become folklore if it is not evidence-bound;
- stale snapshot warnings can become noisy and be ignored;
- push notifications can hide failed finalization if job identity is weak;
- capability profiles can create a false safety sense if only documented, not enforced.

Mitigation:

- first slice is read-only;
- every task lists non-claims;
- no automatic routing or merge authority changes;
- friction and recall are proposal-only until separately promoted;
- privileged/session work depends on a reliable situation spine.

## Epistemic gaps

- Current generated MCP catalog is not inspected in this registration; needed to know exact grip exposure work.
- Current friction-record corpus is not inspected here; needed to prioritize real recurring pain.
- Exact job/task runtime model is not inspected here; needed before push notifications.
- Existing capability enforcement code is not inspected here; needed before claiming profile enforcement.
- Current PR/check state of related Grabowski branches is not proven by this plan; needed before implementation or merge.

## Stop rules

Stop or narrow a slice if:

- it adds a new surface without reducing an existing operator decision;
- it treats generated summaries as proof of repo/runtime truth;
- it lets friction or recall directly authorize action;
- it requires broad shell access where a typed read tool is enough;
- it hides stale connector/runtime uncertainty;
- it blurs Bureau task truth with Grabowski runtime state.

## Non-claims

This registration does not implement any Grabowski code, does not deploy a runtime, does not authorize merges, does not complete existing Bureau tasks and does not prove current MCP catalog freshness.
