# OPERATOR-INTEGRATION-LOOP-V1-FB-AUDIT-B90EE27F9B — Task-Supply-Packability-Audit

Datum: 2026-08-17
Run: `BUR-RUN-20260817T203654Z-1335a75d77`
Task: `OPERATOR-INTEGRATION-LOOP-V1-FB-AUDIT-B90EE27F9B`
Audit-Baseline: `6006336fc38399f7c4bd3869667e2ca952ab9475`
Frischer `origin/main`-Readback während des Audits: `3ea976fbb6af242fda42ba726e24e37cf6cec7e8`
Installierter Bureau-Release während des Audits: `6006336fc38399f7c4bd3869667e2ca952ab9475`
Arbeitsbereich: `docs/`, `docs/evidence/`
Entscheidung: **Belegter Vertragsgap, aber kein paralleler Core-Fix. Der vorhandene Reparaturtask `BUREAU-CONTROL-PLANE-V3-T034` besitzt die einschlägigen Core-Leases; dieses Audit dokumentiert den Gap revisionsgebunden und schließt ohne Scope-Übergriff.**

## Auditvertrag

Geprüft wurde der aktuelle Claimable-Supply-Vertrag aus `docs/bureau-ready-supply-fallback-v1.md`, `src/bureau/task_supply.py` und `src/bureau/supply_runner.py` sowie die bereits registrierte Korrekturanforderung `BUREAU-CONTROL-PLANE-V3-T034`:

1. Der v1-Vertrag soll rohe `ready`-Dokumente nicht mit tatsächlich claimbarer Arbeit verwechseln.
2. Worker-Capabilities, Runtime-Gates und aktuelle Dispatch-Blocker gehen in die individuelle Claimability ein.
3. **Zusätzliche, bereits registrierte T034-Anforderung:** Der konfigurierte Floor soll gemeinsam ausführbare Versorgung ausdrücken; exklusive Ressourcen-, Lease- und Open-PR-Konflikte dürfen deshalb keine scheinbare Kapazität erzeugen.
4. Publication bleibt von Claim-, Lease-, Merge- und Deployment-Autorität getrennt.

Punkt 3 wird hier ausdrücklich nicht rückwirkend als Wortlaut der älteren v1-Dokumentation ausgegeben. Er ist die kanonisch registrierte Korrektur des aus v1 beobachteten Semantik-Gaps.

Die Prüfung ist an `6006336f…` eingefroren. Ein frischer Fetch zeigte während des Audits `origin/main = 3ea976fb…`; für die geprüften Dateien `task_supply.py`, `supply_runner.py`, `agent_frontier.py`, die beiden fokussierten Supply-Tests, den Supply-Vertrag und die T034-TaskSpec gab es zwischen beiden Revisionen **keinen Diff**. Der Befund ist damit nicht durch die zwischenzeitliche Main-Bewegung überholt.

## Methode und Primärevidenz

### 1. Codepfad

`classify_frontier()` in `src/bureau/task_supply.py` berechnet Claimability pro Frontier-Eintrag. `normal_claimable_count`, `fallback_claimable_count` und `total_claimable_count` entstehen anschließend ausschließlich durch Listenlängen dieser individuell claimbaren Einträge.

Die Funktion liest aus `task_documents` für diese Klassifikation nur Supply-Fallback-Metadaten. Die Task-Claims beziehungsweise Ressourcenbeziehungen zweier gleichzeitig gezählter Tasks werden nicht gegeneinander geprüft. Damit existiert auf der auditierten Revision keine Joint-Packability-Stufe für den Floor.

`observe_authoritative_frontier()` in `src/bureau/supply_runner.py` verwendet korrekt den kanonischen Dispatcher und dessen Runtime-Gate. Das schützt gegen **aktuelle** individuelle Blocker. Es beantwortet aber nicht die andere Frage, ob zwei heute jeweils freie exklusive Ressourcenansprüche nach dem ersten Claim noch gemeinsam verfügbar wären.

### 2. Minimaler negativer Reproducer

Revisionsgebundener Probe gegen `classify_frontier()`:

- Task `A`: `ready`, `eligible`, exklusiver Write-Claim auf `repo.same`.
- Task `B`: `ready`, `eligible`, derselbe exklusive Write-Claim auf `repo.same`.
- Beide Taskdokumente werden der Klassifikation übergeben.

Readback:

`{"claimable_ids": ["A", "B"], "normal_claimable_count": 2}`

Damit ist direkt belegt: Der aktuelle Zähler behandelt zwei individuell claimbare, aber nicht gemeinsam ausführbare Aufgaben als Kapazität `2`.

### 3. Regression-Suite

Kanonischer Repo-Testpfad laut `Makefile`: `PYTHONPATH=src pytest`.

Gezielter Lauf:

`env PYTHONPATH=src python -B -m pytest tests/test_task_supply.py tests/test_supply_runner.py -p no:cacheprovider -q`

