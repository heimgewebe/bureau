# Bureau operator-native intake v1

Stand: 2026-07-26

## Rolle und Zweck

ChatGPT über Grabowski ist der ausführende Operator. Der Nutzer bleibt Beobachter und Steuermann. Diese Oberfläche übernimmt daher Kandidatenaufnahme, Bewertung, Task-Vorschlag und kontrollierte Veröffentlichung maschinell. Sie verlangt im Erfolgsweg keine Shell-Befehle, Dateisuche, manuelle Registry-JSON-Bearbeitung oder Statusaggregation durch den Nutzer.

Der Domänenkern liegt ausschließlich in `bureau.operator_intake`. CLI und künftige typisierte Grabowski-Werkzeuge sind dünne Adapter. Es entsteht keine zweite Task-, Queue-, Claim-, PR- oder Approval-Wahrheit.

## Fünf Operationen

### 1. `operator-candidate-record`

Ein versionierter JSON-Request wird append-only in den bestehenden Live Register geschrieben.

Pflichtfelder:

- `schema_version: 1`
- `idempotency_key`
- `title`
- `source_kind`
- `desired_outcome`

Optionale Bindungen sind Repository, Task, Kandidaten-ID, `supersedes_event_id`, Source-Locator, Source-SHA-256, Beobachtungszeit und Notiz. Unbekannte Felder werden abgelehnt. Derselbe Idempotenzschlüssel mit denselben Eingaben liefert die vorhandene Identität; abweichende Eingaben erzeugen `idempotency-conflict`.

Eine append-only Korrektur oder Zustandsfortschreibung verwendet einen neuen Idempotenzschlüssel und bindet `supersedes_event_id` an das aktuelle Event des bestehenden Kandidaten. Das Feld ist Bestandteil des Request-Hashs. Kandidaten-ID, Repository, Task, Status und `promotion_required` werden vom Vorgänger geerbt, sofern die optionale Kandidaten-ID nicht ausdrücklich mit derselben Identität angegeben wird. Ein zuvor fehlendes Repository darf durch eine Refinement-Fortschreibung ergänzt werden; ein bereits gebundenes Repository darf nicht auf eine andere Ressource umgebunden werden. Task-Bindungen dürfen weiterhin weder neu gebunden noch geändert werden. Bei strikter Katalogvalidierung werden die effektiv geerbten Bindungen vor dem Append gegen den aktuellen Registry-Snapshot geprüft. Typfalsche, boolesche oder nichtpositive Event-IDs scheitern vor jeder Mutation; bereits supersedierte oder fremde Events werden durch den Live-Register-Vertrag abgelehnt.

Die Aufnahme begründet keine Registry-, Queue-, Readiness-, Claim- oder Dispatch-Wahrheit. Sie ist wie `live-register` ein Always-on-State-Store-Append und darf den kanonischen read-only Registry-Snapshot zur strikten Katalogvalidierung lesen.

### 2. `operator-candidate-assess`

Die Bewertung ist read-only und liefert:

- Source-Freshness und Katalogvalidierung;
- exakte Duplikatbefunde nur über dieselbe Kandidaten-ID oder eine explizit identische Task-ID;
- höchstens 20 gemeinsame Source-Digests als separate, ausschließlich beratende `source_relationships` mit Gesamtzahl und Trunkierungsindikator;
- höchstens fünf deterministische Ähnlichkeitshinweise;
- Zielinitiative, vorgeschlagene Claims, Risiko- und Approval-Verträge;
- fehlende Felder;
- eine Entscheidung `promote`, `merge`, `refine`, `defer` oder `drop`.

Ein gemeinsamer Source-Digest begründet allein keine Identitätsgleichheit. Kandidaten aus demselben Review-Artefakt bleiben unabhängig vorschlagsfähig, wenn Repository, gewünschtes Ergebnis oder explizite Task-Bindung abweichen. Idempotente Wiederholungen werden bereits bei der Aufnahme über Kandidaten-ID, Idempotenzschlüssel und Request-Hash abgefangen.

Ähnlichkeit ist ausschließlich beratend. Sie darf nie automatisch mergen, schließen, unterdrücken oder Registry-Wahrheit verändern.

Die Bewertung verlangt genau einen Selektor: aktuelle Kandidaten-ID, aktuelle Event-ID oder den ursprünglichen `idempotency_key`. Der Idempotenzselektor ist insbesondere der eindeutige Readback-Pfad nach einem unklaren Aufnahmeergebnis. Er liefert den aktuellen, gegebenenfalls supersedierenden Stand derselben Kandidatenidentität und begründet keine neue Mutation.

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

### 4. `operator-task-review`

Das Review bindet einen expliziten Operatornamen an den exakten `proposal_sha256`. Es akzeptiert nur einen integren, noch ausstehenden Vorschlag ohne ungelöste Felder. Die Plan-Datei wird über einen atomaren Compare-and-Swap im selben Verzeichnis ersetzt; eine zwischenzeitliche Änderung wird erkannt und ohne Überschreiben fremder Bytes abgebrochen.

