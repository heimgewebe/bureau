# Bureau Runtime Refresh v1

## Zweck

`bureau-runtime-refresh` schließt die Verzögerung zwischen einer verifizierten Änderung auf
`heimgewebe/bureau` `main` und dem installierten, unveränderlichen Bureau-Release samt
kanonischem read-only Registry-Snapshot. Der Pfad ersetzt weder GitHub-Merge-Autorität noch
Grabowski-Leases und verändert niemals den konventionellen Checkout
`~/repos/bureau`.

Der Installer veröffentlicht zwei gleich gebundene Launcher:

- `~/.local/bin/bureau`
- `~/.local/bin/bureau-runtime-refresh`

Beide prüfen vor dem Import den SHA-256 des Deployment-Manifests und laden ausschließlich
das im Manifest gebundene immutable Release.

## Wahrheits- und Autoritätsgrenzen

| Aussage oder Effekt | Autorität |
| --- | --- |
| aktueller `main`-Commit, zugehöriger Merge-PR, Pflicht-CI | GitHub |
| aktuell installierter Commit, Paket- und Snapshot-Hashes | Bureau-Deployment-Manifest und Runtime-Identity |
| aktuelle Runtime-Autoritäts-Task, Revision, Zustand und Verbrauch | autoritative Bureau-StateStore-TaskSpec; ein installierter Registry-Snapshot ist dafür niemals hinreichend |
| expliziter Ziel- und Zeitrahmen | create-only Runtime-Refresh-Intent |
| Wirkungserlaubnis für Runtime-Mutationen | typisierte `break_glass`-Freigabe, exakt an Zielhash und Bureau-Task gebunden |
| Konfliktfreiheit der Effektpfade | live gelesene Grabowski-Leases |
| eigentliche Installation | bestehender immutable Bureau-Installer |
| Erfolg | Receipt plus Manifest-, Launcher-, Paket-, Snapshot- und CLI-Readback |

Nicht behauptet werden:

- allgemeine oder dauerhafte Deploy-Autorität;
- automatische Merge-Autorität;
- Modellverständnis oder externe Identität des Intent-Autors;
- sichere Wiederholbarkeit nach einem unklaren Effekt;
- semantische Richtigkeit jeder Registry-Aussage;
- zukünftige Runtime-Gesundheit.

Eine Autoritäts-TaskSpec ist nur zulässig, wenn ihr strukturierter
`metadata.runtime_refresh_authority`-Vertrag eine vom tatsächlich ausführenden Runner
unterstützte Vertragsgeneration deklariert. Der Legacy-Modus `single-use-target-bound`
bleibt nur für Authorities ohne `source_precondition` gültig. Sobald eine
Source-Precondition verpflichtend ist, muss der Modus
`single-use-target-bound-source-precondition-v1` lauten. Ein älterer Runner, der diese
Generation nicht kennt, lehnt sie vor Intent- oder Runtime-Wirkung ab; ein neues
sicherheitskritisches Feld kann damit nicht mehr still unter einer alten
Vertragsgeneration ignoriert werden.

Unabhängig von der Generation muss der Vertrag `runtime_mutation`, `break_glass`, den
Write-Claim `component.bureau.runtime`, die erlaubten Zustände `ready` und `active`, die
Bindung `candidate.target_sha256` sowie das Verbot fremder Task- und historischer
Target-Substitution enthalten. Prosa, Acceptance-Text und ein alter Registry-Snapshot
ersetzen keinen dieser maschinenlesbaren Werte.

## Zustandsmaschine

### 1. `observe`

`observe` liest das aktive Deployment-Manifest und GitHub. Wenn `main` neuer ist, muss
exakt ein zugehöriger, gemergter PR vorliegen. Dessen Merge-Commit muss exakt `main`
entsprechen, die Base muss `main` sein und beide Pflichtchecks müssen den Zustand
`SUCCESS` haben:

- `validate (3.10)`
- `validate (3.12)`

`SKIPPED`, `NEUTRAL`, fehlende, laufende und fehlgeschlagene Checks sind nicht grün.
`main` wird nach der PR- und CI-Prüfung erneut gelesen. Eine Änderung während der
Beobachtung blockiert den Kandidaten.

Mögliche Zustände:

- `already_current`: Runtime und `main` stimmen überein;
- `candidate`: exakt gebundener, innerhalb des Freshness-SLO liegender Kandidat;
- `alert`: exakt gebundener Kandidat oberhalb des SLO;
- `blocked`: uneindeutiger Merge, CI-Problem, Drift oder fehlende Evidenz.

Die Beobachtung trägt `target_sha256` und `observation_sha256` und wird create-only im
State-Root archiviert. `latest-observation.json` ist nur eine atomare Projektion.

```bash
bureau-runtime-refresh observe
```

Standard-State-Root:

```text
~/.local/state/bureau/runtime-refresh
```

Standard-SLO: 5.400 Sekunden.

### 2. `prepare-intent`

Nur `candidate` und `alert` können einen Intent erzeugen. Der Intent bindet:

- Repository, Merge-PR, Merge- und Head-Commit;
- Pflichtchecks und Zielhash;
- derzeit installierten Commit und Manifest-Hash;
- Prefix, Bin-Verzeichnis, isolierten Workspace und State-Root;
- typisierte `break_glass`-Freigabe mit exaktem Zielhash, Bureau-Task, Autor, Quelle und Scope `runtime_mutation`;
- Nonce, Erzeugungs- und Ablaufzeit;
- alle erforderlichen Grabowski-Ressourcen.

Standardgültigkeit: 900 Sekunden; maximal 3.600 Sekunden.

```bash
bureau-runtime-refresh prepare-intent \
  --candidate ~/.local/state/bureau/runtime-refresh/latest-observation.json \
  --authorized-by chatgpt \
  --authorization 'Exact target authorized by the Bureau runtime-refresh watch.' \
  --break-glass \
  --approval-reference '<candidate.target_sha256>' \
  --approval-task-id '<exact Bureau task id>'
```

`prepare-intent` verweigert bereits die Erzeugung, wenn `break_glass`, Zielhash oder
Taskbindung fehlen oder nicht exakt zusammenpassen. Der Intent ist trotzdem keine alleinige
Wirkungserlaubnis: `apply` revalidiert die typisierte Freigabe und verlangt zusätzlich die
live Grabowski-Leases.

Unmittelbar vor dem create-only Intent liest `prepare-intent` außerdem
`approval_task_id` über die typisierte `StateStore.task_spec()`-API. Die aktuelle
TaskSpec muss die exakte Task-ID, eine positive Revision samt Digest, einen erlaubten
nichtterminalen Zustand und den oben beschriebenen Single-Use-Vertrag besitzen. Sie darf
weder bereits an einen anderen Target/Intent gebunden noch verbraucht oder durch einen
`runtime_closeout` geschlossen sein. Revision, TaskSpec-Digest und `contract_mode` sowie der exakte StateStore-Pfad werden
in den Intent aufgenommen. Fehlt die autoritative TaskSpec, ist sie
terminal/supersedet, semantisch falsch oder widerspricht sie dem Snapshot, endet der Aufruf
vor einer Intent-Datei. Die Registry-Projektion kann eine Task auffindbar machen, begründet
aber keine Runtime-Autorität.

### 3. Grabowski-Leases

Vor `apply` müssen alle im Intent genannten Ressourcen atomar mit demselben Owner geleast
sein. Typisch sind:

```text
path:~/.local/bin/bureau
path:~/.local/bin/bureau-runtime-refresh
path:~/.local/share/bureau
path:~/.local/state/bureau/runtime-refresh
path:~/.local/state/bureau/runtime-refresh/workspaces/<main-commit>
```

Die tatsächlichen Werte sind die kanonischen absoluten Pfade aus
`required_resource_keys` des Intents.

`apply` vertraut keiner frei übergebenen Ressourcenliste. Es liest unmittelbar vor der
Wirkung die private Grabowski-Datenbank
`~/.local/state/grabowski/resources.sqlite3` read-only und verlangt:

- genau eine Metadatenzeile `resource_lease_contract_version=1`;
- jede exakte Ressourcenzeile vorhanden;
- identischer Owner;
- gültige Zeitordnung und mindestens zehn Minuten Restlaufzeit;
- gültiger Metadaten-SHA-256;
- private, reguläre, nicht verlinkte Datenbank im Besitz des aktuellen Nutzers.