Ergebnis: **PASS, 69/69 Tests**.

Grabowski-Job: `grabowski-job-27d8b36bb6da`
Argv-SHA-256: `c761458b464242b5a65a90786f20a168cabb99da79082842736b86b631febea0`
Finalisierungsreceipt: `02378a5a93a887038474051a209b035ed22463117221386c8ae0e16e4aa4047d`
Finalstatus: `succeeded`, Exitcode `0`.

Ein vorheriger nackter System-Python-Aufruf scheiterte bereits bei der Test-Collection an fehlendem `PYTHONPATH`; er ist ein Prüfkommando-Fehler und keine Produktregression.

Die bestehende Suite verhindert auf `6006336f…` den reproduzierten Joint-Packability-Fall nicht: Trotz 69/69 PASS liefert der direkte Probe weiterhin Kapazität `2` für zwei identisch exklusiv beanspruchende Tasks. Die grüne Suite widerlegt den Gap daher nicht.

### 4. Registry und vorhandener Reparaturpfad

`registry/tasks/BUREAU-CONTROL-PLANE-V3-T034.json` benennt genau diesen Gap: Der Claimable-Supply-Floor soll capability- und konfliktfeasible werden, einschließlich paarweiser Ressourcen-, Lease- und Open-PR-Konflikte sowie explizitem `unreachable/blocked`, wenn der Floor strukturell nicht erreichbar ist.

Die T034-Acceptance verlangt außerdem eine Regression des am 2026-08-16 beobachteten Falls, in dem ein Floor von acht nicht durch bloßes Nachpublizieren ungeeigneter Fallbacks als erreichbar gelten darf.

Live-Koordination während dieses Audits:

- T034-Run: `BUR-RUN-20260817T193511Z-4052929477`.
- `src/bureau/task_supply.py`, `src/bureau/agent_frontier.py`, `tests/test_task_supply.py` und die T034-TaskSpec sind durch diesen fremden Run geleast.
- Die Lease ist weiterhin aktiv und darf von diesem Audit nicht freigegeben oder übernommen werden.
- Der T034-Execution-Binding-Readback war gleichzeitig `stale` wegen eines alten Heartbeats und ohne externes Executor-Binding. Daraus folgt **nicht**, dass die Arbeit abgebrochen oder freigabefähig ist; die Lease-Autorität bleibt bestehen.

Damit existiert bereits ein eindeutiger Bureau-Folgetask für den bewiesenen Gap. Ein Duplikat wäre Attention-Rauschen statt zusätzlicher Sicherheit.

### 5. Runtime und historische Supply-Evidenz

Der installierte Bureau-Release meldete während des Audit-Bindings Source-Commit `6006336f…`, gültiges Deployment-Manifest und StateStore-Integrität `ok`. Damit stimmt die ausgeführte Bureau-Runtime für den auditierten Codepfad mit der eingefrorenen Audit-Baseline überein.

Der letzte persistierte Task-Supply-Receipt unter `/home/alex/.local/state/bureau-task-supply/latest.json` stammt vom 2026-08-16T14:49:08Z und meldet:

- `normal_claimable_count = 1`
- `fallback_claimable_count = 4`
- `total_claimable_count = 5`
- `floor = 8`
- Status `blocked`
- Blocker `registry-mutation-authority-unavailable`

Der Vertrag dokumentiert ausdrücklich, dass der Supply-Runner **nicht** an einen Timer gebunden ist. Das Alter dieses Receipts ist deshalb kein Runtime-Fehler und wird nicht als aktuelle Frontier-Wahrheit interpretiert.

### 6. GitHub/CI

Beim frischen GitHub-Readback bestanden keine offenen Pull Requests in `heimgewebe/bureau`.

Die unmittelbar in der Audit-Baseline enthaltene PR #2059 hatte grüne Pflicht-/Sicherheitschecks: `validate (3.10)`, `validate (3.12)`, beide `registry-registration-preflight/freshness`-Prüfungen sowie CodeQL `Analyze (python)` und `Analyze (actions)` waren erfolgreich; `main-push-revalidation` war lediglich `SKIPPED`.

### 7. Registry-/Repo-Validierung nach dem Auditbeleg

`env PYTHONPATH=src python -B -m bureau.cli --root . check`

Ergebnis: **PASS** (`valid: True`, 73 Initiativen, 1159 Tasks, 69 Ressourcen, StateStore-Integrität `ok`, keine `broad_bureau_scope_findings`).

Grabowski-Job: `grabowski-job-5e0d60f846bd`
Finalisierungsreceipt: `f9e57e9771962682def0c47679f897fcdf5f2103e98d42dc24e02624413a764e`

## Befunde

### Belegt

