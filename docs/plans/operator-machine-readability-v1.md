# Operator Machine Readability v1

Stand: 2026-07-18

## Zweck

Das Operator-Ökosystem wird so ausgerichtet, dass ChatGPT über Grabowski der ausführende Operator ist. Der Nutzer ist Beobachter und Steuermann: Er setzt Ziel, Bedeutung, Priorität, Risikotoleranz und notwendige Freigaben, interveniert bei Bedarf und kann abbrechen. Der Operator liest den Livezustand, wählt Wahrheitsquellen, plant, registriert, führt aus, verifiziert, reconciliert unklare Effekte und berichtet. Operative Hilfsarbeit wird nicht an den Nutzer zurückdelegiert.

## Arbeitsvertrag: Operator und Beobachter/Steuermann

| Rolle | Verantwortung | Nicht die Aufgabe der Rolle |
|---|---|---|
| ChatGPT über Grabowski — Operator | Livezustand prüfen; Primärquellen wählen; Arbeit planen und im Bureau registrieren; Claims und Leases beachten; autonom ausführen; Tests, CI, Diff, Deployment und Readback verifizieren; unklare Effekte reconciliieren; Ergebnis und Restlücken belegt melden. | Ziele oder Werte des Nutzers erfinden; explizite Kosten-, Sicherheits-, Datenschutz-, Irreversibilitäts- oder Freigabegates umgehen. |
| Nutzer — Beobachter und Steuermann | Ziel und Sinn bestimmen; Prioritäten und Risikotoleranz setzen; notwendige Freigaben geben; Kurs korrigieren; stoppen. | Shell-Befehle ausführen; Dateien suchen oder editieren; Status aus mehreren Systemen zusammensetzen; Routine-Reviews, Retries, Readbacks oder Task-Buchhaltung übernehmen. |

Der Begriff `observer` in technischen Quellbeobachtern bleibt davon getrennt: GitHub-, Runtime- oder Prozess-Observer liefern Fakten. Sie sind keine Beschreibung der menschlichen Systemrolle.

Der erste produktive Slice ist über `heim-pc` PR #37 umgesetzt: ein statischer, maschinenlesbarer Host-Einstieg mit lokalen Projektionen, Wahrheitsquellen und Sicherheitsgrenzen. Dieses Programm erfasst die belegten Restlücken, ohne eine neue konkurrierende Wahrheitsschicht einzuführen.

## Architekturentscheidung

### Gewählte Form

Eine hybride Kette:

1. `heim-pc` hält den kleinen statischen lokalen Einstieg und Checkout-Lokatoren.
2. Grabowski liefert Runtime-Identität, Werkzeuge, Leases, Effekte und Receipts.
3. Systemkatalog hält stabile Ökosystemsemantik.
4. Bureau hält Aufgaben, Claims und Closeout-Receipts.
5. Git, GitHub, CI, systemd, Logs und Healthchecks bleiben Primärquellen für aktuellen Zustand.
6. RepoBrief/Lenskit liefert nur provenance-gebundenen Repository-Kontext.
7. Metarepo hält Fleet-Mitgliedschaft und zentrale Verträge.

### Machine-Operability als primäres Optimierungsziel

Eine Oberfläche ist nicht deshalb gut, weil ein Mensch sie bequem bedienen kann. Sie ist gut, wenn der Operator sie zuverlässig, sparsam und ohne versteckte Gesprächserinnerung konsumieren kann. Primäre Anforderungen sind:

- typisierte Ein- und Ausgaben mit stabiler Schema-Version und stabilen Fehlercodes;
- kompakte Standardantworten und gezielte Detailauflösung über stabile Identitäten;
- Idempotenzschlüssel, exakte Preconditions, Compare-and-swap und Driftverweigerung;
- Quellen-, Commit-, Tree-, Plan-, Diff- und Receipt-Bindung;
- explizite Felder für `effect_started`, Mehrdeutigkeit, Retrybarkeit und erforderlichen Readback;
- keine Prosa-Auswertung als Steuerlogik und kein vollständiges Laden großer Register ohne Bedarf;
- autonome Nutzung ohne Shell-, Dateisystem- oder JSON-Arbeit des Nutzers;
- messbare Werkzeugaufrufe, Bytes, Latenz, Fehlversuche, Duplikatvermeidung und menschliche Interventionen.

