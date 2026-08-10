# Bounded Bureau contract audit: runtime approval task authority

Status: proven gap; documentation-only closeout, code repair intentionally out of scope

Datum: 2026-08-10

Task: `OPERATOR-INTEGRATION-LOOP-V1-FB-AUDIT-D452BA0316`

Run: `BUR-RUN-20260810T080158Z-9eb87afdbb`

## Auditgegenstand

Geprüft wurde der enge Autoritätsvertrag des Bureau-Runtime-Refreshs:

> Eine Break-Glass-Freigabe für `runtime_mutation` darf nicht allein deshalb wirksam werden, weil die übergebene `approval_task_id` syntaktisch mit der Task-ID im Freigaberecord übereinstimmt. Die referenzierte aktuelle autoritative TaskSpec muss selbst diese Runtime-Mutation tragen dürfen.

Der Audit vergleicht den live verwendeten Runtime-Refresh-Vertrag, den generischen Approval-Core, die aktuelle Registry-TaskSpec der vorgesehenen Single-Use-Runtimeautorität T029, die tatsächlich verwendete fremde TaskSpec `CCM-V1-T007`, den aktuellen Deployment-Receipt und einen read-only Negativreproducer.

## Revisionsbindung

- Audit-Worktree-Baseline: `68b02903976cac47ec6daaebd4d1438e7078a5a9`
- Worktree: `/home/alex/repos/.bureau-worktrees/BUR-RUN-20260810T080158Z-9eb87afdbb`
- Branch: `bureau/operator-integration-loop-v1-fb-audit-d452ba0316/9eb87afdbb`
- Task-SHA-256 beim Claim: `9d7d837ce4b0be0c4a0e1eec3c1e589e0fd2164995cbf5be373a737eb42ad542`
- Plan-SHA-256 beim Claim: `12662f2ed585335286d4a73696388d05db725bb058550f810fb5478c03cb6ab2`
- beim Audit installierter Bureau-Runtime-Stand: `5ecbff7111768b9a4b24009256f632840e021f54`
- Deployment-Manifest-SHA-256: `44fe203a5b1172d104a6e68cb27515919419bdd11586e243dae6cbb9ff5c10e3`
- beim frischen Runtime-Refresh-Observe aktueller GitHub-main: `68b02903976cac47ec6daaebd4d1438e7078a5a9`
- frisches Observe: `2026-08-10T07:54:34.258940Z`, Observation-SHA-256 `eabb9f4adfd603e6fefc46123c1bb1c80c6fd23cf13b259e86f242c718a7c110`, Target-SHA-256 `897be0cfb6d131dc6e5a62ce069e05e3b2570a839249d2b40cca0338c50fcd53`

Der Audit-Claim bewies die Pfad-Disjunktheit zu den offenen Bureau-PRs #1837 und #1847. Schreibautorität dieses Runs umfasst ausschließlich `docs`/`docs/evidence`; der nachgewiesene Produktivcodefehler wird deshalb hier nicht direkt repariert.

## Methode und Primärevidenz

1. `src/bureau/runtime_refresh.py` auf Audit-Baseline direkt gelesen: `prepare_intent()` erzeugt Break-Glass-Evidenz aus frei übergebenem `approval_task_id` und ruft `approval.require_approval("runtime_mutation", ..., task_id=approval_task_id)` auf. `validate_runtime_approval_intent()` revalidiert denselben gespeicherten Freigaberecord, liest aber ebenfalls keine TaskSpec.
2. `src/bureau/approval.py` direkt gelesen: `approval_decision_for_effects()` prüft Approval-Level, positive Freigabe, erwartete Referenz, exakte Task-ID-Zeichenkettengleichheit und Scope. Es gibt dort keine Registry-/TaskSpec-Auflösung.
3. `registry/tasks/BUREAU-TRUTH-MODEL-V2-T029.json` direkt gelesen. T029 verlangt ausdrücklich vor `prepare-intent` und erneut vor `apply` eine aktuelle autoritative TaskSpec, die nichtterminal, für `runtime_mutation` vorgesehen und mit `break_glass` vereinbar ist; fremde Tasksubstitution ist verboten und T029 ist Single-Use.
4. `registry/tasks/CCM-V1-T007.json` direkt gelesen. Die Task betrifft ausschließlich stabile Chronik-CLI-Auflösung in Grabowski, claimt `repo.grabowski` und deklariert keine `runtime_mutation`-Freigabe.
5. Live-Deployment-Manifest gelesen. Der am `2026-08-10T07:36:31.454009+00:00` installierte Runtime-Stand `5ecbff711176...` enthält dennoch eine erfolgreiche `runtime_mutation`-Break-Glass-Freigabe mit `task_id=CCM-V1-T007` und `expected_task_id=CCM-V1-T007`.
6. Einen read-only Reproducer auf der Audit-Baseline ausgeführt: `approval.task_approval_contract(CCM-V1-T007)` klassifiziert die Task als `repository_mutation` mit erforderlichem Level `operator`; dieselbe Task-ID wird in einem synthetischen, ansonsten gültigen Break-Glass-Record für `runtime_mutation` dennoch `allowed=true`, solange `expected_task_id` ebenfalls auf `CCM-V1-T007` gesetzt wird. Bei erwarteter T029-ID schlägt die reine String-Bindung korrekt fehl.
7. Den Produktivfix nicht innerhalb dieses Docs-only-Runs versucht. Stattdessen wurde der umfassendere Runtime-Nachfolger/Härtungsbedarf bereits kanonisch im Bureau-Operator-Intake als `candidate-0b212cafab3a56118683c286`, aktuelle Revision Event `5225`, festgehalten.