Der Lease-Vertrag ist absichtlich vom aggregierten Grabowski-Datenbankschema getrennt.
Neue Tabellen oder Indizes dürfen dessen `schema_version` erhöhen, ohne den Refresh zu
blockieren, solange die von Bureau gelesene `metadata`-/`leases`-Projektion weiterhin
explizit als Vertrag 1 publiziert wird. Fehlende, mehrdeutige, beschädigte oder unbekannte
Vertragsversionen blockieren vor dem ersten Zugriff auf Lease-Zeilen und liefern eine
begrenzte Recovery-Diagnose; Tabellenform allein begründet keine Kompatibilität.

Der Datenbankpfad ist im installierten CLI nicht überschreibbar. Testcode kann die
Prüffunktion mit einer synthetischen Datenbank aufrufen. Die beobachteten Aggregat- und
Lease-Vertragsversionen werden beide in die Lease-Bindung und damit in das
Ergebnisreceipt aufgenommen.

### 4. `apply`

`apply` prüft zuerst den Intent-Digest und revalidiert die gespeicherte
`runtime_mutation`-Entscheidung gegen die aktuelle Approval-Policy. Danach liest es erneut
die aktuelle autoritative TaskSpec und verlangt exakt die im Intent gespeicherte Revision
und denselben Digest. Bereits verbrauchte Autorität, terminaler/supersedeter Zustand,
Target-Bindung eines anderen Intents oder jede TaskSpec-Revision dazwischen blockiert vor
Observation, Attempt-Receipt und Runtime-Wirkung. Zusätzlich muss der im Intent gebundene
`contract_mode` der aktuellen Authority-Generation entsprechen. Dieser Generationstest gilt
nicht nur im frischen `apply`, sondern bereits bei der Target-Bindung und vor der erstmaligen
Consumption eines vorhandenen erfolgreichen Results. Ein historischer Intent ohne
`contract_mode` darf ausschließlich den Legacy-Modus ausführen; eine neuere
Source-Precondition-Generation blockiert damit auch Result-Reuse vor StateStore-Wirkung.
Erst danach werden Ablaufzeit und
Live-Leases geprüft. Anschließend wird GitHub erneut beobachtet. Zielhash, `main`,
Pflicht-CI sowie installierter Ausgangscommit und Manifest-Hash müssen unverändert sein.

```bash
bureau-runtime-refresh apply \
  --intent <intent-path> \
  --lease-owner <grabowski-owner> \
  --lease-task-id <grabowski-task-id>
```

Der Runner:

1. bindet die noch unverbrauchte TaskSpec über `StateStore.put_task_spec()` und
   Revision-CAS an Task-ID, ursprüngliche Autoritätsrevision/-digest,
   `target_sha256` und `intent_sha256`;
2. liest diese Bindung über `StateStore.task_spec()` unverändert zurück;
3. legt für `target_sha256` ein create-only Startreceipt an;
4. klont ausschließlich `main` in den intentgebundenen Workspace;
5. verlangt `origin/main == intent.main_commit`;
6. checkt exakt diesen Commit detached aus und verlangt einen sauberen Status;
7. startet den bestehenden immutable Installer mit exakt gebundenem Prefix, Bin-Pfad und Approval-Intent;
8. liest Manifest, alle Launcher, Paketbaum, Registry-Snapshot,
   `bureau --json check` und `bureau --json runtime-identity` zurück;
9. schreibt ein create-only Ergebnisreceipt;
10. bindet den erfolgreichen Verbrauch per TaskSpec-CAS dauerhaft an ursprüngliche
    Autoritätsrevision/-digest, Task-ID, Target, Intent und den finalen `result_sha256`
    und liest ihn aus der TaskSpec zurück;
11. entfernt nur nach bewiesenem Erfolg und Consumption-Readback den eigenen Workspace.

Die Target-Bindungs-CAS ist die letzte Autoritätsoperation vor Attempt- und Runtime-Wirkung.
Scheitert die Consumption-CAS nach einem physisch erfolgreichen, immutable gelesenen
Result, bleibt das Result kanonisch erhalten und der Workspace wird bewahrt. Ein erneuter
Aufruf darf dann ausschließlich dasselbe digestgültige Result gegen dieselbe Task-/Target-/
Intent-Bindung konsumieren; Installer, Source-Checkout und Runtime-Wirkung werden nicht
wiederholt. Ein abweichender oder manipulierter Result-/Consumption-Digest blockiert.

Der konventionelle Checkout wird nicht gelesen, aktualisiert, zurückgesetzt, gestasht,
gesäubert oder entfernt.

