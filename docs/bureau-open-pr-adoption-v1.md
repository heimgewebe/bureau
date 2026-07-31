# Bureau Open-PR-Adoption v1

## Zweck

Der Open-PR-Claim-Guard verhindert parallele Repository-Schreibarbeit, solange bereits
eine offene Pull Request denselben Bereich belegt. Das schützt vor doppelter Umsetzung,
inkonsistenten Reviews und konkurrierenden Mergepfaden.

`open_pr_adoption` ergänzt genau eine enge Ausnahme: Ein separater Merge-Task darf eine
bereits implementierte offene Pull Request übernehmen, wenn deren Identität vollständig
gebunden ist und der zugehörige Implementierungstask bereits revisionsgebunden verifiziert
wurde. Die Ausnahme gilt nur für die Konfliktklassifikation. Sie führt keinen Merge aus und
erteilt keine Merge-, Review-, CI-, Deployment- oder Lease-Autorität.

## Rollen

Eine Adoption besteht aus zwei getrennten Bureau-Tasks:

1. Der **Implementierungstask** beschreibt und verifiziert den bereits in der Pull Request
   enthaltenen Code. Die Pull Request muss gültig und ausschließlich an diesen Task
   gebunden sein.
2. Der **Merge-Task** hängt vom verifizierten Implementierungstask ab. Nur er trägt den
   exakten Adoptionsvertrag und eine einzelne exklusive Repository-Claim.

Diese Trennung verhindert, dass ein Task seine eigene Implementierung als abgeschlossen
voraussetzt oder Implementierung und Merge in einer unprüfbaren Wirkung zusammenfallen.

## Versionierter Vertrag

Der Merge-Task enthält unter `metadata.open_pr_adoption` exakt diese Felder:

```json
{
  "schema_version": 1,
  "repository": "heimgewebe/example",
  "pull_request": 123,
  "base_sha": "0123456789abcdef0123456789abcdef01234567",
  "head_sha": "89abcdef0123456789abcdef0123456789abcdef",
  "implementation_task": "EXAMPLE-V1-T001",
  "merge_task": "EXAMPLE-V1-T002"
}
```

Zusätzliche, fehlende oder typfalsche Felder machen den Vertrag ungültig. `base_sha` und
`head_sha` sind vollständige 40-stellige Git-Objekt-IDs; abgekürzte oder bewegte
Revisionen werden nicht akzeptiert.

## Zulässigkeitsbedingungen

Die Reservierung wird nur als `merge-adopted` klassifiziert, wenn gleichzeitig gilt:

- GitHub-Beobachtung und vollständiger Dateiscope der Pull Request sind erfolgreich.
- Die Pull Request beansprucht genau ein Repository und keine zusätzlichen Repository-
  Ressourcen.
- Repository, PR-Nummer, Base-SHA und Head-SHA stimmen exakt mit dem Vertrag überein.
- Die Pull Request ist gültig an genau den benannten Implementierungstask gebunden.
- Der Implementierungstask existiert und ist im aktuellen Registry-/State-Overlay
  `verified`.
- Der Implementierungstask ist eine Dependency des Merge-Tasks.
- `merge_task` entspricht exakt dem aktuellen Task.
- Der Task verwendet `repository_mutation`.
- Der Task besitzt genau eine konfliktfähige Claim: `mode: exclusive`, `amount: 1`,
  `isolation: worktree` auf das Repository der Pull Request.

Jede verletzte Bedingung lässt den bestehenden Open-PR-Blocker unverändert bestehen.

## Projektion und Revalidierung

`frontier`, `explain-next`, `claim-intent` und `claim-commit` verwenden dieselbe
Adoptionsklassifikation. Die Projektion zeigt den reservierten PR-Eintrag als
`merge-adopted` und behauptet ausdrücklich keine Merge-Bereitschaft.

Vor dem Claim-Commit werden offene Pull Requests und Task-Overlays erneut gelesen. Drift
bei Head, Base, Nummer, Bindung, Vorgängerzustand oder Zusatz-PR macht den Claim
wirkungslos ungültig. Ein vorher erzeugter Intent kann dadurch keine veraltete Ausnahme
konservieren.

## Unveränderte Sicherheitsgrenzen

Die Adoption verändert nicht:

- GitHub-Review- oder CI-Pflichten,
- Branchschutz und Merge-Gates,
- Approval-, Capability- oder Lease-Prüfungen,
- Queue- und Registry-Autorität,
- Rebase-, Force-Push-, Auto-Merge- oder Deployment-Autorität,
- die Behandlung weiterer offener Pull Requests,
- fail-closed Verhalten bei Beobachtungs- oder Scopefehlern.

Der eigentliche Merge bleibt eine getrennte, kurzlebige, revisionsgebundene Operation mit
frischem GitHub-, Review-, CI- und Lease-Readback.

## Negativfälle

Regressionen decken mindestens ab:

- falscher oder unverifizierter Implementierungstask,
- fehlende Dependency,
- nicht exklusive oder zusätzliche Repository-Claims,
- falsche PR-Nummer,
- Base- oder Head-Drift,
- unbekannte Vertragsfelder,
- zusätzliche offene Pull Request,
- unvollständige GitHub-Beobachtung,
- Drift zwischen Claim-Intent und Claim-Commit.

Damit bleibt die allgemeine Regel: Offene Pull Requests blockieren Repository-Schreibarbeit.
Nur der exakt belegte, getrennte Merge-Task darf genau seine eine gebundene Reservierung
adoptieren.
