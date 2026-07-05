# Operator Learning Axis v1

Status: candidate
Date: 2026-07-05

## Goal

Make Grabowski routing decisions learnable by heimlern without changing live routing rules.

## Boundary

The learning axis is offline and proposal-only. It records routing choices and outcomes, then lets heimlern analyze patterns. It does not authorize automatic routing changes.

## First sequence

1. Add routing decision and routing outcome contracts to heimlern.
2. Add a small adapter from Grabowski friction records and receipts to routing outcomes.
3. Map routing outcomes into heimlern feedback analysis.
4. Add a fixture corpus for success, blocked, failed and fail-closed runs.
5. Only after enough evidence, define routing adjustment proposals.

## Organs

- Grabowski emits decisions, friction and execution receipts.
- heimlern analyzes outcomes and proposes changes.
- Bureau coordinates non-conflicting follow-up work.
- Chronik may later transport events.
- Leitstand may later visualize trends.

## Non-claims

This plan does not establish live routing readiness, policy superiority, sample sufficiency or permission to auto-apply learned weights.
