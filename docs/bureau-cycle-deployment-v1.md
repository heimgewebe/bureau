# Bureau cycle scheduler deployment v1

Status: implementation candidate; not deployed
Task: `OPERATOR-ECOSYSTEM-REDUNDANCY-V1-T017`
Implementation base: `e573ce243113a0c0b8f613d01cce307c325574de`

## Purpose

The Bureau discovery, curator, operator, verifier and closure stages previously executed from
mutable user-home sources. Four shims injected `~/.local/libexec` through `PYTHONPATH`; the closure
shim injected `~/repos/bureau/src`. Those paths were outside the immutable Bureau release identity,
were not fully represented in Git, and could drift without a release change.

This contract makes the repository and the immutable Bureau release the sole source owner. It
does not activate or restart any unit.

## Canonical source and stage map

The active scheduler package is versioned under `src/bureau_cycle/`. Only the active Python files
were imported; backups, `deployment.json` fragments and `__pycache__` are excluded. Closure remains
under `src/bureau/closure_runner.py`.

| Stage | Stable unit name | Python module | Schedule |
|---|---|---|---|
| discovery | `bureau-halfhour-operator` | `bureau_cycle.discovery_runner` | every hour at `:30` |
| curator | `bureau-curator` | `bureau_cycle.curator_runner` | every hour at `:45` |
| operator | `bureau-operator-control` | `bureau_cycle.operator_runner` | every hour at `:00:05` |
| verifier | `bureau-verifier-control` | `bureau_cycle.verifier_runner` | every hour at `:15:05` |
| closure | `bureau-closure-planner` | `bureau.closure_runner` | every hour at `:50` |

Each versioned service executes the existing manifest-validating Bureau launcher:

```text
%h/.local/bin/bureau cycle-run <stage>
```

The launcher verifies the deployment manifest and managed runtime-tree digest, then imports the stage from the selected immutable release. Services do not execute Python directly and do not use source from `~/repos/bureau`, `~/.local/libexec/bureau_cycle`, a virtual-environment `site-packages` copy, or a `PYTHONPATH` override. The immutable digest covers `src/bureau`, `src/bureau_cycle`, and the versioned `ops/systemd` artifacts.

## Read-only provenance and drift audit

`bureau cycle-deployment` compares:

- the manifest contract and immutable release path;
- all canonical scheduler source files;
- five service files and five timers;
- five compatibility shims;
- the live unit and shim hashes;
- the loaded `bureau_cycle` and `bureau.closure_runner` module locations.

Default invocation:

```bash
bureau --json cycle-deployment
```

Explicit roots, useful for a candidate or test release:

```bash
bureau --json cycle-deployment \
  --manifest ~/.local/share/bureau/deployment-manifest.json \
  --unit-root ~/.config/systemd/user \
  --shim-root ~/.local/libexec
```

The command emits deterministic JSON. Exit codes are:

- `0`: canonical files, live files and loaded modules agree;
- `1`: readable but drifted;
- `2`: invalid or unsafe input, including malformed manifests, missing canonical files, symlinks,
  path escape, or unsupported manifest identity.

The result always states `activatable: false`, `read_only: true`, and `self_heal: false`. The audit
does not install files, rewrite units, reload systemd, restart services, mutate Registry state, or
select a release.

The verifier stage consumes the same audit. It no longer carries a second source-path policy that
requires and then rejects the old `~/.local/libexec` deployment.

## Compatibility and rollback

`bureau-halfhour-operator.service` and `.timer` remain the stable discovery names. There is no
silent rename and no duplicate timer.

`ops/systemd/libexec/` contains versioned rollback shims for all five historical command names.
They preserve direct operator invocation while delegating to the same manifest-validating `bureau cycle-run <stage>` path. They do not restore mutable source ownership or bypass release-identity checks.

A rollback changes only the installed service/timer or shim to a previously reviewed immutable
release. It must keep the deployment manifest, installed virtual environment and live files on the
same release identity. Copying old unversioned `~/.local/libexec/bureau_cycle` sources back into
place is not a valid rollback.

## Activation boundary

Review and merge establish only versioned source ownership. Runtime activation is a separate
`review-before-effect` operation:

1. build/install a Bureau release from the reviewed commit;
2. verify the new immutable deployment manifest;
3. install the reviewed unit, timer and shim files;
4. run `systemd-analyze --user verify`;
5. run `bureau --json cycle-deployment`;
6. only then perform `systemctl --user daemon-reload` and controlled restarts or timer changes.

This change does not authorize any of those effects.

## Non-claims

This contract does not establish that:

- the current live scheduler is already canonical;
- the candidate has been merged or deployed;
- timers are enabled or healthy;
- a drift finding may be repaired automatically;
- the scheduler stages have broader mutation authority;
- the compatibility names may be removed.
