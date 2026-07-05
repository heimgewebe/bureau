# Leitstand Visualization Layer Expansion v1

Status: planned  
Owner layer: Bureau coordination  
Created: 2026-07-05  
Primary repo: `heimgewebe/leitstand`  
Canonical map repo: `heimgewebe/cabinet`  
Evidence anchor: RepoBrief `leitstand-max-260705-0643`

## Purpose

Expand Leitstand into the ecosystem visualization layer without turning it into a second truth source, orchestration surface, or task dispatcher.

Leitstand should make ecosystem state legible: maps, source freshness, RepoBrief health, timeline context, insights, and reflexion surfaces. It should not own the semantics it displays. Cabinet, Chronik, semantAH, WGX, RepoBrief/Lenskit, Bureau, and GitHub remain primary for their domains.

## Current evidence

A fresh RepoBrief snapshot was created from `heimgewebe/leitstand` `origin/main` after PR `#144` was merged.

- Snapshot directory: `/home/alex/repos/merges/leitstand-max-260705-0643`
- Bundle manifest: `/home/alex/repos/merges/leitstand-max-260705-0643/leitstand-max-260705-0443_merge.bundle.manifest.json`
- Canonical dump: `/home/alex/repos/merges/leitstand-max-260705-0643/leitstand-max-260705-0443_merge.md`
- Agent reading pack: `/home/alex/repos/merges/leitstand-max-260705-0643/leitstand-max-260705-0443_merge.agent_reading_pack.md`
- Snapshot status: `ok`
- Basic preflight: `warn`, with no missing required or recommended artifacts
- Warning: claim-evidence sidecars were skipped; this is a RepoBrief validation limitation, not a Leitstand runtime claim
- Export safety: internal analysis only; export gate was not pass even though redaction was observed

This plan treats the RepoBrief as a navigation and evidence anchor. It does not establish runtime behavior, test sufficiency, review completeness, or that Leitstand is already a finished visualization layer.

## Corrected strategy

### Initial idea

Build a rendered ecosystem map view in Leitstand.

### Problem

A rendered map without source identity, freshness metadata, and explicit boundary language looks authoritative even when it is only a snapshot. That would blur the Cabinet/Leitstand split.

### Optimized path

Build the visualization layer in this order:

1. Source contract and artifact identity.
2. Read-only loader and empty-state behavior.
3. Raw Mermaid/source view with metadata.
4. Dashboard integration and freshness warning.
5. Rendered SVG/HTML consumption.
6. Cross-view linking by stable IDs.
7. RepoBrief/rLens health surface.

The first useful implementation can show raw Mermaid plus metadata before it renders beautiful diagrams. That is safer than a polished but epistemically ambiguous view.

## Organ boundaries

| Organ | Owns | Leitstand may do | Leitstand must not do |
|---|---|---|---|
| Cabinet | ecosystem-map semantics, registry inputs, generated Mermaid projection | display pinned map artifacts and link back | edit map semantics or infer claim truth |
| Leitstand | read-only visualization, freshness display, dashboard composition | render views, show provenance, flag stale data | orchestrate, dispatch, mutate external systems |
| Schauwerk | presentation/publishing surfaces | later provide approved rendered assets | become Cabinet replacement |
| RepoBrief / Lenskit | citable repository snapshot bundles | expose bundle health and freshness | prove repository truth or runtime behavior |
| Chronik | event history | feed timeline and incident context | be rewritten or backfilled from Leitstand |
| semantAH | semantic evidence outputs | feed insight surfaces when evidence-grounded | be treated as truth oracle |
| WGX / GitHub / CI | repo health and check state | feed status and links | be triggered by default from Leitstand |
| Bureau | task ordering and lifecycle | hold this roadmap and task sequence | let visualization tasks bypass lifecycle gates |

## Phase plan

### Phase 0 — Registration and evidence anchor

Goal: record the plan and preserve the RepoBrief evidence used to produce it.

Status: this Bureau slice.

Acceptance:

- plan exists in Bureau;
- initiative exists in Bureau registry;
- tasks are ordered and not inserted into active queue by default;
- RepoBrief evidence path and non-claims are recorded.

### Phase 1 — Cabinet map artifact contract

