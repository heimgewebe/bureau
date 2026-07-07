# RepoBrief Code Impact Map v1

Status: closes Bureau task `RBAE-V1-T003`.

## Purpose

The Code Impact Map is a read-only planning surface for coding agents. It collects available RepoBrief evidence that may indicate what a proposed code change touches: symbols, references, imports, tests, contracts, entrypoints, docs, runbooks and risky boundaries.

It is a navigation and risk surface. It is not a dependency graph proof, test coverage proof, runtime model, security review, patch approval or merge verdict.

## Inputs and existing owners

Impact Map v1 reuses existing RepoBrief and roadmap surfaces instead of duplicating their ownership.

| Surface | Existing owner | Impact Map use |
|---|---|---|
| Canonical source ranges | RepoBrief canonical brief source and range refs | Bind concrete code claims to paths and ranges when available. |
| Relation signals | `RBV1-T014` relation guard goldset and relation-card surfaces | Surface test-by-name/path, schema validation and surface-check hints as guarded relation evidence. |
| Graph availability | `RBV1-T015` graph availability | Report whether graph signals are available, stale, missing, not generated or profile-excluded. |
| Python symbols | `RBV1-T016` Python AST symbol index | Locate likely definitions, methods, modules and source ranges. |
| Agent task evidence | `RBAE-V1-T001` evidence matrix | Decide which evidence is required, recommended or insufficient for a code task. |
| Agent change plan | `RBAE-V1-T002` change plan contract | Consume target behavior and candidate changes; output impact evidence and gaps. |

## Contract shape

A strict Impact Map v1 object should contain these fields. This document defines the contract; it does not implement a parser or a Lenskit schema.

| Field | Required | Meaning |
|---|---:|---|
| `schema_version` | yes | Contract version. This document defines version `1`. |
| `kind` | yes | Constant such as `repobrief.code_impact_map`. |
| `task_kind` | yes | Code-task profile from the evidence matrix. |
| `target` | yes | Target behavior, symbol, path, range or change-plan candidate being mapped. |
| `source_status` | yes | Availability, freshness and staleness status for each evidence source. |
| `impact_surfaces` | yes | Grouped likely affected files, symbols, tests, contracts, entrypoints, docs and risky boundaries. |
| `evidence_links` | yes | Range refs, citations, relation ids, graph ids or symbol ids backing each entry. |
| `gaps` | yes | Missing, stale or not-generated evidence and resulting confidence limits. |
| `stop_conditions` | yes | Conditions under which an agent must stop or escalate before patching. |
| `handoff` | yes | How the map feeds a Change Plan, Patch Evaluation Sidecar, CI or review. |
| `non_claims` | yes | Assertions the map explicitly does not establish. |

## Source status model

Every source must be reported independently. The map must distinguish these states instead of collapsing them into "no impact".

- `available`: source exists and is provenance-coherent enough for diagnostic use.
- `stale`: source exists but does not match the current snapshot, head or provenance.
- `missing`: source was expected for the task or profile but is absent.
- `not_generated`: source was not generated for this snapshot.
- `profile_excluded`: source is excluded by the selected RepoBrief profile.
- `not_applicable`: source does not apply to this task.
- `blocked_by_missing_source`: a required underlying file, range or artifact is absent.
- `blocked_by_missing_provenance`: source cannot be compared because provenance is missing.
- `invalid`: source exists but fails schema, path, range or integrity checks.

A stale or invalid source may be listed as a diagnostic gap, but it must not be used as evidence for a concrete impact claim.

## Impact surfaces

Impact Map v1 may include these surface groups.

### Symbols

- candidate definitions;
- methods and nested functions;
- module-level functions;
- class boundaries;
- known range refs;
- symbol index availability and freshness.

### References and relations

- static references when available;
- relation-card hints such as tests by name/path;
- schema and contract validation hints;
- surface-check hints;
- graph-adjacent nodes only when graph availability is `available`.

### Tests

- tests suggested by relation signals;
- tests suggested by path or name conventions;
- tests suggested by changed contracts;
- missing test evidence when no relation or naming signal exists.

