# Failure-domain capacity and coordination scope v1

Bureau already serializes overlapping resources and supports bounded capacity. This contract extends
that existing mechanism to failure domains and recovery paths; it does not add a second scheduler or
a global one-ball lock.

## Typed resources

A resilience boundary is a Registry resource with type `failure-domain` or `recovery-path`. Both
carry a positive `capacity` and a stable `criticality`. Tasks consume capacity or request exclusive
access through ordinary Bureau claims.

Examples:

```json
{
  "schema_version": 1,
  "id": "failure-domain.github-hosted-control-plane",
  "type": "failure-domain",
  "capacity": 2,
  "criticality": "essential"
}
```

Capacity `1` serializes work that can damage the same boundary. Higher capacity permits a deliberate,
bounded amount of parallel work. Different resources remain independent.

## Hash-bound coordination scope

Tasks that claim a typed resilience resource must include `coordination_scope`. The document binds:

- the base and source commits used for the assessment;
- the complete, sorted changed-path set and its digest;
- every failure-domain and recovery-path claim;
- explicit nonclaims about health, complete runtime state and execution authority;
- a digest over the complete scope document.

The Registry rejects missing scope, stale digests, free-form resource names, wrong resource types,
duplicate claims, claim/scope mismatch and non-neutral isolation. Git reachability and equality of
the asserted path set with the real base-to-source diff remain explicit consumer checks. The exact
document is copied into the execution
envelope and Grabowski handoff so consumers can verify it without treating it as live health or
mutation authority.

## Conflict behavior

File and component claims continue to describe direct write overlap. Resilience claims describe a
second, orthogonal conflict axis. Two changes may touch disjoint files and still serialize because
they consume the same capacity-one failure domain or recovery path. Conversely, work on independent
domains remains parallelizable.
