# RepoBrief Agent Benchmark v1

Status: registered execution plan  
Source issue: `heimgewebe/bureau#481`

## Ziel

Den zusätzlichen Nutzen eines RepoBrief-MCP-Zugangs gegenüber einem normalen
Coding-Agent-Workflow mit Dateisuche, `grep`, Glob und gezieltem Lesen
reproduzierbar messen. Der Benchmark darf ein Null- oder Negativergebnis
liefern und darf keine Standardbeförderung selbst auslösen.

## Zu prüfende Hypothese

Bei identischem Modell, Prompt, Budget und Zielrepository verbessert die
zusätzliche RepoBrief-Fläche mindestens eine vorab benannte Aufgabenklasse,
ohne Trefferquote, Belegbarkeit, Sicherheit oder ehrliches Nichtwissen in
einer anderen Klasse relevant zu verschlechtern.

## Bedingungen

### Baseline

Zulässig sind ausschließlich die normalen read-only Coding-Werkzeuge:

- Dateisuche und Glob;
- Textsuche beziehungsweise `grep`;
- gezieltes Lesen von Dateien und Bereichen;
- keine RepoBrief-Bundles, MCP-Ressourcen oder RepoBrief-Hilfswerkzeuge.

### Behandlung

Die Baseline-Werkzeuge bleiben verfügbar. Zusätzlich zulässig sind:

- `ask_context`;
- RepoBrief-MCP-Ressourcen;
- `grounding_verify`;
- `live_freshness`.

Damit wird der realistische additive Nutzen gemessen. Ein künstlicher
Ersatzvergleich, bei dem der RepoBrief-Agent keine Dateien mehr lesen darf,
ist nicht Teil von v1.

## Isolierung

Jeder Fall wird zweimal in getrennten frischen Agentensitzungen ausgeführt.
Zwischen Bedingungen dürfen keine Transkripte, Toolergebnisse, Caches oder
Erinnerungen übertragen werden. Die Reihenfolge wird pro Fall deterministisch
balanciert, damit weder Baseline noch Behandlung systematisch zuerst läuft.

Fixiert werden vor dem ersten Ergebnis:

- Modell- und Providerkennung;
- System- und Aufgabenprompt;
- Temperatur beziehungsweise deterministische Sampling-Einstellungen;
- maximales Input-, Output- und Zeitbudget;
- erlaubte Werkzeugnamen und Versionen;
- Repository, Commit, Snapshot-Manifest und Freshness-Zustand;
- erwartete Dateien, Symbole, Bereiche, Aussagen und zulässige Abstinenz.

## Taskset

V1 enthält 24 vorab registrierte Fälle auf mindestens drei Repositories:

- 8 Navigationsfälle: relevante Implementierung, Tests, Verträge oder
  Einstiegspunkte finden;
- 8 Struktur-/Auswirkungsfälle: eingehende oder ausgehende Beziehungen,
  Änderungsumfeld und begrenzte Negativaussagen bestimmen;
- 8 Grounding-/Freshness-Fälle: Belege prüfen, unzureichende Evidenz erkennen,
  Stale-Zustände korrekt behandeln und unbelegte Behauptungen verweigern.

Mindestens ein Viertel der Fälle verlangt ausdrücklich eine korrekte
Negativ- oder Abstinenzantwort. Das verhindert, dass bloß viele Pfade genannt
werden und dies fälschlich als Qualität zählt.

## Runner-Vertrag

Der Benchmark-Harness startet den eigentlichen Agenten über einen expliziten,
providerneutralen Runner-Befehl. Der Runner erhält eine JSON-Anfrage und muss
ein strukturiertes Ergebnis plus unverändertes Transcript liefern.

Erforderliche Eingaben:

- Fall-ID und Bedingung;
- fixierter Prompt;
- Repository- und Commitbindung;
- Werkzeug-Allowlist;
- Budgetgrenzen;
- optionaler RepoBrief-MCP-Startvertrag für die Behandlung.

Erforderliche Ausgaben:

- finale Antwort;
- Toolaufrufe mit Namen, Eingabe, Ergebnisstatus, Dauer und Byteumfang;
- Input- und Output-Tokens aus der Providerantwort, nicht geschätzt;
- Modell- und Providerkennung;
- Start-/Endzeit und Abbruchgrund;
- vollständiges Transcript oder ein kryptografisch gebundenes Rohartefakt;
- Exitstatus und strukturierte Fehlerklasse.

Fehlen echte Provider-Tokenwerte, ein unverändertes Transcript oder eine
belegbare Modellkennung, ist der Lauf `invalid`, nicht `0` oder `success`.

## Auswertung

Pro Fall und aggregiert werden gemessen:

