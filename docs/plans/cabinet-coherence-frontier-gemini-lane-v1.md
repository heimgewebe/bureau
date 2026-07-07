# Cabinet Coherence Frontier and Gemini Review Lane v1

## Status

- Typ: Bureau-Registrierungsplan
- Stand: 2026-07-07
- Quelle: Cabinet-/Bureau-/Grabowski-Lageprüfung vom 2026-07-07
- Bureau-Rolle: registrieren, takten, Import prüfen, Delegation vorbereiten, Receipts verwalten
- Autonomie: nicht aktiviert

## Entscheidung

Cabinet soll den Gesamtsinn des Operator-Ökosystems zusammenhalten: Karte, Rollen, Claims, Kohärenz, Fehlerbefunde und Wartungssignale. Bureau soll daraus nicht blind Arbeit starten, sondern geprüfte Cabinet-Kandidaten in Bureau-Aufgaben überführen. Grabowski und gebundene Agenten führen erst nach Bureau- oder Operator-Gate aus.

Kurzform:

```text
Cabinet erkennt.
Bureau registriert und taktet.
Grabowski führt aus.
Agenten schlagen vor oder reviewen.
CI, GitHub und Runtime beweisen.
Heimlern lernt.
Leitstand zeigt.
```

## Dialektik

### These

Das Ökosystem braucht eine Schicht, die quer über Repos, Karten, Claims, CI-Signale, Runtime-Hinweise, RepoBrief-Artefakte und Agentenübergaben nach Widersprüchen sucht. Cabinet ist dafür der richtige Ort, weil es die Ecosystem Map und Registry-Semantik bereits hält.

### Antithese

Wenn Cabinet direkt Bureau-Tasks schreibt, Grabowski startet, Agenten beauftragt, PRs vorbereitet oder Runtime-Wirkungen auslöst, wird Cabinet zum Schatten-Orchestrator. Dann verschwimmen Bureau, Grabowski und Cabinet.

### Synthese

Cabinet darf Wahrnehmung, Sinnbildung, Priorisierung und Vorschläge automatisieren. Bureau importiert nur reviewte Kandidaten. Grabowski und Agenten führen nur gebunden aus. Gemini wird als proposal-only Review- und Scout-Kapazität modelliert, nicht als autonomer Operator.

## Organmodell

| Organ | Aufgabe | Grenze |
|---|---|---|
| Cabinet | Map-Canon, Claims, Kohärenzradar, Findings, Frontier-Kandidaten | keine direkte Task-, Dispatch- oder Runtimewirkung |
| Bureau | Registry, Queue, Import-Gate, Taktung, Delegationsvorbereitung, Receipts | keine fachliche Wahrheitsbehauptung und kein Patch-Organ |
| Grabowski | Repo-Arbeit, GitHub/CI-Prüfung, kontrollierte Ausführung | nur nach Task, Freigabe oder Gate |
| Gemini-Agenten | breite Analyse, Gegenprüfung, strukturierte Vorschläge | proposal-only; kein Push, Merge, Runtime-Effekt oder Credentials-Kontext |
| RepoBrief/Lenskit | externe Kontext- und Dump-Artefakte | keine Cabinet-Task-Erzeugung |
| Heimlern | Outcome-Auswertung und Policy-Vorschläge | keine direkte Regelaktivierung |
| Leitstand/Schauwerk | Anzeige und Visualisierung | nicht Canon, nicht Wahrheit |
| GitHub/CI/Runtime | harte Primärquellen | keine semantische Gesamtdeutung |

## Phasen

### CCFG-1 — Cabinet Organ Map and Gemini Agent Registry

Cabinet erweitert die Ecosystem Registry um eine Gemini-Agentenrolle und präzisiert README, AGENTS und Map-Einstiege. Die Kanten müssen ausdrücken: Gemini berichtet Vorschläge, erhält aber keine Mutationshoheit.

### CCFG-2 — Cabinet Frontier Contract

Cabinet definiert eine maschinenlesbare Frontier für Bureau-Kandidaten. Diese Frontier enthält Findings, Zielrepo, Risiko, Evidence, Akzeptanz und verbotene Effekte. Sie erzeugt keine Bureau-Tasks.

### CCFG-3 — Bureau Frontier Reader

Bureau liest die Cabinet Frontier read-only und erzeugt Preview, Review und Receipt. Invalides, unklares oder riskantes Material wird blockiert.

### CCFG-4 — Reviewed One-Task Import

Bureau darf nach Review genau einen Cabinet-Kandidaten als Bureau-Task importieren. Der Import ist idempotent und erzeugt keinen Dispatch.

### CCFG-5 — Gemini Proposal-Only Review Lane

Gemini-Agenten werden als Review- und Scout-Kapazität angebunden. Sie arbeiten nur auf freigegebenem, credentialfreiem Kontext und geben strukturierte Vorschläge zurück.

### CCFG-6 — Outcome Feedback Loop

Bureau-Receipts und Grabowski-/CI-Ergebnisse laufen als Outcomes nach Cabinet zurück. Heimlern darf daraus Vorschläge ableiten, aber nicht aktivieren.

### CCFG-7 — Read-only Status Projection

Leitstand oder Schauwerk zeigen Map, Frontier, Bureau-Queue, PR-/CI-Status, Agentenlane und blockierte Kandidaten. Anzeige bleibt read-only.

## Stop-Kriterien

Stoppe Import oder Delegation, wenn:

- Zielrepo oder Primärquelle unklar ist;
- offene PRs oder Tasks kollidieren;
- Evidence fehlt;
- Risiko `high` ohne menschliche Freigabe ist;
- Gemini-Output nicht schema-valide ist;
- verbotene Effekte nicht explizit false sind;
- Credentials, Runtime-Privatdaten oder Deploy-Flächen berührt werden.

## Nicht-Ziele

- Kein direkter Cabinet-Dispatch.
- Kein automatischer Merge.
- Kein automatischer Push durch Gemini.
- Keine Runtime-Mutation.
- Keine Credentials in Agentenkontexten.
- Keine Wahrheit aus Mermaid-Karten.
- Kein Bureau als Patch-Organ.

## Erste Umsetzungsscheiben

1. Cabinet Map/Gemini-Rolle registrieren.
2. Cabinet Frontier Contract definieren.
3. Bureau Frontier Reader bauen.
4. One-Task Import nach Review bauen.
5. Gemini Review Lane als proposal-only anbinden.
6. Outcome Feedback Loop schließen.
7. Read-only Status Projection anzeigen.
