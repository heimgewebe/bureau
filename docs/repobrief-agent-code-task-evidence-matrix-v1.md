# RepoBrief Agent Code Task Evidence Matrix v1

Status: closes Bureau task `RBAE-V1-T001`.

## Purpose

This matrix translates RepoBrief's agent-only goal into concrete evidence
requirements for coding agents. It does not implement RepoBrief features. It
specifies what an agent must inspect, what may be used as supporting context,
what cannot be inferred, and when a live GitHub/Git/CI check is required before
patch planning or review claims are made.

The matrix is deliberately stricter than a generic required-reading profile:
code work can change behaviour, contracts, security posture, or operator
state. A snapshot can guide the agent, but it cannot be treated as fresh runtime
truth.

## Authority boundary

RepoBrief and the future Agent Workbench are allowed to provide deterministic,
read-only evidence surfaces:

- canonical markdown and bundle manifest;
- citation maps and strict source ranges;
- required-reading resolution;
- source citation projections;
- claim/evidence maps;
- static relation, graph, symbol, reference and test hints when available;
- freshness, availability and health diagnostics;
- external patch-evaluation artifacts as external observations only.

RepoBrief and Agent Workbench must not:

- apply patches;
- run shell commands, tests, linters or sandboxes;
- mutate Git state;
- create branches, pull requests or worktrees;
- read secrets;
- infer runtime correctness;
- infer test sufficiency;
- infer review completeness;
- infer merge readiness;
- infer security correctness.

Mutable validation belongs to external surfaces such as an isolated Patch
Evaluation Sidecar, CI, GitHub PR checks, explicit operator commands, or human
review.

## Evidence vocabulary

| Term | Meaning | Authority limit |
|---|---|---|
| Required evidence | Evidence the agent must inspect before proposing a code change or review claim for that task type. | Missing required evidence blocks or degrades the answer. |
| Recommended evidence | Evidence that improves confidence but does not automatically block work when absent. | Absence must be stated when it affects risk. |
| Live checks needed | Git/GitHub/CI/runtime checks that cannot be replaced by a RepoBrief snapshot. | Must be done by an external authorized surface. |
| Forbidden inference | A tempting conclusion that must not be drawn from RepoBrief evidence alone. | Must be repeated in agent-facing output when relevant. |
| Non-claims | Facts the evidence explicitly does not establish. | Must survive successful preflight/evaluation. |

## Global minimum for all code tasks

Every code task profile requires at least:

- task intent and target behaviour;
- canonical source or current file ranges for the touched area;
- citation or source range references for every specific code claim;
- freshness/availability status of the evidence used;
- explicit missing evidence and live gaps;
- a minimal-change rationale;
- a stop condition when required evidence is unavailable;
- non-claims for correctness, test sufficiency, runtime behaviour, security and
  merge readiness.

If the available context is only a RepoBrief snapshot and no live check has been
performed, the agent may suggest an investigation path or a provisional plan,
but must not claim the repository is current, the patch is safe, or tests would
pass.

## Matrix

