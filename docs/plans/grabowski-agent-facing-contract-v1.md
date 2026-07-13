# Grabowski Agent-facing Contract v1

## Ausgangspunkt

Grabowski besitzt eine breite, auditierbare Operatoroberfläche. Der bei Erstellung dieses Plans verifizierte Deploymentvertrag umfasst 121 registrierte Tools; diese Zahl ist eine Momentaufnahme, kein Zielwert. Die Oberfläche wird in ChatGPT bedarfsgerecht entdeckt; ein pauschales Vorladen aller Schemas ist daher nicht als aktueller Hauptengpass belegt. Belegt sind dagegen vier andere Reibungsquellen:

1. Serverweite Betriebsregeln liegen überwiegend in Repositoryprosa und werden nicht als MCP-Initialisierungsanweisung ausgeliefert.
2. Fehlerklassifikation und Folgerouting sind vorhanden, erreichen den aufrufenden Agenten aber nicht einheitlich direkt am Fehlerort.
3. Neue Fähigkeiten können die Top-Level-Toolfläche vergrößern, ohne dass ein messbares Surface-Budget oder ein Operation-Katalog-Gate greift.
4. Komplexe Idempotenz-, Retry- und Zustandsverträge sind teilweise nur in langer Prosa normativ sichtbar.

Ein separater Observe-Connector kann unter einer Least-Privilege-Perspektive sinnvoll sein. Sein Vorteil gegenüber dem bereits vorhandenen verzögerten Tool-Laden ist jedoch nicht belegt und muss deshalb durch einen späteren Canary entschieden werden.

## Ziel

Die Agentenoberfläche soll weniger Entscheidungsentropie erzeugen, ohne Sicherheitsgrenzen oder auditierbare Einzelwirkungen in generische String-Dispatcher zurückzufalten.

Der Zielvertrag besteht aus sechs sequenzierten Slices:

1. MCP-Initialisierungsanweisungen mit versionierter, hashgebundener Quelle.
2. Einheitliche maschinenlesbare Fehlernavigation mit enger Alternativroute.
3. Tool-Surface-Budget und Operation-Katalog-Gate für neue Fähigkeiten.
4. Normative Zustands- und Retrytabellen für die komplexesten Verträge.
5. Metadaten-, Namens- und Golden-Prompt-Evaluation mit Präzisions- und Recallmessung.
6. Evidenzgebundener Observe-Connector-Canary; kein zweiter Connector ohne gemessenen Vorteil.

## Leitplanken

- Livezustand, Deploymentprovenienz und konkrete Receipts bleiben höherrangig als Prosa.
- Sicherheits- und Berechtigungsgrenzen bleiben explizite Tools, wenn getrennte Autorität oder Reversibilität dies verlangt.
- Fehlerantworten dürfen niemals automatisch auf ein breiteres oder riskanteres Tool eskalieren.
- Top-Level-Toolwachstum ist kein Selbstzweck. Eine neue Fähigkeit wird bevorzugt als benannte Operation hinter einem bestehenden, typisierten Einstiegspunkt modelliert.
- Toolnamen beschreiben die tatsächliche Wirkung. Explizite Risikonamen wie `destroy` oder `secret_reveal` werden nicht kosmetisch entschärft.
- Ein zweiter Connector ist eine gemessene Produktentscheidung, keine Vermutung aus der Toolanzahl.

## Reihenfolge und Abhängigkeiten

### T040 – MCP initialization instructions

Kleiner, direkt wirksamer Slice. Eine kanonische strukturierte Quelle wird beim Serverstart als `instructions` an FastMCP gebunden. Vertragstests prüfen Inhalt, Hash, Größenlimit und Deploymentbindung.

### T041 – Structured failure navigation

Ein gemeinsames Fehlerobjekt führt mindestens `failure_class`, `error_code`, `retry_unchanged`, `next_action`, `alternative_tool` und `authority_changed`. Bestehende Sicherheits- und Transportambiguität bleibt fail-closed.

### T042 – Tool surface budget and operation catalog gate

Ein Merge-Gate verlangt für jedes neue Top-Level-Tool eine begründete Ausnahme. Standardpfad für neue Fähigkeiten ist eine benannte Operation im Katalog. Das Gate prüft keine bloße Zielzahl, sondern Berechtigungsgrenze, Aufrufstruktur und gemessenen Auswahlvorteil.

### T043 – Machine-readable complex contracts

Für mindestens Agent Workspace, durable tasks/jobs und irreversible beziehungsweise secret-backed Operationen entstehen normative Zustands-, Vorbedingungs-, Fehler- und Retrytabellen. Dokumentation wird daraus erzeugt oder dagegen geprüft.

### T044 – Metadata and golden prompt evaluation

Toolbeschreibungen erhalten handlungsbezogene Anwendungs- und Ausschlussfälle. Ein reproduzierbarer Golden-Prompt-Datensatz misst Toolauswahl, Verwechslungen und unnötige Eskalation. Umbenennungen erfolgen nur bei belegt besserer Auswahl und mit Kompatibilitätsplan.

### T045 – Observe connector canary

Ein kleiner Observe-Connector wird nur als zeitlich begrenzter Canary gebaut, falls T044 eine plausible Auswahl- oder Autoritätsfrage offenlässt. A/B-Messung vergleicht einen gegen zwei Connectoren. Ohne klaren Vorteil wird kein zweiter Default-Connector eingeführt.

## Abnahme über alle Slices

- `make validate` und beide unterstützten Python-Versionen bleiben grün.
- Öffentliche Toolnamen und Fähigkeiten ändern sich nur mit explizitem Kompatibilitätsnachweis.
- Die Runtime wird commitgebunden deployt und live geprüft.
- Nichttriviale PRs erhalten vollständigen GitHub-Diff und head-/diff-gebundenen kritischen Self-Review.
- Jede Messung benennt, was sie nicht beweist.

## Risikobewertung

Der größte kurzfristige Nutzen liegt in T040 und T041. Das größte strukturelle Risiko liegt in T042: Ein zu aggressiver Operation-Dispatcher könnte klare Sicherheitsgrenzen verwischen. T043 reduziert diese Gefahr durch normative Zustandsverträge. T045 hat nur dann Priorität, wenn Messungen einen echten Vorteil erwarten lassen.

## Nichtziele

- keine pauschale Reduktion auf eine beliebige Toolzahl;
- kein generisches Universaltool für irreversible, privilegierte oder secret-backed Wirkungen;
- keine Behauptung, ein Observe-Connector spare in der aktuellen ChatGPT-Integration automatisch Kontext;
- keine automatische Retry- oder Eskalationsautorität aus Fehlerantworten;
- keine Ablösung von Bureau-, Git-, GitHub-, Deployment- oder Receipt-Wahrheit.