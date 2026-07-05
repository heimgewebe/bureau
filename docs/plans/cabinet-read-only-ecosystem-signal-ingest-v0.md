# Cabinet Read-only Ecosystem Signal Ingest v0 — Bureau Plan

## Status

- Typ: Bureau-Anschlussplan
- Stand: 2026-07-05
- Quelle: Cabinet-Kritik und Cabinet Maintenance Radar / Bureau-Bridge-Lage
- Entscheidung: plan-only; keine operative Wirkung
- Autonomie: nicht aktiviert

## Zweck

Cabinet soll nicht mehr vor allem seine eigene Kohärenz verwalten, sondern reale Ökosystemsignale wahrnehmen, datieren, einordnen und als belegte Kandidaten an Bureau anschlussfähig machen.

Bureau soll diesen nächsten Schritt nicht vorwegnehmen. Bureau hält hier nur den Anschlussplan: Welche Signale Cabinet liefern muss, wann Bureau sie lesen darf, welche Grenzen gelten und welcher erste schmale Slice sinnvoll ist.

Kurzform:

```text
GitHub / CI / Worktree -> Cabinet Signal -> Cabinet Claim -> Bureau Preview -> menschliche Freigabe -> späterer Task
```

## These

Bureau profitiert erst dann von Cabinet-Kandidaten, wenn Cabinet nicht nur Selbst-Claims hält, sondern echte externe Zustände als datierte, belegte Signale beobachtet: offene PRs, rote CI, konfligierende Branches, stale Branches, fehlende Dumps, Review-Gates und Worktree-Drift.

## Antithese

Wenn Bureau diesen Schritt als Task-Import, Dispatch oder Queue-Mutation interpretiert, entsteht ein Schatten-Orchestrator. Cabinet würde Wahrnehmung behaupten, Bureau würde daraus Arbeit machen, bevor Quelle, Frische, Evidenz und Freigabe stimmen.

## Synthese

Der nächste Schritt ist eine dünne vertikale Perzeptionsscheibe:

1. Ein Repo als Fixture.
2. Nur read-only GitHub-/CI-/optional Worktree-Signale.
3. Ein maschinenlesbares Cabinet-Signalformat.
4. Daraus wenige Claims mit expliziter Frische.
5. Bureau konsumiert zunächst nur preview-/review-only, erzeugt keine Tasks.

## Alternative Sinnachse

Nicht: „Wie automatisieren wir schneller mehr Arbeit?“

Sondern: „Was muss Bureau sicher wissen dürfen, bevor Arbeit überhaupt verdient, geplant zu werden?“

Das kippt die Zielannahme: Die erste Optimierung ist nicht Durchsatz, sondern Wahrnehmungsqualität. Ein Radar, der sofort schießt, ist kein Radar. Er ist ein sehr nervöser Toaster mit Sicherheitsfreigabe.

## Scope des ersten Slices

Arbeitstitel:

```text
CAB-QA-004 / BUR-CAB-SIG-001: Read-only ecosystem signal ingest fixture
```

### Erlaubt

- ein Fixture-Repo auswählen, bevorzugt `lenskit` oder `weltgewebe`;
- GitHub-PR-Zustand read-only beobachten;
- CI-Status read-only beobachten;
- optional lokalen Worktree-/Branch-Zustand read-only beobachten;
- Signale in Cabinet als JSONL ablegen;
- wenige derived Claims erzeugen oder aktualisieren;
- Bureau-kompatible Preview-Felder ergänzen, aber nicht importieren.

### Verboten

- Bureau-Tasks automatisch erzeugen;
- Bureau-Queue ändern;
- Grabowski dispatchen;
- Merges, Pushes, Rebase, Cleanup oder Runtime-Mutationen auslösen;
- GitHub-/CI-/Runtime-Primärquellen durch Cabinet-Claims ersetzen;
- RepoBrief-/Lenskit-Dumps in Cabinet erzeugen.

## Zielartefakte außerhalb Bureau

Diese Artefakte müssten in Cabinet entstehen, bevor Bureau mehr als Planung tun sollte:

```text
pruefung/00 Signale/ecosystem-signals.jsonl
registry/ecosystem/claims.jsonl
registry/ecosystem/bureau-bridge.json
```

Minimales Signalobjekt:

```json
{
  "schemaVersion": 1,
  "kind": "ecosystem_signal",
  "id": "signal:repo:fixture:pr-state:example",
  "observedAt": "2026-07-05T00:00:00Z",
  "sourceSystem": "github",
  "subject": "repo:lenskit",
  "predicate": "has_open_pr",
  "object": "true",
  "evidence": [
    {
      "type": "github_pr",
      "ref": "heimgewebe/lenskit#887",
      "observedHeadSha": "<sha>"
    }
  ],
  "freshness": {
    "basis": "observedAt",
    "maxAgeHours": 24
  },
  "confidence": 0.8,
  "doesNotEstablish": [
    "task_approval",
    "merge_readiness",
    "runtime_correctness",
    "claim_truth"
  ]
}
```