- Zieltreffer und Fehlertreffer;
- Citation- und Range-Korrektheit;
- korrekte Abstinenz und falsches Vertrauen;
- Zeit bis zur finalen Antwort;
- Anzahl und Art der Toolaufrufe;
- Input- und Output-Tokens;
- gelesene beziehungsweise ausgegebene Bytes;
- Stale-Erkennung und Verhalten bei fehlender Evidenz;
- Abbruch-, Timeout- und Toolfehlerquote.

Die fachliche Bewertung erfolgt gegen vorab fixierte maschinenlesbare
Erwartungen. Freitexturteile ohne Goldziel gelten nicht als alleinige
Erfolgsevidenz.

## Entscheidungsschwelle

Eine Aufgabenklasse darf als RepoBrief-Nutzenpfad gelten, wenn:

- ihre Erfolgs- oder Belegbarkeitsrate nicht sinkt;
- keine relevante Zunahme falschen Vertrauens entsteht;
- mindestens eine vorab registrierte Effizienzmetrik um 20 Prozent oder mehr
  verbessert wird, oder die fachliche Erfolgsrate um mindestens 10
  Prozentpunkte steigt;
- das Ergebnis in einer getrennten Wiederholung mit gleicher Konfiguration
  dieselbe Richtung zeigt.

Eine Standardbeförderung erfordert zusätzlich:

- keine Fallklasse mit einer Erfolgsverschlechterung über 5 Prozentpunkte;
- keine Sicherheits- oder Freshness-Regression;
- eine eigene Bureau-Entscheidung nach Review des vollständigen
  Diff-/Transcript-Pakets.

Der Benchmark selbst setzt `default_promoted=false`.

## Stopregeln

- Kein echter instrumentierter Agent-Runner: Taskset und Harness dürfen gebaut
  werden, die Liveausführung bleibt jedoch `blocked`; Modellwerte werden nicht
  simuliert.
- Unvollständige oder nachträglich veränderte Goldziele: Lauf ungültig.
- Provider-, Modell- oder Tooländerung zwischen Bedingungen: Paar ungültig.
- Transcript- oder Tokenlücke: betroffener Lauf ungültig.
- Recall- oder Sicherheitsregression: keine Nutzenbeförderung trotz möglicher
  Token- oder Zeitersparnis.

## Aktueller Live-Gate-Zustand (13. Juli 2026)

- `RAB-V1-T002C` ist verifiziert.
- `RAB-V1-T002D` hat seine einmalige Freigabe verbraucht, wurde aber vor jedem Provider-Dispatch ungültig: Die äußere Argumentauswertung behandelte `--claude-command` als Abkürzung für `--claude-command-sha256`.
- Es entstanden kein Provider-Intent, kein Claude-Prozess, kein Live-Transcript und keine Providerkosten. Ein Retry von `T002D` ist ausgeschlossen.
- `RAB-V1-T002E` härtet Argumentadapter, Plannervertrag und Export-Redaktion ausschließlich synthetisch.
- `RAB-V1-T002F` bleibt geplant und nicht autorisiert. Es darf erst nach verifiziertem `T002E` eine vollständig neue Einmalfreigabe erhalten.
- `RAB-V1-T002` bleibt blockiert und ungestartet.

## Phasen

### RAB-V1-T001 — Harness und gefrorenes Taskset

- JSON-Verträge für Taskset, Run Request, Transcript Receipt und Evaluation;
- 24 feste Fälle mit Repository- und Commitbindung;
- Runner-Adapter, Budget-/Allowlist-Prüfung und deterministische Paarplanung;
- Offline-Evaluator und synthetische Contract-Fixtures;
- keine Modell- oder Nutzenbehauptung.

### RAB-V1-T002 — reale gepaarte Agentenläufe

- instrumentierten Runner und exakte Modellkonfiguration binden;
- Baseline und Behandlung isoliert ausführen;
- Rohtranskripte, Provider-Tokens und Evaluationsdaten kryptografisch binden;
- mindestens eine identische Wiederholung;
- Null- und Negativergebnisse unverändert behalten.

### RAB-V1-T003 — Entscheidung und Nachlauf

- Nutzenklassen, Regressionen und Unsicherheit terminal einordnen;
- opt-in beibehalten, bevorzugen oder verwerfen;
- erst danach über `bureau#482` inkrementellen Rebuild/Watcher entscheiden;
- keine automatische Standardaktivierung.

## Nichtziele

- kein LLM im deterministischen RepoBrief-Kern;
- kein Tuning des Tasksets nach Ergebnisbeobachtung;
- kein Ersatz von Code-Review oder Tests;
- keine allgemeine Produktreife- oder Marktführerschaftsbehauptung;
- keine Vermischung mit Graphausbau, Memory oder File-Watcher.
