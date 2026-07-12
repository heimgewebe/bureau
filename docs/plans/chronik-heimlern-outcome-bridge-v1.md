# Chronik Heimlern Outcome Bridge v1

Status: active  
Date: 2026-07-12

## Goal

Connect Heimlern to real operator outcomes through Chronik as the append-only historical transport, without giving Heimlern live routing authority or letting Bureau become an event store. Before any review or display product is expanded, prove on a bounded real sample whether Heimlern materially improves an operator diagnosis or decision.

## Decision

Primary transport organ: Chronik.

Reason: Heimlern needs historical outcomes to produce useful offline learning reports and policy-weight proposals. Chronik owns the append-only envelope and history, while Heimlern remains the canonical routing-outcome payload owner. Bureau coordinates task and review truth. Leitstand may display derived reports only after source-grounded real outcomes establish actual operator value.

The sequencing rule is now explicit:

> Real outcome -> bounded usefulness evaluation -> only then review workflow or display.

A technically valid fixture path is necessary but does not justify a product surface.

## Boundary

The bridge is offline and proposal-only.

Allowed:

- Grabowski emits or exposes bounded routing decisions, friction and execution receipts through an explicit opt-in producer.
- Chronik stores or exports a digest-bound envelope as append-only history.
- Heimlern owns and validates the embedded routing-outcome payload, then reads exported outcomes and emits learning reports or non-applying weight proposals.
- A manual operator review classifies the result as useful, insufficient-evidence or misleading.
- Bureau tracks the usefulness verdict and, only after a positive verdict, proposal review decisions and follow-up work.
- Leitstand may display source identity, freshness and proposal status only after a positive usefulness gate.

Forbidden:

- Heimlern applies policy or route weights.
- Chronik rewrites historical outcomes to satisfy Heimlern.
- Bureau becomes the raw outcome ledger.
- Leitstand implies authority over proposals or is built merely because a fixture path exists.
- Grabowski changes routing policy from Heimlern output without a separate reviewed gate.
- Samples are duplicated or model scope is expanded merely to manufacture a positive usefulness result.

## Registered sequence

1. `CHRONIK-HEIMLERN-OUTCOME-BRIDGE-V1-T001` — audit Chronik outcome export surface.
2. `CHRONIK-HEIMLERN-OUTCOME-BRIDGE-V1-T002` — define redacted operator outcome event/export contract.
3. `CHRONIK-HEIMLERN-OUTCOME-BRIDGE-V1-T003` — implement a dedicated typed, review-only Heimlern consumer over Chronik export artifacts.
4. `CHRONIK-HEIMLERN-OUTCOME-BRIDGE-V1-T005` — add the smallest explicit, opt-in Grabowski producer and evaluate Heimlern on a bounded real-outcome sample.
5. `CHRONIK-HEIMLERN-OUTCOME-BRIDGE-V1-T004` — only after a positive T005 verdict, register Bureau proposal review status and an optional Leitstand read-only projection.

## Live audit result

T001 found that Chronik already has generic append-only ingest and cursor export, but the bridge lacked a bounded envelope, a typed Heimlern consumer and a real Grabowski producer. Chronik was inactive locally at audit time. The detailed evidence is in `docs/reports/chronik-heimlern-outcome-bridge-t001-audit.md`.

T002 is implemented by Chronik PR #214. Its envelope pins the Heimlern-owned payload schema, validates canonical payload and event digests, rejects raw, secret and private-path material and requires consumers to recompute freshness.

T003 is implemented by Heimlern PR #200. The dedicated consumer validates both contract layers, canonical identities and digests, recomputes freshness, deduplicates evidence, rejects sensitive input and emits only review-only reports, valid proposal candidates or typed insufficient-evidence results.

The current sequenced task is T005. T004 was moved behind it because review and display infrastructure before a real usefulness result would add maintenance without proving operator value.

## Usefulness gate

T005 must produce an auditable proof with all of the following:

- at least ten real, redacted Grabowski outcomes;
- both successful and blocked or failed execution results;
- deterministic Heimlern and Chronik contract validation;
- no raw logs, command output, secrets or private absolute paths;
- a manual comparison between Heimlern output and the operator's own review;
- one explicit verdict: `useful`, `insufficient-evidence` or `misleading`;
- the diagnosis or decision that was improved, or the reason no improvement occurred.

A positive result means the output changes or materially sharpens a diagnosis or decision with traceable evidence. Merely producing a schema-valid proposal is not sufficient.

If the result is insufficient or misleading, Heimlern remains frozen and T004 is not promoted. No broader event bus, dashboard, model layer, event family or automatic collection is justified by that failure.

## Organ roles

| Organ | Owns | May do | Must not do |
| --- | --- | --- | --- |
| Grabowski | execution decisions, friction, receipts | emit redacted outcome source material | accept learned weights directly |
| Chronik | append-only transport envelope and history | store/export digest-bound envelopes | own the routing payload, orchestrate or mutate routing |
| Heimlern | routing-outcome payload, analysis and proposals | validate payloads and generate reports/proposals offline | own tasks, history or policy application |
| Bureau | commitments, task truth, review gates | record usefulness and later proposal-review decisions | store raw outcome history |
| Leitstand | read-only visibility | display source-grounded reports after a positive usefulness gate | become proposal authority |

## Acceptance boundary

The contract and consumer path is already proven at fixture level. The initiative is useful only when a separately reviewed Grabowski producer supplies real outcomes and a bounded evaluation demonstrates operator value. A live round trip alone proves transport, not usefulness.

## Non-claims

This plan does not establish:

- routing policy superiority;
- sufficient production sample size;
- automatic application permission;
- Chronik runtime deployment readiness;
- Leitstand projection correctness;
- Grabowski routing mutation readiness.
