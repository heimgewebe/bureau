# SEMANTAH-USEFULNESS-V1 — RepoBrief-bound usefulness plan

Status: planned
Registered: 2026-07-05
Target repo: heimgewebe/semantAH
Bureau initiative target: SEMANTAH-USEFULNESS-V1

## Thesis / antithesis / synthesis

Thesis: semantAH can become useful if it provides local, contract-first semantic evidence: versioned embeddings, deterministic search hits, provenance, and factual observatory receipts.

Antithesis: semantAH is not yet useful enough as an operating organ. The current repository contains real indexd and embedding code, but the named pipeline is still partly stubbed; contracts and namespace models drift; and some artifacts can sound more semantic than their evidence permits.

Synthesis: Do not expand semantAH into a knowledge graph first. Shrink it into a Semantic Evidence Service, then prove usefulness through a RepoBrief-bound snapshot, contract repair, real docs ingest, and retrieval evaluation.

## RepoBrief correction

RepoBrief is not a free-form audit essay. In Lenskit terminology it is the public system name for deterministic, citable repository snapshots and agent-briefing bundles. The Canonical Brief Source is the authority inside the bundle; sidecars are navigation, diagnostics, evidence indexes, or caches. Read paths must not silently refresh. RepoBrief does not establish truth, correctness, completeness, runtime behavior, test sufficiency, regression absence, repository understanding, claim validity, freshness, or forensic readiness.

Consequence: the prior chat-produced semantAH audit is an audit input, not a real RepoBrief. The first Bureau task must create or reference an actual RepoBrief snapshot before treating the audit as durable evidence.

## Verified / falsified audit basis

Verified:

- semantAH claims roles around HausKI memory, script pipeline, Rust index service, .gewebe artifacts, KPI search, Obsidian auto-links, timers, and WGX recipes.
- README explicitly says the project is an initial state and many components are placeholders.
- indexd exposes /index/upsert, /index/delete, /index/search, and /embed/text.
- Ollama embedder uses /api/embed with an input field and validates returned embedding count and dimensions.
- tools/build_index.py, tools/build_graph.py, and tools/update_related.py are stubs or dry-run/demo surfaces.
- VectorStore is in-memory and dimension-constrained by first insertion.
- Search is linear over namespace items with heap-based top-k, not ANN/HNSW.
- JSONL persistence rows are currently minimal: namespace, doc_id, chunk_id, embedding, meta.
- docs/contracts/output.md currently contradicts the repository by saying insights.daily.schema.json no longer exists, while contracts/insights.daily.schema.json exists.
- contracts/insights.schema.json is a review-insight schema, not the daily-summary schema.
- docs/namespaces.md uses vault/web/notes:private, while embedding schema uses chronik/osctx/docs/code/insights.
- export_daily_insights.py can derive topics from top-level folders or Observatory confidence, which is not necessarily semantic evidence.
- observatory_mvp.py states its store statistics are heuristic/approximate and not forensic.
- WGX snapshot runs tests and then executes the three stub tools, which can make the pipeline appear more complete than it is.
- Auto-linking is premature because the strategy document says semantAH measures while hausKI decides.

Falsified:

- The prior chat audit is not a real RepoBrief artifact. It lacks a deterministic Bundle Manifest, Canonical Brief Source, snapshot check, mutation boundary, and negative semantics.

Open:

- Local Runtime Smoke is missing.
- Real semantAH search quality is unmeasured.
- p95 latency on actual data is unmeasured.
- Coverage behavior for WGX metrics is unverified locally.
- Existing external consumers of semantAH artifacts were not fully audited.

## Target role

semantAH should become the local, contract-first Semantic Evidence Service for Heimgewebe.

It may:

- create versioned embeddings;
- keep a rebuildable local search index;
- return search hits with provenance;
- publish evidence-only observatory receipts;
- provide read-only evidence to Cabinet, Leitstand, Lenskit, Chronik, or Grabowski.

It must not:

- autonomously write Related blocks;
- decide link acceptance;
- tune policies on its own;
- claim understanding;
- treat generated topics as truth;
- let stub tools count as production proof.

## Phased plan

### Phase 0 — Real RepoBrief starting point

Create a real RepoBrief snapshot for semantAH using the Lenskit RepoBrief toolchain.

Expected command shape:

```bash
repobrief snapshot create \
  --repo /home/alex/repos/semantAH \
  --out /tmp/semantah-repobrief \
  --profile pr-review \
  --output-mode retrieval \
  --redact-secrets

repobrief snapshot check \
  --bundle-manifest /tmp/semantah-repobrief/*.bundle.manifest.json \
  --task-profile basic_repo_question
```

