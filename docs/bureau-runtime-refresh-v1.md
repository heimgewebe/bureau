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
| expliziter Ziel- und Zeitrahmen | create-only Runtime-Refresh-Intent |
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
- explizite Autorisierung, Autorbezeichnung, Nonce, Erzeugungs- und Ablaufzeit;
- alle erforderlichen Grabowski-Ressourcen.

Standardgültigkeit: 900 Sekunden; maximal 3.600 Sekunden.

```bash
bureau-runtime-refresh prepare-intent \
  --candidate ~/.local/state/bureau/runtime-refresh/latest-observation.json \
  --authorized-by chatgpt \
  --authorization 'Exact target authorized by the Bureau runtime-refresh watch.'
```

Ein Intent erteilt allein keine Wirkungserlaubnis. Er beschreibt nur ein unveränderliches
Ziel und den autorisierten Zeitraum.

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

- Schema 1 oder 2; unbekannte Versionen bleiben fail-closed blockiert;
- jede exakte Ressourcenzeile vorhanden;
- identischer Owner;
- gültige Zeitordnung und mindestens zehn Minuten Restlaufzeit;
- gültiger Metadaten-SHA-256;
- private, reguläre, nicht verlinkte Datenbank im Besitz des aktuellen Nutzers.

Der Datenbankpfad ist im installierten CLI nicht überschreibbar. Testcode kann die
Prüffunktion mit einer synthetischen Datenbank aufrufen. Schema 2 ergänzt die
Grabowski-Datenbank um Terminalisierungs- und Authority-Tabellen, lässt aber die von
Bureau gelesene Lease-Projektion unverändert. Die beobachtete Schema-Version wird in
die Lease-Bindung und damit in das Ergebnisreceipt aufgenommen.

### 4. `apply`

`apply` prüft Intent, Ablaufzeit und Live-Leases. Danach wird GitHub erneut beobachtet.
Zielhash, `main`, Pflicht-CI sowie installierter Ausgangscommit und Manifest-Hash müssen
unverändert sein.

```bash
bureau-runtime-refresh apply \
  --intent <intent-path> \
  --lease-owner <grabowski-owner> \
  --lease-task-id <grabowski-task-id>
```

Der Runner:

1. legt für `target_sha256` ein create-only Startreceipt an;
2. klont ausschließlich `main` in den intentgebundenen Workspace;
3. verlangt `origin/main == intent.main_commit`;
4. checkt exakt diesen Commit detached aus und verlangt einen sauberen Status;
5. startet den bestehenden immutable Installer mit exakt gebundenem Prefix und Bin-Pfad;
6. liest Manifest, beide Launcher, Paketbaum, Registry-Snapshot,
   `bureau --json check` und `bureau --json runtime-identity` zurück;
7. schreibt ein create-only Ergebnisreceipt;
8. entfernt nur nach bewiesenem Erfolg den eigenen Workspace.

Der konventionelle Checkout wird nicht gelesen, aktualisiert, zurückgesetzt, gestasht,
gesäubert oder entfernt.

## Einmaligkeit und unklare Ergebnisse

Die Effektledger sind nach `target_sha256`, nicht nach Intent, adressiert. Mehrere Intents
für dasselbe exakte Ziel teilen daher einen Versuch. Ein vorhandenes terminales Ergebnis
wird zurückgegeben; ein Startreceipt ohne Ergebnis wird als
`unclear_existing_attempt` gemeldet.

Nach Beginn der Installerphase führt jeder Timeout, unerwartete Abbruch oder ungültige
Readback zu `unclear`. Der Workspace bleibt erhalten. Es gibt keinen Retry und keine
Selbstheilung. Ein neuer Intent für denselben Zielhash darf keinen zweiten Effekt starten.

Eine Fortsetzung ist erst nach einer gesonderten, operatorautorisierten Reconciliation
zulässig, die den realen Manifest-, Launcher-, Paket- und Snapshotzustand beweist. Dieser
Vertrag implementiert absichtlich keine automatische Reconciliation.

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
lokaler Prozess kann keine Wirkung allein durch Aufruf des Bureau-Runners erlangen, solange
die erforderlichen Live-Leases fehlen.

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
- manipulierte Kandidaten und abgelaufene Intents;
- fehlende, fremde, zu kurze oder öffentlich lesbare Lease-Datenbanken;
- sauberer detached Clone und Origin-Drift;
- intentübergreifende Deduplizierung desselben Zielhashes;
- unklarer Installer-Ausgang ohne Retry;
- Erhaltung eines fremden Dirty-Checkouts;
- beide Launcher, Rollbackkopien und vollständiger Runtime-Readback;
- echten synthetischen Installerlauf in temporären Git-Repositories.

Der Livebeweis muss nach Merge auf einem exakten neuen Bureau-`main`-Commit erfolgen: ein
Kandidat wird beobachtet, ein Intent erzeugt, reale Grabowski-Leases werden erworben und
genau ein Apply-Lauf deployt den Commit. Der zweite Lauf muss ohne Installerwirkung das
vorhandene Ergebnis beziehungsweise `already_current` zurückgeben.
