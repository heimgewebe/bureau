# Operations

## Diagnose

```bash
bureau --root . doctor --json
bureau --root . lifecycle --json
bureau --root . explain-next --capability repository --capability shell --json
```

`doctor` includes a read-only `state_root_hygiene` section. It treats the configured Bureau database, SQLite sidecars, `envelopes/`, `receipts/`, `reviews/`, hash-bound Bureau deployment evidence under `deployments/`, and paired Git recovery bundles under `recovery/` as known state-root artefacts. Deployment and recovery directories are accepted only when their release or checksum receipts match the expected bounded structure; foreign or incomplete content remains a hard finding. Unknown files or directories are reported, not deleted, including when `--repair` is used. Move or quarantine such files manually after checking whether they are operator notes, old backups or unrelated prompts.



## Queue freshness reconcile

`registry/queue.json` is read-only compatibility history after the T013 cutover. It is not an
operational dispatcher-order or lifecycle authority. Current TaskSpec revisions, task priority,
dynamic frontier, lifecycle state, runs, reservations, acceptance and closeout are StateStore truth.
Use
`queue-reconcile` to compare the two without mutating queue state:

```bash
bureau --root . --json queue-reconcile
bureau --root . --json queue-reconcile --resource repo.bureau
```

The report can identify ready priority-now tasks that are not queued, priority-next tasks absent
from queue, later-lane tasks whose advisory priority says now/next, terminal tasks still queued,
and now-lane tasks that are not ready. The default command is read-only: it does not promote
lanes, claim work, write tasks or close anything.

### Runtime identity during the legacy queue transition

The immutable-runtime compatibility gate treats one case specially during the T012/T013 migration:
a clean canonical `heimgewebe/bureau` checkout may be ahead of the deployed source commit when the
deployed commit is an ancestor of `HEAD`, `HEAD == origin/main`, and the complete Git path delta is
exactly the regular file `registry/queue.json`. That bounded forward drift remains runtime-compatible
because the file is only the transitional dispatcher-order projection. The local claim-root preflight
may therefore stay clear, but it still reports that fresh GitHub main and claim authority have not
been established; atomic claimability remains the responsibility of the normal claim path.

This exception is deliberately fail-closed. A dirty checkout, missing or symlinked queue file,
non-canonical origin, `origin/main` mismatch, divergent history, unreadable Git evidence, or any
additional changed path blocks compatibility. In particular, TaskSpec, schema, package, source,
authority, or mixed queue-plus-authoritative drift still requires exact runtime convergence and
continues to report `release-registry-identity-mismatch`. The exception never makes
`registry/queue.json` task, admission, claim, dispatch, lifecycle, acceptance, or closeout truth.

Historically, verified task `BUR-2026-005-T013` made `registry/queue.json` the dispatch-order canon
for the earlier control model. Control Plane V3 subsequently moved operational task revision,
lifecycle, run, reservation, acceptance, closeout, and frontier authority into StateStore. The
legacy queue surface remains readable during migration, and compatibility with a queue-only commit
reflects that migration boundary rather than restoring the older authority model.

`queue-reconcile --write-plan` remains available for audit and review evidence, but
`queue-reconcile --apply-plan` is deliberately retired as a materialisation effect. A reviewed plan
is still validated (including its review, action filter and resource binding), then returns an
explicit `retired`/`no_op` result. It never rewrites `registry/queue.json`. Likewise,
`bureau.now_refill --apply` reports the bounded projection and an explicit retired result without
moving lanes. The dynamic StateStore frontier and TaskSpec priority are the operational scheduling
surfaces; a compatibility-queue discrepancy is diagnostic evidence only.

Do not repair operational readiness by editing `registry/queue.json`, creating a queue PR or running
a historical queue writer. Repair the authoritative TaskSpec/lifecycle state through the typed
StateStore APIs, then re-read Doctor/Frontier. The compatibility file may remain stale until an
explicit archive/projection policy changes it; that does not create a second authority.

## Worktree hygiene and reviewed cleanup

The default `worktree-hygiene` mode is read-only and inventories local Bureau worktree sprawl:

```bash
bureau --root . --json worktree-hygiene
bureau --root . --json worktree-hygiene --max-count 40
```

