# Chronik–Heimlern Outcome Bridge: T001 Live Audit

Status: verified

Bureau task: `CHRONIK-HEIMLERN-OUTCOME-BRIDGE-V1-T001`

Audited at: 2026-07-10T23:43:23Z

## Question

Which existing surface can carry redacted operator routing outcomes from Grabowski through Chronik into Heimlern without moving task, execution or routing authority?

## Live baselines

| Repository/runtime | Audited identity | Finding |
| --- | --- | --- |
| Chronik | `e9d6b7d0de2cf1b7cd95ec254096d0e64a99732c` | Generic append-only JSONL ingest and cursor export already exist. |
| Heimlern | `6d70e4900377c92b540d07bd6b71fe36677c2ba5` | Owns strict `operator.routing_outcome.v1`; the existing generic Chronik consumer parses a different event family. |
| Grabowski | `6a169c5f8f580907b64a51ba797b2d8bcc93b3e0` | Task lifecycle and friction receipts exist, but no `operator.routing_outcome.v1` producer exists. |
| Chronik user service | inactive at audit time | No listener or successful local health response was observed; no runtime change was attempted. |

## Belegt

Chronik already has the transport foundation:

- append-only JSONL writes;
- domain-scoped ingest;
- cursor-based event export;
- optional strict provenance requiring event identity and source repository/component.

The existing surfaces are not yet a complete outcome bridge:

- Heimlern's payload schema is strict and cannot safely receive extra Chronik provenance fields directly;
- the generic Heimlern Chronik path consumes `AussenEvent`, not routing outcomes;
- no Grabowski producer emits the required redacted payload and envelope;
- Chronik runtime inactivity means repository contracts do not establish a deployed path.

## Optimized boundary

Use a two-layer event:

1. **Heimlern owns the routing-outcome payload contract.**
2. **Chronik owns the append-only transport envelope, source identity, payload digest and historical timestamps.**
3. **Heimlern validates both layers through a dedicated typed consumer and recomputes freshness at read time.**
4. **Grabowski later emits real redacted outcomes from execution receipts through a separate reviewed producer task.**

This avoids a second payload truth in Chronik and avoids storing a mutable `fresh`/`stale` verdict in append-only history.

## Missing surfaces

- Dedicated Heimlern typed consumer for the Chronik envelope.
- Real Grabowski producer for redacted routing outcomes.
- Reviewed runtime deployment and live round-trip proof.

## Non-claims

This audit does not establish runtime readiness, sufficient production samples, route superiority, automatic application permission or merge readiness for later tasks.
