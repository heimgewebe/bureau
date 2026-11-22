# Bureau runtime auto-update

Status: active once `bureau-runtime-autoupdate.timer` is enabled on the host.

## Why

Before this path, every green Bureau merge needed a second, hand-written
single-use authority PR (for example #2431, 286 lines) plus publication,
adoption, activation, intent, Grabowski lease and closeout ceremonies before the
installed runtime caught up. The installer itself was never the problem: it
already builds immutable releases, converges the user scheduler, restores the
scheduler preimage on failure and keeps a backup generation.

## What runs

`bureau-runtime-autoupdate.timer` starts
`bureau-runtime-refresh auto-update` every ten minutes. One run:

1. **Observe** GitHub `main` against the installed manifest with the existing
   `observe` logic (merged PR, required checks, ancestry, scheduler state).
2. **Gate**: continue only if `main` is a proven descendant of the installed
   source and the merged PR's required checks
   (`validate (3.10)`, `validate (3.12)`,
   `registry-registration-preflight/freshness`) are green.
3. **Clone** `main` into a fresh detached workspace under
   `~/.local/state/bureau/runtime-refresh/auto-update/workspaces/`.
4. **Install** with `ops/install-bureau-runtime.py --standing-auto-update`.
   The installer re-validates the observation (fresh, same commit, unchanged
   manifest), writes the immutable release, converges the scheduler and rolls
   its own effect back if activation fails.
5. **Read back** (`bureau --json check`, `runtime-identity`, scheduler). If the
   readback fails, the previous manifest, launchers and scheduler generation are
   restored from the installer backup.

It skips while a Grabowski lease is held on the runtime paths (a manual
refresh is in flight) and while another auto-update run holds the lock.

## Outcomes

`~/.local/state/bureau/runtime-refresh/auto-update/last-result.json` holds the
latest result; non-trivial results are also kept in `history/`.

| status | meaning | unit exit |
|---|---|---|
| `unchanged` | runtime already equals `main` | 0 |
| `waiting` | CI not green yet, main moved, or GitHub unreachable | 0 |
| `busy` | lease or lock held | 0 |
| `activated` | new release installed and read back | 0 |
| `blocked` | e.g. installed source is not an ancestor of `main` | 3 |
| `rolled_back` | install or readback failed, previous runtime restored | 4 |
| `failed` | failure and rollback incomplete; manual recovery needed | 5 |

Non-zero exits leave `bureau-runtime-autoupdate.service` failed, so
`systemctl --user --failed` shows it.

## Authority

Enabling the timer is the standing authorization for exactly this update type
(Bureau's own immutable runtime, from green protected `main`). It grants nothing
else: no task, queue, claim, NixOS, or foreign-repository effect.

- Stop: `systemctl --user disable --now bureau-runtime-autoupdate.timer`
- Start: `systemctl --user enable --now bureau-runtime-autoupdate.timer`

The scheduler convergence preserves this choice across updates.

## Bootstrap (once)

The installed runtime predates this path, so the first run uses the new code
from a clone of `main`:

```sh
work=$(mktemp -d)
git clone --quiet https://github.com/heimgewebe/bureau.git "$work/bureau"
PYTHONPATH="$work/bureau/src" python3 -m bureau.runtime_refresh auto-update
systemctl --user enable --now bureau-runtime-autoupdate.timer
rm -rf "$work"
```

## Manual rollback

Every install keeps a backup generation under
`~/.local/share/bureau/backups/<stamp>/`; the install receipt in
`~/.local/share/bureau/receipts/` names it under `rollback`. Previous immutable
releases stay in `~/.local/share/bureau/releases/`.

The old authority path (`prepare-intent` / `apply`) still exists for
exceptional cases, but a normal refresh no longer needs a TaskSpec PR.