Ein identischer Wiederholungsaufruf liefert den vorhandenen Reviewzustand ohne neuen Effekt. Ein anderer Reviewer, ein anderer Proposal-Hash, Symlinks oder unklare Readbacks scheitern mit stabilen Fehlercodes. Das Review erzeugt die hashgebundene `reviewed_plan`-Approval-Evidenz, verändert aber weder Registry noch Queue und veröffentlicht nichts.

### 5. `operator-task-publish`

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

Beispiel für eine Korrektur im selben Kandidaten-Lebenszyklus:

```json
{
  "schema_version": 1,
  "idempotency_key": "conversation:refinement:2",
  "title": "Korrigierte Kandidatenbeschreibung",
  "source_kind": "conversation",
  "desired_outcome": "Den vorhandenen Kandidaten präzisieren",
  "supersedes_event_id": 31
}
```

Bewertung:

```bash
bureau --json operator-candidate-assess --candidate-id candidate-...
```

Mehrdeutiger Aufnahme-Readback über den exakten Idempotenzschlüssel:

```bash
bureau --json operator-candidate-assess --idempotency-key conversation:...
```

Vorschlag in einem expliziten sauberen Registry-Checkout:

```bash
bureau --root /path/to/clean/bureau --json operator-task-propose \
  --candidate-id candidate-... \
  --task-json task.json \
  --publishing-task-id OPERATOR-MACHINE-READABILITY-V1-T017 \
  --write-plan proposal.json
```

Hashgebundenes Operator-Review:

```bash
bureau --json operator-task-review \
  --plan proposal.json \
  --reviewer "ChatGPT through Grabowski" \
  --proposal-sha256 <proposal_sha256>
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

### 6. `operator-task-ready`

Schließt ausschließlich die Readiness-Lücke für **standalone** TaskSpecs nach ihrer bereits belegten Publication. `operator-task-publish` bleibt absichtlich pre-merge und schreibt den neuen Task nur als `planned`; erst dieser zweite, mergegebundene Schritt darf genau diesen Task im autoritativen StateStore auf `ready` setzen.

Der Vertrag ist eng und fail-closed. Er verlangt:

- ein intaktes `bureau_task_publication_receipt` mit exakter Task-ID, Proposal-Digest, TaskSpec-Revision und Taskdatei-Digest;
- aktuelle Registry-Bytes und den kanonischen TaskSpec-Digest exakt wie im Publication-Receipt;
- einen frischen GitHub-Readback desselben PR mit `MERGED`, exakt demselben Head, Branch und Base `main`;
- einen standalone Task ohne `depends_on`, Parent oder Children;
- im StateStore exakt die im Receipt gebundene Revision, denselben Spec-Digest, dieselbe Spec und `state=planned`.

Die Repository-Identität stammt primär aus dem GitHub-`origin` des Registry-Checkouts. Ein immutable canonical Runtime-Snapshot besitzt absichtlich kein Git-Remote; ausschließlich wenn `bureau runtime-identity` genau diesen Snapshot als manifestgebunden, integer und `canonical-read-only` bestätigt, darf dessen gebundene `expected_repository`-Identität verwendet werden. Beliebige Nicht-Git-Verzeichnisse bleiben fail-closed.

Vor jeder Wirkung kann der vollständige Vertrag read-only geprüft werden:

```bash
bureau --json operator-task-ready \
  --publication-receipt /path/to/publication-receipt.json \
  --preview
```

Der Effekt besteht aus genau einem revisionsgebundenen CAS `planned -> ready`. Queue, Claims, Dispatch, Registry-Dateien und Runtime bleiben unverändert. Der erfolgreiche CAS wird sofort revisions- und digestgebunden zurückgelesen und anschließend in einem create-only Promotion-Receipt festgehalten:

```bash
bureau --json operator-task-ready \
  --publication-receipt /path/to/publication-receipt.json \
  --apply \
  --promotion-receipt /path/to/promotion-receipt.json
```

Ein späterer Readback oder exakter Replay akzeptiert nur dieses Receipt, wenn Publication-/Merge-Bindung und die aktuelle promovierte StateStore-Revision weiterhin exakt übereinstimmen:

```bash
bureau --json operator-task-ready \
  --publication-receipt /path/to/publication-receipt.json \
  --readback \
  --promotion-receipt /path/to/promotion-receipt.json
```

Drift bei PR, Head, Branch, Taskdatei, Registry-Spec, StateStore-Revision oder Receipt stoppt fail-closed. Eine fehlgeschlagene oder unklare CAS-/Receipt-Phase begründet keinen Blind-Retry, sondern verlangt exakten StateStore-/Receipt-Readback.

## Nichtbehauptungen

Diese Oberfläche begründet nicht:

- automatische semantische Duplikaterkennung als Wahrheit;
- Queue-, Readiness-, Claim- oder Dispatch-Autorität;
- Merge-, Deployment- oder Verifikationsautorität;
- ein Recht, Kosten-, Sicherheits-, Datenschutz-, Irreversibilitäts- oder Steuergates zu umgehen;
- die Abwesenheit gleichberechtigter Eingriffe außerhalb der belegten Git-, Lease- und Readback-Grenzen.