The report identifies detached, locked, dirty, missing, excessive and already-merged worktrees. A
report is not cleanup authority. It deliberately does not prove process or external lease absence.

Cleanup uses a separate two-step reviewed-plan path. Every candidate must be an explicit absolute
path; the command never expands all findings into effects:

```bash
bureau --root /home/alex/repos/bureau --json worktree-hygiene \
  --candidate /absolute/path/to/linked-worktree \
  --write-plan /absolute/path/outside/candidates/worktree-cleanup-plan.json
```

Plan creation already refuses the canonical worktree, unknown, missing, dirty, locked, process-used
or not-fully-merged candidates. The generated plan is inert with `review.status=pending`. A reviewer
checks every path, head and branch, then sets `status=reviewed`, adds `reviewer` and `reviewed_at`, and
copies the top-level `repository_identity_sha256` and `candidate_states_sha256` into the review
object. The plan file must stay outside every cleanup candidate.

Apply is allowed only while the operator holds the dedicated Bureau worktree-admin gate and has
confirmed that no foreign exact path lease covers a candidate:

```bash
bureau --root /home/alex/repos/bureau --json worktree-hygiene \
  --apply-plan /absolute/path/outside/candidates/worktree-cleanup-plan.json
```

Apply revalidates repository identity, reviewed hashes, candidate path/head/branch, clean state,
merge ancestry, lock state, active process references and unchanged plan bytes immediately before
each removal. It calls `git worktree remove` without force, never deletes a branch, emits a fresh
post-clean hygiene report and attempts to restore already removed worktrees if a later candidate or
post-clean gate fails. External lease absence remains an operator/Grabowski precondition because
Bureau does not treat its report as authority over Grabowski's live lease database. Process checks
are bounded by what Linux `procfs` exposes to the executing user and do not claim visibility into
otherwise hidden process state.

## Console script packaging

Packaged console scripts are declared in `pyproject.toml`. If a command exists in pyproject but is
missing from the local shell, refresh the editable install:

```bash
python3 -m pip install -e '.[dev]'
```

Module entrypoints remain available through `python3 -m bureau.<module>` even when the shell wrapper
is stale.

## Live operational register

Use the Live Register for gitless operational focus and candidate observations. It writes to the
Bureau state-store eventlog, not to `registry/queue.json`:

```bash
bureau --root . --json live-register \
  --kind thread_focus \
  --thread-id chat-20260710-a \
  --repo repo.bureau \
  --title "Current Bureau architecture thread" \
  --source chat

bureau --root . --json live-register \
  --kind candidate_task \
  --repo repo.bureau \
  --title "Promote live candidates into registry PRs" \
  --promotion-required

bureau --root . --json live-list
```

Live-register records are operational evidence only. They do not establish queue truth, task truth,
claim authority, dispatch authority or merge readiness. Durable work still requires a reviewed,
CAS-bound TaskSpec publication into the authoritative StateStore.

Operational views can consume the same state-store evidence without changing dispatch authority:

```bash
bureau --root . --json what-now --capability repository
bureau --root . --json repo-balls --capability repository
bureau --root . --json live-conflicts --capability repository --repo repo.bureau
bureau --root . --json live-retention
bureau --root . --json live-export --format chronik --repo repo.bureau
```

Candidate promotion is a reviewed-plan path and writes task JSON only:

```bash
bureau --root . --json live-promote-plan \
  --event-id 12 \
  --initiative BUREAU-LIVE-REGISTER-V1 \
  --task-id BUREAU-LIVE-REGISTER-V1-T007 \
  --write-plan /tmp/live-promote.json
```

The Live Register plan remains review evidence only. `live-promote-plan --apply-plan` is retired
after the T013 cutover and fails closed before any Registry effect; materialise the reviewed
candidate through `operator-task-propose` / `operator-task-publish`, which writes the authoritative
StateStore TaskSpec directly.

## Operator-native candidate intake and task publication

ChatGPT through Grabowski is the executing operator; the user is observer and steersman. Bureau exposes a machine-first candidate-to-task path that reuses the Live Register, Registry schemas, Approval runtime, exact leases and GitHub readback instead of creating a second task authority.

