# RepoBrief CodeGraph Adoption v1

Registered: 2026-07-13

## Zweck

Die nützlichen Mechanismen von `codegraph-ai/CodeGraph` werden nicht als zweites dauerhaftes Index-, Memory- oder Wahrheitsystem eingeführt. Stattdessen wird geprüft und umgesetzt, welche Mechanismen Lenskit/RepoBrief als read-only Agentenfläche messbar verbessern.

## Ausgangslage

Lenskit besitzt bereits kanonische Bundles, Citation-/Range-Provenienz, FTS5-Retrieval, Symbolsuche, Architekturgraph, Relation Cards, Delta Context und einen gemessenen read-only Workbench-Adapter. `RBAE-V1-T003` definierte bereits den Code-Impact-Map-Vertrag, implementierte aber keine vollständige Lenskit-native Runtime für Edit Context, related tests und Impact Analysis.

CodeGraph liefert als Referenz insbesondere diese Ideen:

- Caller-/Callee- und Dependency-Navigation;
- ein gebündeltes `edit_context` vor Änderungen;
- `related_tests`;
- advisory `impact_analysis` mit sichtbarer Evidenz und Begrenzung;
- kleine, aufgabenbezogene Werkzeugprofile statt einer breiten MCP-Fläche.

## Entscheidung

CodeGraph wird nicht als neues Organ, eigener Memory-Speicher oder kanonische Reviewinstanz betrieben. Lenskit bleibt Frontdoor, Vertragsschicht und Provenienzrahmen. Übernommen werden nur Mechanismen, die aus vorhandenen Lenskit-Artefakten deterministisch, read-only und messbar erzeugt werden können.

## Umsetzung

1. Bestehende Architekturgraph-, Symbolindex-, Query-, Relation-Card- und Delta-Context-Flächen wiederverwenden.
2. Eine Lenskit-native `agent_impact_context.v1`-Projektion bauen.
3. Für explizite Pfade oder Symbole eingehende und ausgehende Beziehungen, passende Tests, Verträge, Dokumentation und Einstiegspunkte als begrenzte Navigation ausgeben.
4. Quellstatus, Provenienz, Evidenzniveau, Kürzungen und fehlende Artefakte sichtbar halten.
5. Einen `edit_context`-Modus bereitstellen, der die kleinste sinnvolle Lesemenge vor einer Änderung bündelt.
6. Keine Risikozahl und kein Mergeurteil erzeugen. `impact_analysis` bleibt eine advisory Kandidatenliste.
7. Deterministische Fixtures, negative Fälle und ein Nutzengoldset ergänzen.
8. Nur bei messbarem Zusatznutzen gegenüber der bestehenden read-only Workbench-Fläche in die Standard-Agentenroute aufnehmen.

## Abnahme

- gleiche Eingaben und Artefakte erzeugen bytegleiches Ergebnis;
- eingehende und ausgehende Graphbeziehungen bleiben richtungs- und evidenztreu;
- Testkandidaten werden aus belegten Graphkanten und klar markierten Heuristiken getrennt;
- fehlende oder stale Artefakte werden nicht als aktuelle Erkenntnis dargestellt;
- `edit_context` enthält Quellziel, Beziehungen, Testkandidaten, Verträge/Dokumentation, Lücken und Nichtaussagen innerhalb eines festen Budgets;
- keine Git-, Patch-, Test-, Shell-, Memory- oder PR-Mutation aus RepoBrief;
- kein Reviewverdikt, keine Testabdeckungs- oder Blast-Radius-Vollständigkeitsbehauptung;
- Tests, CI, vollständiger Diff, diffgebundenes Self-Review und Post-Merge-Prüfung sind grün;
- ein festes Goldset zeigt Zusatznutzen oder die Funktion bleibt opt-in beziehungsweise wird verworfen.

## Nicht-Ziele

- CodeGraph als Dienst installieren oder dauerhaft betreiben;
- CodeGraph-Memory oder eine zweite Dokumentationswahrheit übernehmen;
- Pro-/Security-Funktionen kopieren;
- automatische GitHub-Kommentare;
- funktionsgenaue Call-Graph-Vollständigkeit behaupten, solange Lenskit-Artefakte nur Datei-/Modulgranularität belegen;
- RepoBrief zu einer Write-, Patch-, Test- oder Freigabeinstanz machen.

## Messregel

Ein Zusatznutzen gilt nur als belegt, wenn ein festes Goldset oder reale PR-Fälle mindestens eine dieser Bedingungen reproduzierbar erfüllen:

- höhere Trefferquote für betroffene Pfade, Symbole oder Tests;
- weniger exponierter Kontext bei gleicher Zieltrefferquote;
- zusätzliche richtige Beziehungen ohne zentrale Regression;
- explizitere Sichtbarkeit fehlender oder unsicherer Evidenz.

Ein guter Einzelbefund, eine plausible Graphkante oder eine grüne CI beweisen keine allgemeine Repositorykenntnis.