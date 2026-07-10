# Chronik Heimlern Outcome Bridge v1

Status: active
Date: 2026-07-10

## Goal

Connect heimlern to real operator outcomes through Chronik as the append-only historical transport, without giving heimlern live routing authority or letting Bureau become an event store.

## Decision

Primary organ: Chronik.

Reason: heimlern needs historical outcomes to produce useful offline learning reports and policy-weight proposals. Chronik owns event history and is therefore the safest first integration point. Bureau should coordinate review and task truth; Leitstand should display derived reports only after a source-grounded Chronik export exists.

## Boundary

The bridge is offline and proposal-only.

Allowed:

- Grabowski emits or exposes routing decisions, friction and execution receipts.
- Chronik stores or exports redacted operator outcome events as append-only history.
- heimlern reads exported outcomes and emits learning reports or non-applying weight proposals.
- Bureau tracks review decisions and follow-up work.
- Leitstand displays source identity, freshness and proposal status.

Forbidden:

- heimlern applies policy or route weights.
- Chronik rewrites historical outcomes to satisfy heimlern.
- Bureau becomes the raw outcome ledger.
- Leitstand implies authority over proposals.
- Grabowski changes routing policy from heimlern output without a separate reviewed gate.

## Registered sequence

1. `CHRONIK-HEIMLERN-OUTCOME-BRIDGE-V1-T001` — audit Chronik outcome export surface.
2. `CHRONIK-HEIMLERN-OUTCOME-BRIDGE-V1-T002` — define redacted operator outcome event/export contract.
3. `CHRONIK-HEIMLERN-OUTCOME-BRIDGE-V1-T003` — implement a review-only heimlern consumer smoke over Chronik export artifacts.
4. `CHRONIK-HEIMLERN-OUTCOME-BRIDGE-V1-T004` — register Bureau review status and later Leitstand read-only projection.

## Organ roles

| Organ | Owns | May do | Must not do |
| --- | --- | --- | --- |
| Grabowski | execution decisions, friction, receipts | emit redacted outcome source material | accept learned weights directly |
| Chronik | append-only historical events | store/export redacted outcome events | orchestrate or mutate routing |
| heimlern | analysis and proposals | generate reports/proposals offline | own tasks, history or policy application |
| Bureau | commitments, task truth, review gates | coordinate acceptance/rejection follow-ups | store raw outcome history |
| Leitstand | read-only visibility | display source-grounded reports and freshness | become proposal authority |

## Acceptance boundary

The initiative is complete only when a real or fixture-equivalent Chronik-shaped export can be consumed by heimlern and produces a bounded report/proposal result with source identity, freshness, digest and non-claims.

## Non-claims

This plan does not establish:

- routing policy superiority;
- sufficient production sample size;
- automatic application permission;
- Chronik runtime deployment readiness;
- Leitstand projection correctness;
- Grabowski routing mutation readiness.
