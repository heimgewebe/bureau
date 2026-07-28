# Operator-Plattform Masterplan v2

## Status und Geltungsgrenze

- Stand: 2026-07-28
- Typ: live-validierter Anschlussplan
- Bureau-Basis: `3b3bd37ac3f41c42f8d283b2b473e5ff96f5b391`
- Zielinitiative: `OPERATOR-ECOSYSTEM-REDUNDANCY-V1`
- Primärziel: Vibe-Coding mit ChatGPT über Secure MCP Tunnel und Grabowski auf dem Heim-PC
- Nebenprodukte: Wissensvisualisierung sowie Audiowiedergabe, Aufnahme und Musikproduktion
- Wirkung: dieser Plan ordnet bestehende Arbeit; er erzeugt keine Queue-, Merge-, Deployment- oder Abschaltautorität

Der Plan ersetzt keine Live-Wahrheit. Git, GitHub, CI, Bureau Registry, Runtime-Receipts und aktuelle Hardwarebeobachtung bleiben autoritativ. Er darf nicht als zweite Task- oder Systemwahrheit verwendet werden.

## Dialektisches Urteil

### These

Der Kern ist praktisch wertvoll:

```text
Alex
  -> ChatGPT plant und entscheidet
  -> Secure MCP Tunnel transportiert
  -> Grabowski führt lokal aus
  -> Git, GitHub, CI und Live-Readback belegen das Ergebnis
```

Dieser Pfad ermöglicht vollständige Reparatur-, Entwicklungs-, Review-, Dienst- und Diagnosezyklen ohne manuelles Kopieren von Shell-Befehlen.

### Gegenthese

Die Plattform beschäftigt sich zu stark mit ihrer eigenen Koordination:

- Bureau enthält auf dem geprüften Remote-`main` 787 Tasks und 69 Initiativen.
- Eine bewusst breite, daher überinklusive Schlagwortgruppierung ordnet 525 der 787 Tasks dem Operator-, Grabowski-, Bureau-, RepoGround-, Chronik-, Konvergenz- oder Maschinenlesbarkeitsbereich zu; darunter sind 167 noch `planned`. Das ist ein Belastungsindikator, keine kanonische Taxonomie.
- Die aktuelle Arbeitsprojektion umfasst 200 Einträge, davon 180 blockierend, 14 unbekannt und nur 6 aktiv.
- Auf dem Benutzer-Manager existieren 46 Timer.
- Mehrere Beobachter melden wiederholt Drift, erzeugen aber keine direkte Nutzerwirkung.
- Leitstand ist als allgemeine Anzeige etabliert, zeigt jedoch veraltete Bureau-, Checkout- und Systemkatalog-Snapshots.

### Synthese

Nicht die bloße Zahl der Repositories ist der Hauptfehler. Mehrere Komponenten sind inzwischen bewusst schmal, read-only oder sicherheitsgebunden. Der aktuelle Engpass ist:

1. zu viel dauerhaft aktivierte Beobachtung und Taktung;
2. zu viele nichtterminalisierte Arbeitsreste;
3. veraltete Identitäten und Bindungen;
4. zu wenig Messung realer Nutzerergebnisse;
5. zu oft wird ein großer Koordinationspfad auch für kleine Aufgaben vorbereitet.

Die Strategie lautet deshalb:

> Aktivierungen, Kadenzen, Arbeitsreste und Wahrheitsbindungen reduzieren; sicherheitsrelevante Fähigkeiten erhalten; drei konkrete Nutzerpfade beweisen.

## Live-validierte Realität

### Kernruntime

| Bereich | Livebefund | Einordnung |
|---|---|---|
| Grabowski | gesund, vollständig deployt, Audit gültig und schreibbar, Kill-Switch aus | Kern bestätigt |
| Tunnel | aktiv; während des Audits wurden echte MCP-Requests mit HTTP 200 übertragen | aktuelle Ende-zu-Ende-Funktion bestätigt |
| Grabowski-Release | deployter Head `476c06bad629722b03768b0684d25897405b7cfd` | hinter aktuellem Quellstand, aber dessen Vorfahr |
| Grabowski-Prozess | nach rund 19 Stunden etwa 6,7 GiB Speicher und mehr als 7 CPU-Stunden | auffällig; Leak noch nicht bewiesen |
| fehlgeschlagene User-Units | Browser-Worker, Tunnel-Watchdog, Systemkatalog-Drift-Watch | Schichten sind nicht vollständig gesund |

