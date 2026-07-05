# Ownership

| Concern | Owner |
|---|---|
| commitment, queue, dependencies and coordination conflicts | Bureau Core |
| task-to-run binding and completion verification | Bureau Core |
| observation-derived findings around Bureau tasks | Bureau Ops |
| host/process execution and concrete leases | Grabowski |
| action readiness and specialised evidence | Steuerboard |
| readable research and decisions | Cabinet |
| visual projection | Schauwerk |
| append-only events | Chronik |
| branches, pull requests, reviews and CI facts | GitHub |

Bureau Ops may observe GitHub, Grabowski, Steuerboard, Cabinet, Schauwerk or Chronik facts and turn
them into Bureau-shaped findings, tasks or receipts. It does not replace the owner of the source
fact. When an ops finding changes Bureau state, the change must be explicit in the registry,
operational state or receipt trail.
