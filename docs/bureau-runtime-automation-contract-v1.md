# Bureau Runtime Automation Contract v1

Status: closes `BUR-2026-005-T001`.

## Purpose

This document defines Bureau as a conservative control-tower runtime. It separates source authority, runtime state, observer evidence, status projection and dispatcher authority. It also names the baseline vocabulary and the powers that remain forbidden.

This is a contract document only. It does not implement new automation and does not grant hidden authority.

## Control-tower organ model

| Organ | Owns | May emit | Must not decide alone |
|---|---|---|---|
| Registry | Initiatives, tasks, queue lanes, claims, acceptance criteria and plans. | Planned, ready and verified registry facts through reviewable JSON diffs. | Runtime truth, PR state, CI result, checkout state or merge readiness. |
| State store | Runs, receipts, workspaces, reservations, overlays and live-register events. | Runtime receipts, workspace findings, local overlays, thread focus and candidate observations. | Permanent registry truth, queue truth or external PR truth. |
| Git checkout | Local branch, head, dirty state and worktrees. | Local checkout evidence. | GitHub PR state, CI state or task acceptance. |
| GitHub observer | PR metadata, review state, checks and merge facts. | Source-attributed PR, CI and review observations. | Registry mutation, merge authority or task verification. |
| CI | A job result for one run and one commit. | Pass, fail, pending or skipped observations. | General correctness, test sufficiency, security correctness or merge readiness. |
| Dispatcher | Bounded task selection and run envelopes. | Claim, run, heartbeat and handoff receipts. | Override claims, queue policy, merge gates or cleanup gates. |
| Doctor | Composite health and repair diagnostics. | Findings and repair candidates. | Mutation unless called through explicit repair or reviewed PR. |
| Cabinet | Overview and external signal layer. | Candidate signals and import previews. | Bureau registry authority, dispatch or verification. |
| RepoBrief / Lenskit | Read-only code context and evidence surfaces. | Bundles, citations, required reading and evaluation observations. | Patch application, Git mutation, CI execution, merge or cleanup. |
| Operator / reviewer | Decisions above automation authority. | Approval, rejection, override and escalation evidence. | Silent mutation without recorded evidence. |

## Source authority rules

Bureau automation must keep facts source-bound:

- Registry files are authoritative for intended task state, initiative lifecycle, claims, acceptance and queue placement.
- State-store rows are authoritative for local runtime overlays, run receipts, reservations and workspaces.
- Git is authoritative for local checkout state.
- GitHub is authoritative for observed PR metadata, review state, mergeability and checks.
- CI is authoritative only for the workflow, job, run and commit it executed.
- Cabinet and RepoBrief are evidence providers, not command authorities.
- Human or operator decisions must be recorded as explicit review, PR, registry or receipt evidence.

If sources disagree, automation must report the conflict, identify the source owners and stop or create a repair candidate. It must not silently smooth the contradiction.

## Status vocabulary

| Status | Meaning | Source owner |
|---|---|---|
| `planned` | Task exists but is not ready for dispatch. | Registry |
| `ready` | Task may be selected if claims and queue rules allow it. | Registry |
| `assigned` | A worker or run accepted a bounded envelope. | State store / dispatcher |
| `running` | Work is actively executing under a run envelope. | State store / dispatcher |
| `dispatching` | Dispatcher is preparing or starting execution. | Dispatcher |
| `pr_observed` | A PR was observed for a branch or task. | GitHub observer |
| `ci_unknown` | No current check result is available for the relevant head. | GitHub observer / CI |
| `ci_pending` | A relevant check is in progress. | CI / GitHub observer |
| `ci_passed` | A check passed for a specific head. | CI / GitHub observer |
| `ci_failed` | A check failed for a specific head. | CI / GitHub observer |
| `review_blocked` | Review requested changes or unresolved finding exists. | GitHub observer / reviewer |
| `merged` | PR or branch change is merged into target branch. | GitHub / Git |
| `verified` | Task acceptance is satisfied with evidence and current verification stamp. | Registry |
| `stale` | Stored state no longer matches current source fact or plan hash. | Doctor / registry truth |
| `completion_ready` | All initiative tasks are verified but initiative is not completed. | Doctor lifecycle diagnosis |
| `completed` | Initiative is closed with completed commitment. | Registry |