Acceptance:

- Bundle manifest exists.
- Snapshot check is pass or explicitly warn.
- Mutation boundary says writes are limited to brief bundle artifacts.
- Negative semantics are preserved.

### Phase 1 — Store verified audit as Bureau artifact

Create `docs/audits/semantah-usefulness-audit-v1.md` in Bureau, bound to the real RepoBrief snapshot metadata when available.

Acceptance:

- Claims are split into verified, falsified, and open.
- RepoBrief correction is explicit.
- No claim asserts runtime correctness without evidence.

### Phase 2 — Repair insight contract drift

In semantAH, repair the daily/review insight contract confusion.

Acceptance:

- `docs/contracts/output.md` no longer says `insights.daily.schema.json` does not exist.
- `insights.schema.json` is documented as review-insight.
- `insights.daily.schema.json` is documented as daily-summary.
- Daily exporter validates against the daily schema.

### Phase 3 — Canonicalize namespaces

Adopt canonical namespaces: `docs`, `code`, `chronik`, `osctx`, `insights`. Treat `vault` as legacy alias to `docs` only if needed.

Acceptance:

- Namespace documentation, schemas, and API behavior are aligned.
- Tests cover allowed and rejected namespaces.

### Phase 4 — Separate stubs from production snapshot

Demote the current stub tools to demo-only or require an explicit `--demo` flag.

Acceptance:

- WGX snapshot no longer treats stub artifacts as production pipeline proof.
- Demo tasks are visibly labeled as demo.
- A production ingest smoke either runs real ingest or fails honestly.

### Phase 5 — Define index.store.v1

Add a validated internal Store Row contract.

Required row concepts:

- schema_version;
- namespace;
- source_ref;
- content_hash;
- doc_id;
- chunk_id;
- text_hash;
- snippet;
- embedding;
- embedding_model;
- embedding_dim;
- model_revision;
- generated_at.

Acceptance:

- JSON Schema exists.
- Validator exists.
- Observatory can later rely on validator rather than regex.

### Phase 6 — Implement docs markdown ingest MVP

Build a minimal real ingest path for Markdown docs only.

Acceptance:

- deterministic chunk IDs;
- source_ref and hashes;
- validated store rows;
- no graph;
- no Related writes;
- search smoke against fixture.

### Phase 7 — Add retrieval evaluation harness

Measure usefulness before integration.

Metrics:

- Precision@5;
- Recall@10;
- MRR;
- p95 latency;
- keyword baseline comparison.

Acceptance:

- Golden queries exist.
- Expected refs exist.
- Failures classify why retrieval failed.
- No consumer integration unless semantAH beats or usefully complements baseline.

### Phase 8 — Make Observatory evidence-only

Refactor Observatory so it reports validated evidence only.

Acceptance:

- No regex counting over store rows.
- No static topics without data.
- Blind spots derive from missing evidence.
- Counts are by namespace and model revision.

### Phase 9 — Add read-only consumer adapter

Integrate only after retrieval evaluation.

Preferred order:

1. Leitstand index coverage/status.
2. Cabinet read-only search evidence.
3. Lenskit optional external evidence, not deterministic RepoBrief core.
4. Chronik receipt history.

Acceptance:

- No auto-writes.
- No autonomous decisions.
- Consumer treats semantAH as evidence, not authority.

## PR slice sequence

1. `docs: add verified semantAH usefulness audit`
2. `fix: repair semantAH insight contract drift`
3. `fix: canonicalize semantAH namespaces`
4. `test: separate semantAH demo stubs from production snapshot`
5. `feat: define semantAH index.store.v1 row contract`
6. `feat: implement docs markdown ingest MVP`
7. `test: add semantAH retrieval evaluation harness`
8. `refactor: make semantAH observatory evidence-only`
9. `feat: expose semantAH read-only adapter`

## Risk and benefit

Benefits:

- semantAH becomes measurable instead of aspirational;
- Bureau receives a durable, planned sequence;
- RepoBrief and semantAH stay conceptually separate;
- stubs can no longer masquerade as production proof;
- future consumer integration has evidence.

Risks:

- short-term CI or docs churn;
- some existing documentation becomes explicitly stale;
- knowledge-graph and auto-link ambitions are deferred;
- local RepoBrief tooling may reveal additional gaps.

## Decision rule

If Phase 7 cannot show measurable retrieval usefulness against a keyword baseline, do not proceed to consumer integration. Park semantAH as an embedding/index experiment, not a Heimgewebe operating organ.
