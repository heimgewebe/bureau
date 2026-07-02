# Bureau runtime and repo orchestration rebuild v1

## Decision

Bureau should rebuild its orchestration spine before adding broader automation. The next safe layer is runtime truth, Steuerboard hardening, lifecycle repair, approval semantics, repo registry, safe fetch/import design, a minimal what-now path, and candidate planning from existing Steuerboard artifacts.

## Thesis / antithesis / synthesis

Thesis: Bureau should coordinate repo and runtime work. It should know what exists, what is eligible, what is blocked, and what should be handled next.

Antithesis: If Bureau mutates repos, starts agents, imports sources, and repairs its own runtime without hard gates, it can amplify drift. The map becomes a hand in every drawer.

Synthesis: Bureau should first separate discovery, planning, approval, and execution. Mutation stays bounded, auditable, and task-backed.

## Non-goals

- No runtime repair in this planning commit.
- No broad fetch or pull automation before runtime drift checks exist.
- No Cabinet, rLens, repoLens, or chat memory as a truth oracle.
- No bypass of review requirements for repo, PR, or agent work.

## Source weighting

1. Primary runtime and registry evidence: Bureau CLI, registry JSON, task receipts, Git status, CI.
2. Local operational evidence: Grabowski receipts, Steuerboard artifacts, Operator Relay records.
3. Derived summaries: plans, reports, dashboards.
4. Human intent: valid for priority, not enough as execution evidence.

## Resonance and contrast check

Reading A: the problem is missing automation. Bureau needs better scanning, task cutting, and queue movement.

Reading B: the problem is unsafe automation. Bureau needs stronger state truth before it gets more hands.

Resolution: both can be true. The first slice improves visibility and binding before autonomy.

## Epistemic gaps

- Current Bureau runtime drift facts are incomplete; needed for safe repair.
- Complete repo and vault planning inventory is incomplete; needed for repo registry design.
- Existing Steuerboard artifacts are not yet canonically consumed; needed to avoid duplicate discovery.
- Approval semantics for fetch, import, pull, and dispatch are not fully encoded; needed before orchestration.

## Task cut

1. BUR-2026-003-T001 Runtime drift check.
2. BUR-2026-003-T002 Steuerboard runtime hardening.
3. BUR-2026-003-T003 Lifecycle repair.
4. BUR-2026-003-T004 Approval path.
5. BUR-2026-003-T005 Repo registry and repo-scan.
6. BUR-2026-003-T006 Safe fetch orchestration/import.
7. BUR-2026-003-T007 Minimal what-now.
8. BUR-2026-003-T008 Pull candidate planning through existing Steuerboard artifacts.

## Ordering

T001 is first and read-only. T002 and T003 may follow after T001 gives stable evidence. T004 gates mutation paths. T005 feeds T006 and T008. T007 should consume registry truth and stay deliberately small.

## Risk / benefit

Benefits: fewer wrong-checkout mutations, better operator visibility, cleaner approval boundaries, more reliable conversion of plans into registry tasks.

Risks: more process before visible automation, approval gates can stall maintenance, repo registry scope can grow too broad, drift checks can become noisy.

Mitigation: keep T001 read-only, keep T006 deferred, and require every task to state what evidence makes it safe.

## Alternative axis

Do not assume the goal is more autonomous Bureau. A stronger axis is less surprising Bureau: fewer hidden states, fewer wrong-checkout writes, and faster recovery when a tool blocks.

## Merge boundary

This slice may add only this plan, BUR-2026-003, and BUR-2026-003-T001 through T008. It must not implement runtime behavior or repo mutation.
