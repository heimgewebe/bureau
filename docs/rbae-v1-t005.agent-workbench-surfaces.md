# RBAE-V1-T005 Agent Code Workbench Surfaces

Status: closes `RBAE-V1-T005`.

## Purpose

This contract defines small read-only surfaces for agent code work. The surfaces are shaped for CLI or MCP-style access, but they do not create a new authority layer. They reuse the RepoBrief MCP boundary from `RBV1-T010` and the code contracts from `RBAE-V1-T001` through `RBAE-V1-T004`.

## Boundary

All surfaces are read-only over existing RepoBrief artifacts. A call may read an existing bundle, index, manifest, relation file, symbol file, profile result or contract document. A call must not refresh a snapshot, write files, change Git state, run tests as a verdict, create or update a pull request, or infer patch correctness.

Every result must include availability/freshness where relevant and a `non_claims` list.

## Surface catalog

| Surface | Input | Output | Existing owner |
|---|---|---|---|
| `code_task_evidence.resolve` | task kind, profile, available roles | required/recommended evidence and forbidden inference | `RBAE-V1-T001` |
| `change_plan.validate` | Agent Change Plan object | pass/warn/fail, missing fields, stop conditions | `RBAE-V1-T002` |
| `impact_map.get` | target path/symbol/range and bundle stem | likely impacted surfaces, evidence links, gaps | `RBAE-V1-T003` |
| `required_reading.resolve_code_profile` | task kind and bundle/profile status | required reading preflight result | `RBAE-V1-T004` |
| `symbol.get` | symbol id, path, name or range | symbol record, range refs, availability | `RBV1-T016` |
| `references.get` | path/symbol/range | guarded relation/reference candidates and gaps | `RBV1-T014` |
| `tests_for_target.get` | target path/symbol/range | candidate tests and missing-test evidence | `RBV1-T014`, `RBAE-V1-T003` |
| `graph.status` | bundle stem/profile | graph availability and whether graph evidence may be used | `RBV1-T015` |
| `artifact.get` | bundle stem and artifact role | existing artifact reference only | `RBV1-T010` |
| `range.get` | bundle stem and range ref | bounded source excerpt from existing artifact | `RBV1-T010` |
| `query_existing_index` | query and existing index path | bounded deterministic search results | `RBV1-T010` |

## Result rules

A workbench result should be small and structured:

- `kind`: stable result kind.
- `version`: contract version.
- `status`: `pass`, `warn`, `fail`, `missing`, `available` or `not_applicable`.
- `input`: normalized input values.
- `availability`: source status for every underlying evidence source.
- `items`: bounded results, if applicable.
- `gaps`: missing, stale, invalid or profile-excluded evidence.
- `citations` or `range_refs`: source links when available.
- `non_claims`: what the result does not prove.
- `handoff`: optional external checks or review steps.

Large dumps must not be returned by default. A surface should prefer ids, paths, ranges and short excerpts over full artifact bodies.

## MCP alignment

The surfaces map onto the existing RepoBrief MCP boundary:

- resources expose existing artifacts or bounded projections;
- tools are read-only helpers over those artifacts;
- `snapshot_create`, if ever available, remains a separate explicit write exception and is not part of this workbench surface;
- the workbench must not hide patch, test, shell or PR authority behind a read-only call.

## Availability behavior

Missing or stale evidence is a first-class result, not an implicit failure to search harder. A surface must report:

- `available` when the source exists and is usable;
- `missing` when expected evidence is absent;
- `stale` when provenance or freshness does not match;
- `not_generated` when the artifact was not generated;
- `profile_excluded` when the selected profile excludes it;
- `invalid` when the source exists but fails shape or integrity checks.

Stale or invalid evidence may appear in `gaps`, but must not support a concrete impact claim.

## Minimal example

```json
{
  "kind": "repobrief.workbench.impact_map.get",
  "version": "v1",
  "status": "warn",
  "input": {"path": "src/example.py", "symbol": "build_alias_command"},
  "availability": {"symbol_index": "available", "graph": "not_generated"},
  "items": [{"surface": "symbol", "path": "src/example.py", "range_ref": "file:src/example.py#L10-L42"}],
  "gaps": [{"source": "graph", "status": "not_generated"}],
  "non_claims": ["runtime_correctness", "test_sufficiency", "review_completeness", "merge_readiness"]
}
```

## Acceptance mapping

- `rbae-v1-t005-read-only`: satisfied by the boundary and MCP alignment sections.
- `rbae-v1-t005-ergonomics`: satisfied by the catalog, result rules and bounded example result.
- `rbae-v1-t005-mcp-alignment`: satisfied by explicit reuse of `RBV1-T010` and the RepoBrief MCP boundary.

## Does not establish

This contract does not establish runtime correctness, test sufficiency, dependency completeness, review completeness, merge readiness, security correctness, patch correctness or agent patch quality.