Die pauschale Aussage „der Transport ist derzeit instabil“ ist nicht belegt. Belegt ist eine aktuell funktionierende Kette mit mehreren fehlerhaften oder veralteten Nebenbeobachtern.

### Bureau

| Messwert | Livebefund |
|---|---:|
| Tasks | 787 |
| verified | 476 |
| planned | 260 |
| ready | 11 |
| blocked | 26 |
| Initiativen | 69 |
| Queue-Einträge | 80 |
| Registry-Warnungen | 90 |
| Doctor | `healthy=false`, Datenbankintegrität aber gültig |
| breite Repository-Lease-Warnungen | 11 |

Bureau verhindert reale Kollisionen und besitzt gute fail-closed Grenzen. Gleichzeitig ist sein Bestand zu groß, die Schließungswahrheit lückenhaft und die operative Oberfläche teilweise durch historische Arbeit belastet.

### Arbeitskopien und Lebenszyklen

Die aktuelle Projektion über Bureau, Audio, Systemkatalog und Grabowski meldet:

- 200 projizierte Arbeitsobjekte im begrenzten Snapshot;
- 180 blockierend;
- viele Dirty-Worktrees ohne aktuelle Lease- oder Binding-Autorität;
- verwaiste oder identitätsgedriftete Checkout-Bindungen;
- mehrere als aktiv markierte Bindungen, deren physischer Checkout fehlt oder einen anderen Head besitzt.

Diese Werte belegen keine Löschfreigabe. Sie belegen jedoch, dass Lebenszykluspflege ein reales Produktivitätsproblem ist.

### Systemkatalog

Der aktuelle Quellkatalog enthält 37 Systembindungen, nicht 32. Er kennt bereits:

- Lebenszykluszustände;
- Resilienz- und Kritikalitätsklassen;
- Rollen und Quellbindungen.

Die frühere Empfehlung, solche Felder erst einzuführen, ist daher teilweise falsifiziert. Das reale Problem ist die Pflege:

- `hausKI-audio` ist weiterhin gebunden;
- `audio` fehlt als aktuelles System;
- 21 von 37 Resilienzklassifikationen stehen auf `unknown`;
- die im Leitstand sichtbare Ökosystemkarte war beim Audit mehr als sieben Tage alt.

### Audio

`heimgewebe/audio` ist das aktuelle Repository. `hausKI-audio` ist nur noch ein dokumentierter Spenderbestand und darf nicht als Produktquelle behandelt werden.

Das aktuelle Audio-Repo besitzt bereits:

- einen read-only Audio-Doctor;
- Profilplanung;
- physische Fakten- und Messverträge;
- Labor-Gates;
- Referenzsignale und Pegelanalyse;
- 102 grüne Tests;
- eine gemergte, verwaltete Buckelwal-Live-Voice v1;
- Fail-closed Schutz vor ungebundenem Echtzeitbetrieb.

Es ist damit deutlich reifer als ein bloßes Wiedergabe-MVP.

Live fehlen jedoch MOTU M2 und Roland FP-30X in ALSA, PipeWire beziehungsweise MIDI. Die konfigurierten Defaults verweisen teilweise auf nicht vorhandene Geräte. Zusätzlich bestehen zwei konkrete Vertragsfehler:

1. `audio-doctor` meldete `roland_fp_30x=true`, obwohl der MIDI-spezifische Whale-Doctor und die Live-Gerätelisten keinen Roland-Port fanden. Ursache ist wahrscheinlich die Vermischung konfigurierter Default-Namen mit physisch beobachteter Hardware.
2. Die Architektur dokumentiert das Profil `production`, der aktuelle Profilkatalog kennt es aber nicht. `audio-plan production` endet daher mit `unknown profile`.

Außerdem ist der aktuelle globale PipeWire-Quantumwert 1024 Frames. Das ist für komfortable Wiedergabe möglich, aber für das Live-Instrument und Software-Piano nicht als niedrige Latenz belegt.