## Einmaligkeit und unklare Ergebnisse

Die Effektledger sind nach `target_sha256`, nicht nach Intent, adressiert. Mehrere vor der
Target-CAS erzeugte Intents derselben Task für dasselbe exakte Ziel teilen daher einen
Versuch. Ein vorhandenes terminales Ergebnis wird nur nach Digestprüfung und exakter
Task-/Target-/StateStore-Bindung zurückgegeben; ein Startreceipt ohne Ergebnis wird als
`unclear_existing_attempt` gemeldet. Erfolgsreplay ist wirkungsfrei. Dieselbe konsumierte
Single-Use-Autorität kann weder für einen späteren Target-Hash noch für eine fremde Task
verwendet werden.

Nach Beginn der Installerphase führt jeder Timeout, unerwartete Abbruch oder ungültige
Readback zu `unclear`. Der Workspace bleibt erhalten. Es gibt keinen Retry und keine
Selbstheilung. Ein neuer Intent für denselben Zielhash darf keinen zweiten Effekt starten.

Eine Fortsetzung ist erst nach einer gesonderten, operatorautorisierten Reconciliation
zulässig, die den realen Manifest-, Launcher-, Paket- und Snapshotzustand beweist. Dieser
Vertrag implementiert absichtlich keine automatische Reconciliation.

## Receiptgebundener Closeout ohne Bureau-Run

Ein Runtime-Refresh kann als Bootstrap stattfinden, bevor die neue Runtime einen normalen
Bureau-Claim bilden kann. Nur für eine solche bereits erfolgreiche Single-Use-Autorität
existiert `closeout-authority`. Der Befehl erzeugt keinen nachträglichen Claim, keine
Reservation und keinen synthetischen Run. Sobald irgendein Bureau-Run für die Task
existiert, verweigert er den No-Run-Pfad zugunsten des normalen Run-Closeouts.

```bash
bureau-runtime-refresh --state-root ~/.local/state/bureau/runtime-refresh \
  closeout-authority \
  --approval-task-id '<exact task id>' \
  --target-sha256 '<exact target digest>' \
  --intent-sha256 '<persisted intent digest>' \
  --result-sha256 '<terminal deployed result digest>'
```

Vor der einzigen TaskSpec-Wirkung werden konsistent geprüft:

- aktuelle autoritative TaskSpec und strukturierter Single-Use-Vertrag;
- exakte Task-, ursprüngliche Revision-/Digest-, Target-, Intent- und Consumption-Bindung;
- persistierter digestgültiger Intent und dessen damals gültige typisierte Break-Glass-Freigabe;
- kanonisches `deployed`-Result mit `effect_started=true` und exaktem Result-Digest;
- bounded, digestverifizierte Intent-/Result-Historie derselben `approval_task_id`: der
  angeforderte Effekt muss genau einmal vorkommen und **jede weitere** historische
  `effect_started=true`-Wirkung blockiert als `authority-closeout-historical-multi-use`;
- erneuter immutable Manifest-, Launcher-, Paket-, Registry-, `check`- und
  `runtime-identity`-Readback, bytegleich zur Result-Evidenz;
- `StateStore.integrity()` und vollständiger Event-/TaskSpec-Replay gegen die aktuelle Projektion;
- Abwesenheit jedes Bureau-Runs für diese Task;
- Freigabe aller exakt im Resultreceipt gebundenen Grabowski-Leases aus demselben
  Lease-Store und unverändertem Lease-Vertrag;
- vollständige Abdeckung der eingefrorenen TaskSpec-Acceptance durch den strukturierten
  `metadata.runtime_refresh_authority.no_run_closeout_acceptance`-Vertrag. Dieser muss
  exakt dieselben Criterion-IDs führen. Jedes Kriterium benennt unter dem spezialisierten
  Verifier `runtime-refresh-no-run-evidence-v1` die dafür erforderlichen bereits geprüften
  Evidenzklassen. Ein erfolgreicher Deploy ist daher kein Ersatz für ein zusätzliches
  fachliches Sicherheitskriterium.

