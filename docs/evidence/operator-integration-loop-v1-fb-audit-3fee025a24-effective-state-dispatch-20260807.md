# OPERATOR-INTEGRATION-LOOP-V1-FB-AUDIT-3FEE025A24 — Effective-State-Dispatch-Audit

Datum: 2026-08-07
Run: `BUR-RUN-20260807T101915Z-5ec325afbe`
Task: `OPERATOR-INTEGRATION-LOOP-V1-FB-AUDIT-3FEE025A24`
Baseline: `1219220bea8f7d29d0b9679b6a9b0a522cfd6e0d`
Arbeitsbereich: `docs/`, `docs/evidence/`
Entscheidung: **No-change closeout für Produktionscode; vorhandener Vertrag ist auf der geprüften Revision konsistent belegt.**

## Auditvertrag

Geprüft wurde der aktuelle Bureau-Vertrag für Dispatch-Oberflächen der Statusprojektion:

1. Registry-Zustand und effektiver Taskzustand aus dem StateStore bleiben getrennte Wahrheiten.
2. `repository_balls` darf einen Task mit terminalem **effektivem** Zustand nicht als aktuellen/dispatchbaren Ball projizieren, selbst wenn die Registry-Datei noch `ready` sagt.
3. `next_actions` darf für einen solchen Task keinen `claim-task`-Vorschlag erzeugen.
4. Die Statusprojektion bleibt read-only und erhält keine Task-, Claim-, Merge- oder Runtime-Autorität.

Diese Prüfung ist revisionsgebunden an `1219220bea8f7d29d0b9679b6a9b0a522cfd6e0d`. Der Commit enthält als direkten Vorfahren Merge-Commit `674d2fb457d47cdc3cfd81c8d24b1abe4d3d1ac8` von PR #1692 (`fix/status-projection-effective-state-v1`).

## Methode und Primärevidenz

### 1. Code auf der exakten Baseline

Datei: `src/bureau/status_projection.py`
SHA-256: `6ad70ab936e04e4fdb2e34eb287761afb752b2a86b9818d10cf15364f88bfe89`

Beobachtete Logik:

- `_repository_balls()` bildet zuerst die gequeue-ten Tasks ab und filtert anschließend mit `task.get("effective_state") not in TERMINAL_TASK_STATES`.
- Nur effektive `ready`-Tasks ohne Blocker können als `eligible_task` erscheinen.
- `_next_actions()` erzeugt `claim-task` ausschließlich für `effective_state == "ready"`.
- Die Projektion deklariert explizit, dass sie weder Taskabschluss noch Claim-, Merge-, CI- oder Runtime-Wahrheit etabliert.

### 2. Negativer Regressionstest

Datei: `tests/test_status_projection.py`
SHA-256: `80c94a9b10ee901dc0214c650e9782a146bf963d7bfcd4d0ebe3860b8910f017`

Test: `test_projection_uses_effective_state_for_dispatch_surfaces`

Der Test konstruiert absichtlich den Driftfall:

- Registry-Task bleibt `ready`.
- Der revisionsgebundene StateStore setzt denselben Task effektiv auf `verified`.
- Erwartet wird `registry_state == "ready"`, `effective_state == "verified"`.
- Der zugehörige Repository-Ball muss `empty` sein.
- `next_actions` darf keinen `claim-task` für diesen Task enthalten.

Gezielter Lauf:

`env PYTHONPATH=src python -m pytest -q -p no:cacheprovider tests/test_status_projection.py -k effective_state_for_dispatch_surfaces`

Ergebnis: **PASS (1 Test)**.

Grabowski-Job: `grabowski-job-726d872fdcef`
Argv-SHA-256: `f3a40b021e2709922fb40f71f8d002775c7975cc418620889c27f7a2f80fad00`
Finalisierungsreceipt: `ae863908a066facf11673007d0ae86ccbdd8946983ab614f2fa54d9393a7d742`
Finalstatus: `succeeded`, Exitcode `0`.

Hinweis: Ein vorheriger `uv run --frozen`-Versuch startete Pytest nicht, weil die Revision keinen `uv.lock` enthält. Ein zweiter ungebundener System-Python-Versuch scheiterte vor Testausführung an fehlendem `PYTHONPATH`. Beide sind Infrastruktur-/Aufruffehler und keine Produktfehler; der repo-eigene Makefile-Vertrag setzt ebenfalls `PYTHONPATH=src`.

### 3. Live-Runtime-Readback

Installierter Bureau-Aufruf am 2026-08-07T10:20:48Z:

`/home/alex/.local/bin/bureau --json status-projection --repo grabowski --skip-github`

Belegt:

- `healthy: true`
- `findings: []`
- kanonische Registry-Wurzel: `/home/alex/.local/share/bureau/registry-snapshots/1219220bea8f-tree90bfd157bda2`
- StateStore verfügbar: `/home/alex/.local/state/bureau/bureau.sqlite3`
- die eigene Audit-Ausführung wird unter `repo.bureau` als aktiver Run projiziert.
- die Autoritätsgrenze bleibt `deterministic_only`; LLM-Ausgaben sind `advisory_only`.

Zusätzlicher frischer Heim-PC-Readback am 2026-08-07T10:23:44Z:

- Registry-Head: `1219220bea8f`
- Registry-Ready: `39`
- effektiv Ready: `23`
- damit ist direkt belegt, dass Registry-Dateistatus und effektiver StateStore-Zustand nicht gleichgesetzt werden.
- `BUREAU-TRUTH-MODEL-V2-T027` war zu diesem Zeitpunkt selbst noch effektiv `ready`; sein gleichzeitiger Closeout durch einen fremden, gebundenen Operator wurde deshalb nicht als terminal interpoliert.

