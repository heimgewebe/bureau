# Grabowski operator grips PR slices v1

Status: registered
Source plan: heimgewebe/grabowski:docs/operator-grip-foundation-plan-v1.md
Target repo: heimgewebe/grabowski
Initiative: GRABOWSKI-GRIPS-V1

This document mirrors the schema-backed registry entries for human review. The source repo owns the fachliche plan; Bureau owns this planned implementation list.

## Registered slices

| ID | Registry state | Planning stage | Branch | Dependencies |
| --- | --- | --- | --- | --- |
| GRIP-001 | ready | ready | feat/operator-grip-foundation-v1 | - |
| GRIP-002 | planned | planned | feat/branch-pr-publishing-grips-v1 | GRIP-001 |
| GRIP-003 | planned | planned | feat/privileged-grip-receipts-v1 | GRIP-001 |
| GRIP-004 | planned | planned | feat/worktree-navigation-grips-v1 | GRIP-001 |
| GRIP-005 | inbox | candidate | feat/durable-grabowski-scout-v1 | GRIP-001, GRIP-002 |
| GRIP-006 | inbox | candidate | feat/mechanic-durable-actions-v1 | GRIP-002, GRIP-005 |
| GRIP-007 | inbox | candidate | feat/captain-privileged-actions-v1 | GRIP-003, GRIP-006 |
| GRIP-008 | planned | planned | docs/replace-restrictive-autonomy-language-v1 | GRIP-001 |

## First ready slice

GRIP-001 is the first claimable slice. It establishes the grip spec model, grip receipt model, grip runner skeleton and initial read-only grips (`repo-orient`, `pr-check-readiness`, `post-merge-sync`). Its non-goals keep merge, deploy and cleanup-apply out of the first PR.

## Import / schema note

The current Bureau task schema has no native `candidate` state. Candidate slices are therefore represented as `state: inbox` with `metadata.planning_stage: candidate`. A future schema migration may promote this to a first-class state without changing the planning semantics.