Bei einer Authority mit `source_precondition` muss mindestens ein Acceptance-Kriterium
explizit die Evidenzklasse `source-precondition` verlangen. Außerdem müssen historischer
Intent und Result genau diese Source-Precondition samt Observation-/Ancestry-/Runtime-
Identity-Evidenz tatsächlich gebunden haben. Fehlt das im historischen Runnervertrag,
blockiert der Closeout als `authority-closeout-source-precondition-unproven`; die später
beobachtete physische Runtime-Konvergenz wird nicht rückwirkend zur Gate-Evidenz erklärt.

Nur dann setzt ein einziger `StateStore.put_task_spec()`-CAS den Zustand auf `verified` und
persistiert `metadata.runtime_closeout` mit Task-ID, ursprünglicher Autoritätsrevision und
-digest, Target, Intent, Runtime-Result, Source-Commit, Manifest-/Readback- sowie
Lease-Binding-/Release-Digests. Neue Closeouts enthalten zusätzlich eine digestgebundene
`acceptance_evidence`-Kapsel mit TaskSpec-, Acceptance-Vertrags-, Result-, Readback-,
Lease-, Effekt-Historien-, StateStore- und Run-/No-Run-Bindung. Der anschließende
TaskSpec-Readback und ein erneuter
StateStore-Replay müssen passen. Ein identischer Replay ist wirkungsfrei; fremde, fehlende,
nicht deployte, driftende oder manipulierte Evidenz blockiert. Terminalität wird nie aus
Notizen, Goal- oder Acceptance-Prosa abgeleitet, und es gibt weder Direct-SQL auf Bureau
StateStore noch Queue-/Claim-/Dispatch-Wirkung.

Der normale `closeout-authority` bleibt bei historischer Mehrfachnutzung bewusst
fail-closed. Ein einzelner erfolgreicher Effekt darf eine deklarierte Single-Use-Authority
niemals als `verified` erscheinen lassen, wenn weitere `effect_started=true`-Effekte zur
selben Task existieren.

Für genau diese belegte Vertragsverletzung existiert getrennt
`closeout-authority-incident`. Der Pfad akzeptiert nur einen bereits `deployed`en,
effektgestarteten Result-Beleg, verlangt mindestens einen weiteren abweichenden historischen
Effekt und führt dieselben harten Intent-/Result-, immutable Readback-, StateStore-, Run-
und Lease-Release-Prüfungen wie der normale Closeout aus. Er startet keinen Runtime-Effekt
und erzeugt keinen Claim oder Fake-Run. Sein einziger TaskSpec-CAS setzt den Lifecycle auf
`superseded` und persistiert `metadata.runtime_incident_closeout` mit dem Digest der
vollständigen Effekt-Historie sowie Effekt- und Konfliktanzahl. Die Receipt-Aussage ist
explizit **nicht** `legitimate_single_use_verification` und legitimiert keinen historischen
Effekt rückwirkend.

```bash
bureau-runtime-refresh --state-root ~/.local/state/bureau/runtime-refresh \
  closeout-authority-incident \
  --approval-task-id '<exact task id>' \
  --target-sha256 '<authoritatively consumed target digest>' \
  --intent-sha256 '<authoritatively consumed intent digest>' \
  --result-sha256 '<authoritatively consumed deployed result digest>'
```

Der historische Browser-Control-Bootstrap
`BUREAU-CONTROL-PLANE-V3-FB-RUNTIME-REFRESH-BROWSER-CONTROL-RESOURCE-20260811` ist der
Negativfall: Mehrere unterschiedliche Targets wurden bereits mit Wirkung ausgeführt. Ein
normaler Closeout bleibt deshalb verboten; nur die Incident-/Supersession-Semantik darf den
fehlerhaften Authority-Lifecycle terminalisieren. `BUREAU-TRUTH-MODEL-V2-T029` ist bereits
mit seinem älteren `runtime_closeout` terminal und dient ausschließlich als Präzedenzfall,
nicht als erneut zu schließende Autorität.

## Historische Provenienzgrenze `affe99f…`

Der am 2026-08-11 bereits ausgeführte Refresh auf
`affe99f27d482c75ca4ae61735e7d2d69f911c8d` bleibt physisch durch seinen immutable
Readback integritätsgültig. Seine Autoritätsprovenienz ist jedoch fehlerhaft: als
`approval_task_id` wurde die im installierten Snapshot noch plausibel wirkende, aber
autoritätlich stale Task
`BUREAU-TRUTH-MODEL-V2-FB-TASKSPEC-SEED-DRIFT-20260809` verwendet. Die relevante
Single-Use-Nachfolgersemantik lag in `BUREAU-TRUTH-MODEL-V2-T029`; zusätzlich existierte
`BUREAU-CONTROL-PLANE-V3-FB-RUNTIME-REFRESH-AUTHORITY-20260810`. Das terminale
Runtime-Result ist an
`dea2fa1d51a865b1884ad652c43fd6aaf98f10c00c81df6c5ca2cc576088292b` gebunden.