## Event semantics

Events are observations unless an explicit command gives mutation authority.

| Event | Minimum binding |
|---|---|
| `task_claimed` | task id, claimant, time, source checkout and claim set. |
| `run_started` | run id, task id, worker, branch or worktree when applicable. |
| `heartbeat` | run id, timestamp and source. |
| `receipt_written` | run id, receipt path or hash and task id. |
| `workspace_created` | owner or run id, path, branch, head and base. |
| `pr_seen` | repo, PR number, head ref, head SHA, base ref and observation time. |
| `check_seen` | repo, PR or head SHA, workflow or job, state and run URL. |
| `review_seen` | repo, PR, reviewer or source, decision and head SHA when available. |
| `merge_seen` | repo, PR, merge or squash commit and base branch. |
| `task_verified` | task id, acceptance evidence and verification stamp. |
| `initiative_closed` | initiative id and lifecycle diagnosis or explicit close command. |
| `repair_candidate` | source finding, recommended action and mutation authority false by default. |

Raw evidence must remain attributable to its source. A derived projection may be updated, but it must not erase the source evidence.

## Forbidden implicit powers

This baseline forbids these powers unless a later separate contract grants a narrower evidence-bound gate:

- automatic merge;
- automatic branch deletion;
- automatic worktree cleanup;
- automatic task verification;
- automatic initiative completion;
- automatic queue mutation;
- treating live-register focus as queue truth, claim authority or dispatch permission;
- automatic dispatch of unsafe or claim-conflicting tasks;
- automatic PR creation from observation alone;
- automatic Cabinet import;
- automatic RepoBrief refresh with Git mutation;
- automatic runtime deploy, restart, migration or smoke;
- treating CI pass as general correctness;
- treating a merged PR as task completion without registry evidence;
- treating a doctor repair candidate as permission to mutate;
- treating stale metadata as fresh truth.

## Allowed baseline automation

Within this contract, Bureau may automate bounded non-destructive surfaces:

- read registry, queue and lifecycle diagnostics;
- read state-store integrity, receipts, envelopes, reservations and workspaces;
- read local Git checkout facts;
- read GitHub PR, check, review and merge observations;
- project status with source attribution;
- record gitless live-register focus/candidate events in the state store;
- emit repair candidates;
- run explicit validation commands when invoked by an operator or CI;
- create reviewable PRs for registry or documentation repairs;
- remove queue entries only inside a reviewed registry diff that also carries task verification evidence.

Any external-state change must be visible as a command, receipt, PR or reviewable diff.

## Conflict handling

When facts conflict, automation must choose safety over continuity:

1. Identify conflicting facts and source owners.
2. Prefer the owner for each fact class.
3. Mark the projection stale or unhealthy if the conflict affects normal work selection.
4. Create a repair candidate when the repair is known.
5. Avoid mutation unless an explicit repair command or reviewed PR applies it.
6. Preserve evidence of the conflict and repair.

Examples: verified task still queued; merged PR without task verification; CI pass for an older head; active initiative with all tasks verified; missing workspace for an active run.

## Acceptance mapping

- `control-tower-model`: satisfied by the organ model and source authority rules.
- `status-vocabulary`: satisfied by the shared vocabulary covering assigned, running, dispatching, PR observed, CI unknown, review blocked, merged, verified and stale states.
- `forbidden-powers`: satisfied by the forbidden implicit powers and allowed baseline automation sections.

## Does not establish

This contract does not implement new automation, grant merge authority, grant cleanup authority, grant completion authority, prove runtime correctness, prove CI sufficiency, prove security correctness, prove registry truth in other repos or authorize dispatch beyond existing claim and queue rules.
