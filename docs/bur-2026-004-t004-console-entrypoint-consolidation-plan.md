# BUR-2026-004-T004 — Console Entry-Point Consolidation Plan

Status: planned compatibility path  
Task: `BUR-2026-004-T004`  
Inventory: `docs/reports/bur-2026-004-t004-console-entrypoints.v1.json`  
Source plan: `docs/plans/bureau-boundary-ops-v1.md`

## Purpose

Bureau has one small coordination core and several operational organs. The command surface mirrors that history: some commands are core, some are bridge or observation organs, and some are scheduler-facing deployment hooks.

This document plans consolidation before any binary or systemd command is removed.

## Current packaged console scripts

The canonical source is `[project.scripts]` in `pyproject.toml`. Current packaged scripts:

| Script | Module target | Layer |
|---|---|---|
| `bureau` | `bureau.cli:main` | Core CLI |
| `bureau-agent-frontier` | `bureau.agent_frontier:main` | Ops frontier |
| `bureau-agent-scout` | `bureau.agent_frontier:main` | Compatibility alias for agent frontier |
| `bureau-systemkatalog-bridge-import-policy` | `bureau.cabinet_bridge_import_policy:main` | System catalog bridge ops |
| `bureau-systemkatalog-bridge-preview` | `bureau.cabinet_bridge_preview:main` | System catalog bridge ops |
| `bureau-systemkatalog-bridge-receipt` | `bureau.cabinet_bridge_receipt:main` | System catalog bridge ops |
| `bureau-systemkatalog-bridge-review` | `bureau.cabinet_bridge_review:main` | System catalog bridge ops |
| `bureau-systemkatalog-frontier-reader` | `bureau.cabinet_frontier_reader:main` | System catalog frontier ops |
| `bureau-closure` | `bureau.closure:main` | Closure ops |
| `bureau-closure-runner` | `bureau.closure_runner:main` | Closure ops |
| `bureau-codex-bridge` | `bureau.codex_bridge:main` | Codex bridge ops |
| `bureau-gemini-preflight` | `bureau.gemini_preflight:main` | External-agent preflight ops |
| `bureau-gemini-review-lane` | `bureau.gemini_review_lane:main` | External-agent proposal review ops |
| `bureau-pr-task-finish` | `bureau.task_finish:main` | Closure/task-finish ops |
| `bureau-review-steward` | `bureau.review_steward:main` | Review stewardship ops |
| `bureau-source-pr-bridge` | `bureau.source_pr_bridge:main` | Source PR bridge ops |
| `bureau-status-capsule` | `bureau.status_capsule:main` | Independent read-only status ops |

## Module entry points not promoted as packaged console scripts

`src/bureau/entrypoint_inventory.py` also records modules with a `main` function or `if __name__ == "__main__"` guard. These are important because they can be invoked as `python -m bureau.<module>` or used in tests, but they are not automatically stable external binaries.

Examples include `bureau.cli`, `bureau.cycle_contract`, `bureau.discovery`, `bureau.discovery_runner`, `bureau.github_observer`, `bureau.status_projection`, and bridge modules. Treat these as implementation surfaces unless they are deliberately documented as stable commands.

## Known systemd impact

Current reference units under `ops/systemd/`:

| Unit | ExecStart shape | Compatibility implication |
|---|---|---|
| `bureau-status-projection.service` | `%h/.local/bin/bureau --root %h/repos/bureau --json status-projection` | Uses the manifest- and package-digest-bound immutable Bureau launcher. |
| `bureau-status-capsule.service` | `%h/.local/share/bureau/venv/bin/bureau-status-capsule write ...` | Dedicated independent read-only snapshot writer; keep its narrow binary and file-only reader stable. |
| `bureau-status-capsule.timer` | refreshes `bureau-status-capsule.service` every five minutes | Keep explicit freshness scheduling; no network or source mutation. |
| `bureau-reconcile.service` | `%h/.local/bin/bureau --root %h/repos/bureau --json reconcile --stale-after 900` | Uses the same immutable Bureau launcher as interactive operator calls. |
| `bureau-source-pr-bridge.service` | `%h/.local/share/bureau-source-pr-bridge/venv/bin/bureau-source-pr-bridge` | Dedicated ops binary; needs compatibility shim before consolidation. |
| `bureau-review-steward.service` | `%h/.local/share/bureau-review-steward/venv/bin/bureau-review-steward run` | Dedicated ops binary; needs compatibility shim before consolidation. |
| `bureau-agent-frontier.service` | `%h/.local/libexec/bureau-agent-frontier` | Uses local libexec shim. Replacement must account for deployed shim path. |
| `bureau-codex-bridge.service` | `%h/.local/libexec/bureau-codex-bridge --repo-root %h/repos/bureau --backend=none --json` | Uses local libexec shim. Replacement must account for deployed shim path and arguments. |