### RepoGround

Der Audio-Bundle war exakt frisch, kanonisch und technisch gesund. Eine breite natürliche fachliche Abfrage lieferte trotzdem null Treffer und keine Zitate.

Daraus folgt:

- RepoGround bleibt für große, unbekannte und belegkritische Aufgaben sinnvoll.
- Freshness und Artefaktintegrität allein garantieren keine gute Retrieval-Antwort.
- Der Operator muss zunächst einen kleinen Retrieval-Probe durchführen.
- Bei Nulltreffer wird direkt auf Git, Quelltext und gezielte Suche zurückgefallen.
- RepoGround darf kein obligatorischer Standardweg für kleine Änderungen sein.

Historische Kontextreduktionsmessungen bleiben nützlich, wurden in diesem Audit aber nicht neu reproduziert.

### Wissen und Visualisierung

Schauwerk ist kein bloßer Rohbaustein mehr. Es besitzt:

- deterministische Renderpfade;
- HTML-, SVG-, Miro-, Mermaid-, JSON-Canvas- und Dokumentoberflächen;
- publizierte Pilot- und Companion-Arbeit;
- einen aktiven lokalen Companion.

Leitstand ist bereits als einzige allgemeine Operatoranzeige festgelegt. Diese frühere Empfehlung ist umgesetzt. Die Betriebsabnahme ist jedoch unvollständig:

- Bureau- und Checkout-Snapshots waren ungefähr zwei Tage alt;
- die Ökosystemkarte war älter als die erlaubte Frische;
- `/health` meldete deshalb `warn`.

Technische Nutzbarkeit ist belegt. Wiederkehrender menschlicher Nutzen ist nicht ausreichend gemessen.

### Weitere Komponenten

- Reposkop ist bereits als read-only, zielgebundene Kohärenzprojektion verengt. Eine erneute „Integration“ wäre Doppelarbeit.
- Der Konvergenzregelkreis ist bereits ein kleines Evidenz- und Begriffsmodell ohne eigene Runtime- oder Task-Autorität. Er muss nicht zu einem neuen Organ umgebaut werden.
- Plexer hat einen belegten Relay-/Gateway-Nutzen. Eine Eingliederung in Chronik ist ohne neue Konsumentenanalyse nicht gerechtfertigt.
- Sichter bleibt in wesentlichen Teilen MVP; autonome LLM-Review-Wahrheit ist nicht belegt.
- semantAH besitzt inzwischen reale Rust-Index- und API-Arbeit, enthält aber weiterhin Stub- und Platzhalterpfade. Ein bestehender Bureau-Task sieht Stilllegung und Archivierung vor. Die Entscheidung muss am Wissens-Goldpfad geprüft werden, nicht an Repo-Existenz.

## Zielarchitektur

### Immer aktiver Kern

```text
ChatGPT
  -> Secure MCP Tunnel
  -> Grabowski Core
  -> Git / GitHub / CI / Live-Runtime
```

Der Standardpfad darf höchstens diese fünf logischen Rollen benötigen:

1. menschliches Ziel;
2. ChatGPT-Entscheidung;
3. Tunnel;
4. Grabowski;
5. Zielsystem plus Primärbeleg.

### Bedingt zugeschaltete Schichten

| Schicht | Zuschalten, wenn | Nicht zuschalten, wenn |
|---|---|---|
| RepoGround | großes oder unbekanntes Repo; Quellbelege; Übergabe | bekannte Datei; Nulltrefferprobe; kleine lokale Änderung |
| Bureau | jeder mutierende Lauf: Reconciliation, Task/Claim und run-spezifischer Worktree; zusätzlich Queue und Abhängigkeiten bei Parallelität | reine Read-only-Analyse ohne Mutation |
| Konvergenzbeleg | Deployment, externer Effekt oder R2/R3-Behauptung | reine lokale Planung oder Quelländerung |
| Chronik/Plexer | historisch wertvolles Ereignis oder Relay | Routinekommandos und jede Statusabfrage |
| Leitstand | menschliche Gesamtübersicht | autoritative Mutation oder Taskentscheidung |
| Schauwerk | Visualisierung, Publikation, Wissenskarte | allgemeiner Status oder Taskkoordination |
| Audio | Wiedergabe, Aufnahme, Produktion, Instrument | allgemeine Operatorpflege |

