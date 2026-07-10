# Bureau Scheduler Lane Consolidation Plan v1

Task: `OPERATOR-ECOSYSTEM-REDUNDANCY-V1-T001`

Live observation: 2026-07-10 18:57 CEST on `heim-pc`

Machine report: `docs/reports/operator-ecosystem-t001-bureau-scheduler-lanes.v1.json`

## Decision

The live scheduler is not one redundant block. It contains two real pipelines, two independent support lanes and two diagnostic/decision lanes with unresolved ownership.

Eight lanes have distinct inputs, outputs or consumers and remain enabled:

1. discovery (`bureau-halfhour-operator`)
2. curator
3. operator control
4. verifier control
5. closure planner
6. agent frontier
7. review steward
8. source PR bridge

The Codex bridge is the only lane proven inert: its timer is enabled, but every observed activation is condition-skipped because the dedicated backend worktree is absent. Its installed unit also differs materially from the repository unit. It must be retired or normalized in a separate reviewed cutover.

The GPT connector probe is diagnostic-only from the local repository perspective. It writes fresh receipts, but this audit cannot exclude an external ChatGPT or Grabowski consumer. It therefore remains enabled until consumer coverage is explicit.

No timer, service, state file or runtime configuration is changed by this plan.

## Why the core lanes are not redundant

The installed `bureau_cycle` stages form a receipt-bound feedback loop:

```text
discovery -> curator -> operator -> verifier -> cycle health -> operator
```

- Discovery scans registered sources and publishes a handoff.
- Curator classifies that handoff and records what was seen.
- Operator consumes curator output and the previous health gate.
- Verifier validates the operator receipt and publishes the next health gate.

Removing any stage would either remove a distinct decision boundary or require a new combined contract with equivalent receipts and failure semantics. There is currently no evidence that such a combination would reduce risk or operational cost.

The closure path is separate:

```text
closure planner -> review steward
                -> agent frontier -> bounded manual binding
                -> GPT probe
                -> Codex bridge (currently skipped)
```

Closure produces the large inventory and lane plan. Review Steward adds review evidence. Agent Frontier reduces the plan to a bounded proposal surface. These are different products and consumers, not duplicate status views.

## Live findings

### 1. Codex bridge: retirement or canonical normalization

Current facts:

- `bureau-codex-bridge.timer` remains enabled hourly.
- Every observed trigger is skipped by `ConditionPathIsDirectory`.
- The required dedicated backend worktree is absent.
- No current decision output exists; state files date from 1 July 2026.
- The installed service and timer differ from `ops/systemd/`.

This is scheduler noise and configuration drift. It does not establish that the Codex bridge feature itself is useless; it establishes that the installed lane currently performs no work.

### 2. GPT connector probe: consumer unknown

Current facts:

- The timer runs successfully once per hour.
- It reads Bureau health, closure and frontier evidence and writes a bounded readiness receipt.
- No reader is present in the Bureau repository or local process contracts inspected here.
- An external ChatGPT/Grabowski workflow may still consume or expect this evidence.

The lane is therefore unconsumed locally, not proven globally unconsumed.

### 3. Deployment source split

The core `bureau_cycle` modules, their four timers and the closure planner timer are installed outside the current repository source surface. Some repository-owned units are byte-identical to live units, while Codex is drifted.

This split makes audits and rollback harder. It is a source-ownership problem, not a reason to collapse semantic stages.

### 4. Naming drift

`bureau-halfhour-operator.timer` currently runs hourly at minute 30. The historic name is misleading, but renaming it without a compatibility plan would create avoidable operational risk.

## Staged effects

### Stage A — evidence and ownership

Status: completed by this task.

- Record all ten live timers, services, cadence, source, last result, inputs, outputs and consumers.
- Distinguish systemd success from application health.
- Mark absent consumers as unknown rather than inventing them.
- Preserve the current runtime unchanged.

Rollback: none required; documentation and registry evidence only.

### Stage B — Codex bridge decision

Follow-up: `OPERATOR-ECOSYSTEM-REDUNDANCY-V1-T015`. No implicit authority from this plan.

1. Recheck that the backend worktree remains absent and no current output or external consumer exists.
2. Choose exactly one target:
   - canonical repository service with an explicitly justified backend, or
   - retirement of the scheduled lane while retaining manual tooling if still useful.
3. Capture the current live unit, timer and drop-in hashes as rollback evidence.
4. Test the chosen replacement as a bounded one-shot without changing the timer.
5. Only after review, disable or replace the timer.
6. Observe at least two expected cycles and verify Bureau Core, closure and frontier remain healthy.

Rollback for a disablement:

```text
systemctl --user enable --now bureau-codex-bridge.timer
```

This command is documentation, not authorization to execute it.

### Stage C — GPT probe consumer decision

Follow-up: `OPERATOR-ECOSYSTEM-REDUNDANCY-V1-T016`.

1. Identify the named external or local consumer and its freshness requirement.
2. If consumed, version the probe and its unit in the owning repository and set a justified cadence.
3. If unconsumed, run one explicit external connector canary before removing the timer.
4. A disablement requires a captured unit backup, one-cycle bounded observation and an explicit rollback path.

### Stage D — deployment source ownership

Follow-up: `OPERATOR-ECOSYSTEM-REDUNDANCY-V1-T017`.

- Either version the installed `bureau_cycle` modules, timer units and closure timer under Bureau, or name another canonical deployment repository and immutable release identity.
- Add a drift check comparing live units and executables with that canonical source.
- Preserve compatibility aliases before renaming `bureau-halfhour-operator`.

## Safety boundaries

This plan does not authorize:

- disabling or restarting any timer or service,
- deleting runtime state or receipts,
- replacing the installed cycle implementation,
- automatic task promotion or dispatch,
- automatic PR merge,
- interpreting a successful systemd oneshot as semantic health.

A later cutover is valid only if its current live preflight, exact target, rollback and bounded post-effect proof are all reviewed together.