## Belegt

### 1. Die aktuelle Freigabeprüfung beweist Identität, nicht fachliche Autorität

`approval.require_approval()` beantwortet die Frage: Ist ein positiver Freigaberecord vom richtigen Level mit passender Referenz, Task-ID und Scope vorhanden?

Die Implementierung beantwortet **nicht** die zusätzliche Frage: Erlaubt die aktuelle autoritative TaskSpec dieser Task überhaupt `runtime_mutation`?

Das ist kein Interpretationsproblem. Der Approval-Core erhält nur die erwartete Task-ID als String und keine Registry-/TaskSpec-Evidenz.

### 2. Der konkrete Live-Receipt hat eine fachlich fremde Task als Runtimeautorität akzeptiert

Das aktuelle Deployment-Manifest bindet den erfolgreichen Runtime-Refresh an:

- `action_class=runtime_mutation`
- `required_level=break_glass`
- `task_id=CCM-V1-T007`
- `expected_task_id=CCM-V1-T007`
- Target `27675fe96000535dd951a5457c4711d179f4143b4eec75d398dc259b38df0378`

Die aktuelle TaskSpec `CCM-V1-T007` enthält dagegen weder `component.bureau.runtime` noch einen `runtime_mutation`-Approvalvertrag. Ihr Ziel ist Chronik-CLI-Auflösung in Grabowski.

Damit ist der fehlende semantische TaskSpec-Preflight nicht nur theoretisch erreichbar, sondern bereits in einem echten Deployment-Receipt sichtbar.

### 3. T029 definiert die strengere Sollinvariante explizit

T029 Acceptance `authoritative-task-preflight` verlangt vor `prepare-intent` und vor `apply` die aktuelle autoritative StateStore-TaskSpec und verbietet andere Tasks als Freigabequelle. `stale-task-reuse-forbidden` und `single-use-lifecycle` verschärfen diese Grenze zusätzlich.

Der derzeitige generische Runtime-Refresh-Pfad erzwingt diese Invariante nicht selbst. Damit kann ein Operator oder aufrufender Workflow die T029-Grenze versehentlich umgehen, obwohl der Approval-Core formal grün meldet.

### 4. Read-only Negativreproducer

Reproducer-Ergebnis auf Baseline `68b02903976...`:

```text
CCM-V1-T007 declared task contract:
  action_class = repository_mutation
  required_level = operator

runtime_mutation approval with task_id=CCM-V1-T007
and expected_task_id=CCM-V1-T007:
  allowed = true

same approval with expected_task_id=BUREAU-TRUTH-MODEL-V2-T029:
  allowed = false
  reason = approval task_id CCM-V1-T007 does not match expected BUREAU-TRUTH-MODEL-V2-T029
```

Der vorhandene Negativschutz beweist also String-Isolation zwischen Task-IDs, aber keine fachliche Autoritätsprüfung der referenzierten Task.

## Plausibel

Der engste dauerhafte Fix ist, den Runtime-Refresh vor Intent-Erzeugung und erneut unmittelbar vor Effektstart an eine autoritative, digestgebundene TaskSpec-Projektion zu binden und mindestens folgende Eigenschaften zu prüfen:

- exakte Task-ID;
- aktueller nichtterminaler zulässiger Zustand;
- `execution.approval.action_class == runtime_mutation`;
- `required_level == break_glass`;
- passender Runtime-Claim/Autoritätsmetadatenvertrag;
- keine Single-Use-/Terminal-/Supersede-Wiederverwendung;
- TaskSpec-/StateStore-Digest wird in Intent bzw. Effekt-Readback gebunden und bei Drift verworfen.