Diese Feststellung autorisiert weder Deployment-Replay noch rückwirkende Gültigkeit der
stale Task-ID, reaktiviert keine terminale Task und schreibt keinen historischen Status
künstlich um. Sie ist die dokumentierte Provenienzgrenze, die der neue StateStore-Preflight
für künftige Refreshes vor jeder Wirkung schließt.

## Automationsmodell

Die regelmäßige Automation gehört zur Operator-Ebene, nicht zu einem Bureau-systemd-Timer:

1. stündlich `observe` ausführen;
2. bei `already_current` oder `blocked` ohne Wirkung enden;
3. bei `candidate` oder `alert` einen kurzlebigen, exakt gebundenen Intent erzeugen;
4. die im Intent genannten Ressourcen über Grabowski erwerben;
5. `apply` als einen Grabowski-Durable-Task ausführen;
6. Ergebnis und Readback prüfen;
7. Leases freigeben;
8. bei `unclear` benachrichtigen und nicht wiederholen.

Damit bleibt die eigentliche Mutationsautorität bei Grabowski. Ein Timer oder fremder
lokaler Prozess kann keine Wirkung allein durch Aufruf des Bureau-Runners erlangen: Sowohl
die exakte `break_glass`-Freigabe als auch die erforderlichen Live-Leases müssen vorliegen.
Auch der Installer selbst ist fail-closed: Direkte Aufrufe ohne Approval-Intent oder mit
einem Intent für einen anderen Source-Commit enden vor der ersten Dateiwirkung.

## Inspektion

```bash
bureau-runtime-refresh status
```

`status` zeigt den installierten Commit, das aktuelle Manifest, die letzte Beobachtung und
höchstens 20 terminale oder ungeklärte Zielversuche. Der Befehl verleiht keine
Deploy-Autorität.

## Test- und Livebeweis

Die fokussierten Tests decken unter anderem ab:

- aktueller Stand und exakter Merge-PR;
- fehlgeschlagene, fehlende und übersprungene Pflicht-CI;
- Drift von `main` während der Beobachtung;
- manipulierte Kandidaten, abgelaufene Intents und unzureichende Runtime-Freigaben;
- stale Registry-Snapshot gegen neuere/supersedete autoritative StateStore-TaskSpec;
- TaskSpec-Revision zwischen Prepare und Apply sowie terminale, fremde und falsch
  targetgebundene Autorität vor Wirkung;
- revisions-/target-/resultgebundene Single-Use-Consumption, Post-Binding-TaskSpec-CAS,
  wirkungsfreien Replay und manipulierte Result-/Consumption-Readbacks;
- fehlende, fremde, zu kurze oder öffentlich lesbare Lease-Datenbanken;
- sauberer detached Clone und Origin-Drift;
- intentübergreifende Deduplizierung desselben Zielhashes;
- unklarer Installer-Ausgang ohne Retry;
- Erhaltung eines fremden Dirty-Checkouts;
- beide Launcher, Rollbackkopien und vollständiger Runtime-Readback;
- direkten Installeraufruf ohne Approval-Intent ohne Dateiwirkung;
- Browser-Control- und T029-artigen No-Run-Closeout, historische Mehrfachnutzung einer
  deklarierten Single-Use-Autorität, fehlende Lease-Freigabe, falsche Task/Target-Bindung,
  manipuliertes Result und widersprüchlichen terminalen Zustand;
- echten synthetischen Installerlauf mit exakt source-gebundener `break_glass`-Freigabe in temporären Git-Repositories.

Der Livebeweis muss nach Merge auf einem exakten neuen Bureau-`main`-Commit erfolgen: ein
Kandidat wird beobachtet, ein Intent erzeugt, reale Grabowski-Leases werden erworben und
genau ein Apply-Lauf deployt den Commit. Der zweite Lauf muss ohne Installerwirkung das
vorhandene Ergebnis beziehungsweise `already_current` zurückgeben.
