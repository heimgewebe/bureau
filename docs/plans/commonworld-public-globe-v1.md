# Commonworld public globe follow-up hardening v1

## Purpose

Commonworld PR #66 shipped the first public MapLibre globe and was physically accepted on Android, merged as `92c6993b8c1cac07caf0771c6071de9ca4721047`, deployed through GitHub Pages and hash-verified at `https://commonworld.net/`. That release proves the bounded public slice. It does not prove the four explicit nonclaims left by the release evidence.

This plan keeps the shipped surface as the baseline and registers only residual work. It does not reopen `COMMONWORLD-ATLAS-V1`, add tasks to the execution queue or authorize implementation by itself.

## Bound evidence

- reviewed head: `e1c701a2e7260091e31d22a056daab7b48382a72`;
- reviewed and merged tree: `7e97820d6f2c06010fd072e6dcd6a9020154e3de`;
- squash merge: `92c6993b8c1cac07caf0771c6071de9ca4721047`;
- physical Android PASS receipt SHA-256: `27e7e2a2e5d9fef3c56a9d660b31ab8029d855c69dc3de411fc0c53e15836547`;
- final closeout SHA-256: `0ce6ae2806461f8fb7c296bd46c288cf4fe6382a3f8b298086cc0916a0d4dca4`;
- live Pages smoke SHA-256: `9b329f5112a3c62c093bb1e2e3e481e71196cdb5683649422dcb77b60c6de509`.

## Residual tasks

| Task | Question | Required result |
| --- | --- | --- |
| `COMMONWORLD-PUBLIC-GLOBE-V1-T001` | Does Android Reduced Motion actually select the non-animated runtime path on a physical device? | Exact physical PASS or explicit blocker/nonclaim. |
| `COMMONWORLD-PUBLIC-GLOBE-V1-T002` | Is the globe a usable screen-reader product surface rather than merely keyboard-accessible markup? | Bounded physical assistive-technology evidence and repaired semantics. |
| `COMMONWORLD-PUBLIC-GLOBE-V1-T003` | Who carries production responsibility for delivery and basemap availability? | Explicit architecture/provider authorization or evidence-based deferral. |

## Ordering and queue boundary

The three tasks are independent questions but share the Commonworld repository, so the initiative permits one active task at a time. They remain `planned` in the `later` lane and are deliberately absent from `registry/queue.json`. A later prioritization decision may promote one task after live-state, conflict and resource checks.

## Nonclaims

This registration does not establish Android-wide accessibility, screen-reader support, WCAG conformance, a provider SLA, production architecture authorization, backend readiness, migration permission, runtime correctness after future changes or merge readiness.


## Optimization program registered from the 2026-07-13 deep audit

The live Globe-first release remains the baseline. The audit found that the globe is technically stable but still carries only digital Commons, while seed-only validators, stale release gates and failure-state behavior block the next product phase. The following work is therefore ordered and limited to one active Commonworld task at a time.

| Task | Result | Dependency |
| --- | --- | --- |
| `T005` | Current truth, real browser CI, failure-safe controls, interaction hardening and explicit licensing/method boundary | none |
| `T006` | Real sourced geographic and hybrid vertical slice with semantic zoom | `T005` |
| `T007` | Intent-oriented German search, results, spatial navigation and filters | `T006` |
| `T008` | Scalable static loading, machine access and public metadata | `T007` |
| `T009` | Balanced 30–50 entry catalog, freshness workflow and explicit Weltgewebe handoff | `T008` |
| `T010` | Physical VoiceOver, TalkBack and desktop screen-reader product evidence | `T009` |

The task chain deliberately forbids premature backend, account, database, PMTiles, vector-search or independent CLI work. Such architecture requires measured thresholds from T008 rather than anticipation.