Plausibel ist außerdem, dass die bereits in T029 formulierte `authoritative-task-preflight`-Invariante als kanonische Semantik wiederverwendet werden kann, statt einen zweiten konkurrierenden Autoritätsbegriff zu schaffen.

## Ungeklärt

- Der Audit beweist nicht, welcher bestehende StateStore-Lesepfad die beste atomare TaskSpec-/Digest-Bindung für `runtime_refresh.py` liefert. Das muss der Code-Folgetask gegen aktuelle StateStore-/Registry-Verträge entscheiden.
- Der Audit beweist nicht, ob alle historischen Runtime-Refreshes dieselbe Lücke benutzt haben. Belegt ist der aktuelle Manifest-Receipt vom 10. August 2026.
- Die Runtime liegt beim Audit zwei Commits hinter `main`. Der frische Observe-Target ist gesund und die Pflichtchecks sind grün, erfordert aber erneut Autorität. Wegen der hier belegten Lücke wurde kein weiterer Runtime-Refresh aus diesem Audit heraus autorisiert.

## Reparaturentscheidung

**Belegter Fehler, aber keine Produktivcodeänderung in diesem Run.**

Grund: Der Task besitzt absichtlich nur `component.bureau.docs` und exakte Leases für `docs`/`docs/evidence`. Eine Änderung an `src/bureau/runtime_refresh.py`, `src/bureau/approval.py`, Tests oder Registry-TaskSpecs würde den revisionsgebundenen Claim-Scope verletzen.

Ein `create_workspace=false`-Bypass, eine Direktänderung im Entwicklercheckout oder die Wiederverwendung einer fremden Runtime-Task wären schlechter als der belegte Defekt. Der korrekte bounded Closeout ist daher:

1. reproduzierbare Audit-Evidenz committen;
2. Produktivfix separat über den bereits registrierten Bureau-Kandidaten `candidate-0b212cafab3a56118683c286` / Event `5225` treiben;
3. dort einen Negativtest ergänzen, der genau den hier reproduzierten Fall (`CCM-V1-T007` als fremde Runtimeautorität) vor Effektstart abweist.

## Alternative Sinnachse

Wenn kurzfristige Runtime-Konvergenz höher gewichtet würde als Autoritätstreue, könnte der aktuelle `68b029...`-Target mit irgendeiner passend aufgebauten Break-Glass-Task-ID erneut deployt werden. Das wäre technisch schnell, würde aber den hier belegten Fehler erneut benutzen und die Single-Use-/TaskSpec-Grenze entwerten.

Wenn dagegen Beweissicherheit und spätere Replizierbarkeit höher gewichtet werden, ist der aktuelle Pfad vorzuziehen: kein zweiter Runtime-Effekt, Audit-Receipt sichern, semantische TaskSpec-Bindung zuerst reparieren und danach eine frische Observation/Autorität erzeugen.

## Nutzen, Risiko und Folgen

- **Nutzen des Fixes:** Eine fremde oder stale Task-ID kann nicht mehr allein durch syntaktisch passende Break-Glass-Evidenz Runtimeautorität erhalten.
- **Risiko des Fixes:** Zusätzliche StateStore-/TaskSpec-CAS-Bindung kann legitime Refreshes bei Drift häufiger fail-closed stoppen; deshalb muss die Recovery einen klaren neuen-Intent-Pfad bieten.
- **Folge ohne Fix:** Der Approval-Receipt kann formal korrekt aussehen, obwohl die referenzierte Task fachlich keine Runtime-Mutation autorisiert. Das erschwert Auditierbarkeit und kann Single-Use-Grenzen faktisch unterlaufen.

## Unsicherheit

Unsicherheit: `0.03`. Ursache: Primärcode, aktuelle TaskSpecs, echter Deployment-Receipt und read-only Reproducer stimmen auf denselben Vertragsfehler überein. Restunsicherheit betrifft nur die optimale technische Reparaturform.

Interpolationsgrad: `0.02`. Fast alle Kernaussagen sind direkte Beobachtungen; die vorgeschlagene konkrete CAS-/Digest-Form des Fixes ist eine Architekturfolgerung.

## Acceptance-Abbildung

- `audit-1`: Vertrag, Baseline, Runtime-Stand, Methode und Evidenzquellen sind oben explizit revisionsgebunden.
- `audit-2`: `Belegt`, `Plausibel` und `Ungeklärt` sind getrennt ausgewiesen.
- `audit-3`: Kein Code-Repair innerhalb des Docs-only-Scopes; die Nichtänderung ist explizit begründet. Ein negativer read-only Reproducer ist dokumentiert und der separate Produktivfix verlangt denselben Fall als Regressionstest.