CLI bleibt wichtig, aber anders gewichtet: als deterministischer Transport, lokaler Diagnosepfad und rückrollbarer Fallback. Die primäre Produktfläche für operative Nutzung sind typisierte Grabowski-Aufrufe auf einer einmalig implementierten Bureau-Domänenlogik. Menschenlesbare Dashboards, Berichte und Visualisierungen bleiben für Beobachtung und Steuerung wichtig, dürfen aber keine Voraussetzung für Ausführung, Readback oder Task-Buchhaltung sein. Eine HTTP-API ist erst gerechtfertigt, wenn ein realer entfernter Verbraucher belegt ist.

`Review` bezeichnet eine policy- und evidenzgebundene Prüfung, nicht automatisch manuelle Menschenarbeit. Operator-Self-Review ist zulässig, wenn der aktive Vertrag dies erlaubt; der Nutzer wird nur einbezogen, wenn ein Steuergate, Kostenrisiko, Sicherheitsrisiko, Datenschutzbezug oder irreversible Wirkung dies verlangt.

### Verworfener Alternativpfad

Alle Einstiegslogik ausschließlich in Grabowski einzubauen wäre kurzfristig einfacher, würde aber den Host-Einstieg an eine laufende Runtime binden und lokale Recovery sowie unabhängige Prüfung schwächen. Umgekehrt wäre ein umfassender statischer Hostkatalog schnell stale und würde Live-Zustand duplizieren. Daher bleibt der statische Vertrag klein und Live-Zustand wird immer frisch gelesen.

## Belegter Ausgangszustand

- Grabowski-Runtime und Werkzeugkatalog waren integer, aber der Agent-Bootstrap kannte den lokalen Host-Einstieg und den Systemkatalog nicht als first-class Route.
- Connector-Snapshot-Identität und strukturelle Kompatibilität waren nicht beobachtbar. Das ist von Modellverständnis oder allgemeiner Client-Compliance zu trennen.
- Eine gültige Grabowski-Lease verhinderte nicht, dass ein unbekannter gleichberechtigter Prozess mehrfach in einen isolierten Worktree schrieb; die Schreibherkunft war nicht auditgebunden.
- Der globale `bureau`-Python-Einstieg lud einen veralteten Build aus `~/.local/lib/python3.10/site-packages`, obwohl der aktuelle Checkout die gemeldeten Lifecycle-Probleme bereits behoben hatte.
- Bureau klassifizierte die lokalen Verzeichnisse `evidence` und `plans` nicht.
- RepoBrief-Freshness konnte ohne Source-Commit-Provenienz nur `unknown` liefern.
- Systemkatalog war stark maschinenlesbar, aber die deterministische Leseoberfläche war überwiegend CLI-/Dateipfad-basiert und nicht als typisierte Grabowski-Fläche verfügbar.
- Die lokale Metarepo-Contract-Validierung hing im isolierten Worktree von einem Geschwisterpfad beziehungsweise Prozess-Environment ab.
- `heim-pc`-Placeholder-State wurde im ersten Slice entfernt; ein optionaler künftiger Hostzustand braucht Provenienz statt Null-/Scheinwerten.

## Arbeitspakete

