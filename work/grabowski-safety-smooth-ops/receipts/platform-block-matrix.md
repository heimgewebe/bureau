# Platform Block Matrix

Created: 2026-06-29T16:05:00+02:00

## Beobachtungen

| Fall | Ergebnis | Einordnung |
|---|---|---|
| `grabowski_task_reconcile(auto_resume=false)` | erfolgreich | Lokaler Reconcile-Code funktioniert. |
| `grabowski_task_list` mit explizitem `state: null` | blockiert | Hinweis auf Plattform-/Serialisierungsheuristik. |
| `grabowski_task_list` ohne optionales `state` | erfolgreich | Optionalfelder besser weglassen als null senden. |
| breite Terminal-Diagnose mit grep/find | teils blockiert | Dedizierte read-only Tools sind stabiler. |
| Google-Drive-Link per rclone-ID | 404 / nicht sichtbar | Drive-Konto/API sieht Datei nicht; Chat-Artefakt wurde als Ausweichpfad genutzt, direkter großer Transfer wurde jedoch abgeschnitten. |

## Schluss

Blockaden sind Diagnoseereignisse. Antwort ist nicht Rechteausweitung, sondern engere Toolsemantik: read-only check, begrenztes refresh, explizites resume.