The four transport commands are:

```bash
bureau --json operator-candidate-record --request candidate-request.json
bureau --json operator-candidate-assess --candidate-id candidate-...
bureau --root /clean/bureau --json operator-task-propose \
  --candidate-id candidate-... --task-json task.json \
  --publishing-task-id EXISTING-TASK-ID --write-plan proposal.json
bureau --root /clean/bureau --json operator-task-publish \
  --plan proposal.json --preview
```

Apply additionally requires a reviewed proposal, a live owner/task lease binding for the exact
StateRoot and a create-only receipt path. Bureau reads Grabowski's private resource database
directly; supplied JSON is not treated as lease authority. `operator-task-publish` advertises
`publication_mode: state_store`, binds the absolute coordination StateRoot, performs one TaskSpec
CAS mutation there and reads the exact revision back before releasing the lease. It does not create
a branch, task-file commit or pull request and never mutates `registry/queue.json`. `operator-task-ready`
promotes the same StateStore revision; historical Git publication receipts remain readable only for
compatibility closeout. Neither command claims, dispatches, merges, deploys or verifies the proposed
task.

See `docs/bureau-operator-intake-v1.md` for schemas, failure semantics, idempotency and non-claims.

## Repository-scoped balls

Bureau can project one current ball per repository resource without changing queue state:

```bash
bureau --root . --json repo-balls --capability repository --capability shell
```

Use a resource filter when asking for the next task for one repository:

```bash
bureau --root . --json explain-next --resource repo.bureau \
  --capability repository --capability shell
bureau --root . --json claim-next --worker worker-repo-bureau \
  --resource repo.bureau --capability repository --capability shell
```

A resource-scoped ball does not create a second queue authority. `registry/queue.json` is
read-only compatibility history and does not override StateStore TaskSpec, priority or lifecycle
truth. The resource filter limits observation, explanation and selection to tasks whose claims
overlap the requested resource.

Worker ownership is still one active assignment per worker ID. Use stable resource-scoped worker
IDs, such as `worker-repo-bureau` or `worker-repo-lenskit`, when operating multiple repository
balls in parallel. Reusing a worker ID for a different resource is rejected instead of silently
claiming another task.

## Check out work

```bash
bureau --root . checkout-next --worker <stable-session-id> \
  --capability repository --capability shell --json
```

Use a stable session ID. Repeating the command returns the existing active assignment rather than
claiming another task.

## Complete

Evidence is a JSON object keyed by acceptance criterion ID:

```bash
bureau --root . complete <run-id> --evidence evidence.json --json
```

Completion is idempotent. SQLite is canonical; the receipt file is a deterministic materialisation.

## Workspaces

```bash
bureau --root . workspace-status <run-id> --json
bureau --root . workspace-preserve <run-id> --reason 'needs review'
bureau --root . workspace-cleanup <run-id>
```

Cleanup requires a terminal run. Dirty or unmerged workspaces are preserved unless `--force` is
explicitly supplied.

## Weltgewebe source inbox

Validate the locally available source ref without changing Bureau state:

```bash
bureau --root . --json source-check weltgewebe \
  --repo ~/repos/weltgewebe --ref origin/main
```

Preview drift against the currently materialised source snapshot:

```bash
bureau --root . --json source-sync weltgewebe \
  --repo ~/repos/weltgewebe --ref origin/main
```

Apply the validated snapshot atomically:

```bash
bureau --root . --json source-sync weltgewebe \
  --repo ~/repos/weltgewebe --ref origin/main --apply
```

The adapter performs no fetch and makes no network request. It ignores repository pager, hook and
external-diff configuration, validates both source documents from the same resolved commit, and
bounds preview ID lists. Repeating `--apply` for unchanged source bytes performs no write.

Scheduling may run `source-check`, preview sync or reconciliation. It must not imply promotion,
readiness or approval to execute any source task.

## Scheduled Weltgewebe synchronization

The `sync-weltgewebe-source` GitHub workflow runs at minute 0 and 30 of every hour and can also be
started manually. It checks out the current public Weltgewebe `main`, materialises the candidate
snapshot in an ephemeral Bureau checkout, and runs the full validation suite when the snapshot
changes.