### Arbeitsmodi

#### Direkt

Für kleine, begrenzte Änderungen:

```text
reconcile -> Bureau Task/Claim -> run-spezifischer Worktree -> live lesen -> ändern -> testen -> diff/readback -> Ergebnis
```

Der normale Bureau-Claim und der aufgezeichnete run-spezifische Worktree bleiben auch hier Pflicht. RepoGround entfällt, sofern keine Kontextlücke vorliegt; zusätzliche Queue- und Abhängigkeitszeremonie entfällt, sofern keine Parallelität besteht.

#### Grounded

Für große oder fremde Repositories:

```text
RepoGround Retrieval-Probe -> Kontextpaket oder direkter Git-Fallback -> Änderung -> Tests
```

Ein Nulltreffer ist ein Routing-Signal, kein Beweis fehlender Information.

#### Koordiniert

Für parallele oder langlebige Arbeit:

```text
Bureau Task/Claim -> isolierter Checkout -> Umsetzung -> Review/CI -> Closeout
```

#### Wirkungskritisch

Für Löschung, Deployment, Dienste, Netzwerk, Secrets und externe Publikation:

```text
Preflight -> Recovery -> Wirkung -> Live-Readback -> Receipt -> Konvergenzbewertung
```

#### Wissen

```text
Quellen -> belegtes Modell -> Schauwerk -> rückverfolgbare Darstellung
```

#### Audio

```text
Live-Hardware -> physische Fakten -> Profil -> Routing/Laborprobe -> Wiedergabe oder Aufnahme
```

## Masterplan

### Welle 0: Wahrheitsfehler schließen

#### W0.1 Audio korrekt binden

- Systemkatalog von `hausKI-audio` auf `audio` umstellen.
- Historischen Spenderstatus von `hausKI-audio` ausdrücklich markieren.
- Bureau-Task `OPERATOR-ECOSYSTEM-REDUNDANCY-V1-T041` nicht unverändert ausführen; gegen `audio` neu bewerten, supersedieren oder eng migrieren.
- RepoGround-, Leitstand- und sonstige Quellbindungen auf den aktuellen Repo-Namen prüfen.

Abnahme:

- genau eine aktuelle Audio-Produktbindung;
- historische Referenzen bleiben als Historie erkennbar;
- keine Wartungsarbeit läuft versehentlich gegen `hausKI-audio`.

#### W0.2 Audio-Livewahrheit reparieren

- Hardwareerkennung trennt physisch beobachtete Geräte, konfigurierte Defaults und gewünschte Sollgeräte.
- `roland_fp_30x` wird nur aus aktueller ALSA-/MIDI-Beobachtung wahr.
- `production` wird entweder als reales Profil implementiert oder aus der aktuellen Profilbehauptung entfernt.
- `just check` bleibt grün und erhält Regressionstests für abwesende, aber als Default gespeicherte Geräte.

Abnahme:

- null False-Positive-Readiness bei abgezogener Hardware;
- `audio-doctor`, Whale-Doctor und Profilplaner widersprechen sich nicht;
- jeder dokumentierte Profilname ist ausführbar oder ausdrücklich „geplant“.

#### W0.3 Cockpit- und Beobachterfrische wiederherstellen

- Ursache der veralteten Leitstand-Bureau- und Checkout-Snapshots beheben.
- Systemkatalog-Drift-Watch nicht weiter stündlich denselben schreibblockierten Zustand abarbeiten lassen.
- Fehlgeschlagenen Tunnel-Watchdog entweder reparieren oder als nicht benötigte, deaktivierte Altgeneration einordnen.
- Beobachter müssen nach drei identischen, nicht behebbaren Zyklen deduplizieren und auf ein bestehendes Arbeitsobjekt verweisen.

Abnahme:

- Leitstand `/health` ist grün oder nennt einen tatsächlich neuen Blocker;
- kein identischer Observer-Fehler verbraucht stündlich CPU ohne Informationsgewinn;
- keine zweite Runtime-Wahrheit entsteht.