Goal: stop Leitstand from guessing source paths.

Expected output:

- a Cabinet-produced or Cabinet-versioned manifest such as `cabinet.ecosystem_map_artifact.v1`;
- fields for source commit, generated_at, artifact paths, digests, and non-claims;
- validation in Cabinet CI.

Why first: the viewer should consume a stable contract, not implicit local paths.

### Phase 2 — Leitstand read-only loader and empty-state contract

Goal: introduce the consumer boundary without a polished visualization.

Expected output:

- `src/controllers/ecosystemMap.ts` or equivalent;
- loader reads a pinned local artifact or manifest path configured by environment or fixture;
- missing/corrupt input produces explicit empty or degraded state;
- tests cover valid, missing, corrupt, and stale cases;
- no external fetch, no Git command, no mutation.

### Phase 3 — `/ecosystem-map` source view

Goal: make the map viewable from Leitstand as a source-grounded page.

Expected output:

- route `/ecosystem-map`;
- view shows readable overview and registry projection, initially as Mermaid source or pre-render-safe blocks;
- source path, source commit, digest, retrieval time, and boundary note are visible;
- dashboard card links to the view;
- tests assert boundary copy and absence of action buttons.

### Phase 4 — Rendering handoff

Goal: show rendered diagrams without making Leitstand the render authority.

Preferred path:

- Cabinet or Schauwerk produces SVG/HTML artifacts from the Mermaid source;
- Leitstand consumes those artifacts read-only with digest and freshness metadata.

Fallback path:

- Leitstand performs client-side Mermaid rendering only for display;
- source Mermaid and digest remain visible;
- render success is not presented as map validity.

### Phase 5 — Cross-view identity mapping

Goal: make the map useful as a navigation surface.

Expected output:

- stable IDs shared with Cabinet nodes where possible;
- map nodes link to Anatomy, Timeline, Insights, Reflexion, and Ops only when a deterministic ID mapping exists;
- no heuristic deep links without explicit degraded labels;
- tests cover unknown IDs and stale mapping.

### Phase 6 — RepoBrief/rLens bundle observability

Goal: make repository context health visible in Leitstand.

Expected output:

- read-only view listing RepoBrief bundles by repo;
- status/pass/warn/fail, snapshot freshness, export-safety status, and required artifacts shown;
- links to agent reading pack and canonical dump path for local operators;
- export-safety failures are visible and block public-share framing.

### Phase 7 — Real-ops pilot and rollout

Goal: validate with real Cabinet, RepoBrief, Chronik/WGX/semantAH artifacts.

Expected output:

- one internal pilot dashboard run;
- stale-source and missing-source incidents documented;
- performance and accessibility basics checked;
- no write paths introduced.

## Stop rules

Stop or downgrade the initiative if:

- a view requires Leitstand to edit Cabinet data;
- a rendered diagram is used as proof of runtime state, merge readiness, or claim truth;
- source commit/digest/freshness cannot be displayed;
- the UI adds dispatch/action buttons before an explicit external authority contract exists;
- Home, browser profile, secret, or private local content is needed for normal operation;
- RepoBrief export-safety failures are hidden from operators;
- cross-view links depend on heuristic ID guesses without degraded labeling.

## Task order

1. `LSV-V1-T001` — define Cabinet ecosystem-map artifact manifest.
2. `LSV-V1-T002` — implement Leitstand read-only map loader.
3. `LSV-V1-T003` — add `/ecosystem-map` source view and dashboard card.
4. `LSV-V1-T004` — add rendering handoff through Cabinet/Schauwerk-produced artifacts or clearly bounded client-side rendering.
5. `LSV-V1-T005` — add deterministic cross-view identity mapping.
6. `LSV-V1-T006` — add RepoBrief/rLens bundle observability view.
7. `LSV-V1-T007` — run internal pilot and record rollout evidence.

## Does not establish

This plan does not establish:

- Leitstand runtime correctness;
- ecosystem map truth;
- Cabinet registry correctness;
- semantAH usefulness;
- RepoBrief public export safety;
- test sufficiency;
- readiness to dispatch or mutate tasks from the UI;
- that all relevant visualization work is captured.