- Die auditierten Supply-Dateien sind zwischen der eingefrorenen Baseline `6006336f…` und dem frisch gelesenen `origin/main` `3ea976fb…` unverändert.
- Die installierte Bureau-Runtime war revisionsgleich zu `6006336f…`; der StateStore meldete Integrität `ok`.
- Der aktuelle Supply-Zähler zählt individuelle Claimability und besitzt keine Joint-Packability-Prüfung.
- Der minimale Probe zählt zwei Tasks mit identischem exklusivem Write-Resource als Kapazität `2`.
- Die fokussierte bestehende Regression-Suite ist 69/69 grün, verhindert den reproduzierten Joint-Packability-Fall aber nicht.
- Der Reparaturtask `BUREAU-CONTROL-PLANE-V3-T034` beschreibt genau den bewiesenen Defekt und fordert passende negative Regressionen.
- T034-Corepfade sind fremd geleast. Dieses Audit besitzt nur `docs/`-/`docs/evidence/`-Schreibautorität.

### Plausibel, aber nicht als abgeschlossen behauptet

- T034 ist der richtige dauerhafte Reparaturpfad, weil sein Ziel und seine Acceptance den reproduzierten Gap exakt abdecken.
- Nach einer korrekten T034-Implementierung sollte der Floor eher eine gemeinsam packbare Kapazität als eine bloße Summe einzelner Claimability-Beobachtungen ausdrücken.

### Offen / nicht interpoliert

- Ob T034 gerade produktiv weiterbearbeitet wird. Sein Run hat aktive Leases, aber der Execution-Binding-Readback ist wegen altem Heartbeat `stale` und besitzt kein externes Executor-Binding. Weder Übernahme noch Freigabe ist daraus erlaubt.
- Ob die spätere T034-Implementierung alle Kombinationen aus Capability-, Lease-, PR- und Ressourcen-Konflikten korrekt und effizient löst. Dafür sind der T034-Diff und dessen Regressionen abzuwarten.
- Der historische Supply-Receipt vom 16.08. ist keine aktuelle Supply-Zahl; ein neuer Lauf wäre eine eigene operatorgebundene Aktion.

## Entscheidung / No-Change-Begründung

Innerhalb dieses Audit-Tasks ist **kein Core-Patch zulässig oder sinnvoll**:

1. Der Gap ist reproduziert und damit kein bloßer Verdacht.
2. Der konkrete Reparaturvertrag existiert bereits als T034.
3. Genau die nötigen Core- und Testpfade sind einem fremden T034-Run exklusiv zugeordnet.
4. Ein paralleler Fix würde die Lease- und Single-Writer-Grenze verletzen und könnte zwei konkurrierende Wahrheiten erzeugen.
5. Ein zweiter Bureau-Folgetask wäre ein Duplikat des bereits passenden T034.

Acceptance `audit-03` ist daher als **begründeter No-change closeout** erfüllt: Das Audit repariert nicht außerhalb seines Scopes; die notwendige negative Regression ist bereits expliziter Bestandteil der Acceptance des bestehenden Reparaturtasks T034.

## Alternative Sinnachse

Wenn maximale Reparaturgeschwindigkeit höher gewichtet wird als Single-Writer-Sicherheit, könnte man versuchen, die stale wirkende T034-Lane zu übernehmen. Das wäre hier falsch: Ein alter Heartbeat ist keine Freigabeautorität, und die aktiven Leases verbieten genau diesen Schluss.

Wenn Systemintegrität und eindeutige Autorität höher gewichtet werden, ist der gewählte Pfad besser: Gap reproduzieren, vorhandenen T034-Vertrag als kanonische Reparaturspur bestätigen, keine fremde Lease antasten und das Audit als revisionsgebundene Evidenz abschließen.

## Risiko, Unsicherheit, Interpolation

Nutzen: Der Audit trennt einen echten semantischen Supply-Gap von einer grünen, aber unvollständigen Testlage und verhindert gleichzeitig Doppelarbeit.

Risiko: Bis T034 erfolgreich terminalisiert ist, kann `total_claimable_count` weiterhin gemeinsame Kapazität überschätzen. Das betrifft Planung/Diagnose des Supply-Floors; die normalen Claim-/Lease-Gates selbst werden dadurch nicht abgeschwächt.

Unsicherheit: **0,08** — Source, Reproducer, Tests, Runtime-Revision, Registry-Task und Live-Leases sind direkte Evidenz. Unsicher ist nur der aktuelle Fortschritt der fremden T034-Ausführung.

Interpolationsgrad: **0,06** — der Kerngap ist direkt demonstriert. Die erwartete Wirkung des noch nicht integrierten T034-Fixes wird nicht als erreicht vorweggenommen.

## Abschlussurteil

Der Claimable-Supply-Vertrag auf `6006336f…` ist in einem Punkt nachweislich unvollständig: Er misst individuelle Claimability, nicht gemeinsam ausführbare Packability. Der bestehende Task `BUREAU-CONTROL-PLANE-V3-T034` ist der richtige und bereits reservierte Reparaturpfad. Dieses Fallback-Audit soll deshalb **ohne konkurrierenden Core-Fix** mit diesem revisionsgebundenen Beleg terminalisiert werden.