### Welle 1: Kernpfad messbar zuverlässig machen

#### W1.1 Ende-zu-Ende-Bereitschaft

Ein gemeinsamer Read-only-Status unterscheidet mindestens:

- `READY`;
- `LOCAL_RUNTIME_DOWN`;
- `MCP_LOOP_BLOCKED`;
- `TUNNEL_DOWN`;
- `CONTROL_PLANE_STALE`;
- `CONNECTOR_SCHEMA_STALE`;
- `AUTHORIZATION_MISSING`.

Der Status bindet aktuelle Runtime-, Tunnel- und Connectoridentität, ohne ChatGPT-UI-Zustand zu erfinden.

#### W1.2 Ressourcenbudget

- Grabowski-Speicher über mindestens sieben Tage als Zeitreihe messen.
- 6,7 GiB Momentaufnahme nicht vorschnell als Leak klassifizieren.
- Wachstum, Peak, Garbage-Collection-Verhalten, Auditgröße und aktive Sessions korrelieren.
- Warn- und harte Grenzen erst aus der Messung ableiten.
- Langläufer konsequent als persistente Jobs ausführen.

#### W1.3 Release-Identität

- Quelle, deployter Release, veröffentlichter Connector-Vertrag und aktuell geladener Werkzeugkatalog müssen gemeinsam lesbar sein.
- Ein älterer, aber gültiger Release ist `behind`, nicht automatisch `unhealthy`.
- Nicht rückwärtskompatible Tooländerungen dürfen nicht ohne Connector-Cutover veröffentlicht werden.

### Welle 2: Vibe-Coding als Hauptprodukt beweisen

Drei Goldfälle werden wiederholbar ausgeführt:

1. kleine Änderung in bekanntem Repository im Direktmodus;
2. architekturübergreifender Bugfix mit RepoGround-Probe und direktem Fallback;
3. parallele, langlebige PR-Arbeit mit Bureau-Koordination.

Für jeden Goldfall werden gemessen:

- Zeit bis zum ersten sinnvollen Diff;
- Zeit bis zum verifizierten Ergebnis;
- Zahl manueller Nutzereingriffe außerhalb von ChatGPT;
- Tool- und Schichtwechsel;
- Wiederholungen wegen falschem Kontext;
- 502-, Timeout- oder Ambiguitätsereignisse;
- erzeugte und zurückgelassene Worktrees, Jobs und Leases.

Erfolg gilt nur, wenn mindestens zwei Wiederholungen pro Fall ohne versteckte manuelle Shell-Reparatur gelingen.

### Welle 3: Wissensvisualisierung als Produktpfad beweisen

Keine neue Wissensplattform bauen. Bestehende Schichten verwenden:

```text
Git / Dokumente / Vault
  -> direkte Quellen oder RepoGround
  -> kleines belegtes Zwischenmodell
  -> Schauwerk
  -> HTML, SVG, Miro oder JSON Canvas
```

Goldfälle:

1. Architekturkarte eines Repositories;
2. Entscheidungs- und Ereignisverlauf eines Projekts;
3. thematische Karte über mehrere Dokumentquellen.

Messung:

- Zeit bis zur verwendbaren Darstellung;
- Anteil rückverfolgbarer Aussagen;
- Zahl manueller Zwischenschritte;
- reale Öffnung oder Weiterverwendung durch den Nutzer;
- benötigte Komponenten.

semantAH bleibt nur dann im Produktpfad, wenn mindestens ein Goldfall ohne semantische Suche nachweislich nicht sinnvoll lösbar ist und semantAH den Pfad messbar verbessert.

### Welle 4: Audio als vier klare Produkte liefern

#### A. Hören

- MOTU oder bewusst gewählte Alternative frisch erkennen;
- Zielsenke, Lautstärke, Rate und Mischmodus herstellen;
- Qobuz-/Desktop-Pfad verifizieren;
- Bit-perfect nur nach Messbeleg behaupten.

#### B. Sprache aufnehmen