| task kind | required evidence | recommended evidence | live checks needed | forbidden inference | minimal obligation |
|---|---|---|---|---|---|
| Bugfix | Bug statement; expected vs observed behaviour; cited ranges for suspected implementation; related tests or explicit absence; freshness/availability status. | Static references; relation cards; recent PR/issues; patch-evaluation observations when present. | Current branch/head; dirty state; relevant test command availability; CI state for PR-bound fixes. | A failing symptom proves the cited function is the cause; passing tests prove the bug is fixed. | Name the suspected cause, cite the exact ranges, propose the smallest change, list tests/checks to run externally. |
| Refactor | Target invariant; cited current implementation ranges; callers/references; public API/contract surfaces; related tests or absence. | Symbol index; import graph; relation graph; docs/runbooks referencing behaviour. | Current branch/head; open PR collision; test suite availability; downstream package/build checks when public API may move. | No behaviour change can be inferred from syntactic similarity; unused-looking code is safe to remove. | State preserved behaviour, affected symbols/files, rollback condition, and coverage gaps. |
| Feature add | Product/task goal; existing adjacent code ranges; extension points; contracts/schemas/config; tests for neighbouring behaviour. | Architecture docs; examples/fixtures; graph availability; prior similar PRs. | Current branch/head; open PRs touching same surfaces; CI expectations; runtime/config/secrets constraints when feature touches external systems. | Existing patterns imply acceptance; adding a test proves integration correctness. | Bound the feature slice, identify touched contracts, state missing product/runtime assumptions, and propose external validation. |
| API or contract change | Current schema/API/CLI/MCP contract; consumers; compatibility expectations; migration/backward-compatibility docs; tests validating contract shape. | Generated examples; versioning policy; docs freshness claims; relation cards to consumers. | Current published contract version; open PRs changing same contract; CI/schema checks; downstream consumer checks. | Internal tests prove external compatibility; schema validation proves semantic compatibility. | State compatibility impact, migration need, consumer risk, and exact contract checks required. |
| Test add or repair | Behaviour claim under test; existing test pattern; target code ranges; reason current test is missing/flaky/insufficient. | Coverage hints; historical failure logs; relation graph from target to tests. | Current test result; flaky history if available; CI job status; environment-specific dependencies. | New tests prove correctness; a repaired test proves production behaviour. | Explain the claim under test, why this test is appropriate, and what it still cannot prove. |
| PR review | PR diff; base/head commits; changed files/ranges; relevant contracts/tests/docs; CI/check status; task intent. | RepoBrief snapshot for base context; relation/symbol/test hints; prior review comments; patch-evaluation artifacts. | Live GitHub PR metadata; current head SHA; CI status; mergeability; unresolved review comments when available. | Snapshot context is the PR diff; CI pass means merge-ready; absence of found issues means correctness. | Bind review to head SHA, cite findings to diff/source ranges, distinguish blocking issues from suggestions, list non-claims. |
| Security-sensitive change | Threat model or risk statement; touched auth/secret/network/input/deserialization/file/path/permission surfaces; cited code ranges; existing guard/tests. | Security docs; dependency advisories; static analysis hints; prior incidents; external review notes. | Current dependencies; secret/config handling; CI/security scans when available; human/security review requirement for material risk. | Lack of obvious exploit means safe; tests prove security; static pattern match proves vulnerability. | Escalate uncertainty, avoid exploit overclaiming, identify sensitive boundary, require external/human review for high-risk changes. |
| Documentation-bound code change | Code range and corresponding doc/runbook/contract claim; freshness status; source of truth for the claim. | Doc freshness registry; generated docs/proofs; examples. | Current code/doc head; generated artifact status; CI/doc validation if docs are generated. | Updating docs updates behaviour; generated docs are fresh without checking producer state. | Keep doc and code claim aligned, state source-of-truth direction, and list stale/generated gaps. |
| Build, CI or tooling change | Current workflow/config; command contract; affected jobs/tools; existing failure or motivation; repo role policy. | Recent CI runs; tool version docs; local reproduction notes. | Live CI status; runner/environment constraints; branch protection requirements; open PRs changing same workflow. | Local success predicts CI success; CI config syntax pass proves job semantics. | State affected jobs, fallback path, expected failure mode, and required post-change CI observation. |
| Data migration or stateful runtime change | Schema/state model; migration direction; rollback/compatibility story; affected runtime services; data safety constraints. | Runbooks; production smoke boundaries; previous migration receipts. | Current deployed/runtime state; backups; migration mode; operator approval; smoke/rollback plan. | Repo state proves runtime state; migration success in one environment proves production safety. | Treat as high-impact, require explicit operator/runtime evidence, and avoid RepoBrief-only approval. |

## Profile aliases for future tools

The future Agent Workbench and Required Reading extensions may expose these
profile names. They are aliases for the matrix above, not implementation yet.

| profile | maps to | default strictness |
|---|---|---|
| `code_bugfix` | Bugfix | block on missing source ranges or missing test/absence statement |
| `code_refactor` | Refactor | block on missing references or missing behaviour invariant |
| `code_feature` | Feature add | warn/block depending on missing contracts or adjacent tests |
| `code_contract_change` | API or contract change | block on missing consumer/compatibility evidence |
| `code_test_repair` | Test add or repair | block on missing behaviour claim |
| `code_pr_review` | PR review | block on missing PR diff/head/CI state for review verdicts |
| `code_security_sensitive` | Security-sensitive change | block or escalate on missing sensitive-boundary evidence |
| `code_docs_bound` | Documentation-bound code change | warn/block on missing freshness/source-of-truth evidence |
| `code_tooling_change` | Build, CI or tooling change | block on missing workflow/job impact evidence |
| `code_stateful_change` | Data migration or stateful runtime change | block without explicit live/runtime evidence |

## Snapshot insufficiency rules

A RepoBrief snapshot is insufficient by itself when the task requires any of:

- current PR diff, head SHA, mergeability, review state or CI state;
- dirty working tree or untracked file knowledge;
- current branch collision detection;
- runtime service, deployment, database or state-root truth;
- secret/config availability;
- dependency/security advisory currency;
- generated artifact freshness after the snapshot time;
- proof that a command, test, migration or smoke actually ran.

In these cases an agent may still use RepoBrief to decide what to inspect, but
must request or consume external live evidence before making a final patch,
review, or readiness claim.

## Required non-claims

Every code-task evidence response must preserve these non-claims unless a
separate authority explicitly proves a narrower fact:

- `correctness`
- `test_sufficiency`
- `runtime_behavior`
- `security_correctness`
- `merge_readiness`
- `review_completeness`
- `regression_absence`
- `repo_understood`
- `all_relevant_context_used`
- `snapshot_freshness`

A successful matrix resolution means only that the evidence obligations were
identified and, where possible, satisfied. It is not approval.

## Acceptance mapping

- `rbae-v1-t001-task-matrix`: satisfied by the task-kind matrix covering
  bugfix, refactor, feature, contract change, test repair, PR review,
  security-sensitive, documentation-bound, tooling and stateful runtime changes.
- `rbae-v1-t001-boundary`: satisfied by the authority boundary and snapshot
  insufficiency rules, which require live Git/GitHub/CI/runtime checks when the
  snapshot cannot establish freshness or execution state.
- `rbae-v1-t001-non-claims`: satisfied by the required non-claims section and
  per-task forbidden inference column.

## Does not establish

This matrix does not implement resolver code, extend Lenskit contracts, prove
that RepoBrief improves patch quality, prove runtime correctness, prove test
sufficiency, prove review completeness, authorize merges, or prove security
correctness.
