# Bureau runtime identity v1

## Problem

`python3 -m bureau.cli` previously depended on ambient Python import order. A stale package in `~/.local/lib/pythonX.Y/site-packages` could therefore read a different Registry checkout and make protection decisions with unrelated code.

## Structured response contract

Every `--json` response against a complete Bureau project is returned as an identity envelope:

```json
{
  "schema_version": 1,
  "runtime_identity": {},
  "result": {}
}
```

Temporary fixture or archive Registries retain their legacy JSON shape for compatibility, but dictionary results still carry `runtime_identity`. They are marked `unmanaged-registry` and establish no production-runtime claim. `--json-envelope` forces the envelope explicitly.

The identity binds:

- imported module path and SHA-256;
- source package version and separately observed installed distribution version;
- source checkout head, `origin/main` and dirty paths when available;
- selected Registry root, head, `origin/main`, project classification and dirty paths;
- immutable deployment manifest, release identity and full package-tree SHA-256;
- state database path, schema version and integrity result.

## Mutation gate

Mutation is allowed only when one of these conditions holds:

1. module source and Registry are the same **clean** Bureau checkout; or
2. the module belongs to a manifest- and package-digest-verified immutable release whose source commit equals the clean Registry head.

An ambient, dirty, stale or unbound runtime returns `stale-runtime-blocked`. The command classifier is fail-closed: every new command is treated as mutating until it is explicitly proven read-only. The diagnostic routes `runtime-identity`, `check`, `runtime-drift-check`, `lease-contract`, `registry-truth`, `source-check`, non-applying `source-sync`, `source-promote-plan`, non-applying `worktree-hygiene`, `github-observe` and `status-projection` remain available.

Commands that construct or migrate `StateStore`, even when colloquially called status or doctor operations, remain mutation-gated because initialization can create directories, databases or schema rows.

## Canonical installation

From a clean checkout whose `HEAD` equals `origin/main`:

```bash
python3 ops/install-bureau-runtime.py --source . --replace-existing
```

The installer creates:

- a read-only release below `~/.local/share/bureau/releases/`;
- `~/.local/share/bureau/deployment-manifest.json`;
- a digest-checking `~/.local/bin/bureau` launcher;
- an installation receipt;
- rollback copies of the previous manifest and regular launcher when present;
- for a replaced launcher symlink, rollback metadata with the exact raw link target. The symlink target itself is never modified.

Existing launcher symlinks and unmanaged regular launchers require explicit `--replace-existing`; without it the installer fails closed.

The launcher pins the exact manifest SHA-256, verifies the manifest schema, the runtime module and the complete `pyproject.toml` plus `src/bureau/**/*.py` package tree before importing anything. A changed manifest, symlink, missing file or changed package file fails closed.

The reference `bureau-reconcile.service` and `bureau-status-projection.service` units use `~/.local/bin/bureau`, so timers and interactive operator calls consume the same immutable release.

## Non-claims

Runtime identity does not establish Registry semantics, task priority, CI success, database business correctness or future command success. It only proves which code, Registry and state source participated in the observed invocation and whether that combination is permitted to mutate.