- MOTU, RØDE, 48 V, Gain und sicheren Abhörweg physisch bestätigen;
- Pegelprobe;
- Aufnahme mit exaktem Dateipfad;
- kein Recording-Ready ohne aktuelle Hardware.

#### C. Piano und Walgesang live

- Roland-MIDI frisch erkennen;
- 128-Frame- oder gemessenen Alternativpfad herstellen;
- Whale-Voice nur nach `ready=true` starten;
- XRuns und Ausgangspegel messen;
- Session begrenzen und stoppbar halten.

#### D. Produzieren

- reales `production`-Profil;
- Ardour-Template, Routing, Projektpfad und Plugininventar;
- reproduzierbares Öffnen, Aufnehmen, Speichern und Wiederöffnen;
- kein Anspruch auf vollständige Produktion allein aus installierter Software.

### Welle 5: Selbstbeschäftigung begrenzen

#### W5.1 Timerbudget

Jeder Timer benötigt:

- einen benannten Konsumenten;
- eine Frischeanforderung;
- eine messbare Auswirkung bei Ausfall;
- Deduplizierung;
- eine Ablauf- oder Reviewregel;
- eine kanonische Quellbindung.

Timer ohne diese Angaben werden nicht sofort gelöscht. Sie laufen zunächst in einer begrenzten Shadow-/Beobachtungsphase oder werden durch einen ereignisgebundenen Pfad ersetzt.

Ziel:

- kein neuer Timer ohne Verbraucher und Ablaufregel;
- identische Fehler nicht häufiger als einmal pro relevanter Zustandsänderung;
- allgemeine Selbstreflexion, Optimierung und Konvergenz nicht als mehrere unabhängige Takte ausführen.

#### W5.2 Arbeitsrestebudget

- Dirty fremde Arbeit bleibt geschützt.
- Verwaiste Bindungen werden gelesen und klassifiziert, nicht blind gelöscht.
- Terminale, gemergte und belegte Checkouts werden nach einer einzigen kanonischen Retention-Policy archiviert oder entfernt.
- Neue Arbeit darf nur einen Checkout-Lebenszyklusvertrag erzeugen.

Zielwerte:

- blockierende Projektion von 180 zunächst halbieren;
- null neue ungebundene Dirty-Worktrees aus Operatorläufen;
- alle aktiven Bindungen besitzen einen physisch passenden Checkout oder einen expliziten `inconclusive`-Status.

#### W5.3 Bureau-Bestand verdichten

Bureau bleibt Koordinator, wird aber nicht zum Produktziel.

- Neue Meta-Tasks nur bei Sicherheitsverletzung, Goldpfadfehler, messbarer Regression oder nachweisbarer Oberflächenreduktion.
- Verifizierte Tasks ohne maschinenlesbaren Closeout werden nach bestehendem Truth-Modell nachgezogen.
- Ähnliche geplante Tasks werden auf Initiativebene gruppiert und priorisiert, ohne historische Taskidentität umzuschreiben.
- Die inzwischen 64 Tasks von `OPERATOR-ECOSYSTEM-REDUNDANCY-V1` werden auf Nutzerpfad, notwendige Plattformhygiene oder später/archivierbar klassifiziert.

## Portfolioentscheidung

### Behalten und priorisieren

- ChatGPT als Planungs- und Entscheidungsinstanz;
- Secure MCP Tunnel;
- Grabowski als lokale Ausführungs- und Sicherheitsgrenze;
- Git, GitHub und CI;
- `audio`;
- Schauwerk;
- Leitstand als allgemeine Anzeige;
- Systemkatalog als stabile Semantik;
- Bureau für echte Koordination.

### Bedingt verwenden

- RepoGround nach positiver Retrieval-Probe;
- Chronik für hochwertige historische Ereignisse;
- Plexer für belegte Relay- und Gatewayfälle;
- Reposkop für read-only Checkout-Kohärenz;
- Konvergenzregelkreis für Wirkungsbehauptungen;
- Vibe-Lab nur für entscheidungsgebundene Experimente.

### Reparieren oder neu bewerten

- Audio-Hardwarewahrheit und Produktionsprofil;
- Systemkatalog-Audiobindung;
- Leitstand-Snapshot-Pipeline;
- wiederholt fehlgeschlagene Beobachter;
- Grabowski-Ressourcenentwicklung;
- Bureau-Schließungs- und Restearbeit.

