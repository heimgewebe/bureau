# Agent Frontier Governor v1

## Zweck

Der Agent Frontier Governor schließt die Lücke zwischen Discovery-Scanner und produktiver Task-Auswahl. Der Scanner kann einen großen stabilen Kandidatenbestand halten, während der Curator bei fehlendem Delta korrekt `idle` bleibt. Der Governor bewertet deshalb den gesamten Backlog read-only und erzeugt pro Lauf ein priorisiertes Frontier-Report-Artefakt.

## Nicht-Zweck

Der Governor mutiert keine Registry, dispatcht keine externen Agents und merged keine Branches. Er ist bewusst ein Bewertungs- und Gate-Artefakt, kein Autopilot.

## Zyklusposition

- `*:30` Discovery Scanner aktualisiert den Kandidatenbestand.
- `*:45` Curator prüft Delta-Handoffs.
- `*:50` Closure Planner bewertet vorhandene Arbeitslanes.
- `*:55` Agent Frontier Governor rankt Backlog und Bottlenecks.
- `*:00` Operator kann den Report als nächsten Kontext nutzen.

## Bewertungsachsen

Der Score gewichtet Fokus-Repositories (`weltgewebe`, `lenskit`, `grabowski`), Kandidatentyp, Status, Konfidenz, Planning-Pfadmarker und kanonische Task-Bezüge. Bereits registrierte Titel oder Fingerprints werden verworfen. Archivierte oder stale Pfade werden nicht promotet.

## Sicherheitsprinzip

Der Governor produziert nur Reports und Receipts. Jede Mutation bleibt an einen separaten Bureau-Task, ein kanonisches Task-Binding und die bestehenden Merge-Gates gebunden.