## Bureau-Anschluss

Bureau darf die Signale erst konsumieren, wenn Cabinet daraus einen Bridge-kompatiblen Kandidaten macht. Minimum:

- `status` ist `evidenced`, `approved` oder explizit menschlich freigegeben;
- Evidence verweist auf Primärquelle und beobachteten Zeitpunkt;
- Frische kommt aus `observedAt`, nicht aus einem Batch-`expires_at` allein;
- `next_action` ist eine Review- oder Klärungsaktion, kein automatischer Ausführungsbefehl;
- `responsible_organ` ist gesetzt;
- Bureau-Preview bleibt `proposal_only`.

## Phasen

### P0 — Plan hinterlegen

Diese Datei existiert im Bureau-Repo. Keine Logik ist aktiv.

Akzeptanz:

- Plan ist versioniert.
- Kein Timer liest Cabinet automatisch.
- Kein Bureau-Task wird erzeugt.
- Keine Queue oder Runtime wird verändert.

### P1 — Cabinet-Signalcontract prüfen

Cabinet definiert oder ergänzt das Signalformat und validiert es in CI.

Akzeptanz:

- JSONL-Format ist maschinenprüfbar.
- Signale haben `observedAt`, Primärquelle, Evidence und Frischebasis.
- Batch-datierte Claims werden nicht als echtes Freshness-Signal missverstanden.

### P2 — Ein Repo als Fixture

Ein Repo wird als Testfläche gewählt. Empfehlung: `lenskit`, wenn dort ein aktueller PR-/Review-/CI-Fall sichtbar ist; sonst `weltgewebe`, weil dort CI- und Deploy-Signale hohen Nutzen haben.

Akzeptanz:

- maximal ein Repo im ersten Slice;
- maximal drei Signaltypen;
- keine Generalisierung auf alle Repos;
- Signale bleiben read-only.

### P3 — Bureau Preview

Bureau liest nur den fertigen Cabinet-Bridge-Kandidaten oder einen Probe-Report. Es importiert nichts.

Akzeptanz:

- Preview erzeugt keine Registry-Task-Datei;
- Effektflags bleiben false;
- blocked/admissible wird erklärt;
- fehlende Frische oder Evidence blockiert.

### P4 — Review Gate vor Import

Erst nach bewerteter Preview darf ein separater Import- oder Task-Candidate-Slice geplant werden.

Akzeptanz:

- menschliche Freigabe bleibt sichtbar;
- high-risk Kandidaten werden nicht automatisch operationalisiert;
- Bureau darf Priorität begründen, aber Cabinet-Claims nicht zur Wahrheit erheben.

## Risiken und Gegenmittel

| Risiko | Folge | Gegenmittel |
|---|---|---|
| Scope creep auf viele Repos | Radar wird Orchestrator | exakt ein Fixture-Repo |
| Frische nur aus `expires_at` | falsches Freshness-Gefühl | `observedAt` als Primärdatum |
| Cabinet-Claim ersetzt GitHub/CI | falsche Wahrheitshierarchie | Primärquelle im Evidence-Objekt |
| Bureau importiert zu früh | Schatten-Dispatch | proposal-only und Effektflags false |
| Signalrauschen | schlechte Priorisierung | wenige Signaltypen, explizite Confidence |

## Nutzen

- Cabinet bekommt echte Wahrnehmung statt Selbstbeschreibung.
- Bureau bekommt bessere Kandidaten statt Meta-Claims.
- Review- und Merge-Gates können später auf beobachtete Zustände reagieren.
- Heimlern bekommt später Outcome-Daten mit Quelle, Frische und Fehlerklasse.

## Epistemische Leerstellen

- Der aktuelle lokale Working Tree von Cabinet fehlt; nötig für Driftprüfung.
- Der aktuelle lokale Working Tree von Bureau fehlt; nötig für konfliktfreie lokale Ausführung.
- Das externe RepoBrief-/Lenskit-Manifest fehlt weiterhin; nötig für echte Dump-Freshness.
- Das konkrete Fixture-Repo ist noch nicht endgültig gewählt; nötig für Akzeptanztests.

## Erster Bureau-kompatibler Taskkandidat

```text
BUR-CAB-SIG-001: Review Cabinet read-only ecosystem signal ingest fixture
```

Ziel:

```text
Prüfen, ob Cabinet mit einem Fixture-Repo echte GitHub-/CI-/Worktree-Signale read-only als frische, belegte Claims erzeugen kann, ohne Bureau-Task-Erzeugung oder Dispatch auszulösen.
```

Nicht sofort ausführbar, bis Cabinet den Signalcontract oder ein Probe-Artefakt liefert.

## Kurzform

Bureau soll hier nicht schneller laufen. Bureau soll besser sehen, wann Laufen überhaupt sinnvoll ist. Der nächste Hebel ist nicht mehr Governance, sondern kontrollierte Wahrnehmung.