### Nicht als aktuellen Produktkern behandeln

- `hausKI-audio`;
- Sichter als autonome Reviewinstanz;
- semantAH ohne Goldfallbeleg;
- historische Cabinet-/Heimlern-/Leitwerk-Rollen;
- allgemeine Meta-Automationen ohne externen Konsumenten.

## Anschluss an bestehende Bureau-Tasks

Der Masterplan eröffnet keine zweite Initiative. Vorhandene Aufgaben werden bevorzugt:

| Thema | Bestehender Task | Entscheidung |
|---|---|---|
| Vibe-Lab-Verbraucher-Gate | `T005` | beibehalten |
| hochwertige Chronik-Ereignisse | `T006` | beibehalten |
| RepoGround-Artefaktfläche | `T008` | mit Retrieval-Probe ergänzen |
| WGX-Verengung | `T009` | beibehalten |
| Leitstand als eine Anzeige | `T010` | bereits verifiziert; Betriebsfrische separat reparieren |
| Bureau-Codex-Bridge | `T015` | neu live prüfen |
| GPT-Connector-Probe | `T016` | in Ende-zu-Ende-Bereitschaft integrieren |
| Bureau-Schedulerquellen | `T017` | beibehalten |
| Leitstand-Worktree-Health | `T022` | beibehalten, aber nicht mit Snapshotfrische verwechseln |
| HausKI-Audio-Wartung | `T041` | nicht unverändert ausführen; auf `audio` rebasen oder supersedieren |
| Reposkop | `T051` | Livezustand zeigt Umsetzung; Registry-Abschluss prüfen |
| stale-poll recovery | `T059` | beibehalten |

## Offene Deltas und Registry-Grenze

Die Initiative reicht auf dem geprüften `main` bereits bis `T064`. Die neuen Aufgaben `T060` bis `T064` schließen die hier gefundenen Audio-, Aktivierungs- und Goldpfadlücken nicht:

| Task | Reale Bindung | Verhältnis zu diesem Plan |
|---|---|---|
| `T060` | Heimserver PR #44, durch GitHub-Billing blockiert | unabhängig |
| `T061` | bereits gemergter Leitstand-Lifecycle-Consumer, aber unvollständige Lauf- und Acceptance-Receipts | erklärt einen Teil der Leitstand-Semantik, nicht die Snapshotfrische |
| `T062` | Leitstand-Deployment nach `T063` | durch Lifecycle-Abweichung blockiert |
| `T063` | bereits eingetretener Leitstand-Merge ohne vollständig gebundene Merge-Receipts | Reconciliation statt erneuter Ausführung nötig |
| `T064` | sauberer, checkout-unabhängiger Grabowski–Bureau-Claim-Pfad | direkte Voraussetzung für sichere neue Intake- und Claim-Arbeit |

Fachlich offen bleiben fünf Deltas. Zwei davon wurden nach Deduplizierung über den kanonischen Intake als eigenständige, reviewte Registrierungs-PRs veröffentlicht:

| Delta | Registry-Publikation | Statusgrenze |
|---|---|---|
| aktuelle Audio-Wahrheit über Systemkatalog und Verbraucher | `T065`, Bureau PR #1112 | PR offen; noch keine Registry-, Queue- oder Readiness-Wahrheit |
| Audio-Doctor und Profilkatalog vertragskonsistent machen | `T066`, Bureau PR #1113 | PR offen; noch keine Registry-, Queue- oder Readiness-Wahrheit |
| Aktivierungs- und Timerbudget | vorhandene `T017`, Welle 5 und aktuelle Observerbefunde | vor neuer Taskanlage gegen bestehende Schedulerarbeit prüfen |
| Goldfälle für Vibe-Coding, Wissen und Audio | vorhandene `T005`, `T008`, `T010` sowie Wellen 2 bis 4 | erst Messvertrag präzisieren, keine neue Plattform bauen |
| Ressourcenentwicklung und Ende-zu-Ende-Bereitschaft | vorhandene `T016`, `T059`, `T064` sowie Welle 1 | vorhandene Pfade schließen, nicht duplizieren |