A changed snapshot is pushed only to the bot-owned `automation/weltgewebe-source-sync` branch using
an explicit force-with-lease precondition. The workflow never pushes to `main`, merges a pull
request or promotes a source task. Only `registry/sources/weltgewebe.json` may change; any additional
changed path fails the run.

The Heimgewebe organisation deliberately prevents `GITHUB_TOKEN` from creating pull requests. The
least-privilege design therefore keeps branch publication in GitHub Actions and delegates pull
request creation to the local `bureau-source-pr-bridge`, which uses the already authorised user
`gh` session without exporting its token to GitHub Actions.

Install the supplied user units only after the immutable Bureau release and its stable
`~/.local/bin/bureau` launcher have been activated through the reviewed deployment procedure:

```bash
install -Dm644 ops/systemd/bureau-source-pr-bridge.service \
  ~/.config/systemd/user/bureau-source-pr-bridge.service
install -Dm644 ops/systemd/bureau-source-pr-bridge.timer \
  ~/.config/systemd/user/bureau-source-pr-bridge.timer
systemctl --user daemon-reload
systemctl --user enable --now bureau-source-pr-bridge.timer
```

The timer must not point at a mutable checkout or a separately installed bridge virtual
environment. Rollback restores the previously reviewed unit files and immutable Bureau release,
then runs `systemctl --user daemon-reload` before the timer is started again.

The timer runs every five minutes. A delayed hosted source observation is picked up by a later
bridge run. The bridge is idempotent: it does nothing without an ahead source branch, creates a
missing review pull request, and otherwise refreshes the existing pull request body.

Manual checks:

```bash
~/.local/bin/bureau source-pr-bridge
systemctl --user status bureau-source-pr-bridge.timer
journalctl --user -u bureau-source-pr-bridge.service -n 50 --no-pager
```

Neither half of the pipeline establishes readiness, dependency completeness, safe parallel scope or
autonomous execution permission.

## Now-lane refill: read-only compatibility projection

`bureau.now_refill` remains useful as a bounded diagnostic of the former Now/Next queue model. The
preview can explain which structurally runnable tasks would have been promoted under the legacy
policy, but it no longer grants or performs an operational queue effect. `--apply` requires the
explicit authority string for compatibility with reviewed historical procedures, then returns
`retired: true`, `compatibility_queue_mutated: false` and leaves `registry/queue.json` byte-stable.

The old `source-pr-bridge --kind now-refill --publish` path is retired in the same way: it creates no
worktree, branch, commit or pull request. The installed `bureau-source-pr-bridge.service` therefore
contains no Now-refill publish `ExecStart`. No hosted workflow or local timer may materialise a queue
refill. Operational scheduling comes from StateStore TaskSpec priority plus the dynamic Frontier.

The source bridge itself is **not** retired. Its bounded source-observation path and the separate
redacted StateStore snapshot transport remain allowed Git outputs because they carry observation or
transparency evidence, not task/queue/lifecycle truth. Code, schema and runtime-release changes also
continue through normal protected GitHub PR/CI/merge. Exceptional reviewed one-file runtime-bootstrap
TaskSpecs remain the fail-closed recovery seam for immutable-runtime convergence; they are not the
ordinary task-registration path.

Rollback of an incorrect operational task change uses StateStore revision/CAS plus the normal
acceptance/reconciliation contracts and verified StateStore backup/restore evidence. It does not
reactivate queue writers or hand-edit `registry/queue.json`.

## Source promotion preview

Plan one Weltgewebe task candidate without materialising it:

```bash
bureau --root . --json source-promote-plan weltgewebe --task-id DEPLOY-DNS-001
```

The result is read-only. It exposes the projected Bureau task ID, source binding, unresolved claims,
unknown dependency structure and execution policy decisions. A promotion preview does not imply
readiness or permission to execute.

## Local Review Steward

The `bureau-review-steward` command performs a local, read-mostly review pass over the current
Closure state. It reads `lanes.json`, `plan.json`, generated Grabowski briefs, repository diff state
and, when `gh` is available, pull-request review and check evidence. It writes only lane review
evidence and review receipts under the Closure state root. It never starts coding work and never
merges.

