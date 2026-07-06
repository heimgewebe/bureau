# rLens Agent Ecosystem Integration v1 — Implementation Record

Task: BUR-2026-002-T001 "Finalize first rLens integration slice"
Initiative: BUR-2026-002
Plan: docs/plans/rlens-agent-ecosystem-integration-v1.md
Vibe-Lab operator run card: vibe-lab: experiments/2026-07-01_operator-lab-loop/artifacts/run-002-rlens-agent-ecosystem/run-card.yml

This record documents the first (Phase 1) rLens hardening slice. It records
what was implemented and where the evidence lives. It does not claim merge
readiness, runtime correctness, or actual rLens freshness.

## Slice 1 — Lenskit preflight self-role

- `merger/lenskit/cli/cmd_agent_consumption.py` treats the supplied
  `--bundle-manifest` as the `bundle_manifest` self-role: the manifest is added
  as an available role without pretending it is listed inside its own artifacts
  array (`_load_roles_from_bundle_manifest`).
- Focused tests:
  `merger/lenskit/tests/test_cli_agent_consumption.py::test_cli_preflight_bundle_manifest_self_role_satisfies_surface_review`
  and the surrounding preflight suites.

## Slice 2 — Grabowski read-only rLens tools

- `src/grabowski_mcp.py` exposes read-only `rlens_bundle_discover`,
  `rlens_bundle_status`, and `rlens_freshness_check` (plus `rlens_context_pack`
  used by the Phase 2 bridge). Capabilities are registered in
  `src/grabowski_capabilities.py` and reflected in generated contracts
  (`contracts/capability-catalog.v1.json`, `docs/generated/operator-context.v1.json`,
  `contracts/publication-profiles.v1.json`).
- Tools are read-only: they read bundle metadata/sidecars without dumping
  content and compare bundle commit to live HEAD for freshness classification.
- Focused tests: `tests/test_rlens_bundles.py`.

## Slice 3 — Bureau rlens_context_ref schema support

- Optional `rlens_context_ref` is accepted (with `additionalProperties: false`)
  in `schemas/task.v1.schema.json`, `schemas/execution-envelope.v1.schema.json`,
  and `schemas/receipt.v1.schema.json` via a shared `$defs/rlens_context_ref`.
- Focused tests: `tests/test_rlens_context_ref.py` (accepts valid refs on task,
  envelope, and receipt; rejects unknown fields).

## Slice 4 — Vibe-Lab operator run card reference

- The optimized plan for this slice originated from the Vibe-Lab operator loop.
  The run card is referenced above and in the registry metadata for both the
  task (`registry/tasks/BUR-2026-002-T001.json` `metadata.operator_lab_run`) and
  the initiative (`registry/initiatives/BUR-2026-002.json` `metadata.operator_lab_run`).
- The run card records scope limited to the three slices above and a
  `decision: implement_first_slice`.

## Does not establish

Per the plan and the run card, this record does not establish:

- rLens improves agent quality;
- any existing dump is globally current;
- any agent actually read or understood a dump;
- PRs are merge-ready;
- tests are sufficient or regression-free;
- runtime behavior is correct.

## Guardrails

- No merge, push, deploy, or live-state change was performed for this slice.
- rLens freshness for the three target repos was recorded as `unknown` at claim
  time (bundle/preflight provenance unverified); treat rLens metadata as context
  only, not proof of correctness.
