# Bounded Bureau contract audit: review-before-effect approval

Status: no-change closeout

Datum: 2026-08-08

Task: `OPERATOR-INTEGRATION-LOOP-V1-FB-AUDIT-CB066AD5E2`

Run: `BUR-RUN-20260808T170825Z-729638cea3`

## Auditgegenstand

Geprüft wurde der enge Freigabevertrag für `interactive-agent/review-before-effect` im koordinierten Bureau-Claim-Pfad:

> Eine explizite Operatorfreigabe darf nur den vorhandenen Review-Grund entfernen. Alle übrigen Blocker – insbesondere fehlende Capabilities, Abhängigkeiten, aktive Limits, Ressourcen-/Lease-Konflikte, offene-PR-Konflikte, Dirty-/Fremdzustand und Runtime-Drift – müssen unverändert wirksam bleiben.

Das Audit umfasst Dokumentation, aktuelle Registry-/Frontier-Wahrheit, den live verwendeten Runtime-Stand, aktuellen `main`, den Claim-/Commit-Code und gezielte Negativtests.

## Revisionsbindung

- beim Claim kanonischer Bureau-Runtime-/Registry-Release: `72462b4b0403b30ce131869c051ad6432043a537`
- Audit-Worktree-Baseline und beim ersten Audit-Readback live gelesener GitHub-`main`: `40b5391da104403533548092599ce224abd745b0`
- beim Closeout live deployter Runtime-Release und GitHub-`main`: `828d59d612cd5a9b36ac8d7c38bef4fb55939053`
- Worktree: `/home/alex/repos/.bureau-worktrees/BUR-RUN-20260808T170825Z-729638cea3`
- Branch: `bureau/operator-integration-loop-v1-fb-audit-cb066ad5e2/729638cea3`
- Task-SHA-256: `dff4c5355a4a36a62f7bebbb354499b74e562e19afe51dfc3c4c1a260e18bd21`
- Plan-SHA-256: `12662f2ed585335286d4a73696388d05db725bb058550f810fb5478c03cb6ab2`

Der gezielte Git-Diff zwischen Runtime-Release und aktuellem `main` war für alle auditierten Dateien leer:

- `src/bureau/approval.py`
- `src/bureau/v2.py`
- `docs/bureau-ready-supply-fallback-v1.md`
- `docs/bureau-approval-path-v1.md`
- `tests/test_approval.py`
- `tests/test_v2.py`
- `tests/test_claim_guard.py`

Damit betrifft die Revisiondifferenz zwischen Runtime und Repository den hier geprüften Vertrag nicht.

## Methode und Evidenzquellen

1. Grabowski-Runtime-Health und Bootstrap live gelesen; Runtime gesund, Deploymentintegrität gültig, Kill-Switch aus.
2. Kanonischen Bureau-Checkout nicht als Arbeitswahrheit benutzt: `/home/alex/repos/bureau` war 458 Commits hinter `origin/main` und enthielt unverfolgten Zustand.
3. Canonical Registry/StateStore-Frontier über den releasegebundenen Control-Worktree gelesen: `eligible_count=0`, State-Integrität `ok`.
4. Den bounded fallback `OPERATOR-INTEGRATION-LOOP-V1-FB-AUDIT-CB066AD5E2` gezielt bewertet: `ready`, Scope `component.bureau.docs`, einziger Freigabegrund `execution is interactive-agent/review-before-effect`.
5. Den ersten Claim mit explizit falschem/stalem Registry-Root absichtlich nicht weiterverwendet; Grabowski blockierte vor Wirkung mit `release-registry-identity-mismatch`.
6. Den Claim anschließend über die kanonische Runtime-Registry ausgeführt. Grabowski erzeugte einen eigenen Worktree und exakte Leases für `docs`/`docs/evidence`.
7. Open PR `heimgewebe/bureau#1808` live geprüft. Der Claim belegte `scope-proven-disjoint` mit `overlap_count=0` gegenüber dem Audit-Scope; PR #1808 war beim Readback offen und mergeable.
8. Vertragstext, `approval_decision()`, `task_approval_contract()`, `claim_intent()` und `commit_claim_intent()` direkt gelesen.
9. Vier gezielte Negativ-/Bindungstests ausgeführt. Der erste Runner-Versuch scheiterte ausschließlich am fehlenden `PYTHONPATH` (`ModuleNotFoundError: bureau`); der korrigierte, inhaltlich identische Testlauf lief 4/4 grün.