Manual run:

```bash
bureau-review-steward run
```

The command prints a compact receipt summary by default. Use `--full-json` only when the full
lane evidence needs to be inspected outside the receipt file.

Install the steward into an isolated environment and enable the supplied user timer:

```bash
python3 -m venv ~/.local/share/bureau-review-steward/venv
~/.local/share/bureau-review-steward/venv/bin/pip install .
install -Dm644 ops/systemd/bureau-review-steward.service \
  ~/.config/systemd/user/bureau-review-steward.service
install -Dm644 ops/systemd/bureau-review-steward.timer \
  ~/.config/systemd/user/bureau-review-steward.timer
systemctl --user daemon-reload
systemctl --user enable --now bureau-review-steward.timer
```

The timer runs hourly at minute 23, after Closure lane selection. Conservative classifications are
limited to `reviewing`, `needs_revision`, `ci_failed`, `merge_candidate`, `blocked` and `obsolete`.
A `merge_candidate` means only that the lane can be handed to the merge gatekeeper; it is not a
merge permission.

Manual checks:

```bash
bureau-review-steward run --max-lanes 4
systemctl --user status bureau-review-steward.timer
journalctl --user -u bureau-review-steward.service -n 50 --no-pager
```

## Closure pull-request observation

Closure may observe open GitHub pull requests when the repository origin resolves to a GitHub `owner/repo` slug and `gh pr list` is available. The observation is fail-soft, but not fail-open: if GitHub metadata cannot be read, Closure records a blocked GitHub observation. Existing PR-linked lanes keep their previous PR evidence and are blocked from closure decisions until observation succeeds again.

Open pull requests are recorded as coordination evidence, not as a second pull-request authority. GitHub remains the owner for pull-request state, checks, review decisions and mergeability. Closure only stores `pr`, `pr_title`, `pr_url` and `observed_github_state` on the lane so that existing work can be routed to the right closure path.

Conservative lane derivation from observed GitHub state is intentionally narrow:

- `DIRTY` becomes `needs_revision`.
- `UNSTABLE` or `UNKNOWN` becomes `ci_failed`.
- `CHANGES_REQUESTED` becomes `needs_revision`.
- Draft pull requests become `reviewing`.
- `CLEAN` plus `APPROVED` becomes `merge_candidate`.
- `CLEAN` without approval remains `reviewing`.

Existing `paused` lanes keep their operator hold when pull-request observation would otherwise derive a review, revision, or merge-candidate state.

A `merge_candidate` lane is only eligible for merge-gatekeeper handoff. It is not a merge permission and does not replace explicit checks, review-thread inspection or final merge policy.

## Runtime observation and status projection

The GitHub observer and the read-only status projection board, including
their scheduler contract and the `bureau-status-projection` and
`bureau-reconcile` reference timers under `ops/systemd/`, are documented in
`docs/bureau-runtime-observation-v1.md`. Quick start:

```bash
bureau --root . --json github-observe
bureau --root . --json status-projection
```

Both commands observe and project only. They never verify tasks, mutate the
queue, merge, delete branches or clean up worktrees. When the StateStore contains
TaskSpec revisions, `status-projection` uses that TaskSpec projection as its operational
task catalog. Git task files remain the compatibility/source projection: `registry_state`
shows their declared state when one exists, while `task_spec_state` and
`effective_state` come from StateStore authority. StateStore-only tasks therefore remain
visible to Doctor and dashboard consumers. Likewise, `queue_lane` follows the
authoritative task priority view; the legacy `registry/queue.json` lane is exposed
separately as `compatibility_queue_lane` and can never gate claimability. A malformed
StateStore TaskSpec projection is a blocker rather than a reason to silently treat Git
as equivalent current truth.

### Bounded Control Plane Doctor (T011)

The T011 Control Plane Doctor is separate from the legacy dispatcher `bureau doctor` command. It is
fully read-only and composes the existing status projection with bounded backup and restore
observations:

```bash
PYTHONPATH=src python3 -m bureau.doctor --root . --state-root ~/.local/state/bureau
```

