# semantAH Usefulness Registration v1

Status: planned  
Owner layer: Bureau coordination  
Created: 2026-07-05  
Issue: #77  
Supersedes: closed PR #75; abandoned branch `semantah-usefulness-registration-v2`

## Purpose

Register semantAH usefulness work in Bureau without inflating semantAH into an operating organ before evidence exists.

The target role is a Semantic Evidence Service: semantAH may become useful if it can ingest real repository documentation, preserve source-grounded references, and retrieve better evidence than a simple keyword baseline. It must not be treated as a knowledge graph authority, auto-linking layer, runtime truth source, or review oracle at this stage.

## Corrected assumptions

1. RepoBrief is not a free-form chat audit. In the Lenskit context it is a deterministic, citable repository snapshot and agent briefing bundle.
2. A chat audit may seed hypotheses, but it does not establish repository state, completeness, runtime correctness, test sufficiency, or that the repository was understood.
3. semantAH usefulness must be proven before any Bureau, Cabinet, HausKI, Leitstand, or Chronik consumer relies on it.
4. Bureau registration is a planning act only. It does not make semantAH ready or operational.

## Organ boundaries

| Organ | Owns | Must not claim here |
|---|---|---|
| Bureau | commitments, order, claims, completion, lifecycle state | repository truth, semantic quality, runtime correctness |
| Lenskit / RepoBrief | deterministic, citable repository snapshot evidence | completeness beyond the captured snapshot, runtime truth |
| semantAH | target repository and future Semantic Evidence Service implementation | operational readiness before evidence |
| Cabinet / HausKI / Leitstand / Chronik | later read-only consumers or decision surfaces | implicit execution, implicit trust, write authority from semantAH output |
| Grabowski | bounded execution after Bureau has schema-valid task material | bypassing Bureau lifecycle gates |

## Evidence sequence

### Phase 1 — Evidence anchor

Goal: establish a real RepoBrief snapshot for `heimgewebe/semantAH` before task readiness.

Required evidence:

- deterministic RepoBrief or equivalent Lenskit snapshot artifact;
- cited commit and manifest identity;
- explicit non-claims about truth, completeness, runtime behavior, and test sufficiency.

### Phase 2 — Verified usefulness audit

Goal: turn the earlier semantAH audit into a Bureau artifact grounded in the snapshot.

Required evidence:

- every material claim points to snapshot evidence or is marked as hypothesis;
- contradictions remain visible;
- no consumer integration is proposed as ready.

### Phase 3 — Contract repair

Goal: repair semantAH contract drift before semantic expansion.

Expected work:

- identify public contracts and generated artifacts;
- remove or quarantine misleading contract claims;
- validate contract output against committed schemas or documented shapes.

### Phase 4 — Namespace canon

Goal: define and enforce a stable namespace canon before linking or graph behavior.

Expected work:

- identify source identity fields;
- define canonical namespace rules;
- test collision, alias, and duplicate handling.

### Phase 5 — Stub disempowerment

Goal: prevent placeholder pipelines from being mistaken for useful semantic evidence.

Expected work:

- mark stubs as non-authoritative;
- fail or warn when retrieval results come from placeholder content;
- document non-claims.

### Phase 6 — Real docs ingest MVP

Goal: ingest repository Markdown documentation with source-grounded references.

Expected work:

- define `index.store.v1` or equivalent store contract;
- preserve path, heading, range, commit, and digest identity;
- produce deterministic local artifacts.

### Phase 7 — Retrieval evaluation

Goal: compare semantAH retrieval against a keyword baseline before consumer use.

Required evidence:

- fixed evaluation corpus;
- fixed query set;
- measured hit quality, unsupported-claim risk, and failure modes;
- result at least equal to baseline on safety and materially better on selected semantic queries before any consumer adapter is considered.

### Phase 8 — Evidence-only observability

Goal: expose semantAH evidence health without granting decision authority.

Required evidence:

- health/status surfaces distinguish indexed evidence from semantic claims;
- stale or missing evidence is visible;
- no automated action follows from semantAH output.

### Phase 9 — Read-only consumer adapter

Goal: only after positive retrieval evidence, introduce a read-only adapter for a later consumer.

Constraints:

- no write path;
- no task readiness inflation;
- all consumer-facing claims remain source-grounded and dated.

## Stop rules

Stop or downgrade the initiative if:

- RepoBrief or equivalent deterministic snapshot evidence cannot be produced;
- semantAH cannot preserve source identity through ingest and retrieval;
- retrieval does not beat or at least safely match a keyword baseline;
- stubs or synthetic content remain indistinguishable from real evidence;
- consumers begin treating semantAH as truth, review approval, runtime state, or action authority.

## Does not establish

This plan does not establish:

- semantAH is useful;
- semantAH is ready as an operating organ;
- a knowledge graph is the right first implementation;
- RepoBrief is complete truth;
- any future task is ready;
- any consumer integration is warranted;
- tests are sufficient;
- runtime behavior is correct.