1. Grabowski: kanonischen Operator-Einstieg typisiert lesen und auftragsabhängig routen.
2. Grabowski: Connector-Snapshot-Identität und strukturelle Vertragskompatibilität beobachtbar machen; Verhaltensbelege bleiben getrennt.
3. Grabowski: Lease-Schreibschutz und Herkunftsattribution erzwingen.
4. Bureau: deployed CLI, Checkout und Runtime-Quelle identisch und sichtbar machen. **Verifiziert am 2026-07-13 über PRs #521/#522, immutable Release, Ambient-Quarantäne und exakten Grabowski-Lease-Probe.**
5. Bureau: `evidence`/`plans` klassifizieren oder migrieren, ohne unbekannte Daten zu löschen.
6. RepoBrief/Lenskit: bestehenden Task `OPERATOR-INTEGRATION-LOOP-V1-T004` für Source-Commit, Dirty-State, Erzeugungszeit und Refresh-Provenienz wiederverwenden; keine Dublette anlegen.
7. Systemkatalog/Grabowski: typisierte read-only Abfragefläche mit stabilen Non-Claims.
8. Metarepo/heim-pc: Contract-Auflösung in isolierten Worktrees reproduzierbar machen.
9. heim-pc: optionale Hostzustände nur provenance-gebunden erzeugen; Abwesenheit bleibt gültig.
10. End-to-End: beweisen, dass ein frischer Operator ohne Gesprächsvorwissen beim richtigen Host-, System-, Task- und Runtime-Kanon landet.
11. Grabowski/Bureau: den sicheren Übergangszustand aus kanonischem Manifest-Release und separat hashgebundenem Vertrags-Venv auf eine einzige Release-Identität konsolidieren.
12. Bureau/Grabowski: Kandidatenaufnahme, semantisch begrenzte Bewertung, reviewgebundene Task-Vorschläge und PR-Veröffentlichung als operator-native, typisierte und receipt-fähige Fläche bereitstellen; `BUR-2026-003-T008` bleibt nur Steuerboard-Quelladapter.

## Grenzen

- Kein zweiter Runtime-, Task-, PR- oder CI-Statusspeicher.
- Kein breiter Scan des Home-Verzeichnisses.
- Keine Secret-, Browserprofil-, Keyring- oder privaten Inhaltsflächen.
- Kein automatisches Queuing, Claiming, Dispatching, Mergen oder Deployen durch diese Registrierung.
- Keine für Menschen optimierte Paralleloberfläche als Primärprodukt. Menschenlesbare Ausgaben sind Projektionen; die operator-native typisierte Fläche ist maßgeblich.
- Der Rollenvertrag hebt keine expliziten Kosten-, Sicherheits-, Datenschutz-, Irreversibilitäts- oder Freigabegates auf.
- Prosa und generierte Ansichten bleiben Projektionen; aktuelle Primärquellen haben Vorrang.
- Unbekannte Provenienz führt zu `unknown` oder Blockierung, nicht zu einem Aktualitätsclaim.
- Strukturelle Connector-Kompatibilität belegt weder Modellverständnis noch korrektes künftiges Verhalten.

## Erfolg

Das Programm ist abgeschlossen, wenn ein frischer ChatGPT-Operator über Grabowski:

- den lokalen Einstieg typisiert und hashgebunden erhält;
- die richtige Wahrheitsquelle nach Scope deterministisch auswählt;
- Runtime-, Connector-, Checkout- und Contract-Drift sehen kann;
- fremde Writes in geleasten Worktrees verhindert oder eindeutig attribuiert;
- keine stale installierte Bureau- oder Contract-Runtime benutzt;
- RepoBrief-Kontext nur mit belegter Provenienz als aktuell behandelt;
- einen reproduzierbaren End-to-End-Test ohne menschliche Shell-Hilfe besteht;
- Kandidaten und Task-Vorschläge typisiert, idempotent und quellengebunden verarbeitet;
- keine manuelle JSON-Bearbeitung, Dateisuche oder Statusaggregation an den Nutzer delegiert;
- CLI nur als Transport oder Diagnosefallback und nicht als menschliche Hauptbedienoberfläche benötigt.

## Nichtbelege

Die Registrierung belegt weder Umsetzung noch Nutzen im Betrieb. Nutzen gilt erst nach head-/diff-gebundener Implementierung, Tests, Deployment und einem frischen End-to-End-Verbraucherbeleg.