Its `control_plane` object is the bounded V3 consumer surface. Every organ reports `source`,
`freshness`, `bounds`, `authority` and `status`. The projection covers StateStore, task flow,
frontier, claims/reservations, backup, restore, GitHub bridge, closeout, workspaces and drift. Flow
metrics include intake, ready, claimable, in-flight, closeout-pending, drift, reservation count,
workspace count, backup age and restore status. `claimable` is deliberately only a read-only
approximation; final claimability belongs to the atomic claim path. StateStore reservations do not
prove concrete Grabowski lease liveness. Unknown, stale or blocked evidence is never promoted to a
success state.

`repair_plan` is a proposal, not an effect. Every proposal names the finding and impact, required
authority, a separate typed `apply_contract`, expected readback and a `dry_run_sha256`, while the
proposal itself reports `effect: none`. No repair is executed by this Doctor. An effect requires a
separate, explicitly authorized apply path followed by its own readback.

Dashboard and other read-only consumers must consume only `control_plane` or the Doctor's bounded
`dashboard` projection. They must not reconstruct task, queue, claim, completion or lease truth from
diagnostic output. Operational task/run/acceptance/closeout truth stays in Bureau StateStore;
concrete process and lease liveness stays with Grabowski; PR/review/CI/merge facts stay with GitHub;
backup and restore status comes only from their verified artifacts. This does not pre-empt T012/T013:
legacy Git-backed queue/registry writers may still exist during the transition, but they do not gain
a second V3 authority from this diagnostic surface.

For a Grabowski-independent last-known-good read path, publish and read the sealed capsule:

```bash
bureau-status-capsule write --canonical-repo ~/repos/bureau \
  --state-root ~/.local/state/bureau \
  --output ~/.local/state/bureau-readonly/status-capsule.json
bureau-status-capsule read \
  --path ~/.local/state/bureau-readonly/status-capsule.json
```

The reader touches only the capsule file and reports `fresh`, `stale` or `unavailable`; see
`docs/bureau-status-capsule-v1.md`.

### Canonical cycle scheduler deployment

The discovery, curator, operator, verifier and closure scheduler sources and their five service/timer pairs are versioned in this repository. Audit the installed deployment without writes:

```bash
bureau --json cycle-deployment
```

The audit binds source files, units, timers, compatibility shims and loaded module paths to the immutable Bureau deployment manifest. Exit `0` means agreement, `1` means readable drift and `2` means invalid or unsafe input. It never installs, rewrites, reloads, restarts or self-heals. The historical `bureau-halfhour-operator` discovery name remains stable. Runtime activation is a separate review-before-effect operation; see `docs/bureau-cycle-deployment-v1.md`.

### Bureau repo leases do not reserve operational state

Use `bureau --json lease-contract` when an operator needs to classify a Bureau command. The
contract is fail-closed for unlisted operations. A Bureau repository lease covers Git-backed code,
schema, Registry, merge and deployment mutations, but not Live Register state-store reads or
appends. If the Registry checkout is temporarily unavailable, `live-register
--catalog-validation deferred` preserves a visibly unvalidated operational event instead of
silently dropping the status update.

## Static Systemkatalog boundary

Bureau does not import task candidates, frontiers, promotions or reviewed tasks
from the static Systemkatalog. The former Cabinet bridge execution modules and
`bureau-systemkatalog-*` wrappers were retired by
`OPERATOR-ECOSYSTEM-REDUNDANCY-V1-T024`.

Validate that this authority boundary remains intact:

```bash
make systemkatalog-boundary
```

The gate rejects restored Cabinet execution modules, the retired
`systemkatalog-*` subcommands, obsolete console scripts and the former
`~/repos/cabinet` graph or bridge default paths. Historical contracts remain
available under `docs/archive/cabinet-era/` and do not authorize current
execution.

### Lease authority and run-bound worktrees

`docs/concurrency.md` is the human-readable concurrency contract. The current
`bureau --json lease-contract` result and live Grabowski lease readback are the machine authority.
This runbook neither grants nor narrows a lease.

Normal Registry work uses the exact object, component or path resources returned by the contract.
The broad key `repo:/home/alex/repos/bureau` remains emergency-only. Do not infer Grabowski resource
keys from Bureau's internal `worktree-admin` phase gate: the Bureau gate serializes the Git metadata
effect, while Grabowski independently proves ownership of the concrete repository and target paths.