### Contracts and schemas

- JSON schemas;
- CLI, MCP and API contracts;
- manifest, bundle, trace or evaluation contracts;
- compatibility surfaces touched by candidate changes.

### Entrypoints and runtime boundaries

- CLI entrypoints;
- MCP or API entrypoints;
- workflow and CI entrypoints;
- runtime and runbook surfaces;
- migration, state, secret, network, permission or auth boundaries.

### Documentation and runbooks

- architecture docs;
- proof docs;
- user or operator docs;
- generated or index docs that may need update.

## Evidence rules

Every concrete impact entry should record:

- `surface_type`;
- path or contract id;
- symbol or range when available;
- source ids backing the entry;
- source status;
- whether the evidence is required, supporting or weak;
- what the evidence does not establish.

The map must not infer full transitive impact from one local range. It may say "likely impacted" or "candidate impacted" only with evidence and status.

## Stop conditions

A coding agent should stop or escalate when:

- required symbol or range evidence is missing;
- graph or relation evidence is stale but needed for the task;
- a candidate change touches security, secrets, migrations, production runtime, destructive state, permissions, auth or network behavior;
- no plausible tests or external checks can be identified for behavioral change;
- impact spans unrelated subsystems;
- evidence conflicts with the proposed change;
- the map depends on live state not present in the snapshot;
- an external authority such as CI, Patch Evaluation, reviewer or operator is required.

## Handoff

The Impact Map may feed:

- an Agent Change Plan as supporting evidence and gaps;
- Patch Evaluation Sidecar as evaluation intent;
- CI as a checklist of likely relevant tests and checks;
- human or PR review as a structured risk map.

It must not itself apply a patch, run tests, create a PR, approve a review, or authorize a merge.

## Minimal example

```json
{
  "schema_version": 1,
  "kind": "repobrief.code_impact_map",
  "task_kind": "code_bugfix",
  "target": {
    "path": "src/example.py",
    "symbol": "build_alias_command",
    "range_ref": "file:src/example.py#L10-L42"
  },
  "source_status": {
    "symbol_index": "available",
    "relation_cards": "available",
    "graph": "stale",
    "canonical_ranges": "available"
  },
  "impact_surfaces": {
    "symbols": [
      {
        "path": "src/example.py",
        "symbol": "build_alias_command",
        "range_ref": "file:src/example.py#L10-L42",
        "evidence": ["symbol_index:py:src:example.py:function:build_alias_command"]
      }
    ],
    "tests": [
      {
        "path": "tests/test_example_alias.py",
        "reason": "relation hint by path/name",
        "evidence": ["relation:test_by_path"]
      }
    ],
    "contracts": [],
    "entrypoints": [],
    "docs": [],
    "risky_boundaries": []
  },
  "gaps": [
    {
      "source": "graph",
      "status": "stale",
      "effect": "Graph adjacency is visible as a gap but not used for impact evidence."
    }
  ],
  "stop_conditions": ["live PR diff missing", "no external check identified"],
  "handoff": ["agent_change_plan", "patch_evaluation_sidecar", "github_pr_review"],
  "non_claims": [
    "complete_dependency_coverage",
    "complete_test_coverage",
    "runtime_correctness",
    "security_correctness",
    "review_completeness",
    "merge_readiness",
    "agent_patch_quality_proven"
  ]
}
```

## Acceptance mapping

- `rbae-v1-t003-scope`: satisfied by the source status model distinguishing available, stale, missing, not-generated, profile-excluded, invalid and provenance-blocked evidence.
- `rbae-v1-t003-no-completeness-claim`: satisfied by the evidence rules, stop conditions and non-claims.
- `rbae-v1-t003-existing-owners`: satisfied by the explicit dependency on `RBV1-T014`, `RBV1-T015`, `RBV1-T016`, `RBAE-V1-T001` and `RBAE-V1-T002`.

## Does not establish

Impact Map v1 does not establish complete dependency coverage, complete test coverage, runtime correctness, import success, security correctness, review completeness, regression absence, retrieval improvement, patch correctness, agent patch quality or merge readiness.
