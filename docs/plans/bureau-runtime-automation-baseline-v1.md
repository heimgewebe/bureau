# Bureau Runtime Automation Baseline v1

Status: planned
Owner layer: Bureau Ops around Bureau Core
Primary implementation repo: `heimgewebe/bureau`
Created: 2026-07-06

## Thesis / antithesis / synthesis

Thesis: Bureau should not depend on a chat operator as its heartbeat. Git remains the durable registry for intent, while SQLite remains the operational source for claims, runs, reservations, receipts, workspaces and event evidence.

Antithesis: A fully autonomous Bureau scheduler would be faster, but it could also confuse observation with authority: GitHub owns pull requests, reviews and CI facts; Grabowski owns processes and concrete workers; Bureau owns coordination and verification receipts.

Synthesis: Build a daemonized control tower before building an autopilot. Scheduler-neutral one-shot commands keep Bureau state refreshable, hardened systemd user timers provide the default local Linux deployment profile, a GitHub observer imports PR/CI/review facts as evidence, status projection explains the current truth, and any dispatcher remains opt-in, bounded and non-merge-capable.

## Alternative axis

Do not optimize first for "more automation" or for one scheduler. Optimize for this question:

> Can Bureau stay truthful when no operator is present, without gaining hidden authority to merge, delete, verify or dispatch unsafe work?

This changes the order: define idempotent scheduler contracts first; use systemd only as the local Linux reference implementation; observe and project before dispatch; auto-merge last or never.

## Source weighting

1. Primary runtime contracts: `StateStore`, `Dispatcher`, reconciliation, receipts, stale overlays and event logging.
2. Primary scheduler contracts: idempotent `--once` style commands, locking, state-root paths, CLI output and replay behaviour.
3. Primary deployment contracts: existing systemd units, GitHub Actions, CLI commands and state-root layout.
4. Source authority contracts: GitHub PR/check/review semantics, Grabowski task observation and local `gh` availability.
5. Registry contracts: initiative, task, queue and resource schemas.
6. Operator convenience and chat workflow impressions.

## Scope

### In scope

- Add an explicit runtime automation architecture for Bureau.
- Define scheduler-neutral local loops for reconciliation, health reporting and GitHub observation.
- Provide hardened user-level systemd timers as the default local Linux reference implementation.
- Add a GitHub PR observer that binds PR facts to Bureau runs using explicit markers before branch heuristics.
- Add a status projection command that shows registry state, runtime state, GitHub evidence and receipt/stale state together.
- Define a webhook inbox as append-only event ingestion, not direct state mutation.
- Assess and red-test a conservative dispatcher timer policy, disabled by default, with no merge, cleanup or completion authority.
- Assess safe plan pinning and re-verification strategies before mutating `current_plan` in a way that can stale receipts.

### Out of scope

- Auto-merge.
- Auto-completion without evidence-complete Bureau receipts.
- Production deployment or remote host mutation.
- Changing existing task semantics without a focused task and tests.
- Treating GitHub observations as stronger than GitHub itself.
- Making systemd a Bureau Core dependency.
- Pretending a branch commit is the final canonical plan revision after merge.
- Adding `current_plan.commit` or `document_sha256` to this initiative before a no-stall pinning strategy is documented.

## Optimized operating model

| Layer | Responsibility | Automation mechanism | Authority limit |
|---|---|---|---|
| Registry | durable intent, initiatives, tasks, queue, resources | PR/CI validation | no runtime truth |
| State root | runs, claims, reservations, receipts, events | local SQLite + materialized files | no GitHub truth ownership |
| Reconcile loop | stale runs, external observations, missing materializations | scheduler-neutral one-shot command; systemd reference timer | no merge, no verification shortcut |
| GitHub observer | PR, CI, review, merge facts | scheduler-neutral one-shot command, optional webhook inbox, systemd reference timer | evidence only |
| Status projection | operator dashboard and machine-readable board | CLI JSON output | read-only |
| Dispatcher | optional work claiming and external dispatch | disabled-by-default scheduler profile after assessment | no destructive effects |
| Merge gate | final human or explicit gatekeeper decision | later narrow policy | outside this plan |

