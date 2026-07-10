# Chronik Heimlern Outcome Bridge v1

Status: active
Date: 2026-07-10

## Goal

Connect Heimlern to real operator outcomes through Chronik as the append-only historical transport, without giving heimlern live routing authority or letting Bureau become an event store.

## Decision

Primary transport organ: Chronik.

Reason: Heimlern needs historical outcomes to produce useful offline learning reports and policy-weight proposals. Chronik owns the append-only envelope and history, while Heimlern remains the canonical routing-outcome payload owner. Bureau coordinates review and task truth; Leitstand may display derived reports only after source-grounded exports exist.

## Boundary

The bridge is offline and proposal-only.

Allowed:

- Grabowski emits or exposes routing decisions, friction and execution receipts.
- Chronik stores or exports a digest-bound envelope as append-only history.
- Heimlern owns and validates the embedded routing-outcome payload, then reads exported outcomes and emits learning reports or non-applying weight proposals.
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
3. `CHRONIK-HEIMLERN-OUTCOME-BRIDGE-V1-T003` — implement a dedicated typed, review-only Heimlern consumer over Chronik export artifacts.
4. `CHRONIK-HEIMLERN-OUTCOME-BRIDGE-V1-T004` — register Bureau review status and later Leitstand read-only projection.
5. `CHRONIK-HEIMLERN-OUTCOME-BRIDGE-V1-T005` — add an explicit, opt-in Grabowski producer for real redacted outcomes.

## Live audit result

T001 found that Chronik already has generic append-only ingest and cursor export, but the bridge lacked a bounded envelope, a typed Heimlern consumer and a real Grabowski producer. Chronik was inactive locally at audit time. The detailed evidence is in `docs/reports/chronik-heimlern-outcome-bridge-t001-audit.md`.

T002 is implemented by Chronik PR #214. Its envelope pins the Heimlern-owned payload schema, validates canonical payload/event digests, rejects raw/secret/private-path material and requires consumers to recompute freshness.

## Organ roles

| Organ | Owns | May do | Must not do |
| --- | --- | --- | --- |
| Grabowski | execution decisions, friction, receipts | emit redacted outcome source material | accept learned weights directly |
| Chronik | append-only transport envelope and history | store/export digest-bound envelopes | own the routing payload, orchestrate or mutate routing |
| Heimlern | routing-outcome payload, analysis and proposals | validate payloads and generate reports/proposals offline | own tasks, history or policy application |
| Bureau | commitments, task truth, review gates | coordinate acceptance/rejection follow-ups | store raw outcome history |
| Leitstand | read-only visibility | display source-grounded reports and freshness | become proposal authority |

## Acceptance boundary

The contract/consumer path is proven when a fixture-equivalent Chronik envelope is consumed by Heimlern and produces a bounded report/proposal result with source identity, recomputed freshness, digests and non-claims. A separate Grabowski producer and reviewed deployment are required before any real-outcome or live round-trip claim.

## Non-claims

This plan does not establish:

- routing policy superiority;
- sufficient production sample size;
- automatic application permission;
- Chronik runtime deployment readiness;
- Leitstand projection correctness;
- Grabowski routing mutation readiness.