For a claimed task with `isolation: worktree`, use the run-bound coordinator instead of assembling a
second lease recipe by hand:

```bash
bureau --root /clean/revision-matched/bureau --json workspace-create <run-id>
bureau --root /clean/revision-matched/bureau --json workspace-status <run-id>
```

Before creation, verify the claim envelope's `baseline_commit`, task and plan digests. If the
baseline is not the intended revision, stop and repair the task or claim path; do not reuse, reset or
adopt a foreign checkout. The coordinator-created worktree remains bound to the run and does not by
itself establish test sufficiency, merge readiness or deployment authority.

`registry/tasks/*.json` is now a read-only bootstrap/archive compatibility surface for ordinary
tasks; the current StateStore TaskSpec revision is the publication, revision and lifecycle
authority. New reviewed Operator-Intake tasks and Supply fallbacks are written directly to
StateStore and need no task-file/queue PR. `registry/queue.json` is read-only compatibility history.
The run envelope binds the exact authoritative task and plan revisions.
Diagnose intended resources without acquiring them:

```bash
bureau --json lease-contract \
  --operation registry-task-write \
  --subject BUREAU-TRUTH-MODEL-V2-T003
bureau --json lease-contract \
  --phase worktree-admin \
  --resource-key path:/home/alex/repos/bureau/.bureau-scopes/worktree-admin \
  --ttl-seconds 300 \
  --justification "create one run-bound worktree" \
  --expected-head <current-head>
```

Doctor reports nonterminal legacy tasks that still request the broad repository key. Planned tasks
produce a migration warning; a ready task or a task in `now` produces a blocker until its scope is
narrowed. Historical terminal evidence is not rewritten.


### GitHub protection after the T013 cutover

The protected `main` branch still requires the normal validation matrix and the
`registry-registration-preflight/freshness` context. T013 does **not** remove that required check:
it remains useful for code PR freshness and for the exceptional reviewed runtime-bootstrap TaskSpec
seam. Ordinary task registration, queue movement, Supply publication and closeout no longer create
PRs. Removing or renaming the required check would require a separate ruleset migration; do not use
T013 as an implicit branch-protection bypass.


## Redacted public StateStore snapshot

`BUREAU-CONTROL-PLANE-V3-T009` publishes a deliberately non-authoritative public projection at `registry/public-state.json`. The producer reads the manifest-bound Registry and local SQLite StateStore through `ReadOnlyStateStore`, verifies event/TaskSpec replay, and emits only the exact v1 top-level allowlist: `schema_version`, `kind`, `generated_at`, `repository`, `release`, `counts`, `frontier`, `event_checkpoint`, `roots`, `redaction`, and `snapshot_sha256`. The release identity contains only commit and schema version; the event checkpoint contains only its monotone integer ID. There are no task, event, receipt or log payloads in the artifact.

The two Merkle roots are built from canonically serialized public TaskSpec identities and receipt-digest identities; the underlying identities are not exported. `snapshot_sha256` is SHA-256 over canonical JSON with only that digest field removed, so release, aggregates, checkpoint, roots and the fixed redaction contract are one commitment. Immediately before every write, the exporter recursively scans all keys and values for local-path, secret/credential, prompt, raw-log/trace and PII markers and fails closed. Validation repeats the exact schema, redaction and digest checks without opening a StateStore. The local frontier calculation deliberately excludes GitHub open-PR observations; normal claim/dispatch gates remain authoritative and are not replaced by this aggregate.

Generate and transport the owner-only snapshot through the single manifest-validating local entrypoint:

```bash
~/.local/bin/bureau source-pr-bridge --kind state-snapshot --publish \
  --root ~/repos/bureau \
  --state-root ~/.local/state/bureau \
  --runtime-manifest ~/.local/share/bureau/deployment-manifest.json
```