## Binding rules

1. PRs should include an explicit `Bureau-Run:` marker when they represent an active Bureau run.
2. `Bureau-Task:` may be used for planning or review-only PRs without an active run.
3. Branch naming may be used only as a fallback and must yield lower confidence.
4. Uncertain binding records a blocked observation; it must not silently claim success.
5. `merged` is a GitHub fact, not Bureau completion. Completion still requires evidence for every acceptance criterion.

## Failure semantics

- GitHub unavailable: status becomes `github-observation-blocked`; no fail-open dispatch.
- PR binding ambiguous: record event and keep task non-terminal.
- CI unknown: show unknown; do not infer success.
- Review state conflicted: show the stricter state, for example `changes_requested` over `approved`.
- Receipt stale: keep stale overlay until the current task and plan revision is verified again.
- Scheduler unavailable: scheduled freshness degrades visibly; manual one-shot commands remain the fallback.
- Plan pinning that changes `plan_sha256` after verification can stale existing receipts and must be treated as a re-verification event, not harmless metadata.

## Task sequence

1. **T001 — Runtime automation contract.** Document the control-tower model, status vocabulary, authority limits and scheduler/webhook/CI split.
2. **T002 — Local scheduler contract and reference timers.** Define idempotent local scheduler commands for reconciliation, health reporting and observation; provide hardened systemd user timers as the Linux reference deployment.
3. **T003 — GitHub PR observer.** Implement PR/check/review observation and run binding with explicit markers, confidence levels and fail-closed ambiguity handling.
4. **T004 — Status projection board.** Add a read-only JSON projection that combines registry, SQLite, workspace and GitHub evidence.
5. **T005 — Webhook inbox contract.** Add an append-only webhook/event inbox contract with source verification, replay tests and no direct state mutation.
6. **T006 — Opt-in dispatcher timer assessment.** Red-test and specify a disabled-by-default dispatcher loop before implementation.
7. **T007 — Operations runbook and proof matrix.** Document installation, rollback, logs, safety checks and the evidence required before any later merge-gate automation.
8. **T008 — Plan pinning freshness strategy.** Assess how to bind plan revisions without staling existing receipts or blocking dependent tasks.

## Risk / benefit check

Benefits:

- Bureau status remains fresh without a live chat operator.
- Operators see one truth surface instead of reconstructing state from PRs, SQLite and memory.
- GitHub facts become evidence with source attribution, not manual status folklore.
- Future dispatch automation receives explicit gates before it can start work.
- Scheduler-neutral loops keep Bureau portable beyond one Linux user service setup.

Risks:

- Incorrect PR-to-run binding can mislead the status board.
- Polling and webhooks can race or duplicate events.
- A local `gh` session can become a single point of failure for the local reference deployment.
- Dispatcher automation can create noisy or unsafe work if enabled too early.
- Status projection can appear more authoritative than its sources.
- Treating systemd as architecture rather than deployment profile can reduce portability.
- Premature plan pinning can turn a branch-local commit into fake canonical evidence.
- Later plan pinning can stale verified receipts for tasks in this initiative and stop dependent task eligibility.

Mitigations:

- Prefer explicit markers over heuristics.
- Make observation idempotent and event-backed.
- Fail closed on GitHub ambiguity.
- Keep dispatcher disabled until observer and status board are proven.
- Keep merge and completion outside this plan.
- Require manual one-shot operation to remain possible for every scheduled loop.
- Keep `current_plan` path-bound in this baseline until T008 documents a safe pinning/re-verification strategy.

## Decision gates

- T002 must define idempotent one-shot commands before relying on any timer.
- T003 must not ship without ambiguity tests for marker, branch and no-match cases.
- T004 must show stale receipts and GitHub unknowns explicitly.
- T005 must validate source identity, payload schema and event identity, and must be replayable from stored events.
- T006 must remain an assessment/red-team task unless a later PR explicitly implements the default-off dispatcher.
- T008 must not mutate this initiative's `current_plan` until it documents how plan pinning interacts with envelopes, receipts, stale overlays and dependency eligibility.
- Any later auto-merge plan must be a separate initiative with its own authority review.
