# Bureau runtime identity v1

## Problem

`python3 -m bureau.cli` previously depended on ambient Python import order. A stale package in `~/.local/lib/pythonX.Y/site-packages` could therefore read a different Registry checkout and make protection decisions with unrelated code.

A second failure mode remained after the immutable runtime was introduced: the installed `bureau` launcher still selected `.` as its default Registry root. A stale or dirty shell working directory could therefore block every useful statement even though the deployed code itself was current. Automatically pulling or cleaning that checkout would be unsafe because it could overwrite foreign work.

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
- selected Registry root, selection mode, head, `origin/main`, project classification and dirty paths;
- immutable deployment manifest, release identity and full package-tree SHA-256;
- canonical Registry snapshot root, inventory digest, tracked-tree digest and source commit;
- state database path, schema version and integrity result.

## Registry selection

The root precedence is explicit and deterministic:

1. `--root PATH` selects an operator-bound checkout and records `explicit-cli`;
2. `BUREAU_REGISTRY_ROOT` selects the configured root and records its declared mode;
3. only development invocations without either binding fall back to the current directory and record `ambient-cwd`.

The installed launcher sets `BUREAU_REGISTRY_ROOT` to the immutable Registry snapshot packaged by the same deployment. It therefore never derives normal read truth from the caller's current directory.

## Read path

The canonical deployment snapshot is read-only. Commands proven to be statements use a query-only SQLite connection and do not create state directories, migrate schemas, change journal mode or chmod files. This includes status, lifecycle, frontier, what-now, repo-balls, live-list/export/retention, runs, run, conflicts and non-applying planning or diagnostic routes.

The canonical snapshot can answer operational questions even when `~/repos/bureau` is stale or dirty. Its compatibility state is `canonical-read-only`; this is intentional and is not mutation authority.

## Mutation gate

Mutation is allowed only when one of these conditions holds:

1. module source and Registry are the same **clean** Bureau checkout; or
2. the module belongs to a manifest- and package-digest-verified immutable release whose source commit equals the clean, explicitly selected Registry head.

A mutating command invoked through the launcher's canonical default fails with `explicit-registry-root-required`. The operator must rerun it with `--root` bound to a clean task-specific worktree. The snapshot is never promoted into a writable checkout and the dirty main checkout is never pulled, reset, stashed or cleaned automatically.

An ambient, dirty, stale or unbound explicit checkout returns `stale-runtime-blocked`. The command classifier remains fail-closed: every new command is treated as mutating until explicitly proven read-only. Conditional commands are read-only only in their non-applying form; `doctor --repair`, queue-plan writes/applies and promotion-plan writes/applies remain mutations.

## Canonical installation

From a clean checkout whose `HEAD` equals `origin/main`:

```bash
python3 ops/install-bureau-runtime.py --source . --replace-existing
```

The installer creates:

- a read-only release below `~/.local/share/bureau/releases/`;
- a read-only tracked-file Registry snapshot below `~/.local/share/bureau/registry-snapshots/`;
- a snapshot inventory binding every tracked path to one tree SHA-256 and source commit;
- `~/.local/share/bureau/deployment-manifest.json`;
- a digest-checking `~/.local/bin/bureau` launcher;
- an installation receipt;
- rollback copies of the previous manifest and regular launcher when present;
- for a replaced launcher symlink, rollback metadata with the exact raw link target. The symlink target itself is never modified.

Existing launcher symlinks and unmanaged regular launchers require explicit `--replace-existing`; without it the installer fails closed.

The launcher pins the exact manifest SHA-256, verifies the manifest schema, runtime module, complete `pyproject.toml` plus `src/bureau/**/*.py` package tree, and the existence of the canonical snapshot and inventory before importing anything. Runtime identity then verifies the inventory digest, tracked-file tree digest and source commit. A changed manifest, symlink, missing file, changed package file or changed snapshot file fails closed.

The reference `bureau-reconcile.service` and `bureau-status-projection.service` units use `~/.local/bin/bureau`, so timers and interactive operator calls consume the same immutable release and canonical read snapshot.

## Operational consequence

A stale dirty main checkout remains visible as a worktree-hygiene problem but no longer becomes the default source of Bureau statements. Closeout remains strict: task state changes require an explicit clean worktree at the current Registry head. This separates availability from write authority rather than weakening either.

## Non-claims

Runtime identity does not establish Registry semantics, task priority, CI success, database business correctness or future command success. The canonical snapshot does not establish mutation authority. The contract proves which code, Registry snapshot or explicit worktree, and state source participated in the invocation and whether that combination may write.