Der erste Publikationsversuch wurde vom fail-closed Intake korrekt abgelehnt, weil der deployte Bureau-Core auf Commit `3b3bd37ac3f41c42f8d283b2b473e5ff96f5b391` gebunden war, während die zuerst geprüfte Registry noch auf `43226f13d6d6f1d952dcd922a2b98eb893ef0a5a` stand. Das Gate wurde nicht umgangen. Nach Rebase auf den übereinstimmenden Remote- und Runtime-Stand wurden `T065` und `T066` aus einem separaten sauberen Intake-Checkout publiziert. Dieser Ablauf bestätigt den Nutzen von `T064` für checkout-unabhängige Claims und Publikationen.

Weitere Meta-Aufgaben sind erst zulässig, wenn eine neue Sicherheitsverletzung, ein Goldpfadfehler oder eine nachweislich nicht von den genannten Tasks abgedeckte Lücke vorliegt.

## Messgrößen

### Vibe-Coding

- Medianzeit bis erster sinnvoller Diff;
- Medianzeit bis verifiziertes Ergebnis;
- Erfolgsquote ohne manuelle Shell-Arbeit;
- Ambiguitäts- und Transportfehler;
- Kontextfehlversuche;
- zurückgelassene Arbeitsobjekte pro Abschluss.

### Wissen

- Zeit bis zur Darstellung;
- Quellenabdeckung;
- reale Nutzung;
- Komponenten und manuelle Schritte pro Ergebnis.

### Audio

- Zeit bis hör- oder aufnahmebereit;
- aktuelle Hardwarekorrektheit;
- XRuns, Quantum und Rate;
- Pegel- und Dateibeleg;
- reproduzierbares Wiederöffnen.

### Plattformpflege

- Timerzahl und CPU-/Logbudget;
- identische wiederholte Fehler;
- blockierende Arbeitsprojektion;
- geplante Meta-Tasks;
- Anteil der Arbeit mit benanntem Nutzerergebnis.

## Stop-Regeln

Ein neuer Dienst, Timer, Repo oder Wahrheitsvertrag wird abgelehnt, wenn er nicht mindestens eine bestehende dauerhafte Oberfläche ersetzt oder einen belegten Goldpfadfehler schließt.

Eine Komponente wird nicht allein wegen geringer Nutzung gelöscht, wenn sie eine notwendige Sicherheitsgrenze bildet. Sie wird aber aus dem Standardpfad entfernt, wenn sie nur selten benötigt wird.

Ein Goldpfad gilt nicht als erfolgreich, wenn sein Abschluss verdeckte manuelle Reparatur, fremde Dirty-State-Mutation oder unbelegte Wirkung voraussetzt.

## Entscheidungsreihenfolge

1. falsche Livewahrheit korrigieren;
2. Kernpfad stabilisieren und messen;
3. Vibe-Coding-Goldfälle beweisen;
4. Audio- und Wissenspfade beweisen;
5. Timer und Arbeitsreste anhand der Messergebnisse reduzieren;
6. erst danach Komponenten archivieren oder zusammenlegen.

## Definition von Abschluss

Der Masterplan ist umgesetzt, wenn:

- ChatGPT -> Tunnel -> Grabowski in einem gemeinsamen Read-only-Status nachvollziehbar ist;
- drei Vibe-Coding-Goldfälle wiederholt gelingen;
- mindestens ein Wissens- und zwei Audio-Goldfälle live funktionieren;
- Systemkatalog und Bureau ausschließlich `audio` als aktuelles Audio-Produkt verwenden;
- Leitstand frische Daten oder einen klaren neuen Blocker zeigt;
- blockierende Arbeitsreste deutlich reduziert sind;
- identische Observerfehler nicht stündlich erneut Arbeit erzeugen;
- neue Meta-Arbeit nach Nutzerwirkung statt nach Systemlautstärke priorisiert wird.

Der Plan verlangt keine pauschale Abschaltung. Er verlangt, dass jede aktive Schicht ihren Nutzen an einem Nutzerpfad oder einer unverzichtbaren Sicherheitsinvariante belegt.