The inventory checked `ops/systemd/*.service`, `ops/systemd/*.timer`, `docs/operations.md`, `docs/bureau-runtime-observation-v1.md`, `.github/workflows/`, `Makefile`, and tests for references.

The two core CLI units were migrated by `OPERATOR-MACHINE-READABILITY-V1-T004`. Historical inventory receipts under `docs/reports/` remain unchanged because they describe the previously observed deployment.

## Compatibility-preserving consolidation path

### Phase 0 — Freeze removals

Allowed:

- add inventory and tests;
- document ownership and compatibility expectations;
- add new canonical subcommands behind `bureau`.

Forbidden:

- remove packaged console scripts;
- change `ExecStart` in systemd units;
- warn by default in scheduler-facing commands;
- change command JSON shape.

### Phase 1 — Add canonical grouped commands

Add new `bureau` subcommands as equivalents, not replacements:

| Future canonical shape | Existing command preserved |
|---|---|
| `bureau ops source-pr-bridge run` | `bureau-source-pr-bridge` |
| `bureau ops review-steward run` | `bureau-review-steward run` |
| `bureau ops agent-frontier report` | `bureau-agent-frontier` / `bureau-agent-scout` |
| `bureau ops codex-bridge run` | `bureau-codex-bridge` |
| `bureau ops systemkatalog preview/review/receipt/import-policy/frontier-read` | `bureau-systemkatalog-*` |
| `bureau ops gemini-preflight` | `bureau-gemini-preflight` |
| `bureau ops gemini-review-lane` | `bureau-gemini-review-lane` |
| `bureau closure ...` | `bureau-closure`, `bureau-closure-runner`, `bureau-pr-task-finish` |

Acceptance for Phase 1:

- each new grouped subcommand has tests proving equivalent output or receipt shape;
- existing commands remain byte-for-byte compatible where practical;
- systemd units still call the old stable paths.

### Phase 2 — Dual-run documentation and optional advisory warnings

Only after Phase 1 is merged:

- update docs to show new grouped commands first;
- keep old commands in all install snippets as supported compatibility shims or mark them explicitly as shims;
- optional deprecation/advisory warnings may be added only when output is human-readable.

No warning may be printed in `--json` mode or scheduler units unless the schema includes a stable warning field.

### Phase 3 — Migrate systemd reference units

Only after at least one compatibility release:

- update `ops/systemd/*.service` to use canonical grouped commands where operationally useful;
- provide rollback snippets using the old command names;
- keep dedicated command shims installed.

### Phase 4 — Removal gate

Removal of any old command is allowed only when all of these are true:

1. the command has had a documented compatibility shim period;
2. `entrypoint_inventory` shows no systemd unit still requiring the old binary;
3. operations docs no longer use the old command as primary path;
4. GitHub workflows do not call the old command;
5. tests cover the canonical replacement;
6. a Bureau task explicitly authorises removal.

## Decision for now

The T014-authorized Cabinet-to-Systemkatalog identity migration removes the old Cabinet command aliases; all unrelated command names remain frozen.

Recommended immediate next PR after this plan: implement `bureau ops ...` aliases for one low-risk bridge command only, probably `bureau ops source-pr-bridge run`, while preserving `bureau-source-pr-bridge` exactly. That keeps the migration empirical and reversible.

## Does not establish

This inventory and plan do not establish:

- that deployed user units are installed or healthy;
- that hidden external users do not call old binaries;
- that old entry points are safe to remove;
- that ops extraction is the right packaging decision;
- that systemd runtime behaviour is correct.
