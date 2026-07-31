# BUREAU-TRUTH-MODEL-V2-T021 – Heartbeat-CAS-Pilot

## Entscheidung

`implement-pass` für den engen `StateStore.heartbeat`-Piloten.

Der erfolgreiche Heartbeat prüft Run-Identität, aktiven Zustand und – falls übergeben – den erwarteten Worker nun im Prädikat eines bedingten SQLite-Updates. Erst genau ein Treffer erlaubt Worker-Heartbeat, ein `run-heartbeat`-Event und den unveränderten autoritativen Readback einschließlich Reservierungen.

Ein Nulltreffer führt ausschließlich zu einem schmalen Diagnoseread auf `state, worker_id`. Unbekannter Run, terminaler Run und falscher Worker erzeugen keine Run-, Worker- oder Eventmutation.

## Revisionsbindung

| Gegenstand | Wert |
|---|---|
| Basis | `face8f8b80bcb98e75fcc6c72120a7fcc2e61c46` |
| Implementierungscommit | `ddeada4fb2284f577e310deae709771dda4300a7` |
| Bureau-Run | `BUR-RUN-20260731T121044Z-3da51e863b` |
| Worker | `chatgpt-bureau-truth-model-v2-t021-heartbeat-cas-20260731` |
| Externe Bindung | `chatgpt/bureau-t021-heartbeat-cas-20260731` |
| Workspace | `/home/alex/repos/.bureau-worktrees/BUR-RUN-20260731T121044Z-3da51e863b` |

## Vertragswirkung

### Erfolgsweg

1. `BEGIN IMMEDIATE` reserviert den einzelnen SQLite-Writer.
2. `UPDATE runs ... WHERE run_id=? AND state IN (...) [AND worker_id=?]` muss genau eine Zeile treffen.
3. Der Worker-Heartbeat wird aus dem autoritativen Run-Owner abgeleitet.
4. Genau ein `run-heartbeat`-Event wird geschrieben.
5. `StateStore.run` liefert den vollständigen autoritativen Readback einschließlich Reservierungen.

### Verliererweg

Bei null Treffern unterscheidet ein begrenzter Diagnoseread:

- unbekannter oder nicht aktiver Run → `run ... is not active`;
- aktiver Run mit anderem Worker → `worker does not own this run`;
- unerwartete sonstige Prädikatsverletzung → fail-closed `heartbeat precondition failed`.

Alle Fehler verlassen die unmittelbare Transaktion per Rollback. Tests belegen unveränderte Run- und Worker-Zeitstempel sowie unveränderte Eventanzahl.

## Messung

Hermetischer, balancierter In-Memory-SQLite-Kontrast mit jeweils 10.000 erfolgreichen Heartbeats:

| Kennzahl | Vorher | CAS | Wirkung |
|---|---:|---:|---:|
| vollständige `runs`-Zeilenlesungen gesamt | 20.000 | 10.000 | −50 % |
| vollständige `runs`-Zeilenlesungen je Erfolg | 2,0 | 1,0 | −50 % |
| p95 | 26.010 ns | 28.060 ns | +7,88 % |

Grenze: höchstens +10 % p95. Ergebnis: `pass`.

Die Messung belegt die SQL-Pfad-Hypothese, nicht automatisch einen produktiven End-to-End-Latenzgewinn. Reale Dateisystem-, WAL-, Scheduler- und Konfliktverteilungen sind darin nicht enthalten.

## Prüfbelege

- Fokustests: `5 passed`; Job `grabowski-job-40afcd453bc2`; Finalisierungsreceipt `304f04212cd8e98b228db05ecf41acc491e361cf5a8a3942779c0a59ecb10833`.
- 10.000er Benchmark: Job `grabowski-job-f511bcb8048f`; Finalisierungsreceipt `b5de0aee458b0f8cbb97c78026eb37286c45e9a29a7a674cec0c583b11d4b1b3`.
- Vollvalidierung: Ruff, 951 Tests, Systemkatalog-Grenze und Registry-Check bestanden; Job `grabowski-job-2f1d6abb9b71`; Finalisierungsreceipt `2bda5efe05a7436d6ab394f3981cd635dca233f07984bcb7877121889d39fdc3`.
- State-DB: Integrität `ok`, keine Fremdschlüsselfehler, Schema 3.
- Scope: keine breiten Bureau-Scope-Befunde; offener PR #1268 war revisionsgebunden pfaddisjunkt.

## Akzeptanzzuordnung

- `existing-contract-audit`: bestehender Vorlese-, Mutations-, Event- und Readbackpfad wurde gegen aktuellen Main geprüft; `complete_run` und `fail_run` blieben bewusst unverändert.
- `measured-read-reduction`: 2 → 1 vollständige Run-Zeilenlesungen je erfolgreichem Heartbeat, exakt 50 %.
- `exact-preconditions`: Run-ID, aktiver Lifecycle-Zustand und optional erwarteter Worker liegen im atomaren Update-Prädikat; Nulltreffer mutieren nichts.
- `no-change-closeout`: nicht anwendbar, da die festgelegte Nutzen- und p95-Grenze erreicht wurde.

## Begrenzung

Dieser Pilot erweitert nicht die öffentlichen Verträge von `complete_run` oder `fail_run` und führt weder einen zweiten Wahrheitsstore noch einen neuen Idempotenzkanal ein. Aus dem Ergebnis darf keine allgemeine CAS-Härtung aller Bureau-Zustandsbefehle abgeleitet werden.