## Belegt

### 1. Freigabe entfernt nur den exakten Review-Grund

`claim_intent()` berechnet zunächst die vollständigen Gründe. Nur wenn der exakte String für `interactive-agent/review-before-effect` vorliegt und eine gebundene Freigabe vorhanden ist, wird genau dieser Grund aus der Liste entfernt. Eine Auswahl erfolgt anschließend nur, wenn `reasons` vollständig leer ist.

Folge: Eine Freigabe kann keinen zweiten, unabhängigen Blocker verschwinden lassen.

### 2. Die Freigabe selbst ist eng gebunden

`approval_decision()`/`require_approval()` verlangen bei effectful actions eine vorhandene und positive Freigabe mit zulässigem Level sowie – wenn erwartet – passender Referenz, Task-ID und Scope. Abweichungen failen geschlossen.

Im live erzeugten Claim war die Freigabe an Run, Task, Reviewer und `repository_mutation` gebunden.

### 3. Der Commit-Schritt revalidiert statt dem Intent blind zu vertrauen

`commit_claim_intent()` prüft unter anderem erneut:

- Intent-Digest und gespeicherte Issuance,
- Ablaufzeit,
- Runtime-Wahrheit,
- aktuelle Task-Identität,
- Approval-Reviewer, Scope, Run-Referenz und Task-ID,
- aktuelle Open-PR-Scope-Evidenz,
- exakte Ressourcenmenge,
- Task-SHA,
- Plan-SHA,
- geplanten Workspace.

Drift nach dem Intent führt zum Abbruch.

### 4. Liveverhalten bestätigt die Trennung

Vor der Freigabe war der gewählte Docs-Fallback nur durch `review-before-effect` blockiert. Andere aktuelle Core-Fallbacks enthielten zusätzlich einen offenen-PR-Konflikt mit PR #1808 und blieben daher trotz derselben möglichen Freigabeklasse nicht ausführbar.

Der korrigierte Docs-Claim wurde dagegen nur nach nachgewiesener Pfad-Disjunktheit akzeptiert.

### 5. Gezielte Regressionstests sind grün

Korrigierter Testlauf:

```text
....                                                                     [100%]
```

Geprüft wurden:

- fehlende explizite Freigabe blockiert Repository-Mutation;
- koordinierter Claim-Intent benötigt Freigabe und erzeugt vor Commit keinen Run;
- breite Repository-Lease wird durch exakte Task-Pfade ersetzt;
- gebundene Open-PR-Nichtkonflikt-Evidenz wird bei PR-Head-Drift abgelehnt.

## Plausibel, aber nicht vollständig bewiesen

Die Kombination aus exakter Reason-Filterung, approval-bound Intent, erneuter Commit-Validierung und Negativtests macht einen unbeabsichtigten breiten Freigabe-Bypass innerhalb dieses koordinierten Claim-Pfads unwahrscheinlich. Das Audit beweist jedoch nicht automatisch alle anderen heutigen oder zukünftigen Bureau-Mutationsoberflächen.

## Ungeklärt / Grenzen

- RepoGround konnte für Bureau keinen aktuellen verwertbaren Textindex liefern (`fresh_dirty_unverified`/fehlender Index). Deshalb wurde RepoGround nicht als Evidenzquelle gewertet; stattdessen wurden der isolierte aktuelle Worktree, der kanonische Runtime-Snapshot, GitHub-`main`, Live-Frontier und Tests direkt gelesen.
- Dieses Audit erteilt keine Merge-, Deployment- oder globale Runtime-Mutationsautorität.
- Der auditierte Vertrag ist revisionsgebunden; spätere Änderungen an den genannten Dateien benötigen eine neue Prüfung.

## Post-Audit-Runtime-Drift

