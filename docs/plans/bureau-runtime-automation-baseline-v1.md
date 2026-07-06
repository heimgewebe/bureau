# Bureau Runtime Automation Baseline v1

Status: planned
Owner layer: Bureau Ops around Bureau Core
Primary implementation repo: `heimgewebe/bureau`
Created: 2026-07-06

## Thesis / antithesis / synthesis

Thesis: Bureau should not depend on a chat operator as its heartbeat. Git remains the durable registry for intent, while SQLite remains the operational source for claims, runs, reservations, receipts, workspaces and event evidence.

Antithesis: A fully autonomous Bureau scheduler would be faster, but it could also confuse observation with authority: GitHub owns pull requests, reviews and CI facts; Grabowski owns processes and concrete workers; Bureau owns coordination and verification receipts.

Synthesis: Build a daemonized control tower before building an autopilot. Local systemd timers keep Bureau state fresh, a GitHub observer imports PR/CI/review facts as evidence, status projection explains the current truth, and any dispatcher remains opt-in, bounded and non-merge-capable.

## Alternative axis

Do not optimize first for "more automation". Optimize for this question:

> Can Bureau stay truthful when no operator is present, without gaining hidden authority to merge, delete, verify or dispatch unsafe work?

This changes the order: observe and project first; dispatch later; auto-merge last or never.

## Source weighting

1. Primary runtime contracts: `StateStore`, `Dispatcher`, reconciliation, receipts, stale overlays and event logging.
2. Primary deployment contracts: existing systemd units, GitHub Actions, CLI commands and state-root layout.
3. Source authority contracts: GitHub PR/check/review semantics, Grabowski task observation and local `gh` availability.
4. Registry contracts: initiative, task, queue and resource schemas.
5. Operator convenience and chat workflow impressions.

## Scope

### In scope

- Add an explicit runtime automation architecture for Bureau.
- Add local systemd timers for reconciliation, doctor/reporting and GitHub observation.
- Add a GitHub PR observer that binds PR facts to Bureau runs using explicit markers before branch heuristics.
- Add a status projection command that shows registry state, runtime state, GitHub evidence and receipt/stale state together.
- Define a webhook inbox as append-only event ingestion, not direct state mutation.
- Define a conservative dispatcher timer policy, disabled by default, with no merge, cleanup or completion authority.

### Out of scope

- Auto-merge.
- Auto-completion without evidence-complete Bureau receipts.
- Production deployment or remote host mutation.
- Changing existing task semantics without a focused task and tests.
- Treating GitHub observations as stronger than GitHub itself.

## Optimized operating model

| Layer | Responsibility | Automation mechanism | Authority limit |
|---|---|---|---|
| Registry | durable intent, initiatives, tasks, queue, resources | PR/CI validation | no runtime truth |
| State root | runs, claims, reservations, receipts, events | local SQLite + materialized files | no GitHub truth ownership |
| Reconcile loop | stale runs, external observations, missing materializations | systemd timer | no merge, no verification shortcut |
| GitHub observer | PR, CI, review, merge facts | systemd timer and optional webhook inbox | evidence only |
| Status projection | operator dashboard and machine-readable board | CLI JSON output | read-only |
| Dispatcher | optional work claiming and external dispatch | disabled-by-default systemd timer | no destructive effects |
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

## Task sequence

1. **T001 — Runtime automation contract.** Document the control-tower model, status vocabulary, authority limits and systemd/webhook/CI split.
2. **T002 — Reconcile and doctor timers.** Add hardened user-level systemd units for periodic `reconcile` and read-only health reporting.
3. **T003 — GitHub PR observer.** Implement PR/check/review observation and run binding with explicit markers, confidence levels and fail-closed ambiguity handling.
4. **T004 — Status projection board.** Add a read-only JSON projection that combines registry, SQLite, workspace and GitHub evidence.
5. **T005 — Webhook inbox contract.** Add an append-only webhook/event inbox contract with replay tests; no direct state mutation.
6. **T006 — Opt-in dispatcher timer policy.** Add a disabled-by-default dispatcher loop for autonomous-ready tasks with strict preflight gates and no merge/completion/cleanup authority.
7. **T007 — Operations runbook and proof matrix.** Document installation, rollback, logs, safety checks and the evidence required before any later merge-gate automation.

## Risk / benefit check

Benefits:

- Bureau status remains fresh without a live chat operator.
- Operators see one truth surface instead of reconstructing state from PRs, SQLite and memory.
- GitHub facts become evidence with source attribution, not manual status folklore.
- Future dispatch automation receives explicit gates before it can start work.

Risks:

- Incorrect PR-to-run binding can mislead the status board.
- Polling and webhooks can race or duplicate events.
- A local `gh` session can become a single point of failure.
- Dispatcher automation can create noisy or unsafe work if enabled too early.
- Status projection can appear more authoritative than its sources.

Mitigations:

- Prefer explicit markers over heuristics.
- Make observation idempotent and event-backed.
- Fail closed on GitHub ambiguity.
- Keep dispatcher disabled until observer and status board are proven.
- Keep merge and completion outside this plan.

## Decision gates

- T003 must not ship without ambiguity tests for marker, branch and no-match cases.
- T004 must show stale receipts and GitHub unknowns explicitly.
- T005 must be replayable from stored events.
- T006 must include a dry-run mode and a hard default-off deployment.
- Any later auto-merge plan must be a separate initiative with its own authority review.
