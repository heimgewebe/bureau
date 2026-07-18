# Bureau operator-native intake v1

Stand: 2026-07-18

## Rolle und Zweck

ChatGPT über Grabowski ist der ausführende Operator. Der Nutzer bleibt Beobachter und Steuermann. Diese Oberfläche übernimmt daher Kandidatenaufnahme, Bewertung, Task-Vorschlag und kontrollierte Veröffentlichung maschinell. Sie verlangt im Erfolgsweg keine Shell-Befehle, Dateisuche, manuelle Registry-JSON-Bearbeitung oder Statusaggregation durch den Nutzer.

Der Domänenkern liegt ausschließlich in `bureau.operator_intake`. CLI und künftige typisierte Grabowski-Werkzeuge sind dünne Adapter. Es entsteht keine zweite Task-, Queue-, Claim-, PR- oder Approval-Wahrheit.

## Vier Operationen

### 1. `operator-candidate-record`

Ein versionierter JSON-Request wird append-only in den bestehenden Live Register geschrieben.

Pflichtfelder:

- `schema_version: 1`
- `idempotency_key`
- `title`
- `source_kind`
- `desired_outcome`

Optionale Bindungen sind Repository, Task, Kandidaten-ID, Source-Locator, Source-SHA-256, Beobachtungszeit und Notiz. Unbekannte Felder werden abgelehnt. Derselbe Idempotenzschlüssel mit denselben Eingaben liefert die vorhandene Identität; abweichende Eingaben erzeugen `idempotency-conflict`.

Die Aufnahme begründet keine Registry-, Queue-, Readiness-, Claim- oder Dispatch-Wahrheit. Sie ist wie `live-register` ein Always-on-State-Store-Append und darf den kanonischen read-only Registry-Snapshot zur strikten Katalogvalidierung lesen.

### 2. `operator-candidate-assess`

Die Bewertung ist read-only und liefert:

- Source-Freshness und Katalogvalidierung;
- exakte Duplikatbefunde über Kandidaten-ID, Source-Digest oder vorhandene Task-ID;
- höchstens fünf deterministische Ähnlichkeitshinweise;
- Zielinitiative, vorgeschlagene Claims, Risiko- und Approval-Verträge;
- fehlende Felder;
- eine Entscheidung `promote`, `merge`, `refine`, `defer` oder `drop`.

Ähnlichkeit ist ausschließlich beratend. Sie darf nie automatisch mergen, schließen, unterdrücken oder Registry-Wahrheit verändern.

### 3. `operator-task-propose`

Der Vorschlag bindet:

- die aktuelle Kandidaten-ID und das aktuelle Event;
- Source-Provenienz;
- den exakten Registry-Commit und Registry-Tree;
- den registrierten `publishing_task_id`;
- vollständiges Task-JSON und dessen Hash;
- den gerenderten Task-Dateihash;
- den kanonischen Ein-Datei-Änderungsdigest;
- Assessment, ungelöste Felder und Reviewstatus.

Die Zieldatei muss neu sein. Initiative, Abhängigkeiten, Claims, Capabilities und Acceptance werden gegen die Registry geprüft. Generische Legacy-Acceptance wird ohne explizite Begründung abgelehnt. Die Plan-Datei wird create-only geschrieben.

Ein Vorschlag verändert weder Registry noch Queue.

### 4. `operator-task-publish`

Ohne `--apply` ist der Aufruf eine wirkungsfreie Vorschau. Sie prüft Planintegrität, Reviewbindung, Approval, Registry- und Kandidatendrift, Task-Schema und ungelöste Felder. Sie liefert genau zwei benötigte Ressourcen:

- die neue Task-Datei;
- das kurze Gate `path:/home/alex/repos/bureau/.bureau-scopes/registry-publication`.

Der Effektpfad akzeptiert keine angelieferten Lease-Snapshots als Autorität. Bureau liest die private Grabowski-Resource-Datenbank selbst read-only und prüft:

- unterstütztes DB-Schema und private Datei;
- denselben Owner für beide Ressourcen;
- Bindung an den registrierten `publishing_task_id`;
- vollständige exakte Ressourcenschlüssel;
- gültige Zeit- und Metadatenfelder;
- mindestens 60 Sekunden Restlaufzeit;
- höchstens 300 Sekunden Gesamtlaufzeit des Publication-Gates.

Danach erstellt der Standardpublisher einen isolierten Checkout am exakten Registry-Basiscommit, schreibt nur die eine Task-Datei, validiert die gesamte Registry, committet, prüft Remote-Main erneut, publiziert einen neuen Branch und legt einen PR an. Erfolg erfordert GitHub-Readback von offenem PR, Branch, Base und exaktem Head.

Der Publisher merged nicht. Er queued, claimt, dispatcht, deployt und verifiziert den neuen Task nicht.

## Fehlervertrag

`OperatorIntakeError` liefert stabil:

- `code`
- `retryable`
- `effect_started`
- `ambiguity`
- `required_readback`
- `details`

Ein unbekannter Fehler nach möglichem Push oder PR-Effekt wird nicht blind wiederholt. Er wird als `publication-unclear` mit erforderlichem Remote-Branch-, PR- und Task-Datei-Readback ausgegeben.

## Idempotenz und Receipts

Ein erfolgreicher Effekt schreibt ein create-only Receipt mit Proposal-, Plan-, Registry-, Task-, Lease-, Branch-, PR- und Readback-Bindung. Ein identischer Wiederholungsaufruf liefert dieses Receipt auch nach späterem Registry-Fortschritt erneut, ohne Leases oder Publisher erneut zu benutzen. Manipulierte oder fremde Receipts werden abgelehnt.

## CLI als Transport

Beispiel für die Aufnahme:

```bash
bureau --json operator-candidate-record --request candidate-request.json
```

Bewertung:

```bash
bureau --json operator-candidate-assess --candidate-id candidate-...
```

Vorschlag in einem expliziten sauberen Registry-Checkout:

```bash
bureau --root /path/to/clean/bureau --json operator-task-propose \
  --candidate-id candidate-... \
  --task-json task.json \
  --publishing-task-id OPERATOR-MACHINE-READABILITY-V1-T017 \
  --write-plan proposal.json
```

Wirkungsfreie Veröffentlichungsvorschau:

```bash
bureau --root /path/to/clean/bureau --json operator-task-publish \
  --plan proposal.json --preview
```

Effekt nach Review und Grabowski-Lease-Akquise:

```bash
bureau --root /path/to/clean/bureau --json operator-task-publish \
  --plan proposal.json --apply \
  --lease-binding lease-binding.json \
  --workspace-root /path/to/operator-publications \
  --receipt /path/to/receipt.json
```

`lease-binding.json` enthält nur Owner und registrierte Publishing-Task-ID. Die tatsächlichen Leases werden nicht daraus geglaubt, sondern live aus Grabowskis privater Resource-Datenbank gelesen.

## Nichtbehauptungen

Diese Oberfläche begründet nicht:

- automatische semantische Duplikaterkennung als Wahrheit;
- Queue-, Readiness-, Claim- oder Dispatch-Autorität;
- Merge-, Deployment- oder Verifikationsautorität;
- ein Recht, Kosten-, Sicherheits-, Datenschutz-, Irreversibilitäts- oder Steuergates zu umgehen;
- die Abwesenheit gleichberechtigter Eingriffe außerhalb der belegten Git-, Lease- und Readback-Grenzen.
