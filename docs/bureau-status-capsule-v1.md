# Bureau Status Capsule v1

Task: `BUREAU-TRUTH-MODEL-V2-T002`

## Purpose

The status capsule is Bureau's narrow independent read path. The collector observes
the protected GitHub `main` through an ephemeral exact fetch when available (otherwise
labels the local ref honestly), reads a consistent read-only SQLite backup and atomically
publishes one bounded JSON file. The reader needs only that file. It does not open Git, SQLite, GitHub,
Grabowski or a shell.

This provides time-bound truth when the full Grabowski operator session is unavailable. It does not
make stale data current and does not copy foreign authority into Bureau.

## Commands

Publish from the locally observed canonical Bureau head:

```bash
bureau-status-capsule write \
  --canonical-repo ~/repos/bureau \
  --state-root ~/.local/state/bureau \
  --output ~/.local/state/bureau-readonly/status-capsule.json \
  --freshness-seconds 900
```

Read without touching the source repository or state database:

```bash
bureau-status-capsule read \
  --path ~/.local/state/bureau-readonly/status-capsule.json
```

Reader exit codes:

- `0`: fresh and hash-valid;
- `1`: stale, either by age or because a newer refresh failed;
- `2`: unavailable, missing, unreadable or hash-invalid.

## Snapshot contract

The sealed snapshot contains:

- creation time and freshness threshold;
- observed source-ref head, its provenance, Registry tree and deterministic Registry JSON hash;
- running collector package version and SHA-256 of the loaded `bureau.status_capsule` module;
- explicit remote-freshness evidence; a local-only observation never claims GitHub-main freshness;
- SQLite integrity, foreign-key result and schema version;
- bounded recent and active runs;
- active reservations represented as leases;
- compact repository balls;
- read-only Doctor result;
- Registry Truth result without expensive baseline probes;
- the previous successful snapshot identity;
- an explicit list of non-authorities.

The content hash covers the canonical JSON payload excluding only the hash field itself. It detects
semantic corruption but is not a signature and does not authenticate the producer. Snapshot and
failure files are written with mode `0600`; a newly created output directory uses mode `0700`.
Existing output-parent permissions are never changed.

## Failure semantics

The collector never overwrites the last successful snapshot when source collection fails. It writes a
separate sealed `*.last-failure.json` sidecar. A reader that sees a newer failure reports the last
successful snapshot as `stale`, even while its age remains below the normal threshold.

No valid snapshot yields `unavailable`. Hash manipulation also yields `unavailable`; the reader does
not partially trust a damaged document.

The deployment archive is built from the exact local `origin/main` object, not from working-tree files, and the runtime venv is switched through an atomic symlink. The output path must be outside both Registry and state roots. The reference unit requires the
output directory to exist before activation, so systemd can expose only that narrow path as writable
under `ProtectHome=read-only`. A snapshot is capped at 1,000,000
pretty-printed UTF-8 bytes; oversized refreshes preserve the previous snapshot and emit failure
evidence. Source collection uses a hook-free, fsmonitor-free `git archive` from the locally present
`origin/main` object and a SQLite `mode=ro` backup. Global and system Git configuration, pagers,
external diff helpers and replace objects are disabled. Collection neither fetches nor changes the
checkout, Registry, queue, database, runs, reservations, receipts or workspaces.

## Reference timer

`ops/systemd/bureau-status-capsule.{service,timer}` refreshes the capsule every five minutes.
The default unit isolates filesystem writes to `~/.local/state/bureau-readonly` and permits
AF_UNIX, AF_INET and AF_INET6 for read-only GitHub-main observation.

On NixOS, a root-owned SSH configuration under `/nix/store` may appear as UID 65534 inside a
systemd --user mount namespace. OpenSSH then rejects its ownership. The proven, NixOS-only
`ops/systemd/bureau-status-capsule-nixos-ssh-owner.conf` override disables the five
filesystem namespace controls that cause that failure. It retains the original unit's
process protections and the collector's read-only behavior; filesystem access then follows
the ordinary permissions of the user account.

```bash
release="$(git -C ~/repos/bureau rev-parse origin/main)"
archive="/tmp/bureau-${release}.tar.gz"
release_venv="$HOME/.local/share/bureau/venv-${release}"
git -C ~/repos/bureau archive --format=tar.gz --output="$archive" "$release"
test ! -e "$release_venv"
python3 -m venv "$release_venv"
"$release_venv/bin/pip" install "$archive"
rm -f "$HOME/.local/share/bureau/venv.next"
ln -s "$release_venv" "$HOME/.local/share/bureau/venv.next"
mv -Tf "$HOME/.local/share/bureau/venv.next" "$HOME/.local/share/bureau/venv"
install -d -m 0700 ~/.local/state/bureau-readonly
install -Dm644 ops/systemd/bureau-status-capsule.service \
  ~/.config/systemd/user/bureau-status-capsule.service
install -Dm644 ops/systemd/bureau-status-capsule.timer \
  ~/.config/systemd/user/bureau-status-capsule.timer
if test -e /etc/NIXOS; then
  install -Dm644 ops/systemd/bureau-status-capsule-nixos-ssh-owner.conf \
    ~/.config/systemd/user/bureau-status-capsule.service.d/20-nixos-ssh-owner.conf
fi
systemctl --user daemon-reload
systemctl --user enable --now bureau-status-capsule.timer
```

The snapshot records whether GitHub `main` was confirmed through a read-only ephemeral
exact fetch or whether the collector observed only a local ref. It never calls a local-only
observation remote-fresh. Updating the canonical checkout remains a separate Git/Grabowski operation.

## Boundaries

The capsule does not establish Registry or state mutation, shell authority, task verification, queue
mutation, claim, dispatch, merge, deployment, reviewer identity, CI sufficiency or runtime
correctness. A healthy capsule means its bounded checks passed for the stated head and timestamp,
not that every external system is healthy. In particular, snapshot freshness is collection
freshness for the named local head; it does not prove that local `origin/main` equals GitHub.