### 4. CI der zugrunde liegenden Änderung

PR #1692 wurde am Merge-Commit `674d2fb457d47cdc3cfd81c8d24b1abe4d3d1ac8` in die geprüfte Baseline aufgenommen.

Frischer GitHub-Readback am 2026-08-07:

- `validate (3.10)`: SUCCESS
- `validate (3.12)`: SUCCESS
- `registry-registration-preflight/freshness`: SUCCESS
- CodeQL: SUCCESS
- `Analyze (actions)`: SUCCESS
- `Analyze (python)`: SUCCESS
- `main-push-revalidation`: SKIPPED, kein Fehlstatus.

## Befunde

### Belegt

- Produktionscode nutzt für beide Dispatch-Oberflächen den **effektiven** Taskzustand.
- Ein expliziter negativer Regressionstest deckt den kritischen Registry-`ready`/StateStore-`verified`-Fall ab und besteht auf der aktuellen Baseline.
- Die installierte Runtime liest denselben kanonischen Registry-Head `1219220…` und meldet die Statusprojektion gesund.
- Live sind Registry-Ready (`39`) und effektiv Ready (`23`) verschieden; der Overlay-Vertrag ist also real wirksam und nicht nur Testfixture.
- Die zugrunde liegende Änderung ist gemergt und ihre Pflicht-/Sicherheitschecks sind grün.

### Plausibel, aber für diesen Audit nicht zusätzlich bewiesen

- Dass jede einzelne der 16 Registry-Ready/effektiv-nicht-Ready-Differenzen genau aus revisionsgültigen terminalen StateStore-Receipts stammt. Die Aggregatdifferenz belegt die Trennung, nicht die Ursache jedes einzelnen Tasks.
- Dass GitHub-Beobachtungen für jeden Task ebenfalls aktuell und eindeutig gebunden sind; der Live-Statusprobe wurde bewusst `--skip-github` gegeben. Die Aussage dieses Audits betrifft daher Dispatch aus Registry + StateStore, nicht vollständige PR-Wahrheit.

### Offen / nicht als Fehler gewertet

- `BUREAU-TRUTH-MODEL-V2-T027` erschien während des Audits weiterhin als effektives `ready` und als Now-Kandidat. Gleichzeitig existierte ein fremder, autoritativ gebundener Closeout-Run. Solange dessen terminaler Bureau-Readback noch nicht vorliegt, wäre es falsch, T027 als `verified` zu interpolieren. Das ist eine laufende Lifecycle-Frage, kein Gegenbeweis zum geprüften Filtervertrag.
- Die lokale Standard-Checkout-Kopie `/home/alex/repos/bureau` ist hinter `origin/main` und untracked-dirty. Sie wurde nicht als Autorität verwendet; maßgeblich waren der claim-gebundene Worktree, der kanonische Runtime-Snapshot und der StateStore.

## Entscheidung / No-Change-Begründung

Es gibt **keinen belegten Produktionscode-Gap** innerhalb des Auditscopes. Eine zusätzliche Codeänderung würde entweder den bereits vorhandenen Negativtest duplizieren oder in den gleichzeitig fremd geleasten T027-Scope eingreifen. Beides wäre schlechter als ein revisionsgebundener No-Change-Abschluss.

Acceptance `audit-3` ist damit über den vorhandenen negativen Regressionstest plus aktuellen PASS und die dokumentierte No-Change-Entscheidung erfüllt.

## Alternative Sinnachse

Wenn maximale Aktualitätsstrenge höher gewichtet wird als konfliktfreier Durchsatz, könnte man den Audit offenlassen, bis T027 selbst terminal readbackbar ist, und danach den Live-Probe wiederholen. Das erhöht Frische, schafft hier aber keinen zusätzlichen Codebeleg und würde einen unabhängig abgeschlossenen Audit unnötig an einen fremden Closeout koppeln.

Wenn Konfliktvermeidung und revisionsgebundene Reproduzierbarkeit höher gewichtet werden, ist der jetzige No-Change-Abschluss vorzuziehen: Der konkrete Vertrag ist auf `1219220…` durch Code, Test, Live-Runtime und CI belegt; die fremde T027-Lifecycle-Arbeit bleibt unberührt.

## Risiko und Unsicherheit

Nutzen: verhindert Doppelarbeit und konserviert einen reproduzierbaren Beleg für die neue Effective-State-Dispatch-Semantik.

Risiko: der Live-Probe ohne GitHub kann keine PR-Bindungsfehler ausschließen; außerdem kann T027 nach diesem Audit noch terminalisiert werden und damit die sichtbare Now-Lane ändern. Beides liegt außerhalb der geprüften Aussage.

Unsicherheit: **0,10** — Code, Regressionstest, Runtime-Head und CI sind direkte Primärevidenz; offen bleibt nur die gleichzeitig laufende T027-Terminalisierung.

Interpolationsgrad: **0,08** — fast alle Kernaussagen sind direkt gemessen. Die Aggregatdifferenz Registry-Ready/effective-Ready wird nicht auf einzelne Tasks herunterinterpretiert.

## Abschlussurteil

Der geprüfte Effective-State-Dispatch-Vertrag ist auf Baseline `1219220…` konsistent. Kein Produktionscode-Fix ist gerechtfertigt. Der richtige Abschluss dieses Fallback-Audits ist ein revisionsgebundener No-Change-Beleg; fremde T027-Leases und laufende Closeouts bleiben unangetastet.