When `--snapshot` is omitted, the bridge creates the redacted snapshot locally in an owner-private temporary directory, validates it, then gives those exact bytes to the authenticated transport. Supplying an existing `--snapshot` remains an explicit transport path, but never grants import or writeback authority. Publication copies the validated bytes unchanged into a detached `origin/main` worktree, rejects every changed path except `registry/public-state.json`, and force-updates the dedicated `automation/bureau-state-snapshot` branch with an exact remote lease. Reconcile also reads back the GitHub blob and requires byte-for-byte identity before creating or refreshing the review PR. The periodic snapshot path deliberately does not request auto-merge: `automation/bureau-state-snapshot` plus its review PR are the public transparency surface, while `main` remains the commit identity bound to the installed immutable Bureau runtime. Auto-merging each refreshed snapshot would advance `main` without a corresponding runtime release and correctly trigger `release-registry-identity-mismatch`, so that effect is outside this timer contract. GitHub is therefore file transport and hosted validation only: it never receives StateStore, queue, claim, dispatch, closeout or writeback authority. There is deliberately no snapshot import/apply API.

Install the reviewed user units only from the checked-out/released Bureau source that contains this implementation:

```bash
install -Dm644 ops/systemd/bureau-state-snapshot.service \
  ~/.config/systemd/user/bureau-state-snapshot.service
install -Dm644 ops/systemd/bureau-state-snapshot.timer \
  ~/.config/systemd/user/bureau-state-snapshot.timer
systemctl --user daemon-reload
systemctl --user enable --now bureau-state-snapshot.timer
```

`bureau-state-snapshot.service` invokes the stable manifest-validating `bureau` launcher once every 15 minutes. The bridge generates its owner-private temporary snapshot inside the unit's private temporary namespace before transport; no separate `bureau-state-snapshot` launcher or persistent staging directory is required. StateStore and release paths remain read-only and only the Bureau Git checkout is writable, with `UMask=0077` and the same systemd hardening as the other Bureau oneshots. The snapshot is transparency evidence only; it is not a backup, restore point, runtime-health receipt, signature or operational authority. Encrypted full backup and restore proof remain the separate T010 path below.

## StateStore backup and restore proof

`bureau.state_backup` creates a coherent StateStore snapshot without copying a live WAL file. It uses SQLite Online Backup, requires `PRAGMA integrity_check=ok` and an empty `foreign_key_check`, replays the append-only operational and TaskSpec event streams, and binds the resulting authoritative projection root to every materialised execution envelope and receipt. The manifest also seals aggregate envelope and receipt roots. Missing, extra or digest-mismatched materialisation makes the backup fail closed.

The supplied `bureau-state-backup.timer` stages a bundle every 15 minutes under `%h/artifacts/merges/bureau-state-backups/`. That path is already inside the enabled `heim-pc-restic-backup` source `artifacts/merges`; Restic remains the encrypted off-host transport. Bureau neither reads nor stores Restic credentials, and a local bundle receipt deliberately does not claim that an offsite snapshot completed. Verify the existing transport separately with `heim-pc-restic-backup status` or its normal backup receipt.

The daily `bureau-state-restore-test.timer` restores the newest verified bundle into an empty temporary state root and first proves the same authoritative event/TaskSpec, envelope and receipt roots. Grabowski leases and external process/GitHub state are deliberately absent from the bundle. It then runs Bureau's normal reconciler only against the temporary restored StateStore: local non-external workers are orphaned fail-closed, external runs are freshly observed through the current adapter, and any unavailable or unknown external observation makes the restore drill fail. No historical Grabowski lease is restored or reactivated.

Manual revision-bound checks after the immutable Bureau release containing this module is active:

```bash
~/.local/bin/bureau --state-root ~/.local/state/bureau state-backup \
  --backup-root ~/artifacts/merges/bureau-state-backups
~/.local/bin/bureau state-restore-test \
  --backup-root ~/artifacts/merges/bureau-state-backups \
  --receipt ~/artifacts/merges/bureau-state-backups/restore-tests/latest.json
```

Both service commands deliberately enter through the stable manifest-validating `~/.local/bin/bureau` launcher; they never import from a mutable checkout or historical virtualenv. A green restore receipt proves local recovery consistency plus a fresh, fail-closed reconciliation simulation in the empty restored root. Runtime activation of the supplied units and an actual Restic snapshot remain separate effects with their own readback.