Während des laufenden Audits wurde Bureau weiterentwickelt: PR `heimgewebe/bureau#1808` wurde gemergt; beim Closeout zeigten Deployment-Manifest und GitHub-`main` beide auf `828d59d612cd5a9b36ac8d7c38bef4fb55939053`. Der bereits laufende Audit-Run blieb gemäß Journal korrekt an seinen ursprünglichen Claim-Snapshot `72462b4b0403b30ce131869c051ad6432043a537` gebunden.

Dieser Drift wurde nicht ignoriert:

- PR #1808 änderte unter den auditierten Dateien `src/bureau/v2.py` und `tests/test_v2.py`; `src/bureau/approval.py` sowie die beiden Vertragsdokumente blieben unverändert.
- Die neue Runtime wurde direkt gelesen. `claim_intent()` entfernt weiterhin nur den exakten `interactive-agent/review-before-effect`-Grund und wählt nur bei anschließend leerer Reason-Liste.
- `commit_claim_intent()` revalidiert weiterhin die übrigen Eligibility-Gründe; #1808 ergänzt zusätzlich revisions-, Zustands-, Attempt- und Idempotenz-CAS-Bindungen. Diese Änderungen härten den Claim-Pfad, statt die Freigabe zu verbreitern.
- Derselbe Vierer-Negativtest wurde zusätzlich mit `PYTHONPATH` auf den deployten `828d59d...`-Quellbaum ausgeführt und lief erneut 4/4 grün.

Damit gilt das No-Change-Urteil sowohl für den revisionsgebundenen Claim-Stand als auch für den beim Closeout aktuellen Runtime-Stand.

## Urteil

**Keine Reparatur erforderlich.** Dokumentierter Vertrag, aktueller Quellcode, live ausgeführter Runtime-Pfad und gezielte Regressionstests stimmen auf der geprüften Invariante überein.

Eine künstliche Codeänderung würde hier nur Diff- und Regressionsrisiko erzeugen, ohne einen belegten Fehler zu beheben. Der korrekte Abschluss ist daher ein No-Change-Audit mit dieser reproduzierbaren Evidenz.

## Alternative Sinnachse

Wenn maximale Beweisbreite höher gewichtet wird als Durchsatz, wäre ein Volltest aller Bureau-Claim-/Approval-Surfaces sinnvoll. Für den bounded fallback ist das nicht erforderlich: Sein Vertrag verlangt einen aktuellen, abgegrenzten Contract-Audit und eine Negativregression oder eine begründete No-Change-Entscheidung, nicht die Vollverifikation der gesamten Control Plane.

## Unsicherheit

Unsicherheit: `0.07`. Ursache: direkter Live-Claim, direkte Runtime-/Git-Reads, leerer relevanter Revisionsdiff und gezielte grüne Tests; Restunsicherheit bleibt für nicht auditierte Mutationsoberflächen.

Interpolationsgrad: `0.05`. Nahezu alle Aussagen stammen aus Primärzustand oder direkt ausgeführtem Verhalten; nur die Aussage zur allgemeinen Bypass-Wahrscheinlichkeit ist eine Systeminferenz.

## Typed receipt map

Der reproduzierbare strukturierte Readback liegt neben diesem Bericht in
`operator-integration-loop-v1-fb-audit-cb066ad5e2-typed-receipts-20260808.json`.
Er enthält die typisierten Grabowski-Runtime-/Pickup-Werte, den hashgebundenen
Bureau-Projektionsreadback und den exakt headgebundenen GitHub-PR-/CI-Zustand.
Die drei Acceptance-IDs `audit-1` bis `audit-3` sind dort einzeln auf konkrete
Evidenzpfade abgebildet. Damit sind die zuvor nur prosaisch referenzierten
Livebehauptungen reproduzierbar an strukturierte Werte und Digests gebunden.

Wichtig: Der spätere Projektionsreadback weist `claim_authority_established=false`
aus. Er wird deshalb ausschließlich als Read-only-Evidenz verwendet und erteilt
keine neue Claim-, Merge- oder Deploymentautorität.
